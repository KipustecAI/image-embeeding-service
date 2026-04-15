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

Published by the backend when a user submits a search via `POST /api/v1/search`.

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
    }
  }
}
```

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
    }
  }
}
```

The backend receives the pre-computed query vector and executes the Qdrant search itself.

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

## Consumer Groups

| Stream | Consumer Group | Consumed By |
|--------|---------------|-------------|
| `evidence:embed` | `compute-workers` | embedding-compute |
| `evidence:search` | `compute-workers` | embedding-compute |
| `embeddings:results` | `backend-workers` | embedding-backend |
| `search:results` | `backend-workers` | embedding-backend |

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
