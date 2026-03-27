"""Image Embedding Backend — pipeline orchestration, storage, and API.

Consumes pre-computed vectors from the GPU compute service via Redis Streams.
Stores directly in Qdrant + PostgreSQL — no ARQ queue for the happy path.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextlib import asynccontextmanager
from typing import List, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.api.dependencies import verify_api_key
from src.db.repositories import EmbeddingRequestRepository, SearchRequestRepository
from src.infrastructure.config import get_settings
from src.infrastructure.database import engine, get_session
from src.infrastructure.vector_db.qdrant_repository import QdrantVectorRepository
from src.services.batch_trigger import get_batch_trigger
from src.services.safety_nets import (
    cleanup_old_requests,
    recover_stale_working,
    recalculate_searches,
)
from src.streams.embedding_results_consumer import (
    create_embedding_results_consumer,
    set_results_event_loop,
    set_vector_repo,
)
from src.streams.search_results_consumer import (
    create_search_results_consumer,
    set_search_results_event_loop,
    set_search_vector_repo,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Global references
scheduler = None
embed_consumer = None
search_consumer = None
vector_repo = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler, embed_consumer, search_consumer, vector_repo

    logger.info("Starting Image Embedding Backend...")

    # 1. Verify database connection
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("PostgreSQL connected")

    # 2. Initialize Qdrant (used directly by consumers, no ARQ needed)
    vector_repo = QdrantVectorRepository(settings)
    await vector_repo.initialize()
    set_vector_repo(vector_repo)
    set_search_vector_repo(vector_repo)
    logger.info("Qdrant connected")

    # 3. APScheduler — safety nets + periodic tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        recover_stale_working,
        IntervalTrigger(minutes=5),
        id="stale_recovery",
        misfire_grace_time=60,
    )
    scheduler.add_job(
        recalculate_searches,
        IntervalTrigger(hours=1),
        id="recalculate_searches",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        cleanup_old_requests,
        IntervalTrigger(hours=24),
        id="cleanup_old",
        misfire_grace_time=600,
    )
    scheduler.start()
    logger.info("Scheduler started (stale recovery, recalculation, cleanup)")

    # 4. Stream Consumers (consume output from GPU compute service)
    loop = asyncio.get_running_loop()
    set_results_event_loop(loop)
    set_search_results_event_loop(loop)

    embed_consumer = create_embedding_results_consumer()
    embed_consumer.start()

    search_consumer = create_search_results_consumer()
    search_consumer.start()
    logger.info("Stream consumers started (embeddings:results + search:results)")

    logger.info("Image Embedding Backend ready")

    yield

    # ── SHUTDOWN ──
    logger.info("Shutting down...")

    if embed_consumer:
        embed_consumer.stop()
    if search_consumer:
        search_consumer.stop()

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    await engine.dispose()
    logger.info("Shutdown complete")


# ── FastAPI App ──

app = FastAPI(
    title="Image Embedding Backend",
    description="Event-driven backend for CLIP image embedding storage and similarity search",
    version="2.1.0",
    lifespan=lifespan,
)


# ── Health & Stats ──


@app.get("/")
async def root():
    return {
        "service": "Image Embedding Backend",
        "version": "2.1.0",
        "status": "running",
        "environment": settings.environment,
    }


@app.get("/health")
async def health_check():
    components = {
        "database": False,
        "qdrant": False,
        "scheduler": scheduler.running if scheduler else False,
    }

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["database"] = True
    except Exception:
        pass

    try:
        if vector_repo:
            stats = await vector_repo.get_collection_stats()
            components["qdrant"] = bool(stats)
    except Exception:
        pass

    return {
        "status": "healthy" if all(components.values()) else "degraded",
        "components": components,
        "consumers": {
            "embed": embed_consumer.health() if embed_consumer else None,
            "search": search_consumer.health() if search_consumer else None,
        },
    }


@app.get("/api/v1/stats")
async def get_statistics():
    async with get_session() as session:
        embed_repo = EmbeddingRequestRepository(session)
        search_repo = SearchRequestRepository(session)
        embed_counts = await embed_repo.count_by_status()
        search_counts = await search_repo.count_by_status()

    qdrant_stats = {}
    if vector_repo:
        qdrant_stats = await vector_repo.get_collection_stats()

    return {
        "service": "Image Embedding Backend",
        "pipeline": {
            "embedding_requests": embed_counts,
            "search_requests": search_counts,
        },
        "qdrant": qdrant_stats,
    }


# ── Pipeline Status (used by E2E test scripts) ──


@app.get("/api/v1/pipeline/status")
async def pipeline_status():
    """Combined pipeline status: DB counts + consumer health."""
    async with get_session() as session:
        embed_repo = EmbeddingRequestRepository(session)
        search_repo = SearchRequestRepository(session)
        embed_counts = await embed_repo.count_by_status()
        search_counts = await search_repo.count_by_status()

    return {
        "status_counts": {
            "embedding": embed_counts,
            "search": search_counts,
        },
        "consumers": {
            "embed": embed_consumer.health() if embed_consumer else None,
            "search": search_consumer.health() if search_consumer else None,
        },
    }


# ── Recalculation ──


@app.post("/api/v1/recalculate/searches")
async def trigger_recalculation(
    limit: int = Query(20, ge=1, le=100),
    hours_old: int = Query(2, ge=1, le=168),
    _api_key: str = Depends(verify_api_key),
):
    """Manually trigger recalculation of completed searches."""
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(
            hours_old=hours_old, limit=limit
        )

        if not searches:
            return {"success": True, "message": "No searches eligible", "total": 0}

        for s in searches:
            s.status = 1  # TO_WORK
            s.worker_id = None

    return {
        "success": True,
        "message": f"Queued {len(searches)} searches for recalculation",
        "total": len(searches),
    }


# ── Entry Point ──


def main():
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8001,
        reload=settings.environment == "development",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
