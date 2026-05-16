# Wiki Event Log

Chronological append-only record of meaningful events in the wiki and the system it describes: feature ships, decisions, doc updates, lint passes, incidents, verification runs.

**Convention:** newest entry at the top. Each entry starts with `## [YYYY-MM-DD] <type> | <one-line summary>` so the file is greppable: `grep "^## \[" log.md | head -20`.

**Types:**
- `ingest` — new source ingested or significant doc captured
- `decision` — an architectural / product / contract decision made and recorded
- `ship` — feature shipped to the codebase
- `lint` — wiki health check
- `verification` — manual end-to-end test run
- `incident` — production / dev issue captured for the record

---

## [2026-05-16] lint | architecture-docs refresh after DW publisher ship

Targeted lint pass on the internal arch docs to absorb the DW integration without re-deriving on the next look. Pre-existing drift on `weapons:detected` (never documented in `04_STREAM_CONTRACTS.md` after Phase 04's report-gen work) fixed at the same time.

- [`new_arq_v2/04_STREAM_CONTRACTS.md`](new_arq_v2/04_STREAM_CONTRACTS.md) — added the missing `weapons:detected` section (cross-links to `REPORT_GENERATION_STREAMS.md §2` rather than duplicating). Added a new `## Lookia-DW outbound streams` section listing all 7 with event_type + trigger + source-table columns, plus a paragraph on the PII guarantee and MAXLEN env var. Consumer-groups + dead-letter tables extended to list all 7. Pattern: arch doc points at the authoritative contract docs instead of duplicating shapes.
- [`new_arq_v2/00_OVERVIEW.md`](new_arq_v2/00_OVERVIEW.md) — stream count bumped from 6 to 13. The 7 new rows added to the topology table.
- [`new_arq_v2/01_REPO_STRUCTURE.md`](new_arq_v2/01_REPO_STRUCTURE.md) — `dw_publisher_service.py`, `dw_hashing.py`, `test_dw_publisher.py`, `test_dw_hashing.py` added to the src/ + tests/ tree listings.
- [`new_arq_v2/03_BACKEND_SERVICE.md`](new_arq_v2/03_BACKEND_SERVICE.md) — responsibility list grew 8 → 9 (added DW publish hooks). Lifespan section's StreamProducer injection note updated to mention the new injection site.

No drift found in: `API_REFERENCE.md` (no new HTTP endpoints), `BLACKLIST_API.md`, `CURL_EXAMPLES.md`, `README.md`, `requirements/IMAGE_COMPUTE_STREAMS.md`, `requirements/REPORT_GENERATION_STREAMS.md`, `weapons/*`, `image-blacklist/*`. The DW work was pure publisher-side; no surface-area drift in those domains.

## [2026-05-16] ship | lookia-dw 7 publish hooks producer side

Shipped commit `9790bf6` — the producer side of the 7 DW streams documented in [`requirements/LOOKIA_DW_STREAMS.md`](requirements/LOOKIA_DW_STREAMS.md). DW expects to verify within minutes of `git push origin main`.

**New files:**
- [`src/application/helpers/dw_hashing.py`](../src/application/helpers/dw_hashing.py) — locked `name_hash` recipe (`sha256(name.encode('utf-8')).hexdigest()[:16]`), 7 tests.
- [`src/services/dw_publisher_service.py`](../src/services/dw_publisher_service.py) — 7 publish functions, one per stream. Each builds the contract shape, hashes PII, calls `StreamProducer.publish` with the per-stream MAXLEN. 12 tests.

**Hook sites:**
- [`src/main.py`](../src/main.py) — `image_search.created` after POST /api/v1/search commit.
- [`src/streams/search_results_consumer.py`](../src/streams/search_results_consumer.py) — `image_search.completed` + `image_search.matched` (only when `total_matches > 0`); `image_search.failed` on both top-level except and `compute.error` paths; `blacklist_image_reference.upserted` on the reference-error fall-through.
- [`src/streams/embedding_results_consumer.py`](../src/streams/embedding_results_consumer.py) — `image_embedding_request.created` + `.completed` back-to-back after commit (atomic INSERT; no separate TO_WORK state on our side); `.failed` on exception; one `image_embedding.upserted` per `evidence_embeddings` row.
- [`src/application/use_cases/manage_blacklist_image.py`](../src/application/use_cases/manage_blacklist_image.py) — `blacklist_image_entry.upserted` on create/update; `blacklist_image_reference.upserted` on add; entry `.upserted` again when status flips CREATED→PROCESSING.
- [`src/services/blacklist_embed_service.py`](../src/services/blacklist_embed_service.py) — `blacklist_image_embedding.created` on INSERT; reference `.upserted` on status→PROCESSED; entry `.upserted` on status→INDEXED.

**Producer enhancement:** [`src/streams/producer.py`](../src/streams/producer.py) `publish()` gained optional `maxlen` kwarg for approximate trimming on high-cardinality streams. Existing callers (`weapons:detected`, `image:blacklist_match`) pass no maxlen and keep their current unbounded behavior. `json.dumps` now uses `default=str` so DW payloads handle `datetime` / `UUID` cleanly.

**Config:** new `DW_STREAM_*` (7) and `DW_MAXLEN_*` (7) env-driven settings; defaults match the renegotiated contract. Bump `DW_MAXLEN_IMAGE_EMBEDDING=2_000_000` before any direct-load backfill push.

**Test result:** 136/136 unit tests pass — the PII regression guard asserting raw `name` is never on the wire is in `tests/test_dw_publisher.py::test_blacklist_image_entry_never_includes_raw_name`. FastAPI app loads cleanly.

Open items #5 + #6 in the contract → both resolved. Outstanding: #3 (DW writes their `dw_direct_load_image_embedding.py`) and #4 (we grant read-only Postgres role on Neon when they request).

## [2026-05-16] decision | lookia-dw accepted contract without edit — implementation unblocked

DW agent confirmed [`LOOKIA_DW_STREAMS.md`](requirements/LOOKIA_DW_STREAMS.md) without edits. Both simplifications and the MAXLEN renegotiation accepted as proposed:

- `image_embedding_request.weapon_analyzed` event type — **dropped**. DW keys off `weapon_analyzed=true` in `.completed` payload.
- `image_embedding:raw` UPDATE branch — **dropped**. Single INSERT-only `.upserted` per row.
- MAXLEN §4.6 = 500k, §4.7 = 500k / 2M backfill — **accepted**.
- Enum corrections noted on their side: `SearchRequestStatus` values different from their original guess; `SimilarityStatus` 1/2 will be mirrored as a derived `has_matches` bool on their fact table for fast filtering.
- `weapon_classes[]` free-form for v1 — they'll track distinct values dynamically; we can promote to enum dim later if the image-weapons-compute model ever locks the vocabulary.

**DW-side parallel state:** migration 011 already applied on Neon prod (6 tables for our slice + 1 face-team symmetry + 2 widenings). Worker + bulk_upsert build runs alongside our producer ship. They expect to verify within minutes of `git push origin main` from our side. `dw_direct_load_image_embedding.py` queued next on their side (~30 min build). Read-only Postgres role request expected 2026-05-17 for the historical backfill.

**Open items table on both wiki pages updated** to mark #1, #2 as resolved and #5 (producer implementation) as unblocked.

**Implementation pending kick-off on our side** — ~150 LoC across `services/dw_publisher_service.py` + `helpers/dw_hashing.py` + 5 lifecycle hooks + `DW_MAXLEN_*` env vars + PII regression test. Estimated 2 focused hours.

## [2026-05-16] ingest | lookia-dw authoritative wire-format contract filed

Companion to the negotiation tracker — filed [`requirements/LOOKIA_DW_STREAMS.md`](requirements/LOOKIA_DW_STREAMS.md) as the authoritative wire-format contract for the 7 outbound streams. Mirrors the pattern of [`REPORT_GENERATION_STREAMS.md`](requirements/REPORT_GENERATION_STREAMS.md): per-stream payload schemas + realistic examples + MAXLEN + trigger semantics + delivery / ordering / dedup conventions + versioning policy.

Pattern intent (matches the llmwiki design):

- **`LOOKIA_DW_PUBLISHERS.md`** = process / negotiation tracker. Captures *how* we got here — DW's ask, our pushback, deltas, lessons. Lives in the negotiation-driven layer.
- **`LOOKIA_DW_STREAMS.md`** = wire-format authority. Captures *what gets published* — payload shapes the DW consumer builds against. Lives in the canonical-contract layer.

The two cross-reference each other (companion links). DW consumers build against the contract doc; future maintenance on either side reads the tracker for the *why*.

Authored 9 sections in the new contract:
1. Context (the producer + recovery model)
2. Envelope format (flat hash, JSON payload, XADD example, XRANGE verification)
3. Status enum reference (full table from `src/db/models/constants.py`)
4. Seven per-stream sections (4.1–4.7) — each with event_type table, payload schema, realistic example, MAXLEN, edge cases. §4.6 + §4.7 flagged as Tier 3 with the lifecycle-simplification asks called out.
5. Delivery semantics (fire-and-forget, no outbox)
6. Dedup responsibility (DW side, per-stream key recommendations)
7. Event ordering (within-row monotonic; cross-row none)
8. Versioning policy (additive vs breaking + coordination matrix)
9. Open items (7 entries, ownership tagged)

Cross-linked from [`index.md`](index.md) (new wiki row + updated quick-lookup pointing at the contract for the authoritative wire shape) and from the negotiation tracker (companion link at the top).

## [2026-05-16] ingest + decision | lookia-dw publisher requirements ingested + responded

Lookia-dw published a Tier 3 ask for 7 publish hooks (~80 LoC) feeding their data warehouse — full doc at [`../../lookia-dw/docs/requirements/image-embedding-service.md`](../../../lookia-dw/docs/requirements/image-embedding-service.md). The 2 high-value streams are `image_embedding_request:raw` (weapon detection metadata) and `image_embedding:raw` (per-image embed events including `weapon_detections` JSONB).

**Filed as wiki:** [`requirements/LOOKIA_DW_PUBLISHERS.md`](requirements/LOOKIA_DW_PUBLISHERS.md) — captures the negotiated state. Replied to their four asks:

1. **Status enums delivered** — authoritative from [`src/db/models/constants.py`](../src/db/models/constants.py). Correction: their guess for `search_requests.similarity_status` was wrong (1=NO_MATCHES, 2=MATCHES_FOUND, not pending/computed/indexed).
2. **Volume snapshot delivered** — embedding_requests 35,778 rows; evidence_embeddings 277,899 rows. Their planning numbers undersized us by ~6×, so we renegotiated MAXLEN: `image_embedding_request:raw` 500k (was 100k), `image_embedding:raw` 500k steady / 2M for backfill push (was 100k/500k).
3. **`REDIS_STREAMS_DB=3` confirmed** across all envs.
4. **No `publish_to_many` fan-out** — grep returns zero; our 4 existing `StreamProducer.publish()` call sites are independent XADDs. Their Lesson 1 hazard doesn't apply here.

**Pushed back on two event types** that don't match our state machine: their `image_embedding_request.weapon_analyzed` (separate event when `weapon_analyzed` flips false→true) and the UPDATE branch of `image_embedding.upserted` (when `weapon_detections` populates). Both don't exist — the consumer writes the row atomically with all fields populated at INSERT. Proposed dropping `.weapon_analyzed` entirely and emitting `.upserted` once per row. Awaiting their confirmation before implementation.

**Implementation pending DW acceptance:** ~150 LoC across a new `services/dw_publisher_service.py`, a `helpers/dw_hashing.py` for the PII-safe `name_hash`, hooks in the 5 lifecycle code paths, `DW_MAXLEN_*` env vars, and a PII regression test. Estimated 2 focused hours.

## [2026-05-16] verification + incident | weapons-notification performance investigation

Ops reported *"the stream input consumer for trigger the notifications is taking too much time"*. Investigation against prod (Neon Postgres + remote Redis) measured the actual latency decomposition.

**Numbers (May 14, the last day weapons analysis was reaching us, n=179):**

- **Per-message processing in this service: p50 = 31 ms, p99 = 54 ms, max = 66 ms.** Service is healthy.
- Queue wait in Redis: p50 = 65,883 ms, p99 = 111,176 ms. This is the dominant component of wall-clock latency — throughput-bound, not code-bound.
- Two weapon-positive events fired alerts end-to-end in 4.7s and 7.6s (Redis-input id → Redis-output id, confirmed independently from DB and Redis sources).

**Bigger finding — the actual reason notifications are missing in May 15-16:** zero `weapon_analyzed=TRUE` rows in the last 24h (Q5 daily breakdown shows 0/2,557 on May 16, 0/5,748 on May 15, after 179/179 on May 14). The upstream producer / routing layer stopped enriching evidence with `weapon_analysis` blocks. No errors captured (`weapon_analysis_error = 0 forever`) — upstream is silently skipping rather than failing-and-reporting.

**Filed as wiki:** [`docs/weapons/PERFORMANCE_ANALYSIS_2026_05.md`](weapons/PERFORMANCE_ANALYSIS_2026_05.md) — captures the decision tree, the diagnostic SQL + Redis queries, the May 2026 numbers, and the "what we definitively ruled out" list so the next on-call doesn't redo this work. Cross-linked from [`weapons/RUNTIME.md`](weapons/RUNTIME.md) under a new "Diagnosing slowness" section.

**Open work surfaced (not done in this investigation):**

1. **Upstream coverage gap** — needs an escalation to the routing / `image-weapons-compute` team; coverage flips between 0% and 100% across days with no error trail on our side.
2. **Consumer throughput** — `backend-workers` consumer group on `embeddings:results` shows lag = 7,079 messages at ~6/min processing rate. Either scale the consumer horizontally or parallelize the per-frame storage uploads inside `_process_embeddings_result` (currently serial `for` loop on `storage_uploader.upload_image`).
3. **No alerting on coverage drop** — we noticed because of a user report, not a metric. A daily `weapons_coverage_pct_24h` cron would catch this earlier.

## [2026-05-06] ingest | weapons RUNTIME synthesis page

Filed [`weapons/RUNTIME.md`](weapons/RUNTIME.md) — a current-state synthesis answering "how does a weapon detection become a downstream report alert?" Surfaces three things that were previously scattered across phase plans, contracts, and the consumer code:

1. The decision that **this service doesn't render bbox-annotated images** — we forward plain frames + JSON bboxes; rendering happens on the report-generation side. The only `PIL` import in the tree is in the legacy `clip_embedder.py`.
2. The trigger location: [`src/streams/embedding_results_consumer.py`](../src/streams/embedding_results_consumer.py) around `_process_embeddings_result` after DB commit. Three conditions must all hold (`weapon_analyzed`, `report_images_with_detections` non-empty, `_stream_producer` injected).
3. Fire-and-forget failure semantics — a Redis hiccup logs but doesn't block ingest. Receiver-side dedup catches single misses gracefully.

Cross-linked from [index.md](index.md) (entry + quick-lookup row) and from [new_arq_v2/03_BACKEND_SERVICE.md](new_arq_v2/03_BACKEND_SERVICE.md) (via the existing report-event publishing description).

This page exists because the user asked the question — captured per the wiki pattern's "good answers can be filed back into the wiki as new pages" guidance, so the next person who asks the same thing finds an answer instead of re-deriving it.

## [2026-05-06] lint | code-vs-docs pass on `new_arq_v2/` architecture trio

First full lint pass under the wiki pattern. Compared each architecture wiki page against current `src/` state and fixed drift.

**Drift found and fixed:**

- [`01_REPO_STRUCTURE.md`](new_arq_v2/01_REPO_STRUCTURE.md) — rewrote the `src/` tree to add the entire `api/v1/` subpackage, `application/helpers/` (5 files), `application/use_cases/manage_blacklist_image.py`, `db/models/blacklist_image.py`, `db/repositories/blacklist_image_repo.py`, `infrastructure/entity_taxonomy.py`, three blacklist services in `services/`, `storage_uploader` + `zip_processor`, the `domain/entities/` files actually in use, the new `docs/` files (README, index, log, llmwiki, BLACKLIST_API, image-blacklist/, requirements/, weapons/), and the full test list. Migrations updated from "5" to "10". Qdrant payload-indices list expanded to all 11 active indices. DB tables list expanded to include the 3 blacklist tables. Removed the false "Legacy Code" claim that `src/application/` and `src/domain/` were unused.
- [`00_OVERVIEW.md`](new_arq_v2/00_OVERVIEW.md) — rebuilt the architecture diagram: added `/api/v1/search/categories`, the 8 blacklist endpoints, both report-event producers, the on-demand reverse-search scheduler entry, and the 3 blacklist tables. Stream topology section now shows 6 streams (was 4) — added `weapons:detected` + `image:blacklist_match`. Added five new "Key design decisions" entries covering single Qdrant collection / `purpose` reuse / fire-and-forget publishers.
- [`03_BACKEND_SERVICE.md`](new_arq_v2/03_BACKEND_SERVICE.md) — responsibility list expanded from 7 to 8 (added report-event publishing). Embedding-results flow now documents the `weapons.detected` publish, inline-blacklist-match step, and category/weapon fields in the Qdrant payload. Search-results flow now documents `purpose` dispatch + the `blacklist_embed` branch. New "Reverse search" subsection. Compute-error section rewritten to document the entity-id fallback dispatch. API endpoint table updated for `/search/categories` + blacklist deep-link. Lifespan section expanded with `set_blacklist_*` wiring + scheduler injection.

**Cross-checks passed:**

- API_REFERENCE.md endpoint table matches the live FastAPI route count (`python -c "from src.main import app; ..."` enumerated 18 service routes + auto-generated `/docs` / `/redoc` / `/openapi.json`).
- `POST /api/v1/search` request body in API_REFERENCE.md matches `SearchCreateRequest` in `src/main.py:299`.
- `_EVIDENCE_PAYLOAD_INDICES` in `src/infrastructure/vector_db/qdrant_repository.py` matches the indices list documented in `01_REPO_STRUCTURE.md` and `API_REFERENCE.md`.

**Skipped this round (already current or low-priority):**

- `requirements/IMAGE_COMPUTE_STREAMS.md` and `REPORT_GENERATION_STREAMS.md` — refreshed in the last two days, no drift.
- `weapons/*` phase plans — raw historical, intentionally not retouched.
- `image-blacklist/*` phase plans — raw historical, intentionally not retouched.
- `new_arq/` (v1 architecture) — superseded, kept as raw reference.
- `legacy/` — kept as raw reference.

`docs/llmwiki.md` (the seed pattern) was tracked in this commit too — it was sitting untracked since the wiki adoption commit.

## [2026-05-06] ingest | wiki pattern adopted; docs/ scaffolding created

Reorganized `docs/` to follow the [LLM Wiki](llmwiki.md) pattern. Added [README.md](README.md) (schema), [index.md](index.md) (catalog), this log. No existing files moved — wiki layer is added on top so cross-links stay intact. Classification of each existing file into wiki / raw / schema lives in the index.

Going forward: every feature ship, decision, or doc revision appends a log entry. Wiki pages are the single source of truth for current state.

## [2026-05-06] ship | image-blacklist Phase 07 — user-facing docs + plan-index update

Updated `new_arq_v2/04_STREAM_CONTRACTS.md` with the `purpose` / `blacklist_entry_id` fields on `evidence:search` + `search:results` and the new `image:blacklist_match` producer stream. Updated `API_REFERENCE.md`, `CURL_EXAMPLES.md`, root `README.md`. Phase index in `image-blacklist/README.md` marked complete with commit refs and a "Deviations from the original plan" subsection. Commit `ef8ebbe`.

## [2026-05-06] ship | image-blacklist Phase 06 — REST CRUD API

Eight endpoints under `/api/v1/blacklist/image-entries`: create / list / get / patch / delete entry, add / delete reference, trigger backfill. Multi-tenant via `X-User-Id`; foreign-tenant access returns 404 (not 403) to avoid leaking entry existence. PATCH bumps `blacklist_version` on matching-relevant changes (threshold change, reactivation). DELETE cascades through SQL and cleans Qdrant best-effort.

New `src/api/v1/{routers,schemas}` subpackage introduced — first FastAPI router-based feature (others remain inline in `main.py`). `ManageBlacklistImageUseCase` carries multi-tenant rules + version-bump policy. `docs/BLACKLIST_API.md` published as the standalone frontend contract. Commit `7381a17`.

## [2026-05-05] ship | image-blacklist Phase 05 — match detection + report publishing

Inline match: consumer searches the user's blacklist subset after every new evidence commit; skipped via `count_active_by_user` for non-adopters. Reverse-search publishing: each match from the Phase 04 job publishes an `image:blacklist_match` event. Both paths share `BlacklistMatchService.publish_blacklist_match()` so the wire shape stays in one place.

Qdrant payload now carries `infraction_code` so reverse-search hits can attribute the match without a DB roundtrip. `search_similar` injects the matched point id into result metadata. `REPORT_GENERATION_STREAMS.md` §3 promoted from placeholder to real contract. Commit `fa441b8`.

## [2026-05-05] ship | image-blacklist Phase 04 — embed flow + reverse search

Consumer dispatches `search:results` envelopes on `purpose`: `"search"` → existing flow; `"blacklist_embed"` → `BlacklistEmbedService.store_blacklist_embedding()` (Qdrant first, then DB row, then APScheduler one-shot reverse search). Error routing for `compute.error` envelopes that deliberately omit `purpose` — backend resolves by looking `entity_id` up in `blacklist_image_references` first, falls through to `search_requests`. Commits `a504c77` + `cb849db`.

## [2026-05-05] ship + decision | category stop-gap (entities → category translation + endpoint)

Compute team shipped `entities: list[int]` from `t_configurations.entities` instead of the requested `category: str` (their commit `fb28d8e`). Backend negotiation surfaced that Option A (config-based) and Option B (content-based) return different data — semantic decision deferred to product / UX.

Stop-gap shipped: backend translates `entities[]` → DB `category TEXT` (JSON `"[2,5]"`) and Qdrant payload `category: list[str]` (`["2","5"]`). New `GET /api/v1/search/categories` returns `[{id, label}]` for the frontend dropdown. Labels come from a hardcoded YOLO/COCO-80 map in `src/infrastructure/entity_taxonomy.py` — to be replaced when the platform team exposes `t_configurations.entities` as a readable endpoint. Commit `e80de7c`.

Full negotiation trail in [requirements/IMAGE_COMPUTE_STREAMS.md](requirements/IMAGE_COMPUTE_STREAMS.md) §2.

## [2026-04-20] ship | image-blacklist Phases 01–03

- Phase 01 (`8d03f5b`): category column on `embedding_requests`, Qdrant payload index, search-API `category` filter with `MatchAny`.
- Phase 02 (`4c41780`): three blacklist tables (`blacklist_image_entries` / `_references` / `_embeddings`) + Alembic + repository (16 tests).
- Phase 03 (`64a89d8`): `source_type` filter helpers (`build_evidence_only_filter`, `build_blacklist_only_filter`, `build_blacklist_entry_filter`) + `blacklist_entry_id` Qdrant index (14 tests).

## [2026-04-20] decision | image-blacklist phased plan + IMAGE_COMPUTE_STREAMS contract sent

Plan committed at `bffcb6b`: 7 phases (01_CATEGORY through 07_DOCS_AND_VERIFICATION) under `docs/image-blacklist/`. Decisions locked at planning time:
- Single Qdrant collection with `source_type` discriminator (not separate collections).
- Reuse `evidence:search` with `purpose="blacklist_embed"` rather than a new stream pair.
- Global `BLACKLIST_MATCH_THRESHOLD`; per-entry override stored but only consulted at match time (no rematch on change in v1).
- APScheduler one-shot jobs for the async reverse search (not ARQ, not raw asyncio).

Plus `bde5b85`: `requirements/IMAGE_COMPUTE_STREAMS.md` published as the requirements ask to the compute team — `category` pass-through (§2) and `purpose` + `blacklist_entry_id` echo (§3).

## [2026-04-15] ship | weapons feature complete + report-generation contract proposed

Weapons enrichment shipped across phases 1–5 (commits `a85758c` → `a3a8936`) plus follow-ups for the error column and the `weapons.detected` event publisher (`3e51e69`, `b4268ac`). `weapons/CONTRACT.md` captured as the authoritative producer contract. `requirements/REPORT_GENERATION_STREAMS.md` proposed to the report-generation team (sub-types 1D weapons + 1E placeholder for blacklist).

## [2026-03-27] ship | ETL ZIP integration + multi-tenant

`047fb44`: switched ingest from individual image URLs to a ZIP-download flow driven by ETL. Multi-tenant payloads on Qdrant. Storage uploader for permanent MinIO URLs. Architecture docs refreshed (`9c366b8`, `b258487`).

---

## (Pre-2026-03-27)

Earlier history is captured in `git log`. Major prior milestones:

- v1 → v2 architecture migration (`new_arq/` → `new_arq_v2/`): polling-worker → event-driven pipeline with local persistence and the deepface-restapi-inspired stream patterns.
- Initial implementation of evidence-embedding pipeline with CLIP ViT-B-32, Qdrant, ARQ scheduler.
