"""The P6 shared foundation: the episode key, the enrolment resolution, and the two claims.

The gates asserted here (p6_design.md §8.1): G2's pure-logic half (an arm is frozen and a repeat
enrolment reuses it), G4 (nothing deletes or rewrites an allocation or an adjudication), G8's
type half (``ClaimBVerdict`` has no ``supported``), G9's emitter half (the REDUCED SUPERVISION
sentence travels with the verdict), G10 (P6 forks none of P5's dream vocabulary) and G14 (no
migration drops a table holding a human answer).
"""

from __future__ import annotations

import ast
import dataclasses
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tce_shared import pilot_enrollment
from tce_shared.pilot_enrollment import (
    EXCLUSION_KEYS,
    NOT_COMPUTABLE_REASONS,
    RANDOMIZED_ARMS,
    REDUCED_SUPERVISION_SENTENCE,
    SINGLE_PARTICIPANT_SENTENCE,
    AllocationKind,
    ClaimBState,
    ClauseState,
    CloseFacts,
    CompletionBasis,
    EnrolmentRequest,
    EpisodeFacts,
    PilotArm,
    RescueLevel,
    ReviewVerdict,
    allocate_arm,
    claim_a,
    claim_b,
    clause_not_computable,
    compute_episode_key,
    cost_clause,
    episode_success,
    false_positive_relevance,
    human_intervention_clause,
    resolve_enrolment,
    stratum_id_for,
    validate_decision_family,
    worst_cell,
)
from tce_shared.pilot_thresholds import P6_THRESHOLDS_EFFECTIVE_AT
from tce_shared.policy_evaluation import episode_key as p4_episode_key

REPO = Path(__file__).resolve().parents[2]
SALT = "tce-pilot-p6-v1"
AFTER = P6_THRESHOLDS_EFFECTIVE_AT + timedelta(days=1)

P6_TABLES = (
    "pilot_strata",
    "pilot_episodes",
    "pilot_episode_closes",
    "pilot_episode_observations",
    "dream_relevance_adjudications",
)


