# Phase 07: Documentation Updates + End-to-End Verification

Ships the user-facing docs and a manual verification checklist that exercises the full flow from Phase 01 (category) through Phase 06 (CRUD API). Run after all prior phases deploy.

## Part A — Documentation updates

### 1. `docs/new_arq_v2/04_STREAM_CONTRACTS.md`

**Already partially touched in Phase 01** (new optional `category` field on `embeddings:results`). This phase confirms the doc reflects all blacklist-adjacent stream additions:

- **`embeddings:results` — optional `category` field** documented (from Phase 01).
- **`evidence:search` — optional `purpose` + `blacklist_entry_id` fields** documented. Existing contract gets a new sub-section:
  > **Blacklist embed request.** When `purpose="blacklist_embed"`, the payload represents a reference image that should be embedded and returned via `search:results` for backend-side storage as a blacklist point. `search_id` in that case carries the `BlacklistImageReference.id` as an opaque echo value. The GPU service treats these messages identically to `purpose="search"` (just embed the image and publish the vector); the dispatch happens in the backend consumer.
- **`search:results` — optional `purpose` field preservation** documented. GPU passes through whatever `purpose` value it received.
- **New stream `image:blacklist_match`** pointed at [docs/requirements/REPORT_GENERATION_STREAMS.md §3](../requirements/REPORT_GENERATION_STREAMS.md) (updated in Phase 05 from placeholder to real contract).

### 2. `docs/API_REFERENCE.md`

New top-level section "Blacklist (Image)" with the six endpoints from Phase 06. Cross-linked from the existing "Search" section because they share the gateway-header auth pattern.

Updates to the existing search section:

- `POST /api/v1/search` request body: add `category` field (Phase 01).
- "Weapons filtering modes" table: unchanged.
- Add a "Category filtering" subsection:
  > Narrow results to a specific evidence category. Accepts a single string or a list. List values use `MatchAny` — any of the requested categories must match. Example: `"category": ["vehicle", "scene"]` returns matches from either category.

Qdrant payload indices list gets `category` and `blacklist_entry_id` added.

### 3. `docs/CURL_EXAMPLES.md`

New section "Manage blacklist entries" with curl examples for:

- Create an entry
- List entries (user and admin variants)
- Patch an entry to change threshold
- Add a reference image
- Delete a reference
- Trigger a backfill
- Delete an entry

Also a "Search with category filter" subsection demonstrating the new field from Phase 01.

### 4. `docs/requirements/REPORT_GENERATION_STREAMS.md`

**Already updated in Phase 05** — §3 promoted from placeholder to real contract. This phase confirms the update is in place and cross-checks that the DTO in §3 matches what the producer code actually emits.

### 5. `README.md`

One paragraph added to the "Data Flow" section noting the blacklist path:

> **Blacklist (optional, per user).** Users can register reference images via `POST /api/v1/blacklist/image-entries`. New evidence is auto-matched against the user's active blacklist entries; matches fire `image:blacklist_match` events for the report-generation service. New blacklist entries also trigger a reverse-search against historical evidence (async). See [docs/image-blacklist/](docs/image-blacklist/).

### 6. `docs/image-blacklist/README.md`

Update the phase index checkboxes to "completed" after implementation. Adds any lessons learned or deviations from the plan as a post-mortem section.

## Part B — End-to-end verification

Run in order. Each step depends on the prior one. All phases (01–06) must be deployed.

### Prerequisites

- `make docker-up` — Postgres, Redis, Qdrant up and healthy
- `make migrate` — all migrations applied; `alembic current` shows Phase 02 + 01 revisions
- Backend running against these services
- GPU compute service running (handles `evidence:search` and publishes `search:results`)
- Two test users: `user-A` (regular) and `admin-A` (admin role)
- Conda env: `source ~/anaconda3/etc/profile.d/conda.sh && conda activate image_compute_backend_p11`

### Test 1 — Category infrastructure (Phase 01)

