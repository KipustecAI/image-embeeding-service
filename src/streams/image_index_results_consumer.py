"""Results consumer for the on-demand image-index feature.

Consumes ``image:index:results`` (compute → us), group ``image-index-results`` —
isolated from the live ``backend-workers`` group. Two event types share the one
stream:
  * ``image.index.computed`` (handler timeout 300s) → land the per-item results,
    upsert embedded vectors into the dedicated ``image_index_embeddings``
    collection, recompute counts absolutely, and — when the batch is fully
    accounted — publish ``image_batch.completed`` to ``image_batch:raw``.
  * ``compute.error`` (handler timeout 60s) → terminalize the batch as ``error``
    and publish ``image_batch.failed``.

Reuses ``StreamConsumer`` (daemon thread, XREADGROUP, ACK-on-success,
XPENDING/XCLAIM reclaim, ``{stream}:dead`` DLQ) and ``StreamProducer`` verbatim,
bridging the sync consumer thread onto the shared event loop via
``run_coroutine_threadsafe`` (mirrors the submit consumer /
``embedding_results_consumer`` / ``search_results_consumer``).

**XREADGROUP COUNT=1 (load-bearing).** ``image.index.computed`` messages are
~281 KB (companion / IMAGE_INDEX_COMPUTE.md). The factory forces ``batch_size=1``
so the consumer never buffers more than one large message in memory at a time —
do NOT raise it to ``settings.stream_consumer_batch_size``.

⚠️ COMPUTE-ERROR SHAPE DIVERGENCE — read before copying the live consumers.
Our ``compute.error`` payload is keyed on ``batch_id`` + ``error_message`` (our
compute contract, companion §3) — NOT the ``entity_id`` / ``entity_type`` shape
the LIVE ``embedding_results_consumer`` / ``search_results_consumer`` use. A
maintainer who copies a live consumer and reads ``payload["entity_id"]`` here
would silently no-op every batch-level compute error and hang the no-HTTP
coordinator. ``ImageIndexService.mark_error_from_compute_error`` reads our shape;
the dedicated stream + dedicated group make the divergence collision-free.

Raise-vs-ACK: ``land_computed`` raises ``ValueError`` on an unknown
``vector_encoding`` (dead-letter) and ``RuntimeError`` on a Qdrant upsert failure;
a transient DB/Redis failure (session, publish) also propagates. Any propagating
exception leaves the message unacked → PEL → XCLAIM retry → DLQ after
``dead_letter_max_retries``. Because the land is idempotent (deterministic
point-ids + ON CONFLICT upserts + absolute recompute) a re-land is a no-op that
re-publishes the same terminal event. A ``None`` return (never-minted /
unparseable ``batch_id``, or a not-yet-accounted batch staying ``computing``) is
an ACK no-op — nothing is published.

GATING: constructed + started only under ``IMAGE_INDEX_ENABLED`` (00_DESIGN §5.4).
Cold-start ordering is load-bearing: this results consumer must be started BEFORE
the submit consumer so its group exists before any dispatch can produce a reply.
"""

import asyncio
import logging

from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..services.image_index_service import ImageIndexService
from .consumer import StreamConsumer
from .producer import StreamProducer

logger = logging.getLogger(__name__)
settings = get_settings()

# Handler timeouts (00_DESIGN §5.2): computed does the heavy land + Qdrant
# upsert; compute.error is a cheap terminalize.
_COMPUTED_HANDLER_TIMEOUT_S = 300
_ERROR_HANDLER_TIMEOUT_S = 60

# Event types.
_COMPUTED_EVENT = "image.index.computed"  # inbound (compute → us)
_ERROR_EVENT = "compute.error"  # inbound (compute → us)
_LIFECYCLE_COMPLETED = "image_batch.completed"
_LIFECYCLE_FAILED = "image_batch.failed"

# Module-global sync→async bridge (mirrors the submit consumer).
_event_loop: asyncio.AbstractEventLoop | None = None
_producer: StreamProducer | None = None
_vector_repo = None  # the DEDICATED ImageIndexVectorRepository (NOT the live one)


def set_results_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _event_loop
    _event_loop = loop


def set_results_producer(producer: StreamProducer) -> None:
    global _producer
    _producer = producer


def set_results_vector_repo(vector_repo) -> None:
    """Inject the DEDICATED ImageIndexVectorRepository (shares the live client).

    Never the live ``QdrantVectorRepository`` — the isolation boundary is the
    separate ensure + collection (00_DESIGN §4).
    """
    global _vector_repo
    _vector_repo = vector_repo


