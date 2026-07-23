"""Consumer for search:results stream — receives pre-computed query vectors from GPU service.

Dispatches by ``purpose`` field:

* ``purpose="search"`` (default) — runs Qdrant similarity search and persists
  matches in ``search_matches``. Existing user-facing flow.
* ``purpose="blacklist_embed"`` — stores the vector as a blacklist point and
  schedules a reverse-search job (Phase 04, see
  docs/image-blacklist/04_EMBEDDING_FLOW.md).

The compute service echoes ``purpose`` byte-identical so legacy callers
(no field) get the default ``"search"`` behavior unchanged.
"""

import asyncio
import logging
import uuid as uuid_mod
from datetime import datetime
from uuid import UUID

import numpy as np

from ..application.helpers.source_type_filter import build_evidence_only_filter
from ..application.helpers.weapon_filters import build_weapon_filter_conditions
from ..db.models.constants import (
    BlacklistReferenceStatus,
    SearchRequestStatus,
    SearchType,
    SimilarityStatus,
)
from ..db.models.search_match import SearchMatch
from ..db.repositories import SearchRequestRepository
from ..db.repositories.blacklist_image_repo import BlacklistImageRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..infrastructure.vector_db.image_index_vector_repository import _is_finite_nonzero
from ..services.dw_publisher_service import (
    publish_blacklist_image_reference,
    publish_image_search_matches,
    publish_image_search_request,
)
from .consumer import StreamConsumer

logger = logging.getLogger(__name__)
settings = get_settings()

_event_loop: asyncio.AbstractEventLoop | None = None
_vector_repo = None
# Dedicated image-index collection repo (Capability A). Wired ONLY inside the
# gated lifespan block; None when the feature is off → the branch marks ERROR
# and never falls through to the evidence search (M8).
_image_index_vector_repo = None


def set_search_results_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def set_search_vector_repo(repo):
    global _vector_repo
    _vector_repo = repo


def set_search_image_index_vector_repo(repo):
    global _image_index_vector_repo
    _image_index_vector_repo = repo


def create_search_results_consumer() -> StreamConsumer:
    consumer = StreamConsumer(
        stream=settings.stream_search_results,
        group=settings.stream_backend_group,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password or None,
        redis_db=settings.redis_streams_db,
        block_ms=settings.stream_consumer_block_ms,
        batch_size=settings.stream_consumer_batch_size,
        reclaim_idle_ms=settings.stream_reclaim_idle_ms,
        dead_letter_max_retries=settings.stream_dead_letter_max_retries,
        concurrency=settings.stream_consumer_concurrency,
    )
    consumer.register_handler("search.vector.computed", _handle_search_computed)
    consumer.register_handler("compute.error", _handle_compute_error)
    return consumer


def _handle_search_computed(event_type: str, payload: dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_search_result(payload, message_id),
        _event_loop,
    )
    future.result(timeout=120)


def _handle_compute_error(event_type: str, payload: dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_compute_error(payload),
        _event_loop,
    )
    future.result(timeout=30)


