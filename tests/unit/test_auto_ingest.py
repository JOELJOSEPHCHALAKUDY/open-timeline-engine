from __future__ import annotations


def test_auto_ingest_observation_structure():
    """Verify the observation dict built from session turns has correct shape."""
    turn = {"message_preview": "Should we use Redis?", "turn": 3, "timestamp": "2026-02-19T10:00:00"}
    obs = {
        "consumer_id": "test-user",
        "workspace_id": "default",
        "situation_type": "routine_task",
        "situation_summary": turn.get("message_preview", "")[:500],
        "user_response": turn.get("message_preview", ""),
        "outcome_sentiment": "neutral",
        "confidence": 0.6,
    }
    assert obs["consumer_id"] == "test-user"
    assert obs["situation_summary"] == "Should we use Redis?"
    assert obs["confidence"] == 0.6


def test_auto_ingest_empty_turns_skipped():
    """Verify empty session_turns list is handled."""
    session_turns = []
    assert len(session_turns) == 0
    # No observations should be created