```bash
# 1a. Post an enriched embeddings:results message with category="vehicle"
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"cat-test-001","camera_id":"cam-1","user_id":"user-A",...,"category":"vehicle","embeddings":[{...}]}'

sleep 3

# 1b. Verify DB
psql ... -c "SELECT category FROM embedding_requests WHERE evidence_id='cat-test-001';"
# Expected: vehicle

# 1c. Verify Qdrant payload
POINT_ID=$(psql -t ... -c "SELECT qdrant_point_id FROM evidence_embeddings WHERE request_id = (SELECT id FROM embedding_requests WHERE evidence_id='cat-test-001') LIMIT 1;")
curl -s "http://localhost:6333/collections/evidence_embeddings/points/$POINT_ID" | python3 -m json.tool | grep category
# Expected: "category": "vehicle"

# 1d. Category-filtered search
curl -sX POST http://localhost:8001/api/v1/search -H "X-User-Id: user-A" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"file:///...","threshold":0.3,"category":"vehicle"}'
# Expected: only vehicle-category matches returned
```

### Test 2 — Blacklist CRUD (Phase 02 + Phase 06)

```bash
# 2a. Create an entry
curl -sX POST http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: user-A" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"name":"test entry","category":"vehicle","match_threshold":0.88}'
# Expected: 201, status=1 (CREATED), active=true, blacklist_version=1

ENTRY_ID=<from response>

# 2b. Verify multi-tenant isolation
curl -s http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: user-B" -H "X-User-Role: user"
# Expected: empty list (user-B can't see user-A's entry)

curl -s http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: admin-A" -H "X-User-Role: admin"
# Expected: entry appears (admin sees all)

# 2c. PATCH with name change — no version bump
curl -sX PATCH "http://localhost:8001/api/v1/blacklist/image-entries/$ENTRY_ID" \
  -H "X-User-Id: user-A" -H "X-User-Role: user" \
  -H "Content-Type: application/json" -d '{"name":"renamed entry"}'
psql -t ... -c "SELECT blacklist_version FROM blacklist_image_entries WHERE id='$ENTRY_ID';"
# Expected: 1 (unchanged)

# 2d. PATCH threshold — version bumps
curl -sX PATCH "http://localhost:8001/api/v1/blacklist/image-entries/$ENTRY_ID" \
  -H "X-User-Id: user-A" -H "X-User-Role: user" \
  -H "Content-Type: application/json" -d '{"match_threshold":0.92}'
psql -t ... -c "SELECT blacklist_version FROM blacklist_image_entries WHERE id='$ENTRY_ID';"
# Expected: 2 (bumped)
```

### Test 3 — Reference image embedding (Phase 04)

```bash
# 3a. Add a reference (uses an image already in MinIO — could be any CLIP-embeddable URL)
curl -sX POST "http://localhost:8001/api/v1/blacklist/image-entries/$ENTRY_ID/references" \
  -H "X-User-Id: user-A" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"https://minio.lookia.mx/lucam-assets/sample/blacklist_ref.jpg"}'
# Expected: 202, status=1 (TO_PROCESS)

REF_ID=<from response>

# 3b. Verify evidence:search XADD
docker exec embedding-redis redis-cli -n 3 XREVRANGE evidence:search + - COUNT 1
# Expected: one message with purpose="blacklist_embed", search_id=<REF_ID>, blacklist_entry_id=<ENTRY_ID>

# 3c. Wait for GPU to process + consumer to store
sleep 10

# 3d. Verify DB state
psql ... -c "SELECT status FROM blacklist_image_references WHERE id='$REF_ID';"
# Expected: 3 (PROCESSED)

psql ... -c "SELECT qdrant_point_id FROM blacklist_image_embeddings WHERE reference_id='$REF_ID';"
# Expected: one row with a qdrant_point_id

# 3e. Verify Qdrant payload
POINT_ID=<from 3d>
curl -s "http://localhost:6333/collections/evidence_embeddings/points/$POINT_ID" | python3 -m json.tool | grep source_type
# Expected: "source_type": "blacklist"

curl -s "http://localhost:6333/collections/evidence_embeddings/points/$POINT_ID" | python3 -m json.tool | grep blacklist_entry_id
# Expected: "blacklist_entry_id": "<ENTRY_ID>"

# 3f. Entry status auto-advances
curl -s "http://localhost:8001/api/v1/blacklist/image-entries/$ENTRY_ID" \
  -H "X-User-Id: user-A" -H "X-User-Role: user"
# Expected: status=3 (INDEXED)
```

