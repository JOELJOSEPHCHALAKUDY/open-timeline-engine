"""MCP contract for the trusted-capture channel.

The executor-facing MCP surface must (a) relay the capture-channel health and
the open decision opportunity so an executor can honour the unattended pause,
and (b) expose NO way for an executor to post human input or hold a host
credential: the capture channel is host-only by construction.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tce_mcp import server
from tce_mcp.config import Settings as McpSettings
from tce_mcp.tools import HARD_CONSTRAINTS, _slim_takeover_result

_OPPORTUNITY_ID = "6f3c2c1e-2a6d-4b9a-9d1e-0c4f0f2a7b11"
# Host/API-credential shapes only. Two pre-existing parameters are deliberately NOT matched:
# `tce.consume_capability_grant.token` (a one-use capability grant, consumed server-side) and
# `tce.report_resume_feedback.archaeology_tokens` (an LLM token count).
_CREDENTIAL_PARAM = re.compile(r"(api_?tokens?|bearer|host_?capture|host_?token|credential|secret|password)")
_PAUSE_TEXT = (
    "AUTONOMOUS MODE PAUSED: capture channel unavailable (state=gap). Unattended mutation requires the trusted "
    "human-input audit channel. Ask the user to restore the host capture hook or switch the autonomy profile to human_consultative."
)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": {
            "session_id": "s1",
            "active": True,
            "mode": "takeover",
            "persona_mode": "normal",
            "takeover_context": {"objective": "ship feature", "turn_count": 4},
        },
        "action": "advisor_takeover",
        "classification": "decisive",
        "enforced": True,
        "final_response": None,
        "safety_decision": "allow",
        "note": "directive",
        "decision_confidence": 0.83,
        "decision_source": "fast_path",
        "next_action": {"kind": "execute", "target": "ship feature", "rationale": "bounded-autonomy execution"},
        "needs_human": False,
        "latency_breakdown_ms": {"state": 2, "classify": 3, "retrieval": 10, "safety": 1, "total": 20},
        "execution_permit_required": False,
        "execution_permit_id": None,
        "continuity_ok": True,
    }
    base.update(overrides)
    return base


def test_slim_result_relays_capture_delivery_state_and_open_opportunity() -> None:
    slim = _slim_takeover_result(_payload(capture_delivery_state="gap", open_decision_opportunity_id=_OPPORTUNITY_ID))
    assert slim["capture_delivery_state"] == "gap"
    assert slim["open_decision_opportunity_id"] == _OPPORTUNITY_ID


def test_slim_result_defaults_capture_delivery_state_to_unknown() -> None:
    slim = _slim_takeover_result(_payload())
    assert slim["capture_delivery_state"] == "unknown"
    assert slim["open_decision_opportunity_id"] is None


def test_capture_channel_pause_constraint_is_machine_readable_and_attached_when_active() -> None:
    by_id = {constraint["rule_id"]: constraint for constraint in HARD_CONSTRAINTS}
    assert "pause-when-capture-channel-down" in by_id
    rule = by_id["pause-when-capture-channel-down"]
    assert rule["directive_type"] == "hard_constraint"
    assert rule["enforcement"] == "pre_action_required"
    assert {"edit", "write", "delete", "execute"} <= set(rule["scope"]["actions"])
    assert "capture_delivery_state" in rule["reason"]

    active = _slim_takeover_result(_payload(capture_delivery_state="unavailable"))
    assert "pause-when-capture-channel-down" in {constraint["rule_id"] for constraint in active["constraints"]}

    inactive_state = {"session_id": "s1", "active": False, "mode": "takeover", "persona_mode": "normal", "takeover_context": {}}
    inactive = _slim_takeover_result(_payload(state=inactive_state, action="idle", enforced=False))
    assert "constraints" not in inactive


def test_capture_channel_pause_text_reaches_the_executor_verbatim() -> None:
    slim = _slim_takeover_result(_payload(needs_human=True, final_response=_PAUSE_TEXT, capture_delivery_state="gap"))
    assert slim["has_directive"] is False
    assert slim["final_response"] == _PAUSE_TEXT
    assert slim["next_step"] == _PAUSE_TEXT
    assert slim["capture_delivery_state"] == "gap"


def test_no_executor_tool_can_post_human_input_or_hold_a_host_credential() -> None:
    tools = list(server.mcp._tool_manager.list_tools())
    names = {tool.name for tool in tools}
    assert names, "tool registry must be non-empty"
    for name in names:
        lowered = name.lower()
        assert "input" not in lowered and "capture" not in lowered, f"executor surface exposes a capture-shaped tool: {name}"
    for tool in tools:
        for parameter in tool.parameters.get("properties", {}):
            # Credential-shaped names only; e.g. `archaeology_tokens` is an LLM token *count*, not a secret.
            assert not _CREDENTIAL_PARAM.search(parameter.lower()), f"{tool.name} exposes a credential-shaped parameter: {parameter}"
        description = json.dumps(tool.parameters) + str(getattr(tool, "description", "") or "")
        assert "/v1/inputs" not in description, f"{tool.name} references the host-capture route"
    assert not any("host_capture" in field for field in McpSettings.model_fields), sorted(McpSettings.model_fields)


def test_takeover_step_tool_description_mentions_capture_delivery_state() -> None:
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    assert "tce.takeover_step" in tools
    assert "capture_delivery_state" in str(tools["tce.takeover_step"].description or "")
