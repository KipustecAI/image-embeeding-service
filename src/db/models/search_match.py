"""SearchMatch model — individual match result from a similarity search."""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from ..base import Base


class SearchMatch(Base):
    __tablename__ = "search_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    search_request_id = Column(
        UUID(as_uuid=True), ForeignKey("search_requests.id"), nullable=False, index=True
    )
    evidence_id = Column(String(255), nullable=False, index=True)
    camera_id = Column(String(255))
    similarity_score = Column(Float, nullable=False)
    image_url = Column(Text)
    match_metadata = Column(JSONB)
    # Per-match run tag for image-index searches (02_SEARCH_DESIGN §4). NULL for
    # every evidence row; indexed so the frontend groups matches by run (M4).
    external_id = Column(String(255), nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    search_request = relationship("SearchRequest", back_populates="matches")

    def __repr__(self):
        return f"<SearchMatch {self.id} evidence={self.evidence_id} score={self.similarity_score}>"

    def to_dict(self):
        d = {
            "evidence_id": self.evidence_id,
            "camera_id": self.camera_id,
            "similarity_score": self.similarity_score,
            "image_url": self.image_url,
            "metadata": self.match_metadata or {},
        }
        # Image-index extras — only surfaced for image-index rows (02_SEARCH_DESIGN §4).
        # Absent → omitted so evidence rows stay byte-identical.
        if self.external_id is not None:
            meta = self.match_metadata or {}
            d["external_id"] = self.external_id
            d["batch_id"] = meta.get("batch_id")
            d["item_index"] = meta.get("item_index")
        return d
