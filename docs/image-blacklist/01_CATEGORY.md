# Phase 01: Category Infrastructure *(prerequisite)*

Adds a `category` field that travels from the ETL producer through the backend, lands in Qdrant as an indexed payload field, and becomes a filter on the search API. Independently useful even if the blacklist feature is never shipped. Required by Phase 02+.

## Scope

- **New optional field** `category: string | null` on the `embeddings:results` payload (producer contract change).
- **New nullable column** `embedding_requests.category TEXT`, propagated to `evidence_embeddings.json_data` and the Qdrant point payload.
- **New Qdrant payload index** on `category` (keyword, supports `MatchAny` for subset filtering).
- **New optional field** `category` on `POST /api/v1/search` request body, accepting a single string or a list of strings. Uses the existing `MatchAny` branch in `qdrant_repository.search_similar`.
- **No UI for assigning category after the fact.** Category arrives from the producer only in v1. `PATCH /api/v1/evidence/{id}` is deferred (see README "Out of scope").

## Files modified

| File | Change |
|---|---|
| `alembic/versions/xxx_add_category_to_embedding_requests.py` | **Create** migration |
| [src/db/models/embedding_request.py](../../src/db/models/embedding_request.py) | Add `category` column |
| [src/db/repositories/embedding_request_repo.py](../../src/db/repositories/embedding_request_repo.py) | Extend `create_request` signature |
| [src/infrastructure/vector_db/qdrant_repository.py](../../src/infrastructure/vector_db/qdrant_repository.py) | Add `("category", "keyword")` to `_EVIDENCE_PAYLOAD_INDICES` |
| [src/streams/embedding_results_consumer.py](../../src/streams/embedding_results_consumer.py) | Read `payload["category"]`, inject into Qdrant metadata + DB record |
| [src/main.py](../../src/main.py) | Add `category: str \| list[str] \| None = None` to `SearchCreateRequest`, stash in `search_metadata` |
| [src/streams/search_results_consumer.py](../../src/streams/search_results_consumer.py) | Extend filter allow-list with `category` |
| [docs/new_arq_v2/04_STREAM_CONTRACTS.md](../new_arq_v2/04_STREAM_CONTRACTS.md) | Document the optional `category` field in the `embeddings:results` contract |
| [docs/API_REFERENCE.md](../API_REFERENCE.md) | Document the `category` search filter |

## Database migration

```python
# alembic/versions/xxx_add_category_to_embedding_requests.py

from alembic import op
import sqlalchemy as sa

revision = "<auto>"
down_revision = "c8e5a7b2d4f9"  # The weapon_analysis_error migration
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embedding_requests",
        sa.Column("category", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_embedding_requests_category",
        "embedding_requests",
        ["category"],
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_requests_category", table_name="embedding_requests")
    op.drop_column("embedding_requests", "category")
```

- **Nullable** — legacy evidence has no category, which is fine.
- **Simple B-tree index** — low-cardinality expected (~5-20 distinct categories), cheap.
- **TEXT not VARCHAR(50)** — no benefit to the length cap, and keeps the door open for long category labels if product needs them.
- No `evidence_embeddings.category` column — the value is evidence-level, stored once per request, propagated to each point's Qdrant payload at ingest time.

## Model change

```python
# src/db/models/embedding_request.py

class EmbeddingRequest(Base):
    # ... existing columns ...
    category = Column(Text, nullable=True, index=True)
```

Just add the column. Existing rows stay NULL. No backfill.

## Repository signature

```python
# src/db/repositories/embedding_request_repo.py

async def create_request(
    self,
    # ... existing args ...
    weapon_analysis_error: str | None = None,
    category: str | None = None,   # NEW
) -> EmbeddingRequest:
    request = EmbeddingRequest(
        # ... existing fields ...
        weapon_analysis_error=weapon_analysis_error,
        category=category,          # NEW
    )
    ...
```

## Qdrant payload index

Append one line to `_EVIDENCE_PAYLOAD_INDICES` in [qdrant_repository.py](../../src/infrastructure/vector_db/qdrant_repository.py):

```python
_EVIDENCE_PAYLOAD_INDICES: list[tuple[str, str]] = [
    # ... existing entries ...
    ("weapon_classes", "keyword"),
    # Category — Phase 01 of image-blacklist. Keyword supports MatchAny.
    ("category", "keyword"),
]
```

The idempotent `_ensure_payload_indices` helper picks it up on next startup — same rollout story as the weapons indices.

## Consumer change

In `embedding_results_consumer._process_embeddings_result`:

```python
# Read category from the payload (optional, default None)
category = payload.get("category")

# When building each ImageEmbedding:
additional_metadata={
    "image_index": emb.get("image_index", 0),
    "user_id": user_id,
    # ... existing fields ...
    "category": category,   # NEW — propagates to Qdrant payload
}

# When creating the DB request row:
request = await repo.create_request(
    # ... existing args ...
    weapon_analysis_error=weapon_error_message,
    category=category,      # NEW
)
```

