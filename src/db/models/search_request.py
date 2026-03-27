"""SearchRequest model — tracks each similarity search through the pipeline."""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from ..base import Base


class SearchRequest(Base):
    __tablename__ = "search_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    search_id = Column(String(255), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    image_url = Column(Text, nullable=False)
    status = Column(Integer, nullable=False, default=1, index=True)
    similarity_status = Column(Integer, default=1)

    # Search parameters
    threshold = Column(Float, default=0.75)
    max_results = Column(Integer, default=50)
    search_metadata = Column(JSONB)

    # Results
    total_matches = Column(Integer, default=0)
    results_key = Column(String(255))
    qdrant_query_point_id = Column(String(255), nullable=True)

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
    matches = relationship(
        "SearchMatch",
        back_populates="search_request",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<SearchRequest {self.id} search={self.search_id} status={self.status}>"
