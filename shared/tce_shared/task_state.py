"""Durable task state: the pure, deterministic core both backends and the worker share.

A takeover session today keeps its truth in a JSON blob that every writer rewrites
wholesale. That makes two concurrent turns a coin flip and makes "what did this task
actually agree to?" unanswerable after the fact. This module replaces that with an
append-only event log plus a fold: the projection is a *derived* value, so two folds of
the same rows are byte-identical and a rolled-back deploy cannot make a task unreadable.

Everything here is pure. No I/O, no pydantic, no SQLAlchemy, no ``tce_shared.events`` and
no ``tce_shared.scope`` — the worker imports this module and must not pull the API model
tree in behind it. The only cross-module dependency is ``plan_decomposition``, which is
itself stdlib-only.

Three properties the rest of P2 leans on, stated once here:

* the fold is *total* — an unknown event kind is counted, never raised;
* :func:`derive_status` is a *total, fail-closed* rule table — ``DONE`` is reachable
  through exactly one rule, and that rule demands a verification naming both the current
  contract and the current plan;
* :func:`deterministic_plan` is read-only, always, with no branch that can make it
  otherwise.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .plan_decomposition import (
    BLOCKING_STEP_STATUS,
    MAX_PLAN_STEPS,
    TERMINAL_STEP_STATUSES,
    PlanStep,
    next_open_step,
    parse_plan_steps,
)

__all__ = [
    "CANCEL_REASON_OBJECTIVE_CHANGED",
    "CANCEL_REASON_STAND_DOWN",
    "CANCEL_REASON_TASK_CANCELLED",
    "CONTRACT_EXEMPT_KINDS",
    "MAX_TASK_STATE_EVENTS_PER_FOLD",
    "PINNED_KINDS",
    "PINNED_KIND_VALUES",
    "PLANNING_JOB_KIND_DECOMPOSE",
    "PLANNING_JOB_KIND_DREAM",
    "PLANNING_JOB_REVIVABLE_STATES",
    "PLANNING_JOB_STATES",
    "PLANNING_LOST_LEASE_REASON",
    "PLANNING_PRODUCER_DETERMINISTIC",
    "PLANNING_PRODUCER_MODEL",
    "PLANNING_STALE_REASON",
    "READ_ONLY_DIAGNOSIS_TEMPLATE",
    "TASK_STATE_MIME_TYPE",
    "TASK_STATE_POLICY_REVISION",
    "TASK_STATE_SCHEMA_VERSION",
    "TASK_STATE_URI_SCHEME",
    "UNKNOWN_CONTRACT_REVISION",
    "VERIFICATION_PASS_STATES",
    "VERIFICATION_STATES",
    "ApprovedConstraint",
    "ApprovedPlan",
    "FoldResult",
    "InvalidationPlan",
    "NextPermittedAction",
    "OpenDecision",
    "PlanCharter",
    "PlanState",
    "PlanStepState",
    "StatusInputs",
    "TaskStateEvent",
    "TaskStateEventKind",
    "TaskStatePreconditionFailed",
    "TaskStateProjection",
    "TaskStateRevisionConflict",
    "TaskStateWrite",
    "TaskStatus",
    "UnresolvedEffect",
    "VerificationRef",
    "approved_plan_from_json",
    "approved_plan_id",
    "approved_plan_to_json",
    "canonical_json",
    "charter_for_task",
    "charter_from_json",
    "charter_to_json",
    "constraints_from_json",
    "constraints_to_json",
    "decisions_from_json",
    "decisions_to_json",
    "derive_status",
    "deterministic_plan",
    "effects_from_json",
    "effects_to_json",
    "event_payload_from_json",
    "event_payload_to_json",
    "fold_task_state",
    "invalidation_for_objective_change",
    "objective_contract_revision",
    "objective_set_required",
    "plan_input_revision",
    "plan_steps_from_json",
    "plan_steps_from_model",
    "plan_steps_to_json",
    "planning_idempotency_key",
    "planning_result_is_stale",
    "prepare_write",
    "reconcile_constraint_events",
    "render_task_state_markdown",
    "root_status",
    "short_objective",
    "stamp_verification_provenance",
    "task_scope_digest",
    "task_state_summary_fields",
    "verification_from_json",
    "verification_is_current",
    "verification_to_json",
]


# --------------------------------------------------------------------------- vocabulary


class TaskStateEventKind(StrEnum):
    """The closed set of things that can happen to a task.

    A stored row carrying a kind this build does not know is counted in
    ``FoldResult.unknown_kinds`` and otherwise ignored, so a rollback cannot brick a task.
    """

    OBJECTIVE_SET = "objective_set"
    OBJECTIVE_CLEARED = "objective_cleared"
    CONSTRAINT_GRANTED = "constraint_granted"
    CONSTRAINT_REVOKED = "constraint_revoked"
    DECISION_OPENED = "decision_opened"
    DECISION_RESOLVED = "decision_resolved"
    PLAN_REQUESTED = "plan_requested"
    PLAN_APPROVED = "plan_approved"
    PLAN_INVALIDATED = "plan_invalidated"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_BLOCKED = "step_blocked"
    STEP_DROPPED = "step_dropped"
    EFFECT_RECORDED = "effect_recorded"
    EFFECT_RESOLVED = "effect_resolved"
    VERIFICATION_RECORDED = "verification_recorded"
    CANCELLATION_REQUESTED = "cancellation_requested"


class TaskStatus(StrEnum):
    AWAITING_OBJECTIVE = "awaiting_objective"
    PLANNING = "planning"
    ACTIVE = "active"
    BLOCKED = "blocked"
    AWAITING_DECISION = "awaiting_decision"
    AWAITING_VERIFICATION = "awaiting_verification"
    DONE = "done"
    CANCELLED = "cancelled"


class NextPermittedAction(StrEnum):
    AWAIT_OWNER_OBJECTIVE = "await_owner_objective"
    AWAIT_PLANNING = "await_planning"
    EXECUTE_STEP = "execute_step"
    AWAIT_DECISION = "await_decision"
    AWAIT_VERIFICATION = "await_verification"
    RESOLVE_EFFECTS = "resolve_effects"
    BLOCKED = "blocked"
    NONE = "none"


class PlanState(StrEnum):
    ABSENT = "absent"
    PENDING = "pending"
    APPROVED = "approved"
    INVALIDATED = "invalidated"


# --------------------------------------------------------------------------- constants

TASK_STATE_SCHEMA_VERSION: str = "v1"
TASK_STATE_POLICY_REVISION: str = "p2-2026-09"
TASK_STATE_URI_SCHEME: str = "tce://workspace/{workspace_id}/task/{task_id}/state.md"
TASK_STATE_MIME_TYPE: str = "text/markdown; charset=utf-8"
PLANNING_JOB_KIND_DECOMPOSE: str = "plan_decomposition"
PLANNING_JOB_KIND_DREAM: str = "dream_synthesis"
PLANNING_PRODUCER_MODEL: str = "model"
PLANNING_PRODUCER_DETERMINISTIC: str = "deterministic"
PLANNING_STALE_REASON: str = "stale_input_revision"
PLANNING_LOST_LEASE_REASON: str = "lost_lease"
CANCEL_REASON_TASK_CANCELLED: str = "task_cancelled"
CANCEL_REASON_OBJECTIVE_CHANGED: str = "objective_revision_changed"
CANCEL_REASON_STAND_DOWN: str = "stand_down"
MAX_TASK_STATE_EVENTS_PER_FOLD: int = 2000
PLANNING_JOB_STATES: frozenset[str] = frozenset(
    {"pending", "leased", "succeeded", "failed", "discarded", "cancelled"}
)
PLANNING_JOB_REVIVABLE_STATES: frozenset[str] = frozenset({"cancelled", "failed"})
# The contract revision stamped on a verification whose directive left NO trace on the
# projection -- so the contract its work happened under cannot be established.  Every real
# projection revision is >= 0, so this value can never equal one and
# ``verification_is_current`` refuses it.  Fail-closed is the only safe reading: a
# verification we cannot tie to a contract is not evidence FOR a contract.
UNKNOWN_CONTRACT_REVISION: int = -1
VERIFICATION_PASS_STATES: frozenset[str] = frozenset({"passed"})
VERIFICATION_STATES: frozenset[str] = frozenset({"unverified", "passed", "failed", "skipped"})
_TASK_STATE_NAMESPACE: uuid.UUID = uuid.UUID("8f2c0b6e-9d41-4a2f-9b73-2a51f0c7c1d2")
READ_ONLY_DIAGNOSIS_TEMPLATE: tuple[tuple[str, str], ...] = (
    (
        "Read the current state",
        "Read the files and recent events that describe '{short}' without changing anything.",
    ),
    (
        "Report what is true",
        "Write down what exists today, what is missing, and what is uncertain about '{short}'.",
    ),
    (
        "Ask for the missing decision",
        "Name the one decision the owner must make before any change to '{short}' is safe.",
    ),
)
CONTRACT_EXEMPT_KINDS: frozenset[str] = frozenset(
    {
        TaskStateEventKind.OBJECTIVE_SET,
        TaskStateEventKind.OBJECTIVE_CLEARED,
        TaskStateEventKind.CANCELLATION_REQUESTED,
        TaskStateEventKind.EFFECT_RECORDED,
        TaskStateEventKind.EFFECT_RESOLVED,
        TaskStateEventKind.VERIFICATION_RECORDED,
    }
)
PINNED_KINDS: frozenset[str] = frozenset(
    {
        TaskStateEventKind.OBJECTIVE_SET,
        TaskStateEventKind.OBJECTIVE_CLEARED,
        TaskStateEventKind.CANCELLATION_REQUESTED,
    }
)
# A frozenset has no stable iteration order, and the two backends bind this list into SQL.
# The SQL itself is order-insensitive; the test fixtures are not.
PINNED_KIND_VALUES: tuple[str, ...] = tuple(sorted(str(kind) for kind in PINNED_KINDS))

_KNOWN_KINDS: frozenset[str] = frozenset(str(kind) for kind in TaskStateEventKind)
_STEP_TERMINAL: frozenset[str] = frozenset(str(value) for value in TERMINAL_STEP_STATUSES)


def canonical_json(value: Any) -> str:
    """Byte-stable JSON for digests and column binds.

    Deliberately redefined here rather than imported from ``execution_transitions``: that
    module imports ``.events``, which would pull pydantic into the worker. The body is
    identical, and a unit test asserts the two agree.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True, slots=True)
