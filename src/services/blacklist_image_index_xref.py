"""Capability B — GPU-free blacklist cross-reference (02_SEARCH_DESIGN §7).

"Does this blacklisted image appear in these indexed runs?" — take a blacklist
entry's **already-stored** CLIP vectors (no compute round-trip) and reverse-search
the dedicated ``image_index_embeddings`` collection scoped by tenant +
``external_id`` (and ``batch_id`` when the auto-hook passes one). This is the
[`blacklist_reverse_search`](blacklist_reverse_search.py) pattern re-pointed at a
different collection — **zero GPU, zero compute stream dispatch**.

One match core (`cross_reference_entry`), two callers:
  * The synchronous REST endpoint (v1) on the blacklist router — an operator
    posts ``{entry_id} + external_ids``; we return matches inline.
  * The gated auto-on-land hook (v1.1, `auto_cross_reference_batch`) fired from
    ``image_index_results_consumer._process_computed`` after the terminal
    ``image_batch.completed`` publish — the whole tenant's active blacklist ×
    the just-landed ``batch_id``, fire-and-forget ``image:blacklist_match`` events.

Same-space guards (§3):
  * **model_version** — compare the blacklist point's stored ``model_version``
    against the indexed point's payload ``model_version``; log-and-skip on
    mismatch (a future encoder bump on one path but not the other would silently
    decalibrate the cosine).
  * **degenerate vector** — skip-and-log a zero/NaN blacklist reference vector
    before issuing the search (cosine is undefined; ``search_similar`` also
    guards the query side).

IDOR (§9): the blacklist entry must belong to the tenant (``entry.user_id ==
user_id``) before any ``get_point_vector`` — a foreign ``entry_id`` yields nothing
to search with. ``search_similar`` additionally ANDs ``user_id`` in the Qdrant
filter, so a foreign/other-tenant ``external_id`` contributes zero hits.
"""

from __future__ import annotations

import logging
from uuid import UUID

from ..db.repositories.blacklist_image_repo import BlacklistImageRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..infrastructure.vector_db.image_index_vector_repository import _is_finite_nonzero

logger = logging.getLogger(__name__)

# Injected at startup from main.py's gated lifespan block (§7.5). Both are already
# singletons there. Blacklist vectors live in evidence_embeddings (the live repo);
# indexed images live in the dedicated image_index_embeddings collection.
_evidence_repo = None  # QdrantVectorRepository
_image_index_repo = None  # ImageIndexVectorRepository


def set_xref_evidence_repo(repo) -> None:
    """Inject the live ``QdrantVectorRepository`` (blacklist reference vectors)."""
    global _evidence_repo
    _evidence_repo = repo


def set_xref_image_index_repo(repo) -> None:
    """Inject the dedicated ``ImageIndexVectorRepository`` (indexed images)."""
    global _image_index_repo
    _image_index_repo = repo


