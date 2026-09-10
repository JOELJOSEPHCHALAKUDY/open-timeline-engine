from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from tce_shared.events import DirectiveExecutionState as S
from tce_shared.execution_transitions import (
    ALLOWED_TRANSITIONS,
    SYSTEM_ACTOR,
    TERMINAL_STATES,
    TransitionReason,
    canonical_json,
    completion_payload_fingerprint,
    find_transition,
    idempotency_outcome,
    is_terminal,
    next_lease,
    permit_binding_ok,
    permit_scope_digest,
    report_payload_fingerprint,
    validate_transition,
)

WORKER_A = "worker-a"
WORKER_B = "worker-b"


# --- claim -----------------------------------------------------------------------------------------------------


def test_claim_from_pending_bumps_lease() -> None:
    decision = validate_transition(current_state=S.PENDING, claimed_by=None, lease_generation=0, requested=S.IN_PROGRESS, actor=WORKER_A, actor_lease=None)

    assert decision.allowed is True
    assert decision.reason is TransitionReason.OK
    assert decision.next_state is S.IN_PROGRESS
    assert decision.next_lease == 1
    assert decision.http_status == 0
    assert decision.audited_as == "directive_claimed"


def test_claim_accepts_string_states_and_none_lease() -> None:
    decision = validate_transition(current_state="pending", claimed_by=None, lease_generation=None, requested="in_progress", actor=WORKER_A, actor_lease=None)

    assert decision.allowed is True
    assert decision.next_lease == 1


def test_claim_in_progress_by_other_is_rejected() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.IN_PROGRESS, actor=WORKER_B, actor_lease=None)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.CLAIMED_BY_OTHER
    assert decision.http_status == 409
    assert decision.next_state is None
    assert decision.next_lease == 1
    assert decision.audited_as == ""


def test_reclaim_by_holder_is_noop() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=3, requested=S.IN_PROGRESS, actor=WORKER_A, actor_lease=None)

    assert decision.allowed is True
    assert decision.next_state is S.IN_PROGRESS
    assert decision.next_lease == 3
    assert decision.audited_as == "directive_reclaim_noop"


def test_pending_row_already_bound_to_other_executor_rejects_claim() -> None:
    decision = validate_transition(current_state=S.PENDING, claimed_by=WORKER_A, lease_generation=0, requested=S.IN_PROGRESS, actor=WORKER_B, actor_lease=None)

    assert decision.reason is TransitionReason.CLAIMED_BY_OTHER


# --- verification matrix case 2: two workers claim one attempt concurrently -------------------------------------


@dataclass
class _Row:
    state: str
    claimed_executor: str | None
    lease_generation: int


def _fenced_apply(row: _Row, *, actor: str, expected_lease: int, next_state: str, next_lease: int) -> bool:
    """Mimic `UPDATE ... WHERE state='pending' AND lease_generation=:expected_lease`."""
    if row.state != "pending" or row.lease_generation != expected_lease:
        return False
    row.state = next_state
    row.claimed_executor = actor
    row.lease_generation = next_lease
    return True


def test_concurrent_claim_yields_exactly_one_lease_holder() -> None:
    row = _Row(state="pending", claimed_executor=None, lease_generation=0)
    snapshot_lease = row.lease_generation

    # Both workers validate against the same pre-claim snapshot; the validator lets both through.
    first = validate_transition(current_state=row.state, claimed_by=row.claimed_executor, lease_generation=snapshot_lease, requested=S.IN_PROGRESS, actor=WORKER_A, actor_lease=None)
    second = validate_transition(current_state=row.state, claimed_by=row.claimed_executor, lease_generation=snapshot_lease, requested=S.IN_PROGRESS, actor=WORKER_B, actor_lease=None)
    assert first.allowed and second.allowed
    assert first.next_lease == second.next_lease == 1

    # The fenced UPDATE admits exactly one of them.
    won = [
        _fenced_apply(row, actor=WORKER_A, expected_lease=snapshot_lease, next_state="in_progress", next_lease=first.next_lease),
        _fenced_apply(row, actor=WORKER_B, expected_lease=snapshot_lease, next_state="in_progress", next_lease=second.next_lease),
    ]
    assert won == [True, False]
    assert row.claimed_executor == WORKER_A
    assert row.lease_generation == 1

    # The loser re-reads and re-validates: it can neither claim nor report.
    loser_claim = validate_transition(current_state=row.state, claimed_by=row.claimed_executor, lease_generation=row.lease_generation, requested=S.IN_PROGRESS, actor=WORKER_B, actor_lease=None)
    assert loser_claim.reason is TransitionReason.CLAIMED_BY_OTHER
    loser_report = validate_transition(current_state=row.state, claimed_by=row.claimed_executor, lease_generation=row.lease_generation, requested=S.SUCCEEDED, actor=WORKER_B, actor_lease=1)
    assert loser_report.reason is TransitionReason.CLAIMED_BY_OTHER
    assert loser_report.http_status == 409


