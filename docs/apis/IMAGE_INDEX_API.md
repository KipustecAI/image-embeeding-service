# Image Index API — On-demand CLIP Image Indexing (Integration Guide)

**Audience:** frontends / REST clients (and implementing agents) recovering the results of an on-demand **image-index** batch. This is the read side of the flow; the Redis submit side (for no-HTTP coordinators) is [IMAGE_INDEX_SUBMIT.md](./IMAGE_INDEX_SUBMIT.md).

**What it does:** a coordinator (dw-offline, as its **4th enrichment target** `target="image"`) submits a batch of **public image URLs** — e.g. detection crops from a run. The GPU embeds each image with **CLIP ViT-B-32 (512-D)**, we store the vector in a dedicated Qdrant collection plus a per-item reference row. You then **(a) recover results by id** (§3/§4), **(b) search by a query image** over a set of runs (§5), and **(c) cross-reference a blacklist entry** against those runs, GPU-free (§6).

> ✅ **STATUS — ALL LIVE + verified end-to-end in production (2026-07-23).**
> - **Recovery (§3/§4):** a real batch round-tripped submit → GPU embed → land → **gateway READ 200** (`submitted:2 → embedded:2`, each item a `qdrant_point_id`).
> - **Search by image (§5):** a real query returned a **self-match `score 0.9999998`** in the correct run, scoped to the requested `external_ids`.
> - **Blacklist cross-reference (§6):** a blacklisted image was found in a run at **`similarity_score 0.9999998`**, **GPU-free** (no re-embed). This also proved live that the blacklist and indexed vectors share the same CLIP space (cross-collection cosine is valid).
>
> All behind `IMAGE_INDEX_ENABLED` + `IMAGE_INDEX_SEARCH_ENABLED` (both prod-default true); IDOR-scoped by the gateway `X-User-Id`.

---

## 1. Base URL & Auth

```
Base URL (via API Gateway):  https://api.lookia.mx
```

Authenticate with your **API key** only. The gateway validates it, resolves your identity, and **injects** the user-context headers (`X-User-Id`, `X-User-Role`) — you do **not** send those yourself.

| Header | Value |
|---|---|
| `X-API-Key` | `<your_api_key>` |

> **Tenant isolation is derived from your API key.** Every read is scoped to the tenant the gateway resolves. **The `user_id` the coordinator submitted with must equal this tenant**, or you get `404`.

> **Gateway routing:** `/api/v1/embedding/image-index/*` → `/api/v1/image-index/*` is **registered**
> (sibling of `/api/v1/embedding/{search,stats,pipeline,recalculate}`; `api-gateway` deployed it
> 2026-07-23). The gateway strips the `/embedding` segment and forwards to `ms-embedding-api:8001`.
> A `{"detail":"Route not found: …"}` would be a **gateway** 404 (route missing) — distinct from our
> tenant-miss `404` (`{"detail":"No runs..."}`).
>
> ⚠️ **Cloudflare:** the gateway sits behind Cloudflare, which **403s (error code 1010)** a request
> with a default `Python-urllib` User-Agent. Send a normal `User-Agent` header (`curl/*`, a browser
> UA, or any real client string). `curl` and browsers pass by default.

---

## 2. Flow at a glance

**Recover a batch's results** (indexing done by a coordinator over Redis):
```
(coordinator) XADD image:index:submit        → we mint a batch → dispatch to GPU
        │
        ▼  (GPU embeds each URL with CLIP)
GET /api/v1/image-index/results/{batch_id}                     (recover by OUR id)   → §3
GET /api/v1/image-index/results/by-external-id/{external_id}   (recover by YOUR id)  → §4
```

**Search + cross-reference the indexed images** (the two new query capabilities):
```
POST /api/v1/embedding/image-index/search                      (search by an image, over a set of runs)  → §5
POST /api/v1/images/blacklist/{entry_id}/cross-reference       (does my blacklist appear in these runs?)  → §6
```

Poll until `status` is terminal: `completed`, `completed_with_errors`, or `error`.

---

## 3. `GET /api/v1/image-index/results/{batch_id}` — recover by our id

### Query params

