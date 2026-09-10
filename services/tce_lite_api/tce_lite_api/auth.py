from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException
from tce_shared.decision_capture import HOST_CAPTURE_CAPABILITY
from tce_shared.events import AgentRole
from tce_shared.identity import BoundIdentity, claim_conflicts, parse_identity_claims, resolve_bound_identity
from tce_shared.scope import ResolvedScope, resolve_scope

from .config import get_settings


@dataclass(slots=True)
class AuthContext:
    consumer: str
    role: AgentRole
    workspace_id: str
    user_id: str
    behavior_subject_id: str
    # Capabilities come only from the credential (host-capture token or a bound claim), never from headers.
    capabilities: frozenset[str] = field(default_factory=frozenset)
    # True only when identity was established by the server (a server-bound bearer claim or the
    # host-capture credential). False on the compat path where role/user come from X-TCE-* headers;
    # an unverified USER is never treated as an authenticated human for evidence promotion.
    identity_verified: bool = False

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


def _parse_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _bound_capabilities(bound: BoundIdentity) -> frozenset[str]:
    """Capabilities granted by a server-bound claim (only ``host_capture`` is recognised).

    No executor receives host attestation: ``host_capture`` on a claim whose role is not ``user`` is ignored.
    """
    if str(getattr(bound, "role", "") or "") != AgentRole.USER.value:
        return frozenset()
    raw = getattr(bound, "capabilities", ()) or ()
    return frozenset(str(item) for item in raw if str(item) == HOST_CAPTURE_CAPABILITY)


def get_auth_context(
    authorization: str | None = Header(default=None),
    consumer: str | None = Header(default=None, alias="X-TCE-Consumer"),
    role: str | None = Header(default=None, alias="X-TCE-Role"),
    workspace: str | None = Header(default=None, alias="X-TCE-Workspace"),
    user: str | None = Header(default=None, alias="X-TCE-User"),
    behavior_subject: str | None = Header(default=None, alias="X-TCE-Behavior-Subject"),
) -> AuthContext:
    settings = get_settings()
    token = _parse_bearer_token(authorization)
    host_tokens = settings.host_capture_token_set
    api_tokens = settings.token_set
    if token and token in host_tokens and token in api_tokens:
        # Config error: one credential must never carry both the executor and the host-attestation capability.
        raise HTTPException(status_code=403, detail="credential is configured as both api and host-capture token")
    is_host_token = bool(token and token in host_tokens)
    if not token or (not is_host_token and token not in api_tokens):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials. Provide Authorization: Bearer <token> and X-TCE-Workspace/X-TCE-User headers.",
        )
    normalized_workspace = str(workspace or "personal").strip() or "personal"
    if is_host_token:
        normalized_consumer = str(consumer or "host-capture").strip() or "host-capture"
    else:
        normalized_consumer = str(consumer or "local-user").strip() or "local-user"
    normalized_user = str(user or "").strip() or normalized_consumer
    normalized_behavior_subject = str(behavior_subject or "").strip() or normalized_user
    if len(normalized_behavior_subject) > 160 or any(ord(char) < 32 for char in normalized_behavior_subject):
        raise HTTPException(status_code=400, detail="invalid X-TCE-Behavior-Subject")
    bound = resolve_bound_identity(
        claims=parse_identity_claims(settings.identity_claims_json),
        kind="bearer",
        credential=token,
    )
    enforce = settings.identity_claims_mode.lower() == "enforce"
    if bound is not None and enforce and claim_conflicts(
        bound,
        {
            "consumer": consumer,
            "role": role,
            "workspace_id": workspace,
            "user_id": user,
            "behavior_subject_id": behavior_subject,
        },
    ):
        raise HTTPException(status_code=403, detail="asserted identity conflicts with server-bound bearer claim")

    if is_host_token:
        # Host-capture principal: role is forced to USER and the capability is bound to the credential.
        if bound is not None:
            return AuthContext(
                consumer=f"host:{bound.consumer}",
                role=AgentRole.USER,
                workspace_id=bound.workspace_id,
                user_id=bound.user_id,
                behavior_subject_id=bound.behavior_subject_id,
                capabilities=_bound_capabilities(bound) | {HOST_CAPTURE_CAPABILITY},
                identity_verified=True,
            )
        if enforce:
            raise HTTPException(status_code=403, detail="host capture token is not bound to a server claim")
        return AuthContext(
            consumer=f"host:{normalized_consumer}",
            role=AgentRole.USER,
            workspace_id=normalized_workspace,
            user_id=normalized_user,
            behavior_subject_id=normalized_behavior_subject,
            capabilities=frozenset({HOST_CAPTURE_CAPABILITY}),
            identity_verified=True,
        )

    if bound is not None:
        try:
            bound_role = AgentRole(bound.role)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="server identity claim has invalid role") from exc
        return AuthContext(
            consumer=bound.consumer,
            role=bound_role,
            workspace_id=bound.workspace_id,
            user_id=bound.user_id,
            behavior_subject_id=bound.behavior_subject_id,
            # A bound claim may grant host_capture to an api token; the compat header path never can.
            capabilities=_bound_capabilities(bound),
            identity_verified=True,
        )
    if enforce:
        raise HTTPException(status_code=403, detail="bearer token is not bound to a server claim")
    try:
        parsed_role = AgentRole(role or "user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid X-TCE-Role") from exc
    return AuthContext(
        consumer=normalized_consumer,
        role=parsed_role,
        workspace_id=normalized_workspace,
        user_id=normalized_user,
        behavior_subject_id=normalized_behavior_subject,
    )
