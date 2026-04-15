# Weapons Enrichment — Producer Contract

**Scope:** the exact shape the `compute-weapons` service must publish when it enriches messages on the `embeddings:results` Redis stream. Read by anyone implementing or modifying the producer side.

**Status:** ACCEPTED. Changes require backend sign-off; see the "Versioning" section at the end.

**Canonical stream contract for all `embeddings:results` fields (including the base payload this doc builds on):** [../new_arq_v2/04_STREAM_CONTRACTS.md](../new_arq_v2/04_STREAM_CONTRACTS.md). This file only specifies the optional `weapon_analysis` enrichment block — the base fields (`evidence_id`, `camera_id`, `zip_url`, `embeddings[]`, etc.) are not repeated here.

---

## 1. Stream envelope

| Field | Value | Notes |
|---|---|---|
| Stream | `embeddings:results` | Same stream as the legacy/unenriched path |
| Redis DB | `3` | As configured in `REDIS_STREAMS_DB` |
| XADD envelope key `event_type` | **`embeddings.computed`** | **Required.** Must match the existing consumer handler registration. Do **not** publish `weapons.analyzed` at the envelope level — the backend consumer would drop it. |
| Consumer group | `backend-workers` | Existing group, no change |
| Dead-letter stream | `embeddings:results:dead` | Existing, no change |

### Why `embeddings.computed` and not `weapons.analyzed`?

