# Phase 2: Qdrant — Payload Indices + MatchAny Filter

Two changes, both scoped to [src/infrastructure/vector_db/qdrant_repository.py](../../src/infrastructure/vector_db/qdrant_repository.py):

1. **Three new payload indices** on `evidence_embeddings` — and critically, they must be created even for collections that already exist (today's code skips this path)
2. **`MatchAny` branch in `search_similar`** so list-valued filter conditions work for the new `weapon_classes` field

## Change 1: Payload indices

### Current state (the rollout gotcha)

[qdrant_repository.py:37-102](../../src/infrastructure/vector_db/qdrant_repository.py#L37-L102) only creates payload indices inside `if not collection_exists:`. For any existing Qdrant collection (including dev, staging, prod), no new indices will be created on startup — the collection already exists, so the whole index-creation block is skipped.

This means: **simply adding three more `create_payload_index` calls inside that block will fix new environments only, not the ones we actually care about.**

### Fix: extract index creation into an idempotent helper

Restructure to always ensure indices exist, regardless of whether the collection is new or existing. Qdrant's `create_payload_index` is **idempotent by default** — calling it on an existing index is a no-op (it returns success without error, or raises `UnexpectedResponse` which we catch and log as info-level).

```python
# src/infrastructure/vector_db/qdrant_repository.py

from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,          # NEW
    MatchValue,
    PointStruct,
    UpdateStatus,
    VectorParams,
)

# Field name → schema type mapping. Add new indices here.
_EVIDENCE_PAYLOAD_INDICES: list[tuple[str, str]] = [
    ("source_type", "keyword"),
    ("camera_id", "keyword"),
    ("evidence_id", "keyword"),
    # Multi-tenant
    ("user_id", "keyword"),
    ("device_id", "keyword"),
    ("app_id", "integer"),
    # Weapons enrichment (Phase 2)
    ("weapon_analyzed", "bool"),
    ("has_weapon", "bool"),
    ("weapon_classes", "keyword"),
]


class QdrantVectorRepository(VectorRepository):

    async def initialize(self) -> None:
        """Initialize Qdrant client, create collection + ensure payload indices."""
        try:
            # ... existing client creation ...

            collections = self.client.get_collections().collections
            collection_exists = any(c.name == self.collection_name for c in collections)

            if not collection_exists:
                logger.info(f"Creating collection '{self.collection_name}'")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
                )

            # Always run — idempotent on existing indices.
            # This is important for rolling out new indices to existing environments.
            self._ensure_payload_indices(self.collection_name, _EVIDENCE_PAYLOAD_INDICES)

            # ... search_queries collection logic unchanged ...

        except Exception as e:
            logger.error(f"Error initializing Qdrant: {e}")
            raise

    def _ensure_payload_indices(
        self, collection_name: str, indices: list[tuple[str, str]]
    ) -> None:
        """Create payload indices idempotently. Safe to call on every startup."""
        for field_name, field_schema in indices:
            try:
                self.client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                )
                logger.debug(f"Payload index ensured: {collection_name}.{field_name}")
            except UnexpectedResponse as e:
                # Index already exists or similar benign conflict — safe to ignore.
                # Qdrant's behavior varies by version: some return 200 on duplicate,
                # others raise UnexpectedResponse with a conflict message.
                logger.debug(f"Payload index {field_name} already present: {e}")
            except Exception as e:
                # Log but don't raise — a missing index makes queries slower,
                # not incorrect. Failing startup because of index creation is worse.
                logger.warning(
                    f"Failed to create payload index {field_name}: {e}. "
                    f"Filtering on this field may be slow until the index exists."
                )
```

Key points:

- **The `_EVIDENCE_PAYLOAD_INDICES` list is the source of truth.** Anyone adding a new filterable field to the Qdrant payload adds one line here — no more scattered `create_payload_index` calls.
- **The helper is called unconditionally on every startup.** Idempotent. If you add a new index next month, every running replica picks it up on next deploy without a manual reindex.
- **Failures are logged, not raised.** A missing payload index means the filter path is slower (Qdrant falls back to full scan), not broken. Crashing startup over an index is the wrong tradeoff.
- **The existing three indices and the multi-tenant three are included in the list** — they become idempotent too, which is a nice side effect. If you ever wipe and recreate the collection, the ordering is preserved.

### New indices summary

| Field | Schema | Purpose |
|---|---|---|
| `weapon_analyzed` | `bool` | `weapons_filter="analyzed_clean"` needs `weapon_analyzed=true` |
| `has_weapon` | `bool` | All three non-default filter modes use this |
| `weapon_classes` | `keyword` | Array field — `MatchAny` checks if any stored class matches any requested class |

Qdrant's `keyword` index handles both scalar and array-valued fields. When the stored value is a list, `MatchAny(any=[...])` asks "does any element of the stored list appear in the requested list?" — which is exactly the semantics we want for "images with at least one of these weapon classes".

## Change 2: `MatchAny` branch in `search_similar`

### Current state

[qdrant_repository.py:182-189](../../src/infrastructure/vector_db/qdrant_repository.py#L182-L189):

```python
search_filter = None
if filter_conditions:
    must_conditions = []
    for field, value in filter_conditions.items():
        must_conditions.append(FieldCondition(key=field, match=MatchValue(value=value)))

    if must_conditions:
        search_filter = Filter(must=must_conditions)
```

This emits `MatchValue` for every entry. Passing `{"weapon_classes": ["handgun"]}` produces `MatchValue(value=["handgun"])`, which Qdrant interprets as "the field equals the exact list `["handgun"]`" — wrong semantics, and it never matches a stored list like `["handgun", "knife"]`.

### Fix

Branch on the value type:

```python
search_filter = None
if filter_conditions:
    must_conditions = []
    for field, value in filter_conditions.items():
        if isinstance(value, list):
            # Subset match — "field contains any of these values".
            # Used by weapon_classes (see docs/weapons/02_QDRANT.md).
            must_conditions.append(
                FieldCondition(key=field, match=MatchAny(any=value))
            )
        else:
            # Scalar exact match (existing behavior).
            must_conditions.append(
                FieldCondition(key=field, match=MatchValue(value=value))
            )

    if must_conditions:
        search_filter = Filter(must=must_conditions)
```

Import addition at the top of the file:

```python
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,       # NEW
    MatchValue,
    PointStruct,
    UpdateStatus,
    VectorParams,
)
```

That's it — ~8 lines including the import. All existing callers work unchanged because they only pass scalar values today.

### Why not always use `MatchAny`?

`MatchAny([single_value])` would work, but:
- It obscures intent — "match exactly X" reads differently from "match any of [X, Y]"
- It's marginally slower for the scalar case (Qdrant's query planner handles `MatchValue` more directly)
- The branch is one line of code; there's no maintenance win in collapsing it

## Store paths — no changes needed

`store_embedding` and `store_embeddings_batch` already pass their `metadata` dict through verbatim as the Qdrant point payload. When the consumer (Phase 3) adds `weapon_analyzed`, `has_weapon`, and `weapon_classes` keys to the metadata dict, they land in Qdrant without any code change here.

## Startup ordering

In [src/main.py](../../src/main.py)'s lifespan, Qdrant `initialize()` must run **before** the stream consumers start. Check that this is already the case (it should be — it's the same order as the existing multi-tenant indices). If anyone later reorders lifespan so consumers start first, the first few messages' weapon fields will still be written, but filtering on them will use fall-back full-scan until the index is built, which then happens lazily on first query. Not broken, just slower — but ordering the initialization correctly avoids the edge case entirely.

## Verification

Once the code is deployed:

```bash
# 1. Indices exist
curl -s http://localhost:6333/collections/evidence_embeddings | python3 -m json.tool | grep -A1 payload_schema
# Should show weapon_analyzed (bool), has_weapon (bool), weapon_classes (keyword)

# 2. Filter works with MatchAny
curl -X POST http://localhost:6333/collections/evidence_embeddings/points/search \
  -H "Content-Type: application/json" \
  -d '{
    "vector": [0.01, 0.02, ... 512 floats ...],
    "limit": 10,
    "filter": {
      "must": [
        {"key": "has_weapon", "match": {"value": true}},
        {"key": "weapon_classes", "match": {"any": ["handgun"]}}
      ]
    },
    "with_payload": true
  }'
```

The second request should return only points with `has_weapon=true` AND `weapon_classes` containing `"handgun"`. Before Phase 3 ships, the result will be empty (no points have weapon fields yet) — that's expected, and the important thing is the 200 response (no "unknown field" error from Qdrant, which would mean the index is missing).
