from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tce_shared.decision_capture import (
    ANSWER_PRECEDES_QUESTION,
    CAPTURE_SCHEMA_VERSION,
    EXTRACTION_VERSION,
    CandidateKind,
    CaptureScope,
    DecisionOpportunity,
    OriginKind,
    Promotion,
    analysable_text,
    compute_delivery_key,
    evidence_revision,
    extract_candidates,
    is_concrete_instruction,
    is_retrospective,
    make_receipt,
    map_ack_to_alternative,
    match_alternatives,
    opportunity_expired,
    promotion_decision,
    promotion_decision_detail,
    resolve_prediction_fields,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
SAFETY_QUESTION = "Safety pause: high-risk action detected. Type 'confirm' to continue or 'abort' to stop."


def _opp(
    oid: str = "opp-1",
    alternatives: tuple[str, ...] = ("confirm", "abort"),
    *,
    status: str = "open",
    family: str = "safety_confirmation",
    situation_type: str = "approval_requested",
    question_text: str = SAFETY_QUESTION,
    expires_at: datetime | None = NOW + timedelta(hours=1),
    resolved_at: datetime | None = None,
    frozen_at: datetime | None = None,
) -> DecisionOpportunity:
    return DecisionOpportunity(
        opportunity_id=oid,
        decision_family=family,
        situation_type=situation_type,
        question_text=question_text,
        alternatives=alternatives,
        status=status,
        created_at=NOW - timedelta(minutes=1),
        frozen_at=frozen_at if frozen_at is not None else NOW - timedelta(minutes=1),
        expires_at=expires_at,
        session_id="codex",
        resolved_at=resolved_at,
    )


def _msg(content: str, origin: str = "human_input") -> dict[str, Any]:
    return {"content": content, "origin_kind": origin, "observed_at": NOW, "project_id": "ote", "task_id": None}


def _ctx(**extra: Any) -> dict[str, Any]:
    return {"now": NOW, **extra}


# 1. quoted text (R3 after quoted_line removal)
def test_quoted_text_is_dropped_and_human_line_answers() -> None:
    content = "> Type 'confirm' to continue\nconfirm"
    kept, dropped = analysable_text(content, [_opp()])
    assert "quoted_line" in dropped
    assert kept.strip() == "confirm"
    cands = extract_candidates(_msg(content), _ctx(), [_opp()])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind is CandidateKind.ANSWER
    assert c.selected_option == "confirm"
    assert c.opportunity_id == "opp-1"
    assert c.observed_alternatives == ("confirm", "abort")
    assert c.supporting_span in content
    assert c.span_sha256 == hashlib.sha256(c.supporting_span.encode("utf-8")).hexdigest()
    assert c.extraction_version == EXTRACTION_VERSION
    assert promotion_decision(c, [_opp()], now=NOW) is Promotion.PROMOTE


PASTED = 'Assistant: I will run the deploy now\n{ "has_directive": true }\nnext_step: run tests'


# 2. pasted agent output only (R2)
def test_pasted_agent_output_only_is_noise() -> None:
    cands = extract_candidates(_msg(PASTED), _ctx(), [_opp()])
    assert [c.kind for c in cands] == [CandidateKind.NOISE]
    assert promotion_decision(cands[0], [_opp()], now=NOW) is Promotion.DISCARD


# 3. pasted agent output + human line (R3)
def test_pasted_agent_output_plus_human_line_answers() -> None:
    cands = extract_candidates(_msg(PASTED + "\nabort"), _ctx(), [_opp()])
    assert len(cands) == 1
    assert cands[0].kind is CandidateKind.ANSWER
    assert cands[0].selected_option == "abort"


# 4. negation (R6)
def test_negated_confirm_selects_abort() -> None:
    cands = extract_candidates(_msg("don't confirm, abort it"), _ctx(), [_opp()])
    assert len(cands) == 1
    assert cands[0].kind is CandidateKind.ANSWER
    assert cands[0].selected_option == "abort"


def test_negated_alternative_with_two_options_selects_the_other() -> None:
    opp = _opp(alternatives=("minimal verified fix", "broad refactor"), family="needs_human", situation_type="choice_required", question_text="Which approach?")
    cands = extract_candidates(_msg("not the broad refactor"), _ctx(), [opp])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind is CandidateKind.ANSWER
    assert c.selected_option == "minimal verified fix"
    assert c.is_negated is True
    assert promotion_decision(c, [opp], now=NOW) is Promotion.PROMOTE


def test_negated_alternative_with_three_options_is_rejection_pending_review() -> None:
    opp = _opp(alternatives=("minimal verified fix", "broad refactor", "defer"), family="needs_human", situation_type="choice_required", question_text="Which approach?")
    cands = extract_candidates(_msg("not the broad refactor"), _ctx(), [opp])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind is CandidateKind.REJECTION
    assert c.selected_option is None
    assert c.is_negated is True
    assert promotion_decision(c, [opp], now=NOW) is Promotion.PENDING_REVIEW


# 5. generic yes (R3 ack path + promotion uniqueness)
@pytest.mark.parametrize(
    ("opps", "expected_kind", "expected_promotion"),
    [
        ([], CandidateKind.ACKNOWLEDGEMENT, Promotion.DISCARD),
        ([_opp()], CandidateKind.ANSWER, Promotion.PROMOTE),
        ([_opp("opp-1"), _opp("opp-2")], CandidateKind.ACKNOWLEDGEMENT, Promotion.DISCARD),
        ([_opp(alternatives=(), family="needs_human", question_text="How should I proceed?")], CandidateKind.ACKNOWLEDGEMENT, Promotion.DISCARD),
    ],
    ids=["no-open", "one-open-with-alternatives", "two-open", "one-open-ended"],
)
def test_generic_yes_only_answers_a_unique_alternatives_question(opps: list[DecisionOpportunity], expected_kind: CandidateKind, expected_promotion: Promotion) -> None:
    cands = extract_candidates(_msg("yes"), _ctx(), opps)
    assert len(cands) == 1
    assert cands[0].kind is expected_kind
    if expected_kind is CandidateKind.ANSWER:
        assert cands[0].selected_option == "confirm"
    assert promotion_decision(cands[0], opps, now=NOW) is expected_promotion


# 6. yes with unique but expired open question
def test_yes_against_expired_question_is_pending_review() -> None:
    opp = _opp(expires_at=NOW - timedelta(minutes=5))
    cands = extract_candidates(_msg("yes"), _ctx(), [opp])
    assert cands[0].kind is CandidateKind.ANSWER
    assert promotion_decision(cands[0], [opp], now=NOW) is Promotion.PENDING_REVIEW


def test_host_clock_behind_cannot_rewind_an_answer_into_an_expired_question() -> None:
    """A host clock running behind rewinds observed_at before the TTL; expiry is judged at server time anyway."""

    expired_at = NOW - timedelta(minutes=5)
    opp = _opp(expires_at=expired_at, frozen_at=NOW - timedelta(minutes=30))
    rewound = expired_at - timedelta(minutes=1)  # host says the human answered while the question was still live
    cands = extract_candidates(_msg("yes"), _ctx(), [opp])
    assert cands[0].kind is CandidateKind.ANSWER

    # Judged only at the host-supplied moment the question still looks live -- this is the hole.
    assert promotion_decision(cands[0], [opp], now=rewound) is Promotion.PROMOTE
    # Judged with server time on the receipt it is expired, and the answer is held for review.
    assert promotion_decision_detail(cands[0], [opp], now=rewound, expiry_now=NOW) == (
        Promotion.PENDING_REVIEW,
        "ambiguous_or_expired_target",
    )


def test_expiry_now_never_revives_an_expired_question() -> None:
    """max(now, expiry_now) is monotone: an earlier server time can only tighten expiry, never loosen it."""

    opp = _opp(expires_at=NOW - timedelta(minutes=5), frozen_at=NOW - timedelta(minutes=30))
    candidate = extract_candidates(_msg("yes"), _ctx(), [opp])[0]
    assert promotion_decision(candidate, [opp], now=NOW, expiry_now=NOW - timedelta(hours=1)) is Promotion.PENDING_REVIEW


def test_spooled_answer_delivered_after_the_ttl_is_held_for_review_not_promoted() -> None:
    """The safe direction: a genuine in-time answer spooled past the TTL is reviewed, never silently promoted."""

    opp = _opp(expires_at=NOW + timedelta(minutes=1), frozen_at=NOW - timedelta(minutes=1))
    cands = extract_candidates(_msg("yes"), _ctx(), [opp])
    late_delivery = NOW + timedelta(hours=2)
    assert promotion_decision(cands[0], [opp], now=NOW) is Promotion.PROMOTE
    assert promotion_decision(cands[0], [opp], now=NOW, expiry_now=late_delivery) is Promotion.PENDING_REVIEW


# 7. correction (R5)
def test_correction_against_single_resolved_opportunity_promotes() -> None:
    resolved = _opp("opp-r", alternatives=("minimal verified fix", "broad refactor"), status="resolved", resolved_at=NOW - timedelta(minutes=2))
    cands = extract_candidates(_msg("actually, go with the broad refactor"), _ctx(recently_resolved=[resolved]), [])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind is CandidateKind.CORRECTION
    assert c.is_correction is True
    assert c.opportunity_id == "opp-r"
    assert c.selected_option == "broad refactor"
    assert c.observed_alternatives == ("minimal verified fix", "broad refactor")
    assert promotion_decision(c, [], now=NOW) is Promotion.PROMOTE


def test_correction_matching_two_resolved_opportunities_is_contradicted() -> None:
    r1 = _opp("opp-r1", alternatives=("minimal verified fix", "broad refactor"), status="resolved", resolved_at=NOW - timedelta(minutes=2))
    r2 = _opp("opp-r2", alternatives=("broad refactor", "defer"), status="resolved", resolved_at=NOW - timedelta(minutes=3))
    cands = extract_candidates(_msg("actually, go with the broad refactor"), _ctx(recently_resolved=[r1, r2]), [])
    assert len(cands) == 1
    assert cands[0].kind is CandidateKind.CORRECTION
    assert cands[0].opportunity_id is None
    assert cands[0].contradicted is True
    assert promotion_decision(cands[0], [], now=NOW) is Promotion.DISCARD


# 8. contradiction (R6)
def test_two_non_negated_alternatives_is_contradicted() -> None:
    cands = extract_candidates(_msg("confirm the deploy and then abort"), _ctx(), [_opp()])
    assert len(cands) == 1
    assert cands[0].kind is CandidateKind.ANSWER
    assert cands[0].selected_option is None
    assert cands[0].contradicted is True
    assert promotion_decision(cands[0], [_opp()], now=NOW) is Promotion.DISCARD


# 9. echoed question removed
def test_echoed_question_text_is_removed_before_matching() -> None:
    content = SAFETY_QUESTION + "\nconfirm"
    kept, dropped = analysable_text(content, [_opp()])
    assert "echoed_question" in dropped
    assert kept.strip() == "confirm"
    cands = extract_candidates(_msg(content), _ctx(), [_opp()])
    assert len(cands) == 1
    assert cands[0].kind is CandidateKind.ANSWER
    assert cands[0].selected_option == "confirm"


# 10. rationale only from explicit marker
def test_rationale_is_only_taken_after_marker() -> None:
    with_reason = extract_candidates(_msg("abort because prod is live"), _ctx(), [_opp()])
    assert with_reason[0].kind is CandidateKind.ANSWER
    assert with_reason[0].selected_option == "abort"
    assert with_reason[0].stated_rationale == "prod is live"
    bare = extract_candidates(_msg("abort"), _ctx(), [_opp()])
    assert bare[0].stated_rationale is None


# 11. origin kinds
def test_imported_transcript_answers_are_pending_review() -> None:
    cands = extract_candidates(_msg("confirm", origin="imported_transcript"), _ctx(), [_opp()])
    assert cands[0].kind is CandidateKind.ANSWER
    assert cands[0].origin_kind is OriginKind.IMPORTED_TRANSCRIPT
    assert promotion_decision(cands[0], [_opp()], now=NOW) is Promotion.PENDING_REVIEW


@pytest.mark.parametrize("origin", ["executor_output", "tool_result", "manager_instruction"])
def test_non_human_origins_never_produce_candidates(origin: str) -> None:
    assert extract_candidates(_msg("confirm", origin=origin), _ctx(), [_opp()]) == []


# 12. open-ended concrete answer (R7)
def test_concrete_instruction_answers_open_ended_question() -> None:
    opp = _opp(alternatives=(), family="needs_human", situation_type="choice_required", question_text="Ask the user how to proceed on this objective.")
    text = "fix the flaky retry test in tests/unit first"
    cands = extract_candidates(_msg(text), _ctx(), [opp])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind is CandidateKind.ANSWER
    assert c.opportunity_id == "opp-1"
    assert c.selected_option == text
    assert c.observed_alternatives == ()
    assert promotion_decision(c, [opp], now=NOW) is Promotion.PROMOTE
    ack = extract_candidates(_msg("ok continue"), _ctx(), [opp])
    assert ack[0].kind is CandidateKind.ACKNOWLEDGEMENT
    assert promotion_decision(ack[0], [opp], now=NOW) is Promotion.DISCARD


def test_interrupt_and_free_text_and_preference() -> None:
    opp = _opp(alternatives=(), family="needs_human", question_text="How should I proceed?")
    assert extract_candidates(_msg("wait, stop"), _ctx(), [opp])[0].kind is CandidateKind.INTERRUPT
    free = extract_candidates(_msg("I think the retry logic is still racy under load"), _ctx(), [opp])
    assert free[0].kind is CandidateKind.FREE_TEXT
    assert free[0].opportunity_id is None
    assert promotion_decision(free[0], [opp], now=NOW) is Promotion.PENDING_REVIEW
    pref = extract_candidates(_msg("I prefer pytest over unittest"), _ctx(), [])
    assert pref[0].kind is CandidateKind.PREFERENCE
    assert pref[0].selected_option == "pytest"
    assert pref[0].observed_alternatives == ("pytest", "unittest")
    assert promotion_decision(pref[0], [], now=NOW) is Promotion.PENDING_REVIEW
    assert extract_candidates(_msg("the weather is nice today"), _ctx(), []) == []


# 13. make_receipt
def test_make_receipt_redacts_caps_and_hashes_original() -> None:
    scope = CaptureScope(workspace_id="personal", owner_id="human-1", subject_user_id="human-1", project_id="ote")
    raw = "api_key=abc123 please use it " + ("x" * 50)
    receipt = make_receipt(raw, scope, OriginKind.HUMAN_INPUT, datetime(2026, 9, 9, 12, 0), host_session_id="sess-1", prompt_id="p1", max_chars=40)
    assert receipt.content_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert "abc123" not in receipt.content
    assert "<REDACTED:API_KEY>" in receipt.content
    assert len(receipt.content) == 40
    assert receipt.content_truncated is True
    assert receipt.original_char_count == len(raw)
    assert "api_key" in receipt.redaction_applied
    assert receipt.observed_at.tzinfo is not None
    assert receipt.schema_version == CAPTURE_SCHEMA_VERSION
    assert receipt.delivery_key == compute_delivery_key("sess-1", "p1", receipt.content_sha256)
    again = make_receipt(raw, scope, OriginKind.HUMAN_INPUT, datetime(2026, 9, 9, 12, 0, tzinfo=UTC), host_session_id="sess-1", prompt_id="p1", max_chars=40)
    assert again.delivery_key == receipt.delivery_key
    other = make_receipt(raw, scope, OriginKind.HUMAN_INPUT, datetime(2026, 9, 9, 12, 0, tzinfo=UTC), host_session_id="sess-1", prompt_id="p2", max_chars=40)
    assert other.delivery_key != receipt.delivery_key
    short = make_receipt("hello", scope, OriginKind.HUMAN_INPUT, NOW, host_session_id="sess-1", sequence=3)
    assert short.content_truncated is False
    assert short.delivery_key == compute_delivery_key("sess-1", "3", short.content_sha256)


# 14. helper truth tables
@pytest.mark.parametrize(
    ("prediction_at", "answer_at", "expected"),
    [
        (None, NOW, True),
        (NOW, None, True),
        (NOW, NOW, True),
        (NOW + timedelta(seconds=1), NOW, True),
        (NOW - timedelta(seconds=1), NOW, False),
    ],
)
def test_is_retrospective(prediction_at: datetime | None, answer_at: datetime | None, expected: bool) -> None:
    assert is_retrospective(prediction_at, answer_at) is expected


def test_resolve_prediction_fields() -> None:
    assert resolve_prediction_fields("Confirm", False, " confirm ") == {"correct": True}
    assert resolve_prediction_fields("confirm", False, "abort") == {"correct": False}
    assert resolve_prediction_fields("confirm", True, "confirm") == {"correct": None}
    assert resolve_prediction_fields(None, False, "confirm") == {"correct": None}


def test_evidence_revision_is_order_independent() -> None:
    rows_a = [{"id": "b", "ts": NOW - timedelta(days=1)}, {"id": "a", "ts": NOW}]
    rows_b = [{"id": "a", "ts": NOW.isoformat()}, {"id": "b", "ts": (NOW - timedelta(days=1)).isoformat()}]
    rev_a, ts_a = evidence_revision(rows_a)
    rev_b, ts_b = evidence_revision(rows_b)
    assert rev_a == rev_b
    assert ts_a == ts_b == NOW
    assert len(rev_a) == 32
    assert evidence_revision([]) == ("empty", None)
    assert evidence_revision([{"id": "c", "ts": NOW}])[0] != rev_a


def test_helpers() -> None:
    assert match_alternatives("don't confirm, abort it", ["confirm", "abort"]) == [("confirm", True), ("abort", False)]
    assert match_alternatives("nothing here", ["confirm", "abort"]) == []
    assert match_alternatives("confirmation pending", ["confirm"]) == []
    assert map_ack_to_alternative("yes", ["confirm", "abort"]) == "confirm"
    assert map_ack_to_alternative("go ahead", ["proceed", "abort"]) == "proceed"
    assert map_ack_to_alternative("nope", ["confirm", "abort"]) == "abort"
    assert map_ack_to_alternative("yes", ["red", "blue"]) is None
    assert map_ack_to_alternative("yes", []) is None
    assert is_concrete_instruction("fix the flaky retry test now") is True
    assert is_concrete_instruction("new objective: ship the capture hook") is True
    assert is_concrete_instruction("fix it") is False
    assert is_concrete_instruction("the weather is nice today") is False
    assert opportunity_expired(_opp(expires_at=NOW - timedelta(seconds=1)), NOW) is True
    assert opportunity_expired(_opp(expires_at=None), NOW) is False
    assert opportunity_expired(_opp(), NOW) is False


# --------------------------------------------------------------------------- review defects (P1 review)

_OPEN_ENDED_QUESTION = "How should I proceed?"


def _resolved(oid: str, alternatives: tuple[str, ...], question_text: str = SAFETY_QUESTION) -> DecisionOpportunity:
    family = "safety_confirmation" if alternatives else "needs_human"
    return _opp(oid, alternatives=alternatives, status="resolved", resolved_at=NOW - timedelta(minutes=2), question_text=question_text, family=family)


# 15. R5 must never outrank a live question (defect decision_capture.py:604)
def test_correction_keyword_answering_an_open_question_is_an_answer_not_a_correction() -> None:
    r1 = _resolved("opp-r1", ("confirm", "abort"))
    o2 = _opp("opp-o2")
    cands = extract_candidates(_msg("actually, abort"), _ctx(recently_resolved=[r1]), [o2])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind is CandidateKind.ANSWER
    assert c.is_correction is False
    assert c.opportunity_id == "opp-o2"
    assert c.selected_option == "abort"
    assert promotion_decision(c, [o2], now=NOW) is Promotion.PROMOTE


# 16. an open-ended resolved question is only correctable by a concrete instruction (defect decision_capture.py:607)
@pytest.mark.parametrize(
    ("text", "expected_kinds"),
    [
        ("actually the tests pass now", []),
        ("use pytest instead", []),
        ("I meant the other file", []),
        ("fix the flaky retry test in tests/unit instead", [CandidateKind.CORRECTION]),
    ],
    ids=["chatter", "tool-aside", "vague-reference", "concrete-instruction"],
)
def test_correction_against_open_ended_resolution_requires_a_concrete_instruction(text: str, expected_kinds: list[CandidateKind]) -> None:
    resolved = _resolved("opp-r", (), question_text=_OPEN_ENDED_QUESTION)
    cands = extract_candidates(_msg(text), _ctx(recently_resolved=[resolved]), [])
    assert [c.kind for c in cands] == expected_kinds
    for c in cands:
        # even the concrete instruction is not an auditable revision of a question that had no alternatives
        assert promotion_decision_detail(c, [], now=NOW) == (Promotion.PENDING_REVIEW, "correction_without_alternative_match")


# 17. an answer captured before its question was frozen is never the explicit answer (defect decision_capture.py:700)
def test_answer_observed_before_the_question_was_frozen_is_held_for_review() -> None:
    late = _opp("opp-late", frozen_at=NOW + timedelta(seconds=2))
    cands = extract_candidates(_msg("beru take over and confirm the deploy config values"), _ctx(), [late])
    assert [c.kind for c in cands] == [CandidateKind.ANSWER]
    assert promotion_decision_detail(cands[0], [late], now=NOW) == (Promotion.PENDING_REVIEW, ANSWER_PRECEDES_QUESTION)
    in_time = _opp("opp-late", frozen_at=NOW - timedelta(seconds=2))
    assert promotion_decision(cands[0], [in_time], now=NOW) is Promotion.PROMOTE


def test_promotion_reasons_are_stable() -> None:
    opp = _opp()
    answer = extract_candidates(_msg("confirm"), _ctx(), [opp])[0]
    assert promotion_decision_detail(answer, [opp], now=NOW) == (Promotion.PROMOTE, "unique_open_opportunity")
    assert promotion_decision_detail(answer, [], now=NOW) == (Promotion.PENDING_REVIEW, "ambiguous_or_expired_target")
    correction = extract_candidates(_msg("actually, go with the broad refactor"), _ctx(recently_resolved=[_resolved("opp-r", ("minimal verified fix", "broad refactor"))]), [])[0]
    assert promotion_decision_detail(correction, [], now=NOW) == (Promotion.PROMOTE, "correction")


# 18. the forged-consent trace: a prompt POSTed before takeover_step freezes the safety question
def test_forged_consent_prompt_captured_before_the_freeze_never_promotes() -> None:
    """T1: the host POSTs 'confirm'. T2 > T1: takeover_step freezes the safety question with the same alternatives.

    Same workspace, same subject, matching alternative — the only thing that separates this from real consent is
    the ordering, so the ordering is what has to decide it.
    """

    t1 = NOW
    t2 = NOW + timedelta(seconds=90)
    scope = CaptureScope(workspace_id="personal", owner_id="human-1", subject_user_id="human-1", project_id="ote")
    receipt = make_receipt("confirm", scope, OriginKind.HUMAN_INPUT, t1, host_session_id="sess-1", prompt_id="p1")
    frozen_later = _opp("opp-forged", ("confirm", "abort"), frozen_at=t2, expires_at=t2 + timedelta(hours=1))

    message = {"content": receipt.content, "origin_kind": receipt.origin_kind.value, "observed_at": receipt.observed_at, "project_id": scope.project_id, "task_id": None}
    cands = extract_candidates(message, {"now": receipt.observed_at}, [frozen_later])
    assert [c.kind for c in cands] == [CandidateKind.ANSWER]
    assert cands[0].selected_option == "confirm"
    assert cands[0].opportunity_id == "opp-forged"

    # judged at the answer moment (the receipt's own clock), not wall clock
    assert promotion_decision_detail(cands[0], [frozen_later], now=receipt.observed_at) == (Promotion.PENDING_REVIEW, ANSWER_PRECEDES_QUESTION)
    # and a genuine answer to the same question — same text, spoken after the freeze — still promotes
    assert promotion_decision_detail(cands[0], [frozen_later], now=t2 + timedelta(seconds=1)) == (Promotion.PROMOTE, "unique_open_opportunity")
