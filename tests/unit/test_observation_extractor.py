from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from tce_api.observation_extractor import extract_observations_from_events


def _event(event_type: str, title: str, decision: dict | None = None, outcome: dict | None = None) -> dict:
    return {
        "id": str(uuid4()),
        "ts": datetime.now(tz=UTC).isoformat(),
        "event_type": event_type,
        "title": title,
        "payload": {},
        "decision": decision,
        "outcome": outcome,
    }


def test_extract_from_decision_event():
    events = [
        _event(
            "decision",
            "Should we choose Redis or Memcached for session storage",
            decision={"choice": "Redis", "reasoning": "Better persistence support"},
            outcome={"result": "success", "sentiment": "positive"},
        )
    ]
    observations = extract_observations_from_events(events, consumer_id="user-1")
    assert len(observations) == 1
    assert observations[0]["situation_type"] == "choice_required"
    assert "Redis" in observations[0]["user_response"]


def test_extract_from_error_response_pair():
    events = [
        _event("error", "Build failed: TypeError in auth module"),
        _event("action", "Investigated root cause and fixed auth module type error"),
    ]
    observations = extract_observations_from_events(events, consumer_id="user-1")
    assert len(observations) >= 1
    obs = observations[0]
    assert obs["situation_type"] == "error_occurred"


def test_empty_events_returns_empty():
    assert extract_observations_from_events([], consumer_id="user-1") == []
