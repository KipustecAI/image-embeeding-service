"""Tests for BlacklistImageRepository — Phase 02 of image-blacklist.

Exercises the 3-table spine against a live migrated Postgres:
- Entry CRUD with default status/active values
- List + multi-tenant isolation (caller-enforced, not repo-enforced)
- Reference unique constraint behavior
- Cascade delete removes references AND embeddings
- Version bump + count_active_by_user fast-exit helper

Requires: docker compose up postgres + alembic upgrade head.
"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models.constants import (
    BlacklistEntryStatus,
    BlacklistReferenceStatus,
)
from src.db.repositories.blacklist_image_repo import BlacklistImageRepository
from src.infrastructure.config import get_settings

settings = get_settings()


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as s:
        yield s
    await engine.dispose()


# ── Entry CRUD ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_entry_defaults(session):
    """Minimal create — name + user_id are the only required args."""
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="test", user_id="user-1")
    await session.commit()

    assert entry.id is not None
    assert entry.status == BlacklistEntryStatus.CREATED
    assert entry.active is True
    assert entry.is_archived is False
    assert entry.blacklist_version == 1
    assert entry.category is None
    assert entry.match_threshold is None


@pytest.mark.asyncio
async def test_create_entry_with_optional_fields(session):
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(
        name="wanted vehicle",
        user_id="user-1",
        category="vehicle",
        description="Red Honda Civic, case 2025-042",
        match_threshold=0.88,
        json_data={"case_id": "CRIM-2025-042"},
    )
    await session.commit()

    found = await repo.get_entry(entry.id)
    assert found is not None
    assert found.name == "wanted vehicle"
    assert found.category == "vehicle"
    assert found.description.startswith("Red Honda")
    assert found.match_threshold == pytest.approx(0.88)
    assert found.json_data == {"case_id": "CRIM-2025-042"}


# ── Listing + multi-tenant ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filters_by_user_id(session):
    """user_id scoping is the caller's responsibility — the repo just
    translates the parameter into a WHERE clause. Verifies the filter works."""
    repo = BlacklistImageRepository(session)
    user_a = f"user-A-{uuid4()}"
    user_b = f"user-B-{uuid4()}"

    await repo.create_entry(name="a1", user_id=user_a)
    await repo.create_entry(name="a2", user_id=user_a)
    await repo.create_entry(name="b1", user_id=user_b)
    await session.commit()

    a_entries = await repo.list_entries(user_id=user_a)
    assert len(a_entries) == 2
    assert {e.user_id for e in a_entries} == {user_a}

    b_entries = await repo.list_entries(user_id=user_b)
    assert len(b_entries) == 1
    assert b_entries[0].name == "b1"


@pytest.mark.asyncio
async def test_list_active_only_by_default(session):
    repo = BlacklistImageRepository(session)
    uid = f"active-{uuid4()}"

    active = await repo.create_entry(name="active", user_id=uid)
    inactive = await repo.create_entry(name="inactive", user_id=uid)
    await session.commit()

    await repo.deactivate_entry(inactive.id)
    await session.commit()

    default_list = await repo.list_entries(user_id=uid)
    assert {e.id for e in default_list} == {active.id}

    with_inactive = await repo.list_entries(user_id=uid, active=None)
    assert {e.id for e in with_inactive} == {active.id, inactive.id}


@pytest.mark.asyncio
async def test_list_filter_by_category(session):
    repo = BlacklistImageRepository(session)
    uid = f"cat-{uuid4()}"

    await repo.create_entry(name="v1", user_id=uid, category="vehicle")
    await repo.create_entry(name="v2", user_id=uid, category="vehicle")
    await repo.create_entry(name="s1", user_id=uid, category="scene")
    await session.commit()

    vehicles = await repo.list_entries(user_id=uid, category="vehicle")
    assert len(vehicles) == 2
    assert {e.category for e in vehicles} == {"vehicle"}


# ── Reference CRUD + unique constraint ────────────────────────────────────


@pytest.mark.asyncio
async def test_add_reference_defaults(session):
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="ref-test", user_id="u1")
    await session.commit()

    ref = await repo.add_reference(entry.id, "https://minio/ref.jpg")
    await session.commit()

    assert ref.entry_id == entry.id
    assert ref.status == BlacklistReferenceStatus.TO_PROCESS
    assert ref.image_type == "reference"


@pytest.mark.asyncio
async def test_duplicate_reference_url_per_entry_rejected(session):
    """(entry_id, image_url) must be unique — enforced by DB constraint."""
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="dup-test", user_id="u1")
    await session.commit()

    await repo.add_reference(entry.id, "https://minio/same.jpg")
    await session.commit()

    with pytest.raises(IntegrityError):
        await repo.add_reference(entry.id, "https://minio/same.jpg")
        await session.commit()


@pytest.mark.asyncio
async def test_same_reference_url_across_entries_allowed(session):
    """Same image can legitimately serve two different blacklist entries."""
    repo = BlacklistImageRepository(session)
    e1 = await repo.create_entry(name="e1", user_id="u1")
    e2 = await repo.create_entry(name="e2", user_id="u1")
    await session.commit()

    url = f"https://minio/{uuid4()}.jpg"
    r1 = await repo.add_reference(e1.id, url)
    r2 = await repo.add_reference(e2.id, url)
    await session.commit()

    assert r1.entry_id != r2.entry_id
    assert r1.image_url == r2.image_url


# ── Cascade delete ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_entry_cascades_and_returns_point_ids(session):
    """Deleting an entry removes refs + embeddings in SQL and returns
    the qdrant_point_ids so the caller can clean Qdrant."""
    repo = BlacklistImageRepository(session)

    entry = await repo.create_entry(name="cascade", user_id="u1")
    await session.commit()

    ref = await repo.add_reference(entry.id, "https://minio/cascade.jpg")
    await session.commit()

    emb = await repo.create_embedding(
        entry_id=entry.id,
        reference_id=ref.id,
        qdrant_point_id=f"pt-{uuid4()}",
        model_version="clip-vit-b-32",
    )
    await session.commit()

    expected_point_id = emb.qdrant_point_id

    point_ids = await repo.delete_entry(entry.id)
    await session.commit()

    assert point_ids == [expected_point_id]
    assert await repo.get_entry(entry.id) is None
    # Both child tables cleaned via ON DELETE CASCADE
    remaining_refs = await repo.list_references(entry.id)
    assert remaining_refs == []


@pytest.mark.asyncio
async def test_delete_reference_returns_point_ids(session):
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="ref-del", user_id="u1")
    await session.commit()

    ref = await repo.add_reference(entry.id, "https://minio/del.jpg")
    await session.commit()

    emb = await repo.create_embedding(
        entry_id=entry.id,
        reference_id=ref.id,
        qdrant_point_id=f"pt-{uuid4()}",
        model_version="clip-vit-b-32",
    )
    await session.commit()

    point_ids = await repo.delete_reference(ref.id)
    await session.commit()

    assert point_ids == [emb.qdrant_point_id]
    assert await repo.get_reference(ref.id) is None
    # Entry should still exist
    assert await repo.get_entry(entry.id) is not None


# ── Version bump ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bump_version(session):
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="v-bump", user_id="u1")
    await session.commit()

    assert entry.blacklist_version == 1
    new_v = await repo.bump_version(entry.id)
    await session.commit()
    assert new_v == 2

    new_v = await repo.bump_version(entry.id)
    await session.commit()
    assert new_v == 3


@pytest.mark.asyncio
async def test_bump_version_returns_none_when_missing(session):
    repo = BlacklistImageRepository(session)
    missing = await repo.bump_version(uuid4())
    assert missing is None


# ── Status transitions ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_entry_status(session):
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="status-test", user_id="u1")
    await session.commit()

    await repo.update_entry_status(entry.id, BlacklistEntryStatus.INDEXED)
    await session.commit()

    found = await repo.get_entry(entry.id)
    assert found.status == BlacklistEntryStatus.INDEXED


@pytest.mark.asyncio
async def test_update_reference_status_with_error(session):
    repo = BlacklistImageRepository(session)
    entry = await repo.create_entry(name="ref-status", user_id="u1")
    await session.commit()

    ref = await repo.add_reference(entry.id, "https://minio/err.jpg")
    await session.commit()

    await repo.update_reference_status(
        ref.id, BlacklistReferenceStatus.ERROR, error="zip download timeout"
    )
    await session.commit()

    found = await repo.get_reference(ref.id)
    assert found.status == BlacklistReferenceStatus.ERROR
    assert found.error_message == "zip download timeout"


# ── Fast-exit helpers (Phase 05 integration points) ──────────────────────


@pytest.mark.asyncio
async def test_count_active_by_user_only_indexed(session):
    """Fast-exit check should only count INDEXED+active entries. Entries
    still in CREATED or PROCESSING don't have Qdrant points yet — skip."""
    repo = BlacklistImageRepository(session)
    uid = f"count-{uuid4()}"

    created = await repo.create_entry(name="created", user_id=uid)
    indexed = await repo.create_entry(name="indexed", user_id=uid)
    inactive = await repo.create_entry(name="inactive", user_id=uid)
    await session.commit()

    await repo.update_entry_status(indexed.id, BlacklistEntryStatus.INDEXED)
    await repo.update_entry_status(inactive.id, BlacklistEntryStatus.INDEXED)
    await repo.deactivate_entry(inactive.id)
    # `created` stays at CREATED (default)
    _ = created
    await session.commit()

    count = await repo.count_active_by_user(uid)
    assert count == 1  # Only `indexed` is active + INDEXED


@pytest.mark.asyncio
async def test_get_active_qdrant_point_ids(session):
    repo = BlacklistImageRepository(session)
    uid = f"points-{uuid4()}"

    active = await repo.create_entry(name="active", user_id=uid)
    dead = await repo.create_entry(name="dead", user_id=uid)
    await session.commit()

    await repo.update_entry_status(active.id, BlacklistEntryStatus.INDEXED)
    await repo.update_entry_status(dead.id, BlacklistEntryStatus.INDEXED)
    await repo.deactivate_entry(dead.id)
    await session.commit()

    # Add refs + embeddings to both
    for entry in (active, dead):
        ref = await repo.add_reference(entry.id, f"https://minio/{entry.id}.jpg")
        await session.commit()
        await repo.create_embedding(
            entry_id=entry.id,
            reference_id=ref.id,
            qdrant_point_id=f"pt-{entry.id}",
            model_version="clip-vit-b-32",
        )
        await session.commit()

    point_ids = await repo.get_active_qdrant_point_ids(uid)
    assert point_ids == [f"pt-{active.id}"]
    # Inactive entry's point is excluded
    assert f"pt-{dead.id}" not in point_ids
