from __future__ import annotations

from datetime import UTC, datetime


def recency_decay(event_ts: datetime, half_life_days: int = 30) -> float:
    delta_days = max(0.0, (datetime.now(tz=UTC) - event_ts).total_seconds() / 86400)
    return float(0.5 ** (delta_days / half_life_days))


def weighted_confidence(
    frequency: float,
    recency: float,
    consistency: float,
    feedback_signal: float,
    weights: tuple[float, float, float, float] | None = None,
) -> float:
    w_freq, w_recency, w_consistency, w_feedback = weights or (0.35, 0.30, 0.20, 0.15)
    score = (
        (w_freq * frequency)
        + (w_recency * recency)
        + (w_consistency * consistency)
        + (w_feedback * feedback_signal)
    )
    return float(max(0.0, min(1.0, score)))


def confidence_status(score: float) -> str:
    if score >= 0.8:
        return "active"
    if score >= 0.5:
        return "needs_review"
    return "suppressed"