class TaskStateEvent:
    """One immutable row of ``task_state_events``."""

    seq: int
    kind: TaskStateEventKind | str
    contract_revision: int
    payload: Mapping[str, Any]
    occurred_at: datetime
    actor: str = ""
    source_event_id: str | None = None
    directive_id: str | None = None
    goal_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovedConstraint:
    """A permit or owner rule the task is allowed to rely on.

    ``scope_digest`` is the **permit** digest from
    ``execution_transitions.permit_scope_digest`` — a different concept from the planning
    digest, which is named :func:`task_scope_digest` so the two can never be confused.
    """

    constraint_id: str
    kind: str
    scope_digest: str
    contract_revision: int
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OpenDecision:
    decision_id: str
    family: str
    question_text: str
    alternatives: tuple[str, ...]
    opened_at: datetime
    contract_revision: int
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class PlanStepState:
    """One ordered plan step. ``mutating`` is DESCRIPTIVE ONLY: never an authorization input."""

    step_index: int
    goal_id: str | None
    title: str
    description: str
    status: str
    depends_on: tuple[int, ...] = ()
    attempts: int = 0
    mutating: bool = False
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class UnresolvedEffect:
    effect_id: str
    kind: str
    description: str
    opened_at: datetime
    directive_id: str | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationRef:
    """Evidence that some plan actually holds.

    ``contract_revision`` and ``plan_id`` are provenance: without them a passing
    verification recorded against a finished objective would drive a later, wholly
    unverified objective to ``DONE``.
    """

    verification_id: str
    directive_id: str | None
    state: str
    method: str
    recorded_at: datetime
    contract_revision: int
    plan_id: str | None
    evidence_event_ids: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True, slots=True)
class PlanCharter:
    """The only enforced planning limit.

    There is deliberately no ``allows_mutation`` and no ``allowed_path_prefixes``: nothing
    enforced them, and mutation authorization is not this module's job.
    """

    max_steps: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApprovedPlan:
    task_id: str
    plan_id: str
    contract_revision: int
    state: PlanState
    producer: str
    root_goal_id: str | None
    steps: tuple[PlanStepState, ...]
    approved_at: datetime | None = None

    def root_status(self) -> str:
        """Sugar over the free function of the same name; contains no logic of its own."""
        return root_status(self.steps)


@dataclass(frozen=True, slots=True)
class TaskStateProjection:
    task_id: str
    workspace_id: str
    owner_id: str
    session_id: str
    revision: int
    contract_revision: int
    objective_text: str
    objective_hash: str | None
    objective_set_at: datetime | None
    objective_set_seq: int
    status: TaskStatus
    next_permitted_action: NextPermittedAction
    constraints: tuple[ApprovedConstraint, ...]
    open_decisions: tuple[OpenDecision, ...]
    plan: ApprovedPlan | None
    unresolved_effects: tuple[UnresolvedEffect, ...]
    latest_verification: VerificationRef | None
    citations: tuple[str, ...]
    cancelled_at: datetime | None = None
    cancelled_seq: int = 0
    # The CANCEL EPOCH: the largest seq of any retained CANCELLATION_REQUESTED, or 0.
    # Never cleared — a later OBJECTIVE_SET clears cancelled_at/cancelled_seq (which drive
    # status) and leaves this at its high-water mark, so a post-cancel re-issue of the same
    # objective computes a fresh plan_input_revision instead of reviving the killed job.
    last_cancel_seq: int = 0
    cancel_reason: str = ""
    schema_version: str = TASK_STATE_SCHEMA_VERSION
    policy_revision: str = TASK_STATE_POLICY_REVISION


@dataclass(frozen=True, slots=True)
class FoldResult:
    projection: TaskStateProjection
    source_revision: str
    unknown_kinds: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class TaskStateWrite:
    expected_revision: int
    next_revision: int
    next_seq_start: int
    projection: TaskStateProjection
    source_revision: str
    events: tuple[TaskStateEvent, ...]


@dataclass(frozen=True, slots=True)
class InvalidationPlan:
    contract_revision: int
    invalidate_plan: bool
    revoke_constraint_ids: tuple[str, ...]
    supersede_decision_ids: tuple[str, ...]
    cancel_directive_ids: tuple[str, ...]
    discard_planning_job_ids: tuple[str, ...]
    expire_permits_for_session: str
    new_objective_hash: str | None
    reason: str = CANCEL_REASON_OBJECTIVE_CHANGED