def create_image_index_results_consumer() -> StreamConsumer:
    """Factory: a StreamConsumer for image:index:results on the dedicated group.

    ``batch_size=1`` forces XREADGROUP COUNT=1 — ~281 KB messages must never be
    buffered more than one at a time (companion / 00_DESIGN §5.2).
    """
    consumer = StreamConsumer(
        stream=settings.stream_image_index_results,
        group=settings.image_index_results_group,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password or None,
        redis_db=settings.redis_streams_db,
        block_ms=settings.stream_consumer_block_ms,
        batch_size=1,  # XREADGROUP COUNT=1 — do NOT raise (281 KB messages)
        reclaim_idle_ms=settings.stream_reclaim_idle_ms,
        dead_letter_max_retries=settings.stream_dead_letter_max_retries,
        concurrency=settings.stream_consumer_concurrency,
    )
    consumer.register_handler(_COMPUTED_EVENT, _handle_computed)
    consumer.register_handler(_ERROR_EVENT, _handle_compute_error)
    return consumer


def _handle_computed(event_type: str, payload: dict, message_id: str) -> None:
    """Bridge the sync consumer thread onto the shared event loop (300s)."""
    future = asyncio.run_coroutine_threadsafe(
        _process_computed(payload, message_id),
        _event_loop,
    )
    future.result(timeout=_COMPUTED_HANDLER_TIMEOUT_S)


def _handle_compute_error(event_type: str, payload: dict, message_id: str) -> None:
    """Bridge the sync consumer thread onto the shared event loop (60s)."""
    future = asyncio.run_coroutine_threadsafe(
        _process_compute_error(payload, message_id),
        _event_loop,
    )
    future.result(timeout=_ERROR_HANDLER_TIMEOUT_S)


def _publish_lifecycle(event_type: str, payload: dict) -> None:
    """Publish a lifecycle event to image_batch:raw with EXPLICIT stream.

    ``StreamProducer.publish`` requires ``stream`` positionally (no default) — a
    bare publish raises ``TypeError`` rather than landing on a wrong stream. Every
    lifecycle publish passes ``stream=settings.stream_image_batch_raw`` (§1) with
    the MAXLEN trim; there is no inferred-stream fallback.
    """
    _producer.publish(
        stream=settings.stream_image_batch_raw,
        event_type=event_type,
        payload=payload,
        maxlen=settings.image_index_batch_maxlen,
    )


async def _process_computed(payload: dict, message_id: str) -> None:
    """Land one ``image.index.computed`` payload → maybe publish completed.

    Opens + commits the session around ``land_computed`` (which never commits);
    the lifecycle publish happens AFTER commit (00_DESIGN §5.2 step 5). A ``None``
    return (never-minted batch_id, or a not-yet-accounted batch staying
    ``computing``) is an ACK no-op — nothing is published. A raise (unknown
    encoding → dead-letter, Qdrant/DB failure → PEL retry) propagates: the session
    rolls back and the message is not ACKed.
    """
    service = ImageIndexService()
    async with get_session() as session:
        lifecycle = await service.land_computed(session, payload, vector_repo=_vector_repo)
        # session commits on __aexit__ (before we publish).
    if lifecycle is not None:
        _publish_lifecycle(_LIFECYCLE_COMPLETED, lifecycle)
        # Capability-B auto-on-land hook (v1.1, §7.4) — GATED, AFTER the terminal
        # publish, NEVER before, NEVER blocking completion visibility. The batch is
        # already marked completed + published above; this only cross-references the
        # tenant's active blacklist against the just-landed batch and fires
        # fire-and-forget image:blacklist_match events. Defensive try/except so an
        # unexpected error can never fail the handler / unack the (idempotent) land.
        await _maybe_autocheck_blacklist(lifecycle)


async def _maybe_autocheck_blacklist(lifecycle: dict) -> None:
    """Gated Capability-B cross-reference of a just-landed batch (§7.4).

    No-op unless ``image_index_blacklist_autocheck_enabled`` is on. Fast-exits
    inside ``auto_cross_reference_batch`` when the tenant has no active blacklist.
    Never raises — a match-scan failure must never regress the land path.
    """
    if not settings.image_index_blacklist_autocheck_enabled:
        return
    user_id = lifecycle.get("user_id")
    batch_id = lifecycle.get("batch_id")
    if not user_id or not batch_id:
        return
    try:
        # Imported lazily so the flag-off land path never touches the xref module.
        from ..services.blacklist_image_index_xref import auto_cross_reference_batch

        await auto_cross_reference_batch(user_id=user_id, batch_id=batch_id)
    except Exception as e:  # noqa: BLE001 — never block/unack the land
        logger.error(
            "auto-xref hook failed (user=%s batch=%s): %s", user_id, batch_id, e
        )


async def _process_compute_error(payload: dict, message_id: str) -> None:
    """Terminalize a batch as ``error`` from a ``compute.error`` payload.

    Reads OUR contract shape (``batch_id`` + ``error_message``), NOT the live
    entity_id/entity_type shape — see the module docstring. A ``None`` return
    (never-minted / unparseable batch_id) is an ACK no-op.
    """
    service = ImageIndexService()
    async with get_session() as session:
        lifecycle = await service.mark_error_from_compute_error(session, payload)
        # session commits on __aexit__ (before we publish).
    if lifecycle is not None:
        _publish_lifecycle(_LIFECYCLE_FAILED, lifecycle)
