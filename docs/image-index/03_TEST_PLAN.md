# Image-Index Search + Blacklist Cross-Ref — E2E Test Plan

How we validate the two new capabilities (design: [`02_SEARCH_DESIGN.md`](02_SEARCH_DESIGN.md)) end-to-end
against the deployed prod pipeline, the same way we validated v1/v1.1/v1.2 (skill: `image-index-e2e`).

- **A** — async search-by-image over a list of `external_ids`.
- **B** — GPU-free blacklist cross-reference (REST endpoint + auto-on-land hook).

> Status: **PREP** — written while the delegate-build runs. Endpoint request/response shapes lock to the
> built code; this plan is the scenarios + test data + expected results + pass/fail criteria.

---

## 1. Test philosophy — deterministic self-match first

CLIP cosine on the **identical image** is ~**1.0**. So the backbone of every scenario is a **self-match**:
use the *same public image URL* as both the query (or blacklist reference) **and** one of the indexed
items. The match is then deterministic — it MUST come back at score ≈ 1.0 — which validates the whole
plumbing (embed → store → filtered search → land → recover) without depending on a tuned threshold.

- **Positive (must match):** query URL == an indexed URL → score ≈ 1.0, well above any threshold.
- **Negative (must NOT match above threshold):** a visually distinct indexed image → low score.
- **Precision/recall tuning** (near-dups vs distinct) needs a richer image set — see §2.

---

## 2. Test data

**Public URLs we have** (compute fetches public http(s) — local `data/inputs/*` do NOT work):
- `IMG_A = https://storage.lookia.mx/lucam-assets/kept_000001.png`
- `IMG_B = https://storage.lookia.mx/lucam-assets/kept_000002.png`

Two URLs are enough for the deterministic self-match + one negative. `kept_000001`/`kept_000002` are
consecutive dedup-kept crops, so they may be **near-duplicates** (useful for a realistic threshold check)
or distinct — record the observed query-vs-other score on the first run and pin it.

**To enrich (optional, for precision):** upload a handful of `data/inputs/*` (distinct persons/vehicles)
to storage → get public URLs → gives clean negatives + a same-object-different-frame near-dup pair.
Do this only if the 2-URL set proves too coarse.

**Run ids (external_ids) for isolation:**
- `E1 = tplan-search-<ts>` — batch `[IMG_A, IMG_B]`
- `E2 = tplan-search-<ts>-2` — batch `[IMG_B]` (a second run, to prove scoping/aggregation)

**Tenant:** the storage key's tenant `3996d660-…` (user `davis`), discovered via `GET /api/v1/users/me`.

---

## 3. Preconditions (verify before any scenario)

| Check | How | Expect |
|---|---|---|
| Feature deployed + flags on | (ops) `IMAGE_INDEX_ENABLED=true`, `IMAGE_INDEX_SEARCH_ENABLED=true`; for §7, `image_index_blacklist_autocheck_enabled=true` | — |
| Compute live | `python3 scripts/test_e2e_image_index.py --groups` | `image-index-compute-workers` present |
| Search route reachable | `GET …/embedding/image-index/search/__probe__` with key | our JSON, not gateway "Route not found". **✅ Confirmed: no new gateway route needed** — the existing `"/api/v1/embedding/image-index" → "/api/v1/image-index"` prefix-match already covers `/search`. |
| Blacklist route reachable | `GET https://api.lookia.mx/api/v1/images/blacklist` with key | list JSON. **✅ Confirmed gateway prefix:** `"/api/v1/images/blacklist" → "/api/v1/blacklist/image-entries"`. So the new `POST …/images/blacklist/{id}/cross-reference` is **prefix-covered — no new gateway route needed** either. |
| Cloudflare | send `User-Agent: curl/8.4.0` on every HTTP call | no 403 code 1010 |

---

## 4. Setup — index the searchable batches (reuse the proven flow)

Submit the two index batches over Redis (this part is already-verified v1 machinery):

