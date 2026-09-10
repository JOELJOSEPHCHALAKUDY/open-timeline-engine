"""Lite backend twin of ``tce_api.dream_adjudication_store``.  Same answers, SQLite dialect.

Every rule the Full module states holds here verbatim and is not restated: no DELETE, no UPDATE,
no ``dream_proposals`` column, ``blind_verified`` computed from P5's log rather than taken from
the request, and acceptance never reaching the false-positive numerator.  The two modules call
the SAME functions in ``tce_shared.dream_adjudication`` for every decision, so a divergence
between the backends can only be a SQL divergence, never an arithmetic one.

Three dialect differences, each deliberate:

* ``sqlite_master`` replaces ``information_schema.tables`` for the table-presence check.
* ``ROW_NUMBER() OVER (PARTITION BY ...)`` replaces ``DISTINCT ON`` for "the latest adjudication
  per proposal".  SQLite has had window functions since 3.25 and this file already depends on
  3.35 elsewhere.
* Booleans are ``INTEGER`` and timestamps are ISO-8601 ``TEXT``, the mapping the rest of this
  package uses.

The DDL lives here rather than only in ``db.py`` so that there is one text of it: ``db.py``'s P6
block calls :func:`ensure_dream_adjudication_tables`, and a schema change is a change in one
place.  The statements are ``CREATE ... IF NOT EXISTS``, so calling it twice is a no-op and
calling it after ``db.py`` has already created the table is also a no-op.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

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
    "DREAM_ADJUDICATION_DDL",
    "AdjudicationWrite",
    "DreamTablesMissing",
    "ProposalNotInScope",
    "dream_counts_for_scope",
    "ensure_dream_adjudication_tables",
    "load_verdict_events",
    "record_adjudication",
]

MAX_RATIONALE_CHARS = 500

#: The Lite twin of ``20260909_0043``'s ``dream_relevance_adjudications``.  Column for column,
#: default for default, with the house type mapping (UUID -> TEXT, TIMESTAMPTZ -> TEXT,
#: BOOLEAN -> INTEGER).  ``db.py``'s P6 block calls :func:`ensure_dream_adjudication_tables`
#: rather than repeating any of this.
DREAM_ADJUDICATION_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS dream_relevance_adjudications (
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        subject_user_id TEXT NOT NULL DEFAULT '',
        project_id TEXT,
        adjudicator_id TEXT NOT NULL DEFAULT '',
        adjudicator_verified INTEGER NOT NULL DEFAULT 0,
        relevance TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        blind_claimed INTEGER NOT NULL DEFAULT 0,
        blind_verified INTEGER NOT NULL DEFAULT 0,
        delivery_useful TEXT NOT NULL DEFAULT '',
        counted_for_delivery INTEGER NOT NULL DEFAULT 0,
        adjudicated_at TEXT NOT NULL,
        supersedes_adjudication_id TEXT,
        schema_version TEXT NOT NULL DEFAULT 'v1'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dream_relevance_adjudications_proposal "
    "ON dream_relevance_adjudications (proposal_id, adjudicated_at)",
    "CREATE INDEX IF NOT EXISTS idx_dream_relevance_adjudications_scope "
    "ON dream_relevance_adjudications (workspace_id, owner_id, project_id, adjudicated_at)",
)


def ensure_dream_adjudication_tables(conn: sqlite3.Connection) -> None:
    """Create the adjudication table and its two indexes if they are absent.  Idempotent."""

    for statement in DREAM_ADJUDICATION_DDL:
        conn.execute(statement)


class DreamTablesMissing(RuntimeError):
    """P5's or P6's dream tables are absent.  The caller renders NOT_COMPUTABLE, never zeros."""


class ProposalNotInScope(LookupError):
    """The proposal is not readable by this scope.  The route renders 404.  Twin of the Full class."""


def _scope_kind(scope: ResolvedScope) -> str:
    return SCOPE_KIND_PROJECT if (scope.is_bound() and scope.project_id) else SCOPE_KIND_WORKSPACE


#: The six positional binds of the proposal scope predicate, in order.  Same predicate as
#: ``dream_store._dream_scope_sql``; ``subject_user_id`` is in it for the same reason.
def _scope_params(scope: ResolvedScope) -> tuple[Any, ...]:
    bound = 1 if _scope_kind(scope) == SCOPE_KIND_PROJECT else 0
    return (
        scope.workspace_id,
        scope.owner_id,
        scope.subject_user_id,
        bound,
        scope.project_id or "",
        bound,
    )


_PROPOSAL_SCOPE_SQL = (
    "workspace_id = ?\n"
    "       AND owner_id = ?\n"
    "       AND subject_user_id = ?\n"
    "       AND (   (? = 1 AND scope_kind = 'project'   AND project_id = ?)\n"
    "            OR (? = 0 AND scope_kind = 'workspace' AND project_id IS NULL))"
)


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat()


def load_verdict_events(conn: sqlite3.Connection, *, proposal_id: str) -> list[VerdictEvent]:
    """Every human verdict event on the proposal, in time order, **untruncated**.

    No ``LIMIT`` and no kind pinning, for the reason the Full twin states: a truncated log that
    dropped a verdict would let the server certify an adjudication blind that was not.
    """

    kinds = sorted(VERDICT_EVENT_KINDS)
    marks = ", ".join("?" for _ in kinds)
    rows = conn.execute(
        "SELECT kind, actor, occurred_at FROM dream_proposal_events "
        f"WHERE proposal_id = ? AND kind IN ({marks}) ORDER BY occurred_at ASC, seq ASC",
        (str(proposal_id), *kinds),
    ).fetchall()
    out: list[VerdictEvent] = []
    for row in rows:
        occurred = _parse_dt(row["occurred_at"])
        if occurred is None:  # pragma: no cover - the column is NOT NULL
            continue
        out.append(VerdictEvent(kind=str(row["kind"]), actor=str(row["actor"] or ""), occurred_at=occurred))
    return out


class AdjudicationWrite:
    """What the server decided, alongside what the caller claimed.  Twin of the Full class."""

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
        adjudication_id: str,
        proposal_id: str,
        relevance: RelevanceVerdict,
        blind_claimed: bool,
        blind_verified: bool,
        blind_reason: str,
        delivery_useful: DeliveryUsefulness | None,
        counted_for_delivery: bool,
        delivery_reason: str,
        adjudicated_at: datetime,
        supersedes_adjudication_id: str | None,
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
    conn: sqlite3.Connection,
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
    """Append one adjudication.  Returns the server's answers, not the caller's claims.

    ``completed_at`` is read from P5's row, never accepted as an argument, and the scope is
    re-checked here rather than trusted from the route.  Both for the reasons the Full twin
    states.
    """

    stamp = (now or datetime.now(tz=UTC)).astimezone(UTC)
    proposal = conn.execute(
        "SELECT completed_at FROM dream_proposals WHERE " + _PROPOSAL_SCOPE_SQL + " AND id = ?",
        (*_scope_params(scope), str(proposal_id)),
    ).fetchone()
    if proposal is None:
        raise ProposalNotInScope(str(proposal_id))
    completed_at = _parse_dt(proposal["completed_at"])

    blind = verify_blind(
        blind_claimed=bool(blind_claimed),
        adjudicator_id=adjudicator_id,
        adjudicated_at=stamp,
        verdict_events=load_verdict_events(conn, proposal_id=str(proposal_id)),
    )
    admission = admit_delivery(
        claimed=delivery_useful,
        completed_at=completed_at,
        adjudicated_at=stamp,
        lookback_days=lookback_days,
    )
    redacted, _hints = redact_text(str(rationale or "")[:MAX_RATIONALE_CHARS])
    adjudication_id = str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO dream_relevance_adjudications (
            id, proposal_id, workspace_id, owner_id, subject_user_id, project_id,
            adjudicator_id, adjudicator_verified, relevance, rationale,
            blind_claimed, blind_verified, delivery_useful, counted_for_delivery,
            adjudicated_at, supersedes_adjudication_id, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            adjudication_id,
            str(proposal_id),
            scope.workspace_id,
            scope.owner_id,
            scope.subject_user_id,
            scope.project_id if _scope_kind(scope) == SCOPE_KIND_PROJECT else None,
            adjudicator_id,
            1 if adjudicator_verified else 0,
            relevance.value,
            redacted,
            1 if blind_claimed else 0,
            1 if blind.verified else 0,
            admission.stored.value if admission.stored is not None else "",
            1 if admission.counted else 0,
            _iso(stamp),
            str(supersedes_adjudication_id) if supersedes_adjudication_id else None,
        ),
    )
    return AdjudicationWrite(
        adjudication_id=adjudication_id,
        proposal_id=str(proposal_id),
        relevance=relevance,
        blind_claimed=bool(blind_claimed),
        blind_verified=blind.verified,
        blind_reason=blind.reason,
        delivery_useful=admission.stored,
        counted_for_delivery=admission.counted,
        delivery_reason=admission.reason,
        adjudicated_at=stamp,
        supersedes_adjudication_id=str(supersedes_adjudication_id) if supersedes_adjudication_id else None,
    )


def _tables_present(conn: sqlite3.Connection, names: tuple[str, ...]) -> bool:
    marks = ", ".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({marks})",
        names,
    ).fetchall()
    return {str(row["name"]) for row in rows} >= set(names)


def dream_counts_for_scope(conn: sqlite3.Connection, *, scope: ResolvedScope) -> DreamCounts:
    """Everything the dream block needs, for exactly ONE scope.  Never a cross-project total.

    Three separate reads for the reason the Full twin states: a single joined query would put
    ``status`` and ``relevance`` in one row set, which is all the next person would need to start
    counting a rejection as a false positive.
    """

    required = ("dream_proposals", "dream_proposal_events", "dream_relevance_adjudications")
    if not _tables_present(conn, required):
        raise DreamTablesMissing("dream tables are not present in this database")

    params = _scope_params(scope)
    census = conn.execute(
        "SELECT status, COUNT(*) AS n, "
        "SUM(CASE WHEN surfaced_count > 0 THEN 1 ELSE 0 END) AS surfaced, "
        "SUM(CASE WHEN surfaced_attested = 1 THEN 1 ELSE 0 END) AS attested, "
        "SUM(CASE WHEN completed_at IS NOT NULL AND completed_at != '' THEN 1 ELSE 0 END) AS completed "
        "FROM dream_proposals WHERE " + _PROPOSAL_SCOPE_SQL + " GROUP BY status",
        params,
    ).fetchall()
    by_status: dict[str, int] = {}
    proposals = surfaced = attested = completed = 0
    for row in census:
        count = int(row["n"] or 0)
        by_status[str(row["status"])] = count
        proposals += count
        surfaced += int(row["surfaced"] or 0)
        attested += int(row["attested"] or 0)
        completed += int(row["completed"] or 0)

    nonresponse_rows = conn.execute(
        "SELECT nonresponse_state AS state, COUNT(*) AS n FROM dream_proposals WHERE "
        + _PROPOSAL_SCOPE_SQL
        + " GROUP BY nonresponse_state",
        params,
    ).fetchall()
    nonresponse = {str(row["state"]): int(row["n"] or 0) for row in nonresponse_rows}

    adjudications = conn.execute(
        "WITH scoped AS (SELECT id FROM dream_proposals WHERE "
        + _PROPOSAL_SCOPE_SQL
        + "), ranked AS ("
        "  SELECT a.proposal_id, a.relevance, a.blind_verified, a.delivery_useful, a.counted_for_delivery,"
        "         ROW_NUMBER() OVER (PARTITION BY a.proposal_id"
        "                            ORDER BY a.adjudicated_at DESC, a.id DESC) AS rn"
        "    FROM dream_relevance_adjudications a"
        "    JOIN scoped s ON s.id = a.proposal_id"
        ") SELECT relevance, blind_verified, delivery_useful, counted_for_delivery FROM ranked WHERE rn = 1",
        params,
    ).fetchall()
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
        if int(row["blind_verified"] or 0):
            blind_verified += 1
        delivery = str(row["delivery_useful"] or "")
        if not delivery:
            continue
        if not int(row["counted_for_delivery"] or 0):
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