If `category` is absent from the payload, it flows through as `None` — legacy producers that never send it keep working unchanged.

## Search API

Extend `SearchCreateRequest` in [main.py](../../src/main.py):

```python
class SearchCreateRequest(BaseModel):
    image_url: str
    threshold: float = 0.75
    max_results: int = 50
    metadata: dict | None = None
    weapons_filter: Literal["all", "only", "exclude", "analyzed_clean"] = "all"
    weapon_classes: list[str] | None = None
    # Phase 01 of image-blacklist
    category: str | list[str] | None = None
```

In the handler:

```python
if body.category:
    # list → MatchAny in Qdrant (same path as weapon_classes)
    # scalar → MatchValue
    metadata["category"] = body.category
```

In [search_results_consumer.py](../../src/streams/search_results_consumer.py), extend the filter allow-list:

```python
if search_metadata:
    if "camera_id" in search_metadata:
        filter_conditions["camera_id"] = search_metadata["camera_id"]
    if "object_type" in search_metadata:
        filter_conditions["object_type"] = search_metadata["object_type"]
    if "category" in search_metadata:    # NEW
        filter_conditions["category"] = search_metadata["category"]
    filter_conditions.update(build_weapon_filter_conditions(search_metadata))
```

Same in the recalculation handler in [main.py](../../src/main.py).

## Producer contract update

Add one row to the `embeddings:results` field table in [docs/new_arq_v2/04_STREAM_CONTRACTS.md](../new_arq_v2/04_STREAM_CONTRACTS.md):

| Field | Type | Required | Description |
|---|---|---|---|
| `category` | string \| null | **no** (optional) | Human-assigned evidence category, e.g. `"vehicle"`, `"scene"`, `"person"`, `"infraction_pattern"`. No enum — free-form string. Used for search filtering and (in Phase 05) blacklist auto-match scoping. Null means "uncategorized". Legacy producers that don't send this field stay on the same path as before. |

Also flow through the ETL → `evidence:embed` step so the upstream producer passes it along. That's a coordination item for the ETL team.

## Validation rules *(extensions to the existing table in CONTRACT.md)*

| Condition | Backend behavior | Rationale |
|---|---|---|
| `category` field missing | Treated as `None`. Stored as NULL. Legacy path. | Backwards compat |
| `category: ""` (empty string) | Stored as empty string, not NULL | Caller's choice — not our job to normalize |
| `category` with leading/trailing whitespace | Stored verbatim | Ditto |
| `category` casing | Stored verbatim | Producers should normalize to lowercase at source, but we don't enforce |

Two deliberate choices:
- **No normalization on our side.** The producer decides what `"vehicle"` vs `"Vehicle"` vs `" vehicle "` means. If ops hits this pain later, we add a normalization step at ingest. Don't preempt.
- **No enum validation.** Categories evolve; hard-coding an enum guarantees a migration every time product adds a category. Free-form string + convention is the right default.

## Tests

### New unit tests

Add a new file `tests/test_category_search.py` (or extend `test_weapon_filters.py`):

```python
def test_search_metadata_category_scalar():
    # category="vehicle" → Qdrant MatchValue
    ...

def test_search_metadata_category_list():
    # category=["vehicle", "scene"] → Qdrant MatchAny
    ...

def test_search_metadata_category_missing():
    # category absent → no Qdrant condition added
    ...
```

### DB tests

Extend `tests/test_db.py` with a `category` column assertion (mirrors the `weapon_analysis_error` tests):

```python
async def test_create_request_persists_category(session):
    ...
    assert found.category == "vehicle"
```

## Verification

After `make migrate` and restart:

1. Column exists: `\d embedding_requests` shows `category TEXT` with `ix_embedding_requests_category` index.
2. Qdrant payload index: `curl http://localhost:6333/collections/evidence_embeddings | grep category` returns a keyword schema entry.
3. Ingest a message with `category="vehicle"` — assert the Qdrant point payload contains `"category": "vehicle"` and the DB row has `category="vehicle"`.
4. Ingest a message without `category` — assert behavior matches legacy (no error, `category=NULL`).
5. `POST /api/v1/search` with `category="vehicle"` — assert only vehicle-category matches return.
6. `POST /api/v1/search` with `category=["vehicle", "scene"]` — assert both categories match (MatchAny).

Once all six boxes check, Phase 01 is done and Phase 02 can start.

## Hooks for future extensions

- **Admin PATCH to set category post-ingest.** Straightforward addition: `PATCH /api/v1/evidence/{id}` that updates `embedding_requests.category` AND issues a Qdrant `set_payload` call on all points for that evidence. Keeps DB and Qdrant in sync. Not shipped in v1 because no one has asked for it.
- **Per-category threshold overrides on search.** If product wants `category="vehicle"` searches to use a tighter default threshold than `category="scene"` searches, add a lookup table in config. Out of scope for v1.
- **Category taxonomy as a separate table.** If the list of valid categories grows and needs governance, promote to a FK constraint. Massive overengineering until we have 20+ categories. Deferred.
