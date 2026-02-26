from __future__ import annotations

from datetime import UTC, datetime

from tce_api.main import _compute_hourly_buckets, _compute_outcome_metrics, _compute_top_errors


class _FakeRow:
    def __init__(self, ts=None, payload=None):
        self.ts = ts
        self.payload = payload or {}


def test_hourly_buckets_groups_by_hour() -> None:
    rows = [
        _FakeRow(ts=datetime(2026, 2, 19, 10, 15, tzinfo=UTC)),
        _FakeRow(ts=datetime(2026, 2, 19, 10, 45, tzinfo=UTC)),
        _FakeRow(ts=datetime(2026, 2, 19, 11, 5, tzinfo=UTC)),
    ]
    buckets = _compute_hourly_buckets(rows)
    assert buckets["2026-02-19T10:00:00"] == 2
    assert buckets["2026-02-19T11:00:00"] == 1


def test_outcome_metrics_counts_payload_outcome() -> None:
    rows = [
        _FakeRow(payload={"outcome": "success"}),
        _FakeRow(payload={"outcome": "success"}),
        _FakeRow(payload={"outcome": "failure"}),
        _FakeRow(payload={}),
    ]
    metrics = _compute_outcome_metrics(rows)
    assert metrics == {"success": 2, "failure": 1}


def test_top_errors_extracts_error_messages() -> None:
    rows = [
        _FakeRow(payload={"error": "ConnectionTimeout"}),
        _FakeRow(payload={"error": "ConnectionTimeout"}),
        _FakeRow(payload={"error": "NullPointerException"}),
        _FakeRow(payload={}),
    ]
    errors = _compute_top_errors(rows, max_errors=2)
    assert errors == ["ConnectionTimeout", "NullPointerException"]
