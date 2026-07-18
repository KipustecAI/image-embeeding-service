# On-Demand Image Index — Phased Delegate-Build Plan

Five phases, each **additive + gated-OFF**, each = *implement → adversarial audit → apply must-fixes
+ local CI*. The runtime flag `IMAGE_INDEX_ENABLED` stays **False** through Phases 1–4; it flips to
True only after the Phase 5 live smoke. The full validated spec is [`00_DESIGN.md`](00_DESIGN.md);
this plan is the sequencing + per-phase deliverables + gates.

## Compute-freeze dependency

The compute wire [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md)
is still **v1-DRAFT** (open items: stream names, `N_CAP=100`, whether compute emits `filtered`,
512-D shape). The dependency line:

- **Phases 1–2 do NOT depend on the freeze** — they build against the submit/lifecycle envelopes we own, plus stable table/repo/service scaffolding. Phase 2 *builds* the dispatch path but must **not go live** (no real XADD to `image:index`) until freeze.
- **Phase 3 DEPENDS on the freeze** — `land_computed` decodes the results shape and the `filtered` disposition; write it against the frozen envelope.
- **Phase 5 DEPENDS on the freeze** + a bilateral bring-up (compute's `image:index` group live first).

Local CI at every phase runs inside the conda env:
`source ~/anaconda3/etc/profile.d/conda.sh && conda activate image_compute_backend_p11 && make ci-local`.

---

## Phase 1 — Foundations (flag + config + collection + migration + models/repo/service)

**Gated-off:** yes. Nothing registered — zero runtime footprint.

Deliverables:
- **Canonical Settings block** in `src/infrastructure/config.py` (00_DESIGN §2) — one attribute + one env alias per value.
- `ImageIndexBatchStatus` / `ImageIndexResultStatus` in `src/db/models/constants.py` (append).
- `src/db/models/image_index.py` (both models; `batch_metadata = Column("metadata", JSONB)` alias; `client_batch_ref` UNIQUE; `UNIQUE(batch_id, item_index)`; CheckConstraints) + `__init__.py` exports.
- Migration `alembic/versions/<rev>_create_image_index_tables.py` (`down_revision = e7f2c9a1b3d6` — confirm single head with `alembic heads` first); additive `create_table` ×2, `server_default` on status/counters.
- `src/infrastructure/vector_db/image_index_vector_repository.py` — standalone class, own ensure + `_ensured` latch, **shares the live `QdrantClient`**, `asyncio.to_thread` on every call. v1 surface: `initialize(client)` + `upsert_items` (+ `delete_by_batch`, unwired). **No search / get_point_vector** (deferred).
- `src/db/repositories/image_index_repo.py` — `create_or_get_batch` (`ON CONFLICT (client_batch_ref) DO NOTHING RETURNING id`), `upsert_result` (`ON CONFLICT (batch_id, item_index) DO UPDATE`), `recompute_counts` (GROUP BY), IDOR-scoped reads (`get_batch`/`get_latest_by_external_id`/`list_batches_by_external_id`/`get_items`), `list_stuck_active`/`mark_failed`.
- `src/services/image_index_service.py` — scaffold: `terminal_status(counts)` (the single shared helper, accounted-guard), lifecycle payload builders, `submit_batch_created`/`create_error_batch`/`land_computed`/`mark_error_from_compute_error` signatures.
- **`tests/conftest.py`** — autouse `TRUNCATE … RESTART IDENTITY CASCADE` over the two new tables, guarded to only-existing tables (00_DESIGN §3). **Load-bearing, not deferred.**

Audit focus: UNIQUE placements, absolute-count-not-incremental, terminal-from-counts + accounted-guard, collection isolation from `initialize()`, config attribute-existence.
Gate: migration up/down; repo idempotency + concurrent-redelivery unit tests (exactly one batch); `terminal_status` truth table incl. the unaccounted case; conftest truncate live.

## Phase 2 — Submit-intake consumer

**Gated-off:** yes (build). **Live dispatch gated on compute freeze.**

Deliverables:
- `src/streams/image_index_submit_consumer.py` — factory + module-global loop bridge + `register_handler("image.index.submit", …)`.
- Two-tier rejection (TIER-1 unbindable → log+ACK-drop; TIER-2 bindable-but-rejected → ERROR batch + `image_batch.failed`), atomic idempotent mint, dispatch to `image:index` **on fresh mint only**, **set `status='computing'` after dispatch**, publish `image_batch.created` with explicit `stream=settings.stream_image_batch_raw`.
- DLQ-depth alert on `image:index:submit:dead` (pre-mint failures surfaced, not auto-recovered).
- Gated wiring stub in `src/main.py` (still OFF).

Audit focus: two-tier rejection, atomicity of `created`, fresh-mint-only dispatch + `computing` transition, idempotent re-bind re-publishes `image_batch.created`.
Gate: submit unit tests (fresh vs duplicate, over-cap ERROR batch, bad `user_id` drop, transient-raise → no ACK).

## Phase 3 — Results consumer + reaper  ⟵ depends on compute v1-FREEZE

**Gated-off:** yes.

Deliverables:
- `src/streams/image_index_results_consumer.py` — `register_handler("image.index.computed", …)` (300s) + `register_handler("compute.error", …)` (60s). Module docstring **calls out the `batch_id`-keyed error divergence** from the live consumers.
- `land_computed`: upsert rows by `(batch_id, item_index)`; single batched `upsert_items` for embedded vectors (deterministic point-id); recompute absolute counts; **log-loud on `Σ != submitted`**; terminalize via the shared `terminal_status` (accounted-guard); publish `image_batch.completed`.
- `mark_error_from_compute_error` → `error` + `image_batch.failed`.
- `src/streams/image_index_reaper.py` — gated `AsyncIOScheduler` job on the **configured** `image_index_reaper_interval_seconds`; terminal DB mark committed first, best-effort `image_batch.failed` after; plain SELECT (single-replica; `FOR UPDATE SKIP LOCKED` noted for scale-out).
- `src/main.py`: **results consumer `.start()` before submit consumer `.start()`** inside the single gated block; reaper `add_job`; shutdown `.stop()` both.

Audit focus: never-raise-per-item (dispositions), reconciliation `submitted == Σ dispositions`, redelivery idempotency (row + point + counts), cold-start order, reaper crash-isolation + best-effort gap.
Gate: redelivery test (re-land is a no-op that re-publishes), terminal-status incl. filtered/unaccounted, compute.error terminalizes, reaper sweep test.

## Phase 4 — REST query surface

**Gated-off:** yes (routes 503 when off).

Deliverables:
- `src/api/v1/routers/image_index.py` + `src/api/v1/schemas/image_index.py` — `GET /results/{batch_id}` and `GET /results/by-external-id/{external_id}` (`?all` bounded `le=200`, `include_items`/`limit`/`offset`).
- **Strict IDOR** (no admin bypass), tenant-miss → `404`, missing `X-User-Id` → `401`.
- Mounted **unconditionally at module level** + `require_image_index_enabled` 503 dependency; both the session-factory and (future) repo setters consolidated at the one mount site.
- Docs: `docs/apis/IMAGE_INDEX_SUBMIT.md` (coordinator) + `docs/apis/IMAGE_INDEX_API.md` (frontend read) cloned from the plates/face siblings; `docs/API_REFERENCE.md` update; gateway ROUTE_CONFIG PR for `/api/v1/embedding/image-index/*` (sibling, no parent-prefix collision).

Audit focus: IDOR WHERE-scoping on every method, 404-not-403/200-empty, `?all` newest-first + bounded, 503-when-off.
Gate: cross-tenant 404 test per endpoint, missing-header 401, `?all` cap, 503 when flag off.

## Phase 5 — Live e2e + flip  ⟵ depends on compute v1-FREEZE + bilateral bring-up

**Gated-off:** flips **ON** at the end.

Steps:
1. Confirm compute's `image:index` group is live; confirm dw-offline registers the `image_batch.*` handler + 4-key summary and adds the `target="image"` enrichment row.
2. Flip `IMAGE_INDEX_ENABLED=true` in a staging env; our results→submit consumers + reaper start.
3. Submit a real ≤100-item batch (Redis XADD); watch `image_batch:raw` go `created → completed`; verify vectors land in `image_index_embeddings`; verify `GET /results/by-external-id/{run_id}?all=true`; verify a cross-tenant read 404s.
4. Verify a forced failure (bad URL → `download_failed` inside `completed_with_errors`; killed compute → reaper `error` + `image_batch.failed`).
5. Flip `IMAGE_INDEX_ENABLED=true` in prod only after the smoke passes.

---

## Execution order & rationale

Foundations first (so consumer/service writes have a target + a test harness that doesn't leak),
submit second (mint + dispatch path), results+reaper third (needs the frozen compute shape and the
mint to land against), REST fourth (reads what the first three wrote), live-flip last. Each phase is
independently reviewable (`git log --grep image-index`) and leaves the service shippable with the
flag OFF.

## Out of scope (v1)

- **Search endpoint** `POST /api/v1/image-index/search` — v1.1 (00_DESIGN §7). v1 stores searchable vectors only.
- **Text-query search** — needs a CLIP text-encode this CPU/NO-CLIP service cannot do; would round-trip through compute. Out of scope.
- **Re-run auto-cleanup** — `delete_by_batch` exists but is not auto-invoked; `external_id` scoping surfaces the newest run.
- **Progress fan-out stream** (like plates' `…:progress`) — CLIP batches finish fast; rely on `image_batch:raw` terminal + REST reconcile. Reconsider in v1.1.
- **Multi-replica reaper** (`FOR UPDATE SKIP LOCKED`) — single-replica today.
- **Widening the conftest truncate to pre-existing committed tables** — the two new tables are in-scope for Phase 1; the broader cleanup of the existing no-conftest hole is a separate decision.
