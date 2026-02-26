from __future__ import annotations

from typing import Any

from tce_shared.situation import classify_situation


def extract_observations_from_events(
    events: list[dict[str, Any]],
    consumer_id: str,
    workspace_id: str = "default",
) -> list[dict[str, Any]]:
    if not events:
        return []

    observations: list[dict[str, Any]] = []

    for i, event in enumerate(events):
        event_type = event.get("event_type", "")
        title = event.get("title", "")
        decision = event.get("decision")
        outcome = event.get("outcome")

        # Direct decision events
        if decision and isinstance(decision, dict):
            situation_type = classify_situation(title)
            choice = decision.get("choice", title)
            reasoning = decision.get("reasoning", "")
            user_response = f"{choice}. {reasoning}".strip().rstrip(".")
            outcome_text = None
            outcome_sentiment = None
            if outcome and isinstance(outcome, dict):
                outcome_text = outcome.get("result", str(outcome))
                outcome_sentiment = outcome.get("sentiment")
            observations.append({
                "consumer_id": consumer_id,
                "workspace_id": workspace_id,
                "situation_type": situation_type,
                "situation_summary": title,
                "context_snapshot": event.get("payload", {}),
                "user_response": user_response,
                "response_reasoning": reasoning or None,
                "outcome": outcome_text,
                "outcome_sentiment": outcome_sentiment,
                "source_event_ids": [event.get("id")],
                "confidence": 1.0,
            })
            continue

        # Error -> action pairs
        if event_type == "error" and i + 1 < len(events):
            next_event = events[i + 1]
            next_type = next_event.get("event_type", "")
            if next_type in ("action", "fix", "decision", "resolution"):
                observations.append({
                    "consumer_id": consumer_id,
                    "workspace_id": workspace_id,
                    "situation_type": "error_occurred",
                    "situation_summary": title,
                    "context_snapshot": event.get("payload", {}),
                    "user_response": next_event.get("title", ""),
                    "response_reasoning": None,
                    "outcome": None,
                    "outcome_sentiment": None,
                    "source_event_ids": [event.get("id"), next_event.get("id")],
                    "confidence": 0.8,
                })

    return observations
