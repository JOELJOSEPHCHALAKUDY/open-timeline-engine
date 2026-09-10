"""Append-only proposal store for the Full backend: mint, fold, CAS-write, surface, reconcile.

Raw ``text()`` SQL against ``dream_proposals`` / ``dream_proposal_events`` /
``dream_generation_runs``.  **No ``db.commit()`` and no ``db.rollback()`` anywhere in this
module** — the caller owns the transaction, and the only rollback performed here is
``ROLLBACK TO SAVEPOINT`` on a savepoint this module took itself.  That is the same rule
``task_state_store`` states, for the same reason.

Three properties are structural rather than intentional, and they are the whole point of the
module:

1. **Nothing here deletes.**  This module issues no DELETE against any proposal table, and
   there is no path that reaches an existing proposal's row except through
   :func:`apply_dream_events`.  The behaviour this replaces removed rows from
   ``autonomy_goals`` on every refresh; the replacement has nowhere to put such a statement.
2. **The single row-update statement is the compare-and-swap** in :func:`_cas_update`, and it
   always carries ``AND revision = :expected_revision``.  A blind update would let two writers
   silently overwrite each other's fold.
3. **Completion is never inferred from plan creation.**  ``objective_bound`` records that a
   plan root now exists and touches no status; ``completed`` is written only by
   :func:`reconcile_dream_pursuit`, only when P2's own projection reports ``DONE`` *and* the
   objective hashes agree and are both non-null.  An earlier draft appended an event that
   marked completion the moment a plan was created, which is a worse version of the defect
   this phase exists to remove.

Scope, once: every read is filtered by :func:`_dream_scope_sql`, which is rendered from the
authenticated :class:`ResolvedScope` and nothing else.  ``subject_user_id`` is in that
predicate because one ``owner_id`` on this stack spans six subjects, and a detail route keyed
on ``(workspace_id, owner_id, proposal_id)`` alone would let a verified human bound to one
subject read and accept another subject's proposal.  Project binding comes from the *receipt*,
never from a client-asserted project id.

Lock order (global, extending P2 §0.8 and P3 §0.11): ``task_states`` (1) → ``authority_charters``
(2) → ``planning_jobs`` (3) → ``directive_executions`` (4) → ``execution_permits`` (5) →
``effect_journal`` (6) → ``dispatch_records`` (7) → ``dream_generation_runs`` (8) →
``dream_proposals`` (9) → ``autonomy_goals`` (10).  ``dream_proposals`` sits below
``task_states`` because the pursuit reconciler reads the task projection and then writes a
proposal event, never the reverse.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tce_shared.aspirations import (
    ADMISSIBLE_ORIGIN_KINDS,
    DREAM_POLICY_REVISION,
    DREAM_SCHEMA_VERSION,
    LIVE_STATUSES,
    MAX_DREAM_EVENTS_PER_FOLD,
    PINNED_DREAM_KIND_VALUES,
    PINNED_DREAM_KINDS,
    SCOPE_KIND_PROJECT,
    SCOPE_KIND_WORKSPACE,
    DreamCandidate,
    DreamCitation,
    DreamFoldResult,
    DreamProposalEvent,
    DreamProposalEventKind,
    DreamProposalProjection,
    DreamProposalStatus,
    DreamProposalWrite,
    DreamRevisionConflict,
    NonresponseState,
    PoolMessage,
    citations_from_json,
    citations_to_json,
    dedupe_by_content_sha256,
    evidence_basis_for,
    fold_dream_proposal,
    is_harness_text,
    nonresponse_state,
    prepare_dream_write,
    quote_is_contained,
    resurface_interval_hours,
    surfaced_event,
    theme_tokens_for,
    voice_violations,
)
from tce_shared.decision_capture import evidence_revision
from tce_shared.scope import ResolvedScope
from tce_shared.takeover import objective_hash
from tce_shared.task_state import (
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateRevisionConflict,
    TaskStatus,
    canonical_json,
    objective_contract_revision,
)

from .crypto import maybe_decrypt_payload
from .planning_store import enqueue_planning_job
from .task_state_store import apply_task_state_events, ensure_task_state, load_task_state

logger = logging.getLogger(__name__)


class DreamRunInFlight(RuntimeError):
    """A live generation run already holds this scope's single in-flight slot.

    Raised rather than returned because the alternative — silently reusing the other run's id —
    would let two callers believe they each started the run that is about to mint.  The route
    turns it into ``refused``/``run_already_in_flight`` with the *existing* run id, so the
    caller can go and read what that run actually did.
    """

    def __init__(self, *, run_id: str) -> None:
        super().__init__(f"a dream generation run is already in flight: {run_id}")
        self.run_id = run_id


_PROPOSAL_COLUMNS = """
    id, workspace_id, owner_id, subject_user_id, project_id, scope_kind, session_id, run_id,
    revision, highest_seq, status, nonresponse_state, surfaced_count, surfaced_attested,
    first_surfaced_at, last_surfaced_at, title, connection_text, benefit_text, first_step,
    citations_json, citation_count, evidence_basis, evidence_revision, evidence_cutoff_at,
    theme_tokens_json, supersedes_proposal_id, repropose_depth, snooze_until, expires_at,
    accepted_at, rejected_at, rejection_reason, task_id, objective_hash, plan_root_goal_id,
    pursuit_started_at, completed_at, abandoned_at, abandon_reason, withdrawn_reason,
    source_revision, policy_revision, created_at, updated_at, schema_version
"""

_RUN_COLUMNS = """
    id, workspace_id, owner_id, subject_user_id, project_id, scope_kind, scope_key, session_id,
    planning_job_id, state, refusal_reason, pool_size, pool_json, pool_drops_json,
    evidence_revision, evidence_cutoff_at, candidates_returned, proposals_written, refusals_json,
    model_provider, prompt_hash, started_at, finished_at, schema_version
