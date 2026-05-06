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

### Search with weapons filter

Four filter modes available: `all` (default), `only`, `exclude`, `analyzed_clean`. See [../weapons/04_SEARCH_API.md](../weapons/04_SEARCH_API.md) for the full design.

```bash
# Only images with at least one weapon detected
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "only"
  }'

# Narrow by class — handguns only
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "only",
    "weapon_classes": ["handgun"]
  }'

# Exclude weapon images (broad safe search — includes unanalyzed too)
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "exclude"
  }'

# False-positive review queue — images analyzed by the weapons service
# that came out clean (candidates for human reclassification)
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: admin-001" \
  -H "X-User-Role: admin" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "analyzed_clean"
  }'
```

### List available categories (for the frontend filter dropdown)

Categories are upstream taxonomy entity ids (sourced from `t_configurations.entities`). The endpoint returns the distinct ids that exist for the tenant plus human-readable labels — frontend uses this to populate the dropdown rather than hardcoding ids on its side.

```bash
curl http://localhost:8001/api/v1/search/categories \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Response:
# {
#   "categories": [
#     { "id": 0, "label": "person" },
#     { "id": 2, "label": "car" },
#     { "id": 5, "label": "bus" }
#   ]
# }
```

Empty result (`{"categories": []}`) is normal until evidence with non-null `entities` has been ingested. See [API_REFERENCE.md](API_REFERENCE.md#get-apiv1searchcategories--list-available-categories).

### Search narrowed by category

Category values are stringified entity ids from the upstream taxonomy (e.g. `"2"` for car). Pass a scalar for exact match or a list for `MatchAny` across multiple ids. The frontend should call `GET /api/v1/search/categories` first and let the user pick from the labeled dropdown — the chosen item's `id` (as a string) goes into the `category` field on this request. See [../requirements/IMAGE_COMPUTE_STREAMS.md](../requirements/IMAGE_COMPUTE_STREAMS.md) §2 for the upstream contract.

```bash
# Single id — only car evidence (id 2)
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "category": "2"
  }'

# Multiple ids — matches from any (cars id=2 OR buses id=5)
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "category": ["2", "5"]
  }'

# Category combined with weapons filter — weapon-bearing cars only
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "only",
    "category": "2"
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

The payload must include all ETL metadata fields (`user_id`, `device_id`, `app_id`, `infraction_code`, `zip_url`) and `embeddings[].image_name` — the backend will download the ZIP, extract the named frames, upload them to the storage service, and produce permanent URLs.

```bash
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"test-001","camera_id":"cam-001","user_id":"user-001","device_id":"dev-001","app_id":1,"infraction_code":"TEST-001","zip_url":"https://minio.lookia.mx/lucam-assets/test.zip","embeddings":[{"image_name":"frame_001.jpg","image_index":0,"vector":[0.01,0.02,...512 floats...]}],"input_count":1,"filtered_count":1,"embedded_count":1}'
```

### Publish enriched embedding result (with weapon_analysis)

```bash
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"weapon-test-001","camera_id":"cam-001","user_id":"user-001","device_id":"dev-001","app_id":1,"infraction_code":"WEAPON-TEST","zip_url":"https://minio.lookia.mx/lucam-assets/test.zip","embeddings":[{"image_name":"frame_001.jpg","image_index":0,"vector":[0.01,0.02,"...512 floats..."]}],"input_count":1,"filtered_count":1,"embedded_count":1,"weapon_analysis":{"images":[{"image_name":"frame_001.jpg","image_index":0,"detections":[{"class_name":"handgun","class_id":0,"confidence":0.9,"bbox":{"x1":10,"y1":10,"x2":100,"y2":100}}]}],"summary":{"images_analyzed":1,"images_with_detections":1,"total_detections":1,"classes_detected":["handgun"],"max_confidence":0.9,"has_weapon":true}}}'
```

### Publish compute error (bypass GPU)

```bash
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type compute.error \
  payload '{"entity_id":"test-001","entity_type":"evidence","error":"No images downloadable"}'
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

## Manage blacklist entries

The blacklist API is documented in full at [BLACKLIST_API.md](BLACKLIST_API.md). This section is a quick reference of the most common workflows.

### Create an entry

```bash
curl -X POST http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Suspect plate — case 2026-042",
    "category": "vehicle",
    "description": "Vehicle linked to open investigation",
    "match_threshold": 0.88
  }'
# → 201 Created with id, status=1 (CREATED), blacklist_version=1
```

### List entries (regular user)

```bash
# Default — only active entries owned by this tenant
curl http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Include inactive entries
curl "http://localhost:8001/api/v1/blacklist/image-entries?active=all" \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"

# Filter by category
curl "http://localhost:8001/api/v1/blacklist/image-entries?category=vehicle" \
  -H "X-User-Id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "X-User-Role: user"
```

### List entries (admin — across tenants)

```bash
# All entries, all tenants
curl http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: admin-uuid" \
  -H "X-User-Role: admin"

# Scope to a specific tenant
curl "http://localhost:8001/api/v1/blacklist/image-entries?user_id=550e8400-..." \
  -H "X-User-Id: admin-uuid" \
  -H "X-User-Role: admin"
```

### Update threshold (bumps version → fresh reports)

```bash
curl -X PATCH http://localhost:8001/api/v1/blacklist/image-entries/<ENTRY_ID> \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"match_threshold": 0.92}'
# → blacklist_version increments; receiver dedup key (evidence_id, entry_id, version) sees a new bucket
```

### Attach a reference image (triggers async embed)

```bash
curl -X POST http://localhost:8001/api/v1/blacklist/image-entries/<ENTRY_ID>/references \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://minio.lookia.mx/uploads/ref.jpg"}'
# → 202 Accepted with status=1 (TO_PROCESS)
# Poll GET /image-entries/<ENTRY_ID> until reference status=3 (PROCESSED)
```

### Delete a reference

```bash
curl -X DELETE http://localhost:8001/api/v1/blacklist/image-entries/<ENTRY_ID>/references/<REF_ID> \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user"
# → 204 No Content
```

### Trigger a backfill (re-run reverse search for all references)

Use after a threshold change, or as ops recovery if the natural reverse-search job was lost (backend restart mid-job).

```bash
curl -X POST http://localhost:8001/api/v1/blacklist/image-entries/<ENTRY_ID>/backfill \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user"
# → 202 Accepted with the list of scheduled APScheduler job_ids
```

### Delete an entry (hard-delete + Qdrant cleanup)

```bash
curl -X DELETE http://localhost:8001/api/v1/blacklist/image-entries/<ENTRY_ID> \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user"
# → 204 No Content
# SQL cascades references + embeddings; Qdrant points cleaned up best-effort
```

### Inspect blacklist match events on the stream

```bash
# Count events
docker exec embedding-redis redis-cli -n 3 XLEN image:blacklist_match

# Last 3 events with full payload
docker exec embedding-redis redis-cli -n 3 XREVRANGE image:blacklist_match + - COUNT 3
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
