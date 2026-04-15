"""EmbeddingRequest model — tracks each evidence through the embedding pipeline."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from ..base import Base


class EmbeddingRequest(Base):
    __tablename__ = "embedding_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evidence_id = Column(String(255), nullable=False, index=True)
    camera_id = Column(String(255), nullable=False, index=True)
    status = Column(Integer, nullable=False, default=1, index=True)
    image_urls = Column(JSONB, default=[])

    # ETL fields (multi-tenant + infraction metadata)
    user_id = Column(String(255), nullable=True, index=True)
    device_id = Column(String(255), nullable=True)
    app_id = Column(Integer, nullable=True)
    infraction_code = Column(String(255), nullable=True, index=True)

    # Weapons enrichment — see docs/weapons/01_DATABASE.md
    weapon_analyzed = Column(Boolean, nullable=False, default=False, index=True)
    has_weapon = Column(Boolean, nullable=False, default=False, index=True)
    weapon_classes = Column(JSONB, nullable=False, default=list)
    weapon_max_confidence = Column(Float, nullable=True)
    weapon_summary = Column(JSONB, nullable=True)
    # Failure reason when compute-weapons attempted analysis but failed.
    # Distinguishes "attempted-and-failed" from "never attempted". See CONTRACT.md §5.
    weapon_analysis_error = Column(Text, nullable=True)

    # Worker tracking
    worker_id = Column(String(100))
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)
    processing_started_at = Column(DateTime)
    processing_completed_at = Column(DateTime)

    # Stream metadata
    stream_message_id = Column(String(100))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    embeddings = relationship(
        "EvidenceEmbeddingRecord",
        back_populates="request",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<EmbeddingRequest {self.id} evidence={self.evidence_id} status={self.status}>"