async def _process_search_result(payload: dict, message_id: str):
    """Receive query vector → dispatch by ``purpose`` → search or blacklist-embed."""
    search_id = payload.get("search_id", "")
    user_id = payload.get("user_id", "")
    vector = payload.get("vector")
    purpose = payload.get("purpose", "search")

    if not search_id or vector is None:
        logger.warning(f"Skipping search result with missing data: {search_id}")
        return

    if purpose == "blacklist_embed":
        await _process_blacklist_embed_result(payload, message_id)
        return

    # Image-index SEARCH (Capability A) — pure additive EARLY-RETURN keyed on the
    # discriminator in the echoed metadata (M1/M8). When absent, the evidence +
    # blacklist_embed blocks below are byte-identical. The whole handler wraps
    # its own try/except → marks only the image-index row ERROR, never re-raises.
    meta = payload.get("metadata") or {}
    if meta.get("search_type") == SearchType.IMAGE_INDEX:
        await _process_image_index_search_result(payload, message_id)
        return

    threshold = payload.get("threshold", 0.75)
    max_results = payload.get("max_results", 50)
    search_metadata = payload.get("metadata")

    try:
        query_vector = np.array(vector, dtype=np.float32)

        # Build filter conditions. Explicit allow-list keeps client-supplied
        # metadata from injecting arbitrary Qdrant filter keys.
        filter_conditions: dict = {}
        if search_metadata:
            if "camera_id" in search_metadata:
                filter_conditions["camera_id"] = search_metadata["camera_id"]
            if "object_type" in search_metadata:
                filter_conditions["object_type"] = search_metadata["object_type"]
            if "category" in search_metadata:
                # Scalar or list — list triggers MatchAny in qdrant_repository.search_similar.
                filter_conditions["category"] = search_metadata["category"]
            # Weapons — translated from weapons_filter mode via shared helper
            filter_conditions.update(build_weapon_filter_conditions(search_metadata))

        # User-facing search — never return blacklist points.
        # See docs/image-blacklist/03_QDRANT.md.
        filter_conditions = build_evidence_only_filter(filter_conditions)

        # Search Qdrant
        matches = []
        if _vector_repo:
            matches = await _vector_repo.search_similar(
                query_vector=query_vector,
                limit=max_results,
                threshold=threshold,
                filter_conditions=filter_conditions,
            )

        total_matches = len(matches)
        similarity_status = (
            SimilarityStatus.MATCHES_FOUND if total_matches > 0 else SimilarityStatus.NO_MATCHES
        )

        # Store query vector in Qdrant for future recalculation
        query_point_id = str(uuid_mod.uuid4())
        if _vector_repo:
            await _vector_repo.store_query_vector(
                point_id=query_point_id,
                vector=query_vector.tolist(),
                search_id=search_id,
            )

        # Store everything in one transaction: request + match rows
        match_rows_for_dw: list[SearchMatch] = []
        async with get_session() as session:
            repo = SearchRequestRepository(session)

            # Find existing request (created by POST /api/v1/search) or create new one
            request = await repo.get_by_search_id(search_id)

            if request is None:
                # Search came from stream (not API) — create the request row
                request = await repo.create_request(
                    search_id=search_id,
                    user_id=user_id,
                    image_url="(computed by GPU service)",
                    threshold=threshold,
                    max_results=max_results,
                    metadata=search_metadata,
                    stream_msg_id=message_id,
                )

            # Update status
            request.status = SearchRequestStatus.COMPLETED
            request.similarity_status = similarity_status
            request.total_matches = total_matches
            request.processing_completed_at = datetime.utcnow()
            request.qdrant_query_point_id = query_point_id

            # Clear old matches if this is a recalculation
            if request.matches:
                request.matches.clear()
                await session.flush()

            # Create SearchMatch rows for each result, keep refs for DW publish
            for match in matches:
                m = SearchMatch(
                    search_request_id=request.id,
                    evidence_id=str(match.evidence_id),
                    camera_id=str(match.camera_id) if match.camera_id else None,
                    similarity_score=match.similarity_score,
                    image_url=match.image_url,
                    match_metadata=match.metadata,
                )
                session.add(m)
                match_rows_for_dw.append(m)

            # Snapshot before session closes — DW publishers run after commit.
            await session.flush()
            for m in match_rows_for_dw:
                session.expunge(m)
            session.expunge(request)
            request_snapshot = request
            request_user_id = request.user_id
            request_image_url = request.image_url

        logger.info(f"Search {search_id}: {total_matches} matches stored (threshold={threshold})")

        # ── DW publishers (fire-and-forget) ──
        # `image_search_request.completed` lifecycle event.
        publish_image_search_request("image_search.completed", request_snapshot)
        # `image_search.matched` — only when total_matches > 0 per contract §4.2.
        if total_matches > 0:
            publish_image_search_matches(
                search_request_id=request_snapshot.id,
                user_id=request_user_id,
                image_url=request_image_url,
                total_matches=total_matches,
                matches=match_rows_for_dw,
            )

    except Exception as e:
        logger.error(f"Failed to process search {search_id}: {e}", exc_info=True)
        failed_request = None
        async with get_session() as session:
            repo = SearchRequestRepository(session)
            request = await repo.get_by_search_id(search_id)
            if request:
                request.status = SearchRequestStatus.ERROR
                request.error_message = str(e)[:500]
            else:
                request = await repo.create_request(
                    search_id=search_id,
                    user_id=user_id,
                    image_url="(failed)",
                    stream_msg_id=message_id,
                )
                request.status = SearchRequestStatus.ERROR
                request.error_message = str(e)[:500]
            await session.flush()
            session.expunge(request)
            failed_request = request

        if failed_request is not None:
            publish_image_search_request("image_search.failed", failed_request)


async def _mark_image_index_search_error(
    search_id: str, message: str
) -> None:
    """Terminalize an image-index search row to ERROR. Never re-raises (M8)."""
    try:
        async with get_session() as session:
            repo = SearchRequestRepository(session)
            request = await repo.get_by_search_id(search_id)
            if request is not None:
                request.status = SearchRequestStatus.ERROR
                request.error_message = message[:500]
                await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to mark image-index search %s ERROR: %s", search_id, e)


