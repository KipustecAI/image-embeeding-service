# Step 4: Stream Payload Contracts

The contract between compute and backend. Both services must agree on these schemas.

## Stream: evidence:embed (Video Server → Compute)

Published by the Video Server when evidence reaches status=3 (FOUND).

```json
{
  "event_type": "evidence.ready.embed",
  "payload": {
    "evidence_id": "550e8400-e29b-41d4-a716-446655440000",
    "camera_id": "660e8400-e29b-41d4-a716-446655440000",
    "image_urls": [
      "https://storage.example.com/crop1.jpg",
      "https://storage.example.com/crop2.jpg",
      "https://storage.example.com/crop3.jpg"
    ]
  }
}
```

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

Published by the compute service after CLIP inference completes.

### Success

```json
{
  "event_type": "embeddings.computed",
  "payload": {
    "evidence_id": "550e8400-e29b-41d4-a716-446655440000",
    "camera_id": "660e8400-e29b-41d4-a716-446655440000",
    "embeddings": [
      {
        "image_url": "https://storage.example.com/crop1.jpg",
        "image_index": 0,
        "vector": [0.0123, -0.0345, 0.0678, ...],
        "total_images": 2
      },
      {
        "image_url": "https://storage.example.com/crop3.jpg",
        "image_index": 1,
        "vector": [-0.0234, 0.0456, -0.0789, ...],
        "total_images": 2
      }
    ],
    "input_count": 3,
    "filtered_count": 2,
    "embedded_count": 2
  }
}
```

Notes:
- `input_count`: original number of image URLs received
- `filtered_count`: after diversity filter (some duplicates removed)
- `embedded_count`: successfully embedded (some may fail download)
- `vector`: 512 float array (CLIP ViT-B-32)
- `image_index`: position after filtering, not in the original list

### Error

```json
{
  "event_type": "compute.error",
  "payload": {
    "entity_id": "550e8400-e29b-41d4-a716-446655440000",
    "entity_type": "evidence",
    "error": "No images downloadable",
    "input_count": 3,
    "filtered_count": 0
  }
}
```

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
