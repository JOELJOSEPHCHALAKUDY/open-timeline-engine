"""P2 §10.4 — MCP contract for the task-state / planning passthrough.

This file owns its own payload helper on purpose: it must not edit or depend on the
fixtures in the pre-existing MCP contract test files.
"""

from __future__ import annotations

import re
from typing import Any

from tce_mcp import server
from tce_mcp.config import Settings
from tce_mcp.tools import HARD_CONSTRAINTS, PLANNING_PENDING_CONSTRAINT, _slim_takeover_result

PLANNING_RULE_ID = "no-execute-while-planning-pending"


def _payload(**overrides: Any) -> dict[str, Any]:
    """A minimal active-takeover takeover_step result with no directive and no lock."""
    payload: dict[str, Any] = {
        "state": {
            "session_id": "s-task-state",
            "active": True,
            "mode": "takeover",
            "persona_mode": "normal",
            "takeover_context": {"objective": "fix the dashboard scroll", "turn_count": 3},
        },
        "action": "advisor_takeover",
        "classification": "decisive",
        "enforced": True,
        "final_response": None,
        "safety_decision": "allow",
        "needs_human": False,
        "execution_permit_required": False,
        "execution_permit_id": None,
        "directive_state": None,
        "pending_execution": None,
    }
    payload.update(overrides)
    return payload


def _rule_ids(slim: dict[str, Any]) -> list[str]:
    return [str(item.get("rule_id")) for item in slim.get("constraints", [])]


def test_planning_pending_branch_emits_actionable_next_step() -> None:
    slim = _slim_takeover_result(
        _payload(
            planning_pending=True,
            planning_job_id="9d1a0f3c-1f5e-4a83-9f7d-6b2a0f1c8e44",
            planning_pending_hint_ms=1500,
            task_state_revision=3,
        )
    )

    assert slim["has_directive"] is False
    assert "PLANNING IN PROGRESS" in slim["next_step"]
    assert "1500" in slim["next_step"]
    assert slim["planning_pending"] is True
    assert slim["planning_job_id"] == "9d1a0f3c-1f5e-4a83-9f7d-6b2a0f1c8e44"
    assert slim["planning_pending_hint_ms"] == 1500
    assert slim["task_state_revision"] == 3
    assert PLANNING_RULE_ID in _rule_ids(slim)


def test_planning_pending_hint_defaults_when_absent() -> None:
    slim = _slim_takeover_result(_payload(planning_pending=True))

    assert "1500" in slim["next_step"]
    # The whitelist key reports what the API actually sent, not the fallback used in the text.
    assert slim["planning_pending_hint_ms"] == 0


def test_live_directive_wins_over_planning_pending() -> None:
    """C1-G20: a directive must never be hidden behind a "poll again" message."""
    slim = _slim_takeover_result(
        _payload(
            planning_pending=True,
            note="work the objective",
            directive_id="4e1d8f22-6a1b-4f0e-8f52-1c9a2d7b3e10",
        )
    )

    assert slim["has_directive"] is True
    assert "AUTONOMOUS MODE ACTIVE" in slim["next_step"]
    assert "PLANNING IN PROGRESS" not in slim["next_step"]


def test_execution_claim_required_wins_over_planning_pending() -> None:
    slim = _slim_takeover_result(
        _payload(
            planning_pending=True,
            note="work the objective",
            execution_permit_required=True,
            execution_permit_id="0f9d4a1e-7c2b-4e6a-a1d0-3b5c8e2f9a77",
            directive_state="pending",
        )
    )

    assert slim["has_directive"] is False
    assert "Execution claim is required" in slim["next_step"]
    assert "PLANNING IN PROGRESS" not in slim["next_step"]


def test_execution_permit_required_wins_over_planning_pending() -> None:
    slim = _slim_takeover_result(
        _payload(planning_pending=True, note="work the objective", execution_permit_required=True)
    )

    assert "Execution permit is required" in slim["next_step"]
    assert "PLANNING IN PROGRESS" not in slim["next_step"]


def test_execution_lock_blocks_the_planning_branch() -> None:
    """R1-G7 / CLAUDE.md 18a: the lock is pending_execution OR directive_state in {pending, in_progress}."""
    for lock in (
        {"directive_state": "in_progress"},
        {"directive_state": "pending"},
        {"pending_execution": {"directive_id": "8a0b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d"}},
    ):
        slim = _slim_takeover_result(_payload(planning_pending=True, **lock))
        assert slim["next_step"] is None or "PLANNING IN PROGRESS" not in str(slim["next_step"]), lock


