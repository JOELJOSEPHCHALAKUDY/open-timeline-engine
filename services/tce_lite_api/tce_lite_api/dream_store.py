"""Dream proposals for Lite (P5 §1.4, §2, §3.1, §4, §5.2).

The Lite twin of ``tce_api.dream_store``: the same twenty function names with the same
keyword-only signatures, so a reader can diff the two files and the two backends cannot
answer a route differently.  What differs is only the driver — ``sqlite3.Connection`` and
``?`` params in place of a SQLAlchemy ``Session`` and named binds, ``json_extract`` in place
of ``->>``, ``cursor.rowcount`` in place of ``RETURNING``, and ``json.loads`` in place of a
decrypt (Lite stores event payloads in plaintext; ``db.py`` has no encrypted branch).

Three properties are structural rather than intended, and each is worth naming here because
each replaces a defect that shipped:

* **Refresh can never delete.**  There is no ``DELETE`` statement in this module, the only
  ``UPDATE dream_proposals`` is the compare-and-swap and it always carries
  ``AND revision = ?``, and no code path reaches an existing proposal's row except through
  :func:`apply_dream_events`.  The behaviour being replaced was one unconditional
  ``DELETE FROM autonomy_goals`` on every refresh.

* **The receipt is the only admission.**  :func:`select_candidate_messages` joins ``events``
  to ``trusted_input_receipts`` and admits nothing else.  There is no ``task_type`` arm and no
  ``_tce_owner`` arm, because both are executor-writable: an executor holding an ordinary API
  token can POST a fabricated "owner message", and a system that then quotes it back to the
  owner as something he said has forged his words with his own machine.

* **``BEGIN IMMEDIATE`` is still issued in exactly one place.**  This module imports
  :func:`run_cas_section` from ``task_state_store`` rather than opening its own bracket, and
  the bracket covers only the read-then-write section.

Nothing here commits except through ``run_cas_section``, which is the CAS section's own
boundary.  Lock order (§0.11): ``task_states`` before ``dream_proposals``, and the generation
run slot before any proposal is minted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.aspirations import (
    ADMISSIBLE_ORIGIN_KINDS,
    DREAM_POLICY_REVISION,
    DREAM_SCHEMA_VERSION,
    LIVE_STATUSES,
    QUOTABLE_ORIGIN_KIND,
    SCOPE_KIND_PROJECT,
    SCOPE_KIND_WORKSPACE,
    DreamCandidate,
    DreamCitation,
    DreamProposalEvent,
    DreamProposalEventKind,
    DreamProposalProjection,
    DreamProposalStatus,
    DreamProposalWrite,
    DreamRevisionConflict,
    DreamTransitionRefused,
    NonresponseState,
    PoolMessage,
    citations_from_json,
    citations_to_json,
    dedupe_by_content_sha256,
    evidence_basis_for,
    is_harness_text,
    nonresponse_state,
    normalise_for_containment,
    prepare_dream_write,
    quote_is_contained,
    resurface_interval_hours,
    surfaced_event,
    theme_tokens_for,
    transition_allowed,
    voice_violations,
)
from tce_shared.decision_capture import evidence_revision
from tce_shared.plan_decomposition import MAX_PLAN_STEPS
from tce_shared.scope import ResolvedScope
from tce_shared.takeover import objective_hash
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DECOMPOSE,
    PLANNING_PRODUCER_DETERMINISTIC,
    TASK_STATE_POLICY_REVISION,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatus,
    approved_plan_id,
    canonical_json,
    charter_for_task,
    charter_to_json,
    deterministic_plan,
    objective_contract_revision,
    plan_input_revision,
    plan_steps_to_json,
    task_scope_digest,
)

from .plan_rows import write_plan_rows
from .planning_store import enqueue_planning_job
from .task_state_store import (
    apply_task_state_events,
    ensure_task_state,
    load_task_state,
    run_cas_section,
)

# The ceiling a citation must stay under to remain showable.  Derived the same way the pool
# filter derives it (``block_sensitivity - 1``) but frozen here rather than read from a
# setting: a caller who could raise the ceiling at validation time could show a quote the
# pool filter would never have admitted, which is the ceiling defeating itself.
CITATION_MAX_SENSITIVITY: int = 2

# Generation-run lifecycle states.  ``running`` is the one the unique partial index keys on.
RUN_STATE_RUNNING: str = "running"
RUN_STATE_SUCCEEDED: str = "succeeded"
RUN_STATE_REFUSED: str = "refused"
RUN_STATE_FAILED: str = "failed"


# --------------------------------------------------------------------------------------
# D22 — one scope predicate, every read and every write
# --------------------------------------------------------------------------------------


def _dream_scope_sql() -> str:
    """The ONE ``WHERE`` fragment every ``dream_proposals`` statement in this module uses.

    ``subject_user_id`` is in it, and that is the point: on a live stack one ``owner_id``
    spans several subjects, so a predicate keyed on ``(workspace_id, owner_id, id)`` alone
    lets a verified human read *and accept* another subject's proposal.

    It is deliberately not the lenient ``(project_id = ? OR project_id IS NULL)`` shape the
    handoff predicate uses.  A workspace proposal is built from receipts carrying no project
    and is not about any project; leaking it into a project list is a mis-attribution, not a
    convenience.
    """
    return (
        "workspace_id = ?\n"
        "       AND owner_id = ?\n"
        "       AND subject_user_id = ?\n"
        "       AND (   (? = 1 AND scope_kind = 'project'   AND project_id = ?)\n"
        "            OR (? = 0 AND scope_kind = 'workspace' AND project_id IS NULL))"
    )


def dream_scope_kind(scope: ResolvedScope) -> str:
    """D9.  Derived, never asserted — there is no ``scope_kind`` field on any request body."""
    return SCOPE_KIND_PROJECT if (scope.is_bound() and scope.project_id) else SCOPE_KIND_WORKSPACE


def _dream_scope_params(scope: ResolvedScope) -> tuple[Any, ...]:
    """The six positional binds :func:`_dream_scope_sql` consumes, in order."""
    bound = 1 if dream_scope_kind(scope) == SCOPE_KIND_PROJECT else 0
    return (
        scope.workspace_id,
        scope.owner_id,
        scope.subject_user_id,
        bound,
        scope.project_id or "",
        bound,
    )


def _scope_key(scope_kind: str, project_id: str | None) -> str:
    return f"{scope_kind}:{project_id or ''}"


def _proposals_sql(columns: str, suffix: str = "") -> str:
    """Every ``dream_proposals`` statement in this module, assembled from one predicate.

    Assembled by joining fragments rather than by interpolating into one f-string, and that is
    not a style choice: the ownership gate scans every string literal in this file for one that
    names ``dream_proposals`` and ``WHERE`` together without ``subject_user_id``, because that
    is exactly the shape of a hand-rolled predicate that leaks across subjects.  A literal
    holding half the statement cannot be mistaken for the whole of one.
    """
    return " ".join(part for part in ("SELECT", columns, "FROM dream_proposals", "WHERE", _dream_scope_sql(), suffix) if part)


# --------------------------------------------------------------------------------------
# Small coercions
# --------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


def _str_list(raw: Any) -> tuple[str, ...]:
    parsed = _loads(raw, [])
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if str(item))


def _counter(raw: Any) -> dict[str, int]:
    parsed = _loads(raw, {})
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Row <-> projection
# --------------------------------------------------------------------------------------


def _projection_from_row(row: sqlite3.Row) -> DreamProposalProjection:
    """The stored row IS the fold.  Every field is coerced; a row an older build wrote degrades."""
    try:
        status = DreamProposalStatus(str(row["status"] or "proposed"))
    except ValueError:
        status = DreamProposalStatus.PROPOSED
    try:
        nonresponse = NonresponseState(str(row["nonresponse_state"] or "never_surfaced"))
    except ValueError:
        nonresponse = NonresponseState.NEVER_SURFACED
    project_id = str(row["project_id"]) if row["project_id"] else None
    return DreamProposalProjection(
        proposal_id=str(row["id"]),
        workspace_id=str(row["workspace_id"] or ""),
        owner_id=str(row["owner_id"] or ""),
        subject_user_id=str(row["subject_user_id"] or ""),
        project_id=project_id,
        scope_kind=str(row["scope_kind"] or SCOPE_KIND_PROJECT),
        session_id=str(row["session_id"] or ""),
        revision=int(row["revision"] or 0),
        status=status,
        nonresponse=nonresponse,
        surfaced_count=int(row["surfaced_count"] or 0),
        surfaced_attested=bool(row["surfaced_attested"]),
        first_surfaced_at=_parse_dt(row["first_surfaced_at"]),
        last_surfaced_at=_parse_dt(row["last_surfaced_at"]),
        title=str(row["title"] or ""),
        connection_text=str(row["connection_text"] or ""),
        benefit_text=str(row["benefit_text"] or ""),
        first_step=str(row["first_step"] or ""),
        citations=citations_from_json(_loads(row["citations_json"], [])),
        theme_tokens=_str_list(row["theme_tokens_json"]),
        evidence_basis=str(row["evidence_basis"] or ""),
        evidence_revision=str(row["evidence_revision"] or ""),
        evidence_cutoff_at=_parse_dt(row["evidence_cutoff_at"]),
        supersedes_proposal_id=(str(row["supersedes_proposal_id"]) if row["supersedes_proposal_id"] else None),
        repropose_depth=int(row["repropose_depth"] or 0),
        snooze_until=_parse_dt(row["snooze_until"]),
        expires_at=_parse_dt(row["expires_at"]),
        accepted_at=_parse_dt(row["accepted_at"]),
        rejected_at=_parse_dt(row["rejected_at"]),
        rejection_reason=str(row["rejection_reason"] or ""),
        task_id=(str(row["task_id"]) if row["task_id"] else None),
        objective_hash=(str(row["objective_hash"]) if row["objective_hash"] else None),
        plan_root_goal_id=(str(row["plan_root_goal_id"]) if row["plan_root_goal_id"] else None),
        pursuit_started_at=_parse_dt(row["pursuit_started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
        abandoned_at=_parse_dt(row["abandoned_at"]),
        abandon_reason=str(row["abandon_reason"] or ""),
        withdrawn_reason=str(row["withdrawn_reason"] or ""),
        schema_version=str(row["schema_version"] or DREAM_SCHEMA_VERSION),
        policy_revision=str(row["policy_revision"] or DREAM_POLICY_REVISION),
    )


def _event_from_row(row: sqlite3.Row) -> DreamProposalEvent:
    return DreamProposalEvent(
        seq=int(row["seq"] or 0),
        kind=str(row["kind"] or ""),
        payload=_loads(row["payload_json"], {}) or {},
        occurred_at=_parse_dt(row["occurred_at"]) or datetime(1970, 1, 1, tzinfo=UTC),
        actor=str(row["actor"] or ""),
        actor_class=str(row["actor_class"] or "system"),
        source_event_id=(str(row["source_event_id"]) if row["source_event_id"] else None),
        run_id=(str(row["run_id"]) if row["run_id"] else None),
    )


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------


def load_proposal(
    conn: sqlite3.Connection, *, scope: ResolvedScope, proposal_id: str
) -> tuple[DreamProposalProjection, int, str] | None:
    """``(projection, highest_seq, source_revision)`` or ``None``.  Scoped by the one predicate."""
    row = conn.execute(
        _proposals_sql("*", "AND id = ?"),
        (*_dream_scope_params(scope), str(proposal_id)),
    ).fetchone()
    if row is None:
        return None
    return _projection_from_row(row), int(row["highest_seq"] or 0), str(row["source_revision"] or "")


def load_proposal_events(conn: sqlite3.Connection, *, proposal_id: str) -> list[DreamProposalEvent]:
    rows = conn.execute(
        "SELECT * FROM dream_proposal_events WHERE proposal_id = ? ORDER BY seq ASC",
        (str(proposal_id),),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def load_live_proposals(
    conn: sqlite3.Connection, *, scope: ResolvedScope, limit: int
) -> list[DreamProposalProjection]:
    marks = ", ".join("?" for _ in sorted(LIVE_STATUSES))
    rows = conn.execute(
        _proposals_sql("*", "AND status IN (" + marks + ") ORDER BY updated_at DESC LIMIT ?"),
        (*_dream_scope_params(scope), *sorted(LIVE_STATUSES), max(1, int(limit))),
    ).fetchall()
    return [_projection_from_row(row) for row in rows]


def load_rejected_proposals(
    conn: sqlite3.Connection, *, scope: ResolvedScope, since: datetime, limit: int
) -> list[DreamProposalProjection]:
    """Every rejection in the window, across BOTH scope kinds for this subject.

    There is no ``scope_kind`` parameter and that is deliberate: a theme rejected while
    working unbound is the same theme when it comes back inside a project.  Bucketing the
    scan by scope kind is how a rejection became invisible to the only mode that could
    currently produce anything.
    """
    rows = conn.execute(
        """
        SELECT * FROM dream_proposals
         WHERE workspace_id = ? AND owner_id = ? AND subject_user_id = ?
           AND status = 'rejected'
           AND rejected_at IS NOT NULL
           AND rejected_at >= ?
         ORDER BY rejected_at DESC
         LIMIT ?
        """,
        (
            scope.workspace_id,
            scope.owner_id,
            scope.subject_user_id,
            _iso(since),
            max(1, int(limit)),
        ),
    ).fetchall()
    return [_projection_from_row(row) for row in rows]


def list_proposals(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    statuses: Sequence[str],
    include_history: bool,
    now: datetime,
    limit: int,
) -> list[DreamProposalProjection]:
    """The list route's read.  Hides a past-TTL row the sweep has not reached yet (D4).

    Writes nothing.  The withdrawal of a proposal whose citations no longer verify belongs to
    :func:`sweep_dream_proposals`, which is the named producer of ``withdrawn``; a GET that
    wrote would be the kind of thing that gets cached six months later and silently stops.
    """
    wanted = [str(item) for item in statuses if str(item)]
    if not wanted:
        wanted = sorted(LIVE_STATUSES)
    marks = ", ".join("?" for _ in wanted)
    suffix = "AND status IN (" + marks + ")"
    params: list[Any] = [*_dream_scope_params(scope), *wanted]
    if not include_history:
        suffix += " AND (expires_at IS NULL OR expires_at > ? OR status IN ('accepted', 'pursued'))"
        params.append(_iso(now))
    suffix += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    sql = _proposals_sql("*", suffix)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_projection_from_row(row) for row in rows]


# --------------------------------------------------------------------------------------
# §1.4 — the CAS write.  The only statement in the tree that changes a proposal row.
# --------------------------------------------------------------------------------------

_EVENT_COLUMNS = (
    "id",
    "proposal_id",
    "workspace_id",
    "owner_id",
    "seq",
    "kind",
    "payload_json",
    "actor",
    "actor_class",
    "source_event_id",
    "run_id",
    "occurred_at",
    "schema_version",
)


def _insert_events(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    workspace_id: str,
    owner_id: str,
    events: Sequence[DreamProposalEvent],
) -> None:
    statement = "INSERT OR IGNORE INTO dream_proposal_events ({cols}) VALUES ({marks})".format(
        cols=", ".join(_EVENT_COLUMNS),
        marks=", ".join("?" for _ in _EVENT_COLUMNS),
    )
    for event in events:
        conn.execute(
            statement,
            (
                str(uuid.uuid4()),
                str(proposal_id),
                workspace_id,
                owner_id,
                int(event.seq),
                str(event.kind),
                canonical_json(dict(event.payload)),
                str(event.actor or ""),
                str(event.actor_class or "system"),
                event.source_event_id,
                event.run_id,
                (_iso(event.occurred_at) or ""),
                DREAM_SCHEMA_VERSION,
            ),
        )


def _cas_update(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    write: DreamProposalWrite,
    prior_highest_seq: int,
    now: datetime,
) -> bool:
    """The one ``UPDATE dream_proposals`` in Lite, and it always carries ``AND revision = ?``."""
    projection = write.projection
    next_highest_seq = max([prior_highest_seq, *(int(event.seq) for event in write.events)])
    cursor = conn.execute(
        """
        UPDATE dream_proposals
           SET revision = ?, highest_seq = ?,
               status = ?, nonresponse_state = ?,
               surfaced_count = ?, surfaced_attested = ?,
               first_surfaced_at = ?, last_surfaced_at = ?,
               title = ?, connection_text = ?, benefit_text = ?, first_step = ?,
               citations_json = ?, citation_count = ?,
               evidence_basis = ?, evidence_revision = ?, evidence_cutoff_at = ?,
               theme_tokens_json = ?,
               supersedes_proposal_id = ?, repropose_depth = ?,
               snooze_until = ?, expires_at = ?,
               accepted_at = ?, rejected_at = ?, rejection_reason = ?,
               task_id = ?, objective_hash = ?, plan_root_goal_id = ?,
               pursuit_started_at = ?, completed_at = ?, abandoned_at = ?,
               abandon_reason = ?, withdrawn_reason = ?,
               source_revision = ?, updated_at = ?
         WHERE id = ? AND revision = ?
        """,
        (
            int(write.next_revision),
            int(next_highest_seq),
            str(projection.status),
            str(projection.nonresponse),
            int(projection.surfaced_count),
            1 if projection.surfaced_attested else 0,
            _iso(projection.first_surfaced_at),
            _iso(projection.last_surfaced_at),
            projection.title,
            projection.connection_text,
            projection.benefit_text,
            projection.first_step,
            canonical_json(citations_to_json(projection.citations)),
            len(projection.citations),
            projection.evidence_basis,
            projection.evidence_revision,
            _iso(projection.evidence_cutoff_at),
            canonical_json(list(projection.theme_tokens)),
            projection.supersedes_proposal_id,
            int(projection.repropose_depth),
            _iso(projection.snooze_until),
            _iso(projection.expires_at),
            _iso(projection.accepted_at),
            _iso(projection.rejected_at),
            projection.rejection_reason,
            projection.task_id,
            projection.objective_hash,
            projection.plan_root_goal_id,
            _iso(projection.pursuit_started_at),
            _iso(projection.completed_at),
            _iso(projection.abandoned_at),
            projection.abandon_reason,
            projection.withdrawn_reason,
            write.source_revision,
            _iso(now),
            str(proposal_id),
            int(write.expected_revision),
        ),
    )
    return bool(cursor.rowcount == 1)


def _current_revision(conn: sqlite3.Connection, *, scope: ResolvedScope, proposal_id: str) -> int:
    row = conn.execute(
        _proposals_sql("revision", "AND id = ?"),
        (*_dream_scope_params(scope), str(proposal_id)),
    ).fetchone()
    return int(row["revision"] or 0) if row is not None else -1


def _apply_dream_events_locked(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    proposal_id: str,
    new_events: Sequence[DreamProposalEvent],
    now: datetime,
    after_surfaces: int,
    expected_revision: int | None,
    retry_once: bool,
) -> DreamProposalWrite:
    """The read-then-write section itself.  Callers that already hold the bracket use this.

    ``transition_allowed`` is checked against the row's CURRENT status for every event in the
    batch.  The only multi-event batch is the sweep's ``evidence_revalidated`` +
    ``withdrawn``, and ``evidence_revalidated`` is legal from every status and changes none,
    so checking against the stored status is exact rather than approximate.
    """
    row = conn.execute(
        _proposals_sql("id, revision, highest_seq, status", "AND id = ?"),
        (*_dream_scope_params(scope), str(proposal_id)),
    ).fetchone()
    if row is None:
        raise DreamRevisionConflict(
            proposal_id=str(proposal_id),
            expected_revision=int(expected_revision or 0),
            actual_revision=-1,
        )
    locked_revision = int(row["revision"] or 0)
    highest_seq = int(row["highest_seq"] or 0)
    try:
        current_status = DreamProposalStatus(str(row["status"] or "proposed"))
    except ValueError:
        current_status = DreamProposalStatus.PROPOSED

    if expected_revision is not None and int(expected_revision) != locked_revision:
        raise DreamRevisionConflict(
            proposal_id=str(proposal_id),
            expected_revision=int(expected_revision),
            actual_revision=locked_revision,
        )

    for event in new_events:
        kind = event.kind if isinstance(event.kind, DreamProposalEventKind) else DreamProposalEventKind(str(event.kind))
        allowed, why = transition_allowed(current_status, kind)
        if not allowed:
            raise DreamTransitionRefused(proposal_id=str(proposal_id), reason=why)

    prior_events = load_proposal_events(conn, proposal_id=proposal_id)
    write = prepare_dream_write(
        prior_events,
        new_events,
        proposal_id=str(proposal_id),
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        subject_user_id=scope.subject_user_id,
        session_id="",
        expected_revision=locked_revision,
        highest_seq=highest_seq,
        now=now,
        after_surfaces=after_surfaces,
    )
    _insert_events(
        conn,
        proposal_id=str(proposal_id),
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        events=write.events,
    )
    if not _cas_update(conn, proposal_id=str(proposal_id), write=write, prior_highest_seq=highest_seq, now=now):
        raise DreamRevisionConflict(
            proposal_id=str(proposal_id),
            expected_revision=locked_revision,
            actual_revision=_current_revision(conn, scope=scope, proposal_id=str(proposal_id)),
        )
    return write


def apply_dream_events(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    proposal_id: str,
    new_events: Sequence[DreamProposalEvent],
    now: datetime,
    after_surfaces: int,
    expected_revision: int | None = None,
    retry_once: bool = False,
) -> DreamProposalWrite:
    """CAS write, inside P2's imported ``BEGIN IMMEDIATE`` bracket.

    The bracket is imported rather than re-issued: ``task_state_store.begin_immediate_cas``
    documents itself as the only place Lite opens one, and P5 does not make that false.
    """
    return run_cas_section(
        conn,
        lambda: _apply_dream_events_locked(
            conn,
            scope=scope,
            proposal_id=proposal_id,
            new_events=new_events,
            now=now,
            after_surfaces=after_surfaces,
            expected_revision=expected_revision,
            retry_once=retry_once,
        ),
    )


# --------------------------------------------------------------------------------------
# Minting
# --------------------------------------------------------------------------------------

_PROPOSAL_COLUMNS = (
    "id",
    "workspace_id",
    "owner_id",
    "subject_user_id",
    "project_id",
    "scope_kind",
    "session_id",
    "run_id",
    "revision",
    "highest_seq",
    "status",
    "nonresponse_state",
    "surfaced_count",
    "surfaced_attested",
    "title",
    "connection_text",
    "benefit_text",
    "first_step",
    "citations_json",
    "citation_count",
    "evidence_basis",
    "evidence_revision",
    "evidence_cutoff_at",
    "theme_tokens_json",
    "supersedes_proposal_id",
    "repropose_depth",
    "expires_at",
    "source_revision",
    "policy_revision",
    "created_at",
    "updated_at",
    "schema_version",
)


def mint_proposal(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    session_id: str,
    run_id: str,
    candidate: DreamCandidate,
    evidence_revision: str,
    evidence_cutoff_at: datetime | None,
    supersedes_proposal_id: str | None,
    repropose_depth: int,
    ttl_days: int,
    now: datetime,
) -> str:
    """INSERT a new proposal and its one ``proposed`` event.  Returns the proposal id.

    The row is written as the fold of that single event rather than assembled by hand, so the
    stored projection and a rebuild from the log agree from the first byte.
    """
    proposal_id = str(uuid.uuid4())
    scope_kind = dream_scope_kind(scope)
    expires_at = now + timedelta(days=max(1, int(ttl_days)))
    event = DreamProposalEvent(
        seq=1,
        kind=DreamProposalEventKind.PROPOSED,
        payload={
            "title": candidate.title,
            "connection_text": candidate.connection_text,
            "benefit_text": candidate.benefit_text,
            "first_step": candidate.first_step,
            "citations": citations_to_json(candidate.citations),
            "theme_tokens": list(candidate.theme_tokens),
            "evidence_basis": candidate.evidence_basis,
            "evidence_revision": str(evidence_revision),
            "evidence_cutoff_at": _iso(evidence_cutoff_at),
            "project_id": scope.project_id if scope_kind == SCOPE_KIND_PROJECT else None,
            "scope_kind": scope_kind,
            "supersedes_proposal_id": supersedes_proposal_id,
            "repropose_depth": int(repropose_depth),
            "expires_at": _iso(expires_at),
        },
        occurred_at=now,
        actor="system",
        actor_class="system",
        run_id=str(run_id) or None,
    )
    write = prepare_dream_write(
        [],
        [event],
        proposal_id=proposal_id,
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        subject_user_id=scope.subject_user_id,
        session_id=session_id,
        expected_revision=0,
        highest_seq=0,
        now=now,
        after_surfaces=1,
    )
    projection = write.projection
    conn.execute(
        "INSERT INTO dream_proposals ({cols}) VALUES ({marks})".format(
            cols=", ".join(_PROPOSAL_COLUMNS),
            marks=", ".join("?" for _ in _PROPOSAL_COLUMNS),
        ),
        (
            proposal_id,
            scope.workspace_id,
            scope.owner_id,
            scope.subject_user_id,
            scope.project_id if scope_kind == SCOPE_KIND_PROJECT else None,
            scope_kind,
            session_id,
            str(run_id),
            int(write.next_revision),
            1,
            str(projection.status),
            str(projection.nonresponse),
            0,
            0,
            projection.title,
            projection.connection_text,
            projection.benefit_text,
            projection.first_step,
            canonical_json(citations_to_json(projection.citations)),
            len(projection.citations),
            projection.evidence_basis,
            projection.evidence_revision,
            _iso(projection.evidence_cutoff_at),
            canonical_json(list(projection.theme_tokens)),
            supersedes_proposal_id,
            int(repropose_depth),
            _iso(expires_at),
            write.source_revision,
            DREAM_POLICY_REVISION,
            _iso(now),
            _iso(now),
            DREAM_SCHEMA_VERSION,
        ),
    )
    _insert_events(
        conn,
        proposal_id=proposal_id,
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        events=write.events,
    )
    return proposal_id


# --------------------------------------------------------------------------------------
# §4.2 — surfacing
# --------------------------------------------------------------------------------------


def append_surfaced(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    proposal_id: str,
    actor: str,
    actor_class: str,
    now: datetime,
    min_hours: int,
    ignored_hours: int,
    after_surfaces: int,
) -> DreamProposalWrite | None:
    """Record that this proposal was put in front of the owner, or decline to.

    Rate-limited by :func:`resurface_interval_hours`, so a polling client cannot inflate the
    count into ``ignored`` — and ``ignored``'s only effect is a *longer* interval anyway.
    Returns ``None`` when the showing is inside the interval and nothing was appended.

    ``surfaced`` is a display record, not a verdict: the thing that puts a proposal in front
    of the owner is the executor's renderer, so requiring a verified human to attest that the
    executor displayed something would make the field unreachable.  The event records
    ``actor_class`` and the projection carries ``surfaced_attested``, so the two are never
    confused downstream.
    """
    loaded = load_proposal(conn, scope=scope, proposal_id=proposal_id)
    if loaded is None:
        return None
    projection, _highest_seq, _source_revision = loaded
    interval = resurface_interval_hours(
        projection.nonresponse, min_hours=min_hours, ignored_hours=ignored_hours
    )
    last = projection.last_surfaced_at
    if last is not None and interval > 0 and (now - last) < timedelta(hours=interval):
        return None
    event = surfaced_event(
        ordinal=projection.surfaced_count + 1,
        first_surfaced_at=projection.first_surfaced_at or now,
        occurred_at=now,
        actor=actor,
        actor_class=actor_class,
        prior_attested=projection.surfaced_attested,
    )
    return apply_dream_events(
        conn,
        scope=scope,
        proposal_id=proposal_id,
        new_events=[event],
        now=now,
        after_surfaces=after_surfaces,
    )


# --------------------------------------------------------------------------------------
# §2.4 — the two-stage citation validation
# --------------------------------------------------------------------------------------


def validate_citations_cheap(
    conn: sqlite3.Connection, *, scope: ResolvedScope, citations: Sequence[DreamCitation]
) -> tuple[list[DreamCitation], list[str]]:
    """One query, no decrypt.  Existence, scope, sensitivity, receipt binding, hash equality.

    A citation is dropped when the event is gone, the sensitivity ceiling has risen above it,
    the workspace no longer matches, the receipt binding is gone, the receipt's ``origin_kind``
    is no longer admissible, the receipt's hash differs from the stored one, or the receipt's
    project no longer matches this proposal's project mode.
    """
    if not citations:
        return [], []
    event_ids = [str(item.event_id) for item in citations]
    marks = ", ".join("?" for _ in event_ids)
    rows = conn.execute(
        f"""
        SELECT e.id AS event_id,
               e.sensitivity AS sensitivity,
               json_extract(e.context, '$._tce_workspace') AS workspace_tag,
               r.id AS receipt_id,
               r.content_sha256 AS content_sha256,
               r.origin_kind AS origin_kind,
               r.project_id AS project_id
          FROM events e
          LEFT JOIN trusted_input_receipts r
            ON r.event_id = e.id
           AND r.workspace_id = ?
           AND r.owner_id = ?
           AND r.subject_user_id = ?
         WHERE e.id IN ({marks})
        """,
        (scope.workspace_id, scope.owner_id, scope.subject_user_id, *event_ids),
    ).fetchall()
    by_event = {str(row["event_id"]): row for row in rows}
    scope_kind = dream_scope_kind(scope)

    surviving: list[DreamCitation] = []
    dropped: list[str] = []
    for citation in citations:
        row = by_event.get(str(citation.event_id))
        if row is None:
            dropped.append("event_missing")
            continue
        if int(row["sensitivity"] or 0) > CITATION_MAX_SENSITIVITY:
            dropped.append("sensitivity_raised")
            continue
        if str(row["workspace_tag"] or "") != scope.workspace_id:
            dropped.append("workspace_changed")
            continue
        if not row["receipt_id"]:
            dropped.append("receipt_missing")
            continue
        if str(row["origin_kind"] or "") not in ADMISSIBLE_ORIGIN_KINDS:
            dropped.append("origin_kind_inadmissible")
            continue
        if str(row["content_sha256"] or "") != citation.content_sha256:
            dropped.append("content_hash_changed")
            continue
        receipt_project = str(row["project_id"] or "")
        if scope_kind == SCOPE_KIND_PROJECT:
            if receipt_project != str(scope.project_id or ""):
                dropped.append("project_changed")
                continue
        elif receipt_project:
            dropped.append("project_changed")
            continue
        surviving.append(citation)
    return surviving, dropped


def validate_citations_deep(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    citations: Sequence[DreamCitation],
    quote_max_chars: int = 200,
) -> tuple[list[DreamCitation], list[str]]:
    """Cheap, then read each surviving body and re-find the quote in it.

    Lite's "decrypt" is a ``json.loads``: ``db.py`` stores event payloads in plaintext and has
    no encrypted branch.  Same function name, same contract, one branch different from Full.

    The voice check runs again on the quote here for the same reason it runs at generation: a
    body can be edited after a proposal is written, and a quote that has become pasted
    marketing prose is no longer the owner's voice however verbatim it is.
    """
    surviving, dropped = validate_citations_cheap(conn, scope=scope, citations=citations)
    if not surviving:
        return [], dropped
    event_ids = [str(item.event_id) for item in surviving]
    marks = ", ".join("?" for _ in event_ids)
    rows = conn.execute(
        f"SELECT id, payload FROM events WHERE id IN ({marks})",
        tuple(event_ids),
    ).fetchall()
    bodies: dict[str, str] = {}
    for row in rows:
        payload = _loads(row["payload"], {})
        if not isinstance(payload, dict) or "_tce_encrypted" in payload:
            continue
        bodies[str(row["id"])] = str(payload.get("input_excerpt") or "")

    kept: list[DreamCitation] = []
    for citation in surviving:
        body = bodies.get(str(citation.event_id))
        if not body:
            dropped.append("undecryptable")
            continue
        if not quote_is_contained(citation.quote, body, max_chars=int(quote_max_chars)):
            dropped.append("quote_not_found")
            continue
        if voice_violations(citation.quote):
            dropped.append("quote_voice_violation")
            continue
        kept.append(citation)
    return kept, dropped


# --------------------------------------------------------------------------------------
# §2.1 — the corpus.  The receipt is the only admission (M0).
# --------------------------------------------------------------------------------------


def subject_has_project_receipts(conn: sqlite3.Connection, *, scope: ResolvedScope) -> bool:
    """D9's entitlement check.  Producer of ``refusal_reason='unentitled_project'``.

    A project id is client-assertable — ``project_context`` accepts any well-formed
    ``proj_[a-f0-9]{24}`` verbatim as the project identity — so without this a caller could
    bind a generation run to a project they have never spoken into.
    """
    if not scope.project_id:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM trusted_input_receipts
         WHERE workspace_id = ? AND owner_id = ? AND subject_user_id = ?
           AND project_id = ?
           AND origin_kind IN (?, ?)
         LIMIT 1
        """,
        (
            scope.workspace_id,
            scope.owner_id,
            scope.subject_user_id,
            str(scope.project_id),
            *ADMISSIBLE_ORIGIN_KINDS,
        ),
    ).fetchone()
    return row is not None


