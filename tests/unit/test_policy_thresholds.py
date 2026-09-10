"""The thresholds are frozen constants, and the digest is derived from the values."""

from __future__ import annotations

import ast
import pathlib

from tce_shared import policy_thresholds
from tce_shared.policy_thresholds import (
    MIN_EFFECTIVE_SAMPLE,
    OOD_OVERLAP_FLOOR,
    THRESHOLDS_EFFECTIVE_AT,
    THRESHOLDS_SHA,
    threshold_values,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_THRESHOLDS_PATH = _REPO_ROOT / "shared" / "tce_shared" / "policy_thresholds.py"

_ABSTENTION_FLOORS = (
    "SIMILARITY_FLOOR",
    "OOD_OVERLAP_FLOOR",
    "CONFLICT_SHARE_RATIO",
    "MIN_AGREEMENT_SHARE",
    "MIN_EFFECTIVE_SAMPLE",
)


def _source_files(*relative_paths: str) -> list[pathlib.Path]:
    """Named source files, never a tree walk.

    Any gate in this repository that walks the tree has to exclude ``services/*/build/lib``:
    those trees are gitignored, hold stale copies of the very strings the gates forbid, and
    survive every edit — so a naive walk is red on a developer machine and green in CI.
    Naming the two files avoids the problem outright.
    """

    paths = [_REPO_ROOT / relative for relative in relative_paths]
    for path in paths:
        assert path.is_file(), f"{path} is missing; the gate would pass vacuously"
        assert "build/" not in str(path)
    return paths


def test_digest_changes_when_any_constant_changes() -> None:
    values = threshold_values()
    assert THRESHOLDS_SHA == policy_thresholds.THRESHOLDS_SHA
    baseline = _digest(values)
    assert baseline == THRESHOLDS_SHA
    for name in values:
        mutated = dict(values)
        mutated[name] = f"{mutated[name]}-moved"
        assert _digest(mutated) != baseline, f"{name} does not reach the digest"


def _digest(values: dict[str, object]) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def test_digest_is_derived_from_values_not_from_file_bytes() -> None:
    # Reformatting must be a no-op.  The digest input is the mapping, and nothing in it is a
    # line number, a comment or a byte offset.
    values = threshold_values()
    assert all(not isinstance(value, bytes) for value in values.values())
    assert "__file__" not in values


def test_every_abstention_floor_lives_here_and_reaches_the_digest() -> None:
    values = threshold_values()
    for name in _ABSTENTION_FLOORS:
        assert hasattr(policy_thresholds, name), f"{name} is not a frozen constant"
        assert name in values, f"{name} does not reach THRESHOLDS_SHA"


def test_the_abstention_floors_are_not_settings_in_either_backend() -> None:
    """A threshold a caller can move is not a threshold.

    The tuning path this closes was traced end to end: sweep the OOD floor until coverage
    clears, write a QUALIFIED record with an unchanged digest, revert the environment in
    production, and the exposure check cannot tell.
    """

    for path in _source_files("services/tce_api/tce_api/config.py", "services/tce_lite_api/tce_lite_api/config.py"):
        text = path.read_text()
        for name in _ABSTENTION_FLOORS:
            assert name.lower() not in text.lower(), f"{name} reappeared as a setting in {path.name}"


def test_this_module_imports_no_settings() -> None:
    tree = ast.parse(_THRESHOLDS_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "config" not in module and "settings" not in module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "config" not in alias.name and "settings" not in alias.name


def test_no_gate_constant_is_derived_from_an_effective_sample_number() -> None:
    """An effective-sample number does not calibrate anything.

    Kish ESS bounds the variance of a weighted vote share; it says nothing about the
    probability that the vote is right.  Twelve near-identical paraphrases of one decision
    have a high ESS and one bit of information.  ``MIN_EFFECTIVE_SAMPLE`` is an abstention
    floor, and it is the only place the quantity may appear.
    """

    named = [name for name in dir(policy_thresholds) if "effective_sample" in name.lower()]
    assert named == ["MIN_EFFECTIVE_SAMPLE"]


def test_min_effective_sample_is_one_point_five_and_the_reason_is_arithmetic() -> None:
    from tce_shared.behavior_fidelity import _effective_sample_size

    assert MIN_EFFECTIVE_SAMPLE == 1.5
    two_real_neighbours = _effective_sample_size([0.473947, 0.4725])
    assert two_real_neighbours < 2.0 - 1e-9  # 2.0 and 2.0-1e-9 both reject a genuine pair
    assert two_real_neighbours >= MIN_EFFECTIVE_SAMPLE
    assert _effective_sample_size([0.9]) == 1.0  # and n=1 is still excluded, exactly


def test_ood_overlap_floor_sits_in_the_measured_gap() -> None:
    from tce_shared.behavior_fidelity import _topical_overlap

    query = {"situation_summary": "force push to prod to fix the stripe webhook"}
    unrelated = {"situation_summary": "ssh into the box and restart nginx"}
    tangential = {"situation_summary": "i keep meaning to rewrite the scheduler in rust"}
    same_topic = {"situation_summary": "deploy the payment webhook fix to production now"}
    paraphrase = {"situation_summary": "the stripe webhook is failing, do i force push the fix to prod"}

    assert _topical_overlap(query, unrelated) < OOD_OVERLAP_FLOOR
    assert _topical_overlap(query, tangential) < OOD_OVERLAP_FLOOR
    assert _topical_overlap(query, same_topic) >= OOD_OVERLAP_FLOOR
    assert _topical_overlap(query, paraphrase) >= OOD_OVERLAP_FLOOR


def test_effective_at_cutoff_excludes_traffic_that_predates_registration() -> None:
    from datetime import UTC, datetime

    assert THRESHOLDS_EFFECTIVE_AT.tzinfo is not None
    # Every shadow row stored before P4 was produced by traffic that existed when these
    # numbers were chosen, so the honest first report reads "adjudicated 0/100" with an
    # explicit pre-registration exclusion count beside it.
    assert datetime(2026, 9, 9, tzinfo=UTC) < THRESHOLDS_EFFECTIVE_AT
