"""STREAM_CONSUMER_CONCURRENCY is honoured by StreamConsumer.

Until 2026-10-15 the value was stored and never used, so `embeddings:results` was
handled one message at a time (~0.7 s each) at about the arrival rate, and a ~45 min
backlog never drained (lookia-online-dw #chains-streams-vllm #1180).
"""

import threading
import time

from src.streams.consumer import StreamConsumer


class _FakeBus:
    """Just enough of redis.Redis for the consume loop: XREADGROUP from a list, XACK."""

    def __init__(self, n: int):
        self._pending = [
            (f"{i}-0", {"event_type": "e", "payload": f'{{"i": {i}}}'}) for i in range(n)
        ]
        self._lock = threading.Lock()
        self.acked: list[str] = []
        self.max_count_asked = 0

    def xreadgroup(self, groupname, consumername, streams, count, block):
        with self._lock:
            self.max_count_asked = max(self.max_count_asked, count)
            batch, self._pending = self._pending[:count], self._pending[count:]
        if not batch:
            time.sleep(min(block, 20) / 1000)
            return []
        return [("s", batch)]

    def xack(self, stream, group, message_id):
        with self._lock:
            self.acked.append(message_id)

    def xpending_range(self, *args, **kwargs):
        return []


def _consumer(bus: _FakeBus, concurrency: int, handler) -> StreamConsumer:
    c = StreamConsumer(
        stream="embeddings:results",
        group="g",
        redis_url="redis://bus/0",
        block_ms=20,
        batch_size=10,
        concurrency=concurrency,
    )
    c._redis = bus
    c._ensure_group = lambda: None
    c.register_handler("e", handler)
    return c


def _run_until(consumer: StreamConsumer, done, timeout: float = 10.0) -> float:
    t0 = time.monotonic()
    consumer.start()
    while not done() and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    elapsed = time.monotonic() - t0
    consumer.stop()
    return elapsed


class _SlowHandler:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.seen: list[int] = []

    def __call__(self, event_type, payload, message_id):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
            self.seen.append(payload["i"])


def test_concurrency_8_handles_8_messages_at_once():
    bus, handler = _FakeBus(40), _SlowHandler(0.1)
    consumer = _consumer(bus, 8, handler)

    elapsed = _run_until(consumer, lambda: len(bus.acked) == 40)

    assert sorted(handler.seen) == list(range(40))
    assert len(bus.acked) == 40
    assert handler.peak == 8
    assert elapsed < 2.0  # serial would take 40 x 0.1 s = 4 s
    assert consumer.health()["messages_processed"] == 40


def test_control_concurrency_1_stays_serial():
    bus, handler = _FakeBus(10), _SlowHandler(0.05)
    consumer = _consumer(bus, 1, handler)

    _run_until(consumer, lambda: len(bus.acked) == 10)

    assert handler.peak == 1
    assert handler.seen == list(range(10))  # serial keeps stream order
    assert len(bus.acked) == 10


def test_never_reads_more_than_the_free_slots():
    bus, handler = _FakeBus(30), _SlowHandler(0.05)
    consumer = _consumer(bus, 4, handler)

    _run_until(consumer, lambda: len(bus.acked) == 30)

    assert bus.max_count_asked <= 4  # batch_size is 10, but only 4 slots exist
    assert handler.peak <= 4


def test_a_failing_message_is_not_acked_and_the_others_are():
    bus = _FakeBus(12)

    def handler(event_type, payload, message_id):
        time.sleep(0.02)
        if payload["i"] == 5:
            raise RuntimeError("upload failed")

    consumer = _consumer(bus, 4, handler)

    _run_until(consumer, lambda: len(bus.acked) == 11)
    time.sleep(0.1)

    assert "5-0" not in bus.acked  # stays pending for reclaim / dead letter
    assert len(bus.acked) == 11
    health = consumer.health()
    assert health["messages_failed"] == 1
    assert health["messages_processed"] == 11
    assert health["concurrency"] == 4


def test_stop_waits_for_handlers_in_flight_and_acks_them():
    bus, handler = _FakeBus(4), _SlowHandler(0.3)
    consumer = _consumer(bus, 4, handler)
    consumer.start()
    deadline = time.monotonic() + 2
    while handler.running < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    all_in_flight = handler.running == 4

    consumer.stop()

    assert all_in_flight
    assert len(bus.acked) == 4
