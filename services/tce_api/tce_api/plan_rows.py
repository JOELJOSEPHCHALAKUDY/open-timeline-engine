"""Goal-row authoring for plans and dreams, plus the ResolvedScope JSON boundary.

This module exists so the worker can write the same ``autonomy_goals`` rows the request
thread writes, without importing ``main.py``. Every function here is transactional-neutral:
**nothing in this module commits**. The caller owns the transaction, which is what closes the
orphaned-plan-rows hazard that ``_write_objective_plan``'s own ``db.commit()`` created.

``resolved_scope_to_json`` / ``resolved_scope_from_json`` live here rather than in
``tce_shared.task_state`` because they touch :class:`ResolvedScope`, and ``task_state.py`` must
not import ``tce_shared.scope`` (the worker loads it and must not pull the API model tree).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.dreams import DreamSeed
from tce_shared.events import AutonomyGoalSource, AutonomyGoalStatus, AutonomyRiskTier, GoalKind
from tce_shared.scope import (
    PROJECT_UNBOUND,
    SCOPE_POLICY_REVISION,
    ResolvedScope,
)
from tce_shared.takeover import sanitize_untrusted_objective
from tce_shared.task_state import PlanStepState, canonical_json

PLAN_DREAM_STEP_INDEX: int = -1
"""Marks a stored aspiration. Plan roots are 0 and steps are 1..N, so a dream that gets
pursued simply becomes a root; no separate table or wire field is needed.

