"""Charter model, narrowing monotonicity, and the honesty table.

The load-bearing assertions here are the ones that would let a charter *widen* authority or claim
enforcement it does not have:

* ``CHARTER_FIELD_ENFORCEMENT`` is total over ``ResolvedCharter`` — no field may exist without a
  named enforcer, and no enforcer entry may name a field that does not exist.
* narrowing is monotone in every direction, over generated inputs, not three hand-picked cases.
* ``evaluate_execution_permit(charter=None)`` is byte-identical to the pre-charter ladder.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tce_shared.autonomy_goals import evaluate_execution_permit
from tce_shared.behavior_control import CAPABILITY_REGISTRY
from tce_shared.budget import spend_enforcement_for
from tce_shared.charter import (
    CAPS_FIELD_ENFORCEMENT,
    CHARTER_FIELD_ENFORCEMENT,
    CREDENTIAL_CONTAINMENT_LIMITATION,
    DENIED_READ_PATHS_LIMITATION,
    ENFORCEMENT_TIERS,
    FIELD_ENFORCED_BY,
    TIER_STRENGTH,
    CharterCaps,
    CharterInvalid,
    CharterRequired,
    ResolvedCharter,
    capability_for_action_kind,
    charter_constraints,
    charter_from_row,
    charter_sensitive_path_hit,
    is_mutating_action_kind,
    narrow_charter,
    require_charter_for_action,
    resolved_charter_from_json,
    resolved_charter_to_json,
    unenforced_denied_read_paths,
    validate_charter_payload,
)
from tce_shared.events import AutonomyPolicyProfile, AutonomyRiskTier, ExecutionPermitDecision
from tce_shared.governance import build_governance_status
from tce_shared.runtime_contract import SURFACES

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _caps(**overrides: object) -> CharterCaps:
    values: dict[str, object] = {
        "max_attempts": 3,
        "max_concurrent_dispatches": 2,
        "max_wall_seconds": 1800,
        "budget_minor_units": 500,
        "budget_currency": "USD",
        "spend_enforcement": "unsupported",
    }
    values.update(overrides)
    return CharterCaps(**values)  # type: ignore[arg-type]


def _charter(**overrides: object) -> ResolvedCharter:
    values: dict[str, object] = {
        "charter_id": "charter-1",
        "workspace_id": "ws",
        "owner_id": "owner",
        "project_id": None,
        "charter_version": "v1",
        "policy_revision": "p3-2026-09",
        "status": "active",
        "enforcement_tier": "os_sandbox",
        "permitted_roots": ("services/", "shared/"),
        "protected_write_prefixes": ("tests/", ".github/", ".local/"),
        "denied_read_paths": ("~/.ssh",),
        "permitted_capabilities": frozenset({"filesystem.read", "filesystem.write", "process.execute", "git.write"}),
        "confirm_required_capabilities": frozenset(),
        "egress_mode": "deny_all",
        "runtime_allowlist": (("codex", "0.153.1", "codex/app-server"),),
        "caps": _caps(),
        "approved_by": "owner",
        "approved_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=6),
        "revoked_at": None,
        "narrowing_ids": (),
        "charter_digest": "digest",
        "schema_version": "v1",
        "credential_risk_acknowledged": True,
    }
    values.update(overrides)
    return ResolvedCharter(**values)  # type: ignore[arg-type]


# --- G8: no declared-but-unenforced charter field ------------------------------------------------


def test_every_field_has_a_named_enforcer() -> None:
    field_names = {field.name for field in dataclasses.fields(ResolvedCharter)}
    assert set(CHARTER_FIELD_ENFORCEMENT) == field_names
    assert set(CHARTER_FIELD_ENFORCEMENT.values()) <= set(FIELD_ENFORCED_BY)
    assert not any("domain" in name for name in field_names)


def test_every_cap_has_a_named_enforcer() -> None:
    cap_names = {field.name for field in dataclasses.fields(CharterCaps)}
    assert set(CAPS_FIELD_ENFORCEMENT) == cap_names
    assert set(CAPS_FIELD_ENFORCEMENT.values()) <= set(FIELD_ENFORCED_BY)


# --- G8b: an "unsupported" enforcer must be named in limitations ---------------------------------


def _status(charter: ResolvedCharter | None, **overrides: object) -> dict[str, Any]:
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
        "charter": charter,
    }
    kwargs.update(overrides)
    return build_governance_status(**kwargs)  # type: ignore[arg-type]


def test_unsupported_fields_are_named_in_limitations() -> None:
    unsupported = [name for name, enforcer in CHARTER_FIELD_ENFORCEMENT.items() if enforcer == "unsupported"]
    # This gate is live for exactly one entry today; if a future builder adds another it must also
    # gain a limitation line, and this loop will fail until it does.
    assert unsupported == ["credential_risk_acknowledged"]
    limitations = _status(_charter(enforcement_tier="os_sandbox"))["limitations"]
    assert isinstance(limitations, list)
    assert CREDENTIAL_CONTAINMENT_LIMITATION in limitations
    # And a charter that is NOT os_sandbox does not carry the line, so it is a condition and not a
    # constant that would pass with the work undone.
    assert CREDENTIAL_CONTAINMENT_LIMITATION not in _status(_charter(enforcement_tier="container"))["limitations"]


# --- G11: narrowing is monotone ------------------------------------------------------------------


def test_narrowing_is_monotone() -> None:
    rng = random.Random(20260909)
    base = _charter()
    for _ in range(500):
        narrowing = {
            "remove_roots": rng.sample(list(base.permitted_roots), rng.randint(0, len(base.permitted_roots))),
            "remove_capabilities": rng.sample(sorted(base.permitted_capabilities), rng.randint(0, 2)),
            "add_protected_write_prefixes": rng.sample(["docs/", "infra/", "scripts/"], rng.randint(0, 3)),
            "add_denied_read_paths": rng.sample(["~/.aws", "~/.codex"], rng.randint(0, 2)),
            "enforcement_tier": rng.choice([None, *ENFORCEMENT_TIERS]),
            "egress_mode": rng.choice([None, "deny_all", "https_only"]),
            "budget_minor_units": rng.choice([None, 0, 100, 5000]),
            "max_attempts": rng.choice([None, 1, 3, 99]),
            "max_wall_seconds": rng.choice([None, 60, 1800, 86400]),
            "max_concurrent_dispatches": rng.choice([None, 1, 2, 8]),
        }
        narrowed = narrow_charter(base, narrowing)
        assert set(narrowed.permitted_roots) <= set(base.permitted_roots)
        assert narrowed.permitted_capabilities <= base.permitted_capabilities
        assert set(narrowed.protected_write_prefixes) >= set(base.protected_write_prefixes)
        assert set(narrowed.denied_read_paths) >= set(base.denied_read_paths)
        assert TIER_STRENGTH[narrowed.enforcement_tier] <= TIER_STRENGTH[base.enforcement_tier]
        assert narrowed.caps.budget_minor_units <= base.caps.budget_minor_units
        assert narrowed.caps.max_attempts <= base.caps.max_attempts
        assert narrowed.caps.max_wall_seconds <= base.caps.max_wall_seconds
        assert narrowed.caps.max_concurrent_dispatches <= base.caps.max_concurrent_dispatches
        assert narrowed.expires_at <= base.expires_at
        if base.egress_mode == "deny_all":
            assert narrowed.egress_mode == "deny_all"


def test_narrowing_ignores_unknown_keys_and_never_raises() -> None:
    base = _charter()
    narrowed = narrow_charter(base, {"nonsense": ["/"], "permitted_roots": ["/"]})
    # An unrecognised key changes nothing.  ``protected_write_prefixes`` and ``denied_read_paths``
    # come back sorted (the fold is set-based and deterministic), so compare their content.
    assert narrowed.permitted_roots == base.permitted_roots
    assert narrowed.permitted_capabilities == base.permitted_capabilities
    assert set(narrowed.protected_write_prefixes) == set(base.protected_write_prefixes)
    assert set(narrowed.denied_read_paths) == set(base.denied_read_paths)
    assert narrowed.caps == base.caps
    assert narrowed.enforcement_tier == base.enforcement_tier
    assert narrowed.egress_mode == base.egress_mode


def test_charter_from_row_applies_and_records_narrowings() -> None:
    row = {
        "id": "charter-9",
        "workspace_id": "ws",
        "owner_id": "owner",
        "charter_version": "v1",
        "policy_revision": "p3-2026-09",
        "status": "active",
        "enforcement_tier": "container",
        "permitted_roots_json": json.dumps(["services/", "shared/"]),
        "protected_write_prefixes_json": json.dumps(["tests/"]),
        "denied_read_paths_json": json.dumps(["~/.ssh"]),
        "permitted_capabilities_json": json.dumps(["filesystem.write", "filesystem.read"]),
        "confirm_required_capabilities_json": json.dumps([]),
        "egress_mode": "deny_all",
        "runtime_allowlist_json": json.dumps([["codex", "0.153.1", "codex/exec"]]),
        "max_attempts": 5,
        "max_concurrent_dispatches": 3,
        "max_wall_seconds": 3600,
        "budget_minor_units": 900,
        "budget_currency": "USD",
        "spend_enforcement": "unsupported",
        "charter_digest": "d",
        "approved_by": "owner",
        "approved_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=4)).isoformat(),
        "revoked_at": None,
        "schema_version": "v1",
    }
    narrowings = [
        {"id": "n1", "narrowing_json": json.dumps({"remove_roots": ["shared/"], "max_attempts": 2})},
        {"id": "n2", "narrowing_json": json.dumps({"add_protected_write_prefixes": ["docs/"]})},
    ]
    charter = charter_from_row(row, narrowings=narrowings)
    assert charter.narrowing_ids == ("n1", "n2")
    assert charter.permitted_roots == ("services/",)
    assert charter.caps.max_attempts == 2
    assert "docs/" in charter.protected_write_prefixes
    # Without narrowings the row is unchanged, so the fold is what did the work above.
    assert charter_from_row(row).permitted_roots == ("services/", "shared/")
    assert charter_from_row(row).narrowing_ids == ()


# --- the differential: no behaviour change without a charter -------------------------------------

# Frozen from HEAD before the charter clause existed.  The grid is exhaustive over the enum
# products, so a change anywhere in the ladder moves at least one letter.
_PERMIT_SIZES = (0, 1, 19, 20, 21, 24, 25, 29, 30, 31, 99, 100, 101)
_PERMIT_ROLES = ("advisor", "ADVISOR ", "user", "manager", "executor", "")
_DECISION_LETTER = {"allow": "A", "confirm_required": "C", "blocked": "B"}
_FROZEN_PERMIT_LETTERS = (
    "BBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACAC"
    "BBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACAC"
    "BBBBACACACACBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACAC"
    "BBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACAC"
    "BBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACAC"
    "BBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACACBBBBACACACAC"
    "BBBBACACACACBBBBACACACACBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCC"
    "BBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBCCCCCCCCBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)
_FROZEN_PERMIT_DIGEST = "c52ed1d92075041a69dc859ae3f64a6ac98152394fd5ed74137ae65b9ddee8f0"


def _permit_grid() -> list[tuple[AutonomyPolicyProfile, AutonomyRiskTier, int, str, bool]]:
    grid: list[tuple[AutonomyPolicyProfile, AutonomyRiskTier, int, str, bool]] = []
    for profile in AutonomyPolicyProfile:
        for tier in AutonomyRiskTier:
            for size in _PERMIT_SIZES:
                for role in _PERMIT_ROLES:
                    for hit in (False, True):
                        grid.append((profile, tier, size, role, hit))
    return grid


def test_permit_evaluation_is_unchanged_without_a_charter() -> None:
    letters: list[str] = []
    full: list[list[str]] = []
    for profile, tier, size, role, hit in _permit_grid():
        decision, reason = evaluate_execution_permit(
            policy_profile=profile,
            risk_tier=tier,
            estimated_change_size=size,
            role=role,
            sensitive_path_hit=hit,
            charter=None,
        )
        letters.append(_DECISION_LETTER[decision.value])
        full.append([decision.value, reason])
    assert len(letters) == len(_FROZEN_PERMIT_LETTERS) == 1872
    assert "".join(letters) == _FROZEN_PERMIT_LETTERS
    digest = hashlib.sha256(json.dumps(full, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert digest == _FROZEN_PERMIT_DIGEST


def test_charter_never_widens() -> None:
    rng = random.Random(4242)
    charters = [
        _charter(
            permitted_roots=tuple(rng.sample(["services/", "shared/", "docs/", "tests/"], rng.randint(1, 3))),
            permitted_capabilities=frozenset(rng.sample(sorted(CAPABILITY_REGISTRY), rng.randint(1, len(CAPABILITY_REGISTRY)))),
            confirm_required_capabilities=frozenset(rng.sample(sorted(CAPABILITY_REGISTRY), rng.randint(0, 2))),
        )
        for _ in range(50)
    ]
    order = {ExecutionPermitDecision.ALLOW: 0, ExecutionPermitDecision.CONFIRM_REQUIRED: 1, ExecutionPermitDecision.BLOCKED: 2}
    for profile, tier, size, role, hit in rng.sample(_permit_grid(), 400):
        charter = rng.choice(charters)
        without, _ = evaluate_execution_permit(
            policy_profile=profile, risk_tier=tier, estimated_change_size=size, role=role, sensitive_path_hit=hit
        )
        with_charter, _ = evaluate_execution_permit(
            policy_profile=profile,
            risk_tier=tier,
            estimated_change_size=size,
            role=role,
            sensitive_path_hit=hit,
            charter=charter,
            action_kind=rng.choice(["edit", "write", "commit", "read", "execute", "unheard-of"]),
            target_paths=rng.sample(["services/a.py", "tests/b.py", "docs/c.md", "../escape", "/abs"], 2),
        )
        assert order[with_charter] >= order[without]


# --- validate_charter_payload --------------------------------------------------------------------


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "enforcement_tier": "container",
        "permitted_roots": ["services/"],
        "protected_write_prefixes": ["tests/"],
        "permitted_capabilities": ["filesystem.write"],
        "egress_mode": "deny_all",
    }
    values.update(overrides)
    return values


def test_advisory_tier_requires_no_mutating_capability() -> None:
    with pytest.raises(CharterInvalid) as excinfo:
        validate_charter_payload(_payload(enforcement_tier="advisory"))
    assert excinfo.value.field == "permitted_capabilities"
    # A read-only advisory charter is fine.
    validate_charter_payload(_payload(enforcement_tier="advisory", permitted_capabilities=["filesystem.read"]))


def test_os_sandbox_mutating_requires_credential_acknowledgement() -> None:
    with pytest.raises(CharterInvalid) as excinfo:
        validate_charter_payload(_payload(enforcement_tier="os_sandbox"))
    assert excinfo.value.field == "credential_risk_acknowledged"
    normalised = validate_charter_payload(_payload(enforcement_tier="os_sandbox", credential_risk_acknowledged=True))
    assert normalised["credential_risk_acknowledged"] is True


def test_protected_write_prefixes_may_not_be_empty() -> None:
    with pytest.raises(CharterInvalid) as excinfo:
        validate_charter_payload(_payload(protected_write_prefixes=[]))
    assert excinfo.value.field == "protected_write_prefixes"


def test_permitted_roots_may_not_be_empty() -> None:
    with pytest.raises(CharterInvalid) as excinfo:
        validate_charter_payload(_payload(permitted_roots=[]))
    assert excinfo.value.field == "permitted_roots"


@pytest.mark.parametrize("bad", ["/etc", "../escape", "~/secrets", "a/../../b"])
def test_roots_must_be_relative_posix(bad: str) -> None:
    with pytest.raises(CharterInvalid):
        validate_charter_payload(_payload(permitted_roots=[bad]))


def test_denied_read_paths_outside_home_are_named_as_unenforced() -> None:
    """MEASURED, not argued: `/private/etc/hosts` named in denied_read_paths is read rc=0 inside
    the live rendered profile, because /private/etc is one of the profile's read allows and
    `render_profile` renders no clause from this field at all. The honesty table calls the field's
    enforcer "os" on the strength of the blanket $HOME deny; this is the line that stops that label
    covering the entries it does not reach.
    """
    inside_home = _charter(denied_read_paths=("~/.ssh", "~/Library/Keychains"))
    assert unenforced_denied_read_paths(inside_home) == ()
    assert not [line for line in _status(inside_home)["limitations"] if DENIED_READ_PATHS_LIMITATION in line]

    outside = _charter(denied_read_paths=("~/.ssh", "/private/etc/hosts", "/opt/homebrew/etc"))
    assert unenforced_denied_read_paths(outside) == ("/private/etc/hosts", "/opt/homebrew/etc")
    named = [line for line in _status(outside)["limitations"] if DENIED_READ_PATHS_LIMITATION in line]
    assert len(named) == 1
    assert "/private/etc/hosts" in named[0] and "/opt/homebrew/etc" in named[0]
    # The genuinely enforced entry is NOT named, so the line is a measurement and not a blanket.
    assert "~/.ssh" not in named[0]


def test_no_charter_field_may_name_a_domain() -> None:
    with pytest.raises(CharterInvalid) as excinfo:
        validate_charter_payload(_payload(allowed_network_domains=["example.com"]))
    assert "domain" in excinfo.value.field


def test_unknown_capability_is_refused() -> None:
    with pytest.raises(CharterInvalid):
        validate_charter_payload(_payload(permitted_capabilities=["filesystem.teleport"]))


def test_a_charter_may_not_advertise_a_spend_ceiling_no_surface_enforces() -> None:
    """The honesty gate on `caps.spend_enforcement`.

    `build_governance_status` reads this value straight onto the wire and DROPS the
    "recorded cost is an estimate and may overshoot" limitation line when it says
    "enforced" (`test_spend_line_names_the_actual_enforcement_level`).  Meanwhile every
    dispatch row is stamped from `budget.spend_enforcement_for(surface)`, where no surface
    is enforced.  Unvalidated, a create body could therefore turn the honest line off while
    nothing about the actual enforcement changed.
    """
    assert "enforced" not in {spend_enforcement_for(surface) for surface in SURFACES}
    with pytest.raises(CharterInvalid) as excinfo:
        validate_charter_payload(_payload(caps={"spend_enforcement": "enforced"}))
    assert excinfo.value.field == "caps"
    # Non-vacuous in both directions: the two labels the measurement DOES support pass, and a
    # value outside the enum is refused rather than silently coerced into the row.
    for honest in ("estimated", "unsupported"):
        assert validate_charter_payload(_payload(caps={"spend_enforcement": honest}))["caps"]["spend_enforcement"] == honest
    with pytest.raises(CharterInvalid):
        validate_charter_payload(_payload(caps={"spend_enforcement": "totally-enforced-trust-me"}))


# --- U2: the shared refusal ----------------------------------------------------------------------


def test_require_charter_refuses_a_mutating_action_without_a_charter() -> None:
    with pytest.raises(CharterRequired) as excinfo:
        require_charter_for_action(None, action_kind="edit", enforcement_enabled=True, now=NOW)
    assert excinfo.value.reason == "no_active_charter"
    assert excinfo.value.action_kind == "edit"


def test_require_charter_names_expiry_and_revocation_distinctly() -> None:
    expired = _charter(expires_at=NOW - timedelta(minutes=1))
    with pytest.raises(CharterRequired) as expired_info:
        require_charter_for_action(expired, action_kind="commit", enforcement_enabled=True, now=NOW)
    assert expired_info.value.reason == "charter_expired"

    revoked = _charter(revoked_at=NOW - timedelta(minutes=5))
    with pytest.raises(CharterRequired) as revoked_info:
        require_charter_for_action(revoked, action_kind="commit", enforcement_enabled=True, now=NOW)
    assert revoked_info.value.reason == "charter_revoked"


def test_require_charter_is_a_no_op_for_reads_and_when_disabled() -> None:
    require_charter_for_action(None, action_kind="read", enforcement_enabled=True, now=NOW)
    require_charter_for_action(None, action_kind="edit", enforcement_enabled=False, now=NOW)
    require_charter_for_action(_charter(), action_kind="edit", enforcement_enabled=True, now=NOW)


def test_action_kind_mapping_fails_closed() -> None:
    assert capability_for_action_kind("edit") == "filesystem.write"
    assert capability_for_action_kind("commit") == "git.write"
    # An unheard-of kind maps to the highest-risk mutating capability, never to a read.
    assert capability_for_action_kind("teleport") == "process.execute"
    assert is_mutating_action_kind("takeover_step") is True
    assert is_mutating_action_kind("review") is False


# --- G12's pure half: one path list ---------------------------------------------------------------


def test_sensitive_path_hit_without_a_charter_is_the_legacy_behaviour() -> None:
    assert charter_sensitive_path_hit(None, ["services/tce_mcp/tools.py"]) is True
    assert charter_sensitive_path_hit(None, ["infra/alembic/env.py"]) is True
    assert charter_sensitive_path_hit(None, [".env"]) is True
    assert charter_sensitive_path_hit(None, ["services/tce_api/main.py"]) is False


def test_sensitive_path_hit_with_a_charter_uses_the_charter() -> None:
    charter = _charter(permitted_roots=("services/",), protected_write_prefixes=("services/tce_api/",))
    assert charter_sensitive_path_hit(charter, ["services/tce_api/main.py"]) is True
    assert charter_sensitive_path_hit(charter, ["docs/readme.md"]) is True
    assert charter_sensitive_path_hit(charter, ["services/tce_worker/run.py"]) is False


def test_sensitive_tokens_come_from_the_charter() -> None:
    """G12's tree half.

    The guard below is deliberately narrow: it skips only while a backend file has not adopted the
    shared charter module at all.  The moment Builder B or C imports it, the assertion is live and
    cannot be satisfied by leaving the inline literal in place.
    """
    targets = [
        REPO_ROOT / "services" / "tce_api" / "tce_api" / "main.py",
        REPO_ROOT / "services" / "tce_lite_api" / "tce_lite_api" / "store.py",
    ]
    sources = {path: path.read_text(encoding="utf-8") for path in targets if path.exists()}
    unadopted = [path.name for path, text in sources.items() if "tce_shared.charter" not in text]
    if unadopted:
        pytest.skip(f"backend has not adopted tce_shared.charter yet: {', '.join(sorted(unadopted))}")
    for path, text in sources.items():
        assert '("services/tce_mcp", "infra/", "secrets", ".env")' not in text, path
        assert "charter_sensitive_path_hit(" in text, path


# --- constraints: the floor a charter may not delete ---------------------------------------------


def test_charter_constraints_prepend_the_floor_rules() -> None:
    rules = charter_constraints(_charter(confirm_required_capabilities=frozenset({"git.write"})))
    rule_ids = [rule["rule_id"] for rule in rules]
    assert rule_ids[0] == "no-edit-protected-dirs"
    assert rule_ids[1] == "no-edit-firewall-null-response"
    assert "charter-no-write-protected-prefixes" in rule_ids
    assert "charter-roots-only" in rule_ids
    assert "charter-capability-confirm:git.write" in rule_ids
    assert "pause-when-capture-channel-down" in rule_ids
    # check_context is evidence, not authority: it is not projected as a charter constraint.
    assert "must-check-context-before-edit" not in rule_ids


def test_every_constraint_declares_a_polarity() -> None:
    rules = charter_constraints(_charter())
    assert all(rule["polarity"] in ("deny", "allow_only") for rule in rules)
    by_id = {rule["rule_id"]: rule for rule in rules}
    # The one allow-list in a deny-shaped field.  Without the flag a conforming executor reads
    # "these are the only writable roots" as "never write to these roots".
    assert by_id["charter-roots-only"]["polarity"] == "allow_only"
    assert by_id["no-edit-protected-dirs"]["polarity"] == "deny"
    assert by_id["charter-no-write-protected-prefixes"]["polarity"] == "deny"


def test_floor_rule_prefixes_are_the_protected_infrastructure() -> None:
    rules = {rule["rule_id"]: rule for rule in charter_constraints(_charter(protected_write_prefixes=("docs/",)))}
    floor = rules["no-edit-protected-dirs"]["scope"]["path_prefixes"]
    assert "shared/tce_shared/" in floor
    assert "services/tce_mcp/" in floor
    # A charter with a narrow prefix list does not shrink the floor.
    assert rules["charter-no-write-protected-prefixes"]["scope"]["path_prefixes"] == ["docs/"]


# --- codecs ---------------------------------------------------------------------------------------


def test_resolved_charter_json_round_trips() -> None:
    charter = _charter(narrowing_ids=("n1",), project_id="proj")
    assert resolved_charter_from_json(resolved_charter_to_json(charter)) == charter


def test_is_active_requires_status_expiry_and_no_revocation() -> None:
    assert _charter().is_active(NOW) is True
    assert _charter(status="draft").is_active(NOW) is False
    assert _charter(revoked_at=NOW).is_active(NOW) is False
    assert _charter(expires_at=NOW).is_active(NOW) is False


def test_is_protected_write_checks_every_prefix_not_just_the_first() -> None:
    charter = _charter(protected_write_prefixes=("tests/", ".github/", ".local/"))
    assert charter.is_protected_write("tests/unit/x.py") is True
    assert charter.is_protected_write(".github/workflows/ci.yml") is True
    assert charter.is_protected_write(".local/tce-lite.db") is True
    assert charter.is_protected_write("services/x.py") is False


def test_allows_path_rejects_traversal_and_absolute_paths() -> None:
    charter = _charter(permitted_roots=("services/",))
    assert charter.allows_path("services/tce_api/main.py") is True
    assert charter.allows_path("services/../etc/passwd") is False
    assert charter.allows_path("/etc/passwd") is False
    assert charter.allows_path("~/.ssh/id_rsa") is False