### Test 4 — Reverse search fires on new reference (Phase 04 + Phase 05)

```bash
# Prerequisites: user-A already has several evidence entries ingested (can be from Test 1 data).

# 4a. Check the APScheduler registered the reverse-search job
# This is a log-only verification; the job runs instantly then disappears.
# Look in the backend log for:
# INFO  Reverse search complete: entry=<ENTRY_ID> ref=<REF_ID> matches=N

# 4b. If N > 0, verify image:blacklist_match events fired
docker exec embedding-redis redis-cli -n 3 XLEN image:blacklist_match
# Expected: N (or more, if inline matches also fired in Test 5)

docker exec embedding-redis redis-cli -n 3 XREVRANGE image:blacklist_match + - COUNT 1
# Expected: one event matching docs/requirements/REPORT_GENERATION_STREAMS.md §3 shape
# trigger="reverse_search"
```

### Test 5 — Inline match on new evidence (Phase 05)

```bash
# 5a. Ingest a new evidence that's visually similar to the blacklist reference
docker exec embedding-redis redis-cli -n 3 XADD embeddings:results '*' \
  event_type embeddings.computed \
  payload '{"evidence_id":"inline-match-test-001","user_id":"user-A",...,"embeddings":[...similar vectors to blacklist ref...]}'

sleep 5

# 5b. Check inline match fired
docker exec embedding-redis redis-cli -n 3 XREVRANGE image:blacklist_match + - COUNT 1
# Expected: one event with trigger="inline", evidence_id="inline-match-test-001"

# 5c. Observe in backend log
# INFO  Published image.blacklist_match: evidence=inline-match-test-001 entry=<ENTRY_ID> score=0.XX trigger=inline
```

### Test 6 — Strict source_type filtering (Phase 03)

This is a correctness check — verify blacklist points do NOT leak into user-facing searches.

```bash
# 6a. Run a user-facing search
curl -sX POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: user-A" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"file:///...","threshold":0.3}'
SEARCH_ID=<from response>

sleep 5

# 6b. Fetch the matches
curl -s "http://localhost:8001/api/v1/search/$SEARCH_ID/matches" \
  -H "X-User-Id: user-A" -H "X-User-Role: user" | python3 -m json.tool

# 6c. Manually verify: none of the matches' qdrant_point_ids should appear
# in blacklist_image_embeddings
psql -t ... -c "SELECT qdrant_point_id FROM blacklist_image_embeddings;" > /tmp/blacklist_points.txt

# Extract match point IDs from the response (via jq or manual inspection) and grep
# against /tmp/blacklist_points.txt — expected: zero overlap.
```

**This test is the most important one.** A failure here means blacklist points are leaking into user search results — multi-tenant and UX regression. Must pass.

### Test 7 — Delete entry cleans up Qdrant

```bash
# 7a. Record the current Qdrant point IDs for the entry
psql -t ... -c "SELECT qdrant_point_id FROM blacklist_image_embeddings WHERE entry_id='$ENTRY_ID';" > /tmp/entry_points.txt

# 7b. Delete the entry
curl -sX DELETE "http://localhost:8001/api/v1/blacklist/image-entries/$ENTRY_ID" \
  -H "X-User-Id: user-A" -H "X-User-Role: user"
# Expected: 204 No Content

# 7c. Verify SQL rows gone
psql ... -c "SELECT COUNT(*) FROM blacklist_image_entries WHERE id='$ENTRY_ID';"
# Expected: 0

# 7d. Verify Qdrant points gone
for POINT_ID in $(cat /tmp/entry_points.txt); do
  curl -s "http://localhost:6333/collections/evidence_embeddings/points/$POINT_ID"
  # Expected: 404 or "not found" for each
done
```

### Test 8 — Backfill endpoint (Phase 06)

