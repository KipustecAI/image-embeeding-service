# Image Blacklist — Implementation Plan

CLIP-based image blacklist, modeled on the face-blacklist pattern in [`deepface-restapi`](../../../../lucam/deepface-restapi/src/db/models/blacklist.py). Users register reference images of "things we want to find" (wanted vehicles, stolen objects, anomalous scenes, known-bad infraction patterns), and the system automatically flags any new or existing evidence that matches one of those references above a similarity threshold. Matches become real-time alert reports via the `report-generation` service (sub-type 1E).

The plan is staged so the `category` infrastructure — useful on its own for narrowing normal image searches — ships first, and the blacklist feature builds on top.

## Goals

1. **CRUD blacklist entries** with multi-tenant isolation (users only see their own, admin sees all).
2. **Auto-match on new evidence** — when a new evidence is ingested and clears the embedding pipeline, inline-search against the user's blacklist. Publish a report event for each match.
3. **Auto-match on new blacklist entry** — when a user adds a blacklist reference image, reverse-search all historical evidence (async background job). Publish report events for historical matches.
4. **Narrow normal similarity searches by category** — orthogonal to blacklist, but delivered first as a prerequisite so blacklist reuses the infrastructure.
5. **Backwards compatible** — legacy evidence without a category keeps working; blacklist is a pure addition.

## Decisions locked in

| # | Decision | Choice |
|---|---|---|
| 1 | Blacklist "unit" | Generic `entry` with an open `category` string (defaults null). Mirrors face-blacklist's `category` but broader — supports `"vehicle"`, `"object"`, `"scene"`, `"infraction_pattern"`, etc. |
| 2 | Qdrant collections | **Single `evidence_embeddings` collection** — discriminate via `source_type` payload field (`"evidence"` vs `"blacklist"`). Strict filter enforcement lives in a shared helper so it's architecturally hard to forget. |
| 3 | Embedding path for blacklist images | **Reuse the existing `evidence:search` stream** (which already embeds a single image via the GPU). Adds a `purpose: "blacklist_embed"` discriminator to the payload; the `search:results` consumer dispatches on purpose. No new streams. |
| 4 | Reverse search scope | **All historical evidence, async background job**. API returns 202 immediately; progress queryable via a status endpoint. |
| 5 | Match threshold | **Global** (`BLACKLIST_MATCH_THRESHOLD` env var, default TBD). Per-entry and per-category overrides explicitly deferred — hooks documented in code and in this plan, but not implemented in v1. |
| 6 | Category on normal search | **Phase 01 — shipped before blacklist work starts.** Blacklist filter infrastructure piggybacks on it. |
| 7 | How `category` gets assigned to evidence | Producer-side — add optional `category` field to the ETL → `evidence:embed` → `embeddings:results` payload chain. Default null. **PATCH endpoint for admin overrides is explicitly out of scope for v1** — add it later when ops has a workflow need. |

## Phase index

Phases shipped **in order** between 2026-04-18 and 2026-05-06. Each commit is independently reviewable in `git log --grep image-blacklist`.

| Phase | Status | File | Commit | Scope |
|---|---|---|---|---|
| 0 | n/a | [00_CONTEXT.md](00_CONTEXT.md) | — | Problem, face-blacklist parallels, CLIP vs identity matching, risks (false-positive explosion, reverse-search cost, multi-tenant bleed) |
| **1** | ✅ shipped | [01_CATEGORY.md](01_CATEGORY.md) | `8d03f5b` (+ stop-gap `e80de7c`) | Category infrastructure — Alembic migration, `category` column on `embedding_requests`, Qdrant payload index + MatchAny filter, search API field, producer payload contract update. Stop-gap translates upstream `entities: list[int]` to a stringified id while platform exposes a real taxonomy endpoint — see [requirements/IMAGE_COMPUTE_STREAMS.md](../requirements/IMAGE_COMPUTE_STREAMS.md) §2. |
| 2 | ✅ shipped | [02_DATABASE.md](02_DATABASE.md) | `4c41780` | 3 blacklist tables (entries / references / embeddings) + Alembic migration + repo |
| 3 | ✅ shipped | [03_QDRANT.md](03_QDRANT.md) | `64a89d8` | `source_type="blacklist"` tagging, payload indices, strict-filter helpers (`build_evidence_only_filter`, `build_blacklist_only_filter`), idempotent startup creation |
| 4 | ✅ shipped | [04_EMBEDDING_FLOW.md](04_EMBEDDING_FLOW.md) | `a504c77` | Reuse `evidence:search` with `purpose` discriminator, consumer dispatch in `search_results_consumer`, async APScheduler reverse-search job, defensive error routing for `compute.error` envelopes |
| 5 | ✅ shipped | [05_MATCH_AND_REPORT.md](05_MATCH_AND_REPORT.md) | `fa441b8` | Inline-match on new evidence ingest, `image:blacklist_match` event DTO + shared publisher, formalized [REPORT_GENERATION_STREAMS.md §3](../requirements/REPORT_GENERATION_STREAMS.md) 1E from placeholder to real contract |
| 6 | ✅ shipped | [06_CRUD_API.md](06_CRUD_API.md) | `7381a17` | 8 REST endpoints under `/api/v1/blacklist/image-entries`, multi-tenant scoping (foreign tenants → 404), version-bump rules on PATCH, Qdrant cleanup on DELETE. Frontend contract published as [docs/BLACKLIST_API.md](../BLACKLIST_API.md). |
| 7 | ✅ shipped | [07_DOCS_AND_VERIFICATION.md](07_DOCS_AND_VERIFICATION.md) | (this commit) | Stream-contracts doc + API_REFERENCE + CURL_EXAMPLES + root README updates; manual end-to-end verification checklist for ops to run pre-prod-flip |

