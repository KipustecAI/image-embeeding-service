"""CLIP embedding service implementation."""

import logging
from typing import List, Optional
from io import BytesIO
import numpy as np
import httpx
from PIL import Image
import torch
from sentence_transformers import SentenceTransformer

from ...domain.repositories import EmbeddingService
from ..config import Settings

logger = logging.getLogger(__name__)


class CLIPEmbedder(EmbeddingService):
    """CLIP embedding service using sentence-transformers."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model: Optional[SentenceTransformer] = None
        self.device: Optional[torch.device] = None
        self.http_client = httpx.AsyncClient(timeout=settings.image_download_timeout)
        
    async def initialize(self) -> None:
        """Initialize the CLIP model."""
        try:
            logger.info(f"Initializing CLIP model: {self.settings.clip_model_name}")
            
            # Set device
            if self.settings.clip_device == "cuda" and torch.cuda.is_available():
                self.device = torch.device("cuda")
            else:
                self.device = torch.device("cpu")
            
            # Load model
            self.model = SentenceTransformer(f"clip-{self.settings.clip_model_name}")
            self.model.to(self.device)
            
            logger.info(f"CLIP model loaded successfully on {self.device}")
            
        except Exception as e:
            logger.error(f"Failed to initialize CLIP model: {e}")
            raise
    
    async def generate_embedding(self, image_url: str) -> Optional[np.ndarray]:
        """Generate embedding vector for an image."""
        try:
            # Download image
            image = await self._download_image(image_url)
            if image is None:
                return None
            
            # Generate embedding
            with torch.no_grad():
                embedding = self.model.encode(image, convert_to_tensor=True)
                embedding_np = embedding.cpu().numpy()
            
            # Normalize embedding
            embedding_np = embedding_np / np.linalg.norm(embedding_np)
            
            return embedding_np
            
        except Exception as e:
            logger.error(f"Failed to generate embedding for {image_url}: {e}")
            return None
    
    async def generate_embeddings_batch(self, image_urls: List[str]) -> List[Optional[np.ndarray]]:
        """Generate embeddings for multiple images in batch."""
        try:
            # Download all images
            images = []
            valid_indices = []
            
            for idx, url in enumerate(image_urls):
                image = await self._download_image(url)
                if image is not None:
                    images.append(image)
                    valid_indices.append(idx)
            
            if not images:
                return [None] * len(image_urls)
            
            # Generate embeddings in batch
            with torch.no_grad():
                embeddings = self.model.encode(
                    images,
                    batch_size=self.settings.clip_batch_size,
                    convert_to_tensor=True,
                    show_progress_bar=len(images) > 10
                )
                embeddings_np = embeddings.cpu().numpy()
            
            # Normalize embeddings
            norms = np.linalg.norm(embeddings_np, axis=1, keepdims=True)
            embeddings_np = embeddings_np / norms
            
            # Build result list with None for failed images
            results = [None] * len(image_urls)
            for idx, valid_idx in enumerate(valid_indices):
                results[valid_idx] = embeddings_np[idx]
            
            return results
            
        except Exception as e:
            logger.error(f"Failed to generate batch embeddings: {e}")
            return [None] * len(image_urls)
    
    def get_embedding_dimension(self) -> int:
        """Get the dimension of embedding vectors."""
        # CLIP ViT-B-32 produces 512-dimensional vectors
        return self.settings.qdrant_vector_size
    
    async def validate_image(self, image_url: str) -> bool:
        """Validate if image URL is accessible and processable."""
        try:
            # Check URL format
            if not image_url.startswith(('http://', 'https://')):
                logger.warning(f"Invalid URL format: {image_url}")
                return False
            
            # Check file extension
            supported_formats = self.settings.supported_formats
            if isinstance(supported_formats, str):
                supported_formats = supported_formats.split(',')
            
            extension = image_url.split('.')[-1].lower().split('?')[0]
            if extension not in supported_formats:
                logger.warning(f"Unsupported format {extension} for {image_url}")
                return False
            
            # Try to download and open image
            image = await self._download_image(image_url)
            return image is not None
            
        except Exception as e:
            logger.error(f"Failed to validate image {image_url}: {e}")
            return False
    
    async def _download_image(self, image_url: str) -> Optional[Image.Image]:
        """Download image from URL."""
        try:
            response = await self.http_client.get(image_url)
            response.raise_for_status()
            
            # Check content type
            content_type = response.headers.get('content-type', '').lower()
            if not content_type.startswith('image/'):
                logger.warning(f"Invalid content type {content_type} for {image_url}")
                return None
            
            # Check size
            content_length = int(response.headers.get('content-length', 0))
            if content_length > self.settings.max_image_size:
                logger.warning(f"Image too large ({content_length} bytes) for {image_url}")
                return None
            
            # Open image
            image = Image.open(BytesIO(response.content))
            
            # Convert to RGB if necessary
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            return image
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error downloading {image_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to download image {image_url}: {e}")
            return None
    
    async def cleanup(self) -> None:
        """Clean up resources."""
        await self.http_client.aclose()
        if self.model is not None:
            del self.model
            torch.cuda.empty_cache() if torch.cuda.is_available() else None