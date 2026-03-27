# cURL Examples (v2.0)

All examples assume the service is running at `http://localhost:8001`.

Replace `YOUR_API_KEY` with your `EMBEDDING_SERVICE_API_KEY` value.

---

## Health & Monitoring (no auth)

```bash
# Service info
curl http://localhost:8001/

# Health check (database, scheduler, triggers, consumers)
curl http://localhost:8001/health

# Pipeline stats (request counts by status + CLIP config)
curl http://localhost:8001/api/v1/stats

# Full pipeline status (counts + trigger metrics + consumer health)
curl http://localhost:8001/api/v1/pipeline/status

# Pretty-print pipeline status
curl -s http://localhost:8001/api/v1/pipeline/status | python3 -m json.tool
```

---

## Recalculate Searches (auth required)

Re-queue completed searches for reprocessing against the latest Qdrant state.

```bash
# Recalculate up to 20 searches older than 2 hours (defaults)
curl -X POST "http://localhost:8001/api/v1/recalculate/searches" \
  -H "X-API-Key: YOUR_API_KEY"

# Recalculate up to 50 searches older than 1 hour
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?limit=50&hours_old=1" \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Internal Trigger (no auth, used by ARQ worker)

Notify batch triggers from the separate ARQ worker process. You normally don't call this manually.

```bash
# Notify embedding trigger (5 new items ready)
curl -X POST "http://localhost:8001/api/v1/internal/trigger/embedding?count=5"

# Notify search trigger (1 new item)
curl -X POST "http://localhost:8001/api/v1/internal/trigger/search?count=1"
```

---

## Publishing Events to Redis Streams (redis-cli)

Work enters the service via Redis Streams, not HTTP. Use `redis-cli` to publish events.

### Publish an evidence embedding event

```bash
redis-cli -h localhost -p 6379 -n 3 XADD evidence:embed '*' \
  event_type evidence.ready.embed \
  payload '{"evidence_id":"550e8400-e29b-41d4-a716-446655440000","camera_id":"660e8400-e29b-41d4-a716-446655440000","image_urls":["https://storage.example.com/crop1.jpg","https://storage.example.com/crop2.jpg","https://storage.example.com/crop3.jpg"]}'
```

### Publish a search event

```bash
redis-cli -h localhost -p 6379 -n 3 XADD evidence:search '*' \
  event_type search.created \
  payload '{"search_id":"770e8400-e29b-41d4-a716-446655440000","user_id":"880e8400-e29b-41d4-a716-446655440000","image_url":"https://storage.example.com/query.jpg","threshold":0.75,"max_results":50,"metadata":{"camera_id":"660e8400-e29b-41d4-a716-446655440000"}}'
```

### Publish with local file URLs (development)

```bash
redis-cli -h localhost -p 6379 -n 3 XADD evidence:embed '*' \
  event_type evidence.ready.embed \
  payload '{"evidence_id":"test-001","camera_id":"cam-001","image_urls":["file:///path/to/image1.jpg","file:///path/to/image2.jpg"]}'
```

---

## Inspecting Streams (redis-cli)

```bash
# Check stream length
redis-cli -h localhost -p 6379 -n 3 XLEN evidence:embed
redis-cli -h localhost -p 6379 -n 3 XLEN evidence:search

# Read last 5 messages
redis-cli -h localhost -p 6379 -n 3 XREVRANGE evidence:embed + - COUNT 5

# Check consumer group status
redis-cli -h localhost -p 6379 -n 3 XINFO GROUPS evidence:embed

# Check pending messages (PEL)
redis-cli -h localhost -p 6379 -n 3 XPENDING evidence:embed embed-workers

# Check dead letter queue
redis-cli -h localhost -p 6379 -n 3 XLEN evidence:embed:dead
redis-cli -h localhost -p 6379 -n 3 XREVRANGE evidence:embed:dead + - COUNT 3
```

---

## E2E Test Script

The included bash script automates the full flow:

```bash
# Single evidence embedding test
./scripts/test_e2e_pipeline.sh

# Batch stress test (10 events)
./scripts/test_e2e_pipeline.sh --batch 10

# Search test
./scripts/test_e2e_pipeline.sh --search

# Show pipeline status
./scripts/test_e2e_pipeline.sh --status

# Peek last 3 stream events
./scripts/test_e2e_pipeline.sh --peek
```

---

## Integration Tests (pytest)

```bash
# Run all integration tests (CLIP, Qdrant, diversity filter, DB)
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11
pytest tests/test_integration.py -v -s

# Run a specific test class
pytest tests/test_integration.py::TestQdrantStorage -v -s

# Run a single test
pytest tests/test_integration.py::TestCLIPEmbedder::test_generate_embedding_from_file -v -s
```

---

## Similarity Report

Generate visual quality evaluation (heatmap + search visualization):

```bash
source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11
python scripts/generate_similarity_report.py
# Outputs in data/outputs/:
#   similarity_heatmap.png
#   search_results_*.png
#   report.txt
```

---

## Example Responses

### Health check

```json
{
  "status": "healthy",
  "components": {
    "database": true,
    "scheduler": true
  },
  "triggers": {
    "embedding": {"running": true, "pending_count": 0, "total_flushes": 12},
    "search": {"running": true, "pending_count": 0, "total_flushes": 5}
  },
  "consumers": {
    "embed": {"running": true, "messages_processed": 45, "messages_failed": 0},
    "search": {"running": true, "messages_processed": 18, "messages_failed": 0}
  }
}
```

### Pipeline stats

```json
{
  "service": "Image Embedding Service",
  "pipeline": {
    "embedding_requests": {"to_work": 0, "working": 0, "embedded": 35, "done": 120, "error": 3},
    "search_requests": {"to_work": 0, "working": 0, "completed": 18, "error": 1}
  },
  "configuration": {"model": "ViT-B-32", "device": "cuda", "vector_size": 512}
}
```

### Recalculation

```json
{
  "success": true,
  "message": "Queued 5 searches for recalculation",
  "total": 5
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
