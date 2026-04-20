"""Source-type filter construction for Qdrant queries.

Every search path that queries the ``evidence_embeddings`` collection
must scope its filter by ``source_type`` explicitly. Forgetting would
mix evidence points with blacklist points in search results — a
correctness bug that's easy to introduce and hard to detect.

Centralizing the filter construction here means callers can't forget:
they call the helper that matches their intent. Code review only has
to inspect this one file for correctness.

See docs/image-blacklist/03_QDRANT.md for the design rationale.
"""

from __future__ import annotations

from typing import Any

# Canonical source_type payload values. Keep these in sync with:
# - src/streams/embedding_results_consumer.py (writes SOURCE_TYPE_EVIDENCE)
# - src/services/blacklist_embed_service.py (writes SOURCE_TYPE_BLACKLIST, Phase 04)
SOURCE_TYPE_EVIDENCE = "evidence"
SOURCE_TYPE_BLACKLIST = "blacklist"


def build_evidence_only_filter(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add ``source_type="evidence"`` to an existing filter dict.

    Use for:
      * User-facing similarity searches (must never return blacklist points)
      * Reverse search when a new blacklist entry is added (search existing
        evidence for matches against the new blacklist vector)
      * Recalculation of stored query vectors against new evidence
    """
    out = dict(base or {})
    out["source_type"] = SOURCE_TYPE_EVIDENCE
    return out


def build_blacklist_only_filter(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add ``source_type="blacklist"`` to an existing filter dict.

    Use for:
      * Inline match when a new evidence is ingested — search the user's
        blacklist subset for matches against the new evidence vector.
    """
    out = dict(base or {})
    out["source_type"] = SOURCE_TYPE_BLACKLIST
    return out


def build_blacklist_entry_filter(
    entry_id: str,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restrict to a single blacklist entry's Qdrant points.

    Use for:
      * Re-matching after an entry is edited (``blacklist_version`` bumped)
        — we only need to re-match this specific entry's vectors against
        historical evidence.
      * Deletion cleanup when we want to retrieve a specific entry's points
        via a Qdrant query instead of the SQL-side ``qdrant_point_id`` list.
    """
    out = dict(base or {})
    out["source_type"] = SOURCE_TYPE_BLACKLIST
    out["blacklist_entry_id"] = entry_id
    return out
