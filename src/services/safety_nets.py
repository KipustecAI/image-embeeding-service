"""Safety net jobs — scheduled fallbacks that catch anything the primary path misses.

Under normal operation these find 0 rows. If they find rows, something upstream
(stream consumer) may be broken.
"""

import logging
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import and_, delete

from ..db.models.constants import (
    EmbeddingRequestStatus,
    SearchRequestStatus,
    SearchType,
    SimilarityStatus,
)
from ..db.models.embedding_request import EmbeddingRequest
from ..db.models.search_match import SearchMatch
from ..db.models.search_request import SearchRequest
from ..db.repositories import EmbeddingRequestRepository, SearchRequestRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session

logger = logging.getLogger(__name__)
settings = get_settings()

# Vector repo reference — set from lifespan
_vector_repo = None
# Dedicated image-index collection repo — wired only inside the gated lifespan
# block; None when the feature is off → the image-index recalc branch no-ops.
_image_index_vector_repo = None


def set_vector_repo(repo):
    global _vector_repo
    _vector_repo = repo


def set_image_index_vector_repo(repo):
    global _image_index_vector_repo
    _image_index_vector_repo = repo


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
                logger.error(
                    f"Embedding request {r.id} failed after {settings.max_retries} retries"
                )
            else:
                r.status = EmbeddingRequestStatus.TO_WORK
                r.worker_id = None
                logger.warning(f"Reset stale embedding request {r.id} (retry {r.retry_count})")

        search_repo = SearchRequestRepository(session)
        stale_searches = await search_repo.get_stale_working(
            stale_minutes=settings.stale_working_minutes
        )

        # Image-index rows are inserted at WORKING and there is NO worker that
        # re-dispatches TO_WORK searches (the API dispatches once at POST), so a
        # reset→TO_WORK would hang forever. Terminalize them to ERROR directly,
        # but only past the longer image-index compute cutoff (M7, §6.4).
        image_index_cutoff = datetime.utcnow() - timedelta(
            seconds=settings.image_index_max_compute_seconds
        )
        for r in stale_searches:
            if r.search_type == SearchType.IMAGE_INDEX:
                if r.processing_started_at and r.processing_started_at < image_index_cutoff:
                    r.status = SearchRequestStatus.ERROR
                    r.error_message = "Image-index compute timed out (stale WORKING)"
                    logger.error(
                        "Terminalized stale image-index search %s to ERROR", r.search_id
                    )
                # else: not yet past the image-index cutoff — leave WORKING.
                continue
            # ── evidence rows: existing reset logic, untouched ──
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
    """Every 1h: re-search Qdrant with stored query vectors. No GPU needed."""
    if not settings.recalculation_enabled:
        return

    if not _vector_repo:
        logger.warning("Vector repo not set — skipping recalculation")
        return

    # Image-index recalc (§6.4) — an independent pass with its own query +
    # collection. Runs before the evidence early-return so it is never skipped.
    await _recalculate_image_index_searches()

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(
            hours_old=settings.recalculation_hours_old,
            limit=settings.recalculation_batch_size,
        )

        if not searches:
            return

        recalculated = 0
        for s in searches:
            if not s.qdrant_query_point_id:
                logger.debug(f"Search {s.search_id} has no stored query vector, skipping")
                continue

            query_vector = await _vector_repo.retrieve_query_vector(s.qdrant_query_point_id)
            if query_vector is None:
                logger.warning(f"Query vector not found in Qdrant for search {s.search_id}")
                continue

            # Build filter conditions
            filter_conditions = None
            if s.search_metadata:
                filter_conditions = {}
                if "camera_id" in s.search_metadata:
                    filter_conditions["camera_id"] = s.search_metadata["camera_id"]
                if "object_type" in s.search_metadata:
                    filter_conditions["object_type"] = s.search_metadata["object_type"]
                if not filter_conditions:
                    filter_conditions = None

            matches = await _vector_repo.search_similar(
                query_vector=np.array(query_vector, dtype=np.float32),
                limit=s.max_results,
                threshold=s.threshold,
                filter_conditions=filter_conditions,
            )

            # Delete old matches
            await session.execute(delete(SearchMatch).where(SearchMatch.search_request_id == s.id))

            for match in matches:
                session.add(
                    SearchMatch(
                        search_request_id=s.id,
                        evidence_id=str(match.evidence_id),
                        camera_id=str(match.camera_id) if match.camera_id else None,
                        similarity_score=match.similarity_score,
                        image_url=match.image_url,
                        match_metadata=match.metadata,
                    )
                )

            s.total_matches = len(matches)
            s.similarity_status = (
                SimilarityStatus.MATCHES_FOUND if len(matches) > 0 else SimilarityStatus.NO_MATCHES
            )
            s.processing_completed_at = datetime.utcnow()
            recalculated += 1

    if recalculated > 0:
        logger.info(f"Recalculated {recalculated} searches directly via Qdrant")


