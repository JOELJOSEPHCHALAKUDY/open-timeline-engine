from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_text

BEHAVIOR_SCHEMA_VERSION = "v1"
BEHAVIOR_MEMORY_CLASSES = {
    "decision",
    "fact",
    "preference",
    "safety_constraint",
    "procedural_runbook",
    "episode",
    "hypothesis",
    "rejected_hypothesis",
}
BEHAVIOR_EVIDENCE_SOURCES = {"explicit", "inferred", "correction", "calibration", "backfill"}
BEHAVIOR_LIFECYCLE_STATUSES = {"active", "superseded", "rejected"}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./:-]{1,}")
_CONTEXT_NUMBER_RE = re.compile(r"\b\d+\b")
_MAX_CASE_RESULTS = 100
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "passwd",
    "private_key",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
}


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _redact_string(value: Any, limit: int) -> tuple[str, bool]:
    redacted, applied = redact_text(_clip(value, limit))
    return redacted.strip(), bool(applied)


def _redact_string_list(value: Any, *, limit: int, item_limit: int) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    output: list[str] = []
    seen: set[str] = set()
    redacted_any = False
    for raw in value[:limit]:
        item, applied = _redact_string(raw, item_limit)
        redacted_any = redacted_any or applied
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output, redacted_any


def _redact_structured(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        redacted_any = False
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized_key = key.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(
                ("_api_key", "_password", "_secret", "_token")
            ):
                output[key] = "<REDACTED:SENSITIVE_FIELD>"
                redacted_any = True
                continue
            redacted_value, applied = _redact_structured(raw_value)
            output[key] = redacted_value
            redacted_any = redacted_any or applied
        return output, redacted_any
    if isinstance(value, list):
        output_list: list[Any] = []
        redacted_any = False
        for raw_value in value:
            redacted_value, applied = _redact_structured(raw_value)
            output_list.append(redacted_value)
            redacted_any = redacted_any or applied
        return output_list, redacted_any
    if isinstance(value, str):
        redacted, redaction_types = redact_text(value)
        return redacted, bool(redaction_types)
    return value, False


def normalize_behavior_evidence(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Normalize and redact a Behavior Evidence v1 payload before persistence."""

    current = now or datetime.now(tz=UTC)
    redacted_any = False

    def redact_field(name: str, limit: int) -> str:
        nonlocal redacted_any
        value, applied = _redact_string(payload.get(name), limit)
        redacted_any = redacted_any or applied
        return value

    choices, choices_redacted = _redact_string_list(
        payload.get("available_choices"),
        limit=20,
        item_limit=300,
    )
    redacted_any = redacted_any or choices_redacted
    context_snapshot, context_redacted = _redact_structured(payload.get("context_snapshot") or {})
    constraints, constraints_redacted = _redact_structured(payload.get("constraints") or {})
    redacted_any = redacted_any or context_redacted or constraints_redacted

    source_event_ids = [str(value) for value in (payload.get("source_event_ids") or [])[:40] if str(value).strip()]
    contradicts_ids = [
        str(value) for value in (payload.get("contradicts_observation_ids") or [])[:40] if str(value).strip()
    ]
    memory_class = str(payload.get("memory_class") or "decision").strip().lower()
    if memory_class not in BEHAVIOR_MEMORY_CLASSES:
        memory_class = "decision"
    evidence_source = str(payload.get("evidence_source") or "explicit").strip().lower()
    if evidence_source not in BEHAVIOR_EVIDENCE_SOURCES:
        evidence_source = "explicit"
    lifecycle_status = str(payload.get("lifecycle_status") or "active").strip().lower()
    if lifecycle_status not in BEHAVIOR_LIFECYCLE_STATUSES:
        lifecycle_status = "active"

    selected_choice = redact_field("selected_choice", 500) or redact_field("user_response", 500)
    rationale = redact_field("rationale", 1000) or redact_field("response_reasoning", 1000)
    situation_summary = redact_field("situation_summary", 500)
    objective_text = redact_field("objective", 500) or situation_summary
    normalized = {
        "schema_version": BEHAVIOR_SCHEMA_VERSION,
        "situation_type": (redact_field("situation_type", 80) or "routine_task").lower(),
        "situation_summary": situation_summary,
        "objective_text": objective_text,
        "context_snapshot": context_snapshot if isinstance(context_snapshot, dict) else {},
        "constraints": constraints if isinstance(constraints, dict) else {},
        "available_choices": choices,
        "selected_choice": selected_choice,
        "rationale": rationale,
        "action_taken": redact_field("action_taken", 1000),
        "outcome": redact_field("outcome", 1000),
        "outcome_sentiment": redact_field("outcome_sentiment", 40).lower() or None,
        "correction_text": redact_field("correction_text", 1000),
        "memory_class": memory_class,
        "evidence_source": evidence_source,
        "lifecycle_status": lifecycle_status,
        "source_event_ids": source_event_ids,
        "confidence": max(0.0, min(1.0, float(payload.get("confidence", 1.0) or 0.0))),
        "valid_from": payload.get("valid_from") or current,
        "valid_until": payload.get("valid_until"),
        "confirmed_at": payload.get("confirmed_at"),
        "supersedes_observation_id": payload.get("supersedes_observation_id"),
        "contradicts_observation_ids": contradicts_ids,
        "redaction_applied": redacted_any,
    }
    return normalized


def validate_behavior_evidence(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not _clip(payload.get("situation_summary"), 500):
        errors.append("situation_summary is required")
    if not _clip(payload.get("objective_text") or payload.get("objective"), 500):
        errors.append("objective is required")
    if not _clip(payload.get("selected_choice") or payload.get("user_response"), 500):
        errors.append("selected_choice is required")
    evidence_source = str(payload.get("evidence_source") or "explicit").strip().lower()
    if evidence_source in {"explicit", "correction", "calibration"} and not _clip(
        payload.get("rationale") or payload.get("response_reasoning"), 1000
    ):
        errors.append(f"rationale is required for {evidence_source} evidence")
    if evidence_source == "correction":
        if not _clip(payload.get("correction_text"), 1000):
            errors.append("correction_text is required for correction evidence")
        if not payload.get("supersedes_observation_id"):
            errors.append("supersedes_observation_id is required for correction evidence")
    valid_from = payload.get("valid_from")
    valid_until = payload.get("valid_until")
    if isinstance(valid_from, datetime) and isinstance(valid_until, datetime) and valid_until <= valid_from:
        errors.append("valid_until must be later than valid_from")
    return errors


def behavior_storage_gate(payload: dict[str, Any], *, threshold: float = 0.55) -> dict[str, Any]:
    """Score whether evidence may influence learned behavior; raw evidence remains auditable."""

    source = str(payload.get("evidence_source") or "explicit").strip().lower()
    memory_class = str(payload.get("memory_class") or "decision").strip().lower()
    score = 0.20
    reasons: list[str] = []
    source_weights = {
        "correction": 0.40,
        "explicit": 0.35,
        "calibration": 0.30,
        "inferred": 0.10,
        "backfill": 0.05,
    }
    score += source_weights.get(source, 0.05)
    if payload.get("rationale"):
        score += 0.12
    else:
        reasons.append("missing_rationale")
    if payload.get("outcome"):
        score += 0.08
    if payload.get("available_choices"):
        score += 0.05
    if payload.get("action_taken"):
        score += 0.05
    confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0) or 0.0)))
    score += 0.10 * confidence
    if memory_class in {"safety_constraint", "procedural_runbook"} and source in {"explicit", "correction"}:
        score += 0.10
    if payload.get("lifecycle_status") != "active":
        score = 0.0
        reasons.append("inactive_lifecycle")
    if payload.get("valid_until") and _as_datetime(payload.get("valid_until")) <= datetime.now(tz=UTC):
        score = 0.0
        reasons.append("expired")
    score = round(max(0.0, min(1.0, score)), 4)
    eligible = score >= max(0.0, min(1.0, threshold))
    if not eligible and not reasons:
        reasons.append("below_learning_threshold")
    return {
        "score": score,
        "decision": "learn" if eligible else "audit_only",
        "learning_eligible": eligible,
        "reasons": reasons,
    }


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            result = datetime.min.replace(tzinfo=UTC)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _tokens(value: Any) -> set[str]:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, default=str)
    return set(_TOKEN_RE.findall(str(value or "").lower()))


def _choice_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _evidence_text(item: dict[str, Any]) -> str:
    return " ".join(
        [
            str(item.get("situation_type") or ""),
            str(item.get("situation_summary") or ""),
            str(item.get("objective_text") or item.get("objective") or ""),
            json.dumps(item.get("constraints") or {}, sort_keys=True, default=str),
            json.dumps(item.get("context_snapshot") or {}, sort_keys=True, default=str),
        ]
    )


def _active_evidence(
    item: dict[str, Any],
    *,
    at: datetime | None = None,
    historical: bool = False,
    rows_by_id: dict[str, dict[str, Any]] | None = None,
) -> bool:
    lifecycle_status = str(item.get("lifecycle_status") or "active")
    if lifecycle_status == "rejected":
        return False
    if lifecycle_status not in {"active", "superseded"}:
        return False
    if not bool(item.get("learning_eligible", True)):
        return False
    current = at or datetime.now(tz=UTC)
    superseded_by = str(item.get("superseded_by") or "").strip()
    if superseded_by:
        if not historical:
            return False
        successor = (rows_by_id or {}).get(superseded_by)
        if successor is not None:
            successor_effective_at = _as_datetime(successor.get("valid_from") or successor.get("ts"))
            if successor_effective_at <= current:
                return False
    elif lifecycle_status == "superseded":
        return False
    valid_from = _as_datetime(item.get("valid_from")) if item.get("valid_from") else None
    valid_until = _as_datetime(item.get("valid_until")) if item.get("valid_until") else None
    return not ((valid_from and valid_from > current) or (valid_until and valid_until <= current))


def _eligible_evidence_rows(
    evidence_rows: Iterable[dict[str, Any]],
    *,
    at: datetime | None = None,
    historical: bool = False,
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in evidence_rows]
    rows_by_id = {str(row.get("id")): row for row in rows if row.get("id")}
    active = [
        row
        for row in rows
        if _active_evidence(
            row,
            at=at,
            historical=historical,
            rows_by_id=rows_by_id,
        )
    ]
    contradicted_ids: set[str] = set()
    for row in active:
        if str(row.get("evidence_source") or "") not in {"explicit", "correction", "calibration"}:
            continue
        contradicted_ids.update(
            str(value)
            for value in (row.get("contradicts_observation_ids") or [])
            if str(value).strip()
        )
    return [row for row in active if str(row.get("id") or "") not in contradicted_ids]


def _similarity(query: dict[str, Any], evidence: dict[str, Any], *, newest_ts: datetime) -> float:
    query_tokens = _tokens(_evidence_text(query))
    evidence_tokens = _tokens(_evidence_text(evidence))
    union = query_tokens | evidence_tokens
    overlap = len(query_tokens & evidence_tokens) / max(1, len(union))
    same_situation = str(query.get("situation_type") or "") == str(evidence.get("situation_type") or "")
    evidence_ts = _as_datetime(evidence.get("ts"))
    age_days = max(0.0, (newest_ts - evidence_ts).total_seconds() / 86400.0)
    recency = math.exp(-math.log(2) * age_days / 90.0)
    source = str(evidence.get("evidence_source") or "inferred")
    source_quality = {"correction": 1.0, "explicit": 0.95, "calibration": 0.85, "inferred": 0.65}.get(source, 0.55)
    confidence = max(0.0, min(1.0, float(evidence.get("confidence", 0.5) or 0.5)))
    return round(
        (0.55 * overlap)
        + (0.18 if same_situation else 0.0)
        + (0.12 * recency)
        + (0.10 * source_quality)
        + (0.05 * confidence),
        6,
    )


def _map_choice(choice: str, allowed: list[str]) -> str:
    if not allowed:
        return choice
    choice_tokens = _tokens(choice)
    ranked: list[tuple[float, str]] = []
    for candidate in allowed:
        candidate_tokens = _tokens(candidate)
        overlap = len(choice_tokens & candidate_tokens) / max(1, len(choice_tokens | candidate_tokens))
        if _choice_key(choice) == _choice_key(candidate):
            overlap = 1.0
        ranked.append((overlap, candidate))
    ranked.sort(key=lambda item: (-item[0], _choice_key(item[1])))
    # A constrained prediction must never return a historical label that the
    # caller did not offer. Unmappable evidence is ignored and may cause an
    # abstention instead.
    return ranked[0][1] if ranked and ranked[0][0] >= 0.25 else ""


def _effective_sample_size(weights: Iterable[float]) -> float:
    values = [max(0.0, float(value)) for value in weights]
    total = sum(values)
    squared = sum(value * value for value in values)
    if total <= 0.0 or squared <= 0.0:
        return 0.0
    return (total * total) / squared


def _context_group_key(item: dict[str, Any]) -> str:
    normalized = _choice_key(_evidence_text(item))
    return _CONTEXT_NUMBER_RE.sub("<n>", normalized)


def _wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    probability = successes / total
    denominator = 1.0 + ((z * z) / total)
    center = (probability + ((z * z) / (2.0 * total))) / denominator
    margin = (
        z
        * math.sqrt((probability * (1.0 - probability) / total) + ((z * z) / (4.0 * total * total)))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def predict_behavior(
    evidence_rows: Iterable[dict[str, Any]],
    query: dict[str, Any],
    *,
    candidate_choices: list[str] | None = None,
    min_confidence: float = 0.55,
    max_neighbors: int = 12,
    historical_as_of: datetime | None = None,
) -> dict[str, Any]:
    rows = _eligible_evidence_rows(
        evidence_rows,
        at=historical_as_of,
        historical=historical_as_of is not None,
    )
    allowed = [str(value).strip() for value in (candidate_choices or []) if str(value).strip()][:20]
    if not rows:
        return {
            "predicted_choice": None,
            "ranked_choices": [],
            "confidence": 0.0,
            "abstained": True,
            "needs_clarification": True,
            "clarification_question": "What choice would you make here, and why?",
            "citations": [],
            "neighbor_count": 0,
            "effective_neighbor_count": 0.0,
            "out_of_distribution": True,
            "ood_score": 1.0,
            "predicted_action": None,
        }

    newest_ts = max((_as_datetime(row.get("ts")) for row in rows), default=datetime.now(tz=UTC))
    neighbors = sorted(
        [(_similarity(query, row, newest_ts=newest_ts), row) for row in rows],
        key=lambda item: (-item[0], -_as_datetime(item[1].get("ts")).timestamp(), str(item[1].get("id") or "")),
    )[: max(1, max_neighbors)]
    votes: dict[str, float] = defaultdict(float)
    labels: dict[str, str] = {}
    action_votes: dict[str, float] = defaultdict(float)
    citations: list[str] = []
    voting_weights: list[float] = []
    for similarity, row in neighbors:
        raw_choice = str(row.get("selected_choice") or row.get("user_response") or "").strip()
        if not raw_choice:
            continue
        mapped = _map_choice(raw_choice, allowed)
        if not mapped:
            continue
        key = _choice_key(mapped)
        labels.setdefault(key, mapped)
        weight = max(0.001, similarity)
        votes[key] += weight
        voting_weights.append(weight)
        action = str(row.get("action_taken") or "").strip()
        if action:
            action_votes[action] += max(0.001, similarity)
        if row.get("id"):
            citations.append(str(row["id"]))

    ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    total = sum(weight for _, weight in ranked)
    top_similarity = neighbors[0][0] if neighbors else 0.0
    vote_share = ranked[0][1] / total if ranked and total else 0.0
    confidence = round(min(1.0, vote_share * (0.45 + (0.55 * min(1.0, top_similarity)))), 4)
    ood_score = round(max(0.0, min(1.0, 1.0 - top_similarity)), 4)
    out_of_distribution = bool(neighbors and top_similarity < 0.45)
    ranked_choices = [
        {"choice": labels[key], "score": round(weight / max(total, 1e-9), 4)} for key, weight in ranked[:5]
    ]
    abstained = not ranked_choices or confidence < max(0.0, min(1.0, min_confidence))
    predicted_action = None
    if action_votes:
        predicted_action = sorted(action_votes.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return {
        "predicted_choice": None if abstained else ranked_choices[0]["choice"],
        "ranked_choices": ranked_choices,
        "confidence": confidence,
        "abstained": abstained,
        "needs_clarification": abstained,
        "clarification_question": (
            "Which option should be preferred here, and what constraint matters most?" if abstained else None
        ),
        "citations": citations[:20],
        "neighbor_count": len(neighbors),
        "effective_neighbor_count": round(_effective_sample_size(voting_weights), 4),
        "out_of_distribution": out_of_distribution,
        "ood_score": ood_score,
        "predicted_action": predicted_action,
    }


def _jaccard_text(left: Any, right: Any) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _calibration_error(samples: list[tuple[float, int]], bins: int = 5) -> float:
    if not samples:
        return 0.0
    total = len(samples)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        bucket = [
            (confidence, correct)
            for confidence, correct in samples
            if lower <= confidence < upper or (index == bins - 1 and confidence == upper)
        ]
        if not bucket:
            continue
        avg_confidence = sum(value[0] for value in bucket) / len(bucket)
        accuracy = sum(value[1] for value in bucket) / len(bucket)
        error += (len(bucket) / total) * abs(avg_confidence - accuracy)
    return round(error, 4)


def evaluate_behavior_fidelity(
    evidence_rows: Iterable[dict[str, Any]],
    *,
    holdout_ratio: float = 0.20,
    min_train: int = 5,
    min_confidence: float = 0.55,
    max_cases: int = 500,
) -> dict[str, Any]:
    """Run a chronological expanding-window benchmark without model calls."""

    started = datetime.now(tz=UTC)
    rows = [dict(row) for row in evidence_rows]
    rows_by_id = {str(row.get("id")): row for row in rows if row.get("id")}
    rows = [
        row
        for row in rows
        if _active_evidence(
            row,
            at=_as_datetime(row.get("ts")),
            historical=True,
            rows_by_id=rows_by_id,
        )
    ]
    rows = [row for row in rows if _choice_key(row.get("selected_choice") or row.get("user_response"))]
    rows.sort(key=lambda row: (_as_datetime(row.get("ts")), str(row.get("id") or "")))
    rows = rows[-max(min_train + 1, min(max_cases, 5000)) :]
    if len(rows) <= min_train:
        return {
            "status": "insufficient_data",
            "metrics": _empty_metrics(len(rows)),
            "gate": {
                "passed": False,
                "reason": f"need more than {min_train} eligible chronological observations",
                "minimum_samples": max(30, min_train + 1),
                "minimum_unique_contexts": 10,
                "uncertainty_method": "wilson_95",
            },
            "case_results": [],
            "duration_ms": 0,
        }

    holdout_count = max(1, int(math.ceil(len(rows) * max(0.05, min(0.50, holdout_ratio)))))
    split_index = max(min_train, len(rows) - holdout_count)
    train = rows[:split_index]
    tests = rows[split_index:]
    case_results: list[dict[str, Any]] = []
    top1_hits = 0
    top3_hits = 0
    pairwise_hits = 0
    pairwise_total = 0
    brier_samples: list[tuple[float, int]] = []
    non_abstained_correct = 0
    non_abstained_count = 0
    wrong_cases = 0
    wrong_abstained = 0
    workflow_scores: list[float] = []
    regret_scores: list[float] = []
    majority_hits = 0
    evaluation_context_keys: list[str] = []

    for test in tests:
        available = [str(value) for value in (test.get("available_choices") or []) if str(value).strip()]
        query = {
            "situation_type": test.get("situation_type"),
            "situation_summary": test.get("situation_summary"),
            "objective_text": test.get("objective_text"),
            "constraints": test.get("constraints") or {},
            "context_snapshot": test.get("context_snapshot") or {},
        }
        prediction = predict_behavior(
            train,
            query,
            candidate_choices=available,
            min_confidence=min_confidence,
            historical_as_of=_as_datetime(test.get("ts")),
        )
        actual = str(test.get("selected_choice") or test.get("user_response") or "").strip()
        actual_key = _choice_key(actual)
        evaluation_context_keys.append(_context_group_key(test))
        ranked_keys = [_choice_key(item.get("choice")) for item in prediction["ranked_choices"]]
        predicted_key = _choice_key(prediction.get("predicted_choice"))
        correct = int(bool(predicted_key) and predicted_key == actual_key)
        top3 = int(actual_key in ranked_keys[:3])
        top1_hits += correct
        top3_hits += top3
        if not prediction["abstained"]:
            non_abstained_count += 1
            non_abstained_correct += correct
        if not correct:
            wrong_cases += 1
            wrong_abstained += int(prediction["abstained"])
        confidence = float(prediction["confidence"])
        brier_samples.append((confidence, correct))

        alternatives = [choice for choice in available if _choice_key(choice) != actual_key]
        if alternatives:
            actual_rank = ranked_keys.index(actual_key) if actual_key in ranked_keys else len(ranked_keys) + 1
            for alternative in alternatives:
                alt_key = _choice_key(alternative)
                alt_rank = ranked_keys.index(alt_key) if alt_key in ranked_keys else len(ranked_keys) + 1
                pairwise_hits += int(actual_rank < alt_rank)
                pairwise_total += 1

        predicted_action = prediction.get("predicted_action")
        actual_action = test.get("action_taken")
        if predicted_action and actual_action:
            workflow_scores.append(_jaccard_text(predicted_action, actual_action))

        sentiment = str(test.get("outcome_sentiment") or "").lower()
        if not correct:
            regret_scores.append(1.0 if sentiment in {"positive", "success", "succeeded"} else 0.25)
        else:
            regret_scores.append(0.0)

        same_type_choices = [
            _choice_key(row.get("selected_choice") or row.get("user_response"))
            for row in train
            if str(row.get("situation_type") or "") == str(test.get("situation_type") or "")
        ]
        if not same_type_choices:
            same_type_choices = [_choice_key(row.get("selected_choice") or row.get("user_response")) for row in train]
        majority = Counter(same_type_choices).most_common(1)[0][0] if same_type_choices else ""
        majority_hits += int(majority == actual_key)

        case_results.append(
            {
                "observation_id": str(test.get("id") or ""),
                "ts": _as_datetime(test.get("ts")).isoformat(),
                "situation_type": str(test.get("situation_type") or ""),
                "actual_choice": actual,
                "predicted_choice": prediction.get("predicted_choice"),
                "confidence": confidence,
                "abstained": bool(prediction["abstained"]),
                "top1_correct": bool(correct),
                "top3_correct": bool(top3),
                "citations": prediction["citations"],
            }
        )
        train.append(test)

    evaluated = len(tests)
    top1_accuracy = top1_hits / max(1, evaluated)
    top3_accuracy = top3_hits / max(1, evaluated)
    majority_accuracy = majority_hits / max(1, evaluated)
    midpoint = max(1, evaluated // 2)
    early = case_results[:midpoint]
    recent = case_results[midpoint:] or early
    early_accuracy = sum(int(item["top1_correct"]) for item in early) / max(1, len(early))
    recent_accuracy = sum(int(item["top1_correct"]) for item in recent) / max(1, len(recent))
    top1_lower, top1_upper = _wilson_interval(top1_hits, evaluated)
    majority_lower, majority_upper = _wilson_interval(majority_hits, evaluated)
    precision_lower, precision_upper = _wilson_interval(non_abstained_correct, non_abstained_count)
    unique_context_count = len(set(evaluation_context_keys))
    duplicate_context_ratio = 1.0 - (unique_context_count / max(1, evaluated))
    conservative_lift_lower = top1_lower - majority_upper
    brier_score = sum((confidence - correct) ** 2 for confidence, correct in brier_samples) / max(1, evaluated)
    metrics = {
        "eligible_evidence_count": len(rows),
        "training_count": split_index,
        "evaluation_count": evaluated,
        "top1_accuracy": round(top1_accuracy, 4),
        "top1_accuracy_ci95": [round(top1_lower, 4), round(top1_upper, 4)],
        "top3_accuracy": round(top3_accuracy, 4),
        "pairwise_preference_accuracy": round(pairwise_hits / max(1, pairwise_total), 4),
        "brier_score": round(brier_score, 4),
        "expected_calibration_error": _calibration_error(brier_samples),
        "abstention_rate": round(sum(int(item["abstained"]) for item in case_results) / max(1, evaluated), 4),
        "non_abstained_precision": round(non_abstained_correct / max(1, non_abstained_count), 4),
        "non_abstained_precision_ci95": [round(precision_lower, 4), round(precision_upper, 4)],
        "wrong_case_abstention_rate": round(wrong_abstained / max(1, wrong_cases), 4),
        "workflow_similarity": round(sum(workflow_scores) / max(1, len(workflow_scores)), 4),
        "outcome_regret_proxy": round(sum(regret_scores) / max(1, len(regret_scores)), 4),
        "majority_baseline_accuracy": round(majority_accuracy, 4),
        "majority_baseline_accuracy_ci95": [round(majority_lower, 4), round(majority_upper, 4)],
        "lift_over_majority": round(top1_accuracy - majority_accuracy, 4),
        "conservative_lift_lower_bound": round(conservative_lift_lower, 4),
        "unique_evaluation_contexts": unique_context_count,
        "duplicate_context_ratio": round(duplicate_context_ratio, 4),
        "early_top1_accuracy": round(early_accuracy, 4),
        "recent_top1_accuracy": round(recent_accuracy, 4),
        "drift_adaptation_delta": round(recent_accuracy - early_accuracy, 4),
    }
    minimum_samples = 30
    minimum_unique_contexts = 10
    passed = (
        evaluated >= minimum_samples
        and unique_context_count >= minimum_unique_contexts
        and top1_lower >= 0.65
        and brier_score <= 0.25
        and precision_lower >= 0.70
        and conservative_lift_lower >= 0.05
    )
    gate = {
        "passed": passed,
        "reason": "validated_behavior_fidelity" if passed else "insufficient_or_uncalibrated_fidelity",
        "minimum_samples": minimum_samples,
        "minimum_unique_contexts": minimum_unique_contexts,
        "uncertainty_method": "wilson_95",
        "thresholds": {
            "top1_accuracy_lower_bound": 0.65,
            "brier_score_max": 0.25,
            "non_abstained_precision_lower_bound": 0.70,
            "conservative_lift_lower_bound": 0.05,
            "lift_over_majority": 0.05,
        },
    }
    duration_ms = max(0, int((datetime.now(tz=UTC) - started).total_seconds() * 1000))
    return {
        "status": "completed",
        "metrics": metrics,
        "gate": gate,
        "case_results": case_results[-_MAX_CASE_RESULTS:],
        "duration_ms": duration_ms,
    }


def _empty_metrics(evidence_count: int) -> dict[str, Any]:
    return {
        "eligible_evidence_count": evidence_count,
        "training_count": evidence_count,
        "evaluation_count": 0,
        "top1_accuracy": 0.0,
        "top1_accuracy_ci95": [0.0, 1.0],
        "top3_accuracy": 0.0,
        "pairwise_preference_accuracy": 0.0,
        "brier_score": 0.0,
        "expected_calibration_error": 0.0,
        "abstention_rate": 1.0,
        "non_abstained_precision": 0.0,
        "non_abstained_precision_ci95": [0.0, 1.0],
        "wrong_case_abstention_rate": 0.0,
        "workflow_similarity": 0.0,
        "outcome_regret_proxy": 0.0,
        "majority_baseline_accuracy": 0.0,
        "majority_baseline_accuracy_ci95": [0.0, 1.0],
        "lift_over_majority": 0.0,
        "conservative_lift_lower_bound": -1.0,
        "unique_evaluation_contexts": 0,
        "duplicate_context_ratio": 1.0,
        "early_top1_accuracy": 0.0,
        "recent_top1_accuracy": 0.0,
        "drift_adaptation_delta": 0.0,
    }


CALIBRATION_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "production-risk",
        "situation_type": "escalation_point",
        "objective": "Deploy a high-impact change with incomplete verification",
        "choices": ["pause and complete verification", "deploy with monitoring", "deploy immediately"],
        "memory_class": "safety_constraint",
    },
    {
        "id": "speed-quality",
        "situation_type": "prioritization_needed",
        "objective": "Choose between a minimal fix and a broader refactor",
        "choices": ["minimal verified fix", "broader refactor", "ask for more context"],
        "memory_class": "preference",
    },
    {
        "id": "uncertain-requirement",
        "situation_type": "unknown_territory",
        "objective": "Continue when the requirement is ambiguous",
        "choices": ["ask a focused question", "make a reversible assumption", "continue without clarification"],
        "memory_class": "preference",
    },
    {
        "id": "failure-response",
        "situation_type": "error_occurred",
        "objective": "Respond after the first implementation attempt fails",
        "choices": ["diagnose and narrow scope", "retry unchanged", "rewrite the whole component"],
        "memory_class": "procedural_runbook",
    },
)
