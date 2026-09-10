"""The dream fold: total, deterministic, and lossless under truncation.

Four properties are asserted here rather than reviewed:

* a rebuild of the event log equals the stored projection, field for field;
* an unknown kind is collected, never raised — a rollback cannot brick a proposal;
* truncating an 804-event log to 500 changes **nothing** the projection reports;
* the transition table is closed, ``objective_bound`` never moves the status, and
  ``evidence_revalidated`` never moves it either.

Plus G16 / p45_shared §10 G-X4 (ruling Y5): no migration in the tree drops a table that
holds the owner's own answers.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tce_shared.aspirations import (
    MAX_DREAM_EVENTS_PER_FOLD,
    PINNED_DREAM_KIND_VALUES,
    PINNED_DREAM_KINDS,
    DreamProposalEvent,
    DreamProposalEventKind,
    DreamProposalStatus,
    NonresponseState,
    citations_to_json,
    fold_dream_proposal,
    prepare_dream_write,
    surfaced_event,
    transition_allowed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 1, 1, tzinfo=UTC)

_FOLD_SCOPE = {
    "proposal_id": "prop-1",
    "workspace_id": "ws",
    "owner_id": "owner",
    "subject_user_id": "subject",
    "session_id": "sess",
}


def _fold(events, *, now=BASE, after_surfaces=3, max_events=MAX_DREAM_EVENTS_PER_FOLD, revision=1):
    return fold_dream_proposal(
        events,
        revision=revision,
        now=now,
        after_surfaces=after_surfaces,
        max_events=max_events,
        **_FOLD_SCOPE,
    )


def _proposed(seq: int = 1, *, at: datetime = BASE) -> DreamProposalEvent:
    return DreamProposalEvent(
        seq=seq,
        kind=DreamProposalEventKind.PROPOSED,
        payload={
            "title": "get the witness engine in front of someone",
            "connection_text": "they keep coming back to it",
            "benefit_text": "somebody other than them has used it",
            "first_step": "send it to one person this week",
            "citations": citations_to_json(()),
            "theme_tokens": ["witness", "engine", "front", "someone"],
            "evidence_basis": "trusted_current",
            "evidence_revision": "rev-a",
            "evidence_cutoff_at": at.isoformat(),
            "project_id": "proj_a",
            "scope_kind": "project",
            "expires_at": (at + timedelta(days=45)).isoformat(),
        },
        occurred_at=at,
        actor="system",
    )


# --------------------------------------------------------------------------- rebuild


def test_rebuild_equals_stored() -> None:
    """A re-fold of 200 events equals the projection the writer stored.

    This is the property that makes the row a cache of the log rather than a second source
    of truth: if it ever fails, the stored projection is claiming something the events do
    not say, and every downstream read is quoting that claim.
    """
    events: list[DreamProposalEvent] = [_proposed()]
    first_shown = BASE + timedelta(hours=1)
    for ordinal in range(1, 197):
        events.append(
            surfaced_event(
                ordinal=ordinal,
                first_surfaced_at=first_shown,
                occurred_at=first_shown + timedelta(hours=ordinal),
                actor="executor-a",
                actor_class="executor",
                prior_attested=False,
            )
        )
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.ACCEPTED,
            payload={},
            occurred_at=BASE + timedelta(days=30),
            actor="owner",
            actor_class="human",
        )
    )
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.OBJECTIVE_BOUND,
            payload={"task_id": "task-1", "objective_hash": "h-abc"},
            occurred_at=BASE + timedelta(days=31),
        )
    )
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.PURSUIT_STARTED,
            payload={"plan_root_goal_id": "goal-1"},
            occurred_at=BASE + timedelta(days=32),
        )
    )

    write = prepare_dream_write(
        [],
        events,
        expected_revision=0,
        highest_seq=0,
        now=BASE + timedelta(days=40),
        after_surfaces=3,
        **_FOLD_SCOPE,
    )
    assert len(write.events) == 200
    rebuilt = _fold(write.events, now=BASE + timedelta(days=40), revision=write.next_revision)
    assert rebuilt.projection == write.projection
    assert rebuilt.source_revision == write.source_revision
    assert write.projection.status is DreamProposalStatus.PURSUED
    assert write.projection.surfaced_count == 196
    assert write.projection.nonresponse is NonresponseState.IGNORED


def test_unknown_kind_is_collected_not_raised() -> None:
    """A kind a newer build wrote is counted and ignored, never fatal."""
    events = [
        _proposed(),
        DreamProposalEvent(seq=2, kind="teleported", payload={"x": 1}, occurred_at=BASE),
    ]
    folded = _fold(events)
    assert folded.unknown_kinds == ("teleported",)
    assert folded.projection.status is DreamProposalStatus.PROPOSED


# --------------------------------------------------------------------------- truncation


def test_truncation_is_lossless_for_the_projection() -> None:
    """804 events folded at 10,000 and at 500 give the *same* projection.

    The earlier rule pinned three kinds and accumulated ``surfaced_count += 1``.  On this
    exact log that loses 306 of 800 surfaced events and the entire task binding, so a
    truncated pursued proposal reads as unbound and its nonresponse regresses.  Asserting
    only that a pinned status survived — which the lossy rule also satisfied — is why that
    defect shipped.
    """
    events: list[DreamProposalEvent] = [_proposed()]
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.ACCEPTED,
            payload={},
            occurred_at=BASE + timedelta(hours=1),
            actor="owner",
            actor_class="human",
        )
    )
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.OBJECTIVE_BOUND,
            payload={"task_id": "task-1", "objective_hash": "h-abc"},
            occurred_at=BASE + timedelta(hours=2),
        )
    )
    first_shown = BASE + timedelta(hours=11)
    attested = False
    for ordinal in range(1, 801):
        actor_class = "human" if ordinal == 1 else "executor"
        events.append(
            surfaced_event(
                ordinal=ordinal,
                first_surfaced_at=first_shown,
                occurred_at=first_shown + timedelta(hours=ordinal),
                actor="renderer",
                actor_class=actor_class,
                prior_attested=attested,
            )
        )
        attested = attested or actor_class == "human"
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.PURSUIT_STARTED,
            payload={"plan_root_goal_id": "goal-1"},
            occurred_at=first_shown + timedelta(hours=900),
        )
    )

    write = prepare_dream_write(
        [], events, expected_revision=0, highest_seq=0, now=BASE, after_surfaces=3, **_FOLD_SCOPE
    )
    assert len(write.events) == 804

    full = _fold(write.events, max_events=10_000)
    trunc = _fold(write.events, max_events=500)

    assert trunc.truncated is True
    assert full.truncated is False
    assert trunc.projection == full.projection
    # And the values are the real ones, not merely equal to each other.
    assert full.projection.surfaced_count == 800
    assert full.projection.task_id == "task-1"
    assert full.projection.objective_hash == "h-abc"
    assert full.projection.first_surfaced_at == first_shown
    assert full.projection.surfaced_attested is True
    assert full.projection.status is DreamProposalStatus.PURSUED


def test_every_kind_is_pinned() -> None:
    """All fourteen.  Pinning a subset is what made truncation lossy in the first place."""
    assert PINNED_DREAM_KINDS == frozenset(str(kind) for kind in DreamProposalEventKind)
    assert len(PINNED_DREAM_KIND_VALUES) == 14
    assert PINNED_DREAM_KIND_VALUES == tuple(sorted(PINNED_DREAM_KIND_VALUES))
    assert "surfaced" in PINNED_DREAM_KINDS
    assert "snoozed" in PINNED_DREAM_KINDS


def test_surfaced_survives_truncation_far_beyond_the_pin() -> None:
    """A proposal that was shown must never silently forget it was shown.

    ``surfaced`` and ``snoozed`` are the two kinds an earlier draft did not pin, and both
    carry human-visible state.  Here every event after the first three is ``surfaced``, so a
    tail-only rule keeps the count and a pinned-subset rule that dropped ``snoozed`` would
    lose the deferral the owner asked for.
    """
    events: list[DreamProposalEvent] = [_proposed()]
    events.append(
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.SNOOZED,
            payload={"snooze_until": (BASE + timedelta(days=7)).isoformat()},
            occurred_at=BASE + timedelta(minutes=5),
            actor="owner",
            actor_class="human",
        )
    )
    write = prepare_dream_write(
        [], events, expected_revision=0, highest_seq=0, now=BASE, after_surfaces=3, **_FOLD_SCOPE
    )
    tail: list[DreamProposalEvent] = list(write.events)
    # 600 later events of one kind push the snooze out of any plausible tail window.
    filler = [
        DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.EVIDENCE_REVALIDATED,
            payload={"citations": [], "evidence_revision": f"rev-{i}"},
            occurred_at=BASE + timedelta(days=1, minutes=i),
        )
        for i in range(600)
    ]
    write2 = prepare_dream_write(
        tail,
        filler,
        expected_revision=1,
        highest_seq=len(tail),
        now=BASE,
        after_surfaces=3,
        **_FOLD_SCOPE,
    )
    assert write2.projection.status is DreamProposalStatus.SNOOZED
    assert write2.projection.snooze_until == BASE + timedelta(days=7)


# --------------------------------------------------------------------------- the table


def test_transition_table_is_closed() -> None:
    """Every ``(status, kind)`` pair outside §1.2's table is refused."""
    legal = {
        DreamProposalStatus.PROPOSED: {"surfaced", "accepted", "rejected", "snoozed", "withdrawn", "expired", "superseded"},
        DreamProposalStatus.SURFACED: {"surfaced", "accepted", "rejected", "snoozed", "withdrawn", "expired", "superseded"},
        DreamProposalStatus.SNOOZED: {"unsnoozed", "accepted", "rejected", "withdrawn", "expired"},
        DreamProposalStatus.ACCEPTED: {"objective_bound", "pursuit_started", "pursuit_abandoned", "rejected", "withdrawn"},
        DreamProposalStatus.PURSUED: {"completed", "pursuit_abandoned", "withdrawn"},
        DreamProposalStatus.COMPLETED: set(),
        DreamProposalStatus.ABANDONED: {"accepted"},
        DreamProposalStatus.REJECTED: set(),
        DreamProposalStatus.WITHDRAWN: set(),
        DreamProposalStatus.EXPIRED: set(),
    }
    for status in DreamProposalStatus:
        for kind in DreamProposalEventKind:
            ok, reason = transition_allowed(status, kind)
            if kind is DreamProposalEventKind.EVIDENCE_REVALIDATED:
                assert ok is True, f"{status} -> {kind} must always be legal"
                continue
            expected = str(kind) in legal[status]
            assert ok is expected, f"{status} -> {kind}: got {ok}, expected {expected}"
            if not ok:
                assert reason, f"{status} -> {kind} refused with no reason"


