"""P4 §9.3 / G-X2 — the MCP contract for ``policy_decision`` and its one conditional rule.

Two things are asserted here and they are different in kind.

The first is a **wire contract**: the eleven keys of ``PolicyDecisionBlock`` survive the
firewall in ``_slim_takeover_result``, no twelfth key does, and in particular no score does.
That is a property of this process and it holds.

The second is a **cooperative protocol**: ``no-execute-on-policy-abstention`` tells an
executor to stop.  Nothing in this process, in either backend, or in the operating system
makes it stop.  What does hold, and is the reason the rule lives here rather than in a
prompt, is that this module is loaded into the MCP server at process start — an executor
editing ``tools.py`` on disk does not change the constraints its own running session
receives.  That is a reload boundary, not a sandbox.

The load-bearing assertion in this file is the **negative** one.  Today zero decision
families are qualified, so every turn carries ``exposed=false`` and the rule must not appear.
A version of this rule without the ``exposed`` conjunct would fire on every single turn and
forbid all work — which is the failure P2's R10 and this file both exist to prevent.

This file owns its own payload helper: it must not edit or depend on the fixtures in the
pre-existing MCP contract test files.
"""

from __future__ import annotations

import re
from typing import Any

from tce_mcp import server
from tce_mcp.config import Settings
from tce_mcp.tools import (
    HARD_CONSTRAINTS,
    PLANNING_PENDING_CONSTRAINT,
    POLICY_ABSTENTION_CONSTRAINT,
    POLICY_DECISION_KEYS,
    _policy_abstention_applies,
    _slim_policy_decision,
    _slim_takeover_result,
)

POLICY_RULE_ID = "no-execute-on-policy-abstention"
PLANNING_RULE_ID = "no-execute-while-planning-pending"


