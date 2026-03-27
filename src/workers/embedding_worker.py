"""Embedding storage worker — receives pre-computed vectors, stores in Qdrant + DB.

No CLIP model. No image downloading. Just storage.
"""

import logging
from datetime import datetime
from typing import Dict, List
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import update

from ..db.models.constants import EmbeddingRequestStatus
from ..db.models.embedding_request import EmbeddingRequest
from ..db.models.evidence_embedding import EvidenceEmbeddingRecord
from ..domain.entities import ImageEmbedding
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..infrastructure.vector_db.qdrant_repository import QdrantVectorRepository
from ..streams.embedding_results_consumer import get_pending_vectors

logger = logging.getLogger(__name__)
settings = get_settings()

# Module-level singleton (initialized on worker startup)
_vector_repo: QdrantVectorRepository = None


async def initialize_worker():
    """Called on ARQ worker startup — initialize Qdrant client."""
    global _vector_repo
    _vector_repo = QdrantVectorRepository(settings)
    await _vector_repo.initialize()
    logger.info("Storage worker initialized (Qdrant ready)")


async def cleanup_worker():
    logger.info("Storage worker cleaned up")


class EmbeddingStorageWorker:
    """Stores pre-computed vectors in Qdrant + DB records."""

    def __init__(self):
        self.vector_repo = _vector_repo

    async def process_batch(self, request_ids: List[str]) -> Dict:
        all_embeddings: List[ImageEmbedding] = []
        all_db_records: List[EvidenceEmbeddingRecord] = []
        succeeded_ids: List[str] = []
        failed: List[Dict] = []

        for rid in request_ids:
            try:
                # Get the request to find the evidence_id
                async with get_session() as session:
                    request = await session.get(EmbeddingRequest, UUID(rid))
                    if not request:
                        failed.append({"id": rid, "error": "Request not found"})
                        continue
                    evidence_id = request.evidence_id
                    camera_id = request.camera_id

                # Get cached vectors from the consumer
                vectors_data = get_pending_vectors(evidence_id)
                if not vectors_data:
                    failed.append({"id": rid, "error": "No cached vectors found"})
                    continue

                for emb in vectors_data["embeddings"]:
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
                    all_embeddings.append(embedding)

                    all_db_records.append(
                        EvidenceEmbeddingRecord(
                            request_id=UUID(rid),
                            qdrant_point_id=point_id,
                            image_index=emb["image_index"],
                            image_url=emb["image_url"],
                            json_data=embedding.metadata,
                        )
                    )

                succeeded_ids.append(rid)

            except Exception as e:
                failed.append({"id": rid, "error": str(e)})
                logger.error(f"Failed to prepare vectors for {rid}: {e}")

        # Bulk Qdrant upsert
        if all_embeddings:
            await self.vector_repo.store_embeddings_batch(all_embeddings)

        # Bulk DB commit
        if all_db_records:
            async with get_session() as session:
                session.add_all(all_db_records)

        # Update statuses
        if succeeded_ids:
            async with get_session() as session:
                for rid in succeeded_ids:
                    await session.execute(
                        update(EmbeddingRequest)
                        .where(EmbeddingRequest.id == UUID(rid))
                        .values(
                            status=EmbeddingRequestStatus.EMBEDDED,
                            processing_completed_at=datetime.utcnow(),
                        )
                    )

        if failed:
            async with get_session() as session:
                for item in failed:
                    await session.execute(
                        update(EmbeddingRequest)
                        .where(EmbeddingRequest.id == UUID(item["id"]))
                        .values(
                            status=EmbeddingRequestStatus.ERROR,
                            error_message=item["error"][:500],
                            processing_completed_at=datetime.utcnow(),
                        )
                    )

        logger.info(
            f"Storage batch: {len(succeeded_ids)} stored, {len(failed)} failed, "
            f"{len(all_embeddings)} vectors in Qdrant"
        )

        return {
            "processed": len(request_ids),
            "succeeded": len(succeeded_ids),
            "failed": len(failed),
            "vectors_stored": len(all_embeddings),
        }


async def process_evidence_embeddings_batch(ctx: Dict, request_ids: List[str]) -> Dict:
    """ARQ-registered task for batch embedding storage."""
    worker = EmbeddingStorageWorker()
    return await worker.process_batch(request_ids)
