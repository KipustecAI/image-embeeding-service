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
import math
import uuid

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    UpdateStatus,
    VectorParams,
)

from ..config import Settings

logger = logging.getLogger(__name__)

# FROZEN — changing this re-keys every point. See docs/image-index/00_DESIGN.md §4.
IMAGE_INDEX_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lookia.image-index.v1")

# CLIP variant identifier stamped into every indexed point's payload (S1, §3).
# Mirrors the same literal the blacklist path already writes
# (blacklist_embed_service._MODEL_VERSION) so Capability-B can compare a blacklist
# point's model_version against the indexed point's and log-and-skip on mismatch.
_MODEL_VERSION = "clip-vit-b-32"


def _is_finite_nonzero(vec) -> bool:
    """True only for a real, comparable CLIP vector.

    Cosine is undefined for a zero/NaN vector (§3): a failed embed that leaked
    into a stored point — or a zero/NaN query vector — would produce garbage
    rather than an empty result. Guards `search_similar` (and the Cap-B xref
    core) so we skip-and-log instead of issuing the Qdrant search. Returns False
    for empty, None-bearing, non-numeric, NaN/inf, or all-zero vectors.
    """
    if not vec:
        return False
    total = 0.0
    for x in vec:
        if x is None:
            return False
        try:
            f = float(x)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(f):
            return False
        total += f * f
    return total > 0.0

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
                # Additive model-version stamp (S1, §3) — enables the Cap-B
                # cross-collection same-space guard. Reuses the blacklist literal.
                "model_version": _MODEL_VERSION,
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

    async def search_similar(
        self,
        query_vector,
        *,
        user_id: str,
        external_ids: list[str] | None = None,
        batch_id: str | None = None,
        top_k: int = 50,
        threshold: float = 0.75,
    ) -> list[dict]:
        """Cosine search over the dedicated collection under a CLOSED filter (§5.1).

        The filter is fixed (no free-form dict → no metadata-allow-list surface):
        - ``user_id`` ALWAYS in ``must`` (MatchValue) — the IDOR enforcement point
          at the vector layer; a foreign/cross-tenant ``external_id`` yields zero
          intersection → ``[]``.
        - ``external_ids`` (list → MatchAny) and ``batch_id`` (scalar → MatchValue)
          optional; all three are keyword payload indices so the filter is
          index-served.
        Distance is already COSINE on the collection; ``score_threshold`` prunes.
        A degenerate zero/NaN ``query_vector`` is skipped-and-logged → ``[]`` (§3, S4).
        Returns ``list[dict]`` (not ``SearchResult``, which forces evidence UUIDs).
        """
        if self.client is None:
            logger.error("search_similar called before initialize()")
            return []
        vec = query_vector.tolist() if hasattr(query_vector, "tolist") else list(query_vector)
        if not _is_finite_nonzero(vec):
            logger.warning(
                "search_similar skipped: zero/NaN query vector (user=%s)", user_id
            )
            return []

        must = [FieldCondition(key="user_id", match=MatchValue(value=str(user_id)))]
        if external_ids:
            must.append(
                FieldCondition(
                    key="external_id", match=MatchAny(any=[str(e) for e in external_ids])
                )
            )
        if batch_id:
            must.append(
                FieldCondition(key="batch_id", match=MatchValue(value=str(batch_id)))
            )
        query_filter = Filter(must=must)

        def _sync():
            return self.client.search(
                collection_name=self.collection_name,
                query_vector=vec,
                limit=top_k,
                score_threshold=threshold,
                query_filter=query_filter,
                with_payload=True,
            )

        try:
            hits = await asyncio.to_thread(_sync)
        except Exception as e:  # noqa: BLE001 — match live repo: log-and-empty, never raise
            logger.error("image-index search failed (user=%s): %s", user_id, e)
            return []
        return [self._hit_to_match(h) for h in hits]

    @staticmethod
    def _hit_to_match(hit) -> dict:
        """Map a Qdrant hit → a pure-payload match dict (no DB join, §5.1).

        Every field is a payload key ``upsert_items`` writes. ``image_id`` falls
        back to the point-id when ``item_ref`` is null (NOT-NULL fallback, §4);
        ``model_version`` is surfaced for the Cap-B cross-collection guard (§3).
        """
        p = hit.payload or {}
        point_id = str(hit.id)
        return {
            "external_id": p.get("external_id"),
            "batch_id": p.get("batch_id"),
            "item_index": p.get("item_index"),
            "item_ref": p.get("item_ref"),
            "image_id": p.get("item_ref") or point_id,  # NOT NULL fallback (§4)
            "source_url": p.get("source_url"),
            "score": hit.score,
            "qdrant_point_id": point_id,
            "user_id": p.get("user_id"),
            "model_version": p.get("model_version"),  # B cross-collection guard (§3)
        }

    async def get_point_vector(self, point_id: str) -> list[float] | None:
        """Fetch one indexed point's stored vector from this collection (§5.2).

        ``asyncio.to_thread``-wrapped (S6) — runs on the shared consumer loop.
        Returns ``None`` on a missing/orphaned point; every caller must treat
        ``None`` as skip-and-log and never pass it into ``search_similar``.
        """
        if self.client is None:
            logger.error("get_point_vector called before initialize()")
            return None

        def _sync():
            return self.client.retrieve(
                collection_name=self.collection_name,
                ids=[point_id],
                with_vectors=True,
            )

        try:
            res = await asyncio.to_thread(_sync)
            return res[0].vector if res else None
        except Exception as e:  # noqa: BLE001
            logger.error("get_point_vector failed (%s): %s", point_id, e)
            return None
