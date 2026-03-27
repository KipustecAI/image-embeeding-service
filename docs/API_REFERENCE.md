# API Reference

Base URL: `http://localhost:8001`

## Authentication

Most endpoints require the `X-API-Key` header matching the `EMBEDDING_SERVICE_API_KEY` environment variable.

**No auth required:** `GET /`, `GET /health`, `POST /api/v1/embed/evidence`, `GET /api/v1/stats`

**Auth required:** All other endpoints (`/search/manual`, `/process/*`, `/recalculate/*`)

```
X-API-Key: <your-api-key>
```

Missing or invalid keys return `401 Unauthorized`.

---

## Endpoints

### GET / — Service Info

Returns basic service status. No authentication required.

**Response:**
```json
{
  "service": "Image Embedding Service",
  "status": "running",
  "environment": "development",
  "scheduler_enabled": true
}
```

---

### GET /health — Health Check

Checks that all components (CLIP embedder, Qdrant, Video Server client) are initialized. Returns Qdrant collection stats. No authentication required.

**Response (200):**
```json
{
  "status": "healthy",
  "components": {
    "embedder": true,
    "vector_db": true,
    "api_client": true
  },
  "vector_db_stats": {
    "collection": "evidence_embeddings",
    "points": 1250,
    "status": "green"
  }
}
```

**Response (503):** Service not fully initialized or Qdrant unreachable.

---

### GET /api/v1/stats — Service Statistics

Returns Qdrant collection details and current CLIP configuration.

**Response:**
```json
{
  "service": "Image Embedding Service",
  "vector_database": {
    "collection_name": "evidence_embeddings",
    "vector_size": 512,
    "distance_metric": "Cosine",
    "points_count": 1250,
    "indexed_vectors_count": 1250,
    "status": "green"
  },
  "configuration": {
    "model": "ViT-B-32",
    "device": "cpu",
    "vector_size": 512,
    "evidence_batch_size": 50,
    "search_batch_size": 10
  }
}
```

---

### POST /api/v1/embed/evidence — Embed Single Evidence

Manually embed a single evidence image. Downloads the image, generates a CLIP embedding, stores it in Qdrant, and marks the evidence as embedded (status=4) in the Video Server.

**Query Parameters:**

| Parameter    | Type   | Required | Description                  |
|-------------|--------|----------|------------------------------|
| evidence_id | string | yes      | UUID of the evidence         |
| image_url   | string | yes      | URL of the image to embed    |
| camera_id   | string | yes      | UUID of the source camera    |

**Response (200):**
```json
{
  "success": true,
  "evidence_id": "550e8400-e29b-41d4-a716-446655440000",
  "embedding_id": "emb_550e8400",
  "vector_dimension": 512
}
```

**Errors:** `400` invalid UUID format, `500` embedding or storage failure.

---

### POST /api/v1/process/evidences — Batch Process Evidences

Fetches all evidences with `status=3` (FOUND) from the Video Server and processes them in batch. This is the same operation the ARQ scheduler runs every 10 minutes.

**Response (200):**
```json
{
  "success": true,
  "total_processed": 12,
  "successful": 11,
  "failed": 1,
  "processing_time_ms": 4520.5
}
```

---

### POST /api/v1/search/manual — Manual Similarity Search

Submit an image and find visually similar evidence in the vector database. Generates a CLIP embedding for the query image, searches Qdrant with cosine similarity, and returns the top matches.

**Query Parameters:**

| Parameter   | Type   | Required | Default | Description                          |
|------------|--------|----------|---------|--------------------------------------|
| search_id  | string | yes      |         | UUID for this search                 |
| user_id    | string | yes      |         | UUID of the requesting user          |
| image_url  | string | yes      |         | URL of the image to search with      |
| threshold  | float  | no       | 0.75    | Minimum cosine similarity (0.0-1.0)  |
| max_results| int    | no       | 50      | Maximum number of results            |
| metadata   | object | no       | null    | Filter conditions (see below)        |

**Metadata Filters:**

Pass as a JSON object to narrow search results:

```json
{
  "camera_id": "uuid-string",
  "object_type": "vehicle",
  "date_from": "2025-08-01T00:00:00Z",
  "date_to": "2025-08-31T23:59:59Z",
  "text_description": "red car near building"
}
```

Filters only apply if the corresponding metadata exists in the Qdrant payloads.

**Response (200):**
```json
{
  "success": true,
  "search_id": "660e8400-e29b-41d4-a716-446655440000",
  "total_matches": 5,
  "search_time_ms": 234.7,
  "results": [
    {
      "evidence_id": "550e8400-e29b-41d4-a716-446655440000",
      "similarity_score": 0.92,
      "image_url": "https://example.com/evidence_crop.jpg"
    }
  ]
}
```