def test_a_rejection_can_still_be_revalidated_but_not_reversed() -> None:
    """Re-checking the evidence behind a "no" must not be blocked by the "no"."""
    assert transition_allowed(DreamProposalStatus.REJECTED, DreamProposalEventKind.EVIDENCE_REVALIDATED)[0] is True
    assert transition_allowed(DreamProposalStatus.REJECTED, DreamProposalEventKind.ACCEPTED)[0] is False
    assert transition_allowed(DreamProposalStatus.COMPLETED, DreamProposalEventKind.EVIDENCE_REVALIDATED)[0] is True


def test_proposed_is_a_mint_not_a_transition() -> None:
    for status in DreamProposalStatus:
        ok, reason = transition_allowed(status, DreamProposalEventKind.PROPOSED)
        assert ok is False
        assert "minted" in reason


def test_objective_bound_does_not_change_status() -> None:
    """D6, guarantee 1: binding an objective — and therefore creating a plan — completes nothing."""
    events = [
        _proposed(),
        DreamProposalEvent(
            seq=2,
            kind=DreamProposalEventKind.ACCEPTED,
            payload={},
            occurred_at=BASE + timedelta(hours=1),
            actor="owner",
            actor_class="human",
        ),
        DreamProposalEvent(
            seq=3,
            kind=DreamProposalEventKind.OBJECTIVE_BOUND,
            payload={"task_id": "task-1", "objective_hash": "h-abc", "plan_root_goal_id": "goal-1"},
            occurred_at=BASE + timedelta(hours=2),
        ),
    ]
    projection = _fold(events).projection
    assert projection.status is DreamProposalStatus.ACCEPTED
    assert projection.task_id == "task-1"
    assert projection.objective_hash == "h-abc"
    assert projection.plan_root_goal_id == "goal-1"
    assert projection.pursuit_started_at is None
    assert projection.completed_at is None


