"""Crop labels from the CLIP worker's classifier (CLS-06).

* labels absent → no Qdrant payload keys (byte-identical), DB columns NULL.
* labels present → Qdrant labels / vehicle_type / vehicle_type_score /
  classifier_version, DB labels + classifier_version, DW event fields.
* labels [] → stored as [], vehicle_type None.
* malformed entries are dropped, never fail the message.

Reuses the fake-wired consumer harness of test_embedding_zip_ref_mode.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from src.application.helpers.crop_labels import (
    label_payload_fields,
    normalize_labels,
    top_label,
)
from src.infrastructure.vector_db.qdrant_repository import _EVIDENCE_PAYLOAD_INDICES
from src.services import dw_publisher_service as dw
from src.streams import embedding_results_consumer as mod
from tests.test_embedding_zip_ref_mode import VEC, _payload, harness  # noqa: F401

VERSION = "vehicle_types/v20261027-202226"
LABEL_KEYS = {"labels", "vehicle_type", "vehicle_type_score", "classifier_version"}


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_normalize_absent_is_none():
    assert normalize_labels(None) is None


def test_normalize_non_list_is_treated_as_absent():
    assert normalize_labels({"label": "bus", "score": 0.9}) is None
    assert normalize_labels("bus") is None


def test_normalize_empty_list_stays_empty():
    assert normalize_labels([]) == []


def test_normalize_sorts_by_score_and_drops_malformed():
    raw = [
        {"label": "truck", "score": 0.2},
        {"label": "bus", "score": 0.7},
        {"label": "", "score": 0.5},
        {"label": "pickup", "score": 1.5},
        {"label": "vehicle", "score": float("nan")},
        {"label": "motorcycle", "score": True},
        {"label": "car"},
        "bus",
        {"label": " motorcycle ", "score": 1},
    ]
    assert normalize_labels(raw) == [
        {"label": "motorcycle", "score": 1.0},
        {"label": "bus", "score": 0.7},
        {"label": "truck", "score": 0.2},
    ]


def test_top_label():
    assert top_label(None) == (None, None)
    assert top_label([]) == (None, None)
    assert top_label([{"label": "bus", "score": 0.9}, {"label": "truck", "score": 0.1}]) == (
        "bus",
        0.9,
    )


def test_label_payload_fields():
    assert label_payload_fields(None, VERSION) == {}
    assert label_payload_fields([], VERSION) == {
        "labels": [],
        "vehicle_type": None,
        "vehicle_type_score": None,
        "classifier_version": VERSION,
    }


def test_vehicle_type_is_keyword_indexed():
    assert ("vehicle_type", "keyword") in _EVIDENCE_PAYLOAD_INDICES


# ── Consumer ─────────────────────────────────────────────────────────────────


def _labelled_payload(**overrides) -> dict:
    payload = _payload(
        classifier_version=VERSION,
        embeddings=[
            {
                "image_name": "a.jpg",
                "zip_entry": "cropped/a.jpg",
                "image_index": 0,
                "vector": VEC,
                "labels": [{"label": "bus", "score": 0.97}],
            },
            {
                "image_name": "b.jpg",
                "zip_entry": "cropped/b.jpg",
                "image_index": 1,
                "vector": VEC,
                "labels": [],
            },
            {"image_name": "c.jpg", "zip_entry": "cropped/c.jpg", "image_index": 2, "vector": VEC},
        ],
    )
    payload.update(overrides)
    return payload


async def test_no_labels_keeps_payload_and_columns_unchanged(harness):  # noqa: F811
    await mod._process_embeddings_result(_payload(), "1-0")

    for emb in harness.vector_repo.stored:
        assert not LABEL_KEYS & emb.metadata.keys()
    for rec in harness.added:
        assert rec.labels is None
        assert rec.classifier_version is None


async def test_labels_land_on_qdrant_and_db(harness):  # noqa: F811
    await mod._process_embeddings_result(_labelled_payload(), "1-0")

    labelled, empty, absent = harness.vector_repo.stored
    assert labelled.metadata["labels"] == [{"label": "bus", "score": 0.97}]
    assert labelled.metadata["vehicle_type"] == "bus"
    assert labelled.metadata["vehicle_type_score"] == 0.97
    assert labelled.metadata["classifier_version"] == VERSION

    assert empty.metadata["labels"] == []
    assert empty.metadata["vehicle_type"] is None
    assert empty.metadata["classifier_version"] == VERSION

    assert not LABEL_KEYS & absent.metadata.keys()

    rec_labelled, rec_empty, rec_absent = harness.added
    assert rec_labelled.labels == [{"label": "bus", "score": 0.97}]
    assert rec_labelled.classifier_version == VERSION
    assert rec_empty.labels == []
    assert rec_empty.classifier_version == VERSION
    assert rec_absent.labels is None
    assert rec_absent.classifier_version is None
    # json_data mirrors the Qdrant payload.
    assert rec_labelled.json_data["vehicle_type"] == "bus"


async def test_malformed_labels_never_drop_the_vector(harness):  # noqa: F811
    payload = _labelled_payload(
        embeddings=[
            {
                "image_name": "a.jpg",
                "zip_entry": "cropped/a.jpg",
                "image_index": 0,
                "vector": VEC,
                "labels": "not-a-list",
            },
        ],
    )
    await mod._process_embeddings_result(payload, "1-0")

    (emb,) = harness.vector_repo.stored
    (rec,) = harness.added
    assert not LABEL_KEYS & emb.metadata.keys()
    assert rec.labels is None


async def test_blank_classifier_version_is_null(harness):  # noqa: F811
    await mod._process_embeddings_result(_labelled_payload(classifier_version="  "), "1-0")
    rec = harness.added[0]
    assert rec.labels == [{"label": "bus", "score": 0.97}]
    assert rec.classifier_version is None


# ── DW event ─────────────────────────────────────────────────────────────────


def _row(**kw):
    base = {
        "id": uuid4(),
        "request_id": uuid4(),
        "qdrant_point_id": str(uuid4()),
        "image_index": 0,
        "image_url": "https://api.lookia.dev/api/v1/frames/x",
        "weapon_detections": None,
        "json_data": {},
        "created_at": datetime(2026, 10, 28),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _capture(monkeypatch):
    producer = MagicMock()
    monkeypatch.setattr(dw, "_stream_producer", producer)
    return producer


def _published_payload(producer) -> dict:
    kwargs = producer.publish.call_args.kwargs
    return kwargs["payload"]


def test_upserted_event_carries_labels(monkeypatch):
    producer = _capture(monkeypatch)
    dw.publish_image_embedding(
        _row(
            labels=[{"label": "truck", "score": 0.8}, {"label": "bus", "score": 0.2}],
            classifier_version=VERSION,
        )
    )
    payload = _published_payload(producer)
    assert payload["labels"] == [{"label": "truck", "score": 0.8}, {"label": "bus", "score": 0.2}]
    assert payload["vehicle_type"] == "truck"
    assert payload["vehicle_type_score"] == 0.8
    assert payload["classifier_version"] == VERSION


def test_upserted_event_without_labels_has_null_keys(monkeypatch):
    producer = _capture(monkeypatch)
    dw.publish_image_embedding(_row())
    payload = _published_payload(producer)
    assert payload["labels"] is None
    assert payload["vehicle_type"] is None
    assert payload["vehicle_type_score"] is None
    assert payload["classifier_version"] is None
