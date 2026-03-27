"""Redis Streams producer — publishes events to input streams."""

import json
import logging

import redis

logger = logging.getLogger(__name__)


class StreamProducer:
    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str | None = None,
        redis_db: int = 3,
    ):
        self._redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password or None,
            db=redis_db,
            decode_responses=True,
        )

    def publish(self, stream: str, event_type: str, payload: dict):
        """Publish an event to a Redis Stream."""
        self._redis.xadd(
            stream,
            {
                "event_type": event_type,
                "payload": json.dumps(payload),
            },
        )
        logger.info(f"Published {event_type} to {stream}")
