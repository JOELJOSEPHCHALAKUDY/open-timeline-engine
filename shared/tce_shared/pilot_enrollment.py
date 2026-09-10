"""The P6 operational-proof pilot: its vocabulary, its allocator and its clause arithmetic.

Pure and deterministic.  No I/O, no pydantic, no SQLAlchemy, no settings — both backends, the
worker and ``scripts/p6_pilot_report.py`` import this module, and ``tce_shared.events`` imports
its enums rather than mirroring them.

Five things live here and nothing else does:

1. **The vocabulary** — the arms, the allocation kinds, the rescue ladder, the completion basis,
   the review verdicts, the clause states, the decision families, and the two fixed sentences.
2. **The allocator** — permuted-block randomisation within stratum (§1.3).  The stratum slot is
   supplied by the caller's atomic ``UPDATE ... RETURNING``; everything downstream of the slot is
   a pure function of ``(stratum_id, slot, salt)`` and therefore recomputable by an auditor.
3. **The enrolment resolution** — ``resolve_enrolment`` decides *reuse an existing arm* versus
   *draw a new one* versus *record an election*, from the episode key alone.
4. **The clause arithmetic** — ``claim_a`` and ``claim_b``, each returning its own type.
5. **The refusals** — NOT COMPUTABLE reasons come from a closed list, and no clause ever renders
   ``0.0`` as a measurement when its denominator was zero.

Three rules are structural here rather than conventional, because a convention is not a
guarantee:

* **The party under test does not grade itself.** Every field is ``server_derived``,
  ``human_attested`` or ``agent_asserted``, and no function in this module reads the third class.
  The owner's own review minutes are adjudication; the executor's own confidence is not.
* **``ClaimBVerdict`` has no ``supported`` field.** A caller cannot render Claim B as a supported
  claim because the type has nowhere to put one, and the REDUCED SUPERVISION sentence is emitted
  by ``claim_b`` itself so it travels with the verdict into JSON and text alike.
* **Nothing here pools.** Every entry point takes the episodes of ONE ``(project_id,
  decision_family)`` cell and refuses a mixed sequence with ``ValueError``.

Three unrelated things in this codebase are called an *episode*.  ``episode_key`` and everything
built on it is a **policy episode** (P4).  The ``episodes`` table holds **work episodes** and P6
never reads it.  The behaviour pilot's ``variant`` selects a **memory-format variant** and is
never an arm — which is why P6's allocation gets its own table, its own column name and its own
enum rather than extending ``behavior_projection_pilot_assignments.variant``.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .pilot_thresholds import (
    CLAIM_A_MAX_P_VALUE,
    CLAIM_A_MIN_LOWER_BOUND,
    CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM,
    MAX_DEVIATION_RATE,
    MIN_ACTIVE_DAYS,
    MIN_CLOSE_COVERAGE,
    MIN_CLOSED_PER_ARM,
    MIN_MATCHED_BLOCKS,
    P6_THRESHOLDS_EFFECTIVE_AT,
    P6_THRESHOLDS_SHA,
    P6_THRESHOLDS_VERSION,
)
from .policy_evaluation import episode_key as policy_episode_key
from .policy_evaluation import paired_comparison

__all__ = [
    "ARM_CLASS",
    "ARM_SET_SHA",
    "DESCRIPTIVE_ONLY_SENTENCE",
    "EXCLUSION_KEYS",
    "NOT_COMPUTABLE_REASONS",
    "NO_ENROLLED_EPISODES_SENTENCE",
    "PILOT_FAMILIES",
    "POOLING_REFUSED_SENTENCE",
    "PRODUCER_CLASSES",
    "RANDOMIZED_ARMS",
    "REDUCED_SUPERVISION_SENTENCE",
    "SINGLE_PARTICIPANT_SENTENCE",
    "SUCCESS_DEFINITION",
    "AllocationKind",
    "ArmAllocation",
    "ClaimAVerdict",
    "ClaimBState",
    "ClaimBVerdict",
    "Clause",
    "ClauseState",
    "CloseFacts",
    "CompletionBasis",
    "CostSource",
    "EnrolmentOutcome",
    "EnrolmentRequest",
    "EpisodeFacts",
    "PilotArm",
    "RelevanceVerdict",
    "RescueLevel",
    "ReviewVerdict",
    "allocate_arm",
    "allocation_salt_sha256",
    "arm_class_of",
    "block_permutation",
    "claim_a",
    "claim_b",
    "clause_not_computable",
    "clause_pass",
    "clause_shortfall",
    "compute_episode_key",
    "cost_clause",
    "episode_success",
    "false_positive_relevance",
    "human_intervention_clause",
    "resolve_enrolment",
    "stratum_id_for",
    "validate_decision_family",
    "window_clock",
    "worst_cell",
]


# --------------------------------------------------------------------------------------
# 1. Vocabulary
# --------------------------------------------------------------------------------------


class PilotArm(StrEnum):
    """What the executing party is handed.  A *runtime/workflow* allocation, never a format."""

    EXISTING_RUNTIME = "existing_runtime"  # current agent runtime, no TCE context, no TCE handoff
    MARKDOWN_HANDOFF = "markdown_handoff"  # OWNER-AUTHORED structured-Markdown handoff, no TCE runtime
    TCE_ASSISTED = "tce_assisted"  # full TCE
    OWNER_UNASSISTED = "owner_unassisted"  # no agent; the owner does the work


ARM_CLASS: dict[PilotArm, str] = {
    PilotArm.EXISTING_RUNTIME: "runtime",
    PilotArm.MARKDOWN_HANDOFF: "runtime",
    PilotArm.TCE_ASSISTED: "runtime",
    PilotArm.OWNER_UNASSISTED: "human_workflow",
}

ARM_CLASS_RUNTIME = "runtime"
ARM_CLASS_HUMAN_WORKFLOW = "human_workflow"

#: The only arms an allocator may draw.  ``owner_unassisted`` is reachable by election only.
RANDOMIZED_ARMS: tuple[PilotArm, ...] = (
    PilotArm.EXISTING_RUNTIME,
    PilotArm.MARKDOWN_HANDOFF,
    PilotArm.TCE_ASSISTED,
)

#: Bound key on every stratum row and copied onto every episode.  Adding or removing an arm
#: changes it, so a report can refuse to pool across two different experiments.
ARM_SET_SHA: str = hashlib.sha256("|".join(arm.value for arm in RANDOMIZED_ARMS).encode("utf-8")).hexdigest()[:32]

#: The comparison baselines Claim A is drawn against.  ``tce_assisted`` is the treatment.
CLAIM_A_TREATMENT = PilotArm.TCE_ASSISTED
CLAIM_A_BASELINES: tuple[PilotArm, ...] = (PilotArm.EXISTING_RUNTIME, PilotArm.MARKDOWN_HANDOFF)


class AllocationKind(StrEnum):
    RANDOMIZED = "randomized"  # permuted-block; the only kind Claim A and Claim B admit
    ELECTED = "elected"  # the owner chose the arm; descriptive only, never causal


class RescueLevel(StrEnum):
    """Four-valued rather than boolean: *approved a diff* and *took over and finished it myself*
    are different events, and the plan asks for the second."""

    NONE = "none"
    STEERED = "steered"  # owner redirected mid-episode, agent finished
    TOOK_OVER = "took_over"  # owner finished the work himself
    ABANDONED_TO_OWNER = "abandoned_to_owner"  # agent stopped; owner re-did it from scratch


class CompletionBasis(StrEnum):
    VERIFIED = "verified"  # P3 verification_results.verdict == pass for the bound task
    TASK_STATE_DONE = "task_state_done"  # P2 task_states.status == done, no verification
    OWNER_ATTESTED = "owner_attested"  # neither of the above; the human says it finished
    UNFINISHED = "unfinished"


class ReviewVerdict(StrEnum):
    ACCEPTED_AS_IS = "accepted_as_is"
    ACCEPTED_WITH_EDITS = "accepted_with_edits"
    REJECTED = "rejected"


class CostSource(StrEnum):
    """Mirrors ``tce_shared.budget.COST_SOURCES`` exactly.  There is no fourth vocabulary."""

    PROVIDER_REPORTED = "provider_reported"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class RelevanceVerdict(StrEnum):
    """A judgement about a dream proposal's fit, independent of whether it was accepted."""

    RELEVANT = "relevant"
    NOT_RELEVANT = "not_relevant"
    CANNOT_JUDGE = "cannot_judge"


