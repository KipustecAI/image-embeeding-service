"""Image Embedding Backend — pipeline orchestration, storage, and API.

Consumes pre-computed vectors from the GPU compute service via Redis Streams.
Stores directly in Qdrant + PostgreSQL — no ARQ queue for the happy path.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from src.api.dependencies import UserContext, get_user_context
from src.application.helpers.weapon_filters import build_weapon_filter_conditions
from src.db.repositories import EmbeddingRequestRepository, SearchRequestRepository
from src.infrastructure.config import get_settings
from src.infrastructure.database import engine, get_session
from src.infrastructure.vector_db.qdrant_repository import QdrantVectorRepository
from src.services.safety_nets import (
    cleanup_old_requests,
    recalculate_searches,
    recover_stale_working,
)
from src.services.safety_nets import (
    set_vector_repo as set_safety_nets_vector_repo,
)
from src.services.storage_uploader import StorageUploader
from src.streams.embedding_results_consumer import (
    create_embedding_results_consumer,
    set_results_event_loop,
    set_storage_uploader,
    set_vector_repo,
)
from src.streams.producer import StreamProducer
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
stream_producer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler, embed_consumer, search_consumer, vector_repo, stream_producer

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
    set_safety_nets_vector_repo(vector_repo)
    logger.info("Qdrant connected")

    # 2b. Storage uploader (for ZIP flow image uploads)
    storage_uploader = StorageUploader(base_url=settings.storage_service_url)
    set_storage_uploader(storage_uploader)
    logger.info(f"Storage uploader configured: {settings.storage_service_url}")

    # 3. Stream producer (for publishing search requests to GPU)
    stream_producer = StreamProducer(
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password or None,
        redis_db=settings.redis_streams_db,
    )
    logger.info("Stream producer ready")

    # 4. APScheduler — safety nets + periodic tasks
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


# ── Search API ──

STATUS_NAMES = {1: "pending", 2: "working", 3: "completed", 4: "error"}
SIMILARITY_NAMES = {1: "no_matches", 2: "matches_found"}


class SearchCreateRequest(BaseModel):
    image_url: str
    threshold: float = 0.75
    max_results: int = 50
    metadata: dict | None = None
    # Weapons filter — see docs/weapons/04_SEARCH_API.md
    weapons_filter: Literal["all", "only", "exclude", "analyzed_clean"] = "all"
    weapon_classes: list[str] | None = None


@app.post("/api/v1/search", status_code=202)
async def create_search(
    body: SearchCreateRequest,
    ctx: UserContext = Depends(get_user_context),
):
    """Submit a search — publishes to GPU compute stream, returns search_id."""
    search_id = str(__import__("uuid").uuid4())
    user_id = ctx.user_id
    metadata = body.metadata or {}

    # Multi-tenant: non-admin users can only search their own evidence
    if ctx.role not in ("admin", "root", "dev"):
        metadata["user_id"] = user_id

    # Weapons filter — stashed in metadata so it survives the GPU round-trip.
    # See docs/weapons/04_SEARCH_API.md.
    metadata["weapons_filter"] = body.weapons_filter
    if body.weapon_classes:
        if body.weapons_filter != "only":
            logger.warning(
                f"Search {search_id}: weapon_classes passed with "
                f"weapons_filter={body.weapons_filter!r} (only 'only' uses it) — ignoring classes"
            )
        else:
            metadata["weapon_classes"] = list(body.weapon_classes)

    # Create DB row (status=TO_WORK)
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        await repo.create_request(
            search_id=search_id,
            user_id=user_id,
            image_url=body.image_url,
            threshold=body.threshold,
            max_results=body.max_results,
            metadata=metadata,
        )

    # Publish to GPU compute stream
    stream_producer.publish(
        stream=settings.stream_evidence_search,
        event_type="search.created",
        payload={
            "search_id": search_id,
            "user_id": user_id,
            "image_url": body.image_url,
            "threshold": body.threshold,
            "max_results": body.max_results,
            "metadata": metadata,
        },
    )

    return {
        "search_id": search_id,
        "status": "pending",
        "message": f"Search submitted, check status at /api/v1/search/{search_id}",
    }


@app.get("/api/v1/search/{search_id}")
async def get_search(
    search_id: str,
    ctx: UserContext = Depends(get_user_context),
):
    """Get search status (no matches inline — use /matches endpoint for results)."""
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        request = await repo.get_by_search_id(search_id)

        if not request:
            raise HTTPException(status_code=404, detail="Search not found")

        return {
            "search_id": request.search_id,
            "user_id": request.user_id,
            "image_url": request.image_url,
            "status": STATUS_NAMES.get(request.status, "unknown"),
            "similarity_status": SIMILARITY_NAMES.get(request.similarity_status, "unknown"),
            "total_matches": request.total_matches,
            "threshold": request.threshold,
            "max_results": request.max_results,
            "created_at": request.created_at.isoformat() if request.created_at else None,
            "completed_at": request.processing_completed_at.isoformat()
            if request.processing_completed_at
            else None,
            "error": request.error_message,
        }


@app.get("/api/v1/search/{search_id}/matches")
async def get_search_matches(
    search_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx: UserContext = Depends(get_user_context),
):
    """Get paginated match results for a search, sorted by similarity score."""
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        request = await repo.get_by_search_id(search_id)

        if not request:
            raise HTTPException(status_code=404, detail="Search not found")

        matches = await repo.get_matches(request.id, limit=limit, offset=offset)
        total = await repo.count_matches(request.id)

        return {
            "search_id": search_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "matches": [m.to_dict() for m in matches],
        }


@app.get("/api/v1/search/user/{user_id}")
async def list_user_searches(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx: UserContext = Depends(get_user_context),
):
    """List all searches by a user, paginated, most recent first."""
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_by_user_id(user_id, limit=limit, offset=offset)
        total = await repo.count_by_user_id(user_id)

        return {
            "user_id": user_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "searches": [
                {
                    "search_id": s.search_id,
                    "status": STATUS_NAMES.get(s.status, "unknown"),
                    "similarity_status": SIMILARITY_NAMES.get(s.similarity_status, "unknown"),
                    "total_matches": s.total_matches,
                    "image_url": s.image_url,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in searches
            ],
        }


# ── Recalculation ──


@app.post("/api/v1/recalculate/searches")
async def trigger_recalculation(
    limit: int = Query(20, ge=1, le=100),
    hours_old: int = Query(2, ge=0, le=168),
    ctx: UserContext = Depends(get_user_context),
):
    """Manually trigger recalculation of completed searches using stored query vectors."""
    import numpy as np
    from sqlalchemy import delete as sa_delete

    from src.db.models.constants import SimilarityStatus
    from src.db.models.search_match import SearchMatch

    async with get_session() as session:
        repo = SearchRequestRepository(session)
        searches = await repo.get_for_recalculation(hours_old=hours_old, limit=limit)

        if not searches:
            return {"success": True, "message": "No searches eligible", "total": 0, "skipped": 0}

        recalculated = 0
        skipped = 0
        for s in searches:
            if not s.qdrant_query_point_id:
                skipped += 1
                continue

            query_vector_list = await vector_repo.retrieve_query_vector(s.qdrant_query_point_id)
            if query_vector_list is None:
                skipped += 1
                continue

            filter_conditions: dict = {}
            if s.search_metadata:
                filter_conditions = {
                    k: v
                    for k, v in s.search_metadata.items()
                    if k in ("camera_id", "object_type")
                }
                filter_conditions.update(build_weapon_filter_conditions(s.search_metadata))
            if not filter_conditions:
                filter_conditions = None

            matches = await vector_repo.search_similar(
                query_vector=np.array(query_vector_list, dtype=np.float32),
                limit=s.max_results,
                threshold=s.threshold,
                filter_conditions=filter_conditions,
            )

            # Delete old matches
            await session.execute(
                sa_delete(SearchMatch).where(SearchMatch.search_request_id == s.id)
            )

            for match in matches:
                session.add(
                    SearchMatch(
                        search_request_id=s.id,
                        evidence_id=str(match.evidence_id),
                        camera_id=str(match.camera_id) if match.camera_id else None,
                        similarity_score=match.similarity_score,
                        image_url=match.image_url,
                        match_metadata=match.metadata,
                    )
                )

            s.total_matches = len(matches)
            s.similarity_status = (
                SimilarityStatus.MATCHES_FOUND if matches else SimilarityStatus.NO_MATCHES
            )
            s.processing_completed_at = datetime.utcnow()
            recalculated += 1

    return {
        "success": True,
        "message": f"Recalculated {recalculated} searches ({skipped} skipped — no stored vector)",
        "total": recalculated,
        "skipped": skipped,
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
