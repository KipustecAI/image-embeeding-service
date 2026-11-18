"""Tenant scope + Qdrant filter for user-facing image search — ONE place.

Every path that runs a user search against ``evidence_embeddings`` builds its
filter here: the live search (``search_results_consumer``), the hourly
recalculation (``safety_nets.recalculate_searches``) and the manual one
(``POST /api/v1/recalculate/searches``). Before 2026-11-17 each path built its
own filter and none applied the tenant, so a non-admin search matched every
tenant's evidence (and the hourly one could return blacklist points).

Scope is decided at submit time (``resolve_scope_metadata``, called by
``POST /api/v1/search``) and stored in ``search_metadata``:

  * ``scope_user_ids``  the owners whose evidence the search may match:
                        ``[owner_id]`` for a normal user (a guest → the inviter),
                        the hub members for ``hub_id``. ``[]`` = match NOTHING.
                        Absent = no tenant filter (admin / root / dev only).
  * ``hub_id``          the hub of a hub-scoped search (also sent to the DW).

Legacy rows (created before this module) carry ``user_id`` instead; it is honoured
as a one-owner scope so their recalculation is scoped too.
"""

from __future__ import annotations

from typing import Any

from .source_type_filter import build_evidence_only_filter
from .weapon_filters import build_weapon_filter_conditions

SCOPE_KEY = "scope_user_ids"
HUB_KEY = "hub_id"
LEGACY_USER_KEY = "user_id"
ADMIN_ROLES = frozenset({"admin", "root", "dev"})

# Keys a client may NOT set in ``metadata``: they decide the tenant scope or the
# consumer's dispatch (``search_type`` routes to the image-index handler).
RESERVED_METADATA_KEYS = frozenset({SCOPE_KEY, HUB_KEY, LEGACY_USER_KEY, "search_type"})

# Client-visible filters copied from search_metadata into the Qdrant filter.
_PASSTHROUGH_FILTERS = ("camera_id", "object_type", "category")


class MatchNothing:
    """Sentinel: the scope is empty (a member-less hub) — run no query."""


MATCH_NOTHING = MatchNothing()


def is_admin_role(role: str | None) -> bool:
    return role in ADMIN_ROLES


def strip_reserved(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (metadata or {}).items() if k not in RESERVED_METADATA_KEYS}


def scope_user_ids(search_metadata: dict[str, Any] | None) -> list[str] | None:
    """The owners a stored search is scoped to; ``None`` = unscoped (admin)."""
    meta = search_metadata or {}
    if SCOPE_KEY in meta:
        value = meta[SCOPE_KEY]
        if not isinstance(value, list):
            # A malformed scope never widens to "everything".
            return []
        return [str(v) for v in value if v]
    legacy = meta.get(LEGACY_USER_KEY)
    if legacy:
        return [str(legacy)]
    return None


def build_search_filter(search_metadata: dict[str, Any] | None) -> dict[str, Any] | MatchNothing:
    """Qdrant filter for a user search, or ``MATCH_NOTHING``.

    Always evidence-only (never blacklist points), always tenant-scoped unless the
    stored scope says "unscoped" (admin).
    """
    meta = search_metadata or {}
    conditions: dict[str, Any] = {k: meta[k] for k in _PASSTHROUGH_FILTERS if k in meta}
    conditions.update(build_weapon_filter_conditions(meta))

    owners = scope_user_ids(meta)
    if owners is not None:
        if not owners:
            return MATCH_NOTHING
        # A list → MatchAny in QdrantVectorRepository.search_similar.
        conditions["user_id"] = owners

    return build_evidence_only_filter(conditions)
