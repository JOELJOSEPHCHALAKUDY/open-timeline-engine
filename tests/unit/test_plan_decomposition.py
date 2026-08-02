"""Plan decomposition and progression — the pure logic behind ordered goals.

The load-bearing guarantee is that a completed step can never be proposed again.
``next_open_step`` is the pure mirror of the progression SQL, so that guarantee is
provable here without a database.
"""

from __future__ import annotations

from tce_shared.plan_decomposition import (
    PlanStep,
    fallback_plan_steps,
    linearize_plan_steps,
    next_open_step,
    parse_plan_steps,
)

DONE = "done"
DROPPED = "dropped"
CANDIDATE = "candidate"
BLOCKED = "blocked"
EXECUTING = "executing"


# --- next_open_step: the "never rebuild floor 1" guarantee ---


def test_next_open_step_returns_first_step_of_a_fresh_plan() -> None:
    assert next_open_step([(1, CANDIDATE), (2, CANDIDATE), (3, CANDIDATE)]) == 1


def test_next_open_step_advances_past_completed_steps() -> None:
    """Floor 1 built -> next goal is floor 2, never floor 1 again."""
    assert next_open_step([(1, DONE), (2, CANDIDATE), (3, CANDIDATE)]) == 2
    assert next_open_step([(1, DONE), (2, DONE), (3, CANDIDATE)]) == 3


def test_next_open_step_returns_none_when_plan_is_finished() -> None:
    """The house is built. There is no next floor."""
    assert next_open_step([(1, DONE), (2, DONE), (3, DONE)]) is None


def test_next_open_step_never_returns_a_terminal_step() -> None:
    """Exhaustive over orderings: a done/dropped index is never the answer."""
    terminal = {DONE, DROPPED}
    for done_count in range(4):
        steps = [(i, DONE if i <= done_count else CANDIDATE) for i in range(1, 5)]
        result = next_open_step(steps)
        if result is not None:
            assert dict(steps)[result] not in terminal


def test_next_open_step_skips_dropped_but_stops_on_blocked() -> None:
    """Dropped is terminal so it is skipped; blocked stalls the plan.

    Skipping a blocked step would silently build floor 3 on an unfinished floor 2.
    """
    assert next_open_step([(1, DROPPED), (2, CANDIDATE)]) == 2
    assert next_open_step([(1, DONE), (2, BLOCKED), (3, CANDIDATE)]) is None


def test_next_open_step_treats_in_flight_work_as_the_current_step() -> None:
    assert next_open_step([(1, DONE), (2, EXECUTING), (3, CANDIDATE)]) == 2


def test_next_open_step_is_order_independent() -> None:
    """Row order from SQL is not guaranteed; selection must sort by index."""
    assert next_open_step([(3, CANDIDATE), (1, DONE), (2, CANDIDATE)]) == 2


def test_next_open_step_handles_empty_plan() -> None:
    assert next_open_step([]) is None


# --- parsing model output ---


def test_parse_plan_steps_builds_contiguous_one_based_steps() -> None:
    steps = parse_plan_steps(
        {"steps": [
            {"title": "Lay the foundation"},
            {"title": "Build floor 1"},
            {"title": "Build floor 2"},
        ]}
    )
    assert [s.step_index for s in steps] == [1, 2, 3]
    assert steps[0].title == "Lay the foundation"


def test_parse_plan_steps_rejects_unstructured_model_output() -> None:
    """The gateway wraps unparseable replies as {"raw": "..."} — never a plan."""
    assert parse_plan_steps({"raw": "I think you should start with the foundation"}) == []
    assert parse_plan_steps(None) == []
    assert parse_plan_steps("not a dict") == []
    assert parse_plan_steps({"steps": []}) == []
    assert parse_plan_steps({"steps": "nope"}) == []


def test_parse_plan_steps_skips_entries_without_a_title() -> None:
    steps = parse_plan_steps({"steps": [{"title": "Real"}, {"description": "no title"}, {"title": ""}]})
    assert [s.title for s in steps] == ["Real"]
    assert [s.step_index for s in steps] == [1]


def test_parse_plan_steps_caps_runaway_plans() -> None:
    steps = parse_plan_steps({"steps": [{"title": f"step {i}"} for i in range(50)]}, max_steps=5)
    assert len(steps) == 5
    assert [s.step_index for s in steps] == [1, 2, 3, 4, 5]


# --- linearization ---


def test_linearize_plan_steps_orders_by_declared_dependencies() -> None:
    """A model may return steps out of order with depends_on; flatten to a line."""
    steps = linearize_plan_steps([
        {"id": "roof", "title": "Roof", "depends_on": ["floor2"]},
        {"id": "foundation", "title": "Foundation", "depends_on": []},
        {"id": "floor2", "title": "Floor 2", "depends_on": ["floor1"]},
        {"id": "floor1", "title": "Floor 1", "depends_on": ["foundation"]},
    ])
    assert [s.title for s in steps] == ["Foundation", "Floor 1", "Floor 2", "Roof"]
    assert [s.step_index for s in steps] == [1, 2, 3, 4]


def test_linearize_plan_steps_survives_a_dependency_cycle() -> None:
    """A cycle must not hang or drop everything — degrade to declared order."""
    steps = linearize_plan_steps([
        {"id": "a", "title": "A", "depends_on": ["b"]},
        {"id": "b", "title": "B", "depends_on": ["a"]},
    ])
    assert len(steps) == 2
    assert [s.step_index for s in steps] == [1, 2]


def test_linearize_plan_steps_ignores_unknown_dependencies() -> None:
    steps = linearize_plan_steps([
        {"id": "b", "title": "B", "depends_on": ["ghost"]},
        {"id": "a", "title": "A", "depends_on": []},
    ])
    assert len(steps) == 2


# --- deterministic fallback ---


def test_fallback_plan_steps_always_produces_a_usable_plan() -> None:
    """No model, bad model, dead model — a plan still exists. Never hard-fail a turn."""
    for objective in ("build a house", "x", "   ", "fix the retrieval regression"):
        steps = fallback_plan_steps(objective)
        assert steps, f"empty fallback for {objective!r}"
        assert [s.step_index for s in steps] == list(range(1, len(steps) + 1))
        assert all(isinstance(s, PlanStep) and s.title.strip() for s in steps)


def test_fallback_plan_steps_is_deterministic() -> None:
    assert fallback_plan_steps("build a house") == fallback_plan_steps("build a house")


def test_fallback_plan_carries_the_objective() -> None:
    steps = fallback_plan_steps("build a house")
    assert any("build a house" in s.title.lower() or "build a house" in s.description.lower()
               for s in steps)


def test_fallback_plan_walks_to_completion() -> None:
    """End to end on the pure layer: mark each step done, never revisit one."""
    steps = fallback_plan_steps("build a house")
    status = {s.step_index: CANDIDATE for s in steps}
    seen: list[int] = []
    while (nxt := next_open_step(list(status.items()))) is not None:
        assert nxt not in seen, "a completed step was proposed again"
        seen.append(nxt)
        status[nxt] = DONE
    assert seen == [s.step_index for s in steps]
