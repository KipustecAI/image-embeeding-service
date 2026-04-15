# Phase 0: Context, Payload, Data Model Decisions

## Problem statement

Today the `embedding_results_consumer` at [src/streams/embedding_results_consumer.py](../../src/streams/embedding_results_consumer.py) reads the `embeddings:results` stream and stores each vector in Qdrant + PostgreSQL. The payload schema is: `{evidence_id, camera_id, user_id, device_id, app_id, infraction_code, zip_url, embeddings[], input_count, filtered_count, embedded_count}`.

A new upstream service — **`compute-weapons`** — optionally sits between `image compute` and this backend. When routing sends evidence through it, it enriches the same message with a `weapon_analysis` block containing per-image detections (bboxes + class + confidence) and an evidence-level summary. When routing skips it, the message arrives in exactly the shape we handle today.

We need to:

1. Accept the enriched payload without breaking the legacy path
2. Persist per-image detections, per-evidence summary, and filterable flags
3. Expose search-time filters for four distinct user intents (all / only weapons / exclude weapons / analyzed-clean review queue)
4. Support subset filtering on weapon classes (e.g. "only handguns")

## Real payload sample (from the compute-weapons service)

Success event (vectors truncated for readability):

```json
{
  "event_type": "weapons.analyzed",
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
      }
    ],
    "summary": {
      "images_analyzed": 9,
      "images_with_detections": 2,
      "total_detections": 3,
      "classes_detected": ["handgun", "knife"],
      "max_confidence": 0.873,
      "has_weapon": true
    }
  }
}
```

### Important note on `event_type` — resolved in [CONTRACT.md](CONTRACT.md)

The XADD envelope **must** carry `event_type: "embeddings.computed"` (the existing handler registration). The compute-weapons service may put `"event_type": "weapons.analyzed"` inside the JSON body as a trace label if useful for observability, but the backend consumer does not read body-level event types — it dispatches on the envelope.

Producer-side contract lives in [CONTRACT.md](CONTRACT.md). If the compute-weapons team ever wants to switch to a dedicated envelope event type, that's a breaking change and requires coordinating a second handler registration in the backend first — see CONTRACT.md section 6 ("Versioning").

## The state cube

Originally designed as a two-boolean cube with three reachable states. After capturing the producer's failure pass-through field ([`weapon_analysis_error`](CONTRACT.md#51-the-fourth-reachable-state-attempted-failed)), there are now **four reachable states**, distinguished by a third axis (the error column):

| `weapon_analyzed` | `has_weapon` | `weapon_analysis_error` | Meaning | Example use case |
|---|---|---|---|---|
| `false` | `false` | `NULL` | Never analyzed — legacy image or routing skipped compute-weapons | Broad similarity search, backward compatibility |
| `false` | `false` | `"reason"` | **Attempted and failed** — compute-weapons tried but broke (ZIP timeout, model OOM, corrupt archive, etc.) | Ops observability queries, retry candidates |
| `true` | `false` | `NULL` | Analyzed, nothing detected | **False-positive review queue** — humans audit these for missed weapons |
| `true` | `true` | `NULL` | Analyzed with at least one detection | Weapon flagging, reports, alerts |

The `(false, true)` combination is impossible by construction: we only set `has_weapon=true` when `weapon_analyzed=true`. The error column only carries a value for the "attempted, failed" row — in every other state it is `NULL`.

The error-state row is new (added via migration `c8e5a7b2d4f9`) and only exists for messages produced after the compute-weapons service started publishing `weapon_analysis_error` at the payload root. See [CONTRACT.md §5.1](CONTRACT.md) for the producer contract and ops query.

## Decisions log

### Why per-image, not per-evidence?

Each Qdrant point is one image. A single evidence can have 9 frames with only 2 showing a weapon. If `has_weapon` were an evidence-level flag, a user searching for "similar images with a handgun" would get all 9 frames back, including the 7 weapon-free ones — noise. Per-image filtering matches the user's mental model: they want *the images* with weapons, not *the evidences that contain some images with weapons*.

The evidence-level summary still lives on `embedding_requests` for reports ("how many evidences this week had weapons") — that's a separate SQL query, not a Qdrant filter.

### Why two booleans, not a single nullable boolean?

A single `has_weapon: bool | null` collapses two meaningfully different states into `null`:
- Never analyzed (legacy / routing skipped)
- Analyzed and came out clean

The user's false-positive review workflow specifically needs to find the "analyzed and clean" set so humans can reclassify potential false negatives. A nullable bool can't express that intent as a queryable index.

Qdrant-side argument: `MatchValue` works cleanly on scalars; filtering on "field is not null" uses `IsEmptyCondition`, which is awkward and undermines payload-index performance. Two booleans keep the filter path identical to the existing `user_id`/`device_id` pattern.

**Post-hoc validation:** this decision held up when the producer later started publishing `weapon_analysis_error` for the failure path. A nullable-bool design would have needed a third state in `has_weapon` itself ("was attempted but broke"), which is ontologically a failure mode, not a detection result. Instead, the error lives on its own TEXT column and is orthogonal to the detection state — the existing filter semantics keep working unchanged, and the error state is expressible via `weapon_analysis_error IS NOT NULL` without touching the Qdrant index. Two booleans + one orthogonal column > one nullable bool.

### Why `keyword[]` for `weapon_classes`, not an enum or separate table?

- Enum would require code-side migrations every time the compute-weapons model adds a class (handgun, knife, rifle, taser, …). Keyword keeps it schema-less.
- A separate `weapon_detections` table would be over-normalized: 99% of queries fetch detections alongside the image row, so we'd always join. JSONB keeps it zero-join.
- Qdrant `keyword` index with `MatchAny` gives us subset matching for free: `weapon_classes=["handgun"]` matches points where `"handgun" ∈ stored_classes`.

### Why JSONB for per-image detections, not a normalized `weapon_detections` table?

- Bboxes are inherently JSON-shaped (`{x1,y1,x2,y2}`)
- We always retrieve detections bound to a specific image, never as a top-level query ("show me all bboxes across the whole database")
- Start with JSONB; if a real query pattern emerges ("count handgun detections by confidence bucket"), promote to a table later. YAGNI wins here.

### Why not use the existing `evidence_embeddings.json_data` JSONB column?

[EvidenceEmbeddingRecord](../../src/db/models/evidence_embedding.py) already has a `json_data` JSONB column. We could nest `weapon_detections` inside it, but:

- A dedicated column is independently indexable (`GIN(weapon_detections)` for bbox queries if we ever need them)
- Separation of concerns — `json_data` holds embedding metadata (image_index, user_id, etc.); weapon data lives on its own column
- Backfill and querying are simpler with flat columns

The extra column costs one migration line; the clarity is worth it.

## Scope boundaries (out of scope for this plan)

- **Historical backfill.** Existing Qdrant points and DB rows stay at `weapon_analyzed=false, has_weapon=false`. A separate reprocessing initiative can fix this if needed.
- **Weapon-detection model changes.** The class list, thresholds, and bbox format are owned by `compute-weapons`. This plan takes the payload as-is.
- **Notifications / alerting UI.** The diagram shows a downstream "Notifications and Reports" consumer — that's a separate repo, unblocked once `embedding_requests.has_weapon` is queryable.
- **Re-scoring existing searches.** Completed searches keep their stored query vector; they are not re-run automatically when the weapon flags are added.
- **Rate-limiting or quota on weapon searches.** If the "only" / "analyzed_clean" filters become popular enough to degrade Qdrant performance, that's a separate optimization (likely via payload pre-filtering or a dedicated collection).
