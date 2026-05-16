# Step 4: Stream Payload Contracts

The contract between compute and backend. Both services must agree on these schemas.

## Stream: evidence:embed (ETL → Compute)

Published by the ETL service when an evidence ZIP has been uploaded to MinIO and is ready to be embedded.

```json
{
  "event_type": "evidence.created.embed",
  "payload": {
    "evidence_id": "b94b224e-dfbb-4839-8216-52aab8e5c94f",
    "camera_id": "f39d4374-8a68-4b34-b3ef-5a0514e81d92",
    "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
    "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
    "app_id": 1,
    "infraction_code": "46NHZKCD_1_687_20260327221317",
    "zip_url": "https://minio.lookia.mx/lucam-assets/46NHZKCD_1_687_20260327221317.zip"
  }
}
```

Notes:
- `zip_url` replaces the old `image_urls` array — the compute service downloads the ZIP itself
- `user_id`, `device_id`, `app_id` are required for multi-tenant isolation downstream
- `infraction_code` is passed through for tracing but is not used in embedding logic

## Stream: evidence:search (Backend → Compute)

Published by the backend when a user submits a search via `POST /api/v1/search`, **or** when a user attaches a reference image to a blacklist entry (`POST /api/v1/blacklist/image-entries/{id}/references`). Both paths share the same stream — the optional `purpose` field tells the backend's `search:results` consumer how to dispatch the returned vector.

```json
{
  "event_type": "search.created",
  "payload": {
    "search_id": "770e8400-e29b-41d4-a716-446655440000",
    "user_id": "880e8400-e29b-41d4-a716-446655440000",
    "image_url": "https://storage.example.com/query.jpg",
    "threshold": 0.75,
    "max_results": 50,
    "metadata": {
      "camera_id": "660e8400-e29b-41d4-a716-446655440000"
    },
    "purpose": "search",
    "blacklist_entry_id": null
  }
}
```

### Optional fields *(added 2026-05-05)*

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `purpose` | `"search"` \| `"blacklist_embed"` | no | `"search"` | Discriminator for the backend's `search:results` consumer. Compute treats both values identically — embed the image and publish the vector. The dispatch happens in the backend. Legacy callers that omit the field get `"search"` semantics. |
| `blacklist_entry_id` | string (UUID) \| null | no | null | Present only when `purpose="blacklist_embed"`. Opaque echo value — compute echoes it on `search:results` so the backend can attribute the returned vector to a blacklist entry. |

**Blacklist embed request.** When `purpose="blacklist_embed"`, the payload represents a reference image that should be embedded and returned via `search:results` for backend-side storage as a blacklist point. `search_id` in that case carries the `BlacklistImageReference.id` as an opaque echo value (the GPU does not interpret it). `threshold` and `max_results` are unused by the backend on this path but tolerated for shape uniformity.

See [../image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md) for the dispatch logic and [../requirements/IMAGE_COMPUTE_STREAMS.md](../requirements/IMAGE_COMPUTE_STREAMS.md) §3 for the negotiation trail.

## Stream: embeddings:results (Compute → Backend)

Published by the compute service after CLIP inference completes. The payload passes through all ETL metadata (`user_id`, `device_id`, `app_id`, `infraction_code`, `zip_url`) so the backend can upload images to the storage service and enforce multi-tenant payloads in Qdrant.

### Success

```json
{
  "event_type": "embeddings.computed",
  "payload": {
    "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
    "camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
    "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
    "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
    "app_id": 1,
    "infraction_code": "SMVV8UGE_116_875_20260410100151",
    "zip_url": "https://minio.lookia.mx/lucam-assets/SMVV8UGE_116_875_20260410100151.zip",
    "embeddings": [
      {
        "image_name": "20260410-100150_023389.jpg",
        "image_index": 0,
        "vector": [0.0056, 0.0053, -0.0186, ...]
      },
      {
        "image_name": "20260410-100150_023383.jpg",
        "image_index": 1,
        "vector": [-0.00003, 0.0044, 0.0066, ...]
      }
    ],
    "input_count": 10,
    "filtered_count": 9,
    "embedded_count": 9
  }
}
```

Notes:
- `image_name` is the filename inside the ZIP (no `image_url` — the backend uploads the image to the storage service and generates the permanent URL)
- `input_count`: original number of images inside the ZIP
- `filtered_count`: after diversity filter (duplicates removed)
- `embedded_count`: successfully embedded
- `vector`: 512 float array (CLIP ViT-B-32)
- `image_index`: position after filtering, not in the original ZIP listing
- All ETL metadata (`user_id`, `device_id`, `app_id`, `infraction_code`, `zip_url`) is required — the backend relies on it for storage upload path, Qdrant payload, and DB row creation