```bash
# E1 = [IMG_A, IMG_B]  (Redis submit + wait for image_batch.completed)
python3 scripts/test_e2e_image_index.py --redis-only \
  --user-id 3996d660-99c2-4c9e-bda6-4a5c2be7906e --external-id "$E1" \
  "$IMG_A" "$IMG_B"
# E2 = [IMG_B]
python3 scripts/test_e2e_image_index.py --redis-only \
  --user-id 3996d660-… --external-id "$E2" "$IMG_B"
```
Confirm each `image_batch.completed` with `embedded == submitted`. Now `image_index_embeddings` holds
IMG_A@E1, IMG_B@E1, IMG_B@E2, each with a stored 512-D vector + `model_version="clip-vit-b-32"`.

---

## 5. Scenario A1 — Capability A search happy path

```bash
# SUBMIT the search (query = IMG_A), scoped to [E1, E2]
curl -sS -X POST "https://api.lookia.mx/api/v1/embedding/image-index/search" \
  -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0" -H "Content-Type: application/json" \
  -d "{\"image_url\":\"$IMG_A\",\"external_ids\":[\"$E1\",\"$E2\"],\"threshold\":0.75,\"max_results\":50}"
# → 202 { "search_id": "..." }

# POLL status until terminal (compute embeds the query → we search)
curl -sS "https://api.lookia.mx/api/v1/embedding/image-index/search/<search_id>" -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0"
# → status: pending|working → completed ; similarity_status: matches_found

# MATCHES
curl -sS "https://api.lookia.mx/api/v1/embedding/image-index/search/<search_id>/matches" -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0"
```
**PASS criteria:**
- Terminal `completed`.
- Matches include **IMG_A @ E1 with score ≈ 1.0** (the self-match) — top result.
- Each match is **tagged with its `external_id`** (E1) + `evidence_id`/`image_id` + `source_url` + `batch_id` + score.
- IMG_B appears only if its score ≥ threshold (record the score); results only from `{E1,E2}`.
- The frontend can read the state + relations from the reused `search_requests`/`search_matches` rows.

---

## 6. Scenario A2 — Capability A negatives / IDOR / edges

| # | Action | Expect |
|---|---|---|
| A2.1 | Search with `external_ids:["not-owned-run"]` | `completed`, **0 matches** (scope filter) |
| A2.2 | Search with a **foreign tenant's** external_id (different key/tenant) | 0 matches / 404 on read — **no cross-tenant leak** |
| A2.3 | `threshold: 0.99` | only the self-match (≈1.0) survives; near-dups drop |
| A2.4 | `external_ids` of length 201 | `422`/`400` (cap = 200) |
| A2.5 | Search with `IMAGE_INDEX_SEARCH_ENABLED=false` | **503** on POST |
| A2.6 | Query URL that 404s | search still terminalizes (`completed`/`error`), no hang (reaper backstop) |
| A2.7 | Read another user's `search_id` | **404** (tenant-scoped `get_by_search_id`) |

---

## 7. Scenario B1 — Capability B blacklist cross-ref (REST, GPU-free)

**Setup the blacklist profile (the "create a blacklist profile for our test images" step).**
Public base `https://api.lookia.mx/api/v1/images/blacklist` (gateway rewrites → `/api/v1/blacklist/image-entries`):
```bash
BL=https://api.lookia.mx/api/v1/images/blacklist
# 1) create the entry
curl -sS -X POST "$BL" \
  -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0" -H "Content-Type: application/json" \
  -d '{"name":"tplan blacklist IMG_A","match_threshold":0.85}'
# → { "id": "<entry_id>", "status": 1 (CREATED) }

# 2) attach IMG_A as a reference (triggers ASYNC compute embed)
curl -sS -X POST "$BL/<entry_id>/references" \
  -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0" -H "Content-Type: application/json" \
  -d "{\"image_url\":\"$IMG_A\"}"

# 3) POLL the entry until the reference embeds → entry status 3 (INDEXED)
curl -sS "$BL/<entry_id>" -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0"
# wait for reference status = embedded, entry status = 3
```
Now the blacklist vector for IMG_A lives in `evidence_embeddings` (`source_type=blacklist`) with
`model_version="clip-vit-b-32"`, and IMG_A is ALSO indexed at E1 (§4).

