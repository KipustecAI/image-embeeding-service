# Fix: Search Recalculation Pipeline

## Problem

Recalculation is broken. Both the scheduled job (`recalculate_searches` every 1h) and the manual API endpoint (`POST /api/v1/recalculate/searches`) reset completed search rows to `status=TO_WORK`, but **nothing picks them up**.

In the old architecture, the ARQ worker + BatchTrigger processed TO_WORK rows. Those were removed when we moved to direct storage in stream consumers. Now the only thing that processes searches is the `search_results_consumer`, which only listens to the `search:results` Redis Stream from the GPU.

### What recalculation does

Re-runs old completed searches against the current Qdrant state so they pick up matches from newly embedded evidence.

```
Monday: User searches → 3 matches
Tuesday: 100 new images embedded into Qdrant
Wednesday: Recalculation re-runs Monday's search → 8 matches (5 new)
```

### Current broken flow

```
recalculate_searches()
    → sets search rows to status=TO_WORK
    → calls get_batch_trigger("search") → returns None (never created)
    → rows sit at TO_WORK forever
```

---

## Option A: Re-publish to GPU (quick fix, no migration)

The simplest fix that reuses the existing pipeline. Recalculation re-publishes the search request to `evidence:search`, the GPU re-computes the query vector, and the normal `search_results_consumer` handles the rest.

### Changes

#### 1. `src/services/safety_nets.py` — `recalculate_searches()`

```python
async def recalculate_searches():
    """Every 1h: re-run completed searches by re-publishing to GPU stream."""
    if not settings.recalculation_enabled:
        return

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(
            hours_old=settings.recalculation_hours_old,
            limit=settings.recalculation_batch_size,
        )

        if not searches:
            return

        for s in searches:
            # Delete old matches (will be replaced by new results)
            s.matches.clear()
            s.status = SearchRequestStatus.WORKING
            s.total_matches = 0
            s.similarity_status = SimilarityStatus.NO_MATCHES
            s.processing_started_at = datetime.utcnow()
            s.processing_completed_at = None

    # Re-publish each search to the GPU stream
    producer = _stream_producer  # set from lifespan
    for s in searches:
        producer.publish(
            stream=settings.stream_evidence_search,
            event_type="search.recalculate",
            payload={
                "search_id": s.search_id,
                "user_id": s.user_id,
                "image_url": s.image_url,
                "threshold": s.threshold,
                "max_results": s.max_results,
                "metadata": s.search_metadata,
            },
        )

    logger.info(f"Re-published {len(searches)} searches for recalculation")
```

#### 2. `src/main.py` — Pass StreamProducer to safety_nets

```python
from src.services.safety_nets import set_stream_producer

# In lifespan, after creating stream_producer:
set_stream_producer(stream_producer)
```

#### 3. `src/services/safety_nets.py` — Add producer reference

```python
_stream_producer = None

def set_stream_producer(producer):
    global _stream_producer
    _stream_producer = producer
```

#### 4. `src/main.py` — Fix manual recalculation endpoint

Same pattern: re-publish to GPU stream instead of just setting status=TO_WORK.

#### 5. `search_results_consumer.py` — Handle re-created matches

The existing consumer already handles this correctly: it finds the existing `SearchRequest` by `search_id`, updates its status, and creates new `SearchMatch` rows. The only issue is that old matches must be deleted first — handled by `s.matches.clear()` in the recalculation step above (cascade).

### Pros
- Minimal code changes (4 files)
- Reuses existing pipeline end-to-end
- No DB migration needed

### Cons
- Requires GPU for every recalculation (image re-download + CLIP inference)
- Adds ~2s latency per search recalculation
- If image_url is expired/deleted, recalculation fails

---

## Option B: Store query vector in Qdrant, skip GPU (recommended)

Instead of storing the full 512-dim vector in PostgreSQL (4KB JSONB per row), we store the query vector as a Qdrant point in a lightweight `search_queries` collection, and save only the Qdrant point ID (a UUID) in the DB.

