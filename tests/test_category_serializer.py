"""Pure unit tests for src.application.helpers.category_serializer.

This helper is the single point of translation from the upstream
``entities: list[int]`` field into the two on-disk shapes (DB compact
JSON + Qdrant keyword[] list). A regression here would leak garbage
into both stores or — worse — silently drop category data.

The translation is the load-bearing piece of the categories feature
on the consumer side. Errors here surface as "dropdown shows wrong
or missing buckets". Test exhaustively.
"""

import pytest

from src.application.helpers.category_serializer import entities_to_category

# ── Empty / missing inputs ─────────────────────────────────────────────────


def test_none_input_returns_none_pair():
    """Missing field on the payload — must yield NULL on both stores
    so the row is treated as "uncategorized" rather than "empty list"."""
    assert entities_to_category(None) == (None, None)


def test_empty_list_returns_none_pair():
    """Explicit empty list is semantically equivalent to None — neither
    represents a real category bucket. The categories endpoint's
    SELECT DISTINCT must not surface an empty-list row."""
    assert entities_to_category([]) == (None, None)


def test_empty_tuple_returns_none_pair():
    """Same contract for any falsy iterable."""
    assert entities_to_category(()) == (None, None)


# ── Single-id inputs ───────────────────────────────────────────────────────


def test_single_id_serializes_to_both_shapes():
    db, qdrant = entities_to_category([2])
    assert db == "[2]"
    assert qdrant == ["2"]


def test_single_id_int_zero_is_preserved():
    """Zero is a valid id (COCO 0 = person). Don't drop it as falsy."""
    db, qdrant = entities_to_category([0])
    assert db == "[0]"
    assert qdrant == ["0"]


# ── Multi-id, sorting + dedup ──────────────────────────────────────────────


def test_multiple_ids_are_sorted():
    db, qdrant = entities_to_category([5, 2])
    assert db == "[2,5]"
    assert qdrant == ["2", "5"]


def test_duplicates_are_collapsed():
    """Duplicates in the payload must collapse to one bucket so
    [2, 2, 5] and [2, 5] hit the same category row."""
    db, qdrant = entities_to_category([2, 2, 5])
    assert db == "[2,5]"
    assert qdrant == ["2", "5"]


def test_unsorted_with_duplicates():
    db, qdrant = entities_to_category([5, 2, 5, 2])
    assert db == "[2,5]"
    assert qdrant == ["2", "5"]


def test_three_distinct_ids():
    db, qdrant = entities_to_category([7, 1, 3])
    assert db == "[1,3,7]"
    assert qdrant == ["1", "3", "7"]


# ── Compact JSON format ────────────────────────────────────────────────────


def test_db_format_has_no_whitespace():
    """The DB column is queried with SELECT DISTINCT — value-equality.
    Any whitespace drift between writer and reader splits the bucket
    silently. Lock the compact format."""
    db, _ = entities_to_category([2, 5])
    assert " " not in db
    assert db == "[2,5]"


def test_db_format_round_trips_to_python_list():
    """The categories endpoint json.loads each row. Must round-trip
    cleanly — otherwise the endpoint silently drops rows."""
    import json as _json

    db, _ = entities_to_category([5, 2, 7])
    assert _json.loads(db) == [2, 5, 7]


# ── Tolerance for upstream weirdness ──────────────────────────────────────


def test_string_ids_are_coerced_to_int():
    """Defensive: if upstream ever sends ``["2", "5"]`` (JSON-string ids)
    instead of ``[2, 5]``, we coerce rather than silently corrupt."""
    db, qdrant = entities_to_category(["2", "5"])
    assert db == "[2,5]"
    assert qdrant == ["2", "5"]


def test_non_coercible_input_raises():
    """Garbage payloads (non-numeric strings) must surface as ValueError
    so we notice the shape change, not silently drop rows."""
    with pytest.raises(ValueError):
        entities_to_category(["car"])  # human-readable label, not an id


# ── Symmetry between the two outputs ───────────────────────────────────────


def test_qdrant_list_length_matches_unique_ids():
    """Smoke: the Qdrant list and the DB JSON describe the same set."""
    import json as _json

    inputs = [[2], [2, 5], [3, 1, 7], [5, 5, 5, 2], []]
    for entities in inputs:
        db, qdrant = entities_to_category(entities)
        if db is None:
            assert qdrant is None
            continue
        assert qdrant == [str(x) for x in _json.loads(db)]
