"""Image Search domain entity."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict
from uuid import UUID


@dataclass
class SearchResult:
    """Result item from similarity search."""
    evidence_id: str
    image_url: str
    similarity_score: float
    metadata: Dict = field(default_factory=dict)
    camera_id: Optional[UUID] = None
    timestamp: Optional[datetime] = None


@dataclass
class ImageSearch:
    """Image search request entity."""
    
    id: UUID
    user_id: UUID
    image_url: str
    search_status: int  # 1=TO_WORK, 2=IN_PROGRESS, 3=COMPLETED, 4=FAILED
    similarity_status: int  # 1=NO_MATCHES, 2=MATCHES_FOUND
    created_at: datetime
    updated_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None
    metadata: Optional[Dict] = None
    results_key: Optional[str] = None  # Redis key
    total_matches: int = 0
    
    # Runtime properties (not persisted)
    results: Optional[List[SearchResult]] = None
    error_message: Optional[str] = None
    
    @property
    def is_pending(self) -> bool:
        """Check if search is pending processing."""
        return self.search_status == 1
    
    @property
    def is_processing(self) -> bool:
        """Check if search is currently processing."""
        return self.search_status == 2
    
    @property
    def is_completed(self) -> bool:
        """Check if search is completed."""
        return self.search_status == 3
    
    @property
    def is_failed(self) -> bool:
        """Check if search failed."""
        return self.search_status == 4
    
    @property
    def has_matches(self) -> bool:
        """Check if search found matches."""
        return self.similarity_status == 2 and self.total_matches > 0
    
    def get_similarity_threshold(self) -> float:
        """Get similarity threshold from metadata or use default."""
        if self.metadata and 'threshold' in self.metadata:
            return float(self.metadata['threshold'])
        return 0.75  # Default threshold
    
    def get_max_results(self) -> int:
        """Get max results from metadata or use default."""
        if self.metadata and 'max_results' in self.metadata:
            return int(self.metadata['max_results'])
        return 50  # Default max results
    
    def mark_as_processing(self) -> None:
        """Mark search as processing."""
        self.search_status = 2
        self.updated_at = datetime.utcnow()
    
    def mark_as_completed(self, results: List[SearchResult]) -> None:
        """Mark search as completed with results."""
        self.search_status = 3
        self.results = results
        self.total_matches = len(results)
        self.similarity_status = 2 if results else 1
        self.processed_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
    
    def mark_as_failed(self, error_message: str) -> None:
        """Mark search as failed."""
        self.search_status = 4
        self.error_message = error_message
        self.updated_at = datetime.utcnow()