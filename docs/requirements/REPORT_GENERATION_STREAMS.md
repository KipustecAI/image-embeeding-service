# Requirements for `report-generation` — Stream Inputs from `image-embeeding-service`

**Audience:** the [`report-generation`](../../../report-generation/) team. This document tells you what this service (the image embedding backend) will publish to Redis Streams, so you can build consumers that produce real-time alert PDF reports from the events.

**Our side of the contract is implemented and shippable.** Your side needs a new type-1 sub-type consumer, a DTO, a PDF template, and a consumer group registration. See "What we need from you" at the end.

**Cross-references:**
- Producer architecture (our side): [../weapons/CONTRACT.md](../weapons/CONTRACT.md), [../weapons/03_CONSUMER.md](../weapons/03_CONSUMER.md)
- Report-generation type-1 context: [`../../../report-generation/docs/type1_stream_driven/01_IMPLEMENTATION.md`](../../../report-generation/docs/type1_stream_driven/01_IMPLEMENTATION.md)
- Existing report-generation DTO pattern: [`../../../report-generation/src/type1/dto/evidence-analyzed.event.ts`](../../../report-generation/src/type1/dto/evidence-analyzed.event.ts), [`../../../report-generation/src/type1/dto/face-blacklist.event.ts`](../../../report-generation/src/type1/dto/face-blacklist.event.ts)

---

## 1. Context — why these streams exist

The image embedding pipeline already detects weapons (via the upstream `image-weapons-compute` producer) and stores per-image detection data including bounding boxes, classes, and confidences in both PostgreSQL and Qdrant. That data is queryable via the search API today.

What it does **not** do: generate shareable artifacts for human review. Someone seeing a `weapons_filter="only"` search result has the raw image URL and the bbox JSON, but no PDF, no notification, no "send this to the client's WhatsApp". That work belongs in `report-generation`, which already has the type-1 "real-time alert" pattern, a shared PDF pipeline, and a notification gate.

Rather than have two services duplicate image-fetch/render/store logic, we publish a single stream event per weapon-flagged evidence. Your service consumes it, renders the PDF, runs the shared downstream pipeline. Classic event-driven separation of concerns.

Two streams are in scope. **One ships now**, one is a placeholder for a future feature:

| Sub-type | Stream | Source event | Scope |
|---|---|---|---|
| **1D** | `weapons:detected` | New: weapons were detected on an incoming evidence | **Ship now.** Live data flowing today. |
| **1E** | `image:blacklist_match` | Future: a search matched a known-bad image from a configured blacklist | **Future.** Design note only; payload TBD. |

1D is the immediate deliverable. 1E gets a section at the end so you know it's coming, but its payload is not finalized and should not be implemented yet.

---

## 2. Sub-type 1D — `weapons:detected`

### 2.1 Stream

| Field | Value |
|---|---|
| Stream name | `weapons:detected` |
| Redis DB | `3` (same `REDIS_STREAMS_DB` as the other embedding/search streams) |
| Redis host / port / password | Same as the existing streams you already consume — no new connection |
| Recommended consumer group | `report-workers` *(or your existing type-1 group — see "consumer group choice" below)* |
| Dead-letter (suggested) | `weapons:detected:dead` |

**Why this name:** it parallels the existing `face:blacklist_match` and `plate:blacklist_match` you already document in type 1. We considered `reports:weapons_detected` but rejected the `reports:` prefix because it's redundant with the stream's clear destination — the resource-based naming you've standardized is better.

**Consumer group choice.** You can either:
- Reuse your existing type-1 consumer group if you want all type-1 events in one worker pool, or
- Create a dedicated `report-workers-weapons` group for isolated scaling and DLQ

Both work. Our side publishes regardless of how you partition consumption on the read side.

### 2.2 Envelope

Messages are published with `XADD` using the same two-field envelope as every other stream in this ecosystem:

```
XADD weapons:detected * event_type "weapons.detected" payload "<json-encoded payload>"
```

| Envelope key | Value | Required |
|---|---|---|
| `event_type` | `weapons.detected` | **yes** — matches our existing `domain.verb` naming (`embeddings.computed`, `search.vector.computed`, `search.created`). Use this as your handler registration key. |
| `payload` | JSON-encoded `WeaponsDetectedEvent` (see §2.3) | **yes** |

