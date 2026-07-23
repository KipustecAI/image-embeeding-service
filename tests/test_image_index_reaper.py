"""Unit tests for the image-index timeout reaper (Phase 3).

No live infra — Redis/DB are mocked. Covers the load-bearing reaper invariants
(00_DESIGN §5.3):

  * flag OFF → early return, no session opened (defensive belt).
  * stuck batches → mark_failed each + best-effort publish image_batch.failed
    (explicit stream + maxlen), terminal DB mark before publish.
  * a publish raise does NOT abort the loop — remaining batches still published.
  * no stuck batches → no publish.
  * no producer wired → logs + no crash (reconcile-by-external_id backstop).
"""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.streams.image_index_reaper as reaper
from src.db.models.constants import ImageIndexBatchStatus


def _fake_settings(**overrides):
    s = SimpleNamespace(
        image_index_enabled=True,
        image_index_max_compute_seconds=900,
        stream_image_batch_raw="image_batch:raw",
        image_index_batch_maxlen=20_000,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _fake_batch(batch_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=batch_id,
        client_batch_ref=f"ref-{batch_id}",
        external_id="run-42",
        user_id="11111111-1111-1111-1111-111111111111",
        status=ImageIndexBatchStatus.COMPUTING,
        submitted_count=3,
        embedded_count=0,
        filtered_count=0,
        failed_count=0,
        source_ref="run-42/Vehiculo",
        created_at=datetime(2026, 7, 18, 12, 0, 0),
        completed_at=None,
        error_message=None,
    )


@asynccontextmanager
async def _fake_session_cm():
    session = MagicMock()
    session.commit = AsyncMock()
    yield session


def _patch_repo(stuck):
    repo = MagicMock()
    repo.list_stuck_active = AsyncMock(return_value=stuck)
    repo.mark_failed = AsyncMock()
    return repo


@pytest.fixture
def producer(monkeypatch):
    prod = MagicMock()
    monkeypatch.setattr(reaper, "_producer", prod)
    return prod


# ── Flag guard ───────────────────────────────────────────────────────────────


async def test_flag_off_returns_without_opening_session(producer):
    get_session = MagicMock()
    with patch.object(reaper, "get_settings", return_value=_fake_settings(image_index_enabled=False)), \
         patch.object(reaper, "get_session", get_session):
        await reaper.reap_stuck_image_index_batches()
    get_session.assert_not_called()
    producer.publish.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_marks_and_publishes_each_stuck_batch(producer):
    stuck = [_fake_batch("b1"), _fake_batch("b2")]
    repo = _patch_repo(stuck)
    with patch.object(reaper, "get_settings", return_value=_fake_settings()), \
         patch.object(reaper, "get_session", _fake_session_cm), \
         patch.object(reaper, "ImageIndexRepository", return_value=repo):
        await reaper.reap_stuck_image_index_batches()

    # Each stuck batch terminalized as 'error' with the timeout message.
    assert repo.mark_failed.await_count == 2
    assert repo.mark_failed.await_args_list[0].args[1] == "compute timeout"
    # One image_batch.failed per batch, explicit stream + maxlen.
    assert producer.publish.call_count == 2
    for call in producer.publish.call_args_list:
        assert call.kwargs["stream"] == "image_batch:raw"
        assert call.kwargs["event_type"] == "image_batch.failed"
        assert call.kwargs["maxlen"] == 20_000
    published_ids = {c.kwargs["payload"]["batch_id"] for c in producer.publish.call_args_list}
    assert published_ids == {"b1", "b2"}


async def test_publish_raise_does_not_abort_loop(producer):
    stuck = [_fake_batch("b1"), _fake_batch("b2")]
    repo = _patch_repo(stuck)
    # First publish raises; the second must still be attempted.
    producer.publish.side_effect = [RuntimeError("redis down"), None]
    with patch.object(reaper, "get_settings", return_value=_fake_settings()), \
         patch.object(reaper, "get_session", _fake_session_cm), \
         patch.object(reaper, "ImageIndexRepository", return_value=repo):
        await reaper.reap_stuck_image_index_batches()  # must NOT raise

    # Both batches marked (DB commit happens regardless of publish outcome) and
    # both publishes attempted despite the first raising.
    assert repo.mark_failed.await_count == 2
    assert producer.publish.call_count == 2


async def test_no_stuck_batches_no_publish(producer):
    repo = _patch_repo([])
    with patch.object(reaper, "get_settings", return_value=_fake_settings()), \
         patch.object(reaper, "get_session", _fake_session_cm), \
         patch.object(reaper, "ImageIndexRepository", return_value=repo):
        await reaper.reap_stuck_image_index_batches()
    repo.mark_failed.assert_not_awaited()
    producer.publish.assert_not_called()


async def test_no_producer_wired_marks_but_skips_publish(monkeypatch):
    monkeypatch.setattr(reaper, "_producer", None)
    stuck = [_fake_batch("b1")]
    repo = _patch_repo(stuck)
    with patch.object(reaper, "get_settings", return_value=_fake_settings()), \
         patch.object(reaper, "get_session", _fake_session_cm), \
         patch.object(reaper, "ImageIndexRepository", return_value=repo):
        await reaper.reap_stuck_image_index_batches()  # must NOT raise
    # Batch still terminalized even though nothing can be published.
    repo.mark_failed.assert_awaited_once()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
