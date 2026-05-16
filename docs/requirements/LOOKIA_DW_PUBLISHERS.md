# Requirements from `lookia-dw` — 7 publish hooks

**Audience:** the lookia-dw team. This document captures their inbound requirements doc + our negotiated responses, so the next person on either side can pick up the integration without re-deriving anything.

**Source doc (theirs, authoritative for requirements):** [`../../../lookia-dw/docs/requirements/image-embedding-service.md`](../../../lookia-dw/docs/requirements/image-embedding-service.md)

**Companion (ours, authoritative for wire shape):** [`LOOKIA_DW_STREAMS.md`](LOOKIA_DW_STREAMS.md) — what the DW consumer actually builds against. This doc is the *negotiation tracker* (history, decisions, open items); the companion is the *contract* (payload schemas, examples, semantics).

**Status:** **accepted by lookia-dw 2026-05-16 — implementation queued.** Both lifecycle simplifications accepted without edit; MAXLEN renegotiation accepted; enum corrections noted on their side. We're unblocked to ship.

---

## TL;DR

Lookia-DW wants 7 publish hooks emitting flat-hash `{event_type, payload}` events on Redis DB 3 to feed their data warehouse. Same wire format as our existing `weapons:detected` / `image:blacklist_match` publishers. Cost estimate from their side: ~80 LoC + 45 min review. Our estimate including PII hash helper + tests: ~150 LoC, ~2 hours.

| # | Stream | Source table | Fires on | Volume estimate |
|---|---|---|---|---|
| 1 | `image_search_request:raw` | `search_requests` | lifecycle | very low (8 rows total — API dormant) |
| 2 | `image_search_match:raw` | `search_matches` | terminal completion w/ matches[] array | low (~350 rows total) |
| 3 | `blacklist_image_entry:raw` | `blacklist_image_entries` | INSERT/UPDATE/soft-archive | very low |
| 4 | `blacklist_image_reference:raw` | `blacklist_image_references` | INSERT/UPDATE | low |
| 5 | `blacklist_image_embedding:raw` | `blacklist_image_embeddings` | INSERT only (immutable) | low |
| **6** | **`image_embedding_request:raw`** *(Tier 3)* | `embedding_requests` | lifecycle | **~38k/month** |
| **7** | **`image_embedding:raw`** *(Tier 3)* | `evidence_embeddings` | INSERT (+ UPDATE on `weapon_detections`) | **~290k/month** |

Tier 3 (§6 + §7) is the high-value slice — captures weapon detection + per-image embed observability that lives nowhere else in the platform.

---

## Our authoritative responses

### Enum maps (status fields)

Source: [`../../src/db/models/constants.py`](../../src/db/models/constants.py). All ints; DW stores raw + a sibling enum-name dim for joins.

| Enum | Values | Note |
|---|---|---|
| `search_requests.status` | 1=TO_WORK, 2=WORKING, 3=COMPLETED, 4=ERROR | No 5 |
| `search_requests.similarity_status` | 1=NO_MATCHES, 2=MATCHES_FOUND | **Correction to DW's guess** — purely a result indicator on completed searches, not a lifecycle. |
| `embedding_requests.status` | 1=TO_WORK, 2=WORKING, 3=EMBEDDED, 4=DONE, 5=ERROR | **4=DONE is defined but unused in prod** — consumer transitions 1→3 or 1→5 only. Safe to treat 3 as terminal-success. |
| `embedding_requests.app_id` | int passthrough | **Not an enum on our side** — gateway header `X-App-Type` value (1, 4, NULL currently seen). Platform-team multi-tenancy doc owns the canonical meaning. |
| `blacklist_image_entries.status` | 1=CREATED, 2=PROCESSING, 3=INDEXED, 4=UPDATING, 5=ERROR | Matches DW's guess exactly. |
| `blacklist_image_references.status` | 1=TO_PROCESS, 2=PROCESSING, 3=PROCESSED, 4=ERROR | Matches DW's guess semantically. |
| `embedding_requests.weapon_classes[]` | string[] | **Not an enum on our side** — comes from upstream `image-weapons-compute` (their YOLO classes). Currently observed: `arma` (101), `persona_armada` (79), `persona` (54), `arma_fuego` (29), `objeto` (15), `celular` (1), `mochila` (1). Vocabulary changes if their model upgrades. |

### Volume snapshot (queried 2026-05-16)

```
Total rows:
  embedding_requests:           35,778
  evidence_embeddings:         277,899
  search_requests:                   8     ← API dormant
  search_matches:                  350
  blacklist_image_entries:           2
  blacklist_image_references:        0
  blacklist_image_embeddings:        0

Daily rate (last 7 days, embed pipeline):
  Avg:    ~1,260 embed_req/day  →  ~38,000/month
          ~9,580 evid_emb/day  →  ~290,000/month
  Peak:    5,748 embed_req,  45,786 evid_emb  (bursty — May 15 spike)

Status distribution (embedding_requests):
  status 3 (EMBEDDED):  34,216  (95.6%)
  status 5 (ERROR):      1,567  (4.4%)
  status 4 (DONE):           0
```

