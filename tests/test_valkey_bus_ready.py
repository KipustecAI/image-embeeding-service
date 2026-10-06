"""Readiness for the platform Valkey bus.

ACL user in the URL, MAXLEN on every XADD (dead letters included), OOM retried
while other server errors fail at once, the 6-hour DW caps, and the float16
vector reader (backlog B13). Pure unit tests — no Redis, no database.
"""

import base64
from unittest.mock import MagicMock

import numpy as np
import pytest
import redis

from src.infrastructure.config import Settings
from src.streams import embedding_results_consumer as erc
from src.streams.consumer import StreamConsumer
from src.streams.embedding_results_consumer import decode_vector
from src.streams.producer import DEFAULT_MAXLEN, StreamProducer

OOM = "OOM command not allowed when used memory > 'maxmemory'."
NOPERM = "NOPERM this user has no permissions to access the 'x' key"


def _settings(**env) -> Settings:
    return Settings(REDIS_HOST="bus", REDIS_PORT=6380, REDIS_STREAMS_DB=0, **env)


# ── connection ──────────────────────────────────────────────────────────────


def test_bus_url_carries_the_service_user_and_encodes_the_password():
    s = _settings(REDIS_USERNAME="ms-embedding-api", REDIS_PASSWORD="p@ss:w/rd")
    assert s.redis_streams_url == "redis://ms-embedding-api:p%40ss%3Aw%2Frd@bus:6380/0"


def test_without_a_username_the_url_keeps_the_default_user_form():
    assert _settings(REDIS_PASSWORD="secret").redis_streams_url == "redis://:secret@bus:6380/0"


def test_without_a_password_the_url_has_no_credentials():
    assert _settings().redis_streams_url == "redis://bus:6380/0"


def test_the_url_reaches_redis_py_as_user_and_password():
    s = _settings(REDIS_USERNAME="ms-embedding-api", REDIS_PASSWORD="p@ss:w/rd")
    kwargs = StreamProducer(redis_url=s.redis_streams_url)._redis.connection_pool.connection_kwargs
    assert (kwargs["username"], kwargs["password"], kwargs["db"]) == (
        "ms-embedding-api",
        "p@ss:w/rd",
        0,
    )
    # Control: no username → redis-py sends AUTH <password> (default user), as today.
    kwargs = StreamProducer(
        redis_url=_settings(REDIS_PASSWORD="x").redis_streams_url
    )._redis.connection_pool.connection_kwargs
    assert kwargs.get("username") is None


# ── MAXLEN ──────────────────────────────────────────────────────────────────


def _producer(xadd_side_effect=None) -> StreamProducer:
    producer = StreamProducer(redis_url="redis://bus:6380/0", oom_retry_delay=0)
    producer._redis = MagicMock()
    producer._redis.xadd.side_effect = xadd_side_effect
    return producer


def test_publish_without_maxlen_still_caps_the_stream():
    producer = _producer()
    producer.publish("weapons:detected", "weapons.detected", {"a": 1})
    kwargs = producer._redis.xadd.call_args.kwargs
    assert kwargs == {"maxlen": DEFAULT_MAXLEN, "approximate": True}


def test_publish_uses_the_callers_maxlen():
    producer = _producer()
    producer.publish("image_embedding:raw", "x", {}, maxlen=50_000)
    assert producer._redis.xadd.call_args.kwargs["maxlen"] == 50_000


def test_dead_letters_are_written_with_a_cap():
    consumer = StreamConsumer(stream="embeddings:results", group="g", redis_url="redis://bus/0")
    consumer._redis = MagicMock()
    consumer._redis.xrange.return_value = [("1-0", {"event_type": "x", "payload": "{}"})]
    consumer._dead_letter("1-0")
    call = consumer._redis.xadd.call_args
    assert call.args[0] == "embeddings:results:dead"
    assert call.kwargs == {"maxlen": 1_000, "approximate": True}