| Param | Type | Default | Range | Description |
|---|---|---|---|---|
| `include_items` | bool | `false` | — | include the per-item rows (else counts-only) |
| `limit` | int | `100` | 1–500 | page size for `items` |
| `offset` | int | `0` | ≥0 | page offset for `items` |

### Response

| Field | Type | Description |
|---|---|---|
| `batch_id` | string | our minted id |
| `external_id` | string \| null | your run id |
| `client_batch_ref` | string \| null | your per-submit unique ref |
| `status` | string | `pending`｜`computing`｜`completed`｜`completed_with_errors`｜`error` |
| `counts` | object | `{submitted, embedded, filtered, failed}` |
| `source_ref` | string \| null | free-form origin |
| `created_at` / `completed_at` | ISO-8601 \| null | |
| `error_message` | string \| null | set when `status=error` |
| `items` | array | per-item rows — **only when `include_items=true`** |

**`items[]` element**

| Field | Type | Description |
|---|---|---|
| `item_ref` | string | **your `item_id`** (or `image_id`), echoed |
| `image_id` | string | alias of `item_ref` — same value, for parity with face/plates/analysis |
| `source_url` | string | the submitted image URL, echoed on **every** item — including failed ones (so a `download_failed` row still tells you which URL failed). `null` only in the rare case the caller omitted a URL. |
| `item_index` | int | position in the submitted array (0-based) |
| `status` | string | `embedded`｜`download_failed`｜`decode_failed`｜`no_result` |
| `qdrant_point_id` | string \| null | stable vector id (deterministic; safe to store). Set iff `embedded`. |
| `duplicate_of_index` | int \| null | **always `null` in v1** (dedup disabled) |
| `error_message` | string \| null | detail on a failed disposition |

**Counts invariant:** `submitted == embedded + failed`, where `failed` folds `download_failed + decode_failed + no_result`. **`filtered` is always `0` in v1** — diversity dedup is disabled for this flow, so every submitted image is embedded or a real failure.

> **There is no `matched` field.** Unlike face-index, this flow does **not** inline-match against a blacklist — it stores searchable vectors.

### Example

```bash
curl "https://api.lookia.mx/api/v1/embedding/image-index/results/49c7861d-…?include_items=true" \
  -H "X-API-Key: <your_api_key>"
```

```json
{
  "batch_id": "49c7861d-…",
  "external_id": "run-42",
  "client_batch_ref": "dwoff-run42-image-b7",
  "status": "completed_with_errors",
  "counts": { "submitted": 3, "embedded": 2, "filtered": 0, "failed": 1 },
  "source_ref": "run-42/Vehículo",
  "created_at": "2026-07-22T20:31:07.016197",
  "completed_at": "2026-07-22T20:31:19.241892",
  "error_message": null,
  "items": [
    { "item_ref": "3551", "image_id": "3551", "source_url": "https://storage.lookia.mx/.../crop_000.png",
      "item_index": 0, "status": "embedded",
      "qdrant_point_id": "16d1a741-…", "duplicate_of_index": null, "error_message": null },
    { "item_ref": "3552", "image_id": "3552", "source_url": "https://storage.lookia.mx/.../crop_001.png",
      "item_index": 1, "status": "embedded",
      "qdrant_point_id": "8b3d55b7-…", "duplicate_of_index": null, "error_message": null },
    { "item_ref": "3553", "image_id": "3553", "source_url": "https://storage.lookia.mx/.../crop_002.png",
      "item_index": 2, "status": "download_failed",
      "qdrant_point_id": null, "duplicate_of_index": null, "error_message": "http 404" }
  ]
}
```

---

## 4. `GET /api/v1/image-index/results/by-external-id/{external_id}` — recover by YOUR id

Look up results with the `external_id` the coordinator submitted (dw-offline uses the `run_id`) — no need to store our `batch_id`.

### Query params

| Param | Type | Default | Description |
|---|---|---|---|
| `all` | bool | `false` | `false` = most-recent run only; `true` = every run, newest-first |
| `include_items` | bool | `false` | include per-item rows (**single-run mode only** — always empty in the `?all` list) |
| `limit` / `offset` | int | `100` / `0` | pagination for `items` (limit 1–500) |

