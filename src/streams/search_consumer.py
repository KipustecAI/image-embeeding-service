"""Redis Streams handler for evidence:search events.

Creates SearchRequest DB rows and notifies the search BatchTrigger.
"""

import asyncio
import logging

from ..db.repositories import SearchRequestRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)

settings = get_settings()

_event_loop: asyncio.AbstractEventLoop | None = None


def set_search_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def create_evidence_search_consumer() -> StreamConsumer:
    """Factory: creates a StreamConsumer for evidence:search events."""
    consumer = StreamConsumer(
        stream=settings.stream_evidence_search,
        group=settings.stream_search_group,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password,
        redis_db=settings.redis_streams_db,
        block_ms=settings.stream_consumer_block_ms,
        batch_size=settings.stream_consumer_batch_size,
        reclaim_idle_ms=settings.stream_reclaim_idle_ms,
        dead_letter_max_retries=settings.stream_dead_letter_max_retries,
        concurrency=settings.stream_consumer_concurrency,
    )
    consumer.register_handler("search.created", _handle_search_created)
    return consumer


def _handle_search_created(event_type: str, payload: dict, message_id: str):
    """Called from daemon thread — bridge to asyncio."""
    if _event_loop is None:
        logger.error("Event loop not set — call set_search_event_loop() at startup")
        raise RuntimeError("Event loop not set")

    future = asyncio.run_coroutine_threadsafe(
        _process_search_created(payload, message_id),
        _event_loop,
    )
    future.result(timeout=300)


async def _process_search_created(payload: dict, message_id: str):
    """Create a SearchRequest row and notify the batch trigger."""
    search_id = payload.get("search_id", "")
    user_id = payload.get("user_id", "")
    image_url = payload.get("image_url", "")

    if not search_id or not image_url:
        logger.warning(f"Skipping search event with missing data: {payload}")
        return

    threshold = payload.get("threshold", 0.75)
    max_results = payload.get("max_results", 50)
    search_metadata = payload.get("metadata")

    async with get_session() as session:
        repo = SearchRequestRepository(session)

        # Dedup check
        if await repo.check_duplicate(search_id):
            logger.info(f"Skipping duplicate search {search_id}")
            return

        request = await repo.create_request(
            search_id=search_id,
            user_id=user_id,
            image_url=image_url,
            threshold=threshold,
            max_results=max_results,
            metadata=search_metadata,
            stream_msg_id=message_id,
        )
        await session.commit()

    # Notify BatchTrigger
    from ..services.batch_trigger import get_batch_trigger

    trigger = get_batch_trigger("search")
    if trigger:
        await trigger.notify(count=1)

    logger.info(f"Created search request {request.id} for search {search_id}")
