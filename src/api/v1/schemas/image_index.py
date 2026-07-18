"""Pydantic schemas for the on-demand image-index REST query surface.

Read-only, face-style. The wire shape mirrors the coordinator lifecycle and
the denormalized DB columns exactly (docs/image-index/00_DESIGN.md §6 / §7):
the SINGLE 4-key folded count vocabulary ``{submitted, embedded, filtered,
failed}`` is returned verbatim from the batch columns — the query surface
NEVER recomputes. There is deliberately NO ``matched`` field (this flow stores
searchable vectors, it does not inline-match).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

# ── Shared count vocabulary ──────────────────────────────────────────────────


class BatchCounts(BaseModel):
    """The one 4-key folded shape. ``failed`` folds download_failed +
    decode_failed + no_result. Read from the denormalized batch columns."""

    submitted: int
    embedded: int
    filtered: int
    failed: int


# ── Per-item disposition ─────────────────────────────────────────────────────


class ItemResult(BaseModel):
    """One result row — present only when ``include_items=true``."""

    item_ref: str
    source_url: str | None
    item_index: int
    status: str
    qdrant_point_id: str | None
    duplicate_of_index: int | None
    error_message: str | None


# ── Single-batch response (also the element of the ?all list) ────────────────


class BatchResultResponse(BaseModel):
    """Face-style single-batch view. ``items`` is empty unless
    ``include_items=true`` (and always empty inside the ?all list envelope)."""

    batch_id: str
    external_id: str | None
    client_batch_ref: str | None
    status: str
    counts: BatchCounts
    source_ref: str | None
    created_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    items: list[ItemResult] = []


# ── ?all=true list envelope ──────────────────────────────────────────────────


class BatchListResponse(BaseModel):
    """``GET /results/by-external-id/{external_id}?all=true`` — every run under a
    (non-unique) external_id, newest-first, bounded at ``le=200`` in the repo."""

    external_id: str
    count: int
    batches: list[BatchResultResponse]