def test_hard_constraints_global_is_never_mutated() -> None:
    """C2-G17 / C3-G5: HARD_CONSTRAINTS is assigned by reference every active turn."""
    before = len(HARD_CONSTRAINTS)
    before_ids = [item["rule_id"] for item in HARD_CONSTRAINTS]

    for index in range(100):
        _slim_takeover_result(
            _payload(
                planning_pending=bool(index % 2),
                planning_pending_hint_ms=index * 10,
                note="work" if index % 3 == 0 else None,
                task_state_revision=index,
            )
        )

    assert len(HARD_CONSTRAINTS) == before
    assert [item["rule_id"] for item in HARD_CONSTRAINTS] == before_ids
    assert PLANNING_PENDING_CONSTRAINT["rule_id"] not in before_ids


def test_planning_constraint_absent_when_not_pending() -> None:
    slim = _slim_takeover_result(_payload(planning_pending=False))

    assert PLANNING_RULE_ID not in _rule_ids(slim)
    assert _rule_ids(slim) == [item["rule_id"] for item in HARD_CONSTRAINTS]


def test_constraints_list_is_a_fresh_copy_each_turn() -> None:
    first = _slim_takeover_result(_payload(planning_pending=True))
    second = _slim_takeover_result(_payload(planning_pending=False))

    assert first["constraints"] is not HARD_CONSTRAINTS
    assert second["constraints"] is not HARD_CONSTRAINTS
    assert first["constraints"] is not second["constraints"]


def test_inactive_turn_carries_no_constraints() -> None:
    payload = _payload(planning_pending=True)
    payload["state"]["active"] = False
    slim = _slim_takeover_result(payload)

    assert "constraints" not in slim


def test_citations_passthrough_is_capped_at_twelve() -> None:
    citations = [f"0000000{index:04d}-0000-4000-8000-000000000000" for index in range(20)]
    slim = _slim_takeover_result(_payload(citations=citations))

    assert slim["citations"] == citations[:12]
    assert all(isinstance(item, str) for item in slim["citations"])


def test_task_state_passthrough_defaults() -> None:
    slim = _slim_takeover_result(_payload())

    assert slim["planning_pending"] is False
    assert slim["planning_job_id"] is None
    assert slim["planning_pending_hint_ms"] == 0
    assert slim["task_state_revision"] == 0
    assert slim["task_state"] == {}
    assert slim["citations"] == []


def test_task_state_resource_is_registered_and_not_a_tool() -> None:
    templates = {template.uri_template for template in server.mcp._resource_manager._templates.values()}
    assert "tce://workspace/{workspace_id}/task/{task_id}/state.md" in templates

    tool_names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert not any("task_state" in name or "state.md" in name for name in tool_names)


def test_task_state_resource_mime_type_is_markdown_utf8() -> None:
    template = next(
        item
        for item in server.mcp._resource_manager._templates.values()
        if item.uri_template == "tce://workspace/{workspace_id}/task/{task_id}/state.md"
    )
    assert template.mime_type == "text/markdown; charset=utf-8"


def test_takeover_step_description_mentions_planning_pending() -> None:
    tools_by_name = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    description = str(tools_by_name["tce.takeover_step"].description)
    assert "planning_pending=true" in description


# Every tool name or parameter matching token|secret|key that already existed before P2.
# P2 adds no tool and no parameter, so this set must not grow.  It is pinned rather than
# asserted empty because the pre-existing names are real fields of the P0/P1 surface.
_PRE_P2_CREDENTIAL_SHAPED_NAMES = {
    "tce.assign_behavior_projection_pilot.trial_key",
    "tce.complete_task.completion_key",
    "tce.consume_capability_grant.token",
    "tce.get_takeover_state.activation_keywords",
    "tce.get_takeover_state.stop_keywords",
    "tce.report_execution.idempotency_key",
    "tce.report_resume_feedback.archaeology_tokens",
    "tce.reset_takeover_state.activation_keywords",
    "tce.reset_takeover_state.stop_keywords",
    "tce.takeover_preload.activation_keywords",
    "tce.takeover_preload.stop_keywords",
    "tce.takeover_step.activation_keywords",
    "tce.takeover_step.stop_keywords",
}


def test_no_new_credential_shaped_tool_names_or_parameters() -> None:
    forbidden = re.compile(r"token|secret|key", re.IGNORECASE)
    found: set[str] = set()
    for tool in server.mcp._tool_manager.list_tools():
        assert not forbidden.search(tool.name), tool.name
        properties = (tool.parameters or {}).get("properties") or {}
        for parameter in properties:
            if forbidden.search(str(parameter)):
                found.add(f"{tool.name}.{parameter}")

    assert found == _PRE_P2_CREDENTIAL_SHAPED_NAMES


def test_settings_gain_no_task_state_field() -> None:
    assert not any("task_state" in name for name in Settings.model_fields)
