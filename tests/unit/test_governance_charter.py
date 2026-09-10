"""Governance tells the truth about what is enforced, and the wire actually carries it.

Two halves, and the second is the one that is easy to forget: ``build_governance_status`` can return
a perfectly honest dict and every new key can still be silently dropped at the boundary, because
``GovernanceStatusResponse`` is a closed model and FastAPI filters the handler's dict down to the
declared fields.  A key that never reaches the wire is not a truth surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.charter import CREDENTIAL_CONTAINMENT_LIMITATION, CharterCaps, ResolvedCharter
from tce_shared.events import GovernanceStatusResponse
from tce_shared.governance import build_governance_status

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

ACTION_TRACING_LIMITATION = (
    "Per-command action tracing is unavailable for shell commands; the OS sandbox boundary is "
    "the only enforcement for their contents."
)
NO_CHARTER_LIMITATION = "No active authority charter; mutating dispatch and mutating claims are refused."
ADVISORY_LIMITATION = "Enforcement tier is advisory: no OS boundary is applied and detailed action tracing is unavailable."
UID_LIMITATION = (
    "The supervisor runs as the same OS user as the manager; separation is sandbox-profile-only, not UID-level."
)


def _charter(tier: str = "os_sandbox", spend_enforcement: str = "unsupported") -> ResolvedCharter:
    return ResolvedCharter(
        charter_id="charter-1",
        workspace_id="ws",
        owner_id="owner",
        project_id=None,
        charter_version="v1",
        policy_revision="p3-2026-09",
        status="active",
        enforcement_tier=tier,  # type: ignore[arg-type]
        permitted_roots=("services/",),
        protected_write_prefixes=("tests/",),
        denied_read_paths=("~/.ssh",),
        permitted_capabilities=frozenset({"filesystem.write"}),
        confirm_required_capabilities=frozenset(),
        egress_mode="deny_all",
        runtime_allowlist=(),
        caps=CharterCaps(
            max_attempts=3,
            max_concurrent_dispatches=1,
            max_wall_seconds=1800,
            budget_minor_units=500,
            budget_currency="USD",
            spend_enforcement=spend_enforcement,  # type: ignore[arg-type]
        ),
        approved_by="owner",
        approved_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=6),
        revoked_at=None,
        narrowing_ids=(),
        charter_digest="digest",
        credential_risk_acknowledged=True,
    )


def _status(**overrides: object) -> dict[str, Any]:
    kwargs: dict[str, object] = {
        "runtime": "full",
        "runtime_profile": "custom",
        "auth_mode": "bearer",
        "identity_claims_mode": "enforce",
        "workspace_access_mode": "strict",
        "audit_write_mode": "durable",
        "cors_origins": ["https://example.test"],
        "api_tokens": ["a-real-token"],
        "capability_broker_enabled": True,
        "mcp_tool_profile": "core",
        "requested_execution_enforcement": "protocol_only",
        "execution_interception_attested": False,
        "execution_interception_provider": "",
    }
    kwargs.update(overrides)
    return build_governance_status(**kwargs)  # type: ignore[arg-type]


PASSING_SELF_TEST = {"passed": True, "sandbox_provider": "seatbelt", "ran_at": "2026-09-09T11:59:00+00:00", "uid_separation": False}


# --- G14 first half: enforcement is measured, not claimed -----------------------------------------


def test_enforcement_is_measured_not_claimed() -> None:
    enforced = _status(charter=_charter("os_sandbox"), sandbox_self_test=PASSING_SELF_TEST)
    assert enforced["effective_execution_enforcement"] == "sandbox_enforced"
    assert enforced["non_bypassable_execution"] is True

    failed = _status(charter=_charter("os_sandbox"), sandbox_self_test={**PASSING_SELF_TEST, "passed": False})
    assert failed["effective_execution_enforcement"] == "protocol_only"

    unmeasured = _status(charter=_charter("os_sandbox"), sandbox_self_test=None)
    assert unmeasured["effective_execution_enforcement"] == "protocol_only"


def test_an_advisory_charter_is_protocol_only_regardless_of_the_self_test() -> None:
    status = _status(charter=_charter("advisory"), sandbox_self_test=PASSING_SELF_TEST)
    assert status["effective_execution_enforcement"] == "protocol_only"
    assert ADVISORY_LIMITATION in status["limitations"]


def test_a_container_charter_with_a_passing_self_test_is_enforced() -> None:
    status = _status(charter=_charter("container"), sandbox_self_test={**PASSING_SELF_TEST, "sandbox_provider": "container"})
    assert status["effective_execution_enforcement"] == "sandbox_enforced"
    assert status["sandbox_provider"] == "container"


def test_the_pre_charter_interception_path_still_works() -> None:
    status = _status(
        requested_execution_enforcement="hook_advisory",
        execution_interception_attested=True,
        execution_interception_provider="claude-hooks",
    )
    assert status["effective_execution_enforcement"] == "hook_advisory"
    assert status["charter_active"] is False


# --- the limitation lines are conditions, not constants -------------------------------------------


def test_action_tracing_line_is_emitted_verbatim_when_unavailable() -> None:
    assert ACTION_TRACING_LIMITATION in _status(charter=_charter(), action_tracing_available=False)["limitations"]
    assert ACTION_TRACING_LIMITATION not in _status(charter=_charter(), action_tracing_available=True)["limitations"]


def test_no_charter_line_appears_only_without_a_charter() -> None:
    assert NO_CHARTER_LIMITATION in _status()["limitations"]
    assert NO_CHARTER_LIMITATION not in _status(charter=_charter())["limitations"]


def test_uid_separation_line_appears_while_the_supervisor_shares_the_managers_uid() -> None:
    assert UID_LIMITATION in _status(charter=_charter(), sandbox_self_test=PASSING_SELF_TEST)["limitations"]
    separated = _status(charter=_charter(), sandbox_self_test={**PASSING_SELF_TEST, "uid_separation": True})
    assert UID_LIMITATION not in separated["limitations"]
    assert separated["uid_separation"] is True


def test_spend_line_names_the_actual_enforcement_level() -> None:
    limitations = _status(charter=_charter(spend_enforcement="estimated"))["limitations"]
    assert any("Spend caps for the selected runtime are estimated" in line for line in limitations)
    enforced = _status(charter=_charter(spend_enforcement="enforced"))["limitations"]
    assert not any("Spend caps for the selected runtime" in line for line in enforced)


def test_the_credential_line_is_emitted_for_os_sandbox_only() -> None:
    assert CREDENTIAL_CONTAINMENT_LIMITATION in _status(charter=_charter("os_sandbox"))["limitations"]
    assert CREDENTIAL_CONTAINMENT_LIMITATION not in _status(charter=_charter("container"))["limitations"]
    assert CREDENTIAL_CONTAINMENT_LIMITATION not in _status()["limitations"]


def test_the_existing_cooperative_protocol_admission_is_unchanged() -> None:
    limitations = _status()["limitations"]
    assert (
        "Host file and command mutations outside TCE are not intercepted; "
        "permit/claim/report remains a cooperative protocol." in limitations
    )


# --- G14 second half: the wire carries it ---------------------------------------------------------


def test_response_model_carries_every_new_key() -> None:
    """Without this, eleven honest keys are computed and then silently filtered off the wire."""
    status = _status(charter=_charter(), sandbox_self_test=PASSING_SELF_TEST, action_tracing_available=True)
    assert set(status) <= set(GovernanceStatusResponse.model_fields)


def test_the_new_keys_survive_the_response_model() -> None:
    status = _status(charter=_charter("os_sandbox"), sandbox_self_test=PASSING_SELF_TEST)
    serialised = GovernanceStatusResponse(**status).model_dump()
    assert serialised["charter_active"] is True
    assert serialised["charter_id"] == "charter-1"
    assert serialised["charter_version"] == "v1"
    assert serialised["charter_expires_at"] == (NOW + timedelta(hours=6)).isoformat()
    assert serialised["enforcement_tier"] == "os_sandbox"
    assert serialised["sandbox_self_test_passed"] is True
    assert serialised["sandbox_self_test_at"] == "2026-09-09T11:59:00+00:00"
    assert serialised["sandbox_provider"] == "seatbelt"
    assert serialised["action_tracing_available"] is False
    assert serialised["spend_enforcement"] == "unsupported"
    assert serialised["uid_separation"] is False


def test_a_charterless_status_still_validates() -> None:
    serialised = GovernanceStatusResponse(**_status()).model_dump()
    assert serialised["charter_active"] is False
    assert serialised["charter_id"] is None
    assert serialised["enforcement_tier"] is None
