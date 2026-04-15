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

### Receive Embedding Results (ZIP flow + direct storage, no ARQ)

```
embeddings:results stream
       │
       │ {evidence_id, camera_id, user_id, device_id, app_id,
       │  infraction_code, zip_url,
       │  embeddings: [{image_name, vector, image_index}, ...]}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Dedup check on evidence_id
       ├─ Download ZIP from zip_url (ZipProcessor)
       ├─ Extract only frames listed in embeddings[].image_name
       ├─ Upload each frame to storage service → permanent MinIO URL
       │    POST http://storage-service:8006/api/v1/upload/file
       │    Headers: X-User-Id, X-User-Role: dev
       ├─ For each embedding:
       │    ├─ Upsert vector directly to Qdrant (~70ms)
       │    │   Payload: {evidence_id, camera_id, user_id, device_id,
       │    │             app_id, image_url: <permanent>, image_index,
       │    │             source_type: "evidence"}
       │    └─ Create evidence_embedding DB record
       ├─ Create embedding_request row with ETL metadata
       │  (user_id, device_id, app_id, infraction_code, image_urls=[...])
       ├─ Mark request as EMBEDDED
       └─ XACK original message
```

No BatchTrigger. No ARQ queue. The consumer downloads the ZIP, uploads extracted frames to storage, and writes Qdrant + PostgreSQL inline (~300-500ms total including upload, vs 5-30s with the old ARQ chain).

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

Consumes `embeddings:results` from GPU. Downloads the source ZIP, uploads frames to the storage service, then upserts vectors directly to Qdrant + PostgreSQL in one flow:

```python
async def _process_embeddings_result(payload, message_id):
    evidence_id = payload["evidence_id"]
    camera_id = payload["camera_id"]
    zip_url = payload.get("zip_url")
    user_id = payload.get("user_id")
    device_id = payload.get("device_id")
    app_id = payload.get("app_id")
    infraction_code = payload.get("infraction_code")
    embeddings_data = payload.get("embeddings", [])

    # Dedup
    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if await repo.check_duplicate(evidence_id):
            return

    # 1. Download ZIP → extract filtered frames → upload to storage
    uploaded_urls: dict[str, str] = {}  # image_name → public_url
    if zip_url and _storage_uploader:
        image_names = [e["image_name"] for e in embeddings_data]
        image_map = await _zip_processor.download_and_extract(zip_url, image_names)
        folder = f"embeddings/{camera_id}/{evidence_id}"
        for name, img_bytes in image_map.items():
            public_url = await _storage_uploader.upload_image(
                img_bytes, name, folder, user_id=user_id or "embedding-service"
            )
            if public_url:
                uploaded_urls[name] = public_url

    # 2. Upsert vectors to Qdrant with multi-tenant payload
    for emb in embeddings_data:
        point_id = str(uuid4())
        image_url = uploaded_urls.get(emb["image_name"], "")
        await vector_repo.store_embedding(ImageEmbedding(
            id=point_id,
            vector=np.array(emb["vector"], dtype=np.float32),
            metadata={
                "evidence_id": evidence_id,
                "camera_id": camera_id,
                "user_id": user_id,
                "device_id": device_id,
                "app_id": app_id,
                "image_url": image_url,
                "image_index": emb.get("image_index", 0),
                "source_type": "evidence",
            },
        ))
        # Create evidence_embedding DB record linked to the request
        ...

    # 3. Create embedding_request row with ETL metadata → mark EMBEDDED
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
