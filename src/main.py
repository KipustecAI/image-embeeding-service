"""Main entry point for Image Embedding Service."""

import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Depends, status, Query
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from typing import Optional, List
from uuid import UUID
from datetime import datetime
import uvicorn
import httpx

from src.infrastructure.config import get_settings
from src.infrastructure.scheduler import EmbeddingScheduler

from src.application.dto import ImageSearchRequest, ImageSearchResponse
from src.domain.entities import ImageSearch

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global instances
settings = get_settings()
scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    global scheduler
    
    logger.info("Starting Image Embedding Service...")
    
    # Initialize scheduler components
    scheduler = EmbeddingScheduler()
    await scheduler.initialize()
    
    logger.info("Image Embedding Service started successfully")
    
    yield
    
    # Cleanup
    logger.info("Shutting down Image Embedding Service...")
    if scheduler:
        await scheduler.cleanup()
    logger.info("Image Embedding Service shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Image Embedding Service",
    description="Microservice for image embedding and similarity search",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Image Embedding Service",
        "status": "running",
        "environment": settings.environment,
        "scheduler_enabled": settings.scheduler_enabled
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    try:
        # Check if components are initialized
        if not scheduler or not scheduler.initialized:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service not fully initialized"
            )
        
        # Get vector DB stats
        stats = await scheduler.vector_repo.get_collection_stats()
        
        return {
            "status": "healthy",
            "components": {
                "embedder": scheduler.embedder is not None,
                "vector_db": scheduler.vector_repo is not None,
                "api_client": scheduler.video_client is not None
            },
            "vector_db_stats": {
                "collection": stats.get("collection_name"),
                "points": stats.get("points_count", 0),
                "status": stats.get("status")
            }
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "error": str(e)
            }
        )


