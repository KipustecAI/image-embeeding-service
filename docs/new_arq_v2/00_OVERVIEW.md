# Architecture v2: Compute / Backend Split

## Goal

Split the current monolithic service into two independent repositories:

1. **`embedding-compute`** (GPU) — Stateless CLIP inference service. Streams in, vectors out.
2. **`embedding-backend`** (CPU) — Pipeline orchestration, storage, API, monitoring.

## Current (v1 monolith)

```
evidence:embed ──► [ FastAPI + ARQ Worker (GPU + DB + Qdrant + everything) ]
```

Everything runs together: CLIP model, Qdrant writes, DB updates, API, safety nets. GPU machine wastes cycles on I/O and orchestration.

## Target (v2 split)

```
                    GPU MACHINE                          CPU MACHINE
              ┌────────────────────┐            ┌─────────────────────────┐
              │  embedding-compute  │            │   embedding-backend      │
              │                    │            │                          │
evidence:embed│  StreamConsumer     │ embeddings │  StreamConsumer           │
─────────────►│  ├─ Download imgs  │──:results──►│  ├─ Store in Qdrant      │
              │  ├─ Diversity filter│            │  ├─ Update PostgreSQL    │
              │  ├─ CLIP inference │            │  ├─ BatchTrigger          │
              │  └─ Publish vectors│            │  └─ Safety nets           │
              │                    │            │                          │
evidence:search                    │ search     │  FastAPI API              │
─────────────►│  ├─ Download img   │──:results──►│  ├─ /health, /stats      │
              │  ├─ CLIP inference │            │  ├─ /pipeline/status     │
              │  └─ Publish vector │            │  ├─ /recalculate         │
              │                    │            │  └─ /internal/trigger    │
              │  No DB. No Qdrant. │            │                          │
              │  No FastAPI*.      │            │  PostgreSQL + Qdrant     │
              └────────────────────┘            └─────────────────────────┘

* Optional minimal health endpoint
```

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
     ├─ XADD evidence:embed   ──► embedding-compute ──► XADD embeddings:results ──► embedding-backend
     │
     └─ XADD evidence:search  ──► embedding-compute ──► XADD search:results     ──► embedding-backend
```

4 streams total:
- `evidence:embed` — input: evidence to embed (Video Server → Compute)
- `evidence:search` — input: search queries (Video Server → Compute)
- `embeddings:results` — output: computed vectors (Compute → Backend)
- `search:results` — output: query vectors (Compute → Backend)

## File Split Plan

### Current file → which repo

| Current file | Goes to | Notes |
|---|---|---|
| `src/streams/consumer.py` | **Both** (shared) | Generic StreamConsumer, copy to both repos |
| `src/streams/evidence_consumer.py` | **Compute** | Rewritten: downloads + CLIP + publish results |
| `src/streams/search_consumer.py` | **Compute** | Rewritten: download + CLIP + publish result |
| `src/services/diversity_filter.py` | **Compute** | Runs before CLIP inference |
| `src/infrastructure/embedding/clip_embedder.py` | **Compute** | Core CLIP model |
| `src/services/batch_trigger.py` | **Backend** | Orchestration logic |
| `src/services/safety_nets.py` | **Backend** | Pipeline recovery |
| `src/db/` (all models + repos) | **Backend** | PostgreSQL persistence |
| `src/infrastructure/database.py` | **Backend** | SQLAlchemy engine |
| `src/infrastructure/vector_db/qdrant_repository.py` | **Backend** | Qdrant storage |
| `src/workers/embedding_worker.py` | **Backend** | Rewritten: receives vectors, stores in Qdrant + DB |
| `src/workers/search_worker.py` | **Backend** | Rewritten: receives vector, searches Qdrant |
| `src/main.py` | **Backend** | FastAPI app + lifespan |
| `src/infrastructure/config.py` | **Both** (different configs) | Compute: CLIP + streams. Backend: DB + Qdrant + streams |
| `src/api/dependencies.py` | **Backend** | API auth |
| `src/application/` | **Delete** | Old use cases, no longer needed |
| `src/domain/` | **Backend** (partial) | Keep entities for Qdrant payloads |
| `src/infrastructure/scheduler/` | **Delete** | Legacy, already replaced |
| `src/infrastructure/api/` | **Delete** | Legacy Video Server client |

## Implementation Steps

| Step | Doc | Description |
|------|-----|-------------|
| 1 | [01_REPO_STRUCTURE.md](01_REPO_STRUCTURE.md) | Define folder structure for both repos |
| 2 | [02_COMPUTE_SERVICE.md](02_COMPUTE_SERVICE.md) | GPU compute service: streams → CLIP → streams |
| 3 | [03_BACKEND_SERVICE.md](03_BACKEND_SERVICE.md) | Backend service: streams → Qdrant + DB + API |
| 4 | [04_STREAM_CONTRACTS.md](04_STREAM_CONTRACTS.md) | Stream payload schemas (the contract between services) |
| 5 | [05_MIGRATION_PLAN.md](05_MIGRATION_PLAN.md) | Steps to split current repo into two |