The envelope is intentionally minimal. All semantics live inside `payload`.

### 2.3 Payload — `WeaponsDetectedEvent`

TypeScript interface we recommend you add to [`src/type1/dto/weapons-detected.event.ts`](../../../report-generation/src/type1/dto/):

```typescript
export interface WeaponsDetectedEvent {
  // ── Identity / multi-tenant keys ──
  evidence_id: string;           // UUID
  camera_id: string;             // UUID
  user_id: string;               // UUID — tenant owner
  device_id: string;             // UUID
  app_id: number;                // 1 = Lookia, 2 = Parkia, 3 = Gobia
  infraction_code: string;       // Upstream ETL infraction identifier

  // ── Timing ──
  detected_at: string;           // ISO-8601 UTC, e.g. "2026-04-15T22:38:49.505882Z"

  // ── Evidence-level rollup ──
  summary: {
    has_weapon: boolean;         // Always true in this event; false cases don't publish
    total_detections: number;    // Sum across all per-image detections[]
    classes_detected: string[];  // Deduplicated, e.g. ["persona_armada", "handgun"]
    max_confidence: number;      // Peak confidence across all detections, 0..1
    images_analyzed: number;     // Total frames analyzed (incl. clean frames, not in this event)
    images_with_detections: number;  // Count of frames that made it into the images[] array below
    model_version?: {
      model_a?: string;          // e.g. "persona_armada_1label_V1"
      model_b?: string;          // e.g. "armas_v202"
      person_model?: string;     // e.g. "yolo11s"
    };
  };

  // ── Per-image detail — ONLY frames with detections ──
  // Clean frames from the same evidence are intentionally omitted.
  images: Array<{
    image_name: string;          // Original filename from the upstream ZIP
    image_index: number;         // Index within the evidence's filtered frames
    image_url: string;           // Publicly-accessible MinIO URL, renderable directly
    detections: Array<{
      class_name: string;        // Lowercase ASCII, stable vocabulary
      class_id: number;          // Raw model class id — informational
      confidence: number;        // 0..1
      source?: string;           // "model_a" | "model_b" — which detector produced it
      bbox: {
        x1: number;              // Absolute pixel coordinates, not normalized
        y1: number;
        x2: number;
        y2: number;
      };
    }>;
  }>;
}
```

#### Field-by-field notes

| Field | Notes |
|---|---|
| `evidence_id` | Stable cross-service identifier. Use this as your dedup key (see §2.6). |
| `camera_id` / `device_id` / `app_id` | Multi-tenant routing. If your PDF templates differ by app_id, branch here. |
| `user_id` | Tenant owner. Notifications should route to this user (or their configured channels). |
| `infraction_code` | Human-readable code from the upstream ETL (e.g. `"SMVV8UGE_116_875_20260410100151"`). Useful for PDF filenames and notification subject lines. |
| `detected_at` | When the embedding service persisted the detection, not when the camera captured the frame. For capture time, read from the source system via `evidence_id`. |
| `summary.has_weapon` | Always `true` in this event. Included for symmetry with how our DB stores it and because it makes downstream filters self-describing. |
| `summary.classes_detected` | Deduplicated union across all images. Sort order is not guaranteed; sort on your side if it matters for PDF layout. |
| `summary.max_confidence` | Peak across the entire evidence. Useful for a "priority" indicator on the PDF header. |
| `summary.images_analyzed` vs `images_with_detections` | `images_analyzed` is the total we inspected; `images_with_detections` is the count you'll receive in `images[]`. `images_analyzed - images_with_detections` = frames that were analyzed clean and excluded from this event. |
| `summary.model_version` | Optional, but the upstream producer publishes it and we preserve it. Useful for audit trails in the PDF ("Analyzed by armas_v202"). Absent if the upstream didn't include it. |
| `images[]` | **Only frames that had ≥1 detection.** A 9-frame evidence with weapons on 5 frames sends a 5-element array. We do the filter on publish so you don't have to. |
| `images[].image_url` | Public MinIO URL. Your service downloads it and renders bboxes on top. No auth headers needed. |
| `images[].detections[].bbox` | **Absolute pixel coordinates**, not normalized 0..1. You'll need to read image dimensions yourself (from the downloaded bytes) if you want to scale for display. |
| `images[].detections[].source` | Producer-side detector identifier — `"model_a"` (full-frame) or `"model_b"` (2-stage person-crop). Optional, but present in all live data today. Useful for reports that want to show detection provenance. |

