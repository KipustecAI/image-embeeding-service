"""Use case for embedding evidence images."""

import logging
from typing import List
from datetime import datetime
import time
import json
from uuid import uuid4

from ...domain.entities import Evidence, ImageEmbedding
from ...domain.repositories import (
    EvidenceRepository,
    VectorRepository,
    EmbeddingService
)
from ..dto import (
    EvidenceEmbeddingRequest,
    EvidenceEmbeddingResponse,
    BatchEmbeddingResult
)

logger = logging.getLogger(__name__)


class EmbedEvidenceImagesUseCase:
    """Use case for processing and embedding evidence images."""
    
    def __init__(
        self,
        evidence_repo: EvidenceRepository,
        vector_repo: VectorRepository,
        embedding_service: EmbeddingService
    ):
        self.evidence_repo = evidence_repo
        self.vector_repo = vector_repo
        self.embedding_service = embedding_service
    
    async def execute_single(
        self,
        request: EvidenceEmbeddingRequest
    ) -> EvidenceEmbeddingResponse:
        """Embed a single evidence image."""
        try:
            # Check if already embedded
            if await self.vector_repo.embedding_exists(str(request.evidence_id)):
                logger.info(f"Evidence {request.evidence_id} already embedded")
                return EvidenceEmbeddingResponse(
                    evidence_id=request.evidence_id,
                    success=True,
                    embedding_id=str(request.evidence_id),
                    processed_at=datetime.utcnow()
                )
            
            # Generate embedding
            logger.info(f"Generating embedding for evidence {request.evidence_id}")
            vector = await self.embedding_service.generate_embedding(request.image_url)
            
            if vector is None:
                error_msg = f"Failed to generate embedding for {request.image_url}"
                logger.error(error_msg)
                return EvidenceEmbeddingResponse(
                    evidence_id=request.evidence_id,
                    success=False,
                    error_message=error_msg
                )
            
            # Create embedding entity
            embedding = ImageEmbedding.from_evidence(
                evidence_id=request.evidence_id,
                vector=vector,
                image_url=request.image_url,
                camera_id=request.camera_id,
                additional_metadata=request.metadata
            )
            
            # Store in vector database
            success = await self.vector_repo.store_embedding(embedding)
            
            if success:
                logger.info(f"Successfully embedded evidence {request.evidence_id}")
                return EvidenceEmbeddingResponse(
                    evidence_id=request.evidence_id,
                    success=True,
                    embedding_id=embedding.id,
                    vector_dimension=embedding.vector_dimension,
                    processed_at=datetime.utcnow()
                )
            else:
                error_msg = "Failed to store embedding in vector database"
                logger.error(f"{error_msg} for evidence {request.evidence_id}")
                return EvidenceEmbeddingResponse(
                    evidence_id=request.evidence_id,
                    success=False,
                    error_message=error_msg
                )
                
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(f"Failed to embed evidence {request.evidence_id}: {e}")
            return EvidenceEmbeddingResponse(
                evidence_id=request.evidence_id,
                success=False,
                error_message=error_msg
            )
    
    async def process_evidence_images(self, evidence: Evidence) -> tuple[bool, List[str]]:
        """Process all images from a single evidence."""
        image_urls = evidence.get_image_urls()
        summary = evidence.get_summary()
        if not image_urls:
            logger.warning(f"Evidence {evidence.id} has no image URLs in json_data")
            return False, []
        
        logger.info(f"Processing {len(image_urls)} images for evidence {evidence.id}")
        
        embedding_ids = []
        all_success = True
        
        for idx, image_url in enumerate(image_urls):
            try:
                # Generate embedding for each image
                logger.info(f"Processing image {idx+1}/{len(image_urls)} for evidence {evidence.id}")
                vector = await self.embedding_service.generate_embedding(image_url)
                
                if vector is None:
                    logger.error(f"Failed to generate embedding for image {idx+1}: {image_url}")
                    all_success = False
                    continue
                
                # Create unique embedding ID for this image (Qdrant requires UUID or integer)
                embedding_id = str(uuid4())
                
                # Create embedding entity with evidence metadata
                # Current payload structure in Qdrant:
                metadata = {
                    "evidence_id": str(evidence.id),
                    "camera_id": str(evidence.camera_id),
                    "image_index": idx,
                    "total_images": len(image_urls),
                    "image_url": image_url,
                    "created_at": datetime.utcnow().isoformat(),
                    "summary": summary
                }
                
                # TODO: Add these fields when available from evidence json_data:
                # - text_description: Text caption/description from OCR or AI analysis
                # - object_types: List of detected objects ["person", "vehicle", etc.]
                # - location: GPS coordinates {"lat": float, "lon": float}
                # - scene_type: Indoor/outdoor/street/parking/etc.
                # - confidence_scores: Object detection confidence scores
                # Example future enhancement:
                # if evidence.json_data:
                #     if 'text_description' in evidence.json_data:
                #         metadata['text_description'] = evidence.json_data['text_description']
                #     if 'detected_objects' in evidence.json_data:
                #         metadata['object_types'] = evidence.json_data['detected_objects']
                #     if 'gps_location' in evidence.json_data:
                #         metadata['location'] = evidence.json_data['gps_location']
                
                embedding = ImageEmbedding(
                    id=embedding_id,
                    evidence_id=evidence.id,
                    vector=vector,
                    image_url=image_url,
                    camera_id=evidence.camera_id,
                    created_at=datetime.utcnow(),
                    metadata=metadata,
                    source_type="evidence"
                )
                
                # Store in vector database
                success = await self.vector_repo.store_embedding(embedding)
                
                if success:
                    embedding_ids.append(embedding_id)
                    logger.info(f"Successfully embedded image {idx+1}/{len(image_urls)}")
                else:
                    logger.error(f"Failed to store embedding for image {idx+1}")
                    all_success = False
                    
            except Exception as e:
                logger.error(f"Error processing image {idx+1} for evidence {evidence.id}: {e}")
                all_success = False
        
        return all_success, embedding_ids
    
    async def execute_batch(self, limit: int = 50) -> BatchEmbeddingResult:
        """Process batch of evidences for embedding."""
        start_time = time.time()
        successful = 0
        failed = 0
        errors = []
        embedded_ids = []
        
        try:
            # Get unembedded evidences (status=3)
            evidences = await self.evidence_repo.get_unembedded_evidences(limit)
            total = len(evidences)
            
            if total == 0:
                logger.info("No evidences to embed")
                return BatchEmbeddingResult(
                    total_processed=0,
                    successful=0,
                    failed=0,
                    processing_time_ms=0,
                    errors=[],
                    embedded_ids=[]
                )
            
            logger.info(f"Processing {total} evidences for embedding")
            
            # Process each evidence
            for evidence in evidences:
                try:
                    # Process all images for this evidence
                    all_success, evidence_embedding_ids = await self.process_evidence_images(evidence)
                    
                    if evidence_embedding_ids:
                        # Mark evidence as embedded (status 3 -> 4)
                        success = await self.evidence_repo.mark_evidence_as_embedded(
                            evidence.id,
                            evidence_embedding_ids
                        )
                        
                        if success:
                            successful += 1
                            embedded_ids.extend(evidence_embedding_ids)
                            logger.info(f"Evidence {evidence.id} marked as embedded with {len(evidence_embedding_ids)} images")
                        else:
                            failed += 1
                            errors.append({
                                "evidence_id": str(evidence.id),
                                "error": "Failed to update evidence status"
                            })
                    else:
                        failed += 1
                        errors.append({
                            "evidence_id": str(evidence.id),
                            "error": "No images could be embedded"
                        })
                        
                except Exception as e:
                    failed += 1
                    errors.append({
                        "evidence_id": str(evidence.id),
                        "error": str(e)
                    })
                    logger.error(f"Failed to process evidence {evidence.id}: {e}")
            
            processing_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"Batch embedding completed: {successful}/{total} successful, "
                f"{failed} failed in {processing_time:.2f}ms"
            )
            
            return BatchEmbeddingResult(
                total_processed=total,
                successful=successful,
                failed=failed,
                processing_time_ms=processing_time,
                errors=errors,
                embedded_ids=embedded_ids
            )
            
        except Exception as e:
            logger.error(f"Batch embedding failed: {e}")
            processing_time = (time.time() - start_time) * 1000
            
            return BatchEmbeddingResult(
                total_processed=0,
                successful=successful,
                failed=failed,
                processing_time_ms=processing_time,
                errors=[{"error": str(e)}],
                embedded_ids=embedded_ids
            )