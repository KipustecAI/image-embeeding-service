"""Publishers for the 7 outbound streams consumed by lookia-dw.

Wire-format authority: ``docs/requirements/LOOKIA_DW_STREAMS.md``.

Each `publish_*` function:

1. Skips the work entirely if no producer is wired (typical only in
   unit tests; logged at WARNING level so it surfaces in startup logs
   if a deploy forgets the injection).
2. Builds the payload dict to the exact shape documented in the
   contract — no extra keys, no missing keys.
3. Hashes PII fields (``BlacklistImageEntry.name`` → ``name_hash``) so
   the wire payload never carries raw values.
4. Calls ``StreamProducer.publish`` which does the XADD inside its
   own try/except — failures log but never raise. Same fire-and-forget
   policy as the existing ``weapons:detected`` and
   ``image:blacklist_match`` publishers.

Callers must invoke these **after the source-table session has
committed**, so ``id`` and ``created_at`` are populated. If the caller
holds a SQLAlchemy ORM row across the publish, snapshot it inside the
session (e.g. ``session.expunge(row)``) so attribute access after
commit doesn't lazy-load.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from ..application.helpers.dw_hashing import name_hash
from ..infrastructure.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_stream_producer = None  # StreamProducer, set at startup


def set_dw_publisher_producer(producer) -> None:
    """Inject the StreamProducer used for all 7 DW streams.

    Called from FastAPI lifespan after the StreamProducer is built.
    The same producer is shared with the report-generation publishers
    (`weapons:detected`, `image:blacklist_match`) — single Redis
    connection serves all outbound traffic.
    """
    global _stream_producer
    _stream_producer = producer


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 string for a datetime, or ``None`` for missing values."""
    if value is None:
        return None
    return value.isoformat()


def _uuid_str(value: UUID | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _publish(stream: str, event_type: str, payload: dict, maxlen: int) -> None:
    """Single XADD entry point — handles missing producer + logs failures.

    Centralised so every publish path applies the same policy:
    fire-and-forget, never re-raise. The producer's own ``publish()``
    swallows XADD exceptions, but we add an extra guard here for
    payload-construction failures (e.g. an unserializable value).
    """
    if _stream_producer is None:
        logger.warning(
            "DW publisher not configured; dropping event_type=%s on %s",
            event_type,
            stream,
        )
        return
    try:
        _stream_producer.publish(
            stream=stream,
            event_type=event_type,
            payload=payload,
            maxlen=maxlen,
        )
    except Exception:
        logger.warning(
            "Failed to publish DW event_type=%s on %s",
            event_type,
            stream,
            exc_info=True,
        )


# ── Stream 4.1: image_search_request:raw ───────────────────────────────────


def publish_image_search_request(event_type: str, row) -> None:
    """One of `image_search.created` / `.completed` / `.failed` per the
    lifecycle of a ``search_requests`` row.

    ``row`` is a ``SearchRequest`` ORM instance, snapshotted by the
    caller so attribute access doesn't touch the session.
    """
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "search_id": row.search_id,
        "user_id": row.user_id,
        "image_url": row.image_url,
        "status": row.status,
        "similarity_status": row.similarity_status,
        "threshold": row.threshold,
        "max_results": row.max_results,
        "search_metadata": row.search_metadata,
        "total_matches": row.total_matches,
        "results_key": row.results_key,
        "qdrant_query_point_id": row.qdrant_query_point_id,
        "worker_id": row.worker_id,
        "error_message": row.error_message,
        "retry_count": row.retry_count,
        "processing_started_at": _iso(row.processing_started_at),
        "processing_completed_at": _iso(row.processing_completed_at),
        "stream_message_id": row.stream_message_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    _publish(
        stream=settings.dw_stream_image_search_request,
        event_type=event_type,
        payload=payload,
        maxlen=settings.dw_maxlen_image_search_request,
    )


# ── Stream 4.2: image_search_match:raw ─────────────────────────────────────


def publish_image_search_matches(
    *,
    search_request_id: UUID | str,
    user_id: str,
    image_url: str,
    total_matches: int,
    matches: list,
    timestamp: datetime | None = None,
) -> None:
    """Fire once per completed search with ``total_matches > 0``.

    ``matches`` is a list of ``SearchMatch`` ORM rows (already
    expunged from session). The exploded payload carries all of them
    in a single envelope — the DW consumer explodes into N fact-table
    rows on landing.
    """
    if not matches:
        return  # contract §4.2 — skip empty-result searches

    payload: dict[str, Any] = {
        "search_request_id": _uuid_str(search_request_id),
        "user_id": user_id,
        "image_url": image_url,
        "total_matches": total_matches,
        "matches": [
            {
                "id": _uuid_str(m.id),
                "evidence_id": m.evidence_id,
                "camera_id": m.camera_id,
                "similarity_score": m.similarity_score,
                "image_url": m.image_url,
                "match_metadata": m.match_metadata,
            }
            for m in matches
        ],
        "timestamp": _iso(timestamp or datetime.utcnow()),
    }
    _publish(
        stream=settings.dw_stream_image_search_match,
        event_type="image_search.matched",
        payload=payload,
        maxlen=settings.dw_maxlen_image_search_match,
    )


# ── Stream 4.3: blacklist_image_entry:raw ──────────────────────────────────


def publish_blacklist_image_entry(row) -> None:
    """Fires on INSERT / UPDATE / soft-archive.

    PII: ``row.name`` is hashed before serialization. The raw ``name``
    is **never** placed in the payload — see the regression test in
    ``tests/test_dw_publisher.py``.
    """
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "name_hash": name_hash(row.name) if row.name else "",
        "category": row.category,
        "status": row.status,
        "active": row.active,
        "is_archived": row.is_archived,
        "user_id": row.user_id,
        "blacklist_version": row.blacklist_version,
        "match_threshold": row.match_threshold,
        "json_data": row.json_data,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    _publish(
        stream=settings.dw_stream_blacklist_image_entry,
        event_type="blacklist_image_entry.upserted",
        payload=payload,
        maxlen=settings.dw_maxlen_blacklist_image_entry,
    )


# ── Stream 4.4: blacklist_image_reference:raw ──────────────────────────────


def publish_blacklist_image_reference(row) -> None:
    """Fires on INSERT and on UPDATE that changes status / error / retry."""
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "entry_id": _uuid_str(row.entry_id),
        "image_url": row.image_url,
        "image_type": row.image_type,
        "status": row.status,
        "error_message": row.error_message,
        "retry_count": row.retry_count,
        "json_data": row.json_data,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    _publish(
        stream=settings.dw_stream_blacklist_image_reference,
        event_type="blacklist_image_reference.upserted",
        payload=payload,
        maxlen=settings.dw_maxlen_blacklist_image_reference,
    )


# ── Stream 4.5: blacklist_image_embedding:raw ──────────────────────────────


def publish_blacklist_image_embedding(row) -> None:
    """Fires on INSERT only — embeddings are immutable; model upgrades
    create new rows. See contract §4.5.
    """
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "entry_id": _uuid_str(row.entry_id),
        "reference_id": _uuid_str(row.reference_id),
        "qdrant_point_id": row.qdrant_point_id,
        "model_version": row.model_version,
        "json_data": row.json_data,
        "created_at": _iso(row.created_at),
    }
    _publish(
        stream=settings.dw_stream_blacklist_image_embedding,
        event_type="blacklist_image_embedding.created",
        payload=payload,
        maxlen=settings.dw_maxlen_blacklist_image_embedding,
    )


