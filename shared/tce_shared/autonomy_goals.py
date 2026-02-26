from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .events import (
    AutonomyPolicyProfile,
    AutonomyRiskTier,
    ExecutionPermitDecision,
)

_CRITICAL_TERMS = (
    "rm -rf",
    "drop table",
    "truncate ",
    "delete all",
    "reset --hard",
    "force push",
    "production deploy",
    "rotate key",
    "secret",
    "credential",
)
_HIGH_TERMS = (
    "delete ",
    "rename ",
    "migrate",
    "schema",
    "infra",
    "deploy",
    "rollback",
)
_DESTRUCTIVE_ACTION_TERMS = ("delete", "drop", "truncate", "erase", "wipe", "destroy", "purge", "nuke")
_CRITICAL_OBJECT_TERMS = ("database", "db", "prod", "production", "cluster", "bucket", "table", "tables")
_NEGATION_TOKENS = {"not", "never", "dont", "don't", "without", "avoid"}


def _contains_action_object(normalized: str) -> bool:
    tokens = [token for token in normalized.replace("/", " ").replace("-", " ").split() if token]
    token_set = set(tokens)
    if not (token_set & set(_DESTRUCTIVE_ACTION_TERMS)):
        return False
    if not (token_set & set(_CRITICAL_OBJECT_TERMS)):
        return False
    for idx, token in enumerate(tokens):
        if token not in _DESTRUCTIVE_ACTION_TERMS:
            continue
        start = max(0, idx - 3)
        if any(tokens[pos] in _NEGATION_TOKENS for pos in range(start, idx)):
            continue
        return True
    return False


def score_goal(
    urgency: float,
    recency: float,
    blocker_impact: float,
    success_probability: float,
) -> float:
    raw = (0.35 * urgency) + (0.25 * recency) + (0.20 * blocker_impact) + (0.20 * success_probability)
    return max(0.0, min(1.0, round(raw, 4)))


def classify_risk_tier(
    action_kind: str,
    target_paths: list[str] | None,
    command_preview: str | None,
    estimated_change_size: int,
) -> AutonomyRiskTier:
    normalized = " ".join(
        [
            str(action_kind or "").strip().lower(),
            str(command_preview or "").strip().lower(),
            " ".join(str(item).lower() for item in (target_paths or [])),
        ]
    )
    if _contains_action_object(normalized):
        return AutonomyRiskTier.CRITICAL
    if any(token in normalized for token in _CRITICAL_TERMS):
        return AutonomyRiskTier.CRITICAL
    if any(token in normalized for token in _HIGH_TERMS):
        return AutonomyRiskTier.HIGH
    if estimated_change_size >= 100:
        return AutonomyRiskTier.HIGH
    if estimated_change_size >= 25:
        return AutonomyRiskTier.MEDIUM
    return AutonomyRiskTier.LOW


def evaluate_execution_permit(
    *,
    policy_profile: AutonomyPolicyProfile,
    risk_tier: AutonomyRiskTier,
    estimated_change_size: int,
    role: str,
    sensitive_path_hit: bool,
) -> tuple[ExecutionPermitDecision, str]:
    normalized_role = role.strip().lower()
    if normalized_role == "advisor":
        return (ExecutionPermitDecision.BLOCKED, "advisor role is read-only")
    if risk_tier == AutonomyRiskTier.CRITICAL:
        return (ExecutionPermitDecision.BLOCKED, "critical-risk action is blocked")
    if sensitive_path_hit:
        return (ExecutionPermitDecision.CONFIRM_REQUIRED, "target touches protected/sensitive paths")
    if risk_tier == AutonomyRiskTier.HIGH:
        return (ExecutionPermitDecision.CONFIRM_REQUIRED, "high-risk action requires confirmation")
    if risk_tier == AutonomyRiskTier.MEDIUM:
        if policy_profile == AutonomyPolicyProfile.HUMAN_AGGRESSIVE:
            return (ExecutionPermitDecision.ALLOW, "medium-risk action allowed by aggressive profile")
        if policy_profile == AutonomyPolicyProfile.HUMAN_CONSULTATIVE and estimated_change_size <= 20:
            return (ExecutionPermitDecision.ALLOW, "small-scope medium-risk action allowed by consultative profile")
        return (ExecutionPermitDecision.CONFIRM_REQUIRED, "medium-risk action requires confirmation")
    if policy_profile == AutonomyPolicyProfile.HUMAN_SAFE and estimated_change_size > 30:
        return (ExecutionPermitDecision.CONFIRM_REQUIRED, "safe profile requires confirmation for broad changes")
    return (ExecutionPermitDecision.ALLOW, "low-risk action allowed")


def adjust_consultative_threshold(
    base_threshold: float,
    *,
    recent_outcomes: list[dict[str, Any]] | None,
    semantic_ratio: float,
) -> float:
    threshold = max(0.0, min(1.0, float(base_threshold)))
    outcomes = recent_outcomes or []
    recent = outcomes[-8:]
    failures = 0
    successes = 0
    for row in recent:
        result = str((row or {}).get("result", "")).strip().lower()
        if result in {"failure", "blocked"}:
            failures += 1
        elif result == "success":
            successes += 1
    if failures >= 2:
        threshold += 0.05
    elif successes >= 5 and failures == 0:
        threshold -= 0.03
    if semantic_ratio >= 0.6:
        threshold += 0.02
    return round(max(0.0, min(1.0, threshold)), 4)


def continuity_health(
    *,
    was_active: bool,
    last_message_at: datetime | None,
    now: datetime,
    previous_violations: int,
    max_gap_seconds: int,
) -> tuple[int, bool]:
    if not was_active or last_message_at is None:
        return max(0, previous_violations), True
    previous = int(previous_violations)
    if last_message_at.tzinfo is None:
        baseline = last_message_at.replace(tzinfo=UTC)
    else:
        baseline = last_message_at.astimezone(UTC)
    delta = (now.astimezone(UTC) - baseline).total_seconds()
    if delta > max(1, int(max_gap_seconds)):
        violations = previous + 1
    else:
        violations = max(0, previous - 1)
    return violations, violations < 3
