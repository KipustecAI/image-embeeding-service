"""Two-phase evidence embedding worker.

Phase 1: Parallel image download + CLIP inference (asyncio.gather with semaphore)
Phase 2: Bulk Qdrant upsert + bulk DB commit (N calls → 1 each)
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List
from uuid import UUID, uuid4

from sqlalchemy import update

from ..db.models.constants import EmbeddingRequestStatus
from ..db.models.embedding_request import EmbeddingRequest
from ..db.models.evidence_embedding import EvidenceEmbeddingRecord
from ..domain.entities import ImageEmbedding
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..infrastructure.embedding.clip_embedder import CLIPEmbedder
from ..infrastructure.vector_db.qdrant_repository import QdrantVectorRepository

logger = logging.getLogger(__name__)
settings = get_settings()

# Module-level singletons (initialized on worker startup)
_embedder: CLIPEmbedder = None
_vector_repo: QdrantVectorRepository = None


async def initialize_worker():
    """Called on ARQ worker startup to pre-load CLIP model and Qdrant client."""
    global _embedder, _vector_repo

    _embedder = CLIPEmbedder(settings)
    await _embedder.initialize()

    _vector_repo = QdrantVectorRepository(settings)
    await _vector_repo.initialize()

    logger.info("Embedding worker initialized (CLIP + Qdrant ready)")


async def cleanup_worker():
    """Called on ARQ worker shutdown."""
    global _embedder
    if _embedder:
        await _embedder.cleanup()
    logger.info("Embedding worker cleaned up")


class EmbeddingExtractionWorker:
    def __init__(self):
        self.embedder = _embedder
        self.vector_repo = _vector_repo

    async def process_batch(self, request_ids: List[str]) -> Dict:
        """Process a batch of embedding requests using two-phase approach."""

        # ── Phase 1: Parallel extraction ──
        semaphore = asyncio.Semaphore(settings.clip_batch_size)

        async def extract_with_limit(request_id: str):
            async with semaphore:
                return await self._extract_one(request_id)

        results = await asyncio.gather(
            *[extract_with_limit(rid) for rid in request_ids],
            return_exceptions=True,
        )

        # Collect successes and failures
        all_embeddings: List[ImageEmbedding] = []
        all_db_records: List[EvidenceEmbeddingRecord] = []
        succeeded_ids: List[str] = []
        failed: List[Dict] = []

        for rid, result in zip(request_ids, results):
            if isinstance(result, Exception):
                failed.append({"id": rid, "error": str(result)})
                continue
            if result.get("failed"):
                failed.append({"id": rid, "error": result.get("error", "unknown")})
                continue

            all_embeddings.extend(result["embeddings"])
            all_db_records.extend(result["db_records"])
            succeeded_ids.append(rid)

        # ── Phase 2: Bulk storage ──

        # 2a. Single Qdrant upsert
        if all_embeddings:
            await self.vector_repo.store_embeddings_batch(all_embeddings)

        # 2b. Single DB commit for embedding records
        if all_db_records:
            async with get_session() as session:
                session.add_all(all_db_records)

        # 2c. Update succeeded requests → EMBEDDED
        if succeeded_ids:
            async with get_session() as session:
                for rid in succeeded_ids:
                    stmt = (
                        update(EmbeddingRequest)
                        .where(EmbeddingRequest.id == UUID(rid))
                        .values(
                            status=EmbeddingRequestStatus.EMBEDDED,
                            processing_completed_at=datetime.utcnow(),
                        )
                    )
                    await session.execute(stmt)

        # 2d. Update failed requests → ERROR
        if failed:
            async with get_session() as session:
                for item in failed:
                    stmt = (
                        update(EmbeddingRequest)
                        .where(EmbeddingRequest.id == UUID(item["id"]))
                        .values(
                            status=EmbeddingRequestStatus.ERROR,
                            error_message=item["error"][:500],
                            processing_completed_at=datetime.utcnow(),
                        )
                    )
                    await session.execute(stmt)

        logger.info(
            f"Embedding batch done: {len(succeeded_ids)} succeeded, {len(failed)} failed, "
            f"{len(all_embeddings)} vectors stored"
        )

        return {
            "processed": len(request_ids),
            "succeeded": len(succeeded_ids),
            "failed": len(failed),
            "vectors_stored": len(all_embeddings),
        }

    async def _extract_one(self, request_id: str) -> Dict:
        """Phase 1: extract embeddings for one request (parallel-safe, no storage)."""
        result = {"embeddings": [], "db_records": [], "failed": False, "error": None}

        async with get_session() as session:
            request = await session.get(EmbeddingRequest, UUID(request_id))
            if not request:
                result["failed"] = True
                result["error"] = "Request not found"
                return result

            image_urls = request.image_urls or []
            evidence_id = request.evidence_id
            camera_id = request.camera_id

        for idx, image_url in enumerate(image_urls):
            try:
                vector = await self.embedder.generate_embedding(image_url)
                if vector is None:
                    logger.warning(f"CLIP returned None for {image_url}")
                    continue

                point_id = str(uuid4())

                embedding = ImageEmbedding.from_evidence(
                    evidence_id=evidence_id,
                    vector=vector,
                    image_url=image_url,
                    camera_id=camera_id,
                    additional_metadata={
                        "image_index": idx,
                        "total_images": len(image_urls),
                    },
                )
                embedding.id = point_id
                result["embeddings"].append(embedding)

                result["db_records"].append(
                    EvidenceEmbeddingRecord(
                        request_id=UUID(request_id),
                        qdrant_point_id=point_id,
                        image_index=idx,
                        image_url=image_url,
                        json_data=embedding.metadata,
                    )
                )

            except Exception as e:
                logger.error(f"Failed to embed image {idx} for request {request_id}: {e}")

        if not result["embeddings"]:
            result["failed"] = True
            result["error"] = "No images could be embedded"

        return result


# ── ARQ task function ──

async def process_evidence_embeddings_batch(ctx: Dict, request_ids: List[str]) -> Dict:
    """ARQ-registered task for batch embedding extraction."""
    worker = EmbeddingExtractionWorker()
    return await worker.process_batch(request_ids)
