"""Helmet enrichment on embeddings:results (docs/helmets/CONTRACT.md). No services needed."""

import copy

import pytest

from src.application.helpers.helmet_analysis import parse_helmet_fields
from src.db.models.constants import EmbeddingRequestStatus
from src.db.models.embedding_request import EmbeddingRequest
from src.streams import embedding_results_consumer as mod
from tests.test_embedding_zip_ref_mode import _payload, _Repo, harness  # noqa: F401

BLOCK = {
    "schema_version": 1,
    "request_id": "helmet:ev-1:v1",
    "model_version": "v3b_best",
    "images": [
        {
            "image_name": "not-embedded.jpg",
            "image_index": 0,
            # Filtered out by diversity: has no vector.
            "zip_entry": "cropped/not-embedded.jpg",
            "image_width": 82,
            "image_height": 118,
            "detections": [
                {
                    "class_name": "cabeza_sin_casco",
                    "confidence": 0.81,
                    "bbox": {"x1": 31.0, "y1": 4.0, "x2": 52.0, "y2": 27.0},
                    "moto_ratio": 0.05,
                }
            ],
        }
    ],
    "summary": {
        "has_detection": True,
        "verdict": "ALARMA",
        "alarm": True,
        "frames_sin_casco": 3,
        "max_sin_casco": 0.81,
    },
}
EMPTY = {
    "helmet_analyzed": False,
    "helmet_verdict": None,
    "helmet_alarm": None,
    "helmet_frames_sin_casco": None,
    "helmet_max_sin_casco": None,
    "helmet_analysis": None,
    "helmet_analysis_error": None,
}


def test_absent_is_not_attempted():
    assert parse_helmet_fields({}) == EMPTY


def test_analyzed():
    assert parse_helmet_fields({"helmet_analysis": BLOCK}) == {
        "helmet_analyzed": True,
        "helmet_verdict": "ALARMA",
        "helmet_alarm": True,
        "helmet_frames_sin_casco": 3,
        "helmet_max_sin_casco": 0.81,
        "helmet_analysis": BLOCK,
        "helmet_analysis_error": None,
    }


def test_no_detections_keeps_null_confidence():
    summary = {
        "verdict": "SIN_EVIDENCIA",
        "alarm": False,
        "frames_sin_casco": 0,
        "max_sin_casco": None,
    }
    out = parse_helmet_fields({"helmet_analysis": {"images": [], "summary": summary}})
    assert out["helmet_analyzed"] is True and out["helmet_alarm"] is False
    assert out["helmet_frames_sin_casco"] == 0 and out["helmet_max_sin_casco"] is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"message": "StoreDownloadError: x"}, "StoreDownloadError: x"),
        ("dead-letter tras reintentos", "dead-letter tras reintentos"),
        ({"code": 1}, "unknown"),
        ({}, None),
        (None, None),
        (7, None),
    ],
)
def test_error_shapes(raw, expected):
    assert parse_helmet_fields({"helmet_analysis_error": raw}) == {
        **EMPTY,
        "helmet_analysis_error": expected,
    }


@pytest.mark.parametrize("raw", [None, [], "x", 3, {}])
def test_malformed_block_is_ignored(raw):
    assert parse_helmet_fields({"helmet_analysis": raw}) == EMPTY


def test_malformed_summary_values_become_none():
    summary = {
        "verdict": 1,
        "alarm": "yes",
        "frames_sin_casco": "3",
        "max_sin_casco": float("nan"),
    }
    block = {"summary": summary}
    out = parse_helmet_fields({"helmet_analysis": block})
    assert out == {**EMPTY, "helmet_analyzed": True, "helmet_analysis": block}


def test_every_field_is_a_nullable_column():
    for name in EMPTY:
        assert EmbeddingRequest.__table__.columns[name].nullable is True


@pytest.mark.asyncio
async def test_consumer_persists_block_and_leaves_vectors_alone(harness):  # noqa: F811
    payload = _payload(helmet_analysis=copy.deepcopy(BLOCK), trace_event="helmets.analyzed")
    await mod._process_embeddings_result(payload, "1-0")
    row = _Repo.created[0]
    assert row.status == EmbeddingRequestStatus.EMBEDDED
    assert (row.helmet_analyzed, row.helmet_verdict, row.helmet_alarm) == (True, "ALARMA", True)
    assert row.helmet_analysis == BLOCK
    # One vector per embeddings[] item; the crop without a vector adds none.
    assert len(harness.vector_repo.stored) == 2
    assert all(not k.startswith("helmet") for e in harness.vector_repo.stored for k in e.metadata)


@pytest.mark.asyncio
async def test_consumer_keeps_vectors_when_analysis_failed(harness):  # noqa: F811
    await mod._process_embeddings_result(
        _payload(helmet_analysis_error={"message": "boom"}, trace_event="helmets.failed"), "1-0"
    )
    row = _Repo.created[0]
    assert row.status == EmbeddingRequestStatus.EMBEDDED
    assert row.helmet_analyzed is False and row.helmet_analysis_error == "boom"
    assert len(harness.vector_repo.stored) == 2


@pytest.mark.asyncio
async def test_consumer_accepts_weapons_and_helmets_together(harness):  # noqa: F811
    weapon = {"images": [], "summary": {"has_weapon": False, "classes_detected": []}}
    await mod._process_embeddings_result(
        _payload(helmet_analysis=BLOCK, weapon_analysis=weapon, trace_event="weapons.analyzed"),
        "1-0",
    )
    row = _Repo.created[0]
    assert row.weapon_analyzed is True and row.helmet_analyzed is True
