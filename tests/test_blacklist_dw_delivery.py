"""Tracked blacklist delivery to lookia-dw (src/services/blacklist_dw_delivery.py).

Drives the REAL use case + repository + sweep against a migrated Postgres, with a
fake StreamProducer. Locks the Frontend#dw-online #4936 contract:

  * every create / edit is sent, recorded, and not sent twice;
  * a failed XADD never fails the request: the row stays pending with the error and
    the next sweep delivers it;
  * the bookkeeping never bumps ``updated_at``;
  * hard deletes write tombstones (entry + each reference) in the delete's
    transaction, held back until BLACKLIST_DW_TOMBSTONES_ENABLED;
  * the daily reconcile re-sends everything without touching ``updated_at``;
  * live hits are stored once (deterministic id), name hashed, held back until
    BLACKLIST_MATCH_DW_ENABLED.

Requires a migrated Postgres at DATABASE_URL; skipped when none is reachable.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, text

from src.application.use_cases.manage_blacklist_image import ManageBlacklistImageUseCase
from src.db.models.blacklist_image import (
    BlacklistImageDeletion,
    BlacklistImageEmbedding,
    BlacklistImageEntry,
    BlacklistImageMatch,
    BlacklistImageReference,
)
from src.db.repositories.blacklist_image_repo import BlacklistImageRepository
from src.infrastructure.database import AsyncSessionLocal, engine
from src.services import blacklist_dw_delivery as delivery
from src.services import dw_publisher_service as dw

USER = "dw-test-user"


@pytest.fixture
async def db():
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT to_regclass('public.blacklist_image_deletions') IS NOT NULL")
            )
    except Exception:
        await engine.dispose()
        pytest.skip("no Postgres reachable at DATABASE_URL")
    if not exists:
        await engine.dispose()
        pytest.skip("Postgres not migrated to d7e3a1c9b5f2")
    # Rows left by other suites (the repo tests commit to the same DB) would be
    # pending too: mark them delivered so each test sees only its own sends.
    async with engine.begin() as conn:
        for table in ("blacklist_image_entries", "blacklist_image_references"):
            await conn.execute(
                text(
                    f"UPDATE {table} SET dw_published_updated_at = updated_at "
                    "WHERE dw_published_updated_at IS DISTINCT FROM updated_at"
                )
            )
        for table in ("blacklist_image_embeddings", "blacklist_image_deletions"):
            await conn.execute(
                text(f"UPDATE {table} SET dw_published_at = now() WHERE dw_published_at IS NULL")
            )
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM blacklist_image_matches"))
        await conn.execute(text("DELETE FROM blacklist_image_deletions"))
        await conn.execute(
            text("DELETE FROM blacklist_image_entries WHERE user_id = :u"), {"u": USER}
        )
    await engine.dispose()  # asyncpg connections are bound to this test's loop


@pytest.fixture
def producer(monkeypatch):
    p = MagicMock()
    monkeypatch.setattr(delivery, "_stream_producer", p)
    monkeypatch.setattr(delivery.settings, "blacklist_dw_publish_enabled", True)
    monkeypatch.setattr(delivery.settings, "blacklist_dw_tombstones_enabled", False)
    monkeypatch.setattr(delivery.settings, "blacklist_match_dw_enabled", False)
    return p


def _events(producer) -> list[tuple[str, str, dict]]:
    return [
        (c.kwargs["stream"], c.kwargs["event_type"], c.kwargs["payload"])
        for c in producer.publish.call_args_list
    ]


async def _get(model, row_id):
    async with AsyncSessionLocal() as s:
        return await s.get(model, row_id)


async def _sweep() -> int:
    async with AsyncSessionLocal() as s:
        return await delivery.sweep_pending(s)


def _use_case() -> ManageBlacklistImageUseCase:
    return ManageBlacklistImageUseCase(vector_repo=None, stream_producer=None)


async def _create(name="Plate van") -> BlacklistImageEntry:
    return await _use_case().create_entry(
        user_id=USER,
        name=name,
        category="vehicle",
        description=None,
        match_threshold=None,
        json_data=None,
    )


# ── upserted: sent once, recorded, retried ───────────────────────────────────


async def test_create_is_sent_once_and_recorded(db, producer):
    entry = await _create()

    events = _events(producer)
    assert [(s, e) for s, e, _ in events] == [
        ("blacklist_image_entry:raw", "blacklist_image_entry.upserted")
    ]
    row = await _get(BlacklistImageEntry, entry.id)
    assert row.dw_published_updated_at == row.updated_at
    assert row.dw_publish_error is None
    assert events[0][2]["updated_at"] == row.updated_at.isoformat()

    producer.publish.reset_mock()
    assert await _sweep() == 0
    producer.publish.assert_not_called()


async def test_failed_send_never_fails_the_request_and_is_retried(db, producer):
    producer.publish.side_effect = ConnectionError("bus down")
    entry = await _create()  # must not raise

    row = await _get(BlacklistImageEntry, entry.id)
    assert row.dw_published_updated_at is None
    assert "bus down" in row.dw_publish_error
    updated_at_before = row.updated_at

    producer.publish.side_effect = None
    producer.publish.reset_mock()
    assert await _sweep() == 1
    row = await _get(BlacklistImageEntry, entry.id)
    assert row.dw_published_updated_at == row.updated_at
    assert row.dw_publish_error is None
    # Bookkeeping is not an edit.
    assert row.updated_at == updated_at_before


async def test_edit_is_resent_with_the_new_updated_at(db, producer):
    entry = await _create()
    producer.publish.reset_mock()

    await _use_case().update_entry(
        entry_id=entry.id, requester_user_id=USER, is_admin=False, patch={"category": "truck"}
    )

    (stream, event_type, payload), *rest = _events(producer)
    assert not rest
    assert event_type == "blacklist_image_entry.upserted"
    row = await _get(BlacklistImageEntry, entry.id)
    assert payload["category"] == "truck"
    assert payload["updated_at"] == row.updated_at.isoformat()
    assert row.dw_published_updated_at == row.updated_at


async def test_reference_and_embedding_are_sent(db, producer):
    entry = await _create()
    reference = await _use_case().add_reference(
        entry_id=entry.id,
        requester_user_id=USER,
        is_admin=False,
        image_url="https://x/ref.jpg",
        image_type="reference",
    )
    async with AsyncSessionLocal() as s:
        await BlacklistImageRepository(s).create_embedding(
            entry_id=entry.id,
            reference_id=reference.id,
            qdrant_point_id=f"qp-{uuid.uuid4()}",
            model_version="clip-vit-b-32",
        )
        await s.commit()
    producer.publish.reset_mock()

    assert await _sweep() == 1
    ((stream, event_type, payload),) = _events(producer)
    assert (stream, event_type) == (
        "blacklist_image_embedding:raw",
        "blacklist_image_embedding.created",
    )
    assert payload["reference_id"] == str(reference.id)
    async with AsyncSessionLocal() as s:
        emb = await s.scalar(
            select(BlacklistImageEmbedding).where(BlacklistImageEmbedding.entry_id == entry.id)
        )
    assert emb.dw_published_at is not None
    ref_row = await _get(BlacklistImageReference, reference.id)
    assert ref_row.dw_published_updated_at == ref_row.updated_at


# ── tombstones ───────────────────────────────────────────────────────────────


async def test_entry_delete_writes_tombstones_held_until_enabled(db, producer, monkeypatch):
    entry = await _create()
    reference = await _use_case().add_reference(
        entry_id=entry.id,
        requester_user_id=USER,
        is_admin=False,
        image_url="https://x/ref2.jpg",
        image_type="reference",
    )
    producer.publish.reset_mock()

    await _use_case().delete_entry(entry_id=entry.id, requester_user_id=USER, is_admin=False)

    async with AsyncSessionLocal() as s:
        tombs = list(await s.scalars(select(BlacklistImageDeletion)))
    assert {(t.kind, t.target_id) for t in tombs} == {
        ("entry", entry.id),
        ("reference", reference.id),
    }
    assert all(t.dw_published_at is None for t in tombs)
    producer.publish.assert_not_called()  # DW not ready yet → held back

    monkeypatch.setattr(delivery.settings, "blacklist_dw_tombstones_enabled", True)
    assert await _sweep() == 2
    by_type = {e: (s, p) for s, e, p in _events(producer)}
    assert by_type["blacklist_image_entry.deleted"] == (
        "blacklist_image_entry:raw",
        {
            "id": str(entry.id),
            "user_id": USER,
            "deleted_at": by_type["blacklist_image_entry.deleted"][1]["deleted_at"],
        },
    )
    ref_stream, ref_payload = by_type["blacklist_image_reference.deleted"]
    assert ref_stream == "blacklist_image_reference:raw"
    assert ref_payload["id"] == str(reference.id)
    assert ref_payload["entry_id"] == str(entry.id)
    assert ref_payload["user_id"] == USER

    producer.publish.reset_mock()
    assert await _sweep() == 0


async def test_reference_delete_writes_one_tombstone(db, producer):
    entry = await _create()
    reference = await _use_case().add_reference(
        entry_id=entry.id,
        requester_user_id=USER,
        is_admin=False,
        image_url="https://x/ref3.jpg",
        image_type="reference",
    )
    await _use_case().delete_reference(
        entry_id=entry.id, reference_id=reference.id, requester_user_id=USER, is_admin=False
    )
    async with AsyncSessionLocal() as s:
        tombs = list(await s.scalars(select(BlacklistImageDeletion)))
    assert [(t.kind, t.target_id, t.entry_id) for t in tombs] == [
        ("reference", reference.id, entry.id)
    ]


# ── reconcile ────────────────────────────────────────────────────────────────


async def test_reconcile_resends_everything_without_an_edit(db, producer):
    entry = await _create()
    before = (await _get(BlacklistImageEntry, entry.id)).updated_at
    producer.publish.reset_mock()

    async with AsyncSessionLocal() as s:
        assert await delivery.mark_all_for_reconcile(s) >= 1
    assert await _sweep() >= 1
    sent_ids = {p["id"] for _, e, p in _events(producer) if e.endswith("entry.upserted")}
    assert str(entry.id) in sent_ids
    row = await _get(BlacklistImageEntry, entry.id)
    assert row.updated_at == before
    assert row.dw_published_updated_at == before


# ── live hits ────────────────────────────────────────────────────────────────


def _hit_event(entry_id, reference_id, point="pt-1") -> dict:
    return {
        "user_id": USER,
        "blacklist_entry_id": str(entry_id),
        "blacklist_entry_name": "Suspect: Juan Pérez",
        "blacklist_entry_category": "vehicle",
        "blacklist_entry_version": 1,
        "blacklist_reference_id": str(reference_id),
        "blacklist_reference_url": "https://x/ref.jpg",
        "blacklist_model_version": "clip-vit-b-32",
        "evidence_id": "ev-1",
        "evidence_camera_id": "cam-1",
        "evidence_device_id": None,
        "evidence_app_id": None,
        "evidence_infraction_code": None,
        "evidence_category": None,
        "matched_image_url": "https://x/frame.jpg",
        "matched_image_index": 0,
        "matched_qdrant_point_id": point,
        "matched_point_id": point,
        "zip_url": None,
        "zip_entry": None,
        "similarity_score": 0.91,
        "threshold_used": 0.85,
        "trigger": "inline",
        "matched_at": "2026-11-17T00:00:00Z",
    }


async def test_hit_is_stored_once_name_hashed_and_held_until_enabled(db, producer, monkeypatch):
    entry_id, reference_id = uuid.uuid4(), uuid.uuid4()
    event = _hit_event(entry_id, reference_id)

    async with AsyncSessionLocal() as s:
        first = await delivery.record_match(s, event=event)
        again = await delivery.record_match(s, event={**event, "trigger": "reverse_search"})
        await s.commit()
    assert first == again
    async with AsyncSessionLocal() as s:
        rows = list(await s.scalars(select(BlacklistImageMatch)))
    assert len(rows) == 1

    assert await _sweep() == 0  # held back
    monkeypatch.setattr(delivery.settings, "blacklist_match_dw_enabled", True)
    assert await _sweep() == 1
    ((stream, event_type, payload),) = _events(producer)
    assert (stream, event_type) == ("blacklist_image_match:raw", "blacklist_image_match.detected")
    assert payload["id"] == first
    assert payload["match_target"] == "evidence"
    assert payload["external_id"] is None and payload["batch_id"] is None
    assert "blacklist_entry_name" not in payload
    assert (
        payload["blacklist_entry_name_hash"]
        == dw.build_blacklist_image_entry_payload(
            type(
                "R",
                (),
                {
                    "id": None,
                    "name": "Suspect: Juan Pérez",
                    "category": None,
                    "status": None,
                    "active": None,
                    "is_archived": None,
                    "user_id": None,
                    "blacklist_version": None,
                    "match_threshold": None,
                    "json_data": None,
                    "created_at": None,
                    "updated_at": None,
                },
            )()
        )["name_hash"]
    )
    assert "Juan" not in str(payload)


def test_match_id_is_deterministic_per_target():
    a = delivery.match_id(reference_id="r", matched_point_id="p", match_target="evidence")
    assert a == delivery.match_id(reference_id="r", matched_point_id="p", match_target="evidence")
    assert a != delivery.match_id(
        reference_id="r", matched_point_id="p", match_target="image_index"
    )
