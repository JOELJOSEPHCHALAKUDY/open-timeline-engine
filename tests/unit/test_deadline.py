"""Unit tests for the turn deadline, the retrieval ledger and the cache keys.

Every :class:`Deadline` test drives a fake monotonic clock rather than sleeping, so the
invariants are checked at exact instants instead of approximately.
"""

from __future__ import annotations

from typing import Any

from tce_shared.deadline import (
    ADVISOR_TIMEOUT_BUCKET_MS,
    ADVISOR_TURN_DEADLINE,
    RETRIEVAL_DEADLINE_POLICY_REVISION,
    Deadline,
    RetrievalLedger,
    advisor_timeout_bucket_ms,
    embedding_cache_key,
    embedding_cooldown_key,
    retrieval_cache_key,
    retrieval_child,
)
from tce_shared.goal_cache import cache_key as goal_cache_key
from tce_shared.task_state import TASK_STATE_POLICY_REVISION


class _Clock:
    """A monotonic clock the test advances by hand.

    Time is held in whole milliseconds and divided only on read. ``elapsed_ms`` truncates a
    float subtraction, so a reading can still land one millisecond low; assertions on an
    exact remainder allow for that rather than pretending the arithmetic is exact.
    """

    def __init__(self) -> None:
        self._ms = 1_000_000

    def __call__(self) -> float:
        return self._ms / 1000.0

    def advance_ms(self, ms: int) -> None:
        self._ms += int(ms)


def _turn(clock: _Clock, *, budget_ms: int = 3500, backend_budget_ms: int = 200) -> Deadline:
    return Deadline.start(budget_ms=budget_ms, backend_budget_ms=backend_budget_ms, monotonic=clock)


# --------------------------------------------------------------------------- Deadline


def test_child_never_outlives_parent() -> None:
    clock = _Clock()
    parent = _turn(clock, budget_ms=500)
    child = parent.child(label="retrieval", budget_ms=5000)
    for _ in range(12):
        assert child.remaining_ms() <= parent.remaining_ms()
        clock.advance_ms(50)
    assert child.remaining_ms() == 0
    assert child.expired() is True


def test_child_remaining_is_min_of_both() -> None:
    clock = _Clock()
    parent = _turn(clock, budget_ms=1000)
    clock.advance_ms(900)
    child = parent.child(label="retrieval", budget_ms=5000)
    assert 99 <= child.remaining_ms() <= 101
    clock.advance_ms(101)
    assert child.remaining_ms() == 0


def test_child_backend_budget_can_only_shrink() -> None:
    clock = _Clock()
    parent = _turn(clock, backend_budget_ms=60)
    assert parent.child(label="c", backend_budget_ms=5000).backend_budget_ms == 60
    assert parent.child(label="c", backend_budget_ms=10).backend_budget_ms == 10


def test_unbounded_slices_are_large() -> None:
    unbounded = Deadline.unbounded()
    assert unbounded.slice_ms(60) == 60
    assert unbounded.allows(250) is True
    assert unbounded.expired() is False


def test_sequential_slices_cannot_exceed_budget() -> None:
    """Timeouts must be derived from slice_ms at dispatch, never cached."""
    clock = _Clock()
    deadline = _turn(clock, budget_ms=100, backend_budget_ms=100)
    first = deadline.slice_ms(80)
    assert first == 80
    clock.advance_ms(80)
    second = deadline.slice_ms(80)
    assert second == 20
    clock.advance_ms(20)
    assert deadline.slice_ms(80) == 0


def test_allows_respects_the_floor() -> None:
    clock = _Clock()
    deadline = Deadline.start(budget_ms=100, backend_budget_ms=100, floor_ms=50, monotonic=clock)
    assert deadline.allows(1) is True  # the floor, not the request, is the minimum
    clock.advance_ms(60)
    assert deadline.allows(1) is False
    assert 39 <= deadline.remaining_ms() <= 41


