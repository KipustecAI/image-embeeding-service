# Step 8: Docker, Dependencies, and Makefile Updates

## Objective

Update all infrastructure files to support the new architecture.

## Updated docker-compose.yml

```yaml
services:
  postgres:
    image: postgres:16-alpine
    container_name: embedding-postgres
    ports:
      - "5433:5432"
    environment:
      POSTGRES_DB: embedding_service
      POSTGRES_USER: embed_user
      POSTGRES_PASSWORD: embed_pass
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U embed_user -d embedding_service"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - embedding-network

  redis:
    image: redis:7-alpine
    container_name: embedding-redis
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - embedding-network

  qdrant:
    image: qdrant/qdrant:latest
    container_name: qdrant
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage
    environment:
      - QDRANT__SERVICE__HTTP_PORT=6333
      - QDRANT__SERVICE__GRPC_PORT=6334
    networks:
      - embedding-network

networks:
  embedding-network:
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  qdrant_data:
```

## New Dependencies

Add to `requirements.txt`:

```
# Database (NEW)
SQLAlchemy>=2.0.0
asyncpg
alembic
psycopg2-binary

# Scheduler (NEW — replaces ARQ cron for safety nets)
apscheduler>=3.10.0
```

## Updated Makefile

```makefile
.PHONY: install run-api run-worker run-all docker-up docker-down test clean migrate

install:
	pip install -r requirements.txt

run-api:
	python -m src.main

run-worker:
	arq src.workers.main.WorkerSettings

run-all:
	tmux new-session -d -s embedding 'make run-api' \; split-window -h 'make run-worker' \; attach

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

# Database migrations
migrate:
	alembic upgrade head

migrate-create:
	alembic revision --autogenerate -m "$(msg)"

migrate-downgrade:
	alembic downgrade -1

migrate-history:
	alembic history

# Testing
test:
	pytest tests/ -v

test-single:
	pytest tests/ -v -k "$(t)"

# Code quality
lint:
	ruff check src/ tests/

format:
	ruff check --fix src/ tests/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
	find . -type f -name "*.pyc" -delete 2>/dev/null; \
	rm -rf .coverage htmlcov/
```

## Updated .env.example

```env
# Application
APP_NAME=Image Embedding Service
ENVIRONMENT=development
DEBUG=true
LOG_LEVEL=INFO

# API Security
EMBEDDING_SERVICE_API_KEY=your_api_key_here
VIDEO_SERVER_API_KEY=your_video_server_key_here
VIDEO_SERVER_BASE_URL=http://localhost:8000

# Database (NEW)
DATABASE_URL=postgresql+asyncpg://embed_user:embed_pass@localhost:5433/embedding_service

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DATABASE=5

# Redis Streams (NEW)
REDIS_STREAMS_DB=3
STREAM_EVIDENCE_EMBED=evidence:embed
STREAM_EVIDENCE_SEARCH=evidence:search
STREAM_CONSUMER_GROUP=embed-workers
STREAM_SEARCH_GROUP=search-workers
STREAM_CONSUMER_BLOCK_MS=5000
STREAM_CONSUMER_BATCH_SIZE=10
STREAM_RECLAIM_IDLE_MS=3600000
STREAM_DEAD_LETTER_MAX_RETRIES=3
STREAM_CONSUMER_CONCURRENCY=1

# Batch Trigger (NEW)
BATCH_TRIGGER_SIZE=20
BATCH_TRIGGER_MAX_WAIT=5.0
BATCH_TRIGGER_SEARCH_SIZE=10
BATCH_TRIGGER_SEARCH_WAIT=3.0

# Qdrant
QDRANT_HOST=localhost
QDRANT_PORT=6333
QDRANT_COLLECTION_NAME=evidence_embeddings
QDRANT_VECTOR_SIZE=512

# CLIP
CLIP_MODEL_NAME=ViT-B-32
CLIP_DEVICE=cpu
CLIP_BATCH_SIZE=32

# Scheduler
SCHEDULER_ENABLED=true
RECALCULATION_ENABLED=true
RECALCULATION_HOURS_OLD=2
RECALCULATION_BATCH_SIZE=20

# Safety nets (NEW)
STALE_WORKING_MINUTES=10
MAX_RETRIES=3
CLEANUP_DAYS=30

# Image Processing
IMAGE_DOWNLOAD_TIMEOUT=30
MAX_IMAGE_SIZE=10485760
SUPPORTED_FORMATS=jpg,jpeg,png,webp

# Search
DEFAULT_SIMILARITY_THRESHOLD=0.75
MAX_SEARCH_RESULTS=100

# Worker
WORKER_CONCURRENCY=4
```

## Local Dev Workflow (after migration)

```bash
# 1. Start infrastructure
make docker-up

# 2. Run migrations
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && make migrate

# 3. Terminal 1: API server (includes stream consumers + batch triggers + safety nets)
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && make run-api

# 4. Terminal 2: ARQ worker (processes embedding + search jobs)
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && make run-worker
```

## Final File Structure

```
image-embedding-service/
├── src/
│   ├── main.py                          # FastAPI + lifespan wiring
│   ├── api/
│   │   └── dependencies.py
│   ├── db/                              # NEW
│   │   ├── __init__.py
│   │   ├── base.py                      # DeclarativeBase
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── constants.py
│   │   │   ├── embedding_request.py
│   │   │   ├── evidence_embedding.py
│   │   │   └── search_request.py
│   │   └── repositories/
│   │       ├── __init__.py
│   │       ├── embedding_request_repo.py
│   │       └── search_request_repo.py
│   ├── streams/                         # NEW
│   │   ├── __init__.py
│   │   ├── consumer.py                  # Generic StreamConsumer
│   │   ├── evidence_consumer.py         # evidence:embed handler
│   │   └── search_consumer.py           # evidence:search handler
│   ├── services/                        # NEW
│   │   └── batch_trigger.py
│   ├── workers/                         # NEW (replaces scheduler/)
│   │   ├── __init__.py
│   │   ├── main.py                      # WorkerSettings
│   │   ├── embedding_worker.py          # Two-phase embedding
│   │   └── search_worker.py             # Search processing
│   ├── infrastructure/
│   │   ├── config.py                    # + new settings
│   │   ├── database.py                  # NEW: async SQLAlchemy
│   │   ├── embedding/
│   │   │   └── clip_embedder.py         # Unchanged
│   │   ├── vector_db/
│   │   │   └── qdrant_repository.py     # Unchanged
│   │   └── api/
│   │       └── video_server_client.py   # Unchanged (still notifies VS)
│   ├── application/
│   │   ├── use_cases/                   # Refactored to use DB
│   │   └── dto/                         # Unchanged
│   └── domain/                          # Unchanged
│       ├── entities/
│       └── repositories/
├── alembic/                             # NEW
│   ├── env.py
│   └── versions/
├── alembic.ini                          # NEW
├── worker.py                            # Updated entry point
├── docker-compose.yml                   # + postgres, redis
├── requirements.txt                     # + sqlalchemy, asyncpg, alembic, apscheduler
├── Makefile                             # + migrate targets
└── .env
```
