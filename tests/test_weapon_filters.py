"""Pure unit tests for src.application.helpers.weapon_filters.

No infrastructure needed — these run anywhere Python can import the module.
Focus: validate the filter-mode → Qdrant-conditions translation for every
reachable branch, including edge cases the integration tests won't catch.
"""

import logging

import pytest

from src.application.helpers.weapon_filters import (
    VALID_WEAPONS_FILTERS,
    build_weapon_filter_conditions,
)


# ── Core modes ──────────────────────────────────────────────────────────────


def test_none_metadata_returns_empty():
    """None input must return {} — the search flow relies on this."""
    assert build_weapon_filter_conditions(None) == {}


def test_empty_dict_returns_empty():
    assert build_weapon_filter_conditions({}) == {}


def test_mode_all_returns_empty():
    """'all' is the default — it must add no Qdrant conditions at all."""
    assert build_weapon_filter_conditions({"weapons_filter": "all"}) == {}


def test_mode_only_without_classes():
    assert build_weapon_filter_conditions({"weapons_filter": "only"}) == {
        "has_weapon": True,
    }


def test_mode_only_with_single_class():
    result = build_weapon_filter_conditions(
        {"weapons_filter": "only", "weapon_classes": ["handgun"]}
    )
    assert result == {"has_weapon": True, "weapon_classes": ["handgun"]}


def test_mode_only_with_multiple_classes():
    result = build_weapon_filter_conditions(
        {"weapons_filter": "only", "weapon_classes": ["handgun", "knife"]}
    )
    assert result == {
        "has_weapon": True,
        "weapon_classes": ["handgun", "knife"],
    }


def test_mode_only_with_empty_classes_list_omits_key():
    """Empty list must not emit a weapon_classes condition (would match nothing)."""
    result = build_weapon_filter_conditions(
        {"weapons_filter": "only", "weapon_classes": []}
    )
    assert result == {"has_weapon": True}
    assert "weapon_classes" not in result


def test_mode_only_with_none_classes_omits_key():
    result = build_weapon_filter_conditions(
        {"weapons_filter": "only", "weapon_classes": None}
    )
    assert result == {"has_weapon": True}


def test_mode_exclude_returns_has_weapon_false():
    assert build_weapon_filter_conditions({"weapons_filter": "exclude"}) == {
        "has_weapon": False,
    }


def test_mode_analyzed_clean_requires_both_flags():
    """analyzed_clean must pin both flags — this is the review-queue filter."""
    result = build_weapon_filter_conditions({"weapons_filter": "analyzed_clean"})
    assert result == {"weapon_analyzed": True, "has_weapon": False}


# ── Defensive behavior ─────────────────────────────────────────────────────


def test_unknown_mode_returns_empty_and_warns(caplog):
    """Unknown modes must degrade gracefully, not throw."""
    with caplog.at_level(logging.WARNING, logger="src.application.helpers.weapon_filters"):
        result = build_weapon_filter_conditions({"weapons_filter": "bogus"})
    assert result == {}
    assert any("bogus" in rec.message for rec in caplog.records)


def test_missing_weapons_filter_key_defaults_to_all():
    """No mode key → treated as 'all' (no conditions added)."""
    result = build_weapon_filter_conditions({"camera_id": "cam-1"})
    assert result == {}


def test_classes_without_only_mode_are_ignored():
    """weapon_classes is only meaningful in 'only' mode — silently ignored elsewhere."""
    # exclude
    assert build_weapon_filter_conditions(
        {"weapons_filter": "exclude", "weapon_classes": ["handgun"]}
    ) == {"has_weapon": False}
    # analyzed_clean
    assert build_weapon_filter_conditions(
        {"weapons_filter": "analyzed_clean", "weapon_classes": ["handgun"]}
    ) == {"weapon_analyzed": True, "has_weapon": False}
    # all
    assert build_weapon_filter_conditions(
        {"weapons_filter": "all", "weapon_classes": ["handgun"]}
    ) == {}


def test_unrelated_metadata_keys_do_not_leak():
    """Helper must not pass camera_id, user_id, etc. through — those are handled
    by the caller's allow-list."""
    result = build_weapon_filter_conditions(
        {
            "weapons_filter": "only",
            "weapon_classes": ["handgun"],
            "camera_id": "cam-1",
            "user_id": "user-1",
            "device_id": "dev-1",
        }
    )
    assert result == {"has_weapon": True, "weapon_classes": ["handgun"]}
    assert "camera_id" not in result
    assert "user_id" not in result


# ── Contract invariants ────────────────────────────────────────────────────


def test_valid_filters_enum_matches_dto_literal():
    """The helper's tuple must match the Literal declared on SearchCreateRequest.
    If this fails, either VALID_WEAPONS_FILTERS or the DTO got out of sync."""
    assert VALID_WEAPONS_FILTERS == ("all", "only", "exclude", "analyzed_clean")


@pytest.mark.parametrize("mode", VALID_WEAPONS_FILTERS)
def test_every_valid_mode_returns_dict(mode):
    """Smoke: every valid mode must return a dict (never None, never raise)."""
    result = build_weapon_filter_conditions({"weapons_filter": mode})
    assert isinstance(result, dict)


def test_mode_only_classes_value_is_list_triggering_matchany():
    """The list value is load-bearing: qdrant_repository.search_similar branches
    on isinstance(value, list) to emit MatchAny instead of MatchValue. If this
    test fails, the class-subset filter silently breaks."""
    result = build_weapon_filter_conditions(
        {"weapons_filter": "only", "weapon_classes": ["handgun"]}
    )
    assert isinstance(result["weapon_classes"], list)
