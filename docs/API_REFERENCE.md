# API Reference (v2.0)

Base URL: `http://localhost:8001`

## Architecture Overview

This is an **event-driven** service. Work arrives via Redis Streams, not HTTP polling:

```
Redis Stream (evidence:embed / evidence:search)
       │
       ▼
  StreamConsumer ──► DB row (status=1) ──► BatchTrigger ──► ARQ Worker
       │                                                        │
       │                                                        ▼
       │                                              Qdrant + PostgreSQL
       │
  The HTTP API is for monitoring, manual triggers, and querying results.
```

## Authentication

Endpoints marked with **Auth** require the `X-API-Key` header:

```
X-API-Key: <EMBEDDING_SERVICE_API_KEY>
```

Missing or invalid keys return `401 Unauthorized`.

---

## Endpoints Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Service info |
| GET | `/health` | No | Component health check |
| GET | `/api/v1/stats` | No | Pipeline DB counts + CLIP config |
| GET | `/api/v1/pipeline/status` | No | Full status (DB counts + triggers + consumers) |
| POST | `/api/v1/internal/trigger/{name}` | No | Cross-process trigger notification |
| POST | `/api/v1/recalculate/searches` | Yes | Manually re-queue searches for recalculation |

---

## GET / — Service Info

No auth. Returns basic service info.

**Response:**
```json
{
  "service": "Image Embedding Service",
  "version": "2.0.0",
  "status": "running",
  "environment": "development"
}
```

---

## GET /health — Health Check

No auth. Checks PostgreSQL, scheduler, batch triggers, and stream consumers.

**Response (200):**
```json
{
  "status": "healthy",
  "components": {
    "database": true,
    "scheduler": true
  },
  "triggers": {
    "embedding": {
      "name": "embedding",
      "running": true,
      "pending_count": 0,
      "batch_size": 20,
      "max_wait_seconds": 5.0,
      "has_active_timer": false,
      "total_flushes": 12,
      "total_events": 45,
      "flush_reasons": {"batch_full": 2, "timeout": 10, "manual": 0, "shutdown": 0}
    },
    "search": { "..." : "same structure" }
  },
  "consumers": {
    "embed": {
      "stream": "evidence:embed",
      "group": "embed-workers",
      "consumer": "hostname-123",
      "running": true,
      "thread_alive": true,
      "messages_processed": 45,
      "messages_failed": 0,
      "messages_dead_lettered": 0
    },
    "search": { "..." : "same structure" }
  }
}
```

Returns `"status": "degraded"` if any component is down.

---

## GET /api/v1/stats — Service Statistics

No auth. Returns pipeline request counts by status and CLIP configuration.

**Response:**
```json
{
  "service": "Image Embedding Service",
  "pipeline": {
    "embedding_requests": {
      "to_work": 0,
      "working": 2,
      "embedded": 35,
      "done": 120,
      "error": 3
    },
    "search_requests": {
      "to_work": 0,
      "working": 0,
      "completed": 18,
      "error": 1
    }
  },
  "configuration": {
    "model": "ViT-B-32",
    "device": "cuda",
    "vector_size": 512
  }
}
```

---

## GET /api/v1/pipeline/status — Full Pipeline Status

No auth. Combined view for monitoring and E2E test scripts. Includes DB counts, trigger metrics, and consumer health in one call.

**Response:**
```json
{
  "status_counts": {
    "embedding": { "to_work": 0, "working": 0, "embedded": 35, "done": 120, "error": 3 },
    "search": { "to_work": 0, "working": 0, "completed": 18, "error": 1 }
  },
  "triggers": {
    "embedding": { "total_flushes": 12, "total_events": 45, "pending_count": 0, "..." : "..." },
    "search": { "total_flushes": 5, "total_events": 18, "pending_count": 0, "..." : "..." }
  },
  "consumers": {
    "embed": { "running": true, "messages_processed": 45, "messages_failed": 0, "..." : "..." },
    "search": { "running": true, "messages_processed": 18, "messages_failed": 0, "..." : "..." }
  }
}
```

---

## POST /api/v1/internal/trigger/{trigger_name} — Notify Trigger

No auth. Called by the ARQ worker (separate process) to notify in-memory batch triggers. The worker cannot access triggers directly since they live in the API process memory.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| trigger_name | string | `embedding` or `search` |

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| count | int | 1 | Number of items to notify |

**Response (200):**
```json
{
  "notified": true,
  "trigger": "embedding",
  "count": 5
}
```

**Response (404):** Trigger name not found.

---

## POST /api/v1/recalculate/searches — Recalculate Searches

**Auth required.** Re-queues completed searches back to `status=1` so the pipeline reprocesses them against the current Qdrant state. Useful when new evidence has been embedded since the original search.

Eligible searches: `status=COMPLETED` + `similarity_status=MATCHES_FOUND` + processed more than `hours_old` ago.

**Query Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| limit | int | 20 | 1-100 | Max searches to recalculate |
| hours_old | int | 2 | 1-168 | Only searches processed > X hours ago |