# --- verification matrix case 1: unclaimed report -----------------------------------------------------------------


@pytest.mark.parametrize("requested", [S.SUCCEEDED, S.FAILED, S.BLOCKED])
def test_report_success_from_pending_is_not_claimed(requested: S) -> None:
    decision = validate_transition(current_state=S.PENDING, claimed_by=None, lease_generation=0, requested=requested, actor=WORKER_A, actor_lease=None)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.NOT_CLAIMED
    assert decision.http_status == 409
    assert decision.next_state is None
    assert "claimed" in decision.message


def test_in_progress_without_bound_executor_is_not_claimed() -> None:
    # Legacy rows claimed before lease fencing carry no claimed_executor; they cannot be reported until re-claimed.
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by="", lease_generation=0, requested=S.SUCCEEDED, actor=WORKER_A, actor_lease=None)

    assert decision.reason is TransitionReason.NOT_CLAIMED


def test_system_actor_bypasses_claim_and_lease_checks() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=4, requested=S.BLOCKED, actor=SYSTEM_ACTOR, actor_lease=None)

    assert decision.allowed is True
    assert decision.next_state is S.BLOCKED


# --- reporting by the holder ------------------------------------------------------------------------------------


def test_report_by_holder_with_matching_lease_succeeds() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.SUCCEEDED, actor=WORKER_A, actor_lease=1)

    assert decision.allowed is True
    assert decision.next_state is S.SUCCEEDED
    assert decision.next_lease == 1
    assert decision.audited_as == "directive_succeeded"


def test_report_by_other_executor_rejected() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.SUCCEEDED, actor=WORKER_B, actor_lease=1)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.CLAIMED_BY_OTHER
    assert decision.http_status == 409


# --- verification matrix case 3: stale worker after lease replacement ------------------------------------------


def test_stale_lease_rejected() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=2, requested=S.SUCCEEDED, actor=WORKER_A, actor_lease=1)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.STALE_LEASE
    assert decision.http_status == 409
    assert decision.next_lease == 2


def test_stale_worker_after_reap_hits_terminal() -> None:
    reap = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.ABANDONED, actor=SYSTEM_ACTOR, actor_lease=None)
    assert reap.allowed is True
    assert reap.next_lease == 2

    late = validate_transition(current_state=reap.next_state or "", claimed_by=WORKER_A, lease_generation=reap.next_lease, requested=S.SUCCEEDED, actor=WORKER_A, actor_lease=1)
    assert late.allowed is False
    assert late.reason is TransitionReason.TERMINAL


def test_legacy_client_without_lease_accepted_when_not_strict() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=5, requested=S.FAILED, actor=WORKER_A, actor_lease=None)

    assert decision.allowed is True
    assert decision.audited_as == "directive_failed"


def test_strict_requires_lease_echo() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=5, requested=S.FAILED, actor=WORKER_A, actor_lease=None, require_lease_echo=True)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.LEASE_REQUIRED
    assert decision.http_status == 409


# --- terminal states --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=str))
@pytest.mark.parametrize("requested", list(S))
def test_terminal_rejects_everything(terminal: S, requested: S) -> None:
    decision = validate_transition(current_state=terminal, claimed_by=WORKER_A, lease_generation=7, requested=requested, actor=SYSTEM_ACTOR, actor_lease=7)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.TERMINAL
    assert decision.http_status == 409
    assert decision.next_lease == 7


def test_terminal_state_set_is_exactly_the_six_end_states() -> None:
    assert TERMINAL_STATES == frozenset({S.SUCCEEDED, S.FAILED, S.BLOCKED, S.ABANDONED, S.CANCELLED, S.REJECTED})
    assert is_terminal("succeeded") and is_terminal(S.CANCELLED)
    assert not is_terminal(S.PENDING) and not is_terminal("in_progress")
    assert not is_terminal("not-a-state")


