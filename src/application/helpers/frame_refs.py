"""Frame-from-ZIP reference helpers (Lookia Platform#frames-desde-zip).

Pure functions shared by the ``embeddings:results`` consumer (ingest, contract
A) and the search-match writers (contract B). No I/O, no settings reads except
``frame_url`` which takes the base URL as an argument.

* ``frame_url`` — the public URL of ``GET /api/v1/frames/{qdrant_point_id}``
  stored as ``image_url`` in ``FRAME_STORAGE_MODE=zip_ref``.
* ``strip_cache_buster`` — never persist a ``?nc=<token>`` variant of a ZIP URL.
* ``search_match_frame_fields`` — the nullable frame columns of a
  ``search_matches`` row, read from the matched Qdrant point's payload.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

FRAMES_PATH = "/api/v1/frames"

# Logged once per process when FRAMES_PUBLIC_BASE_URL is empty.
_warned_relative = False


def frame_url(base_url: str | None, point_id: str) -> str:
    """``{base}/api/v1/frames/{point_id}``; relative when ``base_url`` is empty.

    The relative fallback still works for the gateway-hosted UI but cannot be
    opened from outside the platform, so it logs a warning once.
    """
    global _warned_relative
    base = (base_url or "").strip().rstrip("/")
    if not base and not _warned_relative:
        _warned_relative = True
        logger.warning(
            "FRAMES_PUBLIC_BASE_URL is empty: frame URLs are stored relative "
            "(%s/{point_id}). Set it to the public API host.",
            FRAMES_PATH,
        )
    return f"{base}{FRAMES_PATH}/{point_id}"


def _is_presigned(url: str) -> bool:
    query = urlsplit(url).query.lower()
    return "x-amz-signature=" in query or "x-amz-credential=" in query


def strip_cache_buster(url: str | None) -> str | None:
    """Drop a ``nc`` cache-buster query parameter; everything else is kept.

    A presigned URL is returned unchanged (removing a parameter would break its
    signature, and the frame reader never appends ``nc`` to one anyway).
    """
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.query or _is_presigned(url):
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k != "nc"]
    if len(kept) == len(pairs):
        return url
    return urlunsplit(parts._replace(query=urlencode(kept)))


def search_match_frame_fields(metadata: dict | None) -> dict:
    """Frame columns of a ``search_matches`` row from the Qdrant payload.

    ``qdrant_point_id`` is injected into ``SearchResult.metadata`` by
    ``QdrantVectorRepository.search_similar``. ``image_name`` / ``zip_entry``
    exist only on points ingested in zip_ref mode; older points give ``None``.
    """
    md = metadata or {}
    point_id = md.get("qdrant_point_id")
    return {
        "qdrant_point_id": str(point_id) if point_id else None,
        "image_name": md.get("image_name") or None,
        "zip_entry": md.get("zip_entry") or None,
    }
