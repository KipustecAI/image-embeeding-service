"""Pydantic schemas for the image-blacklist REST surface.

Lives under ``src/api/v1/schemas`` so the request/response contracts are
co-located with the router that consumes them. Domain types
(``BlacklistImageEntry``, ``BlacklistImageReference``) are kept out of
the API layer — these schemas are the wire shape and convert at the
router boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, HttpUrl

# ── Requests ───────────────────────────────────────────────────────────────


class CreateEntryRequest(BaseModel):
    """Body of POST /api/v1/blacklist/image-entries."""

    name: str = Field(..., min_length=1, max_length=255)
    category: str | None = None
    description: str | None = None
    match_threshold: float | None = Field(None, ge=0.0, le=1.0)
    json_data: dict[str, Any] | None = None


class PatchEntryRequest(BaseModel):
    """Body of PATCH /api/v1/blacklist/image-entries/{id}.

    Every field is optional — partial update semantics. ``user_id`` is
    deliberately not exposed (cross-tenant reassignment is forbidden).
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    category: str | None = None
    description: str | None = None
    active: bool | None = None
    match_threshold: float | None = Field(None, ge=0.0, le=1.0)
    json_data: dict[str, Any] | None = None


class AddReferenceRequest(BaseModel):
    """Body of POST /api/v1/blacklist/image-entries/{id}/references.

    ``image_url`` is validated as a real http/https URL so a copy-paste
    of a local ``file://`` path doesn't silently end up in the embed
    queue (the GPU side has no way to fetch it).
    """

    image_url: HttpUrl
    image_type: str = "reference"


# ── Responses ─────────────────────────────────────────────────────────────


class EntryResponse(BaseModel):
    """Returned from create / get / update / list (rows) endpoints."""

    id: str
    name: str
    category: str | None
    description: str | None
    status: int
    active: bool
    blacklist_version: int
    match_threshold: float | None
    references_count: int
    embeddings_count: int
    user_id: str
    created_at: datetime
    updated_at: datetime


class ReferenceResponse(BaseModel):
    """One reference row as embedded in the entry-detail response."""

    id: str
    entry_id: str
    image_url: str
    image_type: str | None
    status: int
    error_message: str | None
    created_at: datetime


class EntryDetailResponse(EntryResponse):
    """Entry plus its references — returned by GET /entries/{id}."""

    references: list[ReferenceResponse]


class EntryListResponse(BaseModel):
    """Paginated entry list."""

    total: int
    limit: int
    offset: int
    entries: list[EntryResponse]


class AddReferenceResponse(BaseModel):
    """202 Accepted — the embed actually happens via the GPU stream."""

    id: str
    entry_id: str
    image_url: str
    status: int
    created_at: datetime


class BackfillResponse(BaseModel):
    """202 Accepted — reverse search scheduled via APScheduler."""

    message: str
    entry_id: str
    references_count: int
    job_ids: list[str]
