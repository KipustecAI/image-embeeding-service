# Backend Service (CPU) — image-embeeding-service

## Responsibility

Pipeline orchestration + storage + REST API. **No CLIP model, no GPU dependency.**

Consumes computed vectors from the GPU service and:
1. Stores them directly in Qdrant (~70ms per upsert) — `source_type="evidence"` for ingest, `source_type="blacklist"` for blacklist references.
2. Records metadata in PostgreSQL (evidence + blacklist tables).
3. Executes similarity searches against Qdrant with strict `source_type="evidence"` scoping.
4. Stores individual match results in `search_matches` table.
5. Stores query vectors in `search_queries` Qdrant collection for GPU-free recalculation.
6. Exposes REST API (search submit/status/matches/user/categories + 8 blacklist CRUD endpoints).
7. Runs safety nets (stale recovery, recalculation, cleanup) + on-demand reverse-search jobs for the blacklist.
8. Publishes report events to the report-generation service: `weapons:detected` (when the producer enrichment ran) and `image:blacklist_match` (inline + reverse-search matches).
9. Publishes 7 fat-event streams to the lookia-dw data warehouse: `image_search_request:raw`, `image_search_match:raw`, `blacklist_image_entry:raw`, `blacklist_image_reference:raw`, `blacklist_image_embedding:raw`, `image_embedding_request:raw`, `image_embedding:raw`. Wire-format authority: [`../requirements/LOOKIA_DW_STREAMS.md`](../requirements/LOOKIA_DW_STREAMS.md). PII (blacklist names) hashed before publish.

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
       │    │             app_id, infraction_code, image_url: <permanent>,
       │    │             image_index, source_type: "evidence",
       │    │             category: [<entity ids>], weapon_analyzed,
       │    │             has_weapon, weapon_classes}
       │    └─ Create evidence_embedding DB record (with weapon_detections)
       ├─ Create embedding_request row with ETL metadata
       │  (user_id, device_id, app_id, infraction_code, image_urls=[...],
       │   category (json-stringified entity ids), weapon_* fields)
       ├─ Mark request as EMBEDDED
       ├─ Publish weapons:detected to report-generation
       │  (only if weapon_analysis was present in payload — fire-and-forget)
       ├─ Inline blacklist match:
       │    ├─ Fast-exit when user has no active blacklist entries
       │    ├─ For each new frame, search Qdrant with source_type=blacklist
       │    │   + user_id filter
       │    └─ For each match, publish image:blacklist_match (trigger="inline")
       └─ XACK original message
```

No BatchTrigger. No ARQ queue. The consumer downloads the ZIP, uploads extracted frames to storage, writes Qdrant + PostgreSQL inline, and (optionally) publishes report events — all in ~300-500ms total (vs 5-30s with the old ARQ chain).

### Receive Search Results (direct storage, no ARQ)

```
search:results stream
       │
       │ {search_id, user_id, vector, threshold, max_results, metadata,
       │  purpose: "search" | "blacklist_embed", blacklist_entry_id}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Dispatch by `purpose`:
       │
       ├─ purpose="search" (default):
       │    ├─ Find existing SearchRequest (created by POST API) or create new
       │    ├─ Search Qdrant with strict source_type=evidence filter
       │    ├─ Store query vector in search_queries Qdrant collection → point_id
       │    ├─ Save qdrant_query_point_id on SearchRequest
       │    ├─ Create SearchMatch rows for each result
       │    └─ Update SearchRequest → COMPLETED + similarity_status
       │
       └─ purpose="blacklist_embed" (Phase 04):
            ├─ search_id is overloaded — carries BlacklistImageReference.id
            ├─ Hand off to BlacklistEmbedService.store_blacklist_embedding():
            │    ├─ Upsert vector to Qdrant with source_type="blacklist"
            │    │   payload: blacklist_entry_id, blacklist_reference_id,
            │    │             user_id, model_version, image_url, category?
            │    ├─ Insert blacklist_image_embeddings row
            │    ├─ Lift reference status → PROCESSED, entry → INDEXED
            │    └─ Schedule one-shot reverse-search via APScheduler
            └─ (XACK)
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

### Reverse search (blacklist)

```
BlacklistEmbedService schedules a one-shot APScheduler job (trigger="date")
       │
       ▼
_run_reverse_search(entry_id, reference_id, user_id, vector)
       │
       ├─ Search Qdrant with build_evidence_only_filter({"user_id": user_id})
       ├─ Load entry + reference once (cached for the loop)
       └─ For each match:
            └─ publish_blacklist_match(trigger="reverse_search", …)
                  └─ XADD image:blacklist_match → report-generation
```

If the backend restarts mid-job, the job is lost. Recovery: `POST /api/v1/blacklist/image-entries/{id}/backfill` re-fires it for every PROCESSED reference under the entry.

### Handle Compute Errors

```
search:results / embeddings:results stream
       │
       │ {event_type: "compute.error", entity_id, entity_type, error}
       ▼
  StreamConsumer
       │
       └─ entity_type="evidence" → mark embedding_request as ERROR
       └─ entity_type="search":
              ├─ Try BlacklistImageReference lookup first (UUID PK miss is cheap)
              │     → mark BlacklistImageReference as ERROR
              └─ Fall through to SearchRequest path
                    → mark SearchRequest as ERROR
```

The fallback dispatch exists because compute deliberately omits `purpose` on error envelopes — see [../image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md) §"Error routing".

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
| GET | `/api/v1/search/categories` | Yes | Distinct entity-id categories visible to the tenant + labels |
| GET | `/api/v1/search/{search_id}` | Yes | Search status (no matches inline) |
| GET | `/api/v1/search/{search_id}/matches` | Yes | Paginated match results |
| GET | `/api/v1/search/user/{user_id}` | Yes | List user searches (paginated) |
| POST | `/api/v1/recalculate/searches` | Yes | Re-search with stored query vectors |
| (8) | `/api/v1/blacklist/image-entries/...` | Yes | Image-blacklist CRUD — see [../BLACKLIST_API.md](../BLACKLIST_API.md) |

Auth = gateway headers (`X-User-Id`, `X-User-Role`). No API key.

Full reference: [../API_REFERENCE.md](../API_REFERENCE.md). Blacklist surface lives in `src/api/v1/routers/blacklist_image.py`; everything else is inline in `src/main.py`.

## Lifespan (main.py)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Verify PostgreSQL connection
    # 2. Initialize Qdrant (evidence_embeddings + search_queries collections)
    #    Inject vector_repo into: embedding_results_consumer, search_results_consumer,
    #    safety_nets, blacklist_embed_service, blacklist_reverse_search
    # 3. Construct StorageUploader (for ZIP-extracted frame uploads)
    # 4. Construct StreamProducer; inject into embedding_results_consumer
    #    (for weapons.detected) + blacklist_match_service (for image.blacklist_match) +
    #    blacklist router (for evidence:search publishes on reference create) +
    #    dw_publisher_service (for the 7 lookia-dw outbound streams)
    # 5. Start APScheduler:
    #    - recover_stale_working (every 5m)
    #    - recalculate_searches (every 1h)
    #    - cleanup_old_requests (every 24h)
    #    Inject the scheduler into blacklist_reverse_search (for on-demand jobs)
    # 6. Start stream consumers:
    #    - embedding_results_consumer (embeddings:results)
    #    - search_results_consumer (search:results)
```

No ARQ pool. No BatchTrigger. Single process handles everything. Blacklist CRUD is mounted via `app.include_router(blacklist_image_router.router)` — its dependencies are wired into the router module's globals during lifespan.

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
