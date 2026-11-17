"""Image blacklist → lookia-dw: TRACKED delivery (retry, reconcile, tombstones, hits).

Contract: Frontend#dw-online #4932–#4940, the plates-service pattern
(lookia-online-dw#placas-dw #4888). Wire shapes: ``dw_publisher_service.build_*``.

What is pending
  * entry / reference: ``dw_published_updated_at`` is NULL or != ``updated_at``.
    Any edit bumps ``updated_at`` (ORM ``onupdate``), so it becomes pending by itself.
  * embedding / tombstone / hit: ``dw_published_at`` is NULL.

Who sends
  * ``flush_now()`` — called right after a commit that changed the blacklist, so the
    DW usually gets the change within a second.
  * the sweep job — every ``BLACKLIST_DW_SWEEP_INTERVAL_SECONDS`` (APScheduler, main.py):
    sends everything still pending (a failed XADD, a restart, a missed flush).
  * the reconcile job — daily at ``BLACKLIST_DW_RECONCILE_HOUR_UTC``: marks every entry
    and reference pending (archived / inactive ones too), so the whole blacklist is
    re-sent. Also ``scripts/resend_blacklist_to_dw.py`` for a manual re-send.

Failure semantics — TRACKED, not swallowed: a failed XADD is logged LOUD, stored in
``dw_publish_error`` and the row stays pending; the pass stops (the bus is likely down)
and the next pass retries. Double sends are harmless: the DW upsert is guarded by
``updated_at`` (entries / references) or keyed on ``id`` (embeddings, tombstones, hits).

Bookkeeping UPDATEs set ``updated_at = updated_at`` so they never count as an edit
(``onupdate``) and only record a send when nothing newer was written meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.blacklist_image import (
    BlacklistImageDeletion,
    BlacklistImageEmbedding,
    BlacklistImageEntry,
    BlacklistImageMatch,
    BlacklistImageReference,
)
from ..infrastructure.config import get_settings
from . import dw_publisher_service as dw

logger = logging.getLogger(__name__)
settings = get_settings()

# Fixed namespace for deterministic hit ids (uuid5). Never change it: the DW keys
# blacklist_image_match rows on this id.
_MATCH_NAMESPACE = uuid.UUID("6b1c2f0e-8a4d-4f7e-9c3b-2d5e7a9f1b30")

_SWEEP_LIMIT = 500
_ERROR_MAX = 2000

_stream_producer = None  # StreamProducer, set at startup
_flush_lock: asyncio.Lock | None = None


def set_blacklist_dw_producer(producer) -> None:
    global _stream_producer
    _stream_producer = producer


def _lock() -> asyncio.Lock:
    global _flush_lock
    if _flush_lock is None:
        _flush_lock = asyncio.Lock()
    return _flush_lock


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_MAX]


def match_id(*, reference_id: str, matched_point_id: str, match_target: str) -> str:
    """Deterministic hit id: one row per (reference, matched point, target)."""
    return str(uuid.uuid5(_MATCH_NAMESPACE, f"{reference_id}|{matched_point_id}|{match_target}"))


def _send(stream: str, event_type: str, payload: dict[str, Any], maxlen: int) -> None:
    """One XADD. RAISES on failure (the caller records it); never swallowed here."""
    if _stream_producer is None:
        raise RuntimeError("blacklist DW producer not configured")
    _stream_producer.publish(stream=stream, event_type=event_type, payload=payload, maxlen=maxlen)


class _BusDown(Exception):
    """A send failed — stop this pass, the next one retries."""


# ── Per-kind senders (each records the outcome, commits) ──────────────────────


async def _send_entries(session: AsyncSession, rows: list[BlacklistImageEntry]) -> int:
    sent = 0
    for row in rows:
        sent_updated_at = row.updated_at
        try:
            _send(
                settings.dw_stream_blacklist_image_entry,
                dw.BLACKLIST_IMAGE_ENTRY_UPSERTED,
                dw.build_blacklist_image_entry_payload(row),
                settings.dw_maxlen_blacklist_image_entry,
            )
        except Exception as exc:  # noqa: BLE001 — recorded + retried, never hidden
            logger.error("DW send blacklist_image_entry %s FAILED (will retry): %s", row.id, exc)
            await session.execute(
                update(BlacklistImageEntry)
                .where(BlacklistImageEntry.id == row.id)
                .values(
                    dw_publish_error=_error_text(exc), updated_at=BlacklistImageEntry.updated_at
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            raise _BusDown from exc
        await session.execute(
            update(BlacklistImageEntry)
            .where(
                BlacklistImageEntry.id == row.id, BlacklistImageEntry.updated_at == sent_updated_at
            )
            .values(
                dw_published_updated_at=sent_updated_at,
                dw_publish_error=None,
                updated_at=BlacklistImageEntry.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        sent += 1
    return sent


async def _send_references(session: AsyncSession, rows: list[BlacklistImageReference]) -> int:
    sent = 0
    for row in rows:
        sent_updated_at = row.updated_at
        try:
            _send(
                settings.dw_stream_blacklist_image_reference,
                dw.BLACKLIST_IMAGE_REFERENCE_UPSERTED,
                dw.build_blacklist_image_reference_payload(row),
                settings.dw_maxlen_blacklist_image_reference,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "DW send blacklist_image_reference %s FAILED (will retry): %s", row.id, exc
            )
            await session.execute(
                update(BlacklistImageReference)
                .where(BlacklistImageReference.id == row.id)
                .values(
                    dw_publish_error=_error_text(exc), updated_at=BlacklistImageReference.updated_at
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            raise _BusDown from exc
        await session.execute(
            update(BlacklistImageReference)
            .where(
                BlacklistImageReference.id == row.id,
                BlacklistImageReference.updated_at == sent_updated_at,
            )
            .values(
                dw_published_updated_at=sent_updated_at,
                dw_publish_error=None,
                updated_at=BlacklistImageReference.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        sent += 1
    return sent


async def _send_once(session: AsyncSession, rows: list, build) -> int:
    """Insert-only rows (embeddings, tombstones, hits): ``dw_published_at``."""
    sent = 0
    for row in rows:
        stream, event_type, payload, maxlen = build(row)
        try:
            _send(stream, event_type, payload, maxlen)
        except Exception as exc:  # noqa: BLE001
            logger.error("DW send %s %s FAILED (will retry): %s", event_type, row.id, exc)
            row.dw_publish_error = _error_text(exc)
            await session.commit()
            raise _BusDown from exc
        row.dw_published_at = datetime.utcnow()
        row.dw_publish_error = None
        await session.commit()
        sent += 1
    return sent


def _embedding_event(row):
    return (
        settings.dw_stream_blacklist_image_embedding,
        dw.BLACKLIST_IMAGE_EMBEDDING_CREATED,
        dw.build_blacklist_image_embedding_payload(row),
        settings.dw_maxlen_blacklist_image_embedding,
    )


def _tombstone_event(row):
    stream, event_type, payload = dw.build_blacklist_image_deleted_payload(row)
    maxlen = (
        settings.dw_maxlen_blacklist_image_entry
        if row.kind == "entry"
        else settings.dw_maxlen_blacklist_image_reference
    )
    return stream, event_type, payload, maxlen


def _match_event(row):
    return (
        settings.dw_stream_blacklist_image_match,
        dw.BLACKLIST_IMAGE_MATCH_DETECTED,
        row.payload,
        settings.dw_maxlen_blacklist_image_match,
    )


# ── The sweep ──────────────────────────────────────────────────────────────────


def _pending_versioned(model):
    return or_(
        model.dw_published_updated_at.is_(None),
        model.dw_published_updated_at != model.updated_at,
    )


async def sweep_pending(session: AsyncSession, limit: int = _SWEEP_LIMIT) -> int:
    """Send everything not yet on the DW, oldest first. Returns how many were sent.

    Order: entries → references → embeddings → tombstones → hits, so the DW sees a
    parent before its children. Stops at the first failed XADD.
    """
    if not settings.blacklist_dw_publish_enabled:
        return 0
    sent = 0
    try:
        entries = list(
            await session.scalars(
                select(BlacklistImageEntry)
                .where(_pending_versioned(BlacklistImageEntry))
                .order_by(BlacklistImageEntry.updated_at)
                .limit(limit)
            )
        )
        sent += await _send_entries(session, entries)

        references = list(
            await session.scalars(
                select(BlacklistImageReference)
                .where(_pending_versioned(BlacklistImageReference))
                .order_by(BlacklistImageReference.updated_at)
                .limit(limit)
            )
        )
        sent += await _send_references(session, references)

        embeddings = list(
            await session.scalars(
                select(BlacklistImageEmbedding)
                .where(BlacklistImageEmbedding.dw_published_at.is_(None))
                .order_by(BlacklistImageEmbedding.created_at)
                .limit(limit)
            )
        )
        sent += await _send_once(session, embeddings, _embedding_event)

        if settings.blacklist_dw_tombstones_enabled:
            tombs = list(
                await session.scalars(
                    select(BlacklistImageDeletion)
                    .where(BlacklistImageDeletion.dw_published_at.is_(None))
                    # Order does not matter: the DW keeps the rows and only sets
                    # source_deleted_at.
                    .order_by(BlacklistImageDeletion.deleted_at)
                    .limit(limit)
                )
            )
            sent += await _send_once(session, tombs, _tombstone_event)

        if settings.blacklist_match_dw_enabled:
            hits = list(
                await session.scalars(
                    select(BlacklistImageMatch)
                    .where(BlacklistImageMatch.dw_published_at.is_(None))
                    .order_by(BlacklistImageMatch.matched_at)
                    .limit(limit)
                )
            )
            sent += await _send_once(session, hits, _match_event)
    except _BusDown:
        logger.warning(
            "DW blacklist sweep stopped after %d sends (bus error); next pass retries", sent
        )
        return sent
    if sent:
        logger.info("DW blacklist sweep: %d events published", sent)
    return sent


async def mark_all_for_reconcile(session: AsyncSession) -> int:
    """Daily reconcile / manual re-send: mark EVERY entry and reference pending
    (archived and inactive ones included). Embeddings, tombstones and hits are
    one-time facts, re-sent only with ``include_facts`` in the script.
    Safe to repeat. Returns the number of rows marked.
    """
    marked = 0
    for model in (BlacklistImageEntry, BlacklistImageReference):
        result = await session.execute(
            update(model)
            .values(dw_published_updated_at=None, updated_at=model.updated_at)  # not an edit
            .execution_options(synchronize_session=False)
        )
        marked += result.rowcount or 0
    await session.commit()
    logger.info("DW blacklist reconcile: %d entries + references marked for re-send", marked)
    return marked


async def mark_facts_for_resend(session: AsyncSession) -> int:
    """Manual re-send of the one-time facts (embeddings, tombstones, hits)."""
    marked = 0
    for model in (BlacklistImageEmbedding, BlacklistImageDeletion, BlacklistImageMatch):
        result = await session.execute(
            update(model).values(dw_published_at=None).execution_options(synchronize_session=False)
        )
        marked += result.rowcount or 0
    await session.commit()
    return marked


# ── Recording (inside the caller's transaction) ───────────────────────────────


async def record_entry_deletion(
    session: AsyncSession, *, entry_id, user_id: str | None, reference_ids: list
) -> None:
    """Tombstones for a hard-deleted entry: the entry + each of its references.
    Call BEFORE the DELETE, in the same transaction."""
    now = datetime.utcnow()
    for ref_id in reference_ids:
        await _record_tombstone(
            session, kind="reference", target_id=ref_id, entry_id=entry_id, user_id=user_id, at=now
        )
    await _record_tombstone(
        session, kind="entry", target_id=entry_id, entry_id=None, user_id=user_id, at=now
    )


async def record_reference_deletion(
    session: AsyncSession, *, reference_id, entry_id, user_id: str | None
) -> None:
    await _record_tombstone(
        session,
        kind="reference",
        target_id=reference_id,
        entry_id=entry_id,
        user_id=user_id,
        at=datetime.utcnow(),
    )


async def _record_tombstone(session, *, kind, target_id, entry_id, user_id, at) -> None:
    stmt = (
        pg_insert(BlacklistImageDeletion)
        .values(
            id=uuid.uuid4(),
            kind=kind,
            target_id=target_id,
            entry_id=entry_id,
            user_id=user_id,
            deleted_at=at,
        )
        .on_conflict_do_nothing(constraint="uq_blacklist_image_deletion_target")
    )
    await session.execute(stmt)


async def record_match(session: AsyncSession, *, event: dict[str, Any]) -> str:
    """Store one live hit (the report-generation event) for the DW. Idempotent:
    the same hit found twice is stored once. Returns the hit id."""
    target = event.get("match_target") or "evidence"
    hit_id = match_id(
        reference_id=str(event["blacklist_reference_id"]),
        matched_point_id=str(event.get("matched_qdrant_point_id") or ""),
        match_target=target,
    )
    payload = dw.build_blacklist_image_match_payload(event, match_id=hit_id)
    stmt = (
        pg_insert(BlacklistImageMatch)
        .values(
            id=uuid.UUID(hit_id),
            user_id=event.get("user_id"),
            entry_id=uuid.UUID(str(event["blacklist_entry_id"])),
            reference_id=uuid.UUID(str(event["blacklist_reference_id"])),
            evidence_id=event.get("evidence_id"),
            matched_at=datetime.utcnow(),
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.execute(stmt)
    return hit_id


# ── Entry points used by the service ──────────────────────────────────────────


async def flush_now() -> None:
    """Send what is pending right now (called after a blacklist commit).

    Never raises: anything that fails here stays pending for the sweep job.
    Serialized in-process so two flushes don't race on the same rows.
    """
    from ..infrastructure.database import AsyncSessionLocal

    try:
        async with _lock():
            async with AsyncSessionLocal() as session:
                await sweep_pending(session)
    except Exception:  # noqa: BLE001 — the sweep job retries; loud
        logger.error("DW blacklist flush failed; the sweep retries", exc_info=True)


async def sweep_job() -> None:
    """APScheduler job (every BLACKLIST_DW_SWEEP_INTERVAL_SECONDS)."""
    await flush_now()


async def reconcile_job() -> None:
    """APScheduler job (daily at BLACKLIST_DW_RECONCILE_HOUR_UTC)."""
    from ..infrastructure.database import AsyncSessionLocal

    if not settings.blacklist_dw_publish_enabled:
        return
    try:
        async with _lock():
            async with AsyncSessionLocal() as session:
                await mark_all_for_reconcile(session)
    except Exception:  # noqa: BLE001
        logger.error("DW blacklist reconcile failed", exc_info=True)
        return
    await flush_now()
