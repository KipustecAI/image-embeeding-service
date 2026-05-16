# Architecture v2: Compute / Backend Split

## Goal

The monolithic embedding service has been split into two independent repositories:

1. **`embedding-compute`** (GPU) — Stateless CLIP inference service. Streams in, vectors out.
2. **`image-embeeding-service`** (CPU) — Pipeline orchestration, storage, REST API, monitoring.

## Architecture

```
              ┌─────────────────────────────┐        ┌─────────────────────────────────────┐
              │       embedding-compute      │        │       image-embeeding-service        │
              │              (GPU)           │        │                 (CPU)                │
              │                              │        │                                      │
 evidence:embed StreamConsumer                │ embeddings:results                            │
 ─────────────►├─ Download + diversity        ├───────►─ Consumer: download ZIP, upload       │
              │   filter + CLIP inference     │        │   frames to storage, store Qdrant + │
              │                              │        │   DB, publish weapons.detected,     │
              │                              │        │   inline blacklist match            │
              │                              │        │                                      │
 evidence:search                              │ search:results                                │
 ─────────────►├─ Download + CLIP             ├───────►─ Consumer: dispatch by `purpose`      │
              │  (treats blacklist_embed      │        │   ├─ "search" → Qdrant search +    │
              │   identically to search,      │        │   │              store matches      │
              │   echoes purpose +            │        │   └─ "blacklist_embed" →            │
              │   blacklist_entry_id)         │        │            store as blacklist point │
              │                              │        │            + schedule reverse search │
              │                              │        │                                      │
              │  No DB. No Qdrant.            │        │  REST API (port 8001):              │
              │  No FastAPI*.                 │        │  ├─ /api/v1/search (POST + GET ×3)   │
              └─────────────────────────────┘        │  ├─ /api/v1/search/categories        │
                                                       │  ├─ /api/v1/recalculate/searches    │
                                                       │  ├─ /api/v1/blacklist/image-entries │
                                                       │  │   (8 CRUD endpoints — see        │
                                                       │  │    docs/BLACKLIST_API.md)        │
                                                       │  └─ /health, /stats, /pipeline      │
                                                       │                                      │
                                                       │  Producers (→ report-generation):    │
                                                       │  ├─ weapons:detected                │
                                                       │  └─ image:blacklist_match           │
                                                       │                                      │
                                                       │  APScheduler:                       │
                                                       │  ├─ Stale recovery (5m)             │
                                                       │  ├─ Recalculation (1h)              │
                                                       │  ├─ Cleanup (24h)                   │
                                                       │  └─ Blacklist reverse search        │
                                                       │      (one-shot, on new reference)   │
                                                       │                                      │
                                                       │  Storage:                            │
                                                       │  ├─ PostgreSQL                      │
                                                       │  │   ├─ embedding_requests          │
                                                       │  │   ├─ evidence_embeddings         │
                                                       │  │   ├─ search_requests / matches   │
                                                       │  │   └─ blacklist_image_*  (×3)     │
                                                       │  └─ Qdrant                          │
                                                       │      ├─ evidence_embeddings         │
                                                       │      │   (source_type=evidence|     │
                                                       │      │    blacklist)                │
                                                       │      └─ search_queries              │
                                                       └─────────────────────────────────────┘

* Optional minimal health endpoint on compute
```

## Key Design Decisions (post-implementation)

- **No ARQ queue for happy path** — consumers store directly in Qdrant + DB (~70ms). ARQ was removed because it added 5–30s latency.
- **No BatchTrigger for happy path** — each message is processed inline by the consumer. BatchTrigger code still exists but is not wired in lifespan.
- **Gateway auth** — no API key. The service runs behind the API Gateway which injects `X-User-Id`, `X-User-Role`, `X-Request-Id` headers.
- **Search API publishes to `evidence:search` directly** — the backend, not the Video Server, originates similarity searches.
- **Recalculation without GPU** — query vectors are stored in a `search_queries` Qdrant collection. Recalculation retrieves the vector and re-searches directly (~100ms, no GPU needed).
- **Single Qdrant collection for evidence + blacklist** — discriminated by `source_type`. Strict source-type filtering at search time prevents blacklist points from leaking into user-facing results. See [docs/image-blacklist/03_QDRANT.md](../image-blacklist/03_QDRANT.md).
- **Reuse `evidence:search` for blacklist embedding** — adds optional `purpose` + `blacklist_entry_id` fields. Compute echoes both; backend dispatches in `search_results_consumer`. No new streams. See [docs/image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md).
- **Fire-and-forget publishers for report events** — `weapons:detected` and `image:blacklist_match` failures log but never block ingest.

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
                                                                                          │
                                                                                          ├─ XADD weapons:detected → report-generation
                                                                                          └─ XADD image:blacklist_match (inline) → report-generation

image-embeeding-service
     │
     ├─ POST /api/v1/search                                          purpose="search"
     │   └─ XADD evidence:search ──► embedding-compute ──► XADD search:results ──► image-embeeding-service (Qdrant search + matches)
     │
     └─ POST /api/v1/blacklist/image-entries/{id}/references         purpose="blacklist_embed"
         └─ XADD evidence:search ──► embedding-compute ──► XADD search:results ──► image-embeeding-service
                                                                                          │
                                                                                          ├─ store blacklist point + DB row
                                                                                          └─ schedule reverse search
                                                                                                  │
                                                                                                  └─ XADD image:blacklist_match → report-generation
```

6 streams total:

| Stream | Producer | Consumer |
|---|---|---|
| `evidence:embed` | Video Server | embedding-compute |
| `evidence:search` | image-embeeding-service (API or blacklist CRUD) | embedding-compute |
| `embeddings:results` | embedding-compute | image-embeeding-service |
| `search:results` | embedding-compute | image-embeeding-service |
| `weapons:detected` | image-embeeding-service | report-generation |
| `image:blacklist_match` | image-embeeding-service | report-generation |

Full envelope shapes: [04_STREAM_CONTRACTS.md](04_STREAM_CONTRACTS.md). Outbound contracts to report-generation: [../requirements/REPORT_GENERATION_STREAMS.md](../requirements/REPORT_GENERATION_STREAMS.md).

## Implementation Steps

| Step | Doc | Description |
|------|-----|-------------|
| 1 | [01_REPO_STRUCTURE.md](01_REPO_STRUCTURE.md) | Folder structure for both repos |
| 2 | [02_COMPUTE_SERVICE.md](02_COMPUTE_SERVICE.md) | GPU compute service: streams → CLIP → streams |
| 3 | [03_BACKEND_SERVICE.md](03_BACKEND_SERVICE.md) | Backend service: streams → Qdrant + DB + API |
| 4 | [04_STREAM_CONTRACTS.md](04_STREAM_CONTRACTS.md) | Stream payload schemas (the contract between services) |
| 5 | [05_MIGRATION_PLAN.md](05_MIGRATION_PLAN.md) | v1 → v2 migration plan (historical; the migration is complete) |
