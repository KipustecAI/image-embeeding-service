# Phase 03: Qdrant — Source-Type Discrimination + Strict Filter Helper

## Scope

Two touches in [src/infrastructure/vector_db/qdrant_repository.py](../../src/infrastructure/vector_db/qdrant_repository.py):

1. No new collection. Reuse `evidence_embeddings` with a new `source_type` value — `"blacklist"` — stored in the Qdrant point payload alongside today's `"evidence"` value.
2. Add a shared filter-construction helper (`src/application/helpers/source_type_filter.py`) that every caller uses so `source_type` isolation is enforced in one place, not duplicated across the codebase.

Plus one payload index addition: `blacklist_entry_id` as a `keyword` so we can retrieve a specific entry's Qdrant points for deletion / re-matching / version checks.

## Why reuse the collection instead of splitting

Locked in README §7 item #2. Recap: splitting guarantees isolation by architecture; reusing is cheaper but requires discipline. Mitigation: a single filter helper that every search path calls. If the helper is correct, isolation is guaranteed regardless of caller count.

## Files modified

| File | Change |
|---|---|
| [src/infrastructure/vector_db/qdrant_repository.py](../../src/infrastructure/vector_db/qdrant_repository.py) | Add `("blacklist_entry_id", "keyword")` to `_EVIDENCE_PAYLOAD_INDICES`; no other structural change |
| `src/application/helpers/source_type_filter.py` | **Create** — three builder functions used by all callers |
| [src/streams/search_results_consumer.py](../../src/streams/search_results_consumer.py) | Replace hand-built `source_type="evidence"` filter with call to `build_evidence_only_filter` |
| [src/main.py](../../src/main.py) `/api/v1/recalculate/searches` | Same helper call, same semantics |

## The new `blacklist_entry_id` payload field

Every Qdrant point written for a blacklist reference gets payload entries:

```python
payload = {
    "source_type": "blacklist",            # Already-indexed, discriminator
    "blacklist_entry_id": "<entry UUID>",  # NEW indexed field
    "blacklist_reference_id": "<ref UUID>",  # Indexed? See §"Do we need a reference_id index"
    "user_id": user_id,                    # Multi-tenant isolation
    "model_version": "clip-vit-b-32",
    "image_url": reference.image_url,
    # category inherited via Phase 01 — if the blacklist entry has a category,
    # propagate it to the reference's points. Makes category-filtered search
    # and blacklist-match queries symmetric.
    "category": entry.category,
}
```

Adding the index line to `_EVIDENCE_PAYLOAD_INDICES`:

```python
_EVIDENCE_PAYLOAD_INDICES: list[tuple[str, str]] = [
    # ... existing entries ...
    ("category", "keyword"),          # From Phase 01
    # From Phase 03 — image blacklist
    ("blacklist_entry_id", "keyword"),
]
```

Same idempotent-startup pattern as the weapon indices.

### Do we need a `blacklist_reference_id` index?

Probably not. We'd only query by `reference_id` for deletion-time cleanup ("delete all Qdrant points belonging to this reference"), and the DB already has the `qdrant_point_id` list for that reference. Deletion by point ID is a direct operation, no filter needed.

**v1 decision: store `blacklist_reference_id` in payload for traceability, do NOT index it.** If a query pattern emerges (e.g. ops wants to see all points from a specific reference without hitting the DB), add the index in a follow-up migration with zero code change needed — the `_ensure_payload_indices` helper picks up new entries on startup.

## The strict filter helper

New file `src/application/helpers/source_type_filter.py`:

```python
"""Source-type filter construction for Qdrant queries.

Every search path that queries the `evidence_embeddings` collection must
scope its filter by `source_type` explicitly. Forgetting would mix
evidence points with blacklist points in search results (and vice versa)
— a correctness bug that's easy to introduce and hard to detect.

Centralizing the filter construction here means callers can't forget:
they call the helper that matches their intent. Code review only has to
inspect this one file for correctness.

See docs/image-blacklist/03_QDRANT.md for the design rationale.
"""

from __future__ import annotations

from typing import Any

# Canonical source_type values. Keep this list in sync with what the
# producer-side writes (src/streams/embedding_results_consumer.py) and
# what the blacklist reference-embedding path writes.
SOURCE_TYPE_EVIDENCE = "evidence"
SOURCE_TYPE_BLACKLIST = "blacklist"


def build_evidence_only_filter(
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add `source_type="evidence"` to an existing filter dict.

    Use for:
    - User-facing similarity searches (must never return blacklist points)
    - Reverse search when a new blacklist entry is added (search existing
      evidence for matches against the new blacklist vector)
    - Recalculation of stored query vectors against new evidence
    """
    out = dict(base or {})
    out["source_type"] = SOURCE_TYPE_EVIDENCE
    return out


def build_blacklist_only_filter(
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add `source_type="blacklist"` to an existing filter dict.

    Use for:
    - Inline match when a new evidence is ingested (search the user's
      blacklist for matches against the new evidence vector)
    """
    out = dict(base or {})
    out["source_type"] = SOURCE_TYPE_BLACKLIST
    return out


def build_blacklist_entry_filter(
    entry_id: str,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restrict to a single blacklist entry's points.

    Used when re-matching after an entry is edited (blacklist_version
    bumped) — we only need to re-match this specific entry's vectors
    against historical evidence.
    """
    out = dict(base or {})
    out["source_type"] = SOURCE_TYPE_BLACKLIST
    out["blacklist_entry_id"] = entry_id
    return out
```

Three intents, three helpers. No magic strings at call sites. Any caller adding `source_type` by hand is a code-review red flag.

## Migrating existing call sites

