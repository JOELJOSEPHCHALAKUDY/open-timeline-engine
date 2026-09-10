"""The one decision policy: ``decide(request) -> DecisionResult``.

Before this module the repository had thirteen routes that decided something and one route
that was evaluated, and they were disjoint sets.  The evaluated route — ``predict_behavior``
— had abstention, an out-of-distribution status and real observation-id citations, and no
consumer in any deployed decision.  Every deployed takeover turn was settled instead by an
LLM prompt, and on any failure a fallback fabricated a decision.  ``decide()`` is the single
callable both sides now go through, so the thing that is scored is the thing that ships.

Four properties are worth stating because each replaces a specific defect:

* **An empty candidate set abstains.**  ``_map_choice`` opens with ``if not allowed: return
  choice``, so without an explicit guard the policy hands back an option nobody offered.  On
  the live corpus 32 of 34 observations carry fewer than two candidate options, so this is
  the ordinary turn rather than an edge case.
* **Recency is measured at decision time**, in ``behavior_fidelity._similarity``.
* **A model's self-report never reaches a score.**  ``AdvisorContribution`` has no confidence
  field — that is the type-level enforcement — and the advisor's only powers are to agree, to
  disagree, to abstain, and to name conflicting ids.  It can never select an option the
  deterministic layer did not select and it can never raise a score.  The last indirect route
  was ``Adequacy.learning_eligible_count``, and through it ``evidence_strength_label``: that
  count comes from ``decision_observations.learning_eligible``, which is written by
  ``behavior_fidelity.behavior_storage_gate``, which used to weight the writer's own
  ``confidence``.  A writer could claim confidence, become eligible, be counted, and look
  strong.  That term is gone; the gate's docstring carries the re-derivation.
* **Permission is separate from the decision (``exposed``).**  A family that has not earned
  the right to use personalization still gets a full, truthful, scoreable decision; it is
  simply not applied to the turn.  An unqualified family means personalization is not used.
  It does not mean the product stops, and it is never an abstention reason: with zero
  qualified families — the state of every family today — ``exposed`` is ``False`` everywhere
  and every turn is byte-for-byte what it was before this module existed.

Purity.  Stdlib plus ``tce_shared.behavior_fidelity`` and ``tce_shared.policy_thresholds``
only.  No pydantic, no SQLAlchemy, no settings, and in particular no import of
``tce_shared.events`` — ``events.py`` imports the enums declared here, not the other way
round, because this module has to stay importable by the worker.  ``decide()`` performs no
retrieval: the caller passes the rows its own loader returned, which is what lets the
qualification harness call it with a frozen training set.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .behavior_fidelity import (
    _as_datetime,
    _choice_key,
    _effective_sample_size,
    _map_choice,
    _similarity,
    _tokens,
    _topical_overlap,
    eligible_behavior_evidence,
)
from .policy_thresholds import (
    CONFLICT_SHARE_RATIO,
    EVIDENCE_DRIFT_MAX_GROWTH,
    MAX_NEIGHBOURS,
    MIN_AGREEMENT_SHARE,
    MIN_EFFECTIVE_SAMPLE,
    OOD_OVERLAP_FLOOR,
    SIMILARITY_FLOOR,
    THRESHOLDS_SHA,
)

__all__ = [
    "ADVISOR_ABSENT_MEANS",
    "ADVISOR_UNAVAILABLE_MEANS",
    "DECISION_POLICY_REVISION",
    "DEFAULT_TUNING",
    "AbstainReason",
    "Adequacy",
    "AdvisorAgreement",
    "AdvisorContribution",
    "ConflictStatus",
    "DecisionRequest",
    "DecisionResult",
    "DecisionStatus",
    "ExplicitRule",
    "ExposureState",
    "OodStatus",
    "PolicyTuning",
    "Qualification",
    "QualificationState",
    "RankedOption",
    "advice_names_a_candidate",
    "apply_explicit_rules",
    "context_evidence_snapshot",
    "decide",
    "evidence_strength_label",
    "failed_advisor_contribution",
    "validate_cited_ids",
]


# --- vocabulary ----------------------------------------------------------------------
# Declared here, imported by events.py.  The reverse would make this module depend on
# pydantic, and the worker cannot carry that.


class DecisionStatus(StrEnum):
    SELECTED = "selected"
    ABSTAINED = "abstained"
    RULE_APPLIED = "rule_applied"


class AbstainReason(StrEnum):
    NO_ELIGIBLE_EVIDENCE = "no_eligible_evidence"
    NO_CANDIDATE_MATCH = "no_candidate_match"
    INADEQUATE_EVIDENCE = "inadequate_evidence"
    OUT_OF_DISTRIBUTION = "out_of_distribution"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    ADVISOR_FORCED = "advisor_forced"
    # There is deliberately no FAMILY_NOT_QUALIFIED member.  Permission is `exposed`;
    # lacking it is a reason not to personalize, never a reason to ask a human.


class OodStatus(StrEnum):
    IN_DISTRIBUTION = "in_distribution"
    OUT_OF_DISTRIBUTION = "out_of_distribution"
    UNKNOWN = "unknown"


class ConflictStatus(StrEnum):
    NONE = "none"
    SPLIT_VOTE = "split_vote"
    EXPLICIT_CONTRADICTION = "explicit_contradiction"


class AdvisorAgreement(StrEnum):
    ABSENT = "absent"
    AGREED = "agreed"
    DISAGREED = "disagreed"
    ABSTAINED = "abstained"
    UNAVAILABLE = "unavailable"


class QualificationState(StrEnum):
    QUALIFIED = "qualified"
    NOT_QUALIFIED = "not_qualified"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    DEMOTED = "demoted"


class ExposureState(StrEnum):
    """Why personalization was or was not applied.  Reported, never an abstention."""

    EXPOSED = "exposed"
    NO_QUALIFICATION = "no_qualification"
    QUALIFICATION_EXPIRED = "qualification_expired"
    BINDING_MISMATCH = "binding_mismatch"
    EVIDENCE_DRIFT = "evidence_drift"
    RULE_LAYER = "rule_layer"


DECISION_POLICY_REVISION: str = "p4-2026-09"
"""The decision policy's own revision.

