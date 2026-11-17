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

from ..application.helpers.crop_labels import top_label
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


def _opt(row, name: str):
    """Nullable attribute added by frames-desde-zip (absent on old stubs → None)."""
    return getattr(row, name, None)


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


# Keys of ``search_metadata`` that are internal (search scope) and are stripped
# from the DW payload. Must match ``src/application/helpers/search_scope.py``.
_SEARCH_METADATA_INTERNAL_KEYS = frozenset({"scope_user_ids", "hub_id"})


# ── Stream 4.1: image_search_request:raw ───────────────────────────────────


def publish_image_search_request(event_type: str, row) -> None:
    """One of `image_search.created` / `.completed` / `.failed` per the
    lifecycle of a ``search_requests`` row.

    ``row`` is a ``SearchRequest`` ORM instance, snapshotted by the
    caller so attribute access doesn't touch the session.
    """
    search_metadata = row.search_metadata
    if isinstance(search_metadata, dict):
        # Internal routing keys never leave the service: the resolved hub member
        # list (can be long) and the hub id (sent as its own key below, once the DW
        # accepts it).
        search_metadata = {
            k: v for k, v in search_metadata.items() if k not in _SEARCH_METADATA_INTERNAL_KEYS
        }
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "search_id": row.search_id,
        # The person who searched (X-User-Id; a guest is the guest).
        "user_id": row.user_id,
        "image_url": row.image_url,
        "status": row.status,
        "similarity_status": row.similarity_status,
        "threshold": row.threshold,
        "max_results": row.max_results,
        "search_metadata": search_metadata,
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
    # ``hub_id`` (uuid or null) — the DW models are strict, so the key is sent only
    # once the DW has posted that its SDK accepts it (Frontend#dw-online #4936 (b)).
    if settings.dw_search_hub_id_enabled:
        metadata = row.search_metadata if isinstance(row.search_metadata, dict) else {}
        payload["hub_id"] = metadata.get("hub_id")
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
                # Additive, nullable (frames-desde-zip): the matched vector and
                # its frame reference; NULL for points ingested before zip_ref.
                "qdrant_point_id": _match_point_id(m),
                "image_name": _opt(m, "image_name"),
                "zip_entry": _opt(m, "zip_entry"),
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


def _match_point_id(m) -> str | None:
    """``search_matches.qdrant_point_id``, else the id the Qdrant search
    injected into ``match_metadata`` (same value, rows written before the
    column existed)."""
    point_id = _opt(m, "qdrant_point_id")
    if not point_id and isinstance(m.match_metadata, dict):
        point_id = m.match_metadata.get("qdrant_point_id")
    return str(point_id) if point_id else None


# ── Streams 4.3 – 4.5 + 4.8: the image blacklist (TRACKED delivery) ─────────
#
# These are NOT fire-and-forget any more. The builders below only shape the
# payloads; ``src/services/blacklist_dw_delivery.py`` sends them, records what the
# DW received on the row, retries every 60 s until the XADD succeeds, re-sends the
# whole blacklist daily, and sends hard-delete tombstones. Contract: Frontend#dw-online
# #4932–#4940 + docs/requirements/LOOKIA_DW_STREAMS.md §4.3–§4.5, §4.8.

BLACKLIST_IMAGE_ENTRY_UPSERTED = "blacklist_image_entry.upserted"
BLACKLIST_IMAGE_ENTRY_DELETED = "blacklist_image_entry.deleted"
BLACKLIST_IMAGE_REFERENCE_UPSERTED = "blacklist_image_reference.upserted"
BLACKLIST_IMAGE_REFERENCE_DELETED = "blacklist_image_reference.deleted"
BLACKLIST_IMAGE_EMBEDDING_CREATED = "blacklist_image_embedding.created"
BLACKLIST_IMAGE_MATCH_DETECTED = "blacklist_image_match.detected"


