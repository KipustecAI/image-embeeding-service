"""SearchRequest model — tracks each similarity search through the pipeline."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Float, Integer, String, Text
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

    # Discriminator (image-index SEARCH — 02_SEARCH_DESIGN §1/§4). Backfills to
    # 'evidence' via server_default; every evidence caller stays byte-identical.
    search_type = Column(
        String(32), nullable=False, server_default="evidence", index=True
    )
    # Capability-A query external_ids list — NULL for every evidence row (§4, M4).
    external_ids = Column(JSONB, nullable=True)

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

    __table_args__ = (
        CheckConstraint(
            "search_type IN ('evidence','image_index')",
            name="ck_search_requests_search_type",
        ),
    )

    def __repr__(self):
        return f"<SearchRequest {self.id} search={self.search_id} status={self.status}>"
