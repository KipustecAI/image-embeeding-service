"""Unit tests for blacklist_reverse_search.

Focus: scheduling correctness and the no-op fallbacks. The actual Qdrant
search is exercised in integration; here we verify that a job is
registered with the right id, replace-existing semantics, and that the
runner aborts cleanly when wiring is missing (so a misconfigured deploy
fails loudly in logs rather than silently dropping reverse searches).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services import blacklist_reverse_search as brs


@pytest.fixture(autouse=True)
def reset_module_globals():
    """Each test starts with a clean module state — these globals are
    set during FastAPI lifespan in production, but tests must isolate."""
    brs._scheduler = None
    brs._vector_repo = None
    yield
    brs._scheduler = None
    brs._vector_repo = None


# ── schedule_reverse_search ────────────────────────────────────────────────


def test_schedule_returns_none_when_scheduler_missing():
    """A misconfigured deploy must surface in logs, not silently drop
    reverse searches. Returning None is the signal callers test on."""
    job_id = brs.schedule_reverse_search(
        entry_id=uuid4(),
        reference_id=uuid4(),
        user_id="u-1",
        vector=[0.1, 0.2],
    )
    assert job_id is None


def test_schedule_uses_reference_id_in_job_id():
    """Idempotency hinges on the job id — two scheduling calls for the
    same reference must collide so replace_existing kicks in."""
    fake_scheduler = MagicMock()
    brs.set_reverse_search_scheduler(fake_scheduler)

    ref_id = uuid4()
    returned = brs.schedule_reverse_search(
        entry_id=uuid4(),
        reference_id=ref_id,
        user_id="u-1",
        vector=[0.1, 0.2],
    )

    assert returned == f"reverse_search_{ref_id}"
    fake_scheduler.add_job.assert_called_once()
    kwargs = fake_scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == f"reverse_search_{ref_id}"
    assert kwargs["replace_existing"] is True
    assert kwargs["trigger"] == "date"


def test_schedule_passes_vector_through_unchanged():
    """The reverse-search runner reads the vector by-value via args.
    Mutating it after scheduling must not affect the queued job."""
    fake_scheduler = MagicMock()
    brs.set_reverse_search_scheduler(fake_scheduler)

    vec = [0.1, 0.2, 0.3]
    brs.schedule_reverse_search(
        entry_id=uuid4(),
        reference_id=uuid4(),
        user_id="u-1",
        vector=vec,
    )

    args = fake_scheduler.add_job.call_args.kwargs["args"]
    assert args[3] == [0.1, 0.2, 0.3]


# ── _run_reverse_search ────────────────────────────────────────────────────


async def test_run_aborts_silently_when_vector_repo_missing(caplog):
    """Misconfigured deploy: log + return, do NOT raise (the scheduler
    swallows raises in jobs, so a raise would silently lose the run)."""
    import logging as _logging

    caplog.set_level(_logging.ERROR, logger="src.services.blacklist_reverse_search")
    await brs._run_reverse_search(uuid4(), uuid4(), "u-1", [0.1, 0.2])
    assert any("Vector repo not configured" in rec.message for rec in caplog.records)


async def test_run_calls_search_similar_with_user_scoped_filter():
    """Reverse-search results must scope to the user (multi-tenant) and
    to source_type=evidence (so blacklist points don't match each
    other). Verified via the filter dict passed to search_similar."""
    fake_repo = MagicMock()
    fake_repo.search_similar = AsyncMock(return_value=[])
    brs.set_reverse_search_vector_repo(fake_repo)

    await brs._run_reverse_search(uuid4(), uuid4(), "tenant-42", [0.1, 0.2])

    fake_repo.search_similar.assert_called_once()
    kwargs = fake_repo.search_similar.call_args.kwargs
    filter_conditions = kwargs["filter_conditions"]
    assert filter_conditions["user_id"] == "tenant-42"
    assert filter_conditions["source_type"] == "evidence"


async def test_run_uses_configured_threshold_and_batch_size():
    """Threshold + batch_size come from settings. Hardcoded values
    here would diverge silently from the env-driven defaults."""
    from src.infrastructure.config import get_settings

    s = get_settings()
    fake_repo = MagicMock()
    fake_repo.search_similar = AsyncMock(return_value=[])
    brs.set_reverse_search_vector_repo(fake_repo)

    await brs._run_reverse_search(uuid4(), uuid4(), "u-1", [0.1, 0.2])

    kwargs = fake_repo.search_similar.call_args.kwargs
    assert kwargs["threshold"] == s.blacklist_match_threshold
    assert kwargs["limit"] == s.blacklist_reverse_search_batch_size


async def test_run_logs_match_count_on_success(caplog):
    """The completion log line is the audit trail for v1 (no jobs table
    yet). Ops greps for it after an entry is created."""
    import logging as _logging

    caplog.set_level(_logging.INFO, logger="src.services.blacklist_reverse_search")
    fake_repo = MagicMock()
    fake_repo.search_similar = AsyncMock(return_value=[1, 2, 3, 4])  # any iterable
    brs.set_reverse_search_vector_repo(fake_repo)

    await brs._run_reverse_search(uuid4(), uuid4(), "u-1", [0.1, 0.2])

    assert any("matches=4" in rec.message for rec in caplog.records)


async def test_run_swallows_search_failure_with_explicit_log(caplog):
    """search_similar can throw (network / Qdrant down). The runner
    must NOT propagate — APScheduler eats raises in jobs by default,
    so a propagated raise is a silent loss. Explicit error log is the
    contract."""
    import logging as _logging

    caplog.set_level(_logging.ERROR, logger="src.services.blacklist_reverse_search")
    fake_repo = MagicMock()
    fake_repo.search_similar = AsyncMock(side_effect=RuntimeError("qdrant down"))
    brs.set_reverse_search_vector_repo(fake_repo)

    await brs._run_reverse_search(uuid4(), uuid4(), "u-1", [0.1, 0.2])

    assert any("Reverse search failed" in rec.message for rec in caplog.records)
