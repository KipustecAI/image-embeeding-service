"""Unit tests for Capability B — GPU-free blacklist cross-reference (P3+P3b).

Everything is mocked (no live DB / Qdrant / Redis / compute). Focus:
  * the xref core does ZERO compute dispatch (no stream/XADD to any embed stream)
  * model_version mismatch → log-and-skip (§3)
  * degenerate blacklist vector + orphaned point → skip
  * tenant IDOR (foreign entry → [])
  * max-score-per-indexed-point aggregation
  * REST route: entry-404, 422 over-cap, tenant scope, 503 gate off
  * auto-hook: gated off, fast-exit on no blacklist, fires AFTER terminal publish,
    fire-and-forget (never raises), additive image:blacklist_match fields (S8)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.application.helpers.blacklist_match_events import build_blacklist_match_event
from src.services import blacklist_image_index_xref as xref


class _FakeCM:
    """Minimal async context manager yielding a fixed session object."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _hit(*, point_id="p-idx-1", score=0.9, external_id="run-1", model_version="clip-vit-b-32",
         item_index=3, item_ref="ref-1", source_url="http://img/1.jpg", batch_id="b-1"):
    """A search_similar() result dict (shape from ImageIndexVectorRepository._hit_to_match)."""
    return {
        "external_id": external_id,
        "batch_id": batch_id,
        "item_index": item_index,
        "item_ref": item_ref,
        "image_id": item_ref or point_id,
        "source_url": source_url,
        "score": score,
        "qdrant_point_id": point_id,
        "user_id": "owner-1",
        "model_version": model_version,
    }


def _emb(*, reference_id=None, qdrant_point_id="bl-pt-1", model_version="clip-vit-b-32"):
    return SimpleNamespace(
        reference_id=reference_id or uuid4(),
        qdrant_point_id=qdrant_point_id,
        model_version=model_version,
    )


def _entry(*, entry_id, user_id="owner-1", match_threshold=None):
    return SimpleNamespace(id=entry_id, user_id=user_id, match_threshold=match_threshold)


def _wire_core(monkeypatch, *, entry, embeddings, evidence_repo, image_index_repo):
    """Wire the module globals + a fake session/repo for cross_reference_entry."""
    fake_repo = MagicMock()
    fake_repo.get_entry = AsyncMock(return_value=entry)
    fake_repo.list_embeddings = AsyncMock(return_value=embeddings)
    session = MagicMock()
    monkeypatch.setattr(xref, "get_session", lambda: _FakeCM(session))
    monkeypatch.setattr(xref, "BlacklistImageRepository", lambda s: fake_repo)
    xref.set_xref_evidence_repo(evidence_repo)
    xref.set_xref_image_index_repo(image_index_repo)
    return fake_repo


@pytest.fixture(autouse=True)
def _reset_globals():
    xref._evidence_repo = None
    xref._image_index_repo = None
    yield
    xref._evidence_repo = None
    xref._image_index_repo = None


# ── Core: happy path + NO compute dispatch ──────────────────────────────────


@pytest.mark.asyncio
async def test_core_matches_and_never_dispatches_compute(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock(return_value=[0.1, 0.2, 0.3])
    ii = MagicMock()
    ii.search_similar = AsyncMock(return_value=[_hit()])
    # A stream producer would raise if ANY compute XADD were attempted.
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id),
        embeddings=[_emb()],
        evidence_repo=ev,
        image_index_repo=ii,
    )

    matches = await xref.cross_reference_entry(
        user_id="owner-1", entry_id=entry_id, external_ids=["run-1"]
    )

    assert len(matches) == 1
    m = matches[0]
    assert m["blacklist_entry_id"] == str(entry_id)
    assert m["image_id"] == "ref-1"
    assert m["similarity_score"] == 0.9
    # GPU-free contract: the ONLY Qdrant calls are the read primitives — no
    # embed/search dispatch, no stream producer touched at all.
    ev.get_point_vector.assert_awaited_once()
    ii.search_similar.assert_awaited_once()
    # search_similar is called with the tenant + external_ids scope (IDOR).
    _, kwargs = ii.search_similar.call_args
    assert kwargs["user_id"] == "owner-1"
    assert kwargs["external_ids"] == ["run-1"]


@pytest.mark.asyncio
async def test_core_tenant_miss_returns_empty(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock()
    ii = MagicMock()
    ii.search_similar = AsyncMock()
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id, user_id="someone-else"),
        embeddings=[_emb()],
        evidence_repo=ev,
        image_index_repo=ii,
    )

    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)

    assert matches == []
    ev.get_point_vector.assert_not_awaited()  # never fetches a foreign entry's vectors
    ii.search_similar.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_entry_missing_returns_empty(monkeypatch):
    entry_id = uuid4()
    ii = MagicMock()
    ii.search_similar = AsyncMock()
    _wire_core(
        monkeypatch,
        entry=None,
        embeddings=[],
        evidence_repo=MagicMock(),
        image_index_repo=ii,
    )
    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)
    assert matches == []


