"""Unit tests for the pure task-state core.

The fold, the status table and the deterministic planner are the three places where a
silent wrong answer would be indistinguishable from a right one at runtime, so they are
tested exhaustively rather than by example: the status table is enumerated over every
permutation of its inputs, and the read-only planner is checked against 200 generated
objectives.
"""

from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tce_shared.events import (
    TaskLifecycleStatus,
    TaskNextPermittedAction,
    TaskStateProjectionResponse,
    to_lifecycle_status,
    to_next_permitted_action,
)
from tce_shared.execution_transitions import canonical_json as transitions_canonical_json
from tce_shared.plan_decomposition import MAX_PLAN_STEPS, fallback_plan_steps
from tce_shared.task_state import (
    CONTRACT_EXEMPT_KINDS,
    MAX_TASK_STATE_EVENTS_PER_FOLD,
    PINNED_KIND_VALUES,
    PINNED_KINDS,
    READ_ONLY_DIAGNOSIS_TEMPLATE,
    TASK_STATE_MIME_TYPE,
    TASK_STATE_POLICY_REVISION,
    VERIFICATION_STATES,
    ApprovedConstraint,
    ApprovedPlan,
    NextPermittedAction,
    OpenDecision,
    PlanCharter,
    PlanState,
    PlanStepState,
    StatusInputs,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatus,
    UnresolvedEffect,
    VerificationRef,
    approved_plan_from_json,
    approved_plan_to_json,
    canonical_json,
    charter_for_task,
    charter_from_json,
    charter_to_json,
    constraints_from_json,
    constraints_to_json,
    decisions_from_json,
    decisions_to_json,
    derive_status,
    deterministic_plan,
    effects_from_json,
    effects_to_json,
    event_payload_from_json,
    event_payload_to_json,
    fold_task_state,
    invalidation_for_objective_change,
    objective_contract_revision,
    objective_set_required,
    plan_input_revision,
    plan_steps_from_json,
    plan_steps_from_model,
    plan_steps_to_json,
    planning_idempotency_key,
    planning_result_is_stale,
    prepare_write,
    reconcile_constraint_events,
    render_task_state_markdown,
    root_status,
    short_objective,
    task_scope_digest,
    task_state_summary_fields,
    verification_from_json,
    verification_to_json,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
TASK = "session-1"
WS = "ws-1"
OWNER = "owner-1"


def _event(
    seq: int,
    kind: TaskStateEventKind | str,
    payload: dict[str, Any] | None = None,
    *,
    contract_revision: int = 1,
    at: datetime | None = None,
    source_event_id: str | None = None,
) -> TaskStateEvent:
    return TaskStateEvent(
        seq=seq,
        kind=kind,
        contract_revision=contract_revision,
        payload=payload or {},
        occurred_at=at or (NOW + timedelta(seconds=seq)),
        source_event_id=source_event_id,
    )


def _fold(events: list[TaskStateEvent], **kwargs: Any):
    return fold_task_state(
        events,
        task_id=TASK,
        workspace_id=WS,
        owner_id=OWNER,
        session_id=TASK,
        revision=1,
        now=NOW,
        **kwargs,
    )


def _objective(seq: int = 1, text: str = "ship the thing", digest: str = "h1") -> TaskStateEvent:
    return _event(
        seq,
        TaskStateEventKind.OBJECTIVE_SET,
        {"objective_text": text, "objective_hash": digest},
    )


def _plan_event(seq: int, steps: list[PlanStepState], *, contract_revision: int = 1) -> TaskStateEvent:
    return _event(
        seq,
        TaskStateEventKind.PLAN_APPROVED,
        {
            "producer": "deterministic",
            "root_goal_id": None,
            "steps": plan_steps_to_json(steps),
            "charter": charter_to_json(PlanCharter(max_steps=3)),
            "descriptive_sources": {},
        },
        contract_revision=contract_revision,
    )


def _steps(*statuses: str) -> list[PlanStepState]:
    return [
        PlanStepState(
            step_index=index,
            goal_id=None,
            title=f"step {index}",
            description="d",
            status=status,
            depends_on=(index - 1,) if index > 1 else (),
        )
        for index, status in enumerate(statuses, start=1)
    ]


# --------------------------------------------------------------------------- fold


def test_canonical_json_matches_execution_transitions() -> None:
    value = {"b": 1, "a": [1, 2], "c": "é"}
    assert canonical_json(value) == transitions_canonical_json(value)


def test_fold_is_order_independent() -> None:
    events = [_objective(1), _plan_event(2, _steps("candidate")), _event(3, TaskStateEventKind.STEP_STARTED, {"step_index": 1})]
    forward = _fold(list(events))
    backward = _fold(list(reversed(events)))
    assert forward.source_revision == backward.source_revision
    assert forward.projection == backward.projection


def test_duplicate_events_are_deduped() -> None:
    event = _objective(1)
    assert _fold([event]).source_revision == _fold([event, event]).source_revision


def test_unknown_kind_is_counted_not_raised() -> None:
    result = _fold([_objective(1), _event(2, "invented_in_a_later_build")])
    assert "invented_in_a_later_build" in result.unknown_kinds
    assert result.projection.objective_text == "ship the thing"


def test_truncation_pins_the_objective() -> None:
    """Fold rule 2 keeps identity when the tail scrolls past it, and stays order-stable."""
    events = [_objective(1)]
    events.extend(
        _event(seq, TaskStateEventKind.EFFECT_RECORDED, {"effect_id": f"e{seq}", "kind": "write", "description": "x"})
        for seq in range(2, 3001)
    )
    result = _fold(events)
    assert result.truncated is True
    assert result.projection.objective_text == "ship the thing"
    assert result.projection.contract_revision == 1
    shuffled = _fold(list(reversed(events)))
    assert shuffled.source_revision == result.source_revision


def test_truncation_tail_uses_the_constant_not_the_pinned_count() -> None:
    """The retained set size must not depend on how many pinned kinds happen to exist."""
    events = [_objective(1)]
    events.extend(
        _event(seq, TaskStateEventKind.EFFECT_RECORDED, {"effect_id": f"e{seq}", "kind": "write", "description": "x"})
        for seq in range(2, 60)
    )
    result = _fold(events, max_events=10)
    # 1 pinned OBJECTIVE_SET + (10 - 3) tail events, none of which is the objective.
    assert result.truncated is True
    assert result.projection.objective_text == "ship the thing"
    assert len(result.projection.unresolved_effects) == 10 - len(PINNED_KINDS)


def test_pinned_kind_values_is_a_stable_sorted_tuple() -> None:
    assert PINNED_KIND_VALUES == tuple(sorted(PINNED_KIND_VALUES))
    assert set(PINNED_KIND_VALUES) == {str(kind) for kind in PINNED_KINDS}


def test_contract_filter_exempts_cancellation() -> None:
    """A cancel stamped at an older contract must never be swallowed by the filter."""
    events = [
        _objective(1, digest="h1"),
        _objective(2, text="something else", digest="h2"),
        _event(3, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "stand_down"}, contract_revision=0),
    ]
    projection = _fold(events).projection
    assert projection.contract_revision == 2
    assert projection.status is TaskStatus.CANCELLED
    assert str(TaskStateEventKind.CANCELLATION_REQUESTED) in {str(k) for k in CONTRACT_EXEMPT_KINDS}


