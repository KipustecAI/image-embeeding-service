"""Submit-intake consumer for the on-demand image-index feature.

Consumes ``image:index:submit`` (event ``image.index.submit``), group
``image-index-submit`` — isolated from the live ``backend-workers`` group. Reuses
``StreamConsumer`` (daemon thread, XREADGROUP, ACK-on-success, XPENDING/XCLAIM
reclaim, ``{stream}:dead`` DLQ) and ``StreamProducer`` verbatim, bridging the
sync consumer thread onto the shared event loop via ``run_coroutine_threadsafe``
(mirrors ``embedding_results_consumer`` / ``search_results_consumer``).

Two-tier rejection (00_DESIGN §5.1 / §6.1):
  * TIER-1 unbindable (payload not a dict; missing/blank/non-UUID ``user_id``;
    missing/blank ``client_batch_ref``) → LOG LOUD + return (ACK-drop, no batch,
    NEVER raise). There is nothing to bind a tenant to.
  * TIER-2 bindable-but-rejected (over N_CAP / bad url / missing item_id /
    missing external_id / empty items) → persist an ERROR batch + publish
    ``image_batch.failed``. The no-HTTP coordinator MUST get a loud terminal.

Happy path (fresh mint only): atomic idempotent mint → dispatch to
``image:index`` (event ``image.index.compute``) → set ``computing`` → publish
``image_batch.created``. A redelivered (duplicate ``client_batch_ref``) submit
re-binds the SAME batch and re-publishes ``image_batch.created`` but does NOT
re-dispatch and does NOT mint a second batch.

Raise-vs-ACK: only genuine transient DB/Redis failures (session error,
``_producer`` missing, ``publish`` raising) propagate — the message is then not
ACKed, stays in the PEL, is XCLAIM-retried and finally dead-lettered to
``image:index:submit:dead`` after ``dead_letter_max_retries``. Because the mint
is idempotent a retry re-binds the same batch. **Pre-mint DLQ note:** a submit
that dies before the batch row is minted lands in the DLQ with no batch row —
the reaper never sees it, so surface those via a DLQ-depth alert
(``_dlq_depth`` helper below), not auto-recovery (00_DESIGN §5.1 S6).

GATING: this consumer is constructed + started only under
``IMAGE_INDEX_ENABLED`` (00_DESIGN §5.4). The LIVE dispatch to ``image:index``
must not XADD in prod until the compute v1-FREEZE — the single
``IMAGE_INDEX_ENABLED`` flag (default False through Phases 1-4, flipped only
after the Phase 5 live smoke) is that gate.
"""

import asyncio
import logging
import uuid

from ..db.models.constants import ImageIndexBatchStatus
from ..infrastructure.config import get_settings
from ..services.image_index_service import ImageIndexService
from .consumer import StreamConsumer
from .producer import StreamProducer

logger = logging.getLogger(__name__)
settings = get_settings()

# Handler timeout (00_DESIGN §5.1).
_SUBMIT_HANDLER_TIMEOUT_S = 120

# Event types.
_SUBMIT_EVENT = "image.index.submit"  # inbound (coordinator → us)
_DISPATCH_EVENT = "image.index.compute"  # outbound dispatch (us → compute)
_LIFECYCLE_CREATED = "image_batch.created"
_LIFECYCLE_FAILED = "image_batch.failed"

# Module-global sync→async bridge (mirrors the live consumers).
_event_loop: asyncio.AbstractEventLoop | None = None
_producer: StreamProducer | None = None


def set_submit_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _event_loop
    _event_loop = loop


def set_submit_producer(producer: StreamProducer) -> None:
    global _producer
    _producer = producer


def _is_blank(value) -> bool:
    """True for None, non-str, or whitespace-only strings."""
    return value is None or not (isinstance(value, str) and value.strip())


