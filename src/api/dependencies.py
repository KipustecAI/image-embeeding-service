"""API dependencies — user context from API Gateway headers."""

from dataclasses import dataclass
from typing import Optional

from fastapi import Request


@dataclass
class UserContext:
    user_id: str
    role: str
    scopes: list[str]
    created_by: Optional[str]
    app_type: Optional[int]
    request_id: str

    @property
    def is_guest(self) -> bool:
        return self.role == "guest"

    @property
    def owner_id(self) -> str:
        """The effective owner: parent user for guests, self otherwise."""
        return self.created_by if self.is_guest else self.user_id


def get_user_context(request: Request) -> UserContext:
    """Extract user context from gateway-injected headers."""
    raw_scopes = request.headers.get("X-User-Scopes", "")
    raw_app_type = request.headers.get("X-App-Type")

    return UserContext(
        user_id=request.headers.get("X-User-Id", ""),
        role=request.headers.get("X-User-Role", ""),
        scopes=[s for s in raw_scopes.split(",") if s],
        created_by=request.headers.get("X-User-Created-By"),
        app_type=int(raw_app_type) if raw_app_type else None,
        request_id=request.headers.get("X-Request-Id", ""),
    )
