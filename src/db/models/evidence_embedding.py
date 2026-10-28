"""EvidenceEmbeddingRecord — one row per image embedded in Qdrant."""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from ..base import Base


class EvidenceEmbeddingRecord(Base):
    __tablename__ = "evidence_embeddings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = Column(UUID(as_uuid=True), ForeignKey("embedding_requests.id"), nullable=False)
    qdrant_point_id = Column(String(255), unique=True, index=True)
    image_index = Column(Integer, default=0)
    image_url = Column(Text, nullable=False)
    json_data = Column(JSONB)

    # Per-image weapon detections — see docs/weapons/01_DATABASE.md
    weapon_detections = Column(JSONB, nullable=True)

    # Frame-from-ZIP reference (FRAME_STORAGE_MODE=zip_ref). NULL on rows ingested
    # before the change or in "upload" mode. zip_entry is the RAW path inside the
    # ZIP (e.g. "cropped/a.jpg"); NULL when compute did not send it (older compute),
    # then GET /api/v1/frames matches by basename == image_name.
    zip_url = Column(Text, nullable=True)
    image_name = Column(Text, nullable=True)
    zip_entry = Column(Text, nullable=True)

    # Crop labels from the CLIP worker's classifier (CLS-06). NULL = the
    # classifier did not run; [] = it ran and no class passed its threshold.
    # Sorted by score, highest first: [{"label": "bus", "score": 0.97}, ...].
    labels = Column(JSONB, nullable=True)
    classifier_version = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    request = relationship("EmbeddingRequest", back_populates="embeddings")

    def __repr__(self):
        return f"<EvidenceEmbeddingRecord {self.id} point={self.qdrant_point_id}>"
