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

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    request = relationship("EmbeddingRequest", back_populates="embeddings")

    def __repr__(self):
        return f"<EvidenceEmbeddingRecord {self.id} point={self.qdrant_point_id}>"