def select_candidate_messages(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    scope_kind: str,
    limit: int,
    min_chars: int,
    max_chars: int,
    max_sensitivity: int,
) -> tuple[list[PoolMessage], dict[str, int]]:
    """The ONLY path by which a message reaches a proposal.

    There is no ``task_type`` arm and no ``_tce_owner`` arm.  The receipt is the only
    admission, its ``origin_kind`` is the only evidence class, its ``content_sha256`` is the
    only hash and its ``project_id`` is the only project attribution; ``events`` contributes
    the body, the timestamp and the sensitivity ceiling and nothing else.

    ``subject_user_id`` comes from ``scope`` rather than a second parameter, so there is one
    source of truth for whose messages these are.
    """
    if scope_kind == SCOPE_KIND_PROJECT:
        # Unconditional: scope_kind is DERIVED, so an unbound scope can never reach this arm,
        # and an unentitled project is refused before this function is called.
        receipt_project_clause = "AND r2.project_id = ?"
        project_params: tuple[Any, ...] = (str(scope.project_id or ""),)
    else:
        receipt_project_clause = "AND (r2.project_id IS NULL OR r2.project_id = '')"
        project_params = ()

    rows = conn.execute(
        f"""
        SELECT e.id AS event_id,
               e.ts AS ts,
               e.payload AS payload,
               r.id AS receipt_id,
               r.content_sha256 AS content_sha256,
               r.origin_kind AS origin_kind,
               r.observed_at AS observed_at,
               r.content_truncated AS content_truncated
          FROM events e
          JOIN trusted_input_receipts r
            ON r.id = (
                 SELECT r2.id
                   FROM trusted_input_receipts r2
                  WHERE r2.event_id = e.id
                    AND r2.workspace_id = ?
                    AND r2.owner_id = ?
                    AND r2.subject_user_id = ?
                    AND r2.origin_kind IN (?, ?)
                    {receipt_project_clause}
                  ORDER BY r2.ingested_at DESC
                  LIMIT 1
               )
         WHERE e.sensitivity <= ?
           AND json_extract(e.context, '$._tce_workspace') = ?
         ORDER BY e.ts DESC
         LIMIT ?
        """,
        (
            scope.workspace_id,
            scope.owner_id,
            scope.subject_user_id,
            *ADMISSIBLE_ORIGIN_KINDS,
            *project_params,
            int(max_sensitivity),
            scope.workspace_id,
            max(1, int(limit)),
        ),
    ).fetchall()

    drops: dict[str, int] = {}

    def _drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    pool: list[PoolMessage] = []
    seen_hashes: set[str] = set()
    for row in rows:
        payload = _loads(row["payload"], {})
        if not isinstance(payload, dict) or "_tce_encrypted" in payload:
            _drop("undecryptable")
            continue
        body = str(payload.get("input_excerpt") or "").strip()
        if not body:
            _drop("undecryptable")
            continue
        if is_harness_text(body):
            _drop("harness_text")
            continue
        if bool(row["content_truncated"]):
            _drop("truncated_paste")
            continue
        if len(body) < int(min_chars):
            _drop("too_short")
            continue
        content_sha256 = str(row["content_sha256"] or "")
        if content_sha256 in seen_hashes:
            _drop("duplicate_message")
            continue
        seen_hashes.add(content_sha256)
        origin_kind = str(row["origin_kind"] or QUOTABLE_ORIGIN_KIND)
        observed_at = _parse_dt(row["observed_at"]) or _parse_dt(row["ts"]) or datetime(1970, 1, 1, tzinfo=UTC)
        pool.append(
            PoolMessage(
                n=len(pool) + 1,
                event_id=str(row["event_id"]),
                receipt_id=str(row["receipt_id"] or ""),
                content_sha256=content_sha256,
                origin_kind=origin_kind,
                observed_at=observed_at,
                # Truncated FOR THE PROMPT ONLY.  The persisted hash is always the receipt's
                # hash of the full original, so truncation here can never change identity.
                body=body[: max(1, int(max_chars))],
            )
        )
    return pool, drops