def test_older_contract_plan_is_ignored_for_accumulation() -> None:
    events = [
        _objective(1, digest="h1"),
        _plan_event(2, _steps("candidate")),
        _objective(3, text="new objective", digest="h2"),
    ]
    projection = _fold(events).projection
    assert projection.status is TaskStatus.PLANNING


def test_dropping_a_blocked_step_is_ignored() -> None:
    events = [
        _objective(1),
        _plan_event(2, _steps("candidate", "candidate")),
        _event(3, TaskStateEventKind.STEP_BLOCKED, {"step_index": 2, "blocked_reason": "needs owner"}),
        _event(4, TaskStateEventKind.STEP_DROPPED, {"step_index": 2}),
    ]
    result = _fold(events)
    assert "step_dropped:blocked" in result.unknown_kinds
    assert result.projection.plan is not None
    assert result.projection.plan.steps[1].status == "blocked"
    assert result.projection.status is TaskStatus.BLOCKED


def test_cancel_is_revived_by_a_later_objective_set() -> None:
    events = [
        _objective(1),
        _event(2, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "stand_down"}),
        _objective(3, text="back on", digest="h2"),
    ]
    projection = _fold(events).projection
    assert projection.cancelled_at is None
    assert projection.status is not TaskStatus.CANCELLED