Named in full, never bare ``policy_revision``: ``ResolvedScope.policy_revision`` already
exists and means the *scope* policy revision.  Two things named ``policy_revision`` in one
call frame is how a binding check gets wired to the wrong one and then always matches.
"""


# --- inputs --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyTuning:
    """The only two knobs, and both are hashed into a bound key on the qualification.

    Everything that can move an abstention or a verdict lives in ``policy_thresholds`` as a
    frozen constant.  These two are scoping and advisor-presence switches, and even they
    cannot be moved after a qualification is written without invalidating it.
    """

    allow_unscoped_project_evidence: bool = True
    advisor_required_families: frozenset[str] = frozenset()

    def tuning_sha(self) -> str:
        raw = json.dumps(
            {
                "allow_unscoped_project_evidence": self.allow_unscoped_project_evidence,
                "advisor_required_families": sorted(self.advisor_required_families),
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


DEFAULT_TUNING = PolicyTuning()


@dataclass(frozen=True, slots=True)
class ExplicitRule:
    rule_id: str
    statement: str
    priority: int
    scope_project_id: str | None


@dataclass(frozen=True, slots=True)
class AdvisorContribution:
    """The LLM's parsed output.  Note what is absent: there is no confidence field."""

    recommended_option: str | None
    abstained: bool
    abstain_reason: str | None
    evidence_ids: tuple[str, ...]
    conflicting_evidence_ids: tuple[str, ...]
    advisor_note: str | None
    parse_state: str  # "parsed" | "unparseable" | "call_failed" | "not_run"
    prompt_sha256: str
    model_id: str
    runtime_version: str


#: The two advisor-absence states, and the rule that keeps them apart.
#:
#: **ABSENT** — ``request.advisor is None`` — means *never attempted* on this turn: the cadence
#: was not due, the turn deadline closed the gate, the advisor is switched off, or the family
#: does not require one.  The advisor is an optional input, so the policy simply proceeds
#: without it.
#:
#: **UNAVAILABLE** — a contribution whose ``parse_state != "parsed"`` — means *attempted and
#: failed*: an error, a timeout, a missing credential, or output that would not parse.  That is
#: a materially different fact and it forces an abstention (``_advisor_stage``), which is what
#: preserves the ``advisor_unhealthy -> safety gate`` behaviour a deadline skip must never
#: suppress.  Collapsing it into ``None`` lets a dead advisor look exactly like an advisor
#: nobody asked for, and the turn then decides where it should abstain.
#:
#: So a call site that *knows* an attempt happened must return this, never ``None``.
ADVISOR_ABSENT_MEANS = "never attempted"
ADVISOR_UNAVAILABLE_MEANS = "attempted and failed"


def failed_advisor_contribution(
    reason: str,
    *,
    parse_state: str = "call_failed",
    model_id: str = "",
    runtime_version: str = "",
    prompt_sha256: str = "",
) -> AdvisorContribution:
    """The UNAVAILABLE state, constructed at the one place that knows the call failed.

    ``parse_state`` is ``"call_failed"`` for a dead or erroring provider and ``"unparseable"``
    for output that arrived and could not be read; both are UNAVAILABLE to the policy, and the
    distinction is kept only because the note a human reads is different.
    """

    return AdvisorContribution(
        recommended_option=None,
        # Belt and braces: `_advisor_stage` returns UNAVAILABLE before it ever reads this, but
        # a non-parsed contribution that claimed `abstained=False` would be a lie on its face.
        abstained=True,
        abstain_reason=str(reason or parse_state)[:220],
        evidence_ids=(),
        conflicting_evidence_ids=(),
        advisor_note=None,
        parse_state=str(parse_state or "call_failed"),
        prompt_sha256=str(prompt_sha256 or ""),
        model_id=str(model_id or ""),
        runtime_version=str(runtime_version or ""),
    )


@dataclass(frozen=True, slots=True)
class Qualification:
    qualification_id: str
    state: QualificationState
    project_id: str | None
    decision_family: str
    decision_policy_revision: str
    model_id: str
    runtime_version: str
    prompt_sha256: str
    retrieval_version: str
    thresholds_sha: str
    tuning_sha: str
    evidence_cutoff_at: datetime | None
    learning_eligible_at_qualification: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    decision_family: str
    situation_type: str
    situation_summary: str
    objective_text: str
    constraints: Mapping[str, Any]
    context_snapshot: Mapping[str, Any]
    candidate_options: tuple[str, ...]
    evidence_rows: tuple[Mapping[str, Any], ...]
    decision_at: datetime
    workspace_id: str
    subject_user_id: str
    project_id: str | None
    episode_key: str
    evidence_revision: str
    evidence_cutoff_at: datetime | None
    retrieval_version: str
    model_id: str
    runtime_version: str
    learning_eligible_total: int = 0
    qualification: Qualification | None = None
    explicit_rules: tuple[ExplicitRule, ...] = ()
    advisor: AdvisorContribution | None = None
    tuning: PolicyTuning = DEFAULT_TUNING

    def query(self) -> dict[str, Any]:
        """The neighbour-scoring view of this request."""

        return {
            "situation_type": self.situation_type,
            "situation_summary": self.situation_summary,
            "objective_text": self.objective_text,
            "constraints": dict(self.constraints),
            "context_snapshot": dict(self.context_snapshot),
        }

    def fingerprint(self) -> str:
        """A canonical hash over the inputs a decision actually depends on.

        Captured on a live turn and recomputed in replay.  Asserting that a live turn and its
        replay used the same *policy revision* proves nothing — both read the same module
        constant — so this hashes the request instead, and a divergence in candidate options,
        evidence set, decision time, retrieval version or advisor presence shows up as an
        unequal fingerprint rather than as a silently different evaluation.
        """

        payload = {
            "decision_family": self.decision_family,
            "situation_type": self.situation_type,
            "candidate_options": sorted(self.candidate_options),
            "evidence_ids": sorted(str(row.get("id") or "") for row in self.evidence_rows),
            "decision_at": self.decision_at.isoformat(),
            "evidence_revision": self.evidence_revision,
            "retrieval_version": self.retrieval_version,
            "decision_policy_revision": DECISION_POLICY_REVISION,
            # p45_shared §10 names `thresholds_sha` in the canonical hash, and it is the term that
            # earns its place: without it a replay run under different abstention floors produces a
            # MATCHING fingerprint, so the one instrument that is supposed to prove "same inputs,
            # same decision" would certify a run whose floors had been moved underneath it.
            "thresholds_sha": THRESHOLDS_SHA,
            "advisor_present": self.advisor is not None,
            "advisor_option": self.advisor.recommended_option if self.advisor else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]