def _request(**overrides: object) -> EnrolmentRequest:
    base: dict[str, object] = {
        "workspace_id": "personal",
        "subject_user_id": "owner",
        "project_id": "proj-a",
        "decision_family": "needs_human",
        "session_id": "sess-1",
        "objective_hash": "obj-hash-1",
        "cancel_epoch": 0,
    }
    base.update(overrides)
    return EnrolmentRequest(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# The episode is P4's, computed server-side
# --------------------------------------------------------------------------------------


def test_the_episode_key_is_p4s_unchanged() -> None:
    assert compute_episode_key(
        workspace_id="personal",
        subject_user_id="owner",
        project_id="proj-a",
        session_id="sess-1",
        objective_hash="obj-hash-1",
        cancel_epoch=0,
    ) == p4_episode_key(
        workspace_id="personal",
        subject_user_id="owner",
        project_id="proj-a",
        session_id="sess-1",
        objective_hash="obj-hash-1",
        cancel_epoch=0,
    )


def test_no_caller_supplied_episode_key_or_arm_exists_on_the_request() -> None:
    """The enrolment route never accepts an episode key or an arm.  Both are server-derived."""

    fields = {field.name for field in dataclasses.fields(EnrolmentRequest)}
    assert "episode_key" not in fields
    assert "arm_id" not in fields
    assert "stratum_id" not in fields
    assert "slot" not in fields


def test_changing_the_objective_is_a_different_episode() -> None:
    original = resolve_enrolment(_request(), existing=None, next_slot=0, salt=SALT)
    reworded = resolve_enrolment(_request(objective_hash="obj-hash-2"), existing=None, next_slot=1, salt=SALT)
    assert original.episode_key != reworded.episode_key


def test_a_cancel_and_rerun_is_a_different_episode() -> None:
    first = resolve_enrolment(_request(), existing=None, next_slot=0, salt=SALT)
    rerun = resolve_enrolment(_request(cancel_epoch=1), existing=None, next_slot=1, salt=SALT)
    assert first.episode_key != rerun.episode_key


# --------------------------------------------------------------------------------------
# G2 (pure half): the arm is frozen, and re-enrolling the same work reuses it
# --------------------------------------------------------------------------------------


def test_re_enrolling_the_same_work_returns_the_original_arm_and_draws_nothing() -> None:
    first = resolve_enrolment(_request(), existing=None, next_slot=0, salt=SALT)
    assert first.reused is False and first.slot_consumed is True

    again = resolve_enrolment(_request(), existing=first.allocation, next_slot=None, salt=SALT)
    assert again.reused is True
    assert again.slot_consumed is False, "a repeat enrolment must not consume a stratum slot"
    assert again.episode_key == first.episode_key
    assert again.allocation.arm_id is first.allocation.arm_id
    assert again.allocation.slot == first.allocation.slot


def test_a_repeat_enrolment_never_redraws_even_when_a_slot_is_offered() -> None:
    """Arm shopping has no analogue here: the reuse branch ignores a fresh slot entirely."""

    first = resolve_enrolment(_request(), existing=None, next_slot=0, salt=SALT)
    tempting_slot = next(
        slot
        for slot in range(1, 12)
        if allocate_arm(stratum_id=first.stratum_id, slot=slot, salt=SALT).arm_id is not first.allocation.arm_id
    )
    again = resolve_enrolment(_request(), existing=first.allocation, next_slot=tempting_slot, salt=SALT)
    assert again.allocation.arm_id is first.allocation.arm_id


def test_a_randomized_enrolment_without_a_slot_is_refused() -> None:
    with pytest.raises(ValueError, match="atomic counter"):
        resolve_enrolment(_request(), existing=None, next_slot=None, salt=SALT)


def test_election_is_gated_on_the_setting_and_consumes_no_slot() -> None:
    request = _request(elect_arm=PilotArm.OWNER_UNASSISTED)
    with pytest.raises(ValueError, match="pilot_human_baseline_enabled"):
        resolve_enrolment(request, existing=None, next_slot=None, salt=SALT)

    outcome = resolve_enrolment(request, existing=None, next_slot=None, salt=SALT, human_baseline_enabled=True)
    assert outcome.allocation.allocation_kind is AllocationKind.ELECTED
    assert outcome.slot_consumed is False


def test_an_unknown_decision_family_is_refused_rather_than_bucketed() -> None:
    with pytest.raises(ValueError, match="decision_family"):
        validate_decision_family("something_new")
    assert validate_decision_family("unclassified") == "unclassified"


def test_two_families_land_in_different_strata() -> None:
    left = stratum_id_for(workspace_id="w", subject_user_id="s", project_id="p", decision_family="needs_human")
    right = stratum_id_for(workspace_id="w", subject_user_id="s", project_id="p", decision_family="next_objective")
    assert left != right


# --------------------------------------------------------------------------------------
# Clauses: NOT COMPUTABLE is never a pass and never a zero
# --------------------------------------------------------------------------------------


def test_a_not_computable_reason_must_come_from_the_closed_list() -> None:
    with pytest.raises(ValueError, match="closed NOT_COMPUTABLE reasons"):
        clause_not_computable("cost", "because_i_said_so")


def test_the_empty_corpus_is_legible_and_supports_nothing() -> None:
    verdict = claim_a([], project_id="proj-a", decision_family="needs_human", now=AFTER)
    assert verdict.supported is False
    assert verdict.shortfalls, "an empty corpus must produce shortfalls, not silence"
    assert verdict.enrolled == 0 and verdict.closed == 0
    assert set(verdict.exclusions) == set(EXCLUSION_KEYS), "the ledger is never conditional"
    for clause in verdict.clauses:
        if clause.state is ClauseState.NOT_COMPUTABLE:
            assert clause.reason in NOT_COMPUTABLE_REASONS
        assert "PASS" not in clause.render() or clause.state is ClauseState.PASS


def test_no_clause_renders_a_zero_denominator_as_a_measurement() -> None:
    verdict = claim_a([], project_id="proj-a", decision_family="needs_human", now=AFTER)
    by_name = {clause.name: clause for clause in verdict.clauses}
    assert by_name["close_coverage"].state is ClauseState.NOT_COMPUTABLE
    assert by_name["close_coverage"].measured is None
    assert by_name["deviation_rate"].state is ClauseState.NOT_COMPUTABLE
    assert cost_clause(cost_known=0, episodes=0).state is ClauseState.NOT_COMPUTABLE
    assert cost_clause(cost_known=0, episodes=5).reason == "all_surfaces_unsupported"
    assert human_intervention_clause(known=0, episodes=0).state is ClauseState.NOT_COMPUTABLE


def test_a_shortfall_carries_the_measured_value_and_the_floor_in_that_order() -> None:
    verdict = claim_a(_matched_corpus(blocks=1), now=AFTER)
    rendered = {clause.name: clause.render() for clause in verdict.clauses}
    assert rendered["closed_per_arm"] == "closed_per_arm: SHORTFALL  closed_per_arm 1 < 30"


def test_pooling_across_cells_is_refused_rather_than_averaged() -> None:
    mixed = [*_matched_corpus(blocks=1), *_matched_corpus(blocks=1, family="next_objective")]
    with pytest.raises(ValueError, match="ONE \\(project, decision_family\\) cell"):
        claim_a(mixed, now=AFTER)


# --------------------------------------------------------------------------------------
# The success composite reads no agent assertion
# --------------------------------------------------------------------------------------


def _close(
    *,
    basis: CompletionBasis = CompletionBasis.TASK_STATE_DONE,
    rescue: RescueLevel = RescueLevel.NONE,
    verdict: ReviewVerdict = ReviewVerdict.ACCEPTED_AS_IS,
    arm: PilotArm = PilotArm.TCE_ASSISTED,
    independent: bool = True,
    deviated: bool = False,
) -> CloseFacts:
    return CloseFacts(
        adjudication_independent=independent,
        executed_arm=arm,
        deviated=deviated,
        rescue_level=rescue,
        completion_basis=basis,
        review_verdict=verdict,
        closed_at=AFTER,
        review_minutes=12,
    )


def test_success_needs_all_three_components() -> None:
    assert episode_success(_close()) is True
    assert episode_success(_close(basis=CompletionBasis.OWNER_ATTESTED)) is False
    assert episode_success(_close(rescue=RescueLevel.TOOK_OVER)) is False
    assert episode_success(_close(verdict=ReviewVerdict.REJECTED)) is False
    assert episode_success(_close(basis=CompletionBasis.VERIFIED)) is True


def test_no_clause_function_reads_an_agent_asserted_field() -> None:
    """G5's shared half: the fields exist in their own table, and no type here can hold them."""

    module = ast.parse(Path(pilot_enrollment.__file__).read_text(encoding="utf-8"))
    close_fields = {field.name for field in dataclasses.fields(CloseFacts)}
    episode_fields = {field.name for field in dataclasses.fields(EpisodeFacts)}
    forbidden = {"agent_notes", "agent_declared_steps", "agent_self_rated_difficulty", "agent_confidence"}
    assert not (close_fields | episode_fields) & forbidden

    docstrings = {
        node.body[0].value.value
        for node in ast.walk(module)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(module):
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden, node.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings:
            assert "pilot_episode_observations" not in node.value


# --------------------------------------------------------------------------------------
# Claim A on a real (tiny) matched corpus
# --------------------------------------------------------------------------------------


def _matched_corpus(
    *,
    blocks: int,
    family: str = "needs_human",
    project: str = "proj-a",
    treatment_succeeds: bool = True,
    baseline_succeeds: bool = False,
    allocated_at: datetime | None = None,
) -> list[EpisodeFacts]:
    stamp = allocated_at or AFTER
    episodes: list[EpisodeFacts] = []
    stratum = stratum_id_for(workspace_id="personal", subject_user_id="owner", project_id=project, decision_family=family)
    for block in range(blocks):
        for position in range(len(RANDOMIZED_ARMS)):
            slot = block * len(RANDOMIZED_ARMS) + position
            allocation = allocate_arm(stratum_id=stratum, slot=slot, salt=SALT)
            wins = treatment_succeeds if allocation.arm_id is PilotArm.TCE_ASSISTED else baseline_succeeds
            episodes.append(
                EpisodeFacts(
                    episode_id=f"{family}-{slot}",
                    project_id=project,
                    decision_family=family,
                    arm_id=allocation.arm_id,
                    arm_class=allocation.arm_class,
                    allocation_kind=allocation.allocation_kind,
                    stratum_id=allocation.stratum_id,
                    slot=allocation.slot,
                    block_ordinal=allocation.block_ordinal,
                    allocation_salt_sha256=allocation.allocation_salt_sha256,
                    arm_set_sha=allocation.arm_set_sha,
                    allocated_at=stamp + timedelta(days=block),
                    close=_close(
                        arm=allocation.arm_id,
                        basis=CompletionBasis.TASK_STATE_DONE if wins else CompletionBasis.UNFINISHED,
                    ),
                )
            )
    return episodes


def test_one_closed_block_moves_the_ledger_and_no_verdict() -> None:
    verdict = claim_a(_matched_corpus(blocks=1), now=AFTER)
    assert verdict.closed == 3
    assert verdict.complete_blocks == 1
    assert verdict.supported is False
    assert "closed_per_arm 1 < 30" in verdict.shortfalls
    assert "matched_blocks 1 < 30" in verdict.shortfalls


def test_the_gate_is_not_rigged_to_always_refuse() -> None:
    """Non-vacuity in the other direction: a corpus that genuinely clears every floor passes.

    A gate that can only ever say NOT ENOUGH EVIDENCE proves nothing about the evidence.  Fifty
    complete blocks, thirty distinct enrolment days and a clean separation clear all nine
    clauses — so when today's corpus reads NOT ENOUGH EVIDENCE, that is a fact about the corpus.
    """

    verdict = claim_a(_matched_corpus(blocks=50), now=AFTER + timedelta(days=60))
    assert verdict.closed_by_arm[PilotArm.TCE_ASSISTED.value] == 50
    assert verdict.complete_blocks == 50
    assert verdict.shortfalls == ()
    assert verdict.supported is True


def test_a_short_window_is_a_shortfall_however_many_episodes_it_holds() -> None:
    """Fifty blocks crammed into three days still fails the active-day clause."""

    episodes = [dataclasses.replace(episode, allocated_at=AFTER) for episode in _matched_corpus(blocks=50)]
    verdict = claim_a(episodes, now=AFTER + timedelta(days=60))
    assert verdict.active_days == 1
    assert verdict.supported is False
    assert "active_days 1 < 28" in verdict.shortfalls


def test_an_unclosed_episode_is_excluded_and_counted() -> None:
    episodes = _matched_corpus(blocks=1)
    episodes[0] = dataclasses.replace(episodes[0], close=None)
    verdict = claim_a(episodes, now=AFTER)
    assert verdict.exclusions["unclosed"] == 1
    assert verdict.exclusions["blocks_incomplete"] == 2
    assert verdict.complete_blocks == 0


def test_a_self_adjudicated_close_is_excluded_from_every_numerator() -> None:
    episodes = _matched_corpus(blocks=1)
    episodes[0] = dataclasses.replace(episodes[0], close=_close(arm=episodes[0].arm_id, independent=False))
    verdict = claim_a(episodes, now=AFTER)
    assert verdict.exclusions["adjudicator_not_independent"] == 1
    assert verdict.closed == 2


def test_a_pre_registration_episode_is_excluded_and_counted() -> None:
    episodes = _matched_corpus(blocks=1, allocated_at=P6_THRESHOLDS_EFFECTIVE_AT - timedelta(days=3))
    verdict = claim_a(episodes, now=AFTER)
    assert verdict.exclusions["pre_registration"] == 3
    assert verdict.enrolled == 0


def test_a_salt_change_mid_pilot_is_a_shortfall_not_a_pooled_average() -> None:
    episodes = _matched_corpus(blocks=2)
    episodes[-1] = dataclasses.replace(episodes[-1], allocation_salt_sha256="0" * 32)
    verdict = claim_a(episodes, now=AFTER)
    assert verdict.exclusions["salt_mismatch"] == 1
    assert any("salt_changed_mid_pilot" in shortfall for shortfall in verdict.shortfalls)


def test_a_deviated_cell_refuses_the_paired_test_rather_than_reporting_as_treated() -> None:
    episodes = _matched_corpus(blocks=2)
    episodes = [
        dataclasses.replace(
            episode,
            close=_close(arm=PilotArm.OWNER_UNASSISTED, deviated=True) if index < 4 else episode.close,
        )
        for index, episode in enumerate(episodes)
    ]
    verdict = claim_a(episodes, now=AFTER)
    by_name = {clause.name: clause for clause in verdict.clauses}
    assert by_name["deviation_rate"].state is ClauseState.SHORTFALL
    assert by_name["paired_lift_vs_existing_runtime"].reason == "deviation_rate_exceeded"


def test_no_discordant_pairs_is_not_computable_rather_than_a_zero_lift() -> None:
    verdict = claim_a(_matched_corpus(blocks=4, treatment_succeeds=True, baseline_succeeds=True), now=AFTER)
    by_name = {clause.name: clause for clause in verdict.clauses}
    assert by_name["paired_lift_vs_existing_runtime"].reason == "no_discordant_pairs"


def test_an_arm_with_no_closed_episode_is_named_rather_than_averaged_over() -> None:
    episodes = [episode for episode in _matched_corpus(blocks=2) if episode.arm_id is not PilotArm.MARKDOWN_HANDOFF]
    verdict = claim_a(episodes, now=AFTER, arm_runnable={"markdown_handoff": False})
    by_name = {clause.name: clause for clause in verdict.clauses}
    assert by_name["arm_runnable"].reason == "arm_not_runnable_on_host"
    assert "markdown_handoff" in by_name["arm_runnable"].detail


def test_the_summary_names_the_worst_cell_never_a_mean() -> None:
    strong = claim_a(_matched_corpus(blocks=4), now=AFTER)
    weak = claim_a([], project_id="proj-b", decision_family="next_objective", now=AFTER)
    assert worst_cell([strong, weak]) is weak
    assert worst_cell([]) is None


# --------------------------------------------------------------------------------------
# G8 / G9: Claim B cannot be reported as supported, by construction
# --------------------------------------------------------------------------------------


def test_claim_b_has_nowhere_to_put_a_supported_flag() -> None:
    verdict = claim_b([])
    assert "supported" not in {field.name for field in dataclasses.fields(verdict)}
    assert not hasattr(verdict, "supported")
    assert "supported" not in verdict.to_payload()
    assert set(ClaimBState) == {ClaimBState.NOT_COMPUTABLE, ClaimBState.DESCRIPTIVE_ONLY}


def test_claim_b_emits_the_reduced_supervision_sentence_on_an_empty_corpus() -> None:
    verdict = claim_b([])
    assert verdict.state is ClaimBState.NOT_COMPUTABLE
    assert verdict.sentence == REDUCED_SUPERVISION_SENTENCE
    assert verdict.to_payload()["sentence"] == REDUCED_SUPERVISION_SENTENCE


def test_claim_b_still_says_reduced_supervision_when_human_rows_were_elected() -> None:
    """An elected human arm is descriptive, never causal.  The sentence does not go away."""

    stratum = stratum_id_for(workspace_id="personal", subject_user_id="owner", project_id="proj-a", decision_family="needs_human")
    elected = [
        EpisodeFacts(
            episode_id=f"human-{index}",
            project_id="proj-a",
            decision_family="needs_human",
            arm_id=PilotArm.OWNER_UNASSISTED,
            arm_class="human_workflow",
            allocation_kind=AllocationKind.ELECTED,
            stratum_id=stratum,
            slot=-1,
            block_ordinal=-1,
            allocation_salt_sha256="a" * 32,
            arm_set_sha="b" * 32,
            allocated_at=AFTER,
            close=_close(arm=PilotArm.OWNER_UNASSISTED),
        )
        for index in range(5)
    ]
    verdict = claim_b(elected)
    assert verdict.state is ClaimBState.NOT_COMPUTABLE
    assert verdict.elected == 5 and verdict.randomized == 0
    assert verdict.sentence == REDUCED_SUPERVISION_SENTENCE
    # and Claim A never sees them
    assert claim_a(elected, now=AFTER).exclusions["elected"] == 5


def test_the_single_participant_sentence_is_not_conditional_on_the_verdict() -> None:
    assert "generalises to nobody else" in SINGLE_PARTICIPANT_SENTENCE


# --------------------------------------------------------------------------------------
# Dreams: relevance is adjudicated, never derived from a rejection
# --------------------------------------------------------------------------------------


def test_false_positive_relevance_needs_an_adjudication() -> None:
    value, clause = false_positive_relevance(relevant=0, not_relevant=0, cannot_judge=3)
    assert value is None
    assert clause.reason == "no_adjudications"
    value, clause = false_positive_relevance(relevant=0, not_relevant=1)
    assert value == 1.0 and clause.state is ClauseState.PASS


def test_cannot_judge_is_in_neither_numerator_nor_denominator() -> None:
    value, _ = false_positive_relevance(relevant=1, not_relevant=1, cannot_judge=98)
    assert value == 0.5


# --------------------------------------------------------------------------------------
# G4 / G10 / G14: SQL and migration scanners
# --------------------------------------------------------------------------------------


def _python_sources() -> Iterator[Path]:
    for root in ("shared", "services", "infra/alembic/versions", "scripts"):
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            yield path


def _string_literals(path: Path, *, include_docstrings: bool = True) -> Iterator[str]:
    """Every string constant in the file.

    ``include_docstrings=False`` skips module, class and function docstrings.  The SQL scanners
    below need that: a store whose docstring says "there is no ``DELETE FROM pilot_`` here" is
    *documenting* the rule, not breaking it, and a scanner that cannot tell the two apart forces
    the rule to go undocumented in order to stay green.  ``test_no_clause_function_reads_an_agent_asserted_field``
    already draws this distinction; the SQL scanners now draw it the same way.  Only prose is
    excluded — a docstring is never executed, so no statement is hidden by this.
    """

    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken file is another test's problem
        return
    docstrings: set[int] = set()
    if not include_docstrings:
        for node in ast.walk(module):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


def test_no_delete_no_arm_update() -> None:
    """G4.  An allocation and an adjudication are append-only, in the SQL as well as in the prose."""

    forbidden = (
        re.compile(r"DELETE\s+FROM\s+pilot_", re.IGNORECASE),
        re.compile(r"DELETE\s+FROM\s+dream_relevance_", re.IGNORECASE),
        re.compile(r"UPDATE\s+pilot_episodes[\s\S]*\barm_id\b\s*=", re.IGNORECASE),
    )
    drop_table = re.compile(r"DROP\s+TABLE[^;]*\b(" + "|".join(P6_TABLES) + r")\b", re.IGNORECASE)
    offences: list[str] = []
    scanned = 0
    for path in _python_sources():
        for literal in _string_literals(path, include_docstrings=False):
            scanned += 1
            for pattern in forbidden:
                if pattern.search(literal):
                    offences.append(f"{path.relative_to(REPO)}: {pattern.pattern}")
            if drop_table.search(literal):
                offences.append(f"{path.relative_to(REPO)}: DROP TABLE of a P6 table")
    assert not offences, offences

    # Non-vacuity: the scan read a real corpus, and every pattern in it can still see a violation.
    assert scanned > 1000, f"only {scanned} string literals were scanned; the walk is not reaching the tree"
    assert forbidden[0].search("DELETE FROM pilot_episodes WHERE id = %s")
    assert forbidden[1].search("delete from dream_relevance_adjudications")
    assert forbidden[2].search("UPDATE pilot_episodes SET arm_id = %s WHERE id = %s")
    assert drop_table.search("DROP TABLE pilot_episode_closes")


def test_the_lock_order_is_strata_before_episodes() -> None:
    """G4's other half.  The one deadlock P6 could add, closed by an AST walk rather than by prose.

    ``pilot_strata`` is a single hot row per cell and ``pilot_episodes`` is the row that follows
    it, so a function that touches them in the reverse order — taking the slot only after
    inserting the episode — can interleave with a concurrent enrolment and deadlock.  Builder A
    could not implement this half: both ``pilot_store.py`` files were absent and it would have
    asserted over nothing.  They exist now, so it asserts over them.

    The two writes live in two helpers (``take_stratum_slot`` and the episode INSERT), so the
    walk resolves each helper to the table it writes and then checks the ORDER OF CALLS in every
    function that reaches both.  Checking SQL literals per function would see nothing at all.
    """

    stores = [
        REPO / "services" / "tce_api" / "tce_api" / "pilot_store.py",
        REPO / "services" / "tce_lite_api" / "tce_lite_api" / "pilot_store.py",
    ]
    present = [path for path in stores if path.exists()]
    assert len(present) == 2, f"expected both pilot stores; found {[str(path) for path in present]}"

    # ``INSERT OR IGNORE`` / ``INSERT OR REPLACE`` are SQLite spellings of the same write; a
    # regex that only knew ``INSERT INTO`` would silently see nothing in the Lite store.
    insert = r"INSERT(\s+OR\s+\w+)?\s+INTO"
    strata_write = re.compile(rf"({insert}|UPDATE)\s+pilot_strata", re.IGNORECASE)
    episode_write = re.compile(rf"{insert}\s+pilot_episodes", re.IGNORECASE)
    offences: list[str] = []
    checked = 0

    for path in present:
        module = ast.parse(path.read_text(encoding="utf-8"))
        functions = [node for node in ast.walk(module) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]

        # Which helper writes which table, resolved from its own SQL rather than from its name.
        writes: dict[str, set[str]] = {}
        for node in functions:
            literals = [inner.value for inner in ast.walk(node) if isinstance(inner, ast.Constant) and isinstance(inner.value, str)]
            kinds = set()
            if any(strata_write.search(literal) for literal in literals):
                kinds.add("strata")
            if any(episode_write.search(literal) for literal in literals):
                kinds.add("episodes")
            if kinds:
                writes[node.name] = kinds
        assert writes, f"{path.relative_to(REPO)} writes neither table; the walk would police nothing"

        for node in functions:
            touches: list[tuple[int, str]] = []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    if strata_write.search(inner.value):
                        touches.append((inner.lineno, "strata"))
                    if episode_write.search(inner.value):
                        touches.append((inner.lineno, "episodes"))
                elif isinstance(inner, ast.Call):
                    called = inner.func.id if isinstance(inner.func, ast.Name) else inner.func.attr if isinstance(inner.func, ast.Attribute) else ""
                    if called == node.name:
                        continue
                    for kind in sorted(writes.get(called, ())):
                        touches.append((inner.lineno, kind))
            kinds = {kind for _, kind in touches}
            if kinds != {"strata", "episodes"}:
                continue
            checked += 1
            first_strata = min(line for line, kind in touches if kind == "strata")
            first_episode = min(line for line, kind in touches if kind == "episodes")
            if first_episode < first_strata:
                offences.append(
                    f"{path.relative_to(REPO)}:{node.name} reaches pilot_episodes (line {first_episode}) "
                    f"before pilot_strata (line {first_strata})"
                )
    assert not offences, offences

    # Non-vacuity: at least one function per backend reaches both, so the ordering rule is
    # actually being applied to something.
    assert checked >= 2, (
        f"only {checked} function(s) reach both tables; if the enrolment write moved out of a single "
        "function this walk no longer sees the ordering it exists to police"
    )
    assert episode_write.search("INSERT OR IGNORE INTO pilot_episodes (id) VALUES (?)")
    assert episode_write.search("INSERT INTO pilot_episodes (id) VALUES (%s)")
    assert strata_write.search("INSERT INTO pilot_strata (stratum_id) VALUES (%s) ON CONFLICT DO UPDATE")


def test_no_migration_drops_a_human_answer_table() -> None:
    """G14, extending P5's G16 list with the three P6 tables that are nothing but human answers."""

    protected = (
        # P5's G16 list ...
        "dream_proposals",
        "dream_proposal_events",
        "dream_generation_runs",
        "policy_qualifications",
        # ... extended with the three P6 tables that are nothing but human answers.
        "pilot_episodes",
        "pilot_episode_closes",
        "dream_relevance_adjudications",
    )
    pattern = re.compile(r"DROP\s+TABLE(\s+IF\s+EXISTS)?\s+(" + "|".join(protected) + r")\b", re.IGNORECASE)
    offences = [
        str(path.relative_to(REPO))
        for path in (REPO / "infra" / "alembic" / "versions").rglob("*.py")
        for literal in _string_literals(path)
        if pattern.search(literal)
    ]
    assert not offences, offences
    # Non-vacuity: a scan that finds nothing is worth something only if it can see a violation.
    assert pattern.search("op.execute('DROP TABLE IF EXISTS pilot_episode_closes')")


def test_p6_adds_no_dream_state() -> None:
    """G10.  P5 already ships the vocabulary; P6 composes with it and forks none of it."""

    from tce_shared.aspirations import DreamProposalStatus, NonresponseState

    module = ast.parse(Path(pilot_enrollment.__file__).read_text(encoding="utf-8"))
    forked = {"DreamProposalStatus", "NonresponseState", "DreamProposalState"}
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            assert node.name not in forked, f"{node.name} is P5's; P6 composes with it and does not redeclare it"
            bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
            assert not bases & forked, f"{node.name} extends P5's vocabulary"
        if isinstance(node, ast.ImportFrom) and node.module == "aspirations":
            raise AssertionError("pilot_enrollment must not depend on P5's dream vocabulary")

    # P6's own values are disjoint from P5's status axis, so no renderer can confuse the two.
    p5_values = {member.value for member in DreamProposalStatus} | {member.value for member in NonresponseState}
    p6_values = {member.value for member in pilot_enrollment.RelevanceVerdict}
    assert not p6_values & p5_values, sorted(p6_values & p5_values)

    migration = next(
        (path for path in (REPO / "infra" / "alembic" / "versions").rglob("*pilot_enrol*.py")),
        None,
    )
    if migration is not None:
        for literal in _string_literals(migration):
            assert not re.search(r"ALTER\s+TABLE\s+dream_proposals", literal, re.IGNORECASE)
            assert not re.search(r"UPDATE\s+dream_proposals", literal, re.IGNORECASE)
