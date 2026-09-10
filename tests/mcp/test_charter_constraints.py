"""P3 §9.3 / G12 / G15 — the constraint floor, polarity, and the unresolved-effect pause.

This file owns its own payload helper on purpose: it must not edit or depend on the
fixtures in the pre-existing MCP contract test files.

What is asserted here is a WIRE CONTRACT, not an enforcement guarantee.  The constraints
array is a cooperative protocol; the only structural property is that this module is loaded
at MCP process start, so an executor editing ``tools.py`` on disk does not change what its
own running session receives.  ``docs/charter.md`` states what is and is not enforced.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tce_mcp import server, tool_profiles
from tce_mcp.tools import (
    FLOOR_CONSTRAINT_RULE_IDS,
    HARD_CONSTRAINTS,
    PLANNING_PENDING_CONSTRAINT,
    PROTECTED_PATH_PREFIXES,
    UNRESOLVED_EFFECT_PAUSE_PREFIX,
    _merge_constraints,
    _slim_takeover_result,
)
from tce_shared.charter import (
    CharterCaps,
    ResolvedCharter,
    charter_constraints,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(**overrides: Any) -> dict[str, Any]:
    """A minimal active-takeover takeover_step result with no directive and no lock."""
    payload: dict[str, Any] = {
        "state": {
            "session_id": "s-charter",
            "active": True,
            "mode": "takeover",
            "persona_mode": "normal",
            "takeover_context": {"objective": "tidy the retry ladder", "turn_count": 2},
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


def _charter(**overrides: Any) -> ResolvedCharter:
    now = datetime.now(UTC)
    kwargs: dict[str, Any] = {
        "charter_id": "11111111-1111-4111-8111-111111111111",
        "workspace_id": "ws",
        "owner_id": "owner",
        "project_id": None,
        "charter_version": "v3",
        "policy_revision": "p3-2026-09",
        "status": "active",
        "enforcement_tier": "os_sandbox",
        "permitted_roots": ("src/", "docs/"),
        "protected_write_prefixes": ("tests/", ".github/"),
        "denied_read_paths": ("~/.ssh",),
        "permitted_capabilities": frozenset({"filesystem.write"}),
        "confirm_required_capabilities": frozenset({"vcs.commit"}),
        "egress_mode": "https_only",
        "runtime_allowlist": (),
        "caps": CharterCaps(
            max_attempts=3,
            max_concurrent_dispatches=1,
            max_wall_seconds=900,
            budget_minor_units=500,
            budget_currency="USD",
            spend_enforcement="unsupported",
        ),
        "approved_by": "owner",
        "approved_at": now,
        "expires_at": now,
        "revoked_at": None,
        "narrowing_ids": (),
        "credential_risk_acknowledged": True,
        "charter_digest": "deadbeef",
    }
    kwargs.update(overrides)
    return ResolvedCharter(**kwargs)


def _rule_ids(rules: list[dict[str, Any]]) -> list[str]:
    return [str(rule.get("rule_id")) for rule in rules]


# --- G12: the floor -----------------------------------------------------------------


def test_floor_rules_survive_every_backend_list() -> None:
    """A backend list that names neither floor rule still yields both."""
    backend = [
        {
            "directive_type": "hard_constraint",
            "rule_id": "charter-roots-only",
            "polarity": "allow_only",
            "scope": {"path_prefixes": ["src/"], "actions": ["edit", "write", "delete"]},
            "enforcement": "block_and_escalate",
            "reason": "only these roots",
        }
    ]
    merged = _merge_constraints(backend)
    ids = _rule_ids(merged)

    assert "no-edit-protected-dirs" in ids
    assert "no-edit-firewall-null-response" in ids
    assert FLOOR_CONSTRAINT_RULE_IDS <= set(ids)
    # The floor comes first, before anything the wire supplied.
    assert ids[:2] == ["no-edit-protected-dirs", "no-edit-firewall-null-response"]


def test_floor_survives_a_real_charter_projection() -> None:
    """charter_constraints() emits the floor too; merging must not duplicate it."""
    merged = _merge_constraints(charter_constraints(_charter()))
    ids = _rule_ids(merged)

    assert ids.count("no-edit-protected-dirs") == 1
    assert ids.count("no-edit-firewall-null-response") == 1
    assert "charter-roots-only" in ids
    assert "charter-no-write-protected-prefixes" in ids


def test_floor_text_wins_over_a_backend_rewrite() -> None:
    """A wire copy of a floor rule cannot change the floor rule's own text."""
    merged = _merge_constraints(
        [
            {
                "directive_type": "hard_constraint",
                "rule_id": "no-edit-protected-dirs",
                "polarity": "deny",
                "scope": {"path_prefixes": [], "actions": []},
                "enforcement": "block_and_escalate",
                "reason": "weakened",
            }
        ]
    )
    by_id = {str(rule["rule_id"]): rule for rule in merged}

    assert by_id["no-edit-protected-dirs"]["scope"]["path_prefixes"] == PROTECTED_PATH_PREFIXES
    assert by_id["no-edit-protected-dirs"]["reason"] != "weakened"