@pytest.mark.asyncio
async def test_core_model_version_mismatch_skips(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock(return_value=[0.1, 0.2, 0.3])
    ii = MagicMock()
    # Indexed point stamped with a DIFFERENT model_version than the blacklist point.
    ii.search_similar = AsyncMock(return_value=[_hit(model_version="clip-vit-l-14")])
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id),
        embeddings=[_emb(model_version="clip-vit-b-32")],
        evidence_repo=ev,
        image_index_repo=ii,
    )
    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)
    assert matches == []  # decalibration guard fired


@pytest.mark.asyncio
async def test_core_degenerate_blacklist_vector_skips(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock(return_value=[0.0, 0.0, 0.0])  # zero vector
    ii = MagicMock()
    ii.search_similar = AsyncMock(return_value=[_hit()])
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id),
        embeddings=[_emb()],
        evidence_repo=ev,
        image_index_repo=ii,
    )
    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)
    assert matches == []
    ii.search_similar.assert_not_awaited()  # skipped before issuing the search


@pytest.mark.asyncio
async def test_core_orphaned_point_skips(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock(return_value=None)  # orphaned / missing
    ii = MagicMock()
    ii.search_similar = AsyncMock(return_value=[_hit()])
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id),
        embeddings=[_emb()],
        evidence_repo=ev,
        image_index_repo=ii,
    )
    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)
    assert matches == []
    ii.search_similar.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_aggregates_max_score_per_indexed_point(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock(return_value=[0.1, 0.2, 0.3])
    ii = MagicMock()
    # Two references both hit the SAME indexed point p-idx-1 with different scores.
    ii.search_similar = AsyncMock(
        side_effect=[
            [_hit(point_id="p-idx-1", score=0.80)],
            [_hit(point_id="p-idx-1", score=0.95)],
        ]
    )
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id),
        embeddings=[_emb(qdrant_point_id="bl-1"), _emb(qdrant_point_id="bl-2")],
        evidence_repo=ev,
        image_index_repo=ii,
    )
    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)
    assert len(matches) == 1  # same indexed image never returned twice
    assert matches[0]["similarity_score"] == 0.95  # max wins


@pytest.mark.asyncio
async def test_core_per_entry_threshold_override(monkeypatch):
    entry_id = uuid4()
    ev = MagicMock()
    ev.get_point_vector = AsyncMock(return_value=[0.1, 0.2, 0.3])
    ii = MagicMock()
    ii.search_similar = AsyncMock(return_value=[])
    _wire_core(
        monkeypatch,
        entry=_entry(entry_id=entry_id, match_threshold=0.42),
        embeddings=[_emb()],
        evidence_repo=ev,
        image_index_repo=ii,
    )
    await xref.cross_reference_entry(user_id="owner-1", entry_id=entry_id)
    _, kwargs = ii.search_similar.call_args
    assert kwargs["threshold"] == 0.42  # per-entry override used when no request threshold


@pytest.mark.asyncio
async def test_core_unwired_repos_return_empty(monkeypatch):
    # globals reset by fixture → repos None
    matches = await xref.cross_reference_entry(user_id="owner-1", entry_id=uuid4())
    assert matches == []


# ── Event builder additive widening (S8) ────────────────────────────────────


def _base_event_kwargs():
    return {
        "user_id": "u",
        "blacklist_entry_id": "e",
        "blacklist_entry_name": "n",
        "blacklist_entry_category": "c",
        "blacklist_entry_version": 1,
        "blacklist_reference_id": "r",
        "blacklist_reference_url": "http://ref",
        "blacklist_model_version": "clip-vit-b-32",
        "evidence_id": "ev",
        "evidence_camera_id": None,
        "evidence_device_id": None,
        "evidence_app_id": None,
        "evidence_infraction_code": None,
        "evidence_category": None,
        "matched_image_url": "http://m",
        "matched_image_index": 0,
        "matched_qdrant_point_id": "q",
        "similarity_score": 0.9,
        "threshold_used": 0.85,
    }


def test_event_evidence_path_byte_identical():
    """Evidence callers must produce NO new keys (S8 non-regression)."""
    ev = build_blacklist_match_event(trigger="reverse_search", **_base_event_kwargs())
    assert "match_target" not in ev
    assert "external_id" not in ev
    assert "batch_id" not in ev


def test_event_image_index_path_has_additive_fields():
    ev = build_blacklist_match_event(
        trigger="image_index_xref",
        match_target="image_index",
        external_id="run-1",
        batch_id="b-1",
        **_base_event_kwargs(),
    )
    assert ev["match_target"] == "image_index"
    assert ev["external_id"] == "run-1"
    assert ev["batch_id"] == "b-1"
    assert ev["trigger"] == "image_index_xref"