**Key insight:** DW's planning numbers assumed `embedding_requests` at ~6.5k/month and `evidence_embeddings` at ~50k/month. Actual rates are ~6× and ~6× higher respectively. This drives the MAXLEN renegotiation below.

### MAXLEN sizing (renegotiated)

| Stream | DW proposed | Our recommendation | Reason |
|---|---|---|---|
| `image_search_request:raw` | 10,000 | 10,000 ✅ | Search API dormant; this is fine |
| `image_search_match:raw` | 10,000 | 10,000 ✅ | Low volume |
| `blacklist_image_entry:raw` | 5,000 | 5,000 ✅ | Slow-changing |
| `blacklist_image_reference:raw` | 10,000 | 10,000 ✅ | Per-image churn during embed lifecycle |
| `blacklist_image_embedding:raw` | 10,000 | 10,000 ✅ | Append-only, low volume |
| **`image_embedding_request:raw`** | 100k (~"15 months") | **500k (~13 months at real volume)** | DW's 100k = 2.6 months at observed 38k/month |
| **`image_embedding:raw`** | 100k steady, 500k for backfill | **500k steady, 2M for backfill push** | DW's 100k = ~10 days at observed 290k/month; backfill push at 290k/month saturates 500k in 50 days |

### Lifecycle simplification (our request to DW)

Two of their proposed event types don't map onto our state machine:

1. **`image_embedding_request.weapon_analyzed` as a separate event when `weapon_analyzed` flips `false → true`.** Doesn't happen on our side — the `embeddings:results` upstream envelope either carries `weapon_analysis` or doesn't, and the consumer writes the DB row atomically with `weapon_analyzed=true`/`false` set at INSERT. No async update path.

2. **`image_embedding.upserted` UPDATE when `weapon_detections` is populated.** Same shape — `weapon_detections` is written at the initial INSERT or stays NULL forever.

**Our proposal:** drop the `.weapon_analyzed` event type entirely. The `.completed` event payload carries `weapon_analyzed`, `has_weapon`, `weapon_classes`, `weapon_max_confidence` already — DW can key off `weapon_analyzed=true` in `.completed` for the same downstream signal. Similarly, `image_embedding.upserted` fires once per row at INSERT only, not twice.

This simplifies our publisher (no UPDATE hook needed) and saves DW a routing branch. Awaiting their confirmation.

### Other architectural confirmations

