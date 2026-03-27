# Step 5: Two-Phase ARQ Workers

## Objective

Rewrite the embedding and search workers using deepface's optimized two-phase pattern:
- **Phase 1:** Parallel download + CLIP inference (overlapped I/O + compute)
- **Phase 2:** Bulk Qdrant upsert + bulk DB commit (N calls → 1)

## Evidence Embedding Worker

**Refactored file:** `src/workers/embedding_worker.py`

### Two-Phase Flow

```
┌───────────────────────────────────────────────────────┐
│ PHASE 1: Parallel Extraction (asyncio.gather)          │
│                                                        │
│  Request A ──► download img1 ──► CLIP embed ──► result │
│  Request A ──► download img2 ──► CLIP embed ──► result │
│  Request B ──► download img1 ──► CLIP embed ──► result │
│  (all concurrent with semaphore limiting)              │
│                                                        │
├───────────────────────────────────────────────────────┤
│ PHASE 2: Bulk Storage (single calls)                   │
│                                                        │
│  All qdrant points ──► 1x client.upsert()              │
│  All DB records ──────► 1x session.add_all() + commit  │
│  All status updates ──► batch update to EMBEDDED       │
│  Notify Video Server ─► mark as embedded (status=4)    │
└───────────────────────────────────────────────────────┘
```

### Implementation

```python
class EmbeddingExtractionWorker:
    def __init__(self):
        self.embedder = None      # CLIPEmbedder (initialized on first use)
        self.vector_repo = None   # QdrantVectorRepository

    async def process_batch(self, request_ids: List[str]) -> Dict:
        """Process a batch of embedding requests using two-phase approach."""

        # ── Phase 1: Parallel extraction ──
        semaphore = asyncio.Semaphore(settings.clip_batch_size)  # e.g., 4

        async def extract_with_limit(request_id):
            async with semaphore:
                return await self._extract_one(request_id)

        results = await asyncio.gather(
            *[extract_with_limit(rid) for rid in request_ids],
            return_exceptions=True,
        )

        # Collect successful extractions
        all_qdrant_points = []
        all_db_records = []
        succeeded_ids = []
        failed_ids = []

        for rid, result in zip(request_ids, results):
            if isinstance(result, Exception):
                failed_ids.append((rid, str(result)))
                continue
            if result.get("failed"):
                failed_ids.append((rid, result.get("error", "unknown")))
                continue

            all_qdrant_points.extend(result["qdrant_points"])
            all_db_records.extend(result["db_records"])
            succeeded_ids.append(rid)

        # ── Phase 2: Bulk storage ──

        # 2a. Single Qdrant upsert for ALL points
        if all_qdrant_points:
            await self.vector_repo.store_embeddings_batch(all_qdrant_points)

        # 2b. Single DB commit for ALL embedding records
        if all_db_records:
            async with get_session() as session:
                session.add_all(all_db_records)
                await session.commit()

        # 2c. Update succeeded requests → EMBEDDED (status=3)
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

            # Update failed requests → ERROR (status=5)
            for rid, error in failed_ids:
                stmt = (
                    update(EmbeddingRequest)
                    .where(EmbeddingRequest.id == UUID(rid))
                    .values(
                        status=EmbeddingRequestStatus.ERROR,
                        error_message=error,
                        processing_completed_at=datetime.utcnow(),
                    )
                )
                await session.execute(stmt)
            await session.commit()

        # 2d. Notify Video Server for each succeeded evidence
        for rid in succeeded_ids:
            await self._notify_video_server(rid)

        return {
            "processed": len(request_ids),
            "succeeded": len(succeeded_ids),
            "failed": len(failed_ids),
        }
```

### Phase 1: Single Request Extraction

```python
async def _extract_one(self, request_id: str) -> Dict:
    """Extract embeddings for one request (parallel-safe, no storage)."""
    result = {"qdrant_points": [], "db_records": [], "failed": False}

    async with get_session() as session:
        request = await session.get(EmbeddingRequest, UUID(request_id))
        if not request:
            result["failed"] = True
            result["error"] = "Request not found"
            return result

    # Process each image in the evidence
    for idx, image_url in enumerate(request.image_urls):
        try:
            # Download + CLIP embed
            vector = await self.embedder.generate_embedding(image_url)
            if vector is None:
                continue

            point_id = str(uuid4())

            # Prepare Qdrant point (don't store yet)
            embedding = ImageEmbedding.from_evidence(
                evidence_id=request.evidence_id,
                vector=vector,
                image_url=image_url,
                camera_id=request.camera_id,
                additional_metadata={
                    "image_index": idx,
                    "total_images": len(request.image_urls),
                }
            )
            embedding.id = point_id
            result["qdrant_points"].append(embedding)

            # Prepare DB record (don't commit yet)
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
            logger.error(f"Failed to embed image {idx} for {request_id}: {e}")

    if not result["qdrant_points"]:
        result["failed"] = True
        result["error"] = "No images could be embedded"

    return result
```

## Search Worker

**Refactored file:** `src/workers/search_worker.py`

```python
class SearchWorker:
    async def process_batch(self, request_ids: List[str]) -> Dict:
        """Process a batch of search requests."""

        # Phase 1: Parallel download + CLIP embed for each query image
        semaphore = asyncio.Semaphore(4)

        async def embed_query(request_id):
            async with semaphore:
                return await self._embed_query_image(request_id)

        results = await asyncio.gather(
            *[embed_query(rid) for rid in request_ids],
            return_exceptions=True,
        )

        # Phase 2: Batch Qdrant search + store results
        # (Each search has unique filters so we can't fully batch,
        #  but we can parallelize the Qdrant queries)
        for rid, result in zip(request_ids, results):
            if isinstance(result, Exception):
                await self._mark_error(rid, str(result))
                continue

            # Search Qdrant
            matches = await self.vector_repo.search_similar(
                query_vector=result["vector"],
                limit=result["max_results"],
                threshold=result["threshold"],
                filter_conditions=result.get("filters"),
            )

            # Update DB + notify Video Server
            await self._store_results(rid, matches)
```

## ARQ Task Registration

**Updated file:** `src/workers/main.py`

```python
async def process_evidence_embeddings_batch(ctx: Dict, request_ids: List[str]) -> Dict:
    worker = EmbeddingExtractionWorker()
    await worker.initialize()  # Load CLIP model if not loaded
    return await worker.process_batch(request_ids)

async def process_image_searches_batch(ctx: Dict, request_ids: List[str]) -> Dict:
    worker = SearchWorker()
    await worker.initialize()
    return await worker.process_batch(request_ids)

class WorkerSettings:
    functions = [
        process_evidence_embeddings_batch,
        process_image_searches_batch,
    ]
    redis_settings = get_redis_settings()
    max_jobs = settings.worker_concurrency         # 4
    job_timeout = 600                               # 10 minutes
    keep_result = 3600
    max_tries = 3
    retry_delay = 5

    on_startup = startup   # Pre-load CLIP model
    on_shutdown = shutdown  # Cleanup
```

## Performance Comparison

| Metric | Current (sequential) | New (two-phase) |
|--------|---------------------|-----------------|
| 20 evidences, 3 images each | ~60 serial CLIP calls + 60 Qdrant upserts | 60 parallel CLIP calls + 1 bulk upsert |
| Qdrant calls | N per batch | 1 per batch |
| DB commits | N/A (no DB) | 1 per batch |
| Network round-trips | 2N (download + upsert) | N (download) + 1 (bulk upsert) |
