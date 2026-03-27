# cURL Examples

All examples assume the service is running at `http://localhost:8001`.

Replace `YOUR_API_KEY` with your `EMBEDDING_SERVICE_API_KEY` value.

---

## Health & Status (no auth required)

```bash
# Service info
curl http://localhost:8001/

# Health check
curl http://localhost:8001/health

# Service statistics (Qdrant collection + config)
curl http://localhost:8001/api/v1/stats
```

---

## Embed a Single Evidence

```bash
curl -X POST "http://localhost:8001/api/v1/embed/evidence?evidence_id=550e8400-e29b-41d4-a716-446655440000&image_url=https://example.com/evidence_image.jpg&camera_id=660e8400-e29b-41d4-a716-446655440000"
```

---

## Batch Process All Pending Evidences

Fetches all evidences with `status=3` and embeds them:

```bash
curl -X POST http://localhost:8001/api/v1/process/evidences \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Manual Similarity Search

### Basic search (default threshold 0.75, max 50 results)

```bash
curl -X POST "http://localhost:8001/api/v1/search/manual?search_id=770e8400-e29b-41d4-a716-446655440000&user_id=880e8400-e29b-41d4-a716-446655440000&image_url=https://example.com/query_image.jpg" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Search with custom threshold and limit

```bash
curl -X POST "http://localhost:8001/api/v1/search/manual?search_id=770e8400-e29b-41d4-a716-446655440000&user_id=880e8400-e29b-41d4-a716-446655440000&image_url=https://example.com/query_image.jpg&threshold=0.85&max_results=20" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Search with metadata filters

```bash
curl -X POST "http://localhost:8001/api/v1/search/manual?search_id=770e8400-e29b-41d4-a716-446655440000&user_id=880e8400-e29b-41d4-a716-446655440000&image_url=https://example.com/query_image.jpg&threshold=0.80&max_results=30" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "camera_id": "660e8400-e29b-41d4-a716-446655440000",
    "object_type": "vehicle",
    "date_from": "2025-08-01T00:00:00Z",
    "date_to": "2025-08-31T23:59:59Z"
  }'
```

---

## Batch Process All Pending Searches

Fetches all searches with `status=1` and processes them:

```bash
curl -X POST http://localhost:8001/api/v1/process/searches \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Recalculate Searches

### Recalculate a single search by ID

```bash
curl -X POST http://localhost:8001/api/v1/recalculate/search/550e8400-e29b-41d4-a716-446655440000 \
  -H "X-API-Key: YOUR_API_KEY"
```

### Recalculate specific searches by IDs

```bash
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?search_ids=550e8400-e29b-41d4-a716-446655440000&search_ids=660e8400-e29b-41d4-a716-446655440000" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Recalculate a batch (limit + age filter)

```bash
# Recalculate up to 20 searches that were processed more than 2 hours ago
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?limit=20&hours_old=2" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Recalculate ALL eligible searches

```bash
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?force_all=true" \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Example Responses

### Successful embedding

```json
{
  "success": true,
  "evidence_id": "550e8400-e29b-41d4-a716-446655440000",
  "embedding_id": "emb_550e8400",
  "vector_dimension": 512
}
```

### Successful search

```json
{
  "success": true,
  "search_id": "770e8400-e29b-41d4-a716-446655440000",
  "total_matches": 5,
  "search_time_ms": 234.7,
  "results": [
    {
      "evidence_id": "550e8400-e29b-41d4-a716-446655440000",
      "similarity_score": 0.92,
      "image_url": "https://example.com/evidence_crop.jpg"
    },
    {
      "evidence_id": "660e8400-e29b-41d4-a716-446655440000",
      "similarity_score": 0.87,
      "image_url": "https://example.com/evidence_crop2.jpg"
    }
  ]
}
```

### Batch processing result

```json
{
  "success": true,
  "total_processed": 12,
  "successful": 11,
  "failed": 1,
  "processing_time_ms": 4520.5
}
```

### Recalculation result

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

### Error: missing API key

```json
{
  "detail": "API key required"
}
```

### Error: invalid API key

```json
{
  "detail": "Invalid API key"
}
```

### Error: invalid UUID

```json
{
  "detail": "Invalid UUID format: badly formed hexadecimal UUID string"
}
```
