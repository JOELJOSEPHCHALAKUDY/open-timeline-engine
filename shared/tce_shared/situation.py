from __future__ import annotations

import hashlib
import math
import os
import re
from functools import lru_cache

SITUATION_TYPES = (
    "blocker_encountered",
    "choice_required",
    "approval_requested",
    "error_occurred",
    "prioritization_needed",
    "communication_needed",
    "creative_decision",
    "conflict_detected",
    "unknown_territory",
    "routine_task",
    "escalation_point",
    "feedback_received",
)

_PATTERNS: list[tuple[str, list[str]]] = [
    ("error_occurred", [
        r"(?:error|exception|traceback|failed|failure|crash|bug|broke|broken)",
        r"(?:exit code [1-9]|typeerror|valueerror|keyerror|runtime error)",
    ]),
    ("blocker_encountered", [
        r"(?:blocked|blocking|cannot proceed|stuck|deadlock|unavailable|not available)",
        r"(?:dependency.+(?:missing|unavailable)|waiting on|depends on.+not ready)",
    ]),
    ("approval_requested", [
        r"(?:approve|approval|review|sign.?off|merge request|pull request|pr.+review)",
        r"(?:needs?.+(?:your|review|approval)|lgtm|please review)",
    ]),
    ("choice_required", [
        r"(?:should we|which (?:one|option)|choose between|pick|select|or we could)",
        r"(?:option [a-d]|alternative|trade.?off|versus|vs\.?)",
    ]),
    ("prioritization_needed", [
        r"(?:prioriti[sz]e|urgent|important|deadline|backlog|which first|next sprint)",
        r"(?:competing|conflicting priorities|resource allocation)",
    ]),
    ("feedback_received", [
        r"(?:feedback|review comment|suggestion|nit|improvement|could be better)",
        r"(?:code review|praised|complained|reported)",
    ]),
    ("communication_needed", [
        r"(?:reply|respond|message|email|slack|notify|update stakeholder)",
        r"(?:team meeting|standup|sync|announcement)",
    ]),
    ("conflict_detected", [
        r"(?:conflict|contradicts|incompatible|mismatch|disagree)",
        r"(?:merge conflict|breaking change|regression)",
    ]),
    ("escalation_point", [
        r"(?:escalat|beyond.+(?:scope|authority)|budget|management|policy)",
        r"(?:security incident|compliance|legal)",
    ]),
    ("creative_decision", [
        r"(?:design|ui|ux|naming|api shape|architecture decision|color|layout)",
        r"(?:no single right|subjective|aesthetic|branding)",
    ]),
    ("unknown_territory", [
        r"(?:never.+before|unfamiliar|new technology|first time|unknown|unexplored)",
        r"(?:no experience|learning curve|poc|prototype|experiment)",
    ]),
    ("routine_task", [
        r"(?:deploy|deployed|config|configured|update.+version|bump|release|migration ran)",
        r"(?:successfully|completed|done|finished|merged|shipped)",
    ]),
]


