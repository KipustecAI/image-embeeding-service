"""Image Embedding Backend — pipeline orchestration, storage, and API.

Consumes pre-computed vectors from the GPU compute service via Redis Streams.
No CLIP model, no GPU dependency.
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
from arq import create_pool
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.api.dependencies import verify_api_key
from src.db.repositories import EmbeddingRequestRepository, SearchRequestRepository
from src.infrastructure.config import get_settings
from src.infrastructure.database import engine, get_session
from src.services.batch_trigger import create_batch_trigger, get_batch_trigger
from src.services.safety_nets import (
    cleanup_old_requests,
    dispatch_embedding_batch,
    dispatch_search_batch,
    embedding_safety_net,
    recover_stale_working,
    recalculate_searches,
    search_safety_net,
    set_arq_pool,
)
from src.streams.embedding_results_consumer import (
    create_embedding_results_consumer,
    set_results_event_loop,
)
from src.streams.search_results_consumer import (
    create_search_results_consumer,
    set_search_results_event_loop,
)
from src.workers.main import get_redis_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Global references
arq_pool = None
scheduler = None
embed_consumer = None
search_consumer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool, scheduler, embed_consumer, search_consumer

    logger.info("Starting Image Embedding Service...")

    # 1. Verify database connection
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("PostgreSQL connected")

    # 2. Create ARQ pool
    arq_pool = await create_pool(get_redis_settings())
    set_arq_pool(arq_pool)
    logger.info("ARQ pool created")

    # 3. APScheduler — safety nets + periodic tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        embedding_safety_net,
        IntervalTrigger(seconds=60),
        id="embedding_safety_net",
        misfire_grace_time=30,
    )
    scheduler.add_job(
        search_safety_net,
        IntervalTrigger(seconds=120),
        id="search_safety_net",
        misfire_grace_time=60,
    )
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
    logger.info("Scheduler started (safety nets + periodic tasks)")

    # 4. Batch Triggers
    embedding_trigger = create_batch_trigger(
        name="embedding",
        batch_size=settings.batch_trigger_size,
        max_wait_seconds=settings.batch_trigger_max_wait,
        process_callback=dispatch_embedding_batch,
    )
    await embedding_trigger.start()

    search_trigger = create_batch_trigger(
        name="search",
        batch_size=settings.batch_trigger_search_size,
        max_wait_seconds=settings.batch_trigger_search_wait,
        process_callback=dispatch_search_batch,
    )
    await search_trigger.start()
    logger.info("Batch triggers started")

    # 5. Stream Consumers (consume output from GPU compute service)
    loop = asyncio.get_running_loop()
    set_results_event_loop(loop)
    set_search_results_event_loop(loop)

    embed_consumer = create_embedding_results_consumer()
    embed_consumer.start()

    search_consumer = create_search_results_consumer()
    search_consumer.start()
    logger.info("Stream consumers started (embeddings:results + search:results)")

    logger.info("Image Embedding Service started successfully")

    yield

    # ── SHUTDOWN ──
    logger.info("Shutting down...")

    if embed_consumer:
        embed_consumer.stop()
    if search_consumer:
        search_consumer.stop()

    for name in ("embedding", "search"):
        trigger = get_batch_trigger(name)
        if trigger:
            await trigger.stop()

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    if arq_pool:
        await arq_pool.close()

    await engine.dispose()
    logger.info("Shutdown complete")


# ── FastAPI App ──

app = FastAPI(
    title="Image Embedding Service",
    description="Event-driven microservice for CLIP image embedding and similarity search",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Health & Stats ──


@app.get("/")
async def root():
    return {
        "service": "Image Embedding Service",
        "version": "2.0.0",
        "status": "running",
        "environment": settings.environment,
    }


@app.get("/health")
async def health_check():
    components = {
        "database": False,
        "scheduler": scheduler.running if scheduler else False,
    }

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["database"] = True
    except Exception:
        pass

    embed_trigger = get_batch_trigger("embedding")
    search_trigger = get_batch_trigger("search")

    return {
        "status": "healthy" if all(components.values()) else "degraded",
        "components": components,
        "triggers": {
            "embedding": embed_trigger.health() if embed_trigger else None,
            "search": search_trigger.health() if search_trigger else None,
        },
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

    return {
        "service": "Image Embedding Service",
        "pipeline": {
            "embedding_requests": embed_counts,
            "search_requests": search_counts,
        },
        "configuration": {
            "model": settings.clip_model_name,
            "device": settings.clip_device,
            "vector_size": settings.qdrant_vector_size,
        },
    }


# ── Pipeline Status (used by E2E test scripts) ──


@app.get("/api/v1/pipeline/status")
async def pipeline_status():
    """Combined pipeline status: DB counts + trigger metrics + consumer health."""
    async with get_session() as session:
        embed_repo = EmbeddingRequestRepository(session)
        search_repo = SearchRequestRepository(session)
        embed_counts = await embed_repo.count_by_status()
        search_counts = await search_repo.count_by_status()

    embed_trigger = get_batch_trigger("embedding")
    search_trigger = get_batch_trigger("search")

    return {
        "status_counts": {
            "embedding": embed_counts,
            "search": search_counts,
        },
        "triggers": {
            "embedding": embed_trigger.health() if embed_trigger else None,
            "search": search_trigger.health() if search_trigger else None,
        },
        "consumers": {
            "embed": embed_consumer.health() if embed_consumer else None,
            "search": search_consumer.health() if search_consumer else None,
        },
    }


# ── Internal Trigger (cross-process notification) ──


@app.post("/api/v1/internal/trigger/{trigger_name}")
async def notify_trigger(trigger_name: str, count: int = 1):
    """Called by ARQ worker (separate process) to notify in-memory triggers."""
    trigger = get_batch_trigger(trigger_name)
    if not trigger:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger '{trigger_name}' not found",
        )
    await trigger.notify(count=count)
    return {"notified": True, "trigger": trigger_name, "count": count}


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

    trigger = get_batch_trigger("search")
    if trigger:
        await trigger.notify(count=len(searches))

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
