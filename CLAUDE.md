# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Image Embedding Service — a Python microservice that generates CLIP embeddings from camera evidence images, stores them in Qdrant, and performs vector similarity searches. Part of the larger Video Server project.

## Commands

```bash
make install          # Install dependencies (pip install -r requirements.txt)
make run-api          # Run FastAPI server (python -m src.main) on port 8001
make run-worker       # Run ARQ scheduler worker (python worker.py)
make run-all          # Run both via tmux
make docker-up        # Start Qdrant + Redis via docker-compose
make docker-down      # Stop Docker services
make test             # Run tests: pytest tests/ -v
make clean            # Remove __pycache__, .pytest_cache, coverage files
```

Local dev requires two terminals: one for the API (`make run-api`) and one for the worker (`make run-worker`). Qdrant and Redis must be running first (`make docker-up`).

## Architecture

Clean Architecture with four layers:

- **Domain** (`src/domain/`) — Entities (`Evidence`, `ImageSearch`, `ImageEmbedding`, `SearchResult`) and abstract repository interfaces (`EvidenceRepository`, `ImageSearchRepository`, `VectorRepository`, `EmbeddingService`)
- **Application** (`src/application/`) — Use cases (`EmbedEvidenceImagesUseCase`, `SearchSimilarImagesUseCase`) and DTOs
- **Infrastructure** (`src/infrastructure/`) — Concrete implementations: CLIP embedder, Qdrant repository, Video Server HTTP client, ARQ scheduler
- **Presentation** (`src/main.py`) — FastAPI endpoints

### Key Dependencies Flow

`VideoServerClient` implements both `EvidenceRepository` and `ImageSearchRepository` — it's the single HTTP client to the main Video Server API. The `EmbeddingScheduler` (in `arq_scheduler.py`) wires all components together and hosts the ARQ cron jobs.

### Status Machines

Evidence: `1 (TO_WORK) → 2 (IN_PROGRESS) → 3 (FOUND) → 4 (EMBEDDED)`
The service picks up evidences at status 3 and moves them to 4 after embedding.

ImageSearch: `1 (TO_WORK) → 2 (IN_PROGRESS) → 3 (COMPLETED)` with a separate `similarity_status`: `1 (NO_MATCHES) | 2 (MATCHES_FOUND)`

### Processing Model

Four ARQ cron jobs run in `worker.py`:
1. **process_evidence_embeddings** — every 10 min, batch of 50
2. **process_image_searches** — every 1 min, batch of 10
3. **update_vector_statistics** — hourly at minute 0
4. **recalculate_searches** — hourly at minute 15 (re-searches completed queries against new evidence)

### Embedding Details

- Model: CLIP ViT-B-32 via `sentence-transformers`
- Vector dimension: 512 (cosine distance in Qdrant)
- Collection: `evidence_embeddings` with payload indices on `camera_id`, `evidence_id`, `source_type`

## Configuration

Settings in `src/infrastructure/config.py` use Pydantic `BaseSettings` loaded from `.env`. Singleton via `@lru_cache`. Required env vars: `EMBEDDING_SERVICE_API_KEY`, `VIDEO_SERVER_API_KEY`.

## API Authentication

All endpoints except health checks require `X-API-Key` header matching `EMBEDDING_SERVICE_API_KEY`.



## Conda Environment

**Environment name:** `clip_p11` (Python 3.11)

Run commands inside the conda environment like this:

```bash
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && <command>
```

Examples:
```bash
# Run tests
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && pytest tests/ -v

# Run CI locally
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && make ci-local

# Lint and format
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11 && ruff check --fix src/ tests/
```
