"""Business logic for the image-blacklist CRUD surface.

Sits between the FastAPI router and the repository / Qdrant /
StreamProducer. The router stays thin — Pydantic in/out + dependency
wiring; everything else lives here.

Multi-tenant rules are enforced at this layer: a use-case method takes
``user_id`` (the requesting tenant) plus ``is_admin`` and decides
whether to scope queries / mutations or pass through. Repository
methods are tenant-agnostic by design — they trust the use case.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from ...db.models.blacklist_image import (
    BlacklistImageEntry,
    BlacklistImageReference,
)
from ...db.models.constants import (
    BlacklistEntryStatus,
    BlacklistReferenceStatus,
)
from ...db.repositories.blacklist_image_repo import BlacklistImageRepository
from ...infrastructure.config import get_settings
from ...infrastructure.database import get_session

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Errors ─────────────────────────────────────────────────────────────────


class BlacklistEntryNotFound(LookupError):
    """Raised when an entry id doesn't exist or isn't visible to the caller.

    The router translates this to a 404 — we deliberately don't
    distinguish "doesn't exist" from "not yours" so a non-admin caller
    can't probe for entries they don't own.
    """


class BlacklistReferenceNotFound(LookupError):
    """Raised when a reference id doesn't exist under the named entry."""


class BlacklistDuplicateReference(ValueError):
    """Raised when the same image_url already exists on the same entry."""


class BlacklistAccessDenied(PermissionError):
    """Raised when a non-admin tries to act on another tenant's entry."""


# ── Use case ───────────────────────────────────────────────────────────────


