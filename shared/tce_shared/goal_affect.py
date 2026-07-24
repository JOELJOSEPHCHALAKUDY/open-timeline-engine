from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _recency_decay(ts: datetime | None, half_life_days: float) -> float:
    if ts is None:
        return 0.0
    now = datetime.now(tz=UTC)
    age_days = max(0.0, (now - ts.astimezone(UTC)).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 0.0
    return math.pow(0.5, age_days / half_life_days)


def compute_affective_scores(
    *,
    source: str,
    evidence_count: int,
    rehearsal_count: int,
    confidence: float,
    urgency: float,
    recency: float,
    blocker_impact: float,
    success_probability: float,
    recent_outcomes: list[dict[str, Any]] | None,
    fingerprint: dict[str, Any] | None,
    similarity: dict[str, float] | None = None,
) -> dict[str, Any]:
    outcomes = recent_outcomes or []
    fp = fingerprint or {}

    # Temporal
    pain_hint = 0.0
    anger_hint = 0.0
    success_hint = 0.0
    failure_streak = 0
    for row in outcomes[-20:]:
        result = str((row or {}).get("result", "")).strip().lower()
        ts = None
        ts_raw = (row or {}).get("ts")
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except Exception:
                ts = None
        if result in {"failure", "blocked", "needs_human"}:
            failure_streak += 1
            decay = _recency_decay(ts, 14.0) if ts else 1.0
            pain_hint += decay
        elif result == "success":
            failure_streak = 0
            decay = _recency_decay(ts, 21.0) if ts else 1.0
            success_hint += decay
    if pain_hint > 0:
        anger_hint = pain_hint * math.log1p(max(1, failure_streak))

    base_forget = 0.7 if str(source).strip().lower() == "open_discovery" else 0.3
    evidence_anchor = min(0.3, max(0, evidence_count) * 0.05)
    emotional_anchor = min(0.2, (min(1.0, pain_hint) + min(1.0, anger_hint)) * 0.15)
    forget = _clamp(base_forget - evidence_anchor - emotional_anchor, 0.05, 0.95)
    dream = _clamp((max(0, rehearsal_count) / 10.0) * 0.55 + (1.0 - urgency) * 0.45)
    rehearsal_strength = _clamp(1.0 - (0.5 ** (max(0, rehearsal_count) / 3.0)))

    # Emotional
    pain = _clamp(math.tanh(0.4 * pain_hint))
    happy = _clamp(math.tanh(0.35 * success_hint))
    frustration_triggers = (
        ((fp.get("emotional_patterns") or {}).get("frustration_triggers") or [])
        if isinstance(fp, dict)
        else []
    )
    trigger_match = 1.0 if frustration_triggers else 0.0
    anger = _clamp(math.tanh((0.4 * pain * math.log1p(max(1, failure_streak))) + (0.2 * trigger_match)))
    risk_pref = str((fp.get("decision_making") or {}).get("risk_tolerance", "moderate")).lower()
    risk_modifier = 1.2 if risk_pref in {"conservative", "safe"} else 0.95 if risk_pref in {"aggressive", "bold"} else 1.0
    uncertainty = _clamp(1.0 - confidence)
    anxiety_raw = (0.3 * blocker_impact * uncertainty) + (0.25 * (1.0 - success_probability))
    anxiety = _clamp(math.tanh(anxiety_raw * risk_modifier))

    # Behavioral
    recent_success_rate = _clamp(success_hint / max(1.0, (success_hint + pain_hint)))
    momentum = _clamp((0.55 * recent_success_rate) + (0.45 * recency))
    completion_proximity = _clamp((0.6 * confidence) + (0.4 * rehearsal_strength))
    distraction = _clamp(1.0 - ((urgency * 0.45) + (momentum * 0.35) + (rehearsal_strength * 0.20)))

    # Meta/Human
    curiosity_base = _as_float(((fp.get("learning_style") or {}).get("exploration_vs_exploitation")), 0.5)
    curiosity = _clamp((0.6 * curiosity_base) + (0.4 * (1.0 - confidence)))
    social = _clamp(0.15 + (0.25 if "review" in str(source).lower() else 0.0))
    top_concerns = ((fp.get("priorities") or {}).get("top_recurring_concerns") or [])
    identity = _clamp(0.35 + (0.25 if top_concerns else 0.0) + (0.25 * rehearsal_strength))
    guilt = _clamp((0.4 * (1.0 - recency)) + (0.3 * (1.0 - momentum)) + (0.3 * social)) * (1.0 - happy)
    multitask = str((fp.get("context_switching") or {}).get("multitask_tolerance", "moderate")).lower()
    multitask_penalty = 0.6 if multitask in {"low", "strict"} else 0.45 if multitask == "moderate" else 0.3
    cognitive_load = _clamp((0.4 * blocker_impact) + (0.35 * (1.0 - success_probability)) + (0.25 * multitask_penalty))
    overwhelm = _clamp((0.45 * anxiety) + (0.3 * distraction) + (0.25 * cognitive_load))
    is_unknown = confidence < 0.55 and evidence_count < 3
    is_nothing = overwhelm >= 0.60
    exploration_bonus = _clamp(curiosity * 0.15) if is_unknown else 0.0

    sim = similarity or {}
    context_relevance = _clamp(_as_float(sim.get("context_relevance", 0.5), 0.5))
    recent_event_affinity = _clamp(_as_float(sim.get("recent_event_affinity", recency), recency))
    entity_overlap = _clamp(_as_float(sim.get("entity_overlap", min(1.0, evidence_count / 8.0)), min(1.0, evidence_count / 8.0)))
    pattern_confidence = _clamp(_as_float(sim.get("pattern_confidence", confidence), confidence))
    nearest_goal_distance = _clamp(_as_float(sim.get("nearest_goal_distance", 0.7), 0.7))
    evidence_depth = _clamp(_as_float(sim.get("evidence_depth", min(1.0, evidence_count / 12.0)), min(1.0, evidence_count / 12.0)))

    return {
        "temporal": {
            "forget": round(forget, 4),
            "dream": round(dream, 4),
            "rehearsal_count": int(max(0, rehearsal_count)),
            "rehearsal_strength": round(rehearsal_strength, 4),
        },
        "emotional": {
            "pain": round(pain, 4),
            "happy": round(happy, 4),
            "anger": round(anger, 4),
            "anxiety": round(anxiety, 4),
        },
        "behavioral": {
            "distraction": round(distraction, 4),
            "momentum": round(momentum, 4),
            "completion_proximity": round(completion_proximity, 4),
        },
        "meta": {
            "unknown_goal": bool(is_unknown),
            "nothing_goal": bool(is_nothing),
            "exploration_bonus": round(exploration_bonus, 4),
            "overwhelm": round(overwhelm, 4),
        },
        "human": {
            "curiosity": round(curiosity, 4),
            "social": round(social, 4),
            "identity": round(identity, 4),
            "guilt": round(guilt, 4),
            "cognitive_load": round(cognitive_load, 4),
        },
        "similarity": {
            "context_relevance": round(context_relevance, 4),
            "recent_event_affinity": round(recent_event_affinity, 4),
            "entity_overlap": round(entity_overlap, 4),
            "pattern_confidence": round(pattern_confidence, 4),
            "nearest_goal_distance": round(nearest_goal_distance, 4),
            "evidence_depth": round(evidence_depth, 4),
        },
        "computed_at": datetime.now(tz=UTC).isoformat(),
        "version": 1,
    }


def classify_goal_kind(affective_scores: dict[str, Any]) -> str:
    meta = affective_scores.get("meta", {}) if isinstance(affective_scores, dict) else {}
    if bool(meta.get("nothing_goal")):
        return "nothing"
    if bool(meta.get("unknown_goal")):
        return "unknown"
    return "normal"


def score_goal_affective(
    *,
    urgency: float,
    recency: float,
    blocker_impact: float,
    success_probability: float,
    affective_scores: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    temporal = affective_scores.get("temporal", {}) if isinstance(affective_scores, dict) else {}
    emotional = affective_scores.get("emotional", {}) if isinstance(affective_scores, dict) else {}
    behavioral = affective_scores.get("behavioral", {}) if isinstance(affective_scores, dict) else {}
    meta = affective_scores.get("meta", {}) if isinstance(affective_scores, dict) else {}
    human = affective_scores.get("human", {}) if isinstance(affective_scores, dict) else {}
    sim = affective_scores.get("similarity", {}) if isinstance(affective_scores, dict) else {}

    completion = _as_float(behavioral.get("completion_proximity"), 0.4)
    rehearsal_strength = _as_float(temporal.get("rehearsal_strength"), 0.3)
    base = (
        0.25 * _clamp(urgency)
        + 0.15 * _clamp(recency)
        + 0.15 * _clamp(blocker_impact)
        + 0.10 * _clamp(success_probability)
        + 0.20 * _clamp(completion)
        + 0.15 * _clamp(rehearsal_strength)
    )

    pain = _as_float(emotional.get("pain"), 0.0)
    anger = _as_float(emotional.get("anger"), 0.0)
    happy = _as_float(emotional.get("happy"), 0.0)
    anxiety = _as_float(emotional.get("anxiety"), 0.0)
    momentum = _as_float(behavioral.get("momentum"), 0.0)
    curiosity = _as_float(human.get("curiosity"), 0.0)
    identity = _as_float(human.get("identity"), 0.0)
    guilt = _as_float(human.get("guilt"), 0.0)
    social = _as_float(human.get("social"), 0.0)
    cognitive_load = _as_float(human.get("cognitive_load"), 0.0)
    distraction = _as_float(behavioral.get("distraction"), 0.0)
    forget = _as_float(temporal.get("forget"), 0.6)
    dream = _as_float(temporal.get("dream"), 0.0)
    pattern_confidence = _as_float(sim.get("pattern_confidence"), 0.5)
    context_relevance = _as_float(sim.get("context_relevance"), 0.5)
    recent_affinity = _as_float(sim.get("recent_event_affinity"), 0.5)
    exploration_bonus = _as_float(meta.get("exploration_bonus"), 0.0)

    anxiety_mod = anxiety * (1.0 - anxiety) * 0.2
    emotional_multiplier = 1.0 + (0.3 * pain) + (0.4 * anger) + (0.25 * happy * momentum) + anxiety_mod
    modulated = base * emotional_multiplier

    distraction_resistance = max(anger, curiosity) * 0.5
    effective_distraction = distraction * (1.0 - distraction_resistance) * 0.3
    momentum_bonus = momentum * 0.2
    dream_floor = dream * 0.15
    identity_bonus = identity * 0.10
    guilt_lift = guilt * 0.12
    social_lift = social * 0.08
    load_penalty = max(0.0, cognitive_load - 0.7) * 0.1
    similarity_bonus = (0.1 * context_relevance) + (0.06 * recent_affinity) + (0.06 * pattern_confidence)

    age_decay = 0.5 ** (1.0 / max(1.5, 30.0 * (1.0 - _clamp(forget, 0.05, 0.95))))
    raw_final = (
        modulated
        - effective_distraction
        + momentum_bonus
        + exploration_bonus
        + identity_bonus
        + guilt_lift
        + social_lift
        + similarity_bonus
        - load_penalty
    ) * age_decay
    final = _clamp(max(dream_floor, raw_final))

    if bool(meta.get("nothing_goal")):
        final = _clamp(final * 0.35)
    breakdown = {
        "base": round(base, 4),
        "modulated": round(modulated, 4),
        "raw_final": round(raw_final, 4),
        "dream_floor": round(dream_floor, 4),
        "final": round(final, 4),
    }
    return round(final, 4), breakdown
