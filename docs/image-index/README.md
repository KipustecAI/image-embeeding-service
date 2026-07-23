# On-Demand Image Index — Implementation Plan

An **additive, isolated, gated-OFF** on-demand batch-index feature for `image-embeeding-service`
(FastAPI :8001 + ARQ worker; CPU / REST; **no GPU, no CLIP on this side**). A coordinator
(dw-offline, as its **4th enrichment target `image`/`clip`**) submits a *batch* of image URLs over
Redis; we mint a batch, dispatch to `image-embedding-compute` (GPU), receive **one 512-D CLIP
vector per item**, persist per-item rows, upsert the vectors into a **dedicated Qdrant collection**
(`image_index_embeddings`), and expose an IDOR-scoped REST recovery surface. **Result semantics are
face-style: the full vector is the payload** — indexed images become *searchable* via those vectors.

This mirrors the already-live face (`face:index`), plates (`plate:index`), and etl
(`ondemand_analysis`) on-demand playbook. It **touches none** of the live evidence / search /
blacklist paths — every artifact is net-new or an append-only addition to a shared registry, and
nothing runs until `IMAGE_INDEX_ENABLED` (default **False**) is flipped after a live smoke.

- **Compute envelope (companion, not duplicated here):** [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md) — `image:index` / `image:index:results`, owned by the compute agent. **🟢 v1-FROZEN 2026-07-22.** Two deltas Phase 3 must honour: vectors arrive as **`vector_b64`** (`f32le_b64`, 512-D float32 LE) not `list[float]`, and **diversity dedup is DISABLED** (`filtered` reserved-but-never-emitted, `duplicate_of_index` always null → `submitted == embedded + failed`).
- **This design owns** the submit envelope (`image:index:submit`) and the lifecycle envelope (`image_batch:raw`).

## Goals

1. **Batch-index on demand** — accept a coordinator's batch of `{item_id, image_url}` over Redis, dispatch to compute, land one result per item.
2. **Store vectors, make them searchable** — upsert each 512-D CLIP vector into a dedicated Qdrant collection keyed by a deterministic point-id; per-item disposition rows in Postgres reference them.
3. **Never let the no-HTTP coordinator hang** — every batch reaches a terminal lifecycle event (`created → completed | failed`); a reaper sweeps genuinely-lost compute replies.
4. **IDOR-scoped recovery** — `GET /api/v1/image-index/results/{batch_id | by-external-id/{external_id}}`, tenant-scoped in the WHERE clause; a tenant miss is a `404`.
5. **Zero live-path footprint** — additive tables, a dedicated collection with its own ensure, dedicated streams + consumer groups, one gated wiring block. Flag OFF ⇒ nothing constructed, nothing mounted, no stream touched.

## Locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Result semantics | **Face-style** — the full 512-D CLIP vector is the per-item payload, stored in a **dedicated** Qdrant collection `image_index_embeddings` + a Postgres reference row. Indexed images become searchable. |
| 2 | Coordinator | **dw-offline** as its 4th enrichment target `target="image"` (a.k.a. `clip`), class-agnostic, mirroring its `analysis` path 1:1. |
| 3 | Streams | `image:index:submit` (coordinator→us) · `image:index` (us→compute) · `image:index:results` (compute→us) · lifecycle `image_batch:raw` (us→coordinator). All Redis **DB 3**. |
| 4 | Tables | `t_image_index_batches` / `t_image_index_results`, one additive migration off head `e7f2c9a1b3d6`. |
| 5 | REST | `/api/v1/image-index/*`; public via the gateway rewrite `/api/v1/embedding/image-index/*`. |
| 6 | Flag | `IMAGE_INDEX_ENABLED` (default **False**) — bring-up gate, single wiring chokepoint. |
| 7 | Auth | Gateway-header-trust (`X-User-Id`) on REST; Redis-trust (`user_id` in payload) on the submit. |
| 8 | Isolation posture | Additive + isolated + gated-OFF — the live evidence / search / blacklist paths stay **untouched**. |

### Post-audit resolutions (contradictions the merge had to settle)

These were divergent across the 6 draft dimensions; a 5-lens adversarial audit forced one answer each.
See [`00_DESIGN.md`](00_DESIGN.md) §11 for the full fix log.

| Seam | Resolved to |
|---|---|
| **Lifecycle event_type** | `image_batch.created` / `image_batch.completed` / `image_batch.failed` (self-namespacing on the shared `image_batch:raw` stream). All call sites + the dw-offline handler agree. |
| **Terminal-status rule** | ONE shared helper `ImageIndexService.terminal_status(counts)`: recompute absolute counts → **accounted-guard** (if `embedded+filtered+failed < submitted`, do NOT terminalize) → `completed` iff `failed==0`, else `completed_with_errors`; `error` only on batch-level `compute.error` / reaper timeout. `filtered` is a clean disposition, never a downgrade. |
| **Count vocabulary** | ONE 4-key folded shape `{submitted, embedded, filtered, failed}` across the lifecycle payload, the denormalized DB columns, and the REST response (`failed` folds `download_failed + decode_failed + no_result`). |
| **Config field names** | ONE canonical Settings block (single attribute + single env alias per value) — see [`00_DESIGN.md`](00_DESIGN.md) §2. |
| **Dedicated Qdrant repo** | ONE standalone `ImageIndexVectorRepository` (own file, own `_ensured` latch, ensure **not** called from the live `initialize()`), sharing the live repo's `QdrantClient`. Injected as the dedicated repo into the results consumer — **not** the live `vector_repo`. |
| **Router gating** | Mounted **unconditionally** at module level; every route gated by a shared `require_image_index_enabled` dependency → **503** when off / repo unavailable. |
| **Search endpoint** | **Deferred to v1.1.** v1 stores searchable vectors + ships the results-query GET legs only. |
| **Admin IDOR bypass** | **Dropped.** Strictly tenant-scoped, matching the face sibling and playbook §7.7. |