def test_evidence_revalidated_never_changes_status_and_restamps_revision() -> None:
    """D13.  The citation set, the basis, the revision and the cutoff all move together.

    An earlier draft stamped ``evidence_revision`` once at ``proposed`` and never again, so
    after a merge it described a citation set the proposal no longer had.
    """
    events = [
        _proposed(),
        DreamProposalEvent(
            seq=2,
            kind=DreamProposalEventKind.REJECTED,
            payload={"reason": "not now"},
            occurred_at=BASE + timedelta(hours=1),
            actor="owner",
            actor_class="human",
        ),
        DreamProposalEvent(
            seq=3,
            kind=DreamProposalEventKind.EVIDENCE_REVALIDATED,
            payload={
                "citations": [
                    {
                        "event_id": "e9",
                        "receipt_id": "r9",
                        "content_sha256": "s9",
                        "origin_kind": "imported_transcript",
                        "observed_at": BASE.isoformat(),
                        "quote": "send it to one person this week",
                    }
                ],
                "evidence_basis": "backfill_only",
                "evidence_revision": "rev-b",
                "evidence_cutoff_at": (BASE + timedelta(days=2)).isoformat(),
            },
            occurred_at=BASE + timedelta(hours=2),
        ),
    ]
    projection = _fold(events).projection
    assert projection.status is DreamProposalStatus.REJECTED
    assert projection.rejection_reason == "not now"
    assert projection.evidence_revision == "rev-b"
    assert projection.evidence_basis == "backfill_only"
    assert projection.evidence_cutoff_at == BASE + timedelta(days=2)
    assert [c.event_id for c in projection.citations] == ["e9"]


