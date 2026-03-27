# Implementation TODO

Track progress for each step. Mark `[x]` when complete.

---

## Phase 1: Foundation

### Step 1 — PostgreSQL + SQLAlchemy + Alembic ([01_DATABASE.md](01_DATABASE.md))
- [x] Add `SQLAlchemy`, `asyncpg`, `alembic`, `psycopg2-binary`, `greenlet` to requirements.txt
- [x] Add PostgreSQL to docker-compose.yml
- [x] Create `src/infrastructure/database.py` (engine, session factory, get_session)
- [x] Create `src/db/base.py` (DeclarativeBase)
- [x] Run `alembic init alembic`
- [x] Configure `alembic/env.py` (async URL swap, import models)
- [x] Add `DATABASE_URL` to config.py and .env
- [x] Verify connection works

### Step 2 — Database Models ([02_MODELS.md](02_MODELS.md))
- [x] Create `src/db/models/constants.py` (status enums)
- [x] Create `src/db/models/embedding_request.py`
- [x] Create `src/db/models/evidence_embedding.py`
- [x] Create `src/db/models/search_request.py`
- [x] Create `src/db/repositories/embedding_request_repo.py` (with FOR UPDATE SKIP LOCKED)
- [x] Create `src/db/repositories/search_request_repo.py`
- [x] Generate and run first Alembic migration
- [x] Verify tables created in PostgreSQL

---

## Phase 2: Event-Driven Input

### Step 3 — Redis Streams Consumer ([03_STREAMS.md](03_STREAMS.md))
- [x] Create `src/streams/consumer.py` (generic StreamConsumer)
- [x] Create `src/streams/evidence_consumer.py` (evidence:embed handler)
- [x] Create `src/streams/search_consumer.py` (evidence:search handler)
- [x] Add stream config to config.py and .env
- [ ] Test with manual XADD → verify DB rows created
- [ ] Test dedup (send same evidence_id twice → only 1 row)
- [ ] Test dead letter queue (fail 3 times → message in :dead stream)

### Step 4 — BatchTrigger ([04_BATCH_TRIGGER.md](04_BATCH_TRIGGER.md))
- [x] Create `src/services/batch_trigger.py`
- [ ] Test count-based flush (20 events → immediate dispatch)
- [ ] Test timeout-based flush (1 event → flush after 5s)
- [ ] Test health() metrics endpoint
- [x] Wire into stream consumers (notify after creating rows)

### Step 9 — Diversity Filter ([09_DIVERSITY_FILTER.md](09_DIVERSITY_FILTER.md))
- [x] Add `opencv-python-headless` to requirements.txt
- [x] Create `src/services/diversity_filter.py` (Bhattacharyya histogram filter)
- [x] Add diversity filter config to config.py and .env
- [x] Wire into evidence_consumer.py (filter before creating DB rows)
- [ ] Test: 15 similar images → verify only 4-6 kept
- [ ] Test: min_images guarantee (at least 1 even if all similar)
- [ ] Test: quality rejection (too small, extreme aspect ratio)

---

## Phase 3: Optimized Workers

### Step 5 — Two-Phase ARQ Workers ([05_WORKERS.md](05_WORKERS.md))
- [x] Create `src/workers/embedding_worker.py` (Phase 1: parallel CLIP, Phase 2: bulk upsert)
- [x] Create `src/workers/search_worker.py`
- [x] Create `src/workers/main.py` (WorkerSettings, startup/shutdown)
- [x] Update `worker.py` entry point
- [ ] Test embedding batch: 10 evidences x 3 images → verify Qdrant + DB
- [ ] Test search batch: 5 searches → verify results stored in Redis
- [ ] Benchmark: compare old sequential vs new two-phase processing time

---

## Phase 4: Reliability

### Step 6 — Safety Nets ([06_SAFETY_NETS.md](06_SAFETY_NETS.md))
- [x] Add `apscheduler` to requirements.txt
- [x] Implement embedding_safety_net (60s)
- [x] Implement search_safety_net (120s)
- [x] Implement recover_stale_working (5m)
- [x] Implement recalculate_searches (1h) — own endpoint, no Video Server dependency
- [x] Implement cleanup_old_requests (24h)
- [ ] Test: create row at status=1 without triggering → verify safety net picks it up
- [ ] Test: create row at status=2 with old timestamp → verify stale recovery resets it

---

## Phase 5: Integration

### Step 7 — FastAPI Migration ([07_API_MIGRATION.md](07_API_MIGRATION.md))
- [x] Update lifespan: wire DB, ARQ pool, scheduler, triggers, consumers
- [x] Update `/health` endpoint with new components
- [x] Update `/api/v1/stats` endpoint with pipeline metrics
- [x] Add `/api/v1/internal/trigger/{name}` endpoint
- [x] Add `/api/v1/recalculate` triggerable endpoint
- [x] Disconnect old scheduler and Video Server notification logic
- [ ] Add query endpoints for Video Server to fetch our data (embedding status, search results)
- [ ] Integration test: XADD → verify full flow → DB + Qdrant + Redis results

### Step 8 — Docker & Dependencies ([08_DOCKER_DEPS.md](08_DOCKER_DEPS.md))
- [x] Update docker-compose.yml (postgres + redis + qdrant)
- [x] Update requirements.txt (all new deps)
- [x] Update Makefile (add migrate targets)
- [ ] Update .env.example
- [ ] Test `make docker-up && make migrate && make run-api` workflow
- [ ] Test `make run-worker` in separate terminal

---

## Phase 6: Cleanup & Docs

### Remove Old Code
- [x] Disconnect old ARQ cron scheduler imports
- [x] Disconnect Video Server client imports
- [ ] Delete `src/infrastructure/scheduler/arq_scheduler.py` (kept for reference)
- [ ] Delete `src/infrastructure/api/video_server_client.py` (kept for reference)

### Update Documentation
- [ ] Update CLAUDE.md with new architecture
- [ ] Update docs/API_REFERENCE.md with new endpoints
- [ ] Update docs/CURL_EXAMPLES.md with new curl commands

### Production Readiness
- [ ] Load test: 100 evidences via stream → measure end-to-end latency
- [ ] Verify dead letter queue works under failure
- [ ] Verify stale recovery under worker crash
- [ ] Monitor PostgreSQL connection pool under load
- [ ] Set up search results Redis DB with appropriate TTL
- [ ] Get production PostgreSQL connection string (waiting on Stanley)
