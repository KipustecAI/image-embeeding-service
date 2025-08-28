"""Search result entity."""

from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from uuid import UUID


@dataclass
class SearchResult:
    """Represents a search result from vector database."""
    
    evidence_id: UUID
    camera_id: UUID
    similarity_score: float
    image_url: str
    created_at: datetime
    metadata: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "evidence_id": str(self.evidence_id),
            "camera_id": str(self.camera_id),
            "similarity_score": self.similarity_score,
            "image_url": self.image_url,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata or {}
        }