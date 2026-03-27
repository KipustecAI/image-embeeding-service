# Step 2: GPU Compute Service

## Responsibility

**One job:** Receive image URLs → download → diversity filter → CLIP inference → publish vectors.

No database. No Qdrant. No pipeline state. Completely stateless.

## Processing Flows

### Evidence Embedding

```
evidence:embed stream
       │
       │ {evidence_id, camera_id, image_urls: [...]}
       ▼
  StreamConsumer (daemon thread)
       │
       ├─ Download all images (async, overlap with next batch)
       ├─ Diversity filter (Bhattacharyya, skip duplicates)
       ├─ CLIP inference (batch, GPU)
       │
       ▼
  Publish to embeddings:results stream
       │
       │ {evidence_id, camera_id, embeddings: [{image_url, vector, image_index}, ...]}
       ▼
  XACK original message
```

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
    """Downloads images and runs CLIP inference with I/O overlap."""

    async def process_evidence(self, payload: dict, message_id: str):
        evidence_id = payload["evidence_id"]
        camera_id = payload["camera_id"]
        raw_urls = payload.get("image_urls", [])

        # Step 1: Diversity filter (downloads + histogram comparison)
        filtered_urls = await self.diversity_filter.filter_image_urls(raw_urls)
        if not filtered_urls:
            logger.warning(f"No images passed diversity filter for {evidence_id}")
            return

        # Step 2: Download filtered images (async, all at once)
        images = await self.downloader.download_batch(filtered_urls)
        valid_images = [(url, img) for url, img in zip(filtered_urls, images) if img is not None]

        if not valid_images:
            logger.error(f"No images downloadable for {evidence_id}")
            # Publish error result so backend knows
            await self.producer.publish_error("embeddings:results", evidence_id, "No images downloadable")
            return

        # Step 3: CLIP batch inference
        pil_images = [img for _, img in valid_images]
        vectors = self.embedder.encode_batch(pil_images)

        # Step 4: Build result payload
        embeddings = []
        for idx, ((url, _), vector) in enumerate(zip(valid_images, vectors)):
            embeddings.append({
                "image_url": url,
                "image_index": idx,
                "vector": vector.tolist(),      # 512 floats
                "total_images": len(valid_images),
            })

        # Step 5: Publish to output stream
        await self.producer.publish("embeddings:results", {
            "event_type": "embeddings.computed",
            "evidence_id": evidence_id,
            "camera_id": camera_id,
            "embeddings": embeddings,
            "input_count": len(raw_urls),
            "filtered_count": len(filtered_urls),
            "embedded_count": len(embeddings),
        })

        logger.info(
            f"Evidence {evidence_id}: {len(raw_urls)} → {len(filtered_urls)} filtered → "
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
