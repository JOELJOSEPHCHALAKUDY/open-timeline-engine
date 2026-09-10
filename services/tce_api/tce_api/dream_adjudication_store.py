"""Full backend: write a dream relevance adjudication, and read the dream block the report prints.

Raw ``text()`` SQL against ``dream_relevance_adjudications``, plus **read-only** SELECTs against
P5's ``dream_proposals`` and ``dream_proposal_events``.  **No ``db.commit()`` and no
``db.rollback()`` anywhere in this module** — the caller owns the transaction, the same rule
``dream_store`` and ``task_state_store`` state.

Four properties are structural rather than intentional.

1. **This module issues no DELETE and no UPDATE.**  Not against ``dream_relevance_adjudications``
   (an adjudication is the owner's own answer; a later judgement supersedes an earlier one by id,
   never in place) and not against anything P5 owns.  The only statement shape here that is not a
   SELECT is one INSERT.
2. **It adds no ``dream_proposals`` column, status value or nonresponse value.**  P6 composes with
   P5's vocabulary; what it adds is an adjudication of *relevance* that is independent of
   acceptance, in its own table.
3. **``blind_verified`` is computed here, from P5's log, never taken from the request.**
   :func:`load_verdict_events` deliberately does **not** reuse ``dream_store.load_proposal_events``:
   that read pins kinds and truncates to a tail, and a truncated log that dropped a verdict would
   let the server certify an adjudication blind that was not.  This read selects every verdict
   kind with no limit.
4. **Acceptance never reaches the false-positive numerator.**  ``dream_proposals.status`` is read
   here only to *print the verdict census beside* the adjudication census.  The relevance counts
   come from ``dream_relevance_adjudications`` alone, and the two are assembled by
   ``tce_shared.dream_adjudication``, which has no access to the status column at all.

Lock order: this module takes no lock and holds no row.  Its INSERT touches a table nothing else
writes, and it runs after ``dream_proposals`` (9) in the global order because the route loads the
proposal for scope before it writes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.aspirations import SCOPE_KIND_PROJECT, SCOPE_KIND_WORKSPACE
from tce_shared.dream_adjudication import (
    VERDICT_EVENT_KINDS,
    DreamCounts,
    VerdictEvent,
    admit_delivery,
    verify_blind,
)
from tce_shared.pilot_enrollment import DeliveryUsefulness, RelevanceVerdict
from tce_shared.pilot_thresholds import DELIVERY_LOOKBACK_DAYS
from tce_shared.redaction import redact_text
from tce_shared.scope import ResolvedScope

logger = logging.getLogger(__name__)

__all__ = [
    "AdjudicationWrite",
    "DreamTablesMissing",
    "ProposalNotInScope",
    "dream_counts_for_scope",
    "load_verdict_events",
    "record_adjudication",
]

#: The rationale ceiling, matching ``DreamRelevanceAdjudicationRequest.rationale``'s ``max_length``.
MAX_RATIONALE_CHARS = 500


class DreamTablesMissing(RuntimeError):
    """P5's or P6's dream tables are not present in this database.

    Raised so the caller renders NOT_COMPUTABLE with a reason rather than zeros.  On a database
    still at ``20260909_0041`` the whole dream surface exists on disk and not in the schema, and
    "0 proposals" would read as *nobody proposed anything* when the truth is *nothing could be
    asked*.
    """


class ProposalNotInScope(LookupError):
    """The proposal is not readable by this scope.  The route renders 404.

    The store re-checks scope rather than trusting the route to have done it, because the one
    thing this table must never contain is an adjudication of another subject's proposal: the
    adjudication moves a metric, and a metric moved from outside its own scope is a
    mis-attribution that nothing downstream can detect.
    """


def _scope_kind(scope: ResolvedScope) -> str:
    return SCOPE_KIND_PROJECT if (scope.is_bound() and scope.project_id) else SCOPE_KIND_WORKSPACE


def _scope_params(scope: ResolvedScope) -> dict[str, Any]:
    bound = _scope_kind(scope) == SCOPE_KIND_PROJECT
    return {
        "workspace_id": scope.workspace_id,
        "owner_id": scope.owner_id,
        "subject_user_id": scope.subject_user_id,
        "scope_bound": bound,
        "project_id": scope.project_id if bound else None,
    }


#: The same predicate ``dream_store._dream_scope_sql`` renders, and for the same reason:
#: ``subject_user_id`` is in it because one ``owner_id`` on this stack spans several subjects,
#: and a predicate keyed on ``(workspace_id, owner_id, id)`` alone would let a verified human
#: adjudicate — and thereby move a metric with — another subject's proposal.
_PROPOSAL_SCOPE_SQL = (
    "WHERE workspace_id = :workspace_id\n"
    "  AND owner_id = :owner_id\n"
    "  AND subject_user_id = :subject_user_id\n"
    "  AND (   (:scope_bound AND scope_kind = 'project' AND project_id = :project_id)\n"
    "       OR (NOT :scope_bound AND scope_kind = 'workspace' AND project_id IS NULL) )"
)


def _aware(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def load_verdict_events(db: Session, *, proposal_id: str) -> list[VerdictEvent]:
    """Every human verdict event on the proposal, in time order, **untruncated**.

    ``kind`` is bound from :data:`VERDICT_EVENT_KINDS` rather than interpolated, and there is no
    ``LIMIT``: a limit here would be a silent way to make an adjudication look blind.
    """

    identifier = _as_uuid(proposal_id)
    if identifier is None:
        return []
    rows = (
        db.execute(
            text(
                """
                SELECT kind, actor, occurred_at
                  FROM dream_proposal_events
                 WHERE proposal_id = :proposal_id
                   AND kind = ANY(:kinds)
                 ORDER BY occurred_at ASC, seq ASC
                """
            ),
            {"proposal_id": identifier, "kinds": sorted(VERDICT_EVENT_KINDS)},
        )
        .mappings()
        .all()
    )
    out: list[VerdictEvent] = []
    for row in rows:
        occurred = _aware(row["occurred_at"])
        if occurred is None:  # pragma: no cover - the column is NOT NULL
            continue
        out.append(VerdictEvent(kind=str(row["kind"]), actor=str(row["actor"] or ""), occurred_at=occurred))
    return out


class AdjudicationWrite:
    """The row that was written, plus the two server answers the response carries.

    A plain object rather than a dataclass because it is assembled once and read once; what
    matters is that ``blind_verified`` and ``counted_for_delivery`` are fields the *server*
    filled in, next to the ``blind_claimed`` the caller sent, so the route cannot return the
    caller's claim in the server's field by accident.
    """

    __slots__ = (
        "adjudication_id",
        "adjudicated_at",
        "blind_claimed",
        "blind_reason",
        "blind_verified",
        "counted_for_delivery",
        "delivery_reason",
        "delivery_useful",
        "proposal_id",
        "relevance",
        "supersedes_adjudication_id",
    )

    def __init__(
        self,
        *,
        adjudication_id: uuid.UUID,
        proposal_id: uuid.UUID,
        relevance: RelevanceVerdict,
        blind_claimed: bool,
        blind_verified: bool,
        blind_reason: str,
        delivery_useful: DeliveryUsefulness | None,
        counted_for_delivery: bool,
        delivery_reason: str,
        adjudicated_at: datetime,
        supersedes_adjudication_id: uuid.UUID | None,
    ) -> None:
        self.adjudication_id = adjudication_id
        self.proposal_id = proposal_id
        self.relevance = relevance
        self.blind_claimed = blind_claimed
        self.blind_verified = blind_verified
        self.blind_reason = blind_reason
        self.delivery_useful = delivery_useful
        self.counted_for_delivery = counted_for_delivery
        self.delivery_reason = delivery_reason
        self.adjudicated_at = adjudicated_at
        self.supersedes_adjudication_id = supersedes_adjudication_id


def record_adjudication(
    db: Session,
    *,
    scope: ResolvedScope,
    proposal_id: str,
    adjudicator_id: str,
    adjudicator_verified: bool,
    relevance: RelevanceVerdict,
    rationale: str = "",
    blind_claimed: bool = False,
    delivery_useful: DeliveryUsefulness | None = None,
    supersedes_adjudication_id: str | None = None,
    now: datetime | None = None,
    lookback_days: int = DELIVERY_LOOKBACK_DAYS,
) -> AdjudicationWrite:
    """Append one adjudication.  Returns what the server decided, not what the caller claimed.

    ``blind_verified`` is computed from :func:`load_verdict_events`, ``counted_for_delivery`` from
    the lookback clock against P5's own ``completed_at``, and both are written to the row so a
    later read of the table needs no re-derivation and no access to the caller's word.

    **``completed_at`` is read here, not accepted as an argument.**  Neither the caller nor the
    route can supply it, so there is no way to hand the delivery clock a date that suits the
    answer — and no way for a route that forgets to pass it to make every usefulness answer
    silently uncountable.  The scope check is re-run here for the same reason: a store that
    trusts its caller's scope check is one refactor away from not having one.
    """

    stamp = (now or datetime.now(tz=UTC)).astimezone(UTC)
    identifier = _as_uuid(proposal_id)
    if identifier is None:
        raise ValueError(f"proposal_id {proposal_id!r} is not a uuid")

    proposal = (
        db.execute(
            text(f"SELECT completed_at FROM dream_proposals {_PROPOSAL_SCOPE_SQL} AND id = :proposal_id"),
            {**_scope_params(scope), "proposal_id": identifier},
        )
        .mappings()
        .first()
    )
    if proposal is None:
        raise ProposalNotInScope(str(identifier))
    completed_at = _aware(proposal["completed_at"])

    blind = verify_blind(
        blind_claimed=bool(blind_claimed),
        adjudicator_id=adjudicator_id,
        adjudicated_at=stamp,
        verdict_events=load_verdict_events(db, proposal_id=str(identifier)),
    )
    admission = admit_delivery(
        claimed=delivery_useful,
        completed_at=completed_at,
        adjudicated_at=stamp,
        lookback_days=lookback_days,
    )
    redacted, _hints = redact_text(str(rationale or "")[:MAX_RATIONALE_CHARS])
    adjudication_id = uuid.uuid4()
    supersedes = _as_uuid(supersedes_adjudication_id) if supersedes_adjudication_id else None

    db.execute(
        text(
            """
            INSERT INTO dream_relevance_adjudications (
                id, proposal_id, workspace_id, owner_id, subject_user_id, project_id,
                adjudicator_id, adjudicator_verified, relevance, rationale,
                blind_claimed, blind_verified, delivery_useful, counted_for_delivery,
                adjudicated_at, supersedes_adjudication_id, schema_version
            ) VALUES (
                :id, :proposal_id, :workspace_id, :owner_id, :subject_user_id, :project_id,
                :adjudicator_id, :adjudicator_verified, :relevance, :rationale,
                :blind_claimed, :blind_verified, :delivery_useful, :counted_for_delivery,
                :adjudicated_at, :supersedes_adjudication_id, 'v1'
            )
            """
        ),
        {
            "id": adjudication_id,
            "proposal_id": identifier,
            "workspace_id": scope.workspace_id,
            "owner_id": scope.owner_id,
            "subject_user_id": scope.subject_user_id,
            "project_id": scope.project_id if _scope_kind(scope) == SCOPE_KIND_PROJECT else None,
            "adjudicator_id": adjudicator_id,
            "adjudicator_verified": bool(adjudicator_verified),
            "relevance": relevance.value,
            "rationale": redacted,
            "blind_claimed": bool(blind_claimed),
            "blind_verified": blind.verified,
            "delivery_useful": admission.stored.value if admission.stored is not None else "",
            "counted_for_delivery": admission.counted,
            "adjudicated_at": stamp,
            "supersedes_adjudication_id": supersedes,
        },
    )
    return AdjudicationWrite(
        adjudication_id=adjudication_id,
        proposal_id=identifier,
        relevance=relevance,
        blind_claimed=bool(blind_claimed),
        blind_verified=blind.verified,
        blind_reason=blind.reason,
        delivery_useful=admission.stored,
        counted_for_delivery=admission.counted,
        delivery_reason=admission.reason,
        adjudicated_at=stamp,
        supersedes_adjudication_id=supersedes,
    )


def _tables_present(db: Session, names: Sequence[str]) -> bool:
    rows = (
        db.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_name = ANY(:names)"),
            {"names": list(names)},
        )
        .scalars()
        .all()
    )
    return set(str(name) for name in rows) >= set(names)


def dream_counts_for_scope(db: Session, *, scope: ResolvedScope) -> DreamCounts:
    """Everything the dream block needs, for exactly ONE scope.  Never a cross-project total.

    Three separate reads, kept separate on purpose: the proposal census (P5's fold), the verdict
    census (P5's ``status`` — printed, never a numerator), and the adjudication census (P6's own
    table).  A single joined query would put ``status`` and ``relevance`` in one row set, and the
    next person to touch it would have everything needed to count a rejection as a false positive.

    Only the **latest** adjudication per proposal is counted: the table is append-only and a later
    judgement supersedes an earlier one, so counting both would let one owner changing his mind
    move the rate twice.
    """

    required = (
        "dream_proposals",
        "dream_proposal_events",
        "dream_relevance_adjudications",
    )
    if not _tables_present(db, required):
        raise DreamTablesMissing("dream tables are not present in this database")

    params = _scope_params(scope)
    census = (
        db.execute(
            text(
                f"""
                SELECT status,
                       COUNT(*) AS n,
                       SUM(CASE WHEN surfaced_count > 0 THEN 1 ELSE 0 END) AS surfaced,
                       SUM(CASE WHEN surfaced_attested THEN 1 ELSE 0 END) AS attested,
                       SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed
                  FROM dream_proposals
                  {_PROPOSAL_SCOPE_SQL}
                 GROUP BY status
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    by_status: dict[str, int] = {}
    proposals = surfaced = attested = completed = 0
    for row in census:
        count = int(row["n"] or 0)
        by_status[str(row["status"])] = count
        proposals += count
        surfaced += int(row["surfaced"] or 0)
        attested += int(row["attested"] or 0)
        completed += int(row["completed"] or 0)

    nonresponse_rows = (
        db.execute(
            text(
                f"""
                SELECT nonresponse_state AS state, COUNT(*) AS n
                  FROM dream_proposals
                  {_PROPOSAL_SCOPE_SQL}
                 GROUP BY nonresponse_state
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    nonresponse = {str(row["state"]): int(row["n"] or 0) for row in nonresponse_rows}

    # The latest adjudication per proposal, and only proposals this scope can see.  ``DISTINCT
    # ON`` is the same construction ``dream_store`` uses for its pinned-kind read.
    adjudications = (
        db.execute(
            text(
                f"""
                WITH scoped AS (
                    SELECT id FROM dream_proposals
                    {_PROPOSAL_SCOPE_SQL}
                ), latest AS (
                    SELECT DISTINCT ON (a.proposal_id)
                           a.proposal_id, a.relevance, a.blind_verified,
                           a.delivery_useful, a.counted_for_delivery
                      FROM dream_relevance_adjudications a
                      JOIN scoped s ON s.id = a.proposal_id
                     ORDER BY a.proposal_id, a.adjudicated_at DESC, a.id DESC
                )
                SELECT relevance, blind_verified, delivery_useful, counted_for_delivery
                  FROM latest
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    relevant = not_relevant = cannot_judge = blind_verified = 0
    useful = not_useful = too_early = 0
    for row in adjudications:
        verdict = str(row["relevance"])
        if verdict == RelevanceVerdict.RELEVANT:
            relevant += 1
        elif verdict == RelevanceVerdict.NOT_RELEVANT:
            not_relevant += 1
        elif verdict == RelevanceVerdict.CANNOT_JUDGE:
            cannot_judge += 1
        if bool(row["blind_verified"]):
            blind_verified += 1
        delivery = str(row["delivery_useful"] or "")
        if not delivery:
            continue
        if not bool(row["counted_for_delivery"]):
            too_early += 1
        elif delivery == DeliveryUsefulness.USEFUL:
            useful += 1
        elif delivery == DeliveryUsefulness.NOT_USEFUL:
            not_useful += 1

    return DreamCounts(
        project_id=scope.project_id if _scope_kind(scope) == SCOPE_KIND_PROJECT else None,
        proposals=proposals,
        surfaced=surfaced,
        surfaced_attested=attested,
        by_status=by_status,
        nonresponse=nonresponse,
        completed_proposals=completed,
        relevant=relevant,
        not_relevant=not_relevant,
        cannot_judge=cannot_judge,
        blind_verified=blind_verified,
        adjudicated_proposals=len(adjudications),
        delivery_useful=useful,
        delivery_not_useful=not_useful,
        delivery_too_early=too_early,
    )
