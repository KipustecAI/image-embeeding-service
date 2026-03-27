# Step 1: Repository Structure

## Two Repos

```
lucam/
├── embedding-compute/       ← NEW repo (GPU service)
└── embedding-backend/       ← RENAMED from image-embeeding-service (CPU service)
```

---

## embedding-compute/ (GPU)

Stateless. No DB, no Qdrant. Only CLIP + Redis Streams.

```
embedding-compute/
├── src/
│   ├── main.py                      # Entry point: start consumers + optional health endpoint
│   ├── config.py                    # CLIP + Redis + diversity filter settings
│   ├── streams/
│   │   ├── consumer.py              # Generic StreamConsumer (copied from current)
│   │   ├── producer.py              # Publish results to output streams
│   │   ├── evidence_handler.py      # evidence:embed → download + filter + CLIP → embeddings:results
│   │   └── search_handler.py        # evidence:search → download + CLIP → search:results
│   ├── services/
│   │   ├── clip_embedder.py         # CLIP model (moved from infrastructure/embedding/)
│   │   └── diversity_filter.py      # Bhattacharyya histogram filter (moved from services/)
│   └── utils/
│       └── image_downloader.py      # Async image download with overlap
├── Dockerfile                       # CUDA base image + torch + sentence-transformers
├── docker-compose.yml               # Just Redis (for local dev)
├── requirements.txt                 # torch, sentence-transformers, redis, opencv, Pillow, httpx
├── .env
├── .env.dev
└── CLAUDE.md
```

### requirements.txt (compute)

```
# CLIP model
torch>=2.5.0
sentence-transformers>=3.3.0
Pillow>=11.0.0
numpy>=1.26.0

# Image processing
opencv-python-headless>=4.8.0
httpx>=0.28.0

# Redis Streams
redis>=5.2.0

# Config
pydantic>=2.10.0
pydantic-settings>=2.6.0
python-dotenv>=1.0.0

# Optional: minimal health endpoint
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
```

~4GB Docker image (dominated by torch + CUDA).

### config.py (compute)

```python
class Settings(BaseSettings):
    # CLIP
    clip_model_name: str = "ViT-B-32"
    clip_device: str = "cuda"
    clip_batch_size: int = 4

    # Redis Streams (input)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_streams_db: int = 3
    stream_evidence_embed: str = "evidence:embed"
    stream_evidence_search: str = "evidence:search"
    stream_consumer_group: str = "compute-workers"
    stream_consumer_block_ms: int = 5000
    stream_consumer_batch_size: int = 10
    stream_reclaim_idle_ms: int = 3_600_000
    stream_dead_letter_max_retries: int = 3

    # Redis Streams (output)
    stream_embeddings_results: str = "embeddings:results"
    stream_search_results: str = "search:results"

    # Diversity filter
    diversity_filter_threshold: float = 0.10
    diversity_filter_histogram_bins: int = 64
    diversity_filter_compare_all: bool = False
    diversity_filter_min_dimension: int = 50
    diversity_filter_max_aspect_ratio: float = 5.0
    diversity_filter_max_images: int = 10
    diversity_filter_min_images: int = 1

    # Image download
    image_download_timeout: int = 30
    max_image_size: int = 10485760
```

---

## embedding-backend/ (CPU)

Pipeline orchestration. No CLIP, no torch. Lightweight.

```
embedding-backend/
├── src/
│   ├── main.py                          # FastAPI app + lifespan
│   ├── api/
│   │   └── dependencies.py              # API key auth
│   ├── config.py                        # DB + Qdrant + Redis + API settings
│   ├── db/
│   │   ├── base.py
│   │   ├── models/
│   │   │   ├── constants.py
│   │   │   ├── embedding_request.py
│   │   │   ├── evidence_embedding.py
│   │   │   └── search_request.py
│   │   └── repositories/
│   │       ├── embedding_request_repo.py
│   │       └── search_request_repo.py
│   ├── infrastructure/
│   │   ├── database.py                  # SQLAlchemy async engine
│   │   └── vector_db/
│   │       └── qdrant_repository.py     # Qdrant client
│   ├── streams/
│   │   ├── consumer.py                  # Generic StreamConsumer (same copy)
│   │   ├── embedding_results_consumer.py  # NEW: embeddings:results → Qdrant + DB
│   │   └── search_results_consumer.py     # NEW: search:results → Qdrant search + DB
│   ├── services/
│   │   ├── batch_trigger.py
│   │   ├── safety_nets.py
│   │   └── storage_worker.py            # NEW: stores vectors in Qdrant + DB records
│   └── workers/
│       ├── main.py                      # ARQ worker settings
│       ├── embedding_storage_worker.py  # Receives vectors → bulk Qdrant upsert + DB
│       └── search_execution_worker.py   # Receives query vector → Qdrant search + DB
├── alembic/
│   ├── env.py
│   └── versions/
├── alembic.ini
├── Dockerfile                           # Python slim, no CUDA
├── docker-compose.yml                   # PostgreSQL + Redis + Qdrant
├── requirements.txt
├── .env
├── .env.dev
└── CLAUDE.md
```

### requirements.txt (backend)

```
# API
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
pydantic>=2.10.0
pydantic-settings>=2.6.0
python-dotenv>=1.0.0

# Database
SQLAlchemy>=2.0.0
asyncpg
alembic
psycopg2-binary
greenlet

# Vector DB
qdrant-client>=1.12.0
numpy>=1.26.0

# Redis + task queue
redis>=5.2.0
arq>=0.26.0

# Scheduler
apscheduler>=3.10.0

# HTTP (for Qdrant, internal comms)
httpx>=0.28.0
aiofiles>=24.1.0
```

~200MB Docker image. **No torch, no CUDA, no sentence-transformers.**
