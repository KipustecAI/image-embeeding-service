"""Unit tests for src.services.dw_publisher_service.

Tests the 7 publish functions against a fake producer — no Redis, no
DB needed. Locks the contract:

  * Stream name, event_type, MAXLEN per call
  * Exact payload shape per the wire-format authority
    (docs/requirements/LOOKIA_DW_STREAMS.md §4.x)
  * PII guard — `name` is never in the blacklist_image_entry payload
  * Graceful skip when producer is unwired (no exception)
  * Empty-matches search → no event (contract §4.2)
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from src.infrastructure.config import get_settings
from src.services import dw_publisher_service as dw

settings = get_settings()


@pytest.fixture
def fake_producer():
    """Replace the module-level producer with a MagicMock; restore after."""
    original = dw._stream_producer
    p = MagicMock()
    dw.set_dw_publisher_producer(p)
    yield p
    dw._stream_producer = original


def _ns(**kwargs):
    """Tiny helper — a row-like namespace for tests."""
    return SimpleNamespace(**kwargs)


# ── No-producer behavior ──────────────────────────────────────────────────


def test_publish_no_producer_is_silent_skip(caplog):
    """Misconfigured deploy — log a warning, do not raise."""
    import logging

    dw._stream_producer = None
    caplog.set_level(logging.WARNING, logger="src.services.dw_publisher_service")
    row = _ns(
        id=uuid4(),
        search_id="x",
        user_id="u",
        image_url="https://x/y",
        status=1,
        similarity_status=1,
        threshold=0.75,
        max_results=50,
        search_metadata={},
        total_matches=0,
        results_key=None,
        qdrant_query_point_id=None,
        worker_id=None,
        error_message=None,
        retry_count=0,
        processing_started_at=None,
        processing_completed_at=None,
        stream_message_id=None,
        created_at=datetime(2026, 5, 16),
        updated_at=datetime(2026, 5, 16),
    )
    dw.publish_image_search_request("image_search.created", row)
    assert any("DW publisher not configured" in rec.message for rec in caplog.records)


# ── image_search_request:raw ──────────────────────────────────────────────


def test_image_search_request_publish_shape(fake_producer):
    row_id = uuid4()
    row = _ns(
        id=row_id,
        search_id="search-abc",
        user_id="user-1",
        image_url="https://x.test/q.jpg",
        status=3,
        similarity_status=2,
        threshold=0.75,
        max_results=50,
        search_metadata={"camera_id": "cam-1"},
        total_matches=7,
        results_key=None,
        qdrant_query_point_id="qp-1",
        worker_id=None,
        error_message=None,
        retry_count=0,
        processing_started_at=datetime(2026, 5, 14, 20, 47, 7),
        processing_completed_at=datetime(2026, 5, 14, 20, 47, 14),
        stream_message_id="1778791622621-0",
        created_at=datetime(2026, 5, 14, 20, 47, 2),
        updated_at=datetime(2026, 5, 14, 20, 47, 14),
    )

    dw.publish_image_search_request("image_search.completed", row)

    fake_producer.publish.assert_called_once()
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_image_search_request
    assert kwargs["event_type"] == "image_search.completed"
    assert kwargs["maxlen"] == settings.dw_maxlen_image_search_request
    p = kwargs["payload"]
    assert p["id"] == str(row_id)
    assert p["search_id"] == "search-abc"
    assert p["status"] == 3
    assert p["similarity_status"] == 2
    assert p["total_matches"] == 7
    # Datetimes serialized as ISO strings
    assert p["created_at"] == "2026-05-14T20:47:02"
    assert p["processing_completed_at"] == "2026-05-14T20:47:14"


# ── image_search_match:raw ────────────────────────────────────────────────


def test_image_search_matches_payload_shape(fake_producer):
    req_id = uuid4()
    match_id = uuid4()
    matches = [
        _ns(
            id=match_id,
            evidence_id="ev-1",
            camera_id="cam-1",
            similarity_score=0.876,
            image_url="https://x/ev1.jpg",
            match_metadata={"image_index": 3},
        )
    ]
    dw.publish_image_search_matches(
        search_request_id=req_id,
        user_id="user-1",
        image_url="https://x/q.jpg",
        total_matches=1,
        matches=matches,
        timestamp=datetime(2026, 5, 14, 20, 47, 14),
    )

    fake_producer.publish.assert_called_once()
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_image_search_match
    assert kwargs["event_type"] == "image_search.matched"
    p = kwargs["payload"]
    assert p["search_request_id"] == str(req_id)
    assert p["total_matches"] == 1
    assert len(p["matches"]) == 1
    assert p["matches"][0]["id"] == str(match_id)
    assert p["matches"][0]["similarity_score"] == 0.876
    assert p["timestamp"] == "2026-05-14T20:47:14"


def test_empty_matches_does_not_publish(fake_producer):
    """Contract §4.2: skip empty-result searches."""
    dw.publish_image_search_matches(
        search_request_id=uuid4(),
        user_id="user-1",
        image_url="https://x/q.jpg",
        total_matches=0,
        matches=[],
    )
    fake_producer.publish.assert_not_called()


# ── blacklist_image_entry:raw — PII guard ─────────────────────────────────


def test_blacklist_image_entry_never_includes_raw_name(fake_producer):
    """⚠️ PII REGRESSION GUARD ⚠️

    The contract REQUIRES that `name` never appears on the wire — only
    `name_hash`. A regression here is a privacy incident. Fail loudly.
    """
    row = _ns(
        id=uuid4(),
        name="Suspect: Juan Pérez (case CRIM-2025-042)",  # contains PII
        category="vehicle",
        status=3,
        active=True,
        is_archived=False,
        user_id="u-1",
        blacklist_version=1,
        match_threshold=0.88,
        json_data={"case_id": "CRIM-2025-042"},
        created_at=datetime(2026, 5, 16),
        updated_at=datetime(2026, 5, 16),
    )

    dw.publish_blacklist_image_entry(row)

    fake_producer.publish.assert_called_once()
    payload = fake_producer.publish.call_args.kwargs["payload"]

    # Hash is present, length-locked.
    assert "name_hash" in payload
    assert len(payload["name_hash"]) == 16
    # Raw `name` field MUST NOT exist on the wire.
    assert "name" not in payload, (
        "PII REGRESSION: raw `name` leaked to wire. The DW must never receive raw blacklist names."
    )
    # No substring of the raw name leaks elsewhere.
    serialized = str(payload)
    assert "Juan" not in serialized
    assert "Pérez" not in serialized
    assert "Suspect" not in serialized


def test_blacklist_image_entry_shape(fake_producer):
    row = _ns(
        id=uuid4(),
        name="test",
        category="vehicle",
        status=3,
        active=True,
        is_archived=False,
        user_id="u-1",
        blacklist_version=2,
        match_threshold=0.9,
        json_data={"k": "v"},
        created_at=datetime(2026, 5, 16),
        updated_at=datetime(2026, 5, 16),
    )
    dw.publish_blacklist_image_entry(row)
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_blacklist_image_entry
    assert kwargs["event_type"] == "blacklist_image_entry.upserted"
    assert kwargs["maxlen"] == settings.dw_maxlen_blacklist_image_entry
    p = kwargs["payload"]
    assert p["blacklist_version"] == 2
    assert p["active"] is True
    assert p["json_data"] == {"k": "v"}


# ── blacklist_image_reference:raw ─────────────────────────────────────────


def test_blacklist_image_reference_shape(fake_producer):
    ref = _ns(
        id=uuid4(),
        entry_id=uuid4(),
        image_url="https://x/r.jpg",
        image_type="reference",
        status=3,
        error_message=None,
        retry_count=0,
        json_data={"dims": [1920, 1080]},
        created_at=datetime(2026, 5, 16),
        updated_at=datetime(2026, 5, 16),
    )
    dw.publish_blacklist_image_reference(ref)
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_blacklist_image_reference
    assert kwargs["event_type"] == "blacklist_image_reference.upserted"
    p = kwargs["payload"]
    assert p["status"] == 3
    assert p["image_type"] == "reference"


# ── blacklist_image_embedding:raw ─────────────────────────────────────────


def test_blacklist_image_embedding_shape(fake_producer):
    emb = _ns(
        id=uuid4(),
        entry_id=uuid4(),
        reference_id=uuid4(),
        qdrant_point_id="qp-abc",
        model_version="clip-vit-b-32",
        json_data={},
        created_at=datetime(2026, 5, 16),
    )
    dw.publish_blacklist_image_embedding(emb)
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_blacklist_image_embedding
    assert kwargs["event_type"] == "blacklist_image_embedding.created"
    p = kwargs["payload"]
    assert p["model_version"] == "clip-vit-b-32"
    assert p["qdrant_point_id"] == "qp-abc"


# ── image_embedding_request:raw (Tier 3) ──────────────────────────────────


def test_image_embedding_request_carries_weapon_fields(fake_producer):
    """High-value: weapon fields must surface in the .completed payload
    since DW dropped the separate `.weapon_analyzed` event."""
    row = _ns(
        id=uuid4(),
        evidence_id="ev-1",
        camera_id="cam-1",
        user_id="u-1",
        device_id="d-1",
        app_id=1,
        infraction_code="INF-1",
        category="[2]",
        status=3,
        image_urls=["https://x/1.jpg"],
        worker_id=None,
        error_message=None,
        retry_count=0,
        processing_started_at=datetime(2026, 5, 14, 20, 47, 7),
        processing_completed_at=datetime(2026, 5, 14, 20, 47, 7, 310000),
        stream_message_id="1778791622621-0",
        weapon_analyzed=True,
        has_weapon=True,
        weapon_classes=["arma_fuego"],
        weapon_max_confidence=0.7707,
        weapon_summary={"total_detections": 2},
        weapon_analysis_error=None,
        created_at=datetime(2026, 5, 14, 20, 47, 7),
        updated_at=datetime(2026, 5, 14, 20, 47, 7, 310000),
    )
    dw.publish_image_embedding_request("image_embedding_request.completed", row)
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_image_embedding_request
    assert kwargs["event_type"] == "image_embedding_request.completed"
    assert kwargs["maxlen"] == settings.dw_maxlen_image_embedding_request
    p = kwargs["payload"]
    assert p["weapon_analyzed"] is True
    assert p["has_weapon"] is True
    assert p["weapon_classes"] == ["arma_fuego"]
    assert p["weapon_max_confidence"] == 0.7707
    assert p["status"] == 3


# ── image_embedding:raw (Tier 3) ──────────────────────────────────────────


def test_image_embedding_carries_weapon_detections(fake_producer):
    """Per-image granularity — `weapon_detections` is the load-bearing
    field for the DW's per-image weapons analytics."""
    rec = _ns(
        id=uuid4(),
        request_id=uuid4(),
        qdrant_point_id="qp-img-1",
        image_index=3,
        image_url="https://x/frame_3.jpg",
        weapon_detections=[{"class_name": "arma_fuego", "confidence": 0.77}],
        json_data={"model_version": "clip-vit-b-32"},
        created_at=datetime(2026, 5, 14, 20, 47, 7, 310000),
    )
    dw.publish_image_embedding(rec)
    kwargs = fake_producer.publish.call_args.kwargs
    assert kwargs["stream"] == settings.dw_stream_image_embedding
    assert kwargs["event_type"] == "image_embedding.upserted"
    assert kwargs["maxlen"] == settings.dw_maxlen_image_embedding
    p = kwargs["payload"]
    assert p["weapon_detections"][0]["class_name"] == "arma_fuego"
    assert p["image_index"] == 3
    assert p["qdrant_point_id"] == "qp-img-1"


