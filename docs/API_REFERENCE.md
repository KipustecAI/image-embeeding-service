# API Reference (v2.1)

Base URL: `http://localhost:8001`

## Authentication

This service runs behind the API Gateway. The gateway authenticates requests and injects user context as HTTP headers. **No API key needed.**

| Header | Type | Description |
|--------|------|-------------|
| `X-User-Id` | UUID | Authenticated user's ID (always present) |
| `X-User-Role` | string | User role: `root`, `dev`, `admin`, `user`, `guest` |
| `X-Request-Id` | UUID | Request tracing ID |
| `X-User-Scopes` | string | Comma-separated permission scopes |
| `X-App-Type` | integer | Application type: 1=LOOKIA, 2=PARKIA, 3=GOBIA |

See [GATEWAY_HEADERS.md](../../lookia/microservices/video-server_microservicios_apigateway/docs/GATEWAY_HEADERS.md) for full details.

---

## Endpoints Summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info |
| GET | `/health` | Component health check |
| GET | `/api/v1/stats` | Pipeline counts + Qdrant stats |
| GET | `/api/v1/pipeline/status` | Full status (counts + consumers) |
| POST | `/api/v1/search` | Submit a similarity search |
| GET | `/api/v1/search/{search_id}` | Get search status |
| GET | `/api/v1/search/{search_id}/matches` | Get match results (paginated) |
| GET | `/api/v1/search/user/{user_id}` | List searches by user (paginated) |
| POST | `/api/v1/recalculate/searches` | Re-queue old searches for recalculation |

---

## GET / — Service Info

**Response:**
```json
{
  "service": "Image Embedding Backend",
  "version": "2.1.0",
  "status": "running",
  "environment": "development"
}
```

---

## GET /health — Health Check

Checks PostgreSQL, Qdrant, scheduler, and stream consumers.

**Response (200):**
```json
{
  "status": "healthy",
  "components": {
    "database": true,
    "qdrant": true,
    "scheduler": true
  },
  "consumers": {
    "embed": {
      "stream": "embeddings:results",
      "group": "backend-workers",
      "running": true,
      "thread_alive": true,
      "messages_processed": 12,
      "messages_failed": 0,
      "messages_dead_lettered": 0
    },
    "search": { "...": "same structure" }
  }
}
```

---

## GET /api/v1/stats — Service Statistics

**Response:**
```json
{
  "service": "Image Embedding Backend",
  "pipeline": {
    "embedding_requests": { "to_work": 0, "working": 0, "embedded": 5, "done": 0, "error": 0 },
    "search_requests": { "to_work": 0, "working": 0, "completed": 3, "error": 0 }
  },
  "qdrant": {
    "collection_name": "evidence_embeddings",
    "vector_size": 512,
    "distance_metric": "Cosine",
    "points_count": 5
  }
}
```

---

## GET /api/v1/pipeline/status — Full Pipeline Status

Combined view for monitoring scripts. Returns DB counts + consumer health.

**Response:**
```json
{
  "status_counts": {
    "embedding": { "to_work": 0, "working": 0, "embedded": 5, "done": 0, "error": 0 },
    "search": { "to_work": 0, "working": 0, "completed": 3, "error": 0 }
  },
  "consumers": {
    "embed": { "running": true, "messages_processed": 12, "...": "..." },
    "search": { "running": true, "messages_processed": 3, "...": "..." }
  }
}
```

---

## POST /api/v1/search — Submit Similarity Search

Creates a search request and publishes it to the GPU compute stream. The GPU computes the CLIP embedding, and the backend executes the Qdrant search when the vector returns.

**Request Body:**
```json
{
  "image_url": "https://storage.example.com/query.jpg",
  "threshold": 0.75,
  "max_results": 50,
  "weapons_filter": "only",
  "weapon_classes": ["handgun"],
  "metadata": {
    "camera_id": "660e8400-..."
  }
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| image_url | string | yes | | URL of the image to search with |
| threshold | float | no | 0.75 | Minimum cosine similarity (0.0-1.0) |
| max_results | int | no | 50 | Maximum number of results |
| weapons_filter | string | no | `"all"` | `all` \| `only` \| `exclude` \| `analyzed_clean` — see below |
| weapon_classes | list[str] | no | null | Class subset (e.g. `["handgun"]`). Only meaningful with `weapons_filter="only"`; ignored otherwise. |
| metadata | object | no | null | Filter conditions (camera_id, object_type) |

`user_id` is automatically taken from the `X-User-Id` gateway header.

**Weapons filtering modes:**

| `weapons_filter` | Returns |
|---|---|
| `"all"` *(default)* | All matches, unchanged from legacy behavior |
| `"only"` | Only images with at least one weapon detection (optionally narrowed by `weapon_classes`) |
| `"exclude"` | Images without weapons — includes both analyzed-clean and unanalyzed images |
| `"analyzed_clean"` | **False-positive review queue** — only images explicitly analyzed by the weapons service and found clean |

See [../weapons/04_SEARCH_API.md](../weapons/04_SEARCH_API.md) for the full design.

**Response (202 Accepted):**
```json
{
  "search_id": "e6b50483-df9b-4ee8-969a-88708c039430",
  "status": "pending",
  "message": "Search submitted, check status at /api/v1/search/e6b50483-..."
}
```

**Data Flow:**
```
POST /api/v1/search
  → DB row (status=pending)
  → XADD evidence:search (to GPU)
  → GPU computes CLIP vector
  → XADD search:results (back to backend)
  → Backend searches Qdrant → stores matches in DB
  → status=completed
