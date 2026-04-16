"""Pure unit tests for src.application.helpers.weapon_report_events.

No infrastructure needed. Validates that the WeaponsDetectedEvent builder
produces the exact shape documented in
docs/requirements/REPORT_GENERATION_STREAMS.md §2.3.
"""

from datetime import datetime

from src.application.helpers.weapon_report_events import (
    WEAPONS_DETECTED_EVENT_TYPE,
    build_weapons_detected_event,
)

# ── Fixtures ────────────────────────────────────────────────────────────────

FIXED_TS = datetime(2026, 4, 15, 22, 38, 49, 505882)
EVIDENCE_ID = "cead4516-3dcc-43b0-aebb-8fab34f8800d"
CAMERA_ID = "54c398ab-ea0d-4085-875b-af816eb00b03"
USER_ID = "3996d660-99c2-4c9e-bda6-4a5c2be7906e"
DEVICE_ID = "1d7d50b3-b97e-4725-b044-e2de4624d2e5"

FULL_SUMMARY = {
    "has_weapon": True,
    "total_detections": 5,
    "classes_detected": ["persona_armada"],
    "max_confidence": 0.82,
    "images_analyzed": 7,
    "images_with_detections": 5,
    "model_version": {
        "model_a": "persona_armada_1label_V1",
        "model_b": "armas_v202",
        "person_model": "yolo11s",
    },
}


def _image(name: str, idx: int, *, confidence: float = 0.8) -> dict:
    return {
        "image_name": name,
        "image_index": idx,
        "image_url": f"https://minio.lookia.mx/test/{name}",
        "detections": [
            {
                "class_name": "persona_armada",
                "class_id": 0,
                "confidence": confidence,
                "source": "model_a",
                "bbox": {"x1": 10, "y1": 20, "x2": 100, "y2": 200},
            }
        ],
    }


# ── Constants ───────────────────────────────────────────────────────────────


def test_event_type_constant_matches_contract():
    """The event_type string is load-bearing — it's the handler key on
    the receiver side. Must match docs/requirements/REPORT_GENERATION_STREAMS.md."""
    assert WEAPONS_DETECTED_EVENT_TYPE == "weapons.detected"


# ── Full event shape ───────────────────────────────────────────────────────


def test_full_event_matches_contract_shape():
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="TEST-001",
        weapon_summary=FULL_SUMMARY,
        images_with_detections=[_image("frame_001.jpg", 0)],
        detected_at=FIXED_TS,
    )

    # Top-level identity fields
    assert event["evidence_id"] == EVIDENCE_ID
    assert event["camera_id"] == CAMERA_ID
    assert event["user_id"] == USER_ID
    assert event["device_id"] == DEVICE_ID
    assert event["app_id"] == 1
    assert event["infraction_code"] == "TEST-001"
    assert event["detected_at"] == "2026-04-15T22:38:49.505882Z"

    # Summary
    s = event["summary"]
    assert s["has_weapon"] is True
    assert s["total_detections"] == 5
    assert s["classes_detected"] == ["persona_armada"]
    assert s["max_confidence"] == 0.82
    assert s["images_analyzed"] == 7
    assert s["images_with_detections"] == 1  # Builder recomputes from images[]
    assert s["model_version"] == FULL_SUMMARY["model_version"]

    # Images
    assert len(event["images"]) == 1
    img = event["images"][0]
    assert img["image_name"] == "frame_001.jpg"
    assert img["image_index"] == 0
    assert img["image_url"].startswith("https://minio.lookia.mx/")
    assert len(img["detections"]) == 1
    d = img["detections"][0]
    assert d["class_name"] == "persona_armada"
    assert d["bbox"] == {"x1": 10, "y1": 20, "x2": 100, "y2": 200}
    assert d["source"] == "model_a"


