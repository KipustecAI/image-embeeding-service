# Outbound Streams to `lookia-dw`

**Audience:** the lookia-dw team and any agent building a consumer for the streams we publish to them.

**Status:** **accepted by lookia-dw 2026-05-16 — this is the wire-format authority.** Both lifecycle simplifications (§4.6 drop `.weapon_analyzed`; §4.7 single INSERT-only `.upserted`) and the renegotiated MAXLEN values (500k / 500k+2M backfill) were accepted without edit. Producer-side implementation tracking lives in [`LOOKIA_DW_PUBLISHERS.md`](LOOKIA_DW_PUBLISHERS.md); this doc is what consumers build against.

DW-side state at acceptance: migration 011 already applied on Neon prod (6 tables for our slice + 1 face symmetry + 2 widenings); their worker + bulk_upsert build runs in parallel with our producer ship. When we push to main, DW expects to verify "within minutes."

**Companion docs**
- [`LOOKIA_DW_PUBLISHERS.md`](LOOKIA_DW_PUBLISHERS.md) — negotiation tracker (what was asked, what was negotiated, open items)
- [`REPORT_GENERATION_STREAMS.md`](REPORT_GENERATION_STREAMS.md) — sister outbound contract (different consumer; same wire format)
- [`../../src/db/models/constants.py`](../../src/db/models/constants.py) — status enum source of truth

---

## 1. Context

We publish 7 fat-event streams that mirror lifecycle and state changes on the 7 source tables in `image-embedding-db`. Streams are append-only Redis streams on DB **3** at `31.220.104.212:6379`. The DW consumer reads them, validates with Pydantic, and lands rows into its dim/fact tables.

The producer is `image-embeeding-service`. Publishing follows the same fire-and-forget pattern as our existing `weapons:detected` and `image:blacklist_match` publishers — try/except, log on failure, never re-raise. A DB write commits even if the corresponding XADD fails. Recovery for missed events is via direct-SQL load on the DW side (see [`LOOKIA_DW_PUBLISHERS.md`](LOOKIA_DW_PUBLISHERS.md) §"Lesson 4").

---

## 2. Envelope format

Every event on every stream is a Redis Stream entry with **two top-level fields**:

| Field | Type | Notes |
|---|---|---|
| `event_type` | string | One of the values listed per-stream below. Used by the DW router to dispatch to the right Pydantic entity. |
| `payload` | string (JSON) | JSON-encoded object. Use `json.loads()` on read. Datetimes are ISO 8601 with offset (`...+00:00` or `Z`); UUIDs are lowercase strings. |

Example XADD shape:

```python
redis.xadd(
    "image_embedding_request:raw",
    {
        "event_type": "image_embedding_request.completed",
        "payload": json.dumps(row_dict, default=str),
    },
    maxlen=500_000,
    approximate=True,
)
```

`XRANGE` verification:

```bash
redis-cli -h 31.220.104.212 -p 6379 -a videoserver_redis_password -n 3 \
  XREVRANGE image_embedding_request:raw + - COUNT 1
```

Should show exactly the two fields. Any third field or a `data` field instead is a producer-side bug we want to know about (see [`LOOKIA_DW_PUBLISHERS.md`](LOOKIA_DW_PUBLISHERS.md) §"Lesson 2").

---

## 3. Status enum reference

Pulled from [`src/db/models/constants.py`](../../src/db/models/constants.py). DW persists raw ints + a sibling enum-name dim for joins, same pattern as `app_type` in `t_dim_users`.

| Enum | Field | Values |
|---|---|---|
| `EmbeddingRequestStatus` | `embedding_requests.status` | 1=TO_WORK, 2=WORKING, 3=EMBEDDED, 4=DONE, 5=ERROR. **4=DONE defined but unused in prod** — treat 3 as terminal-success, 5 as terminal-failure. |
| `SearchRequestStatus` | `search_requests.status` | 1=TO_WORK, 2=WORKING, 3=COMPLETED, 4=ERROR. No 5. |
| `SimilarityStatus` | `search_requests.similarity_status` | 1=NO_MATCHES, 2=MATCHES_FOUND. **Result indicator only**, set when status reaches 3 (COMPLETED). |
| `BlacklistEntryStatus` | `blacklist_image_entries.status` | 1=CREATED, 2=PROCESSING, 3=INDEXED, 4=UPDATING, 5=ERROR. |
| `BlacklistReferenceStatus` | `blacklist_image_references.status` | 1=TO_PROCESS, 2=PROCESSING, 3=PROCESSED, 4=ERROR. |

