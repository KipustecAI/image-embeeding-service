"""ARQ worker settings and lifecycle."""

import logging
from typing import Any, Dict

from arq import cron
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
        password=settings.redis_password,
        database=settings.redis_database,
        conn_timeout=30,
        conn_retries=5,
        conn_retry_delay=2,
    )


async def startup(ctx: Dict[str, Any]) -> None:
    """Pre-load CLIP model and Qdrant client on worker startup."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    await initialize_worker()
    logger.info(
        f"ARQ Worker ready | model={settings.clip_model_name} "
        f"device={settings.clip_device} vector_size={settings.qdrant_vector_size}"
    )


async def shutdown(ctx: Dict[str, Any]) -> None:
    await cleanup_worker()
    logger.info("ARQ Worker shut down")


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
    health_check_interval = 30

    on_startup = startup
    on_shutdown = shutdown