- **`external_id` is non-unique** — a class with >100 crops fans out to several batches under one `external_id`, and re-running creates new ones. The default returns the **most recent**; `?all=true` returns every run, newest first.
- **`?all=true` is bounded at 200 runs** — the deliberate cap that keeps a large backfill from returning an unbounded list.

### Example — most recent run

```bash
curl "https://api.lookia.mx/api/v1/embedding/image-index/results/by-external-id/run-42?include_items=true" \
  -H "X-API-Key: <your_api_key>"
```
→ same body shape as §3.

### Example — all runs (`?all=true`)

```json
{
  "external_id": "run-42",
  "count": 2,
  "batches": [
    { "batch_id": "49c7861d-…", "status": "completed",
      "counts": {"submitted": 100, "embedded": 100, "filtered": 0, "failed": 0}, "items": [] },
    { "batch_id": "1f0a92bb-…", "status": "completed_with_errors",
      "counts": {"submitted": 37, "embedded": 36, "filtered": 0, "failed": 1}, "items": [] }
  ]
}
```
> `items` is always `[]` in the list envelope — fetch a specific `batch_id` (§3) with `include_items=true` for per-item detail.

---

## 5. Search by image — find where a photo appears  (Capability A)  ✅ live

**"I have this image — which of my indexed runs contain it, and where?"** You submit a **query image**
+ the list of **`external_ids`** (runs) you want to search; we CLIP-embed the query on the GPU and return
the indexed images that match, each **tagged with the `external_id`/`batch_id` it came from**. It's **async**
(the query image has to be embedded) — submit → poll → read matches, exactly like the evidence search.

### 5.1 `POST /api/v1/embedding/image-index/search` → `202`

```bash
curl -sS -X POST "https://api.lookia.mx/api/v1/embedding/image-index/search" \
  -H "X-API-Key: <key>" -H "User-Agent: curl/8.4.0" -H "Content-Type: application/json" \
  -d '{ "image_url": "https://storage.lookia.mx/.../query.png",
        "external_ids": ["run-42", "run-99"],
        "threshold": 0.75, "max_results": 50 }'
# → 202 { "search_id": "599a2c04-…", "status": "pending", "message": "Search submitted, poll …/search/599a2c04-…" }
```

| Field | Type | Req | Default | Description |
|---|---|---|---|---|
| `image_url` | string | ✅ | | the query image — a durable public `http(s)` URL (the GPU fetches it directly) |
| `external_ids` | string[] | ✅ | | the runs to search — **1–200**; results come **only** from these, and only your tenant's |
| `threshold` | float | — | `0.75` | minimum cosine similarity (0–1). `1.0` = the identical image |
| `max_results` | int | — | `50` | cap on returned matches |

### 5.2 `GET /api/v1/embedding/image-index/search/{search_id}` — poll status

```jsonc
{ "search_id": "599a2c04-…", "status": "completed",        // pending | working | completed | error
  "similarity_status": "matches_found",                     // no_matches | matches_found
  "total_matches": 3, "threshold": 0.75, "max_results": 50,
  "external_ids": ["run-42","run-99"], "image_url": "https://…/query.png",
  "created_at": "…", "completed_at": "…", "error": null }
```
Poll every ~2 s until `status` is terminal (`completed` | `error`). A small search completes in ~1–2 s.

### 5.3 `GET /api/v1/embedding/image-index/search/{search_id}/matches?limit=&offset=` — read matches

Sorted by score (highest first). **Exactly the shape verified in prod:**
```jsonc
{ "search_id": "599a2c04-…", "total": 3, "limit": 50, "offset": 0,
  "matches": [
    { "image_id": "3551", "source_url": "https://storage.lookia.mx/.../crop_000.png",
      "score": 0.9999998, "external_id": "run-42", "batch_id": "8c644c17-…", "item_index": 0 },
    { "image_id": "3552", "source_url": "https://…/crop_001.png",
      "score": 0.9015, "external_id": "run-99", "batch_id": "e9d5cce9-…", "item_index": 0 }
  ] }
```

