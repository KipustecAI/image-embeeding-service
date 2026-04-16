"""Builder for report-generation stream events.

Produces the `WeaponsDetectedEvent` payload documented in
`docs/requirements/REPORT_GENERATION_STREAMS.md §2.3`. The builder is a pure
function with no side effects so it can be unit-tested without Redis, a DB,
or the running consumer.

The caller is responsible for publishing — see
`src/streams/embedding_results_consumer.py` for the wiring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# The event_type string for the XADD envelope.
WEAPONS_DETECTED_EVENT_TYPE = "weapons.detected"


def build_weapons_detected_event(
    *,
    evidence_id: str,
    camera_id: str,
    user_id: str | None,
    device_id: str | None,
    app_id: int | None,
    infraction_code: str | None,
    weapon_summary: dict[str, Any] | None,
    images_with_detections: list[dict[str, Any]],
    detected_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the WeaponsDetectedEvent payload.

    Args:
        evidence_id, camera_id, user_id, device_id, app_id, infraction_code:
            Multi-tenant / identity fields from the upstream
            `embeddings:results` payload. `None` values are preserved as-is
            so the receiver can distinguish "missing" from "empty".
        weapon_summary: The `weapon_analysis.summary` block. May be `None`
            or empty dict if the producer didn't send one; the builder
            fills sane defaults.
        images_with_detections: A list of dicts with keys `image_name`,
            `image_index`, `image_url`, `detections` — **already filtered
            to only frames with at least one detection**. The builder does
            NOT filter again; callers must pre-filter.
        detected_at: Optional explicit timestamp for testing. Defaults to
            now (UTC) at call time. Caller-visible so tests can pin a
            deterministic value.

    Returns:
        A JSON-serializable dict ready to pass to `StreamProducer.publish`.

    The returned shape matches `docs/requirements/REPORT_GENERATION_STREAMS.md`
    exactly. If you change field names or add fields, update that doc and
    coordinate with the report-generation team.
    """
    if detected_at is None:
        detected_at = datetime.utcnow()

    summary = weapon_summary or {}

    # Count helpers — prefer producer-supplied values, fall back to derived.
    total_detections = summary.get("total_detections")
    if total_detections is None:
        total_detections = sum(len(img.get("detections") or []) for img in images_with_detections)

    classes_detected = summary.get("classes_detected")
    if classes_detected is None:
        classes_detected = sorted(
            {
                d.get("class_name")
                for img in images_with_detections
                for d in (img.get("detections") or [])
                if d.get("class_name")
            }
        )

    max_confidence = summary.get("max_confidence")
    if max_confidence is None:
        all_conf = [
            d.get("confidence")
            for img in images_with_detections
            for d in (img.get("detections") or [])
            if isinstance(d.get("confidence"), (int, float))
        ]
        max_confidence = max(all_conf) if all_conf else None

    images_analyzed = summary.get("images_analyzed")
    if images_analyzed is None:
        # Fall back to the count we know about — frames with detections.
        # This undercounts analyzed-clean frames, but is the best we can do
        # without the full producer summary.
        images_analyzed = len(images_with_detections)

    event_summary = {
        "has_weapon": bool(summary.get("has_weapon", True)),
        "total_detections": int(total_detections or 0),
        "classes_detected": list(classes_detected or []),
        "max_confidence": max_confidence,
        "images_analyzed": int(images_analyzed or 0),
        "images_with_detections": len(images_with_detections),
    }

    # model_version is an optional pass-through from the producer summary.
    model_version = summary.get("model_version")
    if model_version:
        event_summary["model_version"] = model_version

    return {
        "evidence_id": evidence_id,
        "camera_id": camera_id,
        "user_id": user_id,
        "device_id": device_id,
        "app_id": app_id,
        "infraction_code": infraction_code,
        "detected_at": detected_at.isoformat() + "Z",
        "summary": event_summary,
        "images": [
            {
                "image_name": img.get("image_name"),
                "image_index": img.get("image_index"),
                "image_url": img.get("image_url"),
                "detections": list(img.get("detections") or []),
            }
            for img in images_with_detections
        ],
    }