_NEGATION_TOKENS = {"not", "never", "no", "dont", "don't", "without"}
_TOKEN_NORMALIZATION: dict[str, str] = {
    "can't": "cant",
    "cant": "cant",
    "cannot": "cannot",
    "blocked": "blocked",
    "blocking": "blocked",
    "failing": "failed",
    "fails": "failed",
    "failure": "failed",
    "reviewing": "review",
    "approvals": "approval",
    "approved": "approval",
    "approving": "approval",
    "options": "option",
    "alternatives": "alternative",
}
_SEMANTIC_SIGNALS: dict[str, tuple[str, ...]] = {
    "error_occurred": (
        "error",
        "exception",
        "traceback",
        "failed",
        "crash",
        "bug",
        "fault",
        "broken",
    ),
    "blocker_encountered": (
        "blocked",
        "cannot",
        "cant",
        "stuck",
        "stalled",
        "halted",
        "waiting",
        "dependency",
        "unavailable",
        "denied",
    ),
    "approval_requested": (
        "approval",
        "approve",
        "review",
        "signoff",
        "permission",
        "lgtm",
    ),
    "choice_required": (
        "choose",
        "option",
        "select",
        "pick",
        "alternative",
        "tradeoff",
        "versus",
        "decision",
    ),
    "prioritization_needed": (
        "prioritize",
        "urgent",
        "important",
        "deadline",
        "backlog",
        "first",
        "competing",
    ),
    "feedback_received": (
        "feedback",
        "comment",
        "suggestion",
        "nit",
        "reported",
        "complaint",
        "praise",
    ),
    "communication_needed": (
        "reply",
        "respond",
        "message",
        "email",
        "notify",
        "update",
        "meeting",
        "sync",
    ),
    "conflict_detected": (
        "conflict",
        "contradict",
        "mismatch",
        "incompatible",
        "regression",
        "disagree",
    ),
    "escalation_point": (
        "escalate",
        "authority",
        "management",
        "policy",
        "security",
        "compliance",
        "legal",
    ),
    "creative_decision": (
        "design",
        "ux",
        "ui",
        "naming",
        "architecture",
        "branding",
        "layout",
    ),
    "unknown_territory": (
        "unknown",
        "unfamiliar",
        "new",
        "first",
        "experiment",
        "prototype",
        "poc",
        "learning",
    ),
    "routine_task": (
        "deploy",
        "release",
        "completed",
        "done",
        "finished",
        "merged",
        "shipped",
        "successful",
    ),
}
_SEMANTIC_PROTOTYPES: dict[str, tuple[str, ...]] = {
    "blocker_encountered": (
        "work is at a standstill and we cannot continue because dependency access is missing",
        "blocked by unavailable upstream service and waiting for credentials",
    ),
    "choice_required": (
        "there are two valid options and we need to choose one approach",
        "select between alternatives after comparing tradeoffs",
    ),
    "approval_requested": (
        "requires human review and approval before proceeding",
        "waiting for signoff and explicit confirmation from reviewer",
    ),
    "error_occurred": (
        "an exception crashed execution and produced a failure",
        "runtime error happened and the task failed",
    ),
    "prioritization_needed": (
        "multiple urgent tasks compete and we need to prioritize what comes first",
        "deadline pressure requires ordering backlog work",
    ),
    "communication_needed": (
        "send an update to stakeholders and reply to team messages",
        "need to communicate status in meeting or chat",
    ),
    "creative_decision": (
        "design and architecture choice without a single objectively correct answer",
        "ui ux naming and layout decision requires taste",
    ),
    "conflict_detected": (
        "changes are incompatible and create conflict or regression",
        "two requirements contradict each other",
    ),
    "unknown_territory": (
        "first time with unfamiliar technology and unknown approach",
        "prototype experiment in unexplored area",
    ),
    "routine_task": (
        "deployment completed successfully and routine maintenance done",
        "normal release update merged and finished",
    ),
    "escalation_point": (
        "issue requires escalation due to policy compliance or security concern",
        "outside scope authority and needs management decision",
    ),
    "feedback_received": (
        "received review feedback and suggestions for improvement",
        "comments from code review reported issues to address",
    ),
}
_SEMANTIC_HASH_DIM = 192


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except Exception:
        return default


