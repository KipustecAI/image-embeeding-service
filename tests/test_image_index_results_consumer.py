"""Unit tests for the image-index results consumer (Phase 3).

No live infra — Redis/DB/Qdrant are mocked. Covers the load-bearing results-leg
invariants (00_DESIGN §5.2):

  * COUNT=1 — the factory forces batch_size=1 (281 KB messages).
  * computed happy path → publish image_batch.completed (explicit stream + maxlen).
  * unaccounted (land returns None) → NO terminal publish (stays computing).
  * land raise (unknown encoding / Qdrant failure) → propagates (message NOT ACKed).
  * compute.error → publish image_batch.failed.
  * never-minted compute.error (mark returns None) → ACK no-op, no publish.
  * land_computed is called with the DEDICATED vector_repo.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.streams.image_index_results_consumer as rc
from src.services.image_index_service import ImageIndexService


@pytest.fixture
def producer(monkeypatch):
    prod = MagicMock()
    monkeypatch.setattr(rc, "_producer", prod)
    return prod


@pytest.fixture
def vector_repo(monkeypatch):
    vr = MagicMock(name="dedicated-image-index-vector-repo")
    monkeypatch.setattr(rc, "_vector_repo", vr)
    return vr


@asynccontextmanager
async def _fake_session_cm():
    session = MagicMock()
    session.commit = AsyncMock()
    yield session


# ── Factory ──────────────────────────────────────────────────────────────────


def test_factory_forces_count_1_and_registers_both_handlers():
    consumer = rc.create_image_index_results_consumer()
    # COUNT=1 — 281 KB messages must never be buffered more than one at a time.
    assert consumer.batch_size == 1
    assert consumer.stream == rc.settings.stream_image_index_results
    assert consumer.group == rc.settings.image_index_results_group
    # Both event types wired on the one stream.
    assert set(consumer._handlers.keys()) == {"image.index.computed", "compute.error"}


def test_handler_timeouts():
    # computed does the heavy land + Qdrant upsert (300s); error is cheap (60s).
    assert rc._COMPUTED_HANDLER_TIMEOUT_S == 300
    assert rc._ERROR_HANDLER_TIMEOUT_S == 60


# ── computed leg ─────────────────────────────────────────────────────────────


async def test_computed_happy_path_publishes_completed(producer, vector_repo):
    lifecycle = {"batch_id": "b1", "status": "completed"}
    payload = {"batch_id": "b1", "results": []}
    with patch.object(rc, "get_session", _fake_session_cm), patch.object(
        ImageIndexService, "land_computed", AsyncMock(return_value=lifecycle)
    ) as land:
        await rc._process_computed(payload, "msg-1")

    # land called with the DEDICATED vector_repo.
    land.assert_awaited_once()
    assert land.call_args.kwargs["vector_repo"] is vector_repo
    # Exactly one image_batch.completed to the lifecycle stream, MAXLEN-trimmed.
    producer.publish.assert_called_once()
    kwargs = producer.publish.call_args.kwargs
    assert kwargs["stream"] == rc.settings.stream_image_batch_raw
    assert kwargs["event_type"] == "image_batch.completed"
    assert kwargs["payload"] is lifecycle
    assert kwargs["maxlen"] == rc.settings.image_index_batch_maxlen


async def test_computed_unaccounted_does_not_publish(producer, vector_repo):
    """land returns None (never-minted or not-yet-accounted) → ACK no-op."""
    with patch.object(rc, "get_session", _fake_session_cm), patch.object(
        ImageIndexService, "land_computed", AsyncMock(return_value=None)
    ):
        await rc._process_computed({"batch_id": "b1", "results": []}, "msg-2")
    producer.publish.assert_not_called()


async def test_computed_land_raise_propagates(producer, vector_repo):
    """Unknown encoding / Qdrant failure raises → message NOT ACKed (PEL/DLQ)."""
    with patch.object(rc, "get_session", _fake_session_cm), patch.object(
        ImageIndexService,
        "land_computed",
        AsyncMock(side_effect=ValueError("unknown vector_encoding")),
    ):
        with pytest.raises(ValueError, match="unknown vector_encoding"):
            await rc._process_computed({"batch_id": "b1", "results": []}, "msg-3")
    producer.publish.assert_not_called()


# ── compute.error leg ────────────────────────────────────────────────────────


async def test_compute_error_publishes_failed(producer, vector_repo):
    lifecycle = {"batch_id": "b1", "status": "error", "error_message": "boom"}
    payload = {"batch_id": "b1", "error_message": "boom"}
    with patch.object(rc, "get_session", _fake_session_cm), patch.object(
        ImageIndexService, "mark_error_from_compute_error", AsyncMock(return_value=lifecycle)
    ) as mark:
        await rc._process_compute_error(payload, "msg-err")

    mark.assert_awaited_once()
    producer.publish.assert_called_once()
    kwargs = producer.publish.call_args.kwargs
    assert kwargs["stream"] == rc.settings.stream_image_batch_raw
    assert kwargs["event_type"] == "image_batch.failed"
    assert kwargs["payload"] is lifecycle
    assert kwargs["maxlen"] == rc.settings.image_index_batch_maxlen


async def test_compute_error_never_minted_is_noop(producer, vector_repo):
    with patch.object(rc, "get_session", _fake_session_cm), patch.object(
        ImageIndexService, "mark_error_from_compute_error", AsyncMock(return_value=None)
    ):
        await rc._process_compute_error({"batch_id": "nope", "error_message": "x"}, "msg-e2")
    producer.publish.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
