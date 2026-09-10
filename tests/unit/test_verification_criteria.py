"""Acceptance criteria, corpus coverage, and the verdict ladder.

The assertion that matters most is the boring one: an EMPTY criteria set FAILS.  With zero checks
and zero results, a naive ladder falls through the length comparison and an empty loop straight to
"all checks matched" — a passing verification with nothing verified, releasing the task as done.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import UTC, datetime

import pytest
from tce_shared import verification as verification_module
from tce_shared.verification import (
    DECIDING_FILE_PATHS,
    MIN_ACCEPTANCE_CHECKS,
    VERIFICATION_STATE_FOR_VERDICT,
    VERIFICATION_VERDICTS,
    AcceptanceCheck,
    AcceptanceCriteria,
    CheckResult,
    CriteriaInvalid,
    VerificationEvidence,
    VerificationOutcome,
    acceptance_check_from_json,
    acceptance_check_to_json,
    build_corpus_manifest,
    check_result_from_json,
    check_result_to_json,
    corpus_digest,
    corpus_manifest_covers,
    criteria_digest,
    criteria_from_json,
    criteria_to_json,
    decide_verdict,
    evidence_from_json,
    evidence_to_json,
    outcome_from_json,
    outcome_to_json,
    validate_checks,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _check(check_id: str = "test", **overrides: object) -> AcceptanceCheck:
    values: dict[str, object] = {
        "check_id": check_id,
        "argv": ("/usr/bin/env", "pytest", "-q"),
        "cwd_rel": ".",
        "expect_exit_code": 0,
        "timeout_seconds": 900,
    }
    values.update(overrides)
    return AcceptanceCheck(**values)  # type: ignore[arg-type]


def _criteria(checks: tuple[AcceptanceCheck, ...], **overrides: object) -> AcceptanceCriteria:
    manifest = (("tests/unit/x.py", "a" * 64),)
    values: dict[str, object] = {
        "criteria_id": "crit-1",
        "directive_id": "dir-1",
        "checks": checks,
        "criteria_digest": criteria_digest(checks),
        "corpus_digest": corpus_digest(manifest),
        "corpus_manifest": manifest,
        "frozen_at": NOW,
        "frozen_by": "manager",
        "policy_revision": "p3-2026-09",
    }
    values.update(overrides)
    return AcceptanceCriteria(**values)  # type: ignore[arg-type]


def _result(check_id: str = "test", exit_code: int = 0) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        argv=("/usr/bin/env", "pytest", "-q"),
        exit_code=exit_code,
        duration_ms=1200,
        stdout_sha256="b" * 64,
        stderr_sha256="c" * 64,
        excerpt="ok",
    )


# --- digests --------------------------------------------------------------------------------------


def test_criteria_digest_is_order_insensitive_but_content_sensitive() -> None:
    first, second = _check("a"), _check("b")
    assert criteria_digest((first, second)) == criteria_digest((second, first))
    assert criteria_digest((first,)) != criteria_digest((first, second))
    assert criteria_digest((first,)) != criteria_digest((_check("a", expect_exit_code=1),))


def test_corpus_digest_is_order_insensitive_but_content_sensitive() -> None:
    a = ("tests/a.py", "1" * 64)
    b = ("tests/b.py", "2" * 64)
    assert corpus_digest([a, b]) == corpus_digest([b, a])
    assert corpus_digest([a, b]) != corpus_digest([a, ("tests/b.py", "3" * 64)])


def test_build_corpus_manifest_is_deterministic_and_covers_deciding_files(tmp_path: pathlib.Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("assert True\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['.']\n")
    (tmp_path / "conftest.py").write_text("\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_bytes(b"\x00")

    manifest = build_corpus_manifest(str(tmp_path), ["tests/"])
    paths = [path for path, _digest in manifest]
    assert paths == sorted(paths)
    # The deciding files come in even though they are not under a listed prefix ...
    assert "pyproject.toml" in paths
    assert "conftest.py" in paths
    assert "tests/test_a.py" in paths
    # ... and nothing else does.
    assert "src/app.py" not in paths
    assert not any("__pycache__" in path for path in paths)
    assert build_corpus_manifest(str(tmp_path), ["tests/"]) == manifest


def test_corpus_manifest_covers_deciding_files() -> None:
    manifest = [("pyproject.toml", "1" * 64), ("tests/a.py", "2" * 64)]
    ok, missing = corpus_manifest_covers(manifest, ["pyproject.toml"])
    assert (ok, missing) == (True, [])
    ok, missing = corpus_manifest_covers(manifest, ["pyproject.toml", "conftest.py"])
    assert ok is False
    assert missing == ["conftest.py"]


def test_deciding_file_paths_name_the_pytest_configuration() -> None:
    """These are the files that decide what a frozen argv actually executes."""
    assert "pyproject.toml" in DECIDING_FILE_PATHS
    assert "conftest.py" in DECIDING_FILE_PATHS


# --- validate_checks --------------------------------------------------------------------------------


def test_an_empty_check_list_is_refused_at_the_freeze() -> None:
    assert MIN_ACCEPTANCE_CHECKS == 1
    with pytest.raises(CriteriaInvalid) as excinfo:
        validate_checks((), max_checks=8, max_timeout_seconds=900)
    assert excinfo.value.field == "checks"


def test_too_many_checks_are_refused() -> None:
    checks = tuple(_check(f"c{index}") for index in range(9))
    with pytest.raises(CriteriaInvalid):
        validate_checks(checks, max_checks=8, max_timeout_seconds=900)


def test_argv0_must_be_absolute() -> None:
    with pytest.raises(CriteriaInvalid) as excinfo:
        validate_checks((_check(argv=("pytest", "-q")),), max_checks=8, max_timeout_seconds=900)
    assert excinfo.value.field == "argv"


def test_cwd_may_not_escape_the_clone() -> None:
    with pytest.raises(CriteriaInvalid) as excinfo:
        validate_checks((_check(cwd_rel="../.."),), max_checks=8, max_timeout_seconds=900)
    assert excinfo.value.field == "cwd_rel"


def test_duplicate_check_ids_are_refused() -> None:
    with pytest.raises(CriteriaInvalid) as excinfo:
        validate_checks((_check("a"), _check("a")), max_checks=8, max_timeout_seconds=900)
    assert excinfo.value.field == "check_id"


def test_timeouts_are_clamped_not_rejected() -> None:
    (clamped,) = validate_checks((_check(timeout_seconds=99999),), max_checks=8, max_timeout_seconds=900)
    assert clamped.timeout_seconds == 900


# --- decide_verdict ---------------------------------------------------------------------------------


def test_empty_criteria_set_fails() -> None:
    outcome = decide_verdict(
        results=(),
        criteria=_criteria((), criteria_digest=criteria_digest(())),
        observed_corpus_digest=corpus_digest((("tests/unit/x.py", "a" * 64),)),
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "failed"
    assert outcome.reason == "empty_criteria_set"
    assert outcome.criteria_digest_match is False


def test_criteria_digest_match_is_computed_not_constant() -> None:
    criteria = _criteria((_check(),), criteria_digest="not-the-real-digest")
    outcome = decide_verdict(
        results=(_result(),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "failed"
    assert outcome.reason == "criteria_digest_mismatch"
    assert outcome.criteria_digest_match is False


def test_runner_equal_to_executor_is_inconclusive() -> None:
    criteria = _criteria((_check(),))
    outcome = decide_verdict(
        results=(_result(),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="supervisor",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason == "runner_is_executing_identity"


def test_criteria_frozen_by_the_executing_identity_is_inconclusive() -> None:
    """The freezer may not be the party the checks are aimed at.

    The freeze route refuses a freezer that matches the directive's claimant, but it can only see
    the claim state as it stands at FREEZE time, and the order of "freeze" and "claim" is the
    implementing agent's own choice. Measured over HTTP against both backends before this clause
    existed: freeze `/bin/sh -c "exit 0"` while the directive is still unclaimed, then claim it,
    report ``succeeded``, and an honest verifier grades the agent's own goalposts ``passed`` --
    all the way to ``DONE``. Grading is the first moment both facts are settled, so the check has
    to live here, where no ordering reaches it.
    """
    criteria = _criteria((_check(),), frozen_by="codex-executor")
    outcome = decide_verdict(
        results=(_result(),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="codex-executor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason == "criteria_frozen_by_executing_identity"
    assert VERIFICATION_STATE_FOR_VERDICT[outcome.verdict] == "unverified"


def test_an_independent_freezer_still_passes() -> None:
    """The other side of it: the guard discriminates, it is not a constant refusal."""
    criteria = _criteria((_check(),), frozen_by="manager-ui")
    outcome = decide_verdict(
        results=(_result(),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="codex-executor",
    )
    assert outcome.verdict == "passed"


def test_corpus_mismatch_is_a_verification_failure_not_an_error() -> None:
    """It detects weakening.  It does not prevent it, and it is not an exception."""
    criteria = _criteria((_check(),))
    outcome = decide_verdict(
        results=(_result(),),
        criteria=criteria,
        observed_corpus_digest="somebody-edited-pyproject",
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "failed"
    assert outcome.reason == "corpus_digest_mismatch"
    assert outcome.corpus_digest_match is False


def test_a_missing_result_is_inconclusive_not_a_pass() -> None:
    criteria = _criteria((_check("a"), _check("b")))
    outcome = decide_verdict(
        results=(_result("a"),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason == "incomplete_check_set"


def test_n_duplicate_results_do_not_cover_n_frozen_checks() -> None:
    """The laundering case a COUNT check cannot see: three copies of the one check the agent can
    make pass, standing in for three frozen checks.  Coverage is a SET question, not a count."""
    criteria = _criteria((_check("a"), _check("b"), _check("c")))
    outcome = decide_verdict(
        results=(_result("a"), _result("a"), _result("a")),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason == "duplicate_check:a"


def test_a_duplicate_result_is_refused_even_when_every_check_is_covered() -> None:
    """Coverage alone is not enough — a repeated check_id means two results claim to be the same
    evidence, and the grader cannot tell which one it graded."""
    criteria = _criteria((_check("a"), _check("b")))
    outcome = decide_verdict(
        results=(_result("a"), _result("b"), _result("b")),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason == "duplicate_check:b"


def test_coverage_is_the_set_of_frozen_check_ids() -> None:
    criteria = _criteria((_check("a"), _check("b"), _check("c")))
    outcome = decide_verdict(
        results=(_result("a"), _result("c")),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason == "incomplete_check_set"


def test_an_unknown_check_id_is_inconclusive() -> None:
    criteria = _criteria((_check("a"),))
    outcome = decide_verdict(
        results=(_result("z"),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "inconclusive"
    assert outcome.reason.startswith("unknown_check:")


def test_a_nonzero_exit_fails_and_names_the_check() -> None:
    criteria = _criteria((_check("lint"),))
    outcome = decide_verdict(
        results=(_result("lint", exit_code=1),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "failed"
    assert outcome.reason == "check_failed:lint:exit=1"


def test_a_full_matching_evidence_set_passes() -> None:
    criteria = _criteria((_check("build"), _check("test")))
    outcome = decide_verdict(
        results=(_result("build"), _result("test")),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome == VerificationOutcome("passed", "all_checks_matched", True, True)


def test_an_expected_nonzero_exit_code_is_honoured() -> None:
    criteria = _criteria((_check("must_fail", expect_exit_code=1),))
    outcome = decide_verdict(
        results=(_result("must_fail", exit_code=1),),
        criteria=criteria,
        observed_corpus_digest=criteria.corpus_digest,
        runner_principal="system:verifier",
        executing_identity="supervisor",
    )
    assert outcome.verdict == "passed"


def test_verdict_function_mentions_no_reviewer() -> None:
    """An advisory model opinion is recorded beside the verdict, never inside it.  Asserted over the
    function's own source rather than by grepping the module, so an unrelated mention elsewhere
    cannot make this vacuous."""
    source = pathlib.Path(inspect.getsourcefile(verification_module) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    segments = [
        ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "decide_verdict"
    ]
    assert len(segments) == 1
    assert segments[0] is not None
    assert "reviewer" not in segments[0]


def test_verdict_vocabulary_maps_into_the_projection_states() -> None:
    assert set(VERIFICATION_STATE_FOR_VERDICT) == set(VERIFICATION_VERDICTS)
    assert VERIFICATION_STATE_FOR_VERDICT["inconclusive"] == "unverified"
    assert set(VERIFICATION_STATE_FOR_VERDICT.values()) <= {"unverified", "passed", "failed", "skipped"}


# --- codecs -----------------------------------------------------------------------------------------


def test_all_codecs_round_trip() -> None:
    check = _check("build")
    assert acceptance_check_from_json(acceptance_check_to_json(check)) == check
    result = _result("build")
    assert check_result_from_json(check_result_to_json(result)) == result
    criteria = _criteria((check,))
    assert criteria_from_json(criteria_to_json(criteria)) == criteria
    outcome = VerificationOutcome("passed", "all_checks_matched", True, True)
    assert outcome_from_json(outcome_to_json(outcome)) == outcome
    evidence = VerificationEvidence(
        directive_id="dir-1",
        results=(result,),
        observed_corpus_digest=criteria.corpus_digest,
        observed_corpus_manifest=criteria.corpus_manifest,
        platform="darwin/arm64 python3.12.12",
        commit_sha="abc123",
        tree_sha=None,
        reviewer_model={"model": "advisory", "note": "looks fine"},
    )
    assert evidence_from_json(evidence_to_json(evidence)) == evidence


def test_outcome_json_carries_the_derived_state() -> None:
    assert outcome_to_json(VerificationOutcome("inconclusive", "x", True, True))["verification_state"] == "unverified"
    assert outcome_to_json(VerificationOutcome("passed", "x", True, True))["verification_state"] == "passed"


# --- adversary 3: weakening a check WITHOUT touching its argv -----------------------------------


def _repo_manifest(root: pathlib.Path) -> list[tuple[str, str]]:
    """The exact call both the supervisor and the verifier make."""
    return build_corpus_manifest(str(root), ("tests/", ".github/", ".local/") + DECIDING_FILE_PATHS)


def _clone(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.py").write_text("def test_real():\n    assert True\n")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff.lint]\nselect = ['E','F']\n")
    (tmp_path / "services" / "pkg").mkdir(parents=True)
    (tmp_path / "services" / "pkg" / "mod.py").write_text("import os\nx=1\n")
    return tmp_path


@pytest.mark.parametrize(
    "bypass",
    ["ruff.toml", ".ruff.toml", "services/.ruff.toml", "services/pkg/.ruff.toml", "services/pkg/conftest.py"],
)
def test_a_nested_tool_config_cannot_silence_a_frozen_check_invisibly(tmp_path: pathlib.Path, bypass: str) -> None:
    """The frozen ``lint`` argv is ``ruff check .``; ruff obeys the NEAREST config, so a two-line
    file anywhere in the tree turns the check off without altering a single byte of the argv.

    Measured before the basename fix: ``services/pkg/.ruff.toml`` with ``select = []`` took
    ``ruff check .`` from exit 1 to exit 0 while ``corpus_digest`` stayed byte-identical, so the
    graded verdict was ``passed``.  Every one of these files must now land in the manifest and move
    the digest, which is what turns the bypass into ``corpus_digest_mismatch``.
    """
    clone = _clone(tmp_path)
    frozen = _repo_manifest(clone)
    frozen_digest = corpus_digest(frozen)

    target = clone / bypass
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("collect_ignore_glob = ['*']\n" if bypass.endswith(".py") else "[lint]\nselect = []\n")

    observed = _repo_manifest(clone)
    assert bypass in [path for path, _digest in observed], f"{bypass} is invisible to the corpus manifest"
    assert corpus_digest(observed) != frozen_digest


def test_the_work_product_is_deliberately_not_in_the_corpus(tmp_path: pathlib.Path) -> None:
    """The counterweight to the test above: the files the agent is PAID to change stay out, or every
    honest run would grade ``corpus_digest_mismatch``."""
    clone = _clone(tmp_path)
    paths = [path for path, _digest in _repo_manifest(clone)]
    assert "services/pkg/mod.py" not in paths
    assert "tests/unit/test_a.py" in paths  # the tests that judge the change DO count


def test_ruff_configuration_files_are_named_as_deciding(tmp_path: pathlib.Path) -> None:
    assert "ruff.toml" in DECIDING_FILE_PATHS
    assert ".ruff.toml" in DECIDING_FILE_PATHS


def test_a_corpus_swapped_after_the_freeze_grades_failed() -> None:
    """End of the chain: a moved digest is a FAILED verification, not a warning."""
    check = AcceptanceCheck(check_id="lint", argv=("/usr/bin/python3", "-m", "ruff", "check", "."), cwd_rel=".", expect_exit_code=0, timeout_seconds=300)
    criteria = AcceptanceCriteria(
        criteria_id="c1",
        directive_id="d1",
        checks=(check,),
        criteria_digest=criteria_digest([check]),
        corpus_digest="frozen" + "0" * 58,
        corpus_manifest=(),
        frozen_at=datetime(2026, 9, 10, tzinfo=UTC),
        frozen_by="system:supervisor",
        policy_revision="p3",
    )
    result = CheckResult(check_id="lint", argv=check.argv, exit_code=0, duration_ms=1, stdout_sha256="", stderr_sha256="", excerpt="All checks passed!")
    outcome = decide_verdict(
        results=[result],
        criteria=criteria,
        observed_corpus_digest="moved" + "1" * 59,
        runner_principal="system:verifier",
        executing_identity="agent:claude",
    )
    assert outcome.verdict == "failed"
    assert outcome.reason == "corpus_digest_mismatch"
