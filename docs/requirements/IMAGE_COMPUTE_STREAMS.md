# Requirements for `image-embedding-compute` — Stream Field Additions

**Audience:** the [`image-embedding-compute`](../../../image-embedding-compute/) team. This document lists additive fields we need flowing through your streams so our backend (image-embeeding-service) can deliver two features: the already-shipped **category** filter and the pending **image blacklist**.

**Both asks are purely additive** — existing callers are unaffected, no envelope changes, no stream renames. Your implementation is a pass-through extension in two specific files.

**Cross-references:**
- Your authoritative contract (the doc we're asking you to extend): [`../../../image-embedding-compute/docs/CONTRACT.md`](../../../image-embedding-compute/docs/CONTRACT.md)
- Our feature plans that drove these requirements: [../image-blacklist/01_CATEGORY.md](../image-blacklist/01_CATEGORY.md), [../image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md)
- Symmetric doc we published for the downstream team: [REPORT_GENERATION_STREAMS.md](REPORT_GENERATION_STREAMS.md)

---

## 0. Current status *(living — updated as the negotiation progresses)*

| Ask | State | Notes |
|---|---|---|
| §2 Phase 01 — evidence pass-through | **Stop-gap shipped on backend; final taxonomy still pending platform team** | Compute shipped `entities: list[int]` from `t_configurations.entities` (commit `fb28d8e`). Backend now translates `entities[]` → `category` on its side using a hardcoded YOLO/COCO-80 map at [src/infrastructure/entity_taxonomy.py](../../src/infrastructure/entity_taxonomy.py). New `GET /api/v1/search/categories` endpoint exposes `[{"id": X, "label": "..."}]` to the frontend dropdown. Option A vs B (where the authoritative taxonomy lives long-term) **deferred to product/UX** — see semantic distinction below. |
| §3 Phase 04 — search pass-through | **Shipped by compute (commit `639d753`)** | `purpose` + `blacklist_entry_id` echo on `search:results` with `event_type` unchanged. `compute.error` **deliberately does not carry** these fields — backend must route blacklist-embed failures by `entity_id` lookup. See [../image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md) §Error routing. |

**Stop-gap on §2 (shipped 2026-04-20):**

Frontend was blocked on having *some* category data flowing. We now:

1. Translate `entities: list[int]` → DB `category TEXT` as compact JSON (`"[2,5]"`) for distinct-value queries.
2. Translate `entities` → Qdrant payload `category: list[str]` (`["2", "5"]`) for `MatchAny` filtering.
3. Resolve ids to labels via a small hardcoded map ([entity_taxonomy.py](../../src/infrastructure/entity_taxonomy.py)) — currently 6 known classes + `"unk"` fallback.
4. Expose `GET /api/v1/search/categories` so the frontend doesn't hardcode the map on its side.

**Why this is still a stop-gap, not a final answer:**

The compute team flagged a semantic distinction we initially missed:

- **Option A (config-based):** `entities` reflects what a tenant is *configured* to monitor. A vehicle-monitoring tenant gets `category=["car"]` on every evidence regardless of content.
- **Option B (content-based):** Some other field (e.g. `classes_detected` from the YOLO detector) reflects what the detector *actually saw*. A vehicle-monitoring tenant only gets `category=["car"]` if a car was visible in the frame.

For a user-facing search filter ("show me images with vehicles"), Option B semantics is almost certainly what users expect. But that requires ETL to forward `classes_detected` and a separate negotiation with that team.

**Until product/UX resolves which semantic to ship long-term**, the stop-gap above gives us a working dropdown today using `entities` (Option A semantics, with stop-gap labels). The endpoint and filter contracts won't change when we eventually swap source-of-truth — only the consumer's translation logic changes.

Next actions:
- **Product/UX:** confirm whether category search should mean "configured to watch X" or "actually saw X".
- **Platform team (long-term):** expose `t_configurations.entities` as a readable endpoint or shared DB view so we can replace the hardcoded label map with a live cache.
- **ETL team (only if Option B wins):** add `classes_detected: list[str]` to the `evidence:embed` publish payload.

---

## 1. Context

We've landed two features that depend on fields moving through your service unchanged:

1. **Category filter (already live on our side).** The ETL producer can tag each evidence with a `category` string. The backend now stores it and filters similarity searches by it. **But categories aren't actually reaching us yet** because your [`evidence_handler.py`](../../../image-embedding-compute/src/streams/evidence_handler.py) reads input fields by explicit name and drops unknown ones. Until `category` joins your explicit pass-through list, our Phase 01 work is effectively inert on the ingest path. **This is a retroactive cleanup item** — please include it in the next release you cut.

2. **Image blacklist (shipping now, Phase 04 blocked on you).** We need to embed blacklist reference images through your existing `evidence:search` → `search:results` flow with a discriminator that tells our consumer "store this as a blacklist point, don't run a user search". We reuse your pipeline instead of asking you to build a new one. The cost on your side is echoing two new fields — you don't interpret them, just pass them through.

Total engineering impact on you:
- **~2 lines** in `evidence_handler.py` (add `category` to the pass-through names)
- **~4 lines** in `search_handler.py` (read + echo `purpose` and `blacklist_entry_id`)
- **A commit to your CONTRACT.md** documenting both additions

No behavior changes to the search/embed logic itself. No new streams. No new envelope `event_type` values.

---

## 2. Phase 01 addition — evidence category pass-through *(stop-gap shipped; long-term semantic still pending)*

### Negotiation history

Compute shipped `entities: list[int]` forwarded byte-identical from `t_configurations.entities` (commit `fb28d8e`, [evidence_handler.py:80-83, :145](../../../image-embedding-compute/src/streams/evidence_handler.py)). Compute's reasoning: taxonomy ownership sits upstream, not in compute. Backend agreed with the principle but flagged a shape mismatch — our pipeline expected a human-readable `category: str`, and opaque `list[int]` IDs would leak taxonomy identifiers into a user-facing search API.

Compute's reply (`BACKEND_INTEGRATION.md`) surfaced a deeper issue we initially missed: **Option A (config-based) and Option B (content-based) return different data**. A tenant configured for `entities=[person, vehicle]` whose detector only saw a person:
- Option A → tagged `category=["person", "vehicle"]` (config inheritance)
- Option B → tagged `category=["person"]` (what was actually seen)

For a user-facing search filter, Option B semantics is almost certainly what users expect — but Option B requires an ETL change we haven't negotiated yet.

### Stop-gap shipped on backend (2026-04-20)

Rather than block frontend on the semantic decision, we shipped a working dropdown today using `entities` (Option A semantics). The contracts are stable across an eventual swap to Option B — only the consumer's translation logic changes.

**What landed on backend:**

| File | Change |
|---|---|
| [src/infrastructure/entity_taxonomy.py](../../src/infrastructure/entity_taxonomy.py) | New — hardcoded YOLO/COCO-80 label map (6 known classes + `"unk"` fallback). Documented as STOPGAP in the module docstring. |
| [src/streams/embedding_results_consumer.py](../../src/streams/embedding_results_consumer.py) | Reads `payload["entities"]`, sorts + dedupes, writes DB `category TEXT` as compact JSON (`"[2,5]"`) and Qdrant payload `category: list[str]` (`["2", "5"]`). |
| [src/main.py](../../src/main.py) | New `GET /api/v1/search/categories` endpoint — returns `[{"id": X, "label": "..."}]` for the frontend dropdown, multi-tenant scoped via `X-User-Id`. |

**Filter UX:** frontend renders the labels from the categories endpoint, sends the stringified id (e.g. `category=["2"]`) on `POST /api/v1/search`. Qdrant `MatchAny` against the keyword[] payload matches points whose category list contains any of the requested ids.

### What still needs to happen

| Party | Action | When |
|---|---|---|
| Product / UX | Confirm: does "search by category=vehicle" mean "configured to watch vehicles" (Option A) or "vehicle visible in image" (Option B)? Implementation pivot is on the consumer side only. | Before peak of feature usage; not blocking initial rollout. |
| Platform team | Expose `t_configurations.entities` as a readable endpoint or shared DB view. Replaces the hardcoded `entity_taxonomy.py` map with a runtime-cached lookup. | Anytime — the stop-gap holds in the meantime. |
| ETL team (Option B only) | Add `classes_detected: list[str]` to the `evidence:embed` publish payload. Compute will pass it through; backend swaps source field. | Conditional on Option B winning. |
| Compute team | **Nothing more required.** `fb28d8e` is the final shape regardless of which option wins. | — |

### What we originally asked for *(still the target shape after either resolution lands)*

On the `evidence:embed` → `embeddings:results` path (and the alternate `embeddings:results:weapon_analysis` routing), recognize and forward an optional top-level `category` string.

### Your contract today

Per [your CONTRACT.md §2.2](../../../image-embedding-compute/docs/CONTRACT.md), `evidence_handler.py` reads these fields by explicit name:

> `evidence_id`, `zip_url`, `camera_id`, `user_id`, `device_id`, `app_id`, `infraction_code`, `deep_processes`

Everything else is dropped. Add `category` to the list.

### The input field

Add to your §2.2 "Input payload" table:

| Field | Type | Required | Notes |
|---|---|---|---|
| `category` | string \| null | no | Human-assigned evidence category (`"vehicle"`, `"scene"`, `"infraction_pattern"`, or any free-form label). No enum. Null/absent = uncategorized. **Pass-through only** — you don't read or interpret it. |

### The output field

Add the same row to your §2.6 "Output payload" description — `category` is forwarded byte-identical to the corresponding output payload, both for `embeddings:results` and for `embeddings:results:weapon_analysis` when weapons routing is active.

### Why this is the bug-shaped one

We already:
- Added a migration + column (`embedding_requests.category`)
- Added a Qdrant payload index on `category`
- Wired the consumer to read `payload["category"]` and write it to the DB + Qdrant
- Exposed a `category` filter on the search API

All of that is live, tested, and shipped. **It just has no data to operate on**, because you drop `category` from the producer payload before we see it. As soon as you add one line, categories start flowing and every downstream feature "just works".

Ops can verify with:
```bash
# Before your fix: returns 0
SELECT COUNT(*) FROM embedding_requests WHERE category IS NOT NULL;
# After your fix: count climbs as new evidence comes in
```

---

## 3. Phase 04 addition — `purpose` + `blacklist_entry_id` on `evidence:search` *(shipped)*

**Status:** shipped by compute on 2026-04-20 as commit `639d753`. [search_handler.py:63-72, :92-94](../../../image-embedding-compute/src/streams/search_handler.py) now reads + echoes both fields with `event_type` unchanged. Legacy callers unchanged. Backend Phase 04 unblocked for end-to-end verification.

**One gotcha — `compute.error` does NOT carry `purpose` / `blacklist_entry_id`.** Compute's reasoning: the failure is structurally identical regardless of intent, so they chose not to echo the routing fields on the error envelope. That's defensible from their side, but it means the backend consumer must route blacklist-embed failures by `entity_id` alone — looking up whether the id exists in `blacklist_image_references` vs `search_requests`. Noted in [../image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md) §Error routing for the implementation.

### What we need

On the `evidence:search` → `search:results` path, recognize and echo two new fields. You don't change how you embed or publish — just add them to the fields you read on input and to the fields you write on output.

### Your contract today

Per [search_handler.py:63-92](../../../image-embedding-compute/src/streams/search_handler.py#L63-L92), you read on input:

> `search_id`, `user_id`, `image_url`, `threshold`, `max_results`, `metadata`

And publish on output:

> `event_type: "search.vector.computed"`, `search_id`, `user_id`, `vector`, `threshold`, `max_results`, `metadata`

We're adding two fields to both sides. `event_type` stays `"search.vector.computed"` — no envelope change.

### The input additions

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `purpose` | `"search"` \| `"blacklist_embed"` | no | `"search"` | Discriminator telling our backend consumer what to do with the returned vector. You do not interpret it — just echo it to `search:results`. Legacy callers that don't send it behave exactly as today. |
| `blacklist_entry_id` | string (UUID) | no | `null` | Present only when `purpose="blacklist_embed"`. Opaque echo value — our backend uses it to attribute the returned vector to a specific blacklist entry. Pass-through only. |

### The output additions

Both fields are echoed byte-identical on `search:results`:

```json
{
  "event_type": "search.vector.computed",
  "search_id": "...",
  "user_id": "...",
  "vector": [...],
  "threshold": 0.75,
  "max_results": 50,
  "metadata": { ... },
  "purpose": "blacklist_embed",        // echoed from input, "search" if absent
  "blacklist_entry_id": "..."          // echoed from input, null if absent
}
```

### Implementation sketch

In [search_handler.py](../../../image-embedding-compute/src/streams/search_handler.py), the change is mechanical. Roughly:

```python
async def _process_search(payload: Dict, message_id: str):
    search_id = payload.get("search_id", "")
    user_id = payload.get("user_id", "")
    image_url = payload.get("image_url", "")
    purpose = payload.get("purpose", "search")              # NEW
    blacklist_entry_id = payload.get("blacklist_entry_id")  # NEW

    # ... existing validation + vector generation ...

    await _producer.publish(output_stream, {
        "event_type": "search.vector.computed",
        "search_id": search_id,
        "user_id": user_id,
        "vector": vector,
        "threshold": payload.get("threshold", 0.75),
        "max_results": payload.get("max_results", 50),
        "metadata": payload.get("metadata"),
        "purpose": purpose,                                 # NEW
        "blacklist_entry_id": blacklist_entry_id,           # NEW
    })
```

~4 lines of actual code change.

### One important semantic note — `search_id` overloading

When the backend sends a message with `purpose="blacklist_embed"`, `search_id` carries a `BlacklistImageReference.id` (the UUID of the specific reference image on our side), not a regular search request ID. You **don't need to care** about this — you treat `search_id` as an opaque echo value and pass it through unchanged, which is exactly what you do today. Our consumer decides what table to look it up in based on the `purpose` value.

We considered adding a separate `reference_id` field to avoid the overloading, but decided the opacity on your side makes it not worth the extra field. Your contract should note (in §3 of your search flow section): *"`search_id` is an opaque echo value. When `purpose="blacklist_embed"`, it carries a different kind of ID, but the semantics from the compute service's perspective are identical."*

---

## 4. What we need from you

| # | Task | File | Status |
|---|---|---|---|
| 1 | ~~Add `category` to the explicit pass-through list~~ → superseded: compute shipped `entities: list[int]` in `fb28d8e`. Resolution chosen: platform exposes the taxonomy table; backend does the translation. **No further action on compute.** | [`src/streams/evidence_handler.py`](../../../image-embedding-compute/src/streams/evidence_handler.py) | **Done on compute side** |
| 2 | Add `purpose` + `blacklist_entry_id` to the read + echo list | [`src/streams/search_handler.py`](../../../image-embedding-compute/src/streams/search_handler.py) | **Shipped — commit `639d753`** |
| 3 | Update your CONTRACT.md §2.2 (input), §2.6 (output), §3 (search flow) | `docs/CONTRACT.md` | Done for both §2 (`fb28d8e`) and §3 (`639d753`) |
| 4 | One release / deploy | — | your call |

That's it. No migrations, no new streams, no new consumer groups.

Please treat these as an **additive patch release** — no breaking changes, no behavior differences for existing callers. Once deployed we flip a feature flag on our side (we don't actually have one; we'd just start sending `category` from the ETL + start using the `purpose` field in our new API endpoint — both graceful).

---

## 5. What's already live on our side

So you know what's waiting for you.

| Our side | State | Blocked on you? |
|---|---|---|
| `embedding_requests.category` column + B-tree index | Live (migration `d4a9b7c3e5f8`) | — |
| Qdrant payload index on `category` | Live (idempotent on startup) | — |
| `embedding_results_consumer` translating `payload["entities"]` → `category` | Live (stop-gap) | **No more compute work needed.** Backend reads `entities`, stores DB JSON-string + Qdrant keyword list, exposes labels via `GET /api/v1/search/categories`. Hardcoded YOLO map at [entity_taxonomy.py](../../src/infrastructure/entity_taxonomy.py) — replace with platform-fed cache when available. |
| `GET /api/v1/search/categories` endpoint | Live | Returns `[{"id": X, "label": "..."}]` for the frontend dropdown. Multi-tenant scoped. |
| `SearchCreateRequest.category` field on `POST /api/v1/search` | Live | No (works within our own data) |
| Filter allow-list in search_results_consumer + recalc | Live | — |
| Blacklist 3-table SQL spine | Live (migration `e7f2c9a1b3d6`) | — |
| `source_type_filter` helper + `blacklist_entry_id` Qdrant index | Live | — |
| Blacklist consumer + publisher (`purpose`-aware dispatch) | **Pending our Phase 04** | **Yes** — dispatches on `purpose` which you must echo |
| CRUD API for blacklist entries / references | Pending our Phase 06 | No |

**Thing 1** is the one that currently produces visible user value once you ship — the `category` filter stops being silent. **Thing 2** unblocks Phase 04, which is prerequisite for Phases 05–07.

---

## 6. Open questions — please confirm

1. ~~Taxonomy bridge for `entities`.~~ **Resolved 2026-04-20 — Option 1 (platform taxonomy endpoint).** Next step is a platform-team ask, not a compute ask. Compute: nothing more needed here.

2. ~~Timeline for the `purpose` echo.~~ **Resolved — shipped as `639d753`.** Backend can proceed with Phase 04 verification against the updated compute build.

3. **Dead-letter behavior for blacklist-embed failures.** Today if CLIP inference fails for a regular search, you publish a `compute.error` envelope to `search:results`. Same path for blacklist embeds? We'd expect yes — the failure is identical from your perspective. We'll handle it on our side by marking the `BlacklistImageReference.status = 4 (ERROR)` and logging.

4. **Anything else you'd like us to echo on your side for observability.** We're fine adding fields both directions if it helps you debug — e.g. a `correlation_id`, a `retry_count`, etc. Say the word.

---

## 7. How to cross-check when you deploy

After your release, we can jointly verify with two quick probes:

### Thing 1 — evidence pass-through

As of 2026-04-20, compute forwards `entities: list[int]` (not `category: str`). Probe shape depends on which §2 resolution lands:

```bash
# If resolution (a) — taxonomy map endpoint — ETL tags an evidence with entity id(s), we translate on read.
# Probe the raw forwarded field first:
rcli XREVRANGE embeddings:results + - COUNT 3 | grep -A1 entities
# Expected: "entities": [2]  (or similar list of ints) appears in the payload.

# Then, once our ingest translates via the cached map, query our DB for the resolved label:
psql -h <our-db> -c "SELECT category FROM embedding_requests WHERE category IS NOT NULL LIMIT 5;"
# Expected: human-readable labels, not ints.

# If resolution (b) — ETL forwards classes_detected — the probe is simpler:
rcli XREVRANGE embeddings:results + - COUNT 3 | grep -A1 classes_detected
# Expected: "classes_detected": ["vehicle"] (or similar list of strings).
```

### Thing 2 — `purpose` echo

```bash
# Our backend publishes to evidence:search with purpose="blacklist_embed".
# Inspect the search:results stream for the echoed field.
rcli XREVRANGE search:results + - COUNT 3 | grep -A1 purpose
# Expected: "purpose": "blacklist_embed" appears in the echoed payload.
```

If both probes green, our Phase 04 implementation on this side can proceed to end-to-end testing.

---

## 8. Summary for your PR description

Suggested copy-paste commit / PR title and body if it helps:

> **`feat(streams): pass through category on evidence + purpose/blacklist_entry_id on search`**
>
> Per `image-embeeding-service/docs/requirements/IMAGE_COMPUTE_STREAMS.md`.
>
> - `evidence_handler.py`: add `category` to the explicit pass-through list so ETL-supplied categories reach the backend through both `embeddings:results` and `embeddings:results:weapon_analysis`.
> - `search_handler.py`: read + echo `purpose` (default `"search"`) and `blacklist_entry_id` on `search:results`. Enables the backend to dispatch vectors from this stream to either the search flow or the blacklist-reference storage flow. We do not interpret the fields.
> - `docs/CONTRACT.md`: document both additions. No envelope changes, no new streams, additive only.

---

**Point of contact on our side:** the committer of [our blacklist plan commit `bffcb6b`](../../docs/image-blacklist/). Open a PR comment when you're ready and we'll coordinate the rollout.
