# cURL Examples (v2.1)

All examples assume the service is running at `http://localhost:8001`.

Authentication is handled by the API Gateway via headers. For local testing, simulate gateway headers:

```bash
# Gateway headers for local testing
GW="-H 'X-User-Id: test-user-001' -H 'X-User-Role: dev' -H 'X-Request-Id: test-123'"
```

---

## Health & Monitoring (no auth)

```bash
# Service info
curl http://localhost:8001/

# Health check (DB, Qdrant, scheduler, consumers)
curl http://localhost:8001/health

# Pipeline stats (request counts + Qdrant collection info)
curl http://localhost:8001/api/v1/stats

# Full pipeline status (for monitoring / test scripts)
curl -s http://localhost:8001/api/v1/pipeline/status | python3 -m json.tool
```

---

## Submit a Search

```bash
# Submit a search with an image URL
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "max_results": 50
  }'

# Response: 202 Accepted
# {
#   "search_id": "e6b50483-df9b-4ee8-969a-88708c039430",
#   "status": "pending",
#   "message": "Search submitted, check status at /api/v1/search/e6b50483-..."
# }
```

### Search with metadata filters

```bash
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.80,
    "max_results": 20,
    "metadata": {
      "camera_id": "f39d4374-8a68-4b34-b3ef-5a0514e81d92"
    }
  }'
```

### Search with local file (development)

```bash
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: test-user-001" \
  -H "X-User-Role: dev" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "file:///path/to/local/image.jpg",
    "threshold": 0.3,
    "max_results": 20
  }'
```

---

## Check Search Status

```bash
curl http://localhost:8001/api/v1/search/e6b50483-df9b-4ee8-969a-88708c039430 \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Response:
# {
#   "search_id": "e6b50483-...",
#   "status": "completed",
#   "similarity_status": "matches_found",
#   "total_matches": 3,
#   ...
# }
```

---

## Get Match Results (Paginated)

```bash
# First page (top 20 matches by score)
curl "http://localhost:8001/api/v1/search/e6b50483-.../matches?limit=20&offset=0" \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Second page
curl "http://localhost:8001/api/v1/search/e6b50483-.../matches?limit=20&offset=20" \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Response:
# {
#   "search_id": "e6b50483-...",
#   "total": 3,
#   "limit": 20,
#   "offset": 0,
#   "matches": [
#     {
#       "evidence_id": "test-1774641848-7055",
#       "camera_id": "f39d4374-...",
#       "similarity_score": 0.8349,
#       "image_url": "https://...",
#       "metadata": { "source_type": "evidence", "image_index": 0 }
#     }
#   ]
# }
```

---

## List User Searches (Paginated)

```bash
curl "http://localhost:8001/api/v1/search/user/550e8400-e29b-41d4-a716-446655440000?limit=10&offset=0" \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Response:
# {
#   "user_id": "550e8400-...",
#   "total": 5,
#   "limit": 10,
#   "offset": 0,
#   "searches": [
#     { "search_id": "...", "status": "completed", "total_matches": 3, ... },
#     { "search_id": "...", "status": "pending", "total_matches": 0, ... }
#   ]
# }
```

---

## Recalculate Searches

Re-runs completed searches against the current Qdrant state using stored query vectors (no GPU needed, ~100ms per search).

```bash
# Recalculate up to 20 searches older than 2 hours
curl -X POST "http://localhost:8001/api/v1/recalculate/searches" \
  -H "X-User-Id: admin-user-001" \
  -H "X-User-Role: admin"

# Custom limits
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?limit=50&hours_old=1" \
  -H "X-User-Id: admin-user-001" \
  -H "X-User-Role: admin"

# Recalculate ALL completed searches (including recent ones)
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?hours_old=0&limit=100" \
  -H "X-User-Id: admin-user-001" \
  -H "X-User-Role: admin"

# Response:
# {
#   "success": true,
#   "message": "Recalculated 3 searches (2 skipped — no stored vector)",
#   "total": 3,
#   "skipped": 2
# }
```

---

## Publishing Events via Redis (for testing)

Work enters the service via Redis Streams from the GPU compute service. For manual testing:

### Publish embedding result (bypass GPU)

```bash
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"test-001","camera_id":"cam-001","embeddings":[{"image_url":"https://example.com/img.jpg","image_index":0,"vector":[0.01,0.02,...512 floats...],"total_images":1}],"input_count":1,"filtered_count":1,"embedded_count":1}'
```

### Inspect streams

```bash
# Stream lengths
docker exec embedding-redis redis-cli -n 3 XLEN embeddings:results
docker exec embedding-redis redis-cli -n 3 XLEN search:results

# Last 3 messages
docker exec embedding-redis redis-cli -n 3 XREVRANGE embeddings:results + - COUNT 3

# Consumer group info
docker exec embedding-redis redis-cli -n 3 XINFO GROUPS embeddings:results
```

---

## Test Scripts

```bash
# Full embedding pipeline test (needs compute service running)
./scripts/test_local_pipeline.sh

# Search API test (POST → GPU → matches)
./scripts/test_local_pipeline.sh --search

# Backend-only test (mock vectors, no GPU needed)
./scripts/test_local_pipeline.sh --mock

# Generate similarity heatmap from local images
python scripts/generate_similarity_report.py
```
