# Phase 5: Documentation Updates + End-to-End Verification

Two deliverables for this phase: update the existing user-facing docs to reflect the weapons enrichment, and run the full verification plan from database up through search API.

## Part A — Documentation updates

### 1. `docs/new_arq_v2/04_STREAM_CONTRACTS.md`

Add an optional `weapon_analysis` block to the `embeddings:results` Success payload section (already updated for the ZIP contract in a previous pass). The addition should make clear the field is **optional** and backwards compatible — legacy producers that omit it continue to work.

Suggested insertion, immediately after the existing `embedded_count` field in the Success JSON example:

````markdown
Optionally, if the message was routed through the `compute-weapons` service, the payload includes a `weapon_analysis` block:

```json
{
  "weapon_analysis": {
    "images": [
      {
        "image_name": "20260410-100150_023389.jpg",
        "image_index": 0,
        "detections": [
          {
            "class_name": "handgun",
            "class_id": 0,
            "confidence": 0.873,
            "bbox": { "x1": 412, "y1": 188, "x2": 596, "y2": 402 }
          }
        ]
      }
    ],
    "summary": {
      "images_analyzed": 9,
      "images_with_detections": 2,
      "total_detections": 3,
      "classes_detected": ["handgun", "knife"],
      "max_confidence": 0.873,
      "has_weapon": true
    }
  }
}
```

This block is optional. When absent, the backend processes the message exactly as the legacy path. See [docs/weapons/](../weapons/) for the full enrichment contract and storage design.
````

### 2. `docs/API_REFERENCE.md`

Two updates:

**(a) `POST /api/v1/search` — new optional body fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `weapons_filter` | `"all"` \| `"only"` \| `"exclude"` \| `"analyzed_clean"` | `"all"` | Filter matches by weapon presence |
| `weapon_classes` | `list[str]` | `null` | Subset of classes to match (only meaningful when `weapons_filter="only"`) |

Add a short "Weapons filtering" subsection with the four-mode table from [04_SEARCH_API.md](04_SEARCH_API.md).

**(b) "Qdrant Collections" section — extend the payload indices list for `evidence_embeddings`:**

Change from:
> Payload indices on `evidence_id`, `camera_id`, `source_type`, `user_id`, `device_id`, `app_id` (multi-tenant filtering)

To:
> Payload indices on `evidence_id`, `camera_id`, `source_type`, `user_id`, `device_id`, `app_id` (multi-tenant filtering), plus `weapon_analyzed`, `has_weapon`, `weapon_classes` (weapons filtering — see docs/weapons/)

**(c) "Database Tables" section — call out the new weapons columns:**

Add a sentence to the `embedding_requests` and `evidence_embeddings` rows noting the weapons columns, or add a dedicated "Weapons enrichment" table section that points to `docs/weapons/01_DATABASE.md`.

### 3. `docs/CURL_EXAMPLES.md`

Two additions:

**(a) Search with weapons filter** — new section right after the existing search examples:

```bash
# Search constrained to images with any weapon detected
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-..." -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "only"
  }'

# Search constrained to images with a handgun specifically
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: 550e8400-..." -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "only",
    "weapon_classes": ["handgun"]
  }'

# False-positive review queue — images analyzed and came out clean
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: admin-001" -H "X-User-Role: admin" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "weapons_filter": "analyzed_clean"
  }'
```

**(b) XADD a synthetic enriched `embeddings.computed` message** — extend the existing XADD example right after the current one:

```bash
# Publish an enriched result (with weapon_analysis block) — for testing Phase 3
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"weapon-test-001","camera_id":"cam-001","user_id":"user-001","device_id":"dev-001","app_id":1,"infraction_code":"WEAPON-TEST","zip_url":"https://minio.lookia.mx/lucam-assets/test.zip","embeddings":[{"image_name":"frame_001.jpg","image_index":0,"vector":[0.01,0.02,"...512 floats..."]}],"input_count":1,"filtered_count":1,"embedded_count":1,"weapon_analysis":{"images":[{"image_name":"frame_001.jpg","image_index":0,"detections":[{"class_name":"handgun","class_id":0,"confidence":0.9,"bbox":{"x1":10,"y1":10,"x2":100,"y2":100}}]}],"summary":{"images_analyzed":1,"images_with_detections":1,"total_detections":1,"classes_detected":["handgun"],"max_confidence":0.9,"has_weapon":true}}}'
```

### 4. `README.md`

Add one sentence to the "Data Flow" section immediately after step 2, and note the optional nature:

> 2.5. **compute-weapons (optional)** If routing sends the evidence through the weapons detection service, it enriches the `embeddings:results` message with a `weapon_analysis` block (per-image bboxes + evidence-level summary). Legacy messages without this block are processed unchanged.

## Part B — End-to-end verification

Execute after each phase deploys. Order matters — later tests depend on earlier phases being live.

### Prerequisites

- Backend running against a dev PostgreSQL + Qdrant + Redis
- Docker containers: `make docker-up`
- Run from the conda env: `source ~/anaconda3/etc/profile.d/conda.sh && conda activate image_compute_backend_p11`