def test_image_embedding_null_weapon_detections(fake_producer):
    """Clean image — weapon_detections is None; payload must carry None."""
    rec = _ns(
        id=uuid4(),
        request_id=uuid4(),
        qdrant_point_id="qp-img-0",
        image_index=0,
        image_url="https://x/frame_0.jpg",
        weapon_detections=None,
        json_data=None,
        created_at=datetime(2026, 5, 14),
    )
    dw.publish_image_embedding(rec)
    p = fake_producer.publish.call_args.kwargs["payload"]
    assert p["weapon_detections"] is None


# ── UUID / datetime serialization helpers ─────────────────────────────────


def test_uuid_objects_are_serialized_as_strings(fake_producer):
    """Pydantic / JSON expects strings on the wire. The publisher's
    _uuid_str helper handles both UUID objects and pre-stringified ids."""
    real_uuid = UUID("11111111-2222-3333-4444-555555555555")
    row = _ns(
        id=real_uuid,
        search_id="s",
        user_id="u",
        image_url="x",
        status=1,
        similarity_status=1,
        threshold=0.75,
        max_results=50,
        search_metadata=None,
        total_matches=0,
        results_key=None,
        qdrant_query_point_id=None,
        worker_id=None,
        error_message=None,
        retry_count=0,
        processing_started_at=None,
        processing_completed_at=None,
        stream_message_id=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    dw.publish_image_search_request("image_search.created", row)
    p = fake_producer.publish.call_args.kwargs["payload"]
    assert p["id"] == "11111111-2222-3333-4444-555555555555"
    assert isinstance(p["id"], str)
