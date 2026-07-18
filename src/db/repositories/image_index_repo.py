"""Repository for the on-demand image-index tables.

Atomic idempotent mint + idempotent land + absolute-count reconciliation +
IDOR-scoped reads for the two dedicated tables. All the correctness invariants
(docs/image-index/00_DESIGN.md §8) live in the SQL here:

- ``create_or_get_batch`` — INSERT ... ON CONFLICT (client_batch_ref) DO NOTHING
  RETURNING id. ``created`` derives from the ATOMIC outcome, never a
  check-then-insert, so two concurrent redeliveries yield exactly one batch.
- ``upsert_result`` — INSERT ... ON CONFLICT (batch_id, item_index) DO UPDATE;
  a redelivered result overwrites the same row rather than double-counting.
- ``recompute_counts`` — GROUP BY status → the 4-key folded shape. NEVER += n.
- reads put ``user_id`` in the WHERE ALWAYS (strict IDOR; no admin bypass).
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.constants import ImageIndexBatchStatus, ImageIndexResultStatus
from ..models.image_index import ImageIndexBatch, ImageIndexResult

logger = logging.getLogger(__name__)

# Bounded ?all=true list — matches the bounded get_items paging (00_DESIGN §7).
LIST_BY_EXTERNAL_ID_CAP = 200


class ImageIndexRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── Atomic idempotent mint ────────────────────────────────────────────

    async def create_or_get_batch(
        self,
        *,
        user_id: str,
        client_batch_ref: str,
        external_id: str | None,
        submitted_count: int,
        source_ref: str | None = None,
        batch_metadata: dict | None = None,
        status: str = ImageIndexBatchStatus.PENDING,
    ) -> tuple[ImageIndexBatch, bool]:
        """INSERT ... ON CONFLICT (client_batch_ref) DO NOTHING RETURNING id.

        Returns ``(batch, created)``. ``created`` is True iff a row was actually
        inserted by THIS call (atomic outcome); otherwise the existing row is
        SELECTed and returned with ``created=False``. Two concurrent
        redeliveries → exactly one batch.

        NOTE: relies on client_batch_ref being non-null. A ref-less submit must
        be rejected upstream (it is the idempotency anchor). ``flush`` is left
        to the caller / session context; this does not commit.
        """
        stmt = (
            pg_insert(ImageIndexBatch)
            .values(
                user_id=user_id,
                client_batch_ref=client_batch_ref,
                external_id=external_id,
                submitted_count=submitted_count,
                source_ref=source_ref,
                batch_metadata=batch_metadata,
                status=status,
            )
            .on_conflict_do_nothing(index_elements=["client_batch_ref"])
            .returning(ImageIndexBatch.id)
        )
        result = await self.session.execute(stmt)
        inserted_id = result.scalar()
        if inserted_id is not None:
            batch = await self.session.get(ImageIndexBatch, inserted_id)
            return batch, True
        # Conflict — the row already exists. Fetch it (unscoped: this is the
        # producer path re-binding its own submit, not a tenant read).
        existing = await self.session.execute(
            select(ImageIndexBatch)
            .where(ImageIndexBatch.client_batch_ref == client_batch_ref)
            .limit(1)
        )
        return existing.scalar(), False

    async def set_status(
        self, batch_id: UUID, status: str, *, completed_at: datetime | None = None
    ) -> None:
        """Set a batch's status (e.g. → 'computing' after dispatch)."""
        values: dict = {"status": status, "updated_at": datetime.utcnow()}
        if completed_at is not None:
            values["completed_at"] = completed_at
        await self.session.execute(
            update(ImageIndexBatch).where(ImageIndexBatch.id == batch_id).values(**values)
        )

    # ── Idempotent land ───────────────────────────────────────────────────

    async def upsert_result(
        self,
        *,
        batch_id: UUID,
        item_index: int,
        item_ref: str,
        status: str,
        source_url: str | None = None,
        qdrant_point_id: str | None = None,
        duplicate_of_index: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """INSERT ... ON CONFLICT (batch_id, item_index) DO UPDATE.

        A redelivered result overwrites the same row in place — the composite
        (batch_id, item_index) is the land-idempotency key.
        """
        insert_values = {
            "batch_id": batch_id,
            "item_index": item_index,
            "item_ref": item_ref,
            "status": status,
            "source_url": source_url,
            "qdrant_point_id": qdrant_point_id,
            "duplicate_of_index": duplicate_of_index,
            "error_message": error_message,
        }
        stmt = pg_insert(ImageIndexResult).values(**insert_values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_image_index_result_item",
            set_={
                "item_ref": stmt.excluded.item_ref,
                "status": stmt.excluded.status,
                "source_url": stmt.excluded.source_url,
                "qdrant_point_id": stmt.excluded.qdrant_point_id,
                "duplicate_of_index": stmt.excluded.duplicate_of_index,
                "error_message": stmt.excluded.error_message,
                "updated_at": datetime.utcnow(),
            },
        )
        await self.session.execute(stmt)

    async def recompute_counts(self, batch_id: UUID) -> dict[str, int]:
        """GROUP BY status → the 4-key folded shape. NEVER += n.

        Returns {submitted, embedded, filtered, failed} where ``submitted`` is
        the batch's stored submitted_count and ``failed`` folds
        download_failed + decode_failed + no_result. Writes the three derived
        counters back onto the batch row.
        """
        rows = await self.session.execute(
            select(ImageIndexResult.status, func.count())
            .where(ImageIndexResult.batch_id == batch_id)
            .group_by(ImageIndexResult.status)
        )
        by_status = {status: int(n) for status, n in rows.all()}
        embedded = by_status.get(ImageIndexResultStatus.EMBEDDED, 0)
        filtered = by_status.get(ImageIndexResultStatus.FILTERED, 0)
        failed = sum(by_status.get(s, 0) for s in ImageIndexResultStatus.FAILED_SET)

        batch = await self.session.get(ImageIndexBatch, batch_id)
        submitted = batch.submitted_count if batch is not None else 0
        if batch is not None:
            batch.embedded_count = embedded
            batch.filtered_count = filtered
            batch.failed_count = failed
            batch.updated_at = datetime.utcnow()

        return {
            "submitted": submitted,
            "embedded": embedded,
            "filtered": filtered,
            "failed": failed,
        }

    # ── IDOR-scoped reads (user_id ALWAYS in WHERE) ───────────────────────

    async def get_batch(self, batch_id: UUID, *, user_id: str) -> ImageIndexBatch | None:
        q = select(ImageIndexBatch).where(
            ImageIndexBatch.id == batch_id,
            ImageIndexBatch.user_id == user_id,
        )
        return (await self.session.execute(q.limit(1))).scalar()

    async def get_latest_by_external_id(
        self, external_id: str, *, user_id: str
    ) -> ImageIndexBatch | None:
        q = (
            select(ImageIndexBatch)
            .where(
                ImageIndexBatch.external_id == external_id,
                ImageIndexBatch.user_id == user_id,
            )
            .order_by(ImageIndexBatch.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(q)).scalar()

    async def list_batches_by_external_id(
        self, external_id: str, *, user_id: str, limit: int = LIST_BY_EXTERNAL_ID_CAP
    ) -> list[ImageIndexBatch]:
        """Every run under an external_id, newest-first. Bounded at le=200."""
        capped = min(max(int(limit), 1), LIST_BY_EXTERNAL_ID_CAP)
        q = (
            select(ImageIndexBatch)
            .where(
                ImageIndexBatch.external_id == external_id,
                ImageIndexBatch.user_id == user_id,
            )
            .order_by(ImageIndexBatch.created_at.desc())
            .limit(capped)
        )
        return list((await self.session.execute(q)).scalars().all())

    async def get_items(
        self, batch_pk: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[ImageIndexResult]:
        """Result rows for a batch, ordered by item_index.

        Scoped transitively: ``batch_pk`` came from an already-tenant-checked
        batch (get_batch / get_latest_by_external_id), so no user_id here.
        """
        q = (
            select(ImageIndexResult)
            .where(ImageIndexResult.batch_id == batch_pk)
            .order_by(ImageIndexResult.item_index.asc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.session.execute(q)).scalars().all())

    # ── Reaper support ────────────────────────────────────────────────────

    async def list_stuck_active(self, cutoff: datetime) -> list[ImageIndexBatch]:
        """status IN (pending, computing) AND created_at < cutoff.

        Plain SELECT (SQLite-portable for CI). Switch to FOR UPDATE SKIP LOCKED
        before running >1 API replica (00_DESIGN §5.3).
        """
        q = select(ImageIndexBatch).where(
            ImageIndexBatch.status.in_(
                [ImageIndexBatchStatus.PENDING, ImageIndexBatchStatus.COMPUTING]
            ),
            ImageIndexBatch.created_at < cutoff,
        )
        return list((await self.session.execute(q)).scalars().all())

    async def mark_failed(self, batch: ImageIndexBatch, error_message: str) -> None:
        """Terminalize a stuck batch as 'error' (reaper timeout)."""
        batch.status = ImageIndexBatchStatus.ERROR
        batch.error_message = error_message
        batch.completed_at = datetime.utcnow()
        batch.updated_at = datetime.utcnow()
        await self.session.flush()