def test_equal_timestamps_break_on_seq() -> None:
    same = NOW
    later_cancel = _fold(
        [
            _event(1, TaskStateEventKind.OBJECTIVE_SET, {"objective_text": "x", "objective_hash": "h"}, at=same),
            _event(2, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "r"}, at=same),
        ]
    ).projection
    assert later_cancel.status is TaskStatus.CANCELLED

    later_objective = _fold(
        [
            _event(1, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "r"}, at=same),
            _event(2, TaskStateEventKind.OBJECTIVE_SET, {"objective_text": "x", "objective_hash": "h"}, at=same),
        ]
    ).projection
    assert later_objective.status is not TaskStatus.CANCELLED


def test_last_cancel_seq_is_never_cleared() -> None:
    events = [
        _objective(1),
        _event(2, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "stand_down"}),
        _objective(3, text="back on", digest="h2"),
    ]
    projection = _fold(events).projection
    assert projection.cancelled_seq == 0
    assert projection.last_cancel_seq == 2

    # ...and it survives a truncating fold, because CANCELLATION_REQUESTED is pinned.
    padded = list(events)
    padded.extend(
        _event(seq, TaskStateEventKind.EFFECT_RECORDED, {"effect_id": f"e{seq}", "kind": "write", "description": "x"})
        for seq in range(4, 200)
    )
    truncated = _fold(padded, max_events=10).projection
    assert truncated.last_cancel_seq == 2


def test_non_uuid_citations_are_dropped_and_counted() -> None:
    good = str(uuid.uuid4())
    events = [
        _objective(1),
        _plan_event(2, _steps("done")),
        _event(
            3,
            TaskStateEventKind.VERIFICATION_RECORDED,
            {
                "verification_id": "v1",
                "state": "passed",
                "method": "test",
                "recorded_at": NOW.isoformat(),
                "contract_revision": 1,
                "plan_id": "p",
                "evidence_event_ids": [good, "not-a-uuid"],
            },
        ),
    ]
    result = _fold(events)
    assert good in result.projection.citations
    assert "not-a-uuid" not in result.projection.citations
    assert "citation_not_uuid" in result.unknown_kinds
    rendered = render_task_state_markdown(
        result.projection, source_revision=result.source_revision, generated_at=NOW
    )
    TaskStateProjectionResponse(**rendered)


# --------------------------------------------------------------------------- root_status


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), "absent"),
        (("candidate",), "active"),
        (("blocked",), "blocked"),
        (("done", "blocked"), "blocked"),
        (("done", "dropped"), "done"),
        (("dropped", "dropped"), "blocked"),
        (("done", "done"), "done"),
        (("done", "candidate"), "active"),
    ],
)
def test_root_status_cases(statuses: tuple[str, ...], expected: str) -> None:
    assert root_status(_steps(*statuses)) == expected


def test_root_status_blocks_on_a_blocked_dependency() -> None:
    steps = [
        PlanStepState(step_index=1, goal_id=None, title="a", description="a", status="dropped"),
        PlanStepState(step_index=2, goal_id=None, title="b", description="b", status="candidate", depends_on=(1,)),
    ]
    assert root_status(steps) == "blocked"


# --------------------------------------------------------------------------- status table


def _status_inputs(
    *,
    plan: ApprovedPlan | None,
    effects: tuple[UnresolvedEffect, ...] = (),
    verification: VerificationRef | None = None,
    contract_revision: int = 1,
    objective_text: str = "ship the thing",
    decisions: tuple[OpenDecision, ...] = (),
    cancelled_at: datetime | None = None,
) -> StatusInputs:
    return StatusInputs(
        cancelled_at=cancelled_at,
        cancelled_seq=9 if cancelled_at else 0,
        objective_set_at=NOW,
        objective_set_seq=1,
        objective_text=objective_text,
        contract_revision=contract_revision,
        open_decisions=decisions,
        plan=plan,
        unresolved_effects=effects,
        latest_verification=verification,
    )


def _plan(statuses: tuple[str, ...], *, contract_revision: int = 1, plan_id: str = "plan-A") -> ApprovedPlan:
    return ApprovedPlan(
        task_id=TASK,
        plan_id=plan_id,
        contract_revision=contract_revision,
        state=PlanState.APPROVED,
        producer="deterministic",
        root_goal_id=None,
        steps=tuple(_steps(*statuses)),
    )


