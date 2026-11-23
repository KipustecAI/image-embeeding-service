"""Parse the optional helmet enrichment of an ``embeddings.computed`` payload.

Producer: compute-image-helmets. Contract: docs/helmets/CONTRACT.md.

Pure and tolerant: a malformed block never raises, because helmet data must not
break evidence ingest. A value of the wrong type becomes ``None``.
"""

import math
from typing import Any


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _error_message(raw: Any) -> str | None:
    """``{"message": "..."}`` (contract §4.2) or a bare string (contract §6.3)."""
    if isinstance(raw, dict):
        # An error block without a usable message is still a failed analysis.
        return str(raw.get("message") or "unknown") if raw else None
    if isinstance(raw, str):
        return raw or None
    return None


def parse_helmet_fields(payload: dict) -> dict[str, Any]:
    """Return the ``embedding_requests.helmet_*`` column values for a payload.

    States (contract §4.3):

    * no block, no error → ``helmet_analyzed=False``, everything else ``None``;
    * block              → ``helmet_analyzed=True`` + summary columns + full block;
    * error              → ``helmet_analyzed=False`` + ``helmet_analysis_error``.
    """
    block = payload.get("helmet_analysis")
    analyzed = isinstance(block, dict) and bool(block)
    summary = block.get("summary") if analyzed else None
    if not isinstance(summary, dict):
        summary = {}

    verdict = summary.get("verdict")
    alarm = summary.get("alarm")
    frames = _finite(summary.get("frames_sin_casco"))
    return {
        "helmet_analyzed": analyzed,
        "helmet_verdict": verdict if isinstance(verdict, str) else None,
        "helmet_alarm": alarm if isinstance(alarm, bool) else None,
        "helmet_frames_sin_casco": int(frames) if frames is not None else None,
        "helmet_max_sin_casco": _finite(summary.get("max_sin_casco")),
        "helmet_analysis": block if analyzed else None,
        "helmet_analysis_error": _error_message(payload.get("helmet_analysis_error")),
    }
