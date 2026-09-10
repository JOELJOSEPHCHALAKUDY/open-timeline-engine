"""Ordered plan decomposition and progression.

A goal queue ranked by score cannot express "floor 2 comes after floor 1" — it
ranks the most recent thing highest, which is usually the step just finished. This
module supplies the missing piece: an objective becomes an ordered list of steps,
and progression is then a pure function of their statuses.

Everything here is deliberately free of I/O so both API backends can share it and
so the central guarantee — a completed step is never proposed again — is provable
in unit tests rather than only observable against a live stack.

The optional model call that produces better steps lives in the API layer. This
module only parses whatever comes back and always has a deterministic fallback,
so a plan exists even when no model does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Statuses that mean a step will not be worked again.
TERMINAL_STEP_STATUSES = frozenset({"done", "dropped"})
# A blocked step stalls the plan. It must not be skipped: building floor 3 on an
# unfinished floor 2 is worse than stopping and saying so.
BLOCKING_STEP_STATUS = "blocked"

MAX_PLAN_STEPS = 12

PLAN_DECOMPOSITION_PROMPT = """Break this objective into an ordered list of concrete steps.

Objective: {objective}

The person doing this is working alone at a keyboard. Every step must be something they
can start themselves today.

Rules:
- Each step must be independently completable and verifiable.
- Order them so each step only depends on earlier ones.
- Between 2 and 8 steps. Fewer is better.
- No step may restate the objective as a whole.
- Nothing requiring other people: no interviews, no stakeholders, no workshops,
  no committees, no sign-off.
- Plain wording, no Title Case, no consultant language.

Respond with JSON only:
{"steps": [{"id": "short-slug", "title": "...", "description": "...", "depends_on": []}]}
"""


@dataclass(frozen=True)
class PlanStep:
    """One ordered step. ``step_index`` is 1-based and contiguous."""

    step_index: int
    title: str
    description: str


def next_open_step(steps: Sequence[tuple[int, str]]) -> int | None:
    """Return the step index to work on next, or None if there is nothing to do.

    ``steps`` is ``(step_index, status)`` pairs in any order. This is the pure
    mirror of the progression query: the lowest-indexed step that is not terminal.
    Because indices are contiguous and ordered, "lowest non-terminal" already means
    "every predecessor is finished" — no dependency walk is needed at runtime.

    Returns None when the plan is complete, and also when the next step is blocked,
    which signals a stalled plan rather than permission to skip ahead.
    """
    open_steps = sorted(
        (index, str(status or "").strip().lower())
        for index, status in steps
        if str(status or "").strip().lower() not in TERMINAL_STEP_STATUSES
    )
    if not open_steps:
        return None
    index, status = open_steps[0]
    if status == BLOCKING_STEP_STATUS:
        return None
    return index


def _coerce_step_dicts(payload: Any) -> list[dict[str, Any]]:
    """Pull a list of step dicts out of whatever the model returned."""
    if not isinstance(payload, dict):
        return []
    # The gateway wraps replies it could not parse as {"raw": "..."}; that is prose,
    # not a plan, and must never be turned into steps.
    if "raw" in payload and "steps" not in payload:
        return []
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return []
    return [item for item in raw_steps if isinstance(item, dict)]


def _step_from_dict(item: dict[str, Any], index: int) -> PlanStep | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    description = str(item.get("description") or "").strip() or title
    return PlanStep(step_index=index, title=title[:180], description=description[:400])


def linearize_plan_steps(
    raw: list[dict[str, Any]], *, max_steps: int = MAX_PLAN_STEPS
) -> list[PlanStep]:
    """Flatten declared dependencies into a single ordered sequence.

    Dependencies are resolved once, here, so nothing at runtime has to walk a graph.
    A cycle or an unknown dependency degrades to the order the model gave rather
    than dropping steps — a slightly wrong order is recoverable, a missing step is not.
    """
    items = [item for item in raw if isinstance(item, dict) and str(item.get("title") or "").strip()]
    if not items:
        return []

    by_id: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(items):
        key = str(item.get("id") or "").strip() or f"__pos{position}"
        by_id.setdefault(key, item)

    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    remaining = list(by_id.items())
    # Kahn-style passes; bail out as soon as a pass places nothing (a cycle).
    while remaining:
        progressed = False
        still_waiting: list[tuple[str, dict[str, Any]]] = []
        for key, item in remaining:
            deps = item.get("depends_on")
            dep_keys = [str(d).strip() for d in deps] if isinstance(deps, list) else []
            unmet = [d for d in dep_keys if d in by_id and d not in placed]
            if unmet:
                still_waiting.append((key, item))
                continue
            ordered.append(item)
            placed.add(key)
            progressed = True
        remaining = still_waiting
        if not progressed:
            # Cycle or unresolvable: keep the rest in declared order.
            ordered.extend(item for _, item in remaining)
            break

    steps: list[PlanStep] = []
    for item in ordered[:max_steps]:
        step = _step_from_dict(item, len(steps) + 1)
        if step is not None:
            steps.append(step)
    return steps


def parse_plan_steps(payload: Any, *, max_steps: int = MAX_PLAN_STEPS) -> list[PlanStep]:
    """Turn a model response into ordered steps, or an empty list if it is unusable."""
    return linearize_plan_steps(_coerce_step_dicts(payload), max_steps=max_steps)


def fallback_plan_steps(objective: str) -> list[PlanStep]:
    """A deterministic plan, used whenever no model plan is available.

    No P2 consumer: the durable-task-state path routes through
    ``task_state.deterministic_plan`` / ``task_state.plan_steps_from_model`` instead. Kept
    because existing tests reference it.

    This exists so that decomposition has no failure mode: a takeover turn must
    never break because a model was slow, absent, or incoherent. The shape is the
    generic engineering loop — understand, do, verify — which is weak as a plan but
    still strictly better than a score-ranked pile of past events, because its steps
    are ordered and can be completed.
    """
    cleaned = " ".join((objective or "").split()).strip() or "the stated objective"
    short = cleaned[:120]
    template = (
        ("Establish scope and current state", f"Determine what '{short}' requires and what already exists."),
        ("Carry out the main work", f"Do the substantive work for '{short}'."),
        ("Verify the outcome", f"Check that '{short}' actually holds, with evidence."),
    )
    return [
        PlanStep(step_index=index, title=title, description=description)
        for index, (title, description) in enumerate(template, start=1)
    ]
