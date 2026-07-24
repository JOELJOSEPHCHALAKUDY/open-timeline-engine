from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tce_shared.continuity import (
    percentile,
    progress_patch,
    recommended_file_rank,
    summarize_attempts,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def test_recommended_file_rank_normalizes_paths() -> None:
    assert recommended_file_rank("./src\\api.py", ["src/api.py", "tests/test_api.py"]) == 1
    assert recommended_file_rank("tests/test_api.py", '["src/api.py","tests/test_api.py"]') == 2
    assert recommended_file_rank("missing.py", ["src/api.py"]) is None


def test_progress_patch_is_idempotent_and_derives_rank() -> None:
    row = {
        "recommended_files_json": ["src/api.py", "tests/test_api.py"],
        "first_file_opened_at": None,
        "productive_at": None,
        "completed_at": None,
    }
    opened = progress_patch(
        row,
        phase="file_opened",
        now=NOW,
        opened_file="./tests/test_api.py",
        progress_source="check_context",
    )
    assert opened["opened_file_rank"] == 2
    assert opened["correct_file"] is False
    assert opened["first_file_opened_at"] == NOW

    existing = {
        **row,
        "first_file_opened_at": NOW - timedelta(seconds=5),
        "productive_at": NOW - timedelta(seconds=2),
    }
    completed = progress_patch(existing, phase="completed", now=NOW)
    assert "first_file_opened_at" not in completed
    assert "productive_at" not in completed
    assert completed["completed_at"] == NOW


def test_summary_separates_handoff_age_from_active_resume_time() -> None:
    requested = NOW
    metrics = summarize_attempts(
        [
            {
                "requested_at": requested,
                "latency_ms": 12,
                "time_since_handoff_ms": 3_600_000,
                "first_file_opened_at": requested + timedelta(seconds=2),
                "productive_at": requested + timedelta(seconds=10),
                "completed_at": requested + timedelta(seconds=30),
                "opened_file_rank": 1,
                "correct_file": True,
                "correct_anchor": True,
                "correction_required": False,
                "archaeology_tool_calls": 3,
                "archaeology_tokens": 400,
                "feedback_at": requested + timedelta(seconds=30),
            }
        ]
    )

    assert metrics["median_time_to_resume_ms"] == 3_600_000
    assert metrics["median_handoff_age_at_resume_ms"] == 3_600_000
    assert metrics["median_time_to_first_file_ms"] == 2_000
    assert metrics["median_active_resume_ms"] == 10_000
    assert metrics["median_completion_after_resume_ms"] == 30_000
    assert metrics["correct_file_at_1_rate"] == 1.0
    assert metrics["correct_file_at_3_rate"] == 1.0
    assert metrics["correct_anchor_rate"] == 1.0
    assert metrics["median_archaeology_tool_calls"] == 3
    assert metrics["median_archaeology_tokens"] == 400


def test_percentile_invariants_hold_across_orderings_and_outliers() -> None:
    samples = [
        [0],
        [7, 1, 3, 2],
        [10_000, 0, 25, 25, 100],
        list(range(100, -1, -1)),
    ]
    for values in samples:
        p0 = percentile(values, 0.0)
        p50 = percentile(values, 0.5)
        p95 = percentile(values, 0.95)
        p100 = percentile(values, 1.0)
        assert p0 is not None
        assert p50 is not None
        assert p95 is not None
        assert p100 is not None
        assert min(values) == p0 <= p50 <= p95 <= p100 == max(values)
        assert percentile(reversed(values), 0.95) == p95


def test_summary_rates_are_bounded_for_partial_feedback() -> None:
    rows = [
        {
            "requested_at": NOW,
            "time_since_handoff_ms": age,
            "latency_ms": age // 100,
            "opened_file_rank": rank,
            "correct_file": rank == 1,
            "correct_anchor": index % 2 == 0,
            "correction_required": index % 3 == 0,
            "feedback_at": NOW + timedelta(seconds=index + 1),
        }
        for index, (age, rank) in enumerate(
            [(500, 1), (1_000, 2), (2_000, 4), (4_000, 1)]
        )
    ]
    metrics = summarize_attempts(rows)
    for name in (
        "correct_file_rate",
        "correct_file_at_1_rate",
        "correct_file_at_3_rate",
        "correct_anchor_rate",
        "correction_rate",
    ):
        value = metrics[name]
        assert value is not None
        assert 0.0 <= value <= 1.0
    assert metrics["p95_handoff_age_at_resume_ms"] >= metrics[
        "median_handoff_age_at_resume_ms"
    ]