| Match field | Meaning |
|---|---|
| `image_id` | the indexed image's id — the **`evidence_id`** the coordinator submitted (maps back to a specific detection) |
| `source_url` | the matched image's URL |
| `score` | cosine similarity 0–1 (**~1.0 = the same image**) |
| `external_id` / `batch_id` / `item_index` | **which run + batch + position** the match is in |

**Frontend tips**
- **"Positivity per run":** each match carries its `external_id` — **group by `external_id`** to show *"run-42 has 2 matches, run-99 has 1"*.
- Pass only the `external_ids` the user has access to; a run they don't own (or another tenant's) simply returns **nothing** — no error, no leak.
- Use a higher `threshold` (e.g. `0.9`) for "the same object", lower (`0.7`) for "visually similar".

---

## 6. Blacklist cross-reference — is a blacklisted image in these runs?  (Capability B)  ✅ live

**"Does one of my blacklisted images appear in these indexed runs?"** This reuses your existing image
**blacklist** entries (see [BLACKLIST_API.md](../BLACKLIST_API.md)): we take the entry's already-stored
reference vector and search the indexed runs. It's **GPU-free** (nothing is re-embedded), so it's a
**single synchronous call — no polling.**

**Prerequisite:** the blacklist entry must be `INDEXED` (its reference image(s) embedded) — create it via
`POST /api/v1/images/blacklist` → attach references → wait for `status: 3`. See BLACKLIST_API.md.

### `POST /api/v1/images/blacklist/{entry_id}/cross-reference` → `200` (inline)

```bash
curl -sS -X POST "https://api.lookia.mx/api/v1/images/blacklist/<entry_id>/cross-reference" \
  -H "X-API-Key: <key>" -H "User-Agent: curl/8.4.0" -H "Content-Type: application/json" \
  -d '{ "external_ids": ["run-42","run-99"], "threshold": 0.85 }'
```
| Field | Type | Req | Default | Description |
|---|---|---|---|---|
| `external_ids` | string[] | ✅ | | runs to check — **1–200**, tenant-scoped |
| `threshold` | float | — | the entry's `match_threshold` (or global `0.85`) | minimum cosine similarity |
| `max_results` | int | — | 50 | cap |

**Response — the exact shape verified in prod:**
```jsonc
{ "entry_id": "e030c419-…", "external_ids": ["run-42","run-99"], "threshold_used": 0.85,
  "match_count": 1,
  "matches": [
    { "blacklist_entry_id": "e030c419-…", "blacklist_reference_id": "c9771d27-…",
      "external_id": "run-42", "batch_id": "8c644c17-…", "item_index": 0,
      "image_id": "3551", "source_url": "https://…/crop_000.png",
      "qdrant_point_id": "89441e20-…", "similarity_score": 0.9999998, "threshold_used": 0.85 } ] }
```
Each hit tells you the blacklisted image was found in **`external_id`** (the run), at **`item_index`** of
**`batch_id`**, with `image_id` = the crop's `evidence_id`. `match_count: 0` = clean (not in those runs).
Tenant-scoped: another tenant's `entry_id` → `404`; a run you don't own → simply absent.

> ⚠️ Cross-collection score note: the blacklist and indexed vectors are the same 512-D CLIP space (verified
> live at `similarity_score: 0.9999998` on an identical image), so scores are comparable — but the ideal
> `threshold` for *detection crops* may differ from the blacklist's default; tune per use case.

---

## 7. Status codes

| Code | When |
|---|---|
| `401` | missing `X-User-Id` (the gateway could not resolve a tenant from your key) |
| `404` | no batch / search / entry for that id **under your tenant** — row-missing and tenant-miss are **indistinguishable by design** (no existence disclosure) |
| `422` | bad body / `limit` / `offset` / `all` (e.g. `external_ids` empty or > 200) |
| `503` | the feature is disabled (`IMAGE_INDEX_ENABLED` / `IMAGE_INDEX_SEARCH_ENABLED`) or its repository is unavailable |

---

## 8. Integration recipe

1. The **coordinator submits** over Redis (see [IMAGE_INDEX_SUBMIT.md](./IMAGE_INDEX_SUBMIT.md)); runs >100 items split into several batches under one `external_id`.
2. **Poll** `by-external-id` (or `batch_id`) every ~2 s until `status` is terminal.
3. **Read** `counts` for the roll-up and `items[]` (with `include_items=true`) for per-item dispositions + `qdrant_point_id`.
4. **Map results back:** `item_ref` is exactly the `item_id` the coordinator sent — for dw-offline that is the crop's **`evidence_id`**, so each vector maps to a specific detection.