async def cross_reference_entry(
    *,
    user_id: str,
    entry_id: UUID,
    external_ids: list[str] | None = None,
    batch_id: str | None = None,
    threshold: float | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Reverse-search the image-index collection with a blacklist entry's vectors.

    For each of the entry's reference points: fetch its stored CLIP vector from
    ``evidence_embeddings`` (``get_point_vector`` — NO re-embed), then
    ``search_similar`` the dedicated collection scoped by ``user_id`` +
    ``external_ids`` (+ ``batch_id`` for the auto-hook). Aggregate the best
    (max) score per **indexed** point so a multi-reference entry never returns the
    same indexed image twice. Returns ``XrefMatch``-shaped dicts, sorted by score
    descending. A tenant/entry miss, unwired repos, or all-orphaned references all
    return ``[]``. **Never dispatches to any compute stream.**
    """
    if _evidence_repo is None or _image_index_repo is None:
        logger.error(
            "cross_reference_entry called before repos wired (entry=%s)", entry_id
        )
        return []

    settings = get_settings()
    thr = threshold if threshold is not None else settings.blacklist_match_threshold
    lim = limit or settings.image_index_xref_limit

    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        entry = await repo.get_entry(entry_id)
        if entry is None or entry.user_id != user_id:  # IDOR — tenant miss ⇒ nothing
            return []
        # Per-entry override only when the caller did not force a threshold.
        if threshold is None and entry.match_threshold is not None:
            thr = entry.match_threshold
        embeddings = await repo.list_embeddings(entry_id)

    best: dict[str, dict] = {}  # indexed qdrant_point_id → best match
    for emb in embeddings:
        vec = await _evidence_repo.get_point_vector(emb.qdrant_point_id)  # NO compute
        if vec is None:
            logger.warning(
                "xref: orphaned blacklist point %s (entry %s)",
                emb.qdrant_point_id,
                entry_id,
            )
            continue
        if not _is_finite_nonzero(vec):  # degenerate-vector guard (§3, S4)
            logger.warning(
                "xref: degenerate blacklist vector point %s (entry %s); skip",
                emb.qdrant_point_id,
                entry_id,
            )
            continue

        hits = await _image_index_repo.search_similar(
            vec,
            user_id=user_id,
            external_ids=external_ids,
            batch_id=batch_id,
            top_k=lim,
            threshold=thr,
        )
        for h in hits:
            # Same-space model-version guard (§3): the blacklist point's stored
            # model_version vs the indexed point's payload model_version.
            if h.get("model_version") != emb.model_version:
                logger.warning(
                    "xref: model_version mismatch blacklist=%s indexed=%s point=%s; skip",
                    emb.model_version,
                    h.get("model_version"),
                    h.get("qdrant_point_id"),
                )
                continue
            key = h["qdrant_point_id"]
            prev = best.get(key)
            if prev is None or h["score"] > prev["similarity_score"]:
                best[key] = {
                    "blacklist_entry_id": str(entry_id),
                    "blacklist_reference_id": str(emb.reference_id),
                    "external_id": h["external_id"],
                    "batch_id": h["batch_id"],
                    "item_index": h["item_index"],
                    "image_id": h["image_id"],  # item_ref or qdrant_point_id (§4)
                    "source_url": h["source_url"],
                    "qdrant_point_id": key,
                    "similarity_score": h["score"],
                    "threshold_used": thr,
                }

    return sorted(best.values(), key=lambda m: m["similarity_score"], reverse=True)


# ── Auto-on-land hook (v1.1, gated by image_index_blacklist_autocheck_enabled) ──


async def auto_cross_reference_batch(*, user_id: str, batch_id: str) -> int:
    """Fire-and-forget blacklist cross-reference for a just-landed batch (§7.4).

    Fast-exits when the tenant has no active+indexed blacklist entries (the same
    ``count_active_by_user`` optimization the evidence inline path uses). For each
    active entry, cross-references against the just-landed ``batch_id`` and
    publishes one ``image:blacklist_match`` per hit with the additive image-index
    fields (S8). **Never raises** — a match-scan failure must never block batch
    completion. Returns the number of published matches (for logging/tests).
    """
    if _evidence_repo is None or _image_index_repo is None:
        return 0

    try:
        async with get_session() as session:
            repo = BlacklistImageRepository(session)
            if await repo.count_active_by_user(user_id) == 0:
                return 0  # fast-exit — nothing to check against
            entries = await repo.list_active_indexed_entries(user_id)
    except Exception as e:  # noqa: BLE001 — never block the land path
        logger.error(
            "auto-xref: failed to load active entries (user=%s batch=%s): %s",
            user_id,
            batch_id,
            e,
        )
        return 0

    published = 0
    for entry in entries:
        try:
            matches = await cross_reference_entry(
                user_id=user_id, entry_id=entry.id, batch_id=batch_id
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "auto-xref: cross_reference_entry failed (entry=%s batch=%s): %s",
                entry.id,
                batch_id,
                e,
            )
            continue
        for match in matches:
            try:
                await _publish_autocheck_match(
                    user_id=user_id, entry=entry, match=match, batch_id=batch_id
                )
                published += 1
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "auto-xref: publish failed (entry=%s point=%s): %s",
                    entry.id,
                    match.get("qdrant_point_id"),
                    e,
                )
    logger.info(
        "auto-xref complete: user=%s batch=%s entries=%d published=%d",
        user_id,
        batch_id,
        len(entries),
        published,
    )
    return published


async def _publish_autocheck_match(*, user_id: str, entry, match: dict, batch_id: str) -> None:
    """Publish one auto-hook match via the shared ``publish_blacklist_match`` (S8).

    Reuses the same stream/event; the additive ``match_target="image_index"`` +
    ``external_id`` + ``batch_id`` fields ride the widened builder (defaults
    byte-identical for evidence callers). Evidence-side fields are null — the
    image-index payload carries no camera/device/app/infraction data. ``entry``
    is the (detached, expire_on_commit=False) row from the active-entry listing;
    the reference is loaded fresh by ``blacklist_reference_id``.
    """
    from .blacklist_match_service import publish_blacklist_match

    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        reference = await repo.get_reference(UUID(match["blacklist_reference_id"]))
    if reference is None:
        logger.warning(
            "auto-xref: reference %s gone before publish; skip",
            match["blacklist_reference_id"],
        )
        return

    await publish_blacklist_match(
        user_id=user_id,
        entry=entry,
        reference=reference,
        evidence_id=str(match["image_id"]),  # item_ref or qdrant_point_id (M2)
        evidence_metadata={},  # image-index carries no evidence fields
        matched_image_url=match["source_url"] or "",
        matched_image_index=match["item_index"] or 0,
        matched_qdrant_point_id=str(match["qdrant_point_id"]),
        similarity_score=match["similarity_score"],
        threshold_used=match["threshold_used"],
        trigger="image_index_xref",
        match_target="image_index",
        external_id=match["external_id"],
        batch_id=str(batch_id),
    )