### Test 1 — Database migration (Phase 1)

```bash
make migrate

# Assert new columns exist
psql -h localhost -p 5433 -U embed_user -d embedding_service -c "\d embedding_requests" | grep -E 'weapon_(analyzed|has_weapon|classes|max_confidence|summary)'
psql -h localhost -p 5433 -U embed_user -d embedding_service -c "\d evidence_embeddings" | grep weapon_detections

# Assert existing rows got backfilled
psql -h localhost -p 5433 -U embed_user -d embedding_service -c "SELECT COUNT(*) FROM embedding_requests WHERE weapon_analyzed = false;"
```

Expected: output shows all the new columns with correct types. Backfill count equals total row count.

Downgrade sanity check (throwaway DB only):
```bash
alembic downgrade -1
alembic upgrade head
```

### Test 2 — Qdrant payload indices (Phase 2)

```bash
# Restart backend so the new initialize() path runs
make run-api  # in terminal A

# Inspect the collection
curl -s http://localhost:6333/collections/evidence_embeddings | python3 -m json.tool > /tmp/col.json
grep -E '"(weapon_analyzed|has_weapon|weapon_classes)"' /tmp/col.json
```

Expected: all three fields present in `payload_schema`.

Smoke test the MatchAny path with a dummy filter (no results expected yet, but the filter must parse without error):

```bash
curl -sX POST http://localhost:6333/collections/evidence_embeddings/points/search \
  -H "Content-Type: application/json" \
  -d '{
    "vector": [0.0]...512...,
    "limit": 1,
    "filter": {"must": [{"key": "weapon_classes", "match": {"any": ["handgun"]}}]}
  }' | python3 -m json.tool
```

Expected: HTTP 200, empty `result: []`. A 400 or "unknown field" error means the index is missing.

### Test 3 — Consumer enrichment (Phase 3)

#### 3a. Enriched message → weapon columns populated

```bash
# Publish the synthetic enriched message from CURL_EXAMPLES.md
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '<enriched JSON from Phase 5 Part A section 3b>'

# Give the consumer a moment to process
sleep 3

# Assert DB state
psql -h localhost -p 5433 -U embed_user -d embedding_service <<'SQL'
SELECT evidence_id, weapon_analyzed, has_weapon, weapon_classes, weapon_max_confidence
FROM embedding_requests
WHERE evidence_id = 'weapon-test-001';

SELECT qdrant_point_id, weapon_detections
FROM evidence_embeddings
WHERE request_id = (SELECT id FROM embedding_requests WHERE evidence_id = 'weapon-test-001');
SQL
```

Expected:
- `embedding_requests`: `weapon_analyzed=t`, `has_weapon=t`, `weapon_classes=["handgun"]`, `weapon_max_confidence=0.9`
- `evidence_embeddings.weapon_detections` is non-null and contains the bbox

#### 3b. Qdrant point has new payload fields

```bash
# Get the qdrant_point_id from the DB query above, then:
POINT_ID=<from above>
curl -s "http://localhost:6333/collections/evidence_embeddings/points/$POINT_ID" | python3 -m json.tool
```

Expected: `payload` contains `weapon_analyzed: true`, `has_weapon: true`, `weapon_classes: ["handgun"]`.

#### 3c. Legacy message (no weapon_analysis) → all flags false

```bash
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"legacy-test-001","camera_id":"cam-001","user_id":"user-001","device_id":"dev-001","app_id":1,"infraction_code":"LEGACY-TEST","zip_url":"https://minio.lookia.mx/lucam-assets/test.zip","embeddings":[{"image_name":"frame_001.jpg","image_index":0,"vector":[0.01,"...512 floats..."]}],"input_count":1,"filtered_count":1,"embedded_count":1}'

sleep 3

psql -h localhost -p 5433 -U embed_user -d embedding_service <<'SQL'
SELECT weapon_analyzed, has_weapon, weapon_classes, weapon_summary
FROM embedding_requests
WHERE evidence_id = 'legacy-test-001';

SELECT weapon_detections
FROM evidence_embeddings
WHERE request_id = (SELECT id FROM embedding_requests WHERE evidence_id = 'legacy-test-001');
SQL
```

Expected:
- `weapon_analyzed=f`, `has_weapon=f`, `weapon_classes=[]`, `weapon_summary=NULL`
- `weapon_detections` is NULL
- Qdrant point still created normally; payload has `weapon_analyzed=false, has_weapon=false, weapon_classes=[]`

#### 3d. Prepare a third record: "analyzed but clean" for Test 4

```bash
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"clean-test-001","camera_id":"cam-001","user_id":"user-001","device_id":"dev-001","app_id":1,"infraction_code":"CLEAN-TEST","zip_url":"https://minio.lookia.mx/lucam-assets/test.zip","embeddings":[{"image_name":"frame_clean.jpg","image_index":0,"vector":[0.01,"...512 floats..."]}],"input_count":1,"filtered_count":1,"embedded_count":1,"weapon_analysis":{"images":[{"image_name":"frame_clean.jpg","image_index":0,"detections":[]}],"summary":{"images_analyzed":1,"images_with_detections":0,"total_detections":0,"classes_detected":[],"max_confidence":null,"has_weapon":false}}}'
```

