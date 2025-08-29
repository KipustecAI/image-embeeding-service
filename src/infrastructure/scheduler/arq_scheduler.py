"""ARQ scheduler for periodic embedding tasks."""

import logging
from typing import Dict, Any
from datetime import datetime, timedelta
import asyncio

from arq import create_pool
from arq.connections import RedisSettings, ArqRedis
from arq.cron import cron

from ..config import Settings, get_settings
from ..embedding import CLIPEmbedder
from ..vector_db import QdrantVectorRepository
from ..api import VideoServerClient
from ...application.use_cases import (
    EmbedEvidenceImagesUseCase,
    SearchSimilarImagesUseCase
)

logger = logging.getLogger(__name__)


class EmbeddingScheduler:
    """Scheduler for embedding tasks."""
    
    def __init__(self):
        self.settings = get_settings()
        self.embedder = None
        self.vector_repo = None
        self.video_client = None
        self.evidence_use_case = None
        self.search_use_case = None
        self.initialized = False
    
    async def initialize(self):
        """Initialize all components."""
        if self.initialized:
            return
        
        try:
            logger.info("Initializing embedding scheduler components...")
            
            # Initialize CLIP embedder
            self.embedder = CLIPEmbedder(self.settings)
            await self.embedder.initialize()
            
            # Initialize Qdrant repository
            self.vector_repo = QdrantVectorRepository(self.settings)
            await self.vector_repo.initialize()
            
            # Initialize Video Server client
            self.video_client = VideoServerClient(self.settings)
            
            # Initialize use cases
            self.evidence_use_case = EmbedEvidenceImagesUseCase(
                evidence_repo=self.video_client,
                vector_repo=self.vector_repo,
                embedding_service=self.embedder
            )
            
            self.search_use_case = SearchSimilarImagesUseCase(
                search_repo=self.video_client,
                vector_repo=self.vector_repo,
                embedding_service=self.embedder
            )
            
            self.initialized = True
            logger.info("Scheduler components initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize scheduler: {e}")
            raise
    
    async def cleanup(self):
        """Clean up resources."""
        if self.embedder:
            await self.embedder.cleanup()
        if self.video_client:
            await self.video_client.cleanup()


# Global scheduler instance
scheduler = EmbeddingScheduler()


