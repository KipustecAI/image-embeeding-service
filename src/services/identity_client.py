"""Identity-service client — hub membership resolution for hub-scoped search.

Ported unchanged from lookia-plates-service ``src/services/identity_client.py``
(the platform's one seam for hub membership).

ONE seam for hub membership: ``GET /internal/hubs/{hub_id}/user-ids``. It returns
ids + roles only, so no user PII crosses the service boundary. Do NOT resolve
membership by pulling full user records — that drags PII across for no benefit.

Contract (pinned by identity-service, live across gateway/alerting/etl):

    GET http://identity-service:8001/internal/hubs/{hub_id}/user-ids

  * IN-CLUSTER ONLY — use the compose alias ``identity-service:8001``, not the
    ``ms-`` container name.
  * SEND NO AUTH HEADER AT ALL (no Authorization, no X-User-*, no API key).
  * 200 -> {"hub_id": "...", "app_type": 1, "is_active": true,
            "members": [{"user_id": "...", "hub_role": "owner|manager|member"}]}
    - ``members`` is NOT in identity's required set: ABSENT means EMPTY, not invalid.
    - Never paginates, no cap.
    - ``hub_role`` is NOT an authorization and enforces nothing — never branch on it.
  * 404 -> the hub does not exist.

Failure policy (deliberate, never hide errors with fallbacks):
identity down / timeout / 5xx / malformed body => raise IdentityUnavailable, which
the API layer maps to 503. We NEVER degrade to "unfiltered" (returns other tenants'
rows) and NEVER to "empty" (silently hides the caller's data).

Caching: per-process TTL, successful 200s ONLY. Caching a 404 or a 5xx is how a
transient outage becomes a persistent wrong answer. identity emits no membership-
change event today, so HUB_MEMBERS_CACHE_TTL is the staleness window — a membership
change takes up to that long to be visible here.

Testability: ``get_hub_user_ids`` is the DI seam. The unit suite has NO network — tests monkeypatch
``_get_client`` (client contract tests) or patch this module's ``get_hub_user_ids``
where a caller imports it (dependency tests). ``clear_hub_cache`` resets state.
"""

import logging
import time

import httpx

from ..infrastructure.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class HubNotFound(Exception):
    """identity-service returned 404 — the hub does not exist."""


class IdentityUnavailable(Exception):
    """identity-service was unreachable, timed out, 5xx'd, or returned a malformed body."""


# hub_id -> (expires_at_monotonic, user_ids). Successful lookups only.
_cache: dict[str, tuple[float, list[str]]] = {}

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy shared client. NOTE: no default headers — the contract forbids auth.

    Constructing the client opens no socket; the first ``get`` is where the
    connection happens — so importing the app stays network-free.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.identity_service_url,
            timeout=settings.identity_timeout_seconds,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _client


async def close_identity_client() -> None:
    """Close the shared client (called on app shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _cache_get(hub_id: str) -> list[str] | None:
    entry = _cache.get(hub_id)
    if entry is None:
        return None
    expires_at, user_ids = entry
    if time.monotonic() >= expires_at:
        _cache.pop(hub_id, None)
        return None
    return user_ids


def clear_hub_cache() -> None:
    """Drop every cached member list (tests + operational reset)."""
    _cache.clear()


async def get_hub_user_ids(hub_id: str) -> list[str]:
    """Resolve a hub to its member user_ids.

    Returns a (possibly EMPTY) list of user_ids. An empty list means "this hub
    exists but has no members" — it is NOT the same as "no hub filter", and the
    caller must translate it to a match-nothing condition, never to unfiltered.

    Raises:
        HubNotFound: identity returned 404.
        IdentityUnavailable: transport error, timeout, non-200/404, or bad body.
    """
    cached = _cache_get(hub_id)
    if cached is not None:
        return cached

    try:
        response = await _get_client().get(f"/internal/hubs/{hub_id}/user-ids")
    except httpx.HTTPError as e:
        # Fail loud — degrading here would either leak or silently hide rows.
        raise IdentityUnavailable(f"identity-service unreachable for hub {hub_id}: {e}") from e

    if response.status_code == 404:
        raise HubNotFound(f"Hub {hub_id} not found")
    if response.status_code != 200:
        raise IdentityUnavailable(
            f"identity-service returned {response.status_code} for hub {hub_id}"
        )

    try:
        body = response.json()
    except ValueError as e:
        raise IdentityUnavailable(
            f"identity-service returned a non-JSON body for hub {hub_id}"
        ) from e
    if not isinstance(body, dict):
        raise IdentityUnavailable(f"identity-service returned a non-object body for hub {hub_id}")

    # `members` is optional in identity's schema: ABSENT means EMPTY, not invalid.
    members = body.get("members") or []
    if not isinstance(members, list):
        raise IdentityUnavailable(
            f"identity-service returned a non-list `members` for hub {hub_id}"
        )

    user_ids: list[str] = []
    for member in members:
        if not isinstance(member, dict):
            raise IdentityUnavailable(
                f"identity-service returned a malformed member entry for hub {hub_id}"
            )
        user_id = member.get("user_id")
        if user_id:
            user_ids.append(str(user_id))

    # Cache successes only — never a 404, never a 5xx.
    _cache[hub_id] = (time.monotonic() + settings.hub_members_cache_ttl, user_ids)
    logger.debug("Hub %s resolved to %d member(s)", hub_id, len(user_ids))
    return user_ids
