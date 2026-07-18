"""Route tests for the image-index REST query surface (Phase 4).

No live infra — the repo dependency is overridden with an AsyncMock and the
503 gate is overridden to simulate the flag-on state. Covers the load-bearing
query invariants (docs/image-index/00_DESIGN.md §7):

  * 503 on BOTH routes when IMAGE_INDEX_ENABLED is off (no overrides).
  * Cross-tenant / row-missing → 404 per endpoint (repo returns None/[]).
  * Missing X-User-Id → 401.
  * ?all=true → bounded list envelope {external_id, count, batches[]}, newest-first.
  * 200 shape — 4-key counts read from the denormalized columns, no `matched`.
  * include_items toggles the item rows; single-batch happy path.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import src.api.v1.routers.image_index as router_mod
from src.db.models.constants import ImageIndexBatchStatus, ImageIndexResultStatus
from src.main import app

TENANT = "tenant-aaa"
OTHER = "tenant-bbb"
BATCH_ID = "49c7861d-0000-0000-0000-000000000000"
HEADERS = {"X-User-Id": TENANT, "X-User-Role": "user"}


# ── Fakes ────────────────────────────────────────────────────────────────────


def _fake_batch(**overrides) -> SimpleNamespace:
    batch = SimpleNamespace(
        id=BATCH_ID,
        external_id="run-42",
        client_batch_ref="dwoff-run42-image-vehiculo-b0",
        user_id=TENANT,
        status=ImageIndexBatchStatus.COMPLETED_WITH_ERRORS,
        submitted_count=3,
        embedded_count=2,
        filtered_count=0,
        failed_count=1,
        source_ref="run-42/vehicle",
        error_message=None,
        created_at=datetime(2026, 7, 17, 12, 0, 0),
        completed_at=datetime(2026, 7, 17, 12, 5, 0),
    )
    for k, v in overrides.items():
        setattr(batch, k, v)
    return batch


def _fake_item(**overrides) -> SimpleNamespace:
    item = SimpleNamespace(
        item_ref="crop-000",
        source_url="https://cdn/a.jpg",
        item_index=0,
        status=ImageIndexResultStatus.EMBEDDED,
        qdrant_point_id="16d1a741-0000-0000-0000-000000000000",
        duplicate_of_index=None,
        error_message=None,
    )
    for k, v in overrides.items():
        setattr(item, k, v)
    return item


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_repo():
    return AsyncMock()


# NOTE: TestClient is constructed WITHOUT the `with` context manager on purpose —
# entering it would run the app lifespan (Postgres/Qdrant/Redis startup), which is
# not available here. Bare construction still routes + honors dependency_overrides.


@pytest.fixture
def client_enabled(mock_repo):
    """Flag-on client: 503 gate bypassed + repo dependency overridden."""
    app.dependency_overrides[router_mod.require_image_index_enabled] = lambda: None
    app.dependency_overrides[router_mod.get_image_index_repo] = lambda: mock_repo
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_raw():
    """No overrides — real 503 gate (flag defaults off)."""
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── 503 when flag off ────────────────────────────────────────────────────────


def test_get_batch_503_when_flag_off(client_raw):
    r = client_raw.get(f"/api/v1/image-index/results/{BATCH_ID}", headers=HEADERS)
    assert r.status_code == 503


def test_by_external_id_503_when_flag_off(client_raw):
    r = client_raw.get(
        "/api/v1/image-index/results/by-external-id/run-42", headers=HEADERS
    )
    assert r.status_code == 503


def test_503_also_when_repo_unavailable(monkeypatch):
    """Flag on but repo wired unavailable → still 503."""
    monkeypatch.setattr(
        router_mod, "get_settings", lambda: SimpleNamespace(image_index_enabled=True)
    )
    monkeypatch.setattr(router_mod, "_repo_available", False)
    c = TestClient(app)
    r = c.get(f"/api/v1/image-index/results/{BATCH_ID}", headers=HEADERS)
    assert r.status_code == 503


# ── 401 missing header ───────────────────────────────────────────────────────


def test_get_batch_401_missing_user(client_enabled):
    r = client_enabled.get(f"/api/v1/image-index/results/{BATCH_ID}")
    assert r.status_code == 401


def test_by_external_id_401_missing_user(client_enabled):
    r = client_enabled.get("/api/v1/image-index/results/by-external-id/run-42")
    assert r.status_code == 401


# ── 404 cross-tenant / row-missing ───────────────────────────────────────────


def test_get_batch_404_cross_tenant(client_enabled, mock_repo):
    # Repo enforces IDOR — a cross-tenant id resolves to None.
    mock_repo.get_batch.return_value = None
    r = client_enabled.get(
        f"/api/v1/image-index/results/{BATCH_ID}",
        headers={"X-User-Id": OTHER},
    )
    assert r.status_code == 404
    # user_id ALWAYS in the WHERE — the requesting tenant was passed through.
    _, kwargs = mock_repo.get_batch.call_args
    assert kwargs["user_id"] == OTHER


def test_by_external_id_single_404(client_enabled, mock_repo):
    mock_repo.get_latest_by_external_id.return_value = None
    r = client_enabled.get(
        "/api/v1/image-index/results/by-external-id/run-42", headers=HEADERS
    )
    assert r.status_code == 404


def test_by_external_id_all_404_when_empty(client_enabled, mock_repo):
    mock_repo.list_batches_by_external_id.return_value = []
    r = client_enabled.get(
        "/api/v1/image-index/results/by-external-id/run-42?all=true", headers=HEADERS
    )
    assert r.status_code == 404


# ── 200 happy path — single batch ────────────────────────────────────────────


def test_get_batch_200_counts_only(client_enabled, mock_repo):
    mock_repo.get_batch.return_value = _fake_batch()
    r = client_enabled.get(
        f"/api/v1/image-index/results/{BATCH_ID}", headers=HEADERS
    )
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == BATCH_ID
    assert body["status"] == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS
    # 4-key folded counts read from the denormalized columns.
    assert body["counts"] == {
        "submitted": 3,
        "embedded": 2,
        "filtered": 0,
        "failed": 1,
    }
    # No inline-match field in this flow.
    assert "matched" not in body
    assert body["items"] == []
    # counts-only → get_items never touched.
    mock_repo.get_items.assert_not_called()


def test_get_batch_200_with_items(client_enabled, mock_repo):
    mock_repo.get_batch.return_value = _fake_batch()
    mock_repo.get_items.return_value = [
        _fake_item(),
        _fake_item(
            item_ref="crop-002",
            item_index=2,
            status=ImageIndexResultStatus.DOWNLOAD_FAILED,
            qdrant_point_id=None,
            error_message="http 404",
        ),
    ]
    r = client_enabled.get(
        f"/api/v1/image-index/results/{BATCH_ID}?include_items=true&limit=50&offset=0",
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["items"][1]["status"] == "download_failed"
    assert body["items"][1]["qdrant_point_id"] is None
    _, kwargs = mock_repo.get_items.call_args
    assert kwargs["limit"] == 50 and kwargs["offset"] == 0


# ── by-external-id single (default, newest) ──────────────────────────────────


def test_by_external_id_single_200(client_enabled, mock_repo):
    mock_repo.get_latest_by_external_id.return_value = _fake_batch()
    r = client_enabled.get(
        "/api/v1/image-index/results/by-external-id/run-42", headers=HEADERS
    )
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == BATCH_ID
    assert "batches" not in body  # single-batch shape, not the list envelope


# ── ?all=true list envelope + bound ──────────────────────────────────────────


def test_by_external_id_all_list_envelope(client_enabled, mock_repo):
    newest = _fake_batch(id="a" * 8, created_at=datetime(2026, 7, 17, 13, 0, 0))
    older = _fake_batch(id="b" * 8, created_at=datetime(2026, 7, 17, 12, 0, 0))
    # Repo returns newest-first (ORDER BY created_at DESC).
    mock_repo.list_batches_by_external_id.return_value = [newest, older]
    r = client_enabled.get(
        "/api/v1/image-index/results/by-external-id/run-42?all=true", headers=HEADERS
    )
    assert r.status_code == 200
    body = r.json()
    assert body["external_id"] == "run-42"
    assert body["count"] == 2
    assert [b["batch_id"] for b in body["batches"]] == ["a" * 8, "b" * 8]
    # Items always empty in the list mode.
    assert all(b["items"] == [] for b in body["batches"])


def test_by_external_id_all_is_bounded(client_enabled, mock_repo):
    """?all=true is bounded — the repo caps at le=200 (00_DESIGN §7, S4)."""
    from src.db.repositories.image_index_repo import LIST_BY_EXTERNAL_ID_CAP

    assert LIST_BY_EXTERNAL_ID_CAP == 200
    capped = [_fake_batch(id=f"{i:08d}") for i in range(200)]
    mock_repo.list_batches_by_external_id.return_value = capped
    r = client_enabled.get(
        "/api/v1/image-index/results/by-external-id/run-42?all=true", headers=HEADERS
    )
    assert r.status_code == 200
    assert r.json()["count"] == 200


# ── 422 on bad paging ────────────────────────────────────────────────────────


def test_get_batch_422_limit_over_cap(client_enabled, mock_repo):
    mock_repo.get_batch.return_value = _fake_batch()
    r = client_enabled.get(
        f"/api/v1/image-index/results/{BATCH_ID}?limit=501", headers=HEADERS
    )
    assert r.status_code == 422