def test_status_rule_table_is_total() -> None:
    """Every combination lands somewhere, and DONE is reachable only through R9."""
    root_shapes: dict[str, tuple[str, ...]] = {
        "absent": (),
        "blocked": ("blocked",),
        "active": ("candidate",),
        "done": ("done",),
    }
    effect = UnresolvedEffect(effect_id="e1", kind="directive", description="x", opened_at=NOW)
    verification_states = sorted(VERIFICATION_STATES) + [None, "future_unknown"]
    seen_done_shapes: set[tuple[Any, ...]] = set()

    for shape, statuses in root_shapes.items():
        for effects in ((), (effect,)):
            for state in verification_states:
                for ref_contract in (1, 2):
                    for ref_plan in ("plan-A", "plan-B", None):
                        ref = (
                            None
                            if state is None
                            else VerificationRef(
                                verification_id="v",
                                directive_id=None,
                                state=state,
                                method="test",
                                recorded_at=NOW,
                                contract_revision=ref_contract,
                                plan_id=ref_plan,
                            )
                        )
                        plan = None if shape == "absent" and not statuses else _plan(statuses)
                        parts = _status_inputs(plan=plan, effects=effects, verification=ref)
                        status, action = derive_status(parts)
                        assert isinstance(status, TaskStatus)
                        assert isinstance(action, NextPermittedAction)
                        if status is TaskStatus.DONE:
                            # R9's four positives, all of them.
                            assert shape == "done"
                            assert effects == ()
                            assert state == "passed"
                            assert ref_contract == 1
                            assert ref_plan == "plan-A"
                            seen_done_shapes.add((shape, state, ref_contract, ref_plan))
    assert seen_done_shapes == {("done", "passed", 1, "plan-A")}


def test_blocked_step_never_completes_root() -> None:
    """Exhaustive: no ordering, and no verification state, can launder a live block into DONE."""
    base = [_objective(1), _plan_event(2, _steps("candidate", "candidate"))]
    step_events: list[tuple[str, dict[str, Any], TaskStateEventKind]] = [
        ("completed", {"step_index": 1}, TaskStateEventKind.STEP_COMPLETED),
        ("blocked", {"step_index": 2, "blocked_reason": "needs owner"}, TaskStateEventKind.STEP_BLOCKED),
        ("dropped", {"step_index": 2}, TaskStateEventKind.STEP_DROPPED),
    ]
    for state in sorted(VERIFICATION_STATES):
        verification_payload: dict[str, Any] = {
            "verification_id": "v",
            "state": state,
            "method": "test",
            "recorded_at": NOW.isoformat(),
            "contract_revision": 1,
            "plan_id": None,
        }
        cases = [*step_events, ("verify", verification_payload, TaskStateEventKind.VERIFICATION_RECORDED)]
        for order in itertools.permutations(cases):
            events = list(base)
            for seq, (_label, payload, kind) in enumerate(order, start=3):
                events.append(_event(seq, kind, dict(payload)))
            result = _fold(events)
            plan = result.projection.plan
            assert plan is not None
            if any(step.status == "blocked" for step in plan.steps):
                assert result.projection.status is not TaskStatus.DONE
                assert plan.root_status() != "done"


def test_stale_verification_does_not_reach_done() -> None:
    plan = _plan(("done",), contract_revision=1, plan_id="plan-A")
    stale_contract = VerificationRef(
        verification_id="v",
        directive_id=None,
        state="passed",
        method="test",
        recorded_at=NOW,
        contract_revision=0,
        plan_id="plan-A",
    )
    status, action = derive_status(_status_inputs(plan=plan, verification=stale_contract))
    assert status is TaskStatus.AWAITING_VERIFICATION
    assert action is NextPermittedAction.AWAIT_VERIFICATION

    superseded_plan = replace(stale_contract, contract_revision=1, plan_id="plan-OLD")
    status, _ = derive_status(_status_inputs(plan=plan, verification=superseded_plan))
    assert status is TaskStatus.AWAITING_VERIFICATION