### Architecture

```
                        Qdrant
                    ┌──────────────────────────────┐
                    │  evidence_embeddings          │  ← main collection (evidence vectors)
                    │  - 512-dim cosine vectors     │
                    │  - payload: camera_id, etc.   │
                    │                               │
                    │  search_queries               │  ← NEW: lightweight collection
                    │  - 512-dim cosine vectors     │
                    │  - payload: search_id only    │
                    │  - no payload indices needed   │
                    └──────────────────────────────┘

                      PostgreSQL
                    ┌──────────────────────────────┐
                    │  search_requests              │
                    │  + qdrant_query_point_id (UUID)│  ← NEW column (just a string)
                    └──────────────────────────────┘
```

### Data flow — first search

```
POST /api/v1/search
    → GPU computes query vector
    → search_results_consumer receives vector
    → 1. Search evidence_embeddings → matches
    → 2. Upsert query vector into search_queries collection → point_id
    → 3. Save point_id in search_requests.qdrant_query_point_id
    → 4. Store matches in search_matches table
    → Done (~2s total, same as today)
```

### Data flow — recalculation (NO GPU needed)

```
recalculate_searches() or POST /api/v1/recalculate/searches
    → Read qdrant_query_point_id from DB
    → client.retrieve("search_queries", [point_id], with_vectors=True)  → ~1ms
    → Search evidence_embeddings with that vector                       → ~70ms
    → Delete old search_matches rows
    → Insert new search_matches rows
    → Update search_requests status → COMPLETED
    → Done (~100ms per search, no GPU, no image download)
```

### Changes

#### 1. Alembic migration — add `qdrant_query_point_id` column

```python
"""add qdrant_query_point_id to search_requests"""

def upgrade():
    op.add_column(
        'search_requests',
        sa.Column('qdrant_query_point_id', sa.String(255), nullable=True)
    )

def downgrade():
    op.drop_column('search_requests', 'qdrant_query_point_id')
```

Lightweight migration — just a nullable string column. No backfill needed (existing searches without the ID fall back to Option A re-publish).

#### 2. `src/db/models/search_request.py` — Add column

```python
qdrant_query_point_id = Column(String(255), nullable=True)
```

#### 3. `src/infrastructure/vector_db/qdrant_repository.py` — Query collection methods

```python
SEARCH_QUERIES_COLLECTION = "search_queries"

async def initialize(self):
    # ... existing evidence_embeddings setup ...

    # Create search_queries collection (lightweight, no payload indices)
    if not any(c.name == SEARCH_QUERIES_COLLECTION for c in collections):
        self.client.create_collection(
            collection_name=SEARCH_QUERIES_COLLECTION,
            vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
        )

async def store_query_vector(self, point_id: str, vector: list, search_id: str) -> bool:
    """Store a search query vector in the search_queries collection."""
    point = PointStruct(
        id=point_id,
        vector=vector,
        payload={"search_id": search_id},
    )
    result = self.client.upsert(
        collection_name=SEARCH_QUERIES_COLLECTION,
        points=[point],
        wait=True,
    )
    return result.status == UpdateStatus.COMPLETED

async def retrieve_query_vector(self, point_id: str) -> Optional[list]:
    """Retrieve a stored query vector by its Qdrant point ID."""
    results = self.client.retrieve(
        collection_name=SEARCH_QUERIES_COLLECTION,
        ids=[point_id],
        with_vectors=True,
    )
    if not results:
        return None
    return results[0].vector
```

#### 4. `src/streams/search_results_consumer.py` — Save query vector on completion

After Qdrant search, upsert the query vector and save the point ID:

