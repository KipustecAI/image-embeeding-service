# Architecture v2: Compute / Backend Split

## Goal

The monolithic embedding service has been split into two independent repositories:

1. **`embedding-compute`** (GPU) — Stateless CLIP inference service. Streams in, vectors out.
2. **`image-embeeding-service`** (CPU) — Pipeline orchestration, storage, Search API, monitoring.

## Architecture

```
                    GPU MACHINE                          CPU MACHINE
              ┌────────────────────┐            ┌──────────────────────────────┐
              │  embedding-compute  │            │   image-embeeding-service     │
              │                    │            │                              │
evidence:embed│  StreamConsumer     │ embeddings │  StreamConsumer               │
─────────────►│  ├─ Download imgs  │──:results──►│  ├─ Store in Qdrant (~70ms)  │
              │  ├─ Diversity filter│            │  ├─ Update PostgreSQL        │
              │  ├─ CLIP inference │            │  └─ XACK                     │
              │  └─ Publish vectors│            │                              │
              │                    │            │  StreamConsumer               │
evidence:search                    │ search     │  ├─ Search Qdrant            │
◄─────────────│  ├─ Download img   │──:results──►│  ├─ Store matches in DB      │
  (from API)  │  ├─ CLIP inference │            │  ├─ Store query vector       │
              │  └─ Publish vector │            │  └─ XACK                     │
              │                    │            │                              │
              │  No DB. No Qdrant. │            │  FastAPI API (port 8001)     │
              │  No FastAPI*.      │            │  ├─ POST /api/v1/search      │
              └────────────────────┘            │  ├─ GET  /search/{id}        │
                                                │  ├─ GET  /search/{id}/matches│
                                                │  ├─ GET  /search/user/{uid}  │
                                                │  ├─ POST /recalculate        │
                                                │  ├─ /health, /stats          │
                                                │  └─ /pipeline/status         │
                                                │                              │
                                                │  APScheduler (safety nets)   │
                                                │  ├─ Stale recovery (5m)      │
                                                │  ├─ Recalculation (1h)       │
                                                │  └─ Cleanup (24h)            │
                                                │                              │
                                                │  PostgreSQL + Qdrant         │
                                                │  ├─ evidence_embeddings      │
                                                │  └─ search_queries           │
                                                └──────────────────────────────┘

* Optional minimal health endpoint on compute
```

## Key Design Decisions (post-implementation)

- **No ARQ queue for happy path** — consumers store directly in Qdrant + DB (~70ms). ARQ was removed because it added 5-30s latency.
- **No BatchTrigger for happy path** — each message is processed inline by the consumer. BatchTrigger code still exists but is not wired in lifespan.
- **Gateway auth** — no API key. The service runs behind the API Gateway which injects `X-User-Id`, `X-User-Role`, `X-Request-Id` headers.
- **Search API** — the backend publishes to `evidence:search` stream (not the Video Server). Users submit searches via `POST /api/v1/search`.
- **Recalculation without GPU** — query vectors are stored in a `search_queries` Qdrant collection. Recalculation retrieves the vector and re-searches directly (~100ms, no GPU needed).

## Why Split

| Concern | Monolith | Split |
|---------|----------|-------|
| GPU utilization | Idles during DB/Qdrant I/O | Saturated with inference |
| Scaling | Scale everything together | Scale GPU and CPU independently |
| Cost | Expensive GPU runs cheap DB logic | GPU only for inference |
| Model upgrades | Redeploy entire service | Redeploy only compute |
| Dependencies | torch + SQLAlchemy + qdrant-client + ... | torch only vs SQLAlchemy + qdrant only |
| Docker image | ~5GB (torch + CUDA + everything) | Compute: ~4GB (torch). Backend: ~200MB |

## Stream Topology

```
Video Server
     │
     └─ XADD evidence:embed   ──► embedding-compute ──► XADD embeddings:results ──► image-embeeding-service

image-embeeding-service (POST /api/v1/search)
     │
     └─ XADD evidence:search  ──► embedding-compute ──► XADD search:results     ──► image-embeeding-service
```

4 streams total:
- `evidence:embed` — input: evidence to embed (Video Server → Compute)
- `evidence:search` — input: search queries (Backend API → Compute)
- `embeddings:results` — output: computed vectors (Compute → Backend)
- `search:results` — output: query vectors (Compute → Backend)

## Implementation Steps

| Step | Doc | Description |
|------|-----|-------------|
| 1 | [01_REPO_STRUCTURE.md](01_REPO_STRUCTURE.md) | Folder structure for both repos |
| 2 | [02_COMPUTE_SERVICE.md](02_COMPUTE_SERVICE.md) | GPU compute service: streams → CLIP → streams |
| 3 | [03_BACKEND_SERVICE.md](03_BACKEND_SERVICE.md) | Backend service: streams → Qdrant + DB + API |
| 4 | [04_STREAM_CONTRACTS.md](04_STREAM_CONTRACTS.md) | Stream payload schemas (the contract between services) |
| 5 | [05_MIGRATION_PLAN.md](05_MIGRATION_PLAN.md) | Migration status (completed) |