**Not enums on our side** (raw pass-through values, no controlled vocabulary on producer):

| Field | Type | Notes |
|---|---|---|
| `embedding_requests.app_id` | int | Pass-through from gateway `X-App-Type` header. Currently observed: `1`, `4`, `NULL`. Canonical meaning owned by the platform team. |
| `embedding_requests.weapon_classes[]` | string[] | YOLO classes from upstream `image-weapons-compute`. Currently observed: `arma`, `persona_armada`, `persona`, `arma_fuego`, `objeto`, `celular`, `mochila`. Vocabulary changes if the producer model upgrades. |

---

## 4. Per-stream contracts

### 4.1 `image_search_request:raw` — search lifecycle

**Trigger:** lifecycle transitions only. NOT every column UPDATE.

| event_type | Fires when |
|---|---|
| `image_search.created` | `POST /api/v1/search` — row inserted at status=1 |
| `image_search.completed` | Status transitions to 3 (consumer set on `search_results_consumer._process_search_result`) |
| `image_search.failed` | Status transitions to 4 (error) |

**Payload:**

```json
{
  "id": "uuid",
  "search_id": "string",
  "user_id": "string",
  "image_url": "https://...",
  "status": 1,
  "similarity_status": 1,
  "threshold": 0.75,
  "max_results": 50,
  "search_metadata": {},
  "total_matches": 7,
  "results_key": "string|null",
  "qdrant_query_point_id": "string|null",
  "worker_id": "string|null",
  "error_message": "text|null",
  "retry_count": 0,
  "processing_started_at": "ISO8601|null",
  "processing_completed_at": "ISO8601|null",
  "stream_message_id": "string|null",
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

`created_at` is the **load-bearing** field for `date_sk` derivation on the DW side.

**Example (`image_search.completed`):**

```json
{
  "id": "770e8400-e29b-41d4-a716-446655440000",
  "search_id": "770e8400-e29b-41d4-a716-446655440000",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "image_url": "https://storage.example.com/query.jpg",
  "status": 3,
  "similarity_status": 2,
  "threshold": 0.75,
  "max_results": 50,
  "search_metadata": {"camera_id": "660e8400-..."},
  "total_matches": 7,
  "results_key": null,
  "qdrant_query_point_id": "326b5f83-...",
  "worker_id": null,
  "error_message": null,
  "retry_count": 0,
  "processing_started_at": "2026-05-14T20:47:07.260+00:00",
  "processing_completed_at": "2026-05-14T20:47:14.318+00:00",
  "stream_message_id": "1778791622621-0",
  "created_at": "2026-05-14T20:47:02.221+00:00",
  "updated_at": "2026-05-14T20:47:14.318+00:00"
}
```

**MAXLEN:** 10,000 (search API dormant in prod — 8 rows total at time of writing).

---

### 4.2 `image_search_match:raw` — terminal-completion exploded match list

**Why a separate stream:** one search has N matches. We emit ONE event per completed search carrying the full `matches[]` array, not N events. DW consumer explodes into N rows on landing.

**Trigger:** fires once when `image_search_request.status` reaches 3 (COMPLETED) **AND** `total_matches > 0`. Empty-result searches do not produce an event.

**event_type:** `image_search.matched` (single value).

**Payload:**

```json
{
  "search_request_id": "uuid",
  "user_id": "string",
  "image_url": "https://...",
  "total_matches": 7,
  "matches": [
    {
      "id": "uuid",
      "evidence_id": "string",
      "camera_id": "string|null",
      "similarity_score": 0.876,
      "image_url": "string|null",
      "match_metadata": {}
    }
  ],
  "timestamp": "ISO8601"
}
```

`timestamp` (envelope-level) is load-bearing for `date_sk` — set to the publish moment.

**Example:**

```json
{
  "search_request_id": "770e8400-e29b-41d4-a716-446655440000",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "image_url": "https://storage.example.com/query.jpg",
  "total_matches": 2,
  "matches": [
    {
      "id": "8a1b2c3d-4e5f-6789-abcd-ef0123456789",
      "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
      "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
      "similarity_score": 0.893,
      "image_url": "https://minio.lookia.mx/embeddings/frame_3.jpg",
      "match_metadata": {"image_index": 3}
    },
    {
      "id": "9b2c3d4e-5f67-8901-bcde-f12345678901",
      "evidence_id": "ee845f25-28c3-4e7d-b1a5-7f8a3ad1fb6c",
      "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
      "similarity_score": 0.812,
      "image_url": "https://minio.lookia.mx/embeddings/frame_7.jpg",
      "match_metadata": {"image_index": 7}
    }
  ],
  "timestamp": "2026-05-14T20:47:14.510+00:00"
}
```

**MAXLEN:** 10,000.

**Edge cases:**
- A search that completes with `total_matches = 0` → no event (intentional).
- A search that errors out (status=4) → no `image_search.matched` event. The lifecycle `.failed` event on `image_search_request:raw` is the only signal.
- Recalculation (when `processing_completed_at` updates without going through `created → completed`) — **not in scope for v1**. Will require a separate event type if DW wants it.

---

### 4.3 `blacklist_image_entry:raw` — blacklist entry CRUD

**Trigger:** every INSERT (POST), every UPDATE (PATCH), every soft-archive (DELETE without `?permanent=true`). **Includes mutations that bump `blacklist_version` from any path** — including reference add/remove operations that touch the parent row.

**event_type:** `blacklist_image_entry.upserted` (single value).

**Payload (PII-redacted — `name` hashed, never raw):**

```json
{
  "id": "uuid",
  "name_hash": "hex16chars",
  "category": "string|null",
  "status": 1,
  "active": true,
  "is_archived": false,
  "user_id": "string",
  "blacklist_version": 1,
  "match_threshold": 0.85,
  "json_data": {},
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

**Hash recipe (locked):** `name_hash = sha256(name.encode('utf-8')).hexdigest()[:16]`. Matches the face team's blacklist_persons recipe. We will never publish the raw `name`. Our producer has a regression test asserting `"name"` is not a key in any `blacklist_image_entry:raw` payload.

**Example:**

```json
{
  "id": "a1b2c3d4-1111-2222-3333-444444444444",
  "name_hash": "3a8e9c2b7d4f1e62",
  "category": "vehicle",
  "status": 3,
  "active": true,
  "is_archived": false,
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "blacklist_version": 1,
  "match_threshold": 0.88,
  "json_data": {"case_id": "CRIM-2025-042"},
  "created_at": "2026-04-18T14:22:03+00:00",
  "updated_at": "2026-04-18T14:22:03+00:00"
}
```

**MAXLEN:** 5,000.

**Does NOT fire on:** bulk-import paths (if added later — direct-load on DW side covers this).

---

### 4.4 `blacklist_image_reference:raw` — per-image reference CRUD

**Trigger:** every INSERT (image attached to an entry), every UPDATE on `status` / `error_message` / `retry_count` (embed lifecycle progression).

**event_type:** `blacklist_image_reference.upserted`.

**Payload:**

```json
{
  "id": "uuid",
  "entry_id": "uuid",
  "image_url": "https://...",
  "image_type": "reference",
  "status": 1,
  "error_message": "string|null",
  "retry_count": 0,
  "json_data": {},
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

**Example (status transition to PROCESSED):**

```json
{
  "id": "e5f6a7b8-1111-2222-3333-444444444444",
  "entry_id": "a1b2c3d4-1111-2222-3333-444444444444",
  "image_url": "https://minio.lookia.mx/blacklist/user-3996/ref_1.jpg",
  "image_type": "reference",
  "status": 3,
  "error_message": null,
  "retry_count": 0,
  "json_data": {"image_dimensions": [1920, 1080]},
  "created_at": "2026-04-18T14:22:10+00:00",
  "updated_at": "2026-04-18T14:22:14+00:00"
}
```

**MAXLEN:** 10,000.

---

### 4.5 `blacklist_image_embedding:raw` — embeddings (immutable, INSERT only)

**Trigger:** every INSERT into `blacklist_image_embeddings`. **NO updates** — embeddings are immutable; model upgrades create new rows.

**event_type:** `blacklist_image_embedding.created`.

**Payload:**

```json
{
  "id": "uuid",
  "entry_id": "uuid",
  "reference_id": "uuid",
  "qdrant_point_id": "string",
  "model_version": "clip-vit-b-32",
  "json_data": {},
  "created_at": "ISO8601"
}
```

**Example:**

```json
{
  "id": "f1e2d3c4-...",
  "entry_id": "a1b2c3d4-1111-2222-3333-444444444444",
  "reference_id": "e5f6a7b8-1111-2222-3333-444444444444",
  "qdrant_point_id": "326b5f83-6d88-4c34-b82d-c46ccaa104b1",
  "model_version": "clip-vit-b-32",
  "json_data": {},
  "created_at": "2026-04-18T14:22:14+00:00"
}
```

**MAXLEN:** 10,000.

**Why `model_version` matters to DW:** when CLIP upgrades (e.g. ViT-B-32 → ViT-L-14), every blacklist entry gets re-embedded — producing NEW rows with new `model_version` values. The DW captures this naturally as event-grain (one row per embedding), supporting "model version distribution" / "re-embed completion %" / "when did we migrate?" queries without losing history.

---

### 4.6 `image_embedding_request:raw` — evidence-embedding lifecycle ⭐ Tier 3

**Why high-value:** this is the only place in the platform where weapon detection metadata lives. `has_weapon`, `weapon_classes`, `weapon_max_confidence`, `weapon_summary`, `weapon_analysis_error` are all on this row. Skipping this stream means DW cannot report on weapons at all.

**Trigger:** lifecycle transitions only.

| event_type | Fires when |
|---|---|
| `image_embedding_request.created` | INSERT — row created at status=1 (TO_WORK) |
| `image_embedding_request.completed` | Status transitions to 3 (EMBEDDED) — **carries final `weapon_analyzed` / `has_weapon` / `weapon_classes` / `weapon_max_confidence` / `weapon_summary` values** |
| `image_embedding_request.failed` | Status transitions to 5 (ERROR) |

**Note on `.weapon_analyzed`:** the original DW spec proposed a separate `.weapon_analyzed` event when the flag flips false→true post-INSERT. Our consumer writes the row atomically — `weapon_analyzed` is `true` (if upstream sent `weapon_analysis`) or `false` (if not) at INSERT time. **No async update path exists.** Per DW's acceptance 2026-05-16, this event type is **dropped from the contract**; DW keys off `weapon_analyzed=true` in the `.completed` payload instead. Producer emits exactly the three event types listed in the table above.

**Payload:**

```json
{
  "id": "uuid",
  "evidence_id": "string",
  "camera_id": "string",
  "user_id": "string|null",
  "device_id": "string|null",
  "app_id": 1,
  "infraction_code": "string|null",
  "category": "string|null",
  "status": 3,
  "image_urls": ["https://..."],
  "worker_id": "string|null",
  "error_message": "string|null",
  "retry_count": 0,
  "processing_started_at": "ISO8601|null",
  "processing_completed_at": "ISO8601|null",
  "stream_message_id": "string|null",
  "weapon_analyzed": true,
  "has_weapon": true,
  "weapon_classes": ["arma_fuego"],
  "weapon_max_confidence": 0.7707,
  "weapon_summary": {},
  "weapon_analysis_error": "string|null",
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

`created_at` load-bearing for `date_sk` derivation.

**Example (`image_embedding_request.completed` with weapons):**

```json
{
  "id": "38e0022d-da52-4271-b44a-1201be347c00",
  "evidence_id": "38e0022d-da52-4271-b44a-1201be347c00",
  "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "app_id": 1,
  "infraction_code": "SMVV8UGE_116_875_20260514204707",
  "category": null,
  "status": 3,
  "image_urls": [
    "https://minio.lookia.mx/lucam-assets/embeddings/.../frame_0.jpg",
    "https://minio.lookia.mx/lucam-assets/embeddings/.../frame_1.jpg"
  ],
  "worker_id": null,
  "error_message": null,
  "retry_count": 0,
  "processing_started_at": "2026-05-14T20:47:07.260+00:00",
  "processing_completed_at": "2026-05-14T20:47:07.310+00:00",
  "stream_message_id": "1778791622621-0",
  "weapon_analyzed": true,
  "has_weapon": true,
  "weapon_classes": ["arma_fuego"],
  "weapon_max_confidence": 0.7707,
  "weapon_summary": {
    "total_detections": 2,
    "images_analyzed": 9,
    "images_with_detections": 1,
    "model_version": "yolo-v8-weapons-v1"
  },
  "weapon_analysis_error": null,
  "created_at": "2026-05-14T20:47:07.260+00:00",
  "updated_at": "2026-05-14T20:47:07.310+00:00"
}
```

**MAXLEN:** 500,000 (renegotiated up from the original 100k — observed volume is ~38k/month, so 500k gives ~13 months retention; 100k would be 2.6 months and trim during normal operation).

**Does NOT fire on:** bulk historical re-process. Direct-load on DW side covers backfill.

**Edge cases:**
- `weapon_analyzed=false` + `weapon_analysis_error=null` → upstream chose not to analyze this evidence (routing skipped it). Currently this is the dominant case in prod (zero coverage during May 15-16 — see [`../weapons/PERFORMANCE_ANALYSIS_2026_05.md`](../weapons/PERFORMANCE_ANALYSIS_2026_05.md)).
- `weapon_analyzed=true` + `has_weapon=false` → analyzed and came up clean. Useful for false-positive review queue.
- `weapon_analyzed=true` + `weapon_analysis_error="..."` → upstream attempted but failed mid-pipeline. Currently observed: 0 in prod (upstream is silent-skip rather than fail-and-report).

---

### 4.7 `image_embedding:raw` — per-image embed event ⭐ Tier 3 (highest cardinality)

**Why high-value:** per-image grain. `evidence_embeddings.weapon_detections` is JSONB per image — answers "WHICH images in a batch had weapons", not just "did the batch have weapons". Also enables per-image quality / similarity analysis.

**Trigger:** INSERT only.

**Note on UPDATE branch:** the original DW spec proposed a second `.upserted` event when `weapon_detections` is later populated. Our consumer writes `weapon_detections` at the initial INSERT (or stays NULL forever) — no async update path. Per DW's acceptance 2026-05-16, this UPDATE branch is **dropped from the contract**. Producer emits exactly one `.upserted` per row at INSERT.

**event_type:** `image_embedding.upserted`.

**Payload:**

```json
{
  "id": "uuid",
  "request_id": "uuid",
  "qdrant_point_id": "string|null",
  "image_index": 0,
  "image_url": "https://...",
  "weapon_detections": {},
  "json_data": {},
  "created_at": "ISO8601"
}
```

`created_at` load-bearing for `date_sk` derivation.

**Example (image with weapons detected):**

```json
{
  "id": "ab123456-7890-cdef-0123-456789abcdef",
  "request_id": "38e0022d-da52-4271-b44a-1201be347c00",
  "qdrant_point_id": "326b5f83-6d88-4c34-b82d-c46ccaa104b1",
  "image_index": 3,
  "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/.../frame_3.jpg",
  "weapon_detections": [
    {
      "class_name": "arma_fuego",
      "confidence": 0.7707,
      "bbox": [120, 340, 280, 520]
    }
  ],
  "json_data": {"model_version": "clip-vit-b-32"},
  "created_at": "2026-05-14T20:47:07.310+00:00"
}
```

**Example (clean image):**

```json
{
  "id": "bc234567-8901-defa-1234-56789abcdef0",
  "request_id": "38e0022d-da52-4271-b44a-1201be347c00",
  "qdrant_point_id": "f0e1d2c3-...",
  "image_index": 0,
  "image_url": "https://minio.lookia.mx/lucam-assets/embeddings/.../frame_0.jpg",
  "weapon_detections": null,
  "json_data": {"model_version": "clip-vit-b-32"},
  "created_at": "2026-05-14T20:47:07.310+00:00"
}
```

**MAXLEN:**
- **Steady-state: 500,000.** Observed volume is ~290k/month — 500k gives ~50 days.
- **Backfill push: bump to 2,000,000** before any historical-data write. Reset to 500k afterwards.

This is the **highest-cardinality stream** in the integration. The producer-side MAXLEN trim hazard (Lesson 3 in [`LOOKIA_DW_PUBLISHERS.md`](LOOKIA_DW_PUBLISHERS.md)) is most acute here.

**Edge cases:**
- `qdrant_point_id = null` is technically possible if the consumer fails after the DB row write but before the Qdrant upsert. In practice this combination should not appear (our consumer writes Qdrant first, then DB) — surface as an alert if you see it.
- `weapon_detections = null` means the parent request had `weapon_analyzed = false` OR the request was analyzed and this specific image had zero detections. Disambiguate via the parent `image_embedding_request:raw` event's `weapon_analyzed` field.

---

## 5. Delivery semantics

**Fire-and-forget.** Each publish is wrapped in a try/except that logs at ERROR level but does not raise:

```python
try:
    redis_client.xadd(stream, {"event_type": evt, "payload": json.dumps(row, default=str)},
                      maxlen=maxlen, approximate=True)
except Exception:
    logger.warning("Failed to publish to %s", stream, exc_info=True)
    # DB write committed — do NOT re-raise
```

This means:

- **Redis outage past your retry window = event lost.** No producer-side outbox in v1. The DB row persists (that's where state of record lives); the DW row is what's missing.
- **Recovery is direct-load.** When the DW side runs `dw_direct_load_image_embedding.py` against a freshly-granted read-only Postgres role on the Neon side, it reconstructs missing rows from `image-embedding-db` directly.
- **Crash mid-publish = at-most-once delivery.** If the service crashes between DB commit and XADD, the event is lost. The same direct-load recovery covers this.

If at-least-once delivery becomes a requirement (e.g. weapons alerts become safety-critical), the right move is a transactional outbox pattern on the producer side, not retries inside the existing publishers.

---

## 6. Dedup responsibility (DW side)

**Producer:** publishes every event. We do NOT dedup on `(entity_id)` or similar.

**Consumer:** dedups via composite UNIQUE keys + deterministic UUID5 row_ids:

| Stream | Recommended dedup key |
|---|---|
| `image_search_request:raw` | `(id, status, updated_at)` — same row at different lifecycle stages is intentionally distinct |
| `image_search_match:raw` | `(search_request_id, evidence_id)` per match — composite UNIQUE in `t_fact_image_search_matches` |
| `blacklist_image_entry:raw` | `(id, blacklist_version, updated_at)` — version bumps signal re-evaluation |
| `blacklist_image_reference:raw` | `(id, status, updated_at)` |
| `blacklist_image_embedding:raw` | `(id)` — immutable, single emission |
| `image_embedding_request:raw` | `(id, status, updated_at)` |
| `image_embedding:raw` | `(id)` — single INSERT, immutable on our side |

A republished event (e.g. backfill rerun) with the same dedup key should be idempotent on the DW side.

---

## 7. Event ordering

**Within a single source row:** events fire in monotonically increasing `updated_at` order. Sequence: `.created` → `.completed`|`.failed`. Subsequent UPDATEs (for streams that emit them) come strictly after `.created`.

**Across rows:** no ordering guarantees. Two evidences ingesting in parallel can interleave their event sequences arbitrarily on the stream.

**Across streams:** no cross-stream ordering. A `search_request.completed` event may appear before or after the corresponding `image_search.matched` event for the same `search_request_id`. DW worker must tolerate either ordering.

Use `(entity_id, source_table_updated_at)` as your stable identity, not stream position.

---

## 8. Versioning policy

This document and the producer-side implementation are versioned together.

| Change category | Coordination required? |
|---|---|
| **Additive optional fields** (new keys in payload, new optional event_type values) | No — DW consumer should ignore unknown fields |
| **Renamed fields, removed fields, changed types, new required fields** | Yes — PR + sign-off from lookia-dw before producer ship |
| **Envelope-level change** (e.g. `event_type` → something else) | Yes — full coordination |
| **New stream** | Yes — DW worker needs a routing branch |
| **Removed stream** | Yes — DW worker needs to remove the subscription |

When this doc and producer code disagree, **producer code is the truth** — DW should report the discrepancy and we fix the doc (or fix the producer).

---

## 9. Open items

| # | Item | Owner | Status |
|---|---|---|---|
| 1 | ~~DW confirms lifecycle simplification~~ | Lookia-DW | **✅ Resolved 2026-05-16** — accepted without edit |
| 2 | ~~DW confirms renegotiated MAXLEN~~ | Lookia-DW | **✅ Resolved 2026-05-16** — accepted (§4.6 = 500k; §4.7 = 500k / 2M backfill) |
| 3 | `dw_direct_load_image_embedding.py` for the 34k+265k historical seed | Lookia-DW | **In progress** — DW queued it next, ~30 min build |
| 4 | Temporary read-only Postgres role on Neon for backfill | This service | **Pending DW request** — likely 2026-05-17 |
| 5 | Producer implementation (~150 LoC, ~2 hours) | This service | **✅ Shipped 2026-05-16** — commit `9790bf6` |
| 6 | Negative test: assert `name` field never in `blacklist_image_entry:raw` payload | This service | **✅ Shipped** — `tests/test_dw_publisher.py::test_blacklist_image_entry_never_includes_raw_name` |
| 7 | `weapon_classes[]` canonical vocabulary — escalate to image-weapons-compute team | Image-weapons-compute | Deferred (not blocking; DW will track distinct values as they appear and we can promote to enum dim later) |

**DW-side parallel work in progress** (per their 2026-05-16 reply):
- Migration 011 applied on Neon prod (6 tables for our slice + 1 face symmetry + 2 widenings)
- Worker + bulk_upsert build runs alongside our producer ship
- DW expects to verify within minutes of `git push origin main` from this side

---

## 10. Cross-references

- Negotiation tracker: [`LOOKIA_DW_PUBLISHERS.md`](LOOKIA_DW_PUBLISHERS.md)
- Sister outbound contract (different consumer, same wire format): [`REPORT_GENERATION_STREAMS.md`](REPORT_GENERATION_STREAMS.md)
- Status-enum source of truth: [`../../src/db/models/constants.py`](../../src/db/models/constants.py)
- Existing producer patterns to follow:
  - [`../weapons/RUNTIME.md`](../weapons/RUNTIME.md) — `weapons:detected` publisher (the model)
  - [`../weapons/PERFORMANCE_ANALYSIS_2026_05.md`](../weapons/PERFORMANCE_ANALYSIS_2026_05.md) — proof publishers don't slow ingest
  - [`../../src/services/blacklist_match_service.py`](../../src/services/blacklist_match_service.py) — `image:blacklist_match` publisher (the most recent example)
- DW's inbound requirements doc: [`../../../lookia-dw/docs/requirements/image-embedding-service.md`](../../../lookia-dw/docs/requirements/image-embedding-service.md)
- Service-side stream contracts (internal): [`../new_arq_v2/04_STREAM_CONTRACTS.md`](../new_arq_v2/04_STREAM_CONTRACTS.md)
