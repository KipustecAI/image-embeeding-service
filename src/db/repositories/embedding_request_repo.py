"""Repository for EmbeddingRequest with atomic row-level locking."""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.constants import EmbeddingRequestStatus
from ..models.embedding_request import EmbeddingRequest

logger = logging.getLogger(__name__)


class EmbeddingRequestRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_pending_requests(
        self, limit: int = 20, max_retries: int = 3
    ) -> list[EmbeddingRequest]:
        """Get TO_WORK requests with FOR UPDATE SKIP LOCKED to prevent double-pickup."""
        query = (
            select(EmbeddingRequest)
            .where(
                and_(
                    EmbeddingRequest.status == EmbeddingRequestStatus.TO_WORK,
                    EmbeddingRequest.retry_count < max_retries,
                )
            )
            .order_by(EmbeddingRequest.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def check_duplicate(self, evidence_id: str) -> bool:
        """Check if evidence already has a processing request."""
        query = select(EmbeddingRequest).where(EmbeddingRequest.evidence_id == evidence_id).limit(1)
        result = await self.session.execute(query)
        return result.scalar() is not None

    async def create_request(
        self,
        evidence_id: str,
        camera_id: str,
        image_urls: list,
        stream_msg_id: str | None = None,
        user_id: str | None = None,
        device_id: str | None = None,
        app_id: int | None = None,
        infraction_code: str | None = None,
        weapon_analyzed: bool = False,
        has_weapon: bool = False,
        weapon_classes: list[str] | None = None,
        weapon_max_confidence: float | None = None,
        weapon_summary: dict | None = None,
        weapon_analysis_error: str | None = None,
    ) -> EmbeddingRequest:
        """Create new embedding request at status=1."""
        request = EmbeddingRequest(
            evidence_id=evidence_id,
            camera_id=camera_id,
            image_urls=image_urls,
            stream_message_id=stream_msg_id,
            user_id=user_id,
            device_id=device_id,
            app_id=app_id,
            infraction_code=infraction_code,
            weapon_analyzed=weapon_analyzed,
            has_weapon=has_weapon,
            weapon_classes=weapon_classes or [],
            weapon_max_confidence=weapon_max_confidence,
            weapon_summary=weapon_summary,
            weapon_analysis_error=weapon_analysis_error,
        )
        self.session.add(request)
        await self.session.flush()
        return request

    async def get_stale_working(self, stale_minutes: int = 10) -> list[EmbeddingRequest]:
        """Find requests stuck in WORKING for too long."""
        cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
        query = select(EmbeddingRequest).where(
            and_(
                EmbeddingRequest.status == EmbeddingRequestStatus.WORKING,
                EmbeddingRequest.processing_started_at < cutoff,
            )
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_by_id(self, request_id: UUID) -> EmbeddingRequest | None:
        return await self.session.get(EmbeddingRequest, request_id)

    async def count_by_status(self) -> dict:
        """Get count of requests per status."""
        counts = {}
        for name, val in [
            ("to_work", 1),
            ("working", 2),
            ("embedded", 3),
            ("done", 4),
            ("error", 5),
        ]:
            result = await self.session.execute(
                select(func.count())
                .select_from(EmbeddingRequest)
                .where(EmbeddingRequest.status == val)
            )
            counts[name] = result.scalar()
        return counts