def _is_uuid(value) -> bool:
    """True iff ``value`` parses as a UUID (the tenant-bind anchor, §5.1)."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def create_image_index_submit_consumer() -> StreamConsumer:
    """Factory: a StreamConsumer for image:index:submit on the dedicated group."""
    consumer = StreamConsumer(
        stream=settings.stream_image_index_submit,
        group=settings.image_index_submit_group,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password or None,
        redis_db=settings.redis_streams_db,
        block_ms=settings.stream_consumer_block_ms,
        batch_size=settings.stream_consumer_batch_size,
        reclaim_idle_ms=settings.stream_reclaim_idle_ms,
        dead_letter_max_retries=settings.stream_dead_letter_max_retries,
        concurrency=settings.stream_consumer_concurrency,
    )
    consumer.register_handler(_SUBMIT_EVENT, _handle_submit)
    return consumer


def _handle_submit(event_type: str, payload: dict, message_id: str) -> None:
    """Bridge the sync consumer thread onto the shared event loop."""
    future = asyncio.run_coroutine_threadsafe(
        _process_submit(payload, message_id),
        _event_loop,
    )
    future.result(timeout=_SUBMIT_HANDLER_TIMEOUT_S)


def _publish_lifecycle(event_type: str, payload: dict) -> None:
    """Publish a lifecycle event to image_batch:raw with EXPLICIT stream.

    ``StreamProducer.publish`` requires ``stream`` as a positional arg (no default),
    so a bare publish raises ``TypeError`` rather than silently landing on a wrong
    stream. Every lifecycle publish passes ``stream=settings.stream_image_batch_raw``
    (§1) — do NOT rely on any inferred-stream fallback; there is none.
    """
    _producer.publish(
        stream=settings.stream_image_batch_raw,
        event_type=event_type,
        payload=payload,
        maxlen=settings.image_index_batch_maxlen,
    )


async def _process_submit(payload: dict, message_id: str) -> None:
    """Intake one submit: two-tier rejection → atomic mint → fresh-mint dispatch."""
    # ── TIER 1: unbindable → LOG LOUD + ACK-drop (never raise, no batch) ──
    if not isinstance(payload, dict):
        logger.error("image-index submit dropped: payload is not a dict (msg=%s)", message_id)
        return
    user_id = payload.get("user_id")
    client_batch_ref = payload.get("client_batch_ref")
    if _is_blank(user_id) or not _is_uuid(user_id):
        logger.error(
            "image-index submit dropped: missing/blank/non-UUID user_id (ref=%r msg=%s)",
            client_batch_ref,
            message_id,
        )
        return
    if _is_blank(client_batch_ref):
        logger.error(
            "image-index submit dropped: missing/blank client_batch_ref (user=%s msg=%s)",
            user_id,
            message_id,
        )
        return

    service = ImageIndexService()

    # ── Atomic idempotent mint (TIER-2 guards run inside submit_batch_created) ──
    batch, created, error = await service.submit_batch_created(payload)

    # ── TIER 2: bindable-but-rejected → ERROR batch + image_batch.failed ──
    if error is not None:
        logger.warning(
            "image-index submit rejected (ref=%s user=%s): %s",
            client_batch_ref,
            user_id,
            error,
        )
        err_batch = await service.create_error_batch(payload, error)
        _publish_lifecycle(
            _LIFECYCLE_FAILED,
            ImageIndexService.build_batch_failed_payload(err_batch),
        )
        return

    # ── Duplicate/redelivered submit → re-announce, NO re-dispatch, NO re-mint ──
    if not created:
        logger.info(
            "image-index submit re-bind (ref=%s batch=%s) — re-publishing created, no dispatch",
            client_batch_ref,
            batch.id,
        )
        _publish_lifecycle(
            _LIFECYCLE_CREATED,
            ImageIndexService.build_batch_created_payload(batch),
        )
        return

    # ── Fresh mint → dispatch → computing → image_batch.created ──
    # item_index is minted here (0-based submit order); the caller never sends it.
    # v1.1: accept the portfolio-standard images/image_id alias (validated above).
    dispatch_items = [
        {
            "item_id": ImageIndexService.item_id_of(it),
            "image_url": it["image_url"],
            "item_index": i,
        }
        for i, it in enumerate(ImageIndexService.extract_items(payload))
    ]
    _producer.publish(
        stream=settings.stream_image_index,
        event_type=_DISPATCH_EVENT,
        payload={
            "batch_id": str(batch.id),
            "user_id": user_id,
            "items": dispatch_items,
            "metadata": payload.get("metadata"),
        },
    )
    # The single actor that writes 'computing' — only AFTER a successful dispatch,
    # so the coordinator never sees `created` for a batch that failed to dispatch.
    await service.mark_computing(batch.id)
    batch.status = ImageIndexBatchStatus.COMPUTING  # reflect on the snapshot for the payload
    _publish_lifecycle(
        _LIFECYCLE_CREATED,
        ImageIndexService.build_batch_created_payload(batch),
    )


def _dlq_depth(redis_client) -> int:
    """Best-effort depth of image:index:submit:dead (pre-mint failure alert hook).

    Wire this into the metrics/alerting surface (00_DESIGN §5.1 S6): a non-zero
    depth means submits died before a batch row was minted — the reaper cannot
    recover those, so they must be surfaced by alert rather than auto-recovery.
    """
    try:
        return int(redis_client.xlen(f"{settings.stream_image_index_submit}:dead"))
    except Exception:  # noqa: BLE001 — alerting must never break the consumer
        return 0
