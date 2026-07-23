"""Hermetic unit tests for Capability A — async search-by-image (02_SEARCH_DESIGN §6).

All DB / Qdrant / Redis are faked (no live connections — the hard rule). Covers
the must-fixes this increment ships: M1 discriminator routing, M2 evidence_id
NOT-NULL fallback, M3 recalc guard, M5 tenant-scoped read (404), M7 reaper ERROR
branch, M8 flag-off/None-repo ERROR terminalize (never fall through), plus the
S4 degenerate-vector guard, S5 owner_id scoping, and the external_ids cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.api.dependencies import UserContext
from src.api.v1.routers import image_index as ii_router
from src.api.v1.schemas.image_index import ImageIndexSearchCreate
from src.db.models.constants import SearchRequestStatus, SearchType, SimilarityStatus
from src.db.repositories.search_request_repo import SearchRequestRepository
from src.streams import search_results_consumer as srcons

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeReq:
    def __init__(self, **kw):
        self.id = uuid4()
        self.search_id = kw.get("search_id", "s-1")
        self.user_id = kw.get("user_id", "owner-9")
        self.matches = kw.get("matches", [])
        self.status = kw.get("status", SearchRequestStatus.WORKING)
        self.similarity_status = None
        self.total_matches = None
        self.processing_completed_at = None
        self.qdrant_query_point_id = None
        self.error_message = None
        self.search_type = kw.get("search_type", SearchType.IMAGE_INDEX)
        self.external_ids = kw.get("external_ids")
        self.max_results = kw.get("max_results", 50)
        self.threshold = kw.get("threshold", 0.75)
        self.processing_started_at = kw.get("processing_started_at")
        self.retry_count = 0
        self.worker_id = "w-1"


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def execute(self, *a, **k):
        return MagicMock()


class _FakeCM:
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *a):
        return False


def _wire_consumer(monkeypatch, *, request, image_index_repo, live_repo):
    session = _FakeSession()
    fake_repo = MagicMock()
    fake_repo.get_by_search_id = AsyncMock(return_value=request)
    fake_repo.create_request = AsyncMock(return_value=request)
    monkeypatch.setattr(srcons, "get_session", lambda: _FakeCM(session))
    monkeypatch.setattr(srcons, "SearchRequestRepository", lambda s: fake_repo)
    monkeypatch.setattr(srcons, "_image_index_vector_repo", image_index_repo)
    monkeypatch.setattr(srcons, "_vector_repo", live_repo)
    return session, fake_repo


def _match(**kw):
    base = {
        "external_id": "run-1",
        "batch_id": "b-1",
        "item_index": 0,
        "item_ref": None,
        "image_id": "point-xyz",  # item_ref or point_id — already folded (§5)
        "source_url": "http://img/1.jpg",
        "score": 0.91,
        "qdrant_point_id": "point-xyz",
        "user_id": "owner-9",
        "model_version": "clip-vit-b-32",
    }
    base.update(kw)
    return base


# ── Handler: landing, scoping, evidence_id fallback (M2/S5) ───────────────────


async def test_image_index_result_lands_scoped_with_evidence_id_fallback(monkeypatch):
    req = _FakeReq(external_ids=["run-1"], max_results=25, threshold=0.8)
    ii_repo = MagicMock()
    ii_repo.search_similar = AsyncMock(return_value=[_match()])
    live_repo = MagicMock()
    live_repo.store_query_vector = AsyncMock()
    session, _ = _wire_consumer(
        monkeypatch, request=req, image_index_repo=ii_repo, live_repo=live_repo
    )

    payload = {
        "search_id": "s-1",
        "vector": [0.1, 0.2, 0.3],
        "threshold": 0.8,
        "max_results": 25,
        "metadata": {
            "search_type": SearchType.IMAGE_INDEX,
            "user_id": "owner-9",
            "external_ids": ["run-1"],
        },
    }
    await srcons._process_image_index_search_result(payload, "msg-1")

    # Scoped by owner_id + external_ids (S5), forwards top_k/threshold.
    ii_repo.search_similar.assert_awaited_once()
    kwargs = ii_repo.search_similar.call_args.kwargs
    assert kwargs["user_id"] == "owner-9"
    assert kwargs["external_ids"] == ["run-1"]
    assert kwargs["top_k"] == 25
    assert kwargs["threshold"] == 0.8

    # Terminal state.
    assert req.status == SearchRequestStatus.COMPLETED
    assert req.similarity_status == SimilarityStatus.MATCHES_FOUND
    assert req.total_matches == 1
    assert req.qdrant_query_point_id is not None
    live_repo.store_query_vector.assert_awaited_once()

    # M2: item_ref is None → evidence_id falls back to the point id (NOT NULL).
    assert len(session.added) == 1
    m = session.added[0]
    assert m.evidence_id == "point-xyz"
    assert m.external_id == "run-1"
    assert m.match_metadata["batch_id"] == "b-1"
    assert m.match_metadata["search_type"] == SearchType.IMAGE_INDEX


async def test_evidence_id_prefers_item_ref_when_present(monkeypatch):
    req = _FakeReq()
    ii_repo = MagicMock()
    ii_repo.search_similar = AsyncMock(
        return_value=[_match(item_ref="caller-ref-7", image_id="caller-ref-7")]
    )
    live_repo = MagicMock()
    live_repo.store_query_vector = AsyncMock()
    session, _ = _wire_consumer(
        monkeypatch, request=req, image_index_repo=ii_repo, live_repo=live_repo
    )
    payload = {
        "search_id": "s-1",
        "vector": [0.1, 0.2, 0.3],
        "metadata": {
            "search_type": SearchType.IMAGE_INDEX,
            "user_id": "owner-9",
            "external_ids": ["run-1"],
        },
    }
    await srcons._process_image_index_search_result(payload, "msg-1")
    assert session.added[0].evidence_id == "caller-ref-7"


# ── M8: flag-off / None repo → ERROR, never fall through ──────────────────────


async def test_none_repo_marks_error_and_never_searches(monkeypatch):
    req = _FakeReq()
    live_repo = MagicMock()
    live_repo.store_query_vector = AsyncMock()
    _wire_consumer(monkeypatch, request=req, image_index_repo=None, live_repo=live_repo)

    payload = {
        "search_id": "s-1",
        "vector": [0.1, 0.2, 0.3],
        "metadata": {
            "search_type": SearchType.IMAGE_INDEX,
            "user_id": "owner-9",
            "external_ids": ["run-1"],
        },
    }
    await srcons._process_image_index_search_result(payload, "msg-1")
    assert req.status == SearchRequestStatus.ERROR
    # Never touched the evidence-side query-vector store.
    live_repo.store_query_vector.assert_not_awaited()


# ── S4: degenerate query vector → ERROR, no search ────────────────────────────


async def test_degenerate_vector_marks_error(monkeypatch):
    req = _FakeReq()
    ii_repo = MagicMock()
    ii_repo.search_similar = AsyncMock(return_value=[])
    _wire_consumer(monkeypatch, request=req, image_index_repo=ii_repo, live_repo=MagicMock())
    payload = {
        "search_id": "s-1",
        "vector": [0.0, 0.0, 0.0],
        "metadata": {
            "search_type": SearchType.IMAGE_INDEX,
            "user_id": "owner-9",
            "external_ids": ["run-1"],
        },
    }
    await srcons._process_image_index_search_result(payload, "msg-1")
    assert req.status == SearchRequestStatus.ERROR
    ii_repo.search_similar.assert_not_awaited()


async def test_handler_never_reraises(monkeypatch):
    """A crash inside the handler must be swallowed (marks ERROR) so it can
    never regress the shared live consumer."""
    req = _FakeReq()
    ii_repo = MagicMock()
    ii_repo.search_similar = AsyncMock(side_effect=RuntimeError("qdrant down"))
    _wire_consumer(monkeypatch, request=req, image_index_repo=ii_repo, live_repo=MagicMock())
    payload = {
        "search_id": "s-1",
        "vector": [0.1, 0.2, 0.3],
        "metadata": {
            "search_type": SearchType.IMAGE_INDEX,
            "user_id": "owner-9",
            "external_ids": ["run-1"],
        },
    }
    # Must not raise.
    await srcons._process_image_index_search_result(payload, "msg-1")
    assert req.status == SearchRequestStatus.ERROR


# ── M1: discriminator routing ─────────────────────────────────────────────────


async def test_image_index_metadata_routes_to_handler(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(srcons, "_process_image_index_search_result", recorder)
    bl = AsyncMock()
    monkeypatch.setattr(srcons, "_process_blacklist_embed_result", bl)
    payload = {
        "search_id": "s-1",
        "vector": [0.1, 0.2],
        "metadata": {"search_type": SearchType.IMAGE_INDEX, "user_id": "o", "external_ids": ["r"]},
    }
    await srcons._process_search_result(payload, "msg-1")
    recorder.assert_awaited_once()
    bl.assert_not_awaited()


async def test_evidence_metadata_does_not_route_to_image_index(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(srcons, "_process_image_index_search_result", recorder)
    payload = {
        "search_id": "s-1",
        "vector": [0.1, 0.2],
        "metadata": {"camera_id": "cam-3"},  # evidence-shaped, no search_type
    }
    # Evidence path hits SQLAlchemy after dispatch; that it raises is fine — the
    # contract is only that the image-index branch was NOT taken.
    try:
        await srcons._process_search_result(payload, "msg-1")
    except Exception:
        pass
    recorder.assert_not_awaited()


async def test_absent_metadata_does_not_route_to_image_index(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(srcons, "_process_image_index_search_result", recorder)
    payload = {"search_id": "s-1", "vector": [0.1, 0.2]}  # no metadata at all
    try:
        await srcons._process_search_result(payload, "msg-1")
    except Exception:
        pass
    recorder.assert_not_awaited()


# ── M7: reaper terminalize branch ─────────────────────────────────────────────


async def test_reaper_terminalizes_stale_image_index_and_resets_evidence(monkeypatch):
    from src.services import safety_nets

    old_ii = _FakeReq(
        search_type=SearchType.IMAGE_INDEX,
        status=SearchRequestStatus.WORKING,
        processing_started_at=datetime.utcnow() - timedelta(minutes=30),
    )
    recent_ii = _FakeReq(
        search_type=SearchType.IMAGE_INDEX,
        status=SearchRequestStatus.WORKING,
        processing_started_at=datetime.utcnow() - timedelta(minutes=12),
    )
    evidence_row = _FakeReq(
        search_type=SearchType.EVIDENCE,
        status=SearchRequestStatus.WORKING,
        processing_started_at=datetime.utcnow() - timedelta(minutes=30),
    )

    fake_embed_repo = MagicMock()
    fake_embed_repo.get_stale_working = AsyncMock(return_value=[])
    fake_search_repo = MagicMock()
    fake_search_repo.get_stale_working = AsyncMock(
        return_value=[old_ii, recent_ii, evidence_row]
    )

    monkeypatch.setattr(safety_nets, "get_session", lambda: _FakeCM(_FakeSession()))
    monkeypatch.setattr(safety_nets, "EmbeddingRequestRepository", lambda s: fake_embed_repo)
    monkeypatch.setattr(safety_nets, "SearchRequestRepository", lambda s: fake_search_repo)

    await safety_nets.recover_stale_working()

    # Past the 900s image-index cutoff → ERROR (no TO_WORK re-dispatch worker).
    assert old_ii.status == SearchRequestStatus.ERROR
    # Not yet past the image-index cutoff → left WORKING, never reset.
    assert recent_ii.status == SearchRequestStatus.WORKING
    # Evidence keeps the existing reset-to-TO_WORK logic, untouched.
    assert evidence_row.status == SearchRequestStatus.TO_WORK


# ── M3 + M5: query-construction guards (compiled WHERE inspection) ────────────


async def test_get_for_recalculation_is_evidence_guarded():
    captured = {}

    class _Sess:
        async def execute(self, query):
            captured["q"] = str(query)
            res = MagicMock()
            res.scalars.return_value.all.return_value = []
            return res

    repo = SearchRequestRepository(_Sess())
    await repo.get_for_recalculation()
    assert "search_type" in captured["q"]


async def test_get_for_image_index_recalculation_filters_by_type():
    captured = {}

    class _Sess:
        async def execute(self, query):
            captured["q"] = str(query)
            res = MagicMock()
            res.scalars.return_value.all.return_value = []
            return res

    repo = SearchRequestRepository(_Sess())
    await repo.get_for_image_index_recalculation()
    assert "search_type" in captured["q"]


async def test_get_by_search_id_scoped_is_tenant_and_type_scoped():
    captured = {}

    class _Sess:
        async def execute(self, query):
            captured["q"] = str(query)
            res = MagicMock()
            res.scalar.return_value = None
            return res

    repo = SearchRequestRepository(_Sess())
    got = await repo.get_by_search_id_scoped(
        "s-1", user_id="owner-9", search_type=SearchType.IMAGE_INDEX
    )
    assert got is None  # row-miss ≡ tenant-miss ≡ wrong-type
    assert "user_id" in captured["q"]
    assert "search_type" in captured["q"]


# ── External_ids cap (S7) — enforced by the schema ───────────────────────────


def test_external_ids_cap_rejected_at_schema():
    with pytest.raises(Exception):
        ImageIndexSearchCreate(
            image_url="http://q", external_ids=[f"r{i}" for i in range(201)]
        )


def test_external_ids_empty_rejected_at_schema():
    with pytest.raises(Exception):
        ImageIndexSearchCreate(image_url="http://q", external_ids=[])


def test_external_ids_within_cap_ok():
    body = ImageIndexSearchCreate(
        image_url="http://q", external_ids=["r1", "r2"], threshold=0.9, max_results=10
    )
    assert body.external_ids == ["r1", "r2"]
    assert body.threshold == 0.9


# ── 503 gate + IDOR 404 (M5) ──────────────────────────────────────────────────


async def test_search_gate_503_when_flag_off():
    # Default settings → image_index_search_enabled is False.
    with pytest.raises(Exception) as ei:
        await ii_router.require_image_index_search_enabled()
    assert getattr(ei.value, "status_code", None) == 503


async def test_search_gate_503_when_repo_unavailable(monkeypatch):
    from src.infrastructure.config import get_settings

    monkeypatch.setattr(get_settings(), "image_index_search_enabled", True)
    monkeypatch.setattr(ii_router, "_search_repo_available", False)
    monkeypatch.setattr(ii_router, "_search_stream_producer", None)
    with pytest.raises(Exception) as ei:
        await ii_router.require_image_index_search_enabled()
    assert getattr(ei.value, "status_code", None) == 503


async def test_get_search_404_on_tenant_or_type_miss(monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_by_search_id_scoped = AsyncMock(return_value=None)
    monkeypatch.setattr(ii_router, "get_session", lambda: _FakeCM(_FakeSession()))
    monkeypatch.setattr(ii_router, "SearchRequestRepository", lambda s: fake_repo)
    ctx = UserContext(
        user_id="owner-9",
        role="user",
        scopes=[],
        created_by=None,
        app_type=None,
        request_id="r",
    )
    with pytest.raises(Exception) as ei:
        await ii_router.get_image_index_search("missing", ctx=ctx)
    assert getattr(ei.value, "status_code", None) == 404
    # Scoped read was used (not the unscoped get_by_search_id).
    fake_repo.get_by_search_id_scoped.assert_awaited_once()
