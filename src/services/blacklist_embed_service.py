"""Success path for `purpose="blacklist_embed"` results.

Called from search_results_consumer when a vector returned by the GPU
service was originally requested as a blacklist reference embed (see
docs/image-blacklist/04_EMBEDDING_FLOW.md). Performs three things in
order:

  1. Upsert the vector into Qdrant with ``source_type="blacklist"`` so it
     is invisible to user-facing similarity searches.
  2. Persist a ``BlacklistImageEmbedding`` row joining the reference to
     its Qdrant point, and bump the reference + entry statuses.
  3. Schedule an APScheduler one-shot reverse-search job so historical
     evidence gets matched against the new vector without blocking
     ingest.

The Qdrant-first ordering is deliberate. The Qdrant point id is the join
key for the DB row — if the SQL insert fails after Qdrant succeeded we
have an orphan point that a future reconciliation job can detect (and
that won't match against anything until SQL knows about it). If we did
the inverse and the Qdrant write failed, we'd have an indexed reference
in SQL pointing to no vector and the next reverse search would silently
miss it.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from ..db.models.constants import BlacklistEntryStatus, BlacklistReferenceStatus
from ..db.repositories.blacklist_image_repo import BlacklistImageRepository
from ..infrastructure.database import get_session
from .dw_publisher_service import (
    publish_blacklist_image_embedding,
    publish_blacklist_image_entry,
    publish_blacklist_image_reference,
)

logger = logging.getLogger(__name__)

_MODEL_VERSION = "clip-vit-b-32"

_vector_repo = None  # QdrantVectorRepository, set at startup


def set_blacklist_vector_repo(repo) -> None:
    """Inject the Qdrant repository instance.

    Same pattern as the other consumer-side services so unit tests can
    swap in a fake without touching FastAPI's lifespan.
    """
    global _vector_repo
    _vector_repo = repo


async def store_blacklist_embedding(
    *,
    entry_id: UUID,
    reference_id: UUID,
    user_id: str,
    vector: list[float],
    stream_msg_id: str,
) -> None:
    """Persist a completed blacklist reference embedding.

    Drops the message silently when the entry or reference no longer
    exists — the upstream call could have lapped a delete (we honor the
    delete request even when an in-flight embed is still landing).
    """
    if _vector_repo is None:
        logger.error(
            "Blacklist vector repo not configured; dropping embed for ref=%s",
            reference_id,
        )
        return

    point_id = str(uuid4())

    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        entry = await repo.get_entry(entry_id)
        reference = await repo.get_reference(reference_id)

        if entry is None or reference is None:
            logger.error(
                "Blacklist embed missing entry=%s ref=%s; dropping (msg=%s)",
                entry_id,
                reference_id,
                stream_msg_id,
            )
            return

        # Snapshot what we need from inside the session so the rest of
        # the function can run without a live transaction.
        category = entry.category
        image_url = reference.image_url

    qdrant_payload = {
        "source_type": "blacklist",
        "blacklist_entry_id": str(entry_id),
        "blacklist_reference_id": str(reference_id),
        "user_id": user_id,
        "model_version": _MODEL_VERSION,
        "image_url": image_url,
    }
    if category:
        qdrant_payload["category"] = category

    qdrant_ok = await _vector_repo.store_raw_point(
        point_id=point_id,
        vector=vector,
        payload=qdrant_payload,
    )
    if not qdrant_ok:
        # Don't mark PROCESSED — the consumer's DLQ retry will re-deliver
        # this message and we'll try again. Logging is enough on this path.
        logger.error(
            "Qdrant upsert failed for blacklist ref=%s entry=%s; will retry",
            reference_id,
            entry_id,
        )
        return

    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        embedding_row = await repo.create_embedding(
            entry_id=entry_id,
            reference_id=reference_id,
            qdrant_point_id=point_id,
            model_version=_MODEL_VERSION,
        )
        await repo.update_reference_status(reference_id, status=BlacklistReferenceStatus.PROCESSED)
        # Lift the entry to INDEXED on the first successful reference.
        # Subsequent references just keep it at INDEXED (no-op).
        await repo.update_entry_status(entry_id, status=BlacklistEntryStatus.INDEXED)

        # Reload updated rows so the DW publishers see post-update state.
        reference_after = await repo.get_reference(reference_id)
        entry_after = await repo.get_entry(entry_id)
        await session.flush()
        session.expunge(embedding_row)
        if reference_after is not None:
            session.expunge(reference_after)
        if entry_after is not None:
            session.expunge(entry_after)

    logger.info(
        "Stored blacklist embedding: entry=%s ref=%s point=%s",
        entry_id,
        reference_id,
        point_id,
    )

    # ── lookia-dw publishers (fire-and-forget) ──
    # Embedding INSERT — immutable, single emission (contract §4.5).
    publish_blacklist_image_embedding(embedding_row)
    # Reference status UPDATE → PROCESSED (contract §4.4).
    if reference_after is not None:
        publish_blacklist_image_reference(reference_after)
    # Entry status UPDATE → INDEXED (contract §4.3).
    if entry_after is not None:
        publish_blacklist_image_entry(entry_after)

    # Reverse search runs after the DB commit so a job restart will see
    # consistent state if it queries the embedding row.
    from .blacklist_reverse_search import schedule_reverse_search

    schedule_reverse_search(
        entry_id=entry_id,
        reference_id=reference_id,
        user_id=user_id,
        vector=vector,
    )