**Cross-reference** (public `POST …/images/blacklist/{entry_id}/cross-reference`):
```bash
curl -sS -X POST "$BL/<entry_id>/cross-reference" \
  -H "X-API-Key: $KEY" -H "User-Agent: curl/8.4.0" -H "Content-Type: application/json" \
  -d "{\"external_ids\":[\"$E1\",\"$E2\"],\"threshold\":0.85}"
# → SYNC inline (no poll): matches: [ { external_id: E1, batch_id, item_index, image_id, source_url, score≈1.0, blacklist_entry_id } ]
```
**PASS criteria:**
- **Sync 200 with the IMG_A@E1 match at score ≈ 1.0** — "this blacklisted image appears in run E1".
- **GPU-free** — no `evidence:search`/`image:embed` XADD happens during the xref (the vectors already exist). *(Verify: watch the embed streams' xlen — unchanged; or check no new search_requests row.)*
- Tenant-scoped: another tenant's `entry_id` → 404; foreign `external_id` → not in results.
- `model_version` match (both stamped `clip-vit-b-32`).

---

## 8. Scenario B2 — Capability B auto-on-land hook

With `image_index_blacklist_autocheck_enabled=true` and the IMG_A blacklist entry INDEXED (§7):
```bash
# index a NEW batch that CONTAINS the blacklisted image
python3 scripts/test_e2e_image_index.py --redis-only \
  --user-id 3996d660-… --external-id "tplan-auto-<ts>" "$IMG_A" "$IMG_B"
# watch the report stream for the emitted event
```
**PASS criteria:**
- After the batch lands (`image_batch.completed`), an **`image:blacklist_match`** event is published with the
  **additive fields** `match_target="image_index"`, `external_id`, `batch_id` (the existing evidence-path
  event shape is byte-unchanged).
- The hook fires **AFTER** the terminal lifecycle publish, is **fire-and-forget** (never blocks/delays land),
  and **fast-exits** when the tenant has no active blacklist entries.
- No match event for a batch with only distinct (non-blacklisted) images.

---

## 9. Non-regression (must stay green throughout)

- The live **evidence `POST /api/v1/search`** (search over `evidence_embeddings`) still works, unchanged.
- The existing **blacklist reverse-search** (evidence path) still fires `image:blacklist_match` as before.
- `land_computed` latency for a normal index batch is unchanged when the auto-hook flag is OFF.
- Turn `IMAGE_INDEX_SEARCH_ENABLED=false` → the new routes 503; everything else behaves exactly as v1.2.

---

## 10. Automation

Extend the `image-index-e2e` skill / `scripts/test_e2e_image_index.py` with (finalize against the built shapes):
- `--search --query <url> --external-ids E1,E2` — submit A, poll, verify the self-match + scoping.
- `--xref --entry <id> --external-ids E1,E2` — call B, assert the match + assert zero embed-stream growth (GPU-free proof).
- A blacklist-setup helper (create entry → add reference → poll INDEXED) so B is one command.

Report per run: preconditions, the search_id + terminal status + top match score + external_id tags, the
xref matches + the GPU-free assertion, the auto-hook event, and any non-regression check. **Never print the API key.**

---

## 11. Cleanup

Test artifacts are cheap + tagged: `search_requests`/`search_matches` rows (by tenant), `image_index_embeddings`
points (by `external_id` `tplan-*`), and the blacklist entry (`DELETE /api/v1/blacklist/image-entries/{id}`
cascades its refs + Qdrant points). Purge the blacklist test entry after; index rows age out / purge by `external_id`.