def _payload(**overrides: Any) -> dict[str, Any]:
    """A minimal active-takeover takeover_step result with no directive and no lock."""
    payload: dict[str, Any] = {
        "state": {
            "session_id": "s-policy",
            "active": True,
            "mode": "takeover",
            "persona_mode": "normal",
            "takeover_context": {"objective": "rework the retry ladder", "turn_count": 4},
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


def _block(**overrides: Any) -> dict[str, Any]:
    """A ``PolicyDecisionBlock`` as ``DecisionResult.block_payload()`` emits it.

    The default is today's real state: abstained for want of candidate options, and NOT
    exposed, because no family is qualified.
    """
    block: dict[str, Any] = {
        "status": "abstained",
        "selected_option": None,
        "abstain_reason": "no_candidate_match",
        "reason_for_asking": "No two options were offered, so there was nothing to choose between.",
        "ood_status": "unknown",
        "conflict_status": "none",
        "evidence_observation_ids": [],
        "decision_policy_revision": "p4-2026-09",
        "exposed": False,
        "exposure_state": "no_qualification",
        "advisor_agreement": "absent",
    }
    block.update(overrides)
    return block


def _rule_ids(rules: list[dict[str, Any]]) -> list[str]:
    return [str(rule.get("rule_id")) for rule in rules]


def _backend_charter_rules() -> list[dict[str, Any]]:
    """Two backend-projected rules, shaped as a charter projection, with no floor rule."""
    return [
        {
            "directive_type": "hard_constraint",
            "rule_id": "charter-roots-only",
            "polarity": "allow_only",
            "scope": {"path_prefixes": ["src/"], "actions": ["edit", "write"]},
            "enforcement": "block_and_escalate",
            "reason": "The charter permits writes under src/ only.",
        },
        {
            "directive_type": "hard_constraint",
            "rule_id": "charter-protected-writes",
            "polarity": "deny",
            "scope": {"path_prefixes": ["tests/"], "actions": ["edit", "write", "delete"]},
            "enforcement": "block_and_escalate",
            "reason": "The charter protects tests/ from autonomous writes.",
        },
    ]


# --- the block crosses the firewall -------------------------------------------------


def test_policy_decision_survives_the_firewall() -> None:
    slim = _slim_takeover_result(_payload(policy_decision=_block()))
    assert slim["policy_decision"] == _block()


def test_every_block_key_is_forwarded() -> None:
    """The whitelist drops a key it does not name, so all eleven must be named."""
    slim = _slim_takeover_result(_payload(policy_decision=_block()))
    assert set(slim["policy_decision"]) == set(POLICY_DECISION_KEYS)
    assert len(POLICY_DECISION_KEYS) == 11


def test_no_score_crosses_the_firewall() -> None:
    """``policy_score`` is uncalibrated and must never reach an executor.

    The producer is supposed to send ``block_payload()``, which has no score.  This asserts
    the firewall holds even when a backend sends the wide ``to_payload()`` instead — the
    projection is what makes "and nothing else" true rather than assumed.
    """
    wide = _block()
    wide.update(
        {
            "policy_score": 0.83,
            "ood_score": 0.41,
            "adequacy": {"above_floor_count": 3},
            "ranked_options": [{"option": "ship", "share": 0.7}],
            "diagnostics": {"neighbours": 4},
            "thresholds_sha": "deadbeef",
        }
    )
    slim = _slim_takeover_result(_payload(policy_decision=wide))
    assert "policy_score" not in slim["policy_decision"]
    assert set(slim["policy_decision"]) == set(POLICY_DECISION_KEYS)
    assert "policy_score" not in slim


def test_calibrated_score_is_not_a_key_anywhere_on_the_block() -> None:
    """Y6: nothing is calibrated, so there is no calibrated sibling to forward."""
    assert "calibrated_score" not in POLICY_DECISION_KEYS
    slim = _slim_takeover_result(
        _payload(policy_decision=dict(_block(), calibrated_score=0.9))
    )
    assert "calibrated_score" not in slim["policy_decision"]


def test_absent_block_is_none_not_an_invented_decision() -> None:
    slim = _slim_takeover_result(_payload())
    assert slim["policy_decision"] is None


def test_a_malformed_block_is_none() -> None:
    values: list[Any] = ["abstained", 7, [], {}, {"unrelated": 1}, None]
    for value in values:
        assert _slim_policy_decision(value) is None


def test_a_partial_block_is_not_defaulted() -> None:
    """A producer that forgot ``exposed`` must not read as "checked, and not exposed"."""
    projected = _slim_policy_decision({"status": "abstained"})
    assert projected == {"status": "abstained"}
    assert "exposed" not in projected


def test_evidence_observation_ids_are_coerced_and_capped() -> None:
    projected = _slim_policy_decision(
        _block(evidence_observation_ids=[f"obs-{index}" for index in range(30)])
    )
    assert projected is not None
    ids = projected["evidence_observation_ids"]
    assert len(ids) == 12
    assert all(isinstance(item, str) for item in ids)

    coerced = _slim_policy_decision(_block(evidence_observation_ids="not-a-list"))
    assert coerced is not None
    assert coerced["evidence_observation_ids"] == []


# --- the conditional rule: the negative case is the load-bearing one -----------------


def test_unqualified_family_adds_no_rule() -> None:
    """Today's state for every family: abstained, and not exposed.  No rule, no change."""
    slim = _slim_takeover_result(_payload(policy_decision=_block()))
    assert POLICY_RULE_ID not in _rule_ids(slim["constraints"])


def test_abstention_without_exposure_never_fires_on_any_exposure_state() -> None:
    for state in ("no_qualification", "qualification_expired", "binding_mismatch", "rule_layer"):
        slim = _slim_takeover_result(
            _payload(policy_decision=_block(exposed=False, exposure_state=state))
        )
        assert POLICY_RULE_ID not in _rule_ids(slim["constraints"]), state


def test_exposure_without_abstention_adds_no_rule() -> None:
    for status in ("selected", "rule_applied"):
        slim = _slim_takeover_result(
            _payload(
                policy_decision=_block(
                    status=status,
                    selected_option="ship it",
                    abstain_reason=None,
                    exposed=True,
                    exposure_state="exposed",
                )
            )
        )
        assert POLICY_RULE_ID not in _rule_ids(slim["constraints"]), status


def test_exposed_abstention_adds_the_rule() -> None:
    slim = _slim_takeover_result(
        _payload(policy_decision=_block(exposed=True, exposure_state="exposed"))
    )
    assert POLICY_RULE_ID in _rule_ids(slim["constraints"])


def test_exposed_is_identity_not_truthiness() -> None:
    """A truthy non-True ``exposed`` is a producer bug, not permission."""
    for value in (1, "true", "yes", [1]):
        assert _policy_abstention_applies(_block(exposed=value, status="abstained")) is False
    assert _policy_abstention_applies(_block(exposed=True, status="abstained")) is True
    assert _policy_abstention_applies(None) is False


def test_no_rule_when_takeover_is_inactive() -> None:
    slim = _slim_takeover_result(
        _payload(
            action="inactive",
            state={"active": False},
            policy_decision=_block(exposed=True, exposure_state="exposed"),
        )
    )
    assert "constraints" not in slim


# --- G-X2: order, polarity, and the module globals -----------------------------------


def test_constraint_order_is_stable() -> None:
    """Floor, the rest of HARD_CONSTRAINTS or the charter rules, planning, then policy."""
    slim = _slim_takeover_result(
        _payload(
            constraints=_backend_charter_rules(),
            planning_pending=True,
            policy_decision=_block(exposed=True, exposure_state="exposed"),
        )
    )
    assert _rule_ids(slim["constraints"]) == [
        "no-edit-protected-dirs",
        "no-edit-firewall-null-response",
        "charter-roots-only",
        "charter-protected-writes",
        PLANNING_RULE_ID,
        POLICY_RULE_ID,
    ]


def test_policy_rule_is_last_with_the_local_fallback_too() -> None:
    """No charter: the fallback is HARD_CONSTRAINTS in file order, then the policy rule."""
    slim = _slim_takeover_result(
        _payload(policy_decision=_block(exposed=True, exposure_state="exposed"))
    )
    ids = _rule_ids(slim["constraints"])
    assert ids[: len(HARD_CONSTRAINTS)] == _rule_ids(HARD_CONSTRAINTS)
    assert ids[-1] == POLICY_RULE_ID


def test_every_returned_rule_carries_a_polarity() -> None:
    """P3's G12, restated over a turn that carries P4's rule."""
    slim = _slim_takeover_result(
        _payload(
            constraints=_backend_charter_rules(),
            planning_pending=True,
            policy_decision=_block(exposed=True, exposure_state="exposed"),
        )
    )
    for rule in slim["constraints"]:
        assert rule["polarity"] in {"deny", "allow_only"}, rule["rule_id"]


def test_the_policy_template_declares_polarity_at_its_definition_site() -> None:
    assert POLICY_ABSTENTION_CONSTRAINT["polarity"] == "deny"
    assert POLICY_ABSTENTION_CONSTRAINT["enforcement"] == "block_and_escalate"
    assert POLICY_ABSTENTION_CONSTRAINT["scope"]["actions"] == ["edit", "write", "delete", "execute"]


def test_the_policy_rule_is_not_in_the_process_global_list() -> None:
    """HARD_CONSTRAINTS is assigned by reference on every active turn.

    A static append would tell every executor in this process never to mutate anything for
    the life of the process — on a corpus where no family is qualified and the rule should
    never fire at all.
    """
    assert POLICY_RULE_ID not in _rule_ids(HARD_CONSTRAINTS)
    assert POLICY_ABSTENTION_CONSTRAINT is not PLANNING_PENDING_CONSTRAINT


def test_an_exposed_abstention_turn_does_not_mutate_the_globals() -> None:
    import copy

    before_hard = copy.deepcopy(HARD_CONSTRAINTS)
    before_template = copy.deepcopy(POLICY_ABSTENTION_CONSTRAINT)
    for _ in range(3):
        slim = _slim_takeover_result(
            _payload(policy_decision=_block(exposed=True, exposure_state="exposed"))
        )
        for rule in slim["constraints"]:
            rule["reason"] = "rewritten by a hostile consumer"
    assert HARD_CONSTRAINTS == before_hard
    assert POLICY_ABSTENTION_CONSTRAINT == before_template


def test_the_returned_rule_is_a_copy_of_the_template() -> None:
    slim = _slim_takeover_result(
        _payload(policy_decision=_block(exposed=True, exposure_state="exposed"))
    )
    returned = [r for r in slim["constraints"] if r["rule_id"] == POLICY_RULE_ID][0]
    assert returned == POLICY_ABSTENTION_CONSTRAINT
    assert returned is not POLICY_ABSTENTION_CONSTRAINT


# --- the tool surface does not grow --------------------------------------------------


# P4 adds no MCP tool.  P3 left 70; this pin is what makes a silently added tool a red test.
_TOOL_COUNT_AFTER_P3 = 70

# P5 adds exactly one, `tce.dreams` (p45_shared.md §3.4), so the pin is stated as "P3's 70
# plus these named additions and nothing else" rather than as a bare count.  Naming the
# addition keeps the assertion at full strength: a tool added by anyone without amending
# this set is still a red test, and the count itself did not move.
_P5_TOOL_NAMES = {"tce.dreams"}


def test_p4_adds_no_mcp_tool() -> None:
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert len(names - _P5_TOOL_NAMES) == _TOOL_COUNT_AFTER_P3
    assert not any("policy" in name for name in names)


def test_p4_adds_no_tool_parameter_named_like_a_secret() -> None:
    """P4 adds no parameter at all, so no P4 name can be credential-shaped."""
    forbidden = re.compile(r"token|secret|key|credential|password", re.IGNORECASE)
    for tool in server.mcp._tool_manager.list_tools():
        properties = (tool.parameters or {}).get("properties") or {}
        for parameter in properties:
            if forbidden.search(str(parameter)):
                assert "policy" not in str(parameter).lower(), f"{tool.name}.{parameter}"


def test_takeover_step_description_carries_both_conjuncts() -> None:
    tools_by_name = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    description = str(tools_by_name["tce.takeover_step"].description)
    assert "policy_decision.status is 'abstained'" in description
    assert "policy_decision.exposed is true" in description
    # And says plainly that the common case is not an instruction to stop.
    assert "exposed=false" in description
    assert "not an instruction to stop" in description


def test_settings_gain_no_policy_field() -> None:
    """The abstention floors are frozen module constants, not MCP settings (§9.2)."""
    assert not any("policy" in name for name in Settings.model_fields)