**Response (200):**
```json
{
  "success": true,
  "message": "Queued 5 searches for recalculation",
  "total": 5
}
```

**Response (200, nothing to do):**
```json
{
  "success": true,
  "message": "No searches eligible",
  "total": 0
}
```

---

## Event-Driven Processing Flows

### Evidence Embedding

Input arrives via Redis Stream `evidence:embed`, not HTTP.

```
Video Server / ETL
       │
       │ XADD evidence:embed { evidence.ready.embed, payload: { evidence_id, camera_id, image_urls } }
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Dedup check (evidence_id already in DB?)
       ├─ Diversity filter (Bhattacharyya histogram, skip near-duplicate crops)
       ├─ Create embedding_request row (status=1)
       └─ BatchTrigger.notify()
              │
              ├─ Flush when count >= 20 OR timeout 5s
              ▼
         ARQ Worker: process_evidence_embeddings_batch
              │
              ├─ Phase 1: Parallel image download + CLIP embed (asyncio.gather)
              ├─ Phase 2: Bulk Qdrant upsert (1 call) + bulk DB commit (1 call)
              └─ Update status → EMBEDDED (3)
```

### Image Search

Input arrives via Redis Stream `evidence:search`.

```
Video Server
       │
       │ XADD evidence:search { search.created, payload: { search_id, user_id, image_url, threshold, metadata } }
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Dedup check
       ├─ Create search_request row (status=1)
       └─ BatchTrigger.notify()
              │
              ▼
         ARQ Worker: process_image_searches_batch
              │
              ├─ Download query image + CLIP embed
              ├─ Qdrant similarity search (cosine, with metadata filters)
              └─ Update status → COMPLETED (3), set similarity_status + total_matches
```

## Safety Nets (Background Jobs)

These run automatically via APScheduler inside the API process:

| Job | Interval | Purpose |
|-----|----------|---------|
| `embedding_safety_net` | 60s | Catch status=1 embedding rows missed by BatchTrigger |
| `search_safety_net` | 120s | Catch status=1 search rows missed by BatchTrigger |
| `recover_stale_working` | 5m | Reset status=2 rows stuck >10min back to status=1 |
| `recalculate_searches` | 1h | Re-queue old completed searches for reprocessing |
| `cleanup_old_requests` | 24h | Delete completed/error rows older than 30 days |

## Database Tables

### embedding_requests

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| evidence_id | string | External evidence reference (indexed) |
| camera_id | string | Source camera (indexed) |
| status | int | Pipeline status (indexed) |
| image_urls | JSONB | List of image URLs to embed |
| worker_id | string | Which worker picked it up |
| error_message | text | Error details if failed |
| retry_count | int | Times retried |
| stream_message_id | string | Redis Stream message ID |
| created_at | datetime | Row creation time (indexed) |

### evidence_embeddings

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| request_id | UUID | FK → embedding_requests |
| qdrant_point_id | string | Qdrant vector ID (unique, indexed) |
| image_index | int | Which image in the evidence |
| image_url | text | Source image URL |
| json_data | JSONB | Metadata stored in Qdrant |

### search_requests

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| search_id | string | External search reference (indexed) |
| user_id | string | Requesting user (indexed) |
| image_url | text | Query image URL |
| status | int | Pipeline status (indexed) |
| similarity_status | int | 1=NO_MATCHES, 2=MATCHES_FOUND |
| threshold | float | Cosine similarity threshold |
| max_results | int | Max results to return |
| search_metadata | JSONB | Filters (camera_id, date range, etc.) |
| total_matches | int | Number of matches found |

## Status Codes

### Embedding Requests

| Status | Name | Description |
|--------|------|-------------|
| 1 | TO_WORK | Created, waiting for BatchTrigger to dispatch |
| 2 | WORKING | Picked up by ARQ worker |
| 3 | EMBEDDED | CLIP vectors stored in Qdrant |
| 4 | DONE | Fully processed |
| 5 | ERROR | Failed (see error_message) |

### Search Requests

| Status | Name | Description |
|--------|------|-------------|
| 1 | TO_WORK | Created, waiting for dispatch |
| 2 | WORKING | Picked up by ARQ worker |
| 3 | COMPLETED | Search executed, results stored |
| 4 | ERROR | Failed (see error_message) |

### Similarity Status

| Status | Name |
|--------|------|
| 1 | NO_MATCHES |
| 2 | MATCHES_FOUND |

## Qdrant Payload Structure

Each stored embedding in the `evidence_embeddings` collection:

```json
{
  "source_type": "evidence",
  "evidence_id": "uuid-string",
  "camera_id": "uuid-string",
  "image_index": 0,
  "total_images": 3,
  "image_url": "https://storage.example.com/crop.jpg",
  "created_at": "2026-03-27T12:00:00Z"
}
```

**Indexed fields:** `source_type`, `camera_id`, `evidence_id`.

**Vector:** 512-dimensional CLIP ViT-B-32, cosine distance.
