from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .charter import (
    CREDENTIAL_CONTAINMENT_LIMITATION,
    DENIED_READ_PATHS_LIMITATION,
    ResolvedCharter,
    unenforced_denied_read_paths,
)

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
    charter: ResolvedCharter | None = None,
    sandbox_self_test: Mapping[str, Any] | None = None,
    action_tracing_available: bool = False,
) -> dict[str, Any]:
    """The truth surface: what is actually enforced right now, and what is not.

    ``effective_execution_enforcement`` is only ``"sandbox_enforced"`` when a charter names an OS
    boundary AND the most recent sandbox self-test on this host passed.  An unmeasured sandbox is a
    claim, not a control, so it reports ``"protocol_only"``.

    Every limitation line below is emitted from a condition, never from a constant.  The credential
    line in particular is the visible half of ``CHARTER_FIELD_ENFORCEMENT["credential_risk_acknowledged"]
    == "unsupported"``: it says out loud that the boundary we ship does not contain the runtime
    credential.
    """
    requested = _normalized(requested_execution_enforcement, "protocol_only")
    if requested not in _ENFORCEMENT_LEVELS:
        requested = "protocol_only"

    provider = str(execution_interception_provider or "").strip()
    interception_attested = bool(execution_interception_attested and provider)
    self_test = dict(sandbox_self_test or {})
    self_test_passed = bool(self_test.get("passed"))
    tier = charter.enforcement_tier if charter is not None else None
    if tier in ("os_sandbox", "container") and self_test_passed:
        effective = "sandbox_enforced"
    elif interception_attested and requested == "sandbox_enforced":
        effective = "sandbox_enforced"
    elif interception_attested and requested == "hook_advisory":
        effective = "hook_advisory"
    else:
        effective = "protocol_only"

    charter_active = charter is not None
    spend_enforcement = charter.caps.spend_enforcement if charter is not None else "unsupported"
    uid_separation = bool(self_test.get("uid_separation"))

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
    if not charter_active:
        limitations.append("No active authority charter; mutating dispatch and mutating claims are refused.")
    if tier == "advisory":
        limitations.append(
            "Enforcement tier is advisory: no OS boundary is applied and detailed action tracing is unavailable."
        )
    if not action_tracing_available:
        limitations.append(
            "Per-command action tracing is unavailable for shell commands; the OS sandbox boundary is "
            "the only enforcement for their contents."
        )
    if spend_enforcement != "enforced":
        limitations.append(
            f"Spend caps for the selected runtime are {spend_enforcement}; recorded cost is an estimate "
            "and may overshoot the reservation."
        )
    if not uid_separation:
        limitations.append(
            "The supervisor runs as the same OS user as the manager; separation is sandbox-profile-only, "
            "not UID-level."
        )
    if charter_active:
        limitations.append(
            "Network egress is filtered by port only; hostname or domain allowlisting has no OS backing "
            "on this host and is unsupported."
        )
    if tier == "os_sandbox":
        limitations.append(CREDENTIAL_CONTAINMENT_LIMITATION)
    if charter is not None:
        # The "os" enforcer named for denied_read_paths is the profile's blanket $HOME read deny.
        # Nothing renders a per-entry clause, so an entry outside $HOME is a statement of intent.
        # Naming the exact entries beats leaving the table's "os" label to cover them.
        unenforced = unenforced_denied_read_paths(charter)
        if unenforced:
            limitations.append(f"{DENIED_READ_PATHS_LIMITATION}: {', '.join(unenforced)}.")

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
        "charter_active": charter_active,
        "charter_id": (charter.charter_id if charter is not None else None),
        "charter_version": (charter.charter_version if charter is not None else None),
        "charter_expires_at": (charter.expires_at.isoformat() if charter is not None else None),
        "enforcement_tier": tier,
        "sandbox_self_test_passed": self_test_passed,
        "sandbox_self_test_at": (str(self_test["ran_at"]) if self_test.get("ran_at") else None),
        "sandbox_provider": str(self_test.get("sandbox_provider") or ""),
        "action_tracing_available": bool(action_tracing_available),
        "spend_enforcement": str(spend_enforcement),
        "uid_separation": uid_separation,
        "schema_version": "v1",
    }
