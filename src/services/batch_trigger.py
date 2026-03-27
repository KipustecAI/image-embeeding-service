"""Signal-driven batch dispatcher.

Accumulates events and flushes when:
  1. Count reaches batch_size (immediate), or
  2. Timeout fires after max_wait_seconds since first event.

Adopted from deepface-restapi BatchTrigger pattern.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class BatchTrigger:
    def __init__(
        self,
        name: str = "default",
        batch_size: int = 20,
        max_wait_seconds: float = 10.0,
        process_callback: Callable[[], Awaitable[None]] | None = None,
    ):
        self.name = name
        self.batch_size = batch_size
        self.max_wait_seconds = max_wait_seconds
        self.process_callback = process_callback

        self._pending_count = 0
        self._first_event_time: float | None = None
        self._timeout_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._running = False

        # Metrics
        self._total_flushes = 0
        self._total_events = 0
        self._flush_reasons: dict[str, int] = {
            "batch_full": 0,
            "timeout": 0,
            "manual": 0,
            "shutdown": 0,
        }

    async def start(self):
        self._running = True
        logger.info(
            f"BatchTrigger '{self.name}' started "
            f"(batch={self.batch_size}, wait={self.max_wait_seconds}s)"
        )

    async def stop(self):
        self._running = False
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        if self._pending_count > 0:
            await self._flush("shutdown")
        logger.info(f"BatchTrigger '{self.name}' stopped")

    async def notify(self, count: int = 1):
        """Called by stream consumer after creating DB rows."""
        async with self._lock:
            self._pending_count += count
            self._total_events += count

            # Start timeout on first event in batch
            if self._first_event_time is None:
                self._first_event_time = asyncio.get_event_loop().time()
                self._timeout_task = asyncio.create_task(self._timeout_flush())

            # Flush immediately if batch full
            if self._pending_count >= self.batch_size:
                await self._flush("batch_full")

    async def _timeout_flush(self):
        """Fire after max_wait_seconds since first event."""
        try:
            await asyncio.sleep(self.max_wait_seconds)
            async with self._lock:
                if self._pending_count > 0:
                    await self._flush("timeout")
        except asyncio.CancelledError:
            pass

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
            "has_active_timer": (self._timeout_task is not None and not self._timeout_task.done()),
            "total_flushes": self._total_flushes,
            "total_events": self._total_events,
            "flush_reasons": self._flush_reasons,
        }


# ── Global Registry ──

_triggers: dict[str, BatchTrigger] = {}


def create_batch_trigger(name: str, **kwargs) -> BatchTrigger:
    trigger = BatchTrigger(name=name, **kwargs)
    _triggers[name] = trigger
    return trigger


def get_batch_trigger(name: str) -> BatchTrigger | None:
    return _triggers.get(name)