def test_backend_none_is_the_local_fallback_byte_for_byte() -> None:
    assert _merge_constraints(None) == HARD_CONSTRAINTS
    assert _merge_constraints([]) == HARD_CONSTRAINTS
    assert _merge_constraints("not a list") == HARD_CONSTRAINTS
    # The fallback keeps the pre-P3 rule that the charter projection deliberately drops.
    assert "must-check-context-before-edit" in _rule_ids(_merge_constraints(None))


def test_merge_never_mutates_the_module_globals() -> None:
    before_hard = copy.deepcopy(HARD_CONSTRAINTS)
    before_prefixes = list(PROTECTED_PATH_PREFIXES)

    for _ in range(50):
        merged = _merge_constraints(charter_constraints(_charter()))
        merged.append({"rule_id": "scribble"})
        merged[0]["scope"]["path_prefixes"].append("scribble/")
        merged[0]["reason"] = "scribble"

    assert HARD_CONSTRAINTS == before_hard
    assert PROTECTED_PATH_PREFIXES == before_prefixes


# --- G12: polarity ------------------------------------------------------------------


def test_every_returned_rule_carries_a_polarity() -> None:
    for backend in (None, charter_constraints(_charter())):
        for rule in _merge_constraints(backend):
            assert rule["polarity"] in {"deny", "allow_only"}, rule["rule_id"]


def test_charter_roots_only_is_allow_only() -> None:
    by_id = {str(r["rule_id"]): r for r in _merge_constraints(charter_constraints(_charter()))}
    assert by_id["charter-roots-only"]["polarity"] == "allow_only"
    # Every other rule uses path_prefixes as a deny list.
    assert by_id["charter-no-write-protected-prefixes"]["polarity"] == "deny"
    assert by_id["no-edit-protected-dirs"]["polarity"] == "deny"


def test_a_rule_without_polarity_is_read_as_deny() -> None:
    merged = _merge_constraints(
        [{"directive_type": "hard_constraint", "rule_id": "legacy-rule", "scope": {}, "enforcement": "block_and_escalate"}]
    )
    by_id = {str(r["rule_id"]): r for r in merged}
    assert by_id["legacy-rule"]["polarity"] == "deny"


def test_local_constraint_globals_all_declare_polarity() -> None:
    for rule in [*HARD_CONSTRAINTS, PLANNING_PENDING_CONSTRAINT]:
        assert rule["polarity"] == "deny", rule["rule_id"]


def test_slim_result_attaches_the_merged_list_when_active() -> None:
    slim = _slim_takeover_result(_payload(constraints=charter_constraints(_charter())))
    ids = _rule_ids(slim["constraints"])

    assert FLOOR_CONSTRAINT_RULE_IDS <= set(ids)
    assert "charter-roots-only" in ids
    assert slim["constraints"] is not HARD_CONSTRAINTS


def test_slim_result_carries_no_constraints_when_inactive() -> None:
    payload = _payload(constraints=charter_constraints(_charter()))
    payload["state"]["active"] = False
    slim = _slim_takeover_result(payload)

    assert "constraints" not in slim
    assert "charter_active" not in slim
    assert "unresolved_effects" not in slim