```python
import uuid

async def _process_search_result(payload, message_id):
    # ... existing code: parse payload, search Qdrant ...

    # Store query vector in Qdrant for future recalculation
    query_point_id = str(uuid.uuid4())
    if _vector_repo:
        await _vector_repo.store_query_vector(
            point_id=query_point_id,
            vector=query_vector.tolist(),
            search_id=search_id,
        )

    async with get_session() as session:
        # ... existing code: find/create request, update status, create matches ...
        request.qdrant_query_point_id = query_point_id
```

#### 5. `src/services/safety_nets.py` — Direct recalculation (no GPU)

Delete all dead ARQ code. Replace `recalculate_searches()`:

```python
_vector_repo = None

def set_vector_repo(repo):
    global _vector_repo
    _vector_repo = repo

async def recalculate_searches():
    """Every 1h: re-search Qdrant with stored query vectors. No GPU needed."""
    if not settings.recalculation_enabled:
        return

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(
            hours_old=settings.recalculation_hours_old,
            limit=settings.recalculation_batch_size,
        )

        if not searches:
            return

        recalculated = 0
        for s in searches:
            # Skip searches without a stored query vector (pre-migration)
            if not s.qdrant_query_point_id:
                logger.debug(f"Search {s.search_id} has no stored query vector, skipping")
                continue

            # Retrieve query vector from Qdrant
            query_vector = await _vector_repo.retrieve_query_vector(s.qdrant_query_point_id)
            if query_vector is None:
                logger.warning(f"Query vector not found in Qdrant for search {s.search_id}")
                continue

            # Build filter conditions from search metadata
            filter_conditions = None
            if s.search_metadata:
                filter_conditions = {}
                if "camera_id" in s.search_metadata:
                    filter_conditions["camera_id"] = s.search_metadata["camera_id"]

            # Re-search Qdrant
            matches = await _vector_repo.search_similar(
                query_vector=np.array(query_vector, dtype=np.float32),
                limit=s.max_results,
                threshold=s.threshold,
                filter_conditions=filter_conditions,
            )

            # Clear old matches and insert new ones
            s.matches.clear()
            await session.flush()  # ensure deletes execute before inserts

            for match in matches:
                session.add(SearchMatch(
                    search_request_id=s.id,
                    evidence_id=str(match.evidence_id),
                    camera_id=str(match.camera_id) if match.camera_id else None,
                    similarity_score=match.similarity_score,
                    image_url=match.image_url,
                    match_metadata=match.metadata,
                ))

            s.total_matches = len(matches)
            s.similarity_status = (
                SimilarityStatus.MATCHES_FOUND if len(matches) > 0
                else SimilarityStatus.NO_MATCHES
            )
            s.processing_completed_at = datetime.utcnow()
            recalculated += 1

    if recalculated > 0:
        logger.info(f"Recalculated {recalculated} searches directly via Qdrant")
```

#### 6. `src/main.py` — Pass vector_repo to safety_nets + fix endpoint

```python
from src.services.safety_nets import set_vector_repo as set_safety_nets_vector_repo

# In lifespan, after initializing vector_repo:
set_safety_nets_vector_repo(vector_repo)
```

Fix the manual recalculation endpoint to use the same direct approach:

