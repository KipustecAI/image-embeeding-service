"""Database model and repository tests (CI-safe, no CLIP/images needed).

Requires: PostgreSQL running.
"""

from uuid import uuid4

import pytest


class TestDatabaseModels:
    """Test DB model creation and repository queries."""

    @pytest.mark.asyncio
    async def test_create_embedding_request(self):
        from src.db.repositories import EmbeddingRequestRepository
        from src.infrastructure.database import get_session

        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            request = await repo.create_request(
                evidence_id=f"test-{uuid4()}",
                camera_id=str(uuid4()),
                image_urls=["file:///test/img1.jpg", "file:///test/img2.jpg"],
                stream_msg_id="test-msg-001",
            )
            assert request.id is not None
            assert request.status == 1  # TO_WORK

        # Verify persisted
        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            found = await repo.get_by_id(request.id)
            assert found is not None
            assert found.evidence_id == request.evidence_id

    @pytest.mark.asyncio
    async def test_create_search_request(self):
        from src.db.repositories import SearchRequestRepository
        from src.infrastructure.database import get_session

        async with get_session() as session:
            repo = SearchRequestRepository(session)
            request = await repo.create_request(
                search_id=f"search-{uuid4()}",
                user_id=str(uuid4()),
                image_url="file:///test/query.jpg",
                threshold=0.8,
                max_results=25,
                metadata={"camera_id": "cam-001"},
            )
            assert request.id is not None
            assert request.status == 1

    @pytest.mark.asyncio
    async def test_dedup_check(self):
        from src.db.repositories import EmbeddingRequestRepository
        from src.infrastructure.database import get_session

        evidence_id = f"dedup-test-{uuid4()}"

        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            assert await repo.check_duplicate(evidence_id) is False

            await repo.create_request(
                evidence_id=evidence_id,
                camera_id="cam-1",
                image_urls=["file:///test/img.jpg"],
            )

        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            assert await repo.check_duplicate(evidence_id) is True

    @pytest.mark.asyncio
    async def test_get_pending_with_skip_locked(self):
        from src.db.repositories import EmbeddingRequestRepository
        from src.infrastructure.database import get_session

        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            pending = await repo.get_pending_requests(limit=5)
            # Should return list (possibly empty, but no error)
            assert isinstance(pending, list)

    @pytest.mark.asyncio
    async def test_count_by_status(self):
        from src.db.repositories import EmbeddingRequestRepository
        from src.infrastructure.database import get_session

        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            counts = await repo.count_by_status()
            assert "to_work" in counts
            assert "working" in counts
            assert "embedded" in counts
            assert "done" in counts
            assert "error" in counts
