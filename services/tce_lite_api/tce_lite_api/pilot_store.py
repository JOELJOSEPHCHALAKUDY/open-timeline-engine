"""P6 operational-proof pilot — the Lite (SQLite) store.

The twin of ``tce_api.pilot_store``: same function names, same arguments, same return shapes,
same refusals — with ``conn: sqlite3.Connection``, ``?`` parameters, ISO-8601 text timestamps and
``INTEGER`` booleans.  The two files are meant to be diffed rather than reasoned about, which is
why the SQL is laid out statement for statement against the Full one.

The three server-derived judgements are computed here for the same reasons stated there:
``deviated`` from ``executed_arm`` against the assigned arm, ``completion_basis`` from P3's
verification result then P2's task state and only then the human's ``finished``, and
``adjudication_independent`` from the adjudicator against the enrolling principal.  The party
under test does not grade itself, and the place that rule is kept is the store.

Two things are SQLite-specific and are the whole reason this file exists rather than a shared one:

* **The stratum slot is taken inside ``BEGIN IMMEDIATE``.**  Python's ``sqlite3`` opens DEFERRED
  transactions, so a read-then-write upgrade on a stale WAL snapshot fails with
  ``SQLITE_BUSY_SNAPSHOT``.  ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` needs SQLite 3.35
  and the write lock, and the P2 bracket (``task_state_store.run_cas_section``) is imported rather
  than re-invented so that Lite still issues ``BEGIN IMMEDIATE`` in exactly one place.
* **``pilot_strata`` is locked before ``pilot_episodes`` and never after**, in the one function
  that writes both.  Same order as Full; the AST walk that asserts it scans both files.

Nothing here deletes or rewrites an allocation.  No statement in this module removes a row from
any P6 table, and the only column an episode row ever updates is ``revealed_at``, only from NULL.
A unit test scans every SQL literal in the tree for a counter-example.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.events import (
    PilotCellReport,
    PilotClaimBReport,
    PilotClause,
    PilotReportResponse,
)
from tce_shared.pilot_enrollment import (
    ARM_SET_SHA,
    NO_ENROLLED_EPISODES_SENTENCE,
    POOLING_REFUSED_SENTENCE,
    RANDOMIZED_ARMS,
    SINGLE_PARTICIPANT_SENTENCE,
    SUCCESS_DEFINITION,
    AllocationKind,
    ArmAllocation,
    Clause,
    CloseFacts,
    CompletionBasis,
    EnrolmentOutcome,
    EnrolmentRequest,
    EpisodeFacts,
    PilotArm,
    RescueLevel,
    ReviewVerdict,
    allocation_salt_sha256,
    claim_a,
    claim_b,
    compute_episode_key,
    cost_clause,
    human_intervention_clause,
    resolve_enrolment,
    stratum_id_for,
    validate_decision_family,
    worst_cell,
)
from tce_shared.pilot_thresholds import (
    P6_THRESHOLDS_EFFECTIVE_AT,
    P6_THRESHOLDS_SHA,
    P6_THRESHOLDS_VERSION,
)

from .task_state_store import run_cas_section

__all__ = [
    "PilotCorpus",
    "build_pilot_report",
    "PilotEpisodeConflict",
    "PilotEpisodeNotFound",
    "close_episode",
    "enroll_episode",
    "list_episodes",
    "load_episode",
    "load_report_corpus",
    "record_observation",
]


class PilotEpisodeNotFound(LookupError):
    """No episode with that id inside the caller's scope."""


class PilotEpisodeConflict(ValueError):
    """The episode already carries a close record.  ``UNIQUE(episode_id)`` says so."""


def cell_key(project_id: str | None, decision_family: str) -> str:
    """The report's cell axis, rendered as one string.  ``(project_id, decision_family)``.

    C10: the cell is the unit and nothing is ever averaged across two of them.  This function
    exists so the dispatch census can be sliced by exactly the same axis the claims are, rather
    than by a second, slightly different one.
    """

    return f"{project_id or ''}|{decision_family}"