```

---

## GET /api/v1/search/{search_id} — Search Status

Returns search status and summary. **Does not include match results** — use the `/matches` endpoint for that.

**Response (completed):**
```json
{
  "search_id": "e6b50483-...",
  "user_id": "550e8400-...",
  "image_url": "https://storage.example.com/query.jpg",
  "status": "completed",
  "similarity_status": "matches_found",
  "total_matches": 3,
  "threshold": 0.75,
  "max_results": 50,
  "created_at": "2026-03-27T20:47:44.590000",
  "completed_at": "2026-03-27T20:47:44.770000",
  "error": null
}
```

**Status values:** `pending`, `working`, `completed`, `error`

**Similarity status values:** `no_matches`, `matches_found`

**Response (404):**
```json
{ "detail": "Search not found" }
```

---

## GET /api/v1/search/{search_id}/matches — Match Results (Paginated)

Returns individual match results sorted by similarity score (highest first).

**Query Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| limit | int | 20 | 1-100 | Results per page |
| offset | int | 0 | 0+ | Skip N results |

**Response:**
```json
{
  "search_id": "e6b50483-...",
  "total": 3,
  "limit": 20,
  "offset": 0,
  "matches": [
    {
      "evidence_id": "test-1774641848-7055",
      "camera_id": "f39d4374-8a68-4b34-b3ef-5a0514e81d92",
      "similarity_score": 0.8349,
      "image_url": "https://storage.example.com/crop.jpg",
      "metadata": {
        "source_type": "evidence",
        "image_index": 0,
        "total_images": 1
      }
    }
  ]
}
```

---

## GET /api/v1/search/user/{user_id} — List User Searches (Paginated)

Returns all searches by a user, most recent first.

**Query Parameters:**

| Parameter | Type | Default | Range |
|-----------|------|---------|-------|
| limit | int | 20 | 1-100 |
| offset | int | 0 | 0+ |

**Response:**
```json
{
  "user_id": "550e8400-...",
  "total": 25,
  "limit": 20,
  "offset": 0,
  "searches": [
    {
      "search_id": "e6b50483-...",
      "status": "completed",
      "similarity_status": "matches_found",
      "total_matches": 3,
      "image_url": "https://storage.example.com/query.jpg",
      "created_at": "2026-03-27T20:47:44.590000"
    }
  ]
}
```

---

## POST /api/v1/recalculate/searches — Recalculate Searches

Re-runs old completed searches against the current Qdrant state using stored query vectors. No GPU needed — the backend retrieves the original query vector from the `search_queries` Qdrant collection and re-searches `evidence_embeddings` directly (~100ms per search).

Searches created before the query vector storage feature was added are skipped (reported in `skipped` count).

**Query Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| limit | int | 20 | 1-100 | Max searches to recalculate |
| hours_old | int | 2 | 0-168 | Only searches completed more than X hours ago (0 = all) |

**Response:**
```json
{
  "success": true,
  "message": "Recalculated 3 searches (2 skipped — no stored vector)",
  "total": 3,
  "skipped": 2
}
```

---

## Event-Driven Processing

### Evidence Embedding (via Redis Streams)

```
Video Server → XADD evidence:embed → GPU compute → XADD embeddings:results → Backend → Qdrant + DB
```

The backend consumer receives pre-computed 512-dim vectors and stores them directly in Qdrant + PostgreSQL. No ARQ queue — ~70ms per upsert.

### Image Search (via API + Redis Streams)

```
POST /api/v1/search → XADD evidence:search → GPU compute → XADD search:results → Backend → Qdrant search → DB
```

On completion, the query vector is stored in the `search_queries` Qdrant collection for future recalculation.

End-to-end latency: ~2 seconds.

### Search Recalculation (no GPU)

```
Scheduled job / POST /api/v1/recalculate → retrieve query vector from Qdrant → search evidence_embeddings → update matches in DB
```

Uses stored query vectors — no GPU, no image re-download. ~100ms per search.

## Qdrant Collections

| Collection | Purpose |
|------------|---------|
| `evidence_embeddings` | Evidence image vectors (512-dim CLIP ViT-B-32, cosine distance). Payload indices on `evidence_id`, `camera_id`, `source_type`, `user_id`, `device_id`, `app_id` (multi-tenant filtering), plus `weapon_analyzed`, `has_weapon`, `weapon_classes` (weapons filtering — see [../weapons/](../weapons/)) |
| `search_queries` | Stored query vectors for recalculation. One point per search, payload: `search_id` only |

## Database Tables

| Table | Purpose |
|-------|---------|
| `embedding_requests` | Tracks each evidence through the embedding pipeline |
| `evidence_embeddings` | One row per vector stored in Qdrant (FK → embedding_requests) |
| `search_requests` | Tracks each similarity search lifecycle. `qdrant_query_point_id` links to the stored query vector |
| `search_matches` | Individual match results per search (FK → search_requests) |

## Background Jobs

| Job | Interval | Purpose |
|-----|----------|---------|
| Stale recovery | 5m | Reset stuck WORKING rows back to TO_WORK |
| Recalculation | 1h | Re-search Qdrant with stored query vectors (no GPU needed) |
| Cleanup | 24h | Delete completed/error rows older than 30 days |