# --- outputs -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankedOption:
    option: str
    share: float
    supporting_observation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Adequacy:
    eligible_count: int
    neighbour_count: int
    above_floor_count: int
    effective_sample_size: float
    top_similarity: float
    top_overlap: float
    agreement_share: float
    learning_eligible_count: int
    median_evidence_age_days: float
    adequate: bool
    shortfalls: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "eligible_count": self.eligible_count,
            "neighbour_count": self.neighbour_count,
            "above_floor_count": self.above_floor_count,
            "effective_sample_size": self.effective_sample_size,
            "top_similarity": self.top_similarity,
            "top_overlap": self.top_overlap,
            "agreement_share": self.agreement_share,
            "learning_eligible_count": self.learning_eligible_count,
            "median_evidence_age_days": self.median_evidence_age_days,
            "adequate": self.adequate,
            "shortfalls": list(self.shortfalls),
        }


_EMPTY_ADEQUACY_SHORTFALL = "no_candidate_options"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    status: DecisionStatus
    selected_option: str | None
    ranked_options: tuple[RankedOption, ...]
    evidence_observation_ids: tuple[str, ...]
    evidence_role: str  # "supporting" | "considered" | "rule"
    conflicting_observation_ids: tuple[str, ...]
    abstain_reason: AbstainReason | None
    reason_for_asking: str | None
    ood_status: OodStatus
    ood_score: float
    conflict_status: ConflictStatus
    policy_score: float  # UNCALIBRATED, and named so.  There is no calibrated sibling.
    adequacy: Adequacy
    suggested_action: str | None
    advisor_agreement: AdvisorAgreement
    advisor_citation_overlap: float | None
    advisor_note: str | None
    applied_rule_id: str | None
    decision_policy_revision: str
    retrieval_version: str
    prompt_sha256: str | None
    thresholds_sha: str
    tuning_sha: str
    evidence_revision: str
    evidence_cutoff_at: datetime | None
    qualification_id: str | None
    exposed: bool
    exposure_state: ExposureState
    request_fingerprint: str
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """The single serializer.

        Every persistence site and every response model goes through this, so a field cannot
        be stored under one name and returned under another.
        """

        return {
            "status": self.status.value,
            "selected_option": self.selected_option,
            "ranked_options": [
                {
                    "option": item.option,
                    "share": item.share,
                    "supporting_observation_ids": list(item.supporting_observation_ids),
                }
                for item in self.ranked_options
            ],
            "evidence_observation_ids": list(self.evidence_observation_ids),
            "evidence_role": self.evidence_role,
            "conflicting_observation_ids": list(self.conflicting_observation_ids),
            "abstain_reason": self.abstain_reason.value if self.abstain_reason else None,
            "reason_for_asking": self.reason_for_asking,
            "ood_status": self.ood_status.value,
            "ood_score": self.ood_score,
            "conflict_status": self.conflict_status.value,
            "policy_score": self.policy_score,
            "adequacy": self.adequacy.to_payload(),
            "suggested_action": self.suggested_action,
            "advisor_agreement": self.advisor_agreement.value,
            "advisor_citation_overlap": self.advisor_citation_overlap,
            "advisor_note": self.advisor_note,
            "applied_rule_id": self.applied_rule_id,
            "decision_policy_revision": self.decision_policy_revision,
            "retrieval_version": self.retrieval_version,
            "prompt_sha256": self.prompt_sha256,
            "thresholds_sha": self.thresholds_sha,
            "tuning_sha": self.tuning_sha,
            "evidence_revision": self.evidence_revision,
            "evidence_cutoff_at": self.evidence_cutoff_at.isoformat() if self.evidence_cutoff_at else None,
            "qualification_id": self.qualification_id,
            "exposed": self.exposed,
            "exposure_state": self.exposure_state.value,
            "request_fingerprint": self.request_fingerprint,
            "diagnostics": dict(self.diagnostics),
        }

    def block_payload(self) -> dict[str, Any]:
        """The narrow projection handed to an executor over MCP.

        ``policy_score`` is deliberately absent: an executor reading a number it cannot
        interpret is how "four different fields named confidence" started.
        """

        return {
            "status": self.status.value,
            "selected_option": self.selected_option,
            "abstain_reason": self.abstain_reason.value if self.abstain_reason else None,
            "reason_for_asking": self.reason_for_asking,
            "ood_status": self.ood_status.value,
            "conflict_status": self.conflict_status.value,
            "evidence_observation_ids": list(self.evidence_observation_ids[:12]),
            "decision_policy_revision": self.decision_policy_revision,
            "exposed": self.exposed,
            "exposure_state": self.exposure_state.value,
            "advisor_agreement": self.advisor_agreement.value,
        }