Moved here from ``main.py`` so the request thread and the worker share one literal."""


_GOAL_INSERT = text(
    """
    INSERT INTO autonomy_goals(
        id, session_id, workspace_id, user_id, title, description,
        source, priority_score, risk_tier, confidence, reasoning,
        evidence_event_ids, goal_kind, affective_scores, selection_score,
        goal_signature, cache_hit, cache_source, status, created_at,
        updated_at, parent_goal_id, step_index,
        plan_contract_revision, attempt_count, blocked_reason, depends_on_json, mutating
    )
    VALUES(
        :id, :session_id, :workspace_id, :user_id, :title, :description,
        :source, :priority_score, :risk_tier, :confidence, :reasoning,
        :evidence_event_ids, :goal_kind, CAST(:affective_scores AS JSONB),
        :selection_score, :goal_signature, :cache_hit, :cache_source,
        :status, :created_at, :updated_at, :parent_goal_id, :step_index,
        :plan_contract_revision, :attempt_count, :blocked_reason,
        CAST(:depends_on_json AS JSONB), :mutating
    )
    """
)


def write_plan_rows(
    db: Session,
    *,
    scope: ResolvedScope,
    session_id: str,
    task_id: str,
    steps: Sequence[PlanStepState],
    producer: str,
    contract_revision: int,
    now: datetime,
    root_goal_id: str | None = None,
    objective_text: str = "",
) -> str:
    """Write the root goal (``step_index=0``) and one row per step (``1..N``).

    Returns the root goal id as a ``str``. **NEVER commits** — the caller owns the
    transaction.

    ``root_goal_id``, when supplied, is used instead of a fresh ``uuid4()``. It exists
    because the worker's write-back takes lock 1 (``task_states``) before lock 5
    (``autonomy_goals``), so the worker must know the root id before this runs in order to
    keep ``PLAN_APPROVED.payload["root_goal_id"]`` honest.

    ``objective_text`` is additive with a default: §S9.1 pins the root row's
    ``title``/``description`` to ``sanitize_untrusted_objective(objective_text, …)`` but the
    declared signature carries no objective, and ``steps`` never contains ``step_index=0``.
    Callers that omit it fall back to the first step's title, so the declared call shape
    stays valid. Lite's twin takes the same keyword with the same fallback — that is what
    makes the two backends write diffable root rows.
    """
    root_id = str(root_goal_id) if root_goal_id else str(uuid.uuid4())
    objective_title = _plan_objective_title(steps, task_id, objective_text)

    _insert_goal_row(
        db,
        goal_id=root_id,
        scope=scope,
        session_id=session_id,
        title=objective_title,
        description=objective_title,
        source=AutonomyGoalSource.USER_OBJECTIVE.value,
        priority_score=0.9,
        selection_score=0.9,
        risk_tier=AutonomyRiskTier.MEDIUM.value,
        confidence=0.85,
        reasoning="Ordered plan step derived from the user objective.",
        evidence_event_ids=[],
        cache_source="plan",
        status=AutonomyGoalStatus.SELECTED.value,
        parent_goal_id=None,
        step_index=0,
        contract_revision=contract_revision,
        attempt_count=0,
        blocked_reason="",
        depends_on=(),
        mutating=False,
        signature_seed=f"plan|{root_id}|0|{objective_title}",
        now=now,
    )
    for step in steps:
        title = str(step.title or "")
        _insert_goal_row(
            db,
            goal_id=str(uuid.uuid4()),
            scope=scope,
            session_id=session_id,
            title=title,
            description=str(step.description or title),
            source=AutonomyGoalSource.USER_OBJECTIVE.value,
            priority_score=0.9,
            selection_score=0.9,
            risk_tier=AutonomyRiskTier.MEDIUM.value,
            confidence=0.85,
            reasoning="Ordered plan step derived from the user objective.",
            evidence_event_ids=[],
            cache_source="plan",
            status=AutonomyGoalStatus.CANDIDATE.value,
            parent_goal_id=root_id,
            step_index=int(step.step_index),
            contract_revision=contract_revision,
            attempt_count=int(step.attempts),
            blocked_reason=str(step.blocked_reason or ""),
            depends_on=tuple(int(x) for x in step.depends_on),
            # Bound from the step, never from the column default: this is what
            # test_deterministic_plan_writes_non_mutating_goal_rows actually guards.
            mutating=bool(step.mutating),
            signature_seed=f"plan|{root_id}|{int(step.step_index)}|{title}",
            now=now,
        )
    _ = producer  # recorded on the task_states projection, not on the goal rows
    return root_id


def write_dream_rows(
    db: Session,
    *,
    scope: ResolvedScope,
    session_id: str,
    seeds: Sequence[DreamSeed],
    contract_revision: int,
    now: datetime,
) -> list[str]:
    """Discovery rows for dream seeds; returns the new goal ids. **NEVER commits.**

    A separate function rather than ``write_plan_rows`` with a mode flag, because the row
    shape genuinely differs: no root goal, ``step_index = PLAN_DREAM_STEP_INDEX``,
    ``parent_goal_id = None``, no dependencies and no attempts. ``mutating`` is always
    ``False`` — a discovery row authorises nothing. ``plan_contract_revision`` is never
    NULL, because the plan-step predicate ``AND plan_contract_revision = :contract_revision``
    can never match NULL.
    """
    goal_ids: list[str] = []
    for seed in seeds:
        goal_id = str(uuid.uuid4())
        title = str(seed.title or "")
        _insert_goal_row(
            db,
            goal_id=goal_id,
            scope=scope,
            session_id=session_id,
            title=title,
            description=str(seed.description or title),
            source=AutonomyGoalSource.OPEN_DISCOVERY.value,
            priority_score=float(seed.weight),
            selection_score=float(seed.weight),
            risk_tier=AutonomyRiskTier.LOW.value,
            confidence=float(seed.weight),
            reasoning=str(seed.rationale or "")[:400],
            evidence_event_ids=_uuid_list(seed.evidence_event_ids),
            cache_source="dream",
            status=AutonomyGoalStatus.CANDIDATE.value,
            parent_goal_id=None,
            step_index=PLAN_DREAM_STEP_INDEX,
            contract_revision=contract_revision,
            attempt_count=0,
            blocked_reason="",
            depends_on=(),
            mutating=False,
            signature_seed=f"dream|{session_id}|{title}",
            now=now,
        )
        goal_ids.append(goal_id)
    return goal_ids


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


def _uuid_list(values: Sequence[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for value in values:
        try:
            out.append(uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def _insert_goal_row(
    db: Session,
    *,
    goal_id: str,
    scope: ResolvedScope,
    session_id: str,
    title: str,
    description: str,
    source: str,
    priority_score: float,
    selection_score: float,
    risk_tier: str,
    confidence: float,
    reasoning: str,
    evidence_event_ids: list[uuid.UUID],
    cache_source: str,
    status: str,
    parent_goal_id: str | None,
    step_index: int,
    contract_revision: int,
    attempt_count: int,
    blocked_reason: str,
    depends_on: tuple[int, ...],
    mutating: bool,
    signature_seed: str,
    now: datetime,
) -> None:
    db.execute(
        _GOAL_INSERT,
        {
            "id": uuid.UUID(goal_id),
            "session_id": session_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.owner_id,
            "title": sanitize_untrusted_objective(title, max_len=140),
            "description": sanitize_untrusted_objective(description, max_len=240),
            "source": source,
            "priority_score": priority_score,
            "risk_tier": risk_tier,
            "confidence": confidence,
            "reasoning": reasoning,
            "evidence_event_ids": evidence_event_ids,
            "goal_kind": GoalKind.NORMAL.value,
            "affective_scores": canonical_json({}),
            "selection_score": selection_score,
            "goal_signature": hashlib.sha256(signature_seed.encode("utf-8")).hexdigest()[:24],
            "cache_hit": False,
            "cache_source": cache_source,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "parent_goal_id": uuid.UUID(parent_goal_id) if parent_goal_id else None,
            "step_index": step_index,
            "plan_contract_revision": contract_revision,
            "attempt_count": attempt_count,
            "blocked_reason": blocked_reason,
            "depends_on_json": canonical_json(list(depends_on)),
            "mutating": mutating,
        },
    )


_SCOPE_KEYS: tuple[str, ...] = (
    "workspace_id",
    "executor_id",
    "owner_id",
    "subject_user_id",
    "project_id",
    "project_binding",
    "task_id",
    "policy_revision",
    "owner_ids",
    "continuity_intent",
    "source_session_id",
    "legacy_session_scope",
    "retention_days",
)


def resolved_scope_to_json(scope: ResolvedScope) -> dict[str, Any]:
    """The thirteen pinned keys, in order. ``owner_ids`` is a sorted list — a frozenset is
    not JSON-safe, and a stable order keeps the stored blob diffable."""
    return {
        "workspace_id": str(scope.workspace_id),
        "executor_id": str(scope.executor_id),
        "owner_id": str(scope.owner_id),
        "subject_user_id": str(scope.subject_user_id),
        "project_id": scope.project_id,
        "project_binding": str(scope.project_binding),
        "task_id": scope.task_id,
        "policy_revision": str(scope.policy_revision),
        "owner_ids": sorted(scope.owner_ids),
        "continuity_intent": bool(scope.continuity_intent),
        "source_session_id": scope.source_session_id,
        "legacy_session_scope": bool(scope.legacy_session_scope),
        "retention_days": int(scope.retention_days),
    }


def resolved_scope_from_json(raw: Any) -> ResolvedScope:
    """Total: a missing or malformed key takes the dataclass default; ``owner_ids`` becomes a
    frozenset that always contains ``owner_id``. Never raises — a job row written by an older
    build must still be claimable."""
    data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    owner_id = _as_str(data.get("owner_id"), "")
    owner_ids_raw = data.get("owner_ids")
    owner_ids: set[str] = set()
    if isinstance(owner_ids_raw, (list, tuple, set, frozenset)):
        owner_ids = {str(item) for item in owner_ids_raw if str(item)}
    if owner_id:
        owner_ids.add(owner_id)
    return ResolvedScope(
        workspace_id=_as_str(data.get("workspace_id"), ""),
        executor_id=_as_str(data.get("executor_id"), ""),
        owner_id=owner_id,
        subject_user_id=_as_str(data.get("subject_user_id"), ""),
        project_id=_as_opt_str(data.get("project_id")),
        project_binding=_as_str(data.get("project_binding"), PROJECT_UNBOUND) or PROJECT_UNBOUND,
        task_id=_as_opt_str(data.get("task_id")),
        policy_revision=_as_str(data.get("policy_revision"), SCOPE_POLICY_REVISION)
        or SCOPE_POLICY_REVISION,
        owner_ids=frozenset(owner_ids),
        continuity_intent=bool(data.get("continuity_intent", False)),
        source_session_id=_as_opt_str(data.get("source_session_id")),
        legacy_session_scope=bool(data.get("legacy_session_scope", False)),
        retention_days=_as_int(data.get("retention_days"), 90),
    )


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value)
    return text_value or None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
