"""Blacklist image models — profile / reference image / embedding spine.

Modeled on the face-blacklist pattern in `deepface-restapi` but adapted for
CLIP semantic matching. See docs/image-blacklist/02_DATABASE.md for the
schema rationale and docs/image-blacklist/00_CONTEXT.md for the broader
feature design.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from ..base import Base


class BlacklistImageEntry(Base):
    """The profile — one entry per "thing we're watching for" (vehicle,
    object, scene, infraction pattern). Each entry can have multiple
    reference images and therefore multiple Qdrant points.
    """

    __tablename__ = "blacklist_image_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    # Free-form — no enum. Producer decides vocabulary.
    category = Column(Text, nullable=True, index=True)
    description = Column(Text, nullable=True)

    # See BlacklistEntryStatus in constants.py
    status = Column(Integer, nullable=False, default=1, index=True)
    active = Column(Boolean, nullable=False, default=True, index=True)
    is_archived = Column(Boolean, nullable=False, default=False)

    # Multi-tenant isolation. Cross-tenant blacklists are explicitly out of
    # scope for v1 (see docs/image-blacklist/README.md out-of-scope list).
    user_id = Column(String(255), nullable=False, index=True)

    # Bumped on matching-relevant edits so receivers can dedup on
    # (evidence_id, entry_id, version). See docs/image-blacklist/06_CRUD_API.md.
    blacklist_version = Column(Integer, nullable=False, default=1)

    # Per-entry threshold override. Nullable — v1 always falls back to the
    # global BLACKLIST_MATCH_THRESHOLD env var. Column exists so Phase 05
    # code has something to read when the override is wired in a follow-up.
    match_threshold = Column(Float, nullable=True)

    # Free-form metadata (notes, case IDs, tags, etc.)
    json_data = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    references = relationship(
        "BlacklistImageReference",
        back_populates="entry",
        cascade="all, delete-orphan",
    )
    embeddings = relationship(
        "BlacklistImageEmbedding",
        back_populates="entry",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<BlacklistImageEntry {self.id} name={self.name!r} "
            f"user_id={self.user_id} status={self.status} v{self.blacklist_version}>"
        )


class BlacklistImageReference(Base):
    """One reference image attached to a blacklist entry. Each reference
    produces exactly one Qdrant point (one CLIP vector) once embedded.
    """

    __tablename__ = "blacklist_image_references"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_url = Column(Text, nullable=False)
    image_type = Column(String(50), default="reference")  # Reserved for future variants

    # See BlacklistReferenceStatus in constants.py
    status = Column(Integer, nullable=False, default=1)

    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # Dimensions, thumbnail, quality metrics captured at embed time
    json_data = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    entry = relationship("BlacklistImageEntry", back_populates="references")
    embeddings = relationship(
        "BlacklistImageEmbedding",
        back_populates="reference",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # A reference image can only appear once per entry. Same URL may
        # legitimately be referenced across different entries.
        UniqueConstraint("entry_id", "image_url", name="uq_entry_image"),
    )

    def __repr__(self) -> str:
        return f"<BlacklistImageReference {self.id} entry_id={self.entry_id} status={self.status}>"


class BlacklistImageEmbedding(Base):
    """One Qdrant point per embedded reference. Links the DB back to Qdrant
    via `qdrant_point_id` so deletions can clean both sides.
    """

    __tablename__ = "blacklist_image_embeddings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reference_id = Column(
        UUID(as_uuid=True),
        ForeignKey("blacklist_image_references.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Unique — catches accidental re-embedding of the same reference.
    qdrant_point_id = Column(String(255), unique=True, nullable=False, index=True)
    # CLIP variant identifier (e.g. "clip-vit-b-32"). Future model upgrades
    # will filter on this to know which rows need re-embedding.
    model_version = Column(String(50), nullable=False)

    # Crop region used, quality metrics, etc. Not used by filter logic.
    json_data = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    entry = relationship("BlacklistImageEntry", back_populates="embeddings")
    reference = relationship("BlacklistImageReference", back_populates="embeddings")

    def __repr__(self) -> str:
        return (
            f"<BlacklistImageEmbedding {self.id} entry_id={self.entry_id} "
            f"point={self.qdrant_point_id}>"
        )
