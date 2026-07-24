from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tce_shared.handoff import normalize_milestone_v1, rank_resume_candidates


def test_normalize_milestone_v1_valid_payload() -> None:
    result = normalize_milestone_v1(
        details={
            "title": "Refactor search boosts",
            "payload": {"files": ["services/tce_api/tce_api/search.py"]},
            "decision": "Use deterministic boosts for handoff records.",
            "outcome": {"status": "succeeded", "next_step": "Run integration tests."},
            "git": {"branch": "codex/handoff-v1"},
            "anchors": [{"file": "services/tce_api/tce_api/search.py", "line": 123, "symbol": "run_search"}],
        },
        state="succeeded",
        fallback_title="fallback",
    )
    assert result["valid"] is True
    normalized = result["normalized"]
    assert normalized["title"] == "Refactor search boosts"
    assert normalized["payload"]["files"] == ["services/tce_api/tce_api/search.py"]
    assert normalized["outcome"]["status"] == "succeeded"
    assert normalized["anchors"][0]["line"] == 123


def test_normalize_milestone_v1_rejects_missing_required_fields() -> None:
    result = normalize_milestone_v1(
        details={"title": "Missing fields"},
        state="failed",
        fallback_title="fallback",
    )
    assert result["valid"] is False
    errors = " | ".join(result["errors"])
    assert "payload.files" in errors
    assert "decision" in errors
    assert "outcome.next_step" in errors


def test_rank_resume_candidates_prefers_task_overlap_then_recency() -> None:
    now = datetime.now(tz=UTC)
    selected, _, _ = rank_resume_candidates(
        [
            {
                "id": "a",
                "ts": now - timedelta(minutes=1),
                "objective_text": "unrelated dashboard styling",
                "anchors_json": [],
                "source": "native",
                "status": "succeeded",
            },
            {
                "id": "b",
                "ts": now - timedelta(hours=4),
                "objective_text": "advisor runtime fallback and retry fix",
                "anchors_json": [{"file": "x.py", "line": 10}],
                "source": "native",
                "status": "succeeded",
            },
        ],
        query_text="continue codex work on advisor runtime fallback",
        k=5,
    )
    assert selected is not None
    assert selected["id"] == "b"
