"""Repository for the blacklist image tables.

Thin CRUD over `BlacklistImageEntry` + `BlacklistImageReference` +
`BlacklistImageEmbedding`. Business logic (status transitions beyond the
basics, match triggering, reverse search) lives in the use cases that
call this repository — not here.

See docs/image-blacklist/02_DATABASE.md for the schema.
"""

import logging
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.blacklist_image import (
    BlacklistImageEmbedding,
    BlacklistImageEntry,
    BlacklistImageReference,
)
from ..models.constants import (
    BlacklistEntryStatus,
    BlacklistReferenceStatus,
)

logger = logging.getLogger(__name__)


class BlacklistImageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── Entry CRUD ────────────────────────────────────────────────────────

    async def create_entry(
        self,
        name: str,
        user_id: str,
        *,
        category: str | None = None,
        description: str | None = None,
        match_threshold: float | None = None,
        json_data: dict | None = None,
    ) -> BlacklistImageEntry:
        """Create a new blacklist entry at status=CREATED (1)."""
        entry = BlacklistImageEntry(
            name=name,
            user_id=user_id,
            category=category,
            description=description,
            status=BlacklistEntryStatus.CREATED,
            match_threshold=match_threshold,
            json_data=json_data,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def get_entry(self, entry_id: UUID) -> BlacklistImageEntry | None:
        return await self.session.get(BlacklistImageEntry, entry_id)

    async def list_entries(
        self,
        user_id: str | None = None,
        *,
        active: bool | None = True,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BlacklistImageEntry]:
        """List entries, most-recent first.

        ``active``: ``True`` = only active (default); ``False`` = only
        inactive; ``None`` = both. Pass ``user_id=None`` only from admin
        paths — the repository doesn't enforce multi-tenant isolation,
        the caller (router/use case) must pin ``user_id`` to the tenant.
        """
        query = self._entries_query(user_id=user_id, active=active, category=category)
        query = query.order_by(BlacklistImageEntry.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_entries(
        self,
        user_id: str | None = None,
        *,
        active: bool | None = True,
        category: str | None = None,
    ) -> int:
        """Total matching entries — paired with ``list_entries`` for pagination."""
        base = self._entries_query(user_id=user_id, active=active, category=category)
        result = await self.session.execute(select(func.count()).select_from(base.subquery()))
        return int(result.scalar() or 0)

    def _entries_query(
        self,
        *,
        user_id: str | None,
        active: bool | None,
        category: str | None,
    ):
        """Shared filter clause for list_entries and count_entries.

        Kept as a private helper so the two paths can't drift on what
        "active" or "category=foo" means.
        """
        query = select(BlacklistImageEntry)
        conditions = []
        if user_id is not None:
            conditions.append(BlacklistImageEntry.user_id == user_id)
        if active is True:
            conditions.append(BlacklistImageEntry.active.is_(True))
        elif active is False:
            conditions.append(BlacklistImageEntry.active.is_(False))
        if category is not None:
            conditions.append(BlacklistImageEntry.category == category)
        if conditions:
            query = query.where(and_(*conditions))
        return query

    async def count_active_by_user(self, user_id: str) -> int:
        """Cheap check — is it worth running inline blacklist-match queries?
        Used by the embedding_results_consumer fast-exit optimization.
        """
        result = await self.session.execute(
            select(func.count())
            .select_from(BlacklistImageEntry)
            .where(
                and_(
                    BlacklistImageEntry.user_id == user_id,
                    BlacklistImageEntry.active.is_(True),
                    BlacklistImageEntry.status == BlacklistEntryStatus.INDEXED,
                )
            )
        )
        return int(result.scalar() or 0)

    async def list_active_indexed_entries(self, user_id: str) -> list[BlacklistImageEntry]:
        """Active + INDEXED entries for a tenant — the auto-hook iteration set.

        Mirrors ``count_active_by_user``'s predicate (active AND status==INDEXED)
        so the Capability-B auto-on-land hook (02_SEARCH_DESIGN §7.4) iterates
        exactly the entries the fast-exit counted. Oldest-first for stable order.
        """
        result = await self.session.execute(
            select(BlacklistImageEntry)
            .where(
                and_(
                    BlacklistImageEntry.user_id == user_id,
                    BlacklistImageEntry.active.is_(True),
                    BlacklistImageEntry.status == BlacklistEntryStatus.INDEXED,
                )
            )
            .order_by(BlacklistImageEntry.created_at.asc())
        )
        return list(result.scalars().all())

    async def update_entry_status(self, entry_id: UUID, status: int) -> BlacklistImageEntry | None:
        entry = await self.get_entry(entry_id)
        if entry is None:
            return None
        entry.status = status
        await self.session.flush()
        return entry

    async def bump_version(self, entry_id: UUID) -> int | None:
        """Increment blacklist_version. Used when a matching-relevant edit
        happens (threshold change, reactivation). Returns the new version
        or None if the entry doesn't exist.
        """
        entry = await self.get_entry(entry_id)
        if entry is None:
            return None
        entry.blacklist_version = (entry.blacklist_version or 0) + 1
        await self.session.flush()
        return entry.blacklist_version

    async def deactivate_entry(self, entry_id: UUID) -> None:
        entry = await self.get_entry(entry_id)
        if entry is None:
            return
        entry.active = False
        await self.session.flush()

    async def delete_entry(self, entry_id: UUID) -> list[str]:
        """Delete an entry. Returns the list of qdrant_point_ids that were
        owned by this entry so the caller can clean up Qdrant. Cascade
        removes references + embeddings rows automatically via FK.
        """
        point_ids_result = await self.session.execute(
            select(BlacklistImageEmbedding.qdrant_point_id).where(
                BlacklistImageEmbedding.entry_id == entry_id
            )
        )
        point_ids = [row[0] for row in point_ids_result.all()]
        await self.session.execute(
            delete(BlacklistImageEntry).where(BlacklistImageEntry.id == entry_id)
        )
        await self.session.flush()
        return point_ids

    # ── Reference CRUD ────────────────────────────────────────────────────

    async def add_reference(
        self,
        entry_id: UUID,
        image_url: str,
        *,
        image_type: str = "reference",
    ) -> BlacklistImageReference:
        """Attach a reference image to an entry at status=TO_PROCESS (1).

        Raises IntegrityError on duplicate (entry_id, image_url) — caller
        must translate to a 409 Conflict at the HTTP boundary.
        """
        reference = BlacklistImageReference(
            entry_id=entry_id,
            image_url=image_url,
            image_type=image_type,
            status=BlacklistReferenceStatus.TO_PROCESS,
        )
        self.session.add(reference)
        await self.session.flush()
        return reference

    async def get_reference(self, reference_id: UUID) -> BlacklistImageReference | None:
        return await self.session.get(BlacklistImageReference, reference_id)

    async def list_references(self, entry_id: UUID) -> list[BlacklistImageReference]:
        result = await self.session.execute(
            select(BlacklistImageReference)
            .where(BlacklistImageReference.entry_id == entry_id)
            .order_by(BlacklistImageReference.created_at.asc())
        )
        return list(result.scalars().all())

    async def update_reference_status(
        self, reference_id: UUID, status: int, error: str | None = None
    ) -> None:
        reference = await self.get_reference(reference_id)
        if reference is None:
            return
        reference.status = status
        if error is not None:
            reference.error_message = error
        await self.session.flush()

    async def delete_reference(self, reference_id: UUID) -> list[str]:
        """Delete a reference and return its qdrant_point_ids so the caller
        can clean up Qdrant. Cascade removes embeddings rows automatically.
        """
        point_ids_result = await self.session.execute(
            select(BlacklistImageEmbedding.qdrant_point_id).where(
                BlacklistImageEmbedding.reference_id == reference_id
            )
        )
        point_ids = [row[0] for row in point_ids_result.all()]
        await self.session.execute(
            delete(BlacklistImageReference).where(BlacklistImageReference.id == reference_id)
        )
        await self.session.flush()
        return point_ids

    # ── Embedding CRUD ────────────────────────────────────────────────────

    async def create_embedding(
        self,
        entry_id: UUID,
        reference_id: UUID,
        qdrant_point_id: str,
        model_version: str,
        json_data: dict | None = None,
    ) -> BlacklistImageEmbedding:
        embedding = BlacklistImageEmbedding(
            entry_id=entry_id,
            reference_id=reference_id,
            qdrant_point_id=qdrant_point_id,
            model_version=model_version,
            json_data=json_data,
        )
        self.session.add(embedding)
        await self.session.flush()
        return embedding

    async def list_embeddings(self, entry_id: UUID) -> list[BlacklistImageEmbedding]:
        result = await self.session.execute(
            select(BlacklistImageEmbedding)
            .where(BlacklistImageEmbedding.entry_id == entry_id)
            .order_by(BlacklistImageEmbedding.created_at.asc())
        )
        return list(result.scalars().all())

    async def count_references_by_entry_ids(self, entry_ids: list[UUID]) -> dict[UUID, int]:
        """Bulk reference-count by entry id. Empty input returns ``{}``.

        The list endpoint calls this once per page rather than firing
        ``list_references`` per entry — keeps the response O(1) queries
        instead of O(N).
        """
        if not entry_ids:
            return {}
        result = await self.session.execute(
            select(BlacklistImageReference.entry_id, func.count())
            .where(BlacklistImageReference.entry_id.in_(entry_ids))
            .group_by(BlacklistImageReference.entry_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def count_embeddings_by_entry_ids(self, entry_ids: list[UUID]) -> dict[UUID, int]:
        """Bulk embedding-count by entry id. Empty input returns ``{}``."""
        if not entry_ids:
            return {}
        result = await self.session.execute(
            select(BlacklistImageEmbedding.entry_id, func.count())
            .where(BlacklistImageEmbedding.entry_id.in_(entry_ids))
            .group_by(BlacklistImageEmbedding.entry_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def get_active_qdrant_point_ids(self, user_id: str) -> list[str]:
        """All qdrant_point_ids for a user's active+indexed blacklist entries.
        Useful for Phase 05 inline-match scoping sanity checks.
        """
        result = await self.session.execute(
            select(BlacklistImageEmbedding.qdrant_point_id)
            .join(
                BlacklistImageEntry,
                BlacklistImageEmbedding.entry_id == BlacklistImageEntry.id,
            )
            .where(
                and_(
                    BlacklistImageEntry.user_id == user_id,
                    BlacklistImageEntry.active.is_(True),
                    BlacklistImageEntry.status == BlacklistEntryStatus.INDEXED,
                )
            )
        )
        return [row[0] for row in result.all()]