### 2.4 Example payload — real data

This is taken from actual rows you saw in the database (minimally cleaned up for readability). A single real evidence with 5 frames, all `persona_armada` detections from `model_a`:

```json
{
  "evidence_id": "cead4516-3dcc-43b0-aebb-8fab34f8800d",
  "camera_id": "54c398ab-ea0d-4085-875b-af816eb00b03",
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "app_id": 1,
  "infraction_code": "SMVV8UGE_116_875_20260410100151",
  "detected_at": "2026-04-15T22:38:49.505882Z",
  "summary": {
    "has_weapon": true,
    "total_detections": 5,
    "classes_detected": ["persona_armada"],
    "max_confidence": 0.8203153014183044,
    "images_analyzed": 7,
    "images_with_detections": 5,
    "model_version": {
      "model_a": "persona_armada_1label_V1",
      "model_b": "armas_v202",
      "person_model": "yolo11s"
    }
  },
  "images": [
    {
      "image_name": "20260415-183821_003261.jpg",
      "image_index": 1,
      "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/54c398ab-ea0d-4085-875b-af816eb00b03/cead4516-3dcc-43b0-aebb-8fab34f8800d/20260415-183821_003261.jpg",
      "detections": [
        {
          "class_name": "persona_armada",
          "class_id": 0,
          "confidence": 0.8203153014183044,
          "source": "model_a",
          "bbox": { "x1": 106, "y1": 91, "x2": 270, "y2": 600 }
        }
      ]
    },
    {
      "image_name": "20260415-183820_003249.jpg",
      "image_index": 2,
      "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/54c398ab-ea0d-4085-875b-af816eb00b03/cead4516-3dcc-43b0-aebb-8fab34f8800d/20260415-183820_003249.jpg",
      "detections": [
        {
          "class_name": "persona_armada",
          "class_id": 0,
          "confidence": 0.7031412720680237,
          "source": "model_a",
          "bbox": { "x1": 66, "y1": 99, "x2": 257, "y2": 677 }
        }
      ]
    },
    {
      "image_name": "20260415-183821_003259.jpg",
      "image_index": 3,
      "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/54c398ab-ea0d-4085-875b-af816eb00b03/cead4516-3dcc-43b0-aebb-8fab34f8800d/20260415-183821_003259.jpg",
      "detections": [
        {
          "class_name": "persona_armada",
          "class_id": 0,
          "confidence": 0.7148565053939819,
          "source": "model_a",
          "bbox": { "x1": 33, "y1": 89, "x2": 263, "y2": 670 }
        }
      ]
    },
    {
      "image_name": "20260415-183820_003251.jpg",
      "image_index": 5,
      "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/54c398ab-ea0d-4085-875b-af816eb00b03/cead4516-3dcc-43b0-aebb-8fab34f8800d/20260415-183820_003251.jpg",
      "detections": [
        {
          "class_name": "persona_armada",
          "class_id": 0,
          "confidence": 0.7857820391654968,
          "source": "model_a",
          "bbox": { "x1": 48, "y1": 88, "x2": 243, "y2": 667 }
        }
      ]
    },
    {
      "image_name": "20260415-183820_003253.jpg",
      "image_index": 7,
      "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/54c398ab-ea0d-4085-875b-af816eb00b03/cead4516-3dcc-43b0-aebb-8fab34f8800d/20260415-183820_003253.jpg",
      "detections": [
        {
          "class_name": "persona_armada",
          "class_id": 0,
          "confidence": 0.7980067729949951,
          "source": "model_a",
          "bbox": { "x1": 60, "y1": 90, "x2": 245, "y2": 653 }
        }
      ]
    }
  ]
}
```

Notice: `images_analyzed=7` but `images[].length=5` — frames 4 and 6 were analyzed clean and omitted from the event.

### 2.5 Trigger semantics — when we publish

We publish exactly one `weapons:detected` event per evidence, under **all** of these conditions:

1. An `embeddings:results` message successfully ingests (vectors stored in Qdrant, DB row committed)
2. The payload carried a `weapon_analysis` block (i.e. `weapon_analyzed=true`)
3. `weapon_analysis.summary.has_weapon=true` AND `total_detections > 0`