# ── Stream 4.6: image_embedding_request:raw (Tier 3) ───────────────────────


def publish_image_embedding_request(event_type: str, row) -> None:
    """One of `image_embedding_request.created` / `.completed` / `.failed`.

    Note (contract §4.6): the original DW spec proposed a separate
    `.weapon_analyzed` event for the async flag-flip case. Our
    consumer writes the row atomically at INSERT — no async update —
    so DW accepted dropping that event type. Producer emits only the
    three lifecycle values above.
    """
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "evidence_id": row.evidence_id,
        "camera_id": row.camera_id,
        "user_id": row.user_id,
        "device_id": row.device_id,
        "app_id": row.app_id,
        "infraction_code": row.infraction_code,
        "category": row.category,
        "status": row.status,
        "image_urls": row.image_urls,
        "worker_id": row.worker_id,
        "error_message": row.error_message,
        "retry_count": row.retry_count,
        "processing_started_at": _iso(row.processing_started_at),
        "processing_completed_at": _iso(row.processing_completed_at),
        "stream_message_id": row.stream_message_id,
        "weapon_analyzed": row.weapon_analyzed,
        "has_weapon": row.has_weapon,
        "weapon_classes": row.weapon_classes,
        "weapon_max_confidence": row.weapon_max_confidence,
        "weapon_summary": row.weapon_summary,
        "weapon_analysis_error": row.weapon_analysis_error,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    _publish(
        stream=settings.dw_stream_image_embedding_request,
        event_type=event_type,
        payload=payload,
        maxlen=settings.dw_maxlen_image_embedding_request,
    )


# ── Stream 4.7: image_embedding:raw (Tier 3) ───────────────────────────────


def publish_image_embedding(row) -> None:
    """Fires once per evidence_embedding INSERT.

    Note (contract §4.7): the original DW spec proposed a second
    `.upserted` event when ``weapon_detections`` is later populated.
    Our consumer writes ``weapon_detections`` at INSERT (or never) —
    no async update — so DW accepted dropping the UPDATE branch.
    Producer emits exactly one `.upserted` per row.
    """
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "request_id": _uuid_str(row.request_id),
        "qdrant_point_id": row.qdrant_point_id,
        "image_index": row.image_index,
        "image_url": row.image_url,
        "weapon_detections": row.weapon_detections,
        "json_data": row.json_data,
        "created_at": _iso(row.created_at),
    }
    _publish(
        stream=settings.dw_stream_image_embedding,
        event_type="image_embedding.upserted",
        payload=payload,
        maxlen=settings.dw_maxlen_image_embedding,
    )