The response includes up to 10 results inline. Full results are stored in Redis via the Video Server.

---

### POST /api/v1/process/searches — Batch Process Searches

Fetches all pending searches (`status=1`) from the Video Server and processes them. Same operation the ARQ scheduler runs every 1 minute.

**Response (200):**
```json
{
  "success": true,
  "total_processed": 3,
  "successful": 3,
  "failed": 0
}
```

---

### POST /api/v1/recalculate/searches — Recalculate Searches

Re-run completed searches against the current vector database. Useful when new evidence has been embedded since the original search.

Supports three modes:
1. **Specific IDs** — provide `search_ids` to recalculate specific searches
2. **Batch** — use `limit` and `hours_old` to recalculate a batch
3. **All** — set `force_all=true` to recalculate all eligible searches

Eligible searches: `search_status=3` (COMPLETED).

**Query Parameters:**

| Parameter  | Type     | Required | Default | Description                                  |
|-----------|----------|----------|---------|----------------------------------------------|
| search_ids| string[] | no       | null    | Specific search UUIDs to recalculate         |
| limit     | int      | no       | 10      | Max searches to recalculate (1-100)          |
| hours_old | int      | no       | null    | Only recalculate if processed > X hours ago  |
| force_all | bool     | no       | false   | Recalculate ALL eligible searches            |

**Response (200):**
```json
{
  "success": true,
  "message": "Recalculated 5 searches",
  "total_processed": 5,
  "successful": 5,
  "failed": 0,
  "mode": "batch",
  "search_ids": null
}
```

---

### POST /api/v1/recalculate/search/{search_id} — Recalculate Single Search

Convenience endpoint to recalculate one search by ID. Delegates to the batch recalculation endpoint internally.

**Path Parameters:**

| Parameter | Type   | Description              |
|----------|--------|--------------------------|
| search_id| string | UUID of the search       |

**Response:** Same format as the batch recalculate endpoint.

---

## Processing Flows

### Evidence Embedding (automatic every 10 min)

```
Video Server                    Embedding Service                  Qdrant
     |                                |                              |
     |  GET evidences (status=3)      |                              |
     |<-------------------------------|                              |
     |  return [{id, camera_id, urls}]|                              |
     |------------------------------->|                              |
     |                                |  download images             |
     |                                |  generate CLIP vectors       |
     |                                |  upsert(id, vector, meta)    |
     |                                |----------------------------->|
     |                                |                         ok   |
     |                                |<-----------------------------|
     |  PATCH status = 4 (EMBEDDED)   |                              |
     |<-------------------------------|                              |
```

### Image Search (automatic every 1 min)

```
Video Server                    Embedding Service                  Qdrant
     |                                |                              |
     |  GET searches (status=1)       |                              |
     |<-------------------------------|                              |
     |  return [{id, image_url, ...}] |                              |
     |------------------------------->|                              |
     |  PATCH status = 2 (PROGRESS)   |                              |
     |<-------------------------------|                              |
     |                                |  download query image        |
     |                                |  generate CLIP vector        |
     |                                |  search(vector, threshold)   |
     |                                |----------------------------->|
     |                                |  return [{id, score}]        |
     |                                |<-----------------------------|
     |  POST results -> Redis cache   |                              |
     |<-------------------------------|                              |
     |  PATCH status = 3 (COMPLETED)  |                              |
     |  similarity = 1 (none) | 2 (found)                            |
     |<-------------------------------|                              |
```

## Qdrant Payload Structure

Each stored embedding has the following metadata in Qdrant:

```json
{
  "evidence_id": "uuid-string",
  "camera_id": "uuid-string",
  "image_index": 0,
  "total_images": 3,
  "image_url": "https://example.com/image.jpg",
  "created_at": "2025-08-28T17:00:00Z",
  "source_type": "evidence"
}
```

Indexed fields (used for filtering): `source_type`, `camera_id`, `evidence_id`.

## Status Codes Reference

| Entity      | Status | Meaning       |
|------------|--------|---------------|
| Evidence   | 1      | TO_WORK       |
| Evidence   | 2      | IN_PROGRESS   |
| Evidence   | 3      | FOUND         |
| Evidence   | 4      | EMBEDDED      |
| Search     | 1      | TO_WORK       |
| Search     | 2      | IN_PROGRESS   |
| Search     | 3      | COMPLETED     |
| Search     | 4      | FAILED        |
| Similarity | 1      | NO_MATCHES    |
| Similarity | 2      | MATCHES_FOUND |
