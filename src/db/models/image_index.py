"""On-demand image-index models — batch job + per-item disposition spine.

Two dedicated tables for the additive / isolated / gated-OFF image-index
feature. Mirrors the `blacklist_image.py` idioms but uses TEXT status columns
(CheckConstraint) so the vocabulary lines up 1:1 with the coordinator
lifecycle. See docs/image-index/00_DESIGN.md §3 for the schema rationale.

Landmine: `metadata` is a reserved attribute on SQLAlchemy's DeclarativeBase
(`Base.metadata` is the MetaData registry). The physical column stays
`metadata` (per locked spec) but is mapped through the aliased Python
attribute `batch_metadata = Column("metadata", JSONB)`. Declaring
`metadata = Column(...)` raises InvalidRequestError at import time.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from ..base import Base

# Batch status vocabulary — kept in sync with ImageIndexBatchStatus in constants.py.
_BATCH_STATUS_CK = "status IN ('pending','computing','completed','completed_with_errors','error')"
# Result status vocabulary — kept in sync with ImageIndexResultStatus in constants.py.
_RESULT_STATUS_CK = "status IN ('embedded','download_failed','decode_failed','filtered','no_result')"


class ImageIndexBatch(Base):
    """The job — one row per coordinator submit (idempotent on client_batch_ref).

    Counts are the single 4-key folded shape {submitted, embedded, filtered,
    failed} where failed folds download_failed + decode_failed + no_result.
    They are recomputed ABSOLUTELY (GROUP BY over the result rows), never
    incremented. See docs/image-index/00_DESIGN.md §3.
    """

    __tablename__ = "image_index_batches"

    # The batch_id we mint and echo to compute + the coordinator lifecycle.
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Coordinator run id — NON-unique; groups re-runs; the recover-by key.
    external_id = Column(String(255), nullable=True, index=True)
    # Idempotency anchor — UNIQUE. Postgres allows multiple NULLs so a ref-less
    # submit is never blocked.
    client_batch_ref = Column(String(255), nullable=True, unique=True)
    # Tenant (Redis-trusted).
    user_id = Column(String(255), nullable=False, index=True)

    # See ImageIndexBatchStatus in constants.py. CheckConstraint below.
    status = Column(
        String(32), nullable=False, default="pending", server_default="pending", index=True
    )

    # 4-key folded counts (absolute; recomputed by GROUP BY).
    submitted_count = Column(Integer, nullable=False, default=0, server_default="0")
    embedded_count = Column(Integer, nullable=False, default=0, server_default="0")
    filtered_count = Column(Integer, nullable=False, default=0, server_default="0")
    failed_count = Column(Integer, nullable=False, default=0, server_default="0")

    # Provenance (e.g. run_id/class_name).
    source_ref = Column(Text, nullable=True)
    # Free-form passthrough. Physical column name "metadata" (reserved attr — aliased).
    batch_metadata = Column("metadata", JSONB, nullable=True)
    # Set only on a batch-level `error`.
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    results = relationship(
        "ImageIndexResult",
        back_populates="batch",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(_BATCH_STATUS_CK, name="ck_image_index_batches_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ImageIndexBatch {self.id} user_id={self.user_id} "
            f"status={self.status} external_id={self.external_id!r}>"
        )


class ImageIndexResult(Base):
    """Per-item disposition + Qdrant reference for a submitted image.

    The UNIQUE(batch_id, item_index) constraint is the land-idempotency key: a
    redelivered result re-upserts the same row rather than double-counting.
    See docs/image-index/00_DESIGN.md §3.
    """

    __tablename__ = "image_index_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("image_index_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Caller's item_id, echoed by compute (join key).
    item_ref = Column(String(255), nullable=False)
    # The dispatched image URL.
    source_url = Column(Text, nullable=True)
    # 0-based submit position; deterministic point-id basis.
    item_index = Column(Integer, nullable=False)

    # See ImageIndexResultStatus in constants.py. CheckConstraint below.
    status = Column(String(32), nullable=False, index=True)

    # Set iff status=='embedded'; deterministic uuid5 (§4).
    qdrant_point_id = Column(String(255), nullable=True)
    # Kept-unique's item_index when status=='filtered'.
    duplicate_of_index = Column(Integer, nullable=True)
    # Short reason on a failed disposition.
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("ImageIndexBatch", back_populates="results")

    __table_args__ = (
        # The real land-idempotency key — a redelivery re-upserts in place.
        UniqueConstraint("batch_id", "item_index", name="uq_image_index_result_item"),
        # Belt-and-suspenders; deterministic point-id + unique item_index already
        # collapse redelivery, so this only fires on the same redelivery.
        UniqueConstraint("qdrant_point_id", name="uq_image_index_result_point"),
        CheckConstraint(_RESULT_STATUS_CK, name="ck_image_index_results_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ImageIndexResult {self.id} batch_id={self.batch_id} "
            f"item_index={self.item_index} status={self.status}>"
        )
