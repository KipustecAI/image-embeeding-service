"""Redis Streams handler for evidence:embed events.

Creates EmbeddingRequest DB rows and notifies the BatchTrigger.
"""

import asyncio
import logging
from typing import Dict, Optional

from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..db.repositories import EmbeddingRequestRepository
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)

settings = get_settings()

_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def create_evidence_embed_consumer() -> StreamConsumer:
    """Factory: creates a StreamConsumer for evidence:embed events."""
    consumer = StreamConsumer(
        stream=settings.stream_evidence_embed,
        group=settings.stream_consumer_group,
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
    consumer.register_handler("evidence.ready.embed", _handle_evidence_ready)
    return consumer


def _handle_evidence_ready(event_type: str, payload: Dict, message_id: str):
    """Called from daemon thread — bridge to asyncio."""
    if _event_loop is None:
        logger.error("Event loop not set — call set_event_loop() at startup")
        raise RuntimeError("Event loop not set")

    future = asyncio.run_coroutine_threadsafe(
        _process_evidence_embed(payload, message_id),
        _event_loop,
    )
    future.result(timeout=300)


async def _process_evidence_embed(payload: Dict, message_id: str):
    """Create an EmbeddingRequest row and notify the batch trigger."""
    evidence_id = payload.get("evidence_id", "")
    camera_id = payload.get("camera_id", "")
    raw_image_urls = payload.get("image_urls", [])

    if not evidence_id or not raw_image_urls:
        logger.warning(f"Skipping evidence event with missing data: {payload}")
        return

    # Apply diversity filter before creating DB row
    from ..services.diversity_filter import get_diversity_filter

    diversity = get_diversity_filter()
    filtered_urls = await diversity.filter_image_urls(raw_image_urls)

    if not filtered_urls:
        logger.warning(f"No images passed diversity filter for evidence {evidence_id}")
        return

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)

        # Dedup check
        if await repo.check_duplicate(evidence_id):
            logger.info(f"Skipping duplicate evidence {evidence_id}")
            return

        request = await repo.create_request(
            evidence_id=evidence_id,
            camera_id=camera_id,
            image_urls=filtered_urls,
            stream_msg_id=message_id,
        )
        await session.commit()

    # Notify BatchTrigger
    from ..services.batch_trigger import get_batch_trigger

    trigger = get_batch_trigger("embedding")
    if trigger:
        await trigger.notify(count=1)

    logger.info(
        f"Created embedding request {request.id} for evidence {evidence_id} "
        f"({len(filtered_urls)}/{len(raw_image_urls)} images after diversity filter)"
    )
