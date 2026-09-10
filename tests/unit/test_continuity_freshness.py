from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tce_shared.continuity import assess_anchor_freshness

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def test_unknown_when_no_current_commit() -> None:
    assert assess_anchor_freshness(record_git={"commit": "abc123def456"}, record_ts=_NOW, current_git=None, now=_NOW) == ("unknown", ["no_current_commit"])
    assert assess_anchor_freshness(record_git={"commit": "abc123def456"}, record_ts=_NOW, current_git={}, now=_NOW) == ("unknown", ["no_current_commit"])
    assert assess_anchor_freshness(record_git={"commit": "abc123def456"}, record_ts=_NOW, current_git={"branch": "main"}, now=_NOW) == ("unknown", ["no_current_commit"])


def test_stale_on_commit_mismatch() -> None:
    label, reasons = assess_anchor_freshness(
        record_git={"commit": "abc123def456"},
        record_ts=_NOW,
        current_git={"commit": "deadbeef0000"},
        now=_NOW,
    )
    assert label == "stale"
    assert reasons == ["commit_mismatch"]


def test_stale_on_repo_mismatch_before_commit_comparison() -> None:
    label, reasons = assess_anchor_freshness(
        record_git={"commit": "abc123def456", "repo": "github.com/acme/one"},
        record_ts=_NOW,
        current_git={"commit": "abc123def456", "repo": " GitHub.com/acme/TWO "},
        now=_NOW,
    )
    assert label == "stale"
    assert reasons == ["repo_mismatch"]


def test_current_on_commit_match_is_prefix_and_case_insensitive() -> None:
    label, reasons = assess_anchor_freshness(
        record_git={"commit": "ABC123DEF456789", "repo": "github.com/acme/one"},
        record_ts=_NOW,
        current_git={"commit": "abc123def456", "repo": "github.com/acme/one"},
        now=_NOW,
    )
    assert label == "current"
    assert reasons == ["commit_match"]


def test_current_but_old_record_is_labelled_current_with_age_reason() -> None:
    label, reasons = assess_anchor_freshness(
        record_git={"commit": "abc123def456"},
        record_ts=_NOW - timedelta(hours=100),
        current_git={"commit": "abc123def456"},
        now=_NOW,
        max_age_hours=72.0,
    )
    assert label == "current"
    assert reasons == ["commit_match", "older_than_max_age"]


def test_unknown_when_record_commit_missing() -> None:
    label, reasons = assess_anchor_freshness(
        record_git={"branch": "main"},
        record_ts=_NOW,
        current_git={"commit": "abc123def456"},
        now=_NOW,
    )
    assert label == "unknown"
    assert reasons == ["record_commit_missing"]
    assert assess_anchor_freshness(record_git=None, record_ts=None, current_git={"commit": "abc123def456"}, now=_NOW)[0] == "unknown"


def test_never_current_without_matching_commit() -> None:
    for record, current in (
        ({"commit": ""}, {"commit": "abc123def456"}),
        ({"commit": "abc123def456"}, {"commit": "abc123def457"}),
        ({"commit": "abc123def456"}, {"commit": ""}),
    ):
        label, _ = assess_anchor_freshness(record_git=record, record_ts=_NOW, current_git=current, now=_NOW)
        assert label != "current", (record, current)