def build_blacklist_image_entry_payload(row) -> dict[str, Any]:
    """``blacklist_image_entry.upserted`` — every INSERT / UPDATE, the daily
    reconcile and a manual re-send. ``updated_at`` is the row's real value (the DW
    keeps the newer one, so a re-send never undoes an edit).

    PII: ``row.name`` is hashed. The raw ``name`` is **never** in the payload — see
    the regression test in ``tests/test_dw_publisher.py``.
    """
    return {
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


def build_blacklist_image_reference_payload(row) -> dict[str, Any]:
    """``blacklist_image_reference.upserted`` — INSERT and every status change."""
    return {
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


def build_blacklist_image_embedding_payload(row) -> dict[str, Any]:
    """``blacklist_image_embedding.created`` — INSERT only (embeddings are
    immutable). May be sent more than once (retry); the DW upserts on ``id``.
    """
    return {
        "id": _uuid_str(row.id),
        "entry_id": _uuid_str(row.entry_id),
        "reference_id": _uuid_str(row.reference_id),
        "qdrant_point_id": row.qdrant_point_id,
        "model_version": row.model_version,
        "json_data": row.json_data,
        "created_at": _iso(row.created_at),
    }


def build_blacklist_image_deleted_payload(tomb) -> tuple[str, str, dict[str, Any]]:
    """Tombstone of a hard delete → ``(stream, event_type, payload)``.

    * entry:     ``blacklist_image_entry.deleted``     ``{id, user_id, deleted_at}``
    * reference: ``blacklist_image_reference.deleted`` ``{id, entry_id, user_id, deleted_at}``

    An entry delete writes one tombstone for the entry AND one per reference (the
    SQL cascade removes them); embeddings follow their reference on the DW side.
    """
    if tomb.kind == "entry":
        return (
            settings.dw_stream_blacklist_image_entry,
            BLACKLIST_IMAGE_ENTRY_DELETED,
            {
                "id": _uuid_str(tomb.target_id),
                "user_id": tomb.user_id,
                "deleted_at": _iso(tomb.deleted_at),
            },
        )
    return (
        settings.dw_stream_blacklist_image_reference,
        BLACKLIST_IMAGE_REFERENCE_DELETED,
        {
            "id": _uuid_str(tomb.target_id),
            "entry_id": _uuid_str(tomb.entry_id),
            "user_id": tomb.user_id,
            "deleted_at": _iso(tomb.deleted_at),
        },
    )


def build_blacklist_image_match_payload(event: dict[str, Any], *, match_id: str) -> dict[str, Any]:
    """``blacklist_image_match.detected`` (§4.8) from the report-generation
    ``image.blacklist_match`` event: the same fields, with the entry NAME replaced
    by ``blacklist_entry_name_hash`` (PII, as on §4.3), plus ``id``.

    The image-index keys (``match_target``, ``external_id``, ``batch_id``) are
    ALWAYS present here (``match_target="evidence"`` + nulls on the evidence path),
    so the DW model has one fixed shape.
    """
    payload = {k: v for k, v in event.items() if k != "blacklist_entry_name"}
    name = event.get("blacklist_entry_name")
    payload["blacklist_entry_name_hash"] = name_hash(name) if name else ""
    payload["id"] = match_id
    payload.setdefault("match_target", "evidence")
    payload.setdefault("external_id", None)
    payload.setdefault("batch_id", None)
    return payload


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
        # Additive, nullable (frames-desde-zip): embeddings stored for the evidence.
        "image_count": _opt(row, "image_count"),
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
    labels = _opt(row, "labels")
    vehicle_type, vehicle_type_score = top_label(labels)
    payload: dict[str, Any] = {
        "id": _uuid_str(row.id),
        "request_id": _uuid_str(row.request_id),
        "qdrant_point_id": row.qdrant_point_id,
        "image_index": row.image_index,
        "image_url": row.image_url,
        "weapon_detections": row.weapon_detections,
        "json_data": row.json_data,
        # Additive, nullable (frames-desde-zip): frame reference inside the
        # evidence ZIP. zip_entry is the RAW path (never URL-encoded).
        "zip_url": _opt(row, "zip_url"),
        "image_name": _opt(row, "image_name"),
        "zip_entry": _opt(row, "zip_entry"),
        # Additive, nullable (CLS-06): crop labels from the CLIP worker's
        # classifier. labels None = did not run; [] = ran, nothing passed.
        "labels": labels,
        "vehicle_type": vehicle_type,
        "vehicle_type_score": vehicle_type_score,
        "classifier_version": _opt(row, "classifier_version"),
        "created_at": _iso(row.created_at),
    }
    _publish(
        stream=settings.dw_stream_image_embedding,
        event_type="image_embedding.upserted",
        payload=payload,
        maxlen=settings.dw_maxlen_image_embedding,
    )