```bash
# 8a. Create a fresh entry + reference (earlier ones were deleted)
curl -sX POST http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: admin-A" -H "X-User-Role: admin" \
  -H "Content-Type: application/json" \
  -d '{"name":"backfill test"}'
NEW_ENTRY_ID=<from response>

# ... attach a reference ...

# Wait for the natural reverse search to complete

# 8b. Trigger manual backfill
curl -sX POST "http://localhost:8001/api/v1/blacklist/image-entries/$NEW_ENTRY_ID/backfill" \
  -H "X-User-Id: admin-A" -H "X-User-Role: admin"
# Expected: 202 Accepted with job_id

# 8c. Second backfill while first runs → 409
curl -sX POST "http://localhost:8001/api/v1/blacklist/image-entries/$NEW_ENTRY_ID/backfill" \
  -H "X-User-Id: admin-A" -H "X-User-Role: admin"
# Expected: 409 Conflict, "Backfill already running"
```

### Test 9 — Stream observability (production)

Against the remote dev Redis:

```bash
rcli() { /opt/homebrew/bin/redis-cli -h 31.220.104.212 -p 6379 -a videoserver_redis_password -n 3 --no-auth-warning "$@"; }

# Count image:blacklist_match events
rcli XLEN image:blacklist_match

# Inspect a recent event
rcli XREVRANGE image:blacklist_match + - COUNT 1

# Consumer group lag (once report-generation is consuming)
rcli XINFO GROUPS image:blacklist_match
```

Once `image:blacklist_match` has consumers and the consumer-group lag stays near 0, the feature is healthy.

## Success criteria

All of these must be checked before declaring v1 shipped:

- [ ] Phase 01 — Category field flows end-to-end; search filter works for scalar and list values
- [ ] Phase 02 — Three blacklist tables exist; unique constraint enforced; cascade delete works
- [ ] Phase 03 — `source_type` + `blacklist_entry_id` indices present in Qdrant; strict filter helper passes unit tests; user-facing search never returns blacklist points
- [ ] Phase 04 — Reference image embedding round-trips via `evidence:search` with `purpose="blacklist_embed"`; reverse-search APScheduler job fires
- [ ] Phase 05 — `image:blacklist_match` events match the §3 DTO contract; both `trigger="inline"` and `trigger="reverse_search"` paths produce events
- [ ] Phase 06 — All 9 CRUD endpoints respond correctly; multi-tenant isolation enforced; delete cleans Qdrant; backfill endpoint schedules jobs
- [ ] Stream contract docs + API docs + README updated
- [ ] report-generation team has consumed at least one `image:blacklist_match` event successfully

Only when every checkbox is green does v1 ship to production.

## Post-shipping monitoring

- **SQL report — active blacklist adoption:** `SELECT user_id, COUNT(*) FROM blacklist_image_entries WHERE active = true GROUP BY 1 ORDER BY 2 DESC;` — tracks which tenants are actually using the feature.
- **Match rate by category:** count of `image:blacklist_match` events per `blacklist_entry_category` per day — shows which categories are triggering alerts.
- **False-positive feedback loop:** if ops reports "too many spurious matches for category X", bump `BLACKLIST_MATCH_THRESHOLD` or move that category to a stricter per-entry threshold (when the override is wired in a future phase).
- **Stuck references:** `SELECT * FROM blacklist_image_references WHERE status IN (1, 2) AND updated_at < NOW() - INTERVAL '1 hour';` — catches references that got stuck in TO_PROCESS or PROCESSING. Expected to be empty in healthy operation.

## Not in scope, but plausible follow-ups

- **Per-category threshold overrides** (hook described in Phase 04).
- **Admin `PATCH /api/v1/evidence/{id}` to assign category post-ingest** (hook described in Phase 01).
- **Storing matches in a `blacklist_image_matches` table** on our side for richer reporting (discussed in Phase 06 §GET matches).
- **Bulk entry import** (CSV/JSON upload).
- **Blacklist entry archival UI** (soft-delete via `active=false` is already supported; the UI just needs to expose it).
