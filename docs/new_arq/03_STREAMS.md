# Step 3: Redis Streams Consumer

## Objective

Replace HTTP polling with push-based Redis Streams. Events arrive instantly instead of waiting for the next poll cycle (10min/1min).

## Architecture

```
Producer (Video Server / ETL)
       │
       │ XADD evidence:embed { evidence_id, camera_id, image_urls, ... }
       │ XADD evidence:search { search_id, user_id, image_url, ... }
       ▼
┌─────────────────────────────────────────────────────┐
│  StreamConsumer (daemon thread in FastAPI process)    │
│                                                      │
│  Thread: XREADGROUP BLOCK 5000ms                     │
│    ↓ message received                                │
│  asyncio.run_coroutine_threadsafe(handler, loop)     │
│    ↓ async handler                                   │
│  Dedup check → Create DB row (status=1) → XACK      │
│    ↓                                                 │
│  BatchTrigger.notify(count=N)                        │
└─────────────────────────────────────────────────────┘
```

## Generic StreamConsumer

**New file:** `src/streams/consumer.py`

Adopt deepface's `StreamConsumer` class directly (it's generic). Key behaviors:

```python
class StreamConsumer:
    def __init__(
        self,
        stream: str,                        # "evidence:embed"
        group: str,                         # "embed-workers"
        consumer_name: str = None,          # Auto-generated hostname
        redis_url: str = None,
        block_ms: int = 5000,               # 5s blocking read
        batch_size: int = 10,               # Messages per XREADGROUP call
        reclaim_idle_ms: int = 3_600_000,   # 1 hour
        dead_letter_max_retries: int = 3,
        concurrency: int = 1,
    ):
```

**Daemon thread consumption loop:**
```python
def _consume_loop(self):
    while self._running:
        messages = self._redis.xreadgroup(
            groupname=self.group,
            consumername=self.consumer_name,
            streams={self.stream: ">"},
            count=self.batch_size,
            block=self.block_ms,
        )
        for stream_name, stream_messages in messages:
            for message_id, fields in stream_messages:
                self._process_message(message_id, fields)
        self._maybe_reclaim()
```

**Handler dispatch:**
```python
def register_handler(self, event_type: str, handler: Callable):
    self._handlers[event_type] = handler

def _process_message(self, message_id, fields):
    event_type = fields.get("event_type", "unknown")
    handler = self._handlers.get(event_type)
    if handler:
        handler(event_type, json.loads(fields["payload"]), message_id)
        self._ack(message_id)
    else:
        logger.warning(f"No handler for event_type={event_type}")
```

**Reclaim + Dead Letter:**
- Every 5 minutes, check PEL for messages idle > 1 hour
- If delivery count < 3: `XCLAIM` and reprocess
- If delivery count >= 3: move to `{stream}:dead` and `XACK`

## Evidence Embed Handler

**New file:** `src/streams/evidence_consumer.py`

```python
_event_loop: asyncio.AbstractEventLoop = None

def set_event_loop(loop):
    global _event_loop
    _event_loop = loop

def create_evidence_embed_consumer() -> StreamConsumer:
    consumer = StreamConsumer(
        stream=settings.stream_evidence_embed,    # "evidence:embed"
        group=settings.stream_consumer_group,      # "embed-workers"
        block_ms=settings.stream_consumer_block_ms,
        batch_size=settings.stream_consumer_batch_size,
        reclaim_idle_ms=settings.stream_reclaim_idle_ms,
        dead_letter_max_retries=settings.stream_dead_letter_max_retries,
        concurrency=settings.stream_consumer_concurrency,
    )
    consumer.register_handler("evidence.ready.embed", _handle_evidence_ready)
    return consumer

def _handle_evidence_ready(event_type: str, payload: dict, message_id: str):
    """Called from daemon thread — bridge to asyncio."""
    future = asyncio.run_coroutine_threadsafe(
        _process_evidence_embed(payload, message_id),
        _event_loop,
    )
    future.result(timeout=300)  # Block thread until async completes
```

**Async processing:**
```python
async def _process_evidence_embed(payload: dict, message_id: str):
    evidence_id = payload["evidence_id"]
    camera_id = payload["camera_id"]
    image_urls = payload.get("image_urls", [])

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)

        # Dedup: skip if already exists
        if await repo.check_duplicate(evidence_id):
            logger.info(f"Skipping duplicate evidence {evidence_id}")
            return

        # Create DB row at status=1
        request = await repo.create_request(
            evidence_id=evidence_id,
            camera_id=camera_id,
            image_urls=image_urls,
            stream_msg_id=message_id,
        )
        await session.commit()

    # Notify BatchTrigger
    trigger = get_batch_trigger("embedding")
    if trigger:
        await trigger.notify(count=1)

    logger.info(f"Created embedding request {request.id} for evidence {evidence_id}")
```

## Evidence Search Handler

**New file:** `src/streams/search_consumer.py`

Same pattern but for search events:

```python
def create_evidence_search_consumer() -> StreamConsumer:
    consumer = StreamConsumer(
        stream=settings.stream_evidence_search,   # "evidence:search"
        group=settings.stream_search_group,        # "search-workers"
        # ... same config pattern
    )
    consumer.register_handler("search.created", _handle_search_created)
    return consumer
```

## Stream Payload Contracts

### evidence:embed

Published by Video Server / ETL when evidence reaches status=3 (FOUND):

```json
{
    "event_type": "evidence.ready.embed",
    "payload": {
        "evidence_id": "550e8400-...",
        "camera_id": "660e8400-...",
        "image_urls": [
            "https://storage.example.com/crop1.jpg",
            "https://storage.example.com/crop2.jpg"
        ],
        "summary": "Evidence description text"
    }
}
```

### evidence:search

Published by Video Server when a new image search is created:

```json
{
    "event_type": "search.created",
    "payload": {
        "search_id": "770e8400-...",
        "user_id": "880e8400-...",
        "image_url": "https://storage.example.com/query.jpg",
        "threshold": 0.75,
        "max_results": 50,
        "metadata": {
            "camera_id": "660e8400-...",
            "date_from": "2025-08-01T00:00:00Z"
        }
    }
}
```

## Config Additions

```python
# Redis Streams (separate DB from ARQ)
redis_streams_db: int = 3                              # REDIS_STREAMS_DB
stream_evidence_embed: str = "evidence:embed"          # STREAM_EVIDENCE_EMBED
stream_evidence_search: str = "evidence:search"        # STREAM_EVIDENCE_SEARCH
stream_consumer_group: str = "embed-workers"           # STREAM_CONSUMER_GROUP
stream_search_group: str = "search-workers"            # STREAM_SEARCH_GROUP
stream_consumer_block_ms: int = 5000                   # STREAM_CONSUMER_BLOCK_MS
stream_consumer_batch_size: int = 10                   # STREAM_CONSUMER_BATCH_SIZE
stream_reclaim_idle_ms: int = 3_600_000                # STREAM_RECLAIM_IDLE_MS
stream_dead_letter_max_retries: int = 3                # STREAM_DEAD_LETTER_MAX_RETRIES
stream_consumer_concurrency: int = 1                   # STREAM_CONSUMER_CONCURRENCY
```

## File Structure

```
src/
├── streams/
│   ├── __init__.py
│   ├── consumer.py              # Generic StreamConsumer (adopted from deepface)
│   ├── evidence_consumer.py     # evidence:embed handler
│   └── search_consumer.py       # evidence:search handler
```
