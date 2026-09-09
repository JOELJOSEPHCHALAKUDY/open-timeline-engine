from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Header, HTTPException, Request, status
from tce_shared.events import AgentRole
from tce_shared.identity import claim_conflicts, parse_identity_claims, resolve_bound_identity
from tce_shared.scope import ResolvedScope, resolve_scope

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
        behavior_subject_id: str | None = None,
    ) -> None:
        self.consumer = consumer
        self.mode = mode
        self.role = role
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.behavior_subject_id = (behavior_subject_id or user_id).strip() or user_id

    def resolved_scope(
        self,
        *,
        project_hint: Mapping[str, Any] | None = None,
        session_project: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        target_owner: str | None = None,
        continuity_intent: bool = False,
        source_session_id: str | None = None,
        legacy_session_scope: bool = False,
        retention_days: int = 90,
    ) -> ResolvedScope:
        """Server-bound request scope: identity comes from this context, never from the body."""
        return resolve_scope(
            self,
            project_hint=project_hint,
            session_project=session_project,
            task_id=task_id,
            target_owner=target_owner,
            continuity_intent=continuity_intent,
            source_session_id=source_session_id,
            legacy_session_scope=legacy_session_scope,
            retention_days=retention_days,
        )


async def get_auth_context(
    request: Request,
    authorization: str | None = Header(default=None),
    x_mtls_subject: str | None = Header(default=None),
    x_tce_consumer: str | None = Header(default=None),
    x_tce_role: str | None = Header(default=None),
    x_tce_workspace: str | None = Header(default=None),
    x_tce_user: str | None = Header(default=None),
    x_tce_behavior_subject: str | None = Header(default=None, alias="X-TCE-Behavior-Subject"),
) -> AuthContext:
    settings = get_settings()
    mode = settings.auth_mode.lower()

    role = AgentRole.USER
    if x_tce_role and x_tce_role in {role.value for role in AgentRole}:
        role = AgentRole(x_tce_role)

    consumer_name = (x_tce_consumer or "user").strip() or "user"
    workspace_id = (x_tce_workspace or "personal").strip() or "personal"
    user_id = (x_tce_user or consumer_name).strip() or consumer_name
    behavior_subject_id = (x_tce_behavior_subject or user_id).strip() or user_id
    if len(behavior_subject_id) > 160 or any(ord(char) < 32 for char in behavior_subject_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid X-TCE-Behavior-Subject")

    if mode in {"mtls", "dual"} and x_mtls_subject:
        bound = resolve_bound_identity(
            claims=parse_identity_claims(settings.identity_claims_json),
            kind="mtls",
            credential=x_mtls_subject,
        )
        if bound is not None:
            if settings.identity_claims_mode.lower() == "enforce" and claim_conflicts(
                bound,
                {
                    "consumer": x_tce_consumer,
                    "role": x_tce_role,
                    "workspace_id": x_tce_workspace,
                    "user_id": x_tce_user,
                    "behavior_subject_id": x_tce_behavior_subject,
                },
            ):
                raise HTTPException(status_code=403, detail="asserted identity conflicts with server-bound mTLS claim")
            return AuthContext(
                consumer=f"mtls:{bound.consumer}",
                mode="mtls",
                role=AgentRole(bound.role),
                workspace_id=bound.workspace_id,
                user_id=bound.user_id,
                behavior_subject_id=bound.behavior_subject_id,
            )
        if settings.identity_claims_mode.lower() == "enforce":
            raise HTTPException(status_code=403, detail="mTLS identity is not bound to a server claim")
        return AuthContext(
            consumer=f"mtls:{consumer_name}:{x_mtls_subject}",
            mode="mtls",
            role=role,
            workspace_id=workspace_id,
            user_id=user_id,
            behavior_subject_id=behavior_subject_id,
        )

    if mode in {"bearer", "dual"} and authorization:
        parts = authorization.strip().split(" ", 1)
        active_tokens = resolve_active_tokens(settings.token_set)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] in active_tokens:
            bound = resolve_bound_identity(
                claims=parse_identity_claims(settings.identity_claims_json),
                kind="bearer",
                credential=parts[1],
            )
            if bound is not None:
                if settings.identity_claims_mode.lower() == "enforce" and claim_conflicts(
                    bound,
                    {
                        "consumer": x_tce_consumer,
                        "role": x_tce_role,
                        "workspace_id": x_tce_workspace,
                        "user_id": x_tce_user,
                        "behavior_subject_id": x_tce_behavior_subject,
                    },
                ):
                    raise HTTPException(status_code=403, detail="asserted identity conflicts with server-bound bearer claim")
                return AuthContext(
                    consumer=f"bearer:{bound.consumer}",
                    mode="bearer",
                    role=AgentRole(bound.role),
                    workspace_id=bound.workspace_id,
                    user_id=bound.user_id,
                    behavior_subject_id=bound.behavior_subject_id,
                )
            if settings.identity_claims_mode.lower() == "enforce":
                raise HTTPException(status_code=403, detail="bearer token is not bound to a server claim")
            return AuthContext(
                consumer=f"bearer:{consumer_name}",
                mode="bearer",
                role=role,
                workspace_id=workspace_id,
                user_id=user_id,
                behavior_subject_id=behavior_subject_id,
            )

    if settings.mtls_required:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="mTLS required")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials. Provide Authorization: Bearer <token> and X-TCE-Workspace/X-TCE-User headers.",
    )
