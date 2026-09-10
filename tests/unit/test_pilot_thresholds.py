"""The P6 thresholds are frozen, hashed from their values, and not settings."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC
from pathlib import Path

from tce_shared import pilot_thresholds
from tce_shared.pilot_thresholds import (
    MIN_ACTIVE_DAYS,
    MIN_CLOSE_COVERAGE,
    MIN_CLOSED_PER_ARM,
    P6_THRESHOLDS_EFFECTIVE_AT,
    P6_THRESHOLDS_SHA,
    P6_THRESHOLDS_VERSION,
    enrolments_required_per_cell,
    threshold_values,
)

MODULE = Path(pilot_thresholds.__file__)


def test_the_digest_is_derived_from_the_values() -> None:
    expected = hashlib.sha256(json.dumps(threshold_values(), sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]
    assert P6_THRESHOLDS_SHA == expected
    assert len(P6_THRESHOLDS_SHA) == 32


def test_one_changed_number_changes_the_digest() -> None:
    """A change *detector*.  It cannot prove the thresholds preceded the data, and does not claim to."""

    moved = dict(threshold_values())
    moved["MIN_CLOSED_PER_ARM"] = 3
    digest = hashlib.sha256(json.dumps(moved, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]
    assert digest != P6_THRESHOLDS_SHA


def test_every_constant_that_moves_a_verdict_is_in_the_digest() -> None:
    module = ast.parse(MODULE.read_text(encoding="utf-8"))
    declared = {
        target.id
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id.isupper() and not target.id.startswith("_")
    }
    declared.discard("P6_THRESHOLDS_SHA")  # the digest itself
    assert declared <= set(threshold_values()), sorted(declared - set(threshold_values()))


def test_thresholds_never_import_config_or_settings() -> None:
    """A threshold a caller can sweep is not a threshold."""

    module = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"hashlib", "json", "datetime", "__future__"}, sorted(imported)


def test_the_floor_is_not_lowered_to_make_the_report_read_better() -> None:
    """The same number the existing behaviour pilot gate already uses."""

    assert MIN_CLOSED_PER_ARM == 30
    assert MIN_ACTIVE_DAYS == 28
    assert MIN_CLOSE_COVERAGE == 0.80


def test_the_arithmetic_the_runbook_opens_with() -> None:
    """Three arms x 30 closed / 0.80 coverage = 113 enrolments per (project, family) cell."""

    assert enrolments_required_per_cell(3) == 113
    assert enrolments_required_per_cell(1) == 38


def test_effective_at_is_tz_aware_utc() -> None:
    assert P6_THRESHOLDS_EFFECTIVE_AT.tzinfo is not None
    assert P6_THRESHOLDS_EFFECTIVE_AT.astimezone(UTC) == P6_THRESHOLDS_EFFECTIVE_AT
    assert P6_THRESHOLDS_VERSION == "p6-thresholds-v1"
