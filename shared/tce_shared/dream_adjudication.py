"""Dream **relevance**, adjudicated independently of acceptance (P6 §5).

Pure and deterministic.  No I/O, no pydantic, no SQLAlchemy, no settings — both backends and
``scripts/p6_pilot_report.py`` import this module, and the two stores are the only things that
touch a database.

This module composes with P5 and forks nothing.  It adds **no** ``dream_proposals`` column, **no**
``DreamProposalStatus`` member and **no** ``NonresponseState`` member; it imports P5's vocabulary
from :mod:`tce_shared.aspirations` and P6's from :mod:`tce_shared.pilot_enrollment`, and the
clause arithmetic it needs that already exists there (``Clause``, ``false_positive_relevance``)
is imported rather than re-implemented, so ``GET /v1/pilot/report`` and the report script stay
one computation with two renderers.

Four rules are structural here rather than conventional.

* **Blindness is checked, not claimed.**  ``blind_claimed`` is the caller's word and is stored as
  the caller's word.  ``blind_verified`` is the server's, and :func:`verify_blind` computes it
  from P5's append-only ``dream_proposal_events`` log: an adjudication that postdates any human
  verdict on the proposal is not blind however loudly the caller says it was, and an adjudicator
  who gave that verdict himself is not blind either.  The report counts the two separately and
  never treats the first as the second.
* **A rejection is never a false positive.**  A rejection is a preference; a ``not_relevant``
  adjudication is a claim about the proposal's fit.  Nothing in this module reads
  ``dream_proposals.status`` into the false-positive numerator, and nothing may be added that
  does — the whole reason the adjudication has its own table is that deriving the second from the
  first would manufacture a measurement out of a preference.
* **Nonresponse is never rejection.**  P5 §1.2 already rules that ``ignored`` is counted in no
  rejection metric.  The renderer prints the nonresponse axis beside the verdict axis with that
  sentence attached, so the two cannot be added up by eye either.
* **Nothing pools.**  A block is one ``project_id``.  :func:`dream_blocks` refuses a sequence
  carrying the same project twice and there is no function here that returns a cross-project
  number.

And one rule about zeros, inherited whole from ``pilot_enrollment``: a denominator of zero
resolves ``NOT_COMPUTABLE`` with a reason from the closed list, never ``0.0``.  "No proposal was
judged irrelevant" at n=0 is not a low false-positive rate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .aspirations import (
    HUMAN_VERDICT_ACTIONS,
    DreamProposalStatus,
    NonresponseState,
)
from .pilot_enrollment import (
    Clause,
    ClauseState,
    DeliveryUsefulness,
    RelevanceVerdict,
    clause_not_computable,
    clause_pass,
    clause_shortfall,
    false_positive_relevance,
)
from .pilot_thresholds import DELIVERY_LOOKBACK_DAYS, MIN_ADJUDICATED_DREAMS

__all__ = [
    "BLIND_REASONS",
    "DELIVERY_REASONS",
    "DREAM_STATUS_KEYS",
    "NONRESPONSE_IS_NOT_REJECTION",
    "NONRESPONSE_KEYS",
    "VERDICT_EVENT_KINDS",
    "BlindVerdict",
    "DeliveryAdmission",
    "DreamBlock",
    "DreamCounts",
    "VerdictEvent",
    "adjudication_coverage_clause",
    "admit_delivery",
    "delivery_eligible_at",
    "dream_block",
    "dream_block_unavailable",
    "dream_blocks",
    "later_useful_delivery",
    "relevance_from_value",
    "render_dream_blocks",
    "summarise_states",
    "verify_blind",
]


# --------------------------------------------------------------------------------------
# 1. Vocabulary — borrowed from P5, never re-declared
# --------------------------------------------------------------------------------------

#: The event kinds that reveal a proposal's standing to whoever is looking at it.  P6 §5.2
#: names three (``accepted``, ``rejected``, ``snoozed``); this uses P5's own
#: ``HUMAN_VERDICT_ACTIONS``, which is those three plus ``unsnoozed``.  **Deliberate and
#: stricter:** an unsnooze is a human touching the proposal's status, so an adjudication after
#: one is not blind either.  Being stricter here can only lower ``blind_verified``, never raise
#: it, which is the safe direction for a field the report leans on.
VERDICT_EVENT_KINDS: frozenset[str] = frozenset(HUMAN_VERDICT_ACTIONS)

#: Why ``blind_verified`` came out the way it did.  A closed list; the store persists the
#: boolean and the route returns the reason, so a ``false`` is never a bare ``false``.
BLIND_REASONS: frozenset[str] = frozenset(
    {
        "verified_no_verdict_yet",  # nothing had been decided when the judgement was made
        "verified_before_verdict",  # a verdict exists but is strictly later
        "not_claimed",  # the caller did not claim blindness; the server does not invent it
        "verdict_precedes_adjudication",  # the status was visible; the claim is refused
        "adjudicator_gave_prior_verdict",  # the judge had already answered; he cannot be blind to his own answer
    }
)

#: Why a ``delivery_useful`` answer was or was not admitted to the metric.
DELIVERY_REASONS: frozenset[str] = frozenset(
    {
        "counted",
        "not_answered",
        "not_delivered",  # the proposal never reached ``completed``; there is no "later" yet
        "too_early",  # answered inside the lookback; stored, printed, never counted
        "answered_too_early_by_caller",  # the caller itself said ``too_early``
    }
)

#: The verdict axis, printed in this order.  Every member is a ``DreamProposalStatus`` value;
#: this module declares no status of its own.
DREAM_STATUS_KEYS: tuple[str, ...] = (
    DreamProposalStatus.ACCEPTED.value,
    DreamProposalStatus.REJECTED.value,
    DreamProposalStatus.SNOOZED.value,
    DreamProposalStatus.EXPIRED.value,
    DreamProposalStatus.WITHDRAWN.value,
)

#: The nonresponse axis, printed beside the verdict axis and never added to it.
NONRESPONSE_KEYS: tuple[str, ...] = (
    NonresponseState.NEVER_SURFACED.value,
    NonresponseState.AWAITING_RESPONSE.value,
    NonresponseState.IGNORED.value,
)

NONRESPONSE_IS_NOT_REJECTION = "(nonresponse is NOT rejection)"


# --------------------------------------------------------------------------------------
# 2. Blindness — the server's answer, not the caller's
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerdictEvent:
    """One row of P5's ``dream_proposal_events`` that revealed the proposal's standing.

    ``actor`` is the principal P5 recorded on the event.  It is compared against the
    adjudicator's principal, which is why the store selects it rather than only the timestamp.
    """

    kind: str
    actor: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class BlindVerdict:
    """``verified`` plus the reason it came out that way.  ``reason`` is in :data:`BLIND_REASONS`."""

    verified: bool
    reason: str
    first_verdict_at: datetime | None = None
    first_verdict_kind: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "blind_verified": self.verified,
            "blind_reason": self.reason,
            "first_verdict_at": self.first_verdict_at.isoformat() if self.first_verdict_at else None,
            "first_verdict_kind": self.first_verdict_kind,
        }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def verify_blind(
    *,
    blind_claimed: bool,
    adjudicator_id: str,
    adjudicated_at: datetime,
    verdict_events: Iterable[VerdictEvent],
) -> BlindVerdict:
    """P6 §5.2's ``blind_verified``, computed from P5's log rather than taken on the caller's word.

    ``blind_verified`` is ``blind_claimed`` **and** the judgement strictly precedes every human
    verdict on the proposal **and** the adjudicator is not the actor on a verdict that already
    happened.  The second and third clauses overlap, and both are kept: the second is the
    ordering rule, the third survives a clock tie, an equal timestamp or a backfilled
    ``occurred_at``, any of which would otherwise let the judge who wrote the verdict certify
    himself blind to it.

    **No verdict at all resolves to verified** (``verified_no_verdict_yet``), and that is not a
    vacuous pass: a proposal with no verdict has no status to have been revealed, so the
    condition the field asserts — "the judgement was made without seeing the answer" — is met by
    the absence of an answer.  What would be vacuous is inferring blindness from a *truncated*
    event log, which is why the two stores read every verdict kind with no pinning and no tail
    limit rather than reusing P5's folding read.
    """

    if not blind_claimed:
        return BlindVerdict(verified=False, reason="not_claimed")

    judged_at = _aware(adjudicated_at)
    verdicts = sorted(
        (event for event in verdict_events if event.kind in VERDICT_EVENT_KINDS),
        key=lambda event: _aware(event.occurred_at),
    )
    if not verdicts:
        return BlindVerdict(verified=True, reason="verified_no_verdict_yet")

    first = verdicts[0]
    first_at = _aware(first.occurred_at)
    prior = [event for event in verdicts if _aware(event.occurred_at) <= judged_at]
    if prior:
        if adjudicator_id and any(event.actor == adjudicator_id for event in prior):
            return BlindVerdict(
                verified=False,
                reason="adjudicator_gave_prior_verdict",
                first_verdict_at=first_at,
                first_verdict_kind=first.kind,
            )
        return BlindVerdict(
            verified=False,
            reason="verdict_precedes_adjudication",
            first_verdict_at=first_at,
            first_verdict_kind=first.kind,
        )
    return BlindVerdict(
        verified=True,
        reason="verified_before_verdict",
        first_verdict_at=first_at,
        first_verdict_kind=first.kind,
    )


# --------------------------------------------------------------------------------------
# 3. Later useful delivery — and the word *later* is a clock
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryAdmission:
    """What was stored, whether it counts, and why.  ``reason`` is in :data:`DELIVERY_REASONS`."""

    stored: DeliveryUsefulness | None
    counted: bool
    reason: str
    eligible_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "delivery_useful": self.stored.value if self.stored is not None else None,
            "counted_for_delivery": self.counted,
            "delivery_reason": self.reason,
            "delivery_eligible_at": self.eligible_at.isoformat() if self.eligible_at else None,
        }


def delivery_eligible_at(completed_at: datetime, *, lookback_days: int = DELIVERY_LOOKBACK_DAYS) -> datetime:
    """The instant a usefulness answer starts counting: ``completed_at + lookback``."""

    return _aware(completed_at) + timedelta(days=int(lookback_days))


def admit_delivery(
    *,
    claimed: DeliveryUsefulness | None,
    completed_at: datetime | None,
    adjudicated_at: datetime,
    lookback_days: int = DELIVERY_LOOKBACK_DAYS,
) -> DeliveryAdmission:
    """P6 §5.3(2).  An answer given before the lookback elapses is **stored and never counted**.

    The caller's answer is persisted verbatim in every branch — an owner who says "useful" on the
    day of delivery said that, and overwriting his word with ``too_early`` would be the report
    editing the evidence.  What the lookback decides is ``counted_for_delivery``, and only
    counted rows reach :func:`later_useful_delivery`'s denominator.
    """

    judged_at = _aware(adjudicated_at)
    if claimed is None:
        return DeliveryAdmission(stored=None, counted=False, reason="not_answered")
    if claimed is DeliveryUsefulness.TOO_EARLY:
        eligible = delivery_eligible_at(completed_at, lookback_days=lookback_days) if completed_at else None
        return DeliveryAdmission(
            stored=claimed, counted=False, reason="answered_too_early_by_caller", eligible_at=eligible
        )
    if completed_at is None:
        return DeliveryAdmission(stored=claimed, counted=False, reason="not_delivered")
    eligible = delivery_eligible_at(completed_at, lookback_days=lookback_days)
    if judged_at < eligible:
        return DeliveryAdmission(stored=claimed, counted=False, reason="too_early", eligible_at=eligible)
    return DeliveryAdmission(stored=claimed, counted=True, reason="counted", eligible_at=eligible)


def later_useful_delivery(
    *,
    useful: int,
    not_useful: int,
    completed_proposals: int,
    too_early: int = 0,
    lookback_days: int = DELIVERY_LOOKBACK_DAYS,
) -> tuple[float | None, Clause]:
    """``useful / (useful + not_useful)`` over **counted** answers only, or NOT_COMPUTABLE.

    Two distinct emptinesses, kept distinct because they call for different actions: nothing has
    been delivered yet (``no_completed_proposals`` — wait), and things were delivered but nobody
    has answered outside the lookback (``no_adjudications`` — answer them).  Neither is ``0.0``.
    """

    if completed_proposals <= 0:
        return None, clause_not_computable(
            "later_useful_delivery",
            "no_completed_proposals",
            detail=f"0 proposals reached completed; lookback {int(lookback_days)}d",
        )
    denominator = useful + not_useful
    if denominator <= 0:
        return None, clause_not_computable(
            "later_useful_delivery",
            "no_adjudications",
            detail=(
                f"{completed_proposals} completed, {too_early} answered inside the "
                f"{int(lookback_days)}d lookback and not counted"
            ),
        )
    return round(useful / denominator, 6), clause_pass("later_useful_delivery")


def adjudication_coverage_clause(adjudicated: int, *, floor: int = MIN_ADJUDICATED_DREAMS) -> Clause:
    """Are there enough independent relevance judgements in this project to read the rate at all?

    A rate over three adjudications is arithmetic, not evidence, so the count carries its own
    clause and the report prints both.  Zero is a SHORTFALL rather than a NOT_COMPUTABLE: the
    number of adjudications is always computable, and it is always short of the floor at zero.
    """

    if adjudicated >= floor:
        return clause_pass("adjudicated_dreams")
    return clause_shortfall("adjudicated_dreams", adjudicated, floor)


# --------------------------------------------------------------------------------------
# 4. The block — one project, never a total
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DreamCounts:
    """Everything the dream block is computed from, for exactly ONE project.

    Every field is ``server_derived`` (a count of rows the executor cannot write) or
    ``human_attested`` (a verdict or a judgement a verified human posted).  There is no
    ``agent_asserted`` field here and no clause below may acquire one: ``surfaced_count`` is
    the executor's claim that it displayed something and is deliberately **not** in this type —
    only ``surfaced_attested``, P5's attested-display count, is.
    """

    project_id: str | None = None
    proposals: int = 0
    surfaced: int = 0
    surfaced_attested: int = 0
    by_status: Mapping[str, int] = field(default_factory=dict)
    nonresponse: Mapping[str, int] = field(default_factory=dict)
    completed_proposals: int = 0
    relevant: int = 0
    not_relevant: int = 0
    cannot_judge: int = 0
    blind_verified: int = 0
    adjudicated_proposals: int = 0
    delivery_useful: int = 0
    delivery_not_useful: int = 0
    delivery_too_early: int = 0

    @property
    def adjudications(self) -> int:
        return self.relevant + self.not_relevant + self.cannot_judge


@dataclass(frozen=True, slots=True)
class DreamBlock:
    """One project's dream reading, rendered and JSON-able from the same fields.

    ``available=False`` is the honest answer when P5's tables are not present — for instance on a
    database still at ``20260909_0041``, where the whole dream surface exists on disk and not in
    the schema.  It renders as NOT_COMPUTABLE with the reason, never as zeros, because zeros
    would read as "no proposals were made" when the truth is "nothing could be asked".
    """

    project_id: str | None
    counts: DreamCounts
    false_positive_rate: float | None
    false_positive_clause: Clause
    delivery_rate: float | None
    delivery_clause: Clause
    coverage_clause: Clause
    available: bool = True

    @property
    def shortfalls(self) -> list[str]:
        return [
            clause.shortfall_text()
            for clause in (self.coverage_clause, self.false_positive_clause, self.delivery_clause)
            if clause.failed
        ]

    def render(self) -> str:
        label = self.project_id or "<workspace>"
        if not self.available:
            return f"DREAMS (project={label}): {self.false_positive_clause.render()}"
        counts = self.counts
        verdicts = " ".join(f"{key}={int(counts.by_status.get(key, 0))}" for key in DREAM_STATUS_KEYS)
        nonresponse = " ".join(f"{key}={int(counts.nonresponse.get(key, 0))}" for key in NONRESPONSE_KEYS)
        rate = "" if self.false_positive_rate is None else f"  rate={self.false_positive_rate}"
        delivery = "" if self.delivery_rate is None else f"  rate={self.delivery_rate}"
        return "\n".join(
            [
                f"DREAMS (project={label}):",
                f"  proposals={counts.proposals}  surfaced={counts.surfaced}  "
                f"surfaced_attested={counts.surfaced_attested}",
                f"  verdicts: {verdicts}",
                f"  nonresponse: {nonresponse}   {NONRESPONSE_IS_NOT_REJECTION}",
                f"  adjudications: total={counts.adjudications}  blind_verified={counts.blind_verified}  "
                f"cannot_judge={counts.cannot_judge}",
                f"  {self.coverage_clause.render()}",
                f"  {self.false_positive_clause.render()}{rate}",
                f"  {self.delivery_clause.render()}{delivery}",
            ]
        )

    def to_payload(self) -> dict[str, Any]:
        counts = self.counts
        return {
            "project_id": self.project_id,
            "available": self.available,
            "proposals": counts.proposals,
            "surfaced": counts.surfaced,
            "surfaced_attested": counts.surfaced_attested,
            "verdicts": {key: int(counts.by_status.get(key, 0)) for key in DREAM_STATUS_KEYS},
            "nonresponse": {key: int(counts.nonresponse.get(key, 0)) for key in NONRESPONSE_KEYS},
            "nonresponse_is_not_rejection": True,
            "completed_proposals": counts.completed_proposals,
            "adjudications": {
                "total": counts.adjudications,
                "relevant": counts.relevant,
                "not_relevant": counts.not_relevant,
                "cannot_judge": counts.cannot_judge,
                "blind_verified": counts.blind_verified,
                "proposals_adjudicated": counts.adjudicated_proposals,
            },
            "delivery": {
                "useful": counts.delivery_useful,
                "not_useful": counts.delivery_not_useful,
                "too_early_not_counted": counts.delivery_too_early,
            },
            "false_positive_relevance": self.false_positive_rate,
            "later_useful_delivery": self.delivery_rate,
            "clauses": [
                self.coverage_clause.to_payload(),
                self.false_positive_clause.to_payload(),
                self.delivery_clause.to_payload(),
            ],
            "shortfalls": self.shortfalls,
            "rendered": self.render(),
        }


def dream_block(counts: DreamCounts, *, lookback_days: int = DELIVERY_LOOKBACK_DAYS) -> DreamBlock:
    """One project's block.  Acceptance feeds no numerator here and cannot be made to."""

    rate, fp_clause = false_positive_relevance(
        relevant=counts.relevant,
        not_relevant=counts.not_relevant,
        cannot_judge=counts.cannot_judge,
    )
    delivery_rate, delivery_clause = later_useful_delivery(
        useful=counts.delivery_useful,
        not_useful=counts.delivery_not_useful,
        completed_proposals=counts.completed_proposals,
        too_early=counts.delivery_too_early,
        lookback_days=lookback_days,
    )
    return DreamBlock(
        project_id=counts.project_id,
        counts=counts,
        false_positive_rate=rate,
        false_positive_clause=fp_clause,
        delivery_rate=delivery_rate,
        delivery_clause=delivery_clause,
        coverage_clause=adjudication_coverage_clause(counts.adjudications),
    )


