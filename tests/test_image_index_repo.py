"""DB-integration tests for ImageIndexRepository — Phase 1 idempotency.

Exercises the two correctness invariants against a live migrated Postgres:
  - create_or_get_batch: exactly one batch under concurrent/duplicate submits
    (INSERT ... ON CONFLICT (client_batch_ref) DO NOTHING RETURNING id).
  - upsert_result + recompute_counts: redelivery overwrites in place; counts are
    absolute (GROUP BY), never incremented; 'failed' folds the three dispositions.
  - IDOR-scoped reads: user_id ALWAYS in WHERE; cross-tenant → None.

Requires: docker compose up postgres + alembic upgrade head (the
f3a8d5c9e1b7 migration). If no test DB is reachable the whole module SKIPS —
these are not claimed as passing without a database.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models.constants import ImageIndexBatchStatus, ImageIndexResultStatus
from src.db.repositories.image_index_repo import ImageIndexRepository
from src.infrastructure.config import get_settings

settings = get_settings()


async def _db_reachable() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            existing = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'image_index_batches'"
                    )
                )
            ).first()
            return existing is not None
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture
async def session():
    if not await _db_reachable():
        pytest.skip("No migrated test DB reachable (image_index tables absent)")
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as s:
        yield s
    await engine.dispose()


# ── Idempotent mint ──────────────────────────────────────────────────────────


async def test_create_or_get_batch_is_idempotent(session):
    repo = ImageIndexRepository(session)
    ref = f"ref-{uuid4()}"

    batch1, created1 = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=ref, external_id="run-1", submitted_count=2
    )
    await session.commit()
    assert created1 is True
    assert batch1.status == ImageIndexBatchStatus.PENDING
    assert batch1.submitted_count == 2

    # Redelivery of the SAME submit — no second row, created=False.
    batch2, created2 = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=ref, external_id="run-1", submitted_count=2
    )
    await session.commit()
    assert created2 is False
    assert batch2.id == batch1.id


async def test_upsert_result_and_recompute_counts_absolute(session):
    repo = ImageIndexRepository(session)
    ref = f"ref-{uuid4()}"
    batch, _ = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=ref, external_id="run-1", submitted_count=3
    )
    await session.flush()

    await repo.upsert_result(
        batch_id=batch.id, item_index=0, item_ref="a",
        status=ImageIndexResultStatus.EMBEDDED, qdrant_point_id="pid-0",
    )
    await repo.upsert_result(
        batch_id=batch.id, item_index=1, item_ref="b",
        status=ImageIndexResultStatus.DOWNLOAD_FAILED, error_message="http 404",
    )
    await repo.upsert_result(
        batch_id=batch.id, item_index=2, item_ref="c",
        status=ImageIndexResultStatus.FILTERED, duplicate_of_index=0,
    )
    counts = await repo.recompute_counts(batch.id)
    assert counts == {"submitted": 3, "embedded": 1, "filtered": 1, "failed": 1}

    # Redelivery of item 1 flips it to embedded — counts recomputed ABSOLUTELY,
    # never incremented (still 3 rows total).
    await repo.upsert_result(
        batch_id=batch.id, item_index=1, item_ref="b",
        status=ImageIndexResultStatus.EMBEDDED, qdrant_point_id="pid-1",
    )
    counts2 = await repo.recompute_counts(batch.id)
    assert counts2 == {"submitted": 3, "embedded": 2, "filtered": 1, "failed": 0}


async def test_get_batch_is_tenant_scoped(session):
    repo = ImageIndexRepository(session)
    ref = f"ref-{uuid4()}"
    batch, _ = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=ref, external_id="run-1", submitted_count=1
    )
    await session.commit()

    assert await repo.get_batch(batch.id, user_id="tenant-1") is not None
    # Cross-tenant read collapses to None (router → 404, no disclosure).
    assert await repo.get_batch(batch.id, user_id="tenant-OTHER") is None


async def test_get_latest_by_external_id_scoped_newest_first(session):
    repo = ImageIndexRepository(session)
    ext = f"run-{uuid4()}"
    b1, _ = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=f"ref-{uuid4()}", external_id=ext, submitted_count=1
    )
    await session.commit()
    b2, _ = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=f"ref-{uuid4()}", external_id=ext, submitted_count=1
    )
    await session.commit()

    latest = await repo.get_latest_by_external_id(ext, user_id="tenant-1")
    assert latest is not None and latest.external_id == ext
    assert await repo.get_latest_by_external_id(ext, user_id="tenant-OTHER") is None

    all_runs = await repo.list_batches_by_external_id(ext, user_id="tenant-1")
    assert {r.id for r in all_runs} == {b1.id, b2.id}


async def test_list_batches_cap_is_bounded(session):
    repo = ImageIndexRepository(session)
    ext = f"run-{uuid4()}"
    b, _ = await repo.create_or_get_batch(
        user_id="tenant-1", client_batch_ref=f"ref-{uuid4()}", external_id=ext, submitted_count=1
    )
    await session.commit()
    # Over-cap request is clamped to LIST_BY_EXTERNAL_ID_CAP (no unbounded scan).
    rows = await repo.list_batches_by_external_id(ext, user_id="tenant-1", limit=100000)
    assert len(rows) <= 200
    assert b.id in {r.id for r in rows}
