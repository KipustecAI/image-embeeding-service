"""Unit tests for the image-index submit-intake consumer (Phase 2).

No live infra — Redis/DB/Qdrant are mocked. Covers the load-bearing submit
invariants (00_DESIGN §5.1 / §8 invariant #1):

  * TIER-2 validation truth table (over N_CAP, bad url, missing item_id,
    missing external_id, empty items) — pure.
  * TIER-1 unbindable drops (non-dict, blank/non-UUID user_id, blank
    client_batch_ref) → no batch, no publish, no raise.
  * Fresh mint → EXACTLY ONE dispatch to image:index + one image_batch.created,
    status flipped to computing.
  * Duplicate (created=False) → NO dispatch, one image_batch.created re-announce.
  * TIER-2 rejection → ERROR batch + image_batch.failed, no dispatch.
  * Transient publish failure propagates (message would NOT be ACKed).
  * submit_batch_created / create_error_batch atomic-outcome plumbing with a
    mocked session + repo.
"""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.services.image_index_service as service_mod
import src.streams.image_index_submit_consumer as sc
from src.db.models.constants import ImageIndexBatchStatus
from src.services.image_index_service import ImageIndexService

VALID_USER = "11111111-1111-1111-1111-111111111111"


def _submit_payload(**overrides) -> dict:
    payload = {
        "user_id": VALID_USER,
        "client_batch_ref": "dwoff-run42-image-vehiculo-b0",
        "external_id": "run-42",
        "source_ref": "run-42/Vehiculo",
        "metadata": {"k": "v"},
        "items": [
            {"item_id": "ev-0", "image_url": "https://cdn/a.jpg"},
            {"item_id": "ev-1", "image_url": "http://cdn/b.jpg"},
        ],
    }
    payload.update(overrides)
    return payload


def _fake_batch(**overrides) -> SimpleNamespace:
    batch = SimpleNamespace(
        id="49c7861d-0000-0000-0000-000000000000",
        client_batch_ref="dwoff-run42-image-vehiculo-b0",
        external_id="run-42",
        user_id=VALID_USER,
        status=ImageIndexBatchStatus.PENDING,
        submitted_count=2,
        embedded_count=0,
        filtered_count=0,
        failed_count=0,
        source_ref="run-42/Vehiculo",
        created_at=datetime(2026, 7, 18, 12, 0, 0),
        completed_at=None,
        error_message=None,
    )
    for k, v in overrides.items():
        setattr(batch, k, v)
    return batch


# ── TIER-2 validation truth table (pure) ─────────────────────────────────────


def test_validate_ok():
    assert ImageIndexService._validate_submit(_submit_payload(), n_cap=100) is None


def test_validate_missing_external_id():
    err = ImageIndexService._validate_submit(_submit_payload(external_id="  "), n_cap=100)
    assert err == "missing external_id"


def test_validate_empty_items():
    assert ImageIndexService._validate_submit(_submit_payload(items=[]), n_cap=100) == (
        "batch has no items"
    )


def test_validate_items_not_a_list():
    assert (
        ImageIndexService._validate_submit(_submit_payload(items="nope"), n_cap=100)
        == "batch has no items"
    )


def test_validate_over_cap():
    items = [{"item_id": f"e{i}", "image_url": "https://x/y.jpg"} for i in range(5)]
    err = ImageIndexService._validate_submit(_submit_payload(items=items), n_cap=3)
    assert err == "batch exceeds N_CAP=3 (got 5)"


# ── v1.1 vocabulary alias: images/image_id accepted alongside items/item_id ──


def test_validate_ok_with_images_image_id_alias():
    """Portfolio-standard `images:[{image_url, image_id}]` validates like `items`."""
    payload = _submit_payload()
    del payload["items"]
    payload["images"] = [
        {"image_id": "ev-0", "image_url": "https://cdn/a.jpg"},
        {"image_id": "ev-1", "image_url": "http://cdn/b.jpg"},
    ]
    assert ImageIndexService._validate_submit(payload, n_cap=100) is None


def test_validate_missing_id_message_names_both():
    payload = _submit_payload(items=[{"image_url": "https://x/y.jpg"}])
    err = ImageIndexService._validate_submit(payload, n_cap=100)
    assert err == "item 0 missing item_id/image_id"


def test_extract_items_prefers_native_over_alias():
    native = [{"item_id": "a", "image_url": "https://x"}]
    alias = [{"image_id": "b", "image_url": "https://y"}]
    assert ImageIndexService.extract_items({"items": native, "images": alias}) is native
    assert ImageIndexService.extract_items({"images": alias}) is alias
    assert ImageIndexService.extract_items({}) is None


