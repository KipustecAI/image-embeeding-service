"""Timeout reaper for the on-demand image-index feature.

A batch whose compute crashed or whose result was lost stays ``pending`` /
``computing`` forever. The coordinator is no-HTTP, so it would hang. This reaper
is the backstop: it sweeps ``t_image_index_batches`` on the EXISTING
``AsyncIOScheduler`` (00_DESIGN §5.3, same pattern as ``recover_stale_working``),
terminalizes stuck batches as ``error``, and best-effort publishes
``image_batch.failed`` so the coordinator learns the terminal.

Ordering (load-bearing): the terminal DB marks are COMMITTED FIRST (on session
``__aexit__``) — that unblocks the stuck state — and only THEN do we publish, one
try/except per publish so a single Redis failure doesn't abort the rest of the
batch's notifications. A marked batch is no longer stuck, so a failed publish is
NOT retried by a later sweep: a transient Redis outage spanning the sweep loses
those notifications, and the REST reconcile-by-``external_id`` is the
coordinator's backstop (acceptable for a coarse 60s safety net, 00_DESIGN §5.3).

``list_stuck_active`` is a plain SELECT (SQLite-portable for CI, single replica
today). Switch to ``FOR UPDATE SKIP LOCKED`` before running >1 API replica
(00_DESIGN §5.3, open item #5).

GATING: the job is added to the scheduler only under ``IMAGE_INDEX_ENABLED``
(00_DESIGN §5.4); the flag-guard below is a defensive belt-and-suspenders so a
stray invocation is a no-op when the feature is dark.
"""

import logging
from datetime import datetime, timedelta

from ..db.repositories.image_index_repo import ImageIndexRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..services.image_index_service import ImageIndexService
from .producer import StreamProducer

logger = logging.getLogger(__name__)

_REAPER_ERROR_MESSAGE = "compute timeout"
_LIFECYCLE_FAILED = "image_batch.failed"

# Module-global producer (mirrors the consumer bridges).
_producer: StreamProducer | None = None


def set_reaper_producer(producer: StreamProducer) -> None:
    global _producer
    _producer = producer


async def reap_stuck_image_index_batches() -> None:
    """Terminalize timed-out image-index batches + best-effort publish failed.

    Flag-guarded; cutoff = ``utcnow - image_index_max_compute_seconds``. Marks
    every ``status IN (pending, computing) AND created_at < cutoff`` batch as
    ``error`` (commit FIRST), then publishes ``image_batch.failed`` per batch with
    a per-publish try/except so one failure doesn't abort the loop.
    """
    settings = get_settings()
    if not settings.image_index_enabled:
        # Defensive: the job is only added when the flag is on (00_DESIGN §5.4).
        return

    cutoff = datetime.utcnow() - timedelta(seconds=settings.image_index_max_compute_seconds)
    failed_payloads: list[dict] = []

    async with get_session() as session:
        repo = ImageIndexRepository(session)
        stuck = await repo.list_stuck_active(cutoff)
        for batch in stuck:
            await repo.mark_failed(batch, _REAPER_ERROR_MESSAGE)
            # Build the lifecycle payload while ORM attrs are loaded (before the
            # session closes) — build_batch_failed_payload reads batch columns.
            failed_payloads.append(ImageIndexService.build_batch_failed_payload(batch))
        if stuck:
            logger.warning(
                "image-index reaper: terminalized %d stuck batch(es) as 'error' "
                "(cutoff=%s, timeout=%ds)",
                len(stuck),
                cutoff.isoformat(),
                settings.image_index_max_compute_seconds,
            )
        # Terminal DB marks committed on __aexit__ FIRST — unblocks the stuck state.

    if not failed_payloads:
        return
    if _producer is None:
        logger.error(
            "image-index reaper: %d batch(es) marked 'error' but no producer wired "
            "— image_batch.failed NOT published (reconcile-by-external_id backstop)",
            len(failed_payloads),
        )
        return

    for payload in failed_payloads:
        # Best-effort AFTER commit: a marked batch is no longer stuck, so a failed
        # publish is not retried by a later sweep (00_DESIGN §5.3). One try/except
        # per publish so a single Redis failure doesn't abort the rest.
        try:
            _producer.publish(
                stream=settings.stream_image_batch_raw,
                event_type=_LIFECYCLE_FAILED,
                payload=payload,
                maxlen=settings.image_index_batch_maxlen,
            )
        except Exception:
            logger.exception(
                "image-index reaper: publish image_batch.failed failed (batch "
                "already marked 'error') id=%s",
                payload.get("batch_id"),
            )