"""


# --------------------------------------------------------------------------- scope


def _dream_scope_sql() -> str:
    """The ONE ``WHERE`` fragment every proposal read uses.  D22.

    Three things about this shape are deliberate.  ``subject_user_id`` is in it, because one
    ``owner_id`` spans several subjects on a real stack.  It is *not* the lenient
    ``(project_id = :p OR project_id IS NULL)`` shape the handoff predicate uses: a workspace
    proposal is built from receipts with no project and is not about any project, so leaking it
    into a project list would be exactly the mis-attribution the receipt rule exists to
    prevent.  And ``owner_id`` is the goal-style personal scope, never widened by continuity
    owners.
    """
    return (
        "WHERE workspace_id = :workspace_id\n"
        "  AND owner_id = :owner_id\n"
        "  AND subject_user_id = :subject_user_id\n"
        "  AND (   (:scope_bound AND scope_kind = 'project' AND project_id = :project_id)\n"
        "       OR (NOT :scope_bound AND scope_kind = 'workspace' AND project_id IS NULL) )"
    )


_REJECTED_SCAN_SQL = (
    "WHERE workspace_id = :workspace_id AND owner_id = :owner_id"
    "  AND subject_user_id = :subject_user_id"
    "  AND status = 'rejected' AND rejected_at >= :since"
    " ORDER BY rejected_at DESC LIMIT :limit"
)
"""Rejection suppression scans BOTH scope kinds for one subject, so it is the one read that
deliberately does not use :func:`_dream_scope_sql`.  It still carries ``subject_user_id``: the
bucket is the person, never the project."""


def scope_kind_for(scope: ResolvedScope) -> str:
    """D9.  Derived, never asserted: a body that could assert it would be a second source of
    truth for the one thing that decides which corpus a run reads."""
    return SCOPE_KIND_PROJECT if (scope.is_bound() and scope.project_id) else SCOPE_KIND_WORKSPACE


def _scope_params(scope: ResolvedScope) -> dict[str, Any]:
    bound = scope_kind_for(scope) == SCOPE_KIND_PROJECT
    return {
        "workspace_id": scope.workspace_id,
        "owner_id": scope.owner_id,
        "subject_user_id": scope.subject_user_id,
        "scope_bound": bound,
        "project_id": scope.project_id if bound else None,
    }


def scope_key_for(scope: ResolvedScope) -> str:
    """``"<scope_kind>:<project_id or ''>"`` — the in-flight uniqueness key for a run."""
    kind = scope_kind_for(scope)
    return f"{kind}:{scope.project_id or ''}" if kind == SCOPE_KIND_PROJECT else f"{kind}:"


# --------------------------------------------------------------------------- row coercions


def _text_value(raw: Any, default: str = "") -> str:
    if raw is None:
        return default
    value = str(raw).strip()
    return value or default


def _opt_text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _int_value(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _aware(raw: Any) -> datetime | None:
    if not isinstance(raw, datetime):
        return None
    return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)


def _json_list(raw: Any) -> list[Any]:
    """JSONB comes back as a list; a TEXT column or an older row may come back as a string."""
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _json_map(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _status_from_row(raw: Any) -> DreamProposalStatus:
    value = _text_value(raw, DreamProposalStatus.PROPOSED.value)
    try:
        return DreamProposalStatus(value)
    except ValueError:
        return DreamProposalStatus.PROPOSED


def _nonresponse_from_row(raw: Any) -> NonresponseState:
    value = _text_value(raw, NonresponseState.NEVER_SURFACED.value)
    try:
        return NonresponseState(value)
    except ValueError:
        return NonresponseState.NEVER_SURFACED


def _projection_from_row(row: Any) -> DreamProposalProjection:
    """``warn_return_any`` binds under this repo's mypy config, so every field is coerced."""
    return DreamProposalProjection(
        proposal_id=str(row["id"]),
        workspace_id=_text_value(row["workspace_id"]),
        owner_id=_text_value(row["owner_id"]),
        subject_user_id=_text_value(row["subject_user_id"]),
        project_id=_opt_text(row["project_id"]),
        scope_kind=_text_value(row["scope_kind"], SCOPE_KIND_WORKSPACE),
        session_id=_text_value(row["session_id"]),
        revision=_int_value(row["revision"]),
        status=_status_from_row(row["status"]),
        nonresponse=_nonresponse_from_row(row["nonresponse_state"]),
        surfaced_count=_int_value(row["surfaced_count"]),
        surfaced_attested=bool(row["surfaced_attested"]),
        first_surfaced_at=_aware(row["first_surfaced_at"]),
        last_surfaced_at=_aware(row["last_surfaced_at"]),
        title=_text_value(row["title"]),
        connection_text=_text_value(row["connection_text"]),
        benefit_text=_text_value(row["benefit_text"]),
        first_step=_text_value(row["first_step"]),
        citations=citations_from_json(_json_list(row["citations_json"])),
        theme_tokens=tuple(str(token) for token in _json_list(row["theme_tokens_json"])),
        evidence_basis=_text_value(row["evidence_basis"]),
        evidence_revision=_text_value(row["evidence_revision"]),
        evidence_cutoff_at=_aware(row["evidence_cutoff_at"]),
        supersedes_proposal_id=_opt_text(row["supersedes_proposal_id"]),
        repropose_depth=_int_value(row["repropose_depth"]),
        snooze_until=_aware(row["snooze_until"]),
        expires_at=_aware(row["expires_at"]),
        accepted_at=_aware(row["accepted_at"]),
        rejected_at=_aware(row["rejected_at"]),
        rejection_reason=_text_value(row["rejection_reason"]),
        task_id=_opt_text(row["task_id"]),
        objective_hash=_opt_text(row["objective_hash"]),
        plan_root_goal_id=_opt_text(row["plan_root_goal_id"]),
        pursuit_started_at=_aware(row["pursuit_started_at"]),
        completed_at=_aware(row["completed_at"]),
        abandoned_at=_aware(row["abandoned_at"]),
        abandon_reason=_text_value(row["abandon_reason"]),
        withdrawn_reason=_text_value(row["withdrawn_reason"]),
        schema_version=_text_value(row["schema_version"], DREAM_SCHEMA_VERSION),
        policy_revision=_text_value(row["policy_revision"], DREAM_POLICY_REVISION),
    )


def _event_from_row(row: Any) -> DreamProposalEvent:
    occurred = _aware(row["occurred_at"]) or datetime.now(tz=UTC)
    return DreamProposalEvent(
        seq=_int_value(row["seq"]),
        kind=_text_value(row["kind"]),
        payload=_json_map(row["payload_json"]),
        occurred_at=occurred,
        actor=_text_value(row["actor"]),
        actor_class=_text_value(row["actor_class"], "system"),
        source_event_id=_opt_text(row["source_event_id"]),
        run_id=_opt_text(row["run_id"]),
    )


def _run_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": _text_value(row["workspace_id"]),
        "owner_id": _text_value(row["owner_id"]),
        "subject_user_id": _text_value(row["subject_user_id"]),
        "project_id": _opt_text(row["project_id"]),
        "scope_kind": _text_value(row["scope_kind"]),
        "scope_key": _text_value(row["scope_key"]),
        "session_id": _text_value(row["session_id"]),
        "planning_job_id": _opt_text(row["planning_job_id"]),
        "state": _text_value(row["state"]),
        "refusal_reason": _text_value(row["refusal_reason"]),
        "pool_size": _int_value(row["pool_size"]),
        "pool": _json_list(row["pool_json"]),
        "pool_drops": _json_map(row["pool_drops_json"]),
        "evidence_revision": _text_value(row["evidence_revision"]),
        "evidence_cutoff_at": _aware(row["evidence_cutoff_at"]),
        "candidates_returned": _int_value(row["candidates_returned"]),
        "proposals_written": _int_value(row["proposals_written"]),
        "refusals": _json_map(row["refusals_json"]),
        "model_provider": _text_value(row["model_provider"]),
        "prompt_hash": _text_value(row["prompt_hash"]),
        "started_at": _aware(row["started_at"]),
        "finished_at": _aware(row["finished_at"]),
        "schema_version": _text_value(row["schema_version"], DREAM_SCHEMA_VERSION),
    }


# --------------------------------------------------------------------------- reads


def load_proposal(
    db: Session, *, scope: ResolvedScope, proposal_id: str
) -> tuple[DreamProposalProjection, int, str] | None:
    """``(projection, highest_seq, source_revision)`` or ``None``.

    Takes the whole :class:`ResolvedScope` rather than a bare ``(workspace_id, owner_id)`` pair,
    so the detail route and the transition route are filtered by the same predicate as the list
    route.  An earlier draft omitted the subject and the project here, and that is a
    cross-subject read on any stack where one owner speaks for more than one subject.
    """
    params = _scope_params(scope)
    params["proposal_id"] = _as_uuid(proposal_id)
    if params["proposal_id"] is None:
        return None
    row = db.execute(
        text(
            f"SELECT {_PROPOSAL_COLUMNS} FROM dream_proposals "
            f"{_dream_scope_sql()} AND id = :proposal_id"
        ),
        params,
    ).mappings().first()
    if row is None:
        return None
    return (
        _projection_from_row(row),
        _int_value(row["highest_seq"]),
        _text_value(row["source_revision"]),
    )


def load_proposal_events(
    db: Session, *, proposal_id: str, max_events: int = MAX_DREAM_EVENTS_PER_FOLD
) -> list[DreamProposalEvent]:
    """The same pinning the fold applies, expressed in SQL.

    ``UNION ALL``, not ``UNION``: the fold already dedups on ``(seq, kind, payload_digest)``.
    The tail size is the constant ``max_events - len(PINNED_DREAM_KINDS)`` on both sides, which
    is the only formula SQL and the fold can evaluate identically without a second round trip.
    """
    identifier = _as_uuid(proposal_id)
    if identifier is None:
        return []
    tail = max(1, int(max_events) - len(PINNED_DREAM_KINDS))
    rows = db.execute(
        text(
            """
            (SELECT DISTINCT ON (kind)
                    seq, kind, payload_json, occurred_at, actor, actor_class,
                    source_event_id, run_id
               FROM dream_proposal_events
              WHERE proposal_id = :proposal_id AND kind = ANY(:pinned)
              ORDER BY kind, seq DESC)
            UNION ALL
            (SELECT seq, kind, payload_json, occurred_at, actor, actor_class,
                    source_event_id, run_id
               FROM dream_proposal_events
              WHERE proposal_id = :proposal_id
              ORDER BY seq DESC
              LIMIT :tail)
            """
        ),
        {"proposal_id": identifier, "pinned": list(PINNED_DREAM_KIND_VALUES), "tail": tail},
    ).mappings().all()
    return [_event_from_row(row) for row in rows]