# ── Auto-on-land hook (P3b) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_hook_gated_off_no_op(monkeypatch):
    """When the flag is off _maybe_autocheck_blacklist must not touch the xref."""
    import src.streams.image_index_results_consumer as cons

    monkeypatch.setattr(cons.settings, "image_index_blacklist_autocheck_enabled", False)
    called = AsyncMock()
    monkeypatch.setattr(
        "src.services.blacklist_image_index_xref.auto_cross_reference_batch", called
    )
    await cons._maybe_autocheck_blacklist({"user_id": "u", "batch_id": "b"})
    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_hook_fires_after_terminal_publish(monkeypatch):
    """The gated hook runs AFTER _publish_lifecycle(completed), never before."""
    import src.streams.image_index_results_consumer as cons

    order = []
    lifecycle = {"user_id": "u", "batch_id": "b", "external_id": "run-1"}

    service = MagicMock()
    service.land_computed = AsyncMock(return_value=lifecycle)
    monkeypatch.setattr(cons, "ImageIndexService", lambda: service)
    monkeypatch.setattr(cons, "get_session", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(
        cons, "_publish_lifecycle", lambda et, p: order.append(("publish", et))
    )

    async def _fake_autocheck(lc):
        order.append(("autocheck", lc["batch_id"]))

    monkeypatch.setattr(cons, "_maybe_autocheck_blacklist", _fake_autocheck)

    await cons._process_computed({"batch_id": "b"}, "msg-1")

    assert order == [("publish", cons._LIFECYCLE_COMPLETED), ("autocheck", "b")]


@pytest.mark.asyncio
async def test_auto_hook_fast_exits_when_no_blacklist(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.count_active_by_user = AsyncMock(return_value=0)
    fake_repo.list_active_indexed_entries = AsyncMock(return_value=[])
    monkeypatch.setattr(xref, "get_session", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(xref, "BlacklistImageRepository", lambda s: fake_repo)
    xref.set_xref_evidence_repo(MagicMock())
    xref.set_xref_image_index_repo(MagicMock())

    published = await xref.auto_cross_reference_batch(user_id="u", batch_id="b")

    assert published == 0
    fake_repo.list_active_indexed_entries.assert_not_awaited()  # fast-exit before listing


@pytest.mark.asyncio
async def test_auto_hook_unwired_repos_no_op(monkeypatch):
    # globals reset by fixture → repos None
    published = await xref.auto_cross_reference_batch(user_id="u", batch_id="b")
    assert published == 0


@pytest.mark.asyncio
async def test_auto_hook_publishes_additive_event(monkeypatch):
    entry_id = uuid4()
    ref_id = uuid4()
    entry = _entry(entry_id=entry_id)

    fake_repo = MagicMock()
    fake_repo.count_active_by_user = AsyncMock(return_value=1)
    fake_repo.list_active_indexed_entries = AsyncMock(return_value=[entry])
    fake_repo.get_reference = AsyncMock(
        return_value=SimpleNamespace(id=ref_id, image_url="http://ref")
    )
    monkeypatch.setattr(xref, "get_session", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(xref, "BlacklistImageRepository", lambda s: fake_repo)
    xref.set_xref_evidence_repo(MagicMock())
    xref.set_xref_image_index_repo(MagicMock())

    match_dict = {
        "blacklist_entry_id": str(entry_id),
        "blacklist_reference_id": str(ref_id),
        "external_id": "run-1",
        "batch_id": "b-1",
        "item_index": 3,
        "image_id": "ref-x",
        "source_url": "http://img/x.jpg",
        "qdrant_point_id": "p-idx-1",
        "similarity_score": 0.91,
        "threshold_used": 0.85,
    }
    monkeypatch.setattr(
        xref, "cross_reference_entry", AsyncMock(return_value=[match_dict])
    )
    published_events = []

    async def _fake_publish(**kwargs):
        published_events.append(kwargs)

    # publish_blacklist_match is imported lazily inside _publish_autocheck_match.
    monkeypatch.setattr(
        "src.services.blacklist_match_service.publish_blacklist_match", _fake_publish
    )

    published = await xref.auto_cross_reference_batch(user_id="owner-1", batch_id="b-1")

    assert published == 1
    ev = published_events[0]
    assert ev["match_target"] == "image_index"
    assert ev["external_id"] == "run-1"
    assert ev["batch_id"] == "b-1"
    assert ev["trigger"] == "image_index_xref"
    assert ev["evidence_id"] == "ref-x"  # item_ref-or-point_id fallback (M2)
    assert ev["evidence_metadata"] == {}  # image-index carries no evidence fields


@pytest.mark.asyncio
async def test_auto_hook_never_raises_on_publish_failure(monkeypatch):
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    fake_repo = MagicMock()
    fake_repo.count_active_by_user = AsyncMock(return_value=1)
    fake_repo.list_active_indexed_entries = AsyncMock(return_value=[entry])
    monkeypatch.setattr(xref, "get_session", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(xref, "BlacklistImageRepository", lambda s: fake_repo)
    xref.set_xref_evidence_repo(MagicMock())
    xref.set_xref_image_index_repo(MagicMock())
    monkeypatch.setattr(
        xref,
        "cross_reference_entry",
        AsyncMock(side_effect=RuntimeError("qdrant down")),
    )
    # Must swallow the per-entry failure and return cleanly (never block land).
    published = await xref.auto_cross_reference_batch(user_id="owner-1", batch_id="b-1")
    assert published == 0


# ── REST endpoint (§7.3) ────────────────────────────────────────────────────


@pytest.fixture
def rest_client():
    """Flag-on client for the cross-reference route (503 gate bypassed)."""
    from fastapi.testclient import TestClient

    import src.api.v1.routers.blacklist_image as bl_router
    import src.api.v1.routers.image_index as ii_router
    from src.main import app

    app.dependency_overrides[ii_router.require_image_index_search_enabled] = lambda: None
    yield TestClient(app), bl_router
    app.dependency_overrides.clear()


HEADERS = {"X-User-Id": "owner-1", "X-User-Role": "user"}


def test_rest_503_when_search_disabled():
    """No override → the real gate 503s (flag defaults off)."""
    from fastapi.testclient import TestClient

    from src.main import app

    app.dependency_overrides.clear()
    client = TestClient(app)
    r = client.post(
        f"/api/v1/blacklist/image-entries/{uuid4()}/cross-reference",
        json={"external_ids": ["run-1"]},
        headers=HEADERS,
    )
    assert r.status_code == 503


def test_rest_401_missing_user(rest_client):
    client, _ = rest_client
    r = client.post(
        f"/api/v1/blacklist/image-entries/{uuid4()}/cross-reference",
        json={"external_ids": ["run-1"]},
    )
    assert r.status_code == 401


def test_rest_404_entry_not_under_tenant(rest_client, monkeypatch):
    client, bl_router = rest_client
    fake_repo = MagicMock()
    # Entry belongs to someone else → tenant miss → 404 for the entry.
    fake_repo.get_entry = AsyncMock(
        return_value=SimpleNamespace(id=uuid4(), user_id="other", match_threshold=None)
    )
    monkeypatch.setattr(bl_router, "get_session", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(bl_router, "BlacklistImageRepository", lambda s: fake_repo)
    xref_spy = AsyncMock()
    monkeypatch.setattr(bl_router, "cross_reference_entry", xref_spy)

    r = client.post(
        f"/api/v1/blacklist/image-entries/{uuid4()}/cross-reference",
        json={"external_ids": ["run-1"]},
        headers=HEADERS,
    )
    assert r.status_code == 404
    xref_spy.assert_not_awaited()  # never runs vectors for a foreign entry


def test_rest_422_over_external_ids_cap(rest_client):
    client, _ = rest_client
    # Pydantic max_length=200 rejects at the schema boundary.
    r = client.post(
        f"/api/v1/blacklist/image-entries/{uuid4()}/cross-reference",
        json={"external_ids": [f"run-{i}" for i in range(201)]},
        headers=HEADERS,
    )
    assert r.status_code == 422


def test_rest_200_returns_matches(rest_client, monkeypatch):
    client, bl_router = rest_client
    entry_id = uuid4()
    fake_repo = MagicMock()
    fake_repo.get_entry = AsyncMock(
        return_value=SimpleNamespace(id=entry_id, user_id="owner-1", match_threshold=None)
    )
    monkeypatch.setattr(bl_router, "get_session", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(bl_router, "BlacklistImageRepository", lambda s: fake_repo)
    match_dict = {
        "blacklist_entry_id": str(entry_id),
        "blacklist_reference_id": str(uuid4()),
        "external_id": "run-1",
        "batch_id": "b-1",
        "item_index": 3,
        "image_id": "ref-x",
        "source_url": "http://img/x.jpg",
        "qdrant_point_id": "p-idx-1",
        "similarity_score": 0.91,
        "threshold_used": 0.85,
    }
    monkeypatch.setattr(
        bl_router, "cross_reference_entry", AsyncMock(return_value=[match_dict])
    )

    r = client.post(
        f"/api/v1/blacklist/image-entries/{entry_id}/cross-reference",
        json={"external_ids": ["run-1"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["entry_id"] == str(entry_id)
    assert body["match_count"] == 1
    assert body["matches"][0]["image_id"] == "ref-x"
    assert body["threshold_used"] == 0.85  # global default reported
