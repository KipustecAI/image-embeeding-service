# Wiki Index

Catalog of every page in `docs/`, grouped by category. Each entry: link, one-line summary, layer (wiki / raw / schema).

See [README.md](README.md) for the wiki schema and conventions.

---

## Schema

The meta-layer that defines how the wiki works.

| Page | Summary | Layer |
|---|---|---|
| [README.md](README.md) | Schema doc — layers, workflows, conventions. Read first when starting work. | schema |
| [llmwiki.md](llmwiki.md) | The seed pattern (external). Don't edit; quote instead. | schema |
| [index.md](index.md) | This file. Categorized catalog of all pages. | schema |
| [log.md](log.md) | Chronological append-only event log. | schema |

---

## REST API surface

Consumer-facing contracts for the HTTP API. Wiki layer — current state.

| Page | Summary | Layer |
|---|---|---|
| [API_REFERENCE.md](API_REFERENCE.md) | Canonical reference for every endpoint: search, recalculate, blacklist, health/stats. Auth, request/response shapes, Qdrant payload indices. | wiki |
| [BLACKLIST_API.md](BLACKLIST_API.md) | Standalone consumer-facing contract for the `/api/v1/blacklist/image-entries` surface. Designed for the frontend team. | wiki |
| [CURL_EXAMPLES.md](CURL_EXAMPLES.md) | Curl cookbook — usage examples for search (with weapons / category / blacklist filters), blacklist CRUD, stream debugging. | wiki |

---

## Architecture (current — `new_arq_v2`)

Current architecture as of v2.x. Wiki layer.

| Page | Summary | Layer |
|---|---|---|
| [new_arq_v2/00_OVERVIEW.md](new_arq_v2/00_OVERVIEW.md) | High-level event-driven design with local persistence. | wiki |
| [new_arq_v2/01_REPO_STRUCTURE.md](new_arq_v2/01_REPO_STRUCTURE.md) | Repo layout: `src/`, `alembic/`, `docs/`. | wiki |
| [new_arq_v2/02_COMPUTE_SERVICE.md](new_arq_v2/02_COMPUTE_SERVICE.md) | Upstream image-embedding-compute service overview. | wiki |
| [new_arq_v2/03_BACKEND_SERVICE.md](new_arq_v2/03_BACKEND_SERVICE.md) | This service: consumers, producers, scheduler, repositories. | wiki |
| [new_arq_v2/04_STREAM_CONTRACTS.md](new_arq_v2/04_STREAM_CONTRACTS.md) | Full payload schemas for every Redis stream we produce or consume. **Authoritative for envelopes.** | wiki |
| [new_arq_v2/05_MIGRATION_PLAN.md](new_arq_v2/05_MIGRATION_PLAN.md) | One-shot migration plan from v1 → v2; now historical. | raw (historical) |
| [new_arq_v2/fixes/ETL_INTEGRATION.md](new_arq_v2/fixes/ETL_INTEGRATION.md) | Decision write-up: how we integrated with the ETL ZIP pipeline. | raw (decision record) |
| [new_arq_v2/fixes/RECALCULATION_FIX.md](new_arq_v2/fixes/RECALCULATION_FIX.md) | Decision write-up: recalculation-job fix. | raw (decision record) |

---

## Inter-team contracts

Wire-level contracts with upstream / downstream services. Wiki layer — these are living contracts but only change through formal negotiation cycles.

| Page | Summary | Layer |
|---|---|---|
| [requirements/IMAGE_COMPUTE_STREAMS.md](requirements/IMAGE_COMPUTE_STREAMS.md) | Field-additions ask sent to the compute team. Tracks the §2 category negotiation and §3 purpose/blacklist_entry_id echo. | wiki (negotiation-driven) |
| [requirements/REPORT_GENERATION_STREAMS.md](requirements/REPORT_GENERATION_STREAMS.md) | Outbound contract to the report-generation team. §2 weapons:detected; §3 image:blacklist_match. **Canonical for those wire shapes.** | wiki (negotiation-driven) |
| [requirements/LOOKIA_DW_PUBLISHERS.md](requirements/LOOKIA_DW_PUBLISHERS.md) | Inbound requirement from lookia-dw — 7 publish hooks feeding their data warehouse. **Negotiation tracker** — captures enum maps, MAXLEN sizing, lifecycle simplification asks, implementation plan. | wiki (negotiation-driven) |
| [requirements/LOOKIA_DW_STREAMS.md](requirements/LOOKIA_DW_STREAMS.md) | **Authoritative wire-format contract** for the 7 outbound streams to lookia-dw. Per-stream payload schemas + examples + MAXLEN + delivery / ordering / dedup semantics. The DW consumer builds against this. | wiki (canonical contract) |
| [weapons/CONTRACT.md](weapons/CONTRACT.md) | Authoritative producer contract for the image-weapons-compute service (their docs, our copy for reference). | wiki (mirror) |