Expected:
- `weapon_analyzed=t, has_weapon=f, weapon_classes=[]`
- Qdrant payload: `weapon_analyzed=true, has_weapon=false, weapon_classes=[]`
- This is the **false-positive review queue** candidate

### Test 4 — Search API filter modes (Phase 4)

Using the three test records from Test 3 (weapon-test-001, legacy-test-001, clean-test-001), submit searches and verify results. Use the query vector of one of them so all three are similar enough to appear in the result set without a filter.

#### 4a. `weapons_filter="all"` — baseline

```bash
curl -sX POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: user-001" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"file:///path/to/query.jpg","threshold":0.3,"weapons_filter":"all"}' | python3 -m json.tool

# Poll for completion
sleep 3
SEARCH_ID=<from response>
curl -s "http://localhost:8001/api/v1/search/$SEARCH_ID/matches" -H "X-User-Id: user-001" -H "X-User-Role: user" | python3 -m json.tool
```

Expected: all three test records (or at least the similar ones) appear.

#### 4b. `weapons_filter="only"` → only weapon-test-001

Repeat the POST with `"weapons_filter":"only"`. Expected: only `weapon-test-001` in matches.

#### 4c. `weapons_filter="only", weapon_classes=["knife"]` → empty

Expected: no matches (only handgun was stored, not knife).

#### 4d. `weapons_filter="only", weapon_classes=["handgun"]` → weapon-test-001

Expected: only weapon-test-001.

#### 4e. `weapons_filter="exclude"` → legacy-test-001 + clean-test-001

Expected: both the unanalyzed record and the analyzed-clean record return.

#### 4f. `weapons_filter="analyzed_clean"` → only clean-test-001

Expected: only the clean-test-001 record (legacy is excluded because `weapon_analyzed=false`).

### Test 5 — Recalculation honors filters (Phase 4)

```bash
# Create a search with a filter
curl -sX POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: user-001" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"file:///path/to/query.jpg","threshold":0.3,"weapons_filter":"only","weapon_classes":["handgun"]}'

# Wait for it to complete
sleep 5

# Add a new weapon-positive record that should match
# (XADD another enriched message with similar vector and a handgun detection)

# Trigger recalculation
curl -sX POST "http://localhost:8001/api/v1/recalculate/searches?hours_old=0&limit=10" \
  -H "X-User-Id: admin-001" -H "X-User-Role: admin"

# Assert the recalculated matches still honor the weapons filter
curl -s "http://localhost:8001/api/v1/search/$SEARCH_ID/matches" -H "X-User-Id: user-001" -H "X-User-Role: user"
```

Expected: the new weapon-positive record is included in the recalculated results; any non-weapon records are excluded.

### Test 6 — Production stream rollout observability

```bash
rcli() { /opt/homebrew/bin/redis-cli -h 31.220.104.212 -p 6379 -a videoserver_redis_password -n 3 --no-auth-warning "$@"; }

# Fetch last 200 messages and count how many have weapon_analysis
rcli XREVRANGE embeddings:results + - COUNT 200 | \
  python3 -c "
import sys, json
lines = sys.stdin.read().splitlines()
with_w = without_w = 0
for i, ln in enumerate(lines):
    if ln == 'payload' and i+1 < len(lines):
        try:
            d = json.loads(lines[i+1])
            if 'weapon_analysis' in d:
                with_w += 1
            else:
                without_w += 1
        except Exception:
            pass
print(f'with weapon_analysis: {with_w}')
print(f'without: {without_w}')
print(f'coverage: {with_w / (with_w+without_w) * 100:.1f}%' if (with_w+without_w) else 'no samples')
"
```

Expected once compute-weapons is fully rolled out: the majority (ideally all) of recent messages carry `weapon_analysis`. Before rollout: this stays at 0% and the legacy path handles everything.

This is a production-safe observability check — pure read-only `XREVRANGE`.

## Success criteria (all phases green)

- [ ] Migration applied; existing rows backfilled to `weapon_analyzed=false`
- [ ] Qdrant collection shows three new payload indices on `evidence_embeddings`
- [ ] Consumer writes weapon fields when present; writes nothing when absent
- [ ] Qdrant points carry the correct per-image flags
- [ ] All four `weapons_filter` modes return the expected match sets
- [ ] `weapon_classes` subset filter reduces results correctly
- [ ] Recalculation honors stored filters
- [ ] Docs updated: `04_STREAM_CONTRACTS.md`, `API_REFERENCE.md`, `CURL_EXAMPLES.md`, `README.md`
- [ ] Live stream inspection shows enrichment coverage (once compute-weapons is rolled out)

Only after every checkbox is green does the weapons-enrichment work ship.
