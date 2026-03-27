"""ARQ worker settings — storage workers only, no CLIP."""

import logging
from typing import Any, Dict

from arq.connections import RedisSettings

from ..infrastructure.config import get_settings
from .embedding_worker import (
    initialize_worker,
    cleanup_worker,
    process_evidence_embeddings_batch,
)
from .search_worker import process_image_searches_batch

logger = logging.getLogger(__name__)
settings = get_settings()


def get_redis_settings() -> RedisSettings:
    return RedisSettings(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        database=settings.redis_database,
        conn_timeout=30,
        conn_retries=5,
        conn_retry_delay=2,
    )


async def startup(ctx: Dict[str, Any]) -> None:
    """Initialize Qdrant client on worker startup. No CLIP model needed."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    await initialize_worker()
    logger.info("ARQ Storage Worker ready (Qdrant connected, no GPU)")


async def shutdown(ctx: Dict[str, Any]) -> None:
    await cleanup_worker()
    logger.info("ARQ Storage Worker shut down")


class WorkerSettings:
    functions = [
        process_evidence_embeddings_batch,
        process_image_searches_batch,
    ]

    redis_settings = get_redis_settings()
    max_jobs = settings.worker_concurrency
    job_timeout = 600
    keep_result = 3600
    max_tries = 3
    retry_delay = 5
    poll_delay = 0.5          # Check for new jobs every 0.5s (was effectively 30s)
    health_check_interval = 30

    on_startup = startup
    on_shutdown = shutdown
