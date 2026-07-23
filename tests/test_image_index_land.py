"""Phase-3 land-logic unit tests — pure / mocked (no live infra).

Covers `ImageIndexService.land_computed` + `mark_error_from_compute_error`
against the FROZEN compute wire (docs/requirements/IMAGE_INDEX_COMPUTE.md):

  * vector_b64 bit-exact decode round-trip (the frozen `<f4` decode);
  * unknown vector_encoding RAISES (dead-letter, never a silent misparse);
  * mixed batch (embedded + download_failed) → completed_with_errors;
  * all-embedded → completed, ONE batched Qdrant upsert;
  * unaccounted (fewer results than submitted) → stays 'computing', None return
    (no terminal publish), but the vectors we DID get are still indexed;
  * land is idempotent (re-land → same terminal, deterministic point-ids);
  * never-minted / unparseable batch_id → None (ACK no-op);
  * compute.error → 'error' + failed payload, and the entity_id-shaped payload
    the LIVE consumers use does NOT bind here (contract-shape divergence).

Session / repo / vector_repo are all mocked; DB/Qdrant/Redis round-trips are
deferred-to-human.
"""

import base64
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.db.models.constants import ImageIndexBatchStatus
from src.db.models.image_index import ImageIndexBatch
from src.infrastructure.vector_db.image_index_vector_repository import (
    image_index_point_id,
)
from src.services.image_index_service import ImageIndexService

BATCH_ID = "49c7861d-0000-0000-0000-000000000001"

_SVC = "src.services.image_index_service.ImageIndexRepository"


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_batch(submitted: int = 2, **overrides) -> ImageIndexBatch:
    """Transient ORM batch (no session) — real attribute reads for the payload."""
    b = ImageIndexBatch(
        external_id="run-42",
        client_batch_ref="dwoff-run42-b0",
        user_id="tenant-1",
        status=ImageIndexBatchStatus.COMPUTING,
        source_ref="run-42/Vehiculo",
    )
    b.id = BATCH_ID
    b.submitted_count = submitted
    b.embedded_count = 0
    b.filtered_count = 0
    b.failed_count = 0
    b.created_at = datetime(2026, 7, 22, 12, 0, 0)
    b.completed_at = None
    b.error_message = None
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def _vec_b64(values) -> str:
    """Encode a float sequence as the frozen f32le base64 buffer."""
    return base64.b64encode(np.asarray(values, dtype="<f4").tobytes()).decode()


def _embedded_item(item_index: int, values, item_id: str | None = None) -> dict:
    return {
        "item_id": item_id or f"c{item_index}",
        "item_index": item_index,
        "status": "embedded",
        "vector_b64": _vec_b64(values),
        "vector_encoding": "f32le_b64",
        "embedding_dim": 512,
        "duplicate_of_index": None,
        "error_message": None,
    }


async def _land(payload, batch, counts, *, vector_ok: bool = True):
    """Run land_computed with a mocked session + repo + vector_repo."""
    session = MagicMock()
    session.get = AsyncMock(return_value=batch)
    vector_repo = MagicMock()
    vector_repo.upsert_items = AsyncMock(return_value=vector_ok)
    repo = MagicMock()
    repo.upsert_result = AsyncMock()
    repo.recompute_counts = AsyncMock(return_value=counts)
    with patch(_SVC, return_value=repo):
        result = await ImageIndexService().land_computed(
            session, payload, vector_repo=vector_repo
        )
    return result, repo, vector_repo, session


# ── decode round-trip ────────────────────────────────────────────────────────


async def test_decode_vector_b64_bit_exact():
    original = [i * 0.01 for i in range(512)]
    payload = {"batch_id": BATCH_ID, "results": [_embedded_item(0, original)]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}

    _, _, vector_repo, _ = await _land(payload, _make_batch(submitted=1), counts)

    points = vector_repo.upsert_items.call_args.args[0]
    assert len(points) == 1
    # Bit-exact through the float32 round-trip (compare against the same cast).
    expected = np.asarray(original, dtype="<f4").tolist()
    assert points[0]["vector"] == expected
    assert len(points[0]["vector"]) == 512
    # Deterministic point-id echoed into the point payload keys.
    assert points[0]["item_index"] == 0
    assert points[0]["batch_id"] == BATCH_ID
    assert points[0]["user_id"] == "tenant-1"
    assert points[0]["external_id"] == "run-42"


