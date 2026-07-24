"""Behavioral sentinels for the _slim_takeover_result firewall.

These pin the properties the firewall exists for: directive text never
reaches the executor verbatim, lifecycle gaps force a pause, constraints
ride along on every active turn, and safety text is never clobbered.
"""

import json
from typing import Any

from tce_mcp.tools import _slim_takeover_result

SENTINEL = "SENTINEL-DIRECTIVE-TEXT-9f3a"


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": {
            "session_id": "s1",
            "active": True,
            "mode": "takeover",
            "persona_mode": "normal",
            "takeover_context": {"objective": "ship feature", "turn_count": 5},
        },
        "action": "advisor_takeover",
        "classification": "decisive",
        "enforced": True,
        "final_response": None,
        "safety_decision": "allow",
        "note": None,
        "decision_confidence": 0.8,
        "decision_source": "fast_path",
        "next_action": {"kind": "execute", "target": "ship feature", "rationale": "r"},
        "needs_human": False,
        "latency_breakdown_ms": {},
        "selected_goal": None,
        "execution_permit_required": False,
        "execution_permit_id": None,
        "continuity_ok": True,
    }
    state_overrides = overrides.pop("state", None)
    base.update(overrides)
    if state_overrides:
        base["state"].update(state_overrides)
    return base


def test_directive_in_note_is_never_echoed() -> None:
    slim = _slim_takeover_result(_payload(note=SENTINEL))
    assert slim["has_directive"] is True
    assert SENTINEL not in json.dumps(slim)


def test_directive_in_final_response_is_stripped() -> None:
    # The "reverted API" case: enforced non-safety directive in final_response.
    slim = _slim_takeover_result(_payload(final_response=SENTINEL))
    assert slim["has_directive"] is True
    assert slim["final_response"] is None
    assert SENTINEL not in json.dumps(slim)
    assert "AUTONOMOUS MODE ACTIVE" in (slim["next_step"] or "")


def test_constraints_attached_on_every_active_turn() -> None:
    slim = _slim_takeover_result(_payload(note="do the thing"))
    rule_ids = {c["rule_id"] for c in slim.get("constraints", [])}
    assert "no-edit-protected-dirs" in rule_ids
    assert "no-edit-firewall-null-response" in rule_ids
    assert "must-check-context-before-edit" in rule_ids


def test_constraints_absent_when_inactive() -> None:
    slim = _slim_takeover_result(
        _payload(action="inactive", state={"active": False})
    )
    assert "constraints" not in slim


def test_missing_permit_forces_pause() -> None:
    slim = _slim_takeover_result(
        _payload(note=SENTINEL, execution_permit_required=True, execution_permit_id=None)
    )
    assert slim["has_directive"] is False
    assert "tce.request_execution_permit" in (slim["next_step"] or "")
    assert SENTINEL not in json.dumps(slim)


def test_claim_required_forces_pause() -> None:
    slim = _slim_takeover_result(
        _payload(
            note=SENTINEL,
            execution_permit_required=True,
            execution_permit_id="p-1",
            directive_state="pending",
        )
    )
    assert slim["has_directive"] is False
    assert "tce.claim_execution" in (slim["next_step"] or "")
    assert SENTINEL not in json.dumps(slim)


def test_safety_confirm_text_survives_persona_ack() -> None:
    # Activation turn + persona + a pending safety confirmation: the safety
    # text is what the user must see — the persona ack must not clobber it.
    safety_text = "Safety pause: confirm before I delete the database (yes/no)."
    slim = _slim_takeover_result(
        _payload(
            note="delete the database",
            safety_decision="confirm_required",
            final_response=safety_text,
            state={"persona_mode": "shadow", "takeover_context": {"objective": "cleanup", "turn_count": 0}},
        )
    )
    assert safety_text in (slim["final_response"] or "")


def test_stopped_action_clears_directive() -> None:
    slim = _slim_takeover_result(_payload(note=SENTINEL, action="stopped"))
    assert slim["has_directive"] is False
