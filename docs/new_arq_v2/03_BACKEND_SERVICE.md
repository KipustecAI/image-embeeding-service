# Step 3: Backend Service (CPU)

## Responsibility

Pipeline orchestration + storage + API. **No CLIP model, no GPU dependency.**

Consumes computed vectors from the GPU service and:
1. Stores them in Qdrant
2. Records metadata in PostgreSQL
3. Executes similarity searches
4. Exposes monitoring API
5. Runs safety nets and recalculation

## Processing Flows

### Receive Embedding Results

```
embeddings:results stream
       │
       │ {evidence_id, camera_id, embeddings: [{image_url, vector, image_index}, ...]}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Create embedding_request row (status=1) if not exists
       ├─ BatchTrigger.notify()
       │       │
       │       ▼ (flush on count OR timeout)
       │
       │  ARQ Worker: store_embeddings_batch
       │       ├─ Bulk Qdrant upsert (1 call for all vectors)
       │       ├─ Bulk DB commit (evidence_embedding records)
       │       └─ Update embedding_request → EMBEDDED
       │
       └─ XACK original message
```

### Receive Search Results

```
search:results stream
       │
       │ {search_id, user_id, vector, threshold, max_results, metadata}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Create search_request row (status=1) if not exists
       ├─ BatchTrigger.notify()
       │       │
       │       ▼ (flush)
       │
       │  ARQ Worker: execute_searches_batch
       │       ├─ For each: query Qdrant with vector + filters
       │       ├─ Store results (Redis TTL or DB)
       │       └─ Update search_request → COMPLETED + similarity_status
       │
       └─ XACK original message
```

### Handle Compute Errors

```
embeddings:results / search:results stream
       │
       │ {event_type: "compute.error", entity_id, error}
       ▼
  StreamConsumer
       │
       └─ Update DB row → ERROR with error message
```

## New Stream Consumers

### embedding_results_consumer.py

Replaces the current `evidence_consumer.py` in the backend. Instead of receiving raw images, it receives **pre-computed vectors**:

```python
async def _process_embedding_result(payload: dict, message_id: str):
    """Handle computed embeddings from GPU service."""

    # Handle errors from compute
    if payload.get("event_type") == "compute.error":
        await _handle_compute_error(payload)
        return

    evidence_id = payload["evidence_id"]
    camera_id = payload["camera_id"]
    embeddings = payload["embeddings"]

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)

        # Create or get request row
        if not await repo.check_duplicate(evidence_id):
            await repo.create_request(
                evidence_id=evidence_id,
                camera_id=camera_id,
                image_urls=[e["image_url"] for e in embeddings],
                stream_msg_id=message_id,
            )

        # Store the pre-computed vectors for the worker to pick up
        # Option A: store vectors in the DB row (JSONB) for the worker
        # Option B: store vectors in Redis with TTL
        # Option C: pass vectors directly via ARQ job args
        # → Option C is simplest for now

    trigger = get_batch_trigger("embedding")
    if trigger:
        await trigger.notify(count=1)
```

### search_results_consumer.py

```python
async def _process_search_result(payload: dict, message_id: str):
    """Handle computed search vector from GPU service."""

    if payload.get("event_type") == "compute.error":
        await _handle_compute_error(payload)
        return

    search_id = payload["search_id"]
    user_id = payload["user_id"]
    vector = payload["vector"]          # Pre-computed 512-dim
    threshold = payload.get("threshold", 0.75)
    max_results = payload.get("max_results", 50)
    search_metadata = payload.get("metadata")

    async with get_session() as session:
        repo = SearchRequestRepository(session)

        if not await repo.check_duplicate(search_id):
            await repo.create_request(
                search_id=search_id,
                user_id=user_id,
                image_url="(computed by GPU service)",
                threshold=threshold,
                max_results=max_results,
                metadata=search_metadata,
            )

    trigger = get_batch_trigger("search")
    if trigger:
        await trigger.notify(count=1)
```

## Simplified Workers

### embedding_storage_worker.py

No more CLIP. Just receives vectors and stores them:

```python
class EmbeddingStorageWorker:
    """Receives pre-computed vectors → bulk Qdrant upsert + DB commit."""

    async def process_batch(self, request_ids: List[str]) -> Dict:
        # Vectors are already computed — just store them

        all_points = []
        all_db_records = []

        for rid in request_ids:
            # Get the vectors (from DB JSONB or Redis cache)
            vectors_data = await self._get_vectors(rid)

            for emb in vectors_data:
                point_id = str(uuid4())
                all_points.append(PointStruct(
                    id=point_id,
                    vector=emb["vector"],
                    payload={
                        "evidence_id": emb["evidence_id"],
                        "camera_id": emb["camera_id"],
                        "image_url": emb["image_url"],
                        "image_index": emb["image_index"],
                        "source_type": "evidence",
                    }
                ))
                all_db_records.append(EvidenceEmbeddingRecord(
                    request_id=UUID(rid),
                    qdrant_point_id=point_id,
                    image_index=emb["image_index"],
                    image_url=emb["image_url"],
                ))

        # Single Qdrant upsert
        if all_points:
            await self.vector_repo.store_embeddings_batch(all_points)

        # Single DB commit
        if all_db_records:
            async with get_session() as session:
                session.add_all(all_db_records)
```

### search_execution_worker.py

Receives pre-computed query vector, searches Qdrant:

```python
class SearchExecutionWorker:
    """Receives pre-computed query vector → Qdrant search → store results."""

    async def process_batch(self, request_ids: List[str]) -> Dict:
        for rid in request_ids:
            vector_data = await self._get_vector(rid)

            matches = await self.vector_repo.search_similar(
                query_vector=np.array(vector_data["vector"]),
                limit=vector_data["max_results"],
                threshold=vector_data["threshold"],
                filter_conditions=vector_data.get("filters"),
            )

            # Update DB
            async with get_session() as session:
                stmt = update(SearchRequest)...
```

## What's Removed from Backend

| Removed | Reason |
|---------|--------|
| `torch`, `sentence-transformers` | No CLIP inference |
| `opencv-python-headless` | No diversity filter |
| `clip_embedder.py` | Moved to compute service |
| `diversity_filter.py` | Moved to compute service |
| Image download logic | Compute service handles it |
| `Pillow` | No image processing |

## Lifespan Changes

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. PostgreSQL ✓ (same)
    # 2. ARQ pool ✓ (same)
    # 3. APScheduler safety nets ✓ (same)
    # 4. Batch triggers ✓ (same)
    # 5. Stream consumers:
    #    OLD: evidence:embed + evidence:search (raw events)
    #    NEW: embeddings:results + search:results (pre-computed vectors)
    results_consumer = create_embedding_results_consumer()
    results_consumer.start()
    search_results_consumer = create_search_results_consumer()
    search_results_consumer.start()
```
