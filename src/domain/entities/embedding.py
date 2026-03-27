"""Image Embedding domain entity."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import numpy as np


@dataclass
class ImageEmbedding:
    """Image embedding entity for vector storage."""

    id: str  # Qdrant point ID (usually evidence_id or search_id)
    vector: np.ndarray  # CLIP embedding vector (512 dimensions for ViT-B-32)
    metadata: dict
    created_at: datetime

    # Metadata fields for easier access
    source_type: str  # 'evidence' or 'search'
    image_url: str
    camera_id: UUID | None = None
    evidence_id: UUID | None = None
    search_id: UUID | None = None
    user_id: UUID | None = None

    @classmethod
    def from_evidence(
        cls,
        evidence_id: UUID,
        vector: np.ndarray,
        image_url: str,
        camera_id: UUID,
        additional_metadata: dict | None = None,
    ) -> "ImageEmbedding":
        """Create embedding from evidence."""
        metadata = {
            "source_type": "evidence",
            "evidence_id": str(evidence_id),
            "camera_id": str(camera_id),
            "image_url": image_url,
            "created_at": datetime.utcnow().isoformat(),
        }
        if additional_metadata:
            metadata.update(additional_metadata)

        return cls(
            id=str(uuid4()),  # Generate new UUID for Qdrant
            vector=vector,
            metadata=metadata,
            created_at=datetime.utcnow(),
            source_type="evidence",
            image_url=image_url,
            camera_id=camera_id,
            evidence_id=evidence_id,
        )

    @classmethod
    def from_search(
        cls,
        search_id: UUID,
        vector: np.ndarray,
        image_url: str,
        user_id: UUID,
        additional_metadata: dict | None = None,
    ) -> "ImageEmbedding":
        """Create embedding from image search."""
        metadata = {
            "source_type": "search",
            "search_id": str(search_id),
            "user_id": str(user_id),
            "image_url": image_url,
            "created_at": datetime.utcnow().isoformat(),
        }
        if additional_metadata:
            metadata.update(additional_metadata)

        return cls(
            id=str(uuid4()),  # Generate new UUID for Qdrant
            vector=vector,
            metadata=metadata,
            created_at=datetime.utcnow(),
            source_type="search",
            image_url=image_url,
            search_id=search_id,
            user_id=user_id,
        )

    @property
    def vector_dimension(self) -> int:
        """Get vector dimension."""
        return len(self.vector)

    @property
    def is_evidence(self) -> bool:
        """Check if embedding is from evidence."""
        return self.source_type == "evidence"

    @property
    def is_search(self) -> bool:
        """Check if embedding is from search."""
        return self.source_type == "search"

    def to_qdrant_point(self) -> dict:
        """Convert to Qdrant point format."""
        return {"id": self.id, "vector": self.vector.tolist(), "payload": self.metadata}