# --- cancellation / rejection without a claim -------------------------------------------------------------------


@pytest.mark.parametrize(("requested", "audited_as"), [(S.CANCELLED, "directive_cancelled"), (S.REJECTED, "directive_rejected")])
def test_pending_cancel_and_reject_need_no_claim(requested: S, audited_as: str) -> None:
    decision = validate_transition(current_state=S.PENDING, claimed_by=None, lease_generation=0, requested=requested, actor=WORKER_B, actor_lease=None)

    assert decision.allowed is True
    assert decision.next_state is requested
    assert decision.next_lease == 0
    assert decision.audited_as == audited_as


def test_in_progress_cancel_requires_holder() -> None:
    holder = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.CANCELLED, actor=WORKER_A, actor_lease=1)
    other = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.CANCELLED, actor=WORKER_B, actor_lease=1)

    assert holder.allowed is True and holder.audited_as == "directive_cancelled"
    assert other.reason is TransitionReason.CLAIMED_BY_OTHER


def test_in_progress_reject_invalid() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.REJECTED, actor=WORKER_A, actor_lease=1)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.INVALID_TRANSITION
    assert decision.http_status == 409


# --- reaper ------------------------------------------------------------------------------------------------------


def test_reaper_transitions_system_only() -> None:
    by_worker = validate_transition(current_state=S.PENDING, claimed_by=None, lease_generation=0, requested=S.ABANDONED, actor=WORKER_A, actor_lease=None)
    assert by_worker.allowed is False
    assert by_worker.reason is TransitionReason.SYSTEM_ONLY

    by_system = validate_transition(current_state=S.PENDING, claimed_by=None, lease_generation=0, requested=S.ABANDONED, actor=SYSTEM_ACTOR, actor_lease=None)
    assert by_system.allowed is True
    assert by_system.next_lease == 0
    assert by_system.audited_as == "directive_reaped"

    in_progress = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=3, requested=S.ABANDONED, actor=SYSTEM_ACTOR, actor_lease=None)
    assert in_progress.allowed is True
    assert in_progress.next_lease == 4
    assert in_progress.audited_as == "directive_reaped"


# --- permits -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("requested", [S.SUCCEEDED, S.FAILED])
def test_permit_required_for_success_and_failure(requested: S) -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=requested, actor=WORKER_A, actor_lease=1, permit_ok=False)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.PERMIT_INVALID
    assert decision.http_status == 409


def test_permit_not_required_for_blocked() -> None:
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested=S.BLOCKED, actor=WORKER_A, actor_lease=1, permit_ok=False)

    assert decision.allowed is True
    assert decision.audited_as == "directive_blocked"


def test_claim_requires_permit() -> None:
    decision = validate_transition(current_state=S.PENDING, claimed_by=None, lease_generation=0, requested=S.IN_PROGRESS, actor=WORKER_A, actor_lease=None, permit_ok=False)

    assert decision.reason is TransitionReason.PERMIT_INVALID


def test_permit_binding_ok_rules() -> None:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    future = now + timedelta(minutes=10)
    past = now - timedelta(minutes=10)
    directive = {"directive_id": "d-1", "directive_user_id": "user-1", "directive_objective_hash": "obj-1"}

    def check(**overrides: object) -> tuple[bool, str]:
        kwargs: dict[str, object] = {
            "permit_decision": "allow",
            "permit_expires_at": future,
            "permit_user_id": "user-1",
            "permit_directive_id": "d-1",
            "permit_objective_hash": "obj-1",
            "started_at": None,
            "now": now,
            "phase": "claim",
            **directive,
        }
        kwargs.update(overrides)
        return permit_binding_ok(**kwargs)  # type: ignore[arg-type]

    assert check() == (True, "ok")
    assert check(permit_decision="confirm_required") == (False, "permit_not_allowed")
    assert check(permit_decision=None) == (False, "permit_not_allowed")
    assert check(permit_expires_at=past) == (False, "permit_expired")
    assert check(permit_expires_at=None) == (True, "ok")
    # scope mismatches on any non-NULL binding column
    assert check(permit_user_id="user-2") == (False, "permit_scope_mismatch")
    assert check(permit_directive_id="d-2") == (False, "permit_scope_mismatch")
    assert check(permit_objective_hash="obj-2") == (False, "permit_scope_mismatch")
    assert check(permit_objective_hash="obj-1", directive_objective_hash=None) == (False, "permit_scope_mismatch")
    # NULL binding columns are wildcards (legacy permits)
    assert check(permit_user_id=None, permit_directive_id=None, permit_objective_hash=None) == (True, "ok")
    # report phase: execution that began under the valid grant is honoured after expiry
    assert check(phase="report", permit_expires_at=past, started_at=past - timedelta(minutes=1)) == (True, "ok")
    assert check(phase="report", permit_expires_at=past, started_at=past + timedelta(minutes=1)) == (False, "permit_expired_before_start")
    assert check(phase="report", permit_expires_at=past, started_at=None) == (False, "permit_expired_before_start")
    assert check(phase="report", permit_expires_at=None, started_at=None) == (True, "ok")
    # naive datetimes are treated as UTC rather than raising
    assert check(permit_expires_at=future.replace(tzinfo=None)) == (True, "ok")
    with pytest.raises(ValueError):
        check(phase="verify")


