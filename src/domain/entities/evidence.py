"""Evidence domain entity."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID
import json


@dataclass
class Evidence:
    """Evidence entity representing camera evidence data."""
    
    id: UUID
    camera_id: UUID
    status: int  # 1=TO_WORK, 2=IN_PROGRESS, 3=FOUND, 4=EMBEDDED
    created_at: datetime
    json_data: Optional[Dict[str, Any]] = None  # Contains the actual evidence data
    updated_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None
    embedding_ids: Optional[List[str]] = None  # Multiple Qdrant point IDs for each image
    
    @property
    def is_ready_for_embedding(self) -> bool:
        """Check if evidence is ready for embedding (status=3)."""
        return self.status == 3
    
    @property
    def is_embedded(self) -> bool:
        """Check if evidence has been embedded (status=4)."""
        return self.status == 4
    
    def get_summary(self) -> str:
        """Extract summary from json_data."""
        if not self.json_data:
            return ""
        return self.json_data.get('summary', "")
    
    def get_image_urls(self) -> List[str]:
        """Extract image URLs from json_data."""
        if not self.json_data:
            return []
        
        image_urls = []
        
        # Handle string json_data (if it comes as string from DB)
        if isinstance(self.json_data, str):
            try:
                data = json.loads(self.json_data)
            except json.JSONDecodeError:
                return []
        else:
            data = self.json_data
        
        # Extract crop_evidence_urls from json_data
        if isinstance(data, dict):
            crop_urls = data.get('crop_evidence_urls', [])
            if isinstance(crop_urls, list):
                image_urls.extend(crop_urls)
        
        return image_urls
    
    def mark_as_embedded(self, embedding_ids: List[str]) -> None:
        """Mark evidence as embedded with multiple embedding IDs."""
        self.status = 4
        self.embedding_ids = embedding_ids
        self.processed_at = datetime.utcnow()