We **do not** publish when:

- The evidence was never analyzed (legacy path, routing skipped compute-weapons)
- The evidence was analyzed but came out clean (`analyzed_clean` state — candidates for the false-positive review queue, but not worth a report)
- The evidence was attempted but failed (`weapon_analysis_error` populated) — no detections means nothing to render
- Our ingest transaction failed (DB or Qdrant write error)

**Timing:** publish happens **after** the DB+Qdrant transaction commits. Publishing failure does **not** roll back the ingest — reports are non-critical (see §2.7).

### 2.6 Dedup responsibility

If our service ever re-processes the same evidence (reprocessing after a migration, or a compute-weapons retry that bypasses our dedup check), we may publish the same `evidence_id` more than once. **Dedup is your responsibility.**

Recommended approach: before generating a PDF for a received event, check whether a report with that `evidence_id` already exists in your own reports store. If yes, skip. This is cheap (indexed lookup), deterministic, and means a double-publish from our side just results in a no-op on your side.

We could try to dedup on our side using an outbox or a per-`evidence_id` publish log, but it'd add complexity to our hot path for a rare edge case. The receiver-side check is simpler and more resilient.

### 2.7 Delivery guarantees — fire-and-forget from our side

Our publisher is fire-and-forget. If the XADD fails for any reason (Redis connection drop, timeout, Redis OOM), we log the failure at ERROR level and continue. **The ingest path is never blocked by report publishing.**

| Failure mode | Our behavior | Your impact |
|---|---|---|
| Redis connection drops exactly at publish time | Log error, continue ingest, move on | Event lost for that evidence. Very rare. |
| Redis Streams back-pressure (consumer group too far behind) | XADD still succeeds; Redis buffers. Your DLQ handles it. | Your problem — adjust consumer group parallelism or DLQ policy |
| Envelope malformed (should never happen) | Log error, continue ingest | Event lost. Indicates our bug — alert us. |

**No retries on our side.** If you need at-least-once, the receiver-side consumer group's pending-entry-recovery mechanism already provides it for messages that made it to the stream. Messages that never made it (case #1 above) are the only true data loss, and we accept that tradeoff for ingest-path simplicity.

If this trade becomes unacceptable later — e.g. you want delivery guarantees tied to customer SLAs — we can add a transactional outbox table on our side that retries publishes with exponential backoff. That's a real engineering effort; let's not pay for it speculatively.

### 2.8 What we guarantee about event ordering

**We do not guarantee ordering across evidences.** If evidence A and evidence B are processed by concurrent workers, their publish order on the stream is non-deterministic. Your consumer must handle events in arbitrary order.

**Within a single evidence**, there is only ever one event, so ordering within an evidence is a non-issue.

