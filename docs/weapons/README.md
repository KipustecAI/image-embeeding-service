# Weapons Enrichment — Implementation Plan

Integration of weapon-detection metadata into the existing `embeddings:results` pipeline. The upstream `compute-weapons` service conditionally enriches the embeddings payload with a `weapon_analysis` block; this service needs to persist it, index it, and expose it through the search API.

## Goals

1. **Store** per-evidence and per-image weapon detection data without losing the raw detail (bboxes, confidences, classes).
2. **Filter** at Qdrant search time so callers can request: all images, only images with weapons, weapon-free images, or a false-positive-review queue.
3. **Subset by class** — callers can narrow a weapon search to specific classes like `["handgun"]`.
4. **Backward compatible** — if `weapon_analysis` is absent (legacy or routing skipped the weapons service), the pipeline behaves exactly as it does today.

## Key design decisions (already locked in)

| Decision | Choice | Rationale |
|---|---|---|
| Stream topology | **Same stream, same event type** (`embeddings:results` / `embeddings.computed`) — optional `weapon_analysis` field | Diagram shows both paths (with/without weapons) converge on the same backend consumer. No new consumer needed. |
| Storage layer | **Hybrid**: scalar columns on `embedding_requests` + JSONB detail on `evidence_embeddings` + Qdrant payload flags | Each layer pulls its weight: SQL for reports, JSONB for detail, Qdrant for search-time filtering. |
| `has_weapon` scope | **Per-image**, not per-evidence | Each Qdrant point is one image; "find images with handguns" must return only the images that actually have the handgun. |
| Three-state tracking | **Two booleans** (`weapon_analyzed` + `has_weapon`), not a single nullable bool | We need to distinguish "never checked" from "checked and clean" for the false-positive review workflow. |
| Class-level filtering | Yes — `weapon_classes` as a Qdrant `keyword[]` payload field, filtered via `MatchAny` | Enables `weapons_filter="only" + weapon_classes=["handgun"]`. |
| Search filter modes | Four: `all` (default), `only`, `exclude`, `analyzed_clean` | `analyzed_clean` is the review queue for potential false negatives. |

## Phase index

| Phase | File | Scope |
|---|---|---|
| 0 | [00_CONTEXT.md](00_CONTEXT.md) | Problem, payload sample, state cube, scope boundaries |
| 1 | [01_DATABASE.md](01_DATABASE.md) | Alembic migration, model changes, repository signature |
| 2 | [02_QDRANT.md](02_QDRANT.md) | New payload indices + `MatchAny` fix in `search_similar` |
| 3 | [03_CONSUMER.md](03_CONSUMER.md) | `embedding_results_consumer.py` enrichment logic |
| 4 | [04_SEARCH_API.md](04_SEARCH_API.md) | Request DTO, search use case, filter mapping |
| 5 | [05_DOCS_AND_VERIFICATION.md](05_DOCS_AND_VERIFICATION.md) | Doc updates + end-to-end verification plan |
| — | [CONTRACT.md](CONTRACT.md) | **Producer contract** — authoritative spec for the `compute-weapons` team. Field-by-field schema, three worked examples, error semantics, validation rules, versioning policy. |

## Execution order

Phases must be applied **in order**: database first (so consumer writes have a target), Qdrant second (so consumer upserts don't silently drop the new payload fields), consumer third, search API fourth, docs/tests last. Each phase should be independently reviewable and shippable.

## Out of scope (explicit non-goals)

- **Historical backfill.** Existing Qdrant points and DB rows are left with `weapon_analyzed=false` / `has_weapon=false`. Reports can show "N of M evidences analyzed" so partial coverage is visible during rollout. Reprocessing old evidences is a separate initiative.
- **Weapon-detection model changes.** The class list, thresholds, and bbox format are owned by the `compute-weapons` service. This plan takes the payload as-is.
- **Notifications / alerting UI.** The diagram shows a "Notifications and Reports" consumer downstream — that work lives in a separate repo and is unblocked once `embedding_requests.has_weapon` is queryable.
- **Re-scoring existing searches.** Completed searches keep their stored query vector; they are not re-run automatically when the weapon flags are added.
