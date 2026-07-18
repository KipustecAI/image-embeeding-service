"""Dedicated Qdrant repository for the on-demand image-index collection.

Standalone from `QdrantVectorRepository` on purpose — this is the real
isolation requirement (docs/image-index/00_DESIGN.md §4). Its `_ensure_collection`
is NEVER called from the live `QdrantVectorRepository.initialize()`, which raises
on any failure and is awaited unguarded in the lifespan; wiring the new
collection there would let a create/index failure wedge the live
evidence/search/blacklist path.

The repo SHARES the live repo's `QdrantClient` (injected into `initialize`)
rather than constructing a second client. Isolation comes from the separate
ensure + latch, not from a separate client.

Every synchronous qdrant-client call runs under `asyncio.to_thread` — consumers
bridge handlers onto the single shared event loop via run_coroutine_threadsafe,
so a 100-item upsert(wait=True) on the loop thread would stall the live
embed/search handlers.
"""

import asyncio
import logging
import uuid

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    UpdateStatus,
    VectorParams,
)

from ..config import Settings

logger = logging.getLogger(__name__)

# FROZEN — changing this re-keys every point. See docs/image-index/00_DESIGN.md §4.
IMAGE_INDEX_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lookia.image-index.v1")

# ids-only, face-style payload: the 512-D vector IS the payload. Keyword indices
# ONLY on the filter fields; item_index/item_ref/source_url are stored-but-unindexed.
_IMAGE_INDEX_PAYLOAD_INDICES: list[tuple[str, str]] = [
    ("user_id", "keyword"),
    ("external_id", "keyword"),
    ("batch_id", "keyword"),
]


def image_index_point_id(batch_id: str, item_index: int) -> str:
    """Deterministic point-id → a redelivered result overwrites the SAME point.

    Recomputable with no Qdrant round-trip; the reference row stores the same value.
    """
    return str(uuid.uuid5(IMAGE_INDEX_NS, f"{batch_id}_{item_index}"))


class ImageIndexVectorRepository:
    """Standalone repo for the dedicated `image_index_embeddings` collection."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: QdrantClient | None = None
        self.collection_name = settings.qdrant_collection_image_index
        self.vector_size = settings.qdrant_vector_size  # reuse the live 512-D knob
        self._ensured = False

    async def initialize(self, client: QdrantClient) -> None:
        """Accept the SHARED live client and ensure the dedicated collection.

        Idempotent: collection_exists-guarded, never recreate/delete.
        """
        self.client = client
        await self._ensure_collection()

    async def _ensure_collection(self) -> None:
        """Create the collection + payload indices if absent. Own latch.

        Never called from QdrantVectorRepository.initialize() — this is the
        isolation boundary.
        """
        if self._ensured:
            return
        if self.client is None:
            raise RuntimeError("ImageIndexVectorRepository.initialize(client) not called")

        def _sync_ensure() -> None:
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            if not exists:
                logger.info("Creating image-index collection '%s'", self.collection_name)
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size, distance=Distance.COSINE
                    ),
                )
                logger.info("Collection '%s' created", self.collection_name)
            else:
                logger.info("Collection '%s' already exists", self.collection_name)
            # Idempotent; create_payload_index conflicts swallowed as benign
            # (matches the live repo policy — a missing index only slows filters).
            for field_name, field_schema in _IMAGE_INDEX_PAYLOAD_INDICES:
                try:
                    self.client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field_name,
                        field_schema=field_schema,
                    )
                except UnexpectedResponse as e:
                    logger.debug(
                        "Payload index %s already present on %s: %s",
                        field_name,
                        self.collection_name,
                        e,
                    )
                except Exception as e:  # noqa: BLE001 — benign; filters degrade, not fail
                    logger.warning(
                        "Failed to create payload index %s on %s: %s",
                        field_name,
                        self.collection_name,
                        e,
                    )

        await asyncio.to_thread(_sync_ensure)
        self._ensured = True

    async def upsert_items(self, points: list[dict]) -> bool:
        """Batch-upsert embedded items into the dedicated collection.

        Each ``points`` entry: {batch_id, item_index, vector, user_id,
        external_id?, item_ref?, source_url?}. Point-id is deterministic
        (redelivery overwrites in place); distance=COSINE; NO re-normalization
        (Qdrant normalizes internally; compute already returns a CLIP vector).
        Empty input is a fast no-op.
        """
        if not points:
            return True
        if self.client is None:
            logger.error("upsert_items called before initialize()")
            return False

        structs: list[PointStruct] = []
        for p in points:
            batch_id = str(p["batch_id"])
            item_index = int(p["item_index"])
            point_id = image_index_point_id(batch_id, item_index)
            payload = {
                "user_id": p.get("user_id"),
                "external_id": p.get("external_id"),
                "batch_id": batch_id,
                "item_index": item_index,
                "item_ref": p.get("item_ref"),
                "source_url": p.get("source_url"),
            }
            structs.append(PointStruct(id=point_id, vector=p["vector"], payload=payload))

        def _sync_upsert() -> bool:
            result = self.client.upsert(
                collection_name=self.collection_name, points=structs, wait=True
            )
            if result.status == UpdateStatus.COMPLETED:
                return True
            logger.error("image-index upsert failed: %s", result)
            return False

        try:
            ok = await asyncio.to_thread(_sync_upsert)
            if ok:
                logger.info(
                    "Upserted %d points into %s", len(structs), self.collection_name
                )
            return ok
        except Exception as e:  # noqa: BLE001
            logger.error("Error upserting %d image-index points: %s", len(structs), e)
            return False

    async def delete_by_batch(self, batch_id: str) -> bool:
        """Filter-delete a whole batch's points. NOT auto-invoked in v1.

        Provided for optional re-run cleanup; external_id scoping surfaces the
        newest run so v1 never calls this.
        """
        if self.client is None:
            logger.error("delete_by_batch called before initialize()")
            return False

        def _sync_delete() -> bool:
            result = self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(
                    must=[FieldCondition(key="batch_id", match=MatchValue(value=str(batch_id)))]
                ),
                wait=True,
            )
            if result.status == UpdateStatus.COMPLETED:
                return True
            logger.error("image-index delete_by_batch failed: %s", result)
            return False

        try:
            return await asyncio.to_thread(_sync_delete)
        except Exception as e:  # noqa: BLE001
            logger.error("Error deleting image-index batch %s: %s", batch_id, e)
            return False
