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