# --- small pure helpers ---------------------------------------------------------------


def validate_cited_ids(claimed: Sequence[str], offered: Sequence[str]) -> tuple[tuple[str, ...], int]:
    """Intersect model-claimed observation ids with the ids actually rendered to it.

    Returns ``(kept, dropped_count)``.  An id the model invented is not evidence, and
    counting how many it invented is the honesty signal.
    """

    allowed = {str(value).strip() for value in offered if str(value).strip()}
    kept: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for value in claimed:
        candidate = str(value).strip()
        if not candidate:
            continue
        if candidate not in allowed:
            dropped += 1
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        kept.append(candidate)
    return tuple(kept), dropped


def evidence_strength_label(adequacy: Adequacy) -> str:
    """``strong`` | ``moderate`` | ``weak``, from properties of the evidence and nothing else.

    No model number and no citation count.  Both backends import this one function, so the
    old Full ``{strong, moderate, weak}`` / Lite ``{high, medium, low}`` fork — which meant a
    branch keyed on the label could never fire on Lite — cannot reappear.
    """

    if (
        adequacy.above_floor_count >= 4
        and adequacy.effective_sample_size >= 3.0
        and adequacy.agreement_share >= 0.75
        and adequacy.learning_eligible_count >= 3
    ):
        return "strong"
    if (
        adequacy.above_floor_count >= 2
        and adequacy.effective_sample_size >= MIN_EFFECTIVE_SAMPLE
        and adequacy.agreement_share >= MIN_AGREEMENT_SHARE
    ):
        return "moderate"
    return "weak"


def advice_names_a_candidate(rendered_text: str, candidate_options: Sequence[str]) -> bool:
    """True when the text the human read already names one of the options they are about to
    choose between.

    This — not "did an advisor run" — is the contamination the promotion gate must exclude.
    The column that used to carry it was written as ``bool(<eight-key dict literal>)``, which
    is ``True`` unconditionally at every site in both backends, so it never measured anything.
    """

    text = str(rendered_text or "").strip()
    if not text:
        return False
    text_tokens = _tokens(text)
    normalized = _choice_key(text)
    for option in candidate_options:
        candidate = str(option or "").strip()
        if not candidate:
            continue
        option_key = _choice_key(candidate)
        if option_key and option_key in normalized:
            return True
        option_tokens = _tokens(candidate)
        if option_tokens and option_tokens <= text_tokens:
            return True
    return False


def apply_explicit_rules(request: DecisionRequest) -> tuple[ExplicitRule, str] | None:
    """The deterministic explicit-rule layer: a stated instruction, not personalization.

    Rules in scope are walked by ``(priority, rule_id)`` and the first whose statement maps
    onto an offered candidate wins.  A rule result is exposed regardless of qualification,
    because obeying an instruction the human wrote down is not the same act as predicting
    what they would have chosen.
    """

    if not request.candidate_options:
        return None
    in_scope = [
        rule
        for rule in request.explicit_rules
        if rule.scope_project_id is None or rule.scope_project_id == request.project_id
    ]
    for rule in sorted(in_scope, key=lambda item: (item.priority, item.rule_id)):
        mapped = _map_choice(str(rule.statement or "").strip(), request.candidate_options)
        if mapped:
            return rule, mapped
    return None


def _reason_for_asking(
    abstain_reason: AbstainReason | None,
    adequacy: Adequacy,
    ranked_options: Sequence[RankedOption],
) -> str | None:
    """A pure string builder, one branch per reason.  Contains no model output."""

    if abstain_reason is None:
        return None
    if abstain_reason is AbstainReason.NO_CANDIDATE_MATCH:
        if not ranked_options:
            return "I do not have a set of options to choose between here — what are the choices?"
        return "None of your past decisions map onto the options on the table — which one applies?"
    if abstain_reason is AbstainReason.NO_ELIGIBLE_EVIDENCE:
        return "Nothing in your history is eligible to go on here — how do you want to handle this?"
    if abstain_reason is AbstainReason.INADEQUATE_EVIDENCE:
        shortfall = ", ".join(adequacy.shortfalls) or "too little evidence"
        return f"Not enough to go on ({shortfall}) — which option do you want?"
    if abstain_reason is AbstainReason.OUT_OF_DISTRIBUTION:
        return "Nothing similar enough in your history to go on — what would you do here?"
    if abstain_reason is AbstainReason.CONFLICTING_EVIDENCE:
        return "Two past decisions here disagree — which one applies?"
    if abstain_reason is AbstainReason.ADVISOR_FORCED:
        return "I could not settle this one — which option do you want?"
    return "Which option do you want here?"


