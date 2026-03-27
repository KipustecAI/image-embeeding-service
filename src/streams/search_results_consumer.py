"""Consumer for search:results stream — receives pre-computed query vectors from GPU service.

Executes Qdrant search + stores results directly in the consumer (no ARQ queue needed).
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

import numpy as np
from sqlalchemy import update

from ..db.models.constants import SearchRequestStatus, SimilarityStatus
from ..db.models.search_request import SearchRequest
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..db.repositories import SearchRequestRepository
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)
settings = get_settings()

_event_loop: Optional[asyncio.AbstractEventLoop] = None
_vector_repo = None  # Set at startup


def set_search_results_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def set_search_vector_repo(repo):
    global _vector_repo
    _vector_repo = repo


def create_search_results_consumer() -> StreamConsumer:
    consumer = StreamConsumer(
        stream=settings.stream_search_results,
        group=settings.stream_backend_group,
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
    consumer.register_handler("search.vector.computed", _handle_search_computed)
    consumer.register_handler("compute.error", _handle_compute_error)
    return consumer


def _handle_search_computed(event_type: str, payload: Dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_search_result(payload, message_id),
        _event_loop,
    )
    future.result(timeout=120)


def _handle_compute_error(event_type: str, payload: Dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_compute_error(payload),
        _event_loop,
    )
    future.result(timeout=30)


async def _process_search_result(payload: Dict, message_id: str):
    """Receive query vector → search Qdrant → store results directly (no ARQ queue)."""
    search_id = payload.get("search_id", "")
    user_id = payload.get("user_id", "")
    vector = payload.get("vector")

    if not search_id or vector is None:
        logger.warning(f"Skipping search result with missing data: {search_id}")
        return

    threshold = payload.get("threshold", 0.75)
    max_results = payload.get("max_results", 50)
    search_metadata = payload.get("metadata")

    # Dedup
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        if await repo.check_duplicate(search_id):
            logger.info(f"Skipping duplicate search {search_id}")
            return

    try:
        query_vector = np.array(vector, dtype=np.float32)

        # Build filter conditions
        filter_conditions = None
        if search_metadata:
            filter_conditions = {}
            if "camera_id" in search_metadata:
                filter_conditions["camera_id"] = search_metadata["camera_id"]
            if "object_type" in search_metadata:
                filter_conditions["object_type"] = search_metadata["object_type"]

        # Search Qdrant directly
        matches = []
        if _vector_repo:
            matches = await _vector_repo.search_similar(
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

        # Create DB row with results
        async with get_session() as session:
            repo = SearchRequestRepository(session)
            request = await repo.create_request(
                search_id=search_id,
                user_id=user_id,
                image_url="(computed by GPU service)",
                threshold=threshold,
                max_results=max_results,
                metadata=search_metadata,
                stream_msg_id=message_id,
            )
            request.status = SearchRequestStatus.COMPLETED
            request.similarity_status = similarity_status
            request.total_matches = total_matches
            request.processing_completed_at = datetime.utcnow()

        logger.info(f"Search {search_id}: {total_matches} matches (threshold={threshold})")

    except Exception as e:
        logger.error(f"Failed to process search {search_id}: {e}", exc_info=True)
        async with get_session() as session:
            repo = SearchRequestRepository(session)
            if not await repo.check_duplicate(search_id):
                request = await repo.create_request(
                    search_id=search_id,
                    user_id=user_id,
                    image_url="(failed)",
                    stream_msg_id=message_id,
                )
                request.status = SearchRequestStatus.ERROR
                request.error_message = str(e)[:500]


async def _process_compute_error(payload: Dict):
    entity_id = payload.get("entity_id", "")
    entity_type = payload.get("entity_type", "")
    error = payload.get("error", "Unknown compute error")

    if entity_type != "search":
        return

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        if not await repo.check_duplicate(entity_id):
            request = await repo.create_request(
                search_id=entity_id,
                user_id="unknown",
                image_url="(failed)",
            )
            request.status = SearchRequestStatus.ERROR
            request.error_message = f"Compute error: {error}"

    logger.error(f"Compute error for search {entity_id}: {error}")
