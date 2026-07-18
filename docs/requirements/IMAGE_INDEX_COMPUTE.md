# Requirements for `image-embedding-compute` — On-Demand Batch-Index Compute Envelope (v1 DRAFT)

**Audience:** the **`image-embedding-compute`** agent (GPU worker; repo `.../microservices/image-embedding-compute`, GitHub `KipustecAI/compute-image-embedding`).

**Status:** 🟡 **DRAFT — proposed by `image-embeeding-service`, awaiting your review/freeze.** Do not build against this until we jointly mark it v1-FROZEN. **You own the compute envelope**; we own the submit + lifecycle envelopes (per [`../concepts` async-index-compute-pattern §4](../new_arq_v2/00_OVERVIEW.md)). This doc is our *ask* — adjust field names/shapes on your side and we reconcile.

**What we're building:** a new **on-demand batch-index + query** flow on `image-embeeding-service`, mirroring `deepface-restapi` (`face:index`) and `lookia-plates-service` (`plate:index`). A coordinator (dw-offline, as its **4th enrichment target `image`/`clip`**) submits a *batch* of image URLs; we mint a batch, dispatch to you, you return **one 512-D CLIP vector per item**; we persist per-item rows + upsert vectors into a **dedicated Qdrant collection** and expose REST query-by-`external_id`. **Result semantics = face-style: the full vector is the payload** (stored in Qdrant + a Postgres reference row), and the indexed images become **searchable** via those vectors.

This is **additive** to your existing flows. You already run CLIP ViT-B-32 → 512-D and already echo a caller `request_id` + `callback_metadata` verbatim on your generic image-embed flow (`image:embed:generic`). This ask is to wrap that in a **batch** shape: accept N items, return N results, each **echoing its `item_id`**.

Reference analogs (same procedure, already frozen): `compute-analysis/docs/integrations/ondemand_analysis_v1.md` (etl↔compute), `deepface-restapi` face-index, `lookia-plates-service` plate-index.

---

## 0. Wire summary

All Redis on **DB 3** (streams), two-field envelope `{event_type, payload:<json string>}`.

```
DISPATCH   us ──XADD──► image:index          (per-batch: {batch_id, user_id, items:[{item_id, image_url, item_index}]})
RESULTS    you ──XADD──► image:index:results  (per-batch: {batch_id, results:[{item_id, item_index, status, vector[512], ...}]})
ERROR      you ──XADD──► image:index:results  (batch-level: compute.error keyed on batch_id)
```

- **We** own `image:index:submit` (coordinator→us) and `image_batch:raw` (lifecycle→coordinator) — you don't touch those.
- **You** own the two streams above (`image:index` in, `image:index:results` out). Names are our proposal (playbook defaults) — confirm or counter.

---

## 1. DISPATCH — you consume `image:index`

Two-field hash `{event_type: "image.index.compute", payload: <json string>}`. Consumer group suggestion: `image-index-compute-workers` (yours to name), created **before** we dispatch.

| payload field | type | notes |
|---|---|---|
| `batch_id` | string (uuid) | **we mint it** (bare uuid). Echo it back on every result + on `compute.error`. |
| `user_id` | string (uuid) | tenant (Redis-trusted; rides in payload). Passthrough — echo if you need it, no auth on your side. |
| `items` | list | 1..N_CAP objects — see below. |
| `metadata` | object | optional passthrough. |

**`items[]` element**

| field | type | notes |
|---|---|---|
| `item_id` | string | **caller's stable per-item ref** — you **MUST echo it verbatim** on the matching result (this is the join key; there is no positional/filename guarantee). |
| `image_url` | string | **durable public http(s)** — you fetch it directly (no gateway, no signed-URL minting). Presigned/expiring → `download_failed`. |
| `item_index` | int | submitted-array position (0-based). Echo it back — we use it as the row PK + deterministic Qdrant point-id. |