async def _process_image_index_search_result(payload: dict, message_id: str):
    """Land a Capability-A query vector against ``image_index_embeddings`` (§6.3).

    A near-clone of the evidence block pointed at the dedicated collection,
    ENTIRELY wrapped in try/except that marks the row ERROR and returns cleanly
    (never re-raises), so it can never regress the shared live consumer (M8).
    Searches scoped by ``owner_id`` (from metadata.user_id) + ``external_ids``;
    lands matches with ``evidence_id = item_ref or qdrant_point_id`` (M2).
    """
    search_id = payload.get("search_id", "")
    meta = payload.get("metadata") or {}
    user_id = meta.get("user_id") or payload.get("user_id", "")
    vector = payload.get("vector")
    threshold = payload.get("threshold", 0.75)
    max_results = payload.get("max_results", 50)
    external_ids = meta.get("external_ids")

    try:
        # Guard 1: repo not wired (flag off / degraded) → ERROR. NEVER fall
        # through to the evidence search (that would be a cross-tenant leak).
        if _image_index_vector_repo is None:
            logger.error(
                "image-index search %s: dedicated repo not wired — marking ERROR",
                search_id,
            )
            await _mark_image_index_search_error(
                search_id, "image-index repo unavailable"
            )
            return

        # Guard 2: degenerate query vector (§3/S4).
        if vector is None or not _is_finite_nonzero(vector):
            logger.warning(
                "image-index search %s: zero/NaN query vector — marking ERROR",
                search_id,
            )
            await _mark_image_index_search_error(search_id, "degenerate query vector")
            return

        query_vector = np.array(vector, dtype=np.float32)

        matches = await _image_index_vector_repo.search_similar(
            query_vector,
            user_id=user_id,
            external_ids=external_ids,
            top_k=max_results,
            threshold=threshold,
        )

        total_matches = len(matches)
        similarity_status = (
            SimilarityStatus.MATCHES_FOUND
            if total_matches > 0
            else SimilarityStatus.NO_MATCHES
        )

        # Store the query vector on the LIVE repo (search_queries collection) so
        # the image-index recalc branch can re-search for free. Idempotent.
        query_point_id = str(uuid_mod.uuid4())
        if _vector_repo is not None:
            await _vector_repo.store_query_vector(
                point_id=query_point_id,
                vector=query_vector.tolist(),
                search_id=search_id,
            )

        async with get_session() as session:
            repo = SearchRequestRepository(session)
            request = await repo.get_by_search_id(search_id)
            if request is None:
                # Row should already exist (POST created it) — defensively
                # create it correctly typed so it can never become 'evidence'.
                request = await repo.create_request(
                    search_id=search_id,
                    user_id=user_id,
                    image_url="(computed by GPU service)",
                    threshold=threshold,
                    max_results=max_results,
                    metadata=payload.get("metadata"),
                    stream_msg_id=message_id,
                    search_type=SearchType.IMAGE_INDEX,
                    external_ids=external_ids,
                    status=SearchRequestStatus.WORKING,
                    processing_started_at=datetime.utcnow(),
                )

            request.status = SearchRequestStatus.COMPLETED
            request.similarity_status = similarity_status
            request.total_matches = total_matches
            request.processing_completed_at = datetime.utcnow()
            request.qdrant_query_point_id = query_point_id

            # Clear old matches if this is a re-land / recalculation.
            if request.matches:
                request.matches.clear()
                await session.flush()

            for m in matches:
                point_id = m.get("qdrant_point_id")
                # NOT-NULL fallback — search_matches.evidence_id is NOT NULL and
                # item_ref is nullable; image_id already carries the fallback (M2).
                evidence_id = m.get("image_id") or point_id
                session.add(
                    SearchMatch(
                        search_request_id=request.id,
                        evidence_id=str(evidence_id),
                        camera_id=None,
                        similarity_score=m.get("score"),
                        image_url=m.get("source_url"),
                        external_id=m.get("external_id"),
                        match_metadata={
                            "batch_id": m.get("batch_id"),
                            "item_index": m.get("item_index"),
                            "item_ref": m.get("item_ref"),
                            "qdrant_point_id": point_id,
                            "search_type": SearchType.IMAGE_INDEX,
                        },
                    )
                )
            await session.commit()

        logger.info(
            "image-index search %s: %d matches stored (threshold=%s)",
            search_id,
            total_matches,
            threshold,
        )

    except Exception as e:  # noqa: BLE001 — never re-raise into the shared consumer
        logger.error(
            "Failed to process image-index search %s: %s", search_id, e, exc_info=True
        )
        await _mark_image_index_search_error(search_id, str(e))


