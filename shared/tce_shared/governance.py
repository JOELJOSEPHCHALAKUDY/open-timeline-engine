from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_ENFORCEMENT_LEVELS = {"protocol_only", "hook_advisory", "sandbox_enforced"}
_INSECURE_DEFAULT_TOKENS = {"", "changeme", "local-dev-token"}


def _normalized(value: Any, fallback: str) -> str:
    return str(value or fallback).strip().lower()


def build_governance_status(
    *,
    runtime: str,
    runtime_profile: str,
    auth_mode: str,
    identity_claims_mode: str,
    workspace_access_mode: str,
    audit_write_mode: str,
    cors_origins: Iterable[str],
    api_tokens: Iterable[str],
    capability_broker_enabled: bool,
    mcp_tool_profile: str,
    requested_execution_enforcement: str,
    execution_interception_attested: bool,
    execution_interception_provider: str,
) -> dict[str, Any]:
    requested = _normalized(requested_execution_enforcement, "protocol_only")
    if requested not in _ENFORCEMENT_LEVELS:
        requested = "protocol_only"

    provider = str(execution_interception_provider or "").strip()
    interception_attested = bool(execution_interception_attested and provider)
    if interception_attested and requested == "sandbox_enforced":
        effective = "sandbox_enforced"
    elif interception_attested and requested == "hook_advisory":
        effective = "hook_advisory"
    else:
        effective = "protocol_only"

    normalized_origins = [str(origin).strip() for origin in cors_origins if str(origin).strip()]
    normalized_tokens = {str(token).strip() for token in api_tokens}
    checks = {
        "authenticated_transport": _normalized(auth_mode, "bearer") in {"bearer", "mtls", "dual"},
        "server_bound_identity": _normalized(identity_claims_mode, "compat") == "enforce",
        "strict_workspace_scope": _normalized(workspace_access_mode, "compat") == "strict",
        "durable_audit": _normalized(audit_write_mode, "async") == "durable",
        "restricted_cors": "*" not in normalized_origins,
        "nondefault_credentials": bool(normalized_tokens)
        and not bool(normalized_tokens & _INSECURE_DEFAULT_TOKENS),
        "capability_broker": bool(capability_broker_enabled),
    }
    server_boundary_secure = all(checks.values())
    non_bypassable_execution = effective == "sandbox_enforced"
    production_autonomy_ready = server_boundary_secure and non_bypassable_execution

    limitations: list[str] = []
    if not checks["server_bound_identity"]:
        limitations.append("Client identity headers are not bound to server-side credential claims.")
    if not checks["strict_workspace_scope"]:
        limitations.append("Workspace membership is operating in compatibility mode.")
    if not checks["durable_audit"]:
        limitations.append("Audit writes may complete asynchronously.")
    if not checks["restricted_cors"]:
        limitations.append("Wildcard CORS is enabled.")
    if not checks["nondefault_credentials"]:
        limitations.append("A default or empty API credential is configured.")
    if not checks["capability_broker"]:
        limitations.append("The capability broker is disabled.")
    if not non_bypassable_execution:
        limitations.append(
            "Host file and command mutations outside TCE are not intercepted; "
            "permit/claim/report remains a cooperative protocol."
        )

    return {
        "runtime": _normalized(runtime, "unknown"),
        "runtime_profile": str(runtime_profile or "custom").strip() or "custom",
        "auth_mode": _normalized(auth_mode, "bearer"),
        "identity_claims_mode": _normalized(identity_claims_mode, "compat"),
        "workspace_access_mode": _normalized(workspace_access_mode, "compat"),
        "audit_write_mode": _normalized(audit_write_mode, "async"),
        "mcp_tool_profile": _normalized(mcp_tool_profile, "core"),
        "requested_execution_enforcement": requested,
        "effective_execution_enforcement": effective,
        "lifecycle_protocol_enforced": True,
        "host_mutation_interception": interception_attested,
        "interception_provider": provider or None,
        "non_bypassable_execution": non_bypassable_execution,
        "server_boundary_checks": checks,
        "server_boundary_secure": server_boundary_secure,
        "production_autonomy_ready": production_autonomy_ready,
        "limitations": limitations,
        "schema_version": "v1",
    }