- ✅ `REDIS_STREAMS_DB=3` confirmed in all envs (default in [`src/infrastructure/config.py`](../../src/infrastructure/config.py)).
- ✅ **No `publish_to_many` fan-out hazard.** Grep returns zero occurrences. Our 4 existing `StreamProducer.publish()` call sites are independent XADDs.
- ✅ PII hashing on `blacklist_image_entry.name` → `name_hash = sha256(name.encode('utf-8')).hexdigest()[:16]`. Raw name never published.
- ✅ Negative test will assert `name` field is never in `blacklist_image_entry:raw` payload (mirrors face team's `tests/events/test_dw_publisher.py::TestDefensiveGuards` per their v1.0.2 lesson).
- ✅ Publish after `session.commit()` + `session.refresh(row)` so `id` and `created_at` are populated (we already follow this pattern for `weapons:detected`).
- ✅ Failure semantics: try/except, log warning, don't re-raise. Service must commit DB even if Redis is down.

### Lessons we acknowledged (their §"Lessons applied")

| Lesson | Acknowledged | Notes |
|---|---|---|
| 1 — Fan-out hazard | ✅ | No `publish_to_many` on our side. DW hooks are their own XADDs. |
| 2 — Verify wire format with XRANGE | ✅ | Existing `StreamProducer.publish()` already emits `{event_type, payload}`. Verified against `weapons:detected` during the May 2026 latency investigation. |
| 3 — MAXLEN trap | ✅ | Drove the upward sizing for streams 6+7. |
| 4 — Direct-load for backfill | ✅ | We'll grant a temporary read-only role on the Neon host when their script is ready. |

---

## Implementation plan (on our side, when DW confirms)

| Step | Files | Notes |
|---|---|---|
| 1. PII hash helper | `src/application/helpers/dw_hashing.py` | One function: `hash_name(name) -> str`. Pure, easy to unit-test. |
| 2. DW publisher service | `src/services/dw_publisher_service.py` | Module-level `set_dw_publisher_producer()` + one function per stream (`publish_search_request_event`, etc.). Mirrors `blacklist_match_service.py`. |
| 3. Hooks in lifecycle code | search create endpoint (`main.py`), search results consumer, blacklist CRUD use case, blacklist embed service, embedding results consumer | One XADD per hook, after `session.commit()`. Each wrapped in `try/except` like existing publishers. |
| 4. MAXLEN configuration | `src/infrastructure/config.py` | New env vars: `DW_MAXLEN_*` for each stream so ops can tune without redeploy. Defaults per the renegotiated table. |
| 5. Tests | `tests/test_dw_publisher.py` | Per-event-shape tests + the PII regression guard + a smoke test that `set_dw_publisher_producer(None)` leads to graceful skip. |
| 6. Wire in lifespan | `src/main.py` | `set_dw_publisher_producer(stream_producer)` — same StreamProducer instance routes all our outbound traffic. |

Estimate: ~150 LoC, ~2 focused hours.

---

## Acceptance criteria (from DW, our paraphrase)

- [ ] POST `/api/v1/search` → `image_search.created` then `.completed`/`.failed` on `image_search_request:raw` within 30s.
- [ ] Search with matches → single `image_search.matched` event with full `matches[]` array.
- [ ] Blacklist entry create → `blacklist_image_entry.upserted` with `name_hash` (not raw `name`).
- [ ] Reference add → `blacklist_image_reference.upserted`.
- [ ] Reference embed completes → `blacklist_image_embedding.created` with `model_version`.
- [ ] **Tier 3** — evidence ingest → `image_embedding_request.created` then `.completed`/`.failed`; weapon fields populated in `.completed` (no separate `.weapon_analyzed` event per our simplification).
- [ ] **Tier 3** — per evidence-embedding INSERT → single `image_embedding.upserted` carrying the final `weapon_detections` if present.
- [ ] `XRANGE <stream> - +` shows two top-level fields per message: `event_type` + `payload` (JSON string).
- [ ] Negative test: `blacklist_image_entry:raw` payload contains `name_hash`, never `name`.
- [ ] Stop Redis → POST a search → service does NOT crash.

---

## Open items

| # | Item | Owner | Status |
|---|---|---|---|
| 1 | ~~DW confirms simplified lifecycle~~ | Lookia-DW | **✅ Accepted 2026-05-16** — no edits |
| 2 | ~~DW confirms upsized MAXLEN~~ | Lookia-DW | **✅ Accepted 2026-05-16** — 500k / 500k+2M backfill |
| 3 | `dw_direct_load_image_embedding.py` for historical seed (34k embedding_requests + 265k evidence_embeddings) | Lookia-DW | **In progress** — queued next, ~30 min build |
| 4 | Temporary read-only Postgres role on Neon for backfill | This service | **Pending DW request** — likely 2026-05-17 |
| 5 | Implementation (~150 LoC, ~2 hours) | This service | **✅ Shipped 2026-05-16** — commit `9790bf6` |
| 6 | `weapon_classes` vocabulary canonical list | Image-weapons-compute team | Deferred — DW tracks distinct values dynamically; promote to enum dim later if the upstream model locks the vocabulary |

**Notable DW-side state at acceptance:**

- Migration 011 already applied on Neon prod — 6 tables for our slice plus 1 face-team symmetry table plus 2 widenings.
- Worker + bulk_upsert build runs in parallel with our producer ship.
- Verification expected within minutes of our `git push origin main`.
- DW will mirror `SimilarityStatus` as a derived bool `has_matches` on their fact table for fast filtering, storing the raw int alongside. That's internal to them — doesn't affect our wire format.

---

## Cross-references

- DW's inbound requirements (source of truth for what they want): [`../../../lookia-dw/docs/requirements/image-embedding-service.md`](../../../lookia-dw/docs/requirements/image-embedding-service.md)
- Companion inbound from face team that DW used as template: their `lucam/deepface-restapi` `docs/requirements/face-platform-publishers.md` (mentioned in their doc)
- Our existing outbound contract patterns (mirror the publisher shape):
  - [`REPORT_GENERATION_STREAMS.md`](REPORT_GENERATION_STREAMS.md) — weapons + blacklist match events to report-generation
  - [`../weapons/RUNTIME.md`](../weapons/RUNTIME.md) — how the existing `weapons:detected` publisher is wired
  - [`../weapons/PERFORMANCE_ANALYSIS_2026_05.md`](../weapons/PERFORMANCE_ANALYSIS_2026_05.md) — proof the publisher pattern doesn't slow down ingest
- Service-side constants the DW depends on: [`../../src/db/models/constants.py`](../../src/db/models/constants.py)
- Stream contracts internal doc: [`../new_arq_v2/04_STREAM_CONTRACTS.md`](../new_arq_v2/04_STREAM_CONTRACTS.md)
