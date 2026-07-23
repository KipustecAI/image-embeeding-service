"""Integration tests for the main.py lifespan wiring of the image-index flow.

No live infra — the DB engine, Qdrant, Redis StreamProducer, APScheduler and every
consumer factory are mocked. Covers the load-bearing lifespan invariants
(00_DESIGN §5.4):

  * flag ON  → the DEDICATED ImageIndexVectorRepository is constructed and
    initialize()'d with the SHARED live QdrantClient (never a second client);
    the RESULTS consumer starts BEFORE the submit consumer (cold-start ordering);
    the reaper job is added to the EXISTING scheduler as id="image_index_reaper".
  * flag OFF → nothing image-index is constructed or started (no dedicated repo,
    no consumers, no reaper job), the module globals stay None, and the FastAPI
    app still imports at 24 routes (Phase 3 adds no routes).

These exercise the ONLY runtime chokepoint (``if settings.image_index_enabled``)
with mocked collaborators; the ENABLED path against live Redis/Qdrant/DB is
deferred-to-human.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import src.main as main


class _AsyncCM:
    """Minimal async context manager wrapping a fixed object (engine.connect())."""

    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def main_patches(monkeypatch):
    """Patch every infra collaborator the lifespan touches; hand back the mocks.

    Leaves the setter functions real (they only assign module globals in other
    modules — no I/O) and IntervalTrigger real (pure construction). Only the
    constructors/factories that would reach Redis/Qdrant/Postgres/threads are
    replaced.
    """
    # DB engine — connect() is an async CM around a conn whose execute is awaited;
    # dispose() is awaited at shutdown.
    conn = MagicMock()
    conn.execute = AsyncMock()
    engine = MagicMock()
    engine.connect = MagicMock(return_value=_AsyncCM(conn))
    engine.dispose = AsyncMock()
    monkeypatch.setattr(main, "engine", engine)

    # Live Qdrant repo — initialize() awaited; .client is the SHARED handle the
    # dedicated repo must reuse.
    qdrant_repo = MagicMock(name="live-qdrant-repo")
    qdrant_repo.initialize = AsyncMock()
    monkeypatch.setattr(
        main, "QdrantVectorRepository", MagicMock(return_value=qdrant_repo)
    )

    # Dedicated image-index repo — initialize(client) awaited.
    image_index_repo = MagicMock(name="dedicated-image-index-repo")
    image_index_repo.initialize = AsyncMock()
    image_index_vr_cls = MagicMock(return_value=image_index_repo)
    monkeypatch.setattr(main, "ImageIndexVectorRepository", image_index_vr_cls)

    # Storage uploader + stream producer — plain constructors, mock to avoid I/O.
    monkeypatch.setattr(main, "StorageUploader", MagicMock())
    stream_producer = MagicMock(name="stream-producer")
    monkeypatch.setattr(main, "StreamProducer", MagicMock(return_value=stream_producer))

    # Scheduler — mock the class so no real thread starts; capture add_job calls.
    scheduler = MagicMock(name="scheduler")
    monkeypatch.setattr(main, "AsyncIOScheduler", MagicMock(return_value=scheduler))

    # Live consumers.
    monkeypatch.setattr(main, "create_embedding_results_consumer", MagicMock())
    monkeypatch.setattr(main, "create_search_results_consumer", MagicMock())

    # Image-index consumers — distinct mocks so start-ordering is observable.
    results_consumer = MagicMock(name="image-index-results-consumer")
    submit_consumer = MagicMock(name="image-index-submit-consumer")
    results_factory = MagicMock(return_value=results_consumer)
    submit_factory = MagicMock(return_value=submit_consumer)
    monkeypatch.setattr(main, "create_image_index_results_consumer", results_factory)
    monkeypatch.setattr(main, "create_image_index_submit_consumer", submit_factory)

    # Reset the module globals the gated block assigns so a prior ON test can't
    # leak state into an OFF assertion.
    monkeypatch.setattr(main, "image_index_results_consumer", None)
    monkeypatch.setattr(main, "image_index_submit_consumer", None)
    monkeypatch.setattr(main, "image_index_vector_repo", None)

    return {
        "engine": engine,
        "qdrant_repo": qdrant_repo,
        "image_index_repo": image_index_repo,
        "image_index_vr_cls": image_index_vr_cls,
        "stream_producer": stream_producer,
        "scheduler": scheduler,
        "results_factory": results_factory,
        "submit_factory": submit_factory,
        "results_consumer": results_consumer,
        "submit_consumer": submit_consumer,
    }


def _reaper_job_ids(scheduler) -> list:
    return [c.kwargs.get("id") for c in scheduler.add_job.call_args_list]


# ── flag ON ──────────────────────────────────────────────────────────────────


async def test_lifespan_enabled_inits_dedicated_repo_with_shared_client(
    main_patches, monkeypatch
):
    monkeypatch.setattr(main.settings, "image_index_enabled", True)

    async with main.lifespan(MagicMock()):
        pass

    # Dedicated repo constructed once and initialize()'d with the SHARED live
    # QdrantClient — never a second client.
    main_patches["image_index_vr_cls"].assert_called_once()
    main_patches["image_index_repo"].initialize.assert_awaited_once_with(
        main_patches["qdrant_repo"].client
    )
    # The live repo's own initialize() is still awaited unguarded (byte-unchanged).
    main_patches["qdrant_repo"].initialize.assert_awaited_once()


async def test_lifespan_enabled_starts_results_before_submit(main_patches, monkeypatch):
    monkeypatch.setattr(main.settings, "image_index_enabled", True)

    order: list[str] = []
    main_patches["results_consumer"].start.side_effect = lambda: order.append("results")
    main_patches["submit_consumer"].start.side_effect = lambda: order.append("submit")

    async with main.lifespan(MagicMock()):
        pass

    # Both image-index consumers were constructed and started, results FIRST
    # (cold-start ordering is load-bearing — 00_DESIGN §5.4).
    main_patches["results_factory"].assert_called_once()
    main_patches["submit_factory"].assert_called_once()
    assert order == ["results", "submit"]


async def test_lifespan_enabled_registers_reaper_on_existing_scheduler(
    main_patches, monkeypatch
):
    monkeypatch.setattr(main.settings, "image_index_enabled", True)

    async with main.lifespan(MagicMock()):
        pass

    # Reaper added to the EXISTING scheduler (not a second one) with the interval
    # from settings and the reaper callable.
    scheduler = main_patches["scheduler"]
    assert "image_index_reaper" in _reaper_job_ids(scheduler)
    reaper_call = next(
        c
        for c in scheduler.add_job.call_args_list
        if c.kwargs.get("id") == "image_index_reaper"
    )
    assert reaper_call.args[0] is main.reap_stuck_image_index_batches
    # Exactly one scheduler was constructed for the whole app.
    main.AsyncIOScheduler.assert_called_once()


async def test_lifespan_enabled_stops_results_consumer_on_shutdown(
    main_patches, monkeypatch
):
    monkeypatch.setattr(main.settings, "image_index_enabled", True)

    async with main.lifespan(MagicMock()):
        pass

    main_patches["results_consumer"].stop.assert_called_once()
    main_patches["submit_consumer"].stop.assert_called_once()


# ── flag OFF ─────────────────────────────────────────────────────────────────


async def test_lifespan_disabled_constructs_and_starts_nothing_image_index(
    main_patches, monkeypatch
):
    monkeypatch.setattr(main.settings, "image_index_enabled", False)

    async with main.lifespan(MagicMock()):
        pass

    # No dedicated repo, no image-index consumers, no reaper job.
    main_patches["image_index_vr_cls"].assert_not_called()
    main_patches["results_factory"].assert_not_called()
    main_patches["submit_factory"].assert_not_called()
    assert "image_index_reaper" not in _reaper_job_ids(main_patches["scheduler"])
    # Module globals stay None → shutdown skips them cleanly.
    assert main.image_index_results_consumer is None
    assert main.image_index_submit_consumer is None
    assert main.image_index_vector_repo is None


def test_app_imports_at_28_routes():
    # Phase 4 mounted the 2 read legs; the image-index SEARCH increment
    # (02_SEARCH_DESIGN Capability A) added 3 gated routes (POST /search,
    # GET /search/{id}, GET /search/{id}/matches). Capability B (§7.3) adds 1
    # more gated route — POST /api/v1/blacklist/image-entries/{id}/cross-reference
    # on the unconditionally-mounted blacklist router (503 when the search flag is
    # off). 24 → 27 → 28.
    assert len(main.app.routes) == 28


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