### Semantics to know
- **`completed_with_errors` is normal** — a bad/expired crop URL yields `download_failed` for that item; the batch still completes.
- **`error` is batch-level only** (rejected submit, compute batch failure, reaper timeout) — never a single bad image.
- **Idempotency:** re-submitting the same `client_batch_ref` re-binds the **same** batch (no duplicate work); a genuinely new run under the same `external_id` creates a new batch — use `?all=true` to see the history.
- **Query the indexed vectors two ways (both ✅ live):** **search-by-image** over a set of runs (§5) and the
  GPU-free **blacklist cross-reference** (§6). Vectors land in `image_index_embeddings` (payload-indexed on
  `user_id`/`external_id`/`batch_id`); the recovery legs (§3/§4) fetch a batch's results, §5/§6 search them.
  Design + rationale: [`../image-index/02_SEARCH_DESIGN.md`](../image-index/02_SEARCH_DESIGN.md).

---

## 9. Worked example (verified in prod, 2026-07-23)

Recover a completed batch through the gateway with the tenant's API key. **This is the exact response
shape the frontend receives** — `source_url`, `image_id`, and `qdrant_point_id` are all populated on
each embedded item.

```bash
# READ leg — through the gateway (Cloudflare needs a real User-Agent)
curl -sS "https://api.lookia.mx/api/v1/embedding/image-index/results/by-external-id/<external_id>?include_items=true" \
  -H "X-API-Key: <key>" -H "User-Agent: curl/8.4.0" | python3 -m json.tool
```

```jsonc
{
  "batch_id": "36de210e-32b7-49e5-9bff-518f1aafb7b4",
  "external_id": "<external_id>", "client_batch_ref": "<client_batch_ref>",
  "status": "completed",
  "counts": { "submitted": 2, "embedded": 2, "filtered": 0, "failed": 0 },
  "source_ref": "image-index-e2e", "created_at": "2026-07-23T20:07:37.20Z",
  "completed_at": "2026-07-23T20:07:38.74Z", "error_message": null,
  "items": [
    { "item_ref": "crop-0", "image_id": "crop-0",
      "source_url": "https://storage.lookia.mx/lucam-assets/kept_000002.png",
      "item_index": 0, "status": "embedded",
      "qdrant_point_id": "df7ef3c6-70c8-5479-9265-d65d100dab1c",
      "duplicate_of_index": null, "error_message": null },
    { "item_ref": "crop-1", "image_id": "crop-1",
      "source_url": "https://storage.lookia.mx/lucam-assets/kept_000001.png",
      "item_index": 1, "status": "embedded",
      "qdrant_point_id": "70d200ad-006d-58c2-90c9-ccbdf60c6b69",
      "duplicate_of_index": null, "error_message": null }
  ]
}
```

Invariants: terminal `completed`; `submitted == embedded + failed`; `filtered == 0`; one item row per
submitted; each `embedded` item carries a populated `source_url` + `qdrant_point_id`, and `image_id`
mirrors `item_ref`. End-to-end wall time (submit → completed) is **< 1 s** for a small batch. Reproduce
with `scripts/test_e2e_image_index.py` (skill: `image-index-e2e`).

## 10. Version

Contract **v1.2** (2026-07-23 — adds **§5 search-by-image (Cap A)** + **§6 blacklist cross-reference (Cap B)**, both verified live in prod; v1.1 `image_id` alias + `source_url`; base v1 2026-07-22). Compute envelope **v1-FROZEN** — [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md). Design + rationale: [`../image-index/00_DESIGN.md`](../image-index/00_DESIGN.md) §7. Reference analogs: `deepface-restapi/docs/apis/FACE_INDEX_API.md` §4–5, `lookia-plates-service/docs/apis/PLATE_INDEX_API.md` §4–5, `video-server_microservicios_etl-service/docs/apis/ONDEMAND_ANALYSIS_API.md` §3.