**We do guarantee that a `weapons:detected` event is only published after the corresponding evidence is fully committed to our DB + Qdrant.** If your consumer wanted to cross-reference via our search API (you shouldn't need to, but just in case), the data will be readable at the time you receive the event.

---

## 3. Sub-type 1E — `image:blacklist_match` *(future, design note only)*

**Status:** not in scope for the current ship. Do **not** implement a consumer for this yet. The payload shape will be finalized when the blacklist feature is actually designed on our side.

**What it will be:** when a user runs a similarity search (`POST /api/v1/search`) and the matches include an image that lives in a configured "blacklist" set (e.g. known-bad evidences flagged for alerting), we'll publish an `image:blacklist_match` event so your service can generate a match-alert report.

**Naming rationale:** parallels your existing `face:blacklist_match` and `plate:blacklist_match` sub-types. It's the image-similarity analog.

**Open questions on our side (not yours to answer):**
- How is the blacklist stored? Separate Qdrant collection? A boolean `is_blacklisted` on existing points? A separate DB table of `evidence_id`s? — TBD
- Who populates the blacklist? Manual admin action? Automated from another source? — TBD
- Is it app-scoped, user-scoped, or global? — TBD
- Does publishing happen at search completion, or only when a new evidence matches an existing blacklist? — TBD

**Rough payload shape** (subject to change — do **not** build to this yet):

```typescript
export interface ImageBlacklistMatchEvent {
  search_id: string;             // Our search request ID
  user_id: string;               // Tenant running the search
  app_id: number;

  // The blacklist entry that matched
  matched_evidence_id: string;
  matched_image_url: string;
  matched_blacklist_reason?: string;

  // The query that triggered it
  query_image_url: string;
  similarity_score: number;

  // Matching frame detail
  // (probably similar structure to WeaponsDetectedEvent.images[0])
  matched_at: string;
}
```

Take this as a "here's the rough direction" signal, not a contract. When we're ready to ship 1E, we'll add a `REPORT_IMAGE_BLACKLIST.md` sibling doc with the real shape.

---

## 4. What we need from you

To consume `weapons:detected` (1D) end-to-end:

| Deliverable | Where | Effort |
|---|---|---|
| 1. DTO interface | `src/type1/dto/weapons-detected.event.ts` — copy-paste the TS from §2.3 | 5 min |
| 2. Stream consumer | `src/type1/consumers/weapons-detected.consumer.ts` — extend `BaseStreamConsumer`, follow the `evidence-analyzed.consumer.ts` pattern. `streamName = 'weapons:detected'` | 30 min |
| 3. Consumer group registration | Your type1 module wiring — same pattern as 1A/1B | 5 min |
| 4. PDF template | A new type-1D template. Input: downloaded `image_url` + the `detections[].bbox` list. Output: per-image page with boxes drawn. | Depends on template complexity |
| 5. Notification gate hookup | Feed the event into your existing "check notification gate → build PDF → MinIO → notify" pipeline — shared across all type-1 sub-types already | Should be zero if the shared pipeline is parametric on the event |
| 6. Config for stream name | Your config file — add `REDIS_STREAM_WEAPONS_DETECTED=weapons:detected` | 2 min |

**Rendering bboxes.** Each `detections[].bbox` is in absolute pixel coordinates. When you render:

1. Download the image from `image_url` — public MinIO, no auth needed
2. Read its actual width/height from the decoded bytes (don't trust client-side scaling)
3. Draw each bbox as a rectangle over the top, with color/label per `class_name`
4. Optionally label with `class_name` + formatted confidence (`"persona_armada 0.82"`)
5. Crop to a reasonable display size before embedding in the PDF

Our side does **not** pre-render annotated images. We considered it and rejected baking bboxes into the stored image for two reasons: (a) it would break the false-positive review workflow, which needs raw images for human audit; (b) it would freeze a specific model version into pixels that are meant to be source of truth. You always get raw images with structured detection data — render on your terms.

---

## 5. Configuration on our side (for your reference only)

We add two env vars to `image-embeeding-service` to let you override the stream name in dev/staging/prod:

```env
STREAM_REPORTS_WEAPONS_DETECTED=weapons:detected
STREAM_REPORTS_IMAGE_BLACKLIST_MATCH=image:blacklist_match
```

Defaults shown are what we'll ship with. If you prefer different names (e.g. environment-specific suffixes), tell us and we'll match — easier than both services coordinating string literals in code.

---

## 6. Open questions — please confirm

Before we ship our producer side, confirm the following four decisions:

1. **Stream name `weapons:detected`** — OK? Or do you prefer `reports:weapons_detected` / something else?
2. **Envelope `event_type = "weapons.detected"`** — OK? Matches our existing `domain.verb` pattern; matches a common convention on your side's handler registration.
3. **Consumer group** — will you use your existing type-1 group or add a dedicated one? (Implementation detail on your side — doesn't change our publish.)
4. **Dedup on your side via `evidence_id` lookup** — agreed, or do you want us to carry an explicit `report_idempotency_key`?

**Point of contact** on our side: whoever is implementing [`src/streams/producer.py`](../../src/streams/producer.py) changes in this repo. Open a PR comment on the shipping commit or ping directly.

---

## 7. What happens after you confirm

1. You sign off on the four questions in §6
2. We implement the producer helper + wire it into [`embedding_results_consumer.py`](../../src/streams/embedding_results_consumer.py) — ~1 hour, single commit
3. We publish the first `weapons:detected` event against staging, you verify your consumer picks it up
4. You build the PDF template and wire the notification gate
5. We both flip the config in production and watch the stream

Total coordination cost is minimal because the shape is self-contained and the stream infrastructure already exists on both sides. Biggest single effort is your PDF template, which is orthogonal to the stream work.