# --------------------------------------------------------------------------------------
# §3.1 — generation runs.  The ROUTE owns everything that does not need a model.
# --------------------------------------------------------------------------------------


def start_generation_run(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    session_id: str,
    scope_kind: str,
    stale_minutes: int,
    now: datetime,
) -> str:
    """Take the one in-flight slot for this scope.  Returns the run id.

    Sweeps ``running`` rows older than ``stale_minutes`` to ``failed/run_abandoned`` first, so
    a crashed run does not wedge the scope forever.  The slot itself is held by a unique
    partial index rather than by a read followed by a write, so two concurrent refreshes
    cannot both take it; the loser raises ``sqlite3.IntegrityError`` and the route turns that
    into ``run_already_in_flight``.
    """
    cutoff = now - timedelta(minutes=max(1, int(stale_minutes)))
    conn.execute(
        """
        UPDATE dream_generation_runs
           SET state = 'failed', refusal_reason = 'run_abandoned', finished_at = ?
         WHERE workspace_id = ? AND owner_id = ? AND state = 'running' AND started_at < ?
        """,
        (_iso(now), scope.workspace_id, scope.owner_id, _iso(cutoff)),
    )
    run_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO dream_generation_runs
            (id, workspace_id, owner_id, subject_user_id, project_id, scope_kind, scope_key,
             session_id, state, started_at, schema_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (
            run_id,
            scope.workspace_id,
            scope.owner_id,
            scope.subject_user_id,
            scope.project_id if scope_kind == SCOPE_KIND_PROJECT else None,
            scope_kind,
            _scope_key(scope_kind, scope.project_id if scope_kind == SCOPE_KIND_PROJECT else None),
            session_id,
            _iso(now),
            DREAM_SCHEMA_VERSION,
        ),
    )
    return run_id