---

## Feature — Weapons enrichment

Optional weapons-detection enrichment on `embeddings:results`. Implementation complete; phase plans are raw history.

| Page | Summary | Layer |
|---|---|---|
| [weapons/README.md](weapons/README.md) | Phase index + decision log for the weapons feature. | wiki (current-state, with deviations) |
| [weapons/RUNTIME.md](weapons/RUNTIME.md) | Current-state synthesis — how a weapon detection becomes a downstream report alert end-to-end. Trigger location, conditions, failure semantics, "we don't draw bboxes here" decision. | wiki |
| [weapons/PERFORMANCE_ANALYSIS_2026_05.md](weapons/PERFORMANCE_ANALYSIS_2026_05.md) | Investigation write-up + diagnostic recipe. "Is the consumer slow?" decision tree, SQL/Redis queries, measured baseline (p99 processing = 54 ms), and the May 2026 upstream-coverage outage. | wiki |
| [weapons/00_CONTEXT.md](weapons/00_CONTEXT.md) | Problem statement, payload sample, state-cube model. | raw (plan) |
| [weapons/01_DATABASE.md](weapons/01_DATABASE.md) | Alembic migration + model additions for weapon analysis fields. | raw (plan) |
| [weapons/02_QDRANT.md](weapons/02_QDRANT.md) | New payload indices + `MatchAny` fix to `search_similar`. | raw (plan) |
| [weapons/03_CONSUMER.md](weapons/03_CONSUMER.md) | Consumer enrichment in `embedding_results_consumer`. | raw (plan) |
| [weapons/04_SEARCH_API.md](weapons/04_SEARCH_API.md) | Search-filter modes (`all`/`only`/`exclude`/`analyzed_clean`) and the `weapon_classes` param. | raw (plan) |
| [weapons/05_DOCS_AND_VERIFICATION.md](weapons/05_DOCS_AND_VERIFICATION.md) | Doc updates + manual verification checklist. | raw (plan) |

---

## Feature — Image blacklist + category

CLIP-based blacklist + the category infrastructure it depends on. Implementation complete; phase plans are raw history.

| Page | Summary | Layer |
|---|---|---|
| [image-blacklist/README.md](image-blacklist/README.md) | Phase index + commit refs + deviations from the original plan. | wiki (current-state, with deviations) |
| [image-blacklist/00_CONTEXT.md](image-blacklist/00_CONTEXT.md) | Problem, face-blacklist parallels, CLIP vs identity matching, risks. | raw (plan) |
| [image-blacklist/01_CATEGORY.md](image-blacklist/01_CATEGORY.md) | Category infrastructure — column, Qdrant index, search filter. Prerequisite for blacklist. | raw (plan) |
| [image-blacklist/02_DATABASE.md](image-blacklist/02_DATABASE.md) | 3 blacklist tables (entries / references / embeddings) + Alembic + repo. | raw (plan) |
| [image-blacklist/03_QDRANT.md](image-blacklist/03_QDRANT.md) | `source_type` discrimination, payload indices, strict-filter helpers. | raw (plan) |
| [image-blacklist/04_EMBEDDING_FLOW.md](image-blacklist/04_EMBEDDING_FLOW.md) | Reuse `evidence:search` with `purpose`; async APScheduler reverse-search. Has the `compute.error` routing recipe. | raw (plan) |
| [image-blacklist/05_MATCH_AND_REPORT.md](image-blacklist/05_MATCH_AND_REPORT.md) | Inline-match block, `image:blacklist_match` DTO, shared publisher. | raw (plan) |
| [image-blacklist/06_CRUD_API.md](image-blacklist/06_CRUD_API.md) | 8 REST endpoints + multi-tenant rules + version-bump policy. | raw (plan) |
| [image-blacklist/07_DOCS_AND_VERIFICATION.md](image-blacklist/07_DOCS_AND_VERIFICATION.md) | Doc updates + manual end-to-end verification checklist. **Operational runbook.** | raw (plan) |

