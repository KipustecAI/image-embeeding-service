"""REST router for the image-blacklist CRUD surface.

The router stays thin: gateway-header auth via the existing
``UserContext`` dependency, Pydantic in/out, error translation. All
business logic lives in ``ManageBlacklistImageUseCase``.

Multi-tenant scoping is enforced by the use case (we pass through
``ctx.owner_id`` and ``ctx.role``); the router doesn't try to be smart
about visibility.

Mounted under ``/api/v1/blacklist`` from ``src/main.py`` lifespan
wiring. See docs/BLACKLIST_API.md for the consumer-facing contract.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ....api.dependencies import UserContext, get_user_context
from ....application.use_cases.manage_blacklist_image import (
    BlacklistDuplicateReference,
    BlacklistEntryNotFound,
    BlacklistReferenceNotFound,
    ManageBlacklistImageUseCase,
)
from ..schemas.blacklist_image import (
    AddReferenceRequest,
    AddReferenceResponse,
    BackfillResponse,
    CreateEntryRequest,
    EntryDetailResponse,
    EntryListResponse,
    EntryResponse,
    PatchEntryRequest,
    ReferenceResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/blacklist",
    tags=["image-blacklist"],
)


# ── Dependency wiring ─────────────────────────────────────────────────────

# Set by main.py during lifespan startup; tests can replace these via the
# FastAPI dependency-overrides API.
_vector_repo = None
_stream_producer = None


def set_blacklist_router_deps(*, vector_repo, stream_producer) -> None:
    """Wire the singleton Qdrant + Redis instances for this router.

    Called from main.py lifespan after the global instances are
    constructed. Same shape as the other ``set_*`` injection helpers
    used by stream consumers and services.
    """
    global _vector_repo, _stream_producer
    _vector_repo = vector_repo
    _stream_producer = stream_producer


def _build_use_case() -> ManageBlacklistImageUseCase:
    """Construct a fresh use case per request.

    The use case is stateless — same singleton instances are passed in.
    Per-request construction keeps the FastAPI dependency tree obvious
    rather than smuggling globals through closures.
    """
    return ManageBlacklistImageUseCase(
        vector_repo=_vector_repo,
        stream_producer=_stream_producer,
    )


# ── Helpers ───────────────────────────────────────────────────────────────


_ADMIN_ROLES = {"admin", "root", "dev"}


def _is_admin(ctx: UserContext) -> bool:
    return ctx.role in _ADMIN_ROLES


def _require_user(ctx: UserContext) -> str:
    """All endpoints need a tenant id; the gateway should set it."""
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id")
    return ctx.owner_id


def _entry_to_response(
    entry,
    *,
    references_count: int,
    embeddings_count: int,
) -> EntryResponse:
    return EntryResponse(
        id=str(entry.id),
        name=entry.name,
        category=entry.category,
        description=entry.description,
        status=entry.status,
        active=entry.active,
        blacklist_version=entry.blacklist_version,
        match_threshold=entry.match_threshold,
        references_count=references_count,
        embeddings_count=embeddings_count,
        user_id=entry.user_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _reference_to_response(reference) -> ReferenceResponse:
    return ReferenceResponse(
        id=str(reference.id),
        entry_id=str(reference.entry_id),
        image_url=reference.image_url,
        image_type=reference.image_type,
        status=reference.status,
        error_message=reference.error_message,
        created_at=reference.created_at,
    )


# ── Entry endpoints ───────────────────────────────────────────────────────


@router.post(
    "/image-entries",
    response_model=EntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_entry(
    body: CreateEntryRequest,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
) -> EntryResponse:
    user_id = _require_user(ctx)
    entry = await use_case.create_entry(
        user_id=user_id,
        name=body.name,
        category=body.category,
        description=body.description,
        match_threshold=body.match_threshold,
        json_data=body.json_data,
    )
    return _entry_to_response(entry, references_count=0, embeddings_count=0)


@router.get("/image-entries", response_model=EntryListResponse)
async def list_entries(
    active: str = Query(
        "true",
        regex="^(true|false|all)$",
        description="`true` (default) = only active; `false` = only inactive; `all` = both",
    ),
    category: str | None = Query(None),
    user_id: str | None = Query(
        None,
        description="Admin-only filter to a specific tenant. Ignored for non-admin callers.",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
) -> EntryListResponse:
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)

    active_flag: bool | None
    if active == "true":
        active_flag = True
    elif active == "false":
        active_flag = False
    else:  # "all"
        active_flag = None

    entries, total, ref_counts, emb_counts = await use_case.list_entries(
        requester_user_id=requester,
        is_admin=is_admin,
        target_user_id=user_id if is_admin else None,
        active=active_flag,
        category=category,
        limit=limit,
        offset=offset,
    )

    return EntryListResponse(
        total=total,
        limit=limit,
        offset=offset,
        entries=[
            _entry_to_response(
                e,
                references_count=ref_counts.get(e.id, 0),
                embeddings_count=emb_counts.get(e.id, 0),
            )
            for e in entries
        ],
    )


@router.get("/image-entries/{entry_id}", response_model=EntryDetailResponse)
async def get_entry(
    entry_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
) -> EntryDetailResponse:
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)
    try:
        entry, references, embeddings_count = await use_case.get_entry_detail(
            entry_id=entry_id,
            requester_user_id=requester,
            is_admin=is_admin,
        )
    except BlacklistEntryNotFound as e:
        raise HTTPException(status_code=404, detail="Entry not found") from e

    base = _entry_to_response(
        entry,
        references_count=len(references),
        embeddings_count=embeddings_count,
    )
    return EntryDetailResponse(
        **base.model_dump(),
        references=[_reference_to_response(r) for r in references],
    )


@router.patch("/image-entries/{entry_id}", response_model=EntryResponse)
async def update_entry(
    entry_id: UUID,
    body: PatchEntryRequest,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
) -> EntryResponse:
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)
    # exclude_unset=True so the use case can distinguish "not sent" from
    # "sent as null" — the latter clears optional fields explicitly.
    patch = body.model_dump(exclude_unset=True)
    try:
        entry = await use_case.update_entry(
            entry_id=entry_id,
            requester_user_id=requester,
            is_admin=is_admin,
            patch=patch,
        )
    except BlacklistEntryNotFound as e:
        raise HTTPException(status_code=404, detail="Entry not found") from e
    # Counts on the returned entry are an approximation — we don't
    # re-query for them on update. Caller can GET the detail endpoint
    # if they need fresh counts.
    return _entry_to_response(entry, references_count=0, embeddings_count=0)


@router.delete(
    "/image-entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_entry(
    entry_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
):
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)
    try:
        await use_case.delete_entry(
            entry_id=entry_id,
            requester_user_id=requester,
            is_admin=is_admin,
        )
    except BlacklistEntryNotFound as e:
        raise HTTPException(status_code=404, detail="Entry not found") from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Reference endpoints ───────────────────────────────────────────────────


@router.post(
    "/image-entries/{entry_id}/references",
    response_model=AddReferenceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def add_reference(
    entry_id: UUID,
    body: AddReferenceRequest,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
) -> AddReferenceResponse:
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)
    try:
        reference = await use_case.add_reference(
            entry_id=entry_id,
            requester_user_id=requester,
            is_admin=is_admin,
            image_url=str(body.image_url),
            image_type=body.image_type,
        )
    except BlacklistEntryNotFound as e:
        raise HTTPException(status_code=404, detail="Entry not found") from e
    except BlacklistDuplicateReference as e:
        raise HTTPException(
            status_code=409,
            detail="Reference already exists for this entry",
        ) from e

    return AddReferenceResponse(
        id=str(reference.id),
        entry_id=str(reference.entry_id),
        image_url=reference.image_url,
        status=reference.status,
        created_at=reference.created_at,
    )


@router.delete(
    "/image-entries/{entry_id}/references/{reference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_reference(
    entry_id: UUID,
    reference_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
):
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)
    try:
        await use_case.delete_reference(
            entry_id=entry_id,
            reference_id=reference_id,
            requester_user_id=requester,
            is_admin=is_admin,
        )
    except BlacklistEntryNotFound as e:
        raise HTTPException(status_code=404, detail="Entry not found") from e
    except BlacklistReferenceNotFound as e:
        raise HTTPException(status_code=404, detail="Reference not found") from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Operational endpoints ─────────────────────────────────────────────────


@router.post(
    "/image-entries/{entry_id}/backfill",
    response_model=BackfillResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_backfill(
    entry_id: UUID,
    ctx: UserContext = Depends(get_user_context),
    use_case: ManageBlacklistImageUseCase = Depends(_build_use_case),
) -> BackfillResponse:
    """Re-run reverse search for every PROCESSED reference under an entry.

    Use case: ops recovery, or post-threshold-change re-evaluation. Each
    reference fires its own APScheduler job; the response carries the
    list of scheduled job ids for visibility.
    """
    requester = _require_user(ctx)
    is_admin = _is_admin(ctx)
    try:
        references_count, scheduled = await use_case.trigger_backfill(
            entry_id=entry_id,
            requester_user_id=requester,
            is_admin=is_admin,
        )
    except BlacklistEntryNotFound as e:
        raise HTTPException(status_code=404, detail="Entry not found") from e

    return BackfillResponse(
        message="Backfill scheduled",
        entry_id=str(entry_id),
        references_count=references_count,
        job_ids=scheduled,
    )
