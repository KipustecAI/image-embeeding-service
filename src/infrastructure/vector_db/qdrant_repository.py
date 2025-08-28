"""Qdrant vector database repository implementation."""

import logging
from typing import List, Optional, Dict
from datetime import datetime
import numpy as np
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    SearchRequest,
    UpdateStatus
)

from ...domain.entities import ImageEmbedding, SearchResult
from ...domain.repositories import VectorRepository
from ..config import Settings

logger = logging.getLogger(__name__)


class QdrantVectorRepository(VectorRepository):
    """Qdrant vector database implementation."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Optional[QdrantClient] = None
        self.collection_name = settings.qdrant_collection_name
        self.vector_size = settings.qdrant_vector_size
    
    async def initialize(self) -> None:
        """Initialize Qdrant client and create collection if needed."""
        try:
            # Create Qdrant client
            if self.settings.qdrant_api_key:
                self.client = QdrantClient(
                    host=self.settings.qdrant_host,
                    port=self.settings.qdrant_port,
                    api_key=self.settings.qdrant_api_key,
                    timeout=30
                )
            else:
                self.client = QdrantClient(
                    host=self.settings.qdrant_host,
                    port=self.settings.qdrant_port,
                    timeout=30
                )
            
            # Check if collection exists
            collections = self.client.get_collections().collections
            collection_exists = any(
                c.name == self.collection_name for c in collections
            )
            
            if not collection_exists:
                logger.info(f"Creating collection '{self.collection_name}'")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size,
                        distance=Distance.COSINE
                    )
                )
                
                # Create payload indices for better search performance
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="source_type",
                    field_schema="keyword"
                )
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="camera_id",
                    field_schema="keyword"
                )
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="evidence_id",
                    field_schema="keyword"
                )
                
                logger.info(f"Collection '{self.collection_name}' created successfully")
            else:
                logger.info(f"Collection '{self.collection_name}' already exists")
            
        except Exception as e:
            logger.error(f"Failed to initialize Qdrant: {e}")
            raise
    
    async def store_embedding(self, embedding: ImageEmbedding) -> bool:
        """Store a single embedding in Qdrant."""
        try:
            point = PointStruct(
                id=embedding.id,
                vector=embedding.vector.tolist(),
                payload=embedding.metadata
            )
            
            result = self.client.upsert(
                collection_name=self.collection_name,
                points=[point],
                wait=True
            )
            
            if result.status == UpdateStatus.COMPLETED:
                logger.debug(f"Stored embedding {embedding.id}")
                return True
            else:
                logger.error(f"Failed to store embedding {embedding.id}: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Error storing embedding {embedding.id}: {e}")
            return False
    
    async def store_embeddings_batch(
        self,
        embeddings: List[ImageEmbedding]
    ) -> bool:
        """Store multiple embeddings in batch."""
        try:
            points = [
                PointStruct(
                    id=embedding.id,
                    vector=embedding.vector.tolist(),
                    payload=embedding.metadata
                )
                for embedding in embeddings
            ]
            
            result = self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True
            )
            
            if result.status == UpdateStatus.COMPLETED:
                logger.info(f"Stored batch of {len(embeddings)} embeddings")
                return True
            else:
                logger.error(f"Failed to store batch: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Error storing batch: {e}")
            return False
    
    async def search_similar(
        self,
        query_vector: np.ndarray,
        limit: int = 50,
        threshold: float = 0.75,
        filter_conditions: Optional[Dict] = None
    ) -> List[SearchResult]:
        """Search for similar vectors."""
        try:
            # Build filter
            search_filter = None
            if filter_conditions:
                must_conditions = []
                for field, value in filter_conditions.items():
                    must_conditions.append(
                        FieldCondition(
                            key=field,
                            match=MatchValue(value=value)
                        )
                    )
                
                if must_conditions:
                    search_filter = Filter(must=must_conditions)
            
            # Perform search
            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector.tolist(),
                limit=limit,
                score_threshold=threshold,
                query_filter=search_filter,
                with_payload=True
            )
            
            # Convert to SearchResult objects
            search_results = []
            for result in results:
                payload = result.payload or {}
                
                # Parse camera_id and timestamp from payload
                camera_id = None
                if "camera_id" in payload:
                    try:
                        camera_id = UUID(payload["camera_id"])
                    except:
                        pass
                
                created_at = None
                if "created_at" in payload:
                    try:
                        created_at = datetime.fromisoformat(payload["created_at"])
                    except:
                        created_at = datetime.utcnow()
                else:
                    created_at = datetime.utcnow()
                
                search_result = SearchResult(
                    evidence_id=payload.get("evidence_id", result.id),
                    image_url=payload.get("image_url", ""),
                    similarity_score=result.score,
                    metadata=payload,
                    camera_id=camera_id,
                    created_at=created_at
                )
                search_results.append(search_result)
            
            logger.info(
                f"Found {len(search_results)} similar images "
                f"(threshold={threshold}, limit={limit})"
            )
            
            return search_results
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []
    
    async def embedding_exists(self, embedding_id: str) -> bool:
        """Check if embedding exists."""
        try:
            result = self.client.retrieve(
                collection_name=self.collection_name,
                ids=[embedding_id]
            )
            return len(result) > 0
            
        except Exception as e:
            logger.error(f"Failed to check embedding existence: {e}")
            return False
    
    async def get_embedding(self, embedding_id: str) -> Optional[ImageEmbedding]:
        """Retrieve embedding by ID."""
        try:
            results = self.client.retrieve(
                collection_name=self.collection_name,
                ids=[embedding_id],
                with_vectors=True,
                with_payload=True
            )
            
            if not results:
                return None
            
            point = results[0]
            vector = np.array(point.vector)
            metadata = point.payload or {}
            
            embedding = ImageEmbedding(
                id=point.id,
                vector=vector,
                metadata=metadata,
                created_at=datetime.fromisoformat(
                    metadata.get("created_at", datetime.utcnow().isoformat())
                ),
                source_type=metadata.get("source_type", "unknown"),
                image_url=metadata.get("image_url", "")
            )
            
            return embedding
            
        except Exception as e:
            logger.error(f"Failed to retrieve embedding {embedding_id}: {e}")
            return None
    
    async def delete_embedding(self, embedding_id: str) -> bool:
        """Delete embedding from database."""
        try:
            result = self.client.delete(
                collection_name=self.collection_name,
                points_selector=[embedding_id],
                wait=True
            )
            
            if result.status == UpdateStatus.COMPLETED:
                logger.info(f"Deleted embedding {embedding_id}")
                return True
            else:
                logger.error(f"Failed to delete embedding {embedding_id}: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Error deleting embedding {embedding_id}: {e}")
            return False
    
    async def get_collection_stats(self) -> Dict:
        """Get collection statistics."""
        try:
            collection_info = self.client.get_collection(self.collection_name)
            
            stats = {
                "collection_name": self.collection_name,
                "vector_size": collection_info.config.params.vectors.size,
                "distance_metric": collection_info.config.params.vectors.distance.value,
                "points_count": collection_info.points_count,
                "indexed_vectors_count": collection_info.indexed_vectors_count,
                "status": collection_info.status.value
            }
            
            return stats
            
        except Exception as e:
            logger.error(f"Failed to get collection stats: {e}")
            return {}