"""Pydantic-level validation tests for the blacklist API schemas.

Catches regressions in the request validators before they reach the
router — bad inputs surface as 422s, but the schemas are also imported
by the use-case tests and any drift here would silently let through
malformed data.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.v1.schemas.blacklist_image import (
    AddReferenceRequest,
    CreateEntryRequest,
    PatchEntryRequest,
)

# ── CreateEntryRequest ─────────────────────────────────────────────────────


def test_create_entry_minimal_valid():
    body = CreateEntryRequest(name="Suspect plate")
    assert body.name == "Suspect plate"
    assert body.category is None
    assert body.match_threshold is None


def test_create_entry_full_valid():
    body = CreateEntryRequest(
        name="x",
        category="vehicle",
        description="case 042",
        match_threshold=0.88,
        json_data={"case_id": "C-042"},
    )
    assert body.match_threshold == 0.88
    assert body.json_data == {"case_id": "C-042"}


def test_create_entry_empty_name_rejected():
    """min_length=1 catches '' before it reaches the DB."""
    with pytest.raises(ValidationError):
        CreateEntryRequest(name="")


def test_create_entry_name_too_long_rejected():
    """max_length=255 — DB column is TEXT but we cap at the API boundary."""
    with pytest.raises(ValidationError):
        CreateEntryRequest(name="x" * 256)


def test_create_entry_threshold_above_one_rejected():
    """Cosine similarity is in [0, 1] — anything else is a typo."""
    with pytest.raises(ValidationError):
        CreateEntryRequest(name="x", match_threshold=1.5)


def test_create_entry_threshold_below_zero_rejected():
    with pytest.raises(ValidationError):
        CreateEntryRequest(name="x", match_threshold=-0.01)


def test_create_entry_threshold_at_bounds_accepted():
    """Inclusive bounds — 0.0 and 1.0 both legal (degenerate but allowed)."""
    CreateEntryRequest(name="x", match_threshold=0.0)
    CreateEntryRequest(name="x", match_threshold=1.0)


# ── PatchEntryRequest ──────────────────────────────────────────────────────


def test_patch_entry_all_fields_optional():
    """Partial-update semantics — empty body is a no-op, must validate."""
    body = PatchEntryRequest()
    assert body.model_dump(exclude_unset=True) == {}


def test_patch_entry_partial_update():
    """Pydantic distinguishes "not sent" from "sent as null" via
    exclude_unset — load-bearing for the use case's patch-merge logic."""
    body = PatchEntryRequest(category=None)
    sent = body.model_dump(exclude_unset=True)
    assert "category" in sent
    assert sent["category"] is None


def test_patch_entry_threshold_validated():
    with pytest.raises(ValidationError):
        PatchEntryRequest(match_threshold=2.0)


def test_patch_entry_empty_name_rejected():
    """Same min_length=1 rule as create — prevents PATCH from clearing
    the name to empty string by mistake."""
    with pytest.raises(ValidationError):
        PatchEntryRequest(name="")


def test_patch_entry_active_toggle():
    body = PatchEntryRequest(active=False)
    assert body.active is False


# ── AddReferenceRequest ────────────────────────────────────────────────────


def test_add_reference_https_url_accepted():
    body = AddReferenceRequest(image_url="https://minio.lookia.mx/blacklist/x.jpg")
    # str() unwraps the validated HttpUrl
    assert "minio.lookia.mx" in str(body.image_url)


def test_add_reference_http_url_accepted():
    """Some internal storage URLs are http (intra-cluster). Allow both."""
    body = AddReferenceRequest(image_url="http://internal-storage:9000/x.jpg")
    assert str(body.image_url).startswith("http://")


def test_add_reference_file_url_rejected():
    """Local file:// URLs would silently fail downstream — the GPU
    can't fetch them from a different host. Reject at the boundary."""
    with pytest.raises(ValidationError):
        AddReferenceRequest(image_url="file:///tmp/local.jpg")


def test_add_reference_default_image_type():
    body = AddReferenceRequest(image_url="https://x/y.jpg")
    assert body.image_type == "reference"


def test_add_reference_explicit_image_type_preserved():
    body = AddReferenceRequest(image_url="https://x/y.jpg", image_type="enhanced")
    assert body.image_type == "enhanced"


def test_add_reference_invalid_url_rejected():
    """Garbage strings caught by HttpUrl validator."""
    with pytest.raises(ValidationError):
        AddReferenceRequest(image_url="not-a-url")
