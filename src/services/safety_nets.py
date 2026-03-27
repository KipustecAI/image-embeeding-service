"""Safety net jobs — scheduled fallbacks that catch anything the primary path misses.

Under normal operation these find 0 rows. If they find rows, something upstream
(stream consumer or BatchTrigger) may be broken.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, select, func

from ..db.models.constants import (
    EmbeddingRequestStatus,
    SearchRequestStatus,
    SimilarityStatus,
)
from ..db.models.embedding_request import EmbeddingRequest
from ..db.models.search_request import SearchRequest
from ..db.repositories import EmbeddingRequestRepository, SearchRequestRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..services.batch_trigger import get_batch_trigger

logger = logging.getLogger(__name__)
settings = get_settings()

# ARQ pool reference — set from lifespan
_arq_pool = None


def set_arq_pool(pool):
    global _arq_pool
    _arq_pool = pool


async def dispatch_embedding_batch():
    """Callback for embedding BatchTrigger: pick pending rows, mark WORKING, enqueue ARQ."""
    if _arq_pool is None:
        logger.error("ARQ pool not set — cannot dispatch embedding batch")
        return

    try:
        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            pending = await repo.get_pending_requests(limit=settings.batch_trigger_size)

            if not pending:
                logger.debug("dispatch_embedding_batch: no pending rows found")
                return

            request_ids = [str(r.id) for r in pending]

            for r in pending:
                r.status = EmbeddingRequestStatus.WORKING
                r.processing_started_at = datetime.utcnow()
            await session.flush()

            await _arq_pool.enqueue_job(
                "process_evidence_embeddings_batch", request_ids
            )

        logger.info(f"Dispatched {len(request_ids)} embedding requests to ARQ")

    except Exception as e:
        logger.error(f"dispatch_embedding_batch failed: {e}", exc_info=True)


async def dispatch_search_batch():
    """Callback for search BatchTrigger: pick pending rows, mark WORKING, enqueue ARQ."""
    if _arq_pool is None:
        logger.error("ARQ pool not set — cannot dispatch search batch")
        return

    try:
        async with get_session() as session:
            repo = SearchRequestRepository(session)
            pending = await repo.get_pending_requests(limit=settings.batch_trigger_search_size)

            if not pending:
                logger.debug("dispatch_search_batch: no pending rows found")
                return

            request_ids = [str(r.id) for r in pending]

            for r in pending:
                r.status = SearchRequestStatus.WORKING
                r.processing_started_at = datetime.utcnow()
            await session.flush()

            await _arq_pool.enqueue_job(
                "process_image_searches_batch", request_ids
            )

        logger.info(f"Dispatched {len(request_ids)} search requests to ARQ")

    except Exception as e:
        logger.error(f"dispatch_search_batch failed: {e}", exc_info=True)


async def embedding_safety_net():
    """Every 60s: catch status=1 rows missed by BatchTrigger."""
    await dispatch_embedding_batch()


async def search_safety_net():
    """Every 120s: catch status=1 search rows missed by BatchTrigger."""
    await dispatch_search_batch()


async def recover_stale_working():
    """Every 5m: reset rows stuck in WORKING for too long."""
    async with get_session() as session:
        embed_repo = EmbeddingRequestRepository(session)
        stale_embeds = await embed_repo.get_stale_working(
            stale_minutes=settings.stale_working_minutes
        )

        for r in stale_embeds:
            r.retry_count += 1
            if r.retry_count >= settings.max_retries:
                r.status = EmbeddingRequestStatus.ERROR
                r.error_message = "Max retries exceeded (stale WORKING recovery)"
                logger.error(f"Embedding request {r.id} failed after {settings.max_retries} retries")
            else:
                r.status = EmbeddingRequestStatus.TO_WORK
                r.worker_id = None
                logger.warning(f"Reset stale embedding request {r.id} (retry {r.retry_count})")

        search_repo = SearchRequestRepository(session)
        stale_searches = await search_repo.get_stale_working(
            stale_minutes=settings.stale_working_minutes
        )

        for r in stale_searches:
            r.retry_count += 1
            if r.retry_count >= settings.max_retries:
                r.status = SearchRequestStatus.ERROR
                r.error_message = "Max retries exceeded (stale WORKING recovery)"
            else:
                r.status = SearchRequestStatus.TO_WORK
                r.worker_id = None

    total = len(stale_embeds) + len(stale_searches)
    if total > 0:
        logger.warning(f"Recovered {total} stale WORKING rows")


async def recalculate_searches():
    """Every 1h: re-queue completed searches for recalculation."""
    if not settings.recalculation_enabled:
        return

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(
            hours_old=settings.recalculation_hours_old,
            limit=settings.recalculation_batch_size,
        )

        if not searches:
            return

        for s in searches:
            s.status = SearchRequestStatus.TO_WORK
            s.worker_id = None

    trigger = get_batch_trigger("search")
    if trigger:
        await trigger.notify(count=len(searches))

    logger.info(f"Queued {len(searches)} searches for recalculation")


async def cleanup_old_requests():
    """Every 24h: delete completed/errored rows older than cleanup_days."""
    cutoff = datetime.utcnow() - timedelta(days=settings.cleanup_days)

    async with get_session() as session:
        result = await session.execute(
            delete(EmbeddingRequest).where(
                and_(
                    EmbeddingRequest.status.in_([
                        EmbeddingRequestStatus.DONE,
                        EmbeddingRequestStatus.ERROR,
                    ]),
                    EmbeddingRequest.created_at < cutoff,
                )
            )
        )
        embed_count = result.rowcount

        result = await session.execute(
            delete(SearchRequest).where(
                and_(
                    SearchRequest.status.in_([
                        SearchRequestStatus.COMPLETED,
                        SearchRequestStatus.ERROR,
                    ]),
                    SearchRequest.created_at < cutoff,
                )
            )
        )
        search_count = result.rowcount

    if embed_count or search_count:
        logger.info(f"Cleanup: deleted {embed_count} embed + {search_count} search old rows")
