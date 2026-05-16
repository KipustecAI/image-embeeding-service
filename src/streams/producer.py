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

    def publish(
        self,
        stream: str,
        event_type: str,
        payload: dict,
        maxlen: int | None = None,
    ):
        """Publish an event to a Redis Stream.

        When ``maxlen`` is set, the XADD uses approximate trimming
        (``XADD ... MAXLEN ~ N``) — Redis trims to roughly N entries
        without scanning the full stream. Required for high-cardinality
        DW streams; existing report-generation publishers (weapons,
        blacklist_match) leave it None and rely on Redis' default
        retention.

        See docs/requirements/LOOKIA_DW_STREAMS.md §2 + §4.x for the
        MAXLEN sizing per stream.
        """
        kwargs: dict = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = True
        self._redis.xadd(
            stream,
            {
                "event_type": event_type,
                "payload": json.dumps(payload, default=str),
            },
            **kwargs,
        )
        logger.info(f"Published {event_type} to {stream}")
