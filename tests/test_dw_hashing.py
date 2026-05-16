"""Tests for src.application.helpers.dw_hashing.

The PII hash is the single load-bearing primitive that protects
blacklist names from reaching the DW. A regression here is a PII
incident — test thoroughly.
"""

from __future__ import annotations

import hashlib

from src.application.helpers.dw_hashing import name_hash


def test_hash_is_16_hex_chars():
    """Locked length per contract §4.3."""
    h = name_hash("any string")
    assert len(h) == 16
    int(h, 16)  # must parse as hex


def test_hash_is_deterministic_across_calls():
    """Same input → same output; load-bearing for DW dedup keys."""
    assert name_hash("Suspect plate") == name_hash("Suspect plate")


def test_hash_recipe_matches_contract_exactly():
    """The contract pins the exact recipe:
        sha256(name.encode('utf-8')).hexdigest()[:16]
    Any drift from this and our hash won't match what the face team's
    publishers produce for the same name (cross-service join broken).
    """
    name = "Placa del sospechoso — caso 2025-042"
    expected = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    assert name_hash(name) == expected


def test_hash_is_irreversible():
    """Smoke check: not the original string, not even a substring."""
    name = "Juan Pérez"
    h = name_hash(name)
    assert name not in h
    assert "Juan" not in h
    assert "Pérez" not in h


def test_distinct_names_yield_distinct_hashes():
    """sha256 collision space at 16 hex chars is ~2^64; vanishingly
    rare collisions for any realistic name set, but lock the smoke
    test so a future "optimization" of the recipe surfaces."""
    a = name_hash("Suspect A")
    b = name_hash("Suspect B")
    c = name_hash("suspect a")  # case differs
    d = name_hash("Suspect A ")  # trailing space
    assert len({a, b, c, d}) == 4


def test_handles_unicode_normalization_consistently():
    """UTF-8 encoding is part of the recipe. Test a string with
    non-ASCII to confirm we're not silently double-encoding."""
    h1 = name_hash("Müller")
    h2 = name_hash("Müller")
    assert h1 == h2
    # And matches the canonical recipe directly
    assert h1 == hashlib.sha256("Müller".encode()).hexdigest()[:16]


def test_empty_string_produces_known_sentinel():
    """The empty string is the sha256 of nothing — fixed value. The
    publisher uses this when a caller passes name="" rather than
    skipping the field, so it's important the value is stable."""
    expected = hashlib.sha256(b"").hexdigest()[:16]
    assert name_hash("") == expected
    assert name_hash("") == "e3b0c44298fc1c14"
