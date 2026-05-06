"""Pure unit tests for src.application.helpers.source_type_filter.

No infrastructure needed. Validates that the three filter builders emit
the right source_type scope and preserve caller-provided base filters.
Correctness of these functions is load-bearing — a bug here could leak
blacklist points into user searches. Tests here are the first line of
defense against that regression.
"""

import pytest

from src.application.helpers.source_type_filter import (
    SOURCE_TYPE_BLACKLIST,
    SOURCE_TYPE_EVIDENCE,
    build_blacklist_entry_filter,
    build_blacklist_only_filter,
    build_evidence_only_filter,
)

# ── Constants ───────────────────────────────────────────────────────────────


def test_source_type_values():
    """The canonical string values must match what the consumer writes.
    If these ever drift, filters stop matching anything — silent dataloss."""
    assert SOURCE_TYPE_EVIDENCE == "evidence"
    assert SOURCE_TYPE_BLACKLIST == "blacklist"


# ── build_evidence_only_filter ─────────────────────────────────────────────


def test_evidence_only_from_empty():
    assert build_evidence_only_filter() == {"source_type": "evidence"}


def test_evidence_only_from_none():
    assert build_evidence_only_filter(None) == {"source_type": "evidence"}


def test_evidence_only_preserves_base():
    base = {"user_id": "u", "camera_id": "c"}
    assert build_evidence_only_filter(base) == {
        "user_id": "u",
        "camera_id": "c",
        "source_type": "evidence",
    }


def test_evidence_only_does_not_mutate_base():
    base = {"user_id": "u"}
    build_evidence_only_filter(base)
    assert base == {"user_id": "u"}  # unchanged


def test_evidence_only_overrides_any_existing_source_type():
    """If a caller accidentally sets source_type in the base dict, we
    still force 'evidence' — the helper's intent is non-negotiable."""
    base = {"source_type": "blacklist"}
    assert build_evidence_only_filter(base) == {"source_type": "evidence"}


# ── build_blacklist_only_filter ────────────────────────────────────────────


def test_blacklist_only_from_empty():
    assert build_blacklist_only_filter() == {"source_type": "blacklist"}


def test_blacklist_only_preserves_base():
    base = {"user_id": "u"}
    assert build_blacklist_only_filter(base) == {
        "user_id": "u",
        "source_type": "blacklist",
    }


def test_blacklist_only_overrides_any_existing_source_type():
    base = {"source_type": "evidence"}
    assert build_blacklist_only_filter(base) == {"source_type": "blacklist"}


# ── build_blacklist_entry_filter ───────────────────────────────────────────


def test_blacklist_entry_filter_sets_both_keys():
    """Must pin source_type AND blacklist_entry_id together — filtering on
    entry_id alone without source_type would scan the whole collection."""
    result = build_blacklist_entry_filter("entry-abc")
    assert result == {
        "source_type": "blacklist",
        "blacklist_entry_id": "entry-abc",
    }


def test_blacklist_entry_filter_preserves_base():
    base = {"user_id": "u"}
    result = build_blacklist_entry_filter("entry-abc", base)
    assert result == {
        "user_id": "u",
        "source_type": "blacklist",
        "blacklist_entry_id": "entry-abc",
    }


def test_blacklist_entry_filter_does_not_mutate_base():
    base = {"user_id": "u"}
    build_blacklist_entry_filter("entry-abc", base)
    assert base == {"user_id": "u"}


# ── Parametric: every helper is idempotent and base-agnostic ───────────────


@pytest.mark.parametrize(
    "helper,expected_source_type",
    [
        (build_evidence_only_filter, "evidence"),
        (build_blacklist_only_filter, "blacklist"),
    ],
)
def test_helpers_always_produce_matching_source_type(helper, expected_source_type):
    """Smoke: no matter the base dict, source_type comes out correct."""
    for base in (None, {}, {"x": 1}, {"source_type": "wrong"}):
        result = helper(base)
        assert result["source_type"] == expected_source_type
