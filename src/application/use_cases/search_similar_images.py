"""Use case for searching similar images."""

import logging
import time
import json
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ...domain.entities import ImageSearch, ImageEmbedding
from ...domain.repositories import (
    ImageSearchRepository,
    VectorRepository,
    EmbeddingService
)
from ..dto import (
    ImageSearchRequest,
    ImageSearchResponse,
    SearchResultDTO
)

logger = logging.getLogger(__name__)


class SearchSimilarImagesUseCase:
    """Use case for searching similar images in the vector database."""
    
    def __init__(
        self,
        search_repo: ImageSearchRepository,
        vector_repo: VectorRepository,
        embedding_service: EmbeddingService
    ):
        self.search_repo = search_repo
        self.vector_repo = vector_repo
        self.embedding_service = embedding_service
    
    async def execute(
        self,
        request: ImageSearchRequest,
        force_recalculate: bool = False
    ) -> ImageSearchResponse:
        """Execute a similarity search for an image.
        
        Args:
            request: The search request
            force_recalculate: If True, recalculate even if embedding exists
        """
        start_time = time.time()
        
        try:
            logger.info(f"Starting search {request.search_id}")
            
            # Get the ImageSearch entity to check similarity_status first
            searches = await self.search_repo.get_pending_searches(limit=100)  # API max limit
            current_search = next((s for s in searches if s.id == request.search_id), None)
            
            vector = None
            
            # Check if we need to generate embedding based on similarity_status
            if current_search:
                # Only update to IN_PROGRESS if it's not already processing
                if current_search.search_status != 2:
                    await self.search_repo.update_search_status(
                        search_id=request.search_id,
                        search_status=2  # IN_PROGRESS
                    )
                
                if current_search.similarity_status == 1 or force_recalculate:
                    # Status 1: Never processed - need to generate embedding
                    logger.info(f"Generating embedding for search image: {request.image_url}")
                    vector = await self.embedding_service.generate_embedding(request.image_url)
                    
                    if vector is None:
                        error_msg = f"Failed to generate embedding for {request.image_url}"
                        logger.error(error_msg)
                        
                        # Update status to failed
                        await self.search_repo.update_search_status(
                            search_id=request.search_id,
                            search_status=4  # FAILED
                        )
                        
                        return ImageSearchResponse(
                            search_id=request.search_id,
                            success=False,
                            error_message=error_msg
                        )
                    
                    # No need to update status here - already IN_PROGRESS
                    # This avoids clearing metadata unnecessarily
                    
                elif current_search.similarity_status == 2:
                    # Status 2: Already embedded - just recalculate similarity
                    logger.info(f"Re-calculating similarity for existing search {request.search_id}")
                    # Try to retrieve existing embedding from a cache or regenerate
                    vector = await self.embedding_service.generate_embedding(request.image_url)
                    
                elif current_search.similarity_status == 3:
                    # Status 3: Disabled - skip this search
                    logger.info(f"Search {request.search_id} is disabled (similarity_status=3), skipping")
                    return ImageSearchResponse(
                        search_id=request.search_id,
                        success=True,
                        results=[],
                        total_matches=0,
                        search_time_ms=0,
                        processed_at=datetime.now(timezone.utc)
                    )
            else:
                # Fallback: generate embedding if no search found
                vector = await self.embedding_service.generate_embedding(request.image_url)
            
            if vector is None:
                error_msg = "Failed to get embedding vector"
                logger.error(error_msg)
                await self.search_repo.update_search_status(
                    search_id=request.search_id,
                    search_status=4  # FAILED
                )
                return ImageSearchResponse(
                    search_id=request.search_id,
                    success=False,
                    error_message=error_msg
                )
            
            # Search for similar images
            logger.info(f"Searching for similar images with threshold={request.threshold}, max_results={request.max_results}")
            
            # Build filter conditions from metadata if needed
            filter_conditions = None
            
            # Extract valid filter fields from metadata if present
            # Future filters can include:
            # - text_description: Filter by text/caption of the image
            # - camera_id: Filter by specific camera
            # - date_range: Filter by time period
            # - location: Filter by geographic area
            # - object_types: Filter by detected objects (person, vehicle, etc.)
            if request.metadata and isinstance(request.metadata, dict):
                valid_filters = {}
                
                # Add text description filter if present
                if 'text_description' in request.metadata:
                    valid_filters['text_description'] = request.metadata['text_description']
                
                # Add camera filter if present
                if 'camera_id' in request.metadata:
                    valid_filters['camera_id'] = request.metadata['camera_id']
                
                # Add object type filter if present
                if 'object_type' in request.metadata:
                    valid_filters['object_type'] = request.metadata['object_type']
                
                # Add time range filter if present
                if 'date_from' in request.metadata:
                    valid_filters['date_from'] = request.metadata['date_from']
                
                if 'date_to' in request.metadata:
                    valid_filters['date_to'] = request.metadata['date_to']
                
                # Only set filter_conditions if we have valid filters
                if valid_filters:
                    filter_conditions = valid_filters
                    logger.info(f"Applying filters: {list(valid_filters.keys())}")
            
            search_results = await self.vector_repo.search_similar(
                query_vector=vector,  # Fixed parameter name
                threshold=request.threshold,
                limit=request.max_results,
                filter_conditions=filter_conditions
            )
            
            logger.info(f"Found {len(search_results)} similar images")
            
            # Convert results to DTOs with proper structure
            result_dtos = []
            metadata_results = []
            
            for result in search_results:
                # Create DTO for API response
                result_dtos.append(
                    SearchResultDTO(
                        evidence_id=str(result.evidence_id),
                        similarity_score=result.similarity_score,
                        image_url=result.image_url,
                        camera_id=str(result.camera_id) if result.camera_id else None,
                        timestamp=result.created_at.isoformat() if result.created_at else None,
                        metadata=result.metadata or {}
                    )
                )
                
                # Create metadata entry for database storage
                metadata_results.append({
                    "evidence_id": str(result.evidence_id),
                    "similarity_score": result.similarity_score,
                    "image_url": result.image_url,
                    "camera_id": str(result.camera_id) if result.camera_id else None
                })
            
            # Calculate processing time
            search_time_ms = (time.time() - start_time) * 1000
            
            # Prepare data for Redis storage - using 'matches' as expected by API
            redis_data = {
                "search_image_url": request.image_url,
                "total_matches": len(search_results),
                "matches": metadata_results  # Changed from 'results' to 'matches'
            }
            
            # Store results in cache via API
            logger.info(f"Storing {len(search_results)} results in cache")
            await self.search_repo.store_search_results(
                search_id=request.search_id,
                results=redis_data,
                ttl=3600  # Will use configurable TTL from settings
            )
            
            # Prepare metadata for database storage
            db_metadata = {
                "total_matches": len(search_results),
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "results": metadata_results
            }
            
            # Update search status to completed with metadata
            similarity_status = 2 if search_results else 1  # MATCHES_FOUND or NO_MATCHES
            await self.search_repo.update_search_status(
                search_id=request.search_id,
                search_status=3,  # COMPLETED
                similarity_status=similarity_status,
                total_matches=len(search_results),
                metadata=db_metadata
            )
            
            logger.info(
                f"Search {request.search_id} completed: {len(search_results)} matches "
                f"found in {search_time_ms:.2f}ms"
            )
            
            return ImageSearchResponse(
                search_id=request.search_id,
                success=True,
                results=result_dtos,
                total_matches=len(result_dtos),
                search_time_ms=search_time_ms,
                processed_at=datetime.now(timezone.utc)
            )
            
        except Exception as e:
            error_msg = f"Unexpected error during search: {str(e)}"
            logger.error(f"Search {request.search_id} failed: {e}")
            
            # Update status to failed
            await self.search_repo.update_search_status(
                search_id=request.search_id,
                search_status=4,  # FAILED
                similarity_status=1  # NO_MATCHES
            )
            
            return ImageSearchResponse(
                search_id=request.search_id,
                success=False,
                error_message=error_msg,
                search_time_ms=(time.time() - start_time) * 1000
            )
    
    async def process_pending_searches(
        self,
        limit: int = 10
    ) -> List[ImageSearchResponse]:
        """Process multiple pending searches.
        
        This will process searches with similarity_status = 1 or 2:
        - 1: New searches that need embedding and similarity calculation
        - 2: Existing searches that need similarity recalculation (new evidences may exist)
        - 3: Disabled searches (will be skipped)
        """
        responses = []
        
        try:
            # Get pending searches (API now properly filters by status)
            searches = await self.search_repo.get_pending_searches(limit)
            
            # API already filters properly, no need for additional filtering
            searches_to_process = searches
            
            if not searches_to_process:
                logger.info("No pending searches to process")
                return []
            
            logger.info(f"Processing {len(searches_to_process)} pending searches")
            
            # Process each search
            for search in searches_to_process:
                # Check similarity_status to determine processing mode
                if search.similarity_status == 3:
                    logger.info(f"Skipping disabled search {search.id}")
                    continue
                
                force_recalc = search.similarity_status == 2
                
                request = ImageSearchRequest(
                    search_id=search.id,
                    user_id=search.user_id,
                    image_url=search.image_url,
                    threshold=search.get_similarity_threshold(),
                    max_results=search.get_max_results(),
                    metadata=search.metadata
                )
                
                response = await self.execute(request, force_recalculate=force_recalc)
                responses.append(response)
            
            successful = sum(1 for r in responses if r.success)
            failed = len(responses) - successful
            
            logger.info(
                f"Batch search completed: {successful}/{len(responses)} successful, "
                f"{failed} failed"
            )
            
            return responses
            
        except Exception as e:
            logger.error(f"Failed to process pending searches: {e}")
            return responses