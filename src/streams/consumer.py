"""Generic Redis Streams consumer with dead letter queue and message reclaim.

Runs XREADGROUP in a daemon thread, bridges messages to asyncio handlers.
Adopted from deepface-restapi pattern.
"""

import json
import logging
import socket
import threading
import time
from collections.abc import Callable

import redis

logger = logging.getLogger(__name__)


class StreamConsumer:
    def __init__(
        self,
        stream: str,
        group: str,
        consumer_name: str | None = None,
        redis_url: str | None = None,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str | None = None,
        redis_db: int = 3,
        block_ms: int = 5000,
        batch_size: int = 10,
        reclaim_idle_ms: int = 3_600_000,
        dead_letter_max_retries: int = 3,
        concurrency: int = 1,
    ):
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name or f"{socket.gethostname()}-{id(self)}"
        self.block_ms = block_ms
        self.batch_size = batch_size
        self.reclaim_idle_ms = reclaim_idle_ms
        self.dead_letter_max_retries = dead_letter_max_retries
        self._concurrency = concurrency

        self._handlers: dict[str, Callable] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_reclaim_time = 0.0
        self._reclaim_interval = 300  # 5 minutes

        # Stats
        self._messages_processed = 0
        self._messages_failed = 0
        self._messages_dead_lettered = 0

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

    def register_handler(self, event_type: str, handler: Callable):
        """Register a handler for a specific event_type."""
        self._handlers[event_type] = handler
        logger.info(f"Registered handler for '{event_type}' on stream '{self.stream}'")

    def start(self):
        """Start the consumer in a daemon thread."""
        self._ensure_group()
        self._running = True
        self._thread = threading.Thread(
            target=self._consume_loop,
            name=f"stream-consumer-{self.stream}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"StreamConsumer started: stream={self.stream} group={self.group} "
            f"consumer={self.consumer_name}"
        )

    def stop(self):
        """Stop the consumer."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info(f"StreamConsumer stopped: stream={self.stream}")

    def _ensure_group(self):
        """Create consumer group if it doesn't exist."""
        try:
            self._redis.xgroup_create(self.stream, self.group, id="$", mkstream=True)
            logger.info(f"Created consumer group '{self.group}' on '{self.stream}'")
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug(f"Consumer group '{self.group}' already exists")
            else:
                raise

    def _consume_loop(self):
        """Main consumption loop — runs in daemon thread."""
        while self._running:
            try:
                messages = self._redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer_name,
                    streams={self.stream: ">"},
                    count=self.batch_size,
                    block=self.block_ms,
                )

                if messages:
                    for _stream_name, stream_messages in messages:
                        for message_id, fields in stream_messages:
                            self._process_message(message_id, fields)

                self._maybe_reclaim()

            except redis.ConnectionError as e:
                logger.error(f"Redis connection lost: {e}. Retrying in 5s...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Consumer loop error: {e}")
                time.sleep(1)

    def _process_message(self, message_id: str, fields: dict):
        """Dispatch message to registered handler."""
        event_type = fields.get("event_type", "unknown")
        handler = self._handlers.get(event_type)

        if not handler:
            logger.warning(
                f"No handler for event_type='{event_type}' on stream '{self.stream}'. "
                f"ACKing to avoid redelivery."
            )
            self._ack(message_id)
            return

        try:
            payload_raw = fields.get("payload", "{}")
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            handler(event_type, payload, message_id)
            self._ack(message_id)
            self._messages_processed += 1
        except Exception as e:
            self._messages_failed += 1
            logger.error(
                f"Handler failed for {event_type} msg={message_id}: {e}",
                exc_info=True,
            )
            # Don't ACK — message stays in PEL for reclaim/dead letter

    def _ack(self, message_id: str):
        try:
            self._redis.xack(self.stream, self.group, message_id)
        except Exception as e:
            logger.error(f"Failed to ACK {message_id}: {e}")

    def _maybe_reclaim(self):
        """Periodically reclaim stale messages and dead-letter failures."""
        now = time.time()
        if now - self._last_reclaim_time < self._reclaim_interval:
            return
        self._last_reclaim_time = now

        try:
            pending = self._redis.xpending_range(
                self.stream,
                self.group,
                min="-",
                max="+",
                count=self.batch_size,
                idle=self.reclaim_idle_ms,
            )

            for entry in pending:
                msg_id = entry["message_id"]
                delivery_count = entry["times_delivered"]

                if delivery_count >= self.dead_letter_max_retries:
                    self._dead_letter(msg_id)
                    continue

                # Reclaim
                claimed = self._redis.xclaim(
                    self.stream,
                    self.group,
                    self.consumer_name,
                    min_idle_time=self.reclaim_idle_ms,
                    message_ids=[msg_id],
                )
                for claimed_id, claimed_fields in claimed:
                    logger.info(f"Reclaimed message {claimed_id} (delivery #{delivery_count + 1})")
                    self._process_message(claimed_id, claimed_fields)

        except Exception as e:
            logger.error(f"Reclaim error: {e}")

    def _dead_letter(self, message_id: str):
        """Move message to dead letter stream after max retries."""
        dlq_stream = f"{self.stream}:dead"
        try:
            msgs = self._redis.xrange(self.stream, min=message_id, max=message_id)
            if msgs:
                _, fields = msgs[0]
                self._redis.xadd(
                    dlq_stream,
                    {
                        **fields,
                        "original_stream": self.stream,
                        "original_message_id": message_id,
                        "dead_lettered_at": str(int(time.time())),
                    },
                )
            self._ack(message_id)
            self._messages_dead_lettered += 1
            logger.warning(f"Dead-lettered message {message_id} → {dlq_stream}")
        except Exception as e:
            logger.error(f"Dead letter failed for {message_id}: {e}")

    def health(self) -> dict:
        return {
            "stream": self.stream,
            "group": self.group,
            "consumer": self.consumer_name,
            "running": self._running,
            "thread_alive": self._thread.is_alive() if self._thread else False,
            "messages_processed": self._messages_processed,
            "messages_failed": self._messages_failed,
            "messages_dead_lettered": self._messages_dead_lettered,
        }
