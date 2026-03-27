# Step 5: Migration Plan

## Strategy

Split the current monorepo into two repos in phases. Each phase is independently deployable.

## Phase 1: Create embedding-compute repo (GPU side)

### 1a. Create new repo

```bash
mkdir -p ~/Desktop/2025/lucam/embedding-compute/src/{streams,services,utils}
cd ~/Desktop/2025/lucam/embedding-compute
git init
```

### 1b. Copy shared code

```bash
# From current image-embeeding-service:
cp src/streams/consumer.py          → embedding-compute/src/streams/consumer.py
cp src/services/diversity_filter.py → embedding-compute/src/services/diversity_filter.py
cp src/infrastructure/embedding/clip_embedder.py → embedding-compute/src/services/clip_embedder.py
```

### 1c. Write new code

| File | Description |
|------|-------------|
| `src/config.py` | Compute-specific settings (CLIP + streams, no DB/Qdrant) |
| `src/streams/producer.py` | Publish results to output streams |
| `src/streams/evidence_handler.py` | evidence:embed → download + filter + CLIP → embeddings:results |
| `src/streams/search_handler.py` | evidence:search → download + CLIP → search:results |
| `src/utils/image_downloader.py` | Async batch image downloader with overlap |
| `src/main.py` | Entry point: load model, start consumers |
| `Dockerfile` | CUDA base + torch |
| `requirements.txt` | torch, sentence-transformers, redis, opencv, httpx |

### 1d. Test independently

```bash
# Start compute service
python -m src.main

# Publish test event
redis-cli -n 3 XADD evidence:embed '*' \
  event_type evidence.ready.embed \
  payload '{"evidence_id":"test-001","camera_id":"cam-001","image_urls":["file:///path/to/img.jpg"]}'

# Verify output appears in embeddings:results
redis-cli -n 3 XREVRANGE embeddings:results + - COUNT 1
```

## Phase 2: Refactor embedding-backend (CPU side)

### 2a. Remove GPU dependencies

```bash
# Remove from requirements.txt:
# - torch
# - torchvision
# - sentence-transformers
# - opencv-python-headless
# - Pillow (only if not needed for anything else)
```

### 2b. Replace stream consumers

| Remove | Add |
|--------|-----|
| `streams/evidence_consumer.py` | `streams/embedding_results_consumer.py` |
| `streams/search_consumer.py` | `streams/search_results_consumer.py` |

The new consumers listen to **output** streams (`embeddings:results`, `search:results`) instead of **input** streams (`evidence:embed`, `evidence:search`).

### 2c. Simplify workers

| Remove | Add |
|--------|-----|
| `workers/embedding_worker.py` (CLIP + Qdrant) | `workers/embedding_storage_worker.py` (Qdrant only) |
| `workers/search_worker.py` (CLIP + Qdrant) | `workers/search_execution_worker.py` (Qdrant only) |

Workers no longer download images or run CLIP — they receive pre-computed vectors.

### 2d. Remove GPU code

```bash
rm src/infrastructure/embedding/clip_embedder.py
rm src/services/diversity_filter.py
# Keep src/infrastructure/vector_db/qdrant_repository.py (backend needs it)
```

### 2e. Update config

Remove from `config.py`:
- `clip_model_name`, `clip_device`, `clip_batch_size`
- `diversity_filter_*` (7 settings)
- `image_download_timeout`, `max_image_size`, `supported_formats`

Add to `config.py`:
- `stream_embeddings_results = "embeddings:results"`
- `stream_search_results = "search:results"`
- `stream_backend_group = "backend-workers"`

### 2f. Update lifespan

```python
# OLD consumers (input streams)
embed_consumer = create_evidence_embed_consumer()      # evidence:embed
search_consumer = create_evidence_search_consumer()    # evidence:search

# NEW consumers (output streams from compute)
results_consumer = create_embedding_results_consumer() # embeddings:results
search_results_consumer = create_search_results_consumer() # search:results
```

## Phase 3: Deploy

### Deployment order

1. Deploy **embedding-compute** first — it starts consuming `evidence:embed` and publishing `embeddings:results`
2. Deploy **embedding-backend** — it starts consuming `embeddings:results`
3. Verify end-to-end flow
4. Remove old monolith

### Docker Compose (production)

```yaml
# On GPU machine
services:
  embedding-compute:
    build: ./embedding-compute
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=0
      - CLIP_DEVICE=cuda
    env_file: .env

# On CPU machine
services:
  embedding-backend:
    build: ./embedding-backend
    depends_on:
      - postgres
      - qdrant
    env_file: .env

  embedding-worker:
    build: ./embedding-backend
    command: arq src.workers.main.WorkerSettings
    depends_on:
      - postgres
      - qdrant
      - redis
    env_file: .env
```

## Rollback Plan

If the split causes issues:
1. Both repos can coexist — compute publishes to output streams, but nothing stops the old monolith from also consuming input streams
2. Re-deploy the v1 monolith (still in git history)
3. Remove compute service

## TODO Checklist

### Phase 1: Compute Repo
- [ ] Create `embedding-compute` repo
- [ ] Copy `consumer.py`, `diversity_filter.py`, `clip_embedder.py`
- [ ] Write `config.py` (compute-specific)
- [ ] Write `streams/producer.py`
- [ ] Write `streams/evidence_handler.py`
- [ ] Write `streams/search_handler.py`
- [ ] Write `main.py` entry point
- [ ] Write `Dockerfile` (CUDA)
- [ ] Write `requirements.txt`
- [ ] Test: XADD → verify embeddings:results output
- [ ] Test: XADD search → verify search:results output

### Phase 2: Backend Refactor
- [ ] Remove torch/CLIP from requirements.txt
- [ ] Write `streams/embedding_results_consumer.py`
- [ ] Write `streams/search_results_consumer.py`
- [ ] Write `workers/embedding_storage_worker.py`
- [ ] Write `workers/search_execution_worker.py`
- [ ] Remove `clip_embedder.py`, `diversity_filter.py` from backend
- [ ] Update `config.py` (remove GPU settings, add output stream settings)
- [ ] Update `main.py` lifespan (swap consumers)
- [ ] Test: publish to embeddings:results → verify Qdrant + DB
- [ ] Test: publish to search:results → verify Qdrant search works

### Phase 3: Deploy
- [ ] Deploy compute to GPU machine
- [ ] Deploy backend to CPU machine
- [ ] E2E test: evidence:embed → compute → backend → Qdrant
- [ ] Verify safety nets still work
- [ ] Monitor stream lag (consumer falling behind?)
