"""One turn, one budget: the deadline arithmetic both backends share.

A takeover turn today can spend an unbounded amount of time in retrieval and then a
further unbounded amount in the advisor, because every timeout is configured
independently and none of them knows how much of the turn is already gone. This module
is the missing piece: a single :class:`Deadline` is created at the top of the turn, every
sub-budget is a *child* of it, and a child can only ever shrink.

Pure and stdlib-only — no pydantic, no ``.events``, no ``.task_state``. It is imported by
the request path in both backends and by nothing that does I/O.

The one rule callers must follow: **derive every timeout from ``slice_ms`` at the moment of
dispatch, never cache it.** A cached slice re-grants time that has already been spent.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ADVISOR_TIMEOUT_BUCKET_MS",
    "ADVISOR_TURN_DEADLINE",
    "RETRIEVAL_DEADLINE_POLICY_REVISION",
    "RETRIEVAL_REASON_DEADLINE_ABORT",
    "RETRIEVAL_REASON_DEADLINE_SKIP",
    "RETRIEVAL_SOURCE_DEADLINE_PARTIAL",
    "SQLITE_INTERRUPT_MARKER",
    "Deadline",
    "RetrievalLedger",
    "advisor_timeout_bucket_ms",
    "embedding_cache_key",
    "embedding_cooldown_key",
    "retrieval_cache_key",
    "retrieval_child",
]

RETRIEVAL_DEADLINE_POLICY_REVISION: str = "p2-2026-09"
RETRIEVAL_SOURCE_DEADLINE_PARTIAL: str = "deadline_partial"
RETRIEVAL_REASON_DEADLINE_SKIP: str = "deadline_expired_skip"
RETRIEVAL_REASON_DEADLINE_ABORT: str = "deadline_expired_abort"
SQLITE_INTERRUPT_MARKER: str = "interrupted"
ADVISOR_TIMEOUT_BUCKET_MS: int = 250


@dataclass(frozen=True, slots=True)
class Deadline:
    """A monotonic time budget that children can narrow but never widen."""

    started_at: float
    budget_ms: int
    backend_budget_ms: int
    floor_ms: int = 5
    label: str = "turn"
    parent: Deadline | None = None
    monotonic: Callable[[], float] = time.monotonic

    @classmethod
    def start(
        cls,
        *,
        budget_ms: int,
        backend_budget_ms: int,
        floor_ms: int = 5,
        label: str = "turn",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Deadline:
        return cls(
            started_at=monotonic(),
            budget_ms=max(0, int(budget_ms)),
            backend_budget_ms=max(1, int(backend_budget_ms)),
            floor_ms=max(0, int(floor_ms)),
            label=label,
            parent=None,
            monotonic=monotonic,
        )

    @classmethod
    def unbounded(
        cls, label: str = "unbounded", monotonic: Callable[[], float] = time.monotonic
    ) -> Deadline:
        """``backend_budget_ms`` must be huge too.

        Otherwise ``slice_ms()`` is 0 and turning ``retrieval_deadline_enabled`` off would
        disable all retrieval instead of unbounding it.
        """
        return cls(
            started_at=monotonic(),
            budget_ms=10**9,
            backend_budget_ms=10**9,
            floor_ms=0,
            label=label,
            parent=None,
            monotonic=monotonic,
        )

    def elapsed_ms(self, now: float | None = None) -> int:
        return max(0, int(((now if now is not None else self.monotonic()) - self.started_at) * 1000))

    def remaining_ms(self, now: float | None = None) -> int:
        own = max(0, self.budget_ms - self.elapsed_ms(now))
        return own if self.parent is None else min(own, self.parent.remaining_ms(now))

    def remaining_seconds(self, now: float | None = None) -> float:
        return self.remaining_ms(now) / 1000.0

    def expired(self, now: float | None = None) -> bool:
        return self.remaining_ms(now) <= 0

    def allows(self, minimum_ms: int, now: float | None = None) -> bool:
        return self.remaining_ms(now) >= max(int(minimum_ms), self.floor_ms)

    def slice_ms(self, requested_ms: int, now: float | None = None) -> int:
        return max(0, min(int(requested_ms), self.backend_budget_ms, self.remaining_ms(now)))

    def child(
        self,
        *,
        label: str,
        budget_ms: int | None = None,
        backend_budget_ms: int | None = None,
        floor_ms: int | None = None,
    ) -> Deadline:
        """A sub-deadline that can only shrink.

        It cannot outlive its parent — ``remaining_ms()`` is ``min(own, parent.remaining_ms())``
        transitively — and its clock starts NOW, so time already spent on the parent is never
        re-granted.
        """
        now = self.monotonic()
        remaining = self.remaining_ms(now)
        requested = remaining if budget_ms is None else max(0, int(budget_ms))
        return Deadline(
            started_at=now,
            budget_ms=min(requested, remaining),
            backend_budget_ms=min(
                self.backend_budget_ms if backend_budget_ms is None else max(1, int(backend_budget_ms)),
                self.backend_budget_ms,
            ),
            floor_ms=self.floor_ms if floor_ms is None else max(0, int(floor_ms)),
            label=label,
            parent=self,
            monotonic=self.monotonic,
        )


def retrieval_child(turn: Deadline, *, enabled: bool, budget_ms: int) -> Deadline:
    """Build the retrieval sub-deadline AT THE RETRIEVAL CALL SITE.

    ``enabled=False`` returns ``turn`` UNCHANGED — no child, no budget, no guard. With the
    knob off, ``turn`` is ``Deadline.unbounded()``, so ``slice_ms()`` is large and ``allows()``
    is always True: retrieval is genuinely unbounded, which is what the knob is for. A child
    built anyway would still cap retrieval at ``budget_ms``, which is the opposite of an
    off-switch.

    ``enabled=True`` returns ``turn.child(label="retrieval", budget_ms=max(0, int(budget_ms)))``.
    The child's clock starts NOW, which is why this must be called immediately before the first
    retrieval query and not at the top of the turn: several hundred lines and a few round trips
    sit between the two points, and that time would otherwise come out of the retrieval budget.
    """
    if not enabled:
        return turn
    return turn.child(label="retrieval", budget_ms=max(0, int(budget_ms)))


@dataclass(slots=True)
class RetrievalLedger:
    """What retrieval actually did this turn, so an operator can see which query was dropped."""

    executed: list[dict[str, Any]] = field(default_factory=list)
    timed_out: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    cached: list[dict[str, Any]] = field(default_factory=list)

    def record_executed(self, name: str, *, ms: int, rows: int = 0) -> None:
        self.executed.append({"name": name, "ms": int(ms), "rows": int(rows)})

    def record_timeout(self, name: str, *, budget_ms: int) -> None:
        self.timed_out.append({"name": name, "budget_ms": int(budget_ms)})

    def record_skip(self, name: str, *, remaining_ms: int, reason: str) -> None:
        self.skipped.append({"name": name, "remaining_ms": int(remaining_ms), "reason": reason})

    def record_cache_hit(self, name: str, *, key: str) -> None:
        self.cached.append({"name": name, "key": key})

    def is_first_query(self) -> bool:
        """True until a query has completed or timed out.

        The first query of a turn is deliberately unguarded, so it cannot be aborted by the
        budget and ``record_executed`` always fires for it.
        """
        return not (self.executed or self.timed_out)

    def degraded(self) -> bool:
        return bool(self.timed_out or self.skipped)

    def to_meta(self, deadline: Deadline | None) -> dict[str, Any]:
        """The ten retrieval-meta keys, in this order, identically in both backends."""
        return {
            "deadline_policy_revision": RETRIEVAL_DEADLINE_POLICY_REVISION,
            "deadline_budget_ms": int(deadline.budget_ms) if deadline is not None else 0,
            "deadline_backend_budget_ms": int(deadline.backend_budget_ms) if deadline is not None else 0,
            "deadline_remaining_ms": int(deadline.remaining_ms()) if deadline is not None else 0,
            "deadline_expired": bool(deadline.expired()) if deadline is not None else False,
            "queries_executed": list(self.executed),
            "queries_timed_out": list(self.timed_out),
            "queries_skipped": list(self.skipped),
            "queries_cached": list(self.cached),
            "retrieval_degraded": self.degraded(),
        }


# --------------------------------------------------------------------------- cache keys
#
# The `v2` prefixes are mandatory: they orphan every existing key in one step, which is the
# fix for the recorded 768 -> 1024 embedding-dimension incident. The cooldown key is
# workspace-scoped so one workspace's slow provider no longer suppresses vector search
# globally.


def _collapse_ws(value: str) -> str:
    return " ".join(str(value or "").split())


def _canonical_json(value: Any) -> str:
    """Byte-identical to ``task_state.canonical_json``, redefined so this module stays stdlib-only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def retrieval_cache_key(
    *,
    kind: str,
    workspace_id: str,
    owner_ids: Sequence[str],
    subject_user_id: str,
    project_id: str | None,
    policy_revision: str,
    contract_revision: int,
    evidence_revision: str,
    query_intent: str,
    flags: Mapping[str, Any],
    query_text: str,
) -> str:
    raw = _canonical_json(
        {
            "workspace_id": workspace_id,
            "owner_ids": sorted(str(item) for item in owner_ids),
            "subject_user_id": subject_user_id,
            "project_id": project_id or "",
            "policy_revision": policy_revision,
            "contract_revision": int(contract_revision),
            "evidence_revision": evidence_revision,
            "query_intent": query_intent,
            "flags": {str(key): flags[key] for key in sorted(flags)},
            "query_text": query_text,
        }
    )
    return f"tce:r2:{kind}:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def embedding_cache_key(*, model_id: str, dimensions: int, query_text: str) -> str:
    raw = f"{model_id}|{int(dimensions)}|{_collapse_ws(query_text).lower()}"
    return "tce:emb:v2:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def embedding_cooldown_key(*, model_id: str, workspace_id: str) -> str:
    raw = f"{model_id}|{workspace_id}"
    return "tce:emb:cooldown:v2:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def advisor_timeout_bucket_ms(remaining_ms: int) -> int:
    """Bucket a deadline-derived advisor timeout so it cannot thrash the gateway cache.

    Rounds DOWN, so the bucketed value never exceeds what the deadline actually has left.
    The floor equals ``retrieval_advisor_min_ms`` (250) — the advisor gate refuses to open
    below that, so the floor is never reached in practice. A 3500 ms turn yields at most 14
    distinct buckets, which is why the gateway cache is sized well above that.
    """
    return max(
        ADVISOR_TIMEOUT_BUCKET_MS,
        (int(remaining_ms) // ADVISOR_TIMEOUT_BUCKET_MS) * ADVISOR_TIMEOUT_BUCKET_MS,
    )


# The advisor is a FastAPI path operation; adding a `deadline` parameter would make FastAPI
# treat it as a request field and change the OpenAPI document. So the deadline travels out of
# band. FastAPI runs each request in its own context, so this cannot leak between turns.
ADVISOR_TURN_DEADLINE: ContextVar[Deadline | None] = ContextVar(
    "tce_advisor_turn_deadline", default=None
)