# --- G15: the pause is scoped to unknown effects ------------------------------------


def test_pause_text_only_for_unknown_effects() -> None:
    healthy = _slim_takeover_result(
        _payload(
            note="do the thing",
            unresolved_effects=[
                {"effect_id": "e1", "state": "prepared"},
                {"effect_id": "e2", "state": "running"},
            ],
        )
    )
    assert UNRESOLVED_EFFECT_PAUSE_PREFIX not in str(healthy["next_step"] or "")
    assert "AUTONOMOUS MODE ACTIVE" in str(healthy["next_step"])

    paused = _slim_takeover_result(
        _payload(
            note="do the thing",
            unresolved_effects=[
                {"effect_id": "e2", "state": "running"},
                {"effect_id": "e3", "state": "unknown"},
            ],
        )
    )
    next_step = str(paused["next_step"])
    assert next_step.startswith(UNRESOLVED_EFFECT_PAUSE_PREFIX)
    assert "e3" in next_step
    # The original instruction is preserved after the pause, not replaced.
    assert "AUTONOMOUS MODE ACTIVE" in next_step


def test_pause_fires_with_no_prior_next_step() -> None:
    slim = _slim_takeover_result(
        _payload(unresolved_effects=[{"effect_id": "e9", "state": "unknown"}])
    )
    assert str(slim["next_step"]).startswith(UNRESOLVED_EFFECT_PAUSE_PREFIX)


def test_an_effect_row_without_a_state_does_not_pause() -> None:
    """Deliberate direction: the API's own final_response carries the authoritative pause,
    and over-triggering here would pause every step of a healthy dispatch (C3-G14)."""
    slim = _slim_takeover_result(
        _payload(note="work", unresolved_effects=[{"effect_id": "e1"}, "junk", None])
    )
    assert UNRESOLVED_EFFECT_PAUSE_PREFIX not in str(slim["next_step"] or "")


def test_no_unresolved_effects_key_is_harmless() -> None:
    slim = _slim_takeover_result(_payload(note="work"))
    assert slim["unresolved_effects"] == []
    assert UNRESOLVED_EFFECT_PAUSE_PREFIX not in str(slim["next_step"] or "")


# --- charter provenance passthrough --------------------------------------------------


def test_charter_provenance_is_forwarded_while_active() -> None:
    slim = _slim_takeover_result(
        _payload(charter_active=True, charter_version="v3", enforcement_tier="os_sandbox")
    )
    assert slim["charter_active"] is True
    assert slim["charter_version"] == "v3"
    assert slim["enforcement_tier"] == "os_sandbox"


def test_charter_provenance_defaults_are_not_a_claim_of_enforcement() -> None:
    slim = _slim_takeover_result(_payload())
    assert slim["charter_active"] is False
    assert slim["charter_version"] == ""
    assert slim["enforcement_tier"] is None


# --- §9.3: no new MCP tool, and no override claim ------------------------------------


def test_no_supervisor_tool_is_exposed() -> None:
    """P3 adds no MCP tool: an executor must not be able to start its own governed work."""
    for profile, names in tool_profiles.PROFILE_TOOLS.items():
        overlap = names & tool_profiles.SUPERVISOR_ONLY_TOOL_NAMES
        assert overlap == set(), f"{profile} exposes {sorted(overlap)}"

    declared = {str(tool.name) for tool in server.mcp._tool_manager.list_tools()}
    assert declared & tool_profiles.SUPERVISOR_ONLY_TOOL_NAMES == set()


def test_the_override_claim_is_deleted_everywhere_it_was_asserted() -> None:
    """P3 §0.6: `override constraint <rule_id>` was documentation with no implementing code.

    The phrase may still appear as an explicit statement that it does NOT work; what must be
    gone is the instruction telling an executor to honour it.
    """
    banned = "Only override when user explicitly commands"
    for relative in ("CLAUDE.md", "AGENTS.md", "services/tce_mcp/tce_mcp/tools.py"):
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert banned not in text, relative

    for relative in ("CLAUDE.md", "AGENTS.md"):
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "There is no override." in text, relative
        assert "polarity" in text, relative
