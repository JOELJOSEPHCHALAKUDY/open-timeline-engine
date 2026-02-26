from __future__ import annotations

from fastapi import Header, HTTPException, Request, status
from tce_shared.events import AgentRole

from .config import get_settings
from .token_store import resolve_active_tokens


class AuthContext:
    def __init__(
        self,
        consumer: str,
        mode: str,
        role: AgentRole,
        workspace_id: str,
        user_id: str,
    ) -> None:
        self.consumer = consumer
        self.mode = mode
        self.role = role
        self.workspace_id = workspace_id
        self.user_id = user_id


async def get_auth_context(
    request: Request,
    authorization: str | None = Header(default=None),
    x_mtls_subject: str | None = Header(default=None),
    x_tce_consumer: str | None = Header(default=None),
    x_tce_role: str | None = Header(default=None),
    x_tce_workspace: str | None = Header(default=None),
    x_tce_user: str | None = Header(default=None),
) -> AuthContext:
    settings = get_settings()
    mode = settings.auth_mode.lower()

    role = AgentRole.USER
    if x_tce_role and x_tce_role in {role.value for role in AgentRole}:
        role = AgentRole(x_tce_role)

    consumer_name = (x_tce_consumer or "user").strip() or "user"
    workspace_id = (x_tce_workspace or "personal").strip() or "personal"
    user_id = (x_tce_user or consumer_name).strip() or consumer_name

    if mode in {"mtls", "dual"} and x_mtls_subject:
        return AuthContext(
            consumer=f"mtls:{consumer_name}:{x_mtls_subject}",
            mode="mtls",
            role=role,
            workspace_id=workspace_id,
            user_id=user_id,
        )

    if mode in {"bearer", "dual"} and authorization:
        parts = authorization.strip().split(" ", 1)
        active_tokens = resolve_active_tokens(settings.token_set)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] in active_tokens:
            return AuthContext(
                consumer=f"bearer:{consumer_name}",
                mode="bearer",
                role=role,
                workspace_id=workspace_id,
                user_id=user_id,
            )

    if settings.mtls_required:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="mTLS required")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials. Provide Authorization: Bearer <token> and X-TCE-Workspace/X-TCE-User headers.",
    )