def rebuild_dream_proposal(
    db: Session, *, scope: ResolvedScope, proposal_id: str, after_surfaces: int
) -> DreamFoldResult | None:
    """SELECT the row, SELECT its events, fold.  Read-only; the deterministic reconstruction."""
    loaded = load_proposal(db, scope=scope, proposal_id=proposal_id)
    if loaded is None:
        return None
    projection, _highest_seq, _source_revision = loaded
    events = load_proposal_events(db, proposal_id=proposal_id)
    return fold_dream_proposal(
        events,
        proposal_id=projection.proposal_id,
        workspace_id=projection.workspace_id,
        owner_id=projection.owner_id,
        subject_user_id=projection.subject_user_id,
        session_id=projection.session_id,
        revision=projection.revision,
        now=datetime.now(tz=UTC),
        after_surfaces=after_surfaces,
    )


def load_live_proposals(
    db: Session, *, scope: ResolvedScope, limit: int
) -> list[DreamProposalProjection]:
    params = _scope_params(scope)
    params["statuses"] = sorted(LIVE_STATUSES)
    params["limit"] = max(1, int(limit))
    rows = db.execute(
        text(
            f"SELECT {_PROPOSAL_COLUMNS} FROM dream_proposals "
            f"{_dream_scope_sql()} AND status = ANY(:statuses) "
            "ORDER BY updated_at DESC LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return [_projection_from_row(row) for row in rows]


def load_rejected_proposals(
    db: Session, *, scope: ResolvedScope, since: datetime, limit: int
) -> list[DreamProposalProjection]:
    """Rejections, across BOTH scope kinds for the same ``(workspace, owner, subject)``.

    Deliberately not filtered by ``_dream_scope_sql()``: a theme rejected while working unbound
    is the same theme when it comes back inside a project, and bucketing the scan by scope kind
    is how a rejection became invisible to the only mode that can currently produce anything.
    """
    rows = db.execute(
        text(
            f"SELECT {_PROPOSAL_COLUMNS} FROM dream_proposals "
            f"{_REJECTED_SCAN_SQL}"
        ),
        {
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "subject_user_id": scope.subject_user_id,
            "since": since,
            "limit": max(1, int(limit)),
        },
    ).mappings().all()
    return [_projection_from_row(row) for row in rows]


def list_proposals(
    db: Session,
    *,
    scope: ResolvedScope,
    statuses: Sequence[str],
    include_history: bool,
    now: datetime,
    limit: int,
) -> list[DreamProposalProjection]:
    """The list route's read.  **Writes nothing** (D21).

    A past-TTL row that the sweep has not reached yet is hidden here rather than expired here:
    a GET with a side effect is the kind of thing that gets "optimised" into a cache six months
    later, and the surfaced record is the only thing standing between nonresponse and rejection.
    """
    wanted = [str(item) for item in statuses if str(item).strip()] or sorted(LIVE_STATUSES)
    params = _scope_params(scope)
    params["statuses"] = wanted
    params["limit"] = max(1, int(limit))
    params["now"] = now
    ttl_clause = "" if include_history else "  AND (expires_at IS NULL OR expires_at > :now)\n"
    rows = db.execute(
        text(
            f"SELECT {_PROPOSAL_COLUMNS} FROM dream_proposals "
            f"{_dream_scope_sql()} AND status = ANY(:statuses)\n"
            f"{ttl_clause}"
            "ORDER BY updated_at DESC LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return [_projection_from_row(row) for row in rows]


def count_pending_proposals(db: Session, *, scope: ResolvedScope) -> int:
    """One indexed ``COUNT(*)`` for ``TakeoverStepResponse.dream_proposals_pending``.

    A count, not a payload: putting proposal text on the turn response would put quotes in front
    of the owner without the citation validation the two GET routes run.
    """
    row = db.execute(
        text(
            "SELECT COUNT(*) AS pending FROM dream_proposals "
            f"{_dream_scope_sql()} AND status IN ('proposed', 'surfaced')"
        ),
        _scope_params(scope),
    ).mappings().first()
    return _int_value(row["pending"]) if row is not None else 0


# --------------------------------------------------------------------------- the CAS write


def _as_uuid(value: Any) -> uuid.UUID | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


def _insert_event(
    db: Session,
    *,
    proposal_id: str,
    workspace_id: str,
    owner_id: str,
    event: DreamProposalEvent,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO dream_proposal_events(
                id, proposal_id, workspace_id, owner_id, seq, kind, payload_json,
                actor, actor_class, source_event_id, run_id, occurred_at, schema_version
            )
            VALUES(
                :id, :proposal_id, :workspace_id, :owner_id, :seq, :kind,
                CAST(:payload_json AS JSONB), :actor, :actor_class, :source_event_id,
                :run_id, :occurred_at, :schema_version
            )
            ON CONFLICT (proposal_id, seq) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "proposal_id": uuid.UUID(str(proposal_id)),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "seq": int(event.seq),
            "kind": str(event.kind),
            "payload_json": canonical_json(dict(event.payload)),
            "actor": event.actor,
            "actor_class": event.actor_class,
            "source_event_id": _as_uuid(event.source_event_id),
            "run_id": _as_uuid(event.run_id),
            "occurred_at": event.occurred_at,
            "schema_version": DREAM_SCHEMA_VERSION,
        },
    )


def _cas_update(
    db: Session, *, proposal_id: str, locked_revision: int, write: DreamProposalWrite, now: datetime
) -> int:
    """The one row-update statement in the tree, and it always compares the revision.

    Every projection field is written from the fold, so the row can never drift from its log.
    """
    projection = write.projection
    result = db.execute(
        text(
            """
            UPDATE dream_proposals
               SET revision = :next_revision,
                   highest_seq = :highest_seq,
                   status = :status,
                   nonresponse_state = :nonresponse_state,
                   surfaced_count = :surfaced_count,
                   surfaced_attested = :surfaced_attested,
                   first_surfaced_at = :first_surfaced_at,
                   last_surfaced_at = :last_surfaced_at,
                   title = :title,
                   connection_text = :connection_text,
                   benefit_text = :benefit_text,
                   first_step = :first_step,
                   citations_json = CAST(:citations_json AS JSONB),
                   citation_count = :citation_count,
                   evidence_basis = :evidence_basis,
                   evidence_revision = :evidence_revision,
                   evidence_cutoff_at = :evidence_cutoff_at,
                   theme_tokens_json = CAST(:theme_tokens_json AS JSONB),
                   supersedes_proposal_id = :supersedes_proposal_id,
                   repropose_depth = :repropose_depth,
                   snooze_until = :snooze_until,
                   expires_at = :expires_at,
                   accepted_at = :accepted_at,
                   rejected_at = :rejected_at,
                   rejection_reason = :rejection_reason,
                   task_id = :task_id,
                   objective_hash = :objective_hash,
                   plan_root_goal_id = :plan_root_goal_id,
                   pursuit_started_at = :pursuit_started_at,
                   completed_at = :completed_at,
                   abandoned_at = :abandoned_at,
                   abandon_reason = :abandon_reason,
                   withdrawn_reason = :withdrawn_reason,
                   source_revision = :source_revision,
                   updated_at = :now
             WHERE id = :proposal_id AND revision = :expected_revision
            """
        ),
        {
            "next_revision": int(write.next_revision),
            "highest_seq": _highest_seq(write),
            "status": str(projection.status),
            "nonresponse_state": str(projection.nonresponse),
            "surfaced_count": int(projection.surfaced_count),
            "surfaced_attested": bool(projection.surfaced_attested),
            "first_surfaced_at": projection.first_surfaced_at,
            "last_surfaced_at": projection.last_surfaced_at,
            "title": projection.title,
            "connection_text": projection.connection_text,
            "benefit_text": projection.benefit_text,
            "first_step": projection.first_step,
            "citations_json": canonical_json(citations_to_json(projection.citations)),
            "citation_count": len(projection.citations),
            "evidence_basis": projection.evidence_basis,
            "evidence_revision": projection.evidence_revision,
            "evidence_cutoff_at": projection.evidence_cutoff_at,
            "theme_tokens_json": canonical_json(list(projection.theme_tokens)),
            "supersedes_proposal_id": _as_uuid(projection.supersedes_proposal_id),
            "repropose_depth": int(projection.repropose_depth),
            "snooze_until": projection.snooze_until,
            "expires_at": projection.expires_at,
            "accepted_at": projection.accepted_at,
            "rejected_at": projection.rejected_at,
            "rejection_reason": projection.rejection_reason,
            "task_id": projection.task_id,
            "objective_hash": projection.objective_hash,
            "plan_root_goal_id": _as_uuid(projection.plan_root_goal_id),
            "pursuit_started_at": projection.pursuit_started_at,
            "completed_at": projection.completed_at,
            "abandoned_at": projection.abandoned_at,
            "abandon_reason": projection.abandon_reason,
            "withdrawn_reason": projection.withdrawn_reason,
            "source_revision": write.source_revision,
            "now": now,
            "proposal_id": uuid.UUID(str(proposal_id)),
            "expected_revision": int(locked_revision),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _highest_seq(write: DreamProposalWrite) -> int:
    return max((int(event.seq) for event in write.events), default=write.next_seq_start - 1)


def apply_dream_events(
    db: Session,
    *,
    scope: ResolvedScope,
    proposal_id: str,
    new_events: Sequence[DreamProposalEvent],
    now: datetime,
    after_surfaces: int,
    expected_revision: int | None = None,
    retry_once: bool = False,
) -> DreamProposalWrite:
    """Compare-and-swap write of the projection plus its new events.

    Same shape as ``task_state_store.apply_task_state_events`` — savepoint, ``SELECT ... FOR
    UPDATE``, caller precondition, insert events, CAS the row, commit or roll back the savepoint
    — because that is the shape that has already been reasoned about in this tree, and a second
    invented bracket would be a second thing to get wrong.  ``expected_revision=None`` means
    "no caller precondition": the CAS runs against the revision read under *this* call's own
    lock, so two writes in one request cannot conflict with each other.
    """
    savepoint = db.begin_nested()
    params = _scope_params(scope)
    identifier = _as_uuid(proposal_id)
    if identifier is None:
        savepoint.rollback()
        raise DreamRevisionConflict(
            proposal_id=str(proposal_id),
            expected_revision=int(expected_revision or 0),
            actual_revision=-1,
        )
    params["proposal_id"] = identifier
    locked = db.execute(
        text(
            "SELECT id, revision, highest_seq, surfaced_attested FROM dream_proposals "
            f"{_dream_scope_sql()} AND id = :proposal_id FOR UPDATE"
        ),
        params,
    ).mappings().first()
    if locked is None:
        savepoint.rollback()
        raise DreamRevisionConflict(
            proposal_id=str(proposal_id),
            expected_revision=int(expected_revision or 0),
            actual_revision=-1,
        )

    locked_revision = _int_value(locked["revision"])
    highest_seq = _int_value(locked["highest_seq"])

    if expected_revision is not None and int(expected_revision) != locked_revision:
        savepoint.rollback()
        if not retry_once:
            raise DreamRevisionConflict(
                proposal_id=str(proposal_id),
                expected_revision=int(expected_revision),
                actual_revision=locked_revision,
            )
        return apply_dream_events(
            db,
            scope=scope,
            proposal_id=proposal_id,
            new_events=new_events,
            now=now,
            after_surfaces=after_surfaces,
            expected_revision=None,
            retry_once=False,
        )

    prior_events = load_proposal_events(db, proposal_id=str(proposal_id))
    write = prepare_dream_write(
        prior_events,
        new_events,
        proposal_id=str(proposal_id),
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        subject_user_id=scope.subject_user_id,
        session_id=str(scope.task_id or ""),
        expected_revision=locked_revision,
        highest_seq=highest_seq,
        now=now,
        after_surfaces=after_surfaces,
    )
    for event in write.events:
        _insert_event(
            db,
            proposal_id=str(proposal_id),
            workspace_id=scope.workspace_id,
            owner_id=scope.owner_id,
            event=event,
        )
    rowcount = _cas_update(
        db, proposal_id=str(proposal_id), locked_revision=locked_revision, write=write, now=now
    )
    if rowcount == 1:
        savepoint.commit()
        return write

    savepoint.rollback()
    if not retry_once:
        raise DreamRevisionConflict(
            proposal_id=str(proposal_id),
            expected_revision=locked_revision,
            actual_revision=-1,
        )
    return apply_dream_events(
        db,
        scope=scope,
        proposal_id=proposal_id,
        new_events=new_events,
        now=now,
        after_surfaces=after_surfaces,
        expected_revision=None,
        retry_once=False,
    )


# --------------------------------------------------------------------------- minting


def mint_proposal(
    db: Session,
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
    """INSERT the row at revision 0, then append its one ``proposed`` event through the CAS.

    The row is created empty-of-content and the ``proposed`` event fills it, so the projection
    is a fold of the log from the first revision onwards rather than an INSERT the log has to
    agree with afterwards.
    """
    proposal_id = uuid.uuid4()
    kind = scope_kind_for(scope)
    db.execute(
        text(
            """
            INSERT INTO dream_proposals(
                id, workspace_id, owner_id, subject_user_id, project_id, scope_kind,
                session_id, run_id, revision, highest_seq, status, nonresponse_state,
                title, created_at, updated_at, policy_revision, schema_version
            )
            VALUES(
                :id, :workspace_id, :owner_id, :subject_user_id, :project_id, :scope_kind,
                :session_id, :run_id, 0, 0, 'proposed', 'never_surfaced',
                :title, :now, :now, :policy_revision, :schema_version
            )
            """
        ),
        {
            "id": proposal_id,
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "subject_user_id": scope.subject_user_id,
            "project_id": scope.project_id if kind == SCOPE_KIND_PROJECT else None,
            "scope_kind": kind,
            "session_id": session_id,
            "run_id": _as_uuid(run_id),
            "title": candidate.title,
            "now": now,
            "policy_revision": DREAM_POLICY_REVISION,
            "schema_version": DREAM_SCHEMA_VERSION,
        },
    )
    expires_at = now + timedelta(days=max(1, int(ttl_days)))
    proposed = DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.PROPOSED,
        payload={
            "title": candidate.title,
            "connection_text": candidate.connection_text,
            "benefit_text": candidate.benefit_text,
            "first_step": candidate.first_step,
            "citations": citations_to_json(candidate.citations),
            "theme_tokens": list(candidate.theme_tokens),
            "evidence_basis": candidate.evidence_basis,
            "evidence_revision": evidence_revision,
            "evidence_cutoff_at": _iso(evidence_cutoff_at),
            "project_id": scope.project_id if kind == SCOPE_KIND_PROJECT else None,
            "scope_kind": kind,
            "supersedes_proposal_id": supersedes_proposal_id,
            "repropose_depth": max(0, int(repropose_depth)),
            "expires_at": _iso(expires_at),
        },
        occurred_at=now,
        actor="system",
        actor_class="system",
        run_id=str(run_id) if run_id else None,
    )
    apply_dream_events(
        db,
        scope=scope,
        proposal_id=str(proposal_id),
        new_events=[proposed],
        now=now,
        after_surfaces=3,
    )
    return str(proposal_id)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------- surfacing


def append_surfaced(
    db: Session,
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
    """Record that the proposal was put in front of the owner.  Rate-limited, never a verdict.

    Returns ``None`` when the interval has not elapsed, so a polling client cannot inflate the
    count.  The ceiling on abuse is stated rather than assumed: an executor spamming this can
    only drive nonresponse to ``ignored``, whose sole effect is a *longer* re-surface interval.
    It cannot reject, cannot suppress a theme, cannot expire anything early, and cannot appear
    as the owner's answer.
    """
    loaded = load_proposal(db, scope=scope, proposal_id=proposal_id)
    if loaded is None:
        return None
    projection, _highest_seq, _source_revision = loaded
    state = nonresponse_state(
        surfaced_count=projection.surfaced_count,
        last_surfaced_at=projection.last_surfaced_at,
        now=now,
        after_surfaces=after_surfaces,
    )
    interval = resurface_interval_hours(state, min_hours=min_hours, ignored_hours=ignored_hours)
    if projection.last_surfaced_at is not None and interval > 0:
        if now - projection.last_surfaced_at < timedelta(hours=interval):
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
        db,
        scope=scope,
        proposal_id=proposal_id,
        new_events=[event],
        now=now,
        after_surfaces=after_surfaces,
    )


# --------------------------------------------------------------------------- citation validation


def validate_citations_cheap(
    db: Session, *, scope: ResolvedScope, citations: Sequence[DreamCitation]
) -> tuple[list[DreamCitation], list[str]]:
    """Stage 2, cheap arm: one query, no decrypt.

    A citation is dropped when the event is gone, its sensitivity has risen above the ceiling,
    the workspace no longer matches, **the receipt binding is gone**, the receipt's origin kind
    is no longer admissible, its content hash differs from the stored one, or its project no
    longer matches the proposal's project mode.
    """
    if not citations:
        return [], []
    from .config import get_settings

    settings = get_settings()
    ids = [_as_uuid(citation.event_id) for citation in citations]
    wanted = [identifier for identifier in ids if identifier is not None]
    if not wanted:
        return [], ["citation_event_missing"] * len(citations)
    rows = db.execute(
        text(
            """
            SELECT e.id AS event_id, e.sensitivity AS sensitivity,
                   r.id AS receipt_id, r.content_sha256 AS content_sha256,
                   r.origin_kind AS origin_kind, r.project_id AS project_id
              FROM events e
              LEFT JOIN trusted_input_receipts r
                ON r.event_id = e.id
               AND r.workspace_id = :workspace_id
               AND r.owner_id = :owner_id
               AND r.subject_user_id = :subject_user_id
             WHERE e.id = ANY(:event_ids)
               AND e.context->>'_tce_workspace' = :workspace_id
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "subject_user_id": scope.subject_user_id,
            "event_ids": wanted,
        },
    ).mappings().all()
    by_event = {str(row["event_id"]): row for row in rows}
    max_sensitivity = int(getattr(settings, "block_sensitivity", 3)) - 1
    bound = scope_kind_for(scope) == SCOPE_KIND_PROJECT
    surviving: list[DreamCitation] = []
    dropped: list[str] = []
    for citation in citations:
        row = by_event.get(str(citation.event_id))
        if row is None:
            dropped.append("citation_event_missing")
            continue
        if _int_value(row["sensitivity"]) > max_sensitivity:
            dropped.append("citation_sensitivity_raised")
            continue
        if row["receipt_id"] is None:
            dropped.append("citation_receipt_missing")
            continue
        if _text_value(row["origin_kind"]) not in ADMISSIBLE_ORIGIN_KINDS:
            dropped.append("citation_origin_kind_inadmissible")
            continue
        if _text_value(row["content_sha256"]) != citation.content_sha256:
            dropped.append("citation_content_changed")
            continue
        receipt_project = _opt_text(row["project_id"])
        if bound and receipt_project != scope.project_id:
            dropped.append("citation_project_changed")
            continue
        if not bound and receipt_project is not None:
            dropped.append("citation_project_changed")
            continue
        surviving.append(citation)
    return surviving, dropped


def validate_citations_deep(
    db: Session, *, scope: ResolvedScope, citations: Sequence[DreamCitation]
) -> tuple[list[DreamCitation], list[str]]:
    """Cheap, then decrypt each surviving body and re-apply containment and the voice check.

    This is the expensive arm and it is bounded on purpose: it runs at generation, in the detail
    route and inside the sweep, never in the list route.  The wire says which arm ran, so a
    reader is never left guessing how much the system just proved.
    """
    surviving, dropped = validate_citations_cheap(db, scope=scope, citations=citations)
    if not surviving:
        return [], dropped
    from .config import get_settings

    settings = get_settings()
    quote_max_chars = int(getattr(settings, "dream_quote_max_chars", 200))
    wanted = [_as_uuid(citation.event_id) for citation in surviving]
    ids = [identifier for identifier in wanted if identifier is not None]
    rows = db.execute(
        text("SELECT id, payload FROM events WHERE id = ANY(:event_ids)"),
        {"event_ids": ids},
    ).mappings().all()
    bodies: dict[str, str] = {}
    for row in rows:
        try:
            payload = maybe_decrypt_payload(dict(row["payload"] or {}))
        except Exception:  # noqa: BLE001 — an unreadable body drops the citation, never the turn
            continue
        bodies[str(row["id"])] = str(payload.get("input_excerpt") or "")
    kept: list[DreamCitation] = []
    for citation in surviving:
        body = bodies.get(str(citation.event_id), "")
        if not body:
            dropped.append("citation_body_unreadable")
            continue
        if not quote_is_contained(citation.quote, body, max_chars=quote_max_chars):
            dropped.append("quote_not_found")
            continue
        if voice_violations(citation.quote):
            dropped.append("quote_voice_violation")
            continue
        kept.append(citation)
    return kept, dropped


# --------------------------------------------------------------------------- the corpus (M0)


def subject_has_project_receipts(db: Session, *, scope: ResolvedScope) -> bool:
    """D9's entitlement check, and it is not decorative.

    ``project_context`` accepts any client-supplied ``proj_[a-f0-9]{24}`` verbatim as the
    project identity, with no server-side check.  Without this, a caller could bind a
    generation run to a project they have never spoken into.
    """
    if not (scope.is_bound() and scope.project_id):
        return False
    row = db.execute(
        text(
            """
            SELECT 1 AS ok FROM trusted_input_receipts
             WHERE workspace_id = :workspace_id
               AND owner_id = :owner_id
               AND subject_user_id = :subject_user_id
               AND project_id = :project_id
             LIMIT 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "subject_user_id": scope.subject_user_id,
            "project_id": scope.project_id,
        },
    ).mappings().first()
    return row is not None


def select_candidate_messages(
    db: Session,
    *,
    scope: ResolvedScope,
    scope_kind: str,
    limit: int,
    min_chars: int,
    max_chars: int,
    max_sensitivity: int,
) -> tuple[list[PoolMessage], dict[str, int]]:
    """M0.  The only path by which a message reaches a proposal.

    **The receipt is the only admission.**  There is no ``task_type`` arm and no ``_tce_owner``
    arm, because the event corpus is executor-writable: an executor holding an ordinary API
    token can POST a fabricated "owner message", and a system that quoted it back would be
    certifying the forger's own words as the owner's.  The receipt is written by the
    host-capture credential, which the executor client is structurally forbidden from holding.

    ``events`` contributes exactly three things — the encrypted body, the timestamp and the
    sensitivity ceiling.  The receipt contributes the identity, the hash and the project.
    """
    project_clause = (
        "AND r.project_id = :project_id"
        if scope_kind == SCOPE_KIND_PROJECT
        else "AND (r.project_id IS NULL OR r.project_id = '')"
    )
    params: dict[str, Any] = {
        "workspace_id": scope.workspace_id,
        "owner_id": scope.owner_id,
        "subject_user_id": scope.subject_user_id,
        "max_sensitivity": int(max_sensitivity),
        "limit": max(1, int(limit)),
        "origin_kinds": list(ADMISSIBLE_ORIGIN_KINDS),
    }
    if scope_kind == SCOPE_KIND_PROJECT:
        params["project_id"] = scope.project_id
    rows = db.execute(
        text(
            f"""
            SELECT e.id AS event_id, e.ts AS ts, e.payload AS payload,
                   r.id AS receipt_id,
                   r.content_sha256 AS content_sha256,
                   r.origin_kind AS origin_kind,
                   r.observed_at AS observed_at,
                   r.content_truncated AS content_truncated
              FROM events e
              JOIN LATERAL (
                    SELECT r.id, r.content_sha256, r.origin_kind, r.observed_at,
                           r.content_truncated
                      FROM trusted_input_receipts r
                     WHERE r.event_id = e.id
                       AND r.workspace_id = :workspace_id
                       AND r.owner_id = :owner_id
                       AND r.subject_user_id = :subject_user_id
                       AND r.origin_kind = ANY(:origin_kinds)
                       {project_clause}
                     ORDER BY r.ingested_at DESC
                     LIMIT 1
                   ) r ON TRUE
             WHERE e.sensitivity <= :max_sensitivity
               AND e.context->>'_tce_workspace' = :workspace_id
             ORDER BY e.ts DESC
             LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    drops: dict[str, int] = {}
    pool: list[PoolMessage] = []
    seen: set[str] = set()
    for row in rows:
        try:
            payload = maybe_decrypt_payload(dict(row["payload"] or {}))
        except Exception:  # noqa: BLE001 — an unreadable body is a pool drop, never a failure
            drops["undecryptable"] = drops.get("undecryptable", 0) + 1
            continue
        body = str(payload.get("input_excerpt") or "").strip()
        if not body:
            drops["undecryptable"] = drops.get("undecryptable", 0) + 1
            continue
        if is_harness_text(body):
            drops["harness_text"] = drops.get("harness_text", 0) + 1
            continue
        if bool(row["content_truncated"]):
            drops["truncated_paste"] = drops.get("truncated_paste", 0) + 1
            continue
        if len(body) < int(min_chars):
            drops["too_short"] = drops.get("too_short", 0) + 1
            continue
        content_sha256 = _text_value(row["content_sha256"])
        if content_sha256 and content_sha256 in seen:
            drops["duplicate_message"] = drops.get("duplicate_message", 0) + 1
            continue
        seen.add(content_sha256)
        observed_at = _aware(row["observed_at"]) or _aware(row["ts"]) or datetime.now(tz=UTC)
        pool.append(
            PoolMessage(
                n=len(pool) + 1,
                event_id=str(row["event_id"]),
                receipt_id=str(row["receipt_id"]),
                content_sha256=content_sha256,
                origin_kind=_text_value(row["origin_kind"]),
                observed_at=observed_at,
                # Truncated for the prompt ONLY.  The persisted hash is always the receipt's
                # hash of the full original, so it stays stable under later redaction.
                body=body[: max(1, int(max_chars))],
            )
        )
    return pool, drops


def pool_evidence_revision(pool: Sequence[PoolMessage]) -> tuple[str, datetime | None]:
    """P1's ``evidence_revision`` over the frozen pool.  Reuse, not new machinery."""
    return evidence_revision([{"id": item.event_id, "ts": item.observed_at} for item in pool])


def pool_to_json(pool: Sequence[PoolMessage]) -> list[dict[str, Any]]:
    """What ``dream_generation_runs.pool_json`` stores: the numbering, and **no body text**.

    A citation number stays auditable months later without the run row becoming a second,
    unencrypted copy of the owner's words.
    """
    return [
        {
            "n": item.n,
            "event_id": item.event_id,
            "receipt_id": item.receipt_id,
            "content_sha256": item.content_sha256,
            "origin_kind": item.origin_kind,
            "observed_at": _iso(item.observed_at),
        }
        for item in pool
    ]


# --------------------------------------------------------------------------- generation runs


def start_generation_run(
    db: Session,
    *,
    scope: ResolvedScope,
    session_id: str,
    scope_kind: str,
    stale_minutes: int,
    now: datetime,
) -> str:
    """Take the single in-flight slot for this scope.  Called by the ROUTE, not the worker.

    The run row exists before anything can refuse, which is what makes every refusal
    observable: with the run created after the model gate, the shipped default
    ``takeover_dream_llm_enabled = False`` produced no row, no refusal and nothing at all — the
    exact "no proposals today" silence a broken generation path must never be able to produce.

    A crashed run does not wedge the scope: ``running`` rows older than ``stale_minutes`` are
    swept to ``failed``/``run_abandoned`` first.  Raises when a live run already holds the slot.
    """
    key = scope_key_for(scope)
    cutoff = now - timedelta(minutes=max(1, int(stale_minutes)))
    db.execute(
        text(
            """
            UPDATE dream_generation_runs
               SET state = 'failed', refusal_reason = 'run_abandoned', finished_at = :now
             WHERE workspace_id = :workspace_id
               AND owner_id = :owner_id
               AND scope_key = :scope_key
               AND state = 'running'
               AND started_at < :cutoff
            """
        ),
        {
            "now": now,
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "scope_key": key,
            "cutoff": cutoff,
        },
    )
    run_id = uuid.uuid4()
    savepoint = db.begin_nested()
    try:
        db.execute(
            text(
                """
            INSERT INTO dream_generation_runs(
                id, workspace_id, owner_id, subject_user_id, project_id, scope_kind, scope_key,
                session_id, state, started_at, schema_version
            )
            VALUES(
                :id, :workspace_id, :owner_id, :subject_user_id, :project_id, :scope_kind,
                :scope_key, :session_id, 'running', :now, :schema_version
            )
            """
            ),
            {
                "id": run_id,
                "workspace_id": scope.workspace_id,
                "owner_id": scope.owner_id,
                "subject_user_id": scope.subject_user_id,
                "project_id": scope.project_id if scope_kind == SCOPE_KIND_PROJECT else None,
                "scope_kind": scope_kind,
                "scope_key": key,
                "session_id": session_id,
                "now": now,
                "schema_version": DREAM_SCHEMA_VERSION,
            },
        )
    except IntegrityError as exc:
        # ``uq_dream_generation_runs_inflight`` is a partial unique index on
        # (workspace, owner, scope_key) WHERE state = 'running'.  Two overlapping refreshes for
        # one scope would otherwise each read "no duplicate live proposal" and each mint.  The
        # savepoint keeps the outer transaction usable so the caller can still answer.
        savepoint.rollback()
        existing = db.execute(
            text(
                "SELECT id FROM dream_generation_runs "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "  AND scope_key = :scope_key AND state = 'running' "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"workspace_id": scope.workspace_id, "owner_id": scope.owner_id, "scope_key": key},
        ).mappings().first()
        raise DreamRunInFlight(run_id=str(existing["id"]) if existing is not None else "") from exc
    savepoint.commit()
    return str(run_id)


def bind_generation_run(db: Session, *, run_id: str, planning_job_id: str) -> None:
    """How the worker finds its run: ``planning_jobs`` has no free-form payload field and is
    P2's table, which P5 does not edit."""
    db.execute(
        text("UPDATE dream_generation_runs SET planning_job_id = :job_id WHERE id = :run_id"),
        {"job_id": _as_uuid(planning_job_id), "run_id": _as_uuid(run_id)},
    )


def load_generation_run_by_job(db: Session, *, planning_job_id: str) -> dict[str, Any] | None:
    identifier = _as_uuid(planning_job_id)
    if identifier is None:
        return None
    row = db.execute(
        text(
            f"SELECT {_RUN_COLUMNS} FROM dream_generation_runs "
            "WHERE planning_job_id = :job_id ORDER BY started_at DESC LIMIT 1"
        ),
        {"job_id": identifier},
    ).mappings().first()
    return _run_from_row(row) if row is not None else None


def load_generation_run(
    db: Session, *, scope: ResolvedScope, run_id: str
) -> dict[str, Any] | None:
    identifier = _as_uuid(run_id)
    if identifier is None:
        return None
    row = db.execute(
        text(
            f"SELECT {_RUN_COLUMNS} FROM dream_generation_runs "
            "WHERE id = :run_id AND workspace_id = :workspace_id AND owner_id = :owner_id "
            "  AND subject_user_id = :subject_user_id"
        ),
        {
            "run_id": identifier,
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "subject_user_id": scope.subject_user_id,
        },
    ).mappings().first()
    return _run_from_row(row) if row is not None else None


def finish_generation_run(
    db: Session,
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
    """Close the run, recording *why* as well as *what*.

    A generation path that is entirely broken must not read as "no proposals today", so every
    member of the run-refusal vocabulary lands in a real row a reader can find, and every
    candidate the devices dropped lands in ``refusals_json`` with the failing condition.
    """
    db.execute(
        text(
            """
            UPDATE dream_generation_runs
               SET state = :state,
                   refusal_reason = :refusal_reason,
                   pool_size = :pool_size,
                   pool_json = CAST(:pool_json AS JSONB),
                   pool_drops_json = CAST(:pool_drops_json AS JSONB),
                   evidence_revision = :evidence_revision,
                   evidence_cutoff_at = :evidence_cutoff_at,
                   candidates_returned = :candidates_returned,
                   proposals_written = :proposals_written,
                   refusals_json = CAST(:refusals_json AS JSONB),
                   prompt_hash = :prompt_hash,
                   model_provider = :model_provider,
                   finished_at = :now
             WHERE id = :run_id
            """
        ),
        {
            "state": str(state),
            "refusal_reason": str(refusal_reason),
            "pool_size": len(pool),
            "pool_json": canonical_json(pool_to_json(pool)),
            "pool_drops_json": canonical_json(dict(pool_drops)),
            "evidence_revision": str(evidence_revision),
            "evidence_cutoff_at": evidence_cutoff_at,
            "candidates_returned": int(candidates_returned),
            "proposals_written": int(proposals_written),
            "refusals_json": canonical_json(dict(refusals)),
            "prompt_hash": str(prompt_hash),
            "model_provider": str(model_provider),
            "now": now,
            "run_id": _as_uuid(run_id),
        },
    )


# --------------------------------------------------------------------------- the sweep


def sweep_dream_proposals(db: Session, *, scope: ResolvedScope, now: datetime) -> dict[str, int]:
    """The ONLY writer of ``expired``, ``unsnoozed`` and ``withdrawn``.

    Also the only place a GET's *discovery* turns into a write — which is why it lives here and
    not in the list route.  ``accepted`` and ``pursued`` never expire: the owner said yes, and a
    clock does not get to withdraw that.
    """
    from .config import get_settings

    settings = get_settings()
    after_surfaces = int(getattr(settings, "dream_nonresponse_after_surfaces", 3))
    ttl_days = int(getattr(settings, "dream_proposal_ttl_days", 45))
    min_citations = int(getattr(settings, "dream_min_citations", 2))
    counts = {"expired": 0, "unsnoozed": 0, "withdrawn": 0, "revalidated": 0}

    params = _scope_params(scope)
    params["statuses"] = sorted(LIVE_STATUSES)
    rows = db.execute(
        text(
            f"SELECT {_PROPOSAL_COLUMNS} FROM dream_proposals "
            f"{_dream_scope_sql()} AND status = ANY(:statuses) "
            "ORDER BY updated_at ASC"
        ),
        params,
    ).mappings().all()

    for row in rows:
        projection = _projection_from_row(row)
        events: list[DreamProposalEvent] = []
        if (
            projection.status is DreamProposalStatus.SNOOZED
            and projection.snooze_until is not None
            and now >= projection.snooze_until
        ):
            # A snooze the owner asked for must not cause the proposal to die of old age, so
            # the TTL is extended from now rather than left where it was.
            events.append(
                DreamProposalEvent(
                    seq=0,
                    kind=DreamProposalEventKind.UNSNOOZED,
                    payload={"expires_at": _iso(now + timedelta(days=max(1, ttl_days)))},
                    occurred_at=now,
                    actor="system",
                    actor_class="system",
                )
            )
            counts["unsnoozed"] += 1
        elif (
            projection.status
            in {DreamProposalStatus.PROPOSED, DreamProposalStatus.SURFACED}
            and projection.expires_at is not None
            and now >= projection.expires_at
        ):
            events.append(
                DreamProposalEvent(
                    seq=0,
                    kind=DreamProposalEventKind.EXPIRED,
                    payload={},
                    occurred_at=now,
                    actor="system",
                    actor_class="system",
                )
            )
            counts["expired"] += 1
        else:
            surviving, dropped = validate_citations_deep(
                db, scope=scope, citations=projection.citations
            )
            if dropped:
                digest, cutoff = evidence_revision(
                    [{"id": item.event_id, "ts": item.observed_at} for item in surviving]
                )
                events.append(
                    DreamProposalEvent(
                        seq=0,
                        kind=DreamProposalEventKind.EVIDENCE_REVALIDATED,
                        payload={
                            "citations": citations_to_json(surviving),
                            "evidence_basis": evidence_basis_for(surviving),
                            "evidence_revision": digest,
                            "evidence_cutoff_at": _iso(cutoff),
                            "dropped": dropped,
                        },
                        occurred_at=now,
                        actor="system",
                        actor_class="system",
                    )
                )
                counts["revalidated"] += 1
                if len(surviving) < min_citations:
                    # The fold never changes status on its own — a fold that did would make the
                    # event log a lie — so the withdrawal is a second event in the SAME write.
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
                db,
                scope=scope,
                proposal_id=projection.proposal_id,
                new_events=events,
                now=now,
                after_surfaces=after_surfaces,
            )
        except DreamRevisionConflict:
            logger.warning("dream sweep lost a CAS race; retrying next sweep", exc_info=True)
    return counts


# --------------------------------------------------------------------------- pursuit


def reconcile_dream_pursuit(
    db: Session, *, scope: ResolvedScope, session_id: str, now: datetime
) -> dict[str, int]:
    """The one place a proposal meets P2.  Reads the task projection; writes proposal events.

    Never the reverse — that is the lock order, and it is why ``dream_proposals`` sits below
    ``task_states``.

    **Completion is never inferred from plan creation.**  Branch A binds an objective and
    appends ``objective_bound``, which touches no status: a plan now exists and *nothing* is
    pursued or done.  Branch B writes ``completed`` only when P2's own projection says ``DONE``
    and both objective hashes are non-null and equal.  ``None == None`` is explicitly not
    agreement, because if the owner re-points the task the honest answer is ``abandoned``.

    ``TaskStatus.DONE`` is deliberately **not** idle in branch A.  A finished task's
    verification would be inherited by whatever objective was bound onto it next, and the very
    next tick would then read that inherited verification as this proposal's completion, with
    zero steps ever run for it.
    """
    from .config import get_settings

    settings = get_settings()
    after_surfaces = int(getattr(settings, "dream_nonresponse_after_surfaces", 3))
    counts = {"bound": 0, "pursuit_started": 0, "completed": 0, "abandoned": 0}

    params = _scope_params(scope)
    params["statuses"] = [
        DreamProposalStatus.ACCEPTED.value,
        DreamProposalStatus.PURSUED.value,
    ]
    rows = db.execute(
        text(
            f"SELECT {_PROPOSAL_COLUMNS} FROM dream_proposals "
            f"{_dream_scope_sql()} AND status = ANY(:statuses) "
            "ORDER BY accepted_at ASC NULLS LAST"
        ),
        params,
    ).mappings().all()

    for row in rows:
        proposal = _projection_from_row(row)
        try:
            event = _reconcile_one(
                db, scope=scope, proposal=proposal, session_id=session_id, now=now, counts=counts
            )
        except (TaskStateRevisionConflict, TaskStatePreconditionFailed):
            # Nothing is lost: the proposal keeps its status and the next tick tries again.
            logger.warning("dream pursuit reconcile lost a task-state race", exc_info=True)
            continue
        if event is None:
            continue
        try:
            apply_dream_events(
                db,
                scope=scope,
                proposal_id=proposal.proposal_id,
                new_events=[event],
                now=now,
                after_surfaces=after_surfaces,
            )
        except DreamRevisionConflict:
            logger.warning("dream pursuit reconcile lost a CAS race", exc_info=True)
    return counts


def _reconcile_one(
    db: Session,
    *,
    scope: ResolvedScope,
    proposal: DreamProposalProjection,
    session_id: str,
    now: datetime,
    counts: dict[str, int],
) -> DreamProposalEvent | None:
    if proposal.task_id is None:
        return _bind_objective(
            db, scope=scope, proposal=proposal, session_id=session_id, now=now, counts=counts
        )

    loaded = load_task_state(
        db, workspace_id=scope.workspace_id, owner_id=scope.owner_id, task_id=proposal.task_id
    )
    if loaded is None:
        counts["abandoned"] += 1
        return _abandon(proposal, reason="task_state_missing", now=now)
    projection = loaded[0]
    if (
        proposal.objective_hash is None
        or projection.objective_hash is None
        or projection.objective_hash != proposal.objective_hash
    ):
        counts["abandoned"] += 1
        return _abandon(proposal, reason="objective_changed", now=now)
    if projection.status is TaskStatus.CANCELLED:
        counts["abandoned"] += 1
        return _abandon(proposal, reason="task_cancelled", now=now)
    if projection.status is TaskStatus.DONE and proposal.status is not DreamProposalStatus.COMPLETED:
        counts["completed"] += 1
        return DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.COMPLETED,
            payload={"task_id": proposal.task_id, "objective_hash": proposal.objective_hash},
            occurred_at=now,
            actor="system",
            actor_class="system",
        )
    if proposal.status is DreamProposalStatus.ACCEPTED and _any_step_underway(projection):
        plan = projection.plan
        counts["pursuit_started"] += 1
        return DreamProposalEvent(
            seq=0,
            kind=DreamProposalEventKind.PURSUIT_STARTED,
            payload={"plan_root_goal_id": plan.root_goal_id if plan is not None else None},
            occurred_at=now,
            actor="system",
            actor_class="system",
        )
    return None


def _any_step_underway(projection: Any) -> bool:
    plan = getattr(projection, "plan", None)
    steps = getattr(plan, "steps", ()) if plan is not None else ()
    for step in steps:
        status = str(getattr(step, "status", "") or "")
        if status in {"in_progress", "completed"}:
            return True
    return False


def _abandon(
    proposal: DreamProposalProjection, *, reason: str, now: datetime
) -> DreamProposalEvent:
    return DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.PURSUIT_ABANDONED,
        payload={"reason": reason, "task_id": proposal.task_id},
        occurred_at=now,
        actor="system",
        actor_class="system",
    )


def _bind_objective(
    db: Session,
    *,
    scope: ResolvedScope,
    proposal: DreamProposalProjection,
    session_id: str,
    now: datetime,
    counts: dict[str, int],
) -> DreamProposalEvent | None:
    """Branch A.  Mint a plan root through P2's machinery, and change no proposal status."""
    from .config import get_settings

    settings = get_settings()
    loaded = load_task_state(
        db, workspace_id=scope.workspace_id, owner_id=scope.owner_id, task_id=session_id
    )
    projection = loaded[0] if loaded is not None else None
    idle = projection is None or projection.status in {
        TaskStatus.AWAITING_OBJECTIVE,
        TaskStatus.CANCELLED,
    }
    if not idle:
        return None  # try again next tick; nothing is lost

    # The objective is ``first_step``, not ``title``.  The title is an aspiration; P2's planner
    # decomposes an objective, and the small concrete thing is both the better planning input
    # and the reason the title never has to be sanitised into an instruction.
    new_hash = objective_hash(proposal.first_step)
    if not new_hash or not proposal.first_step.strip():
        return None
    next_rev = objective_contract_revision(
        projection.contract_revision if projection is not None else 0,
        projection.objective_hash if projection is not None else None,
        new_hash,
    )
    task_state_id, _projection, _highest_seq, _source_revision = ensure_task_state(
        db,
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        subject_user_id=scope.subject_user_id,
        session_id=session_id,
        task_id=session_id,
        project_id=scope.project_id,
        now=now,
    )
    apply_task_state_events(
        db,
        workspace_id=scope.workspace_id,
        owner_id=scope.owner_id,
        session_id=session_id,
        task_id=session_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.OBJECTIVE_SET,
                contract_revision=next_rev,
                payload={
                    # ``objective_hash`` is not optional here.  Without it the contract revision
                    # does not bump, the previous plan's verification still reads as current, and
                    # the next tick would mark this proposal completed off another objective's
                    # work.
                    "objective_text": proposal.first_step,
                    "objective_hash": new_hash,
                    "source": "dream_proposal",
                    "proposal_id": proposal.proposal_id,
                },
                occurred_at=now,
                actor="system",
            )
        ],
        now=now,
    )
    _enqueue_decompose(
        db,
        scope=scope,
        session_id=session_id,
        task_state_id=task_state_id,
        objective_text=proposal.first_step,
        objective_hash=new_hash,
        contract_revision=next_rev,
        settings=settings,
        now=now,
    )
    counts["bound"] += 1
    return DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.OBJECTIVE_BOUND,
        payload={"task_id": session_id, "objective_hash": new_hash},
        occurred_at=now,
        actor="system",
        actor_class="system",
    )


def _enqueue_decompose(
    db: Session,
    *,
    scope: ResolvedScope,
    session_id: str,
    task_state_id: str,
    objective_text: str,
    objective_hash: str,
    contract_revision: int,
    settings: Any,
    now: datetime,
) -> None:
    """Hand the decomposition to P2, unchanged.  Best effort: a queue that is down must not
    lose the binding that was already written."""
    from tce_shared.task_state import (
        PLANNING_JOB_KIND_DECOMPOSE,
        TASK_STATE_POLICY_REVISION,
        charter_for_task,
        plan_input_revision,
        task_scope_digest,
    )

    try:
        enqueue_planning_job(
            db,
            workspace_id=scope.workspace_id,
            owner_id=scope.owner_id,
            session_id=session_id,
            task_id=session_id,
            task_state_id=task_state_id,
            job_kind=PLANNING_JOB_KIND_DECOMPOSE,
            input_revision=plan_input_revision(
                objective_hash=objective_hash,
                contract_revision=contract_revision,
                policy_revision=TASK_STATE_POLICY_REVISION,
                scope_digest=task_scope_digest(
                    workspace_id=scope.workspace_id,
                    executor_id=scope.executor_id,
                    owner_ids=sorted(scope.owner_ids),
                    subject_user_id=scope.subject_user_id,
                    project_id=scope.project_id,
                    project_binding=scope.project_binding,
                ),
                cancel_epoch=0,
            ),
            contract_revision=contract_revision,
            objective_hash=objective_hash,
            objective_text=objective_text,
            charter=charter_for_task(
                max_steps=int(getattr(settings, "takeover_plan_max_steps", 8))
            ),
            scope=scope,
            now=now,
            max_attempts=int(getattr(settings, "planning_job_max_attempts", 3)),
        )
    except Exception:  # noqa: BLE001
        logger.warning("dream decomposition enqueue failed", exc_info=True)


# --------------------------------------------------------------------------- candidate helpers


def candidate_from_parts(
    *,
    title: str,
    connection_text: str,
    benefit_text: str,
    first_step: str,
    citations: Sequence[DreamCitation],
) -> DreamCandidate:
    """Build a candidate with its derived fields, so no caller re-derives them differently."""
    kept = dedupe_by_content_sha256(citations)
    return DreamCandidate(
        title=title,
        connection_text=connection_text,
        benefit_text=benefit_text,
        first_step=first_step,
        citations=kept,
        theme_tokens=theme_tokens_for(title, first_step),
        evidence_basis=evidence_basis_for(kept),
    )


def merge_citations_event(
    *,
    live: DreamProposalProjection,
    candidate: DreamCandidate,
    max_citations: int,
    now: datetime,
) -> DreamProposalEvent | None:
    """The duplicate-merge path: the live proposal gets *stronger*, and keeps its whole history.

    Its ``surfaced_count``, its ``accepted_at`` and every event behind it survive, because a
    restatement of a theme the owner is already looking at is not a reason to throw away what he
    has already said about it.  Returns ``None`` when the candidate brings nothing new.
    """
    existing = {citation.content_sha256 for citation in live.citations}
    added = [c for c in candidate.citations if c.content_sha256 not in existing]
    if not added:
        return None
    merged = dedupe_by_content_sha256(list(live.citations) + added)[: max(1, int(max_citations))]
    digest, cutoff = evidence_revision(
        [{"id": item.event_id, "ts": item.observed_at} for item in merged]
    )
    return DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.EVIDENCE_REVALIDATED,
        payload={
            "citations": citations_to_json(merged),
            "evidence_basis": evidence_basis_for(merged),
            "evidence_revision": digest,
            "evidence_cutoff_at": _iso(cutoff),
            "merged_from_candidate": True,
        },
        occurred_at=now,
        actor="system",
        actor_class="system",
    )
