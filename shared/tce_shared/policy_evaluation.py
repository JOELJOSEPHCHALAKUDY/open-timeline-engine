"""The qualification harness: the only thing that may write a ``policy_qualifications`` row.

This is not the diagnostic ``POST /v1/behavior/evaluate`` endpoint.  That one keeps its
chronological row-index split and its caller-supplied parameters, and after P4 nothing reads
its gate block.  This module is the harness with authority, and the differences are
deliberate:

* it **ignores the request body** and reads the frozen constants in ``policy_thresholds``,
  because a caller who can sweep the holdout ratio between 0.05 and 0.50 until a run passes
  is a caller who can qualify anything;
* it splits over **task episodes**, not rows, and moves every episode sharing a leakage
  group into the holdout, because the same objective is re-run across sessions and projects
  and a row-index split puts near-identical repeats on both sides;
* it compares against three baselines with a **paired** test.  The pre-P4 code differenced
  two independent Wilson intervals and called it a lift, which is not a paired comparison of
  anything.  A baseline that cannot be computed makes the comparison NOT COMPUTABLE, which
  makes the family NOT QUALIFIED — never "passed by default".  That is the rule that stops
  an empty rule table from being read as zero opposition.

Both harnesses call the same ``decide()``.  That is the whole point of P4: the route that is
scored is the route that ships.

Nothing here is calibrated.  A calibration map becomes possible when a single
``(project, decision_family)`` reaches 100 adjudicated prospective cases that can be split
into a fit fold disjoint from the gate fold, and only ships if its expected calibration error
beats the raw ``policy_score``.  Today that count is 0 for every family.

Pure and I/O-free: stdlib plus ``decision_policy``, ``behavior_fidelity`` and
``policy_thresholds``.  The caller supplies the rows and the baseline callables.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .behavior_fidelity import _choice_key, _context_group_key, _wilson_interval
from .decision_policy import DecisionRequest, DecisionStatus, decide
from .policy_thresholds import (
    HOLDOUT_RATIO,
    MAX_DUPLICATE_CONTEXT_RATIO,
    MIN_ADJUDICATED,
    MIN_COVERAGE,
    MIN_DISTINCT_EPISODES,
    MIN_PRECISION_LOWER_BOUND,
    PAIRED_LIFT_MAX_P_VALUE,
    PAIRED_LIFT_MIN_LOWER_BOUND,
    THRESHOLDS_SHA,
    THRESHOLDS_VERSION,
)

__all__ = [
    "BASELINE_EXPLICIT_RULE",
    "BASELINE_ORDINARY_RETRIEVAL",
    "BASELINE_TASK_FAMILY_MAJORITY",
    "REQUIRED_BASELINES",
    "FrozenSplit",
    "PolicyCase",
    "assert_qualification_is_earned",
    "episode_key",
    "evaluate_policy",
    "freeze_split",
    "mcnemar_exact_p_value",
    "paired_comparison",
]

BASELINE_EXPLICIT_RULE = "explicit_rule"
BASELINE_TASK_FAMILY_MAJORITY = "task_family_majority"
BASELINE_ORDINARY_RETRIEVAL = "ordinary_retrieval"
REQUIRED_BASELINES: tuple[str, ...] = (
    BASELINE_EXPLICIT_RULE,
    BASELINE_TASK_FAMILY_MAJORITY,
    BASELINE_ORDINARY_RETRIEVAL,
)


def episode_key(
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    session_id: str,
    objective_hash: str | None,
    cancel_epoch: int,
) -> str:
    """The unit a split may not straddle.

    ``task_id`` is not the episode: every producer passes ``task_id = session_id`` and the
    freeze site hard-codes ``None``, so 0 of 12 live opportunities carry one.  ``session_id``
    alone is not a safe boundary either — one live ``objective_hash`` spans eight sessions
    and another spans six sessions and two projects, because the same objective was re-run.
    ``cancel_epoch`` is the task's ``last_cancel_seq``, which distinguishes a re-run after a
    cancel from a continuation of the same work.
    """

    raw = "|".join(
        [
            workspace_id,
            subject_user_id,
            project_id or "",
            session_id,
            objective_hash or "",
            str(cancel_epoch),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PolicyCase:
    """One adjudicated prospective case, already frozen by the caller."""

    case_id: str
    episode_key: str
    leakage_group: str  # objective_hash, or "" when the row has none
    frozen_at: datetime
    decision_family: str
    project_id: str | None
    actual_choice: str
    request: DecisionRequest
    context_row: Mapping[str, Any]

    def context_group_key(self) -> str:
        return _context_group_key(self.context_row)


@dataclass(frozen=True, slots=True)
class FrozenSplit:
    split_sha256: str
    holdout_episode_keys: tuple[str, ...]
    train_episode_keys: tuple[str, ...]
    leakage_groups_moved: int
    cases_moved: int
    duplicate_context_ratio: float
    thresholds_sha: str
    tuning_sha: str
    frozen_at: datetime | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "split_sha256": self.split_sha256,
            "holdout_episode_keys": list(self.holdout_episode_keys),
            "train_episode_keys": list(self.train_episode_keys),
            "leakage_groups_moved": self.leakage_groups_moved,
            "cases_moved": self.cases_moved,
            "duplicate_context_ratio": self.duplicate_context_ratio,
            "thresholds_sha": self.thresholds_sha,
            "tuning_sha": self.tuning_sha,
            "frozen_at": self.frozen_at.isoformat() if self.frozen_at else None,
        }


def _split_digest(holdout: Sequence[str], train: Sequence[str], tuning_sha: str) -> str:
    payload = {
        "holdout": sorted(holdout),
        "train": sorted(train),
        "thresholds_sha": THRESHOLDS_SHA,
        "tuning_sha": tuning_sha,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def freeze_split(cases: Sequence[PolicyCase]) -> FrozenSplit:
    """Split by episode, then repair leakage, then record the assignment.

    Note the missing parameter: there is no ``holdout_ratio``.  It reads ``HOLDOUT_RATIO``
    from the frozen constants, and the resulting assignment is stored so a run whose
    ``split_sha256`` cannot be reproduced is rejected rather than quietly compared against a
    differently-split predecessor.
    """

    tuning_sha = cases[0].request.tuning.tuning_sha() if cases else ""
    if not cases:
        return FrozenSplit(
            split_sha256=_split_digest((), (), tuning_sha),
            holdout_episode_keys=(),
            train_episode_keys=(),
            leakage_groups_moved=0,
            cases_moved=0,
            duplicate_context_ratio=0.0,
            thresholds_sha=THRESHOLDS_SHA,
            tuning_sha=tuning_sha,
            frozen_at=None,
        )

    by_episode: dict[str, list[PolicyCase]] = {}
    for case in cases:
        by_episode.setdefault(case.episode_key, []).append(case)
    ordered = sorted(
        by_episode.items(),
        key=lambda item: (min(entry.frozen_at for entry in item[1]), item[0]),
    )

    total = len(cases)
    wanted = math.ceil(total * HOLDOUT_RATIO)
    holdout_keys: list[str] = []
    held = 0
    for key, entries in reversed(ordered):
        if held >= wanted:
            break
        holdout_keys.append(key)
        held += len(entries)
    holdout_set = set(holdout_keys)

    # Leakage repair.  Two episodes that pursue the same objective are near-duplicates of
    # each other; splitting them across the boundary trains on the answer.
    holdout_groups = {
        case.leakage_group
        for key in holdout_set
        for case in by_episode[key]
        if case.leakage_group
    }
    moved_groups: set[str] = set()
    cases_moved = 0
    for key, entries in ordered:
        if key in holdout_set:
            continue
        groups = {case.leakage_group for case in entries if case.leakage_group}
        shared = groups & holdout_groups
        if shared:
            holdout_set.add(key)
            moved_groups |= shared
            cases_moved += len(entries)

    holdout_final = sorted(holdout_set)
    train_final = sorted(key for key, _ in ordered if key not in holdout_set)

    holdout_cases = [case for key in holdout_final for case in by_episode[key]]
    context_keys = [case.context_group_key() for case in holdout_cases]
    duplicate_ratio = 0.0
    if context_keys:
        duplicate_ratio = round(1.0 - (len(set(context_keys)) / len(context_keys)), 6)

    return FrozenSplit(
        split_sha256=_split_digest(holdout_final, train_final, tuning_sha),
        holdout_episode_keys=tuple(holdout_final),
        train_episode_keys=tuple(train_final),
        leakage_groups_moved=len(moved_groups),
        cases_moved=cases_moved,
        duplicate_context_ratio=duplicate_ratio,
        thresholds_sha=THRESHOLDS_SHA,
        tuning_sha=tuning_sha,
        frozen_at=min(case.frozen_at for case in cases),
    )


def mcnemar_exact_p_value(b: int, c: int) -> float:
    """Two-sided exact binomial p-value on ``b`` successes out of ``b + c`` at ``p = 0.5``.

    ``b`` = policy right where the baseline was wrong, ``c`` = the reverse.  Cases where both
    agree carry no information about which is better and are excluded, which is what makes
    this paired.
    """

    n = b + c
    if n <= 0:
        return 1.0
    smaller = min(b, c)
    tail = sum(math.comb(n, k) for k in range(0, smaller + 1)) / (2.0**n)
    return float(min(1.0, 2.0 * tail))


def paired_comparison(
    policy_correct: Sequence[bool],
    baseline_correct: Sequence[bool],
) -> dict[str, Any]:
    """McNemar's exact test plus a Wilson lower bound on the paired win rate."""

    if len(policy_correct) != len(baseline_correct):
        raise ValueError("paired_comparison requires the same cases on both sides")
    b = sum(1 for policy, base in zip(policy_correct, baseline_correct, strict=True) if policy and not base)
    c = sum(1 for policy, base in zip(policy_correct, baseline_correct, strict=True) if base and not policy)
    discordant = b + c
    lower, upper = _wilson_interval(b, discordant)
    return {
        "available": True,
        "policy_only_correct": b,
        "baseline_only_correct": c,
        "discordant": discordant,
        "paired_win_rate": round(b / discordant, 6) if discordant else 0.0,
        "paired_lift_lower_bound": round(lower - 0.5, 6),
        "paired_win_rate_lower_bound": round(lower, 6),
        "paired_win_rate_upper_bound": round(upper, 6),
        "p_value": round(mcnemar_exact_p_value(b, c), 6),
    }