The search consumer and the recalculation handler currently pass `filter_conditions` to `vector_repo.search_similar()` without any `source_type` clause. Today this is fine because all points have `source_type="evidence"`. After Phase 03, we write points with `"blacklist"` too — unfiltered queries will start leaking them into user results.

The migration is mechanical: every place that currently builds `filter_conditions` for a user-facing search wraps its dict with `build_evidence_only_filter()`. Two call sites:

### `src/streams/search_results_consumer.py`

Before:
```python
filter_conditions: dict = {}
if search_metadata:
    if "camera_id" in search_metadata:
        filter_conditions["camera_id"] = search_metadata["camera_id"]
    # ... other allow-listed keys ...
    filter_conditions.update(build_weapon_filter_conditions(search_metadata))
if not filter_conditions:
    filter_conditions = None
```

After:
```python
filter_conditions: dict = {}
if search_metadata:
    if "camera_id" in search_metadata:
        filter_conditions["camera_id"] = search_metadata["camera_id"]
    # ... other allow-listed keys ...
    filter_conditions.update(build_weapon_filter_conditions(search_metadata))

# User-facing search — never return blacklist points.
filter_conditions = build_evidence_only_filter(filter_conditions)
```

### `src/main.py` recalculation handler

Same pattern. The stored query vectors re-query `evidence_embeddings` — also must be scoped to `source_type="evidence"`.

**Do NOT add the filter in `qdrant_repository.search_similar()` itself** — keep the repository layer dumb. Filter intent belongs in the application layer where the helper functions live, not in the generic vector-search primitive. This is the same reason we don't do multi-tenant user_id filtering in the repo.

## What about weapons_filter / category / user_id?

Those already live in `search_metadata` and get injected via the filter allow-list or the weapons helper. They compose naturally with `source_type`:

```python
# A non-admin user searching weapons-only handguns in category=vehicle:
filter_conditions = {
    "user_id": "user-1",
    "category": "vehicle",
    "source_type": "evidence",
    "has_weapon": True,
    "weapon_classes": ["handgun"],
}
```

All keys become Qdrant `FieldCondition`s in the `must` list. Qdrant ANDs them. The existing `MatchAny` branch in `search_similar` handles list values for the class filter.

## Startup ordering

`QdrantVectorRepository.initialize()` is called during lifespan setup before the consumers start. `_ensure_payload_indices` runs unconditionally every time. The new `blacklist_entry_id` index is created on first startup after deploy — no migration script, no manual Qdrant operation.

In the unlikely edge case where a blacklist reference is embedded and stored *before* the backend has finished `initialize()`, Qdrant still accepts the write; the index just doesn't include those points until the index-build completes (which is fast for low-cardinality keyword fields). No correctness impact.

## Verification

After deploy:

```bash
# Index present
curl -s http://localhost:6333/collections/evidence_embeddings | python3 -m json.tool \
  | grep -A1 blacklist_entry_id
# Expected: "blacklist_entry_id" -> "keyword"

# Dummy filter parses correctly (no points exist yet, but 200 response = parser is happy)
curl -sX POST http://localhost:6333/collections/evidence_embeddings/points/search \
  -H "Content-Type: application/json" \
  -d '{
    "vector": [0.0, ...512 zeros...],
    "limit": 1,
    "filter": {
      "must": [
        {"key": "source_type", "match": {"value": "blacklist"}},
        {"key": "blacklist_entry_id", "match": {"value": "deadbeef-..."}}
      ]
    }
  }'
```

Unit test for the helpers in `tests/test_source_type_filter.py`:

```python
def test_evidence_only_filter_adds_source_type():
    assert build_evidence_only_filter() == {"source_type": "evidence"}

def test_evidence_only_filter_preserves_base():
    base = {"user_id": "u", "camera_id": "c"}
    result = build_evidence_only_filter(base)
    assert result == {"user_id": "u", "camera_id": "c", "source_type": "evidence"}

def test_evidence_only_filter_does_not_mutate_base():
    base = {"user_id": "u"}
    build_evidence_only_filter(base)
    assert base == {"user_id": "u"}  # unchanged

def test_blacklist_only_filter():
    assert build_blacklist_only_filter() == {"source_type": "blacklist"}

def test_blacklist_entry_filter():
    assert build_blacklist_entry_filter("abc-123") == {
        "source_type": "blacklist",
        "blacklist_entry_id": "abc-123",
    }
```

Six pure-unit tests, no infra. Runs in under 20ms.

## Security consideration — cross-tenant read through a missing user_id filter

The `source_type` helper does NOT enforce `user_id`. Multi-tenant isolation is a separate concern, enforced today by the search use case via `metadata["user_id"] = ctx.user_id` for non-admin roles.

The blacklist reverse-search and inline-match paths in Phase 04/05 must **also** apply the `user_id` scope — a user must only match against their own blacklist, and a new blacklist entry must only reverse-search its owner's evidence. This is code-review territory for Phase 04/05; Phase 03 just provides the source_type primitive.

If we wanted to be belt-and-braces, the helper could require a `user_id` kwarg. Decision: don't — some admin paths legitimately need to search across tenants, and forcing `user_id` on the helper would make admin paths awkward. The existing multi-tenant discipline is enough; the new source_type helper is one more layer of it.

## Rollout order reminder

1. Phase 02 (tables) must be deployed first — the model imports break otherwise.
2. Apply this phase. Existing behavior is unchanged until Phase 04/05 starts writing points with `source_type="blacklist"`.
3. The two call-site migrations (search consumer + recalculation) are **required** before Phase 04/05 — deploying Phase 04 without them would cause blacklist points to leak into user searches.
