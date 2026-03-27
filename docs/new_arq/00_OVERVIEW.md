# Architecture Migration Overview

## Goal

Evolve the Image Embedding Service from a **polling-based stateless worker** into an **event-driven pipeline with local persistence**, adopting the proven patterns from the deepface-restapi project.

## Current State

```
Video Server (polls every 10min / 1min)
       │ HTTP
       ▼
┌──────────────────┐
│  Embedding Svc   │  ← stateless, no DB
│  FastAPI + ARQ   │
└──┬───────────┬───┘
   ▼           ▼
 Qdrant      Redis
(vectors)   (ARQ queue)
```

**Problems:**
- 10-minute polling delay for evidence, 1-minute for searches
- No local state: debugging requires Video Server logs
- No dedup: same evidence can be picked up twice across cycles
- No retry tracking: failed items retry forever or get lost
- No batch optimization: sequential processing, one-at-a-time Qdrant calls

## Target State

```
Video Server / ETL
       │ XADD (instant)
       ▼
┌──────────────────────────────────────────┐
│          FASTAPI SERVER (port 8001)       │
│                                           │
│  ├─ HTTP API (manual triggers, health)    │
│  ├─ StreamConsumer (daemon thread)        │
│  │   ├─ evidence:embed → DB row (st=1)   │
│  │   └─ evidence:search → DB row (st=1)  │
│  ├─ BatchTrigger (flush on count|timeout) │
│  └─ Safety Nets (catch missed rows)       │
└──┬──────────┬──────────┬─────────────────┘
   ▼          ▼          ▼
 PostgreSQL  Redis      Qdrant
 (requests,  (streams   (512-dim
  embeds)    + ARQ)     vectors)

┌──────────────────────────────────────────┐
│        ARQ WORKER (separate process)      │
│                                           │
│  ├─ process_evidence_embeddings_batch     │
│  │   Phase 1: parallel download + CLIP    │
│  │   Phase 2: bulk Qdrant upsert + DB    │
│  │                                        │
│  └─ process_image_searches_batch          │
│      Phase 1: download + CLIP embed       │
│      Phase 2: batch Qdrant search + store │
└──────────────────────────────────────────┘
```

**Wins:**
- **Instant processing** — events arrive via Redis Streams, not polling
- **Local state** — own DB for debugging, retry tracking, auditing
- **Dedup** — check before processing, skip duplicates
- **Atomic dispatch** — status update + ARQ enqueue in same transaction
- **Batch optimization** — bulk Qdrant upsert, batch search, parallel downloads
- **Reliability** — dead letter queue, reclaim, safety nets, stale row recovery

## Implementation Steps

| Step | Doc | Description | Dependencies |
|------|-----|-------------|--------------|
| 1 | [01_DATABASE.md](01_DATABASE.md) | PostgreSQL + SQLAlchemy + Alembic setup | None |
| 2 | [02_MODELS.md](02_MODELS.md) | DB models for embedding_requests, evidence_embeddings, search_requests | Step 1 |
| 3 | [03_STREAMS.md](03_STREAMS.md) | Redis Streams consumer (generic + evidence/search handlers) | Step 2 |
| 4 | [04_BATCH_TRIGGER.md](04_BATCH_TRIGGER.md) | BatchTrigger signal-driven dispatcher | Step 2 |
| 5 | [05_WORKERS.md](05_WORKERS.md) | Two-phase ARQ workers (parallel extract + bulk store) | Steps 2-4 |
| 6 | [06_SAFETY_NETS.md](06_SAFETY_NETS.md) | Safety nets, stale recovery, cleanup jobs | Steps 2, 5 |
| 7 | [07_API_MIGRATION.md](07_API_MIGRATION.md) | Update FastAPI endpoints + lifespan wiring | Steps 1-6 |
| 8 | [08_DOCKER_DEPS.md](08_DOCKER_DEPS.md) | Docker Compose, requirements, Makefile updates | All |
| 9 | [09_DIVERSITY_FILTER.md](09_DIVERSITY_FILTER.md) | Image diversity filter (ported from deepface) | Step 3 |

## Key Decisions (from Q&A)

- **We are the source of truth** for embedding/search data — no Video Server callbacks
- **Big bang migration** — no backward compatibility, clean slate
- **Search results in Redis with TTL** — dedicated Redis DB, not PostgreSQL
- **Recalculation is ours** — triggerable endpoint + scheduled, like deepface's blacklist rematch
- **Diversity filter required** — Bhattacharyya histogram distance to skip duplicate crops
- **Stream consumer only** — Video Server publishes, we consume
- **APScheduler** for safety nets (same as deepface)
- **Multi-worker ready** from day one (FOR UPDATE SKIP LOCKED)

## Principles (from deepface patterns)

1. **Push over poll** — Redis Streams for instant event delivery
2. **Own your state** — we are the source of truth for our domain data, expose query endpoints
3. **Two-phase workers** — Phase 1 parallel extraction, Phase 2 bulk storage
4. **Dual-trigger batching** — flush on count OR timeout, never wait indefinitely
5. **Safety nets everywhere** — scheduled fallbacks catch anything the primary path misses
6. **Atomic dispatch** — status update + job enqueue succeed or fail together
7. **FOR UPDATE SKIP LOCKED** — prevents double-pickup in concurrent scheduling
8. **Dead letter queue** — messages failing 3+ times go to analysis stream, not infinite retry
9. **Diversity filter** — skip near-duplicate images before processing to save compute