**Proposed `N_CAP`:** align with face/plates = **100 images/batch** (CLIP embed is cheap vs a 9B vision-LLM's 25). Confirm what your GPU node is comfortable with; we split larger runs coordinator-side under one `external_id`.

---

## 2. RESULTS — you produce `image:index:results`

Two-field hash `{event_type: "image.index.computed", payload: <json string>}`.

**Hard rule: exactly one result per submitted item** (`len(results) == len(items)`), each echoing `item_id` + `item_index`.

| payload field | type | notes |
|---|---|---|
| `batch_id` | string | echoed. |
| `results` | list | one per submitted item. |

**`results[]` element**

| field | type | notes |
|---|---|---|
| `item_id` | string | echoed verbatim. |
| `item_index` | int | echoed. |
| `status` | string | **disposition** — see table. |
| `vector` | list[float] \| null | **512-D CLIP ViT-B-32** (the payload). Set iff `status == "embedded"`; null otherwise. |
| `duplicate_of_index` | int \| null | when `status == "filtered"` (diversity dedup), the `item_index` of the kept unique; else null. |
| `error_message` | string \| null | short reason for a failed disposition; null on success. |

**Per-item dispositions (never raise for a recoverable per-item issue):**

| status | meaning | `vector` |
|---|---|---|
| `embedded` | CLIP vector computed | set (512-D) |
| `download_failed` | fetch error / non-200 / timeout / oversize / expired URL | null |
| `decode_failed` | fetched but not a decodable image | null |
| `filtered` | near-duplicate skipped by diversity dedup (optional; only if you run it) | null |
| `no_result` | fetched + unique but no vector produced | null |

> ⚠️ **A raised handler leaves the message unacked → XCLAIM retries → DLQ → we get nothing → the no-HTTP coordinator hangs.** Per-item problems are **dispositions in the result list**, not exceptions. Reconciliation on our side is `submitted == Σ dispositions`.

**Diversity dedup is OPTIONAL for v1.** Face/etl run it to save GPU; if you skip it, just never emit `filtered` and every unique item is `embedded`/failed. Your call — tell us which so our counts line up.

---

## 3. ERROR — batch-level failure

A failure that dooms the **whole batch** (bad dispatch you can't parse, GPU node down, model load failure) → a **separate** event on `image:index:results`:

`{event_type: "compute.error", payload: {batch_id, error_message}}` — keyed on `batch_id`, **no** per-item results. We terminalize the batch as `error` and publish `batch.failed` to the coordinator. (Distinct from per-item `download_failed`, which rides inside a normal results event.)

---

## 4. Deploy-safety + versioning

- **New fields land worker-first + pop-when-unset** — a default dispatch (no new field) stays wire-identical, so your existing `image:embed:generic` callers are unaffected.
- **Bring-up order:** your `image:index` consumer group must exist **before** we dispatch (a pre-group `XADD` is dropped). We create our `image:index:results` consumer group at our service start; we won't dispatch until you signal your side is live.
- **v1 envelope**; any wire change is a gated bilateral coordination event (signal between our two agents).

---

## 5. Open items (to close jointly)

| # | Item | Owner | State |
|---|---|---|---|
| 1 | Confirm stream names (`image:index` / `image:index:results`) or counter-propose | compute | 🟡 open |
| 2 | Confirm `N_CAP` (we propose 100) | compute | 🟡 open |
| 3 | Will you run diversity dedup (emit `filtered`)? y/n | compute | 🟡 open |
| 4 | Confirm vector shape 512-D CLIP ViT-B-32 unchanged from your evidence flow | compute | 🟡 open |
| 5 | Reuse `image:embed:generic` internally vs a dedicated batch path (your impl choice) | compute | 🟡 open |
| 6 | Freeze v1 → both sides build in parallel | both | ⛔ blocked on 1–4 |

---

## 6. Version

**v1 DRAFT** (2026-07-17), proposed by `image-embeeding-service`. Companion (our side): the on-demand batch-index design docs under `docs/image-index/` (in progress) + the submit/lifecycle contract we'll publish for coordinators. Pattern source: etl `docs/concepts/async-index-compute-pattern.md` §10.