def test_status_specific_rules() -> None:
    assert derive_status(_status_inputs(plan=None, objective_text="  ")) == (
        TaskStatus.AWAITING_OBJECTIVE,
        NextPermittedAction.AWAIT_OWNER_OBJECTIVE,
    )
    decision = OpenDecision(
        decision_id="d1", family="f", question_text="q", alternatives=("a",), opened_at=NOW, contract_revision=1
    )
    assert derive_status(_status_inputs(plan=None, decisions=(decision,)))[0] is TaskStatus.AWAITING_DECISION
    assert derive_status(_status_inputs(plan=None, decisions=(replace(decision, superseded=True),)))[0] is (
        TaskStatus.PLANNING
    )
    assert derive_status(_status_inputs(plan=None, cancelled_at=NOW + timedelta(seconds=5)))[0] is (
        TaskStatus.CANCELLED
    )
    effect = UnresolvedEffect(effect_id="e", kind="directive", description="x", opened_at=NOW)
    assert derive_status(_status_inputs(plan=_plan(("done",)), effects=(effect,))) == (
        TaskStatus.AWAITING_VERIFICATION,
        NextPermittedAction.RESOLVE_EFFECTS,
    )
    assert derive_status(_status_inputs(plan=_plan(("candidate",)))) == (
        TaskStatus.ACTIVE,
        NextPermittedAction.EXECUTE_STEP,
    )
    assert derive_status(_status_inputs(plan=_plan((), contract_revision=1)))[0] is TaskStatus.PLANNING
    assert derive_status(_status_inputs(plan=_plan(("candidate",), contract_revision=99)))[0] is TaskStatus.PLANNING


# --------------------------------------------------------------------------- objective/planning


def test_objective_contract_revision_truth_table() -> None:
    assert objective_contract_revision(2, "h1", None) == 2
    assert objective_contract_revision(2, "h1", "") == 2
    assert objective_contract_revision(2, "h1", "h1") == 2
    assert objective_contract_revision(2, "h1", "h2") == 3
    assert objective_contract_revision(0, None, "h1") == 1


def test_objective_set_is_required_on_a_change_and_on_revival() -> None:
    """Ruling V1. The same-hash case is the one that matters: without it a stood-down session
    that re-activates on the SAME objective is CANCELLED forever and can never dispatch."""
    live = _fold([_objective(1)]).projection
    assert live.status is not TaskStatus.CANCELLED
    # A change: yes. An unchanged objective on a live task: no — nothing to say.
    assert objective_set_required(live, new_objective_hash="h2") is True
    assert objective_set_required(live, new_objective_hash="h1") is False
    # Nothing to set is never a reason to write.
    assert objective_set_required(live, new_objective_hash=None) is False
    assert objective_set_required(live, new_objective_hash="") is False
    assert objective_set_required(live, new_objective_hash="   ") is False

    cancelled = _fold(
        [_objective(1), _event(2, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "stand_down"})]
    ).projection
    assert cancelled.status is TaskStatus.CANCELLED
    # Revival: the identical objective must still be appended.
    assert objective_set_required(cancelled, new_objective_hash="h1") is True
    assert objective_set_required(cancelled, new_objective_hash="h2") is True
    # ...but an activation carrying no objective at all still writes nothing.
    assert objective_set_required(cancelled, new_objective_hash=None) is False

    # The first objective a task ever receives is a "change" from no hash at all.
    empty = _fold([]).projection
    assert objective_set_required(empty, new_objective_hash="h1") is True


def test_same_hash_objective_set_revives_a_cancelled_task() -> None:
    """The other half of ruling V1: the append the predicate asks for actually unblocks the
    task, and it does NOT clear the cancel epoch, so the revived turn plans afresh."""
    events = [
        _objective(1),
        _event(2, TaskStateEventKind.CANCELLATION_REQUESTED, {"reason": "stand_down"}),
    ]
    cancelled = _fold(events).projection
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.next_permitted_action is NextPermittedAction.NONE

    revived = _fold([*events, _objective(3)]).projection
    assert revived.status is not TaskStatus.CANCELLED
    assert revived.next_permitted_action is not NextPermittedAction.NONE
    assert revived.cancelled_at is None
    assert revived.objective_hash == "h1"
    assert revived.objective_text == "ship the thing"
    # Unchanged hash => unchanged contract; revival is not a new contract.
    assert revived.contract_revision == cancelled.contract_revision
    # ...and the cancel epoch survives, so plan_input_revision still differs and the revived
    # turn cannot pick the killed planning job back up (S4).
    assert revived.last_cancel_seq == 2
    assert plan_input_revision(
        objective_hash=revived.objective_hash,
        contract_revision=revived.contract_revision,
        policy_revision=TASK_STATE_POLICY_REVISION,
        scope_digest="d",
        cancel_epoch=revived.last_cancel_seq,
    ) != plan_input_revision(
        objective_hash=cancelled.objective_hash,
        contract_revision=cancelled.contract_revision,
        policy_revision=TASK_STATE_POLICY_REVISION,
        scope_digest="d",
        cancel_epoch=0,
    )


