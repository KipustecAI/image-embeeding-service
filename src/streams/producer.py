"""Redis Streams producer — publishes events to input streams."""

import json
import logging
import time

import redis

logger = logging.getLogger(__name__)

# The platform bus requires MAXLEN on every XADD; a caller that passes none
# gets this cap instead of an unbounded stream.
DEFAULT_MAXLEN = 10_000


def _is_oom(err: redis.ResponseError) -> bool:
    return str(err).startswith("OOM")


class StreamProducer:
    def __init__(
        self,
        redis_url: str | None = None,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str | None = None,
        redis_db: int = 3,
        oom_max_retries: int = 3,
        oom_retry_delay: float = 0.5,
    ):
        if redis_url:
            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        else:
            self._redis = redis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password or None,
                db=redis_db,
                decode_responses=True,
            )
        self._oom_max_retries = oom_max_retries
        self._oom_retry_delay = oom_retry_delay

    def publish(
        self,
        stream: str,
        event_type: str,
        payload: dict,
        maxlen: int | None = None,
    ):
        """Publish an event to a Redis Stream.

        Every XADD uses approximate trimming (``XADD ... MAXLEN ~ N``): the
        caller's ``maxlen``, or ``DEFAULT_MAXLEN`` when it passes none.

        The platform bus answers ``OOM`` to every write while an instance is at
        its memory ceiling; that is retried with exponential backoff. Any other
        server error (NOPERM, WRONGTYPE...) is raised at once.

        See docs/requirements/LOOKIA_DW_STREAMS.md §2 + §4.x for the
        MAXLEN sizing per stream.
        """
        fields = {
            "event_type": event_type,
            "payload": json.dumps(payload, default=str),
        }
        cap = maxlen if maxlen is not None else DEFAULT_MAXLEN
        attempt = 0
        while True:
            try:
                self._redis.xadd(stream, fields, maxlen=cap, approximate=True)
                break
            except redis.ResponseError as err:
                if not _is_oom(err) or attempt >= self._oom_max_retries:
                    raise
                delay = self._oom_retry_delay * (2**attempt)
                attempt += 1
                logger.warning(
                    "OOM publishing %s to %s; retry %d/%d in %.1fs",
                    event_type,
                    stream,
                    attempt,
                    self._oom_max_retries,
                    delay,
                )
                time.sleep(delay)
        logger.info(f"Published {event_type} to {stream}")
