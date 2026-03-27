"""Search processing worker.

Downloads query image, generates CLIP embedding, searches Qdrant,
stores results in Redis with TTL.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List
from uuid import UUID

from sqlalchemy import update

from ..db.models.constants import SearchRequestStatus, SimilarityStatus
from ..db.models.search_request import SearchRequest
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session

logger = logging.getLogger(__name__)
settings = get_settings()


class SearchWorker:
    def __init__(self):
        # Import from embedding_worker module singletons
        from .embedding_worker import _embedder, _vector_repo

        self.embedder = _embedder
        self.vector_repo = _vector_repo

    async def process_batch(self, request_ids: List[str]) -> Dict:
        """Process a batch of search requests."""

        # Phase 1: Parallel download + CLIP embed for each query image
        semaphore = asyncio.Semaphore(4)

        async def embed_query(request_id: str):
            async with semaphore:
                return await self._embed_and_search(request_id)

        results = await asyncio.gather(
            *[embed_query(rid) for rid in request_ids],
            return_exceptions=True,
        )

        succeeded = 0
        failed = 0

        for rid, result in zip(request_ids, results):
            if isinstance(result, Exception):
                await self._mark_error(rid, str(result))
                failed += 1
            elif result.get("failed"):
                await self._mark_error(rid, result.get("error", "unknown"))
                failed += 1
            else:
                succeeded += 1

        logger.info(
            f"Search batch done: {succeeded} succeeded, {failed} failed"
        )

        return {
            "processed": len(request_ids),
            "succeeded": succeeded,
            "failed": failed,
        }

    async def _embed_and_search(self, request_id: str) -> Dict:
        """Download query image, embed, search Qdrant, store results."""
        async with get_session() as session:
            request = await session.get(SearchRequest, UUID(request_id))
            if not request:
                return {"failed": True, "error": "Request not found"}

            image_url = request.image_url
            threshold = request.threshold or 0.75
            max_results = request.max_results or 50
            search_metadata = request.search_metadata

        # Generate CLIP embedding for query image
        vector = await self.embedder.generate_embedding(image_url)
        if vector is None:
            return {"failed": True, "error": "Failed to generate embedding for query image"}

        # Build filter conditions from metadata
        filter_conditions = None
        if search_metadata:
            filter_conditions = {}
            if "camera_id" in search_metadata:
                filter_conditions["camera_id"] = search_metadata["camera_id"]
            if "object_type" in search_metadata:
                filter_conditions["object_type"] = search_metadata["object_type"]

        # Search Qdrant
        matches = await self.vector_repo.search_similar(
            query_vector=vector,
            limit=max_results,
            threshold=threshold,
            filter_conditions=filter_conditions,
        )

        total_matches = len(matches)
        similarity_status = (
            SimilarityStatus.MATCHES_FOUND if total_matches > 0
            else SimilarityStatus.NO_MATCHES
        )

        # Update search request in DB
        async with get_session() as session:
            stmt = (
                update(SearchRequest)
                .where(SearchRequest.id == UUID(request_id))
                .values(
                    status=SearchRequestStatus.COMPLETED,
                    similarity_status=similarity_status,
                    total_matches=total_matches,
                    processing_completed_at=datetime.utcnow(),
                )
            )
            await session.execute(stmt)

        # TODO: Store results in dedicated Redis DB with TTL (per Q9 decision)

        logger.info(
            f"Search {request_id} completed: {total_matches} matches "
            f"(threshold={threshold})"
        )

        return {"failed": False, "total_matches": total_matches}

    async def _mark_error(self, request_id: str, error: str):
        async with get_session() as session:
            stmt = (
                update(SearchRequest)
                .where(SearchRequest.id == UUID(request_id))
                .values(
                    status=SearchRequestStatus.ERROR,
                    error_message=error[:500],
                    processing_completed_at=datetime.utcnow(),
                )
            )
            await session.execute(stmt)


# ── ARQ task function ──

async def process_image_searches_batch(ctx: Dict, request_ids: List[str]) -> Dict:
    """ARQ-registered task for batch search processing."""
    worker = SearchWorker()
    return await worker.process_batch(request_ids)