```python
@app.post("/api/v1/recalculate/searches")
async def trigger_recalculation(
    limit: int = Query(20, ge=1, le=100),
    hours_old: int = Query(2, ge=1, le=168),
    ctx: UserContext = Depends(get_user_context),
):
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(
            hours_old=hours_old, limit=limit
        )

        if not searches:
            return {"success": True, "message": "No searches eligible", "total": 0}

        recalculated = 0
        skipped = 0
        for s in searches:
            if not s.qdrant_query_point_id:
                skipped += 1
                continue

            query_vector = await vector_repo.retrieve_query_vector(s.qdrant_query_point_id)
            if query_vector is None:
                skipped += 1
                continue

            filter_conditions = None
            if s.search_metadata:
                filter_conditions = {k: v for k, v in s.search_metadata.items()
                                     if k in ("camera_id", "object_type")}

            matches = await vector_repo.search_similar(
                query_vector=np.array(query_vector, dtype=np.float32),
                limit=s.max_results,
                threshold=s.threshold,
                filter_conditions=filter_conditions or None,
            )

            s.matches.clear()
            await session.flush()

            for match in matches:
                session.add(SearchMatch(
                    search_request_id=s.id,
                    evidence_id=str(match.evidence_id),
                    camera_id=str(match.camera_id) if match.camera_id else None,
                    similarity_score=match.similarity_score,
                    image_url=match.image_url,
                    match_metadata=match.metadata,
                ))

            s.total_matches = len(matches)
            s.similarity_status = (
                SimilarityStatus.MATCHES_FOUND if matches
                else SimilarityStatus.NO_MATCHES
            )
            s.processing_completed_at = datetime.utcnow()
            recalculated += 1

    return {
        "success": True,
        "message": f"Recalculated {recalculated} searches ({skipped} skipped — no stored vector)",
        "total": recalculated,
        "skipped": skipped,
    }
```

### Pros
- No GPU needed for recalculation (~100ms vs ~2s per search)
- Works even if image_url is expired or deleted
- Can recalculate large batches efficiently
- PostgreSQL row stays small (UUID string vs 4KB JSONB)
- Vector stored in Qdrant where it belongs (optimized storage)
- Graceful fallback: searches without `qdrant_query_point_id` are simply skipped

### Cons
- Requires lightweight Alembic migration (one nullable string column)
- New Qdrant collection (`search_queries`) to manage
- Searches created before this change won't have stored vectors (no backfill, just skip)

---

## Dead code to clean up alongside this fix

These functions in `safety_nets.py` reference the removed ARQ pool and should be deleted:
- `set_arq_pool()`
- `dispatch_embedding_batch()`
- `dispatch_search_batch()`
- `embedding_safety_net()`
- `search_safety_net()`
- `_arq_pool` global

The `batch_trigger` import can also be removed from `safety_nets.py`.

---

## Recommendation

**Go with Option B.** The migration is trivial (one nullable column), the `search_queries` collection is created automatically on startup, and the recalculation is 20x faster without GPU dependency. Old searches without a stored vector are gracefully skipped — no backfill needed.

Option A is only useful as a temporary fallback if for some reason the Qdrant `search_queries` collection is unavailable.

---

## Files to modify

| File | Change |
|------|--------|
| `src/db/models/search_request.py` | Add `qdrant_query_point_id` column |
| `src/infrastructure/vector_db/qdrant_repository.py` | Create `search_queries` collection, add `store_query_vector()` + `retrieve_query_vector()` |
| `src/streams/search_results_consumer.py` | After search, upsert query vector to Qdrant, save point ID |
| `src/services/safety_nets.py` | Delete dead ARQ code, add vector_repo ref, rewrite `recalculate_searches()` |
| `src/main.py` | Pass vector_repo to safety_nets, rewrite recalculation endpoint |
| `alembic/versions/xxx_add_qdrant_query_point_id.py` | Migration: add nullable string column |

## New env vars

None. The `search_queries` collection name is a constant in `qdrant_repository.py`. The existing `QDRANT_*` and `RECALCULATION_*` vars cover everything.

## Verification

1. Run migration: `make migrate`
2. Start backend: `make run-api`
3. Start compute: `cd ../embedding-compute && make run`
4. Submit a search: `./scripts/test_local_pipeline.sh --search`
5. Verify `qdrant_query_point_id` is populated: `SELECT search_id, qdrant_query_point_id FROM search_requests`
6. Verify vector exists in Qdrant: check `search_queries` collection point count
7. Wait 2+ hours (or reduce `RECALCULATION_HOURS_OLD=0` for testing)
8. Trigger manual recalculation: `curl -X POST http://localhost:8001/api/v1/recalculate/searches?hours_old=0`
9. Verify response shows `recalculated > 0` and `skipped = 0`
10. Check match counts updated in `search_matches` table