@app.post("/api/v1/embed/evidence")
async def embed_evidence_manual(
    evidence_id: str,
    image_url: str,
    camera_id: str
):
    """Manually trigger embedding for a specific evidence."""
    try:
        from uuid import UUID
        from src.application.dto import EvidenceEmbeddingRequest
        
        request = EvidenceEmbeddingRequest(
            evidence_id=UUID(evidence_id),
            image_url=image_url,
            camera_id=UUID(camera_id),
            evidence_type="image"
        )
        
        response = await scheduler.evidence_use_case.execute_single(request)
        
        if response.success:
            return {
                "success": True,
                "evidence_id": str(response.evidence_id),
                "embedding_id": response.embedding_id,
                "vector_dimension": response.vector_dimension
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=response.error_message
            )
            
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid UUID format: {e}"
        )
    except Exception as e:
        logger.error(f"Failed to embed evidence: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.post("/api/v1/search/manual")
async def search_manual(
    search_id: str,
    user_id: str,
    image_url: str,
    threshold: float = 0.75,
    max_results: int = 50
):
    """Manually trigger a similarity search."""
    try:
        from uuid import UUID
        from src.application.dto import ImageSearchRequest
        
        request = ImageSearchRequest(
            search_id=UUID(search_id),
            user_id=UUID(user_id),
            image_url=image_url,
            threshold=threshold,
            max_results=max_results
        )
        
        response = await scheduler.search_use_case.execute(request)
        
        if response.success:
            return {
                "success": True,
                "search_id": str(response.search_id),
                "total_matches": response.total_matches,
                "search_time_ms": response.search_time_ms,
                "results": [
                    {
                        "evidence_id": r.evidence_id,
                        "similarity_score": r.similarity_score,
                        "image_url": r.image_url
                    }
                    for r in response.results[:10]  # First 10 results
                ]
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=response.error_message
            )
            
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid UUID format: {e}"
        )
    except Exception as e:
        logger.error(f"Failed to execute search: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.post("/api/v1/process/evidences")
async def process_evidences():
    """Manually trigger evidence processing."""
    try:
        result = await scheduler.evidence_use_case.execute_batch(
            limit=scheduler.settings.evidence_batch_size
        )
        
        return {
            "success": True,
            "total_processed": result.total_processed,
            "successful": result.successful,
            "failed": result.failed,
            "processing_time_ms": result.processing_time_ms
        }
        
    except Exception as e:
        logger.error(f"Failed to process evidences: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.post("/api/v1/process/searches")
async def process_searches():
    """Manually trigger search processing."""
    try:
        responses = await scheduler.search_use_case.process_pending_searches(
            limit=scheduler.settings.image_search_batch_size
        )
        
        successful = sum(1 for r in responses if r.success)
        failed = len(responses) - successful
        
        return {
            "success": True,
            "total_processed": len(responses),
            "successful": successful,
            "failed": failed
        }
        
    except Exception as e:
        logger.error(f"Failed to process searches: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.post("/api/v1/recalculate/searches")
async def recalculate_searches(
    search_ids: Optional[List[str]] = Query(None, description="Specific search IDs to recalculate"),
    limit: int = Query(10, ge=1, le=100, description="Max searches to recalculate (when not using search_ids)"),
    hours_old: Optional[int] = Query(None, ge=1, le=168, description="Only recalculate searches older than X hours"),
    force_all: bool = Query(False, description="Recalculate ALL eligible searches (ignores limit)")
):
    """Trigger recalculation for completed searches with new evidence.
    
    This endpoint supports three modes:
    1. Specific IDs: Provide search_ids to recalculate specific searches
    2. Batch: Use limit and hours_old to recalculate a batch
    3. All: Set force_all=true to recalculate ALL eligible searches
    
    Eligible searches have:
    - search_status = 3 (COMPLETED)
    - similarity_status = 2 (MATCHES_FOUND)
    
    Args:
        search_ids: Optional list of specific search IDs to recalculate
        limit: Maximum number of searches to recalculate (ignored if search_ids provided)
        hours_old: Optional - only recalculate if processed more than X hours ago
        force_all: Recalculate ALL eligible searches (use with caution!)
    """
    try:
        searches_to_recalc = []
        
        if search_ids:
            # Mode 1: Specific IDs provided
            logger.info(f"Recalculating specific searches: {search_ids}")
            
            # Get each search by ID
            async with httpx.AsyncClient(timeout=30) as client:
                headers = {"X-API-Key": scheduler.settings.video_server_api_key}
                
                for search_id in search_ids:
                    try:
                        # Validate UUID format
                        search_uuid = UUID(search_id)
                        
                        # Get search details (we'll create a simple endpoint for this)
                        url = f"{scheduler.settings.video_server_base_url}/api/v1/internal/image-search/{search_uuid}"
                        response = await client.get(url, headers=headers)
                        
                        if response.status_code == 200:
                            search_data = response.json()
                            # Only add if it's completed with matches
                            if search_data["search_status"] == 3 and search_data["similarity_status"] == 2:
                                searches_to_recalc.append(search_data)
                            else:
                                logger.warning(f"Search {search_id} not eligible for recalculation")
                    except ValueError:
                        logger.error(f"Invalid UUID format: {search_id}")
                    except Exception as e:
                        logger.error(f"Failed to get search {search_id}: {e}")
        else:
            # Mode 2 or 3: Get batch from recalculate endpoint
            async with httpx.AsyncClient(timeout=30) as client:
                url = f"{scheduler.settings.video_server_base_url}/api/v1/internal/image-search/recalculate"
                
                # Use appropriate limit
                actual_limit = 1000 if force_all else limit
                params = {"limit": actual_limit}
                
                if hours_old:
                    params["hours_old"] = hours_old
                    
                headers = {"X-API-Key": scheduler.settings.video_server_api_key}
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                
                data = response.json()
                searches_to_recalc = data.get("searches", [])
        
        if not searches_to_recalc:
            return {
                "success": True,
                "message": "No searches need recalculation",
                "total_processed": 0,
                "mode": "specific" if search_ids else ("all" if force_all else "batch")
            }
        
        # Process each search for recalculation
        logger.info(f"Recalculating {len(searches_to_recalc)} searches")
        responses = []
        
        for search_data in searches_to_recalc:
            try:
                search = ImageSearch(
                    id=UUID(search_data["id"]),
                    user_id=UUID(search_data["user_id"]),
                    image_url=search_data["image_url"],
                    search_status=search_data["search_status"],
                    similarity_status=search_data["similarity_status"],
                    created_at=datetime.fromisoformat(search_data["created_at"]),
                    updated_at=datetime.fromisoformat(search_data["updated_at"]) if search_data.get("updated_at") else None,
                    processed_at=datetime.fromisoformat(search_data["processed_at"]) if search_data.get("processed_at") else None,
                    metadata=search_data.get("search_metadata"),
                    results_key=search_data.get("results_key"),
                    total_matches=search_data.get("total_matches", 0)
                )
                
                request = ImageSearchRequest(
                    search_id=search.id,
                    user_id=search.user_id,
                    image_url=search.image_url,
                    threshold=search.get_similarity_threshold(),
                    max_results=search.get_max_results(),
                    metadata=search.metadata
                )
                
                # Force recalculation
                response = await scheduler.search_use_case.execute(request, force_recalculate=True)
                responses.append(response)
                
            except Exception as e:
                logger.error(f"Failed to process search {search_data.get('id')}: {e}")
                # Create a failed response
                responses.append(ImageSearchResponse(
                    search_id=UUID(search_data["id"]),
                    success=False,
                    error_message=str(e)
                ))
        
        successful = sum(1 for r in responses if r.success)
        failed = len(responses) - successful
        
        return {
            "success": True,
            "message": f"Recalculated {len(responses)} searches",
            "total_processed": len(responses),
            "successful": successful,
            "failed": failed,
            "mode": "specific" if search_ids else ("all" if force_all else "batch"),
            "search_ids": [str(r.search_id) for r in responses] if search_ids else None
        }
        
    except httpx.HTTPError as e:
        logger.error(f"Failed to get searches for recalculation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get searches from API: {e}"
        )
    except Exception as e:
        logger.error(f"Failed to recalculate searches: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.post("/api/v1/recalculate/search/{search_id}")
async def recalculate_single_search(search_id: str):
    """Trigger recalculation for a single specific search.
    
    Convenience endpoint for recalculating a single search by ID.
    """
    try:
        # Validate UUID
        try:
            search_uuid = UUID(search_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid search ID format: {search_id}"
            )
        
        # Call the batch endpoint with single ID
        return await recalculate_searches(
            search_ids=[str(search_uuid)],
            limit=1,
            hours_old=None,
            force_all=False
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to recalculate search {search_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.get("/api/v1/stats")
async def get_statistics():
    """Get service statistics."""
    try:
        stats = await scheduler.vector_repo.get_collection_stats()
        
        return {
            "service": "Image Embedding Service",
            "vector_database": stats,
            "configuration": {
                "model": settings.clip_model_name,
                "device": settings.clip_device,
                "vector_size": settings.qdrant_vector_size,
                "evidence_batch_size": settings.evidence_batch_size,
                "search_batch_size": settings.image_search_batch_size
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get statistics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


def main():
    """Main entry point."""
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8001,
        reload=settings.environment == "development",
        log_level=settings.log_level.lower()
    )


if __name__ == "__main__":
    main()