async def process_evidence_embeddings(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Task to process evidence embeddings.
    Runs every N minutes as configured.
    """
    start_time = datetime.utcnow()
    
    try:
        await scheduler.initialize()
        
        logger.info("Starting evidence embedding task")
        
        # Process batch of evidences
        result = await scheduler.evidence_use_case.execute_batch(
            limit=scheduler.settings.evidence_batch_size
        )
        
        duration = (datetime.utcnow() - start_time).total_seconds()
        
        logger.info(
            f"Evidence embedding task completed: "
            f"{result.successful}/{result.total_processed} successful, "
            f"{result.failed} failed in {duration:.2f}s"
        )
        
        return {
            "task": "process_evidence_embeddings",
            "timestamp": start_time.isoformat(),
            "duration_seconds": duration,
            "total_processed": result.total_processed,
            "successful": result.successful,
            "failed": result.failed,
            "embedded_ids": result.embedded_ids[:10]  # First 10 for logging
        }
        
    except Exception as e:
        logger.error(f"Evidence embedding task failed: {e}")
        return {
            "task": "process_evidence_embeddings",
            "timestamp": start_time.isoformat(),
            "error": str(e)
        }


async def process_image_searches(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Task to process pending image searches.
    Runs every N seconds as configured.
    """
    start_time = datetime.utcnow()
    
    try:
        await scheduler.initialize()
        
        logger.info("Starting image search processing task")
        
        # Process pending searches
        responses = await scheduler.search_use_case.process_pending_searches(
            limit=scheduler.settings.image_search_batch_size
        )
        
        successful = sum(1 for r in responses if r.success)
        failed = len(responses) - successful
        duration = (datetime.utcnow() - start_time).total_seconds()
        
        logger.info(
            f"Image search task completed: "
            f"{successful}/{len(responses)} successful, "
            f"{failed} failed in {duration:.2f}s"
        )
        
        return {
            "task": "process_image_searches",
            "timestamp": start_time.isoformat(),
            "duration_seconds": duration,
            "total_processed": len(responses),
            "successful": successful,
            "failed": failed,
            "search_ids": [str(r.search_id) for r in responses[:10]]
        }
        
    except Exception as e:
        logger.error(f"Image search task failed: {e}")
        return {
            "task": "process_image_searches",
            "timestamp": start_time.isoformat(),
            "error": str(e)
        }


async def update_vector_statistics(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Task to update vector database statistics.
    Runs every hour.
    """
    start_time = datetime.utcnow()
    
    try:
        await scheduler.initialize()
        
        stats = await scheduler.vector_repo.get_collection_stats()
        
        logger.info(
            f"Vector DB stats: {stats.get('points_count', 0)} points, "
            f"status: {stats.get('status', 'unknown')}"
        )
        
        return {
            "task": "update_vector_statistics",
            "timestamp": start_time.isoformat(),
            "stats": stats
        }
        
    except Exception as e:
        logger.error(f"Failed to get vector statistics: {e}")
        return {
            "task": "update_vector_statistics",
            "timestamp": start_time.isoformat(),
            "error": str(e)
        }


async def recalculate_searches(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Task to recalculate completed searches with new evidence.
    Runs periodically based on configuration.
    """
    start_time = datetime.utcnow()
    
    # Check if recalculation is enabled
    if not scheduler.settings.recalculation_enabled:
        logger.debug("Search recalculation is disabled")
        return {
            "task": "recalculate_searches",
            "timestamp": start_time.isoformat(),
            "skipped": True,
            "reason": "Recalculation disabled"
        }
    
    try:
        await scheduler.initialize()
        
        logger.info("Starting search recalculation task")
        
        # Get searches for recalculation based on configured hours_old
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{scheduler.settings.video_server_base_url}/api/v1/internal/image-search/recalculate"
            params = {
                "limit": scheduler.settings.recalculation_batch_size,
                "hours_old": scheduler.settings.recalculation_hours_old
            }
            headers = {"X-API-Key": scheduler.settings.video_server_api_key}
            
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            searches_to_recalc = data.get("searches", [])
        
        if not searches_to_recalc:
            logger.info("No searches need recalculation")
            return {
                "task": "recalculate_searches",
                "timestamp": start_time.isoformat(),
                "total_processed": 0,
                "message": "No searches need recalculation"
            }
        
        # Process each search for recalculation
        from uuid import UUID
        from datetime import datetime
        from ...domain.entities import ImageSearch
        from ...application.dto import ImageSearchRequest
        
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
                logger.error(f"Failed to recalculate search {search_data.get('id')}: {e}")
        
        successful = sum(1 for r in responses if r.success)
        failed = len(responses) - successful
        duration = (datetime.utcnow() - start_time).total_seconds()
        
        logger.info(
            f"Recalculation task completed: "
            f"{successful}/{len(responses)} successful, "
            f"{failed} failed in {duration:.2f}s"
        )
        
        return {
            "task": "recalculate_searches",
            "timestamp": start_time.isoformat(),
            "duration_seconds": duration,
            "total_processed": len(responses),
            "successful": successful,
            "failed": failed,
            "search_ids": [str(r.search_id) for r in responses[:10]]
        }
        
    except Exception as e:
        logger.error(f"Search recalculation task failed: {e}")
        return {
            "task": "recalculate_searches",
            "timestamp": start_time.isoformat(),
            "error": str(e)
        }


async def startup(ctx: Dict[str, Any]):
    """Startup task - runs once when worker starts."""
    logger.info("Starting embedding service scheduler...")
    await scheduler.initialize()
    
    # Run initial tasks
    if scheduler.settings.scheduler_enabled:
        logger.info("Running initial embedding tasks...")
        await process_evidence_embeddings(ctx)
        await process_image_searches(ctx)
        await update_vector_statistics(ctx)


async def shutdown(ctx: Dict[str, Any]):
    """Shutdown task - cleanup when worker stops."""
    logger.info("Shutting down embedding service scheduler...")
    await scheduler.cleanup()


def create_scheduler_settings() -> Dict[str, Any]:
    """Create ARQ worker settings."""
    settings = get_settings()
    
    # Build Redis URL
    if settings.redis_password:
        redis_url = (
            f"redis://:{settings.redis_password}@"
            f"{settings.redis_host}:{settings.redis_port}/{settings.redis_database}"
        )
    else:
        redis_url = (
            f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_database}"
        )
    
    redis_settings = RedisSettings.from_dsn(redis_url)
    
    # Define cron jobs
    cron_jobs = []
    
    if settings.scheduler_enabled:
        # Evidence embedding - run every N minutes
        evidence_minutes = settings.evidence_check_interval // 60
        if evidence_minutes > 0:
            # Run every N minutes
            cron_jobs.append(
                cron(
                    process_evidence_embeddings,
                    minute={m for m in range(0, 60, evidence_minutes)},
                    run_at_startup=True
                )
            )
        else:
            # If interval is less than 60 seconds, run every minute
            # (ARQ cron doesn't support second-level scheduling)
            cron_jobs.append(
                cron(
                    process_evidence_embeddings,
                    minute={m for m in range(60)},  # Every minute
                    run_at_startup=True
                )
            )
        
        # Image search processing - run frequently
        # Since it's every 30 seconds, we'll use a different approach
        # Run every minute and handle internally
        cron_jobs.append(
            cron(
                process_image_searches,
                minute={m for m in range(60)},  # Every minute
                run_at_startup=True
            )
        )
        
        # Vector statistics - run every hour
        cron_jobs.append(
            cron(
                update_vector_statistics,
                minute=0,  # At the start of every hour
                run_at_startup=True
            )
        )
        
        # Search recalculation - run based on configured interval
        if settings.recalculation_enabled:
            recalc_minutes = settings.recalculation_interval // 60
            if recalc_minutes >= 60:
                # Run every N hours at minute 15 (to avoid collision with vector stats)
                recalc_hours = recalc_minutes // 60
                cron_jobs.append(
                    cron(
                        recalculate_searches,
                        minute=15,
                        hour={h for h in range(0, 24, recalc_hours)},
                        run_at_startup=False  # Don't run at startup
                    )
                )
            else:
                # Run every N minutes
                cron_jobs.append(
                    cron(
                        recalculate_searches,
                        minute={m for m in range(0, 60, recalc_minutes)},
                        run_at_startup=False  # Don't run at startup
                    )
                )
    
    return {
        "redis_settings": redis_settings,
        "cron_jobs": cron_jobs,
        "on_startup": startup,
        "on_shutdown": shutdown,
        "max_jobs": settings.worker_concurrency,
        "job_timeout": 600,  # 10 minutes timeout (increased for large batches)
        "keep_result": 3600,  # Keep results for 1 hour
        "poll_delay": 0.5,
        "queue_read_limit": 100,
    }


# For running with arq worker
class WorkerSettings:
    """Settings for arq worker."""
    
    redis_settings = None
    cron_jobs = None
    on_startup = None
    on_shutdown = None
    max_jobs = None
    job_timeout = None
    
    @classmethod
    def load(cls):
        """Load settings from config."""
        config = create_scheduler_settings()
        print("Configs: ", config)
        cls.redis_settings = config["redis_settings"]
        cls.cron_jobs = config["cron_jobs"]
        cls.on_startup = config["on_startup"]
        cls.on_shutdown = config["on_shutdown"]
        cls.max_jobs = config["max_jobs"]
        cls.job_timeout = config["job_timeout"]
        return cls


# Load settings when module is imported
WorkerSettings.load()