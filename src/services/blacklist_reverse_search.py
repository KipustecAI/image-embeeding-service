"""Reverse search: when a new blacklist reference vector lands, find every
existing piece of evidence that matches it.

Scheduled as an APScheduler one-shot ``trigger="date"`` job from
``BlacklistEmbedService.store_blacklist_embedding`` so the consumer
returns immediately and the (potentially long-running) similarity scan
runs in the scheduler's thread pool.

v1 design tradeoffs:

- **Job lost on restart.** If the backend dies mid-job, partial matches
  may have been published and the rest are dropped. ``POST /api/v1/
  blacklist/image-entries/{id}/backfill`` (Phase 06) is the recovery
  knob; ops triggers it after a known incident.
- **Single-page search.** ``search_similar`` with limit=batch_size is
  enough for current evidence volumes. A paginated variant is the right
  follow-up if the post-threshold result count ever exceeds the batch
  size — but that would also be a strong signal that the threshold is
  wrong, not just that we need more pages.
- **Match publishing.** Each match goes out as one ``image:blacklist_match``
  event via the shared ``BlacklistMatchService`` (same publisher as the
  inline match path).
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

import numpy as np

from ..application.helpers.source_type_filter import build_evidence_only_filter
from ..db.repositories.blacklist_image_repo import BlacklistImageRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session

logger = logging.getLogger(__name__)
settings = get_settings()

_scheduler = None  # AsyncIOScheduler, set at startup
_vector_repo = None  # QdrantVectorRepository, set at startup


def set_reverse_search_scheduler(scheduler) -> None:
    """Inject the APScheduler instance from the FastAPI lifespan."""
    global _scheduler
    _scheduler = scheduler


def set_reverse_search_vector_repo(repo) -> None:
    """Inject the Qdrant repository from the FastAPI lifespan."""
    global _vector_repo
    _vector_repo = repo


def schedule_reverse_search(
    *,
    entry_id: UUID,
    reference_id: UUID,
    user_id: str,
    vector: list[float],
) -> str | None:
    """Register a one-shot APScheduler job to run the reverse search.

    Returns the scheduled job id (or ``None`` if no scheduler is wired —
    typical only in unit tests). Job id is keyed by ``reference_id`` and
    uses ``replace_existing=True`` so a re-trigger on the same reference
    just updates the run instead of leaking duplicate jobs.
    """
    if _scheduler is None:
        logger.warning(
            "Scheduler not configured; skipping reverse search for ref=%s",
            reference_id,
        )
        return None

    job_id = f"reverse_search_{reference_id}"
    _scheduler.add_job(
        _run_reverse_search,
        trigger="date",
        run_date=datetime.utcnow(),
        args=(entry_id, reference_id, user_id, vector),
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Scheduled reverse search: entry=%s ref=%s job=%s",
        entry_id,
        reference_id,
        job_id,
    )
    return job_id


async def _run_reverse_search(
    entry_id: UUID,
    reference_id: UUID,
    user_id: str,
    vector: list[float],
) -> None:
    """Execute the reverse search and (Phase 05) publish match events.

    Bug-shaped failure mode here is silent: APScheduler swallows
    exceptions in jobs by default unless we attach a listener. The
    try/except below logs explicitly so an ops grep for
    ``"Reverse search failed"`` finds incidents without needing a job
    listener configured.
    """
    if _vector_repo is None:
        logger.error(
            "Vector repo not configured; reverse search aborted for ref=%s",
            reference_id,
        )
        return

    threshold = settings.blacklist_match_threshold
    batch_size = settings.blacklist_reverse_search_batch_size

    # Multi-tenant scope: only this user's evidence. ``source_type=evidence``
    # via the helper keeps blacklist points (and other future source_types)
    # out of the result set — see docs/image-blacklist/03_QDRANT.md.
    filter_conditions = build_evidence_only_filter({"user_id": user_id})

    try:
        matches = await _vector_repo.search_similar(
            query_vector=np.array(vector, dtype=np.float32),
            limit=batch_size,
            threshold=threshold,
            filter_conditions=filter_conditions,
        )
    except Exception as e:
        logger.error(
            "Reverse search failed: entry=%s ref=%s err=%s",
            entry_id,
            reference_id,
            e,
            exc_info=True,
        )
        return

    logger.info(
        "Reverse search complete: entry=%s ref=%s matches=%d threshold=%.3f",
        entry_id,
        reference_id,
        len(matches),
        threshold,
    )

    if not matches:
        return

    # Load the entry + reference once. Wrapped in try/except because
    # APScheduler swallows job exceptions silently — without this guard
    # a transient DB blip would lose the entire match batch with no log
    # trail. Same defensive pattern as the search call above.
    try:
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            reference = await repo.get_reference(reference_id)
    except Exception as e:
        logger.error(
            "Reverse search DB load failed: entry=%s ref=%s err=%s",
            entry_id,
            reference_id,
            e,
            exc_info=True,
        )
        return

    if entry is None or reference is None:
        logger.error(
            "Reverse search produced %d matches but entry=%s or ref=%s "
            "was deleted before publish; dropping",
            len(matches),
            entry_id,
            reference_id,
        )
        return

    # Imported lazily to avoid a circular at module-load time (the match
    # service imports from helpers/, which is light, but this defers
    # the publisher_set side-effect check too).
    from .blacklist_match_service import publish_blacklist_match

    for match in matches:
        md = match.metadata or {}
        await publish_blacklist_match(
            user_id=user_id,
            entry=entry,
            reference=reference,
            evidence_id=str(match.evidence_id),
            evidence_metadata=md,
            matched_image_url=match.image_url,
            matched_image_index=md.get("image_index", 0),
            matched_qdrant_point_id=str(md.get("qdrant_point_id", "")),
            similarity_score=match.similarity_score,
            threshold_used=threshold,
            trigger="reverse_search",
        )