def _resolve_semantic_enabled(explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return _env_bool("TCE_SEMANTIC_CLASSIFIER_ENABLED", False)


def _resolve_situation_threshold(explicit: float | None) -> float:
    base = explicit if explicit is not None else _env_float("TCE_SEMANTIC_CLASSIFIER_SITUATION_THRESHOLD", 0.61)
    return max(0.0, min(1.0, float(base)))


def _resolve_semantic_margin(explicit: float | None) -> float:
    base = explicit if explicit is not None else _env_float("TCE_SEMANTIC_CLASSIFIER_MARGIN", 0.06)
    return max(0.0, min(0.5, float(base)))


def _feature_bucket(feature: str, *, dim: int) -> int:
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dim


def _vectorize_tokens(tokens: list[str], *, dim: int = _SEMANTIC_HASH_DIM) -> dict[int, float]:
    if not tokens:
        return {}
    counts: dict[int, float] = {}
    for idx, token in enumerate(tokens):
        bucket = _feature_bucket(token, dim=dim)
        counts[bucket] = counts.get(bucket, 0.0) + 1.0
        if idx + 1 < len(tokens):
            bigram = f"{token}_{tokens[idx + 1]}"
            bigram_bucket = _feature_bucket(bigram, dim=dim)
            counts[bigram_bucket] = counts.get(bigram_bucket, 0.0) + 0.85
    norm = math.sqrt(sum(value * value for value in counts.values()))
    if norm <= 0:
        return {}
    return {bucket: value / norm for bucket, value in counts.items()}


@lru_cache(maxsize=128)
def _prototype_vector(prototype_text: str, dim: int) -> dict[int, float]:
    return _vectorize_tokens(_tokenize(prototype_text), dim=dim)


def _cosine_sparse(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    total = 0.0
    for key, value in left.items():
        total += value * right.get(key, 0.0)
    return max(0.0, min(1.0, total))


def _semantic_prototype_scores(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {situation_type: 0.0 for situation_type in SITUATION_TYPES}
    vector = _vectorize_tokens(tokens)
    scores: dict[str, float] = {}
    for situation_type, prototypes in _SEMANTIC_PROTOTYPES.items():
        best = 0.0
        for phrase in prototypes:
            best = max(best, _cosine_sparse(vector, _prototype_vector(phrase, _SEMANTIC_HASH_DIM)))
        scores[situation_type] = best
    return scores


def _tokenize(text: str) -> list[str]:
    raw_tokens = [token.strip() for token in re.split(r"\W+", text.lower()) if token.strip()]
    tokens: list[str] = []
    idx = 0
    while idx < len(raw_tokens):
        token = raw_tokens[idx]
        nxt = raw_tokens[idx + 1] if idx + 1 < len(raw_tokens) else ""
        if token == "can" and nxt == "t":
            tokens.append("cant")
            idx += 2
            continue
        if token == "don" and nxt == "t":
            tokens.append("dont")
            idx += 2
            continue
        tokens.append(token)
        idx += 1
    return [_TOKEN_NORMALIZATION.get(token, token) for token in tokens]


def _negated(tokens: list[str], idx: int, *, lookback: int = 3) -> bool:
    start = max(0, idx - lookback)
    return any(tokens[pos] in _NEGATION_TOKENS for pos in range(start, idx))


def _semantic_score(situation_type: str, tokens: list[str]) -> float:
    targets = _SEMANTIC_SIGNALS.get(situation_type, ())
    if not targets or not tokens:
        return 0.0
    target_set = set(targets)
    score = 0.0
    for idx, token in enumerate(tokens):
        if token in target_set:
            if situation_type == "blocker_encountered" and _negated(tokens, idx):
                score -= 0.4
            else:
                score += 0.35
    token_set = set(tokens)
    if situation_type == "blocker_encountered":
        if {"cannot", "proceed"} <= token_set or {"cant", "proceed"} <= token_set:
            score += 0.35
        if {"access", "denied"} <= token_set:
            score += 0.25
    elif situation_type == "choice_required":
        if {"should", "we"} <= token_set or {"which", "option"} <= token_set:
            score += 0.3
    elif situation_type == "approval_requested":
        if {"needs", "approval"} <= token_set or {"need", "review"} <= token_set:
            score += 0.3
    return max(0.0, min(1.6, score))


def classify_situation(
    text: str,
    *,
    semantic_enabled: bool | None = None,
    semantic_threshold: float | None = None,
    semantic_margin: float | None = None,
) -> str:
    lower = text.lower()
    tokens = _tokenize(lower)
    best_type = "routine_task"
    best_score = 0.0
    rule_scores: dict[str, float] = {}
    for situation_type, patterns in _PATTERNS:
        score = 0.0
        for pattern in patterns:
            if re.search(pattern, lower):
                score += 1.0
        score += _semantic_score(situation_type, tokens)
        rule_scores[situation_type] = score
        if score > best_score:
            best_score = score
            best_type = situation_type
    if not _resolve_semantic_enabled(semantic_enabled):
        return best_type
    semantic_scores = _semantic_prototype_scores(tokens)
    combined_scores: list[tuple[str, float]] = []
    for situation_type in SITUATION_TYPES:
        rule_norm = max(0.0, min(1.0, float(rule_scores.get(situation_type, 0.0)) / 2.4))
        semantic_norm = max(0.0, min(1.0, float(semantic_scores.get(situation_type, 0.0)) * 1.35))
        combined_scores.append((situation_type, max(semantic_norm, (0.45 * rule_norm) + (0.55 * semantic_norm))))
    combined_scores.sort(key=lambda item: item[1], reverse=True)
    top_type, top_score = combined_scores[0]
    second_score = combined_scores[1][1] if len(combined_scores) > 1 else 0.0
    threshold = _resolve_situation_threshold(semantic_threshold)
    margin = _resolve_semantic_margin(semantic_margin)
    if top_score >= threshold and (top_score - second_score) >= margin:
        return top_type
    return best_type