def test_invalidation_for_objective_change() -> None:
    projection = _fold([_objective(1)]).projection
    projection = replace(
        projection,
        constraints=(
            ApprovedConstraint(
                constraint_id="c1", kind="permit", scope_digest="d", contract_revision=0, granted_at=NOW
            ),
        ),
        open_decisions=(
            OpenDecision(
                decision_id="d1", family="f", question_text="q", alternatives=(), opened_at=NOW, contract_revision=1
            ),
        ),
    )
    assert (
        invalidation_for_objective_change(
            projection, new_objective_hash="h1", pending_directive_ids=[], pending_planning_job_ids=[]
        )
        is None
    )
    plan = invalidation_for_objective_change(
        projection, new_objective_hash="h2", pending_directive_ids=["d-1"], pending_planning_job_ids=["j-1"]
    )
    assert plan is not None
    assert plan.contract_revision == 2
    assert plan.invalidate_plan is True
    assert plan.revoke_constraint_ids == ("c1",)
    assert plan.supersede_decision_ids == ("d1",)
    assert plan.cancel_directive_ids == ("d-1",)
    assert plan.discard_planning_job_ids == ("j-1",)
    assert plan.expire_permits_for_session == TASK


def test_plan_input_revision_varies() -> None:
    base: dict[str, Any] = {
        "objective_hash": "h1",
        "contract_revision": 1,
        "policy_revision": TASK_STATE_POLICY_REVISION,
        "scope_digest": "sd",
        "cancel_epoch": 0,
    }
    baseline = plan_input_revision(**base)
    for field, value in (
        ("objective_hash", "h2"),
        ("contract_revision", 2),
        ("policy_revision", "other"),
        ("scope_digest", "sd2"),
        ("cancel_epoch", 7),
    ):
        assert plan_input_revision(**{**base, field: value}) != baseline, field


def test_planning_idempotency_key_is_stable() -> None:
    args: dict[str, Any] = {
        "job_kind": "plan_decomposition",
        "workspace_id": WS,
        "owner_id": OWNER,
        "task_id": TASK,
        "input_revision": "r1",
    }
    assert planning_idempotency_key(**args) == planning_idempotency_key(**args)
    assert planning_idempotency_key(**{**args, "input_revision": "r2"}) != planning_idempotency_key(**args)


def test_planning_result_is_stale() -> None:
    assert planning_result_is_stale(
        job_input_revision="a",
        job_contract_revision=1,
        current_input_revision="a",
        current_contract_revision=1,
        cancel_requested=True,
    ) == (True, "cancelled")
    assert planning_result_is_stale(
        job_input_revision="a",
        job_contract_revision=1,
        current_input_revision="b",
        current_contract_revision=1,
        cancel_requested=False,
    ) == (True, "stale_input_revision")
    assert planning_result_is_stale(
        job_input_revision="a",
        job_contract_revision=1,
        current_input_revision="a",
        current_contract_revision=1,
        cancel_requested=False,
    ) == (False, "ok")


def test_task_scope_digest_varies_and_is_order_insensitive() -> None:
    base: dict[str, Any] = {
        "workspace_id": WS,
        "executor_id": "exec",
        "owner_ids": ["b", "a"],
        "subject_user_id": "subject",
        "project_id": None,
        "project_binding": "unbound",
    }
    assert task_scope_digest(**base) == task_scope_digest(**{**base, "owner_ids": ["a", "b"]})
    assert task_scope_digest(**{**base, "project_id": "p1"}) != task_scope_digest(**base)


# --------------------------------------------------------------------------- planners


def test_deterministic_plan_is_always_read_only() -> None:
    charter = charter_for_task(max_steps=MAX_PLAN_STEPS)
    titles = {title for title, _ in READ_ONLY_DIAGNOSIS_TEMPLATE}
    for index in range(200):
        objective = f"objective {index} " + ("x" * (index % 40))
        steps = deterministic_plan(objective, charter=charter)
        assert steps
        for step in steps:
            assert step.mutating is False
            assert step.title in titles


def test_deterministic_plan_respects_the_charter() -> None:
    assert len(deterministic_plan("x", charter=charter_for_task(max_steps=1))) == 1
    assert len(deterministic_plan("x", charter=charter_for_task(max_steps=99))) == len(READ_ONLY_DIAGNOSIS_TEMPLATE)


def test_plan_charter_has_no_mutation_field() -> None:
    assert not hasattr(PlanCharter(max_steps=1), "allows_mutation")
    assert not hasattr(PlanCharter(max_steps=1), "allowed_path_prefixes")