---

## Raw / historical (superseded)

Kept for reference. Don't update — refer to wiki pages for current state.

| Page | Summary | Layer |
|---|---|---|
| [new_arq/00_OVERVIEW.md](new_arq/00_OVERVIEW.md) and siblings | Previous architecture iteration (v1). Superseded by `new_arq_v2/`. | raw (historical) |
| [new_arq/QUESTIONS.md](new_arq/QUESTIONS.md) | Open questions during v1 design — resolved or moot. | raw (historical) |
| [new_arq/TODO.md](new_arq/TODO.md) | v1 TODO list — irrelevant now. | raw (historical) |
| [legacy/API_INTEGRATION.md](legacy/API_INTEGRATION.md) | Original Video-Server integration design. | raw (historical) |
| [legacy/RECALCULATION.md](legacy/RECALCULATION.md) | Original recalculation flow. | raw (historical) |
| [legacy/TEST_GUIDE.md](legacy/TEST_GUIDE.md) | Pre-v2 testing notes. | raw (historical) |

---

## Quick lookups (by topic)

When you need to find something fast, this is the table to scan.

| I want to know about… | Start here |
|---|---|
| HTTP API surface for end users / frontend | [API_REFERENCE.md](API_REFERENCE.md), [BLACKLIST_API.md](BLACKLIST_API.md) |
| Sample curl invocations | [CURL_EXAMPLES.md](CURL_EXAMPLES.md) |
| Redis stream envelope shapes | [new_arq_v2/04_STREAM_CONTRACTS.md](new_arq_v2/04_STREAM_CONTRACTS.md) |
| Contract with the upstream compute service | [requirements/IMAGE_COMPUTE_STREAMS.md](requirements/IMAGE_COMPUTE_STREAMS.md), [weapons/CONTRACT.md](weapons/CONTRACT.md) |
| Contract with the downstream report-generation service | [requirements/REPORT_GENERATION_STREAMS.md](requirements/REPORT_GENERATION_STREAMS.md) |
| Contract with the downstream lookia-dw service (data warehouse) | **Authoritative wire shape:** [requirements/LOOKIA_DW_STREAMS.md](requirements/LOOKIA_DW_STREAMS.md). Negotiation history: [requirements/LOOKIA_DW_PUBLISHERS.md](requirements/LOOKIA_DW_PUBLISHERS.md) |
| How the weapons enrichment works | [weapons/README.md](weapons/README.md) → individual phase plans |
| How weapon notifications get triggered + where bboxes are drawn | [weapons/RUNTIME.md](weapons/RUNTIME.md) |
| "Weapons notifications are slow" — what to check first | [weapons/PERFORMANCE_ANALYSIS_2026_05.md](weapons/PERFORMANCE_ANALYSIS_2026_05.md) |
| How the image blacklist works | [image-blacklist/README.md](image-blacklist/README.md) → individual phase plans |
| What `category` is and why values look like `"[2,5]"` | [image-blacklist/01_CATEGORY.md](image-blacklist/01_CATEGORY.md), [requirements/IMAGE_COMPUTE_STREAMS.md](requirements/IMAGE_COMPUTE_STREAMS.md) §2 |
| Operational verification checklists | [image-blacklist/07_DOCS_AND_VERIFICATION.md](image-blacklist/07_DOCS_AND_VERIFICATION.md), [weapons/05_DOCS_AND_VERIFICATION.md](weapons/05_DOCS_AND_VERIFICATION.md) |
| The migration from v1 → v2 architecture | [new_arq_v2/05_MIGRATION_PLAN.md](new_arq_v2/05_MIGRATION_PLAN.md) |