### Optional field: `category` *(added 2026-04-18)*

| Field | Type | Required | Notes |
|---|---|---|---|
| `category` | string \| null | **no** | Human-assigned evidence category — `"vehicle"`, `"scene"`, `"person"`, `"infraction_pattern"`, or any free-form label the producer chooses. No enum. Null/absent means "uncategorized". Backend stores on `embedding_requests.category` and injects into each Qdrant point payload for search-time filtering. Legacy producers that don't send this field continue to work unchanged. See [../image-blacklist/01_CATEGORY.md](../image-blacklist/01_CATEGORY.md). |

### Optional: `weapon_analysis` enrichment

If the message was routed through the `compute-weapons` service, the payload includes an optional `weapon_analysis` block alongside the fields above:

```json
{
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

The block is **optional and backwards compatible** — producers that omit it are handled on the legacy path. When present, the backend persists per-image detections to `evidence_embeddings.weapon_detections`, evidence-level summary to `embedding_requests`, and per-image flags (`weapon_analyzed`, `has_weapon`, `weapon_classes`) into the Qdrant point payload for search-time filtering. See [../weapons/](../weapons/) for the full enrichment contract and storage design.

### Error

```json
{
  "event_type": "compute.error",
  "payload": {
    "entity_id": "6ce54053-320d-4a39-8313-c2ca8a2dfee1",
    "entity_type": "evidence",
    "error": "File name in directory '...' and header '...' differ."
  }
}
```

Common `error` values observed in production:
- `"No images downloadable"` — ZIP download failed or was empty
- `"File name in directory ... and header ... differ."` — corrupt ZIP with mismatched central-directory / local-header filenames (ETL-side ZIP-building bug)
- `"No images passed diversity filter"` — all frames rejected as duplicates

## Stream: search:results (Compute → Backend)

Published by the compute service after embedding the query image.

### Success

```json
{
  "event_type": "search.vector.computed",
  "payload": {
    "search_id": "770e8400-e29b-41d4-a716-446655440000",
    "user_id": "880e8400-e29b-41d4-a716-446655440000",
    "vector": [0.0123, -0.0345, 0.0678, ...],
    "threshold": 0.75,
    "max_results": 50,
    "metadata": {
      "camera_id": "660e8400-e29b-41d4-a716-446655440000"
    },
    "purpose": "search",
    "blacklist_entry_id": null
  }
}
```

The backend receives the pre-computed query vector and executes the Qdrant search itself, OR — when `purpose="blacklist_embed"` — stores the vector as a blacklist point and schedules a reverse-search job. Compute echoes both `purpose` and `blacklist_entry_id` byte-identical from the input request; legacy callers that didn't send `purpose` get the default `"search"` value here.

### Error

```json
{
  "event_type": "compute.error",
  "payload": {
    "entity_id": "770e8400-e29b-41d4-a716-446655440000",
    "entity_type": "search",
    "error": "Failed to download query image"
  }
}
```

**Note:** `compute.error` envelopes deliberately do **not** carry `purpose` or `blacklist_entry_id` — the failure shape is identical regardless of intent. The backend dispatches errors by looking `entity_id` up in `blacklist_image_references` first and falling through to `search_requests`. See [../image-blacklist/04_EMBEDDING_FLOW.md](../image-blacklist/04_EMBEDDING_FLOW.md) §"Error routing".

## Stream: weapons:detected (Backend → report-generation) *(added 2026-04-15)*

Published by the backend when a `weapon_analysis` block arrives on `embeddings:results` with at least one frame carrying detections. Consumed by the report-generation service for sub-type 1D alerts.

Full DTO + trigger semantics: [`../requirements/REPORT_GENERATION_STREAMS.md`](../requirements/REPORT_GENERATION_STREAMS.md) §2. Diagnostic recipe + measured latency baseline: [`../weapons/PERFORMANCE_ANALYSIS_2026_05.md`](../weapons/PERFORMANCE_ANALYSIS_2026_05.md). Producer code: [`../../src/streams/embedding_results_consumer.py`](../../src/streams/embedding_results_consumer.py) (the publish block after the DB commit).

## Stream: image:blacklist_match (Backend → report-generation) *(added 2026-05-05)*

Published by the backend whenever an evidence frame matches a blacklist reference image — either inline at ingest time or via a reverse search after a new blacklist entry is registered. Consumed by the report-generation service to produce a sub-type 1E alert report.

```json
{
  "event_type": "image.blacklist_match",
  "payload": {
    "user_id": "...",
    "blacklist_entry_id": "...",
    "blacklist_entry_name": "...",
    "blacklist_entry_version": 1,
    "blacklist_reference_id": "...",
    "blacklist_reference_url": "...",
    "evidence_id": "...",
    "matched_image_url": "...",
    "matched_qdrant_point_id": "...",
    "similarity_score": 0.893,
    "threshold_used": 0.85,
    "trigger": "inline",
    "matched_at": "2026-05-05T14:22:03.501Z"
  }
}
```

The full DTO + field-by-field semantics live in [../requirements/REPORT_GENERATION_STREAMS.md §3](../requirements/REPORT_GENERATION_STREAMS.md) (the contract authority). Receiver dedups on `(evidence_id, blacklist_entry_id, blacklist_entry_version)` so a version bump (entry edit) produces fresh reports while a no-op republish does not.

## Lookia-DW outbound streams *(added 2026-05-16)*

Seven outbound streams feed the lookia-dw data warehouse. **Wire-format authority: [`../requirements/LOOKIA_DW_STREAMS.md`](../requirements/LOOKIA_DW_STREAMS.md) §4.x.** That doc carries the per-stream payload schemas, examples, MAXLEN, edge cases. Below is a one-line directory; don't duplicate the contract here.

| Stream | Source table | Trigger | event_type values |
|---|---|---|---|
| `image_search_request:raw` | `search_requests` | lifecycle | `image_search.created` / `.completed` / `.failed` |
| `image_search_match:raw` | `search_matches` | terminal `.completed` w/ `total_matches > 0` | `image_search.matched` |
| `blacklist_image_entry:raw` | `blacklist_image_entries` | INSERT / UPDATE / status flip | `blacklist_image_entry.upserted` |
| `blacklist_image_reference:raw` | `blacklist_image_references` | INSERT / status update | `blacklist_image_reference.upserted` |
| `blacklist_image_embedding:raw` | `blacklist_image_embeddings` | INSERT only (immutable) | `blacklist_image_embedding.created` |
| `image_embedding_request:raw` | `embedding_requests` | lifecycle | `image_embedding_request.created` / `.completed` / `.failed` |
| `image_embedding:raw` | `evidence_embeddings` | INSERT (per image) | `image_embedding.upserted` |

All 7 use the standard `{event_type, payload}` flat-hash envelope. PII protection: `blacklist_image_entry.name` is hashed to `name_hash` before publish — raw name never on the wire (locked by [`../../tests/test_dw_publisher.py::test_blacklist_image_entry_never_includes_raw_name`](../../tests/test_dw_publisher.py)). MAXLEN per stream is env-driven via `DW_MAXLEN_*` settings; embed streams default 500k, bump `DW_MAXLEN_IMAGE_EMBEDDING=2_000_000` before any backfill push.

Producer code: [`../../src/services/dw_publisher_service.py`](../../src/services/dw_publisher_service.py). Hash helper: [`../../src/application/helpers/dw_hashing.py`](../../src/application/helpers/dw_hashing.py).

## Consumer Groups

| Stream | Consumer Group | Consumed By |
|--------|---------------|-------------|
| `evidence:embed` | `compute-workers` | embedding-compute |
| `evidence:search` | `compute-workers` | embedding-compute |
| `embeddings:results` | `backend-workers` | embedding-backend |
| `search:results` | `backend-workers` | embedding-backend |
| `weapons:detected` | (configurable on receiver side) | report-generation |
| `image:blacklist_match` | (configurable on receiver side) | report-generation |
| `image_search_request:raw` | (configurable on receiver side) | lookia-dw |
| `image_search_match:raw` | (configurable on receiver side) | lookia-dw |
| `blacklist_image_entry:raw` | (configurable on receiver side) | lookia-dw |
| `blacklist_image_reference:raw` | (configurable on receiver side) | lookia-dw |
| `blacklist_image_embedding:raw` | (configurable on receiver side) | lookia-dw |
| `image_embedding_request:raw` | (configurable on receiver side) | lookia-dw |
| `image_embedding:raw` | (configurable on receiver side) | lookia-dw |

## Dead Letter Streams

| Primary Stream | Dead Letter |
|----------------|-------------|
| `evidence:embed` | `evidence:embed:dead` |
| `evidence:search` | `evidence:search:dead` |
| `embeddings:results` | `embeddings:results:dead` |
| `search:results` | `search:results:dead` |

## Vector Encoding

The 512-float vector is JSON-encoded in the stream payload. This adds ~4KB per embedding to the message size. Alternatives considered:

| Format | Size per vector | Complexity |
|--------|----------------|------------|
| JSON array of floats | ~4KB | Lowest (current choice) |
| Base64-encoded binary | ~700B | Medium |
| MessagePack | ~2KB | Medium |

JSON is fine for our throughput. If we ever need to optimize, we can switch to base64 without changing the stream topology.
