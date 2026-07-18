"""Pure unit tests for the image-index Phase-1 foundations.

No database, no Redis, no Qdrant — the terminal-status truth table, the config
attribute-existence guard (00_DESIGN §2 / fix M4), and the lifecycle payload
builders. These are the load-bearing correctness invariants for the whole
feature (00_DESIGN §8).
"""

from datetime import datetime

import pytest

from src.db.models.constants import ImageIndexBatchStatus, ImageIndexResultStatus
from src.db.models.image_index import ImageIndexBatch
from src.infrastructure.config import Settings
from src.services.image_index_service import ImageIndexService

# ── terminal_status truth table (00_DESIGN §4/§8) ────────────────────────────


def _counts(submitted, embedded, filtered, failed):
    return {
        "submitted": submitted,
        "embedded": embedded,
        "filtered": filtered,
        "failed": failed,
    }


def test_terminal_unaccounted_stays_computing():
    """embedded+filtered+failed < submitted → None (stay computing, reaper backstop)."""
    assert ImageIndexService.terminal_status(_counts(3, 1, 0, 0)) is None


def test_terminal_zero_results_stays_computing():
    """Fresh batch, nothing landed yet → not terminal."""
    assert ImageIndexService.terminal_status(_counts(2, 0, 0, 0)) is None


def test_terminal_all_embedded_completed():
    """Fully accounted, no failures → completed."""
    assert (
        ImageIndexService.terminal_status(_counts(3, 3, 0, 0))
        == ImageIndexBatchStatus.COMPLETED
    )


def test_terminal_with_failure_completed_with_errors():
    """Fully accounted, at least one failure → completed_with_errors."""
    assert (
        ImageIndexService.terminal_status(_counts(3, 2, 0, 1))
        == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS
    )


def test_terminal_filtered_is_not_a_downgrade():
    """filtered counts toward accounting and is clean — no failures → completed."""
    assert (
        ImageIndexService.terminal_status(_counts(3, 1, 2, 0))
        == ImageIndexBatchStatus.COMPLETED
    )


def test_terminal_filtered_accounts_but_failure_downgrades():
    """filtered accounts; a real failure still downgrades to completed_with_errors."""
    assert (
        ImageIndexService.terminal_status(_counts(4, 1, 2, 1))
        == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS
    )


def test_terminal_all_failed_completed_with_errors():
    """Every item failed but fully accounted → completed_with_errors (never 'error')."""
    result = ImageIndexService.terminal_status(_counts(3, 0, 0, 3))
    assert result == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS
    # 'error' is NEVER derivable from item counts.
    assert result != ImageIndexBatchStatus.ERROR


def test_terminal_never_returns_error():
    """error is only a batch-level disposition — never from counts."""
    for c in [_counts(1, 0, 0, 1), _counts(2, 1, 1, 0), _counts(1, 1, 0, 0)]:
        assert ImageIndexService.terminal_status(c) != ImageIndexBatchStatus.ERROR


def test_terminal_empty_batch_completed():
    """submitted==0 (edge) → accounted (0>=0), no failures → completed."""
    assert (
        ImageIndexService.terminal_status(_counts(0, 0, 0, 0))
        == ImageIndexBatchStatus.COMPLETED
    )


def test_terminal_missing_keys_default_zero():
    """Missing keys default to 0 — {} → completed (0 accounted >= 0 submitted)."""
    assert ImageIndexService.terminal_status({}) == ImageIndexBatchStatus.COMPLETED


# ── FAILED_SET folds the three failure dispositions ──────────────────────────


def test_failed_set_membership():
    assert ImageIndexResultStatus.DOWNLOAD_FAILED in ImageIndexResultStatus.FAILED_SET
    assert ImageIndexResultStatus.DECODE_FAILED in ImageIndexResultStatus.FAILED_SET
    assert ImageIndexResultStatus.NO_RESULT in ImageIndexResultStatus.FAILED_SET
    assert ImageIndexResultStatus.EMBEDDED not in ImageIndexResultStatus.FAILED_SET
    assert ImageIndexResultStatus.FILTERED not in ImageIndexResultStatus.FAILED_SET


# ── Config attribute-existence guard (00_DESIGN §2, fix M4) ──────────────────


