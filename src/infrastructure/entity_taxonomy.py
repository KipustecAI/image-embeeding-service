"""STOPGAP entity-id → label resolver.

The upstream `image-embedding-compute` service forwards `entities: list[int]`
on every `embeddings:results` envelope. Those integers come from the
`t_configurations.entities` table on the platform side (forwarded by ETL).

The authoritative `id → label` map lives in that table — not here. Until the
platform team exposes it as a readable endpoint or shared connection (the
"Option A" path tracked in docs/requirements/IMAGE_COMPUTE_STREAMS.md §2),
we hold a small hardcoded map so the categories endpoint can return human-
readable labels for the dropdown UX.

The hardcoded values follow the standard YOLO/COCO-80 indexing. **They may
not match the actual platform taxonomy** — this is a deliberate, time-bounded
stop-gap. When platform exposes the real map:

  1. Replace ENTITY_LABELS with a runtime-cached lookup
  2. Drop "unk" fallback in resolve_entity_label (or keep it as defense)
  3. Delete this module-level docstring's "STOPGAP" preamble

Unknown ids (not in the dict) resolve to "unk" so the endpoint always returns
a non-null label and the frontend never has to render a literal None.
"""

from __future__ import annotations

UNKNOWN_LABEL = "unk"

ENTITY_LABELS: dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}


def resolve_entity_label(entity_id: int) -> str:
    """Return the human-readable label for an entity id, or `"unk"` if unknown.

    Always returns a string — callers can safely render the result without
    null-checks. The "unk" fallback signals to ops that the platform map
    needs more entries (or that the upstream sent an id we don't recognize).
    """
    return ENTITY_LABELS.get(entity_id, UNKNOWN_LABEL)
