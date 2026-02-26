from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException
from tce_shared.events import AgentRole

from .config import get_settings


@dataclass(slots=True)
class AuthContext:
    consumer: str
    role: AgentRole
    workspace_id: str
    user_id: str


def _parse_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def get_auth_context(
    authorization: str | None = Header(default=None),
    consumer: str = Header(default="local-user", alias="X-TCE-Consumer"),
    role: str = Header(default="user", alias="X-TCE-Role"),
    workspace: str = Header(default="personal", alias="X-TCE-Workspace"),
    user: str = Header(default="", alias="X-TCE-User"),
) -> AuthContext:
    settings = get_settings()
    token = _parse_bearer_token(authorization)
    if not token or token not in settings.token_set:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials. Provide Authorization: Bearer <token> and X-TCE-Workspace/X-TCE-User headers.",
        )
    try:
        parsed_role = AgentRole(role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid X-TCE-Role") from exc
    normalized_workspace = workspace.strip() or "personal"
    normalized_user = user.strip() or consumer.strip() or "local-user"
    return AuthContext(
        consumer=consumer,
        role=parsed_role,
        workspace_id=normalized_workspace,
        user_id=normalized_user,
    )