def test_settings_image_index_attributes_exist():
    """Every attribute the consumers/service/reaper read must exist with the
    documented default (feature gated OFF by default).
    """
    s = Settings()
    assert s.image_index_enabled is False
    assert s.stream_image_index_submit == "image:index:submit"
    assert s.stream_image_index == "image:index"
    assert s.stream_image_index_results == "image:index:results"
    assert s.stream_image_batch_raw == "image_batch:raw"
    assert s.image_index_submit_group == "image-index-submit"
    assert s.image_index_results_group == "image-index-results"
    assert s.qdrant_collection_image_index == "image_index_embeddings"
    assert s.image_index_n_cap == 100
    assert s.image_index_batch_maxlen == 20_000
    assert s.image_index_max_compute_seconds == 900
    assert s.image_index_reaper_interval_seconds == 60


def test_settings_lifecycle_stream_is_not_bare_image_batch():
    """The lifecycle stream must carry the colon suffix — a bare 'image_batch'
    is the split('.')[0] landmine the contract warns about.
    """
    s = Settings()
    assert s.stream_image_batch_raw == "image_batch:raw"
    assert s.stream_image_batch_raw != "image_batch"


# ── Lifecycle payload builders (00_DESIGN §6.2) ──────────────────────────────


def _make_batch(**overrides) -> ImageIndexBatch:
    """A transient ORM instance (no session) for pure payload-shape tests."""
    now = datetime(2026, 7, 18, 12, 0, 0)
    batch = ImageIndexBatch(
        external_id="run-42",
        client_batch_ref="dwoff-run42-image-vehiculo-b0",
        user_id="tenant-1",
        status=ImageIndexBatchStatus.COMPUTING,
        source_ref="run-42/Vehiculo",
    )
    batch.id = "49c7861d-0000-0000-0000-000000000000"
    batch.submitted_count = 2
    batch.embedded_count = 1
    batch.filtered_count = 0
    batch.failed_count = 1
    batch.created_at = now
    batch.completed_at = None
    batch.error_message = None
    for k, v in overrides.items():
        setattr(batch, k, v)
    return batch


def test_counts_from_batch_is_four_key():
    counts = ImageIndexService.counts_from_batch(_make_batch())
    assert set(counts.keys()) == {"submitted", "embedded", "filtered", "failed"}
    assert counts == {"submitted": 2, "embedded": 1, "filtered": 0, "failed": 1}


def test_created_payload_shape():
    payload = ImageIndexService.build_batch_created_payload(_make_batch())
    assert payload["batch_id"] == "49c7861d-0000-0000-0000-000000000000"
    assert payload["client_batch_ref"] == "dwoff-run42-image-vehiculo-b0"
    assert payload["external_id"] == "run-42"
    assert payload["user_id"] == "tenant-1"
    assert payload["status"] == ImageIndexBatchStatus.COMPUTING
    assert payload["counts"] == {"submitted": 2, "embedded": 1, "filtered": 0, "failed": 1}
    assert payload["source_ref"] == "run-42/Vehiculo"
    assert payload["created_at"] == "2026-07-18T12:00:00"
    assert payload["completed_at"] is None
    # created payload does NOT carry error_message
    assert "error_message" not in payload


def test_completed_payload_shape():
    batch = _make_batch(
        status=ImageIndexBatchStatus.COMPLETED_WITH_ERRORS,
        completed_at=datetime(2026, 7, 18, 12, 5, 0),
    )
    payload = ImageIndexService.build_batch_completed_payload(batch)
    assert payload["status"] == ImageIndexBatchStatus.COMPLETED_WITH_ERRORS
    assert payload["completed_at"] == "2026-07-18T12:05:00"


def test_failed_payload_carries_error_message():
    batch = _make_batch(
        status=ImageIndexBatchStatus.ERROR,
        error_message="batch exceeds N_CAP=100 (got 137)",
        submitted_count=137,
        embedded_count=0,
        failed_count=0,
    )
    payload = ImageIndexService.build_batch_failed_payload(batch)
    assert payload["status"] == ImageIndexBatchStatus.ERROR
    assert payload["error_message"] == "batch exceeds N_CAP=100 (got 137)"
    assert payload["counts"] == {"submitted": 137, "embedded": 0, "filtered": 0, "failed": 0}


# ── Deterministic point-id (00_DESIGN §4) ────────────────────────────────────


def test_point_id_deterministic_and_stable():
    from src.infrastructure.vector_db.image_index_vector_repository import (
        image_index_point_id,
    )

    a = image_index_point_id("batch-x", 3)
    b = image_index_point_id("batch-x", 3)
    c = image_index_point_id("batch-x", 4)
    assert a == b  # same inputs → same point (redelivery overwrites in place)
    assert a != c  # different item_index → different point
    assert len(a) == 36  # UUID string shape
    # FROZEN namespace value — guards against an accidental NS re-key that would
    # silently re-key every stored point.
    assert a == "abf22730-0b9a-5d38-a95e-2e3f11cba6a2"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