def _unavailable_comparison(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "policy_only_correct": 0,
        "baseline_only_correct": 0,
        "discordant": 0,
        "paired_win_rate": 0.0,
        "paired_lift_lower_bound": 0.0,
        "paired_win_rate_lower_bound": 0.0,
        "paired_win_rate_upper_bound": 0.0,
        "p_value": 1.0,
    }


def evaluate_policy(
    cases: Sequence[PolicyCase],
    *,
    baselines: Mapping[str, Callable[[PolicyCase], str | None] | None],
    split: FrozenSplit | None = None,
) -> dict[str, Any]:
    """Score one ``(project, decision_family)`` and return a verdict with its shortfalls.

    A failed run is a first-class artifact: the caller writes a ``NOT_QUALIFIED`` record with
    the per-clause shortfalls, never nothing at all.  Absence of a qualification record is
    refusal, so an absent record and a recorded refusal must not be the same thing.
    """

    frozen = split if split is not None else freeze_split(cases)
    holdout_keys = set(frozen.holdout_episode_keys)
    holdout = [case for case in cases if case.episode_key in holdout_keys]

    policy_correct: list[bool] = []
    non_abstained_correct = 0
    non_abstained = 0
    abstain_reasons: dict[str, int] = {}
    overlap_histogram: dict[str, int] = {}
    score_histogram: dict[str, int] = {}
    case_results: list[dict[str, Any]] = []

    for case in holdout:
        result = decide(case.request)
        actual_key = _choice_key(case.actual_choice)
        predicted_key = _choice_key(result.selected_option or "")
        correct = bool(predicted_key) and predicted_key == actual_key
        policy_correct.append(correct)
        if result.status is not DecisionStatus.ABSTAINED:
            non_abstained += 1
            non_abstained_correct += int(correct)
        else:
            reason = result.abstain_reason.value if result.abstain_reason else "unknown"
            abstain_reasons[reason] = abstain_reasons.get(reason, 0) + 1
        overlap_bucket = f"{min(9, int(result.adequacy.top_overlap * 10)) / 10:.1f}"
        overlap_histogram[overlap_bucket] = overlap_histogram.get(overlap_bucket, 0) + 1
        score_bucket = f"{min(9, int(result.policy_score * 10)) / 10:.1f}"
        score_histogram[score_bucket] = score_histogram.get(score_bucket, 0) + 1
        case_results.append(
            {
                "case_id": case.case_id,
                "episode_key": case.episode_key,
                "status": result.status.value,
                "selected_option": result.selected_option,
                "actual_choice": case.actual_choice,
                "correct": correct,
                "abstain_reason": result.abstain_reason.value if result.abstain_reason else None,
                "ood_status": result.ood_status.value,
                "conflict_status": result.conflict_status.value,
                "policy_score": result.policy_score,
                "advisor_citation_overlap": result.advisor_citation_overlap,
                "request_fingerprint": result.request_fingerprint,
            }
        )

    adjudicated = len(holdout)
    coverage = round(non_abstained / adjudicated, 6) if adjudicated else 0.0
    precision_lower, precision_upper = _wilson_interval(non_abstained_correct, non_abstained)
    distinct_episodes = len({case.episode_key for case in holdout})

    comparisons: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_BASELINES:
        callback = baselines.get(name)
        if callback is None:
            comparisons[name] = _unavailable_comparison("baseline_unavailable")
            continue
        baseline_correct: list[bool] = []
        for case in holdout:
            answer = callback(case)
            baseline_correct.append(bool(answer) and _choice_key(answer or "") == _choice_key(case.actual_choice))
        comparisons[name] = paired_comparison(policy_correct, baseline_correct)

    shortfalls: list[str] = []
    if adjudicated < MIN_ADJUDICATED:
        shortfalls.append(f"adjudicated {adjudicated} < {MIN_ADJUDICATED}")
    if precision_lower < MIN_PRECISION_LOWER_BOUND:
        shortfalls.append(f"precision_lower_bound {round(precision_lower, 4)} < {MIN_PRECISION_LOWER_BOUND}")
    if coverage < MIN_COVERAGE:
        shortfalls.append(f"coverage {coverage} < {MIN_COVERAGE}")
    if distinct_episodes < MIN_DISTINCT_EPISODES:
        shortfalls.append(f"distinct_episodes {distinct_episodes} < {MIN_DISTINCT_EPISODES}")
    if frozen.duplicate_context_ratio > MAX_DUPLICATE_CONTEXT_RATIO:
        shortfalls.append(
            f"duplicate_context_ratio {frozen.duplicate_context_ratio} > {MAX_DUPLICATE_CONTEXT_RATIO}"
        )
    for name, comparison in comparisons.items():
        if not comparison["available"]:
            shortfalls.append(f"baseline_{name} not_computable")
            continue
        if comparison["paired_lift_lower_bound"] <= PAIRED_LIFT_MIN_LOWER_BOUND:
            shortfalls.append(f"baseline_{name} paired_lift_lower_bound not > {PAIRED_LIFT_MIN_LOWER_BOUND}")
        if comparison["p_value"] > PAIRED_LIFT_MAX_P_VALUE:
            shortfalls.append(f"baseline_{name} p_value {comparison['p_value']} > {PAIRED_LIFT_MAX_P_VALUE}")
    if split is not None and split.split_sha256 != freeze_split(cases).split_sha256:
        shortfalls.append("split_sha256 does not reproduce from the stored episode assignment")

    return {
        "qualified": not shortfalls,
        "shortfalls": tuple(shortfalls),
        "adjudicated": adjudicated,
        "non_abstained": non_abstained,
        "non_abstained_correct": non_abstained_correct,
        "coverage": coverage,
        "precision_lower_bound": round(precision_lower, 6),
        "precision_upper_bound": round(precision_upper, 6),
        "distinct_episodes": distinct_episodes,
        "duplicate_context_ratio": frozen.duplicate_context_ratio,
        "abstain_reasons": dict(sorted(abstain_reasons.items())),
        "baselines": comparisons,
        "split": frozen.to_payload(),
        "thresholds_sha": THRESHOLDS_SHA,
        "thresholds_version": THRESHOLDS_VERSION,
        "diagnostics": {
            "top_overlap_histogram": dict(sorted(overlap_histogram.items())),
            "policy_score_histogram": dict(sorted(score_histogram.items())),
            "calibration": (
                "Nothing is calibrated. A calibration map becomes possible when a single "
                "(project, decision_family) reaches 100 adjudicated prospective cases that can be "
                "split into a fit fold disjoint from the gate fold, and only ships if its expected "
                "calibration error beats the raw policy_score. Today that count is 0 for every family."
            ),
        },
        "case_results": case_results,
    }


