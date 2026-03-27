"""Consumer for search:results stream — receives pre-computed query vectors from GPU service.

Stores vector in DB and triggers Qdrant search via BatchTrigger.
"""

import asyncio
import logging
from typing import Dict, Optional

from sqlalchemy import update

from ..db.models.constants import SearchRequestStatus
from ..db.models.search_request import SearchRequest
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..db.repositories import SearchRequestRepository
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)
settings = get_settings()

_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_search_results_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


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
    """Receive pre-computed query vector → store in DB → notify trigger."""
    search_id = payload.get("search_id", "")
    user_id = payload.get("user_id", "")
    vector = payload.get("vector")

    if not search_id or vector is None:
        logger.warning(f"Skipping search result with missing data: {search_id}")
        return

    threshold = payload.get("threshold", 0.75)
    max_results = payload.get("max_results", 50)
    search_metadata = payload.get("metadata")

    async with get_session() as session:
        repo = SearchRequestRepository(session)

        if await repo.check_duplicate(search_id):
            logger.info(f"Skipping duplicate search {search_id}")
            return

        request = await repo.create_request(
            search_id=search_id,
            user_id=user_id,
            image_url="(computed by GPU service)",
            threshold=threshold,
            max_results=max_results,
            metadata=search_metadata,
            stream_msg_id=message_id,
        )

        # Store vector in DB so the ARQ worker can read it
        request.vector_data = {
            "vector": vector,
            "threshold": threshold,
            "max_results": max_results,
            "metadata": search_metadata,
        }
        await session.commit()

    from ..services.batch_trigger import get_batch_trigger

    trigger = get_batch_trigger("search")
    if trigger:
        await trigger.notify(count=1)

    logger.info(f"Received query vector for search {search_id}")


async def _process_compute_error(payload: Dict):
    entity_id = payload.get("entity_id", "")
    entity_type = payload.get("entity_type", "")
    error = payload.get("error", "Unknown compute error")

    if entity_type != "search":
        return

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        if not await repo.check_duplicate(entity_id):
            await repo.create_request(
                search_id=entity_id,
                user_id="unknown",
                image_url="(failed)",
            )

        stmt = (
            update(SearchRequest)
            .where(SearchRequest.search_id == entity_id)
            .values(
                status=SearchRequestStatus.ERROR,
                error_message=f"Compute error: {error}",
            )
        )
        await session.execute(stmt)

    logger.error(f"Compute error for search {entity_id}: {error}")
