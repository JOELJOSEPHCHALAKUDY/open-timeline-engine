from __future__ import annotations

from typing import Any

from tce_shared.behavior_control import (
    capability_policy,
    mine_process_models,
    normalize_capability_operation,
    shadow_evaluation_metrics,
)


def test_capability_digest_is_exact_and_unknown_capabilities_fail_closed() -> None:
    first = normalize_capability_operation(
        capability="filesystem.read",
        action="open",
        resource="src/app.py",
        arguments={"line": 10},
    )
    reordered = normalize_capability_operation(
        capability="filesystem.read",
        action="open",
        resource="src/app.py",
        arguments={"line": 10},
    )
    changed = normalize_capability_operation(
        capability="filesystem.read",
        action="open",
        resource="src/app.py",
        arguments={"line": 11},
    )
    assert first["action_digest"] == reordered["action_digest"]
    assert first["action_digest"] != changed["action_digest"]
    assert capability_policy("filesystem.read")["mutating"] is False
    assert capability_policy("shell.root")["decision"] == "blocked"


def test_process_mining_requires_cross_session_support_and_preserves_order() -> None:
    rows = []
    for session in ("a", "b"):
        rows.extend(
            [
                {"id": f"{session}-1", "session_id": session, "ts": "2026-01-01T00:00:00Z", "action_kind": "diagnose", "result": "success"},
                {"id": f"{session}-2", "session_id": session, "ts": "2026-01-01T00:01:00Z", "action_kind": "patch", "result": "success"},
                {"id": f"{session}-3", "session_id": session, "ts": "2026-01-01T00:02:00Z", "action_kind": "verify", "result": "succeeded"},
            ]
        )
    models = mine_process_models(rows, min_support=2)
    assert len(models) == 1
    assert models[0]["steps"] == ["diagnose", "patch", "verify"]
    assert models[0]["support"] == 2
    assert models[0]["status"] == "candidate"


def test_shadow_metrics_detect_recent_precision_drop() -> None:
    rows = [
        {"abstained": False, "correct": False} for _ in range(30)
    ] + [
        {"abstained": False, "correct": True} for _ in range(30)
    ]
    metrics = shadow_evaluation_metrics(rows)
    assert metrics["precision"] == 0.5
    assert metrics["drift_delta"] == -1.0
    assert metrics["drift_alert"] is True


def test_shadow_metrics_exclude_pending_rows_from_precision_and_report_counts() -> None:
    """Only resolved rows can be scored; everything else is counted, never averaged in.

    A prospective prediction that has not been answered yet has no ground truth, so
    treating it as 'incorrect' (or 'correct') would silently corrupt precision.
    """
    rows: list[dict[str, Any]] = [
        {"abstained": False, "correct": True, "resolution_state": "resolved", "prediction_stage": "prospective"},
        {"abstained": False, "correct": False, "resolution_state": "resolved", "prediction_stage": "retrospective"},
        {"abstained": False, "correct": None, "resolution_state": "pending", "prediction_stage": "prospective"},
        {"abstained": False, "correct": None, "resolution_state": "unanswered", "prediction_stage": "prospective"},
        {"abstained": True, "correct": None, "resolution_state": "missing_label", "prediction_stage": "prospective"},
        {"abstained": False, "correct": None, "resolution_state": "extraction_error", "prediction_stage": "prospective"},
        {"abstained": False, "correct": None, "resolution_state": "missed_capture", "prediction_stage": "prospective"},
        {"abstained": False, "correct": None, "resolution_state": "abandoned", "prediction_stage": "prospective"},
    ]
    metrics = shadow_evaluation_metrics(rows)
    assert metrics["sample_count"] == 8
    assert metrics["resolved_count"] == 2
    assert metrics["precision"] == 0.5, "only the two resolved rows may be scored"
    assert metrics["prospective_precision"] == 1.0, "the one resolved prospective row was correct"
    for key, expected in {
        "pending_count": 1, "unanswered_count": 1, "missing_label_count": 1, "extraction_error_count": 1,
        "missed_capture_count": 1, "abandoned_count": 1, "prospective_count": 7, "retrospective_count": 1,
    }.items():
        assert metrics[key] == expected, (key, metrics[key])


def test_shadow_metrics_treat_legacy_rows_without_resolution_state_as_resolved() -> None:
    rows = [{"abstained": False, "correct": True}, {"abstained": False, "correct": True}]
    metrics = shadow_evaluation_metrics(rows)
    assert metrics["resolved_count"] == 2 and metrics["precision"] == 1.0
    assert metrics["retrospective_count"] == 2 and metrics["prospective_precision"] is None