def dream_block_unavailable(*, project_id: str | None = None, detail: str) -> DreamBlock:
    """The tables are not there.  Say that, with the reason, and print no counts at all."""

    clause = clause_not_computable("dreams", "dream_tables_absent", detail=detail)
    return DreamBlock(
        project_id=project_id,
        counts=DreamCounts(project_id=project_id),
        false_positive_rate=None,
        false_positive_clause=clause,
        delivery_rate=None,
        delivery_clause=clause,
        coverage_clause=clause,
        available=False,
    )


def dream_blocks(counts: Sequence[DreamCounts], *, lookback_days: int = DELIVERY_LOOKBACK_DAYS) -> list[DreamBlock]:
    """One block per project, in the order given.  **Never** a pooled total.

    A repeated ``project_id`` is a ``ValueError`` rather than a silent merge: two rows for one
    project mean the caller already aggregated something, and the whole point of the cell is that
    this module does not know how it was aggregated.
    """

    seen: set[str | None] = set()
    for entry in counts:
        if entry.project_id in seen:
            raise ValueError(f"dream_blocks refuses to pool: project_id {entry.project_id!r} appears twice")
        seen.add(entry.project_id)
    return [dream_block(entry, lookback_days=lookback_days) for entry in counts]


def render_dream_blocks(blocks: Sequence[DreamBlock]) -> str:
    """The text the report prints.  No block is an average of another and none is a total."""

    if not blocks:
        return "DREAMS: no project in scope has a dream corpus."
    return "\n\n".join(block.render() for block in blocks)


def summarise_states(blocks: Sequence[DreamBlock]) -> dict[str, int]:
    """A census of clause states across blocks — for the exit line, never for a verdict.

    It counts how many blocks are short and how many are not computable.  It deliberately
    computes no rate: averaging two projects' false-positive rates is the pooling this report
    refuses, and a census of refusals is not a measurement of anything.
    """

    census = {state.value: 0 for state in ClauseState}
    for block in blocks:
        for clause in (block.coverage_clause, block.false_positive_clause, block.delivery_clause):
            census[clause.state.value] += 1
    return census


def relevance_from_value(value: str) -> RelevanceVerdict:
    """Parse a stored ``relevance`` string, refusing anything outside the closed vocabulary."""

    return RelevanceVerdict(str(value))
