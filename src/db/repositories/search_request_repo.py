"""Repository for SearchRequest with atomic row-level locking."""

import logging
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from sqlalchemy import and_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models.constants import SearchRequestStatus, SimilarityStatus
from ..models.search_match import SearchMatch
from ..models.search_request import SearchRequest

logger = logging.getLogger(__name__)


class SearchRequestRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_pending_requests(
        self, limit: int = 10, max_retries: int = 3
    ) -> List[SearchRequest]:
        """Get TO_WORK search requests with FOR UPDATE SKIP LOCKED."""
        query = (
            select(SearchRequest)
            .where(
                and_(
                    SearchRequest.status == SearchRequestStatus.TO_WORK,
                    SearchRequest.retry_count < max_retries,
                )
            )
            .order_by(SearchRequest.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def check_duplicate(self, search_id: str) -> bool:
        """Check if search already has a processing request."""
        query = (
            select(SearchRequest)
            .where(SearchRequest.search_id == search_id)
            .limit(1)
        )
        result = await self.session.execute(query)
        return result.scalar() is not None

    async def create_request(
        self,
        search_id: str,
        user_id: str,
        image_url: str,
        threshold: float = 0.75,
        max_results: int = 50,
        metadata: Optional[dict] = None,
        stream_msg_id: Optional[str] = None,
    ) -> SearchRequest:
        """Create new search request at status=1."""
        request = SearchRequest(
            search_id=search_id,
            user_id=user_id,
            image_url=image_url,
            threshold=threshold,
            max_results=max_results,
            metadata=metadata,
            stream_message_id=stream_msg_id,
        )
        self.session.add(request)
        await self.session.flush()
        return request

    async def get_stale_working(
        self, stale_minutes: int = 10
    ) -> List[SearchRequest]:
        """Find search requests stuck in WORKING for too long."""
        cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
        query = select(SearchRequest).where(
            and_(
                SearchRequest.status == SearchRequestStatus.WORKING,
                SearchRequest.processing_started_at < cutoff,
            )
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_for_recalculation(
        self, hours_old: int = 2, limit: int = 20
    ) -> List[SearchRequest]:
        """Get completed searches eligible for recalculation."""
        cutoff = datetime.utcnow() - timedelta(hours=hours_old)
        query = (
            select(SearchRequest)
            .where(
                and_(
                    SearchRequest.status == SearchRequestStatus.COMPLETED,
                    SearchRequest.similarity_status == SimilarityStatus.MATCHES_FOUND,
                    SearchRequest.processing_completed_at < cutoff,
                )
            )
            .order_by(SearchRequest.processing_completed_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_by_id(self, request_id: UUID) -> Optional[SearchRequest]:
        return await self.session.get(SearchRequest, request_id)

    async def get_by_search_id(self, search_id: str) -> Optional[SearchRequest]:
        """Fetch by external search_id with matches eagerly loaded."""
        query = (
            select(SearchRequest)
            .where(SearchRequest.search_id == search_id)
            .options(selectinload(SearchRequest.matches))
            .limit(1)
        )
        result = await self.session.execute(query)
        return result.scalar()

    async def get_by_user_id(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> List[SearchRequest]:
        """List searches by user, most recent first."""
        query = (
            select(SearchRequest)
            .where(SearchRequest.user_id == user_id)
            .order_by(SearchRequest.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_by_user_id(self, user_id: str) -> int:
        """Total search count for a user (for pagination)."""
        result = await self.session.execute(
            select(func.count())
            .select_from(SearchRequest)
            .where(SearchRequest.user_id == user_id)
        )
        return result.scalar()

    async def get_matches(
        self, search_request_id, limit: int = 20, offset: int = 0
    ) -> List[SearchMatch]:
        """Get paginated matches for a search, sorted by score descending."""
        query = (
            select(SearchMatch)
            .where(SearchMatch.search_request_id == search_request_id)
            .order_by(SearchMatch.similarity_score.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_matches(self, search_request_id) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(SearchMatch)
            .where(SearchMatch.search_request_id == search_request_id)
        )
        return result.scalar()

    async def count_by_status(self) -> dict:
        """Get count of search requests per status."""
        counts = {}
        for name, val in [
            ("to_work", 1), ("working", 2), ("completed", 3), ("error", 4),
        ]:
            result = await self.session.execute(
                select(func.count()).select_from(SearchRequest).where(
                    SearchRequest.status == val
                )
            )
            counts[name] = result.scalar()
        return counts
