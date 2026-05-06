"""Pure unit tests for src.application.helpers.blacklist_match_events.

The builder is the single source of truth for the
``image:blacklist_match`` wire shape (see
docs/requirements/REPORT_GENERATION_STREAMS.md §3). Mirrors the
test_weapon_report_events pattern for symmetry.

A regression here means report-generation receives a malformed event
and silently drops or misattributes the report — the failure is
invisible from our side, so test exhaustively.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.application.helpers.blacklist_match_events import (
    IMAGE_BLACKLIST_MATCH_EVENT_TYPE,
    build_blacklist_match_event,
)

# ── Realistic kwargs used across multiple tests ────────────────────────────


_BASE_KWARGS = {
    "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
    "blacklist_entry_id": "a1b2c3d4-1111-2222-3333-444444444444",
    "blacklist_entry_name": "Placa del sospechoso",
    "blacklist_entry_category": "vehicle",
    "blacklist_entry_version": 1,
    "blacklist_reference_id": "e5f6a7b8-1111-2222-3333-444444444444",
    "blacklist_reference_url": "https://minio.lookia.mx/blacklist/ref_1.jpg",
    "blacklist_model_version": "clip-vit-b-32",
    "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
    "evidence_camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
    "evidence_device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
    "evidence_app_id": 1,
    "evidence_infraction_code": "SMVV8UGE_116_875_20260410100151",
    "evidence_category": "[2]",
    "matched_image_url": "https://minio.lookia.mx/embeddings/frame_3.jpg",
    "matched_image_index": 3,
    "matched_qdrant_point_id": "326b5f83-6d88-4c34-b82d-c46ccaa104b1",
    "similarity_score": 0.893,
    "threshold_used": 0.85,
    "trigger": "inline",
}


# ── Contract surface ───────────────────────────────────────────────────────


def test_event_type_constant_matches_contract():
    """If the event_type drifts the receiver's consumer group filter
    silently misses every event. Lock the literal value."""
    assert IMAGE_BLACKLIST_MATCH_EVENT_TYPE == "image.blacklist_match"


def test_full_event_matches_contract_shape():
    """One end-to-end shape check against the schema in
    REPORT_GENERATION_STREAMS.md §3.3 — every field present, no extras."""
    fixed_at = datetime(2026, 4, 18, 14, 22, 3, 501000)
    event = build_blacklist_match_event(matched_at=fixed_at, **_BASE_KWARGS)

    expected_keys = {
        "user_id",
        "blacklist_entry_id",
        "blacklist_entry_name",
        "blacklist_entry_category",
        "blacklist_entry_version",
        "blacklist_reference_id",
        "blacklist_reference_url",
        "blacklist_model_version",
        "evidence_id",
        "evidence_camera_id",
        "evidence_device_id",
        "evidence_app_id",
        "evidence_infraction_code",
        "evidence_category",
        "matched_image_url",
        "matched_image_index",
        "matched_qdrant_point_id",
        "similarity_score",
        "threshold_used",
        "trigger",
        "matched_at",
    }
    assert set(event.keys()) == expected_keys


# ── Trigger values ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("trigger", ["inline", "reverse_search"])
def test_both_trigger_values_pass_through_unchanged(trigger):
    """Receiver branches on this field — must echo verbatim."""
    kwargs = {**_BASE_KWARGS, "trigger": trigger}
    event = build_blacklist_match_event(**kwargs)
    assert event["trigger"] == trigger


# ── Optional / null fields ─────────────────────────────────────────────────


def test_none_evidence_fields_preserved():
    """Multi-tenant fields can be null (e.g. legacy evidence missing
    device_id). Don't coerce to empty string — the receiver may render
    differently for null vs empty."""
    kwargs = {
        **_BASE_KWARGS,
        "evidence_camera_id": None,
        "evidence_device_id": None,
        "evidence_app_id": None,
        "evidence_infraction_code": None,
        "evidence_category": None,
    }
    event = build_blacklist_match_event(**kwargs)
    assert event["evidence_camera_id"] is None
    assert event["evidence_device_id"] is None
    assert event["evidence_app_id"] is None
    assert event["evidence_infraction_code"] is None
    assert event["evidence_category"] is None


def test_none_blacklist_category_preserved():
    """Entry category is optional — preserve null, don't fall back to ""."""
    kwargs = {**_BASE_KWARGS, "blacklist_entry_category": None}
    event = build_blacklist_match_event(**kwargs)
    assert event["blacklist_entry_category"] is None


# ── matched_at timestamp ──────────────────────────────────────────────────


def test_matched_at_defaults_to_now_when_omitted():
    """Production callers don't pin matched_at — it should default to
    'now' so the event isn't time-traveling."""
    before = datetime.utcnow()
    event = build_blacklist_match_event(**_BASE_KWARGS)
    after = datetime.utcnow()

    # Strip the trailing Z and the ISO marker for parsing
    parsed = datetime.fromisoformat(event["matched_at"].rstrip("Z"))
    assert before <= parsed <= after


def test_matched_at_format_has_trailing_z():
    """ISO-8601 with explicit UTC 'Z' suffix — receivers parse on this."""
    event = build_blacklist_match_event(**_BASE_KWARGS)
    assert event["matched_at"].endswith("Z")


def test_matched_at_explicit_value_is_serialized():
    fixed_at = datetime(2026, 4, 18, 14, 22, 3, 501000)
    event = build_blacklist_match_event(matched_at=fixed_at, **_BASE_KWARGS)
    assert event["matched_at"] == "2026-04-18T14:22:03.501000Z"


# ── Numeric fields ─────────────────────────────────────────────────────────


def test_blacklist_entry_version_is_int_not_string():
    """Receiver dedups on (evidence_id, entry_id, version). String "1"
    and int 1 hash differently — would silently break the dedup key."""
    event = build_blacklist_match_event(**_BASE_KWARGS)
    assert isinstance(event["blacklist_entry_version"], int)
    assert event["blacklist_entry_version"] == 1


def test_similarity_score_and_threshold_are_floats():
    event = build_blacklist_match_event(**_BASE_KWARGS)
    assert isinstance(event["similarity_score"], float)
    assert isinstance(event["threshold_used"], float)


def test_matched_image_index_is_int():
    event = build_blacklist_match_event(**_BASE_KWARGS)
    assert isinstance(event["matched_image_index"], int)


# ── Categories propagate independently ─────────────────────────────────────


def test_entry_and_evidence_categories_are_independent():
    """One entry tagged 'vehicle', evidence tagged '[5,2]' — the event
    must echo both verbatim. Common bug shape: one field overwrites
    the other when reusing variable names."""
    kwargs = {
        **_BASE_KWARGS,
        "blacklist_entry_category": "vehicle",
        "evidence_category": "[5,2]",
    }
    event = build_blacklist_match_event(**kwargs)
    assert event["blacklist_entry_category"] == "vehicle"
    assert event["evidence_category"] == "[5,2]"


# ── JSON-serializability ───────────────────────────────────────────────────


def test_event_is_json_serializable():
    """The publisher does json.dumps on the payload — anything not
    naively serializable (datetime, UUID, set) breaks the publish."""
    event = build_blacklist_match_event(**_BASE_KWARGS)
    # Should not raise
    serialized = json.dumps(event)
    # Round-trip back and verify trigger preserved
    parsed = json.loads(serialized)
    assert parsed["trigger"] == "inline"