def test_permit_scope_digest_is_order_and_duplicate_insensitive() -> None:
    a = permit_scope_digest(action_kind="edit", target_paths=["b.py", "a.py", "a.py"], command_preview=None)
    b = permit_scope_digest(action_kind="edit", target_paths=("a.py", "b.py"), command_preview="")

    assert a == b
    assert len(a) == 64
    assert a != permit_scope_digest(action_kind="run", target_paths=["a.py", "b.py"], command_preview="")
    assert a != permit_scope_digest(action_kind="edit", target_paths=["a.py"], command_preview="")


# --- verification matrix case 4: idempotency ---------------------------------------------------------------------


def test_idempotency_outcome() -> None:
    assert idempotency_outcome(None, "abc") == "new"
    assert idempotency_outcome("abc", "abc") == "replay"
    assert idempotency_outcome("abc", "abd") == "conflict"


def test_report_payload_fingerprint_ignores_transport_fields() -> None:
    base = {"session_id": "s1", "directive_id": "d1", "state": "succeeded", "result": "success", "details": {"files": ["a.py"]}, "lease_generation": 1, "idempotency_key": "k1"}
    retry = {**base, "session_id": "s2", "directive_id": "d2", "lease_generation": 2, "idempotency_key": "k2"}
    changed = {**base, "state": "failed"}

    assert report_payload_fingerprint(base) == report_payload_fingerprint(retry)
    assert report_payload_fingerprint(base) != report_payload_fingerprint(changed)
    assert len(report_payload_fingerprint(base)) == 64


def test_report_payload_fingerprint_treats_enum_and_string_state_alike() -> None:
    assert report_payload_fingerprint({"state": S.SUCCEEDED}) == report_payload_fingerprint({"state": "succeeded"})


def test_report_cannot_set_verified_complete() -> None:
    # Verification is not a lifecycle state: no report can request it ...
    assert find_transition(S.IN_PROGRESS, "verified_complete") is None
    decision = validate_transition(current_state=S.IN_PROGRESS, claimed_by=WORKER_A, lease_generation=1, requested="verified_complete", actor=WORKER_A, actor_lease=1)
    assert decision.allowed is False
    assert decision.reason is TransitionReason.INVALID_TRANSITION
    # ... and a caller-supplied verification_state is not part of the reported payload identity.
    assert report_payload_fingerprint({"state": "succeeded", "verification_state": "verified_complete"}) == report_payload_fingerprint({"state": "succeeded"})


def test_completion_payload_fingerprint_ignores_volatile_keys() -> None:
    a = {"title": "wire lease", "decision": "fenced update", "ts": "2026-09-09T00:00:00Z", "packet_id": "p1", "session_id": "codex-a", "id": "x"}
    b = {"title": "wire lease", "decision": "fenced update", "ts": "2026-09-10T00:00:00Z", "packet_id": "p2", "session_id": "claude-b", "created_at": "later", "captured_at": "later"}
    c = {"title": "wire lease", "decision": "different decision"}

    assert completion_payload_fingerprint(a) == completion_payload_fingerprint(b)
    assert completion_payload_fingerprint(a) != completion_payload_fingerprint(c)