def test_item_id_of_prefers_native_over_alias():
    assert ImageIndexService.item_id_of({"item_id": "a", "image_id": "b"}) == "a"
    assert ImageIndexService.item_id_of({"image_id": "b"}) == "b"
    assert ImageIndexService.item_id_of({}) is None
    assert ImageIndexService.item_id_of("nope") is None


def test_validate_missing_item_id():
    items = [{"item_id": "", "image_url": "https://x/y.jpg"}]
    err = ImageIndexService._validate_submit(_submit_payload(items=items), n_cap=100)
    assert err == "item 0 missing item_id/image_id"


def test_validate_bad_url_scheme():
    items = [{"item_id": "e0", "image_url": "ftp://x/y.jpg"}]
    err = ImageIndexService._validate_submit(_submit_payload(items=items), n_cap=100)
    assert err == "item 0 has invalid image_url"


def test_validate_missing_url():
    items = [{"item_id": "e0", "image_url": None}]
    err = ImageIndexService._validate_submit(_submit_payload(items=items), n_cap=100)
    assert err == "item 0 has invalid image_url"


def test_validate_item_not_object():
    items = ["just-a-string"]
    err = ImageIndexService._validate_submit(_submit_payload(items=items), n_cap=100)
    assert err == "item 0 is not an object"


# ── TIER-1 helpers ───────────────────────────────────────────────────────────


def test_is_uuid():
    assert sc._is_uuid(VALID_USER) is True
    assert sc._is_uuid("not-a-uuid") is False
    assert sc._is_uuid(None) is False


def test_is_blank():
    assert sc._is_blank(None) is True
    assert sc._is_blank("  ") is True
    assert sc._is_blank("x") is False


# ── Consumer orchestration (mocked service + producer) ───────────────────────


@pytest.fixture
def producer(monkeypatch):
    prod = MagicMock()
    monkeypatch.setattr(sc, "_producer", prod)
    return prod


def _patch_service(*, submit_return, create_error_return=None):
    """Patch the DB-touching service methods; keep the real static builders."""
    return (
        patch.object(
            ImageIndexService, "submit_batch_created", AsyncMock(return_value=submit_return)
        ),
        patch.object(
            ImageIndexService,
            "create_error_batch",
            AsyncMock(return_value=create_error_return),
        ),
        patch.object(ImageIndexService, "mark_computing", AsyncMock(return_value=None)),
    )


async def test_fresh_mint_dispatches_once_and_publishes_created(producer):
    batch = _fake_batch()
    p_submit, p_err, p_mark = _patch_service(submit_return=(batch, True, None))
    with p_submit, p_err as create_error, p_mark as mark_computing:
        await sc._process_submit(_submit_payload(), "msg-1")

    # Exactly one dispatch to image:index + one image_batch.created.
    streams = [c.kwargs["stream"] for c in producer.publish.call_args_list]
    events = [c.kwargs["event_type"] for c in producer.publish.call_args_list]
    assert streams == [sc.settings.stream_image_index, sc.settings.stream_image_batch_raw]
    assert events == ["image.index.compute", "image_batch.created"]

    # Dispatch payload: item_index minted 0-based, in submit order.
    dispatch_payload = producer.publish.call_args_list[0].kwargs["payload"]
    assert dispatch_payload["batch_id"] == str(batch.id)
    assert dispatch_payload["user_id"] == VALID_USER
    assert [it["item_index"] for it in dispatch_payload["items"]] == [0, 1]
    assert dispatch_payload["items"][0]["item_id"] == "ev-0"

    # computing set exactly once, and the created payload reflects it.
    mark_computing.assert_awaited_once_with(batch.id)
    created_payload = producer.publish.call_args_list[1].kwargs["payload"]
    assert created_payload["status"] == ImageIndexBatchStatus.COMPUTING
    # lifecycle publish is MAXLEN-trimmed.
    assert producer.publish.call_args_list[1].kwargs["maxlen"] == sc.settings.image_index_batch_maxlen
    create_error.assert_not_called()


async def test_duplicate_submit_reannounces_without_dispatch(producer):
    batch = _fake_batch(status=ImageIndexBatchStatus.COMPUTING)
    p_submit, p_err, p_mark = _patch_service(submit_return=(batch, False, None))
    with p_submit, p_err, p_mark as mark_computing:
        await sc._process_submit(_submit_payload(), "msg-dup")

    # No dispatch; exactly one re-published image_batch.created.
    streams = [c.kwargs["stream"] for c in producer.publish.call_args_list]
    events = [c.kwargs["event_type"] for c in producer.publish.call_args_list]
    assert streams == [sc.settings.stream_image_batch_raw]
    assert events == ["image_batch.created"]
    mark_computing.assert_not_awaited()