# --------------------------------------------------------------------------- exceptions


class TaskStateRevisionConflict(RuntimeError):
    """CAS lost the race and the caller declined (or exhausted) a retry."""

    task_id: str
    expected_revision: int
    actual_revision: int

    def __init__(self, *, task_id: str, expected_revision: int, actual_revision: int) -> None:
        super().__init__(
            f"task_state revision conflict for {task_id}: "
            f"expected {expected_revision}, actual {actual_revision}"
        )
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class TaskStatePreconditionFailed(RuntimeError):
    """The retry re-read the row and revalidate() said the write is no longer valid.

    Nothing is applied.
    """

    task_id: str
    reason: str

    def __init__(self, *, task_id: str, reason: str) -> None:
        super().__init__(f"task_state precondition failed for {task_id}: {reason}")
        self.task_id = task_id
        self.reason = reason


# --------------------------------------------------------------------------- JSON codecs
#
# Every dataclass above crosses a JSON boundary in two backends and a worker. These are the
# only serialisers; nobody writes an ad-hoc dict(...). Every ``*_from_json`` is TOTAL: an
# unknown key is ignored, a missing key takes the default, a wrong-typed value falls back to
# the default, and none of them ever raises. A rolled-back deploy must not make a task
# unreadable — the same principle as ``unknown_kinds``.


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _dt_to_json(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    return _aware(value).astimezone(UTC).isoformat()


def _dt_from_json(raw: Any, default: datetime | None = None) -> datetime | None:
    if isinstance(raw, datetime):
        return _aware(raw)
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return default


def _text(raw: Any, default: str = "") -> str:
    if isinstance(raw, str):
        return raw
    if raw is None or isinstance(raw, (dict, list, tuple, set)):
        return default
    return str(raw)


def _opt_text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = _text(raw, "")
    return value or None


def _integer(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


def _flag(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    return default


def _text_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(_text(item) for item in raw if _text(item))


def _int_tuple(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(_integer(item) for item in raw if not isinstance(item, (dict, list, tuple)))


def _mappings(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def constraints_to_json(items: Sequence[ApprovedConstraint]) -> list[dict[str, Any]]:
    return [
        {
            "constraint_id": item.constraint_id,
            "kind": item.kind,
            "scope_digest": item.scope_digest,
            "contract_revision": item.contract_revision,
            "granted_at": _dt_to_json(item.granted_at),
            "expires_at": _dt_to_json(item.expires_at),
            "revoked_at": _dt_to_json(item.revoked_at),
            "reason": item.reason,
        }
        for item in items
    ]


def constraints_from_json(raw: Any) -> tuple[ApprovedConstraint, ...]:
    out: list[ApprovedConstraint] = []
    for item in _mappings(raw):
        constraint_id = _text(item.get("constraint_id"))
        if not constraint_id:
            continue
        out.append(
            ApprovedConstraint(
                constraint_id=constraint_id,
                kind=_text(item.get("kind"), "permit") or "permit",
                scope_digest=_text(item.get("scope_digest")),
                contract_revision=_integer(item.get("contract_revision")),
                granted_at=_dt_from_json(item.get("granted_at")) or _EPOCH,
                expires_at=_dt_from_json(item.get("expires_at")),
                revoked_at=_dt_from_json(item.get("revoked_at")),
                reason=_text(item.get("reason")),
            )
        )
    return tuple(out)


def decisions_to_json(items: Sequence[OpenDecision]) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": item.decision_id,
            "family": item.family,
            "question_text": item.question_text,
            "alternatives": list(item.alternatives),
            "opened_at": _dt_to_json(item.opened_at),
            "contract_revision": item.contract_revision,
            "superseded": item.superseded,
        }
        for item in items
    ]


def decisions_from_json(raw: Any) -> tuple[OpenDecision, ...]:
    out: list[OpenDecision] = []
    for item in _mappings(raw):
        decision_id = _text(item.get("decision_id"))
        if not decision_id:
            continue
        out.append(
            OpenDecision(
                decision_id=decision_id,
                family=_text(item.get("family")),
                question_text=_text(item.get("question_text")),
                alternatives=_text_tuple(item.get("alternatives")),
                opened_at=_dt_from_json(item.get("opened_at")) or _EPOCH,
                contract_revision=_integer(item.get("contract_revision")),
                superseded=_flag(item.get("superseded")),
            )
        )
    return tuple(out)


def plan_steps_to_json(items: Sequence[PlanStepState]) -> list[dict[str, Any]]:
    return [
        {
            "step_index": item.step_index,
            "goal_id": item.goal_id,
            "title": item.title,
            "description": item.description,
            "status": item.status,
            "depends_on": list(item.depends_on),
            "attempts": item.attempts,
            "mutating": item.mutating,
            "blocked_reason": item.blocked_reason,
        }
        for item in items
    ]


def plan_steps_from_json(raw: Any) -> tuple[PlanStepState, ...]:
    out: list[PlanStepState] = []
    for item in _mappings(raw):
        out.append(
            PlanStepState(
                step_index=_integer(item.get("step_index")),
                goal_id=_opt_text(item.get("goal_id")),
                title=_text(item.get("title")),
                description=_text(item.get("description")),
                status=_text(item.get("status"), "candidate") or "candidate",
                depends_on=_int_tuple(item.get("depends_on")),
                attempts=_integer(item.get("attempts")),
                mutating=_flag(item.get("mutating")),
                blocked_reason=_text(item.get("blocked_reason")),
            )
        )
    return tuple(out)


def approved_plan_to_json(plan: ApprovedPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "task_id": plan.task_id,
        "plan_id": plan.plan_id,
        "contract_revision": plan.contract_revision,
        "state": str(plan.state),
        "producer": plan.producer,
        "root_goal_id": plan.root_goal_id,
        "steps": plan_steps_to_json(plan.steps),
        "approved_at": _dt_to_json(plan.approved_at),
    }


def approved_plan_from_json(raw: Any) -> ApprovedPlan | None:
    if not isinstance(raw, Mapping):
        return None
    task_id = _text(raw.get("task_id"))
    plan_id = _text(raw.get("plan_id"))
    if not plan_id:
        return None
    try:
        state = PlanState(_text(raw.get("state"), "absent") or "absent")
    except ValueError:
        state = PlanState.ABSENT
    return ApprovedPlan(
        task_id=task_id,
        plan_id=plan_id,
        contract_revision=_integer(raw.get("contract_revision")),
        state=state,
        producer=_text(raw.get("producer")),
        root_goal_id=_opt_text(raw.get("root_goal_id")),
        steps=plan_steps_from_json(raw.get("steps")),
        approved_at=_dt_from_json(raw.get("approved_at")),
    )


def effects_to_json(items: Sequence[UnresolvedEffect]) -> list[dict[str, Any]]:
    return [
        {
            "effect_id": item.effect_id,
            "kind": item.kind,
            "description": item.description,
            "opened_at": _dt_to_json(item.opened_at),
            "directive_id": item.directive_id,
            "paths": list(item.paths),
        }
        for item in items
    ]


def effects_from_json(raw: Any) -> tuple[UnresolvedEffect, ...]:
    out: list[UnresolvedEffect] = []
    for item in _mappings(raw):
        effect_id = _text(item.get("effect_id"))
        if not effect_id:
            continue
        out.append(
            UnresolvedEffect(
                effect_id=effect_id,
                kind=_text(item.get("kind"), "directive") or "directive",
                description=_text(item.get("description")),
                opened_at=_dt_from_json(item.get("opened_at")) or _EPOCH,
                directive_id=_opt_text(item.get("directive_id")),
                paths=_text_tuple(item.get("paths")),
            )
        )
    return tuple(out)


def verification_to_json(ref: VerificationRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {
        "verification_id": ref.verification_id,
        "directive_id": ref.directive_id,
        "state": ref.state,
        "method": ref.method,
        "recorded_at": _dt_to_json(ref.recorded_at),
        "contract_revision": ref.contract_revision,
        "plan_id": ref.plan_id,
        "evidence_event_ids": list(ref.evidence_event_ids),
        "summary": ref.summary,
    }


def verification_from_json(raw: Any) -> VerificationRef | None:
    if not isinstance(raw, Mapping):
        return None
    verification_id = _text(raw.get("verification_id"))
    if not verification_id:
        return None
    return VerificationRef(
        verification_id=verification_id,
        directive_id=_opt_text(raw.get("directive_id")),
        state=_text(raw.get("state"), "unverified") or "unverified",
        method=_text(raw.get("method"), "none") or "none",
        recorded_at=_dt_from_json(raw.get("recorded_at")) or _EPOCH,
        contract_revision=_integer(raw.get("contract_revision")),
        plan_id=_opt_text(raw.get("plan_id")),
        evidence_event_ids=_text_tuple(raw.get("evidence_event_ids")),
        summary=_text(raw.get("summary")),
    )


def charter_to_json(charter: PlanCharter) -> dict[str, Any]:
    return {"max_steps": charter.max_steps, "reason": charter.reason}


def charter_from_json(raw: Any) -> PlanCharter:
    if not isinstance(raw, Mapping):
        return PlanCharter(max_steps=MAX_PLAN_STEPS)
    return PlanCharter(
        max_steps=_integer(raw.get("max_steps"), MAX_PLAN_STEPS),
        reason=_text(raw.get("reason")),
    )


def event_payload_to_json(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Identity plus JSON-safety: datetimes to ISO, tuples to lists, anything else via str()."""
    return {str(key): _json_safe(value) for key, value in payload.items()}


def event_payload_from_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
    return {}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return _dt_to_json(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- the fold


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    return payload if isinstance(payload, Mapping) else {}


def _payload_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(_json_safe(_payload_mapping(payload))).encode("utf-8")).hexdigest()[:12]


def _sort_key(event: TaskStateEvent) -> tuple[int, str, str]:
    return (int(event.seq), str(event.kind), canonical_json(_json_safe(_payload_mapping(event.payload))))


def _dedupe(events: Sequence[TaskStateEvent]) -> list[TaskStateEvent]:
    seen: set[tuple[int, str, str]] = set()
    out: list[TaskStateEvent] = []
    for event in events:
        key = (int(event.seq), str(event.kind), _payload_digest(event.payload))
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def root_status(steps: Sequence[PlanStepState]) -> str:
    """``absent`` | ``blocked`` | ``active`` | ``done``, in that order of precedence.

    Total, and never returns ``done`` for a plan whose steps were all dropped: a plan that
    finished nothing is stalled, not finished.
    """
    if not steps:
        return "absent"
    status_by_index = {int(step.step_index): str(step.status or "").strip().lower() for step in steps}
    if any(status == BLOCKING_STEP_STATUS for status in status_by_index.values()):
        return "blocked"
    for step in steps:
        status = str(step.status or "").strip().lower()
        if status in _STEP_TERMINAL:
            continue
        for dependency in step.depends_on:
            dependency_status = status_by_index.get(int(dependency))
            if dependency_status in {BLOCKING_STEP_STATUS, "dropped"}:
                return "blocked"
    if next_open_step([(int(step.step_index), str(step.status)) for step in steps]) is not None:
        return "active"
    statuses = list(status_by_index.values())
    if all(status in _STEP_TERMINAL for status in statuses) and any(status == "done" for status in statuses):
        return "done"
    return "blocked"


def fold_task_state(
    events: Sequence[TaskStateEvent],
    *,
    task_id: str,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    revision: int,
    now: datetime,
    max_events: int = MAX_TASK_STATE_EVENTS_PER_FOLD,
) -> FoldResult:
    """Replay ``events`` into a projection. Deterministic, total, and never raises."""
    ordered = _dedupe(sorted(events, key=_sort_key))
    truncated = False
    if len(ordered) > max_events:
        truncated = True
        # The constant 3, whether or not all three pinned kinds are present: the SQL window
        # cannot count them without a second round trip, and a formula both sides can
        # evaluate identically is the only way rebuild and stored row provably agree.
        tail_size = max(0, max_events - len(PINNED_KINDS))
        tail = list(ordered[-tail_size:]) if tail_size else []
        pinned: list[TaskStateEvent] = []
        for kind in PINNED_KIND_VALUES:
            latest = None
            for event in ordered:
                if str(event.kind) == kind:
                    latest = event
            if latest is not None:
                pinned.append(latest)
        ordered = _dedupe(sorted(pinned + tail, key=_sort_key))

    contract_revision = 0
    objective_text = ""
    objective_hash: str | None = None
    objective_set_at: datetime | None = None
    objective_set_seq = 0
    constraints: dict[str, ApprovedConstraint] = {}
    decisions: dict[str, OpenDecision] = {}
    plan: ApprovedPlan | None = None
    steps: dict[int, PlanStepState] = {}
    effects: dict[str, UnresolvedEffect] = {}
    latest_verification: VerificationRef | None = None
    citations: set[str] = set()
    cancelled_at: datetime | None = None
    cancelled_seq = 0
    last_cancel_seq = 0
    cancel_reason = ""
    unknown_kinds: list[str] = []

    def _cite(candidate: Any) -> None:
        value = _text(candidate)
        if not value:
            return
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            # A non-UUID citation would make TaskStateProjectionResponse.source_evidence_ids
            # raise on GET /state.md. Drop it here rather than widening the wire type.
            unknown_kinds.append("citation_not_uuid")
            return
        citations.add(value)

    for event in ordered:
        kind = str(event.kind)
        payload = _payload_mapping(event.payload)
        occurred_at = _aware(event.occurred_at)
        _cite(event.source_event_id)

        if kind not in _KNOWN_KINDS:
            unknown_kinds.append(kind)
            continue
        if kind not in CONTRACT_EXEMPT_KINDS and int(event.contract_revision) < contract_revision:
            # Superseded by a newer contract: it still cites, it no longer decides.
            continue

        if kind == TaskStateEventKind.OBJECTIVE_SET:
            new_hash = _opt_text(payload.get("objective_hash"))
            contract_revision = objective_contract_revision(contract_revision, objective_hash, new_hash)
            objective_text = _text(payload.get("objective_text"))
            objective_hash = new_hash
            objective_set_at = occurred_at
            objective_set_seq = int(event.seq)
            if cancelled_at is not None and (occurred_at, int(event.seq)) > (cancelled_at, cancelled_seq):
                cancelled_at = None
                cancelled_seq = 0
                cancel_reason = ""
        elif kind == TaskStateEventKind.OBJECTIVE_CLEARED:
            objective_text = ""
            objective_hash = None
        elif kind == TaskStateEventKind.CONSTRAINT_GRANTED:
            for constraint in constraints_from_json([payload]):
                constraints[constraint.constraint_id] = constraint
        elif kind == TaskStateEventKind.CONSTRAINT_REVOKED:
            constraint_id = _text(payload.get("constraint_id"))
            existing = constraints.get(constraint_id)
            if existing is not None:
                constraints[constraint_id] = replace(
                    existing,
                    revoked_at=_dt_from_json(payload.get("revoked_at")) or occurred_at,
                    reason=_text(payload.get("reason"), existing.reason) or existing.reason,
                )
        elif kind == TaskStateEventKind.DECISION_OPENED:
            for decision in decisions_from_json([payload]):
                decisions[decision.decision_id] = decision
        elif kind == TaskStateEventKind.DECISION_RESOLVED:
            decision_id = _text(payload.get("decision_id"))
            existing_decision = decisions.get(decision_id)
            if existing_decision is not None:
                decisions[decision_id] = replace(existing_decision, superseded=True)
        elif kind == TaskStateEventKind.PLAN_REQUESTED:
            # Provenance only. It must never downgrade a plan already approved under this
            # contract — dream synthesis emits PLAN_REQUESTED beside a live plan.
            if plan is None or plan.contract_revision != contract_revision:
                plan = ApprovedPlan(
                    task_id=task_id,
                    plan_id=_plan_id_for(task_id, contract_revision),
                    contract_revision=contract_revision,
                    state=PlanState.PENDING,
                    producer=_text(payload.get("producer")),
                    root_goal_id=None,
                    steps=(),
                )
                steps = {}
        elif kind == TaskStateEventKind.PLAN_APPROVED:
            plan_contract = int(event.contract_revision) or contract_revision
            approved_steps = plan_steps_from_json(payload.get("steps"))
            plan = ApprovedPlan(
                task_id=task_id,
                plan_id=_text(payload.get("plan_id")) or _plan_id_for(task_id, plan_contract),
                contract_revision=plan_contract,
                state=PlanState.APPROVED,
                producer=_text(payload.get("producer"), PLANNING_PRODUCER_DETERMINISTIC)
                or PLANNING_PRODUCER_DETERMINISTIC,
                root_goal_id=_opt_text(payload.get("root_goal_id")),
                steps=approved_steps,
                approved_at=occurred_at,
            )
            steps = {int(step.step_index): step for step in approved_steps}
        elif kind == TaskStateEventKind.PLAN_INVALIDATED:
            if plan is not None:
                plan = replace(plan, state=PlanState.INVALIDATED)
        elif kind in {
            TaskStateEventKind.STEP_STARTED,
            TaskStateEventKind.STEP_COMPLETED,
            TaskStateEventKind.STEP_BLOCKED,
            TaskStateEventKind.STEP_DROPPED,
        }:
            index = _integer(payload.get("step_index"), -1)
            step = steps.get(index)
            if step is None:
                unknown_kinds.append(f"{kind}:unknown_step")
                continue
            if kind == TaskStateEventKind.STEP_STARTED:
                steps[index] = replace(step, status="executing", attempts=step.attempts + 1, blocked_reason="")
            elif kind == TaskStateEventKind.STEP_COMPLETED:
                steps[index] = replace(step, status="done", blocked_reason="")
            elif kind == TaskStateEventKind.STEP_BLOCKED:
                steps[index] = replace(
                    step,
                    status=BLOCKING_STEP_STATUS,
                    blocked_reason=_text(payload.get("blocked_reason")),
                )
            elif str(step.status or "").strip().lower() == BLOCKING_STEP_STATUS:
                # Dropping a blocked step would launder a stall into a completion. Only a
                # restart, a completion or a plan invalidation clears a block.
                unknown_kinds.append("step_dropped:blocked")
            else:
                steps[index] = replace(step, status="dropped")
        elif kind == TaskStateEventKind.EFFECT_RECORDED:
            for effect in effects_from_json([payload]):
                effects[effect.effect_id] = effect
        elif kind == TaskStateEventKind.EFFECT_RESOLVED:
            effects.pop(_text(payload.get("effect_id")), None)
        elif kind == TaskStateEventKind.VERIFICATION_RECORDED:
            ref = verification_from_json(payload)
            if ref is not None:
                latest_verification = ref
                for candidate in ref.evidence_event_ids:
                    _cite(candidate)
        elif kind == TaskStateEventKind.CANCELLATION_REQUESTED:
            cancelled_at = occurred_at
            cancelled_seq = int(event.seq)
            last_cancel_seq = max(last_cancel_seq, int(event.seq))
            cancel_reason = _text(payload.get("reason"), CANCEL_REASON_TASK_CANCELLED) or CANCEL_REASON_TASK_CANCELLED

    if plan is not None:
        plan = replace(plan, steps=tuple(steps[index] for index in sorted(steps)))

    ordered_constraints = tuple(constraints[key] for key in sorted(constraints))
    ordered_decisions = tuple(decisions[key] for key in sorted(decisions))
    ordered_effects = tuple(effects[key] for key in sorted(effects))

    status, next_action = derive_status(
        StatusInputs(
            cancelled_at=cancelled_at,
            cancelled_seq=cancelled_seq,
            objective_set_at=objective_set_at,
            objective_set_seq=objective_set_seq,
            objective_text=objective_text,
            contract_revision=contract_revision,
            open_decisions=ordered_decisions,
            plan=plan,
            unresolved_effects=ordered_effects,
            latest_verification=latest_verification,
        )
    )

    projection = TaskStateProjection(
        task_id=task_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        revision=int(revision),
        contract_revision=contract_revision,
        objective_text=objective_text,
        objective_hash=objective_hash,
        objective_set_at=objective_set_at,
        objective_set_seq=objective_set_seq,
        status=status,
        next_permitted_action=next_action,
        constraints=ordered_constraints,
        open_decisions=ordered_decisions,
        plan=plan,
        unresolved_effects=ordered_effects,
        latest_verification=latest_verification,
        citations=tuple(sorted(citations)),
        cancelled_at=cancelled_at,
        cancelled_seq=cancelled_seq,
        last_cancel_seq=last_cancel_seq,
        cancel_reason=cancel_reason,
    )
    _ = now  # the fold is time-free by design; `now` is kept for signature stability
    return FoldResult(
        projection=projection,
        source_revision=_source_revision(ordered),
        unknown_kinds=tuple(unknown_kinds),
        truncated=truncated,
    )


def _plan_id_for(task_id: str, contract_revision: int) -> str:
    return str(uuid.uuid5(_TASK_STATE_NAMESPACE, f"{task_id}|{int(contract_revision)}"))


def approved_plan_id(task_id: str, contract_revision: int) -> str:
    """The public name for §S3.6's deterministic plan id.

    Three writers mint this id — Full's inline planner, Lite's inline planner and the
    worker — and they must agree, so the namespace literal lives here and nowhere else.
    """
    return _plan_id_for(task_id, contract_revision)


def _source_revision(events: Sequence[TaskStateEvent]) -> str:
    if not events:
        return "empty"
    joined = "|".join(
        f"{int(event.seq)}:{event.kind}:{_payload_digest(event.payload)}" for event in events
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- status table


@dataclass(frozen=True, slots=True)
class StatusInputs:
    """The typed shape :func:`derive_status` folds over."""

    cancelled_at: datetime | None
    cancelled_seq: int
    objective_set_at: datetime | None
    objective_set_seq: int
    objective_text: str
    contract_revision: int
    open_decisions: tuple[OpenDecision, ...]
    plan: ApprovedPlan | None
    unresolved_effects: tuple[UnresolvedEffect, ...]
    latest_verification: VerificationRef | None


def verification_is_current(parts: StatusInputs) -> bool:
    """A VerificationRef counts only against the CURRENT contract and the CURRENT plan.

    Anything older stays visible as history, never as evidence: without this, a passing
    verification from a finished objective drives the next, wholly unverified objective to
    ``DONE``.
    """
    ref = parts.latest_verification
    if ref is None:
        return False
    if ref.contract_revision != parts.contract_revision:
        return False
    if ref.plan_id is None:
        return False
    plan = parts.plan
    return plan is not None and ref.plan_id == plan.plan_id


def stamp_verification_provenance(
    verification: dict[str, Any],
    *,
    projection: TaskStateProjection,
    directive_id: str | None,
    work_contract_revision: int | None = None,
) -> dict[str, Any]:
    """S5 provenance, stamped in ONE place for every writer of a verification, in both backends.

    ``verification_is_current`` -- R9's first positive -- reads exactly the two fields this
    function writes, so what they are read FROM decides whether the predicate can ever fail.

    **The stamp is a claim about what the evidence is evidence OF, and it must not come from the
    clock.**  Reading ``projection.contract_revision`` at grading time makes both sides of the
    comparison the same value read from the same row at the same instant: the ref then asserts
    "I am about whatever the contract is now", whichever contract that happens to be.  A
    verification still in flight when the owner restates the objective would be re-badged as
    evidence for the NEW contract, and R9 would release a wholly unverified objective to ``DONE``
    on the strength of the previous one's checks -- the same defect as accepting an answer that
    predates its question.

    So ``work_contract_revision`` is the contract the WORK happened under, supplied by the caller
    from the directive's own recorded history rather than from the projection.  Passing ``None``
    keeps the legacy reading -- the projection's current revision -- and is correct only for a
    writer whose verification is being recorded AS the work ends (the execution report, whose
    state is pinned to ``'unverified'`` and can never satisfy R9 anyway).

    ``plan_id`` follows from the revision, because §S3.6's plan id is
    ``uuid5(task_id | contract_revision)`` and nothing else.  When the work's revision is the
    current one the live plan's id is used verbatim (``None`` when there is no plan, which R9
    already refuses); when it is older, the deterministic id for THAT revision is stamped, so the
    ref stays readable as history and still cannot match the live plan.
    """
    current = int(projection.contract_revision)
    revision = current if work_contract_revision is None else int(work_contract_revision)
    verification["contract_revision"] = revision
    if revision == current:
        verification["plan_id"] = projection.plan.plan_id if projection.plan is not None else None
    elif revision >= 0:
        verification["plan_id"] = approved_plan_id(projection.task_id, revision)
    else:
        verification["plan_id"] = None
    verification["directive_id"] = directive_id
    return verification


def derive_status(parts: StatusInputs) -> tuple[TaskStatus, NextPermittedAction]:
    """The total, fail-closed rule table. First match wins; R10 has no predicate."""
    # R1 — a cancel that is newer than the objective is terminal.
    if parts.cancelled_at is not None and (parts.cancelled_at, parts.cancelled_seq) > (
        parts.objective_set_at or datetime.min.replace(tzinfo=UTC),
        parts.objective_set_seq,
    ):
        return TaskStatus.CANCELLED, NextPermittedAction.NONE
    # R2
    if not parts.objective_text.strip():
        return TaskStatus.AWAITING_OBJECTIVE, NextPermittedAction.AWAIT_OWNER_OBJECTIVE
    # R3
    if any(not decision.superseded for decision in parts.open_decisions):
        return TaskStatus.AWAITING_DECISION, NextPermittedAction.AWAIT_DECISION
    # R4 — catches every `plan is None` case, so R5-R9 dereference a proven-live plan.
    plan = parts.plan
    if (
        plan is None
        or plan.state in (PlanState.ABSENT, PlanState.PENDING, PlanState.INVALIDATED)
        or plan.contract_revision != parts.contract_revision
    ):
        return TaskStatus.PLANNING, NextPermittedAction.AWAIT_PLANNING
    root = root_status(plan.steps)
    # R5
    if root == "blocked":
        return TaskStatus.BLOCKED, NextPermittedAction.BLOCKED
    # R6
    if root == "active":
        return TaskStatus.ACTIVE, NextPermittedAction.EXECUTE_STEP
    # R7
    if root == "absent":
        return TaskStatus.PLANNING, NextPermittedAction.AWAIT_PLANNING
    # R8
    if parts.unresolved_effects:
        return TaskStatus.AWAITING_VERIFICATION, NextPermittedAction.RESOLVE_EFFECTS
    # R9 — the ONLY route to DONE, and it needs four positives.
    ref = parts.latest_verification
    if verification_is_current(parts) and ref is not None and ref.state in VERIFICATION_PASS_STATES:
        return TaskStatus.DONE, NextPermittedAction.NONE
    # R10
    return TaskStatus.AWAITING_VERIFICATION, NextPermittedAction.AWAIT_VERIFICATION


# ------------------------------------------------------- objective, invalidation, planning


def objective_contract_revision(current: int, current_hash: str | None, new_hash: str | None) -> int:
    """Bump only when the owner actually restated the objective."""
    if new_hash in (None, "", current_hash):
        return int(current)
    return int(current) + 1


def objective_set_required(
    projection: TaskStateProjection, *, new_objective_hash: str | None
) -> bool:
    """Must this turn append ``OBJECTIVE_SET``? Two yeses, and the second is the subtle one.

    The obvious yes is an objective **change**: a new hash bumps the contract revision, so the
    pointer has to move.

    The second is **revival**. A session that stands down and re-activates on the SAME
    objective computes an unchanged hash, so "did the objective change?" says no and no
    ``OBJECTIVE_SET`` is appended. But the projection's last retained
    ``CANCELLATION_REQUESTED`` is then newer than its last ``OBJECTIVE_SET`` forever, status
    rule R1 keeps returning ``CANCELLED``, and dispatch is blocked for the life of the session.
    ``CLAUDE.md`` mandates exactly that cycle — one stable ``session_id``,
    ``reset_takeover_state`` on stand-down, re-activate later — so a cancelled projection must
    accept a same-hash ``OBJECTIVE_SET``. The fold's time-ordered clearing then does the rest:
    a later ``OBJECTIVE_SET`` clears ``cancelled_at``/``cancelled_seq`` and the task is live
    again.

    What revival does **not** do is resurrect the cancelled plan run: ``last_cancel_seq`` is a
    high-water mark that is never cleared (S4), so ``plan_input_revision`` still differs and the
    revived turn mints a **fresh** planning job rather than reviving the one the owner killed.
    """
    if not (new_objective_hash or "").strip():
        return False
    if new_objective_hash != projection.objective_hash:
        return True
    return projection.status is TaskStatus.CANCELLED


def invalidation_for_objective_change(
    projection: TaskStateProjection,
    *,
    new_objective_hash: str | None,
    pending_directive_ids: Sequence[str],
    pending_planning_job_ids: Sequence[str],
) -> InvalidationPlan | None:
    """The pure value that closes out the previous contract. ``None`` when nothing changed.

    Computed BEFORE the write, so a CAS retry can replay both the events and the side
    effects after a rollback without recomputing anything.
    """
    next_revision = objective_contract_revision(
        projection.contract_revision, projection.objective_hash, new_objective_hash
    )
    if next_revision == projection.contract_revision:
        return None
    return InvalidationPlan(
        contract_revision=next_revision,
        invalidate_plan=True,
        revoke_constraint_ids=tuple(
            constraint.constraint_id
            for constraint in projection.constraints
            if constraint.contract_revision < next_revision and constraint.revoked_at is None
        ),
        supersede_decision_ids=tuple(
            decision.decision_id for decision in projection.open_decisions if not decision.superseded
        ),
        cancel_directive_ids=tuple(str(item) for item in pending_directive_ids),
        discard_planning_job_ids=tuple(str(item) for item in pending_planning_job_ids),
        expire_permits_for_session=projection.session_id,
        new_objective_hash=new_objective_hash,
    )


def task_scope_digest(
    *,
    workspace_id: str,
    executor_id: str,
    owner_ids: Sequence[str],
    subject_user_id: str,
    project_id: str | None,
    project_binding: str,
) -> str:
    """The PLANNING scope digest — distinct from ``permit_scope_digest``.

    Primitives only, deliberately: this module must not import ``tce_shared.scope``, and the
    worker rehydrates a ResolvedScope from ``planning_jobs.scope_json`` rather than from a
    request it does not have.
    """
    raw = canonical_json(
        {
            "workspace_id": workspace_id,
            "executor_id": executor_id,
            "owner_ids": sorted(set(str(item) for item in owner_ids)),
            "subject_user_id": subject_user_id,
            "project_id": project_id or "",
            "project_binding": project_binding,
        }
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def plan_input_revision(
    *,
    objective_hash: str | None,
    contract_revision: int,
    policy_revision: str,
    scope_digest: str,
    cancel_epoch: int,
) -> str:
    """Everything a planning job's result depends on, in one digest.

    ``cancel_epoch`` is ``projection.last_cancel_seq``. It is what stops a post-cancel turn
    on the *same* objective computing the same idempotency key and reviving the job the
    cancel just killed.
    """
    raw = "|".join(
        (
            objective_hash or "",
            str(int(contract_revision)),
            policy_revision,
            scope_digest,
            str(int(cancel_epoch)),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def planning_idempotency_key(
    *, job_kind: str, workspace_id: str, owner_id: str, task_id: str, input_revision: str
) -> str:
    raw = f"{job_kind}|{workspace_id}|{owner_id}|{task_id}|{input_revision}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def planning_result_is_stale(
    *,
    job_input_revision: str,
    job_contract_revision: int,
    current_input_revision: str,
    current_contract_revision: int,
    cancel_requested: bool,
) -> tuple[bool, str]:
    if cancel_requested:
        return True, "cancelled"
    if job_input_revision != current_input_revision:
        return True, PLANNING_STALE_REASON
    if int(job_contract_revision) != int(current_contract_revision):
        return True, PLANNING_STALE_REASON
    return False, "ok"


# --------------------------------------------------------------------------- plan producers


def short_objective(objective: str) -> str:
    """Byte-identical to ``plan_decomposition.fallback_plan_steps``' derivation.

    Two producers must not disagree on a value that ``source_revision`` and
    ``content_sha256`` hash over.
    """
    cleaned = " ".join((objective or "").split()).strip() or "the stated objective"
    return cleaned[:120]


def charter_for_task(*, max_steps: int, reason: str = "") -> PlanCharter:
    return PlanCharter(max_steps=max(1, min(MAX_PLAN_STEPS, int(max_steps))), reason=reason)


def deterministic_plan(objective: str, *, charter: PlanCharter) -> tuple[PlanStepState, ...]:
    """The read-only diagnosis plan. NEVER mutating.

    There is no branch, no charter flag and no caller-supplied way to make this produce a
    mutating step. A mutating plan can come only from an approved model plan.
    """
    short = short_objective(objective)
    out: list[PlanStepState] = []
    for index, (title, description) in enumerate(READ_ONLY_DIAGNOSIS_TEMPLATE[: charter.max_steps], start=1):
        out.append(
            PlanStepState(
                step_index=index,
                goal_id=None,
                title=title,
                description=description.format(short=short),
                status="candidate",
                depends_on=(index - 1,) if index > 1 else (),
                attempts=0,
                mutating=False,
                blocked_reason="",
            )
        )
    return tuple(out)


def plan_steps_from_model(payload: Any, *, charter: PlanCharter) -> tuple[PlanStepState, ...]:
    """Map a parsed model plan 1:1 onto plan steps.

    Returns ``()`` when the payload is unusable — the caller then FAILS the job. It never
    substitutes a deterministic plan into a ``producer='model'`` slot.
    """
    parsed: list[PlanStep] = parse_plan_steps(payload, max_steps=charter.max_steps)
    return tuple(
        PlanStepState(
            step_index=step.step_index,
            goal_id=None,
            title=step.title,
            description=step.description,
            status="candidate",
            depends_on=(step.step_index - 1,) if step.step_index > 1 else (),
            attempts=0,
            mutating=True,
            blocked_reason="",
        )
        for step in parsed
    )


def reconcile_constraint_events(
    *,
    projection: TaskStateProjection,
    permit_rows: Sequence[Mapping[str, Any]],
    now: datetime,
) -> list[TaskStateEvent]:
    """Diff the session's permit rows against the projection's constraints.

    PURE, and the ONLY producer of CONSTRAINT_GRANTED / CONSTRAINT_REVOKED: the permit
    grant path emits nothing, so a lost CAS race can never deny a permit. Takes rows rather
    than a session, so both backends call it with whatever their own SELECT returned.
    """
    known = {item.constraint_id: item for item in projection.constraints}
    out: list[TaskStateEvent] = []
    stamp = _aware(now)
    for row in permit_rows:
        constraint_id = _text(row.get("id") or row.get("constraint_id"))
        if not constraint_id:
            continue
        revoked_at = _dt_from_json(row.get("revoked_at"))
        existing = known.get(constraint_id)
        if existing is None:
            out.append(
                TaskStateEvent(
                    seq=0,
                    kind=TaskStateEventKind.CONSTRAINT_GRANTED,
                    contract_revision=projection.contract_revision,
                    payload={
                        "constraint_id": constraint_id,
                        "kind": _text(row.get("kind"), "permit") or "permit",
                        "scope_digest": _text(row.get("scope_digest")),
                        "contract_revision": projection.contract_revision,
                        "granted_at": _dt_to_json(
                            _dt_from_json(row.get("granted_at") or row.get("created_at")) or stamp
                        ),
                        "expires_at": _dt_to_json(_dt_from_json(row.get("expires_at"))),
                        "revoked_at": _dt_to_json(revoked_at),
                        "reason": _text(row.get("reason")),
                    },
                    occurred_at=stamp,
                    actor="system",
                )
            )
            continue
        if revoked_at is not None and existing.revoked_at is None:
            out.append(
                TaskStateEvent(
                    seq=0,
                    kind=TaskStateEventKind.CONSTRAINT_REVOKED,
                    contract_revision=projection.contract_revision,
                    payload={
                        "constraint_id": constraint_id,
                        "revoked_at": _dt_to_json(revoked_at),
                        "reason": _text(row.get("reason"), existing.reason) or existing.reason,
                    },
                    occurred_at=stamp,
                    actor="system",
                )
            )
    return out


def prepare_write(
    prior_events: Sequence[TaskStateEvent],
    new_events: Sequence[TaskStateEvent],
    *,
    task_id: str,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    expected_revision: int,
    highest_seq: int,
    now: datetime,
) -> TaskStateWrite:
    """Stamp sequence numbers and fold prior+new at ``expected_revision + 1``. Pure."""
    stamped: list[TaskStateEvent] = []
    seq = int(highest_seq)
    for event in new_events:
        seq += 1
        stamped.append(replace(event, seq=seq))
    folded = fold_task_state(
        list(prior_events) + stamped,
        task_id=task_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        revision=int(expected_revision) + 1,
        now=now,
    )
    return TaskStateWrite(
        expected_revision=int(expected_revision),
        next_revision=int(expected_revision) + 1,
        next_seq_start=int(highest_seq) + 1,
        projection=folded.projection,
        source_revision=folded.source_revision,
        events=tuple(stamped),
    )


# --------------------------------------------------------------------------- wire helpers


def task_state_summary_fields(
    projection: TaskStateProjection, *, source_revision: str
) -> dict[str, Any]:
    """Every ``TaskStateSummary`` field, derived once so the two backends cannot diverge.

    Returns a plain dict: this module stays pydantic-free, and the caller converts the two
    enum fields with ``to_lifecycle_status`` / ``to_next_permitted_action``.
    """
    plan = projection.plan
    open_step_index: int | None = None
    if plan is not None:
        open_step_index = next_open_step([(step.step_index, step.status) for step in plan.steps])
    return {
        "task_id": projection.task_id,
        "revision": projection.revision,
        "contract_revision": projection.contract_revision,
        "status": projection.status,
        "next_permitted_action": projection.next_permitted_action,
        "plan_state": str(plan.state) if plan is not None else str(PlanState.ABSENT),
        "plan_producer": plan.producer if plan is not None else None,
        "open_step_index": open_step_index,
        "open_decision_count": sum(1 for item in projection.open_decisions if not item.superseded),
        "unresolved_effect_count": len(projection.unresolved_effects),
        "source_revision": source_revision,
    }


_MARKDOWN_METADATA_ORDER: tuple[str, ...] = (
    "projection_id",
    "schema_version",
    "uri",
    "view",
    "format",
    "source_revision",
    "content_sha256",
    "generated_at",
    "trust_level",
    "sensitivity",
    "read_only",
    "projection_learning_eligible",
    "expires_at",
    "evidence_count",
    "truncated",
    "redaction_applied",
)


def _md_row(values: Sequence[str]) -> str:
    return "| " + " | ".join(values) + " |"


def render_task_state_markdown(
    projection: TaskStateProjection,
    *,
    source_revision: str,
    generated_at: datetime,
    max_steps: int = 24,
) -> dict[str, Any]:
    """The read-only Markdown view of a task, in the shape the projection emitter already uses.

    ``expires_at`` is None because a task-state projection has no TTL — it is regenerated per
    request. ``evidence_count`` is the citation count. ``redaction_applied`` is False because
    the renderer prints only the projection's own typed fields, never a free-text event
    payload, so there is nothing to redact.
    """
    plan = projection.plan
    steps = list(plan.steps) if plan is not None else []
    shown = steps[: max(0, int(max_steps))]
    truncated = len(steps) > len(shown)

    lines: list[str] = [f"# Task {projection.task_id}", ""]
    lines.append("## Objective")
    lines.append(projection.objective_text or "_none_")
    lines.append("")
    lines.append(f"- objective_hash: {projection.objective_hash or '_none_'}")
    lines.append(f"- contract_revision: {projection.contract_revision}")
    lines.append(f"- revision: {projection.revision}")
    lines.append("")
    lines.append("## Status")
    lines.append(f"- status: {projection.status}")
    lines.append(f"- next permitted action: {projection.next_permitted_action}")
    lines.append("")
    lines.append("## Approved constraints")
    if projection.constraints:
        lines.append(_md_row(("id", "kind", "scope digest", "expires")))
        lines.append(_md_row(("---", "---", "---", "---")))
        for constraint in projection.constraints:
            lines.append(
                _md_row(
                    (
                        constraint.constraint_id,
                        constraint.kind,
                        constraint.scope_digest or "-",
                        _dt_to_json(constraint.expires_at) or "-",
                    )
                )
            )
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("## Open decisions")
    open_decisions = [item for item in projection.open_decisions if not item.superseded]
    if open_decisions:
        for decision in open_decisions:
            lines.append(f"- {decision.question_text or decision.decision_id}")
            for alternative in decision.alternatives:
                lines.append(f"  - {alternative}")
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("## Plan")
    if shown:
        lines.append(_md_row(("#", "title", "status", "depends_on", "attempts", "mutating")))
        lines.append(_md_row(("---", "---", "---", "---", "---", "---")))
        for step in shown:
            lines.append(
                _md_row(
                    (
                        str(step.step_index),
                        step.title,
                        step.status,
                        ",".join(str(item) for item in step.depends_on) or "-",
                        str(step.attempts),
                        "yes" if step.mutating else "no",
                    )
                )
            )
        if truncated:
            lines.append(f"_… {len(steps) - len(shown)} more steps_")
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("## Unresolved effects")
    if projection.unresolved_effects:
        for effect in projection.unresolved_effects:
            lines.append(f"- {effect.kind}: {effect.description or effect.effect_id}")
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("## Latest verification")
    ref = projection.latest_verification
    if ref is not None:
        lines.append(f"- state: {ref.state}")
        lines.append(f"- method: {ref.method}")
        lines.append(f"- contract_revision: {ref.contract_revision}")
        lines.append(f"- plan_id: {ref.plan_id or '-'}")
        if ref.summary:
            lines.append(f"- summary: {ref.summary}")
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("## Citations")
    if projection.citations:
        lines.extend(f"- {item}" for item in projection.citations)
    else:
        lines.append("_none_")
    body = "\n".join(lines)

    projection_id = str(
        uuid.uuid5(
            _TASK_STATE_NAMESPACE,
            f"{projection.workspace_id}|{projection.task_id}|state|{projection.contract_revision}",
        )
    )
    uri = TASK_STATE_URI_SCHEME.format(
        workspace_id=urllib.parse.quote(projection.workspace_id, safe=""),
        task_id=urllib.parse.quote(projection.task_id, safe=""),
    )
    metadata: dict[str, Any] = {
        "projection_id": projection_id,
        "schema_version": projection.schema_version,
        "uri": uri,
        "view": "state",
        "format": "markdown",
        "source_revision": source_revision,
        "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "generated_at": _aware(generated_at).isoformat(),
        "trust_level": "projection",
        "sensitivity": 1,
        "read_only": True,
        "projection_learning_eligible": False,
        "expires_at": None,
        "evidence_count": len(projection.citations),
        "truncated": truncated,
        "redaction_applied": False,
    }
    source_evidence_ids = list(projection.citations)
    frontmatter = ["---"]
    frontmatter.extend(f"{key}: {json.dumps(metadata[key], ensure_ascii=True)}" for key in _MARKDOWN_METADATA_ORDER)
    frontmatter.append(f"task_id: {json.dumps(projection.task_id, ensure_ascii=True)}")
    frontmatter.append(f"revision: {json.dumps(projection.revision, ensure_ascii=True)}")
    frontmatter.append(f"contract_revision: {json.dumps(projection.contract_revision, ensure_ascii=True)}")
    frontmatter.append("source_evidence_ids:")
    frontmatter.extend(f"  - {json.dumps(item)}" for item in source_evidence_ids)
    frontmatter.extend(["---", "", body])

    return {
        **metadata,
        "source_evidence_ids": source_evidence_ids,
        "task_id": projection.task_id,
        "revision": projection.revision,
        "contract_revision": projection.contract_revision,
        "mime_type": TASK_STATE_MIME_TYPE,
        "content": "\n".join(frontmatter),
    }