def bind_generation_run(conn: sqlite3.Connection, *, run_id: str, planning_job_id: str) -> None:
    """Bind the enqueued planning job to the run, so the worker can find its run row.

    ``enqueue_planning_job`` has no free-form payload field and is P2's, which P5 does not
    edit; this is how the two are joined without one.
    """
    conn.execute(
        "UPDATE dream_generation_runs SET planning_job_id = ? WHERE id = ?",
        (str(planning_job_id), str(run_id)),
    )


def load_generation_run_by_job(
    conn: sqlite3.Connection, *, planning_job_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM dream_generation_runs WHERE planning_job_id = ? ORDER BY started_at DESC LIMIT 1",
        (str(planning_job_id),),
    ).fetchone()
    return _run_to_dict(row) if row is not None else None


def load_generation_run(
    conn: sqlite3.Connection, *, scope: ResolvedScope, run_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM dream_generation_runs
         WHERE id = ? AND workspace_id = ? AND owner_id = ? AND subject_user_id = ?
        """,
        (str(run_id), scope.workspace_id, scope.owner_id, scope.subject_user_id),
    ).fetchone()
    return _run_to_dict(row) if row is not None else None


def _run_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"] or ""),
        "owner_id": str(row["owner_id"] or ""),
        "subject_user_id": str(row["subject_user_id"] or ""),
        "project_id": (str(row["project_id"]) if row["project_id"] else None),
        "scope_kind": str(row["scope_kind"] or SCOPE_KIND_PROJECT),
        "scope_key": str(row["scope_key"] or ""),
        "session_id": str(row["session_id"] or ""),
        "planning_job_id": (str(row["planning_job_id"]) if row["planning_job_id"] else None),
        "state": str(row["state"] or RUN_STATE_RUNNING),
        "refusal_reason": str(row["refusal_reason"] or ""),
        "pool_size": int(row["pool_size"] or 0),
        "pool_json": _loads(row["pool_json"], []),
        "pool_drops": _counter(row["pool_drops_json"]),
        "evidence_revision": str(row["evidence_revision"] or ""),
        "evidence_cutoff_at": _parse_dt(row["evidence_cutoff_at"]),
        "candidates_returned": int(row["candidates_returned"] or 0),
        "proposals_written": int(row["proposals_written"] or 0),
        "refusals": _counter(row["refusals_json"]),
        "model_provider": str(row["model_provider"] or ""),
        "prompt_hash": str(row["prompt_hash"] or ""),
        "started_at": _parse_dt(row["started_at"]),
        "finished_at": _parse_dt(row["finished_at"]),
    }


def finish_generation_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    state: str,
    refusal_reason: str,
    pool: Sequence[PoolMessage],
    evidence_revision: str,
    evidence_cutoff_at: datetime | None,
    candidates_returned: int,
    proposals_written: int,
    refusals: Mapping[str, int],
    pool_drops: Mapping[str, int],
    prompt_hash: str,
    model_provider: str,
    now: datetime,
) -> None:
    """Close the run and release the in-flight slot.

    ``pool_json`` records the ordered ``n -> (event_id, receipt_id, content_sha256,
    origin_kind, observed_at)`` mapping and **no message text**, so a citation number stays
    auditable months later without the run row becoming a second, unencrypted copy of the
    owner's words.
    """
    pool_json = [
        {
            "n": int(message.n),
            "event_id": message.event_id,
            "receipt_id": message.receipt_id,
            "content_sha256": message.content_sha256,
            "origin_kind": message.origin_kind,
            "observed_at": _iso(message.observed_at),
        }
        for message in pool
    ]
    conn.execute(
        """
        UPDATE dream_generation_runs
           SET state = ?, refusal_reason = ?, pool_size = ?, pool_json = ?, pool_drops_json = ?,
               evidence_revision = ?, evidence_cutoff_at = ?, candidates_returned = ?,
               proposals_written = ?, refusals_json = ?, model_provider = ?, prompt_hash = ?,
               finished_at = ?
         WHERE id = ?
        """,
        (
            str(state),
            str(refusal_reason),
            len(pool_json),
            canonical_json(pool_json),
            canonical_json({str(k): int(v) for k, v in pool_drops.items()}),
            str(evidence_revision),
            _iso(evidence_cutoff_at),
            int(candidates_returned),
            int(proposals_written),
            canonical_json({str(k): int(v) for k, v in refusals.items()}),
            str(model_provider),
            str(prompt_hash),
            _iso(now),
            str(run_id),
        ),
    )


# --------------------------------------------------------------------------------------
# §4.5 — the sweep.  The only writer of expired, unsnoozed and withdrawn.
# --------------------------------------------------------------------------------------


def sweep_dream_proposals(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    now: datetime,
    ttl_days: int = 45,
    min_citations: int = 2,
    quote_max_chars: int = 200,
    after_surfaces: int = 3,
) -> dict[str, int]:
    """Expire, unsnooze, revalidate, withdraw.  ``accepted`` and ``pursued`` never expire.

    This is also the only place a GET's discovery turns into a write: the list route omits a
    proposal whose citations no longer verify and appends nothing, and the withdrawal happens
    here, where it has a named producer and an event.
    """
    counts = {"expired": 0, "unsnoozed": 0, "withdrawn": 0, "revalidated": 0}
    rows = conn.execute(
        _proposals_sql("*", "AND status IN ('proposed', 'surfaced', 'snoozed', 'accepted', 'pursued')"),
        _dream_scope_params(scope),
    ).fetchall()

    for row in rows:
        projection = _projection_from_row(row)
        status = str(projection.status)
        events: list[DreamProposalEvent] = []

        if status == DreamProposalStatus.SNOOZED and projection.snooze_until and now >= projection.snooze_until:
            # A snooze the owner asked for must not cause the proposal to die of old age, so
            # the TTL restarts from now rather than from the original mint.
            events.append(
                DreamProposalEvent(
                    seq=0,
                    kind=DreamProposalEventKind.UNSNOOZED,
                    payload={"expires_at": _iso(now + timedelta(days=max(1, int(ttl_days))))},
                    occurred_at=now,
                    actor="system",
                    actor_class="system",
                )
            )
            counts["unsnoozed"] += 1
        elif (
            status in {DreamProposalStatus.PROPOSED, DreamProposalStatus.SURFACED}
            and projection.expires_at
            and now >= projection.expires_at
        ):
            events.append(
                DreamProposalEvent(
                    seq=0,
                    kind=DreamProposalEventKind.EXPIRED,
                    payload={"expired_at": _iso(now)},
                    occurred_at=now,
                    actor="system",
                    actor_class="system",
                )
            )
            counts["expired"] += 1

        if not events and projection.citations:
            surviving, dropped = validate_citations_deep(
                conn,
                scope=scope,
                citations=projection.citations,
                quote_max_chars=quote_max_chars,
            )
            if dropped:
                revision, cutoff = evidence_revision(
                    [{"id": item.event_id, "ts": item.observed_at} for item in surviving]
                )
                events.append(
                    DreamProposalEvent(
                        seq=0,
                        kind=DreamProposalEventKind.EVIDENCE_REVALIDATED,
                        payload={
                            "citations": citations_to_json(surviving),
                            "evidence_basis": evidence_basis_for(surviving),
                            "evidence_revision": revision,
                            "evidence_cutoff_at": _iso(cutoff),
                            "dropped": sorted(set(dropped)),
                        },
                        occurred_at=now,
                        actor="system",
                        actor_class="system",
                    )
                )
                counts["revalidated"] += 1
                if len(surviving) < int(min_citations):
                    events.append(
                        DreamProposalEvent(
                            seq=0,
                            kind=DreamProposalEventKind.WITHDRAWN,
                            payload={"reason": "citations_unverifiable"},
                            occurred_at=now,
                            actor="system",
                            actor_class="system",
                        )
                    )
                    counts["withdrawn"] += 1

        if not events:
            continue
        try:
            apply_dream_events(
                conn,
                scope=scope,
                proposal_id=projection.proposal_id,
                new_events=events,
                now=now,
                after_surfaces=after_surfaces,
            )
        except (DreamRevisionConflict, DreamTransitionRefused):
            # Another writer moved this proposal between the scan and the write.  The sweep is
            # idempotent and runs again on the next refresh; losing a race is not an error.
            continue
    return counts


# --------------------------------------------------------------------------------------
# §5.2 — the one place a proposal meets P2's task projection
# --------------------------------------------------------------------------------------


def reconcile_dream_pursuit(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    session_id: str,
    now: datetime,
    max_plan_steps: int = MAX_PLAN_STEPS,
    after_surfaces: int = 3,
) -> dict[str, int]:
    """Read the task projection, write proposal events.  Never the reverse (lock order §0.11).

    Completion is READ, never inferred.  ``objective_bound`` sets ``task_id`` and
    ``objective_hash`` and does not touch status, so creating a plan cannot complete anything;
    ``completed`` requires the task projection to report ``DONE`` **and** both objective
    hashes to be non-null and equal.  ``TaskStatus.DONE`` is deliberately NOT idle: a finished
    task cannot have a new objective bound onto it, so its verification can never be inherited
    and read as this proposal's completion.
    """
    counts = {"bound": 0, "pursuit_started": 0, "completed": 0, "abandoned": 0}
    rows = conn.execute(
        _proposals_sql("*", "AND status IN ('accepted', 'pursued') ORDER BY accepted_at ASC"),
        _dream_scope_params(scope),
    ).fetchall()

    for row in rows:
        projection = _projection_from_row(row)
        if projection.task_id is None:
            if _bind_objective(
                conn,
                scope=scope,
                projection=projection,
                session_id=session_id,
                now=now,
                max_plan_steps=max_plan_steps,
                after_surfaces=after_surfaces,
            ):
                counts["bound"] += 1
            continue

        loaded = load_task_state(
            conn,
            workspace_id=scope.workspace_id,
            owner_id=scope.owner_id,
            task_id=projection.task_id,
        )
        events: list[DreamProposalEvent] = []
        if loaded is None:
            events.append(_abandon_event("task_state_missing", now))
        else:
            task_projection = loaded[0]
            if (
                projection.objective_hash is None
                or task_projection.objective_hash is None
                or task_projection.objective_hash != projection.objective_hash
            ):
                # ``None == None`` is explicitly not agreement: an unset hash on both sides is
                # the exact shape that let a different objective's verification complete a
                # proposal that had never run a step.
                events.append(_abandon_event("objective_changed", now))
            elif task_projection.status is TaskStatus.CANCELLED:
                events.append(_abandon_event("task_cancelled", now))
            elif task_projection.status is TaskStatus.DONE and projection.status is not DreamProposalStatus.COMPLETED:
                if projection.status is DreamProposalStatus.ACCEPTED:
                    # The closed transition table has no accepted -> completed edge, and that
                    # is right: a task cannot reach DONE without steps having run, so the
                    # pursuit really did start and the log should say so.
                    events.append(_pursuit_started_event(task_projection, now))
                events.append(
                    DreamProposalEvent(
                        seq=0,
                        kind=DreamProposalEventKind.COMPLETED,
                        payload={"completed_at": _iso(now), "task_id": projection.task_id},
                        occurred_at=now,
                        actor="system",
                        actor_class="system",
                    )
                )
            elif projection.status is DreamProposalStatus.ACCEPTED and _plan_has_started(task_projection):
                events.append(_pursuit_started_event(task_projection, now))

        if not events:
            continue
        try:
            apply_dream_events(
                conn,
                scope=scope,
                proposal_id=projection.proposal_id,
                new_events=events,
                now=now,
                after_surfaces=after_surfaces,
            )
        except (DreamRevisionConflict, DreamTransitionRefused):
            continue
        for event in events:
            if str(event.kind) == DreamProposalEventKind.PURSUIT_STARTED:
                counts["pursuit_started"] += 1
            elif str(event.kind) == DreamProposalEventKind.COMPLETED:
                counts["completed"] += 1
            elif str(event.kind) == DreamProposalEventKind.PURSUIT_ABANDONED:
                counts["abandoned"] += 1
    return counts


def _abandon_event(reason: str, now: datetime) -> DreamProposalEvent:
    return DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.PURSUIT_ABANDONED,
        payload={"reason": reason, "abandoned_at": _iso(now)},
        occurred_at=now,
        actor="system",
        actor_class="system",
    )


def _pursuit_started_event(task_projection: Any, now: datetime) -> DreamProposalEvent:
    plan = getattr(task_projection, "plan", None)
    return DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.PURSUIT_STARTED,
        payload={
            "pursuit_started_at": _iso(now),
            "plan_root_goal_id": getattr(plan, "root_goal_id", None) if plan is not None else None,
        },
        occurred_at=now,
        actor="system",
        actor_class="system",
    )


def _plan_has_started(task_projection: Any) -> bool:
    plan = getattr(task_projection, "plan", None)
    if plan is None:
        return False
    for step in getattr(plan, "steps", ()) or ():
        if str(getattr(step, "status", "")) in {"in_progress", "completed"}:
            return True
    return False


def _bind_objective(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    projection: DreamProposalProjection,
    session_id: str,
    now: datetime,
    max_plan_steps: int,
    after_surfaces: int,
) -> bool:
    """Branch A: bind the accepted proposal's first step as the session's objective.

    The objective is ``first_step``, not ``title``.  The title is an aspiration; the planner
    decomposes an objective, and the model was already required to produce a small concrete
    next step.  Handing the planner the small concrete thing is both better planning input and
    the reason the title never has to be sanitised into an instruction.

    The task-state write and the ``objective_bound`` event are one CAS section, in that order
    (§0.11).  Splitting them would let a crash between the two leave the session carrying the
    objective with no proposal event to say so, and the next tick would then see a non-idle
    task and never bind again.
    """
    first_step = str(projection.first_step or "").strip()
    if not first_step:
        return False

    def _body() -> bool:
        task_state_id, task_projection, _highest_seq, _source_revision = ensure_task_state(
            conn,
            workspace_id=scope.workspace_id,
            owner_id=scope.owner_id,
            subject_user_id=scope.subject_user_id,
            session_id=session_id,
            task_id=session_id,
            project_id=scope.project_id,
            now=now,
        )
        idle = task_projection.status in {TaskStatus.AWAITING_OBJECTIVE, TaskStatus.CANCELLED}
        if not idle:
            # Try again next tick; nothing is lost.  TaskStatus.DONE is NOT in the idle set.
            return False
        new_hash = objective_hash(first_step)
        next_revision = objective_contract_revision(
            int(task_projection.contract_revision),
            task_projection.objective_hash,
            new_hash,
        )
        charter = charter_for_task(
            max_steps=max(1, int(max_plan_steps)),
            reason="deterministic read-only diagnosis",
        )
        steps = deterministic_plan(first_step, charter=charter)
        job_id, _created = enqueue_planning_job(
            conn,
            workspace_id=scope.workspace_id,
            owner_id=scope.owner_id,
            session_id=session_id,
            task_id=session_id,
            task_state_id=task_state_id,
            job_kind=PLANNING_JOB_KIND_DECOMPOSE,
            input_revision=plan_input_revision(
                objective_hash=new_hash,
                contract_revision=next_revision,
                policy_revision=TASK_STATE_POLICY_REVISION,
                scope_digest=task_scope_digest(
                    workspace_id=scope.workspace_id,
                    executor_id=scope.executor_id,
                    owner_ids=scope.sql_owner_ids(),
                    subject_user_id=scope.subject_user_id,
                    project_id=scope.project_id,
                    project_binding=scope.project_binding,
                ),
                cancel_epoch=int(task_projection.last_cancel_seq),
            ),
            contract_revision=next_revision,
            objective_hash=new_hash,
            objective_text=first_step,
            charter=charter,
            scope=scope,
            now=now,
            max_attempts=3,
        )
        root_goal_id = write_plan_rows(
            conn,
            scope=scope,
            session_id=session_id,
            task_id=session_id,
            steps=steps,
            producer=PLANNING_PRODUCER_DETERMINISTIC,
            contract_revision=next_revision,
            now=now,
            objective_text=first_step,
        )
        apply_task_state_events(
            conn,
            workspace_id=scope.workspace_id,
            owner_id=scope.owner_id,
            session_id=session_id,
            task_id=session_id,
            new_events=[
                TaskStateEvent(
                    seq=0,
                    kind=TaskStateEventKind.OBJECTIVE_SET,
                    contract_revision=next_revision,
                    payload={
                        "objective_text": first_step,
                        "objective_hash": new_hash,
                        "source": "dream_proposal",
                        "proposal_id": projection.proposal_id,
                    },
                    occurred_at=now,
                    actor="system",
                ),
                TaskStateEvent(
                    seq=0,
                    kind=TaskStateEventKind.PLAN_APPROVED,
                    contract_revision=next_revision,
                    payload={
                        "producer": PLANNING_PRODUCER_DETERMINISTIC,
                        "plan_id": approved_plan_id(session_id, next_revision),
                        "root_goal_id": root_goal_id,
                        "steps": plan_steps_to_json(steps),
                        "charter": charter_to_json(charter),
                        "descriptive_sources": {},
                    },
                    occurred_at=now,
                    actor="system",
                    goal_id=root_goal_id,
                ),
            ],
            now=now,
        )
        _apply_dream_events_locked(
            conn,
            scope=scope,
            proposal_id=projection.proposal_id,
            new_events=[
                DreamProposalEvent(
                    seq=0,
                    kind=DreamProposalEventKind.OBJECTIVE_BOUND,
                    payload={
                        "task_id": session_id,
                        "objective_hash": new_hash,
                        "plan_root_goal_id": root_goal_id,
                        "planning_job_id": job_id,
                    },
                    occurred_at=now,
                    actor="system",
                    actor_class="system",
                )
            ],
            now=now,
            after_surfaces=after_surfaces,
            expected_revision=None,
            retry_once=False,
        )
        return True

    try:
        return bool(run_cas_section(conn, _body))
    except (DreamRevisionConflict, DreamTransitionRefused):
        return False


# --------------------------------------------------------------------------------------
# Wire helpers used by both dream routes
# --------------------------------------------------------------------------------------


def citation_quote_digest(quote: str) -> str:
    """The quote hash the store stamps when a citation is rebuilt from a payload."""
    return _sha256(normalise_for_containment(quote))


def candidate_from_projection(projection: DreamProposalProjection) -> DreamCandidate:
    """A projection re-read as a candidate, for the suppression comparisons.

    Used by the generation path when it needs to compare a stored proposal against a fresh
    one; the theme tokens are recomputed rather than trusted so a row written before the
    tokeniser changed compares on today's rules.
    """
    return DreamCandidate(
        title=projection.title,
        connection_text=projection.connection_text,
        benefit_text=projection.benefit_text,
        first_step=projection.first_step,
        citations=dedupe_by_content_sha256(projection.citations),
        theme_tokens=projection.theme_tokens
        or theme_tokens_for(projection.title, projection.first_step),
        evidence_basis=projection.evidence_basis,
    )


def dream_nonresponse_for(
    projection: DreamProposalProjection, *, now: datetime, after_surfaces: int
) -> NonresponseState:
    """The nonresponse axis, recomputed on read.  Time alone moves nothing (X2)."""
    return nonresponse_state(
        surfaced_count=projection.surfaced_count,
        last_surfaced_at=projection.last_surfaced_at,
        now=now,
        after_surfaces=after_surfaces,
    )


def count_pending_proposals(conn: sqlite3.Connection, *, scope: ResolvedScope) -> int:
    """One indexed ``COUNT(*)`` for ``TakeoverStepResponse.dream_proposals_pending``.

    A count, not a payload: the proposals themselves are read through the dream routes, which
    run the citation validation.  Putting proposal text on the turn response would put
    unvalidated quotes in front of the owner on every step.
    """
    row = conn.execute(
        _proposals_sql("COUNT(*) AS n", "AND status IN ('proposed', 'surfaced')"),
        _dream_scope_params(scope),
    ).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def load_proposal_stamps(
    conn: sqlite3.Connection, *, scope: ResolvedScope, proposal_ids: Sequence[str]
) -> dict[str, tuple[datetime | None, datetime | None]]:
    """``{proposal_id: (created_at, updated_at)}`` for the wire model, in one query.

    These two are the only wire fields the fold does not produce — they are row bookkeeping,
    not projection state — so they are read here rather than pushed into
    :class:`DreamProposalProjection`, where they would look like something an event set.
    """
    ids = [str(item) for item in proposal_ids if str(item)]
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        _proposals_sql("id, created_at, updated_at", "AND id IN (" + marks + ")"),
        (*_dream_scope_params(scope), *ids),
    ).fetchall()
    return {
        str(row["id"]): (_parse_dt(row["created_at"]), _parse_dt(row["updated_at"]))
        for row in rows
    }
