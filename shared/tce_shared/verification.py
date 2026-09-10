"""Acceptance criteria the implementing agent cannot reach, and a verdict it cannot write.

The split this module exists to enforce: the runner produces **raw evidence only**
(:class:`CheckResult` — exit codes and hashes, no verdict field anywhere), and the API computes the
verdict from that evidence plus the criteria row frozen before the work began.  A self-report is not
a verification.

Three properties are load-bearing and each has a guard:

* **An empty criteria set FAILS.**  It never passes.  A zero-check criteria row that fell through an
  empty loop to "all checks matched" would be a passing verification with nothing verified, and it
  would release the task to DONE.  :func:`decide_verdict`'s first clause refuses it.
* **``criteria_digest_match`` is computed, not asserted.**  A column carrying a constant has a
  constant's content and a column's authority.
* **The corpus manifest covers the files that DECIDE what a check does**, not only the files the
  check reads.  Freezing ``["/usr/bin/pytest", "tests/..."]`` freezes the command, not what the
  command executes: this repo's ``pyproject.toml`` carries ``[tool.pytest.ini_options] pythonpath``
  and ``testpaths``, and it is writable inside the sandbox.  Hence :data:`DECIDING_FILE_PATHS`,
  matched **by basename at any depth** rather than only at the repository root.

  That last clause is not a refinement, it is the difference between a control and a decoration.
  Measured against the frozen ``lint`` check (``python -m ruff check .``): ruff resolves its
  configuration per file from the nearest ancestor ``.ruff.toml`` / ``ruff.toml`` / ``pyproject.toml``,
  so dropping ``services/pkg/.ruff.toml`` containing ``[lint]\nselect = []`` took the check from
  ``exit=1, Found 2 errors`` to ``exit=0, All checks passed!``.  With root-only exact-path matching
  neither that file nor a root ``ruff.toml`` entered the manifest, so ``corpus_digest`` was
  byte-identical before and after and the graded verdict was ``passed`` on a check that had been
  switched off.  The argv never changed, which is exactly why binding the argv is not enough.

A corpus mismatch is a verification **failure**, not an error.  It says: the tests that judged this
change are not the tests that were frozen at dispatch.  It **detects** weakening; the OS-level
protected-prefix deny **prevents** it for the sandboxed process tree; nothing prevents it outside
that tree.

Standard library only — the supervisor imports this module, so it must not pull in pydantic.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

VERIFICATION_SCHEMA_VERSION: str = "v1"
VERIFICATION_VERDICTS: tuple[str, str, str] = ("passed", "failed", "inconclusive")
VERIFICATION_STATE_FOR_VERDICT: dict[str, str] = {"passed": "passed", "failed": "failed", "inconclusive": "unverified"}
MIN_ACCEPTANCE_CHECKS: int = 1  # an EMPTY criteria set FAILS, it never passes

# The ``method`` stamped on a task-state verification ref produced by the evidence-graded route
# (``decide_verdict`` behind POST /v1/verification/results).  It lives here, shared, because BOTH
# backends write it and a projection that labels the same verdict differently per backend is a
# parity break a reader has no way to reconcile.
VERIFICATION_METHOD_EVIDENCE_GRADED: str = "evidence_graded"

# The files that decide what a frozen check actually executes.  build_corpus_manifest is always
# called with prefixes = tuple(charter.protected_write_prefixes) + DECIDING_FILE_PATHS.
#
# These are BASENAMES, matched at any depth (see _manifest_wanted).  ``ruff.toml``/``.ruff.toml`` are
# here because the frozen ``lint`` check is ``python -m ruff check .`` and ruff takes a nested config
# over the root ``pyproject.toml``; without them a two-line file anywhere in the tree silences the
# check while leaving corpus_digest unchanged.  ``conftest.py`` is nested for the same reason.
DECIDING_FILE_PATHS: tuple[str, ...] = (
    "pyproject.toml",
    "conftest.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "ruff.toml",
    ".ruff.toml",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
    "Makefile",
)
_DECIDING_FILE_NAMES: frozenset[str] = frozenset(DECIDING_FILE_PATHS)

_SKIP_DIR_NAMES: frozenset[str] = frozenset({".git", "__pycache__", "node_modules", "build", "dist"})
_SKIP_DIR_PREFIXES: tuple[str, ...] = (".venv",)


class CriteriaFrozen(RuntimeError):
    """A second freeze for a directive whose criteria are already frozen.  The first freeze wins."""

    directive_id: str

    def __init__(self, directive_id: str) -> None:
        super().__init__(f"acceptance criteria for directive {directive_id} are already frozen")
        self.directive_id = directive_id


class CriteriaInvalid(ValueError):
    """``field`` is one of: "checks", "argv", "cwd_rel", "timeout_seconds", "expect_exit_code",
    "check_id"."""

    field: str

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    check_id: str
    argv: tuple[str, ...]
    cwd_rel: str
    expect_exit_code: int
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class AcceptanceCriteria:
    criteria_id: str
    directive_id: str
    checks: tuple[AcceptanceCheck, ...]
    criteria_digest: str
    corpus_digest: str
    corpus_manifest: tuple[tuple[str, str], ...]  # (repo-relative path, sha256)
    frozen_at: datetime
    frozen_by: str
    policy_revision: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    """RAW EVIDENCE ONLY.  There is deliberately no verdict field: the supervisor may only produce
    this shape, and only the API turns evidence into a verdict."""

    check_id: str
    argv: tuple[str, ...]
    exit_code: int
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str
    excerpt: str  # redacted, <= 2000 chars


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """The entire POST body of ``/v1/verification/results``, in one type.  The API computes the
    verdict from this plus the stored criteria row; the caller never sends a verdict."""

    directive_id: str
    results: tuple[CheckResult, ...]
    observed_corpus_digest: str
    observed_corpus_manifest: tuple[tuple[str, str], ...]
    platform: str
    commit_sha: str | None
    tree_sha: str | None
    reviewer_model: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    verdict: Literal["passed", "failed", "inconclusive"]
    reason: str
    criteria_digest_match: bool
    corpus_digest_match: bool


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def criteria_digest(checks: Sequence[AcceptanceCheck]) -> str:
    ordered = sorted(checks, key=lambda check: check.check_id)
    return _sha256_hex(
        _canonical_json(
            [
                {
                    "check_id": check.check_id,
                    "argv": list(check.argv),
                    "cwd_rel": check.cwd_rel,
                    "expect_exit_code": check.expect_exit_code,
                    "timeout_seconds": check.timeout_seconds,
                }
                for check in ordered
            ]
        )
    )


def corpus_digest(manifest: Sequence[tuple[str, str]]) -> str:
    return _sha256_hex(_canonical_json(sorted([list(entry) for entry in manifest])))


def build_corpus_manifest(root: str, prefixes: Sequence[str]) -> list[tuple[str, str]]:
    """Walk ``root`` and hash every file under any of ``prefixes``, plus every file **at any depth**
    whose basename is in :data:`DECIDING_FILE_PATHS`.  Deterministic and sorted.

    Depth matters: a deciding file is deciding wherever it sits.  ``ruff`` reads the nearest
    ancestor config, and ``pytest`` reads a ``conftest.py`` from every collected directory, so
    matching only the repository root leaves a one-file bypass in a subdirectory."""
    wanted_prefixes = [str(prefix).strip().strip("/") for prefix in prefixes if str(prefix).strip()]
    entries: list[tuple[str, str]] = []
    root_abs = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SKIP_DIR_NAMES and not name.startswith(_SKIP_DIR_PREFIXES)
        )
        for filename in sorted(filenames):
            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, root_abs).replace(os.sep, "/")
            if not _manifest_wanted(relative, wanted_prefixes):
                continue
            try:
                with open(absolute, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            entries.append((relative, digest))
    return sorted(entries)


def _manifest_wanted(relative: str, prefixes: Sequence[str]) -> bool:
    # Basename, not exact path: a nested `.ruff.toml` or `conftest.py` decides as much as a root one.
    if relative.rsplit("/", 1)[-1] in _DECIDING_FILE_NAMES:
        return True
    for prefix in prefixes:
        if relative == prefix or relative.startswith(prefix + "/"):
            return True
    return False


def corpus_manifest_covers(manifest: Sequence[tuple[str, str]], required: Sequence[str]) -> tuple[bool, list[str]]:
    """``(ok, missing)``.  ``required`` is :data:`DECIDING_FILE_PATHS` filtered to the files that
    actually exist in the clone; the freeze route refuses when ``ok`` is False."""
    present = {str(path) for path, _digest in manifest}
    missing = [str(path) for path in required if str(path) not in present]
    return (not missing, missing)


def validate_checks(checks: Sequence[AcceptanceCheck], *, max_checks: int, max_timeout_seconds: int) -> tuple[AcceptanceCheck, ...]:
    """Raise ``CriteriaInvalid``; return the normalised, timeout-clamped tuple."""
    if len(checks) < MIN_ACCEPTANCE_CHECKS:
        raise CriteriaInvalid("checks", f"at least {MIN_ACCEPTANCE_CHECKS} acceptance check is required")
    if len(checks) > max_checks:
        raise CriteriaInvalid("checks", f"at most {max_checks} acceptance checks are allowed")
    seen: set[str] = set()
    normalised: list[AcceptanceCheck] = []
    for check in checks:
        check_id = str(check.check_id or "").strip()
        if not check_id:
            raise CriteriaInvalid("check_id", "check_id must be non-empty")
        if check_id in seen:
            raise CriteriaInvalid("check_id", f"duplicate check_id {check_id!r}")
        seen.add(check_id)
        argv = tuple(str(item) for item in check.argv)
        if not argv:
            raise CriteriaInvalid("argv", f"check {check_id!r} has an empty argv")
        if not argv[0].startswith("/"):
            raise CriteriaInvalid("argv", f"check {check_id!r} argv[0] must be an absolute path (no PATH resolution, no shell)")
        cwd_rel = str(check.cwd_rel or ".").strip() or "."
        if cwd_rel.startswith("/") or ".." in cwd_rel.split("/"):
            raise CriteriaInvalid("cwd_rel", f"check {check_id!r} cwd_rel must be relative with no '..'")
        timeout = int(check.timeout_seconds)
        if timeout < 1:
            raise CriteriaInvalid("timeout_seconds", f"check {check_id!r} timeout_seconds must be >= 1")
        normalised.append(
            AcceptanceCheck(
                check_id=check_id,
                argv=argv,
                cwd_rel=cwd_rel,
                expect_exit_code=int(check.expect_exit_code),
                timeout_seconds=min(timeout, int(max_timeout_seconds)),
            )
        )
    return tuple(normalised)


def decide_verdict(
    *,
    results: Sequence[CheckResult],
    criteria: AcceptanceCriteria,
    observed_corpus_digest: str,
    runner_principal: str,
    executing_identity: str,
) -> VerificationOutcome:
    """Pure, evidence-only grading.  It is the API that calls this, never the runner.

    No model output reaches this function; an advisory model opinion is recorded beside the
    verdict, never inside it.
    """
    if not criteria.checks:
        return VerificationOutcome("failed", "empty_criteria_set", False, False)
    criteria_match = criteria_digest(criteria.checks) == criteria.criteria_digest
    if not criteria_match:
        return VerificationOutcome("failed", "criteria_digest_mismatch", False, False)
    if runner_principal == executing_identity:
        return VerificationOutcome("inconclusive", "runner_is_executing_identity", criteria_match, False)
    # The SAME rule, applied to the other half of "grading your own homework": whoever WROTE the
    # goalposts may not be the party the goalposts are aimed at.  The freeze route already refuses a
    # freezer that matches the directive's claimant, but that check can only see the claim state at
    # FREEZE time, and the ordering of "freeze" and "claim" is the implementing agent's own choice.
    # Measured over HTTP against both backends before this clause existed: an agent holding a
    # server-bound identity froze `/bin/sh -c "exit 0"` as its acceptance check for a directive it
    # had not yet claimed (no dispatch_records row, claimed_executor still NULL, so the route saw
    # nothing to refuse), then claimed it, reported `succeeded`, and an HONEST verifier graded the
    # criteria `passed` -- driving the task's projection all the way to `DONE` on Lite and on Full.
    # Grading time is the first moment BOTH facts are recorded and settled, so the check belongs
    # here, where no ordering can get around it.  ``inconclusive``, not ``failed``: the checks may
    # even have been honest, but nobody independent chose them, so nothing was established either
    # way -- and `inconclusive` maps to `unverified`, which R9 refuses.
    if criteria.frozen_by and criteria.frozen_by == executing_identity:
        return VerificationOutcome("inconclusive", "criteria_frozen_by_executing_identity", criteria_match, False)
    corpus_match = observed_corpus_digest == criteria.corpus_digest
    if not corpus_match:
        return VerificationOutcome("failed", "corpus_digest_mismatch", criteria_match, False)
    # Coverage is a SET question, never a count.  ``len(results) == len(checks)`` is satisfied by N
    # copies of the one check the implementing agent can make pass, so duplicates are refused first
    # and the evidenced check_id SET must then equal the frozen one.
    by_id = {check.check_id: check for check in criteria.checks}
    evidenced: set[str] = set()
    for result in results:
        if result.check_id in evidenced:
            return VerificationOutcome("inconclusive", f"duplicate_check:{result.check_id}", criteria_match, corpus_match)
        evidenced.add(result.check_id)
        if result.check_id not in by_id:
            return VerificationOutcome("inconclusive", f"unknown_check:{result.check_id}", criteria_match, corpus_match)
    if evidenced != set(by_id):
        return VerificationOutcome("inconclusive", "incomplete_check_set", criteria_match, corpus_match)
    for result in results:
        want = by_id[result.check_id]
        if result.exit_code != want.expect_exit_code:
            return VerificationOutcome("failed", f"check_failed:{result.check_id}:exit={result.exit_code}", criteria_match, corpus_match)
    return VerificationOutcome("passed", "all_checks_matched", criteria_match, corpus_match)


def acceptance_check_to_json(check: AcceptanceCheck) -> dict[str, Any]:
    return {
        "check_id": check.check_id,
        "argv": list(check.argv),
        "cwd_rel": check.cwd_rel,
        "expect_exit_code": check.expect_exit_code,
        "timeout_seconds": check.timeout_seconds,
    }


def acceptance_check_from_json(value: Mapping[str, Any]) -> AcceptanceCheck:
    return AcceptanceCheck(
        check_id=str(value.get("check_id") or ""),
        argv=tuple(str(item) for item in (value.get("argv") or ())),
        cwd_rel=str(value.get("cwd_rel") or "."),
        expect_exit_code=int(value.get("expect_exit_code") or 0),
        timeout_seconds=int(value.get("timeout_seconds") or 900),
    )


def criteria_to_json(criteria: AcceptanceCriteria) -> dict[str, Any]:
    return {
        "criteria_id": criteria.criteria_id,
        "directive_id": criteria.directive_id,
        "checks": [acceptance_check_to_json(check) for check in criteria.checks],
        "criteria_digest": criteria.criteria_digest,
        "corpus_digest": criteria.corpus_digest,
        "corpus_manifest": [list(entry) for entry in criteria.corpus_manifest],
        "frozen_at": criteria.frozen_at.isoformat(),
        "frozen_by": criteria.frozen_by,
        "policy_revision": criteria.policy_revision,
        "schema_version": VERIFICATION_SCHEMA_VERSION,
    }


def criteria_from_json(value: Mapping[str, Any]) -> AcceptanceCriteria:
    return AcceptanceCriteria(
        criteria_id=str(value.get("criteria_id") or ""),
        directive_id=str(value.get("directive_id") or ""),
        checks=tuple(acceptance_check_from_json(entry) for entry in (value.get("checks") or ())),
        criteria_digest=str(value.get("criteria_digest") or ""),
        corpus_digest=str(value.get("corpus_digest") or ""),
        corpus_manifest=_manifest_tuple(value.get("corpus_manifest")),
        frozen_at=_parse_dt(value.get("frozen_at")),
        frozen_by=str(value.get("frozen_by") or ""),
        policy_revision=str(value.get("policy_revision") or ""),
    )


def check_result_to_json(result: CheckResult) -> dict[str, Any]:
    return {
        "check_id": result.check_id,
        "argv": list(result.argv),
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "excerpt": result.excerpt,
    }


def check_result_from_json(value: Mapping[str, Any]) -> CheckResult:
    return CheckResult(
        check_id=str(value.get("check_id") or ""),
        argv=tuple(str(item) for item in (value.get("argv") or ())),
        exit_code=int(value.get("exit_code") or 0),
        duration_ms=int(value.get("duration_ms") or 0),
        stdout_sha256=str(value.get("stdout_sha256") or ""),
        stderr_sha256=str(value.get("stderr_sha256") or ""),
        excerpt=str(value.get("excerpt") or "")[:2000],
    )


def evidence_to_json(evidence: VerificationEvidence) -> dict[str, Any]:
    return {
        "directive_id": evidence.directive_id,
        "results": [check_result_to_json(result) for result in evidence.results],
        "observed_corpus_digest": evidence.observed_corpus_digest,
        "observed_corpus_manifest": [list(entry) for entry in evidence.observed_corpus_manifest],
        "platform": evidence.platform,
        "commit_sha": evidence.commit_sha,
        "tree_sha": evidence.tree_sha,
        "reviewer_model": dict(evidence.reviewer_model) if evidence.reviewer_model is not None else None,
    }


def evidence_from_json(value: Mapping[str, Any]) -> VerificationEvidence:
    reviewer = value.get("reviewer_model")
    return VerificationEvidence(
        directive_id=str(value.get("directive_id") or ""),
        results=tuple(check_result_from_json(entry) for entry in (value.get("results") or ())),
        observed_corpus_digest=str(value.get("observed_corpus_digest") or ""),
        observed_corpus_manifest=_manifest_tuple(value.get("observed_corpus_manifest")),
        platform=str(value.get("platform") or ""),
        commit_sha=(str(value["commit_sha"]) if value.get("commit_sha") else None),
        tree_sha=(str(value["tree_sha"]) if value.get("tree_sha") else None),
        reviewer_model=(dict(reviewer) if isinstance(reviewer, Mapping) else None),
    )


def outcome_to_json(outcome: VerificationOutcome) -> dict[str, Any]:
    return {
        "verdict": outcome.verdict,
        "reason": outcome.reason,
        "criteria_digest_match": outcome.criteria_digest_match,
        "corpus_digest_match": outcome.corpus_digest_match,
        "verification_state": VERIFICATION_STATE_FOR_VERDICT[outcome.verdict],
        "schema_version": VERIFICATION_SCHEMA_VERSION,
    }


def outcome_from_json(value: Mapping[str, Any]) -> VerificationOutcome:
    return VerificationOutcome(
        verdict=_verdict_literal(value.get("verdict")),
        reason=str(value.get("reason") or ""),
        criteria_digest_match=bool(value.get("criteria_digest_match")),
        corpus_digest_match=bool(value.get("corpus_digest_match")),
    )


def _verdict_literal(value: Any) -> Literal["passed", "failed", "inconclusive"]:
    raw = str(value or "inconclusive")
    if raw == "passed":
        return "passed"
    if raw == "failed":
        return "failed"
    return "inconclusive"


def _manifest_tuple(value: Any) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for entry in value or ():
        pair = [str(item) for item in entry]
        if len(pair) == 2:
            entries.append((pair[0], pair[1]))
    return tuple(entries)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
