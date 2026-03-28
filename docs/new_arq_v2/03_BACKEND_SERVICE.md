# Backend Service (CPU) — image-embeeding-service

## Responsibility

Pipeline orchestration + storage + Search API. **No CLIP model, no GPU dependency.**

Consumes computed vectors from the GPU service and:
1. Stores them directly in Qdrant (~70ms per upsert)
2. Records metadata in PostgreSQL
3. Executes similarity searches against Qdrant
4. Stores individual match results in `search_matches` table
5. Stores query vectors in `search_queries` Qdrant collection for recalculation
6. Exposes Search API (submit, status, matches, user searches)
7. Runs safety nets (stale recovery, recalculation, cleanup)

## Processing Flows

### Receive Embedding Results (direct storage, no ARQ)

```
embeddings:results stream
       │
       │ {evidence_id, camera_id, embeddings: [{image_url, vector, image_index}, ...]}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Create embedding_request row (status=TO_WORK) if not exists
       ├─ For each embedding:
       │    ├─ Upsert vector directly to Qdrant (~70ms)
       │    └─ Create evidence_embedding DB record
       ├─ Update embedding_request → EMBEDDED
       └─ XACK original message
```

No BatchTrigger. No ARQ queue. The consumer stores vectors inline for speed (~70ms total vs 5-30s with the old ARQ chain).

### Receive Search Results (direct storage, no ARQ)

```
search:results stream
       │
       │ {search_id, user_id, vector, threshold, max_results, metadata}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Find existing SearchRequest (created by POST API) or create new one
       ├─ Search Qdrant with pre-computed query vector
       ├─ Store query vector in search_queries Qdrant collection → get point_id
       ├─ Save qdrant_query_point_id on SearchRequest
       ├─ Create SearchMatch rows for each result
       ├─ Update SearchRequest → COMPLETED + similarity_status
       └─ XACK original message
```

### Search API Flow

```
POST /api/v1/search
       │
       ├─ Create SearchRequest row (status=TO_WORK)
       ├─ Publish to evidence:search stream (→ GPU compute)
       └─ Return 202 { search_id }
              │
              ▼
         GPU compute
              │ XADD search:results { vector }
              ▼
         search_results_consumer (flow above)

GET /api/v1/search/{search_id}          → status + summary
GET /api/v1/search/{search_id}/matches  → paginated match results
GET /api/v1/search/user/{user_id}       → paginated user searches
```

### Recalculation (no GPU needed)

```
Scheduled job (every 1h) or POST /api/v1/recalculate/searches
       │
       ├─ Find completed searches older than N hours
       ├─ For each with qdrant_query_point_id:
       │    ├─ Retrieve query vector from search_queries collection (~1ms)
       │    ├─ Search evidence_embeddings with that vector (~70ms)
       │    ├─ Delete old SearchMatch rows
       │    ├─ Insert new SearchMatch rows
       │    └─ Update totals
       └─ Searches without stored vectors are skipped (pre-migration)
```

### Handle Compute Errors

```
embeddings:results / search:results stream
       │
       │ {event_type: "compute.error", entity_id, entity_type, error}
       ▼
  StreamConsumer
       │
       └─ Update DB row → ERROR with error message
```

## Stream Consumers

### embedding_results_consumer.py

Consumes `embeddings:results` from GPU. Stores vectors **directly** in Qdrant + creates DB rows in one flow:

```python
async def _process_embedding_result(payload, message_id):
    evidence_id = payload["evidence_id"]
    camera_id = payload["camera_id"]
    embeddings = payload["embeddings"]

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if not await repo.check_duplicate(evidence_id):
            await repo.create_request(...)

    # Direct Qdrant upsert — no ARQ queue
    for emb in embeddings:
        point_id = str(uuid4())
        await vector_repo.store_embedding(ImageEmbedding(
            id=point_id,
            vector=np.array(emb["vector"]),
            metadata={...},
        ))
        # Create evidence_embedding DB record
        ...

    # Update request → EMBEDDED
```

### search_results_consumer.py

Consumes `search:results` from GPU. Searches Qdrant and stores matches + query vector:

```python
async def _process_search_result(payload, message_id):
    search_id = payload["search_id"]
    vector = payload["vector"]  # Pre-computed 512-dim

    query_vector = np.array(vector, dtype=np.float32)
    matches = await vector_repo.search_similar(query_vector, ...)

    # Store query vector for future recalculation
    query_point_id = str(uuid4())
    await vector_repo.store_query_vector(query_point_id, query_vector.tolist(), search_id)

    async with get_session() as session:
        request = await repo.get_by_search_id(search_id)
        request.status = COMPLETED
        request.qdrant_query_point_id = query_point_id

        # Clear old matches (for recalculation case)
        # Create SearchMatch rows for each result
        for match in matches:
            session.add(SearchMatch(...))
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Service info |
| GET | `/health` | No | Component health (DB, Qdrant, scheduler, consumers) |
| GET | `/api/v1/stats` | No | Pipeline counts + Qdrant stats |
| GET | `/api/v1/pipeline/status` | No | Full status (counts + consumer health) |
| POST | `/api/v1/search` | Yes | Submit similarity search → 202 |
| GET | `/api/v1/search/{search_id}` | Yes | Search status (no matches inline) |
| GET | `/api/v1/search/{search_id}/matches` | Yes | Paginated match results |
| GET | `/api/v1/search/user/{user_id}` | Yes | List user searches (paginated) |
| POST | `/api/v1/recalculate/searches` | Yes | Re-search with stored query vectors |

Auth = gateway headers (`X-User-Id`, `X-User-Role`). No API key.

## Lifespan (main.py)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Verify PostgreSQL connection
    # 2. Initialize Qdrant (evidence_embeddings + search_queries collections)
    #    Set vector_repo for both consumers and safety_nets
    # 3. Create StreamProducer (for publishing search requests to GPU)
    # 4. Start APScheduler:
    #    - recover_stale_working (every 5m)
    #    - recalculate_searches (every 1h)
    #    - cleanup_old_requests (every 24h)
    # 5. Start stream consumers:
    #    - embedding_results_consumer (embeddings:results)
    #    - search_results_consumer (search:results)
```

No ARQ pool. No BatchTrigger. Single process handles everything.

## What's Not in Backend

| Removed | Reason |
|---------|--------|
| `torch`, `sentence-transformers` | No CLIP inference |
| `opencv-python-headless` | No diversity filter |
| `Pillow` | No image processing |
| CLIP embedder | Moved to compute service |
| Diversity filter | Moved to compute service |
| Image download logic | Compute service handles it |
| ARQ queue (happy path) | Consumers store directly |
| BatchTrigger (happy path) | Not needed with direct storage |
| API key auth | Replaced by gateway headers |
