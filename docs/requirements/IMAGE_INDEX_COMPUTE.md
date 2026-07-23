# `image-embedding-compute` ↔ `image-embeeding-service` — On-Demand Batch-Index Compute Envelope

**Status:** 🟢 **v1-FROZEN** (2026-07-22). Negotiated on agentmemory thread `thr_mrpk005r` — our draft (`sig_mrpk005r`), compute's review + counter (`sig_mrwiho2f`), our close (`sig_mrwjk27o`). **Both sides may now build against this.** Any wire change from here is a gated bilateral coordination event.

**Ownership:** **compute owns the compute envelope** (`image:index` in / `image:index:results` out); **we own** the submit envelope (`image:index:submit`) and the lifecycle envelope (`image_batch:raw`) — compute does not touch those.

**What this serves:** the on-demand **batch-index + query** flow on `image-embeeding-service`, mirroring `deepface-restapi` (`face:index`) and `lookia-plates-service` (`plate:index`). A coordinator (dw-offline, as its **4th enrichment target `image`/`clip`**) submits a batch of image URLs; we mint a batch and dispatch; compute returns **one 512-D CLIP vector per item**; we persist per-item rows, upsert vectors into the dedicated `image_index_embeddings` Qdrant collection, and expose REST query-by-`external_id`. **Result semantics are face-style: the vector is the payload**; indexed images become searchable.

Companion (our side): [`../image-index/00_DESIGN.md`](../image-index/00_DESIGN.md) + [`../image-index/01_PLAN.md`](../image-index/01_PLAN.md).

---

## 0. Wire summary

All Redis on **DB 3**, two-field envelope `{event_type, payload:<json string>}`.

```
DISPATCH   us ──XADD──► image:index          event_type "image.index.compute"
RESULTS    compute ──XADD──► image:index:results   event_type "image.index.computed"
ERROR      compute ──XADD──► image:index:results   event_type "compute.error"   (batch-level)
```

Compute consumer group: **`image-index-compute-workers`** (dedicated — not the shared `compute-workers`).

---

## 1. DISPATCH — compute consumes `image:index`

| payload field | type | notes |
|---|---|---|
| `batch_id` | string (uuid) | **we mint it** (bare uuid). Echoed back on every result + on `compute.error`. |
| `user_id` | string (uuid) | tenant (Redis-trusted). Passthrough; no auth on compute's side. |
| `items` | list | **1..100** objects (`N_CAP=100`, see §4-ii). |
| `metadata` | object | optional passthrough. |

**`items[]`**

| field | type | notes |
|---|---|---|
| `item_id` | string | caller's stable per-item ref — **MUST be echoed verbatim** on the matching result (the join key; no positional/filename guarantee). |
| `image_url` | string | **durable public http(s)**; compute fetches directly. Presigned/expiring → `download_failed`. |
| `item_index` | int | submitted-array position (0-based). Echoed back; we use it as the row PK + deterministic Qdrant point-id. |

---

## 2. RESULTS — compute produces `image:index:results`

Event type **`image.index.computed`**. **Hard rule: exactly one result per submitted item** (`len(results) == len(items)`), each echoing `item_id` + `item_index`.

| payload field | type | notes |
|---|---|---|
| `batch_id` | string | echoed. |
| `results` | list | one per submitted item. |

### `results[]` element

| field | type | notes |
|---|---|---|
| `item_id` | string | echoed verbatim. |
| `item_index` | int | echoed. |
| `status` | string | disposition — see table. |
| **`vector_b64`** | string \| null | **base64 of the raw 512 × float32 LITTLE-ENDIAN (`'<f4'`) buffer.** Set **iff** `status == "embedded"`, else null. |
| **`vector_encoding`** | string | constant **`"f32le_b64"`** — a real field, not implied, so encodings can be versioned. Our reader asserts this value and **dead-letters on an unknown encoding** rather than silently misparsing. |
| **`embedding_dim`** | int | constant **512**. |
| `duplicate_of_index` | int \| null | **always null in v1** (dedup disabled — §3). Forward-compatible. |
| `error_message` | string \| null | short reason on a failed disposition. |

**Why base64, not `list[float]`** — compute measured the real producer path at N=100: raw `tolist()` **1114.3 KB** vs `f32le_b64` **280.9 KB** (their largest proven prod message today is 110.7 KB). `tolist()` spends ~11 KB/vector to carry 2 KB of float32, because float32 values print as long float64 reprs; zlib+base64 only reaches 56%, so compression is not the lever. Base64 of the raw buffer is **lossless** (exact float32 bytes) and keeps `N_CAP=100`.

Producer pins endianness with `v.astype('<f4').tobytes()`. Our decode (verified bit-exact in `image_compute_backend_p11`, numpy present, ~2732 b64 chars/vector):

```python
np.frombuffer(base64.b64decode(r["vector_b64"]), dtype="<f4")   # -> (512,) float32; .tolist() for Qdrant
```

### Dispositions (per-item — **never** a raise)

| status | meaning | `vector_b64` |
|---|---|---|
| `embedded` | CLIP vector computed | set |
| `download_failed` | fetch error / non-200 / timeout / oversize / expired URL | null |
| `decode_failed` | fetched but not a decodable image | null |
| `no_result` | fetched but no vector produced | null |
| `filtered` | ⚠️ **RESERVED — never emitted in v1** (dedup disabled, §3) | null |

**v1 reconciliation:** `submitted == embedded + failed`, where `failed` folds `download_failed + decode_failed + no_result`.

> ⚠️ A raised handler leaves the message unacked → XCLAIM retries → DLQ → we get nothing → the no-HTTP coordinator hangs. Per-item problems are **dispositions in the result list**, not exceptions.

