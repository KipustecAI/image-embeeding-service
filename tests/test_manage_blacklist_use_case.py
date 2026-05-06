"""Tests for ManageBlacklistImageUseCase — the bits that don't need a DB.

DB-coupled methods (create/list/update/delete) are exercised in
integration coverage. Here we test the multi-tenant scoping helpers
and the error-translation contract, which are the load-bearing parts
that have no equivalent in the repository tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.application.use_cases.manage_blacklist_image import (
    BlacklistEntryNotFound,
    ManageBlacklistImageUseCase,
)

# ── _scope_user_id ─────────────────────────────────────────────────────────


def test_scope_user_id_admin_returns_none():
    """Admins see everything by default — caller may override with a
    target_user_id at the router boundary."""
    assert ManageBlacklistImageUseCase._scope_user_id("u-1", is_admin=True) is None


def test_scope_user_id_non_admin_pinned_to_self():
    """Non-admins are scoped to their own user_id regardless of any
    request param — multi-tenant isolation enforced here, not in the
    repository."""
    assert ManageBlacklistImageUseCase._scope_user_id("u-42", is_admin=False) == "u-42"


def test_scope_user_id_non_admin_with_none_returns_none():
    """Edge: gateway didn't set X-User-Id and caller is non-admin —
    scope is None, which the repository turns into an unscoped query.
    The router rejects this case (401) before we get here, but the
    helper itself stays permissive."""
    assert ManageBlacklistImageUseCase._scope_user_id(None, is_admin=False) is None


# ── _enforce_owner ─────────────────────────────────────────────────────────


def _fake_entry(user_id: str):
    """Lightweight stand-in — the helper only inspects user_id and id."""
    return SimpleNamespace(id="entry-uuid", user_id=user_id)


def test_enforce_owner_admin_passes_for_any_tenant():
    """Admins can act on any tenant's data."""
    entry = _fake_entry("other-tenant")
    # Should not raise
    ManageBlacklistImageUseCase._enforce_owner(entry, "admin-id", is_admin=True)


def test_enforce_owner_owner_passes():
    entry = _fake_entry("u-1")
    ManageBlacklistImageUseCase._enforce_owner(entry, "u-1", is_admin=False)


def test_enforce_owner_foreign_tenant_raises_not_found():
    """Critical: foreign tenants get 404, NOT 403. A 403 would leak the
    existence of entries the caller shouldn't see. The use case raises
    NotFound; the router translates to 404."""
    entry = _fake_entry("u-1")
    with pytest.raises(BlacklistEntryNotFound):
        ManageBlacklistImageUseCase._enforce_owner(entry, "u-2", is_admin=False)


# ── Construction ───────────────────────────────────────────────────────────


def test_use_case_holds_passed_dependencies():
    """Smoke: the use case stores the singletons it gets handed; per-
    request construction in the router relies on this."""
    fake_qdrant = object()
    fake_producer = object()
    uc = ManageBlacklistImageUseCase(vector_repo=fake_qdrant, stream_producer=fake_producer)
    assert uc._vector_repo is fake_qdrant
    assert uc._stream_producer is fake_producer