The backend consumer in [src/streams/embedding_results_consumer.py:68](../../src/streams/embedding_results_consumer.py#L68) registers a handler for `embeddings.computed`. Switching the envelope `event_type` to `weapons.analyzed` would require a coordinated deploy of the backend to register a second handler. Keeping the envelope stable — and using the optional `weapon_analysis` block as the only signal of enrichment — means the backend can ship its consumer code independently of compute-weapons' rollout schedule.

The body-level `event_type` field (if any) is free to carry a trace label like `"weapons.analyzed"` for observability; it is not read by the consumer.

---

## 2. The `weapon_analysis` block

Optional top-level field inside the JSON payload. Presence is the only signal that compute-weapons ran on this evidence.

```json
{
  "weapon_analysis": {
    "images": [ ... ],   // required if weapon_analysis is present
    "summary": { ... }   // required if weapon_analysis is present
  }
}
```

If `weapon_analysis` is omitted entirely, the backend behaves exactly as the legacy path (all weapon flags default to `false`, no detection data is stored). This is the backwards-compatible fall-through.

### 2.1 `images[]` — per-image detection detail

Array of objects, **one per image that compute-weapons inspected**. Order is not significant.

| Field | Type | Required | Constraints | Example |
|---|---|---|---|---|
| `image_name` | string | **yes** | Must match the `image_name` of exactly one element in the top-level `embeddings[]` array. Images that compute-weapons inspected but the embedding pipeline dropped (e.g. diversity-filtered out) MAY be present but will be silently ignored by the backend. | `"20260410-100150_023389.jpg"` |
| `image_index` | integer | no | Informational. Should match `embeddings[].image_index` for the same `image_name`. If present and mismatched, the backend uses `image_name` as the join key and ignores `image_index`. | `0` |
| `detections` | list | **yes** | Empty list `[]` means "analyzed, no weapons found". `null` is accepted but treated as `[]`. | see below |

An image with `detections: []` explicitly signals "analyzed clean" — this is the signal that powers the false-positive review queue. An image that is **absent from `images[]` entirely** is treated as "not analyzed" — semantically different, and the backend stores `NULL` for `weapon_detections` in that case.

### 2.2 `detections[]` — individual weapon detections

Array of objects within each `images[]` entry. One object per detected instance (not per class). If an image has two handguns, send two detection objects with `class_name: "handgun"`.

| Field | Type | Required | Constraints | Example |
|---|---|---|---|---|
| `class_name` | string | **yes** | Lowercase ASCII. Backend uses this verbatim for Qdrant filtering. Stable vocabulary — additions are fine, renames break existing stored data. | `"handgun"` |
| `class_id` | integer | no | Informational. Not used by backend filtering. Useful for reports that need the raw model output. | `0` |
| `confidence` | float | **yes** | Range `[0.0, 1.0]`. Backend does not threshold — the producer is responsible for filtering low-confidence detections before publishing. | `0.873` |
| `bbox` | object | **yes** | See 2.3 below. | see below |

### 2.3 `bbox` — bounding box

| Field | Type | Required | Constraints |
|---|---|---|---|
| `x1` | integer | **yes** | Top-left X in **absolute pixel coordinates** (not normalized). Must be `>= 0`. |
| `y1` | integer | **yes** | Top-left Y, absolute pixels, `>= 0`. |
| `x2` | integer | **yes** | Bottom-right X, absolute pixels, `> x1`. |
| `y2` | integer | **yes** | Bottom-right Y, absolute pixels, `> y1`. |

**Absolute coordinates, not normalized.** The image dimensions are not re-published by the producer; consumers that need normalized coordinates must re-open the image from `storage-service` and divide. This is deliberate — it keeps the contract lossless and pushes normalization to the clients that actually need it.

The backend does not validate bbox coordinates against image dimensions. Producers must not emit negative or out-of-bounds boxes; if they do, downstream UI rendering is undefined.

### 2.4 `summary` — evidence-level rollup

Single object, not an array. Required if `weapon_analysis` is present. All fields are stored verbatim on `embedding_requests.weapon_summary` as JSONB, plus the specific columns listed below.

| Field | Type | Required | Stored in column | Notes |
|---|---|---|---|---|
| `images_analyzed` | integer | **yes** | — (JSONB only) | Number of images compute-weapons actually inspected. May exceed `embedded_count` if compute-weapons received more frames than survived the diversity filter. |
| `images_with_detections` | integer | **yes** | — (JSONB only) | Count of images that had at least one detection. |
| `total_detections` | integer | **yes** | — (JSONB only) | Total detection objects across all images (`sum(len(img.detections))`). |
| `classes_detected` | list[string] | **yes** | `embedding_requests.weapon_classes` | Deduplicated, lowercase ASCII. Order is not significant. Empty list is allowed and means "analyzed but nothing found". |
| `max_confidence` | float \| null | **yes** | `embedding_requests.weapon_max_confidence` | `null` when `total_detections == 0`. Otherwise the peak confidence across all detections for this evidence. |
| `has_weapon` | bool | **yes** | `embedding_requests.has_weapon` | Must equal `total_detections > 0`. Producer-computed redundancy for fast evidence-level filtering; the backend does not cross-check. |

---

## 3. Three worked examples

Same evidence, same `evidence_id` / `camera_id` / `zip_url`, three reachable states. Each is a complete `embeddings:results` payload (with vectors truncated for readability).

### 3.1 Legacy — no enrichment

Compute-weapons was not invoked or was bypassed by routing. The payload is byte-for-byte identical to today's contract.

```json
{
  "event_type": "embeddings.computed",
  "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
  "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "app_id": 1,
  "infraction_code": "SMVV8UGE_116_875_20260410100151",
  "zip_url": "https://minio.lookia.mx/lucam-assets/SMVV8UGE_116_875_20260410100151.zip",
  "embeddings": [
    { "image_name": "20260410-100150_023389.jpg", "image_index": 0, "vector": [0.012, -0.034, "..."] }
  ],
  "input_count": 10,
  "filtered_count": 9,
  "embedded_count": 9
}
```

**Backend state after ingest:**
- `embedding_requests.weapon_analyzed = false`
- `embedding_requests.has_weapon = false`
- `embedding_requests.weapon_classes = []`
- `evidence_embeddings.weapon_detections = NULL`
- Qdrant payload: `weapon_analyzed: false, has_weapon: false, weapon_classes: []`

### 3.2 Analyzed, clean — false-positive review candidate

Compute-weapons inspected the evidence but found nothing. This is the state that powers the `weapons_filter="analyzed_clean"` search mode — humans can audit these to catch model false negatives.

```json
{
  "event_type": "embeddings.computed",
  "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
  "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "app_id": 1,
  "infraction_code": "SMVV8UGE_116_875_20260410100151",
  "zip_url": "https://minio.lookia.mx/lucam-assets/SMVV8UGE_116_875_20260410100151.zip",
  "embeddings": [
    { "image_name": "20260410-100150_023389.jpg", "image_index": 0, "vector": [0.012, -0.034, "..."] }
  ],
  "input_count": 10,
  "filtered_count": 9,
  "embedded_count": 9,
  "weapon_analysis": {
    "images": [
      { "image_name": "20260410-100150_023389.jpg", "image_index": 0, "detections": [] }
    ],
    "summary": {
      "images_analyzed": 1,
      "images_with_detections": 0,
      "total_detections": 0,
      "classes_detected": [],
      "max_confidence": null,
      "has_weapon": false
    }
  }
}
```

**Backend state after ingest:**
- `embedding_requests.weapon_analyzed = true`
- `embedding_requests.has_weapon = false`
- `embedding_requests.weapon_classes = []`
- `embedding_requests.weapon_max_confidence = NULL`
- `evidence_embeddings.weapon_detections = []` (empty array, not NULL)
- Qdrant payload: `weapon_analyzed: true, has_weapon: false, weapon_classes: []`

### 3.3 Analyzed, weapons detected

```json
{
  "event_type": "embeddings.computed",
  "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
  "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "app_id": 1,
  "infraction_code": "SMVV8UGE_116_875_20260410100151",
  "zip_url": "https://minio.lookia.mx/lucam-assets/SMVV8UGE_116_875_20260410100151.zip",
  "embeddings": [
    { "image_name": "20260410-100150_023389.jpg", "image_index": 0, "vector": [0.012, -0.034, "..."] },
    { "image_name": "20260410-100150_023383.jpg", "image_index": 1, "vector": [0.021,  0.008, "..."] }
  ],
  "input_count": 10,
  "filtered_count": 9,
  "embedded_count": 9,
  "weapon_analysis": {
    "images": [
      {
        "image_name": "20260410-100150_023389.jpg",
        "image_index": 0,
        "detections": [
          {
            "class_name": "handgun",
            "class_id": 0,
            "confidence": 0.873,
            "bbox": { "x1": 412, "y1": 188, "x2": 596, "y2": 402 }
          }
        ]
      },
      {
        "image_name": "20260410-100150_023383.jpg",
        "image_index": 1,
        "detections": []
      }
    ],
    "summary": {
      "images_analyzed": 2,
      "images_with_detections": 1,
      "total_detections": 1,
      "classes_detected": ["handgun"],
      "max_confidence": 0.873,
      "has_weapon": true
    }
  }
}
```

**Backend state after ingest:**
- `embedding_requests.weapon_analyzed = true`
- `embedding_requests.has_weapon = true`
- `embedding_requests.weapon_classes = ["handgun"]`
- `embedding_requests.weapon_max_confidence = 0.873`
- `evidence_embeddings[0].weapon_detections = [{class_name: "handgun", ...}]` (frame 0)
- `evidence_embeddings[1].weapon_detections = []` (frame 1 — analyzed clean)
- Qdrant payload for frame 0: `weapon_analyzed: true, has_weapon: true, weapon_classes: ["handgun"]`
- Qdrant payload for frame 1: `weapon_analyzed: true, has_weapon: false, weapon_classes: []`

Note how `weapon_classes` on the Qdrant payload is **per-image**, not evidence-level: frame 1 has an empty list even though the evidence overall contains a handgun. This is deliberate — it lets `weapons_filter="only"` return the frames that actually show a weapon, not every frame of an evidence that happens to contain one.

---

## 4. Error semantics

### 4.1 Weapons analysis failed — but embeddings succeeded

**Contract:** drop the `weapon_analysis` block entirely. Publish the rest of the payload as if compute-weapons had never been invoked.

**Rationale:** embedding work succeeded and must not be lost. A weapons failure is a soft degrade, not a pipeline failure. Dropping the block makes the backend fall through to the legacy path, and the evidence lands with `weapon_analyzed=false` — indistinguishable from "compute-weapons was skipped by routing". If observability matters, log the weapons failure at the producer; do not encode it in the payload.

**Do not:**
- Publish `compute.error` with `entity_type: "evidence"` for a weapons-only failure (the backend treats that as an embedding-level error and refuses to store vectors).
- Publish a `weapon_analysis` block with `images: []` and `summary.images_analyzed: 0` to signal "tried and failed" — the backend would interpret that as `weapon_analyzed=true, has_weapon=false`, which is a **semantic claim** (analyzed-clean) that isn't actually true. It would pollute the false-positive review queue.

### 4.2 Embeddings step failed

Out of scope for this document — existing `compute.error` contract in [04_STREAM_CONTRACTS.md](../new_arq_v2/04_STREAM_CONTRACTS.md) applies unchanged.

### 4.3 Partial analysis

If compute-weapons analyzed some frames but not others (e.g. OOM on frame 7 of 9), publish `images[]` with **only the frames actually analyzed**. Do not include un-analyzed frames with synthetic empty detection lists — that would misclassify them as "analyzed clean".

The backend's contract is: "an image is in `images[]` ⇒ it was analyzed". Uphold that invariant strictly.

---

## 5. Validation rules — what the backend accepts, tolerates, or ignores

These are frozen behaviors the backend guarantees. If a rule becomes inconvenient, coordinate a change; don't work around it with producer-side hacks.

| Condition | Backend behavior | Rationale |
|---|---|---|
| `weapon_analysis` absent | Legacy path. All weapon columns default to "unanalyzed". No error. | Backwards compat |
| `weapon_analysis: null` | Same as absent | `None`-safe parsing |
| `weapon_analysis: {}` (empty dict) | Same as absent — `weapon_analyzed=false` | Empty dict is falsy |
| `weapon_analysis.images` missing or `null` | `weapon_analyzed=true`, all per-image fields empty | Evidence-level flag can still be set via `summary` |
| `weapon_analysis.summary` missing | `weapon_analyzed=true`, all evidence-level columns take default values (`has_weapon=false`, `weapon_classes=[]`, etc.) | Defensive — we trust producer summary as source of truth when present |
| `images[N].image_name` missing | Entry silently skipped | No way to join to `embeddings[]` |
| `images[N].image_name` doesn't match any `embeddings[].image_name` | Entry silently skipped | Compute-weapons may inspect more frames than survived the diversity filter |
| `detections[N].class_name` missing | Detection kept in `weapon_detections` JSONB (for audit trail) but NOT added to `weapon_classes` | Un-typed detections are useless for filtering |
| Duplicate `image_name` in `images[]` | Last occurrence wins (`dict[str, list]` build) | Upstream bug; log on producer side |
| `confidence` out of `[0.0, 1.0]` | Accepted verbatim. Stored in JSONB and used for `max_confidence`. | Backend does not validate ranges |
| `bbox` missing or partial | Detection kept in JSONB but marked malformed in logs | Audit trail preserved |
| Unknown top-level fields in `weapon_analysis` | Silently ignored | Forward compat |
| Unknown fields in `detections[N]` | Preserved verbatim in `weapon_detections` JSONB | Forward compat |
| **Unknown root-level fields** (e.g. `trace_event`) | Silently ignored | The consumer uses explicit `payload.get(...)` per known field; anything else is dropped without error. Producers can add observability fields at the root freely. |
| `weapon_analysis_error: {"message": "..."}` at root | **Captured** into `embedding_requests.weapon_analysis_error` column | Producer's failure pass-through path. See §5.1 below. |
| `trace_event: "weapons.analyzed"` / `"weapons.failed"` at root | Logged, not persisted | Producer trace label for observability. Redundant with other fields (presence of `weapon_analysis` or `weapon_analysis_error`). |

### 5.1 The fourth reachable state: "attempted, failed"

Since Phase 1 of the weapons enrichment, the backend has tracked three states via the two-boolean cube (`weapon_analyzed` × `has_weapon`). Capturing `weapon_analysis_error` adds a fourth reachable state that distinguishes "never attempted" from "attempted and failed" — both of which have `weapon_analyzed=false, has_weapon=false`, but mean very different things operationally.

| State | `weapon_analyzed` | `has_weapon` | `weapon_analysis_error` | Meaning |
|---|---|---|---|---|
| Legacy / skipped | `false` | `false` | `NULL` | Never went through compute-weapons (routing bypass or pre-rollout) |
| **Attempted, failed** | `false` | `false` | `"reason"` | **New.** compute-weapons tried but hit a ZIP timeout, OOM, corrupt archive, etc. |
| Analyzed clean | `true` | `false` | `NULL` | Analyzed, no detections. Powers the false-positive review queue. |
| Weapons detected | `true` | `true` | `NULL` | Analyzed with at least one detection. |

**Producer contract for the failed state** (lives in the [`image-weapons-compute` contract §3.2](../../../image-weapons-compute/docs/CONTRACT.md)):

- `weapon_analysis` block is **dropped entirely** — the backend falls through to the legacy-shaped payload path.
- `weapon_analysis_error: {"message": "<reason>"}` is added at the payload root.
- `trace_event: "weapons.failed"` may also be added at the root (observability only).
- All other fields are preserved unchanged.

**Backend handling:**
- Consumer extracts `payload["weapon_analysis_error"]["message"]` defensively (tolerates `None`, `{}`, and non-dict values by treating them as "no error").
- The error message is stored in `embedding_requests.weapon_analysis_error` (TEXT, NULL = no error).
- `weapon_analyzed=false` is the correct value — compute-weapons did **not** successfully analyze the images. The error column is the signal that it was attempted.
- No impact on Qdrant payload or `evidence_embeddings` — the failure is an evidence-level fact, not per-image.

**Operational query:**
```sql
-- Most common weapon-analysis errors in the last 7 days
SELECT weapon_analysis_error, COUNT(*) AS n
FROM embedding_requests
WHERE weapon_analysis_error IS NOT NULL
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 2 DESC;
```

**Non-goals for this column:**
- No structured error code / type — producers publish free-text messages because error taxonomies drift faster than DB schemas.
- No retry state machine — the column is a record of what happened, not a queue. If we ever add a retry worker, it would read from this column but not write to it.
- No alerting integration — that's a separate pipeline on top of the column.

The backend's parsing code lives in [src/streams/embedding_results_consumer.py:89-216](../../src/streams/embedding_results_consumer.py#L89-L216). The edge-case table in [03_CONSUMER.md](03_CONSUMER.md) is the same information from the consumer's perspective.

---

## 6. Versioning & forward compatibility

**Additive changes** (new fields on existing objects, new values for `class_name`) are always safe. Producers may add them unilaterally.

**Breaking changes** — renaming a field, removing a field, changing a type, changing `bbox` from absolute to normalized, switching the `event_type` envelope — require:

1. A PR to this document describing the change and its motivation
2. Backend sign-off (the consumer code must be updated first, so the new producer payload is handled when it arrives)
3. Coordinated deploy order: backend first (accepts both old and new shapes for a transition window), then producer

The transition window for any breaking change is **at least one business day**. If you need something shorter, something is wrong with the design — stop and reconsider.

**Class vocabulary changes** (renaming `handgun` to `pistol`) are breaking because Qdrant's payload index stores the exact string. A rename requires a bulk `update_payload` operation on existing Qdrant points, which is a backfill job — out of scope for this document.

---

## 7. Open questions, non-commitments & resolved items

### Resolved

- **[RESOLVED] Model version tagging.** The producer now publishes `summary.model_version` as a dict with three keys: `model_a`, `model_b`, `person_model` (e.g. `{"model_a": "persona_armada_1label_V1", "model_b": "armas_v202", "person_model": "yolo11s"}`). The backend stores the full `summary` block verbatim in `embedding_requests.weapon_summary` JSONB, so `model_version` is available for post-hoc traceability without any backend code change. No dedicated column — query it via `weapon_summary->'model_version'`.
- **[RESOLVED] Per-detection `source` field.** The producer now publishes `detections[N].source: "model_a" | "model_b"` indicating which detector found the instance. The backend stores the full `detections[]` list verbatim in `evidence_embeddings.weapon_detections` JSONB, so `source` flows through automatically — forward-compat behavior (per §5, "unknown fields in `detections[N]` preserved verbatim").
- **[RESOLVED] Root-level extension fields.** The producer's `image-weapons-compute` contract §3.2 asked whether the backend tolerates root-level fields like `trace_event` and `weapon_analysis_error`. Answer: **yes**. The consumer uses explicit `payload.get(...)` per known field, so unknown root keys are silently dropped — no rejection, no dead-letter. Additionally, `weapon_analysis_error` is **not just tolerated but captured** into a dedicated column (§5.1). `trace_event` is logged but not persisted.

### Open

- **Per-class confidence thresholds.** The backend stores whatever the producer publishes. If product decides "handguns need 0.9, knives need 0.7", that's a producer-side policy — the backend is not the right enforcement point.
- **Throttling.** If compute-weapons falls behind, it does not delay the embedding pipeline — producers may publish the legacy (no `weapon_analysis`) shape temporarily, and those messages take the backwards-compat path. This is an acceptable degradation.
- **Historical backfill.** Re-analyzing existing evidences after a model upgrade is explicitly **not** in scope for this pipeline. It's a separate job that would XADD synthetic `embeddings.computed` messages with fresh `weapon_analysis` blocks. The backend's dedup check on `evidence_id` would refuse them — so backfill requires a separate entry point or a deliberate bypass flag. We'll design that when we need it.
- **Bbox oriented boxes (OBB).** The producer's contract §7 flags that Model B is OBB internally but publishes axis-aligned envelopes. If downstream UI ever needs oriented polygons, the producer will add `bbox_obb: {cx, cy, w, h, angle}` as an additive field on `detections[]`. No backend change needed — it'll flow through the JSONB column automatically.
- **`global_score` promotion.** Today it's producer-internal (used for alerting thresholds). If product wants evidence-level scoring for sorting/reports, promote to `summary.global_score` via the additive change path in §6.

---

## 8. Quick self-check for producer implementors

Before publishing a message with `weapon_analysis`, verify:

- [ ] XADD envelope `event_type` is `embeddings.computed` (not `weapons.analyzed`)
- [ ] Every entry in `images[]` has an `image_name` that exactly matches one in the top-level `embeddings[]`
- [ ] `detections[]` is a list (possibly empty) — never `null`, never missing
- [ ] Every `detection` has `class_name`, `confidence`, and a complete `bbox`
- [ ] `summary.has_weapon` equals `total_detections > 0`
- [ ] `summary.classes_detected` is the deduplicated union of all `detections[*].class_name`
- [ ] `bbox` coordinates are absolute pixels, not normalized `0..1`

Before publishing a **failed** message (weapons analysis was attempted but broke):

- [ ] `weapon_analysis` block is omitted entirely (not sent as empty or synthetic)
- [ ] `weapon_analysis_error: {"message": "<short reason>"}` is added at the payload root
- [ ] `trace_event: "weapons.failed"` may be added at the root for observability (optional, logged but not persisted)
- [ ] All other canonical fields are preserved unchanged

If these boxes check, the backend will ingest the message, persist the data to the appropriate stores, and (in the success case) expose it through the search API's `weapons_filter` modes. Failed messages become queryable via the `weapon_analysis_error IS NOT NULL` predicate for ops reporting.
