# Step 2: GPU Compute Service

## Responsibility

**One job:** Receive a ZIP URL → download → extract frames → diversity filter → CLIP inference → publish vectors.

No database. No Qdrant. No pipeline state. Completely stateless.

## Processing Flows

### Evidence Embedding

```
evidence:embed stream
       │
       │ {evidence_id, camera_id, user_id, device_id, app_id,
       │  infraction_code, zip_url}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Download ZIP from zip_url
       ├─ Extract image frames (jpg, png) to memory/tempdir
       ├─ Diversity filter (Bhattacharyya, skip duplicates)
       ├─ CLIP inference (batch, GPU)
       │
       ▼
  Publish to embeddings:results stream
       │
       │ {evidence_id, camera_id, user_id, device_id, app_id,
       │  infraction_code, zip_url,
       │  embeddings: [{image_name, vector, image_index}, ...]}
       ▼
  XACK original message
```

Note: the compute service publishes `image_name` (the filename inside the ZIP), **not** a URL. The backend is responsible for downloading the ZIP again, extracting the filtered frames by name, uploading them to the storage service, and producing permanent MinIO URLs.

### Search Query

```
evidence:search stream
       │
       │ {search_id, user_id, image_url, threshold, max_results, metadata}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Download query image
       ├─ CLIP inference (single image)
       │
       ▼
  Publish to search:results stream
       │
       │ {search_id, user_id, vector, threshold, max_results, metadata}
       ▼
  XACK original message
```

## Key Implementation: Async I/O + GPU Overlap

The compute service should overlap image downloads with GPU inference (Option C from the discussion). This maximizes GPU utilization:

```python
class EvidenceComputeHandler:
    """Downloads ZIP, extracts frames, runs CLIP inference with I/O overlap."""

    async def process_evidence(self, payload: dict, message_id: str):
        evidence_id = payload["evidence_id"]
        camera_id = payload["camera_id"]
        zip_url = payload.get("zip_url")

        if not zip_url:
            logger.warning(f"Skipping evidence {evidence_id}: no zip_url")
            return

        # Step 1: Download ZIP + extract frames (filename, PIL.Image)
        frames = await self.zip_handler.download_and_extract(zip_url)
        if not frames:
            await self.producer.publish_error(
                "embeddings:results", evidence_id, "No images downloadable"
            )
            return

        # Step 2: Diversity filter (histogram comparison on extracted frames)
        filtered = self.diversity_filter.filter_images(frames)
        if not filtered:
            await self.producer.publish_error(
                "embeddings:results", evidence_id, "No images passed diversity filter"
            )
            return

        # Step 3: CLIP batch inference
        pil_images = [img for _, img in filtered]
        vectors = self.embedder.encode_batch(pil_images)

        # Step 4: Build result payload — image_name, not image_url
        embeddings = [
            {
                "image_name": name,
                "image_index": idx,
                "vector": vector.tolist(),  # 512 floats
            }
            for idx, ((name, _), vector) in enumerate(zip(filtered, vectors))
        ]

        # Step 5: Publish to output stream — pass through all ETL metadata
        await self.producer.publish("embeddings:results", {
            "event_type": "embeddings.computed",
            "evidence_id": evidence_id,
            "camera_id": camera_id,
            "user_id": payload.get("user_id"),
            "device_id": payload.get("device_id"),
            "app_id": payload.get("app_id"),
            "infraction_code": payload.get("infraction_code"),
            "zip_url": zip_url,
            "embeddings": embeddings,
            "input_count": len(frames),
            "filtered_count": len(filtered),
            "embedded_count": len(embeddings),
        })

        logger.info(
            f"Evidence {evidence_id}: {len(frames)} → {len(filtered)} filtered → "
            f"{len(embeddings)} embedded"
        )
```

## Stream Producer

New component — publishes results to output streams:

```python
class StreamProducer:
    """Publishes computed results to Redis Streams."""

    def __init__(self, redis_client):
        self.redis = redis_client

    async def publish(self, stream: str, payload: dict):
        """Publish a result message to the output stream."""
        self.redis.xadd(stream, {
            "event_type": payload.get("event_type", "unknown"),
            "payload": json.dumps(payload),
        })

    async def publish_error(self, stream: str, entity_id: str, error: str):
        """Publish an error result so the backend can mark the request as failed."""
        self.redis.xadd(stream, {
            "event_type": "compute.error",
            "payload": json.dumps({
                "entity_id": entity_id,
                "error": error,
            }),
        })
```

## Entry Point (main.py)

Minimal — just starts stream consumers and an optional health endpoint:

```python
import asyncio
from src.config import get_settings
from src.streams.consumer import StreamConsumer
from src.streams.evidence_handler import create_evidence_compute_consumer
from src.streams.search_handler import create_search_compute_consumer
from src.services.clip_embedder import CLIPEmbedder

async def main():
    settings = get_settings()

    # Pre-load CLIP model
    embedder = CLIPEmbedder(settings)
    await embedder.initialize()
    logger.info(f"CLIP model loaded: {settings.clip_model_name} on {settings.clip_device}")

    # Start consumers
    loop = asyncio.get_running_loop()

    evidence_consumer = create_evidence_compute_consumer(embedder, loop)
    evidence_consumer.start()

    search_consumer = create_search_compute_consumer(embedder, loop)
    search_consumer.start()

    logger.info("Compute service ready — consuming from streams")

    # Keep alive
    try:
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        evidence_consumer.stop()
        search_consumer.stop()
        await embedder.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
```

## Docker (GPU)

```dockerfile
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y python3.11 python3-pip libgl1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY .env .

CMD ["python3", "-m", "src.main"]
```

## Scaling

Multiple compute instances can run in parallel:
- Each joins the same consumer group (`compute-workers`)
- Redis Streams distributes messages across consumers automatically
- `FOR UPDATE SKIP LOCKED` not needed here — no DB, streams handle it natively via consumer groups

```bash
# Run 3 compute workers on a multi-GPU machine
CLIP_DEVICE=cuda:0 python -m src.main &
CLIP_DEVICE=cuda:1 python -m src.main &
CLIP_DEVICE=cuda:2 python -m src.main &
```
