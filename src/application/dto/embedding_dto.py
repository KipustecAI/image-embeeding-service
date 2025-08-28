"""Data Transfer Objects for embedding operations."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict
from uuid import UUID


@dataclass
class EvidenceEmbeddingRequest:
    """Request to embed evidence images."""
    evidence_id: UUID
    image_url: str
    camera_id: UUID
    evidence_type: str
    metadata: Optional[Dict] = None


@dataclass
class EvidenceEmbeddingResponse:
    """Response from evidence embedding."""
    evidence_id: UUID
    success: bool
    embedding_id: Optional[str] = None
    error_message: Optional[str] = None
    vector_dimension: Optional[int] = None
    processed_at: Optional[datetime] = None


@dataclass
class SearchResultDTO:
    """Search result item."""
    evidence_id: str
    image_url: str
    similarity_score: float
    camera_id: Optional[str] = None
    timestamp: Optional[str] = None
    metadata: Dict = field(default_factory=dict)


@dataclass
class ImageSearchRequest:
    """Request to search for similar images."""
    search_id: UUID
    user_id: UUID
    image_url: str
    threshold: float = 0.75
    max_results: int = 50
    metadata: Optional[Dict] = None


@dataclass
class ImageSearchResponse:
    """Response from image search."""
    search_id: UUID
    success: bool
    results: List[SearchResultDTO] = field(default_factory=list)
    total_matches: int = 0
    search_time_ms: Optional[float] = None
    error_message: Optional[str] = None
    processed_at: Optional[datetime] = None


@dataclass
class BatchEmbeddingResult:
    """Result of batch embedding operation."""
    total_processed: int
    successful: int
    failed: int
    processing_time_ms: float
    errors: List[Dict] = field(default_factory=list)
    embedded_ids: List[str] = field(default_factory=list)