def test_charter_for_task_clamps() -> None:
    assert charter_for_task(max_steps=0).max_steps == 1
    assert charter_for_task(max_steps=999).max_steps == MAX_PLAN_STEPS


def test_plan_steps_from_model_returns_empty_on_garbage() -> None:
    charter = charter_for_task(max_steps=4)
    garbage: list[Any] = [None, {}, {"raw": "prose"}, {"steps": "nope"}, []]
    for payload in garbage:
        assert plan_steps_from_model(payload, charter=charter) == ()


def test_plan_steps_from_model_marks_mutating() -> None:
    payload = {"steps": [{"id": "a", "title": "one"}, {"id": "b", "title": "two", "depends_on": ["a"]}]}
    steps = plan_steps_from_model(payload, charter=charter_for_task(max_steps=4))
    assert [step.step_index for step in steps] == [1, 2]
    assert all(step.mutating for step in steps)
    assert steps[1].depends_on == (1,)


def test_short_objective_matches_fallback_derivation() -> None:
    for objective in ("", "   ", "a b   c", "x" * 300):
        expected = fallback_plan_steps(objective)[0].description
        assert f"'{short_objective(objective)}'" in expected


def test_reconcile_constraint_events_is_idempotent() -> None:
    projection = _fold([_objective(1)]).projection
    rows = [{"id": "permit-1", "kind": "permit", "scope_digest": "sd", "granted_at": NOW.isoformat()}]
    first = reconcile_constraint_events(projection=projection, permit_rows=rows, now=NOW)
    assert [str(event.kind) for event in first] == [str(TaskStateEventKind.CONSTRAINT_GRANTED)]

    settled = replace(projection, constraints=constraints_from_json([dict(first[0].payload)]))
    assert reconcile_constraint_events(projection=settled, permit_rows=rows, now=NOW) == []

    revoked_rows = [{**rows[0], "revoked_at": NOW.isoformat()}]
    second = reconcile_constraint_events(projection=settled, permit_rows=revoked_rows, now=NOW)
    assert [str(event.kind) for event in second] == [str(TaskStateEventKind.CONSTRAINT_REVOKED)]


# --------------------------------------------------------------------------- prepare_write


def test_prepare_write_stamps_seq_and_revision() -> None:
    prior = [_objective(1)]
    new = [_event(0, TaskStateEventKind.EFFECT_RECORDED, {"effect_id": "e1", "kind": "write", "description": "x"})]
    write = prepare_write(
        prior,
        new,
        task_id=TASK,
        workspace_id=WS,
        owner_id=OWNER,
        session_id=TASK,
        expected_revision=4,
        highest_seq=7,
        now=NOW,
    )
    assert write.expected_revision == 4
    assert write.next_revision == 5
    assert write.next_seq_start == 8
    assert [event.seq for event in write.events] == [8]
    assert write.projection.revision == 5
    assert len(write.projection.unresolved_effects) == 1


# --------------------------------------------------------------------------- codecs


def test_json_codecs_round_trip_and_never_raise() -> None:
    constraint = ApprovedConstraint(
        constraint_id="c1",
        kind="permit",
        scope_digest="sd",
        contract_revision=1,
        granted_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        reason="because",
    )
    decision = OpenDecision(
        decision_id="d1", family="f", question_text="q", alternatives=("a", "b"), opened_at=NOW, contract_revision=1
    )
    step = PlanStepState(
        step_index=1, goal_id="g", title="t", description="d", status="candidate", depends_on=(), mutating=True
    )
    effect = UnresolvedEffect(effect_id="e1", kind="write", description="d", opened_at=NOW, paths=("a.py",))
    ref = VerificationRef(
        verification_id="v1",
        directive_id="d",
        state="passed",
        method="test",
        recorded_at=NOW,
        contract_revision=2,
        plan_id="p",
        evidence_event_ids=("x",),
        summary="s",
    )
    plan = ApprovedPlan(
        task_id=TASK,
        plan_id="p",
        contract_revision=2,
        state=PlanState.APPROVED,
        producer="model",
        root_goal_id="root",
        steps=(step,),
        approved_at=NOW,
    )
    charter = PlanCharter(max_steps=5, reason="r")

    assert constraints_from_json(constraints_to_json([constraint])) == (constraint,)
    assert decisions_from_json(decisions_to_json([decision])) == (decision,)
    assert plan_steps_from_json(plan_steps_to_json([step])) == (step,)
    assert effects_from_json(effects_to_json([effect])) == (effect,)
    assert verification_from_json(verification_to_json(ref)) == ref
    assert approved_plan_from_json(approved_plan_to_json(plan)) == plan
    assert charter_from_json(charter_to_json(charter)) == charter

    malformed: list[Any] = [None, [], {}, "nonsense", 7, {"unknown": "key"}]
    for bad in malformed:
        assert constraints_from_json(bad) == ()
        assert decisions_from_json(bad) == ()
        assert plan_steps_from_json(bad) == ()
        assert effects_from_json(bad) == ()
        assert verification_from_json(bad) is None
        assert approved_plan_from_json(bad) is None
        assert charter_from_json(bad).max_steps == MAX_PLAN_STEPS
        assert isinstance(event_payload_from_json(bad), dict)