class DeliveryUsefulness(StrEnum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    TOO_EARLY = "too_early"  # answered inside the lookback; stored, reported, never counted


class ClauseState(StrEnum):
    PASS = "pass"
    SHORTFALL = "shortfall"
    NOT_COMPUTABLE = "not_computable"


class ClaimBState(StrEnum):
    """Claim B's only two states.  There is deliberately no ``SUPPORTED``."""

    NOT_COMPUTABLE = "not_computable"
    DESCRIPTIVE_ONLY = "descriptive_only"


#: P1/P4's five decision families, plus one honest bucket.  P6 adds NO new family.
PILOT_FAMILIES: tuple[str, ...] = (
    "safety_confirmation",
    "needs_human",
    "operator_action",
    "next_objective",
    "policy_abstention",
    "unclassified",
)

#: Which party may write a field, and whether a gate clause may read it.  The rule is not
#: "no self-report" — it is "the party under test does not grade itself".
PRODUCER_CLASSES: dict[str, bool] = {
    "server_derived": True,
    "human_attested": True,
    "agent_asserted": False,  # diagnostics only; no clause function may read these
}

REDUCED_SUPERVISION_SENTENCE = (
    "No adequate human baseline was collected, so this result is described as REDUCED SUPERVISION "
    "and not as human-level capability."
)
SINGLE_PARTICIPANT_SENTENCE = (
    "One owner participated. Every statement here applies to that owner and to the projects "
    "observed, and generalises to nobody else."
)
DESCRIPTIVE_ONLY_SENTENCE = (
    "Randomized human-workflow episodes exist, so this block is descriptive. It is still not a "
    "supported claim: this report has no state in which Claim B is supported."
)
POOLING_REFUSED_SENTENCE = (
    "POOLING REFUSED: this report never averages across projects or decision families. A weak "
    "segment is visible above or it is not measured."
)
NO_ENROLLED_EPISODES_SENTENCE = "NO ENROLLED EPISODES. The pilot has not started. Nothing below is a measurement."

SUCCESS_DEFINITION = (
    "success = completion_basis in {verified, task_state_done} and rescue_level == none and "
    "review_verdict != rejected  (two human_attested components, one server_derived, none agent_asserted)"
)

#: NOT_COMPUTABLE always carries a reason from this closed list, never a bare ``None``.
NOT_COMPUTABLE_REASONS: frozenset[str] = frozenset(
    {
        "no_enrolled_episodes",
        "no_closed_episodes",
        "no_eligible_episodes",
        "no_complete_blocks",
        "no_discordant_pairs",
        "deviation_rate_exceeded",
        "arm_not_runnable_on_host",
        "human_arm_not_randomized",
        "salt_changed_mid_pilot",
        "all_surfaces_unsupported",
        "no_dispatch_rows",
        "no_adjudications",
        "no_completed_proposals",
        "dream_tables_absent",
        "pilot_tables_absent",
    }
)

#: The exclusion ledger printed beside every verdict.  ``closed=0`` with no explanation is
#: indistinguishable from a hidden shutdown, which is why the ledger is not conditional.
#: ``deviated`` is a census entry, NOT a removal: the as-assigned (intention-to-treat) analysis
#: counts a deviated episode under its assigned arm, and the deviation RATE is its own clause.
EXCLUSION_KEYS: tuple[str, ...] = (
    "unclosed",
    "deviated",
    "blocks_incomplete",
    "pre_registration",
    "adjudicator_not_independent",
    "elected",
    "salt_mismatch",
)


def arm_class_of(arm: PilotArm) -> str:
    return ARM_CLASS[arm]


def validate_decision_family(value: str) -> str:
    """P6 adds no family.  An unknown one is a caller error, not a new bucket."""

    family = (value or "").strip()
    if family not in PILOT_FAMILIES:
        raise ValueError(f"decision_family must be one of {PILOT_FAMILIES!r}, got {value!r}")
    return family


# --------------------------------------------------------------------------------------
# 2. The allocator — permuted blocks within stratum
# --------------------------------------------------------------------------------------


def stratum_id_for(
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    decision_family: str,
) -> str:
    """The stratum a block is drawn inside.  Two decision families never share a block."""

    raw = f"{workspace_id}|{subject_user_id}|{project_id or ''}|{decision_family}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def allocation_salt_sha256(salt: str) -> str:
    """Copied from the stratum row onto every episode, so a mid-pilot salt change is visible."""

    return hashlib.sha256(salt.encode("utf-8")).hexdigest()[:32]


def compute_episode_key(
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    session_id: str,
    objective_hash: str | None,
    cancel_epoch: int,
) -> str:
    """P4's ``episode_key``, imported unchanged.  P6 defines no second episode.

    This is computed server-side at enrolment and is **never** accepted from a caller: to get a
    different key you must change the objective, start a new session, or name a different
    project — each of which is a different piece of work, and the last of which also moves the
    episode into the cell it named rather than into the one it left.  Nothing else the caller can
    send reaches this function; in particular ``body.task_id`` must never reach ``cancel_epoch``,
    which is why the two stores look that epoch up by the session.

    This does not make redrawing impossible, and it should not be described as if it did.
    ``session_id`` is a caller-supplied component of the key and is absent from ``stratum_id``, so
    a new session id draws again inside the same ``(project, decision_family)`` cell.  That is
    deliberate -- an allocator cannot distinguish an honest new task from a discarded one -- and the
    containment is visibility rather than prevention: an abandoned draw leaves an enrolled,
    never-closed episode, and the per-cell ``close_coverage`` clause surfaces repeated shopping as a
    SHORTFALL in the cell where it happened.
    """

    return policy_episode_key(
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        session_id=session_id,
        objective_hash=objective_hash,
        cancel_epoch=cancel_epoch,
    )


def _fisher_yates(items: Sequence[PilotArm], seed_int: int) -> tuple[PilotArm, ...]:
    """A deterministic shuffle that does not depend on any stdlib RNG's internals.

    ``random.Random`` would work today, but its stream is an implementation detail of CPython
    and this permutation has to recompute from ``(salt, stratum, block)`` years later for an
    auditor.  A named 64-bit LCG is the whole dependency.
    """

    pool = list(items)
    state = seed_int & 0xFFFFFFFFFFFFFFFF
    for index in range(len(pool) - 1, 0, -1):
        state = (state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        pick = (state >> 16) % (index + 1)
        pool[index], pool[pick] = pool[pick], pool[index]
    return tuple(pool)


def block_permutation(*, stratum_id: str, block_ordinal: int, salt: str) -> tuple[PilotArm, ...]:
    """The order this block's arms are handed out in.  Every arm appears exactly once."""

    digest = hmac.new(
        salt.encode("utf-8"),
        f"{stratum_id}|{block_ordinal}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return _fisher_yates(RANDOMIZED_ARMS, int(digest[:8], 16))


@dataclass(frozen=True, slots=True)
class ArmAllocation:
    """The frozen allocation.  Written before the response carrying the arm is built."""

    arm_id: PilotArm
    arm_class: str
    allocation_kind: AllocationKind
    stratum_id: str
    slot: int  # -1 for an election: an elected arm never consumes a randomised slot
    block_ordinal: int  # -1 for an election
    block_position: int  # -1 for an election
    arm_set_sha: str
    allocation_salt_sha256: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id.value,
            "arm_class": self.arm_class,
            "allocation_kind": self.allocation_kind.value,
            "stratum_id": self.stratum_id,
            "slot": self.slot,
            "block_ordinal": self.block_ordinal,
            "block_position": self.block_position,
            "arm_set_sha": self.arm_set_sha,
            "allocation_salt_sha256": self.allocation_salt_sha256,
        }


def allocate_arm(*, stratum_id: str, slot: int, salt: str) -> ArmAllocation:
    """Slot ``n`` of a stratum to an arm, by permuted block.

    ``slot`` comes from the caller's single atomic ``INSERT ... ON CONFLICT DO UPDATE SET
    next_slot = next_slot + 1 RETURNING next_slot - 1``.  Because the counter is exact, every
    complete block contains each arm exactly once and a cell's arms differ by at most one at any
    moment — and a block is therefore also a *matched set*, which is where the paired test in
    ``claim_a`` gets its pairs from, for free.
    """

    if slot < 0:
        raise ValueError("slot must be >= 0; an elected arm does not consume a slot")
    width = len(RANDOMIZED_ARMS)
    block_ordinal = slot // width
    block_position = slot % width
    arm = block_permutation(stratum_id=stratum_id, block_ordinal=block_ordinal, salt=salt)[block_position]
    return ArmAllocation(
        arm_id=arm,
        arm_class=ARM_CLASS[arm],
        allocation_kind=AllocationKind.RANDOMIZED,
        stratum_id=stratum_id,
        slot=slot,
        block_ordinal=block_ordinal,
        block_position=block_position,
        arm_set_sha=ARM_SET_SHA,
        allocation_salt_sha256=allocation_salt_sha256(salt),
    )


def elect_arm(*, stratum_id: str, arm: PilotArm, salt: str) -> ArmAllocation:
    """Record an arm the owner chose.  Descriptive only, and it perturbs no randomised block.

    An allocator cannot randomise a human into doing the work himself; pretending it can would be
    a fake arm.  So the election branch exists, it is marked ``elected``, it does **not** consume
    a stratum slot, and every Claim A denominator drops it.
    """

    if arm in RANDOMIZED_ARMS:
        raise ValueError(f"{arm.value} is randomised; it must not be elected")
    return ArmAllocation(
        arm_id=arm,
        arm_class=ARM_CLASS[arm],
        allocation_kind=AllocationKind.ELECTED,
        stratum_id=stratum_id,
        slot=-1,
        block_ordinal=-1,
        block_position=-1,
        arm_set_sha=ARM_SET_SHA,
        allocation_salt_sha256=allocation_salt_sha256(salt),
    )


# --------------------------------------------------------------------------------------
# 3. Enrolment resolution — reuse, elect or draw
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnrolmentRequest:
    """Everything the enrolment route has after authentication.  Note what is absent: there is
    no ``episode_key`` and no ``arm_id``.  Both are server-derived, which is the whole point."""

    workspace_id: str
    subject_user_id: str
    project_id: str | None
    decision_family: str
    session_id: str
    objective_hash: str | None
    cancel_epoch: int
    elect_arm: PilotArm | None = None


@dataclass(frozen=True, slots=True)
class EnrolmentOutcome:
    episode_key: str
    stratum_id: str
    allocation: ArmAllocation
    reused: bool
    slot_consumed: bool

    def to_payload(self) -> dict[str, Any]:
        payload = self.allocation.to_payload()
        payload["episode_key"] = self.episode_key
        payload["reused"] = self.reused
        return payload


def resolve_enrolment(
    request: EnrolmentRequest,
    *,
    existing: ArmAllocation | None,
    next_slot: int | None,
    salt: str,
    human_baseline_enabled: bool = False,
) -> EnrolmentOutcome:
    """Decide this enrolment, given whatever the store already had for this episode key.

    Three branches and no fourth:

    * ``existing`` is not None — the same work is being enrolled again, so the **original** arm
      comes back with ``reused=True`` and no slot is consumed.  A repeat enrolment is not a fresh
      draw, which is what makes the uniqueness key ``(workspace, subject, episode_key)``
      un-shoppable.
    * ``request.elect_arm`` — the election branch (§1.3), gated on the setting.
    * otherwise — the allocator, which needs the atomic slot the caller just took.
    """

    key = compute_episode_key(
        workspace_id=request.workspace_id,
        subject_user_id=request.subject_user_id,
        project_id=request.project_id,
        session_id=request.session_id,
        objective_hash=request.objective_hash,
        cancel_epoch=request.cancel_epoch,
    )
    stratum = stratum_id_for(
        workspace_id=request.workspace_id,
        subject_user_id=request.subject_user_id,
        project_id=request.project_id,
        decision_family=validate_decision_family(request.decision_family),
    )
    if existing is not None:
        return EnrolmentOutcome(episode_key=key, stratum_id=existing.stratum_id, allocation=existing, reused=True, slot_consumed=False)
    if request.elect_arm is not None:
        if not human_baseline_enabled:
            raise ValueError("pilot_human_baseline_enabled is off; an arm may not be elected")
        return EnrolmentOutcome(
            episode_key=key,
            stratum_id=stratum,
            allocation=elect_arm(stratum_id=stratum, arm=request.elect_arm, salt=salt),
            reused=False,
            slot_consumed=False,
        )
    if next_slot is None:
        raise ValueError("a randomised enrolment requires the stratum slot from the atomic counter")
    return EnrolmentOutcome(
        episode_key=key,
        stratum_id=stratum,
        allocation=allocate_arm(stratum_id=stratum, slot=next_slot, salt=salt),
        reused=False,
        slot_consumed=True,
    )


# --------------------------------------------------------------------------------------
# 4. Clauses
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Clause:
    """One clause: ``PASS``, ``SHORTFALL(measured, floor)`` or ``NOT_COMPUTABLE(reason)``.

    There is no fourth state and there is no partial credit.  In particular there is no state in
    which a clause with a zero denominator renders as a measurement: "no bad event was observed"
    at n=0 is ``NOT_COMPUTABLE``, never ``PASS`` and never ``0.0``.
    """

    name: str
    state: ClauseState
    measured: float | int | None = None
    floor: float | int | None = None
    comparison: str = "<"
    reason: str = ""
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.state is not ClauseState.PASS

    def shortfall_text(self) -> str:
        if self.state is ClauseState.SHORTFALL:
            return f"{self.name} {self.measured} {self.comparison} {self.floor}"
        if self.state is ClauseState.NOT_COMPUTABLE:
            return f"{self.name} not_computable ({self.reason})"
        return ""

    def render(self) -> str:
        if self.state is ClauseState.PASS:
            return f"{self.name}: PASS"
        if self.state is ClauseState.SHORTFALL:
            return f"{self.name}: SHORTFALL  {self.name} {self.measured} {self.comparison} {self.floor}"
        detail = f": {self.detail}" if self.detail else ""
        return f"{self.name}: NOT_COMPUTABLE  ({self.reason}{detail})"

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "measured": self.measured,
            "floor": self.floor,
            "comparison": self.comparison,
            "reason": self.reason,
            "detail": self.detail,
            "rendered": self.render(),
        }


def clause_pass(name: str) -> Clause:
    return Clause(name=name, state=ClauseState.PASS)


def clause_shortfall(name: str, measured: float | int, floor: float | int, *, comparison: str = "<") -> Clause:
    """SHORTFALL always carries the measured value AND the floor, in that order."""

    return Clause(name=name, state=ClauseState.SHORTFALL, measured=measured, floor=floor, comparison=comparison)


def clause_not_computable(name: str, reason: str, *, detail: str = "") -> Clause:
    if reason not in NOT_COMPUTABLE_REASONS:
        raise ValueError(f"{reason!r} is not one of the closed NOT_COMPUTABLE reasons")
    return Clause(name=name, state=ClauseState.NOT_COMPUTABLE, reason=reason, detail=detail)


def _clause_from_floor(name: str, measured: float | int, floor: float | int) -> Clause:
    return clause_pass(name) if measured >= floor else clause_shortfall(name, measured, floor)


def cost_clause(*, cost_known: int, episodes: int, detail: str = "") -> Clause:
    """A zero that means *we do not know* must never render as a zero that means *free*.

    On this host every runnable surface reports ``unsupported`` (``budget.py`` sets ``codex/*``
    to ``spend_enforcement="unsupported"``), so the honest reading is NOT_COMPUTABLE and a cost
    total is simply not printed.
    """

    if episodes <= 0:
        return clause_not_computable("cost", "no_dispatch_rows", detail=detail)
    if cost_known <= 0:
        return clause_not_computable("cost", "all_surfaces_unsupported", detail=detail)
    return clause_pass("cost")


def human_intervention_clause(*, known: int, episodes: int, detail: str = "") -> Clause:
    """``human_intervention_count``'s first aggregator in this repository.

    It counts approval callbacks inside one supervised run.  It is **not** rescue and is never
    renamed to it; on ``codex/app-server`` with approvals set to ``never`` it is structurally
    zero, which is a zero that means *not measured*.
    """

    if episodes <= 0 or known <= 0:
        return clause_not_computable("human_intervention", "no_dispatch_rows", detail=detail)
    return clause_pass("human_intervention")


# --------------------------------------------------------------------------------------
# 5. The facts a claim is computed from
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CloseFacts:
    """The adjudication record.  Every field here is ``server_derived`` or ``human_attested``."""

    adjudication_independent: bool  # server: adjudicator_id != episode.agent_principal
    executed_arm: PilotArm  # human
    deviated: bool  # server: executed_arm != assigned arm
    rescue_level: RescueLevel  # human
    completion_basis: CompletionBasis  # server first, human only as fallback
    review_verdict: ReviewVerdict  # human
    closed_at: datetime  # server
    review_minutes: int | None = None  # human
    late_close: bool = False  # server; diagnostics, never a refusal


@dataclass(frozen=True, slots=True)
class EpisodeFacts:
    """One enrolled episode, with its close record if a verified human wrote one.

    Deliberately absent: anything from ``pilot_episode_observations``.  Those fields are
    ``agent_asserted`` — the executor's account of its own run — and no clause function in this
    module can reach them, because the type they would arrive in does not exist here.
    """

    episode_id: str
    project_id: str | None
    decision_family: str
    arm_id: PilotArm
    arm_class: str
    allocation_kind: AllocationKind
    stratum_id: str
    slot: int
    block_ordinal: int
    allocation_salt_sha256: str
    arm_set_sha: str
    allocated_at: datetime
    close: CloseFacts | None = None


def episode_success(close: CloseFacts) -> bool:
    """The Claim A composite, recomputed at read time and never stored.

    Storing it would let a later change to the definition silently disagree with history.
    Recomputing means the definition is auditable and versioned with ``P6_THRESHOLDS_SHA``.
    """

    return (
        close.completion_basis in {CompletionBasis.VERIFIED, CompletionBasis.TASK_STATE_DONE}
        and close.rescue_level is RescueLevel.NONE
        and close.review_verdict is not ReviewVerdict.REJECTED
    )


def window_clock(episodes: Iterable[EpisodeFacts], *, now: datetime | None = None) -> tuple[int, int]:
    """``(active_days, calendar_days)``.  Only the first is ever a clause.

    ``active_days`` counts distinct UTC dates on which at least one episode was **enrolled**.
    ``calendar_days`` is printed beside it as a non-qualifying fact, precisely so that "six weeks
    elapsed" is never mistaken for "six weeks of evidence".
    """

    stamps = [episode.allocated_at.astimezone(UTC) for episode in episodes]
    if not stamps:
        return 0, 0
    active = len({stamp.date() for stamp in stamps})
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    calendar = max(0, int((current - min(stamps)).total_seconds() // 86400))
    return active, calendar


# --------------------------------------------------------------------------------------
# 6. Claim A
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimAVerdict:
    project_id: str | None
    decision_family: str
    supported: bool
    clauses: tuple[Clause, ...]
    shortfalls: tuple[str, ...]
    exclusions: dict[str, int]
    enrolled: int
    closed: int
    closed_by_arm: dict[str, int]
    success_by_arm: dict[str, int]
    as_treated_by_arm: dict[str, int]
    complete_blocks: int
    active_days: int
    calendar_days: int
    comparisons: dict[str, dict[str, Any]]
    thresholds_version: str = P6_THRESHOLDS_VERSION
    thresholds_sha: str = P6_THRESHOLDS_SHA

    @property
    def failed_clause_count(self) -> int:
        return sum(1 for clause in self.clauses if clause.failed)

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim": "A",
            "project_id": self.project_id,
            "decision_family": self.decision_family,
            "supported": self.supported,
            "clauses": [clause.to_payload() for clause in self.clauses],
            "shortfalls": list(self.shortfalls),
            "exclusions": dict(self.exclusions),
            "enrolled": self.enrolled,
            "closed": self.closed,
            "closed_by_arm": dict(self.closed_by_arm),
            "success_by_arm": dict(self.success_by_arm),
            "as_treated_by_arm": dict(self.as_treated_by_arm),
            "complete_blocks": self.complete_blocks,
            "active_days": self.active_days,
            "calendar_days": self.calendar_days,
            "comparisons": dict(self.comparisons),
            "success_definition": SUCCESS_DEFINITION,
            "thresholds_version": self.thresholds_version,
            "thresholds_sha": self.thresholds_sha,
        }


def _require_single_cell(episodes: Sequence[EpisodeFacts]) -> tuple[str | None, str]:
    cells = {(episode.project_id, episode.decision_family) for episode in episodes}
    if len(cells) > 1:
        raise ValueError(f"a claim is computed for ONE (project, decision_family) cell; got {sorted(map(str, cells))}")
    return next(iter(cells))


def claim_a(
    episodes: Sequence[EpisodeFacts],
    *,
    project_id: str | None = None,
    decision_family: str = "",
    now: datetime | None = None,
    arm_runnable: Mapping[str, bool] | None = None,
) -> ClaimAVerdict:
    """Incremental benefit over existing runtimes, for ONE ``(project, decision_family)`` cell.

    Reads only ``arm_class = 'runtime'`` rows.  There is no query here that returns both claims'
    rows, which is the first of the three structural devices keeping Claim B from being reported
    by accident.
    """

    if episodes:
        cell_project, cell_family = _require_single_cell(episodes)
        project_id = cell_project
        decision_family = cell_family

    exclusions: dict[str, int] = dict.fromkeys(EXCLUSION_KEYS, 0)
    enrolled_runtime: list[EpisodeFacts] = []
    salts: dict[str, int] = {}

    for episode in episodes:
        if episode.allocation_kind is AllocationKind.ELECTED or episode.arm_class != ARM_CLASS_RUNTIME:
            exclusions["elected"] += 1
            continue
        if episode.allocated_at.astimezone(UTC) < P6_THRESHOLDS_EFFECTIVE_AT:
            exclusions["pre_registration"] += 1
            continue
        salts[episode.allocation_salt_sha256] = salts.get(episode.allocation_salt_sha256, 0) + 1
        enrolled_runtime.append(episode)

    # A salt change mid-pilot splits the experiment; the report refuses to pool across the two.
    dominant_salt = max(salts, key=lambda key: (salts[key], key)) if salts else ""
    in_salt: list[EpisodeFacts] = []
    for episode in enrolled_runtime:
        if episode.allocation_salt_sha256 != dominant_salt:
            exclusions["salt_mismatch"] += 1
            continue
        in_salt.append(episode)

    eligible: list[EpisodeFacts] = []
    for episode in in_salt:
        if episode.close is None:
            exclusions["unclosed"] += 1
            continue
        if not episode.close.adjudication_independent:
            exclusions["adjudicator_not_independent"] += 1
            continue
        if episode.close.deviated:
            exclusions["deviated"] += 1  # a census entry, not a removal — see EXCLUSION_KEYS
        eligible.append(episode)

    closed_by_arm: dict[str, int] = {arm.value: 0 for arm in RANDOMIZED_ARMS}
    success_by_arm: dict[str, int] = {arm.value: 0 for arm in RANDOMIZED_ARMS}
    as_treated_by_arm: dict[str, int] = {arm.value: 0 for arm in RANDOMIZED_ARMS}
    for episode in eligible:
        close = episode.close
        assert close is not None  # noqa: S101 - narrowed by the loop above
        closed_by_arm[episode.arm_id.value] += 1
        if episode_success(close):
            success_by_arm[episode.arm_id.value] += 1
        if close.executed_arm.value in as_treated_by_arm:
            as_treated_by_arm[close.executed_arm.value] += 1

    # A block is a matched set.  Only blocks in which every randomised arm closed can be paired.
    by_block: dict[int, dict[str, EpisodeFacts]] = {}
    for episode in eligible:
        by_block.setdefault(episode.block_ordinal, {})[episode.arm_id.value] = episode
    complete_blocks = sorted(
        ordinal for ordinal, members in by_block.items() if all(arm.value in members for arm in RANDOMIZED_ARMS)
    )
    incomplete = sum(len(members) for ordinal, members in by_block.items() if ordinal not in set(complete_blocks))
    exclusions["blocks_incomplete"] += incomplete

    enrolled = len(in_salt)
    closed = len(eligible)
    deviations = sum(1 for episode in eligible if episode.close is not None and episode.close.deviated)
    deviation_rate = round(deviations / closed, 6) if closed else 0.0
    active_days, calendar_days = window_clock(episodes, now=now)

    clauses: list[Clause] = [_clause_from_floor("active_days", active_days, MIN_ACTIVE_DAYS)]

    if enrolled == 0:
        clauses.append(clause_not_computable("close_coverage", "no_enrolled_episodes"))
        clauses.append(clause_not_computable("salt_stable", "no_enrolled_episodes"))
    else:
        coverage = round(closed / enrolled, 6)
        clauses.append(_clause_from_floor("close_coverage", coverage, MIN_CLOSE_COVERAGE))
        distinct_salts = len(salts)
        clauses.append(
            clause_pass("salt_stable")
            if distinct_salts == 1
            else clause_shortfall("salt_changed_mid_pilot", distinct_salts, 1, comparison=">")
        )

    weakest_arm = min(closed_by_arm.values()) if closed_by_arm else 0
    clauses.append(_clause_from_floor("closed_per_arm", weakest_arm, MIN_CLOSED_PER_ARM))

    runnable = dict(arm_runnable or {})
    unrunnable = [arm.value for arm in RANDOMIZED_ARMS if closed_by_arm[arm.value] == 0]
    if unrunnable:
        detail = ", ".join(f"{arm}: closed=0, host_runnable={runnable.get(arm, 'unknown')}" for arm in unrunnable)
        clauses.append(clause_not_computable("arm_runnable", "arm_not_runnable_on_host", detail=detail))
    else:
        clauses.append(clause_pass("arm_runnable"))

    if closed == 0:
        clauses.append(clause_not_computable("deviation_rate", "no_closed_episodes"))
        deviation_ok = False
    else:
        deviation_ok = deviation_rate <= MAX_DEVIATION_RATE
        clauses.append(
            clause_pass("deviation_rate")
            if deviation_ok
            else clause_shortfall("deviation_rate", deviation_rate, MAX_DEVIATION_RATE, comparison=">")
        )

    clauses.append(_clause_from_floor("matched_blocks", len(complete_blocks), MIN_MATCHED_BLOCKS))

    comparisons: dict[str, dict[str, Any]] = {}
    for baseline in CLAIM_A_BASELINES:
        name = f"paired_lift_vs_{baseline.value}"
        if closed and not deviation_ok:
            clauses.append(clause_not_computable(name, "deviation_rate_exceeded"))
            continue
        if not complete_blocks:
            clauses.append(clause_not_computable(name, "no_complete_blocks"))
            continue
        treatment_correct: list[bool] = []
        baseline_correct: list[bool] = []
        for ordinal in complete_blocks:
            members = by_block[ordinal]
            treatment_close = members[CLAIM_A_TREATMENT.value].close
            baseline_close = members[baseline.value].close
            assert treatment_close is not None and baseline_close is not None  # noqa: S101
            treatment_correct.append(episode_success(treatment_close))
            baseline_correct.append(episode_success(baseline_close))
        comparison = paired_comparison(treatment_correct, baseline_correct)
        comparisons[name] = comparison
        if int(comparison["discordant"]) == 0:
            clauses.append(clause_not_computable(name, "no_discordant_pairs"))
            continue
        lower = float(comparison["paired_lift_lower_bound"])
        p_value = float(comparison["p_value"])
        if lower <= CLAIM_A_MIN_LOWER_BOUND:
            clauses.append(clause_shortfall(name, lower, CLAIM_A_MIN_LOWER_BOUND, comparison="<="))
        elif p_value > CLAIM_A_MAX_P_VALUE:
            clauses.append(clause_shortfall(f"{name}_p_value", p_value, CLAIM_A_MAX_P_VALUE, comparison=">"))
        else:
            clauses.append(clause_pass(name))

    shortfalls = tuple(clause.shortfall_text() for clause in clauses if clause.failed)
    return ClaimAVerdict(
        project_id=project_id,
        decision_family=decision_family,
        supported=not shortfalls,
        clauses=tuple(clauses),
        shortfalls=shortfalls,
        exclusions=exclusions,
        enrolled=enrolled,
        closed=closed,
        closed_by_arm=closed_by_arm,
        success_by_arm=success_by_arm,
        as_treated_by_arm=as_treated_by_arm,
        complete_blocks=len(complete_blocks),
        active_days=active_days,
        calendar_days=calendar_days,
        comparisons=comparisons,
    )


def worst_cell(verdicts: Sequence[ClaimAVerdict]) -> ClaimAVerdict | None:
    """The summary names the WORST cell, never a mean.  Ties break on the cell axis, not on luck."""

    if not verdicts:
        return None
    return max(verdicts, key=lambda v: (v.failed_clause_count, str(v.project_id or ""), v.decision_family))


# --------------------------------------------------------------------------------------
# 7. Claim B — structurally unable to be reported as supported
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimBVerdict:
    """Performance relative to a specified human workflow.

    **This type has no ``supported`` field, and it must never acquire one.**  A caller cannot
    render Claim B as a supported claim because there is nowhere to put one, and the REDUCED
    SUPERVISION sentence is emitted here rather than by a renderer, so it travels with the
    verdict into the JSON and the text output alike.  A convention is not a guarantee; a missing
    field is.
    """

    state: ClaimBState
    clause: Clause
    closed: int
    randomized: int
    elected: int
    sentence: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim": "B",
            "state": self.state.value,
            "clause": self.clause.to_payload(),
            "closed": self.closed,
            "randomized": self.randomized,
            "elected": self.elected,
            "sentence": self.sentence,
        }


def claim_b(episodes: Sequence[EpisodeFacts]) -> ClaimBVerdict:
    """Reads only ``arm_class = 'human_workflow'`` rows, and only closed ones count.

    Because ``owner_unassisted`` is reachable only through the election branch, every such row
    carries ``allocation_kind='elected'`` and ``CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM`` fails by
    construction.  It is not permanently closed: a genuinely randomised human-baseline block
    satisfies the clause and the state becomes DESCRIPTIVE_ONLY — which is still not supported.
    """

    human = [episode for episode in episodes if episode.arm_class == ARM_CLASS_HUMAN_WORKFLOW]
    closed = [episode for episode in human if episode.close is not None]
    randomized = sum(1 for episode in closed if episode.allocation_kind is AllocationKind.RANDOMIZED)
    elected = sum(1 for episode in closed if episode.allocation_kind is AllocationKind.ELECTED)
    if CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM and randomized == 0:
        return ClaimBVerdict(
            state=ClaimBState.NOT_COMPUTABLE,
            clause=clause_not_computable("human_arm_randomized", "human_arm_not_randomized"),
            closed=len(closed),
            randomized=randomized,
            elected=elected,
            sentence=REDUCED_SUPERVISION_SENTENCE,
        )
    return ClaimBVerdict(
        state=ClaimBState.DESCRIPTIVE_ONLY,
        clause=clause_pass("human_arm_randomized"),
        closed=len(closed),
        randomized=randomized,
        elected=elected,
        sentence=DESCRIPTIVE_ONLY_SENTENCE,
    )


# --------------------------------------------------------------------------------------
# 8. Dreams — relevance is adjudicated, never derived from a rejection
# --------------------------------------------------------------------------------------


def false_positive_relevance(*, relevant: int, not_relevant: int, cannot_judge: int = 0) -> tuple[float | None, Clause]:
    """``not_relevant / (relevant + not_relevant)``, and nothing else feeds it.

    A ``rejected`` status is never a false positive: a rejection is a preference, a
    ``not_relevant`` adjudication is a claim about the proposal's fit, and the second must not be
    derived from the first.  ``cannot_judge`` is in neither numerator nor denominator and is
    printed as its own count.
    """

    denominator = relevant + not_relevant
    if denominator <= 0:
        detail = f"{cannot_judge} cannot_judge; acceptance is not adjudication"
        return None, clause_not_computable("false_positive_relevance", "no_adjudications", detail=detail)
    return round(not_relevant / denominator, 6), clause_pass("false_positive_relevance")
