from __future__ import annotations

from typing import Any

from tce_shared.governance import build_governance_status


def _status(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "runtime": "full",
        "runtime_profile": "team-secure",
        "auth_mode": "bearer",
        "identity_claims_mode": "enforce",
        "workspace_access_mode": "strict",
        "audit_write_mode": "durable",
        "cors_origins": [],
        "api_tokens": ["deployment-secret"],
        "capability_broker_enabled": True,
        "mcp_tool_profile": "core",
        "requested_execution_enforcement": "protocol_only",
        "execution_interception_attested": False,
        "execution_interception_provider": "",
    }
    values.update(overrides)
    return build_governance_status(**values)


def test_secure_server_boundary_does_not_claim_host_enforcement() -> None:
    status = _status()

    assert status["server_boundary_secure"] is True
    assert status["effective_execution_enforcement"] == "protocol_only"
    assert status["non_bypassable_execution"] is False
    assert status["production_autonomy_ready"] is False
    assert any("cooperative protocol" in item for item in status["limitations"])


def test_sandbox_enforcement_requires_provider_attestation() -> None:
    unattested = _status(requested_execution_enforcement="sandbox_enforced")
    attested = _status(
        requested_execution_enforcement="sandbox_enforced",
        execution_interception_attested=True,
        execution_interception_provider="test-sandbox",
    )

    assert unattested["effective_execution_enforcement"] == "protocol_only"
    assert attested["effective_execution_enforcement"] == "sandbox_enforced"
    assert attested["non_bypassable_execution"] is True
    assert attested["production_autonomy_ready"] is True


def test_default_credentials_and_wildcard_cors_fail_server_boundary() -> None:
    status = _status(cors_origins=["*"], api_tokens=["local-dev-token"])

    assert status["server_boundary_secure"] is False
    assert status["server_boundary_checks"]["restricted_cors"] is False
    assert status["server_boundary_checks"]["nondefault_credentials"] is False
