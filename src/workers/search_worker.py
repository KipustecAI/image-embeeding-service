"""Search execution worker — receives pre-computed query vector, searches Qdrant.

No CLIP model. No image downloading. Just vector search.
"""

import logging
from datetime import datetime
from typing import Dict, List
from uuid import UUID

import numpy as np
from sqlalchemy import update

from ..db.models.constants import SearchRequestStatus, SimilarityStatus
from ..db.models.search_request import SearchRequest
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..infrastructure.vector_db.qdrant_repository import QdrantVectorRepository
logger = logging.getLogger(__name__)
settings = get_settings()


class SearchExecutionWorker:
    """Receives pre-computed query vector → Qdrant search → store results."""

    def __init__(self):
        from .embedding_worker import _vector_repo
        self.vector_repo = _vector_repo

    async def process_batch(self, request_ids: List[str]) -> Dict:
        succeeded = 0
        failed = 0

        for rid in request_ids:
            try:
                # Get the request to find the search_id
                async with get_session() as session:
                    request = await session.get(SearchRequest, UUID(rid))
                    if not request:
                        await self._mark_error(rid, "Request not found")
                        failed += 1
                        continue
                    search_id = request.search_id

                # Get pre-computed vector from DB
                vector_data = request.vector_data
                if not vector_data:
                    await self._mark_error(rid, "No cached query vector found")
                    failed += 1
                    continue

                query_vector = np.array(vector_data["vector"], dtype=np.float32)
                threshold = vector_data.get("threshold", 0.75)
                max_results = vector_data.get("max_results", 50)
                search_metadata = vector_data.get("metadata")

                # Build filter conditions
                filter_conditions = None
                if search_metadata:
                    filter_conditions = {}
                    if "camera_id" in search_metadata:
                        filter_conditions["camera_id"] = search_metadata["camera_id"]
                    if "object_type" in search_metadata:
                        filter_conditions["object_type"] = search_metadata["object_type"]

                # Search Qdrant
                matches = await self.vector_repo.search_similar(
                    query_vector=query_vector,
                    limit=max_results,
                    threshold=threshold,
                    filter_conditions=filter_conditions,
                )

                total_matches = len(matches)
                similarity_status = (
                    SimilarityStatus.MATCHES_FOUND if total_matches > 0
                    else SimilarityStatus.NO_MATCHES
                )

                # Update DB
                async with get_session() as session:
                    await session.execute(
                        update(SearchRequest)
                        .where(SearchRequest.id == UUID(rid))
                        .values(
                            status=SearchRequestStatus.COMPLETED,
                            similarity_status=similarity_status,
                            total_matches=total_matches,
                            processing_completed_at=datetime.utcnow(),
                        )
                    )

                succeeded += 1
                logger.info(f"Search {rid}: {total_matches} matches (threshold={threshold})")

            except Exception as e:
                await self._mark_error(rid, str(e))
                failed += 1
                logger.error(f"Search {rid} failed: {e}")

        logger.info(f"Search batch: {succeeded} succeeded, {failed} failed")
        return {"processed": len(request_ids), "succeeded": succeeded, "failed": failed}

    async def _mark_error(self, request_id: str, error: str):
        async with get_session() as session:
            await session.execute(
                update(SearchRequest)
                .where(SearchRequest.id == UUID(request_id))
                .values(
                    status=SearchRequestStatus.ERROR,
                    error_message=error[:500],
                    processing_completed_at=datetime.utcnow(),
                )
            )


async def process_image_searches_batch(ctx: Dict, request_ids: List[str]) -> Dict:
    """ARQ-registered task for batch search execution."""
    worker = SearchExecutionWorker()
    return await worker.process_batch(request_ids)