def test_new_stream_caps_default_to_10k_and_dead_letters_to_1k():
    s = _settings()
    assert s.stream_evidence_search_maxlen == 10_000
    assert s.stream_reports_weapons_detected_maxlen == 10_000
    assert s.stream_reports_image_blacklist_match_maxlen == 10_000
    assert s.stream_image_index_maxlen == 10_000
    assert s.stream_dead_letter_maxlen == 1_000


def test_dw_embed_caps_follow_the_six_hour_rule():
    s = _settings()
    assert s.dw_maxlen_image_embedding_request == 13_000
    assert s.dw_maxlen_image_embedding == 50_000
    # Control: the env still overrides them (planned backfill).
    assert _settings(DW_MAXLEN_IMAGE_EMBEDDING=2_000_000).dw_maxlen_image_embedding == 2_000_000


# ── OOM ─────────────────────────────────────────────────────────────────────


def test_oom_from_the_bus_is_retried():
    producer = _producer([redis.ResponseError(OOM), "1-0"])
    producer.publish("evidence:search", "search.created", {})
    assert producer._redis.xadd.call_count == 2


def test_oom_that_persists_is_raised_after_the_retry_budget():
    producer = _producer(redis.ResponseError(OOM))
    with pytest.raises(redis.ResponseError, match="OOM"):
        producer.publish("evidence:search", "search.created", {})
    assert producer._redis.xadd.call_count == 1 + 3


def test_other_server_errors_are_not_retried():
    producer = _producer(redis.ResponseError(NOPERM))
    with pytest.raises(redis.ResponseError, match="NOPERM"):
        producer.publish("evidence:search", "search.created", {})
    assert producer._redis.xadd.call_count == 1


# ── float16 vectors (B13) ───────────────────────────────────────────────────


def _f16(values: np.ndarray) -> str:
    return base64.b64encode(values.astype("<f2").tobytes()).decode()


def test_json_list_vectors_are_read_as_today():
    vector = decode_vector({}, [0.5, -0.25, 1.0])
    assert vector.dtype == np.float32
    assert vector.tolist() == [0.5, -0.25, 1.0]


def test_f16_vectors_are_decoded_to_float32():
    original = np.linspace(-1, 1, 512, dtype=np.float32)
    payload = {"vector_encoding": "f16-b64-le", "vector_dim": 512}
    vector = decode_vector(payload, _f16(original))
    assert vector.dtype == np.float32 and vector.shape == (512,)
    np.testing.assert_allclose(vector, original, atol=1e-3)
    # Control: reading the same bytes as float32 would be wrong (half the length).
    wrong = np.frombuffer(base64.b64decode(_f16(original)), dtype="<f4")
    assert wrong.shape != (512,)


def test_f16_vector_dim_defaults_to_512():
    assert decode_vector({"vector_encoding": "f16-b64-le"}, _f16(np.ones(512))).shape == (512,)


def test_f16_vector_of_the_wrong_length_is_rejected():
    with pytest.raises(ValueError, match="expected 512"):
        decode_vector({"vector_encoding": "f16-b64-le"}, _f16(np.ones(256)))


def test_unknown_encoding_is_rejected():
    with pytest.raises(ValueError, match="unknown vector_encoding"):
        decode_vector({"vector_encoding": "bf16-b64"}, "AAAA")


async def test_unknown_encoding_raises_before_any_side_effect(monkeypatch):
    """The handler must raise (message stays pending), not write an error row and ACK."""
    get_session = MagicMock(side_effect=AssertionError("database touched"))
    monkeypatch.setattr(erc, "get_session", get_session)
    payload = {
        "evidence_id": "ev-1",
        "vector_encoding": "bf16-b64",
        "embeddings": [{"vector": "AAAA", "image_name": "a.jpg"}],
    }
    with pytest.raises(ValueError, match="unknown vector_encoding"):
        await erc._process_embeddings_result(payload, "1-0")
    get_session.assert_not_called()