def test_start_clamps_negative_inputs() -> None:
    deadline = Deadline.start(budget_ms=-5, backend_budget_ms=-5, floor_ms=-5)
    assert deadline.budget_ms == 0
    assert deadline.backend_budget_ms == 1
    assert deadline.floor_ms == 0


def test_remaining_seconds_tracks_remaining_ms() -> None:
    clock = _Clock()
    deadline = _turn(clock, budget_ms=2000)
    clock.advance_ms(500)
    assert deadline.remaining_seconds() == 1.5


# --------------------------------------------------------------------------- retrieval_child


def test_retrieval_child_disabled_returns_the_parent() -> None:
    """The off-switch must actually switch off, not cap retrieval at the same budget."""
    clock = _Clock()
    turn = Deadline.unbounded(monotonic=clock)
    assert retrieval_child(turn, enabled=False, budget_ms=120) is turn
    clock.advance_ms(200)
    assert turn.allows(250) is True
    assert turn.slice_ms(60) == 60


def test_retrieval_child_enabled_is_a_bounded_child() -> None:
    clock = _Clock()
    turn = _turn(clock, budget_ms=3500)
    child = retrieval_child(turn, enabled=True, budget_ms=120)
    assert child is not turn
    assert child.label == "retrieval"
    assert child.remaining_ms() == 120
    clock.advance_ms(121)
    assert child.expired() is True
    assert turn.expired() is False


def test_retrieval_child_clock_starts_at_the_call_site() -> None:
    clock = _Clock()
    turn = _turn(clock, budget_ms=3500)
    clock.advance_ms(1000)  # everything before retrieval
    child = retrieval_child(turn, enabled=True, budget_ms=120)
    assert child.remaining_ms() == 120  # the child's budget, not what is left of the turn


# --------------------------------------------------------------------------- ledger


def test_ledger_meta_key_set_and_order() -> None:
    clock = _Clock()
    deadline = _turn(clock, budget_ms=3500, backend_budget_ms=60)
    ledger = RetrievalLedger()
    ledger.record_executed("primary", ms=12, rows=4)
    ledger.record_skip("vector", remaining_ms=0, reason="deadline_expired_skip")
    meta = ledger.to_meta(deadline)
    assert list(meta) == [
        "deadline_policy_revision",
        "deadline_budget_ms",
        "deadline_backend_budget_ms",
        "deadline_remaining_ms",
        "deadline_expired",
        "queries_executed",
        "queries_timed_out",
        "queries_skipped",
        "queries_cached",
        "retrieval_degraded",
    ]
    assert meta["deadline_policy_revision"] == RETRIEVAL_DEADLINE_POLICY_REVISION
    assert meta["deadline_budget_ms"] == 3500
    assert meta["deadline_backend_budget_ms"] == 60
    assert meta["retrieval_degraded"] is True
    assert meta["queries_executed"] == [{"name": "primary", "ms": 12, "rows": 4}]


def test_ledger_meta_without_a_deadline() -> None:
    meta = RetrievalLedger().to_meta(None)
    assert meta["deadline_budget_ms"] == 0
    assert meta["deadline_backend_budget_ms"] == 0
    assert meta["deadline_remaining_ms"] == 0
    assert meta["deadline_expired"] is False
    assert meta["retrieval_degraded"] is False


def test_ledger_is_first_query() -> None:
    ledger = RetrievalLedger()
    assert ledger.is_first_query() is True
    ledger.record_cache_hit("primary", key="k")
    assert ledger.is_first_query() is True  # a cache hit ran no query
    ledger.record_skip("vector", remaining_ms=0, reason="r")
    assert ledger.is_first_query() is True  # nor did a skip
    ledger.record_executed("primary", ms=1)
    assert ledger.is_first_query() is False


def test_ledger_degraded_only_on_timeout_or_skip() -> None:
    ledger = RetrievalLedger()
    ledger.record_executed("primary", ms=1)
    ledger.record_cache_hit("primary", key="k")
    assert ledger.degraded() is False
    ledger.record_timeout("vector", budget_ms=10)
    assert ledger.degraded() is True


