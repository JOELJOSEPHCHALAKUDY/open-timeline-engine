"""Plan goal rows for Lite (P2 §5.3, §0.7 S9/S12.5).

The Lite twin of `tce_api.plan_rows`. Same column list, same pinned values, so the two
backends' `autonomy_goals` tables stay diffable — only the driver differs (`sqlite3.Connection`
and `?` params in place of a SQLAlchemy `Session` and named binds).

Nothing here commits: the caller owns the transaction, which under §5.2 is the
`begin_immediate_cas` bracket. That is what closes the orphaned-plan-rows hazard.

`resolved_scope_to_json` / `resolved_scope_from_json` are re-declared here with bodies identical
to Full's because `tce_lite_api` may not import `tce_api`. They live in this module rather than in
`tce_shared.task_state` because they touch `ResolvedScope`, and `task_state.py` must not import
`tce_shared.scope`.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tce_shared.events import (
    AutonomyGoalSource,
    AutonomyGoalStatus,
    AutonomyRiskTier,
    GoalKind,
)
from tce_shared.scope import PROJECT_UNBOUND, SCOPE_POLICY_REVISION, ResolvedScope
from tce_shared.takeover import sanitize_untrusted_objective
from tce_shared.task_state import PlanStepState, canonical_json

_PLAN_REASONING = "Ordered plan step derived from the user objective."

_GOAL_COLUMNS = (
    "id",
    "session_id",
    "workspace_id",
    "user_id",
    "title",
    "description",
    "source",
    "priority_score",
    "risk_tier",
    "confidence",
    "reasoning",
    "evidence_event_ids",
    "goal_kind",
    "affective_scores",
    "selection_score",
    "goal_signature",
    "cache_hit",
    "cache_source",
    "status",
    "created_at",
    "updated_at",
    "parent_goal_id",
    "step_index",
    "plan_contract_revision",
    "attempt_count",
    "blocked_reason",
    "depends_on_json",
    "mutating",
)

_GOAL_INSERT = "INSERT INTO autonomy_goals ({cols}) VALUES ({marks})".format(
    cols=", ".join(_GOAL_COLUMNS),
    marks=", ".join("?" for _ in _GOAL_COLUMNS),
)


def _goal_signature(prefix: str, *parts: object) -> str:
    raw = "|".join([prefix, *(str(part) for part in parts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def write_plan_rows(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    session_id: str,
    task_id: str,
    steps: Sequence[PlanStepState],
    producer: str,
    contract_revision: int,
    now: datetime,
    objective_text: str = "",
) -> str:
    """Write the root goal (``step_index=0``) and one row per step (``step_index=1..N``).

    Returns the root goal id. NEVER commits — the caller owns the transaction.

    ``objective_text`` is an ADDITIVE keyword with a default (see the note in the module header
    of the P2 report): §0.7 S9.1 pins the root row's ``title``/``description`` to
    ``sanitize_untrusted_objective(objective_text, ...)`` but the declared signature carries no
    objective, and ``steps`` never contains ``step_index=0`` (``next_open_step`` would otherwise
    return the root as an open step). Callers that omit it fall back to the first step's title,
    which keeps the declared call shape valid.
    """
    del producer  # descriptive only; the producer is recorded on the task_states row
    stamp = now.isoformat()
    root_id = str(uuid.uuid4())
    objective_text = _plan_objective_title(steps, task_id, objective_text)

    conn.execute(
        _GOAL_INSERT,
        (
            root_id,
            session_id,
            scope.workspace_id,
            scope.owner_id,
            sanitize_untrusted_objective(objective_text, max_len=140),
            sanitize_untrusted_objective(objective_text, max_len=240),
            AutonomyGoalSource.USER_OBJECTIVE.value,
            0.9,
            AutonomyRiskTier.MEDIUM.value,
            0.85,
            _PLAN_REASONING,
            canonical_json([]),
            GoalKind.NORMAL.value,
            canonical_json({}),
            0.9,
            _goal_signature("plan", root_id, 0, objective_text),
            0,
            "plan",
            AutonomyGoalStatus.SELECTED.value,
            stamp,
            stamp,
            None,
            0,
            int(contract_revision),
            0,
            "",
            canonical_json([]),
            0,
        ),
    )
    for step in steps:
        if step.step_index == 0:
            continue
        conn.execute(
            _GOAL_INSERT,
            (
                str(uuid.uuid4()),
                session_id,
                scope.workspace_id,
                scope.owner_id,
                sanitize_untrusted_objective(step.title, max_len=140),
                sanitize_untrusted_objective(step.description, max_len=240),
                AutonomyGoalSource.USER_OBJECTIVE.value,
                0.9,
                AutonomyRiskTier.MEDIUM.value,
                0.85,
                _PLAN_REASONING,
                canonical_json([]),
                GoalKind.NORMAL.value,
                canonical_json({}),
                0.9,
                _goal_signature("plan", root_id, step.step_index, step.title),
                0,
                "plan",
                AutonomyGoalStatus.CANDIDATE.value,
                stamp,
                stamp,
                root_id,
                int(step.step_index),
                int(contract_revision),
                int(step.attempts),
                str(step.blocked_reason or ""),
                canonical_json(list(step.depends_on)),
                1 if step.mutating else 0,
            ),
        )
    return root_id


def resolved_scope_to_json(scope: ResolvedScope) -> dict[str, Any]:
    """Exactly thirteen keys, in this order. ``owner_ids`` is sorted (a frozenset is not JSON-safe)."""
    return {
        "workspace_id": scope.workspace_id,
        "executor_id": scope.executor_id,
        "owner_id": scope.owner_id,
        "subject_user_id": scope.subject_user_id,
        "project_id": scope.project_id,
        "project_binding": scope.project_binding,
        "task_id": scope.task_id,
        "policy_revision": scope.policy_revision,
        "owner_ids": sorted(scope.owner_ids),
        "continuity_intent": bool(scope.continuity_intent),
        "source_session_id": scope.source_session_id,
        "legacy_session_scope": bool(scope.legacy_session_scope),
        "retention_days": int(scope.retention_days),
    }


def resolved_scope_from_json(raw: Any) -> ResolvedScope:
    """Total: a missing or malformed key takes the dataclass default. Never raises.

    A job row written by an older build must still be claimable.
    """
    data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

    def _text(key: str, default: str = "") -> str:
        value = data.get(key, default)
        return str(value) if value is not None else default

    def _optional(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        text = str(value)
        return text or None

    owner_id = _text("owner_id")
    raw_owner_ids = data.get("owner_ids")
    owner_ids = {owner_id} if owner_id else set()
    if isinstance(raw_owner_ids, (list, tuple, set, frozenset)):
        owner_ids |= {str(item) for item in raw_owner_ids if str(item)}

    try:
        retention_days = int(data.get("retention_days", 90))
    except (TypeError, ValueError):
        retention_days = 90

    return ResolvedScope(
        workspace_id=_text("workspace_id"),
        executor_id=_text("executor_id"),
        owner_id=owner_id,
        subject_user_id=_text("subject_user_id"),
        project_id=_optional("project_id"),
        project_binding=_text("project_binding", PROJECT_UNBOUND) or PROJECT_UNBOUND,
        task_id=_optional("task_id"),
        policy_revision=_text("policy_revision", SCOPE_POLICY_REVISION) or SCOPE_POLICY_REVISION,
        owner_ids=frozenset(owner_ids),
        continuity_intent=bool(data.get("continuity_intent", False)),
        source_session_id=_optional("source_session_id"),
        legacy_session_scope=bool(data.get("legacy_session_scope", False)),
        retention_days=retention_days,
    )


__all__ = [
    "resolved_scope_from_json",
    "resolved_scope_to_json",
    "write_plan_rows",
]


def _plan_objective_title(
    steps: Sequence[PlanStepState], task_id: str, objective_text: str = ""
) -> str:
    """The root row's title, derived identically in Full and Lite.

    The objective itself when the caller supplied it (§S9.1); otherwise the ``step_index=0``
    step if one exists, then the first step that names anything, then the task id.
    """
    stated = str(objective_text or "").strip()
    if stated:
        return stated
    for step in steps:
        if step.step_index == 0 and str(step.title or "").strip():
            return str(step.title).strip()
    for step in steps:
        title = str(step.title or "").strip()
        if title:
            return title
    return str(task_id or "objective")
