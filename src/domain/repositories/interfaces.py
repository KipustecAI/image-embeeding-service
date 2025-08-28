"""Repository interfaces for domain layer."""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from uuid import UUID
import numpy as np

from ..entities import Evidence, ImageSearch, ImageEmbedding, SearchResult


class EvidenceRepository(ABC):
    """Interface for evidence repository."""
    
    @abstractmethod
    async def get_unembedded_evidences(self, limit: int = 50) -> List[Evidence]:
        """Get evidences with status=3 (FOUND) that need embedding."""
        pass
    
    @abstractmethod
    async def mark_evidence_as_embedded(self, evidence_id: UUID, embedding_id: str) -> bool:
        """Update evidence status to 4 (EMBEDDED) with embedding ID."""
        pass


class ImageSearchRepository(ABC):
    """Interface for image search repository."""
    
    @abstractmethod
    async def get_pending_searches(self, limit: int = 10) -> List[ImageSearch]:
        """Get image searches with status=1 (TO_WORK)."""
        pass
    
    @abstractmethod
    async def update_search_status(
        self,
        search_id: UUID,
        search_status: int,
        similarity_status: Optional[int] = None,
        total_matches: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Update search processing status."""
        pass
    
    @abstractmethod
    async def store_search_results(
        self,
        search_id: UUID,
        results: List[SearchResult],
        ttl: int = 3600
    ) -> bool:
        """Store search results in Redis cache."""
        pass


class VectorRepository(ABC):
    """Interface for vector database operations."""
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize vector database and create collection if needed."""
        pass
    
    @abstractmethod
    async def store_embedding(self, embedding: ImageEmbedding) -> bool:
        """Store image embedding in vector database."""
        pass
    
    @abstractmethod
    async def store_embeddings_batch(self, embeddings: List[ImageEmbedding]) -> bool:
        """Store multiple embeddings in batch."""
        pass
    
    @abstractmethod
    async def search_similar(
        self,
        query_vector: np.ndarray,
        limit: int = 50,
        threshold: float = 0.75,
        filter_conditions: Optional[Dict] = None
    ) -> List[SearchResult]:
        """Search for similar vectors in database."""
        pass
    
    @abstractmethod
    async def embedding_exists(self, embedding_id: str) -> bool:
        """Check if embedding already exists in database."""
        pass
    
    @abstractmethod
    async def get_embedding(self, embedding_id: str) -> Optional[ImageEmbedding]:
        """Retrieve embedding by ID."""
        pass
    
    @abstractmethod
    async def delete_embedding(self, embedding_id: str) -> bool:
        """Delete embedding from database."""
        pass
    
    @abstractmethod
    async def get_collection_stats(self) -> Dict:
        """Get statistics about the vector collection."""
        pass


class EmbeddingService(ABC):
    """Interface for image embedding generation."""
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the embedding model."""
        pass
    
    @abstractmethod
    async def generate_embedding(self, image_url: str) -> Optional[np.ndarray]:
        """Generate embedding vector for an image."""
        pass
    
    @abstractmethod
    async def generate_embeddings_batch(self, image_urls: List[str]) -> List[Optional[np.ndarray]]:
        """Generate embeddings for multiple images in batch."""
        pass
    
    @abstractmethod
    def get_embedding_dimension(self) -> int:
        """Get the dimension of embedding vectors."""
        pass
    
    @abstractmethod
    async def validate_image(self, image_url: str) -> bool:
        """Validate if image URL is accessible and processable."""
        pass