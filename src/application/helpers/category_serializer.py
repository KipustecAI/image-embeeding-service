"""Serialize an upstream `entities: list[int]` into the two on-disk shapes.

Two stores, two shapes:

  * ``embedding_requests.category TEXT`` (Postgres) → compact JSON string,
    e.g. ``"[2,5]"``. Used by ``GET /api/v1/search/categories`` via
    ``SELECT DISTINCT category`` to populate the frontend dropdown.

  * Qdrant payload ``category`` → list of stringified ids,
    e.g. ``["2", "5"]``. Indexed as keyword[] so a single-id filter
    like ``"2"`` matches both ``["2"]`` and ``["2","5"]`` points via
    Qdrant's ``MatchAny``.

Sorted + deduped before serializing so ``[5, 2, 2]`` and ``[2, 5]`` collapse
to the same bucket — frontend dropdown counts match user intuition.

See docs/requirements/IMAGE_COMPUTE_STREAMS.md §2 for the negotiation trail.
"""

from __future__ import annotations

import json
from collections.abc import Iterable


def entities_to_category(
    entities_raw: Iterable[int] | None,
) -> tuple[str | None, list[str] | None]:
    """Return ``(db_value, qdrant_value)`` for the category column.

    Both values are ``None`` when the input is empty/missing — represents
    "no category info on this evidence", which the consumer writes as
    SQL NULL (filterable as IS NULL on the dashboard, ignored by the
    distinct-category endpoint).

    Non-int entries in the input are coerced via ``int(...)`` to tolerate
    upstream JSON-string ids; failures bubble up as ``ValueError`` so we
    notice malformed payloads rather than silently dropping data.
    """
    if not entities_raw:
        return None, None

    sorted_entities = sorted({int(e) for e in entities_raw})
    if not sorted_entities:
        return None, None

    db_value = json.dumps(sorted_entities, separators=(",", ":"))
    qdrant_value = [str(e) for e in sorted_entities]
    return db_value, qdrant_value