async def test_pure_decode_helper_is_bit_exact():
    values = [0.0, 1.5, -2.25, 3.1400001, 1e-7]
    padded = values + [0.0] * (512 - len(values))
    decoded = ImageIndexService._decode_vector_b64(
        _vec_b64(padded), batch_uuid=BATCH_ID, item_index=0
    )
    assert decoded == np.asarray(padded, dtype="<f4").tolist()


async def test_decode_helper_dead_letters_bad_buffers():
    """Contract-boundary validation raises a labeled ValueError (dead-letter)."""
    import base64

    # null buffer (embedded item that violated 'vector_b64 set iff embedded')
    with pytest.raises(ValueError, match="null vector_b64"):
        ImageIndexService._decode_vector_b64(None, batch_uuid=BATCH_ID, item_index=0)
    # non-multiple-of-4 byte buffer
    with pytest.raises(ValueError, match="whole number"):
        ImageIndexService._decode_vector_b64(
            base64.b64encode(b"abc").decode(), batch_uuid=BATCH_ID, item_index=1
        )
    # wrong dimension (10 floats, not 512)
    with pytest.raises(ValueError, match="dim 10"):
        ImageIndexService._decode_vector_b64(
            base64.b64encode(np.zeros(10, dtype="<f4").tobytes()).decode(),
            batch_uuid=BATCH_ID,
            item_index=2,
        )


# ── unknown encoding dead-letters ────────────────────────────────────────────


async def test_unknown_vector_encoding_raises():
    item = _embedded_item(0, [1.0] * 512)
    item["vector_encoding"] = "f32be_b64"  # unknown → dead-letter
    payload = {"batch_id": BATCH_ID, "results": [item]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}
    with pytest.raises(ValueError, match="unknown vector_encoding"):
        await _land(payload, _make_batch(submitted=1), counts)


async def test_missing_vector_encoding_raises():
    item = _embedded_item(0, [1.0] * 512)
    del item["vector_encoding"]  # absent → treated as unknown, dead-letter
    payload = {"batch_id": BATCH_ID, "results": [item]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}
    with pytest.raises(ValueError, match="unknown vector_encoding"):
        await _land(payload, _make_batch(submitted=1), counts)


# ── terminal-from-counts ─────────────────────────────────────────────────────


async def test_mixed_batch_completed_with_errors():
    payload = {
        "batch_id": BATCH_ID,
        "results": [
            _embedded_item(0, [0.5] * 512),
            {
                "item_id": "c1",
                "item_index": 1,
                "status": "download_failed",
                "vector_b64": None,
                "vector_encoding": "f32le_b64",
                "embedding_dim": 512,
                "duplicate_of_index": None,
                "error_message": "http 404",
            },
        ],
    }
    counts = {"submitted": 2, "embedded": 1, "filtered": 0, "failed": 1}

    result, repo, vector_repo, _ = await _land(payload, _make_batch(submitted=2), counts)

    assert result is not None
    assert result["status"] == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS
    assert result["counts"] == counts
    assert result["completed_at"] is not None
    # Both rows upserted; only the embedded item goes to Qdrant.
    assert repo.upsert_result.await_count == 2
    points = vector_repo.upsert_items.call_args.args[0]
    assert len(points) == 1 and points[0]["item_index"] == 0


async def test_all_embedded_completed_single_batched_upsert():
    payload = {
        "batch_id": BATCH_ID,
        "results": [_embedded_item(i, [0.1 * i] * 512) for i in range(3)],
    }
    counts = {"submitted": 3, "embedded": 3, "filtered": 0, "failed": 0}

    result, repo, vector_repo, _ = await _land(payload, _make_batch(submitted=3), counts)

    assert result["status"] == ImageIndexBatchStatus.COMPLETED
    assert result["counts"] == counts
    # ONE batched Qdrant call carrying all three points.
    assert vector_repo.upsert_items.await_count == 1
    assert len(vector_repo.upsert_items.call_args.args[0]) == 3