def assert_qualification_is_earned(
    *,
    state: str,
    shortfalls: Sequence[str],
    adjudicated_count: int,
    non_abstained_count: int,
    coverage: float,
    precision_lower_bound: float,
    distinct_episodes: int,
    duplicate_context_ratio: float,
) -> None:
    """Refuse to record a QUALIFIED verdict that its own numbers do not support.

    Without this, the two store writers accept ``state=QUALIFIED`` beside a seven-item
    shortfall list and ``adjudicated_count=0``, and the resulting row grants full exposure:
    ``lookup_qualification`` returns it, ``_exposure`` reports ``EXPOSED`` and
    ``qualification_gate`` flips to ``passed=True``.  Every bound key on that row is honest —
    the thresholds digest, the model, the retriever — so nothing downstream can tell that the
    verdict was asserted rather than measured.  Bound keys answer *"is this measurement still
    about the system in use?"*; they have never answered *"was there a measurement?"*.

    The clauses below are exactly the numeric clauses ``evaluate_policy`` reports as
    shortfalls, read from the same frozen constants.  They are necessary conditions, not a
    re-run of the gate: a caller cannot relax one without editing ``policy_thresholds``, and
    that edit moves ``THRESHOLDS_SHA``, which is a bound key on every record already written.

    Every non-QUALIFIED state — including the NOT_QUALIFIED row a failed run must still leave
    behind — is written unchanged.  Refusal is recorded; only a claim is checked.
    """

    if state != "qualified":
        return
    failures: list[str] = []
    if shortfalls:
        failures.append(f"shortfalls is non-empty: {list(shortfalls)}")
    if int(adjudicated_count) < MIN_ADJUDICATED:
        failures.append(f"adjudicated_count {int(adjudicated_count)} < {MIN_ADJUDICATED}")
    if int(non_abstained_count) > int(adjudicated_count):
        failures.append(
            f"non_abstained_count {int(non_abstained_count)} > adjudicated_count {int(adjudicated_count)}"
        )
    if float(coverage) < MIN_COVERAGE:
        failures.append(f"coverage {float(coverage)} < {MIN_COVERAGE}")
    if float(precision_lower_bound) < MIN_PRECISION_LOWER_BOUND:
        failures.append(
            f"precision_lower_bound {float(precision_lower_bound)} < {MIN_PRECISION_LOWER_BOUND}"
        )
    if int(distinct_episodes) < MIN_DISTINCT_EPISODES:
        failures.append(f"distinct_episodes {int(distinct_episodes)} < {MIN_DISTINCT_EPISODES}")
    if float(duplicate_context_ratio) > MAX_DUPLICATE_CONTEXT_RATIO:
        failures.append(
            f"duplicate_context_ratio {float(duplicate_context_ratio)} > {MAX_DUPLICATE_CONTEXT_RATIO}"
        )
    if failures:
        raise ValueError(
            "refusing to record state=qualified: the record's own numbers do not clear the frozen "
            "promotion clauses (" + "; ".join(failures) + "). Write NOT_QUALIFIED with these "
            "shortfalls instead — a recorded refusal is a first-class result."
        )