async def test_over_cap_creates_error_batch_and_publishes_failed(producer):
    err_batch = _fake_batch(
        status=ImageIndexBatchStatus.ERROR,
        error_message="batch exceeds N_CAP=100 (got 137)",
        submitted_count=137,
    )
    p_submit, p_err, p_mark = _patch_service(
        submit_return=(None, False, "batch exceeds N_CAP=100 (got 137)"),
        create_error_return=err_batch,
    )
    with p_submit, p_err as create_error, p_mark:
        await sc._process_submit(_submit_payload(), "msg-over")

    create_error.assert_awaited_once()
    # No dispatch — only image_batch.failed to the lifecycle stream.
    streams = [c.kwargs["stream"] for c in producer.publish.call_args_list]
    events = [c.kwargs["event_type"] for c in producer.publish.call_args_list]
    assert streams == [sc.settings.stream_image_batch_raw]
    assert events == ["image_batch.failed"]
    failed_payload = producer.publish.call_args_list[0].kwargs["payload"]
    assert failed_payload["status"] == ImageIndexBatchStatus.ERROR
    assert failed_payload["error_message"] == "batch exceeds N_CAP=100 (got 137)"


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        _submit_payload(user_id="  "),
        _submit_payload(user_id="not-a-uuid"),
        _submit_payload(client_batch_ref=""),
    ],
)
async def test_tier1_unbindable_drops_without_batch_or_publish(producer, payload):
    submit = AsyncMock()
    with patch.object(ImageIndexService, "submit_batch_created", submit):
        await sc._process_submit(payload, "msg-drop")
    submit.assert_not_called()
    producer.publish.assert_not_called()


async def test_transient_publish_failure_propagates(producer):
    """A publish raise must propagate → StreamConsumer will NOT ACK (PEL retry)."""
    batch = _fake_batch()
    producer.publish.side_effect = RuntimeError("redis down")
    p_submit, p_err, p_mark = _patch_service(submit_return=(batch, True, None))
    with p_submit, p_err, p_mark:
        with pytest.raises(RuntimeError, match="redis down"):
            await sc._process_submit(_submit_payload(), "msg-transient")


# ── Service mint plumbing (mocked session + repo) ────────────────────────────


@asynccontextmanager
async def _fake_session_cm():
    session = MagicMock()
    session.flush = AsyncMock()
    session.expunge = MagicMock()
    yield session


async def test_submit_batch_created_returns_atomic_outcome():
    batch = _fake_batch()
    fake_repo = MagicMock()
    fake_repo.create_or_get_batch = AsyncMock(return_value=(batch, True))
    with patch.object(service_mod, "get_session", _fake_session_cm), patch.object(
        service_mod, "ImageIndexRepository", return_value=fake_repo
    ):
        result_batch, created, error = await ImageIndexService().submit_batch_created(
            _submit_payload()
        )
    assert created is True
    assert error is None
    assert result_batch is batch
    # submitted_count derives from len(items); status minted pending.
    kwargs = fake_repo.create_or_get_batch.call_args.kwargs
    assert kwargs["submitted_count"] == 2
    assert kwargs["status"] == ImageIndexBatchStatus.PENDING


async def test_submit_batch_created_rejects_without_touching_db():
    get_session = MagicMock()
    with patch.object(service_mod, "get_session", get_session):
        result_batch, created, error = await ImageIndexService().submit_batch_created(
            _submit_payload(items=[])
        )
    assert result_batch is None
    assert created is False
    assert error == "batch has no items"
    get_session.assert_not_called()  # no session opened on a validation reject


async def test_create_error_batch_sets_error_status_and_message():
    batch = _fake_batch()
    fake_repo = MagicMock()
    fake_repo.create_or_get_batch = AsyncMock(return_value=(batch, True))
    with patch.object(service_mod, "get_session", _fake_session_cm), patch.object(
        service_mod, "ImageIndexRepository", return_value=fake_repo
    ):
        out = await ImageIndexService().create_error_batch(
            _submit_payload(), "batch exceeds N_CAP=100 (got 137)"
        )
    assert out.status == ImageIndexBatchStatus.ERROR
    assert out.error_message == "batch exceeds N_CAP=100 (got 137)"
    assert out.completed_at is not None
    # error batch minted with submitted_count = len(items).
    assert fake_repo.create_or_get_batch.call_args.kwargs["submitted_count"] == 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