# --------------------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------------------


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _as_datetime(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join("?" for _ in values) or "''"


def _allocation_from_row(row: Mapping[str, Any]) -> ArmAllocation:
    """Rebuild the frozen allocation from the stored row rather than re-deriving it.

    Re-deriving would be a second draw wearing the first one's name.  A repeat enrolment returns
    what was written, even if the salt has since changed.
    """
    return ArmAllocation(
        arm_id=PilotArm(str(row["arm_id"])),
        arm_class=str(row["arm_class"]),
        allocation_kind=AllocationKind(str(row["allocation_kind"])),
        stratum_id=str(row["stratum_id"]),
        slot=int(row["slot"]),
        block_ordinal=int(row["block_ordinal"]),
        block_position=int(row["block_position"]),
        arm_set_sha=str(row["arm_set_sha"] or ""),
        allocation_salt_sha256=str(row["allocation_salt_sha256"] or ""),
    )


_EPISODE_COLUMNS = """
    id, workspace_id, owner_id, subject_user_id, project_id, decision_family, session_id,
    objective_hash, cancel_epoch, episode_key, task_id, stratum_id, slot, block_ordinal,
    block_position, arm_id, arm_class, allocation_kind, arm_set_sha, allocation_salt_sha256,
    agent_principal, allocated_at, revealed_at, enrolled_before_execution, thresholds_sha,
    schema_version
"""


def _episode_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["id"] = str(item["id"])
    item["allocated_at"] = _as_datetime(item["allocated_at"])
    item["revealed_at"] = _optional_datetime(item.get("revealed_at"))
    item["enrolled_before_execution"] = bool(item.get("enrolled_before_execution", 1))
    return item


# --------------------------------------------------------------------------------------
# 1. Enrolment — pilot_strata first, pilot_episodes second
# --------------------------------------------------------------------------------------


def take_stratum_slot(
    conn: sqlite3.Connection,
    *,
    stratum_id: str,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    decision_family: str,
    allocation_salt: str,
    now: datetime,
) -> int:
    """The atomic counter.  One statement, and it is the only write this table ever takes.

    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` is SQLite >= 3.35.  It runs inside the
    caller's ``BEGIN IMMEDIATE`` bracket, so two concurrent enrolments serialise on the write lock
    rather than reading the same ``next_slot`` and both writing it back.
    """
    stamp = _iso(now)
    row = conn.execute(
        """
        INSERT INTO pilot_strata (
            stratum_id, workspace_id, subject_user_id, project_id, decision_family,
            arm_set_sha, allocation_salt_sha256, next_slot, created_at, updated_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'v1')
        ON CONFLICT(stratum_id) DO UPDATE
           SET next_slot = next_slot + 1,
               updated_at = excluded.updated_at
        RETURNING next_slot - 1 AS slot
        """,
        (
            stratum_id,
            workspace_id,
            subject_user_id,
            project_id,
            decision_family,
            ARM_SET_SHA,
            allocation_salt_sha256(allocation_salt),
            stamp,
            stamp,
        ),
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING on an upsert always yields a row
        raise RuntimeError("pilot_strata slot was not returned")
    return int(row["slot"])


def _load_episode_by_key(
    conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str, episode_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT {_EPISODE_COLUMNS}
        FROM pilot_episodes
        WHERE workspace_id = ? AND subject_user_id = ? AND episode_key = ?
        LIMIT 1
        """,
        (workspace_id, subject_user_id, episode_key),
    ).fetchone()
    return _episode_from_row(row) if row is not None else None


def _cancel_epoch_for(
    conn: sqlite3.Connection, *, workspace_id: str, owner_ids: Sequence[str], task_id: str
) -> int:
    """P2's ``last_cancel_seq``, the sixth component of ``episode_key``.  Absent means 0.

    ``task_id`` here is the SESSION's own task, never a task the caller named.  The body's
    ``task_id`` is stored on the row for the P2/P3 joins (§1.6, §2.3) and is read by nothing
    else: routing it into this lookup would put a caller-varied label inside ``episode_key``
    and hand back exactly the arm shopping of Proof 4 under a different field name.
    """
    if not task_id or not owner_ids:
        return 0
    row = conn.execute(
        f"""
        SELECT last_cancel_seq
        FROM task_states
        WHERE workspace_id = ? AND owner_id IN ({_placeholders(owner_ids)}) AND task_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (workspace_id, *owner_ids, task_id),
    ).fetchone()
    return int(row["last_cancel_seq"] or 0) if row is not None else 0


def enroll_episode(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    owner_ids: Sequence[str],
    subject_user_id: str,
    agent_principal: str,
    project_id: str | None,
    decision_family: str,
    session_id: str,
    objective_hash: str,
    task_id: str | None,
    elect_arm: PilotArm | None,
    allocation_salt: str,
    human_baseline_enabled: bool,
    thresholds_sha: str,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """Freeze an arm to a piece of work, before the work starts.  ``(episode_row, reused)``.

    The slot take and the episode INSERT run in one ``BEGIN IMMEDIATE`` section, in that order,
    so a crash between them cannot leave a consumed slot with no episode behind it.  A repeat
    enrolment consumes no slot at all: re-enrolling the same work must not perturb the randomised
    blocks it sits beside, which is the same reason an elected arm does not consume one either.
    """
    family = validate_decision_family(decision_family)
    # C4 — the epoch is the SESSION's, exactly as the turn path's ``policy_cancel_epoch`` is.
    # ``task_id`` from the body must never reach this lookup: it is caller-varied, and a
    # caller-varied field inside ``episode_key`` is an allocator a caller can shop.
    cancel_epoch = _cancel_epoch_for(conn, workspace_id=workspace_id, owner_ids=owner_ids, task_id=session_id)
    request = EnrolmentRequest(
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        decision_family=family,
        session_id=session_id,
        objective_hash=objective_hash or None,
        cancel_epoch=cancel_epoch,
        elect_arm=elect_arm,
    )
    key = compute_episode_key(
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        session_id=session_id,
        objective_hash=objective_hash or None,
        cancel_epoch=cancel_epoch,
    )
    stratum = stratum_id_for(
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        decision_family=family,
    )

    def _section() -> tuple[dict[str, Any], bool]:
        existing = _load_episode_by_key(
            conn, workspace_id=workspace_id, subject_user_id=subject_user_id, episode_key=key
        )
        if existing is not None:
            outcome = resolve_enrolment(
                request,
                existing=_allocation_from_row(existing),
                next_slot=None,
                salt=allocation_salt,
                human_baseline_enabled=human_baseline_enabled,
            )
            return existing, outcome.reused
        next_slot: int | None = None
        if elect_arm is None:
            # pilot_strata FIRST.  Never after pilot_episodes, in this function or any other.
            next_slot = take_stratum_slot(
                conn,
                stratum_id=stratum,
                workspace_id=workspace_id,
                subject_user_id=subject_user_id,
                project_id=project_id,
                decision_family=family,
                allocation_salt=allocation_salt,
                now=now,
            )
        outcome = resolve_enrolment(
            request,
            existing=None,
            next_slot=next_slot,
            salt=allocation_salt,
            human_baseline_enabled=human_baseline_enabled,
        )
        _insert_episode(
            conn,
            outcome=outcome,
            workspace_id=workspace_id,
            owner_id=owner_id,
            subject_user_id=subject_user_id,
            project_id=project_id,
            decision_family=family,
            session_id=session_id,
            objective_hash=objective_hash,
            cancel_epoch=cancel_epoch,
            task_id=task_id,
            agent_principal=agent_principal,
            thresholds_sha=thresholds_sha,
            now=now,
        )
        written = _load_episode_by_key(
            conn, workspace_id=workspace_id, subject_user_id=subject_user_id, episode_key=key
        )
        if written is None:  # pragma: no cover - the row cannot vanish inside the write lock
            raise RuntimeError("pilot episode was not persisted")
        return written, False

    return run_cas_section(conn, _section)


def _insert_episode(
    conn: sqlite3.Connection,
    *,
    outcome: EnrolmentOutcome,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    project_id: str | None,
    decision_family: str,
    session_id: str,
    objective_hash: str,
    cancel_epoch: int,
    task_id: str | None,
    agent_principal: str,
    thresholds_sha: str,
    now: datetime,
) -> None:
    """The freeze.  ``revealed_at`` is deliberately NULL: the row exists before anyone reads it."""
    allocation = outcome.allocation
    conn.execute(
        """
        INSERT OR IGNORE INTO pilot_episodes (
            id, workspace_id, owner_id, subject_user_id, project_id, decision_family,
            session_id, objective_hash, cancel_epoch, episode_key, task_id, stratum_id,
            slot, block_ordinal, block_position, arm_id, arm_class, allocation_kind,
            arm_set_sha, allocation_salt_sha256, agent_principal, allocated_at, revealed_at,
            enrolled_before_execution, thresholds_sha, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, 'v1')
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            owner_id,
            subject_user_id,
            project_id,
            decision_family,
            session_id,
            objective_hash,
            cancel_epoch,
            outcome.episode_key,
            task_id,
            allocation.stratum_id,
            allocation.slot,
            allocation.block_ordinal,
            allocation.block_position,
            allocation.arm_id.value,
            allocation.arm_class,
            allocation.allocation_kind.value,
            allocation.arm_set_sha,
            allocation.allocation_salt_sha256,
            agent_principal,
            _iso(now),
            thresholds_sha,
        ),
    )


def mark_revealed(conn: sqlite3.Connection, *, episode_ids: Sequence[str], now: datetime) -> None:
    """Stamp the first read of an arm.  Only ever NULL -> now, and it touches no other column.

    This is the whole of ``UPDATE pilot_episodes`` in this backend.  ``allocated_at`` strictly
    before ``revealed_at`` is what makes "frozen before it was revealed" an auditable fact rather
    than a claim in a runbook.
    """
    identifiers = [str(value) for value in episode_ids if str(value or "").strip()]
    if not identifiers:
        return
    conn.execute(
        f"""
        UPDATE pilot_episodes
           SET revealed_at = ?
         WHERE id IN ({_placeholders(identifiers)})
           AND revealed_at IS NULL
        """,
        (_iso(now), *identifiers),
    )
    conn.commit()


def load_episode(
    conn: sqlite3.Connection, *, workspace_id: str, owner_ids: Sequence[str], episode_id: str
) -> dict[str, Any]:
    if not owner_ids:
        raise PilotEpisodeNotFound(str(episode_id))
    row = conn.execute(
        f"""
        SELECT {_EPISODE_COLUMNS}
        FROM pilot_episodes
        WHERE id = ? AND workspace_id = ? AND owner_id IN ({_placeholders(owner_ids)})
        LIMIT 1
        """,
        (str(episode_id), workspace_id, *owner_ids),
    ).fetchone()
    if row is None:
        raise PilotEpisodeNotFound(str(episode_id))
    return _episode_from_row(row)


# --------------------------------------------------------------------------------------
# 2. Observations — agent_asserted, and structurally out of reach of every clause
# --------------------------------------------------------------------------------------


def record_observation(
    conn: sqlite3.Connection,
    *,
    episode: Mapping[str, Any],
    principal: str,
    agent_notes: str,
    agent_declared_steps: Sequence[str],
    agent_self_rated_difficulty: int | None,
    now: datetime,
) -> dict[str, Any]:
    """The executor's own account of its run.  Its own table, and no clause function reads it."""
    observation_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO pilot_episode_observations (
            id, episode_id, workspace_id, owner_id, principal, producer_class, agent_notes,
            agent_declared_steps_json, agent_self_rated_difficulty, observed_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, 'agent_asserted', ?, ?, ?, ?, 'v1')
        """,
        (
            str(observation_id),
            str(episode["id"]),
            str(episode["workspace_id"]),
            str(episode["owner_id"]),
            principal,
            agent_notes,
            json.dumps([str(step) for step in agent_declared_steps]),
            agent_self_rated_difficulty,
            _iso(now),
        ),
    )
    conn.commit()
    return {
        "observation_id": observation_id,
        "episode_id": uuid.UUID(str(episode["id"])),
        "producer_class": "agent_asserted",
        "read_by_any_gate": False,
        "observed_at": now,
    }


# --------------------------------------------------------------------------------------
# 3. The close — server-derived first, human-attested as the fallback
# --------------------------------------------------------------------------------------


def derive_completion_basis(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_ids: Sequence[str],
    task_id: str | None,
    finished: bool,
) -> CompletionBasis:
    """§1.6, in the stated order.  The human's answer is the third branch, never the first."""
    task = str(task_id or "").strip()
    if task and owner_ids:
        verified = conn.execute(
            f"""
            SELECT 1 FROM verification_results
            WHERE workspace_id = ? AND owner_id IN ({_placeholders(owner_ids)})
              AND task_id = ? AND verdict = 'pass'
            LIMIT 1
            """,
            (workspace_id, *owner_ids, task),
        ).fetchone()
        if verified is not None:
            return CompletionBasis.VERIFIED
        done = conn.execute(
            f"""
            SELECT 1 FROM task_states
            WHERE workspace_id = ? AND owner_id IN ({_placeholders(owner_ids)})
              AND task_id = ? AND status = 'done'
            LIMIT 1
            """,
            (workspace_id, *owner_ids, task),
        ).fetchone()
        if done is not None:
            return CompletionBasis.TASK_STATE_DONE
    return CompletionBasis.OWNER_ATTESTED if finished else CompletionBasis.UNFINISHED


def close_episode(
    conn: sqlite3.Connection,
    *,
    episode: Mapping[str, Any],
    owner_ids: Sequence[str],
    adjudicator_id: str,
    adjudicator_verified: bool,
    executed_arm: PilotArm,
    rescue_level: RescueLevel,
    finished: bool,
    review_verdict: ReviewVerdict,
    review_minutes: int,
    deviation_reason: str,
    unfinished_reason: str,
    grace_days: int,
    now: datetime,
) -> dict[str, Any]:
    """The adjudication.  One per episode, ``UNIQUE(episode_id)``, and a second one is a 409.

    ``late_close`` is a diagnostic and NEVER a refusal: refusing a late close would convert a
    finished episode into a coverage deficit that can never be repaired.
    """
    assigned = PilotArm(str(episode["arm_id"]))
    deviated = executed_arm is not assigned
    independent = str(adjudicator_id or "") != str(episode.get("agent_principal") or "")
    basis = derive_completion_basis(
        conn,
        workspace_id=str(episode["workspace_id"]),
        owner_ids=owner_ids,
        task_id=episode.get("task_id"),
        finished=finished,
    )
    allocated_at = _as_datetime(episode["allocated_at"])
    late = (now - allocated_at) > timedelta(days=max(0, int(grace_days)))
    close_id = uuid.uuid4()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO pilot_episode_closes (
            id, episode_id, workspace_id, owner_id, adjudicator_id, adjudicator_verified,
            adjudication_independent, executed_arm, deviated, deviation_reason, rescue_level,
            finished, completion_basis, review_minutes, review_verdict, unfinished_reason,
            late_close, closed_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            str(close_id),
            str(episode["id"]),
            str(episode["workspace_id"]),
            str(episode["owner_id"]),
            adjudicator_id,
            int(bool(adjudicator_verified)),
            int(independent),
            executed_arm.value,
            int(deviated),
            deviation_reason,
            rescue_level.value,
            int(bool(finished)),
            basis.value,
            max(0, int(review_minutes)),
            review_verdict.value,
            unfinished_reason,
            int(late),
            _iso(now),
        ),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        raise PilotEpisodeConflict(str(episode["id"]))
    conn.commit()
    return {
        "close_id": close_id,
        "episode_id": uuid.UUID(str(episode["id"])),
        "deviated": deviated,
        "adjudication_independent": independent,
        "adjudicator_verified": bool(adjudicator_verified),
        "completion_basis": basis,
        "late_close": late,
        "closed_at": now,
    }


# --------------------------------------------------------------------------------------
# 4. Reading the ledger
# --------------------------------------------------------------------------------------


def list_episodes(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_ids: Sequence[str],
    subject_user_id: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The enrolment ledger, newest first, with the close record folded in where one exists."""
    if not owner_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT e.id, e.episode_key, e.project_id, e.decision_family, e.arm_id, e.arm_class,
               e.allocation_kind, e.block_ordinal, e.slot, e.allocated_at, e.revealed_at,
               c.closed_at, c.executed_arm, c.deviated, c.rescue_level, c.completion_basis,
               c.review_verdict, c.late_close
        FROM pilot_episodes e
        LEFT JOIN pilot_episode_closes c ON c.episode_id = e.id
        WHERE e.workspace_id = ? AND e.owner_id IN ({_placeholders(owner_ids)})
          AND e.subject_user_id = ?
        ORDER BY e.allocated_at DESC
        LIMIT ?
        """,
        (workspace_id, *owner_ids, subject_user_id, max(1, int(limit))),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["id"] = str(item["id"])
        item["allocated_at"] = _as_datetime(item["allocated_at"])
        item["revealed_at"] = _optional_datetime(item.get("revealed_at"))
        item["closed_at"] = _optional_datetime(item.get("closed_at"))
        item["deviated"] = bool(item.get("deviated"))
        item["late_close"] = bool(item.get("late_close"))
        out.append(item)
    return out


@dataclass(frozen=True, slots=True)
class PilotCorpus:
    """Everything the report reads, and nothing that a clause may not read.

    There is no field here fed by ``pilot_episode_observations``.  The corpus type is where the
    "no gate clause reads an agent assertion" rule stops being a convention: the diagnostics have
    nowhere to arrive.
    """

    episodes: list[EpisodeFacts]
    enrolled_total: int
    strata_total: int
    dispatch_rows: int = 0
    cost_known_rows: int = 0
    cost_buckets: dict[str, int] = field(default_factory=dict)
    intervention_rows: int = 0
    intervention_sum: int = 0
    intervention_known_rows: int = 0
    completion_buckets: dict[str, int] = field(default_factory=dict)
    review_minutes: list[int] = field(default_factory=list)
    review_verdicts: dict[str, int] = field(default_factory=dict)
    rescue_levels: dict[str, int] = field(default_factory=dict)
    cell_dispatch: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _census(self, key: str | None) -> dict[str, Any]:
        """The dispatch census for one cell, or for the whole corpus when ``key`` is None."""
        if key is None:
            return {
                "rows": self.dispatch_rows,
                "cost_known": self.cost_known_rows,
                "intervention_sum": self.intervention_sum,
                "intervention_known": self.intervention_known_rows,
                "buckets": dict(self.cost_buckets),
            }
        return self.cell_dispatch.get(
            key,
            {"rows": 0, "cost_known": 0, "intervention_sum": 0, "intervention_known": 0, "buckets": {}},
        )

    def render_cost_clause(self, key: str | None = None) -> Clause:
        """A zero that means *we do not know* must never render as a zero that means *free*."""
        census = self._census(key)
        buckets = dict(census.get("buckets") or {})
        detail = (
            f"buckets provider_reported={buckets.get('provider_reported', 0)} "
            f"estimated={buckets.get('estimated', 0)} "
            f"unavailable={buckets.get('unavailable', 0)}; "
            f"cost_known={int(census['cost_known'])}/{int(census['rows'])} "
            f"dispatch_rows_matched={int(census['rows'])}"
        )
        return cost_clause(
            cost_known=int(census["cost_known"]), episodes=int(census["rows"]), detail=detail
        )

    def render_intervention_clause(self, key: str | None = None) -> Clause:
        """``human_intervention_count``'s first aggregator in this repository.  Not rescue.

        ``known`` counts dispatch rows carrying a POSITIVE count, not rows that exist.  The column
        is ``NOT NULL DEFAULT 0``, so a stored 0 is indistinguishable from *never measured* — and
        on ``codex/app-server`` with approvals set to ``never`` every row is structurally 0.
        Counting those as known would make this clause pass on the strength of a column nothing
        was writing, which is the same vacuous pass §0.9 deletes elsewhere.
        """
        census = self._census(key)
        detail = (
            f"sum={int(census['intervention_sum'])} "
            f"intervention_known={int(census['intervention_known'])}/{int(census['rows'])}; "
            "a stored 0 is not a measurement — codex/app-server reports 0 structurally "
            "(adapters/codex_app_server.py)"
        )
        return human_intervention_clause(
            known=int(census["intervention_known"]), episodes=int(census["rows"]), detail=detail
        )


def _facts_from_rows(rows: Sequence[sqlite3.Row]) -> list[EpisodeFacts]:
    facts: list[EpisodeFacts] = []
    for row in rows:
        item = dict(row)
        close: CloseFacts | None = None
        if item.get("closed_at") not in (None, ""):
            close = CloseFacts(
                adjudication_independent=bool(item.get("adjudication_independent")),
                executed_arm=PilotArm(str(item["executed_arm"])),
                deviated=bool(item.get("deviated")),
                rescue_level=RescueLevel(str(item["rescue_level"])),
                completion_basis=CompletionBasis(str(item["completion_basis"])),
                review_verdict=ReviewVerdict(str(item["review_verdict"])),
                closed_at=_as_datetime(item["closed_at"]),
                review_minutes=int(item["review_minutes"] or 0),
                late_close=bool(item.get("late_close")),
            )
        facts.append(
            EpisodeFacts(
                episode_id=str(item["id"]),
                project_id=(str(item["project_id"]) if item.get("project_id") else None),
                decision_family=str(item["decision_family"]),
                arm_id=PilotArm(str(item["arm_id"])),
                arm_class=str(item["arm_class"]),
                allocation_kind=AllocationKind(str(item["allocation_kind"])),
                stratum_id=str(item["stratum_id"]),
                slot=int(item["slot"]),
                block_ordinal=int(item["block_ordinal"]),
                allocation_salt_sha256=str(item["allocation_salt_sha256"] or ""),
                arm_set_sha=str(item["arm_set_sha"] or ""),
                allocated_at=_as_datetime(item["allocated_at"]),
                close=close,
            )
        )
    return facts


def load_report_corpus(
    conn: sqlite3.Connection, *, workspace_id: str, owner_ids: Sequence[str], subject_user_id: str
) -> PilotCorpus:
    """Every enrolled episode in scope, with its close, plus the cost and intervention census."""
    if not owner_ids:
        return PilotCorpus(episodes=[], enrolled_total=0, strata_total=0)
    rows = conn.execute(
        f"""
        SELECT e.id, e.project_id, e.decision_family, e.arm_id, e.arm_class, e.allocation_kind,
               e.stratum_id, e.slot, e.block_ordinal, e.allocation_salt_sha256, e.arm_set_sha,
               e.allocated_at, e.session_id,
               c.adjudication_independent, c.executed_arm, c.deviated, c.rescue_level,
               c.completion_basis, c.review_verdict, c.review_minutes, c.late_close, c.closed_at
        FROM pilot_episodes e
        LEFT JOIN pilot_episode_closes c ON c.episode_id = e.id
        WHERE e.workspace_id = ? AND e.owner_id IN ({_placeholders(owner_ids)})
          AND e.subject_user_id = ?
        ORDER BY e.allocated_at ASC
        """,
        (workspace_id, *owner_ids, subject_user_id),
    ).fetchall()
    facts = _facts_from_rows(rows)

    strata_row = conn.execute(
        "SELECT COUNT(*) AS n FROM pilot_strata WHERE workspace_id = ? AND subject_user_id = ?",
        (workspace_id, subject_user_id),
    ).fetchone()

    sessions = sorted({str(dict(row)["session_id"] or "") for row in rows if str(dict(row)["session_id"] or "")})
    dispatch = _dispatch_census(conn, workspace_id=workspace_id, session_ids=sessions)
    # Sliced by exactly the cell axis the claims use, so "cost per cell" and "claim per cell"
    # cannot quietly mean two different groupings.
    per_cell: dict[str, set[str]] = {}
    for raw in rows:
        item = dict(raw)
        session = str(item["session_id"] or "")
        if not session:
            continue
        key = cell_key(
            str(item["project_id"]) if item.get("project_id") else None, str(item["decision_family"])
        )
        per_cell.setdefault(key, set()).add(session)
    cell_dispatch = {
        key: _dispatch_census(conn, workspace_id=workspace_id, session_ids=sorted(values))
        for key, values in per_cell.items()
    }

    completion: dict[str, int] = dict.fromkeys((basis.value for basis in CompletionBasis), 0)
    verdicts: dict[str, int] = dict.fromkeys((verdict.value for verdict in ReviewVerdict), 0)
    rescues: dict[str, int] = dict.fromkeys((level.value for level in RescueLevel), 0)
    minutes: list[int] = []
    for row in rows:
        item = dict(row)
        if item.get("closed_at") in (None, ""):
            continue
        completion[str(item["completion_basis"])] += 1
        verdicts[str(item["review_verdict"])] += 1
        rescues[str(item["rescue_level"])] += 1
        minutes.append(int(item["review_minutes"] or 0))

    return PilotCorpus(
        episodes=facts,
        enrolled_total=len(facts),
        strata_total=int(dict(strata_row)["n"]) if strata_row is not None else 0,
        dispatch_rows=dispatch["rows"],
        cost_known_rows=dispatch["cost_known"],
        cost_buckets=dispatch["buckets"],
        intervention_rows=dispatch["rows"],
        intervention_sum=dispatch["intervention_sum"],
        intervention_known_rows=dispatch["intervention_known"],
        completion_buckets=completion,
        review_minutes=minutes,
        review_verdicts=verdicts,
        rescue_levels=rescues,
        cell_dispatch=cell_dispatch,
    )


def _dispatch_census(
    conn: sqlite3.Connection, *, workspace_id: str, session_ids: Sequence[str]
) -> dict[str, Any]:
    """``cost_minor_units``, ``cost_source`` and ``human_intervention_count`` acquire a reader.

    ``human_intervention_count`` counts approval callbacks inside one supervised run.  It is NOT
    rescue and P6 never renames it; on ``codex/app-server`` with approvals set to ``never`` it is
    structurally zero, a zero that means *not measured*.
    """
    empty: dict[str, Any] = {
        "rows": 0,
        "cost_known": 0,
        "intervention_sum": 0,
        "intervention_known": 0,
        "buckets": {"provider_reported": 0, "estimated": 0, "unavailable": 0},
    }
    if not session_ids:
        return empty
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS rows_matched,
               COALESCE(SUM(CASE WHEN cost_source = 'provider_reported' THEN 1 ELSE 0 END), 0) AS provider_reported,
               COALESCE(SUM(CASE WHEN cost_source = 'estimated' THEN 1 ELSE 0 END), 0) AS estimated,
               COALESCE(SUM(CASE WHEN cost_source IS NULL OR cost_source = '' OR cost_source = 'unavailable'
                                 THEN 1 ELSE 0 END), 0) AS unavailable,
               COALESCE(SUM(COALESCE(human_intervention_count, 0)), 0) AS intervention_sum,
               COALESCE(SUM(CASE WHEN COALESCE(human_intervention_count, 0) > 0 THEN 1 ELSE 0 END), 0)
                   AS intervention_known
        FROM dispatch_records
        WHERE workspace_id = ? AND session_id IN ({_placeholders(session_ids)})
        """,
        (workspace_id, *session_ids),
    ).fetchone()
    if row is None:  # pragma: no cover - an aggregate always returns one row
        return empty
    item = dict(row)
    provider = int(item["provider_reported"])
    estimated = int(item["estimated"])
    return {
        "rows": int(item["rows_matched"]),
        "cost_known": provider + estimated,
        "intervention_sum": int(item["intervention_sum"]),
        "intervention_known": int(item["intervention_known"]),
        "buckets": {
            "provider_reported": provider,
            "estimated": estimated,
            "unavailable": int(item["unavailable"]),
        },
    }


# --------------------------------------------------------------------------------------
# 5. Dreams — NOT here.
#
# Relevance adjudication lives in ``dream_adjudication.py`` (the arithmetic) and
# ``dream_adjudication_store.py`` (the two backends).  An earlier draft of this file carried its
# own copy; two implementations of "was this proposal relevant" is exactly the fork the brief
# forbids, and theirs is the one with the closed vocabulary, the superseding read and its own
# tests.  ``GET /v1/pilot/report`` calls ``dream_counts_for_scope`` -> ``dream_block`` and hands
# the payload to ``build_pilot_report`` as ``dreams``.
# --------------------------------------------------------------------------------------


def _clause_model(clause: Clause) -> PilotClause:
    payload = clause.to_payload()
    return PilotClause(
        name=str(payload["name"]),
        state=clause.state,
        measured=(float(payload["measured"]) if payload["measured"] is not None else None),
        floor=(float(payload["floor"]) if payload["floor"] is not None else None),
        comparison=str(payload["comparison"]),
        reason=str(payload["reason"]),
        detail=str(payload["detail"]),
        rendered=str(payload["rendered"]),
    )


def build_pilot_report(corpus: PilotCorpus, *, dreams: dict[str, Any], now: datetime) -> PilotReportResponse:
    """One computation, two renderers.  ``GET /v1/pilot/report`` and the script share this.

    Three things this function will not do, each of them a defect it exists to prevent:

    * **It never pools.** One ``claim_a`` per ``(project_id, decision_family)`` cell, no mean
      across cells, and the summary line names the WORST cell rather than the average of them.
      ``POOLING REFUSED`` is printed on every run, not only when a caller asks for a pool.
    * **It never reports a zero as a measurement.** Every clause is PASS, SHORTFALL(measured,
      floor) or NOT_COMPUTABLE(reason).  At n=0 the answer is the third, never the first.
    * **It grants nothing.** It is read-only, it writes no qualification record, and no
      promotion, exposure or personalization path reads it.

    ``arm_availability`` is reported as ``unknown`` for every arm on purpose: the API cannot see
    which runtime binaries exist on the host it is asked about, and a clause that read a guess
    would be the same defect as a gate that passes vacuously.
    """
    cells: dict[tuple[str | None, str], list[EpisodeFacts]] = {}
    for episode in corpus.episodes:
        cells.setdefault((episode.project_id, episode.decision_family), []).append(episode)

    verdicts = [claim_a(members, now=now) for members in cells.values()]
    worst = worst_cell(verdicts)
    b_verdict = claim_b(corpus.episodes)

    reports: list[PilotCellReport] = []
    for verdict in sorted(verdicts, key=lambda v: (str(v.project_id or ""), v.decision_family)):
        key = cell_key(verdict.project_id, verdict.decision_family)
        reports.append(
            PilotCellReport(
                project_id=verdict.project_id,
                decision_family=verdict.decision_family,
                supported=verdict.supported,
                clauses=[_clause_model(clause) for clause in verdict.clauses],
                shortfalls=list(verdict.shortfalls),
                exclusions=dict(verdict.exclusions),
                enrolled=verdict.enrolled,
                closed=verdict.closed,
                closed_by_arm=dict(verdict.closed_by_arm),
                success_by_arm=dict(verdict.success_by_arm),
                as_treated_by_arm=dict(verdict.as_treated_by_arm),
                complete_blocks=verdict.complete_blocks,
                active_days=verdict.active_days,
                calendar_days=verdict.calendar_days,
                comparisons=dict(verdict.comparisons),
                cost=_clause_model(corpus.render_cost_clause(key)),
                human_intervention=_clause_model(corpus.render_intervention_clause(key)),
            )
        )

    worst_report: PilotCellReport | None = None
    if worst is not None:
        wanted = (worst.project_id, worst.decision_family)
        worst_report = next(
            (item for item in reports if (item.project_id, item.decision_family) == wanted), None
        )

    notices = [SINGLE_PARTICIPANT_SENTENCE, b_verdict.sentence]
    if not corpus.episodes:
        notices.insert(0, NO_ENROLLED_EPISODES_SENTENCE)
    notices.append(
        "arm_runnable is reported as unknown: the API cannot observe which runtime binaries exist "
        "on the host, and no clause reads an unobserved capability as a pass."
    )
    notices.append(
        "Producer classes: server_derived and human_attested are read by clauses; agent_asserted "
        "(pilot_episode_observations) is diagnostic and is read by none of them."
    )

    return PilotReportResponse(
        generated_at=now,
        thresholds_version=P6_THRESHOLDS_VERSION,
        thresholds_sha=P6_THRESHOLDS_SHA,
        thresholds_effective_at=P6_THRESHOLDS_EFFECTIVE_AT,
        single_participant_notice=SINGLE_PARTICIPANT_SENTENCE,
        pooling_refused=POOLING_REFUSED_SENTENCE,
        success_definition=SUCCESS_DEFINITION,
        enrolled_total=corpus.enrolled_total,
        strata_total=corpus.strata_total,
        arms_enabled=[arm.value for arm in RANDOMIZED_ARMS],
        arm_availability=dict.fromkeys((arm.value for arm in RANDOMIZED_ARMS), "unknown"),
        cells=reports,
        worst_cell=worst_report,
        claim_b=PilotClaimBReport(
            state=b_verdict.state.value,
            clause=_clause_model(b_verdict.clause),
            closed=b_verdict.closed,
            randomized=b_verdict.randomized,
            elected=b_verdict.elected,
            sentence=b_verdict.sentence,
        ),
        cost=_clause_model(corpus.render_cost_clause(None)),
        human_intervention=_clause_model(corpus.render_intervention_clause(None)),
        dreams=dict(dreams),
        notices=notices,
    )