# --------------------------------------------------------------------------- nonresponse


def test_a_proposal_never_surfaced_reads_never_surfaced_a_year_later() -> None:
    """X2, with a far-future clock.  Time is not an answer.

    The whole defect this axis replaces was one nullable ``acknowledged_at`` meaning "not
    seen", "seen and ignored" and "never surfaced" at once — so a proposal nobody ever showed
    the owner aged into something indistinguishable from a rejection.
    """
    projection = _fold([_proposed()], now=BASE + timedelta(days=365 * 5)).projection
    assert projection.surfaced_count == 0
    assert projection.nonresponse is NonresponseState.NEVER_SURFACED
    assert projection.status is DreamProposalStatus.PROPOSED
    assert projection.rejected_at is None


# --------------------------------------------------------------------------- Y5


def _git_files(patterns: tuple[str, ...]) -> list[Path]:
    """The ``git ls-files --cached --others --exclude-standard`` view.

    ``services/*/build/`` holds stale untracked trees on a developer machine, so a gate that
    walks the filesystem with ``rglob`` is red locally and green in CI — worse than no gate.
    ``--others`` is deliberate: a phase's own new revision file is untracked until the phase
    commits, and a Y5 scanner that could not see the migration being written would pass at
    exactly the moment it matters.
    """
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    seen: dict[str, Path] = {}
    for line in completed.stdout.splitlines():
        rel = line.strip()
        # A file git still has in the index but that has been deleted in the working tree
        # cannot be scanned; skipping it is what lets a deletion land before its commit.
        if not rel or "/build/" in rel or not (REPO_ROOT / rel).is_file():
            continue
        seen.setdefault(rel, REPO_ROOT / rel)
    return sorted(seen.values())


# Resolved once, at import time: tests/conftest.py blocks ``subprocess`` inside an unmarked
# test, and a gate that needs a marker to read its own file list is a gate people stop writing.
_MIGRATIONS: list[Path] = _git_files(("infra/alembic/versions/*.py",))


HUMAN_ANSWER_TABLES: tuple[str, ...] = (
    "dream_proposals",
    "dream_proposal_events",
    "dream_generation_runs",
    "policy_qualifications",
)


def test_no_migration_drops_a_human_answer_table() -> None:
    """G16 / p45_shared §10 G-X4 (ruling Y5).

    ``ci.yml`` runs a real ``alembic downgrade`` on every integration job.  A ``downgrade()``
    that dropped these tables would destroy every accept and reject the owner ever recorded —
    the exact loss P5 exists to prevent, moved into a statement a DELETE-scanner would not
    see.  The P5 revision's ``downgrade()`` is a documented no-op for precisely this reason.
    """
    offenders: list[str] = []
    for path in _MIGRATIONS:
        text = path.read_text(encoding="utf-8")
        for table in HUMAN_ANSWER_TABLES:
            pattern = re.compile(rf"DROP\s+TABLE(\s+IF\s+EXISTS)?\s+{re.escape(table)}\b", re.IGNORECASE)
            if pattern.search(text):
                offenders.append(f"{path.name}: DROP TABLE {table}")
    assert offenders == [], (
        "a migration drops a table holding the owner's own answers: " + "; ".join(offenders)
    )


def test_the_scan_is_not_vacuous() -> None:
    """The Y5 scanner must actually match a ``DROP TABLE`` when one is there.

    A guard-for-later gate that would pass on a tree containing the very statement it
    forbids proves nothing, so the regex is exercised against a synthetic offender.
    """
    sample = "def downgrade() -> None:\n    op.execute('DROP TABLE IF EXISTS dream_proposals')\n"
    pattern = re.compile(r"DROP\s+TABLE(\s+IF\s+EXISTS)?\s+dream_proposals\b", re.IGNORECASE)
    assert pattern.search(sample) is not None
    assert _MIGRATIONS, "no migrations found — the scan is scoped wrong"


@pytest.mark.parametrize("table", HUMAN_ANSWER_TABLES)
def test_human_answer_tables_are_named_not_guessed(table: str) -> None:
    assert table.islower() and " " not in table
