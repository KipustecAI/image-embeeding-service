# Image Embedding Backend

Pipeline orchestration, vector storage, and API for the image embedding system. Consumes pre-computed CLIP vectors from the [embedding-compute](../embedding-compute/) GPU service, stores them in Qdrant, and executes similarity searches.

**No GPU. No CLIP model. Just storage + API.**

## Architecture

```
embedding-compute (GPU)                     this service (CPU)
┌─────────────────────┐                    ┌──────────────────────────┐
│ evidence:embed ──►  │                    │                          │
│   download + filter │  embeddings:results│  StreamConsumer           │
│   CLIP inference  ──┼───────────────────►│  ├─ Store in Qdrant      │
│                     │                    │  ├─ Update PostgreSQL    │
│ evidence:search ──► │  search:results    │  └─ Execute searches     │
│   CLIP inference  ──┼───────────────────►│                          │
└─────────────────────┘                    │  FastAPI API (port 8001) │
                                           │  PostgreSQL + Qdrant     │
                                           └──────────────────────────┘
```

## Quick Start

```bash
# 1. Start infrastructure
make docker-up

# 2. Run database migrations
make migrate

# 3. Start the backend (API + worker in tmux)
make run

# Or in separate terminals:
make run-api      # Terminal 1: FastAPI server
make run-worker   # Terminal 2: ARQ storage worker
```

The compute service must also be running separately (see [embedding-compute](../embedding-compute/)).

## Prerequisites

- Python 3.11+
- Docker (for PostgreSQL, Redis, Qdrant)
- The [embedding-compute](../embedding-compute/) service running (for CLIP inference)

## Configuration

Copy `.env.dev` for local development or create `.env`:

```env
# Database
DATABASE_URL=postgresql+asyncpg://embed_user:embed_pass@localhost:5433/embedding_service

# Qdrant
QDRANT_HOST=localhost
QDRANT_PORT=6333

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DATABASE=5

# Streams (output from compute service)
REDIS_STREAMS_DB=3
STREAM_EMBEDDINGS_RESULTS=embeddings:results
STREAM_SEARCH_RESULTS=search:results
STREAM_BACKEND_GROUP=backend-workers
```

See `.env` for all available settings.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Service info |
| GET | `/health` | No | Component health (DB, scheduler, triggers, consumers) |
| GET | `/api/v1/stats` | No | Pipeline request counts + config |
| GET | `/api/v1/pipeline/status` | No | Full status (counts + triggers + consumers) |
| POST | `/api/v1/internal/trigger/{name}` | No | Cross-process trigger notification |
| POST | `/api/v1/recalculate/searches` | Yes | Re-queue searches for recalculation |

See [docs/API_REFERENCE.md](docs/API_REFERENCE.md) for full details and [docs/CURL_EXAMPLES.md](docs/CURL_EXAMPLES.md) for usage examples.

## Data Flow

1. **ETL service** publishes to `evidence:embed` with `{evidence_id, camera_id, user_id, device_id, app_id, infraction_code, zip_url}` (event: `evidence.created.embed`)
2. **embedding-compute** (GPU) downloads the ZIP, extracts frames, runs diversity filter + CLIP, publishes vectors to `embeddings:results` with `embeddings: [{image_name, vector, image_index}, ...]` plus all ETL metadata passed through
2a. **compute-weapons** *(optional)* If routing sends the evidence through the weapons-detection service, it enriches the `embeddings:results` message with a `weapon_analysis` block (per-image bboxes + evidence-level summary). Messages without this block take the legacy path unchanged.
3. **This service** consumes from `embeddings:results`: downloads the ZIP again, extracts the filtered frames by name, uploads them to the `storage-service` (MinIO) to get permanent URLs, then stores vectors in Qdrant (multi-tenant + weapons payload) + PostgreSQL
4. **Search** flows separately: `POST /api/v1/search` → `evidence:search` stream → GPU embeds the query image → `search:results` → this service executes the Qdrant search (with optional `weapons_filter`) and stores matches

See [docs/new_arq_v2/04_STREAM_CONTRACTS.md](docs/new_arq_v2/04_STREAM_CONTRACTS.md) for full payload schemas and [docs/weapons/](docs/weapons/) for the weapons-enrichment design.

## Background Jobs (Safety Nets)

| Job | Interval | Purpose |
|-----|----------|---------|
| Embedding safety net | 60s | Catch missed embedding rows |
| Search safety net | 120s | Catch missed search rows |
| Stale recovery | 5m | Reset stuck WORKING rows |
| Recalculation | 1h | Re-run old searches against new evidence |
| Cleanup | 24h | Delete rows older than 30 days |

## Database

Three PostgreSQL tables managed by Alembic:

- `embedding_requests` — tracks each evidence through the pipeline
- `evidence_embeddings` — one row per vector stored in Qdrant
- `search_requests` — tracks each similarity search

```bash
make migrate          # Apply migrations
make migrate-create msg="add new table"  # Create new migration
make migrate-history  # Show migration history
```

## Testing

```bash
# Integration tests (requires Docker services running)
make test

# Full pipeline test with example payload
make test-pipeline

# Similarity report (heatmap + search visualization)
python scripts/generate_similarity_report.py
```

## Project Structure

```
src/
├── main.py                              # FastAPI app + lifespan
├── api/dependencies.py                  # API key auth
├── db/
│   ├── models/                          # SQLAlchemy models
│   └── repositories/                    # DB queries (FOR UPDATE SKIP LOCKED)
├── infrastructure/
│   ├── config.py                        # Pydantic settings
│   ├── database.py                      # Async SQLAlchemy engine
│   └── vector_db/qdrant_repository.py   # Qdrant client
├── streams/
│   ├── consumer.py                      # Generic Redis Streams consumer
│   ├── embedding_results_consumer.py    # embeddings:results → DB + trigger
│   └── search_results_consumer.py       # search:results → DB + trigger
├── services/
│   ├── batch_trigger.py                 # Signal-driven batch dispatcher
│   └── safety_nets.py                   # Scheduled fallback jobs
└── workers/
    ├── main.py                          # ARQ worker settings
    ├── embedding_worker.py              # Bulk Qdrant upsert + DB
    └── search_worker.py                 # Qdrant search + DB
```