class ManageBlacklistImageUseCase:
    """Wraps Qdrant + StreamProducer + repository for the blacklist CRUD.

    Constructed per request by the router (see Depends-style factories
    in routers/blacklist_image.py). ``vector_repo`` and
    ``stream_producer`` are the same instances wired in main.py
    lifespan — passed in rather than imported as module-level globals
    so unit tests can swap fakes without monkey-patching.
    """

    def __init__(self, vector_repo, stream_producer):
        self._vector_repo = vector_repo
        self._stream_producer = stream_producer

    # ── Authorization helpers ─────────────────────────────────────────────

    @staticmethod
    def _scope_user_id(user_id: str | None, is_admin: bool) -> str | None:
        """Resolve the multi-tenant scope for list / read operations.

        Non-admins always see only their own data. Admins see everything
        unless they explicitly pass a ``user_id`` filter (then they see
        that tenant's data).
        """
        return None if is_admin else user_id

    @staticmethod
    def _enforce_owner(entry: BlacklistImageEntry, user_id: str, is_admin: bool) -> None:
        """Raise ``BlacklistEntryNotFound`` (NOT AccessDenied) for non-owners.

        Returning a 404 (rather than 403) on a foreign-tenant entry
        avoids leaking the existence of entries the caller can't see.
        """
        if is_admin:
            return
        if entry.user_id != user_id:
            raise BlacklistEntryNotFound(str(entry.id))

    # ── Entry CRUD ────────────────────────────────────────────────────────

    async def create_entry(
        self,
        *,
        user_id: str,
        name: str,
        category: str | None,
        description: str | None,
        match_threshold: float | None,
        json_data: dict | None,
    ) -> BlacklistImageEntry:
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.create_entry(
                name=name,
                user_id=user_id,
                category=category,
                description=description,
                match_threshold=match_threshold,
                json_data=json_data,
            )
            # Detach the entry from the session so the caller can access
            # attributes after the session closes. SQLAlchemy 2.x
            # otherwise raises on attribute access post-commit.
            session.expunge(entry)
            return entry

    async def list_entries(
        self,
        *,
        requester_user_id: str,
        is_admin: bool,
        target_user_id: str | None,
        active: bool | None,
        category: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[BlacklistImageEntry], int, dict[UUID, int], dict[UUID, int]]:
        """Return (entries, total, references_count_map, embeddings_count_map).

        Counts are bulk-resolved in two queries so the response is
        O(1) DB calls regardless of page size.
        """
        # Non-admins are pinned to their own user_id even if they
        # pass target_user_id. Admins respect target_user_id when set,
        # otherwise see all tenants.
        scope = requester_user_id if not is_admin else (target_user_id or None)

        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entries = await repo.list_entries(
                user_id=scope,
                active=active,
                category=category,
                limit=limit,
                offset=offset,
            )
            total = await repo.count_entries(user_id=scope, active=active, category=category)
            ids = [e.id for e in entries]
            ref_counts = await repo.count_references_by_entry_ids(ids)
            emb_counts = await repo.count_embeddings_by_entry_ids(ids)
            for entry in entries:
                session.expunge(entry)

        return entries, total, ref_counts, emb_counts

    async def get_entry_detail(
        self,
        *,
        entry_id: UUID,
        requester_user_id: str,
        is_admin: bool,
    ) -> tuple[BlacklistImageEntry, list[BlacklistImageReference], int]:
        """Return (entry, references, embeddings_count).

        Tenant scoping is enforced via ``_enforce_owner`` which raises
        ``BlacklistEntryNotFound`` for foreign tenants — same response
        as a truly-missing id.
        """
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            if entry is None:
                raise BlacklistEntryNotFound(str(entry_id))
            self._enforce_owner(entry, requester_user_id, is_admin)
            references = await repo.list_references(entry_id)
            embeddings_count = len(await repo.list_embeddings(entry_id))
            session.expunge(entry)
            for r in references:
                session.expunge(r)
        return entry, references, embeddings_count

    async def update_entry(
        self,
        *,
        entry_id: UUID,
        requester_user_id: str,
        is_admin: bool,
        patch: dict,
    ) -> BlacklistImageEntry:
        """Apply a partial update and bump version when the change is
        matching-relevant.

        Matching-relevant rules (v1):

          * ``match_threshold`` change → bump.
          * ``active`` toggling False → True (re-activation) → bump.
          * Other fields → no bump.

        Reasoning: the report receiver dedups on
        ``(evidence_id, entry_id, version)`` — bump only when a matching
        re-evaluation should produce fresh reports.
        """
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            if entry is None:
                raise BlacklistEntryNotFound(str(entry_id))
            self._enforce_owner(entry, requester_user_id, is_admin)

            should_bump = False

            if "name" in patch and patch["name"] is not None:
                entry.name = patch["name"]
            if "category" in patch:
                entry.category = patch["category"]
            if "description" in patch:
                entry.description = patch["description"]
            if "match_threshold" in patch and patch["match_threshold"] != entry.match_threshold:
                entry.match_threshold = patch["match_threshold"]
                should_bump = True
            if "active" in patch and patch["active"] is not None:
                if patch["active"] and not entry.active:
                    should_bump = True
                entry.active = patch["active"]
            if "json_data" in patch:
                entry.json_data = patch["json_data"]

            entry.updated_at = datetime.utcnow()
            if should_bump:
                entry.blacklist_version = entry.blacklist_version + 1

            await session.flush()
            session.expunge(entry)
        return entry

    async def delete_entry(
        self,
        *,
        entry_id: UUID,
        requester_user_id: str,
        is_admin: bool,
    ) -> None:
        """Hard-delete: SQL cascade + Qdrant cleanup.

        Qdrant first via the SQL-fetched point ids; SQL cascade after.
        If Qdrant fails we leave SQL intact and surface 503 — preferring
        an orphaned blacklist entry over orphaned Qdrant points that
        would keep silently matching against incoming evidence.
        """
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            if entry is None:
                raise BlacklistEntryNotFound(str(entry_id))
            self._enforce_owner(entry, requester_user_id, is_admin)
            point_ids = await repo.delete_entry(entry_id)

        if self._vector_repo and point_ids:
            ok = await self._vector_repo.delete_points(point_ids)
            if not ok:
                # SQL row is already gone — log and continue. The
                # orphan points still match against blacklist filters
                # but no SQL row references them, so an admin
                # reconciliation is the recovery path.
                logger.error(
                    "Qdrant cleanup failed after entry delete: entry=%s points=%d",
                    entry_id,
                    len(point_ids),
                )

    # ── Reference CRUD ────────────────────────────────────────────────────

    async def add_reference(
        self,
        *,
        entry_id: UUID,
        requester_user_id: str,
        is_admin: bool,
        image_url: str,
        image_type: str,
    ) -> BlacklistImageReference:
        """Insert a TO_PROCESS reference and trigger the embed via the
        existing ``evidence:search`` stream with ``purpose="blacklist_embed"``.

        The 202 returned by the router reflects this — the actual
        embedding lands asynchronously when ``search_results_consumer``
        receives the result envelope. Status transitions to PROCESSING /
        PROCESSED / ERROR are the source of truth for completion.
        """
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            if entry is None:
                raise BlacklistEntryNotFound(str(entry_id))
            self._enforce_owner(entry, requester_user_id, is_admin)

            try:
                reference = await repo.add_reference(
                    entry_id=entry_id, image_url=image_url, image_type=image_type
                )
            except Exception as e:
                # Translate the unique constraint violation surface
                # without leaking IntegrityError specifics to the router.
                msg = str(e).lower()
                if "uq_entry_image" in msg or "unique" in msg:
                    raise BlacklistDuplicateReference(image_url) from e
                raise

            # Lift the entry from CREATED → PROCESSING on the first
            # reference. INDEXED / UPDATING / ERROR remain (the consumer
            # owns transitions out of those).
            if entry.status == BlacklistEntryStatus.CREATED:
                entry.status = BlacklistEntryStatus.PROCESSING
            session.expunge(reference)

        # Fire the embed request to the GPU. ``search_id`` is overloaded
        # to carry the reference id — see docs/image-blacklist/04_EMBEDDING_FLOW.md.
        if self._stream_producer is not None:
            try:
                self._stream_producer.publish(
                    stream=settings.stream_evidence_search,
                    event_type="search.created",
                    payload={
                        "search_id": str(reference.id),
                        "user_id": requester_user_id,
                        "image_url": image_url,
                        "purpose": "blacklist_embed",
                        "blacklist_entry_id": str(entry_id),
                    },
                )
            except Exception as pub_err:
                # Reference already inserted at TO_PROCESS — admin can
                # retry by deleting + re-adding, or via a future reembed
                # endpoint. We log and surface a partial-success state
                # to the caller via the returned status.
                logger.error(
                    "Failed to publish blacklist embed for ref=%s: %s",
                    reference.id,
                    pub_err,
                    exc_info=True,
                )

        return reference

    async def delete_reference(
        self,
        *,
        entry_id: UUID,
        reference_id: UUID,
        requester_user_id: str,
        is_admin: bool,
    ) -> None:
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            if entry is None:
                raise BlacklistEntryNotFound(str(entry_id))
            self._enforce_owner(entry, requester_user_id, is_admin)
            reference = await repo.get_reference(reference_id)
            if reference is None or reference.entry_id != entry_id:
                raise BlacklistReferenceNotFound(str(reference_id))
            point_ids = await repo.delete_reference(reference_id)

        if self._vector_repo and point_ids:
            ok = await self._vector_repo.delete_points(point_ids)
            if not ok:
                logger.error(
                    "Qdrant cleanup failed after reference delete: ref=%s points=%d",
                    reference_id,
                    len(point_ids),
                )

    # ── Backfill ──────────────────────────────────────────────────────────

    async def trigger_backfill(
        self,
        *,
        entry_id: UUID,
        requester_user_id: str,
        is_admin: bool,
    ) -> tuple[int, list[str]]:
        """Re-run the reverse search for every PROCESSED reference under
        an entry. Returns ``(references_count, scheduled_job_ids)``.

        Use case: ops recovery after a known incident, or after a
        ``match_threshold`` change where the user wants existing
        evidence re-evaluated under the new threshold.

        We pull the stored vector for each processed reference straight
        from Qdrant rather than recomputing — same vector, same model
        version, no GPU round-trip needed.
        """
        # Imported here because the reverse-search module pulls in
        # config at import time and unit tests want to stub the
        # scheduler before that side effect.
        from ...services.blacklist_reverse_search import schedule_reverse_search

        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            entry = await repo.get_entry(entry_id)
            if entry is None:
                raise BlacklistEntryNotFound(str(entry_id))
            self._enforce_owner(entry, requester_user_id, is_admin)
            references = await repo.list_references(entry_id)
            embeddings = await repo.list_embeddings(entry_id)
            for r in references:
                session.expunge(r)
            for e in embeddings:
                session.expunge(e)

        # Map reference -> qdrant_point_id so we can fetch the stored
        # vector for each processed reference in one pass.
        ref_to_point = {emb.reference_id: emb.qdrant_point_id for emb in embeddings}
        scheduled: list[str] = []

        for reference in references:
            if reference.status != BlacklistReferenceStatus.PROCESSED:
                continue
            point_id = ref_to_point.get(reference.id)
            if not point_id or self._vector_repo is None:
                continue
            vector = await self._vector_repo.retrieve_query_vector(point_id)
            if vector is None:
                # Re-fetch via the evidence collection — query_vector is
                # for search_queries collection, blacklist points live
                # in evidence_embeddings.
                fetched = await self._vector_repo.get_embedding(point_id)
                vector = fetched.vector.tolist() if fetched else None
            if vector is None:
                logger.warning(
                    "Backfill skipped: no stored vector for ref=%s point=%s",
                    reference.id,
                    point_id,
                )
                continue

            job_id = schedule_reverse_search(
                entry_id=entry_id,
                reference_id=reference.id,
                user_id=requester_user_id,
                vector=vector,
            )
            if job_id:
                scheduled.append(job_id)

        return len(references), scheduled
