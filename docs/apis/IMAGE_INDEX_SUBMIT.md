# Image Index Submit — Redis-only Intake (for no-HTTP coordinators)

**Audience:** services that **cannot call HTTP** and want to delegate **CLIP embedding + vector indexing** of their image crops to `image-embeeding-service` over **Redis Streams** (both directions) — e.g. `lookia-dw-offline-service` as its **4th enrichment target** (`target="image"`, alongside `face` / `plates` / `analysis`).

Mirrors the face-service `face:index:submit` and plates `plate:index:submit` contracts. The frontend read-side guide is [IMAGE_INDEX_API.md](./IMAGE_INDEX_API.md).

> ⚠️ **Do NOT XADD our internal `image:index` compute stream directly** — that bypasses persist/mint/tenant-guard. The submit-intake stream below is the **guarded front door**.

> ✅ **STATUS — LIVE + verified end-to-end in production (2026-07-23).** The submit-intake consumer,
> the compute round-trip (`image-embedding-compute`'s `image-index-compute-workers` group is live),
> the results-consumer/land, and the gateway REST read are all deployed behind
> `IMAGE_INDEX_ENABLED=true`. A real 2-URL batch round-tripped `image_batch.created → completed`
> (`submitted:2 → embedded:2`), and the vectors were recovered through the gateway. **Coordinators
> can integrate against this contract now** — see [IMAGE_INDEX_API.md §8](./IMAGE_INDEX_API.md) for the
> verified worked example.

## The three legs

```
SUBMIT     you ──XADD──► image:index:submit   (we run the full submit pipeline) ──► image:index (compute)
CONFIRM    image_batch:raw   (authoritative: image_batch.created / .completed / .failed + counts + client_batch_ref + batch_id)
QUERY      GET /api/v1/image-index/results/by-external-id/{external_id}   (frontend — see IMAGE_INDEX_API.md)
```

All Redis on DB **3**. Host coordinated out-of-band with Stanley.

---

## 1. SUBMIT — publish to `image:index:submit`

Envelope: flat hash `{event_type: "image.index.submit", payload: <json string>}`.

| payload field | Req | Notes |
|---|---|---|
| `user_id` | **Yes** | Your tenant, as a **UUID**. No gateway on Redis → it rides IN the payload and we **trust it** (Redis-trust, symmetric to gateway-header trust). **It must equal the tenant the frontend's API key resolves to**, or the REST read (leg 3) will `404`. |
| `client_batch_ref` | **Yes** | Your **per-submit unique** id — the idempotency anchor. Echoed on **every** lifecycle event so you bind our `batch_id` ↔ your submit. Distinct from `external_id`. |
| `external_id` | **Yes** | Your run id (**non-unique**; groups re-runs). The recover-by key for the REST read. dw-offline uses `run_id`. |
| `items` | **Yes** | **1–100** objects (`N_CAP=100`) — see below. |
| `source_ref` | opt | Free-form origin (e.g. `run-42/Vehículo`). Preserved on the batch and returned by the REST read. |
| `metadata` | opt | Free-form; passed through to compute. |

**`items[]` element**

| field | Req | Notes |
|---|---|---|
| `image_url` | **Yes** | **Durable public `http(s)` URL** — the GPU worker fetches it **directly** (no gateway, no signed-URL minting). Presigned/expiring or private URLs → every item `download_failed`. |
| `item_id` | **Yes** | Your stable per-item ref — **echoed back as `item_ref`** on the result. This is the join key (there is no positional guarantee). dw-offline sends the crop's `evidence_id`. |

> `item_index` is **minted by us** from the 0-based submit order — do **not** send it.

> **v1.1 vocabulary alias (additive, non-breaking):** for parity with the face / plates / analysis
> targets, the submit payload also accepts the portfolio-standard **`images:[{image_url, image_id}]`**
> as an alias for `items:[{image_url, item_id}]`. Send either; if both are present the native
> `items`/`item_id` wins. `image_id` is echoed back on the result as **both** `item_ref` and `image_id`
> (same value). New integrations may use whichever the rest of the family uses.

**Example (Python `redis`):**
```python
import json, redis
r = redis.Redis(host="<redis-host>", port=6379, password="<pw>", db=3)
r.xadd("image:index:submit", {
    "event_type": "image.index.submit",
    "payload": json.dumps({
        "user_id": "<tenant-uuid>",                  # REQUIRED, UUID, trusted
        "client_batch_ref": "dwoff-run42-image-b7",   # your per-submit unique id
        "external_id": "run-42",                      # your run_id (non-unique)
        "source_ref": "run-42/Vehículo",
        "items": [
            {"image_url": "https://storage.lookia.mx/.../crop_000.png", "item_id": "3551"},
            {"image_url": "https://storage.lookia.mx/.../crop_001.png", "item_id": "3552"}
        ]
    })
})
```

`batch_id` is **ours to mint** (a bare UUID) and comes back on `image_batch.created` — never supplied by you.

### Rejection — two tiers

| Tier | Trigger | What happens |
|---|---|---|
| **1 — unbindable** | payload not an object · `user_id` missing/blank/**not a UUID** · `client_batch_ref` missing/blank | **Logged loud + ACK-dropped. No batch, no event.** We cannot bind a tenant or your ref, so there is nothing to report against. |
| **2 — bindable but rejected** | missing `external_id` · `items` not a list or empty · **over `N_CAP=100`** · an item missing `item_id` · an item whose `image_url` is not `http(s)` | We persist an **ERROR batch** and publish **`image_batch.failed`** carrying your `client_batch_ref`. Failures are loud on the **same** channel, so a no-HTTP coordinator never hangs. |

**Split runs larger than 100 items** into multiple submits under one `external_id` — the REST read's `?all=true` lists them all.

### Idempotency

`client_batch_ref` is the anchor. A **redelivered** submit (same ref) **re-binds the same batch** and **re-publishes `image_batch.created`**, but does **NOT** re-mint and does **NOT** re-dispatch to the GPU. Exactly one batch, exactly one dispatch. Treat `image_batch.created` idempotently by `batch_id`.

---

## 2. CONFIRM — consume `image_batch:raw`

Attach your **own** consumer group (e.g. `dwoff-image-acks`). Events `{event_type, payload: <json string>}` with event types **`image_batch.created`** / **`image_batch.completed`** / **`image_batch.failed`**.

- **`image_batch.created`** carries **both your `client_batch_ref` AND our minted `batch_id`** — bind them here.
- **`image_batch.completed`** / **`image_batch.failed`** are terminal. **No HTTP poll needed.**
- Every event carries `external_id`, `user_id`, `status`, and `counts`.

```jsonc
// image_batch.created
{ "batch_id": "49c7861d-…", "client_batch_ref": "dwoff-run42-image-b7", "external_id": "run-42",
  "user_id": "<tenant-uuid>", "status": "computing",
  "counts": {"submitted": 2, "embedded": 0, "filtered": 0, "failed": 0},
  "source_ref": "run-42/Vehículo", "created_at": "…", "completed_at": null }

// image_batch.completed (terminal)
{ "batch_id": "49c7861d-…", "client_batch_ref": "dwoff-run42-image-b7", "external_id": "run-42",
  "user_id": "<tenant-uuid>", "status": "completed_with_errors",
  "counts": {"submitted": 2, "embedded": 1, "filtered": 0, "failed": 1},
  "source_ref": "run-42/Vehículo", "completed_at": "…" }
```

### Status machine

`pending` → `computing` (set only after a successful dispatch) → `completed` | `completed_with_errors` | `error`

- **`completed`** — every item embedded (`failed == 0`).
- **`completed_with_errors`** — at least one per-item failure. **Still a success path**, not a job failure.
- **`error`** — a **batch-level** failure only: a rejected submit (tier 2), a compute batch-level error, or a reaper timeout. **A per-item `download_failed` is NOT an `error` batch.**

### Counts — one 4-key folded shape

`{submitted, embedded, filtered, failed}` — identical in the lifecycle payload, our DB columns, and the REST response.

- `failed` folds `download_failed + decode_failed + no_result`.
- **`filtered` is always `0` in v1** — diversity dedup is **disabled** for this flow, so every submitted image is embedded or a real failure. (Reserved for a possible v1.1.)
- **Reconciliation:** `submitted == embedded + failed`.

> **Reliability:** best-effort publish + your consumer-group cursor replay (`image_batch:raw` is MAXLEN-trimmed). If streams are briefly down a notice can be missed — a periodic **reconcile-by-`external_id`** via the REST read is the safe backstop.

---

## 2B. PROGRESS (optional) — multiplex the progress stream  🟢 v1.2 (shipped by compute)

For a live progress bar on a large batch, attach your **own** consumer group to the advisory stream
**`image:index:progress`** and read the same events a frontend would — a Redis fan-out (independent
cursors, zero contention). Mirrors plates' `…:progress` and face's `face:index:progress`.

- Emitted **by `image-embedding-compute`** as it embeds (the progress granularity lives on the GPU
  side; we do not sit on this path). Two-field `{event_type:"image.index.progress", payload:<json>}`.
- **`payload` (locked, v1.2):** `{batch_id, processed, total, embedded_so_far, failed_so_far, stage}`
  where `stage ∈ {"downloading", "embedding"}`.
- **Advisory + best-effort + monotonic**, **ephemeral** (`MAXLEN ~1000`, no replay, **never terminal**).
- Emitted **only for batches ≥ 20 items** (`IMAGE_INDEX_PROGRESS_MIN_BATCH=20`) — small/fast batches
  finish before a bar matters; below the threshold the stream is empty and you treat that as "no
  progress" (same as analysis today). Profile for a 100-item batch: one `downloading` heartbeat at
  entry, then ~20 throttled `embedding` frames climbing to 100/100.
- **Authoritative status is always `image_batch:raw`** — `progress` is a live estimate, never the
  source of truth; read `counts` on the terminal event for the final numbers.

> ⚠️ **CRITICAL RENDER SEMANTIC — `processed` counts TERMINAL dispositions only:**
> `processed == embedded_so_far + failed_so_far`. A downloaded-but-not-yet-embedded item is **not**
> terminal, so during the **download** phase `processed` stays near `0` (it only moves on failures)
> and then climbs to `total` during the **embedding** phase. This is the one mapping that is globally
> **monotonic** across compute's two phases (a single `0..total` counter that also moved during
> download would have to regress at the phase boundary). **Render the download phase from `stage`, NOT
> from `processed`:** `stage=="downloading"` → an indeterminate "Downloading N images…" state;
> `stage=="embedding"` → a determinate `processed/total` bar. (If your UX genuinely needs motion during
> download, compute can add a separate `download_processed` field — coordinate on `thr_mrpk005r` before
> building the consumer.)

> 🟢 **Status: v1.2 shipped by `image-embedding-compute`** (thread `thr_mrpk005r`). Field names above are
> **final**. Until you point a consumer at it, rely on the `image_batch:raw` lifecycle + a stale-batch
> watchdog (recommended ≥ ~960 s, just outside our reaper's 900 s backstop).

---

## 3. Result recovery

Results stay in **our DB + Qdrant**. You are **seed + coordinator — you never call HTTP.** The `image_batch.completed` counts tell you what landed; the enriched per-item rows (status, `qdrant_point_id`) are recovered by a frontend/REST client by `external_id` — see **[IMAGE_INDEX_API.md](./IMAGE_INDEX_API.md)**.

Each embedded item's 512-D CLIP vector lands in the dedicated **`image_index_embeddings`** Qdrant collection, keyed by a deterministic point id and payload-indexed on `user_id` / `external_id` / `batch_id`, so the indexed images are **searchable**. (A query-time similarity endpoint is deferred to v1.1 — v1 stores the vectors and ships the results-query GETs.)

---

## 4. Rollout

Our `image:index:submit` consumer group is created at service start — it must exist **before** you publish, else pre-group XADDs are dropped. We will signal when this is deployed so you flip mock → live. Reference analogs: `deepface-restapi/docs/apis/FACE_INDEX_API.md`, `lookia-plates-service/docs/apis/PLATE_INDEX_SUBMIT.md`, `video-server_microservicios_etl-service/docs/apis/ONDEMAND_ANALYSIS_API.md`.

## 5. Version

Redis submit-intake **v1.1** (2026-07-23 — additive `images`/`image_id` alias; base v1 2026-07-22). **v1.2 in flight:** the advisory `image:index:progress` leg (§2B) + compute's `image_url`→`source_url` echo, both additive + built on the compute side in parallel (thread `thr_mrpk005r`)., mirroring the face/plates submit shape. Compute envelope (`image:index` / `image:index:results`) is **v1-FROZEN** — see [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md). Internal design: [`../image-index/00_DESIGN.md`](../image-index/00_DESIGN.md).
