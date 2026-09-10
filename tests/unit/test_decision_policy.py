"""The decision policy, case by case.

Every case here corresponds to a defect that was reproduced against the pre-P4 tree, so a
failure means the defect is back rather than that a threshold moved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.behavior_fidelity import (
    _effective_sample_size,
    _similarity,
    _topical_overlap,
    behavior_storage_gate,
)
from tce_shared.decision_policy import (
    DECISION_POLICY_REVISION,
    AbstainReason,
    AdvisorContribution,
    ConflictStatus,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    ExplicitRule,
    ExposureState,
    OodStatus,
    PolicyTuning,
    Qualification,
    QualificationState,
    advice_names_a_candidate,
    decide,
    evidence_strength_label,
    validate_cited_ids,
)
from tce_shared.policy_thresholds import MIN_EFFECTIVE_SAMPLE, OOD_OVERLAP_FLOOR, THRESHOLDS_SHA

_NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)
_OPTIONS = ("pause and verify", "force push to prod")
_SUMMARY = "the stripe webhook is failing, do i force push the fix to prod"


def _row(
    index: int,
    *,
    selected: str = "pause and verify",
    summary: str = _SUMMARY,
    situation_type: str = "choice_required",
    age_days: float = 3.0,
    source: str = "explicit",
    action: str = "",
    contradicts: list[str] | None = None,
    project_id: str | None = "proj-1",
) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "ts": (_NOW - timedelta(days=age_days)).isoformat(),
        "situation_type": situation_type,
        "situation_summary": summary,
        "objective_text": summary,
        "constraints": {},
        "context_snapshot": {},
        "available_choices": list(_OPTIONS),
        "selected_choice": selected,
        "action_taken": action or f"{selected} carefully",
        "evidence_source": source,
        "memory_class": "decision",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "valid_from": (_NOW - timedelta(days=age_days)).isoformat(),
        "superseded_by": None,
        "contradicts_observation_ids": contradicts or [],
        "project_id": project_id,
        # A model's own number about itself.  It is written into this column live; nothing in
        # the policy is allowed to read it, and D-self-report below proves it does not.
        "confidence": 1.0,
    }


def _request(
    rows: list[dict[str, Any]],
    *,
    options: tuple[str, ...] = _OPTIONS,
    summary: str = _SUMMARY,
    situation_type: str = "choice_required",
    decision_at: datetime = _NOW,
    qualification: Qualification | None = None,
    explicit_rules: tuple[ExplicitRule, ...] = (),
    advisor: AdvisorContribution | None = None,
    tuning: PolicyTuning | None = None,
    learning_eligible_total: int = 0,
    project_id: str | None = "proj-1",
) -> DecisionRequest:
    return DecisionRequest(
        decision_family="choice_required",
        situation_type=situation_type,
        situation_summary=summary,
        objective_text=summary,
        constraints={},
        context_snapshot={},
        candidate_options=options,
        evidence_rows=tuple(rows),
        decision_at=decision_at,
        workspace_id="ws",
        subject_user_id="subject",
        project_id=project_id,
        episode_key="episode",
        evidence_revision="rev-1",
        evidence_cutoff_at=None,
        retrieval_version="full-knn-v1",
        model_id="model-a",
        runtime_version="runtime-1",
        learning_eligible_total=learning_eligible_total,
        qualification=qualification,
        explicit_rules=explicit_rules,
        advisor=advisor,
        tuning=tuning or PolicyTuning(),
    )


def _qualification(
    *,
    state: QualificationState = QualificationState.QUALIFIED,
    prompt_sha256: str = "",
    tuning_sha: str | None = None,
    learning_eligible_at_qualification: int = 0,
    expires_at: datetime | None = None,
) -> Qualification:
    return Qualification(
        qualification_id="qual-1",
        state=state,
        project_id="proj-1",
        decision_family="choice_required",
        decision_policy_revision=DECISION_POLICY_REVISION,
        model_id="model-a",
        runtime_version="runtime-1",
        prompt_sha256=prompt_sha256,
        retrieval_version="full-knn-v1",
        thresholds_sha=THRESHOLDS_SHA,
        tuning_sha=tuning_sha if tuning_sha is not None else PolicyTuning().tuning_sha(),
        evidence_cutoff_at=None,
        learning_eligible_at_qualification=learning_eligible_at_qualification,
        expires_at=expires_at or (_NOW + timedelta(days=30)),
    )


def _advisor(**overrides: Any) -> AdvisorContribution:
    payload: dict[str, Any] = {
        "recommended_option": "pause and verify",
        "abstained": False,
        "abstain_reason": None,
        "evidence_ids": (),
        "conflicting_evidence_ids": (),
        "advisor_note": None,
        "parse_state": "parsed",
        "prompt_sha256": "",
        "model_id": "model-a",
        "runtime_version": "runtime-1",
    }
    payload.update(overrides)
    return AdvisorContribution(**payload)


# --- D1 / D2: the explicit-rule layer -------------------------------------------------


def test_d1_explicit_rule_wins_and_is_exposed_without_any_qualification() -> None:
    rule = ExplicitRule(rule_id="rule-1", statement="force push to prod", priority=1, scope_project_id=None)
    result = decide(_request([_row(1), _row(2)], explicit_rules=(rule,)))
    assert result.status is DecisionStatus.RULE_APPLIED
    assert result.selected_option == "force push to prod"
    assert result.applied_rule_id == "rule-1"
    assert result.evidence_role == "rule"
    # Obeying a written instruction is not a prediction about the human, so it does not need
    # the permission that predicting does.
    assert result.exposed is True
    assert result.exposure_state is ExposureState.RULE_LAYER


def test_d2_rule_that_maps_to_nothing_is_skipped() -> None:
    rule = ExplicitRule(
        rule_id="rule-1",
        statement="rewrite the scheduler in rust",
        priority=1,
        scope_project_id=None,
    )
    result = decide(_request([_row(1), _row(2)], explicit_rules=(rule,)))
    assert result.status is not DecisionStatus.RULE_APPLIED
    assert result.applied_rule_id is None


def test_d2b_rule_scoped_to_another_project_is_skipped() -> None:
    rule = ExplicitRule(rule_id="rule-1", statement="force push to prod", priority=1, scope_project_id="proj-2")
    result = decide(_request([_row(1), _row(2)], explicit_rules=(rule,)))
    assert result.applied_rule_id is None


# --- D3 / D4 / D5: the candidate and eligibility guards --------------------------------


def test_d3_empty_candidate_set_abstains_and_returns_no_option() -> None:
    """The reproduced defect: ``_map_choice('force push to prod', [])`` returns its input.

    Without this guard the policy answers with an option nobody offered.  32 of 34 live
    observations carry fewer than two candidates, so this is the ordinary turn.
    """

    result = decide(_request([_row(1), _row(2)], options=()))
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.NO_CANDIDATE_MATCH
    assert result.selected_option is None
    assert result.adequacy.shortfalls == ("no_candidate_options",)
    assert result.reason_for_asking


def test_d4_empty_evidence_abstains_with_no_eligible_evidence() -> None:
    result = decide(_request([]))
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.NO_ELIGIBLE_EVIDENCE
    assert result.selected_option is None


def test_d5_unmappable_choices_abstain_and_never_return_an_unoffered_label() -> None:
    rows = [_row(1, selected="rewrite everything"), _row(2, selected="rewrite everything")]
    result = decide(_request(rows))
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.NO_CANDIDATE_MATCH
    assert result.selected_option is None
    assert result.ranked_options == ()


# --- D6 / D7: effective sample size ----------------------------------------------------


def test_d6_single_neighbour_abstains_as_inadequate() -> None:
    result = decide(_request([_row(1)]))
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.INADEQUATE_EVIDENCE
    assert result.adequacy.effective_sample_size == 1.0
    assert "effective_sample_size" in result.adequacy.shortfalls
    assert "above_floor_count" in result.adequacy.shortfalls


def test_d7_two_real_neighbours_clear_the_floor_that_two_point_zero_would_reject() -> None:
    """Kish ESS for two genuine neighbours falls just short of 2.0, by ~4.7e-6.

    ``>= 2.0`` therefore silently means "three or more" and contradicts the sibling clause
    ``above_floor_count >= 2``; ``2.0 - 1e-9`` does not rescue it either, because the deficit
    is six orders of magnitude larger than machine epsilon.
    """

    ess = _effective_sample_size([0.473947, 0.4725])
    assert ess < 2.0
    assert 2.0 - ess > 1e-9
    assert ess >= MIN_EFFECTIVE_SAMPLE

    result = decide(_request([_row(1), _row(2)]))
    assert result.status is DecisionStatus.SELECTED
    assert result.adequacy.effective_sample_size >= MIN_EFFECTIVE_SAMPLE
    assert result.adequacy.effective_sample_size < 2.0 + 1e-9


# --- D8: recency at decision time ------------------------------------------------------


def test_d8_corpus_age_changes_the_score_instead_of_being_invisible() -> None:
    """The reproduced defect: a 900-day corpus and a 5-day corpus predicted identically.

    Anchoring recency on ``max(row.ts)`` makes the decay a property of the corpus, so in both
    corpora the newest row is zero days old relative to itself.  Anchored at decision time,
    a stale corpus scores strictly lower.
    """

    fresh = decide(_request([_row(1, age_days=5.0), _row(2, age_days=5.0)]))
    stale = decide(_request([_row(1, age_days=900.0), _row(2, age_days=900.0)]))
    assert fresh.adequacy.top_similarity > stale.adequacy.top_similarity
    assert fresh.policy_score > stale.policy_score

    row = _row(1, age_days=900.0)
    query = {"situation_type": "choice_required", "situation_summary": _SUMMARY, "objective_text": _SUMMARY}
    old_anchor = _similarity(query, row, decision_at=datetime.fromisoformat(str(row["ts"])))
    at_decision = _similarity(query, row, decision_at=_NOW)
    assert old_anchor > at_decision


def test_d8b_similarity_ignores_a_self_reported_confidence_column() -> None:
    query = {"situation_type": "choice_required", "situation_summary": _SUMMARY, "objective_text": _SUMMARY}
    confident = _row(1)
    confident["confidence"] = 1.0
    unconfident = _row(1)
    unconfident["confidence"] = 0.0
    assert _similarity(query, confident, decision_at=_NOW) == _similarity(query, unconfident, decision_at=_NOW)


# --- D9: out of distribution is measured on topical overlap ----------------------------


def test_d9_unrelated_but_fresh_and_explicit_evidence_is_out_of_distribution() -> None:
    """The finding that matters most for the abstention machinery.

    ``_similarity`` is a ranking score: 40% of its range is recency, provenance and
    situation-type match.  A semantically unrelated row that is fresh, explicit and shares a
    ``situation_type`` therefore out-scores the identical row aged a year, so asking that
    number whether a query is in distribution gets an answer about freshness.  Raw topical
    overlap separates them cleanly.
    """

    unrelated = "ssh into the box and restart nginx"
    rows = [_row(1, summary=unrelated, action="restart nginx"), _row(2, summary=unrelated, action="restart nginx")]
    request = _request(rows)
    query = request.query()

    assert _similarity(query, rows[0], decision_at=_NOW) > 0.40
    assert _topical_overlap(query, rows[0]) < OOD_OVERLAP_FLOOR

    result = decide(request)
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.OUT_OF_DISTRIBUTION
    assert result.ood_status is OodStatus.OUT_OF_DISTRIBUTION
    assert result.adequacy.adequate is True  # adequate, and still out of distribution


def test_d9b_a_paraphrase_of_the_query_is_in_distribution() -> None:
    result = decide(_request([_row(1), _row(2)]))
    assert result.ood_status is OodStatus.IN_DISTRIBUTION
    assert result.status is DecisionStatus.SELECTED


# --- D10 / D11: conflict ---------------------------------------------------------------


def test_d10_a_split_vote_abstains_and_reports_the_split() -> None:
    rows = [_row(1), _row(2), _row(3, selected="force push to prod"), _row(4, selected="force push to prod")]
    result = decide(_request(rows))
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.CONFLICTING_EVIDENCE
    assert result.conflict_status is ConflictStatus.SPLIT_VOTE
    assert result.conflicting_observation_ids
    assert "disagree" in (result.reason_for_asking or "")


def test_d11_a_residual_cross_option_contradiction_is_reported() -> None:
    # An ``inferred`` row's contradiction link does not drop its target in the eligibility
    # filter (only explicit/correction/calibration do), so both rows survive and the residual
    # cross-option link is a real disagreement.
    left = _row(1, source="inferred", contradicts=["00000000-0000-0000-0000-000000000003"])
    rows = [
        left,
        _row(2),
        _row(3, selected="force push to prod", source="inferred"),
    ]
    result = decide(_request(rows))
    assert result.conflict_status is ConflictStatus.EXPLICIT_CONTRADICTION
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.CONFLICTING_EVIDENCE
    assert left["id"] in result.conflicting_observation_ids


# --- D12 / D13: binding evidence and action to the chosen option -----------------------


def test_d12_evidence_ids_only_ever_support_the_selected_option() -> None:
    rows = [_row(1), _row(2), _row(3), _row(4, selected="force push to prod")]
    result = decide(_request(rows))
    assert result.status is DecisionStatus.SELECTED
    assert result.selected_option == "pause and verify"
    assert result.evidence_role == "supporting"
    assert set(result.evidence_observation_ids) == {rows[0]["id"], rows[1]["id"], rows[2]["id"]}
    assert rows[3]["id"] not in result.evidence_observation_ids


def test_d13_suggested_action_never_comes_from_a_losing_neighbour() -> None:
    """The reproduced defect: the policy answered "pause and verify" and handed the executor
    "force push to prod", because the action vote was summed over every neighbour."""

    rows = [
        _row(1, action="open a canary and watch the error rate"),
        _row(2, action="open a canary and watch the error rate"),
        _row(3, action="open a canary and watch the error rate"),
        _row(4, selected="force push to prod", action="force push to prod"),
    ]
    result = decide(_request(rows))
    assert result.selected_option == "pause and verify"
    assert result.suggested_action == "open a canary and watch the error rate"


# --- D14 / D15: the advisor contributes, never decides ---------------------------------


def test_d14_a_disagreeing_advisor_abstains_and_cannot_change_the_option() -> None:
    result = decide(_request([_row(1), _row(2), _row(3)], advisor=_advisor(recommended_option="force push to prod")))
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.ADVISOR_FORCED
    assert result.advisor_agreement.value == "disagreed"
    assert result.selected_option is None


def test_d14b_an_agreeing_advisor_leaves_the_decision_alone() -> None:
    plain = decide(_request([_row(1), _row(2), _row(3)]))
    advised = decide(_request([_row(1), _row(2), _row(3)], advisor=_advisor()))
    assert advised.status is DecisionStatus.SELECTED
    assert advised.selected_option == plain.selected_option
    assert advised.policy_score == plain.policy_score
    assert advised.advisor_agreement.value == "agreed"


def test_d15_a_failed_advisor_call_abstains_even_with_no_required_families() -> None:
    """Abstention is the failure mode by default, not behind a knob.

    ``advisor_required_families`` defaults to empty, so checking it before the parse state
    would mean a dead model is ignored on every family.
    """

    tuning = PolicyTuning(advisor_required_families=frozenset())
    result = decide(
        _request(
            [_row(1), _row(2), _row(3)],
            advisor=_advisor(parse_state="call_failed", recommended_option=None),
            tuning=tuning,
        )
    )
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.ADVISOR_FORCED
    assert result.advisor_agreement.value == "unavailable"


def test_d15b_an_absent_advisor_is_fine_unless_the_family_requires_one() -> None:
    permissive = decide(_request([_row(1), _row(2), _row(3)]))
    assert permissive.status is DecisionStatus.SELECTED
    assert permissive.advisor_agreement.value == "absent"

    strict = decide(
        _request(
            [_row(1), _row(2), _row(3)],
            tuning=PolicyTuning(advisor_required_families=frozenset({"choice_required"})),
        )
    )
    assert strict.status is DecisionStatus.ABSTAINED
    assert strict.abstain_reason is AbstainReason.ADVISOR_FORCED


def test_advisor_citation_overlap_is_the_reader_for_claimed_evidence_ids() -> None:
    rows = [_row(1), _row(2), _row(3)]
    advisor = _advisor(evidence_ids=(rows[0]["id"], "00000000-0000-0000-0000-000000009999"))
    result = decide(_request(rows, advisor=advisor))
    assert result.advisor_citation_overlap == 1.0  # the invented id is dropped, not counted


# --- D16 to D19: permission is exposure, and only exposure -----------------------------


def _comparable(result: DecisionResult) -> tuple[Any, ...]:
    return (
        result.status,
        result.abstain_reason,
        result.selected_option,
        result.ranked_options,
        result.adequacy,
        result.ood_status,
        result.ood_score,
        result.conflict_status,
        result.policy_score,
        result.suggested_action,
    )


def test_d16_an_unqualified_family_changes_nothing_except_exposure() -> None:
    """The property that makes "the turn is unchanged" a fact rather than a promise.

    An earlier draft downgraded an unqualified family to an abstention, which on a corpus
    where every family is unqualified turns every takeover turn into a human escalation —
    a product shutdown.
    """

    rows = [_row(1), _row(2), _row(3)]
    unqualified = decide(_request(rows))
    qualified = decide(_request(rows, qualification=_qualification()))

    assert _comparable(unqualified) == _comparable(qualified)
    assert unqualified.exposed is False
    assert unqualified.exposure_state is ExposureState.NO_QUALIFICATION
    assert qualified.exposed is True
    assert qualified.exposure_state is ExposureState.EXPOSED
    assert AbstainReason.__members__.get("FAMILY_NOT_QUALIFIED") is None


def test_d17_a_prompt_hash_that_moved_invalidates_the_qualification() -> None:
    result = decide(_request([_row(1), _row(2), _row(3)], qualification=_qualification(prompt_sha256="other")))
    assert result.exposed is False
    assert result.exposure_state is ExposureState.BINDING_MISMATCH
    assert result.status is DecisionStatus.SELECTED  # the decision itself is untouched


def test_d18_a_tuning_hash_that_moved_invalidates_the_qualification() -> None:
    result = decide(
        _request(
            [_row(1), _row(2), _row(3)],
            qualification=_qualification(tuning_sha="stale-tuning-sha"),
        )
    )
    assert result.exposed is False
    assert result.exposure_state is ExposureState.BINDING_MISMATCH


def test_d18b_an_expired_qualification_is_not_exposure() -> None:
    expired = _qualification(expires_at=_NOW - timedelta(days=1))
    result = decide(_request([_row(1), _row(2), _row(3)], qualification=expired))
    assert result.exposed is False
    assert result.exposure_state is ExposureState.QUALIFICATION_EXPIRED


def test_d19_evidence_growth_past_the_frozen_fraction_withdraws_exposure() -> None:
    record = _qualification(learning_eligible_at_qualification=100)
    steady = decide(_request([_row(1), _row(2), _row(3)], qualification=record, learning_eligible_total=140))
    drifted = decide(_request([_row(1), _row(2), _row(3)], qualification=record, learning_eligible_total=200))
    assert steady.exposed is True
    assert drifted.exposed is False
    assert drifted.exposure_state is ExposureState.EVIDENCE_DRIFT


def test_d20_there_is_no_calibrated_score_anywhere_on_the_result() -> None:
    result = decide(_request([_row(1), _row(2), _row(3)]))
    fields = set(DecisionResult.__dataclass_fields__)
    assert "calibrated_score" not in fields
    assert "calibration_method" not in fields
    assert not [name for name in fields if "calibrat" in name]
    assert "policy_score" in result.to_payload()
    assert "calibrated_score" not in result.to_payload()


# --- the small pure helpers ------------------------------------------------------------


def test_validate_cited_ids_drops_invented_ids_and_counts_them() -> None:
    kept, dropped = validate_cited_ids(["a", "b", "a", "zzz", ""], ["a", "b", "c"])
    assert kept == ("a", "b")
    assert dropped == 1


def test_evidence_strength_label_reads_only_properties_of_the_evidence() -> None:
    weak = decide(_request([_row(1)]))
    moderate = decide(_request([_row(1), _row(2), _row(3)]))
    assert evidence_strength_label(weak.adequacy) == "weak"
    assert evidence_strength_label(moderate.adequacy) in {"moderate", "strong"}


def test_advice_names_a_candidate_detects_the_real_contamination() -> None:
    assert advice_names_a_candidate("I would pause and verify before shipping", list(_OPTIONS)) is True
    assert advice_names_a_candidate("Have a look at the webhook logs first", list(_OPTIONS)) is False
    assert advice_names_a_candidate("", list(_OPTIONS)) is False


def test_fingerprint_moves_when_the_inputs_move_and_not_otherwise() -> None:
    base = _request([_row(1), _row(2)])
    assert base.fingerprint() == _request([_row(1), _row(2)]).fingerprint()
    assert base.fingerprint() != _request([_row(1)]).fingerprint()
    assert base.fingerprint() != _request([_row(1), _row(2)], options=("pause and verify",)).fingerprint()
    assert base.fingerprint() != _request([_row(1), _row(2)], advisor=_advisor()).fingerprint()
    assert base.fingerprint() != _request([_row(1), _row(2)], decision_at=_NOW + timedelta(hours=1)).fingerprint()


def test_result_block_payload_carries_no_score_for_an_executor_to_misread() -> None:
    block = decide(_request([_row(1), _row(2), _row(3)])).block_payload()
    assert "policy_score" not in block
    assert "calibrated_score" not in block
    assert block["decision_policy_revision"] == DECISION_POLICY_REVISION
    assert len(block["evidence_observation_ids"]) <= 12


# --- Y8, one layer deeper: the self-report cannot reach the strength label either -------


def _row_scored_by_the_storage_gate(
    index: int,
    *,
    confidence: float,
    rationale: str,
) -> dict[str, Any]:
    """A row whose ``learning_eligible`` is decided by the real storage gate.

    That flag is the WHERE clause of both policy evidence loaders, so it is the hop by which
    a writer's own number about itself used to reach ``evidence_strength_label``: claim
    confidence, become eligible, be counted in ``learning_eligible_count``, look strong.
    """

    row = _row(index)
    row["confidence"] = confidence
    row["rationale"] = rationale
    row["evidence_source"] = "inferred"
    row["outcome"] = "the deploy held for a week"
    gate = behavior_storage_gate(dict(row), threshold=0.55)
    row["learning_eligible"] = gate["learning_eligible"]
    return row


def test_a_self_reported_confidence_changes_neither_eligibility_nor_strength_label() -> None:
    for rationale in ("", "reversible, and verified against the affected package"):
        unconfident = [_row_scored_by_the_storage_gate(i, confidence=0.0, rationale=rationale) for i in (1, 2, 3)]
        confident = [_row_scored_by_the_storage_gate(i, confidence=1.0, rationale=rationale) for i in (1, 2, 3)]

        assert [row["learning_eligible"] for row in unconfident] == [row["learning_eligible"] for row in confident]

        quiet = decide(_request(unconfident))
        loud = decide(_request(confident))
        assert evidence_strength_label(quiet.adequacy) == evidence_strength_label(loud.adequacy)
        assert quiet.adequacy.to_payload() == loud.adequacy.to_payload()
        assert quiet.status is loud.status

    # And the two rationale branches are genuinely different populations, so the equality
    # above is not the trivial "everything is weak" case.
    documented = [_row_scored_by_the_storage_gate(i, confidence=0.0, rationale="reversible and verified") for i in (1, 2, 3)]
    undocumented = [_row_scored_by_the_storage_gate(i, confidence=1.0, rationale="") for i in (1, 2, 3)]
    assert all(row["learning_eligible"] for row in documented)
    assert not any(row["learning_eligible"] for row in undocumented)
    assert evidence_strength_label(decide(_request(documented)).adequacy) != evidence_strength_label(
        decide(_request(undocumented)).adequacy
    )