# --------------------------------------------------------------------------- advisor


def test_advisor_turn_deadline_default_and_reset() -> None:
    assert ADVISOR_TURN_DEADLINE.get() is None
    deadline = Deadline.unbounded()
    token = ADVISOR_TURN_DEADLINE.set(deadline)
    try:
        assert ADVISOR_TURN_DEADLINE.get() is deadline
    finally:
        ADVISOR_TURN_DEADLINE.reset(token)
    assert ADVISOR_TURN_DEADLINE.get() is None


def test_advisor_timeout_bucket_ms() -> None:
    assert advisor_timeout_bucket_ms(0) == ADVISOR_TIMEOUT_BUCKET_MS
    assert advisor_timeout_bucket_ms(249) == ADVISOR_TIMEOUT_BUCKET_MS
    assert advisor_timeout_bucket_ms(900) == 750  # rounds DOWN, never above what is left
    assert advisor_timeout_bucket_ms(1000) == 1000
    buckets = {advisor_timeout_bucket_ms(value) for value in range(0, 3500, 7)}
    assert len(buckets) <= 14


# --------------------------------------------------------------------------- cache keys


def _retrieval_args(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "events",
        "workspace_id": "ws-1",
        "owner_ids": ["a", "b"],
        "subject_user_id": "subject",
        "project_id": None,
        "policy_revision": RETRIEVAL_DEADLINE_POLICY_REVISION,
        "contract_revision": 1,
        "evidence_revision": "ev1",
        "query_intent": "recall",
        "flags": {"vector": True},
        "query_text": "why did the build fail",
    }
    base.update(overrides)
    return base


def test_cache_key_varies() -> None:
    baseline = retrieval_cache_key(**_retrieval_args())
    for field, value in (
        ("workspace_id", "ws-2"),
        ("owner_ids", ["a", "c"]),
        ("subject_user_id", "other"),
        ("project_id", "p1"),
        ("policy_revision", "other"),
        ("contract_revision", 2),
        ("evidence_revision", "ev2"),
        ("query_intent", "plan"),
        ("flags", {"vector": False}),
        ("query_text", "something else"),
    ):
        assert retrieval_cache_key(**_retrieval_args(**{field: value})) != baseline, field

    # owner order and flag order must not change the key
    assert retrieval_cache_key(**_retrieval_args(owner_ids=["b", "a"])) == baseline
    assert baseline.startswith("tce:r2:events:")

    assert embedding_cache_key(model_id="m1", dimensions=1024, query_text="x") != embedding_cache_key(
        model_id="m2", dimensions=1024, query_text="x"
    )
    assert embedding_cache_key(model_id="m1", dimensions=768, query_text="x") != embedding_cache_key(
        model_id="m1", dimensions=1024, query_text="x"
    )
    assert embedding_cache_key(model_id="m1", dimensions=1024, query_text=" A  b ") == embedding_cache_key(
        model_id="m1", dimensions=1024, query_text="a b"
    )
    assert embedding_cache_key(model_id="m", dimensions=1, query_text="x").startswith("tce:emb:v2:")
    assert embedding_cooldown_key(model_id="m", workspace_id="ws-1") != embedding_cooldown_key(
        model_id="m", workspace_id="ws-2"
    )

    goal_args: dict[str, Any] = {
        "workspace_id": "ws-1",
        "user_id": "u",
        "session_id": "s",
        "objective_hash": "h",
        "state_version": "v",
        "contract_revision": 1,
        "scope_digest": "sd",
        "policy_revision": TASK_STATE_POLICY_REVISION,
    }
    goal_baseline = goal_cache_key(**goal_args)
    assert goal_baseline.startswith("goalq:")
    assert goal_cache_key(**{**goal_args, "contract_revision": 2}) != goal_baseline
    assert goal_cache_key(**{**goal_args, "scope_digest": "sd2"}) != goal_baseline
    assert goal_cache_key(**{**goal_args, "policy_revision": "other"}) != goal_baseline