def test_multiple_images_preserve_order():
    images = [
        _image("a.jpg", 1, confidence=0.70),
        _image("b.jpg", 2, confidence=0.85),
        _image("c.jpg", 3, confidence=0.78),
    ]
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=None,
        device_id=None,
        app_id=None,
        infraction_code=None,
        weapon_summary=FULL_SUMMARY,
        images_with_detections=images,
    )
    assert [img["image_name"] for img in event["images"]] == ["a.jpg", "b.jpg", "c.jpg"]
    assert event["summary"]["images_with_detections"] == 3


# ── Multi-tenant None handling ─────────────────────────────────────────────


def test_none_multitenant_fields_preserved():
    """Missing multi-tenant fields pass through as None — the receiver can
    distinguish 'missing' from 'empty string'."""
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=None,
        device_id=None,
        app_id=None,
        infraction_code=None,
        weapon_summary=FULL_SUMMARY,
        images_with_detections=[_image("f.jpg", 0)],
        detected_at=FIXED_TS,
    )
    assert event["user_id"] is None
    assert event["device_id"] is None
    assert event["app_id"] is None
    assert event["infraction_code"] is None


# ── Summary fallbacks (when producer didn't send a full summary) ───────────


def test_summary_none_uses_computed_fallbacks():
    """If summary is None, the builder derives fields from the images list."""
    images = [_image("a.jpg", 0, confidence=0.7), _image("b.jpg", 1, confidence=0.9)]
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary=None,
        images_with_detections=images,
    )
    s = event["summary"]
    assert s["total_detections"] == 2  # 1 detection per image
    assert s["classes_detected"] == ["persona_armada"]
    assert s["max_confidence"] == 0.9
    assert s["images_analyzed"] == 2
    assert s["images_with_detections"] == 2
    assert "model_version" not in s  # None summary → no model_version


def test_summary_empty_dict_uses_fallbacks():
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary={},
        images_with_detections=[_image("a.jpg", 0)],
    )
    assert event["summary"]["total_detections"] == 1


def test_summary_without_model_version_omits_key():
    summary = {**FULL_SUMMARY}
    del summary["model_version"]
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary=summary,
        images_with_detections=[_image("a.jpg", 0)],
    )
    assert "model_version" not in event["summary"]


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_empty_images_list_returns_event_with_empty_array():
    """Callers must pre-filter to only frames with detections — the builder
    trusts its input. An empty list still produces a valid event dict (caller
    decides whether to publish it)."""
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary=FULL_SUMMARY,
        images_with_detections=[],
    )
    assert event["images"] == []
    assert event["summary"]["images_with_detections"] == 0


def test_detected_at_defaults_to_now_when_omitted():
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary=FULL_SUMMARY,
        images_with_detections=[_image("a.jpg", 0)],
    )
    # ISO-8601 UTC with trailing Z
    assert event["detected_at"].endswith("Z")
    # Parseable back to a datetime (drop the trailing Z first)
    parsed = datetime.fromisoformat(event["detected_at"].rstrip("Z"))
    assert isinstance(parsed, datetime)


def test_detected_at_format_has_trailing_z():
    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary=FULL_SUMMARY,
        images_with_detections=[_image("a.jpg", 0)],
        detected_at=FIXED_TS,
    )
    assert event["detected_at"] == "2026-04-15T22:38:49.505882Z"


# ── JSON-serializability ────────────────────────────────────────────────────


def test_event_is_json_serializable():
    """StreamProducer calls json.dumps on the payload — the event must
    contain only JSON-safe types (no datetime objects, no sets, etc.)."""
    import json

    event = build_weapons_detected_event(
        evidence_id=EVIDENCE_ID,
        camera_id=CAMERA_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        app_id=1,
        infraction_code="T",
        weapon_summary=FULL_SUMMARY,
        images_with_detections=[_image("a.jpg", 0)],
        detected_at=FIXED_TS,
    )
    serialized = json.dumps(event)
    assert "weapons.detected" not in serialized  # Envelope key is separate
    assert EVIDENCE_ID in serialized
    # Round-trip
    parsed = json.loads(serialized)
    assert parsed["evidence_id"] == EVIDENCE_ID
