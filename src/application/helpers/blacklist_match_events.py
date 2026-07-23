"""Builder for `image:blacklist_match` stream events.

Produces the ``ImageBlacklistMatchEvent`` payload documented in
``docs/requirements/REPORT_GENERATION_STREAMS.md §3``. Pure function with
no side effects so it can be unit-tested without Redis, a DB, or Qdrant.

Both code paths that detect a match (the inline check after a new
evidence is ingested, and the reverse-search job that runs after a new
blacklist reference is embedded) call this builder via
``BlacklistMatchService`` so the wire shape stays in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

# The event_type string for the XADD envelope. Kept here (not in
# blacklist_match_service) so unit tests can assert the contract value
# without importing the publishing machinery.
IMAGE_BLACKLIST_MATCH_EVENT_TYPE = "image.blacklist_match"

# Whether the match fired from the inline path (new evidence matched
# existing blacklist), the reverse-search path (new blacklist matched
# historical evidence), or the image-index cross-reference path (Capability B —
# blacklist matched an on-demand indexed image). Receiver can phrase alerts
# differently. The ``image_index_xref`` value is additive (S8) — evidence
# consumers that switch on this must tolerate the new member.
BlacklistMatchTrigger = Literal["inline", "reverse_search", "image_index_xref"]

# Which collection the matched image came from. ``"evidence"`` (default) is the
# byte-identical existing path; ``"image_index"`` is Capability B (§7.4).
BlacklistMatchTarget = Literal["evidence", "image_index"]


def build_blacklist_match_event(
    *,
    user_id: str,
    # ── Blacklist side ──
    blacklist_entry_id: str,
    blacklist_entry_name: str,
    blacklist_entry_category: str | None,
    blacklist_entry_version: int,
    blacklist_reference_id: str,
    blacklist_reference_url: str,
    blacklist_model_version: str,
    # ── Evidence side ──
    evidence_id: str,
    evidence_camera_id: str | None,
    evidence_device_id: str | None,
    evidence_app_id: int | None,
    evidence_infraction_code: str | None,
    evidence_category: str | None,
    matched_image_url: str,
    matched_image_index: int,
    matched_qdrant_point_id: str,
    # ── Match detail ──
    similarity_score: float,
    threshold_used: float,
    trigger: BlacklistMatchTrigger,
    # ── Image-index cross-reference (Capability B, additive — §7.4/S8) ──
    # Defaults keep the evidence-path event BYTE-IDENTICAL: the three keys are
    # omitted entirely when match_target == "evidence", so inline/reverse_search
    # payloads are unchanged. They appear only for the image_index_xref path.
    match_target: BlacklistMatchTarget = "evidence",
    external_id: str | None = None,
    batch_id: str | None = None,
    # ── Optional testing hook ──
    matched_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the ImageBlacklistMatchEvent payload.

    The returned shape matches ``docs/requirements/REPORT_GENERATION_STREAMS.md §3``
    exactly. If you change field names here, coordinate with the
    report-generation team — this is the wire contract between us.

    ``matched_at`` is caller-visible so tests can pin a deterministic
    value; production callers leave it unset and accept the default
    (``datetime.utcnow()`` formatted as ISO-8601 with trailing ``Z``).
    """
    if matched_at is None:
        matched_at = datetime.utcnow()

    event = {
        "user_id": user_id,
        "blacklist_entry_id": blacklist_entry_id,
        "blacklist_entry_name": blacklist_entry_name,
        "blacklist_entry_category": blacklist_entry_category,
        "blacklist_entry_version": blacklist_entry_version,
        "blacklist_reference_id": blacklist_reference_id,
        "blacklist_reference_url": blacklist_reference_url,
        "blacklist_model_version": blacklist_model_version,
        "evidence_id": evidence_id,
        "evidence_camera_id": evidence_camera_id,
        "evidence_device_id": evidence_device_id,
        "evidence_app_id": evidence_app_id,
        "evidence_infraction_code": evidence_infraction_code,
        "evidence_category": evidence_category,
        "matched_image_url": matched_image_url,
        "matched_image_index": matched_image_index,
        "matched_qdrant_point_id": matched_qdrant_point_id,
        "similarity_score": similarity_score,
        "threshold_used": threshold_used,
        "trigger": trigger,
        "matched_at": matched_at.isoformat() + "Z",
    }

    # Additive image-index cross-reference fields (§7.4/S8). Omitted entirely for
    # the evidence path so inline/reverse_search events stay byte-identical; the
    # report-generation consumer sees them ONLY on image_index_xref events.
    if match_target != "evidence":
        event["match_target"] = match_target
        event["external_id"] = external_id
        event["batch_id"] = batch_id

    return event
