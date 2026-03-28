# Repository Structure

## Two Repos

```
lucam/
├── embedding-compute/           ← GPU service (separate repo)
└── image-embeeding-service/     ← CPU backend (this repo)
```

---

## embedding-compute/ (GPU)

Stateless. No DB, no Qdrant. Only CLIP + Redis Streams.

```
embedding-compute/
├── src/
│   ├── main.py                      # Entry point: load CLIP, start consumers, signal shutdown
│   ├── config.py                    # CLIP + Redis + diversity filter settings
│   ├── streams/
│   │   ├── consumer.py              # Generic StreamConsumer (shared pattern)
│   │   ├── producer.py              # Publish results to output streams
│   │   ├── evidence_handler.py      # evidence:embed → download + filter + CLIP → embeddings:results
│   │   └── search_handler.py        # evidence:search → download + CLIP → search:results
│   ├── services/
│   │   ├── clip_embedder.py         # CLIP ViT-B-32 via sentence-transformers
│   │   └── diversity_filter.py      # Bhattacharyya histogram filter
│   └── utils/
│       └── image_downloader.py      # Async image download, supports file:// URLs
├── Dockerfile                       # CUDA base image + torch + sentence-transformers
├── docker-compose.yml               # Just Redis (for local dev)
├── requirements.txt                 # torch, sentence-transformers, redis, opencv, Pillow, httpx
├── .env
├── .env.dev
└── CLAUDE.md
```

~4GB Docker image (dominated by torch + CUDA).

---

## image-embeeding-service/ (CPU Backend — this repo)

Pipeline orchestration + storage + Search API. No CLIP, no torch. Lightweight.

```
image-embeeding-service/
├── src/
│   ├── main.py                          # FastAPI app + lifespan (port 8001)
│   ├── api/
│   │   └── dependencies.py              # UserContext from gateway headers (X-User-Id, X-User-Role)
│   ├── db/
│   │   ├── base.py                      # SQLAlchemy declarative base
│   │   ├── models/
│   │   │   ├── constants.py             # Status enums
│   │   │   ├── embedding_request.py     # Tracks evidence through embedding pipeline
│   │   │   ├── evidence_embedding.py    # One row per vector stored in Qdrant
│   │   │   ├── search_request.py        # Tracks search lifecycle + qdrant_query_point_id
│   │   │   └── search_match.py          # Individual match results per search
│   │   └── repositories/
│   │       ├── embedding_request_repo.py
│   │       └── search_request_repo.py
│   ├── infrastructure/
│   │   ├── config.py                    # Pydantic BaseSettings (DB, Qdrant, Redis, streams)
│   │   ├── database.py                  # SQLAlchemy async engine + get_session()
│   │   └── vector_db/
│   │       └── qdrant_repository.py     # Qdrant client (evidence_embeddings + search_queries)
│   ├── streams/
│   │   ├── consumer.py                  # Generic StreamConsumer (daemon thread + XREADGROUP)
│   │   ├── producer.py                  # StreamProducer (publishes search requests to GPU)
│   │   ├── embedding_results_consumer.py  # embeddings:results → Qdrant upsert + DB
│   │   └── search_results_consumer.py     # search:results → Qdrant search + store matches
│   └── services/
│       └── safety_nets.py               # Stale recovery, recalculation, cleanup
├── alembic/
│   ├── env.py
│   └── versions/                        # 5 migrations
├── scripts/
│   ├── test_local_pipeline.sh           # E2E test: embedding + search + mock modes
│   └── generate_similarity_report.py    # Heatmap visualization
├── docs/
│   ├── API_REFERENCE.md                 # v2.1 — 9 endpoints
│   └── CURL_EXAMPLES.md                 # v2.1 — gateway header examples
├── tests/
│   └── test_db.py                       # CI-safe DB model tests
├── docker-compose.yml                   # PostgreSQL + Redis + Qdrant
├── Dockerfile
├── Makefile                             # install, run-api, run-worker, docker-up, migrate, test
├── requirements.txt
├── pyproject.toml                       # ruff + pytest config
├── .env                                 # Production defaults (remote hosts)
├── .env.dev                             # Local dev overrides (localhost, no password)
├── .env.docker                          # Docker environment
├── .env.example                         # Documented template
└── CLAUDE.md
```

~200MB Docker image. **No torch, no CUDA, no sentence-transformers.**

### Qdrant Collections

| Collection | Purpose |
|------------|---------|
| `evidence_embeddings` | Evidence image vectors (512-dim, cosine). Payload indices on `camera_id`, `evidence_id`, `source_type` |
| `search_queries` | Stored query vectors for GPU-free recalculation. One point per search |

### Database Tables

| Table | Purpose |
|-------|---------|
| `embedding_requests` | Tracks each evidence through the embedding pipeline |
| `evidence_embeddings` | One row per vector stored in Qdrant (FK → embedding_requests) |
| `search_requests` | Tracks search lifecycle. `qdrant_query_point_id` links to stored query vector |
| `search_matches` | Individual match results per search (FK → search_requests) |

### Legacy Code (still present, not used in happy path)

These files exist from the v1 monolith but are not wired into the current lifespan:

| File | Status |
|------|--------|
| `src/services/batch_trigger.py` | Not used — consumers store directly |
| `src/workers/` | Legacy ARQ workers — not needed (no `make run-worker` required) |
| `src/infrastructure/scheduler/arq_scheduler.py` | Legacy — replaced by APScheduler in main.py |
| `src/infrastructure/api/video_server_client.py` | Legacy — HTTP client to Video Server |
| `src/infrastructure/embedding/clip_embedder.py` | Legacy — CLIP moved to compute service |
| `src/services/diversity_filter.py` | Legacy — moved to compute service |
| `src/streams/evidence_consumer.py` | Legacy — replaced by embedding_results_consumer |
| `src/streams/search_consumer.py` | Legacy — replaced by search_results_consumer |
| `src/application/` | Legacy — old use cases |
| `src/domain/` | Legacy — old entities/interfaces |