---

## 3. Diversity dedup — **DISABLED in v1** (decided on evidence)

Compute offered to run diversity dedup and emit `filtered`, conditional on us resolving `duplicate_of_index` to the kept twin's vector — correctly warning that otherwise a filtered image is **silently unsearchable**. We asked them to **turn it off for this flow**. Rationale:

1. **The crops are already deduplicated upstream.** dw-offline's detection-worker shipped CONTRACT v2.4 static-object temporal dedup (pHash track ledger, 2026-07-14): **446 occurrences → 10 rows, 98% suppressed**. Their wiki records the consequence: *"enrichment dispatch (face/plates) gets cheaper — fewer near-dup crops to the GPU indexers."* We are the 4th such indexer.
2. **The metrics available compute-side are the two worst performers.** detection-worker benchmarked 5 methods over 4,005 labeled pairs (`docs/wiki/comparisons/dedup-methods-benchmark.md`): DINOv2 **0.905** > pHash **0.893** > SSIM 0.740 > **Bhattacharyya 0.690** > **CLIP ViT-B/32 0.631 (last)**. Bhattacharyya misses **43%** of true duplicates and its median distance hits **0.300** under a mere ×0.85 brightness shift (3× the 0.10 prod threshold). CLIP is last precisely because it is *semantic* — two different cars read as "similar": right for our index, wrong for instance-level dedup.
3. **Index semantics.** Each item is a distinct dw-offline `evidence_id`; a filtered item returns nothing on query-by-`external_id`. The face-index sibling (whose result semantics we adopted) defaults `diversity_filter=false` for the same reason. etl runs dedup ON only because a 9B vision-LLM is expensive; CLIP is not.

**Consequence:** `filtered` reserved-but-never-emitted; `duplicate_of_index` always null. **If revisited in v1.1** we lift detection-worker's `src/services/phash.py` (~100 lines, pure numpy+PIL, no cv2) rather than copy the drifted Bhattacharyya filter — and we commit to `duplicate_of_index` pointer-resolution at that time.

> Compute's condition 3(b) (the evidence-path `DIVERSITY_FILTER_MAX_IMAGES=10` would cap a 100-item batch at 10 and violate `len(results)==len(items)`) is moot with dedup off — **but it was independently correct**: the same benchmark found *"the `max_images=10` cap is what compresses 50→10 in production, not the similarity metric."*

---

## 4. Operational constraints (compute's, acknowledged)

- **(i) Timeout.** 100 images exceeds the existing 120 s handler budget on downloads alone. Compute runs a dedicated longer timeout with **concurrent bounded-semaphore downloads**. Accepted semantics: a dead/slow host times out **per-item into `download_failed`** and must **not** doom the batch. Our reaper interval sits comfortably above compute's worst case and sweeps only genuinely-lost replies.
- **(ii) Event-loop fairness.** Compute's pipelines share one asyncio loop; a 100-image batch is a long occupant. `N_CAP=100` for v1, with an explicit agreement that **lowering `N_CAP` later is a NON-breaking config change** — the coordinator just splits into more batches under one `external_id`, and reconciliation is per-batch.
- **(iii) Stream trim.** Ours to honor: MAXLEN on `image:index` (our dispatch; ~20 KB at N=100 → **~2000**), and **`XREADGROUP COUNT=1`** on our results consumer so we never buffer more than one ~281 KB message client-side. For `image:index:results` (compute XADDs it) we proposed **MAXLEN ~200** (~56 MB, ≈20,000 images of history) — compute may counter to fit their Redis budget; the requirement is a bound, not a specific number.
- **Deploy-safety.** New fields land **worker-first + pop-when-unset**, so existing `image:embed:generic` callers stay wire-identical.

**Implementation (compute, item 5):** a **dedicated batch path** — shared internal helpers (same `CLIPEmbedder` singleton, same download/decode) but a separate handler, because the envelope, batch semantics, and above all the error model differ (generic raises; here per-item problems must be dispositions). Existing generic callers unaffected.

---

## 5. Open items — **ALL CLOSED**

| # | Item | Outcome |
|---|---|---|
| 1 | Stream names / event types / consumer group | ✅ Accepted as proposed |
| 2 | `N_CAP` + vector encoding | ✅ `N_CAP=100`, **`vector_b64` (`f32le_b64`)** adopted per compute's counter |
| 3 | Diversity dedup | ✅ **Disabled in v1**; `filtered` reserved, `duplicate_of_index` always null |
| 4 | Vector shape | ✅ 512-D CLIP ViT-B-32, same singleton, L2-normalized |
| 5 | Compute impl choice | ✅ Dedicated batch path |
| — | 3 operational constraints | ✅ Acknowledged; (iii) is an action on our side |

---

## 6. Bring-up order (locked)

1. Compute's **`image-index-compute-workers`** group on `image:index` must exist **before** we dispatch — a pre-group `XADD` is dropped.
2. Compute **signals this thread when live**; we do not XADD before that signal.
3. Our side is built and shipped **gated-OFF** (`IMAGE_INDEX_ENABLED=false`): Phases 1 (foundations), 2 (submit-intake), 4 (REST) committed. **Phase 3** (results consumer + land + reaper) is built against this frozen shape.
4. Coordinated first real batch smoke, then the flag flip.

---

## 7. Version

**v1 — FROZEN 2026-07-22.** Compute mirrors this into their `docs/CONTRACT.md`. Reference analogs: `compute-analysis/docs/integrations/ondemand_analysis_v1.md`, `deepface-restapi` face-index, `lookia-plates-service` plate-index. Pattern source: etl `docs/concepts/async-index-compute-pattern.md` §4/§10.
