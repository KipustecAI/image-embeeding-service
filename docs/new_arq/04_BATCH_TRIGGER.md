# Step 4: BatchTrigger — Signal-Driven Dispatcher

## Objective

Replace fixed-interval cron jobs with event-driven batch dispatch. Instead of "run every 10 minutes", the system flushes work when enough items accumulate OR a short timeout expires.

## How It Works

```
Stream event arrives
       │
       ▼
  DB row created (status=1)
       │
       ▼
  BatchTrigger.notify(count=1)
       │
       ├── pending_count >= batch_size (20)?
       │       YES → flush immediately ("batch_full")
       │       NO  ↓
       │
       └── First event? Start timeout timer
                │
                ▼ (after max_wait_seconds)
           flush ("timeout")
```

**Result:** Under load, batches flush every 20 items (sub-second). Under low traffic, items wait at most 5-10 seconds.

## Implementation

**New file:** `src/services/batch_trigger.py`

```python
import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


class BatchTrigger:
    def __init__(
        self,
        name: str = "default",
        batch_size: int = 20,
        max_wait_seconds: float = 10.0,
        process_callback: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.name = name
        self.batch_size = batch_size
        self.max_wait_seconds = max_wait_seconds
        self.process_callback = process_callback

        self._pending_count = 0
        self._first_event_time = None
        self._timeout_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._running = False

        # Metrics
        self._total_flushes = 0
        self._total_events = 0
        self._flush_reasons = {"batch_full": 0, "timeout": 0, "manual": 0}

    async def start(self):
        self._running = True
        logger.info(f"BatchTrigger '{self.name}' started (batch={self.batch_size}, wait={self.max_wait_seconds}s)")

    async def stop(self):
        self._running = False
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        # Flush remaining
        if self._pending_count > 0:
            await self._flush("shutdown")
        logger.info(f"BatchTrigger '{self.name}' stopped")

    async def notify(self, count: int = 1):
        """Called by stream consumer after creating DB rows."""
        async with self._lock:
            self._pending_count += count
            self._total_events += count

            # Start timeout on first event
            if self._first_event_time is None:
                self._first_event_time = asyncio.get_event_loop().time()
                self._timeout_task = asyncio.create_task(self._timeout_flush())

            # Flush if batch full
            if self._pending_count >= self.batch_size:
                await self._flush("batch_full")

    async def _timeout_flush(self):
        """Fire after max_wait_seconds since first event."""
        await asyncio.sleep(self.max_wait_seconds)
        async with self._lock:
            if self._pending_count > 0:
                await self._flush("timeout")

    async def _flush(self, reason: str):
        """Execute callback and reset state."""
        count = self._pending_count
        self._pending_count = 0
        self._first_event_time = None

        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None

        self._total_flushes += 1
        self._flush_reasons[reason] = self._flush_reasons.get(reason, 0) + 1

        logger.info(f"BatchTrigger '{self.name}' flush: reason={reason}, count={count}")

        if self.process_callback:
            try:
                await self.process_callback()
            except Exception as e:
                logger.error(f"BatchTrigger '{self.name}' callback failed: {e}")

    def health(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "pending_count": self._pending_count,
            "batch_size": self.batch_size,
            "max_wait_seconds": self.max_wait_seconds,
            "has_active_timer": self._timeout_task is not None and not self._timeout_task.done(),
            "total_flushes": self._total_flushes,
            "total_events": self._total_events,
            "flush_reasons": self._flush_reasons,
        }


# ── Registry (global access) ──

_triggers: dict[str, BatchTrigger] = {}

def create_batch_trigger(name: str, **kwargs) -> BatchTrigger:
    trigger = BatchTrigger(name=name, **kwargs)
    _triggers[name] = trigger
    return trigger

def get_batch_trigger(name: str) -> Optional[BatchTrigger]:
    return _triggers.get(name)
```

## Integration Points

### Wired in lifespan (Step 7):

```python
# FastAPI lifespan startup
embedding_trigger = create_batch_trigger(
    name="embedding",
    batch_size=settings.batch_trigger_size,            # 20
    max_wait_seconds=settings.batch_trigger_max_wait,  # 5.0s
    process_callback=dispatch_embedding_batch,          # enqueue ARQ job
)
await embedding_trigger.start()

search_trigger = create_batch_trigger(
    name="search",
    batch_size=settings.batch_trigger_search_size,     # 10
    max_wait_seconds=settings.batch_trigger_search_wait, # 3.0s
    process_callback=dispatch_search_batch,
)
await search_trigger.start()
```

### Called by stream consumers:

```python
# After creating DB rows in evidence_consumer.py
trigger = get_batch_trigger("embedding")
await trigger.notify(count=requests_created)
```

### Dispatch callbacks (atomic status update + ARQ enqueue):

```python
async def dispatch_embedding_batch():
    """Callback: pick pending rows, mark WORKING, enqueue to ARQ."""
    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        pending = await repo.get_pending_requests(limit=20)

        if not pending:
            return

        request_ids = [str(r.id) for r in pending]

        # Atomic: status update + enqueue
        for r in pending:
            r.status = EmbeddingRequestStatus.WORKING
            r.processing_started_at = datetime.utcnow()
        await session.flush()

        await arq_pool.enqueue_job("process_evidence_embeddings_batch", request_ids)
        await session.commit()

        logger.info(f"Dispatched {len(request_ids)} embedding requests to ARQ")
```

## Config Additions

```python
# Batch Trigger
batch_trigger_size: int = 20                 # BATCH_TRIGGER_SIZE
batch_trigger_max_wait: float = 5.0          # BATCH_TRIGGER_MAX_WAIT
batch_trigger_search_size: int = 10          # BATCH_TRIGGER_SEARCH_SIZE
batch_trigger_search_wait: float = 3.0       # BATCH_TRIGGER_SEARCH_WAIT
```

## Cross-Process Trigger (ARQ Worker → FastAPI)

The ARQ worker runs in a separate process and cannot access in-memory BatchTrigger. For cases where the worker needs to notify a trigger (e.g., after embedding completes, notify search recalculation), use an internal API endpoint:

```python
# In FastAPI router
@router.post("/api/v1/internal/trigger/{trigger_name}")
async def notify_trigger(trigger_name: str, count: int = 1):
    trigger = get_batch_trigger(trigger_name)
    if not trigger:
        raise HTTPException(404, f"Trigger '{trigger_name}' not found")
    await trigger.notify(count=count)
    return {"notified": True, "count": count}
```

This matches deepface's `/api/v1/internal/trigger/stage2` pattern.
