"""Tenant scope of user image search (src/application/helpers/search_scope.py).

Before 2026-11-17 ``POST /api/v1/search`` stored ``metadata.user_id`` but no search
path applied it, so a non-admin search matched every tenant's evidence (and the
hourly recalculation could also return blacklist points). These tests lock:

  * the scope fixed at submit time (own / guest → inviter / hub / admin);
  * the filter every path builds (evidence-only + owners, empty → match nothing);
  * the live consumer and the hourly recalculation actually pass it to Qdrant;
  * the read routes refuse other people's searches.

The consumer + recalculation tests need a migrated Postgres at DATABASE_URL and are
skipped otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import text

import src.main as main
from src.api.dependencies import UserContext
from src.application.helpers import search_scope as sc
from src.infrastructure.database import AsyncSessionLocal, engine
from src.services import identity_client, safety_nets
from src.streams import search_results_consumer as consumer


def _ctx(user_id="u-1", role="user", created_by=None) -> UserContext:
    return UserContext(
        user_id=user_id, role=role, scopes=[], created_by=created_by, app_type=None, request_id=""
    )


# ── build_search_filter ──────────────────────────────────────────────────────


def test_scoped_filter_is_owners_and_evidence_only():
    f = sc.build_search_filter({sc.SCOPE_KEY: ["owner-a", "owner-b"], "camera_id": "cam-1"})
    assert f == {"camera_id": "cam-1", "user_id": ["owner-a", "owner-b"], "source_type": "evidence"}


def test_empty_scope_matches_nothing():
    assert sc.build_search_filter({sc.SCOPE_KEY: []}) is sc.MATCH_NOTHING


def test_malformed_scope_never_widens():
    assert sc.build_search_filter({sc.SCOPE_KEY: "owner-a"}) is sc.MATCH_NOTHING


def test_legacy_user_id_is_honoured():
    assert sc.build_search_filter({"user_id": "u-9"})["user_id"] == ["u-9"]


def test_unscoped_admin_search_is_still_evidence_only():
    assert sc.build_search_filter({}) == {"source_type": "evidence"}
    assert sc.build_search_filter(None) == {"source_type": "evidence"}


def test_weapons_and_category_pass_through():
    f = sc.build_search_filter(
        {sc.SCOPE_KEY: ["o"], "weapons_filter": "only", "category": ["2", "5"]}
    )
    assert f["category"] == ["2", "5"]
    assert f["has_weapon"] is True
    assert f["user_id"] == ["o"]


def test_client_cannot_set_scope_keys():
    assert sc.strip_reserved(
        {
            "scope_user_ids": ["victim"],
            "hub_id": "h",
            "user_id": "x",
            "search_type": "image_index",
            "camera_id": "c",
        }
    ) == {"camera_id": "c"}


# ── submit-time scope ────────────────────────────────────────────────────────


async def test_user_is_scoped_to_self():
    assert await main._resolve_search_scope(_ctx("u-1"), None) == {sc.SCOPE_KEY: ["u-1"]}


async def test_guest_is_scoped_to_the_inviter():
    ctx = _ctx("guest-1", role="guest", created_by="owner-1")
    assert await main._resolve_search_scope(ctx, None) == {sc.SCOPE_KEY: ["owner-1"]}


async def test_admin_without_hub_is_unscoped():
    assert await main._resolve_search_scope(_ctx("a", role="admin"), None) == {}


async def test_missing_user_is_401():
    with pytest.raises(HTTPException) as e:
        await main._resolve_search_scope(_ctx(""), None)
    assert e.value.status_code == 401


async def test_hub_member_gets_every_member(monkeypatch):
    hub = uuid.uuid4()

    async def members(hub_id):
        assert hub_id == str(hub)
        return ["u-1", "u-2", "u-3"]

    monkeypatch.setattr(main, "get_hub_user_ids", members)
    scope = await main._resolve_search_scope(_ctx("u-1"), hub)
    assert scope == {sc.SCOPE_KEY: ["u-1", "u-2", "u-3"], sc.HUB_KEY: str(hub)}


async def test_guest_of_a_member_may_search_the_hub(monkeypatch):
    async def members(_):
        return ["owner-1", "u-2"]

    monkeypatch.setattr(main, "get_hub_user_ids", members)
    ctx = _ctx("guest-1", role="guest", created_by="owner-1")
    scope = await main._resolve_search_scope(ctx, uuid.uuid4())
    assert scope[sc.SCOPE_KEY] == ["owner-1", "u-2"]


async def test_non_member_is_403(monkeypatch):
    async def members(_):
        return ["u-2"]

    monkeypatch.setattr(main, "get_hub_user_ids", members)
    with pytest.raises(HTTPException) as e:
        await main._resolve_search_scope(_ctx("u-1"), uuid.uuid4())
    assert e.value.status_code == 403


async def test_member_less_hub_matches_nothing(monkeypatch):
    async def members(_):
        return []

    monkeypatch.setattr(main, "get_hub_user_ids", members)
    scope = await main._resolve_search_scope(_ctx("a", role="admin"), uuid.uuid4())
    assert sc.build_search_filter(scope) is sc.MATCH_NOTHING


@pytest.mark.parametrize(
    ("exc", "status"),
    [(identity_client.HubNotFound("x"), 404), (identity_client.IdentityUnavailable("x"), 503)],
)
async def test_identity_failures_fail_loud(monkeypatch, exc, status):
    async def members(_):
        raise exc

    monkeypatch.setattr(main, "get_hub_user_ids", members)
    with pytest.raises(HTTPException) as e:
        await main._resolve_search_scope(_ctx("u-1"), uuid.uuid4())
    assert e.value.status_code == status


async def test_hub_scope_disabled_is_503(monkeypatch):
    monkeypatch.setattr(main.settings, "hub_scope_enabled", False)
    with pytest.raises(HTTPException) as e:
        await main._resolve_search_scope(_ctx("u-1"), uuid.uuid4())
    assert e.value.status_code == 503


# ── read routes ──────────────────────────────────────────────────────────────


def test_only_the_searcher_or_admin_can_read_a_search():
    row = SimpleNamespace(user_id="u-1")
    assert main._can_read_search(_ctx("u-1"), row)
    assert not main._can_read_search(_ctx("u-2"), row)
    assert not main._can_read_search(_ctx(""), SimpleNamespace(user_id=""))
    assert main._can_read_search(_ctx("ops", role="root"), row)


async def test_listing_someone_elses_searches_is_403():
    with pytest.raises(HTTPException) as e:
        await main.list_user_searches(user_id="u-2", limit=20, offset=0, ctx=_ctx("u-1"))
    assert e.value.status_code == 403


# ── the paths that hit Qdrant ────────────────────────────────────────────────


class _FakeVectors:
    def __init__(self, vector=None):
        self.filters: list = []
        self._vector = vector or [0.1] * 512

    async def search_similar(self, *, query_vector, limit, threshold, filter_conditions):
        self.filters.append(filter_conditions)
        return []

    async def store_query_vector(self, **_):
        return True

    async def retrieve_query_vector(self, _point_id):
        return self._vector


@pytest.fixture
async def db():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM search_requests LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("no migrated Postgres at DATABASE_URL")
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM search_requests WHERE user_id LIKE 'scope-test-%'"))
    await engine.dispose()


async def test_live_search_applies_the_scope(db, monkeypatch):
    fake = _FakeVectors()
    monkeypatch.setattr(consumer, "_vector_repo", fake)
    payload = {
        "search_id": str(uuid.uuid4()),
        "user_id": "scope-test-u1",
        "vector": [0.1] * 512,
        "threshold": 0.5,
        "max_results": 5,
        "metadata": {sc.SCOPE_KEY: ["owner-a"]},
    }
    await consumer._process_search_result(payload, "1-0")
    assert fake.filters == [{"user_id": ["owner-a"], "source_type": "evidence"}]


async def test_live_search_with_empty_scope_never_queries(db, monkeypatch):
    fake = _FakeVectors()
    monkeypatch.setattr(consumer, "_vector_repo", fake)
    payload = {
        "search_id": str(uuid.uuid4()),
        "user_id": "scope-test-u1",
        "vector": [0.1] * 512,
        "metadata": {sc.SCOPE_KEY: []},
    }
    await consumer._process_search_result(payload, "1-0")
    assert fake.filters == []


async def test_hourly_recalculation_applies_the_scope(db, monkeypatch):
    from src.db.models.constants import SearchRequestStatus, SimilarityStatus
    from src.db.models.search_request import SearchRequest

    async with AsyncSessionLocal() as s:
        s.add(
            SearchRequest(
                search_id=str(uuid.uuid4()),
                user_id="scope-test-u2",
                image_url="https://x/q.jpg",
                status=SearchRequestStatus.COMPLETED,
                similarity_status=SimilarityStatus.MATCHES_FOUND,
                threshold=0.5,
                max_results=5,
                search_metadata={sc.SCOPE_KEY: ["owner-z"]},
                qdrant_query_point_id=str(uuid.uuid4()),
                processing_completed_at=datetime.utcnow() - timedelta(hours=5),
            )
        )
        await s.commit()

    fake = _FakeVectors(vector=list(np.full(512, 0.1)))
    monkeypatch.setattr(safety_nets, "_vector_repo", fake)

    async def _no_image_index():
        return None

    monkeypatch.setattr(safety_nets, "_recalculate_image_index_searches", _no_image_index)
    await safety_nets.recalculate_searches()

    assert {"user_id": ["owner-z"], "source_type": "evidence"} in fake.filters
    # No unscoped query ever ran for a scoped search.
    assert all("user_id" in f for f in fake.filters)
