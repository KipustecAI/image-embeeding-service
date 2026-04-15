"""Weapon-filter translation for search requests.

Shared by the search API, the search results consumer, and the recalculation
endpoint so every path applies the same filter semantics. See
docs/weapons/04_SEARCH_API.md for the design.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_WEAPONS_FILTERS = ("all", "only", "exclude", "analyzed_clean")


def build_weapon_filter_conditions(search_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Translate weapons_filter + weapon_classes into Qdrant filter conditions.

    Reads the `weapons_filter` and (optionally) `weapon_classes` keys from the
    search_metadata dict persisted on the SearchRequest row. Returns a dict
    ready to merge into `filter_conditions` for `VectorRepository.search_similar`.

    Missing or `"all"` mode → empty dict (no additions, preserves default
    behavior). Unknown modes → empty dict + warning log.
    """
    if not search_metadata:
        return {}

    mode = search_metadata.get("weapons_filter", "all")
    if mode not in VALID_WEAPONS_FILTERS:
        logger.warning(
            f"Unknown weapons_filter '{mode}' — ignoring. Valid: {VALID_WEAPONS_FILTERS}"
        )
        return {}

    if mode == "all":
        return {}

    if mode == "only":
        conditions: dict[str, Any] = {"has_weapon": True}
        classes = search_metadata.get("weapon_classes") or []
        if classes:
            # List value triggers MatchAny in qdrant_repository.search_similar.
            conditions["weapon_classes"] = list(classes)
        return conditions

    if mode == "exclude":
        return {"has_weapon": False}

    if mode == "analyzed_clean":
        return {"weapon_analyzed": True, "has_weapon": False}

    # Unreachable — enumeration above is exhaustive
    return {}
