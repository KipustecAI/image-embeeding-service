"""Crop labels from the CLIP worker's classifier (CLS-06, clasificacion-de-recortes).

image-embedding-compute may attach a vehicle-type label to each kept crop:

* each item of ``embeddings[]`` gets ``labels: [{"label": str, "score": float}]``
* the message gets ``classifier_version`` (e.g. ``vehicle_types/v20261027-202226``)

Semantics agreed in export-compute#model-optimization #3185:

* ``labels`` key absent → the classifier did not run for this crop → NULL, and
  nothing is added to the Qdrant payload (byte-identical to before).
* ``labels: []`` → it ran and no class passed its threshold → stored as ``[]``.

Pure functions, no I/O. Malformed entries are dropped, never raised: a bad label
must not cost the crop its CLIP vector.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def normalize_labels(raw: Any) -> list[dict] | None:
    """Validated ``labels`` sorted by score (highest first); None when absent.

    Keeps entries with a non-empty string ``label`` and a finite numeric
    ``score`` in [0, 1]. A non-list value is treated as absent.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning("Ignoring crop labels that are not a list: %r", type(raw).__name__)
        return None
    labels: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        score = item.get("score")
        if not isinstance(label, str) or not label.strip():
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        score = float(score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            continue
        labels.append({"label": label.strip(), "score": score})
    if len(labels) != len(raw):
        logger.warning("Dropped %d malformed crop label(s)", len(raw) - len(labels))
    labels.sort(key=lambda entry: entry["score"], reverse=True)
    return labels


def top_label(labels: list[dict] | None) -> tuple[str | None, float | None]:
    """``(label, score)`` of the highest-scored entry; ``(None, None)`` if none."""
    if not labels:
        return None, None
    best = max(labels, key=lambda entry: entry["score"])
    return best["label"], best["score"]


def label_payload_fields(labels: list[dict] | None, classifier_version: str | None) -> dict:
    """Qdrant payload keys for one crop; ``{}`` when the classifier did not run.

    ``vehicle_type`` is keyword-indexed so search can filter on it.
    """
    if labels is None:
        return {}
    vehicle_type, vehicle_type_score = top_label(labels)
    return {
        "labels": labels,
        "vehicle_type": vehicle_type,
        "vehicle_type_score": vehicle_type_score,
        "classifier_version": classifier_version,
    }