async def test_unaccounted_stays_computing_no_terminal():
    payload = {"batch_id": BATCH_ID, "results": [_embedded_item(0, [0.2] * 512)]}
    counts = {"submitted": 3, "embedded": 1, "filtered": 0, "failed": 0}
    batch = _make_batch(submitted=3)

    result, repo, vector_repo, _ = await _land(payload, batch, counts)

    # terminal_status(counts) is None → no lifecycle publish.
    assert result is None
    assert batch.status == ImageIndexBatchStatus.COMPUTING  # left untouched
    assert batch.completed_at is None
    # But the one vector we DID receive is still indexed.
    assert vector_repo.upsert_items.await_count == 1
    assert repo.upsert_result.await_count == 1


# ── idempotency ──────────────────────────────────────────────────────────────


async def test_land_idempotent_reland_stable():
    payload = {
        "batch_id": BATCH_ID,
        "results": [_embedded_item(0, [0.3] * 512), _embedded_item(1, [0.4] * 512)],
    }
    counts = {"submitted": 2, "embedded": 2, "filtered": 0, "failed": 0}

    r1, repo1, _, _ = await _land(payload, _make_batch(submitted=2), counts)
    r2, repo2, vr2, _ = await _land(payload, _make_batch(submitted=2), counts)

    # Same terminal disposition + counts across a redelivery.
    assert r1["status"] == r2["status"] == ImageIndexBatchStatus.COMPLETED
    assert r1["counts"] == r2["counts"] == counts
    assert r1["batch_id"] == r2["batch_id"] == BATCH_ID
    # Each land re-upserts every row in place (overwrite, not double-count).
    assert repo1.upsert_result.await_count == 2
    assert repo2.upsert_result.await_count == 2
    # Deterministic point-ids: the reference row would carry these exact ids.
    points = vr2.upsert_items.call_args.args[0]
    assert {p["item_index"] for p in points} == {0, 1}
    assert image_index_point_id(BATCH_ID, 0) != image_index_point_id(BATCH_ID, 1)


async def test_v11_image_url_echo_persisted_as_source_url():
    """v1.1: compute echoes `image_url`; we persist it as source_url (DB + Qdrant)."""
    item = _embedded_item(0, [0.5] * 512)
    item["image_url"] = "https://storage.lookia.mx/crops/run/kept_0.png"
    payload = {"batch_id": BATCH_ID, "results": [item]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}
    _, repo, vector_repo, _ = await _land(payload, _make_batch(submitted=1), counts)
    assert repo.upsert_result.call_args.kwargs["source_url"] == item["image_url"]
    point = vector_repo.upsert_items.call_args.args[0][0]
    assert point["source_url"] == item["image_url"]


async def test_v11_empty_image_url_persists_as_none():
    """Compute sends `image_url=""` when the input omitted it → we store None."""
    item = _embedded_item(0, [0.5] * 512)
    item["image_url"] = ""
    payload = {"batch_id": BATCH_ID, "results": [item]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}
    _, repo, _, _ = await _land(payload, _make_batch(submitted=1), counts)
    assert repo.upsert_result.call_args.kwargs["source_url"] is None


async def test_reference_row_gets_deterministic_point_id():
    payload = {"batch_id": BATCH_ID, "results": [_embedded_item(2, [0.9] * 512)]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}

    _, repo, _, _ = await _land(payload, _make_batch(submitted=1), counts)

    kwargs = repo.upsert_result.call_args.kwargs
    assert kwargs["qdrant_point_id"] == image_index_point_id(BATCH_ID, 2)
    assert kwargs["status"] == "embedded"


async def test_failed_row_stores_no_point_id():
    payload = {
        "batch_id": BATCH_ID,
        "results": [
            {
                "item_id": "c0",
                "item_index": 0,
                "status": "no_result",
                "vector_b64": None,
                "vector_encoding": "f32le_b64",
                "embedding_dim": 512,
                "duplicate_of_index": None,
                "error_message": "no vector",
            }
        ],
    }
    counts = {"submitted": 1, "embedded": 0, "filtered": 0, "failed": 1}

    result, repo, vector_repo, _ = await _land(payload, _make_batch(submitted=1), counts)

    kwargs = repo.upsert_result.call_args.kwargs
    assert kwargs["qdrant_point_id"] is None
    assert kwargs["error_message"] == "no vector"
    vector_repo.upsert_items.assert_not_awaited()  # nothing to index
    assert result["status"] == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS


# ── never-minted / unparseable ───────────────────────────────────────────────


async def test_never_minted_batch_returns_none():
    payload = {"batch_id": BATCH_ID, "results": [_embedded_item(0, [1.0] * 512)]}
    session = MagicMock()
    session.get = AsyncMock(return_value=None)  # never minted
    vector_repo = MagicMock()
    vector_repo.upsert_items = AsyncMock(return_value=True)
    repo = MagicMock()
    repo.upsert_result = AsyncMock()
    repo.recompute_counts = AsyncMock()
    with patch(_SVC, return_value=repo):
        result = await ImageIndexService().land_computed(
            session, payload, vector_repo=vector_repo
        )
    assert result is None
    repo.upsert_result.assert_not_awaited()
    repo.recompute_counts.assert_not_awaited()
    vector_repo.upsert_items.assert_not_awaited()


async def test_unparseable_batch_id_returns_none():
    payload = {"batch_id": "not-a-uuid", "results": []}
    session = MagicMock()
    session.get = AsyncMock()
    vector_repo = MagicMock()
    vector_repo.upsert_items = AsyncMock()
    with patch(_SVC, return_value=MagicMock()):
        result = await ImageIndexService().land_computed(
            session, payload, vector_repo=vector_repo
        )
    assert result is None
    session.get.assert_not_awaited()


async def test_qdrant_upsert_failure_raises():
    payload = {"batch_id": BATCH_ID, "results": [_embedded_item(0, [1.0] * 512)]}
    counts = {"submitted": 1, "embedded": 1, "filtered": 0, "failed": 0}
    with pytest.raises(RuntimeError, match="Qdrant upsert_items failed"):
        await _land(payload, _make_batch(submitted=1), counts, vector_ok=False)


# ── compute.error (OUR contract shape) ───────────────────────────────────────


async def test_mark_error_from_compute_error_terminalizes():
    batch = _make_batch(submitted=5)
    session = MagicMock()
    session.get = AsyncMock(return_value=batch)
    session.flush = AsyncMock()
    payload = {"batch_id": BATCH_ID, "error_message": "gpu oom"}

    result = await ImageIndexService().mark_error_from_compute_error(session, payload)

    assert result["status"] == ImageIndexBatchStatus.ERROR
    assert result["error_message"] == "gpu oom"
    assert batch.status == ImageIndexBatchStatus.ERROR
    assert batch.completed_at is not None
    session.flush.assert_awaited_once()


async def test_mark_error_truncates_long_message():
    batch = _make_batch(submitted=1)
    session = MagicMock()
    session.get = AsyncMock(return_value=batch)
    session.flush = AsyncMock()
    payload = {"batch_id": BATCH_ID, "error_message": "x" * 5000}

    result = await ImageIndexService().mark_error_from_compute_error(session, payload)

    assert len(result["error_message"]) == ImageIndexService.ERROR_MESSAGE_MAX


async def test_mark_error_never_minted_returns_none():
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    result = await ImageIndexService().mark_error_from_compute_error(
        session, {"batch_id": BATCH_ID, "error_message": "x"}
    )
    assert result is None
    session.flush.assert_not_awaited()


async def test_mark_error_ignores_live_entity_id_shape():
    """A live-consumer-shaped payload (entity_id, NO batch_id) must not bind —
    documents the intentional contract-shape divergence (module docstring)."""
    session = MagicMock()
    session.get = AsyncMock()
    session.flush = AsyncMock()
    payload = {"entity_id": BATCH_ID, "entity_type": "batch", "error_message": "x"}
    result = await ImageIndexService().mark_error_from_compute_error(session, payload)
    assert result is None  # batch_id missing → unparseable → ACK no-op
    session.get.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
