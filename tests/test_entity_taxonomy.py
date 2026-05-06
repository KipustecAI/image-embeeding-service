"""Pure unit tests for src.infrastructure.entity_taxonomy.

Resolver is a thin lookup with a fallback. Tests guard the contract
that the categories endpoint depends on: every id resolves to a
non-null string, known ids return their canonical label, unknown
ids return ``"unk"`` (never None, never a number).

The hardcoded map is a stop-gap — when platform exposes the real
taxonomy these tests will need their fixture values updated, but
the contract (always a string, fallback for unknown) stays.
"""

from src.infrastructure.entity_taxonomy import (
    ENTITY_LABELS,
    UNKNOWN_LABEL,
    resolve_entity_label,
)


def test_known_ids_resolve_to_their_canonical_label():
    """Spot-check the entries we hardcoded — bug here means the dropdown
    silently shows the wrong label."""
    assert resolve_entity_label(0) == "person"
    assert resolve_entity_label(1) == "bicycle"
    assert resolve_entity_label(2) == "car"


def test_unknown_id_returns_unk_sentinel():
    """The endpoint's contract is "always a string". Any id outside
    the hardcoded set must fall through to the sentinel rather than
    None or the raw int."""
    assert resolve_entity_label(99999) == UNKNOWN_LABEL
    assert resolve_entity_label(-1) == UNKNOWN_LABEL


def test_unknown_label_constant_is_a_short_string():
    """Frontend renders the result verbatim. If the sentinel ever
    becomes None or empty string, the dropdown breaks silently."""
    assert isinstance(UNKNOWN_LABEL, str)
    assert UNKNOWN_LABEL  # not empty


def test_resolver_always_returns_a_string():
    """No id should ever resolve to None — defensive contract test."""
    for entity_id in (0, 1, 2, 3, 5, 7, 99, -100, 0):
        result = resolve_entity_label(entity_id)
        assert isinstance(result, str)
        assert result  # not empty


def test_entity_labels_dict_is_int_to_string():
    """Catch a regression where someone adds a non-int key or
    non-string value during the eventual platform-fed migration."""
    for key, value in ENTITY_LABELS.items():
        assert isinstance(key, int), f"Non-int key in ENTITY_LABELS: {key!r}"
        assert isinstance(value, str), f"Non-str value in ENTITY_LABELS for {key}: {value!r}"
        assert value, f"Empty label for id {key}"