async def _recalculate_image_index_searches():
    """Re-search landed image-index rows against ``image_index_embeddings`` (§6.4).

    Delivers recalculation-against-new-index for free: images landing later under
    the same ``external_ids`` fold in on the next pass. Scoped by user_id +
    external_ids on the dedicated collection — NEVER the evidence collection.
    No-op when the dedicated repo is not wired (feature off).
    """
    if _image_index_vector_repo is None:
        return

    from ..db.models.search_match import SearchMatch as _SearchMatch

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_image_index_recalculation(
            hours_old=settings.recalculation_hours_old,
            limit=settings.recalculation_batch_size,
        )
        if not searches:
            return

        recalculated = 0
        for s in searches:
            if not s.qdrant_query_point_id:
                continue
            query_vector = await _vector_repo.retrieve_query_vector(s.qdrant_query_point_id)
            if query_vector is None:
                logger.warning(
                    "Query vector not found for image-index search %s", s.search_id
                )
                continue

            matches = await _image_index_vector_repo.search_similar(
                np.array(query_vector, dtype=np.float32),
                user_id=s.user_id,
                external_ids=s.external_ids,
                top_k=s.max_results,
                threshold=s.threshold,
            )

            await session.execute(
                delete(_SearchMatch).where(_SearchMatch.search_request_id == s.id)
            )
            for m in matches:
                point_id = m.get("qdrant_point_id")
                evidence_id = m.get("image_id") or point_id  # NOT-NULL fallback (M2)
                session.add(
                    _SearchMatch(
                        search_request_id=s.id,
                        evidence_id=str(evidence_id),
                        camera_id=None,
                        similarity_score=m.get("score"),
                        image_url=m.get("source_url"),
                        external_id=m.get("external_id"),
                        match_metadata={
                            "batch_id": m.get("batch_id"),
                            "item_index": m.get("item_index"),
                            "item_ref": m.get("item_ref"),
                            "qdrant_point_id": point_id,
                            "search_type": SearchType.IMAGE_INDEX,
                        },
                    )
                )

            s.total_matches = len(matches)
            s.similarity_status = (
                SimilarityStatus.MATCHES_FOUND if matches else SimilarityStatus.NO_MATCHES
            )
            s.processing_completed_at = datetime.utcnow()
            recalculated += 1

    if recalculated > 0:
        logger.info("Recalculated %d image-index searches", recalculated)


async def cleanup_old_requests():
    """Every 24h: delete completed/errored rows older than cleanup_days."""
    cutoff = datetime.utcnow() - timedelta(days=settings.cleanup_days)

    async with get_session() as session:
        result = await session.execute(
            delete(EmbeddingRequest).where(
                and_(
                    EmbeddingRequest.status.in_(
                        [
                            EmbeddingRequestStatus.DONE,
                            EmbeddingRequestStatus.ERROR,
                        ]
                    ),
                    EmbeddingRequest.created_at < cutoff,
                )
            )
        )
        embed_count = result.rowcount

        result = await session.execute(
            delete(SearchRequest).where(
                and_(
                    SearchRequest.status.in_(
                        [
                            SearchRequestStatus.COMPLETED,
                            SearchRequestStatus.ERROR,
                        ]
                    ),
                    SearchRequest.created_at < cutoff,
                )
            )
        )
        search_count = result.rowcount

    if embed_count or search_count:
        logger.info(f"Cleanup: deleted {embed_count} embed + {search_count} search old rows")
