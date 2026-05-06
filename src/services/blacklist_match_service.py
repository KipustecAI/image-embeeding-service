"""Publish ``image:blacklist_match`` events to the report-generation stream.

Single publish path used by both:

  * Inline match — fires from ``embedding_results_consumer`` after a new
    evidence is persisted, when one of its frames matches an existing
    blacklist entry's Qdrant points.
  * Reverse search — fires from ``blacklist_reverse_search`` after a new
    blacklist reference is embedded, when historical evidence matches
    the new vector.

Fire-and-forget semantics: a publish failure logs at ERROR level but does
**not** raise. Report generation is downstream and non-critical; ingest
and the reverse-search job must continue regardless. Same policy as the
``weapons.detected`` publish in the embedding consumer.
"""

from __future__ import annotations

import logging

from ..application.helpers.blacklist_match_events import (
    IMAGE_BLACKLIST_MATCH_EVENT_TYPE,
    BlacklistMatchTrigger,
    build_blacklist_match_event,
)
from ..db.models.blacklist_image import BlacklistImageEntry, BlacklistImageReference
from ..infrastructure.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_BLACKLIST_MODEL_VERSION = "clip-vit-b-32"

_stream_producer = None  # StreamProducer, set at startup


def set_blacklist_match_stream_producer(producer) -> None:
    """Inject the StreamProducer instance from the FastAPI lifespan."""
    global _stream_producer
    _stream_producer = producer


async def publish_blacklist_match(
    *,
    user_id: str,
    entry: BlacklistImageEntry,
    reference: BlacklistImageReference,
    evidence_id: str,
    evidence_metadata: dict,
    matched_image_url: str,
    matched_image_index: int,
    matched_qdrant_point_id: str,
    similarity_score: float,
    threshold_used: float,
    trigger: BlacklistMatchTrigger,
) -> None:
    """Build and publish one ``image.blacklist_match`` event.

    ``evidence_metadata`` is the Qdrant point's payload — pulled from
    ``SearchResult.metadata`` for reverse-search hits, or the
    consumer-side ``ImageEmbedding.metadata`` dict for inline hits. The
    field is left as ``dict`` rather than a typed entity because the
    payload shape evolves across phases and a strict type would force a
    rebuild every time we add a field.
    """
    event = build_blacklist_match_event(
        user_id=user_id,
        blacklist_entry_id=str(entry.id),
        blacklist_entry_name=entry.name,
        blacklist_entry_category=entry.category,
        blacklist_entry_version=entry.blacklist_version,
        blacklist_reference_id=str(reference.id),
        blacklist_reference_url=reference.image_url,
        blacklist_model_version=_BLACKLIST_MODEL_VERSION,
        evidence_id=evidence_id,
        evidence_camera_id=evidence_metadata.get("camera_id"),
        evidence_device_id=evidence_metadata.get("device_id"),
        evidence_app_id=evidence_metadata.get("app_id"),
        evidence_infraction_code=evidence_metadata.get("infraction_code"),
        evidence_category=evidence_metadata.get("category"),
        matched_image_url=matched_image_url,
        matched_image_index=matched_image_index,
        matched_qdrant_point_id=matched_qdrant_point_id,
        similarity_score=similarity_score,
        threshold_used=threshold_used,
        trigger=trigger,
    )

    if _stream_producer is None:
        logger.warning(
            "Blacklist match produced but no stream_producer configured; "
            "dropping. evidence=%s entry=%s",
            evidence_id,
            entry.id,
        )
        return

    try:
        _stream_producer.publish(
            stream=settings.stream_reports_image_blacklist_match,
            event_type=IMAGE_BLACKLIST_MATCH_EVENT_TYPE,
            payload=event,
        )
        logger.info(
            "Published image.blacklist_match: evidence=%s entry=%s score=%.3f trigger=%s",
            evidence_id,
            entry.id,
            similarity_score,
            trigger,
        )
    except Exception as pub_err:
        logger.error(
            "Failed to publish image.blacklist_match for evidence=%s entry=%s: %s",
            evidence_id,
            entry.id,
            pub_err,
            exc_info=True,
        )
