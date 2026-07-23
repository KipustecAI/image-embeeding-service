# Image Index API — On-demand CLIP Image Indexing (Integration Guide)

**Audience:** frontends / REST clients (and implementing agents) recovering the results of an on-demand **image-index** batch. This is the read side of the flow; the Redis submit side (for no-HTTP coordinators) is [IMAGE_INDEX_SUBMIT.md](./IMAGE_INDEX_SUBMIT.md).

**What it does:** a coordinator (dw-offline, as its **4th enrichment target** `target="image"`) submits a batch of **public image URLs** — e.g. detection crops from a run. The GPU embeds each image with **CLIP ViT-B-32 (512-D)**, we store the vector in a dedicated Qdrant collection plus a per-item reference row, and you **recover the results by your own id**.

> 🚧 **STATUS — BUILT, NOT YET LIVE.** These endpoints are implemented and committed but ship **gated-OFF** (`IMAGE_INDEX_ENABLED=false`) → every route currently returns **`503`**. Also pending: the **gateway route registration** (below), and our **Phase 3** results-consumer (until it lands, batches will not reach a terminal state). Build against these shapes now; we will signal when the flag flips.

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

> **Gateway routing prerequisite:** `/api/v1/embedding/image-index/*` → `/api/v1/image-index/*` must be registered in the gateway `ROUTE_CONFIG` (a sibling of the existing `/api/v1/embedding/{search,stats,pipeline,recalculate}`). Until then a call returns `{"detail":"Route not found: …"}` **from the gateway** — distinct from our tenant-miss `404`. The Redis submit/lifecycle legs do not depend on this.

---

## 2. Flow at a glance

```
(coordinator) XADD image:index:submit        → we mint a batch → dispatch to GPU
        │
        ▼  (GPU embeds each URL with CLIP)
GET /api/v1/image-index/results/{batch_id}                     (recover by OUR id)
GET /api/v1/image-index/results/by-external-id/{external_id}   (recover by YOUR id)
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
| `item_ref` | string | **your `item_id`**, echoed |
| `source_url` | string \| null | the submitted image URL |
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
    { "item_ref": "3551", "source_url": "https://storage.lookia.mx/.../crop_000.png",
      "item_index": 0, "status": "embedded",
      "qdrant_point_id": "16d1a741-…", "duplicate_of_index": null, "error_message": null },
    { "item_ref": "3552", "source_url": "https://storage.lookia.mx/.../crop_001.png",
      "item_index": 1, "status": "embedded",
      "qdrant_point_id": "8b3d55b7-…", "duplicate_of_index": null, "error_message": null },
    { "item_ref": "3553", "source_url": "https://storage.lookia.mx/.../crop_002.png",
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

## 5. Status codes

| Code | When |
|---|---|
| `401` | missing `X-User-Id` (the gateway could not resolve a tenant from your key) |
| `404` | no batch for that `batch_id`/`external_id` **under your tenant** — row-missing and tenant-miss are **indistinguishable by design** (no existence disclosure) |
| `422` | bad `limit` / `offset` / `all` |
| `503` | the feature is disabled (`IMAGE_INDEX_ENABLED=false`) or its repository is unavailable |

---

## 6. Integration recipe

1. The **coordinator submits** over Redis (see [IMAGE_INDEX_SUBMIT.md](./IMAGE_INDEX_SUBMIT.md)); runs >100 items split into several batches under one `external_id`.
2. **Poll** `by-external-id` (or `batch_id`) every ~2 s until `status` is terminal.
3. **Read** `counts` for the roll-up and `items[]` (with `include_items=true`) for per-item dispositions + `qdrant_point_id`.
4. **Map results back:** `item_ref` is exactly the `item_id` the coordinator sent — for dw-offline that is the crop's **`evidence_id`**, so each vector maps to a specific detection.

### Semantics to know
- **`completed_with_errors` is normal** — a bad/expired crop URL yields `download_failed` for that item; the batch still completes.
- **`error` is batch-level only** (rejected submit, compute batch failure, reaper timeout) — never a single bad image.
- **Idempotency:** re-submitting the same `client_batch_ref` re-binds the **same** batch (no duplicate work); a genuinely new run under the same `external_id` creates a new batch — use `?all=true` to see the history.
- **Vectors are searchable but there is no search endpoint in v1** — `POST /api/v1/image-index/search` is deferred to v1.1; v1 stores the vectors in `image_index_embeddings` (payload-indexed on `user_id`/`external_id`/`batch_id`) and ships these read legs.

---

## 7. Version

Contract **v1** (2026-07-22). Compute envelope **v1-FROZEN** — [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md). Design + rationale: [`../image-index/00_DESIGN.md`](../image-index/00_DESIGN.md) §7. Reference analogs: `deepface-restapi/docs/apis/FACE_INDEX_API.md` §4–5, `lookia-plates-service/docs/apis/PLATE_INDEX_API.md` §4–5, `video-server_microservicios_etl-service/docs/apis/ONDEMAND_ANALYSIS_API.md` §3.
