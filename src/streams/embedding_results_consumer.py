"""Consumer for embeddings:results stream — receives pre-computed vectors from GPU service.

Stores vectors directly in Qdrant + DB in the consumer (no ARQ queue needed).
The operation is fast (~70ms) so there's no benefit to deferring it.
"""

import asyncio
import logging
from datetime import datetime
from uuid import uuid4

import numpy as np

from ..db.models.constants import EmbeddingRequestStatus
from ..db.models.evidence_embedding import EvidenceEmbeddingRecord
from ..db.repositories import EmbeddingRequestRepository
from ..domain.entities import ImageEmbedding
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)
settings = get_settings()

_event_loop: asyncio.AbstractEventLoop | None = None
_vector_repo = None  # QdrantVectorRepository, set at startup


def set_results_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def set_vector_repo(repo):
    global _vector_repo
    _vector_repo = repo


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


def _handle_embeddings_computed(event_type: str, payload: dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_embeddings_result(payload, message_id),
        _event_loop,
    )
    future.result(timeout=300)


def _handle_compute_error(event_type: str, payload: dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_compute_error(payload),
        _event_loop,
    )
    future.result(timeout=30)


async def _process_embeddings_result(payload: dict, message_id: str):
    """Receive pre-computed vectors → store in Qdrant + DB directly (no ARQ queue)."""
    evidence_id = payload.get("evidence_id", "")
    camera_id = payload.get("camera_id", "")
    embeddings_data = payload.get("embeddings", [])

    if not evidence_id or not embeddings_data:
        logger.warning(f"Skipping result with missing data: evidence_id={evidence_id}")
        return

    # Dedup check
    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if await repo.check_duplicate(evidence_id):
            logger.info(f"Skipping duplicate evidence {evidence_id}")
            return

    try:
        # 1. Build Qdrant embeddings + DB records
        qdrant_embeddings: list[ImageEmbedding] = []
        db_records: list[EvidenceEmbeddingRecord] = []

        for emb in embeddings_data:
            point_id = str(uuid4())
            vector = np.array(emb["vector"], dtype=np.float32)

            embedding = ImageEmbedding.from_evidence(
                evidence_id=evidence_id,
                vector=vector,
                image_url=emb["image_url"],
                camera_id=camera_id,
                additional_metadata={
                    "image_index": emb["image_index"],
                    "total_images": emb["total_images"],
                },
            )
            embedding.id = point_id
            qdrant_embeddings.append(embedding)

            db_records.append(
                EvidenceEmbeddingRecord(
                    qdrant_point_id=point_id,
                    image_index=emb["image_index"],
                    image_url=emb["image_url"],
                    json_data=embedding.metadata,
                )
            )

        # 2. Store in Qdrant (single bulk upsert)
        if _vector_repo and qdrant_embeddings:
            await _vector_repo.store_embeddings_batch(qdrant_embeddings)

        # 3. Create DB request + embedding records in one transaction
        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            request = await repo.create_request(
                evidence_id=evidence_id,
                camera_id=camera_id,
                image_urls=[e["image_url"] for e in embeddings_data],
                stream_msg_id=message_id,
            )

            # Link DB records to the request
            for record in db_records:
                record.request_id = request.id
            session.add_all(db_records)

            # Mark as EMBEDDED directly
            request.status = EmbeddingRequestStatus.EMBEDDED
            request.processing_completed_at = datetime.utcnow()

        logger.info(
            f"Stored {len(qdrant_embeddings)} vectors for evidence {evidence_id} "
            f"(input={payload.get('input_count')}, filtered={payload.get('filtered_count')})"
        )

    except Exception as e:
        logger.error(f"Failed to store embeddings for {evidence_id}: {e}", exc_info=True)
        # Create error row for tracking
        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            if not await repo.check_duplicate(evidence_id):
                request = await repo.create_request(
                    evidence_id=evidence_id,
                    camera_id=camera_id,
                    image_urls=[],
                    stream_msg_id=message_id,
                )
                request.status = EmbeddingRequestStatus.ERROR
                request.error_message = str(e)[:500]


async def _process_compute_error(payload: dict):
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
            request.status = EmbeddingRequestStatus.ERROR
            request.error_message = f"Compute error: {error}"

    logger.error(f"Compute error for evidence {entity_id}: {error}")
