"""REST query surface for the on-demand image-index feature (read-only).

Leg 3 of the playbook (docs/image-index/00_DESIGN.md §7). Two GET endpoints
under ``/api/v1/image-index`` that let the coordinator/frontend reconcile a
batch by ``batch_id`` or recover every run under a (non-unique) ``external_id``.

Design invariants enforced here:

* **Gated-OFF, mounted unconditionally.** The router is included at module level
  in ``src/main.py`` (like ``blacklist_image_router``), but every route depends
  on ``require_image_index_enabled`` → **503** when ``IMAGE_INDEX_ENABLED`` is
  off or the feature's repo was never wired. A truer no-op-when-off than a
  missing mount (routes 503 rather than the app 404-ing).
* **Strict IDOR — no admin bypass.** Every repo read ANDs ``user_id`` in the
  WHERE (``get_user_context().owner_id``). A tenant miss OR a missing row →
  **404** (never 403, never 200-empty — no existence disclosure). Missing
  ``X-User-Id`` → **401**.
* **Counts are read, never recomputed.** The 4-key folded shape comes straight
  off the denormalized batch columns (recomputed-on-land by the results
  consumer). The query path does zero aggregation.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ....api.dependencies import UserContext, get_user_context
from ....db.models.image_index import ImageIndexBatch, ImageIndexResult
from ....db.repositories.image_index_repo import ImageIndexRepository
from ....infrastructure.config import get_settings
from ....infrastructure.database import get_session
from ....services.image_index_service import ImageIndexService
from ..schemas.image_index import (
    BatchCounts,
    BatchListResponse,
    BatchResultResponse,
    ItemResult,
)

logger = logging.getLogger(__name__)


# ── Feature gate ─────────────────────────────────────────────────────────────

# Set by main.py's gated lifespan block. Defaults True so the read path (which
# only needs Postgres, always up while the app is) works whenever the flag is on
# without extra wiring; a future phase can set it False on an init failure so the
# router 503s in lock-step with the rest of the feature.
_repo_available: bool = True


def set_image_index_router_deps(*, available: bool) -> None:
    """Wire feature availability from main.py (00_DESIGN §7).

    Mirrors the other ``set_*`` injection helpers. Called only from the gated
    lifespan block; with the flag OFF it is never called and the flag check in
    ``require_image_index_enabled`` short-circuits to 503.
    """
    global _repo_available
    _repo_available = available


async def require_image_index_enabled() -> None:
    """503 gate shared by every route (00_DESIGN §7, invariant #9).

    Returns 503 when ``image_index_enabled`` is off OR the feature repo is
    unavailable after an init failure. Read once per request at the router
    boundary — never in a hot loop.
    """
    settings = get_settings()
    if not settings.image_index_enabled:
        raise HTTPException(
            status_code=503, detail="Image-index feature is disabled"
        )
    if not _repo_available:
        raise HTTPException(
            status_code=503, detail="Image-index repository unavailable"
        )


router = APIRouter(
    prefix="/api/v1/image-index",
    tags=["image-index"],
    dependencies=[Depends(require_image_index_enabled)],
)


# ── Dependencies ─────────────────────────────────────────────────────────────


async def get_image_index_repo():
    """Yield a read-only repo bound to a fresh session.

    Overridable in tests via ``app.dependency_overrides`` so the route logic can
    be exercised against a mocked repo with no database.
    """
    async with get_session() as session:
        yield ImageIndexRepository(session)


def _require_user(ctx: UserContext) -> str:
    """All reads need a tenant id; the gateway injects it. Missing → 401."""
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id")
    return ctx.owner_id


# ── Serialization helpers ────────────────────────────────────────────────────


def _item_to_response(item: ImageIndexResult) -> ItemResult:
    return ItemResult(
        item_ref=item.item_ref,
        source_url=item.source_url,
        item_index=item.item_index,
        status=item.status,
        qdrant_point_id=item.qdrant_point_id,
        duplicate_of_index=item.duplicate_of_index,
        error_message=item.error_message,
    )


def _batch_to_response(
    batch: ImageIndexBatch, items: list[ImageIndexResult] | None = None
) -> BatchResultResponse:
    # Counts are read straight off the denormalized columns — never recomputed
    # in the query path (00_DESIGN §7). Reuse the one shared reader.
    counts = ImageIndexService.counts_from_batch(batch)
    return BatchResultResponse(
        batch_id=str(batch.id),
        external_id=batch.external_id,
        client_batch_ref=batch.client_batch_ref,
        status=batch.status,
        counts=BatchCounts(**counts),
        source_ref=batch.source_ref,
        created_at=batch.created_at,
        completed_at=batch.completed_at,
        error_message=batch.error_message,
        items=[_item_to_response(i) for i in (items or [])],
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/results/{batch_id}", response_model=BatchResultResponse)
async def get_batch_results(
    batch_id: UUID,
    include_items: bool = Query(
        False, description="Include per-item disposition rows (default counts-only)."
    ),
    limit: int = Query(100, ge=1, le=500, description="Item page size."),
    offset: int = Query(0, ge=0, description="Item page offset."),
    ctx: UserContext = Depends(get_user_context),
    repo: ImageIndexRepository = Depends(get_image_index_repo),
) -> BatchResultResponse:
    """Reconcile a single batch by its ``batch_id`` (strict tenant scope)."""
    user_id = _require_user(ctx)
    batch = await repo.get_batch(batch_id, user_id=user_id)
    if batch is None:
        # Row-missing and tenant-miss are indistinguishable, by design.
        raise HTTPException(status_code=404, detail="Batch not found")
    items = (
        await repo.get_items(batch.id, limit=limit, offset=offset)
        if include_items
        else []
    )
    return _batch_to_response(batch, items)


@router.get("/results/by-external-id/{external_id}", response_model=None)
async def get_results_by_external_id(
    external_id: str,
    all_runs: bool = Query(
        False,
        alias="all",
        description="Return every run under this external_id (newest-first, bounded le=200).",
    ),
    include_items: bool = Query(
        False,
        description="Include per-item rows (single-run mode only; empty in the ?all list).",
    ),
    limit: int = Query(100, ge=1, le=500, description="Item page size."),
    offset: int = Query(0, ge=0, description="Item page offset."),
    ctx: UserContext = Depends(get_user_context),
    repo: ImageIndexRepository = Depends(get_image_index_repo),
) -> BatchResultResponse | BatchListResponse:
    """Recover by (non-unique) ``external_id``.

    Default → the most-recent run (single-batch shape). ``?all=true`` → every run
    newest-first in a ``{external_id, count, batches[]}`` envelope, bounded at
    ``le=200`` in the repo (items are always empty in the list mode).
    """
    user_id = _require_user(ctx)

    if all_runs:
        batches = await repo.list_batches_by_external_id(external_id, user_id=user_id)
        if not batches:
            raise HTTPException(status_code=404, detail="No runs for external_id")
        return BatchListResponse(
            external_id=external_id,
            count=len(batches),
            batches=[_batch_to_response(b) for b in batches],
        )

    batch = await repo.get_latest_by_external_id(external_id, user_id=user_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="No runs for external_id")
    items = (
        await repo.get_items(batch.id, limit=limit, offset=offset)
        if include_items
        else []
    )
    return _batch_to_response(batch, items)
