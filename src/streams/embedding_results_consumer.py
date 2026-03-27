"""Consumer for embeddings:results stream — receives pre-computed vectors from GPU service.

Creates DB rows with vector_data and triggers Qdrant storage via BatchTrigger.
"""

import asyncio
import logging
from typing import Dict, Optional

from sqlalchemy import update

from ..db.models.constants import EmbeddingRequestStatus
from ..db.models.embedding_request import EmbeddingRequest
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..db.repositories import EmbeddingRequestRepository
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)
settings = get_settings()

_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_results_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def create_embedding_results_consumer() -> StreamConsumer:
    """Factory: creates a consumer for embeddings:results from the GPU service."""
    consumer = StreamConsumer(
        stream=settings.stream_embeddings_results,
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
    consumer.register_handler("embeddings.computed", _handle_embeddings_computed)
    consumer.register_handler("compute.error", _handle_compute_error)
    return consumer


def _handle_embeddings_computed(event_type: str, payload: Dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_embeddings_result(payload, message_id),
        _event_loop,
    )
    future.result(timeout=300)


def _handle_compute_error(event_type: str, payload: Dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_compute_error(payload),
        _event_loop,
    )
    future.result(timeout=30)


async def _process_embeddings_result(payload: Dict, message_id: str):
    """Receive pre-computed vectors → store in DB row → notify trigger."""
    evidence_id = payload.get("evidence_id", "")
    camera_id = payload.get("camera_id", "")
    embeddings = payload.get("embeddings", [])

    if not evidence_id or not embeddings:
        logger.warning(f"Skipping result with missing data: evidence_id={evidence_id}")
        return

    image_urls = [e["image_url"] for e in embeddings]

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)

        if await repo.check_duplicate(evidence_id):
            logger.info(f"Skipping duplicate evidence {evidence_id}")
            return

        # Create request with vector_data stored in DB (not in-memory cache)
        request = await repo.create_request(
            evidence_id=evidence_id,
            camera_id=camera_id,
            image_urls=image_urls,
            stream_msg_id=message_id,
        )

        # Store the pre-computed vectors in the DB row so the ARQ worker can read them
        request.vector_data = embeddings
        await session.commit()

    # Notify BatchTrigger
    from ..services.batch_trigger import get_batch_trigger

    trigger = get_batch_trigger("embedding")
    if trigger:
        await trigger.notify(count=1)

    logger.info(
        f"Received {len(embeddings)} embeddings for evidence {evidence_id} "
        f"(input={payload.get('input_count')}, filtered={payload.get('filtered_count')})"
    )


async def _process_compute_error(payload: Dict):
    """Mark the request as ERROR when the compute service fails."""
    entity_id = payload.get("entity_id", "")
    entity_type = payload.get("entity_type", "")
    error = payload.get("error", "Unknown compute error")

    if entity_type != "evidence":
        return

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if not await repo.check_duplicate(entity_id):
            request = await repo.create_request(
                evidence_id=entity_id,
                camera_id="unknown",
                image_urls=[],
            )

        stmt = (
            update(EmbeddingRequest)
            .where(EmbeddingRequest.evidence_id == entity_id)
            .values(
                status=EmbeddingRequestStatus.ERROR,
                error_message=f"Compute error: {error}",
            )
        )
        await session.execute(stmt)

    logger.error(f"Compute error for evidence {entity_id}: {error}")