### Deviations from the original plan

| Phase | Deviation | Why |
|---|---|---|
| 1 | Stored `category` as a JSON-stringified list of upstream entity ids (`"[2,5]"`) rather than a free-form label | Compute team shipped `entities: list[int]` instead of the requested `category: str`. Backend stop-gap added `entity_taxonomy.py` + a `GET /api/v1/search/categories` endpoint to translate ids → labels. Long-term resolution (platform exposes the taxonomy table) is tracked in [requirements/IMAGE_COMPUTE_STREAMS.md](../requirements/IMAGE_COMPUTE_STREAMS.md) §2. |
| 4 | Added defensive error-routing for `compute.error` envelopes that don't carry `purpose` | Compute's `639d753` deliberately omits the dispatch fields on errors. Backend looks up `entity_id` in `blacklist_image_references` first, falls through to `search_requests` — see [04_EMBEDDING_FLOW.md §"Error routing"](04_EMBEDDING_FLOW.md). |
| 5 | Added `infraction_code` to the Qdrant evidence payload | Reverse-search match events need it; previously required a DB roundtrip per match. Now passed inline. Trivial consumer-side change with no migration. |
| 6 | Introduced `src/api/v1/{routers,schemas}` subpackage instead of inline `@app.get(...)` in `main.py` | `main.py` was already 620+ lines and adding 8 endpoints inline would push past 900. The router pattern is justifiable at this size. Other features (search, stats) remain inline; only the blacklist surface uses the new subpackage. |

## Out of scope

- **Per-entry / per-category threshold overrides.** Global threshold only in v1. Extension point documented.
- **Admin `PATCH /api/v1/evidence/{id}` for category overrides.** Category arrives from the ETL producer only in v1.
- **Bulk import of blacklist entries.** `POST /api/v1/blacklist/entries/bulk` with a CSV/JSON batch is a natural v2 feature. v1 is single-entry-at-a-time.
- **Cross-user / "global" blacklists.** All blacklists are tenant-scoped (own `user_id`) in v1. A "shared" blacklist feature would need product decisions on access control that aren't made yet.
- **Qdrant sharding / collection splits.** Even if reverse-search cost becomes a problem at scale, the fix is async backfill (shipped in v1) plus HNSW tuning — not splitting the collection.
- **Model upgrade backfills.** If we ever re-train CLIP, the blacklist embeddings would need re-computation. Separate initiative, not in v1.
- **UI for reviewing false positives.** The `analyzed_clean` review queue pattern from weapons doesn't directly apply — a blacklist "no match" is uninteresting. If product wants a "review matches" UI later, that's a report-generation concern.

## Cross-references

- Face-blacklist pattern (source of inspiration): [`deepface-restapi/src/db/models/blacklist.py`](../../../../lucam/deepface-restapi/src/db/models/blacklist.py), [`deepface-restapi/src/streams/blacklist_results_consumer.py`](../../../../lucam/deepface-restapi/src/streams/blacklist_results_consumer.py)
- Report-generation stream contract (our output to their side): [docs/requirements/REPORT_GENERATION_STREAMS.md](../requirements/REPORT_GENERATION_STREAMS.md) — §3 formalized in Phase 05
- Existing weapons-enrichment pattern we're reusing (helper structure, migration style, CONTRACT doc): [docs/weapons/README.md](../weapons/README.md)
- Existing search-path filter helper (model for our new `build_source_type_filter`): [src/application/helpers/weapon_filters.py](../../src/application/helpers/weapon_filters.py)

## Rough effort estimate

~2× the weapons feature because of the new CRUD surface and the async reverse-search job. Roughly 6–8 commits, 1–2 days of focused work once this plan is accepted.