def _exposure(request: DecisionRequest) -> tuple[bool, ExposureState, str | None]:
    """Whether personalization may be applied to this turn, and why not when it may not.

    This is the only place a qualification record is read.  It reads no setting, and refusal
    is the default: the absence of a qualification record *is* the refusal.  Nothing about
    the decision itself changes here.
    """

    record = request.qualification
    if record is None:
        return False, ExposureState.NO_QUALIFICATION, None
    if record.state is not QualificationState.QUALIFIED:
        state = (
            ExposureState.QUALIFICATION_EXPIRED
            if record.state is QualificationState.EXPIRED
            else ExposureState.NO_QUALIFICATION
        )
        return False, state, record.qualification_id
    if record.expires_at <= request.decision_at:
        return False, ExposureState.QUALIFICATION_EXPIRED, record.qualification_id

    bound = (
        record.decision_policy_revision == DECISION_POLICY_REVISION
        and record.model_id == request.model_id
        and record.runtime_version == request.runtime_version
        and record.retrieval_version == request.retrieval_version
        and record.thresholds_sha == THRESHOLDS_SHA
        and record.tuning_sha == request.tuning.tuning_sha()
        and record.decision_family == request.decision_family
        and record.project_id == request.project_id
    )
    advisor_prompt = request.advisor.prompt_sha256 if request.advisor is not None else ""
    if bound and record.prompt_sha256 != advisor_prompt:
        bound = False
    if not bound:
        return False, ExposureState.BINDING_MISMATCH, record.qualification_id

    # Evidence drift: the learning-eligible corpus for this family has grown so much since
    # the qualification was measured that the measurement no longer describes the corpus in
    # use.  Growth only — a shrinking corpus is a different problem and is not silently
    # treated as this one.
    baseline = record.learning_eligible_at_qualification
    if baseline > 0:
        grown = request.learning_eligible_total - baseline
        if (grown / baseline) > EVIDENCE_DRIFT_MAX_GROWTH:
            return False, ExposureState.EVIDENCE_DRIFT, record.qualification_id
    return True, ExposureState.EXPOSED, record.qualification_id


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2.0)


def _empty_adequacy(*, eligible_count: int = 0, shortfalls: tuple[str, ...] = ()) -> Adequacy:
    return Adequacy(
        eligible_count=eligible_count,
        neighbour_count=0,
        above_floor_count=0,
        effective_sample_size=0.0,
        top_similarity=0.0,
        top_overlap=0.0,
        agreement_share=0.0,
        learning_eligible_count=0,
        median_evidence_age_days=0.0,
        adequate=False,
        shortfalls=shortfalls,
    )


def context_evidence_snapshot(
    request: DecisionRequest,
    result: DecisionResult | None = None,
) -> tuple[int, float]:
    """The decision evidence THIS turn actually has: (count above the similarity floor, median age).

    ``context_quality_score`` reads this rather than ``result.adequacy``, and the difference is
    not cosmetic.  ``decide()`` abstains at **stage 2** when the turn offers no candidate
    options, which is every ordinary takeover turn, and that exit returns ``_empty_adequacy`` --
    zero evidence, zero age -- without ever reaching stage 3.  Scoring the context from that
    abstention makes the quality of the context a constant: an empty corpus and a corpus holding
    two hundred relevant prior decisions score identically, and on the low side, so bounded
    retrieval fires and the turn escalates on every corpus forever.  "Always ask the human" is a
    product shutdown wearing a safety argument, and it is not what a quality score is for.

    The evidence is a property of the request, so it is read from the request.  Whether any of it
    mapped onto an offered option is a property of the *question*, and belongs to the vote.

    When the policy did reach stage 3 its own adequacy is authoritative and is used as-is.
    """

    if result is not None and result.adequacy.eligible_count > 0:
        return result.adequacy.above_floor_count, result.adequacy.median_evidence_age_days

    eligible = eligible_behavior_evidence(
        request.evidence_rows,
        at=request.decision_at,
        historical=True,
    )
    if not eligible:
        return 0, 0.0
    query = request.query()
    scored = sorted(
        ((_similarity(query, row, decision_at=request.decision_at), row) for row in eligible),
        key=lambda item: (-item[0], -_as_datetime(item[1].get("ts")).timestamp(), str(item[1].get("id") or "")),
    )[:MAX_NEIGHBOURS]
    # Same admissibility rule as stage 7, including §4.1's NULL-project clause, so the two
    # readings of "evidence above the floor" cannot drift apart.
    project_bound = request.project_id is not None and not request.tuning.allow_unscoped_project_evidence
    above: list[Mapping[str, Any]] = []
    for similarity, row in scored:
        if similarity < SIMILARITY_FLOOR:
            continue
        if project_bound and not str(row.get("project_id") or "").strip():
            continue
        above.append(row)
    if not above:
        return 0, 0.0
    ages = [
        max(0.0, (request.decision_at - _as_datetime(row.get("ts"))).total_seconds() / 86400.0)
        for row in above
    ]
    return len(above), round(_median(ages), 6)


def _advisor_stage(
    request: DecisionRequest,
    selected_option: str | None,
    supporting_ids: Sequence[str],
    offered_ids: Sequence[str],
) -> tuple[AdvisorAgreement, bool, tuple[str, ...], float | None, str | None]:
    """Returns ``(agreement, forces_abstention, conflicting_ids, citation_overlap, note)``.

    Precedence is stated once and in this order, because the interesting case is a *failed*
    model call.  ``parse_state != "parsed"`` forces an abstention **unconditionally**, before
    ``advisor_required_families`` is ever consulted — that setting defaults to empty, so
    checking it first would mean a dead model is ignored on every family.  Abstention is the
    failure mode by default, not behind a knob.
    """

    advisor = request.advisor
    if advisor is None:
        forced = request.decision_family in request.tuning.advisor_required_families
        return AdvisorAgreement.ABSENT, forced, (), None, None

    note = advisor.advisor_note
    if advisor.parse_state != "parsed":
        return AdvisorAgreement.UNAVAILABLE, True, (), None, note

    conflicting, _ = validate_cited_ids(advisor.conflicting_evidence_ids, offered_ids)
    kept, _dropped = validate_cited_ids(advisor.evidence_ids, offered_ids)
    overlap: float | None = None
    if kept:
        supporting = {str(value) for value in supporting_ids}
        overlap = round(len([value for value in kept if value in supporting]) / len(kept), 6)

    if advisor.abstained:
        return AdvisorAgreement.ABSTAINED, True, conflicting, overlap, note
    if selected_option is not None and _choice_key(advisor.recommended_option or "") == _choice_key(selected_option):
        return AdvisorAgreement.AGREED, False, conflicting, overlap, note
    return AdvisorAgreement.DISAGREED, True, conflicting, overlap, note