async def _process_blacklist_embed_result(payload: dict, message_id: str):
    """Hand off a blacklist_embed result to the embed service.

    ``search_id`` here is overloaded — for blacklist embeds it carries the
    ``BlacklistImageReference.id`` rather than a regular search id. See
    docs/image-blacklist/04_EMBEDDING_FLOW.md §"The purpose field" for the
    rationale (we accepted the overloading rather than adding a parallel
    ``reference_id`` field that the GPU would have to echo).
    """
    reference_id_raw = payload.get("search_id", "")
    entry_id_raw = payload.get("blacklist_entry_id")
    user_id = payload.get("user_id", "")
    vector = payload.get("vector")

    if not entry_id_raw:
        logger.error(
            "blacklist_embed result missing blacklist_entry_id (ref=%s); dropping",
            reference_id_raw,
        )
        return

    try:
        reference_uuid = UUID(reference_id_raw)
        entry_uuid = UUID(entry_id_raw)
    except (TypeError, ValueError) as e:
        logger.error(
            "blacklist_embed result has malformed UUIDs (ref=%s entry=%s err=%s); dropping",
            reference_id_raw,
            entry_id_raw,
            e,
        )
        return

    # Imported here to avoid a circular import at module load — the embed
    # service imports from this consumer's siblings, and we only need it
    # on the blacklist path which is rare relative to the search path.
    from ..services.blacklist_embed_service import store_blacklist_embedding

    try:
        await store_blacklist_embedding(
            entry_id=entry_uuid,
            reference_id=reference_uuid,
            user_id=user_id,
            vector=list(vector),
            stream_msg_id=message_id,
        )
    except Exception as e:
        # Don't mark PROCESSED — the consumer's retry will redeliver.
        logger.error(
            "Failed to store blacklist embedding entry=%s ref=%s: %s",
            entry_uuid,
            reference_uuid,
            e,
            exc_info=True,
        )


async def _process_compute_error(payload: dict):
    """Mark either a search request or a blacklist reference as ERROR.

    Compute's ``compute.error`` envelope **does not carry ``purpose``** —
    see docs/image-blacklist/04_EMBEDDING_FLOW.md §"Error routing" and the
    upstream contract at image-embedding-compute/docs/CONTRACT.md §3.4.
    We dispatch by looking up ``entity_id`` first in the blacklist
    references table (cheap UUID PK lookup) and fall through to the
    search-requests path on miss. UUID-namespace collisions across the
    two tables are not possible in practice.
    """
    entity_id = payload.get("entity_id", "")
    entity_type = payload.get("entity_type", "")
    error = payload.get("error", "Unknown compute error")

    if entity_type != "search":
        return

    # Try blacklist-reference resolution first.
    try:
        ref_uuid: UUID | None = UUID(entity_id)
    except (TypeError, ValueError):
        ref_uuid = None

    if ref_uuid is not None:
        reference_after_error = None
        async with get_session() as session:
            bl_repo = BlacklistImageRepository(session)
            reference = await bl_repo.get_reference(ref_uuid)
            if reference is not None:
                await bl_repo.update_reference_status(
                    reference.id,
                    status=BlacklistReferenceStatus.ERROR,
                    error=f"Compute error: {error}"[:500],
                )
                reference_after_error = await bl_repo.get_reference(ref_uuid)
                if reference_after_error is not None:
                    await session.flush()
                    session.expunge(reference_after_error)
        if reference is not None:
            logger.error("Blacklist embed compute error: ref=%s err=%s", ref_uuid, error)
            if reference_after_error is not None:
                # DW publish on reference status → ERROR (contract §4.4).
                publish_blacklist_image_reference(reference_after_error)
            return

    # Fall through to the legacy search-request path.
    failed_request_snapshot = None
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        request = await repo.get_by_search_id(entity_id)
        if request:
            request.status = SearchRequestStatus.ERROR
            request.error_message = f"Compute error: {error}"
        else:
            request = await repo.create_request(
                search_id=entity_id,
                user_id="unknown",
                image_url="(failed)",
            )
            request.status = SearchRequestStatus.ERROR
            request.error_message = f"Compute error: {error}"
        await session.flush()
        session.expunge(request)
        failed_request_snapshot = request

    logger.error(f"Compute error for search {entity_id}: {error}")

    # DW-silent for image-index searches (02_SEARCH_DESIGN §6.4): the DW pipeline
    # only knows the evidence-shaped image_search.* lifecycle. Emitting an
    # image_search.failed for an image-index row would leak an evidence-shaped
    # event (with null evidence fields) to report-generation. Status stays ERROR
    # either way; only the DW publish is gated to the evidence path.
    if failed_request_snapshot is not None and (
        getattr(failed_request_snapshot, "search_type", None) != SearchType.IMAGE_INDEX
    ):
        publish_image_search_request("image_search.failed", failed_request_snapshot)
