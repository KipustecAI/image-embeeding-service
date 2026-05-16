"""PII-safe hashing for lookia-dw publishers.

The blacklist_image_entry stream is the only DW stream that touches
PII (`BlacklistImageEntry.name`, which often contains case numbers,
suspect identifiers, or personal details). We hash the name before
publishing so the DW never receives raw values — same pattern as the
face team's `blacklist_persons.name_hash`.

Recipe locked by the contract:
    name_hash = sha256(name.encode('utf-8')).hexdigest()[:16]

See ``docs/requirements/LOOKIA_DW_STREAMS.md`` §4.3 and the
``test_dw_hashing`` regression tests for the canonical behavior.
"""

from __future__ import annotations

import hashlib


def name_hash(name: str) -> str:
    """Return a stable, irreversible 16-char hex hash of a name.

    Deterministic across processes and language runtimes. Empty strings
    hash to a fixed sentinel (the sha256 of the empty byte sequence,
    truncated) — preserved on the wire as a hash rather than translated
    to ``None`` because the DW's UNIQUE keys on the dim table expect a
    non-null string. Callers wanting "no name" semantics should set the
    field to ``None`` before reaching this function.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
