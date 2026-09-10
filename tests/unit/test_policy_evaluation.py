"""The qualification harness: the episode split, the leakage repair, and the paired test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tce_shared.decision_policy import DecisionRequest, PolicyTuning
from tce_shared.policy_evaluation import (
    BASELINE_EXPLICIT_RULE,
    BASELINE_ORDINARY_RETRIEVAL,
    BASELINE_TASK_FAMILY_MAJORITY,
    REQUIRED_BASELINES,
    PolicyCase,
    assert_qualification_is_earned,
    episode_key,
    evaluate_policy,
    freeze_split,
    mcnemar_exact_p_value,
    paired_comparison,
)

_NOW = datetime(2026, 6, 1, tzinfo=UTC)
_OPTIONS = ("pause and verify", "force push to prod")


def _row(index: int, *, selected: str = "pause and verify") -> dict[str, Any]:
    summary = "the stripe webhook is failing, do i force push the fix to prod"
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "ts": (_NOW - timedelta(days=3)).isoformat(),
        "situation_type": "choice_required",
        "situation_summary": summary,
        "objective_text": summary,
        "constraints": {},
        "context_snapshot": {},
        "selected_choice": selected,
        "action_taken": selected,
        "evidence_source": "explicit",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "valid_from": (_NOW - timedelta(days=3)).isoformat(),
        "project_id": "proj-1",
    }


def _request(rows: list[dict[str, Any]]) -> DecisionRequest:
    summary = "the stripe webhook is failing, do i force push the fix to prod"
    return DecisionRequest(
        decision_family="choice_required",
        situation_type="choice_required",
        situation_summary=summary,
        objective_text=summary,
        constraints={},
        context_snapshot={},
        candidate_options=_OPTIONS,
        evidence_rows=tuple(rows),
        decision_at=_NOW,
        workspace_id="ws",
        subject_user_id="subject",
        project_id="proj-1",
        episode_key="episode",
        evidence_revision="rev-1",
        evidence_cutoff_at=None,
        retrieval_version="full-knn-v1",
        model_id="model-a",
        runtime_version="runtime-1",
        tuning=PolicyTuning(),
    )


def _case(
    index: int,
    *,
    episode: str,
    leakage_group: str = "",
    actual: str = "pause and verify",
    rows: list[dict[str, Any]] | None = None,
    summary: str | None = None,
) -> PolicyCase:
    context_row = {
        "situation_type": "choice_required",
        "situation_summary": summary or f"case {index} about the stripe webhook",
        "objective_text": summary or f"case {index} about the stripe webhook",
    }
    return PolicyCase(
        case_id=f"case-{index}",
        episode_key=episode,
        leakage_group=leakage_group,
        frozen_at=_NOW + timedelta(minutes=index),
        decision_family="choice_required",
        project_id="proj-1",
        actual_choice=actual,
        request=_request(rows if rows is not None else [_row(1), _row(2), _row(3)]),
        context_row=context_row,
    )


# --- the episode key -------------------------------------------------------------------


def _key(
    *,
    session_id: str = "sess-1",
    project_id: str | None = "proj-1",
    objective_hash: str | None = "abc",
    cancel_epoch: int = 0,
) -> str:
    return episode_key(
        workspace_id="ws",
        subject_user_id="subject",
        project_id=project_id,
        session_id=session_id,
        objective_hash=objective_hash,
        cancel_epoch=cancel_epoch,
    )


def test_episode_key_separates_a_rerun_after_a_cancel_from_a_continuation() -> None:
    assert _key() == _key()
    assert _key() != _key(cancel_epoch=1)
    assert _key() != _key(session_id="sess-2")
    assert _key() != _key(project_id=None)
    assert _key() != _key(objective_hash="def")


# --- the split -------------------------------------------------------------------------


def test_split_cuts_on_an_episode_boundary_and_never_inside_one() -> None:
    cases = [_case(index, episode=f"ep-{index // 3}") for index in range(30)]
    split = freeze_split(cases)
    assert set(split.holdout_episode_keys) & set(split.train_episode_keys) == set()
    assert len(split.holdout_episode_keys) + len(split.train_episode_keys) == 10
    holdout_cases = [case for case in cases if case.episode_key in set(split.holdout_episode_keys)]
    assert len(holdout_cases) >= 6  # ceil(30 * 0.20)


def test_split_moves_every_episode_sharing_a_leakage_group_into_the_holdout() -> None:
    """The same objective is genuinely re-run across sessions and projects.

    One live ``objective_hash`` spans eight sessions and another spans six sessions and two
    projects, so splitting on sessions alone puts near-identical repeats on both sides and
    trains on the answer.
    """

    cases = [_case(index, episode=f"ep-{index}") for index in range(20)]
    # The first (oldest, therefore training-side) episode shares an objective with the last.
    cases[0] = _case(0, episode="ep-0", leakage_group="objective-x")
    cases[19] = _case(19, episode="ep-19", leakage_group="objective-x")

    split = freeze_split(cases)
    assert "ep-19" in split.holdout_episode_keys
    assert "ep-0" in split.holdout_episode_keys, "the leaking twin stayed in training"
    assert split.leakage_groups_moved == 1
    assert split.cases_moved == 1


def test_split_reproduces_from_its_stored_episode_assignment() -> None:
    cases = [_case(index, episode=f"ep-{index // 2}") for index in range(20)]
    first = freeze_split(cases)
    second = freeze_split(list(reversed(cases)))
    assert first.split_sha256 == second.split_sha256
    assert first.holdout_episode_keys == second.holdout_episode_keys


def test_split_takes_no_holdout_ratio_parameter() -> None:
    import inspect

    parameters = list(inspect.signature(freeze_split).parameters)
    assert parameters == ["cases"], "a caller who can sweep the split can qualify anything"


def test_duplicate_context_ratio_is_reported_rather_than_computed_and_ignored() -> None:
    cases = [_case(index, episode=f"ep-{index}", summary="identical paraphrase of one decision") for index in range(20)]
    split = freeze_split(cases)
    assert split.duplicate_context_ratio > 0.0


# --- the paired test -------------------------------------------------------------------


def test_mcnemar_matches_a_hand_worked_two_by_two() -> None:
    # b=8, c=1: two-sided exact binomial on 1 of 9 at p=0.5
    # 2 * (C(9,0) + C(9,1)) / 2^9 = 2 * 10 / 512 = 0.0390625
    assert mcnemar_exact_p_value(8, 1) == 0.0390625
    assert mcnemar_exact_p_value(5, 5) == 1.0
    assert mcnemar_exact_p_value(0, 0) == 1.0  # no discordant pairs carries no evidence


def test_paired_comparison_only_counts_the_cases_that_discriminate() -> None:
    policy = [True, True, True, False, True]
    baseline = [True, False, False, True, False]
    comparison = paired_comparison(policy, baseline)
    assert comparison["policy_only_correct"] == 3
    assert comparison["baseline_only_correct"] == 1
    assert comparison["discordant"] == 4  # the case both got right is dropped


# --- the verdict -----------------------------------------------------------------------


def _baselines(answer: str | None = "force push to prod") -> dict[str, Any]:
    return {name: (lambda case, value=answer: value) for name in REQUIRED_BASELINES}


def test_an_unavailable_baseline_is_not_computable_and_never_passes_by_default() -> None:
    """Zero live rules is zero information, not zero opposition."""

    cases = [_case(index, episode=f"ep-{index}") for index in range(30)]
    baselines = _baselines()
    baselines[BASELINE_EXPLICIT_RULE] = None
    report = evaluate_policy(cases, baselines=baselines)
    assert report["qualified"] is False
    assert any("explicit_rule not_computable" in shortfall for shortfall in report["shortfalls"])
    assert report["baselines"][BASELINE_EXPLICIT_RULE]["available"] is False


def test_a_thin_corpus_is_not_qualified_and_says_exactly_which_clauses_fell_short() -> None:
    """The honest result on the corpus this was written against.

    A failed run is a first-class artifact.  The caller writes a NOT_QUALIFIED record with
    these shortfalls; absence of a record is refusal, and the two must not look the same.
    """

    cases = [_case(index, episode=f"ep-{index}") for index in range(12)]
    report = evaluate_policy(cases, baselines=_baselines())
    assert report["qualified"] is False
    joined = " | ".join(report["shortfalls"])
    assert "adjudicated" in joined
    assert "distinct_episodes" in joined
    assert report["adjudicated"] < 100
    assert report["thresholds_sha"]


def test_the_report_says_plainly_that_nothing_is_calibrated() -> None:
    report = evaluate_policy([_case(0, episode="ep-0")], baselines=_baselines())
    assert "Nothing is calibrated" in report["diagnostics"]["calibration"]
    assert "top_overlap_histogram" in report["diagnostics"]
    assert "policy_score_histogram" in report["diagnostics"]
    assert "calibrated_score" not in report


def test_every_case_is_decided_by_the_deployed_callable() -> None:
    cases = [_case(index, episode=f"ep-{index}") for index in range(10)]
    report = evaluate_policy(cases, baselines=_baselines())
    assert report["case_results"]
    for entry in report["case_results"]:
        # The fingerprint is what lets a live turn and its replay be compared field by field
        # rather than by comparing a module constant to itself.
        assert entry["request_fingerprint"]
        assert entry["status"] in {"selected", "abstained", "rule_applied"}


def test_abstentions_are_reported_by_reason_rather_than_as_one_number() -> None:
    thin = [_case(index, episode=f"ep-{index}", rows=[_row(1)]) for index in range(10)]
    report = evaluate_policy(thin, baselines=_baselines())
    # Only the holdout is scored; a run that scored its own training set would be measuring
    # nothing.
    assert report["adjudicated"] == 2
    assert report["abstain_reasons"] == {"inadequate_evidence": 2}
    assert report["coverage"] == 0.0


def test_baseline_names_are_the_three_the_gate_requires() -> None:
    assert REQUIRED_BASELINES == (
        BASELINE_EXPLICIT_RULE,
        BASELINE_TASK_FAMILY_MAJORITY,
        BASELINE_ORDINARY_RETRIEVAL,
    )


# --------------------------------------------------------------------------- the write interlock


def _earned(**over: Any) -> dict[str, Any]:
    from tce_shared.policy_thresholds import (
        MIN_ADJUDICATED,
        MIN_COVERAGE,
        MIN_DISTINCT_EPISODES,
        MIN_PRECISION_LOWER_BOUND,
    )

    payload: dict[str, Any] = {
        "state": "qualified",
        "shortfalls": (),
        "adjudicated_count": MIN_ADJUDICATED,
        "non_abstained_count": round(MIN_ADJUDICATED * MIN_COVERAGE),
        "coverage": MIN_COVERAGE,
        "precision_lower_bound": MIN_PRECISION_LOWER_BOUND,
        "distinct_episodes": MIN_DISTINCT_EPISODES,
        "duplicate_context_ratio": 0.0,
    }
    payload.update(over)
    return payload


def test_a_measured_qualification_is_accepted() -> None:
    assert_qualification_is_earned(**_earned())


def test_a_qualified_verdict_beside_zero_adjudicated_cases_is_refused() -> None:
    """The exact record the adversarial probe wrote: state=qualified, nothing measured.

    Bound keys answer "is this measurement still about the system in use?".  They have never
    answered "was there a measurement?", so a record asserting a verdict its own numbers
    contradict passes every one of them and grants full exposure.
    """

    with pytest.raises(ValueError) as excinfo:
        assert_qualification_is_earned(
            **_earned(
                shortfalls=(
                    "adjudicated 0 < 100",
                    "coverage 0.0 < 0.3",
                    "baseline_explicit_rule not_computable",
                ),
                adjudicated_count=0,
                non_abstained_count=0,
                coverage=0.0,
                precision_lower_bound=0.0,
                distinct_episodes=0,
            )
        )
    message = str(excinfo.value)
    assert "refusing to record state=qualified" in message
    assert "shortfalls is non-empty" in message
    assert "adjudicated_count 0 < 100" in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shortfalls", ("coverage 0.1 < 0.3",)),
        ("adjudicated_count", 99),
        ("coverage", 0.29),
        ("precision_lower_bound", 0.89),
        ("distinct_episodes", 19),
        ("duplicate_context_ratio", 0.26),
    ],
)
def test_every_numeric_clause_is_a_necessary_condition(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        assert_qualification_is_earned(**_earned(**{field: value}))


def test_a_recorded_refusal_is_never_blocked() -> None:
    """The failed run must still leave a row; only a *claim* is checked."""

    for state in ("not_qualified", "expired", "invalidated", "demoted"):
        assert_qualification_is_earned(
            **_earned(
                state=state,
                shortfalls=("adjudicated 0 < 100",),
                adjudicated_count=0,
                non_abstained_count=0,
                coverage=0.0,
                precision_lower_bound=0.0,
                distinct_episodes=0,
            )
        )


def test_the_interlock_reads_the_frozen_constants_so_it_cannot_be_relaxed_quietly() -> None:
    """Lowering a clause to admit a weaker record moves THRESHOLDS_SHA, a bound key on every
    record already written, so the relaxation invalidates exactly what it was reached for."""

    import inspect

    from tce_shared import policy_evaluation, policy_thresholds

    source = inspect.getsource(policy_evaluation.assert_qualification_is_earned)
    digest_inputs = policy_thresholds.threshold_values()
    for name in (
        "MIN_ADJUDICATED",
        "MIN_COVERAGE",
        "MIN_PRECISION_LOWER_BOUND",
        "MIN_DISTINCT_EPISODES",
        "MAX_DUPLICATE_CONTEXT_RATIO",
    ):
        assert name in source, f"{name} must be read from policy_thresholds, not inlined"
        assert name in digest_inputs, f"{name} must be hashed into THRESHOLDS_SHA"