def decide(request: DecisionRequest) -> DecisionResult:
    """The policy.  Ten ordered stages; the stage that terminates names the reason."""

    fingerprint = request.fingerprint()
    tuning_sha = request.tuning.tuning_sha()
    prompt_sha = request.advisor.prompt_sha256 if request.advisor is not None else None

    # ABSENT vs UNAVAILABLE, reported on every exit rather than only the one that runs
    # `_advisor_stage`.  `finish` used to default this to ABSENT, so a stage that terminates
    # early -- an explicit rule, an empty candidate set, no eligible evidence -- reported "no
    # advisor was asked" about a turn that asked one and got a dead socket back.  That is not a
    # cosmetic difference: `candidate_options` is empty on every takeover turn today, so the
    # stage-2 exit is the ONLY exit those turns take and the two states were indistinguishable
    # everywhere a human or a report could see them.  `_advisor_stage` still overrides this on
    # the path that reaches it, and it still forces the abstention.
    advisor_presence = (
        AdvisorAgreement.ABSENT
        if request.advisor is None or request.advisor.parse_state == "parsed"
        else AdvisorAgreement.UNAVAILABLE
    )

    def finish(
        *,
        status: DecisionStatus,
        selected_option: str | None = None,
        ranked_options: tuple[RankedOption, ...] = (),
        evidence_observation_ids: tuple[str, ...] = (),
        evidence_role: str = "considered",
        conflicting_observation_ids: tuple[str, ...] = (),
        abstain_reason: AbstainReason | None = None,
        ood_status: OodStatus = OodStatus.UNKNOWN,
        ood_score: float = 1.0,
        conflict_status: ConflictStatus = ConflictStatus.NONE,
        policy_score: float = 0.0,
        adequacy: Adequacy | None = None,
        suggested_action: str | None = None,
        advisor_agreement: AdvisorAgreement | None = None,
        advisor_citation_overlap: float | None = None,
        advisor_note: str | None = None,
        applied_rule_id: str | None = None,
        rule_layer: bool = False,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> DecisionResult:
        if rule_layer:
            exposed, exposure_state, qualification_id = True, ExposureState.RULE_LAYER, None
        else:
            exposed, exposure_state, qualification_id = _exposure(request)
        resolved_adequacy = adequacy if adequacy is not None else _empty_adequacy()
        return DecisionResult(
            status=status,
            selected_option=selected_option,
            ranked_options=ranked_options,
            evidence_observation_ids=evidence_observation_ids,
            evidence_role=evidence_role,
            conflicting_observation_ids=conflicting_observation_ids,
            abstain_reason=abstain_reason,
            reason_for_asking=_reason_for_asking(abstain_reason, resolved_adequacy, ranked_options),
            ood_status=ood_status,
            ood_score=round(max(0.0, min(1.0, ood_score)), 6),
            conflict_status=conflict_status,
            policy_score=round(max(0.0, min(1.0, policy_score)), 6),
            adequacy=resolved_adequacy,
            suggested_action=suggested_action,
            advisor_agreement=advisor_presence if advisor_agreement is None else advisor_agreement,
            advisor_citation_overlap=advisor_citation_overlap,
            advisor_note=advisor_note,
            applied_rule_id=applied_rule_id,
            decision_policy_revision=DECISION_POLICY_REVISION,
            retrieval_version=request.retrieval_version,
            prompt_sha256=prompt_sha,
            thresholds_sha=THRESHOLDS_SHA,
            tuning_sha=tuning_sha,
            evidence_revision=request.evidence_revision,
            evidence_cutoff_at=request.evidence_cutoff_at,
            qualification_id=qualification_id,
            exposed=exposed,
            exposure_state=exposure_state,
            request_fingerprint=fingerprint,
            diagnostics=dict(diagnostics or {}),
        )

    # Stage 1 — the explicit-rule layer.  Skips every stage below and is never gated by
    # qualification, because a stated instruction is not a prediction about the human.
    rule_hit = apply_explicit_rules(request)
    if rule_hit is not None:
        rule, option = rule_hit
        return finish(
            status=DecisionStatus.RULE_APPLIED,
            selected_option=option,
            ranked_options=(RankedOption(option=option, share=1.0, supporting_observation_ids=()),),
            evidence_role="rule",
            policy_score=1.0,
            applied_rule_id=rule.rule_id,
            rule_layer=True,
        )

    # Stage 2 — the candidate guard.  BEFORE any call to _map_choice, which returns the raw
    # historical label when `allowed` is empty and would hand back an option nobody offered.
    if not request.candidate_options:
        return finish(
            status=DecisionStatus.ABSTAINED,
            abstain_reason=AbstainReason.NO_CANDIDATE_MATCH,
            adequacy=_empty_adequacy(shortfalls=(_EMPTY_ADEQUACY_SHORTFALL,)),
        )

    # Stage 3 — eligibility, filtered at decision time.  `historical=True` unconditionally,
    # so the live path and the harness apply the same filter rather than two.
    eligible = eligible_behavior_evidence(
        request.evidence_rows,
        at=request.decision_at,
        historical=True,
    )
    if not eligible:
        return finish(
            status=DecisionStatus.ABSTAINED,
            abstain_reason=AbstainReason.NO_ELIGIBLE_EVIDENCE,
            adequacy=_empty_adequacy(shortfalls=("no_eligible_evidence",)),
        )

    # Stage 4 — neighbours.
    query = request.query()
    scored = sorted(
        ((_similarity(query, row, decision_at=request.decision_at), row) for row in eligible),
        key=lambda item: (-item[0], -_as_datetime(item[1].get("ts")).timestamp(), str(item[1].get("id") or "")),
    )[:MAX_NEIGHBOURS]

    # Stage 5 / 6 — map each neighbour onto an offered option, vote, and bind the neighbour's
    # id to the option it voted for.  The binding is what makes "the evidence for the chosen
    # option" a lookup instead of a re-derivation nobody can check.
    votes: dict[str, float] = {}
    labels: dict[str, str] = {}
    supporters: dict[str, list[str]] = {}
    weights_by_option: dict[str, list[float]] = {}
    actions_by_option: dict[str, dict[str, float]] = {}
    row_by_id: dict[str, Mapping[str, Any]] = {}
    option_by_row_id: dict[str, str] = {}
    considered_ids: list[str] = []
    unmappable = 0
    for similarity, row in scored:
        row_id = str(row.get("id") or "")
        if row_id:
            row_by_id[row_id] = row
            considered_ids.append(row_id)
        raw_choice = str(row.get("selected_choice") or row.get("user_response") or "").strip()
        if not raw_choice:
            unmappable += 1
            continue
        mapped = _map_choice(raw_choice, request.candidate_options)
        if not mapped:
            unmappable += 1
            continue
        key = _choice_key(mapped)
        labels.setdefault(key, mapped)
        weight = max(0.001, similarity)
        votes[key] = votes.get(key, 0.0) + weight
        weights_by_option.setdefault(key, []).append(weight)
        if row_id:
            supporters.setdefault(key, []).append(row_id)
            option_by_row_id[row_id] = key
        action = str(row.get("action_taken") or "").strip()
        if action:
            bucket = actions_by_option.setdefault(key, {})
            bucket[action] = bucket.get(action, 0.0) + weight

    neighbour_count = len(scored)
    top_similarity = round(scored[0][0], 6) if scored else 0.0
    top_overlap = round(max((_topical_overlap(query, row) for _, row in scored), default=0.0), 6)
    ages = [
        max(0.0, (request.decision_at - _as_datetime(row.get("ts"))).total_seconds() / 86400.0)
        for _, row in scored
    ]
    median_age = round(_median(ages), 6)
    learning_eligible_count = sum(1 for _, row in scored if bool(row.get("learning_eligible", True)))

    # `above_floor_count` is a property of the NEIGHBOURS -- §1.5 stage 7 defines it as the
    # neighbours clearing SIMILARITY_FLOOR -- so it is computed here, before the vote, and both
    # exits report the same number.  It used to be hardcoded to 0 on the no-candidate exit, and
    # an ordinary takeover turn offers no candidate options, so the field was 0 by construction
    # on every ordinary turn.  Anything reading it as "how much decision evidence do I have"
    # -- which is exactly what `context_quality_score` does -- was reading a constant.
    #
    # This loosens no gate.  `evidence_strength_label` is conjunctive on `effective_sample_size`
    # and `agreement_share`, both of which are 0 without a vote, so the label stays `weak`; and
    # the no-candidate exit still abstains with `adequate=False` and its own shortfall.
    project_bound = request.project_id is not None and not request.tuning.allow_unscoped_project_evidence
    above_floor = 0
    for similarity, row in scored:
        if similarity < SIMILARITY_FLOOR:
            continue
        # A NULL-project row is admissible evidence — it is the same subject's decision — but
        # under the strict reading it can never on its own make a project-bound family adequate.
        if project_bound and not str(row.get("project_id") or "").strip():
            continue
        above_floor += 1

    if not votes:
        return finish(
            status=DecisionStatus.ABSTAINED,
            abstain_reason=AbstainReason.NO_CANDIDATE_MATCH,
            evidence_observation_ids=tuple(considered_ids),
            adequacy=Adequacy(
                eligible_count=len(eligible),
                neighbour_count=neighbour_count,
                above_floor_count=above_floor,
                effective_sample_size=0.0,
                top_similarity=top_similarity,
                top_overlap=top_overlap,
                agreement_share=0.0,
                learning_eligible_count=learning_eligible_count,
                median_evidence_age_days=median_age,
                adequate=False,
                shortfalls=("no_mapped_neighbour",),
            ),
            diagnostics={"unmappable_neighbours": unmappable},
        )

    ranked_keys = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    total_weight = sum(weight for _, weight in ranked_keys)
    ranked_options = tuple(
        RankedOption(
            option=labels[key],
            share=round(weight / max(total_weight, 1e-9), 6),
            supporting_observation_ids=tuple(supporters.get(key, ())),
        )
        for key, weight in ranked_keys
    )
    winning_key = ranked_keys[0][0]
    winning = ranked_options[0]
    agreement_share = winning.share

    # Stage 7 — adequacy.  The effective sample size is Kish ESS over the WINNING option's
    # weights, not over every voting weight: a one-row corpus otherwise reported an effective
    # neighbour count of 1.0 beside a confidence of 0.9972 and abstained=False.
    #
    # `above_floor` is computed above, before the vote, because it is a property of the
    # neighbours rather than of the vote.  §4.1 adds a stricter NULL-project rule on top of
    # §1.5's definition and names ONE setting as the thing that governs it,
    # `policy_allow_unscoped_project_evidence`.  That setting had a producer
    # (`policy_store._policy_tuning`) and no reader anywhere, while the strict rule was applied
    # unconditionally — so the two clauses of the spec were both half-implemented and the
    # switch between them did nothing.  It is read there now, and that is the only reader.
    #
    # The default (True) matters and is not cosmetic.  `decision_observations.project_id` is
    # NEW in 20260909_0040 and every pre-P4 row keeps NULL forever, so denying those rows
    # pins `above_floor_count` at 0 for the entire historical corpus on any project-bound turn.
    # That is the same `AND false` the loader's `allow_null_project` exists to avoid, moved one
    # stage later: every prospective case would abstain, coverage could never leave 0, and the
    # promotion gate would be unsatisfiable by construction rather than by lack of data.
    # Set the flag False to get §4.1's strict reading once the corpus carries projects.
    ess = round(_effective_sample_size(weights_by_option.get(winning_key, [])), 6)

    shortfalls: list[str] = []
    if above_floor < 2:
        shortfalls.append("above_floor_count")
    if ess < MIN_EFFECTIVE_SAMPLE:
        shortfalls.append("effective_sample_size")
    if agreement_share < MIN_AGREEMENT_SHARE:
        shortfalls.append("agreement_share")
    adequacy = Adequacy(
        eligible_count=len(eligible),
        neighbour_count=neighbour_count,
        above_floor_count=above_floor,
        effective_sample_size=ess,
        top_similarity=top_similarity,
        top_overlap=top_overlap,
        agreement_share=agreement_share,
        learning_eligible_count=learning_eligible_count,
        median_evidence_age_days=median_age,
        adequate=not shortfalls,
        shortfalls=tuple(shortfalls),
    )

    policy_score = agreement_share * (0.45 + (0.55 * min(1.0, top_similarity)))
    ood_score = 1.0 - top_overlap
    ood_status = OodStatus.OUT_OF_DISTRIBUTION if top_overlap < OOD_OVERLAP_FLOOR else OodStatus.IN_DISTRIBUTION

    # Stage 9's two conflict tests are computed here so that conflict is REPORTED even on a
    # turn that terminates earlier for a different reason.  A family whose evidence is
    # routinely split has to be legible in the report, not only in its abstention rate.
    conflict_status = ConflictStatus.NONE
    conflicting_ids: list[str] = []
    if len(ranked_keys) >= 2:
        top_share = ranked_keys[0][1]
        second_share = ranked_keys[1][1]
        if top_share > 0 and (second_share / top_share) >= CONFLICT_SHARE_RATIO:
            conflict_status = ConflictStatus.SPLIT_VOTE
            conflicting_ids.extend(ranked_options[1].supporting_observation_ids)
    for row_id, row in row_by_id.items():
        source_option = option_by_row_id.get(row_id)
        if source_option is None:
            continue
        for raw_target in row.get("contradicts_observation_ids") or []:
            target_id = str(raw_target).strip()
            target_option = option_by_row_id.get(target_id)
            if target_option is None or target_option == source_option:
                continue
            # The eligibility filter already drops rows contradicted by a confirmed
            # correction.  What is left here is a residual cross-option link between two
            # neighbours that both survived, and that is a real disagreement.
            conflict_status = ConflictStatus.EXPLICIT_CONTRADICTION
            for value in (row_id, target_id):
                if value not in conflicting_ids:
                    conflicting_ids.append(value)

    diagnostics: dict[str, Any] = {
        "unmappable_neighbours": unmappable,
        "considered_count": len(considered_ids),
    }

    def abstain(reason: AbstainReason, **extra: Any) -> DecisionResult:
        return finish(
            status=DecisionStatus.ABSTAINED,
            abstain_reason=reason,
            evidence_observation_ids=tuple(considered_ids),
            conflicting_observation_ids=tuple(conflicting_ids),
            ranked_options=ranked_options,
            ood_status=ood_status,
            ood_score=ood_score,
            conflict_status=conflict_status,
            policy_score=policy_score,
            adequacy=adequacy,
            diagnostics=diagnostics,
            **extra,
        )

    # Stages 7-9 are all computed above and all three statuses are on the result whichever
    # one terminates, so a family whose evidence is routinely split stays legible in the
    # report and not only in its abstention rate.
    #
    # The order the *reason* is chosen in is conflict, then distribution, then adequacy, and
    # that is measured rather than stylistic.  Taking adequacy first makes a split vote
    # unreachable as a reason: CONFLICT_SHARE_RATIO is 0.80 and MIN_AGREEMENT_SHARE is 0.60,
    # and no vote can satisfy `top >= 0.60` and `second/top >= 0.80` at once, because that
    # needs the two shares to sum past 1.  Every genuine disagreement would then be reported
    # as "not enough evidence", which is both less true and less actionable than "two past
    # decisions here disagree - which one applies?".
    if conflict_status is not ConflictStatus.NONE:
        return abstain(AbstainReason.CONFLICTING_EVIDENCE)

    if ood_status is OodStatus.OUT_OF_DISTRIBUTION:
        return abstain(AbstainReason.OUT_OF_DISTRIBUTION)

    if not adequacy.adequate:
        return abstain(AbstainReason.INADEQUATE_EVIDENCE)

    # Stage 10 — selection, then the advisor, then exposure.
    suggested_action: str | None = None
    winning_actions = actions_by_option.get(winning_key, {})
    if winning_actions:
        suggested_action = sorted(winning_actions.items(), key=lambda item: (-item[1], item[0]))[0][0]

    agreement, forces_abstention, advisor_conflicts, citation_overlap, advisor_note = _advisor_stage(
        request,
        winning.option,
        winning.supporting_observation_ids,
        considered_ids,
    )
    for value in advisor_conflicts:
        if value not in conflicting_ids:
            conflicting_ids.append(value)

    if forces_abstention:
        return abstain(
            AbstainReason.ADVISOR_FORCED,
            advisor_agreement=agreement,
            advisor_citation_overlap=citation_overlap,
            advisor_note=advisor_note,
        )

    return finish(
        status=DecisionStatus.SELECTED,
        selected_option=winning.option,
        ranked_options=ranked_options,
        evidence_observation_ids=winning.supporting_observation_ids,
        evidence_role="supporting",
        conflicting_observation_ids=tuple(conflicting_ids),
        ood_status=ood_status,
        ood_score=ood_score,
        conflict_status=conflict_status,
        policy_score=policy_score,
        adequacy=adequacy,
        suggested_action=suggested_action,
        advisor_agreement=agreement,
        advisor_citation_overlap=citation_overlap,
        advisor_note=advisor_note,
        diagnostics=diagnostics,
    )
