# Migration Plan — COMPLETED

## Status: All phases completed

The split from monolith to compute/backend was implemented successfully. This document records what was done for reference.

## Phase 1: Create embedding-compute repo (DONE)

Created `~/Desktop/2025/lucam/embedding-compute/` with:

- `src/main.py` — loads CLIP, starts consumers for `evidence:embed` + `evidence:search`, signal shutdown
- `src/config.py` — CLIP + Redis + diversity filter settings
- `src/streams/consumer.py` — generic StreamConsumer (copied from backend)
- `src/streams/producer.py` — publishes to `embeddings:results` and `search:results`
- `src/streams/evidence_handler.py` — download images → diversity filter → CLIP batch → publish vectors
- `src/streams/search_handler.py` — download query image → CLIP → publish vector
- `src/services/clip_embedder.py` — CLIP ViT-B-32 via sentence-transformers, supports `file://` URLs
- `src/services/diversity_filter.py` — Bhattacharyya histogram filter (threshold=0.10)
- Dockerfile, requirements.txt, .env, CLAUDE.md

Tested: `XADD evidence:embed` → verified `embeddings:results` output with real CLIP vectors.

## Phase 2: Refactor backend (DONE)

### Removed from backend
- torch, sentence-transformers, opencv, Pillow from requirements.txt
- CLIP embedder and diversity filter code (kept as legacy files, not used)
- API key auth → replaced with gateway headers (`X-User-Id`, `X-User-Role`)

### Added to backend
- `streams/embedding_results_consumer.py` — consumes `embeddings:results`, stores directly in Qdrant + DB
- `streams/search_results_consumer.py` — consumes `search:results`, searches Qdrant, stores matches + query vector
- `streams/producer.py` — publishes search requests to `evidence:search` stream
- `db/models/search_match.py` — individual match results table
- `qdrant_query_point_id` column on `search_requests` — links to stored query vector
- `search_queries` Qdrant collection — GPU-free recalculation
- Search API endpoints (POST /search, GET /search/{id}, GET /matches, GET /user/{uid})
- Recalculation endpoint and scheduled job using stored query vectors

### Key architecture decision: removed ARQ for happy path
- Originally planned: consumer → BatchTrigger → ARQ queue → worker → Qdrant
- Problem: 5-30s latency from ARQ polling
- Solution: consumers store directly in Qdrant (~70ms). ARQ worker code still exists but isn't wired.

### Updated config
Removed: CLIP settings, diversity filter settings, API key, batch trigger vars, worker vars
Added: `STREAM_EVIDENCE_SEARCH`, `STREAM_EMBEDDINGS_RESULTS`, `STREAM_SEARCH_RESULTS`, `STREAM_BACKEND_GROUP`

### Migrations
1. `5317a69cb648` — initial tables (embedding_requests, evidence_embeddings, search_requests)
2. `44422def2930` — add vector_data column (temporary)
3. `3bc63254ad56` — drop vector_data column (no longer needed)
4. `75010ceac028` — add search_matches table
5. `c8030dc66743` — add qdrant_query_point_id to search_requests

## Phase 3: Deploy (DONE locally)

### Local testing results
- Embedding pipeline: **3s** end-to-end (evidence:embed → GPU → backend → Qdrant + DB)
- Search pipeline: **2s** end-to-end (POST /search → GPU → backend → Qdrant search → matches stored)
- Recalculation: **~100ms** per search (retrieve stored vector → re-search Qdrant, no GPU)

### Running locally
```bash
# Terminal 1: Infrastructure
make docker-up && make migrate

# Terminal 2: GPU compute
cd ../embedding-compute && make run

# Terminal 3: Backend
make run-api

# Terminal 4: Test
./scripts/test_local_pipeline.sh          # Embedding test
./scripts/test_local_pipeline.sh --search # Search API test
./scripts/test_local_pipeline.sh --mock   # Backend-only test (no GPU)
```

### Production deployment
```bash
# GPU machine: embedding-compute
docker compose up -d

# CPU machine: image-embeeding-service
docker compose up -d
# No separate worker process needed — single make run-api handles everything
```

## Differences from original plan

| Planned | Actual | Reason |
|---------|--------|--------|
| ARQ workers process batches | Consumers store directly | 20x faster (70ms vs 5-30s) |
| BatchTrigger dispatches work | Not used | Direct storage eliminates the need |
| `embedding-backend` repo name | `image-embeeding-service` kept | Less disruption |
| Two processes (API + worker) | Single process | No ARQ means no separate worker |
| API key auth | Gateway headers | Service runs behind API Gateway |
| Vectors in DB JSONB for recalc | Query vectors in Qdrant collection | Lighter DB rows, vectors belong in Qdrant |
| `storage_worker.py`, `search_execution_worker.py` | Never created | Direct storage made them unnecessary |