## Phase index

Each phase is independently additive and gated-OFF; each = implement → adversarial audit → must-fixes + local CI. Full detail in [`01_PLAN.md`](01_PLAN.md).

| Phase | Gated-off | Depends on compute v1-FREEZE | Scope |
|---|---|---|---|
| **1 — Foundations** | yes | no | Canonical config block, status constants, two models + migration, dedicated Qdrant repo (ensure + upsert only), read repo + service scaffold, **`tests/conftest.py` truncate fixture**. Nothing registered. |
| **2 — Submit-intake** | yes | no (build) / **yes (live dispatch)** | Submit consumer: two-tier rejection, atomic idempotent mint, dispatch on fresh mint, `image_batch.created`. |
| **3 — Results + reaper** | yes | **yes** | Results consumer (`image.index.computed` + `compute.error`), idempotent land, absolute counts, terminal-from-counts, `image_batch.completed/failed`; gated APScheduler reaper. |
| **4 — REST query surface** | yes | no | Two GET endpoints, strict IDOR, `require_image_index_enabled` 503 gate, bounded `?all` paging. |
| **5 — Live e2e + flip** | flips ON | **yes** | Bilateral bring-up with compute, real batch smoke, verify vectors + REST + cross-tenant 404, flip `IMAGE_INDEX_ENABLED=true`. |

## Open items

| # | Item | Owner | Blocks |
|---|---|---|---|
| 1 | ~~Freeze `IMAGE_INDEX_COMPUTE.md`~~ — ✅ **CLOSED 2026-07-22, v1-FROZEN.** All 5 items agreed: streams/event-types accepted · `N_CAP=100` with **`vector_b64`** encoding · **dedup DISABLED** · 512-D CLIP confirmed · dedicated batch path. Compute signals when their consumer group is live. | ~~compute~~ | ~~Phase 3~~ — **unblocked** |
| 2 | Confirm lifecycle event_type strings `image_batch.*` and the 4-key folded counts with dw-offline; add the `target="image"` enrichment row | dw-offline | Phase 5 go-live |
| 3 | Register gateway route `/api/v1/embedding/image-index/*` → `/api/v1/image-index/*` (sibling of `.../embedding/search`) | gateway | Public REST reads (not the Redis legs) |
| 4 | `client_batch_ref` UNIQUE scope — global vs per-user (recommend global; dw refs are globally unique) | us + dw-offline | Nice-to-confirm, not blocking |
| 5 | Reaper `FOR UPDATE SKIP LOCKED` — required only before running >1 API replica (single-replica today) | us | Scale-out, not v1 |

## Consumer-facing contracts (hand these out)

| Doc | Audience | Sibling analog |
|---|---|---|
| [`../apis/IMAGE_INDEX_SUBMIT.md`](../apis/IMAGE_INDEX_SUBMIT.md) | **Coordinators** (dw-offline) — Redis-only intake, lifecycle, status machine, counts | plates `PLATE_INDEX_SUBMIT.md` |
| [`../apis/IMAGE_INDEX_API.md`](../apis/IMAGE_INDEX_API.md) | **Frontend / REST clients** — the two GET recovery legs incl. `by-external-id?all` | `FACE_INDEX_API.md` §4–5 |

## Cross-references

- **Compute envelope (companion):** [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md)
- **Pattern source:** etl `docs/concepts/async-index-compute-pattern.md` §4/§6/§7/§10
- **Sibling implementations:** face `FACE_INDEX_API.md`, plates `PLATE_INDEX_SUBMIT.md`, etl `ONDEMAND_ANALYSIS_API.md`
- **Coordinator:** dw-offline `DW_OFFLINE_SERVICE_API.md` (Enrichment / target selector)
- **House-style precedents in this repo:** [`../image-blacklist/README.md`](../image-blacklist/README.md), [`../weapons/README.md`](../weapons/README.md)
- **Reuse anchors:** `src/streams/consumer.py` (`StreamConsumer`), `src/streams/producer.py` (`StreamProducer`), `src/infrastructure/vector_db/qdrant_repository.py`, `src/db/models/blacklist_image.py` + `alembic/versions/e7f2c9a1b3d6_*`, `src/api/v1/routers/blacklist_image.py`, `src/api/dependencies.py` (`get_user_context`).
