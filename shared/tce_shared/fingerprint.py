from __future__ import annotations

import copy
from typing import Any

DEFAULT_FINGERPRINT: dict[str, Any] = {
    "decision_making": {
        "risk_tolerance": "moderate",
        "speed_vs_thoroughness": 0.5,
        "delegation_tendency": 0.5,
        "conflict_resolution_style": "compromise",
        "decision_reversal_frequency": 0.2,
        "information_needs_before_deciding": "moderate",
    },
    "communication": {
        "verbosity": "moderate",
        "formality": "neutral",
        "emoji_usage": False,
        "preferred_response_length": "medium",
        "explanation_depth": "moderate",
        "tone_under_pressure": "calm",
    },
    "priorities": {
        "speed_vs_quality": 0.5,
        "user_experience_vs_technical": 0.5,
        "pragmatic_vs_principled": 0.5,
        "top_recurring_concerns": [],
    },
    "context_switching": {
        "multitask_tolerance": "moderate",
        "interruption_handling": "accept",
        "context_retention_depth": "moderate",
    },
    "learning_style": {
        "exploration_vs_exploitation": 0.5,
        "feedback_response": "neutral",
        "mistake_handling": "investigate_root_cause",
    },
    "emotional_patterns": {
        "frustration_triggers": [],
        "satisfaction_signals": [],
        "stress_indicators": [],
    },
}


def extract_communication_signals(text: str) -> dict[str, str]:
    word_count = len(text.split())
    if word_count < 10:
        verbosity = "terse"
    elif word_count < 40:
        verbosity = "moderate"
    else:
        verbosity = "verbose"

    has_emoji = any(ord(ch) > 0x1F600 for ch in text)
    formality = "casual" if any(w in text.lower() for w in ("lol", "haha", "yeah", "nah", "ok")) else "neutral"

    return {
        "verbosity": verbosity,
        "emoji_usage": str(has_emoji).lower(),
        "formality": formality,
    }


def _update_running_average(current: float, new_value: float, alpha: float = 0.15) -> float:
    return round(current * (1 - alpha) + new_value * alpha, 3)


def feedback_adjusted_alpha(
    base_alpha: float,
    feedback_type: str | None,
    *,
    alpha_min: float = 0.08,
    alpha_max: float = 0.35,
) -> float:
    normalized = (feedback_type or "").strip().lower()
    if normalized in {"helpful", "positive", "correct"}:
        adjusted = base_alpha * 0.75
    elif normalized in {"unhelpful", "negative", "wrong"}:
        adjusted = base_alpha * 1.35
    else:
        adjusted = base_alpha
    return max(alpha_min, min(alpha_max, round(adjusted, 4)))


def apply_feedback_to_fingerprint(
    fingerprint: dict[str, Any],
    *,
    feedback_type: str | None,
    correction_text: str | None = None,
    base_alpha: float = 0.15,
    alpha_min: float = 0.08,
    alpha_max: float = 0.35,
) -> dict[str, Any]:
    fp = copy.deepcopy(fingerprint)
    normalized = (feedback_type or "").strip().lower()
    if normalized not in {"unhelpful", "negative", "wrong"}:
        return fp

    alpha = feedback_adjusted_alpha(base_alpha, normalized, alpha_min=alpha_min, alpha_max=alpha_max)
    text = (correction_text or "").lower()

    if any(token in text for token in ("faster", "quick", "ship", "direct", "concise")):
        fp["decision_making"]["speed_vs_thoroughness"] = _update_running_average(
            float(fp["decision_making"]["speed_vs_thoroughness"]),
            0.25,
            alpha,
        )
        fp["priorities"]["speed_vs_quality"] = _update_running_average(
            float(fp["priorities"]["speed_vs_quality"]),
            0.25,
            alpha,
        )
        fp["communication"]["preferred_response_length"] = "short"
    if any(token in text for token in ("thorough", "careful", "detailed", "safe", "deep")):
        fp["decision_making"]["speed_vs_thoroughness"] = _update_running_average(
            float(fp["decision_making"]["speed_vs_thoroughness"]),
            0.85,
            alpha,
        )
        fp["priorities"]["speed_vs_quality"] = _update_running_average(
            float(fp["priorities"]["speed_vs_quality"]),
            0.85,
            alpha,
        )
        fp["communication"]["preferred_response_length"] = "long"
    if any(token in text for token in ("less verbose", "too long", "shorter")):
        fp["communication"]["verbosity"] = "terse"
        fp["communication"]["preferred_response_length"] = "short"
    if any(token in text for token in ("more context", "more detail", "explain why")):
        fp["communication"]["verbosity"] = "verbose"
        fp["communication"]["preferred_response_length"] = "long"
        fp["communication"]["explanation_depth"] = "deep"
    if any(token in text for token in ("take risks", "bold", "aggressive")):
        fp["decision_making"]["risk_tolerance"] = "aggressive"
    if any(token in text for token in ("conservative", "safe option", "low risk")):
        fp["decision_making"]["risk_tolerance"] = "conservative"

    return fp


def merge_observation_into_fingerprint(
    fingerprint: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    fp = copy.deepcopy(fingerprint)
    situation_type = observation.get("situation_type", "")
    user_response = observation.get("user_response", "")
    outcome_sentiment = observation.get("outcome_sentiment", "")

    # Communication signals
    comm = extract_communication_signals(user_response)
    fp["communication"]["verbosity"] = comm["verbosity"]
    if comm["formality"] != "neutral":
        fp["communication"]["formality"] = comm["formality"]

    # Decision making signals from situation type
    response_lower = user_response.lower()

    if situation_type == "error_occurred":
        if "investigate" in response_lower or "root cause" in response_lower:
            fp["learning_style"]["mistake_handling"] = "investigate_root_cause"
            fp["decision_making"]["speed_vs_thoroughness"] = _update_running_average(
                fp["decision_making"]["speed_vs_thoroughness"], 0.8
            )
        elif "fix" in response_lower and "quick" in response_lower:
            fp["learning_style"]["mistake_handling"] = "fix_silently"
            fp["decision_making"]["speed_vs_thoroughness"] = _update_running_average(
                fp["decision_making"]["speed_vs_thoroughness"], 0.2
            )

    if situation_type == "choice_required":
        if "safe" in response_lower or "conservative" in response_lower:
            fp["decision_making"]["risk_tolerance"] = "conservative"
        elif "try" in response_lower or "experiment" in response_lower:
            fp["decision_making"]["risk_tolerance"] = "aggressive"

    if situation_type == "prioritization_needed":
        if "quality" in response_lower or "thorough" in response_lower:
            fp["priorities"]["speed_vs_quality"] = _update_running_average(
                fp["priorities"]["speed_vs_quality"], 0.8
            )
        elif "fast" in response_lower or "ship" in response_lower:
            fp["priorities"]["speed_vs_quality"] = _update_running_average(
                fp["priorities"]["speed_vs_quality"], 0.2
            )

    # Emotional pattern tracking
    if outcome_sentiment == "negative":
        triggers = fp["emotional_patterns"]["frustration_triggers"]
        if situation_type not in triggers:
            triggers.append(situation_type)
            fp["emotional_patterns"]["frustration_triggers"] = triggers[-5:]
    elif outcome_sentiment == "positive":
        signals = fp["emotional_patterns"]["satisfaction_signals"]
        if situation_type not in signals:
            signals.append(situation_type)
            fp["emotional_patterns"]["satisfaction_signals"] = signals[-5:]

    return fp
