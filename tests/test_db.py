"""Database model and repository tests (CI-safe, no CLIP/images needed).

Requires: PostgreSQL running with migrations applied.
"""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.config import get_settings

settings = get_settings()


@pytest.fixture
async def session():
    """Fresh engine + session per test to avoid event loop conflicts."""
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_embedding_request(session):
    from src.db.repositories import EmbeddingRequestRepository

    repo = EmbeddingRequestRepository(session)
    request = await repo.create_request(
        evidence_id=f"test-{uuid4()}",
        camera_id=str(uuid4()),
        image_urls=["file:///test/img1.jpg", "file:///test/img2.jpg"],
        stream_msg_id="test-msg-001",
    )
    await session.commit()
    assert request.id is not None
    assert request.status == 1  # TO_WORK

    found = await repo.get_by_id(request.id)
    assert found is not None
    assert found.evidence_id == request.evidence_id


@pytest.mark.asyncio
async def test_create_search_request(session):
    from src.db.repositories import SearchRequestRepository

    repo = SearchRequestRepository(session)
    request = await repo.create_request(
        search_id=f"search-{uuid4()}",
        user_id=str(uuid4()),
        image_url="file:///test/query.jpg",
        threshold=0.8,
        max_results=25,
        metadata={"camera_id": "cam-001"},
    )
    await session.commit()
    assert request.id is not None
    assert request.status == 1


@pytest.mark.asyncio
async def test_dedup_check(session):
    from src.db.repositories import EmbeddingRequestRepository

    evidence_id = f"dedup-test-{uuid4()}"
    repo = EmbeddingRequestRepository(session)
    assert await repo.check_duplicate(evidence_id) is False

    await repo.create_request(
        evidence_id=evidence_id,
        camera_id="cam-1",
        image_urls=["file:///test/img.jpg"],
    )
    await session.commit()

    assert await repo.check_duplicate(evidence_id) is True


@pytest.mark.asyncio
async def test_get_pending_with_skip_locked(session):
    from src.db.repositories import EmbeddingRequestRepository

    repo = EmbeddingRequestRepository(session)
    pending = await repo.get_pending_requests(limit=5)
    assert isinstance(pending, list)


@pytest.mark.asyncio
async def test_count_by_status(session):
    from src.db.repositories import EmbeddingRequestRepository

    repo = EmbeddingRequestRepository(session)
    counts = await repo.count_by_status()
    assert "to_work" in counts
    assert "working" in counts
    assert "embedded" in counts
    assert "done" in counts
    assert "error" in counts