def test_event_payload_to_json_is_json_safe() -> None:
    payload = event_payload_to_json({"when": NOW, "items": (1, 2), "obj": object()})
    json.dumps(payload)
    assert payload["items"] == [1, 2]
    assert isinstance(payload["when"], str)


# --------------------------------------------------------------------------- wire mirror


def test_enum_mirrors_do_not_drift() -> None:
    assert {member.value for member in TaskStatus} == {member.value for member in TaskLifecycleStatus}
    assert {member.value for member in NextPermittedAction} == {
        member.value for member in TaskNextPermittedAction
    }
    assert to_lifecycle_status("nonsense") is TaskLifecycleStatus.AWAITING_VERIFICATION
    assert to_next_permitted_action("nonsense") is TaskNextPermittedAction.NONE
    assert to_lifecycle_status(TaskStatus.DONE) is TaskLifecycleStatus.DONE
    assert to_next_permitted_action(NextPermittedAction.EXECUTE_STEP) is TaskNextPermittedAction.EXECUTE_STEP


def test_task_state_summary_fields() -> None:
    result = _fold([_objective(1), _plan_event(2, _steps("done", "candidate"))])
    fields = task_state_summary_fields(result.projection, source_revision=result.source_revision)
    assert fields["plan_state"] == "approved"
    assert fields["plan_producer"] == "deterministic"
    assert fields["open_step_index"] == 2
    assert fields["source_revision"] == result.source_revision

    empty = _fold([_objective(1)])
    empty_fields = task_state_summary_fields(empty.projection, source_revision="sr")
    assert empty_fields["plan_state"] == "absent"
    assert empty_fields["plan_producer"] is None
    assert empty_fields["open_step_index"] is None


# --------------------------------------------------------------------------- renderer


def test_render_task_state_markdown_shape() -> None:
    result = _fold([_objective(1), _plan_event(2, _steps("candidate", "candidate"))])
    rendered = render_task_state_markdown(
        result.projection, source_revision=result.source_revision, generated_at=NOW
    )
    assert set(rendered) == {
        "projection_id",
        "schema_version",
        "uri",
        "view",
        "format",
        "source_revision",
        "content_sha256",
        "generated_at",
        "source_evidence_ids",
        "trust_level",
        "sensitivity",
        "read_only",
        "projection_learning_eligible",
        "expires_at",
        "evidence_count",
        "truncated",
        "redaction_applied",
        "task_id",
        "revision",
        "contract_revision",
        "mime_type",
        "content",
    }
    assert rendered["mime_type"] == TASK_STATE_MIME_TYPE
    assert rendered["read_only"] is True
    assert rendered["projection_learning_eligible"] is False
    assert rendered["expires_at"] is None
    assert rendered["content"].startswith("---\n")
    body = rendered["content"].split("\n---\n\n", 1)[1]
    import hashlib

    assert rendered["content_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    TaskStateProjectionResponse(**rendered)


def test_render_truncates_the_plan_table() -> None:
    result = _fold([_objective(1), _plan_event(2, _steps(*(["candidate"] * 6)))])
    rendered = render_task_state_markdown(
        result.projection, source_revision="sr", generated_at=NOW, max_steps=2
    )
    assert rendered["truncated"] is True
    assert "4 more steps" in rendered["content"]


def test_render_quotes_uri_segments() -> None:
    result = _fold([_objective(1)])
    projection = replace(result.projection, workspace_id="ws/one", task_id="task one")
    rendered = render_task_state_markdown(projection, source_revision="sr", generated_at=NOW)
    assert "ws%2Fone" in rendered["uri"]
    assert "task%20one" in rendered["uri"]


def test_max_events_default_is_what_the_sql_binds() -> None:
    assert MAX_TASK_STATE_EVENTS_PER_FOLD == 2000