def test_canonical_json_is_sorted_compact_and_str_fallback() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert canonical_json({"when": datetime(2026, 1, 1, tzinfo=UTC)}) == '{"when":"2026-01-01 00:00:00+00:00"}'
    assert canonical_json({"s": "é"}) == '{"s":"é"}'


# --- table sanity ------------------------------------------------------------------------------------------------


def test_next_lease_treats_none_as_zero() -> None:
    assert next_lease(None) == 1
    assert next_lease(0) == 1
    assert next_lease(7) == 8


def test_find_transition_table_is_unique_and_complete() -> None:
    pairs = [(t.source, t.target) for t in ALLOWED_TRANSITIONS]
    assert len(pairs) == len(set(pairs))
    assert find_transition("pending", "in_progress") is not None
    assert find_transition(S.PENDING, S.SUCCEEDED) is None
    assert find_transition("bogus", "pending") is None
    for terminal in TERMINAL_STATES:
        assert all(t.source is not terminal for t in ALLOWED_TRANSITIONS)


def test_unknown_current_state_is_invalid_transition() -> None:
    decision = validate_transition(current_state="bogus", claimed_by=None, lease_generation=0, requested=S.IN_PROGRESS, actor=WORKER_A, actor_lease=None)

    assert decision.allowed is False
    assert decision.reason is TransitionReason.INVALID_TRANSITION


# --- P3: scope-digest and charter binding, both additive ------------------------------------------------------------


def _permit_check(**overrides: object) -> tuple[bool, str]:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    kwargs: dict[str, object] = {
        "permit_decision": "allow",
        "permit_expires_at": now + timedelta(minutes=10),
        "permit_user_id": "user-1",
        "permit_directive_id": "d-1",
        "permit_objective_hash": "obj-1",
        "directive_id": "d-1",
        "directive_user_id": "user-1",
        "directive_objective_hash": "obj-1",
        "started_at": None,
        "now": now,
        "phase": "claim",
    }
    kwargs.update(overrides)
    return permit_binding_ok(**kwargs)  # type: ignore[arg-type]


def test_scope_digest_is_optional_and_backwards_compatible() -> None:
    digest = permit_scope_digest(action_kind="edit", target_paths=["services/a.py"], command_preview=None)
    other = permit_scope_digest(action_kind="edit", target_paths=["services/b.py"], command_preview=None)

    # Every pre-P3 call form still passes: the new arguments all default.
    assert _permit_check() == (True, "ok")
    assert _permit_check(permit_scope_digest=digest) == (True, "ok")
    assert _permit_check(expected_scope_digest=digest) == (True, "ok")
    assert _permit_check(permit_scope_digest=digest, expected_scope_digest=digest) == (True, "ok")

    # Only a mismatched pair of non-None digests refuses.
    assert _permit_check(permit_scope_digest=digest, expected_scope_digest=other) == (False, "permit_scope_digest_mismatch")


def test_an_absent_scope_digest_under_enforcement_refuses_rather_than_skipping() -> None:
    """A NULL that silently skips the check is how a written-but-never-read column stays unread."""
    digest = permit_scope_digest(action_kind="edit", target_paths=["services/a.py"], command_preview=None)
    assert _permit_check(scope_digest_enforced=True, expected_scope_digest=digest) == (False, "permit_scope_digest_missing")
    assert _permit_check(scope_digest_enforced=True, permit_scope_digest=digest) == (False, "permit_scope_digest_missing")
    assert _permit_check(scope_digest_enforced=True) == (False, "permit_scope_digest_missing")
    assert _permit_check(scope_digest_enforced=True, permit_scope_digest=digest, expected_scope_digest=digest) == (True, "ok")


def test_a_permit_from_a_superseded_charter_no_longer_authorises() -> None:
    assert _permit_check(permit_charter_id="c-1", active_charter_id="c-1") == (True, "ok")
    assert _permit_check(permit_charter_id="c-1", active_charter_id="c-2") == (False, "permit_charter_superseded")
    # Either side absent is a legacy permit and stays a wildcard, exactly like the other bindings.
    assert _permit_check(permit_charter_id="c-1") == (True, "ok")
    assert _permit_check(active_charter_id="c-2") == (True, "ok")


def test_the_new_refusals_are_ordered_after_the_allow_check() -> None:
    """A permit that was never granted refuses for THAT reason, not for a missing digest."""
    assert _permit_check(permit_decision="blocked", scope_digest_enforced=True) == (False, "permit_not_allowed")
