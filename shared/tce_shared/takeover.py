from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from .decision_policy import DecisionResult, DecisionStatus
from .events import (
    SafetyDecision,
    TakeoverClassification,
    TakeoverMode,
    TakeoverPolicy,
)

_HIGH_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(?:-?rf)\b"), "destructive_filesystem"),
    (re.compile(r"\bdelete\s+all\b"), "destructive_delete_all"),
    (re.compile(r"\breset\s+hard\b"), "destructive_reset"),
    (re.compile(r"\bdelete\s+(?:the\s+)?(?:entire\s+)?(?:database|db)\b"), "irreversible_data_operation"),
    (re.compile(r"\bdrop\s+(?:the\s+)?(?:entire\s+)?(?:database|db)\b"), "irreversible_data_operation"),
    (re.compile(r"\bdrop\s+table\b"), "irreversible_data_operation"),
    (re.compile(r"\btruncate\s+table\b"), "irreversible_data_operation"),
    (re.compile(r"\bprod(uction)?\s+deploy\b"), "production_deploy"),
    (re.compile(r"\brotate\s+key\b"), "credential_operation"),
    (re.compile(r"\bapi\s+key\b"), "credential_operation"),
    (re.compile(r"\bsecret\b"), "credential_operation"),
)

_DELIBERATION_HINTS = ("research", "deep", "investigate", "analyze", "analysis")

_NEGATION_TOKENS = {"not", "never", "no", "dont", "don't", "without"}
_HANDOFF_TERMS = {
    "approval",
    "approve",
    "approved",
    "confirm",
    "confirmation",
    "permission",
    "review",
    "input",
    "signoff",
    "sign",
    "authorize",
    "authorization",
}
_HANDOFF_GATING_TERMS = {
    "need",
    "needs",
    "require",
    "required",
    "requires",
    "await",
    "awaiting",
    "wait",
    "waiting",
    "until",
    "before",
    "cannot",
    "cant",
    "unable",
    "pause",
}
_SUGGESTION_TERMS = {
    "choose",
    "choice",
    "select",
    "pick",
    "option",
    "options",
    "prefer",
    "alternative",
    "alternatives",
    "versus",
    "vs",
}
_INTERROGATIVE_TERMS = {
    "should",
    "could",
    "would",
    "can",
    "which",
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
}
_ACTIONABLE_OBJECTIVE_VERBS = {
    "add",
    "build",
    "check",
    "debug",
    "deploy",
    "document",
    "fix",
    "implement",
    "investigate",
    "lint",
    "optimize",
    "profile",
    "refactor",
    "remove",
    "repair",
    "revert",
    "run",
    "ship",
    "test",
    "update",
    "verify",
}
_GENERIC_OBJECTIVE_OBJECTS = {
    "it",
    "this",
    "that",
    "thing",
    "stuff",
    "something",
    "anything",
}
_DESTRUCTIVE_ACTION_TOKENS = {
    "delete",
    "drop",
    "erase",
    "wipe",
    "destroy",
    "nuke",
    "purge",
    "truncate",
    "remove",
}
_CRITICAL_RESOURCE_TOKENS = {
    "database",
    "db",
    "production",
    "prod",
    "table",
    "tables",
    "cluster",
    "bucket",
}
_TOKEN_NORMALIZATION: dict[str, str] = {
    "signoff": "signoff",
    "sign-off": "signoff",
    "signing": "sign",
    "authorized": "authorize",
    "authorise": "authorize",
    "authorised": "authorize",
    "cannot": "cannot",
    "can't": "cant",
    "cant": "cant",
    "dont": "dont",
    "don't": "dont",
    "vs.": "vs",
}
_SEMANTIC_INTENT_PROTOTYPES: dict[str, tuple[str, ...]] = {
    "handoff": (
        "I need your confirmation before I continue this change",
        "please review and approve before I proceed",
        "cannot move forward without your signoff",
        "need human approval first",
        "waiting for your green light to continue",
    ),
    "suggestion": (
        "there are multiple options choose one and I will execute",
        "pick one approach from these alternatives",
        "which direction do you prefer among these paths",
        "select your preferred option and I will continue",
    ),
    "question": (
        "should we continue with this step",
        "which approach should we take next",
        "what is the best option here",
        "can we proceed now",
    ),
    "decisive": (
        "implement the fix now and run tests",
        "apply the change and verify the result",
        "continue execution and complete the task",
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


def _resolve_intent_threshold(explicit: float | None) -> float:
    base = explicit if explicit is not None else _env_float("TCE_SEMANTIC_CLASSIFIER_INTENT_THRESHOLD", 0.67)
    return max(0.0, min(1.0, float(base)))


def _resolve_semantic_margin(explicit: float | None) -> float:
    base = explicit if explicit is not None else _env_float("TCE_SEMANTIC_CLASSIFIER_MARGIN", 0.06)
    return max(0.0, min(0.5, float(base)))


def _feature_bucket(feature: str, *, dim: int) -> int:
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dim


def _vectorize_semantic_tokens(tokens: list[str], *, dim: int = _SEMANTIC_HASH_DIM) -> dict[int, float]:
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
    normalized = normalize_text(prototype_text)
    return _vectorize_semantic_tokens(_tokenize_normalized(normalized), dim=dim)


def _cosine_sparse(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    total = 0.0
    for key, value in left.items():
        total += value * right.get(key, 0.0)
    return max(0.0, min(1.0, total))


def _semantic_intent_prototype_scores(tokens: list[str], text: str) -> dict[str, float]:
    if not tokens:
        return {"handoff": 0.0, "suggestion": 0.0, "question": 0.0, "decisive": 0.0}
    vector = _vectorize_semantic_tokens(tokens)
    scores: dict[str, float] = {}
    for label, prototypes in _SEMANTIC_INTENT_PROTOTYPES.items():
        best = 0.0
        for phrase in prototypes:
            best = max(best, _cosine_sparse(vector, _prototype_vector(phrase, _SEMANTIC_HASH_DIM)))
        scores[label] = best
    if text.rstrip().endswith("?"):
        scores["question"] = min(1.0, scores.get("question", 0.0) + 0.06)
    if any(token in _NEGATION_TOKENS for token in tokens):
        has_handoff_target = any(token in _HANDOFF_TERMS for token in tokens)
        has_continue_target = any(token in {"continue", "proceed", "automatically", "auto"} for token in tokens)
        if has_handoff_target and has_continue_target:
            scores["handoff"] = max(0.0, scores.get("handoff", 0.0) - 0.12)
    return scores


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in value)
    return " ".join(cleaned.split())


# Imperative override phrases that must never survive as bare instructions when
# untrusted event text is promoted into an autonomous objective.
_INJECTION_LEAD_IN = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b[^.;\n]*?\b(?:previous|prior|earlier|above|all)\b"
    r"[^.;\n]*?\binstructions?\b",
    re.IGNORECASE,
)


def sanitize_untrusted_objective(value: str | None, *, max_len: int = 180) -> str:
    """Neutralize untrusted text (e.g. an event title) promoted to an objective.

    Collapses to a single line, defangs prompt-injection lead-ins, and caps
    length. This is not a substitute for delimiting the objective as data at
    the executor boundary — it is defense in depth on the way in.
    """
    if not value:
        return ""
    collapsed = " ".join(str(value).split())
    defanged = _INJECTION_LEAD_IN.sub("[redacted-directive]", collapsed)
    defanged = " ".join(defanged.split())
    if len(defanged) > max_len:
        defanged = defanged[:max_len].rstrip()
    return defanged


def persona_defaults(persona_mode: str) -> tuple[str, str]:
    normalized = persona_mode.strip().lower()
    if normalized == "naruto":
        return "hey kurama take over", "kurama stand down"
    if normalized in {"shadow", "shadowmode", "shadow_mode"}:
        return "hey igris take over,hey beru take over", "shadow stand down,beru stand down,igris stand down"
    return "hey advisor take over", "advisor stand down"


def split_phrases(value: str | None) -> list[str]:
    if value is None:
        return []
    output: list[str] = []
    for item in value.split(","):
        normalized = normalize_text(item.strip())
        if normalized:
            output.append(normalized)
    return output


def contains_phrase(message: str, phrases: str | list[str] | None) -> str | None:
    normalized_message = normalize_text(message)
    if isinstance(phrases, list):
        items = [normalize_text(item) for item in phrases if normalize_text(item)]
    else:
        items = split_phrases(phrases)
    for item in items:
        if item and item in normalized_message:
            return item
    return None


def mode_override(persona_mode: str, message: str) -> TakeoverMode | None:
    normalized_mode = persona_mode.strip().lower()
    normalized_text = normalize_text(message)
    if normalized_mode in {"shadow", "shadowmode", "shadow_mode"}:
        names = ("beru", "igris", "advisor", "kurama")
    elif normalized_mode == "naruto":
        names = ("kurama", "advisor", "beru", "igris")
    else:
        names = ("advisor", "beru", "igris", "kurama")
    for name in names:
        if f"{name} suggest" in normalized_text:
            return TakeoverMode.SUGGEST
        if f"{name} take over" in normalized_text or f"{name} takeover" in normalized_text:
            return TakeoverMode.TAKEOVER
    return None


def _tokenize_normalized(value: str) -> list[str]:
    raw_tokens = [token.strip() for token in value.split() if token.strip()]
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


def _has_negation_near(tokens: list[str], idx: int, *, lookback: int = 3) -> bool:
    start = max(0, idx - lookback)
    return any(tokens[pos] in _NEGATION_TOKENS for pos in range(start, idx))


def _semantic_intent_scores(normalized: str, text: str) -> tuple[float, float, float]:
    if not normalized:
        return 0.0, 0.0, 0.0
    tokens = _tokenize_normalized(normalized)
    token_set = set(tokens)

    handoff_score = 0.0
    if token_set & _HANDOFF_TERMS:
        handoff_score += 0.35
    if token_set & _HANDOFF_GATING_TERMS:
        handoff_score += 0.30
    if ("user" in token_set or "human" in token_set or "you" in token_set) and (token_set & _HANDOFF_TERMS):
        handoff_score += 0.20
    if {"before", "approval"} <= token_set or {"before", "confirmation"} <= token_set:
        handoff_score += 0.15
    if {"cannot", "proceed"} <= token_set or {"cant", "proceed"} <= token_set:
        handoff_score += 0.15
    for idx, token in enumerate(tokens):
        if token in _HANDOFF_TERMS and _has_negation_near(tokens, idx):
            handoff_score -= 0.45
            break
    handoff_score = max(0.0, min(1.0, handoff_score))

    suggestion_score = 0.0
    if token_set & _SUGGESTION_TERMS:
        suggestion_score += 0.5
    if "between" in token_set and (token_set & {"or", "option", "options", "alternative", "alternatives"}):
        suggestion_score += 0.25
    if "prefer" in token_set and ("which" in token_set or text.rstrip().endswith("?")):
        suggestion_score += 0.25
    suggestion_score = max(0.0, min(1.0, suggestion_score))

    question_score = 0.0
    if text.rstrip().endswith("?"):
        question_score += 0.5
    if tokens:
        first = tokens[0]
        if first in _INTERROGATIVE_TERMS:
            question_score += 0.4
        elif len(tokens) >= 2 and f"{tokens[0]} {tokens[1]}" in {"can we", "should we", "could we", "would we"}:
            question_score += 0.4
    question_score = max(0.0, min(1.0, question_score))
    return handoff_score, suggestion_score, question_score


def classify_text(
    value: str | None,
    *,
    takeover_active: bool = False,
    semantic_enabled: bool | None = None,
    semantic_threshold: float | None = None,
    semantic_margin: float | None = None,
) -> TakeoverClassification:
    """Classify advisor text for takeover enforcement.

    When *takeover_active* is True the classifier biases heavily toward
    DECISIVE so that autonomous execution is not interrupted by false
    positives (numbered lists, rhetorical questions, polite phrasing).
    """
    if value is None:
        return TakeoverClassification.EMPTY
    text = value.strip()
    if not text:
        return TakeoverClassification.EMPTY
    normalized = normalize_text(text)
    if not normalized:
        return TakeoverClassification.EMPTY

    # --- Handoff: only match unambiguous delegation phrases ---
    handoff_fragments = (
        "ask for human confirmation",
        "user confirmation required",
        "wait for confirmation",
        "waiting for your confirmation",
        "need your explicit confirmation",
        "cannot proceed without your approval",
    )
    if any(fragment in normalized for fragment in handoff_fragments):
        return TakeoverClassification.HANDOFF

    # --- Suggestion: only match explicit menu-style offerings ---
    suggestion_fragments = (
        "pick your top",
        "choose one of the following",
        "which option do you prefer",
        "select from the following",
    )
    if any(fragment in normalized for fragment in suggestion_fragments):
        return TakeoverClassification.SUGGESTION

    handoff_score, suggestion_score, question_score = _semantic_intent_scores(normalized, text)
    tokens = _tokenize_normalized(normalized)
    if _resolve_semantic_enabled(semantic_enabled):
        proto_scores = _semantic_intent_prototype_scores(tokens, text)
        ranked = sorted(proto_scores.items(), key=lambda item: item[1], reverse=True)
        if ranked:
            top_label, top_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else 0.0
            threshold = _resolve_intent_threshold(semantic_threshold)
            margin = _resolve_semantic_margin(semantic_margin)
            if top_score >= threshold and (top_score - second_score) >= margin:
                mapped = {
                    "handoff": TakeoverClassification.HANDOFF,
                    "suggestion": TakeoverClassification.SUGGESTION,
                    "question": TakeoverClassification.QUESTION,
                    "decisive": TakeoverClassification.DECISIVE,
                }.get(top_label, TakeoverClassification.DECISIVE)
                if mapped == TakeoverClassification.HANDOFF:
                    return mapped
                if mapped == TakeoverClassification.SUGGESTION:
                    if takeover_active and top_score < (threshold + 0.08):
                        pass
                    else:
                        return mapped
                if mapped == TakeoverClassification.QUESTION and not takeover_active:
                    return mapped
                if mapped == TakeoverClassification.DECISIVE and takeover_active:
                    return mapped
    if handoff_score >= 0.72:
        return TakeoverClassification.HANDOFF
    if suggestion_score >= 0.72:
        return TakeoverClassification.SUGGESTION

    # In takeover mode, bias toward DECISIVE — the whole point is
    # autonomous execution, so only very clear non-decisive signals
    # (matched above) should override.
    if takeover_active:
        return TakeoverClassification.DECISIVE

    # --- Non-takeover (suggest mode) classification ---
    # Full question: last sentence ends with ?
    last_line = text.rstrip().rsplit("\n", 1)[-1].strip()
    if (last_line.endswith("?") and len(last_line) > 10) or question_score >= 0.74:
        return TakeoverClassification.QUESTION

    # Softer handoff phrases — only outside takeover
    soft_handoff = (
        "ask user",
        "ask the user",
        "need your input",
        "up to you",
        "your call",
    )
    if any(fragment in normalized for fragment in soft_handoff):
        return TakeoverClassification.HANDOFF
    if handoff_score >= 0.5:
        return TakeoverClassification.HANDOFF

    # Softer suggestion phrases — only outside takeover
    soft_suggestion = (
        "recommended actions",
        "next exploration targets",
        "i can execute immediately",
    )
    if any(fragment in normalized for fragment in soft_suggestion):
        return TakeoverClassification.SUGGESTION
    if suggestion_score >= 0.5:
        return TakeoverClassification.SUGGESTION

    return TakeoverClassification.DECISIVE


def extract_action_lines(value: str, max_items: int = 3) -> list[str]:
    actions: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        normalized = normalize_text(line)
        if not normalized:
            continue
        if any(
            token in normalized
            for token in (
                "next exploration targets",
                "recommended actions",
                "choose one",
                "pick your top",
                "i can execute immediately",
            )
        ):
            continue
        actions.append(line[:140])
        if len(actions) >= max_items:
            break
    return actions


def is_control_message(value: str | None) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return True
    if normalized in {
        "continue",
        "go on",
        "resume",
        "keep going",
        "stopped",
        "again stopped",
        "still stopped",
        "you stopped",
        "you still stopped",
        "you did it again",
    }:
        return True
    control_phrases = (
        "take over",
        "takeover",
        "stand down",
        "why did you stop",
        "it stopped",
        "still stopped",
        "again stopped",
    )
    if any(phrase in normalized for phrase in control_phrases):
        return True
    return False


def is_activation_or_mode_command(value: str | None) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return False
    if "take over" in normalized or "takeover" in normalized:
        return True
    if "stand down" in normalized:
        return True
    return " suggest" in f" {normalized}"


def objective_update_requested(value: str | None) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return False
    update_fragments = (
        "new objective",
        "new goal",
        "switch objective",
        "change objective",
        "change goal",
        "set objective",
        "set goal",
        "objective is",
        "goal is",
        "work on",
        "focus on",
    )
    return any(fragment in normalized for fragment in update_fragments)


OBJECTIVE_PLACEHOLDER_VALUES = {
    "current objective",
    "continue active objective",
    "follow latest concrete objective",
}

# Titles and task types TCE writes about its own operation. Every takeover step,
# search, and context-bundle call emits one, which makes them by far the most
# frequent rows in the events table.
SELF_REFERENTIAL_TITLE_PREFIXES = ("interaction:",)
# Directive lifecycle rows are emitted for every terminal state, so match the shape
# rather than listing states — naming only "succeeded" let "Directive failed:"
# through and it came back as a top-ranked goal.
_DIRECTIVE_LIFECYCLE_TITLE = re.compile(r"^directive\s+[a-z_]+:", re.IGNORECASE)
SELF_REFERENTIAL_TASK_TYPE_PREFIX = "interaction_"


def is_self_referential_event(title: str | None, task_type: str | None) -> bool:
    """True when an event describes TCE's own activity rather than the user's work.

    Goal discovery reads the most recent events, so without this filter the window
    fills with records of TCE running and it proposes working on its own exhaust —
    the queue observed live was 36/40 "Interaction: takeover_step - ..." entries.
    Matching is anchored to the start of the title so ordinary prose containing
    "interaction" is not caught.
    """
    normalized_title = (title or "").strip().lower()
    if not normalized_title:
        return True
    if normalized_title.startswith(SELF_REFERENTIAL_TITLE_PREFIXES):
        return True
    if _DIRECTIVE_LIFECYCLE_TITLE.match(normalized_title):
        return True
    return (task_type or "").strip().lower().startswith(SELF_REFERENTIAL_TASK_TYPE_PREFIX)


def _is_vague_objective(value: str) -> bool:
    """Detect objectives that are too vague, meta, or self-referential to be actionable.

    Examples of vague objectives:
    - "can you make it continues and autonomous"
    - "current objective"
    - "continue"
    - "keep going"
    - "make it work"
    """
    normalized = normalize_text(value)
    if not normalized:
        return True
    if normalized in OBJECTIVE_PLACEHOLDER_VALUES.union({"continue", "keep going", "resume", "go on"}):
        return True
    # Self-referential: objective is about making the system autonomous/continuous
    meta_fragments = (
        "make it continu",
        "make it autonomo",
        "make it continues",
        "be autonomous",
        "be continuous",
        "continues and autonomo",
        "autonomous and continu",
        "keep running",
        "run continuously",
        "dont stop",
        "don t stop",
        "never stop",
    )
    if any(fragment in normalized for fragment in meta_fragments):
        return True
    words = normalized.split()
    # Too short to be meaningful (e.g. "do it", "go", "yes").
    # Allow compact but concrete verbs such as "fix tests" or "deploy staging".
    if len(words) <= 2:
        if len(words) == 2 and words[0] in _ACTIONABLE_OBJECTIVE_VERBS and words[1] not in _GENERIC_OBJECTIVE_OBJECTS:
            return False
        return True
    return False


def _fallback_objective_from_context(takeover_context: dict[str, Any]) -> str:
    existing = str(takeover_context.get("objective") or "").strip()
    if existing and not _is_vague_objective(existing):
        return existing[:280]
    latest = str(takeover_context.get("last_user_message") or "").strip()
    if latest:
        if is_activation_or_mode_command(latest):
            latest = _strip_activation_prefix(latest)
        if latest and not is_control_message(latest) and not _is_vague_objective(latest):
            return latest[:280]
    goal_title = str(
        takeover_context.get("selected_goal_title")
        or takeover_context.get("active_goal_title")
        or ""
    ).strip()
    if goal_title and not _is_vague_objective(goal_title):
        return goal_title[:280]
    return "continue active objective"


def resolve_objective(message: str, task: str | None, takeover_context: dict[str, Any]) -> str:
    existing = takeover_context.get("objective")
    existing_objective = existing.strip() if isinstance(existing, str) and existing.strip() else ""
    existing_is_vague = _is_vague_objective(existing_objective) if existing_objective else True
    explicit_task = task.strip() if isinstance(task, str) else ""

    if explicit_task:
        if is_activation_or_mode_command(explicit_task):
            # Activation command — try to extract the real content part
            content = _strip_activation_prefix(explicit_task)
            if content and not is_control_message(content) and not _is_vague_objective(content):
                return content[:280]
            # Fall back to existing objective when concrete, otherwise use context-derived fallback.
            return existing_objective[:280] if (existing_objective and not existing_is_vague) else _fallback_objective_from_context(takeover_context)
        if existing_objective and not existing_is_vague and is_control_message(explicit_task):
            return existing_objective[:280]
        if not is_control_message(explicit_task) and not _is_vague_objective(explicit_task):
            return explicit_task[:280]
        # explicit_task is vague or control — prefer existing if non-vague
        if existing_objective and not existing_is_vague:
            return existing_objective[:280]

    message_candidate = (message or "").strip()

    # If existing objective is vague, aggressively try to replace it
    if existing_is_vague and message_candidate:
        if is_activation_or_mode_command(message_candidate):
            content = _strip_activation_prefix(message_candidate)
            if content and not is_control_message(content) and not _is_vague_objective(content):
                return content[:280]
        elif not is_control_message(message_candidate) and not _is_vague_objective(message_candidate):
            return message_candidate[:280]

    if existing_objective and not existing_is_vague:
        if objective_update_requested(message_candidate):
            return message_candidate[:280]
        if is_activation_or_mode_command(existing_objective):
            if message_candidate and not is_control_message(message_candidate):
                return message_candidate[:280]
            return _fallback_objective_from_context(takeover_context)
        return existing_objective[:280]

    if message_candidate:
        if is_activation_or_mode_command(message_candidate):
            content = _strip_activation_prefix(message_candidate)
            if content and not is_control_message(content):
                return content[:280]
            return _fallback_objective_from_context(takeover_context)
        if not is_control_message(message_candidate):
            return message_candidate[:280]

    if existing_objective and not existing_is_vague:
        return existing_objective[:280]
    return _fallback_objective_from_context(takeover_context)


def _cycle_prefix(takeover_context: dict[str, Any]) -> str:
    turn_count = 0
    turn_count_raw = takeover_context.get("turn_count")
    if isinstance(turn_count_raw, int):
        turn_count = max(0, turn_count_raw)
    elif isinstance(turn_count_raw, str) and turn_count_raw.strip().isdigit():
        turn_count = max(0, int(turn_count_raw.strip()))
    return f"Autonomous cycle {turn_count}" if turn_count > 0 else "Autonomous cycle"


def _latest_request(takeover_context: dict[str, Any]) -> str:
    """Return the latest user message, filtering out activation/control commands.

    If the latest message is purely an activation/control command (e.g.
    "hey beru take over"), return "" so it doesn't get echoed as a task
    directive.  If the message contains an activation phrase *plus* real
    content (e.g. "hey beru take over, implement the auth flow"), extract
    and return the real content part.
    """
    latest = takeover_context.get("last_user_message")
    if not isinstance(latest, str) or not latest.strip():
        return ""
    text = latest.strip()[:220]
    # Pure control messages ("continue", "keep going") are not tasks.
    if is_control_message(text):
        return ""
    # Activation commands ("hey beru take over ...") — extract content if any.
    if is_activation_or_mode_command(text):
        cleaned = _strip_activation_prefix(text)
        if cleaned and not is_control_message(cleaned):
            return cleaned[:220]
        return ""
    return text


def _strip_activation_prefix(text: str) -> str:
    """Remove known activation phrase prefixes, returning the remaining content."""
    normalized = normalize_text(text)
    # Common patterns: "hey X take over <content>", "hey X takeover <content>"
    # We strip up to and including "take over" / "takeover"
    for marker in ("take over", "takeover"):
        idx = normalized.find(marker)
        if idx >= 0:
            after = text[idx + len(marker):].strip()
            # Remove leading punctuation/conjunctions. Sentence-ending marks matter
            # here too: "beru take over. Objective: ..." is a natural way to phrase
            # an activation, and without "." the period leaks into the objective and
            # then into every directive built from it.
            after = after.lstrip(".!?,;:- ")
            for prefix in ("and ", "then ", "now ", "please "):
                if after.lower().startswith(prefix):
                    after = after[len(prefix):].strip()
            return after
    return ""


def build_decisive_response(
    task: str,
    takeover_context: dict[str, Any],
    advice: dict[str, Any] | None = None,
    suggested_actions: list[str] | None = None,
) -> str:
    """Build an imperative directive for the executor to ACT on.

    The output must read as a direct instruction, NOT a status report.
    The executor will receive this as its marching order and must execute it
    (read files, write code, run commands) — not display it verbatim.
    """
    objective = takeover_context.get("objective", task)
    if not isinstance(objective, str) or not objective.strip():
        objective = task
    objective = objective.strip()[:280]
    latest_request_text = _latest_request(takeover_context)
    cycle_prefix = _cycle_prefix(takeover_context)
    latest_request_norm = normalize_text(latest_request_text)

    # --- Frustration detection: explain the turn-based constraint ---
    if latest_request_norm and any(
        token in latest_request_norm
        for token in (
            "still same issue",
            "same issue",
            "you repeat",
            "repeat again",
            "still stopped",
            "why did you stop",
        )
    ):
        return (
            f"[TAKEOVER {cycle_prefix}] "
            "This chat is turn-based — each response requires a new user message. "
            "Takeover is still active: every message you send triggers autonomous execution. "
            f"Current objective: {objective}. "
            "To get multi-turn execution in one go, use `tce-capture advisor-chat --auto-continue-turns 20`."
        )

    # --- Collect context hints ---
    style_hint = ""
    obs_hint = ""
    action_items: list[str] = []

    clone_context = None
    if isinstance(advice, dict):
        clone_context = advice.get("clone_context")
    if isinstance(clone_context, dict) and clone_context.get("clone_prompt"):
        fingerprint = clone_context.get("fingerprint", {})
        speed_quality = fingerprint.get("priorities", {}).get("speed_vs_quality", 0.5)
        style_hint = "Move fast, favor shipping." if speed_quality < 0.4 else "Prioritize thoroughness." if speed_quality >= 0.7 else ""
        similar_obs = clone_context.get("similar_observations", [])
        if similar_obs:
            obs_hint = f" Reference {len(similar_obs)} past decisions if relevant."

    if suggested_actions:
        action_items.extend(item for item in suggested_actions if item.strip())
    if not action_items and isinstance(advice, dict):
        do_items = advice.get("do")
        if isinstance(do_items, list):
            for item in do_items:
                if isinstance(item, str) and item.strip():
                    action_items.append(item.strip()[:140])
                    if len(action_items) >= 3:
                        break

    # --- Build the imperative directive ---
    # This must tell the executor WHAT TO DO, not report status.
    parts: list[str] = [f"[TAKEOVER {cycle_prefix}]"]

    # Primary instruction: what to work on
    if latest_request_text and latest_request_text.lower() != objective.lower():
        parts.append(f"Your task: {latest_request_text}.")
        parts.append(f"(Overall objective: {objective}.)")
    elif latest_request_text:
        parts.append(f"Your task: {latest_request_text}.")
    else:
        parts.append(f"Your task: {objective}.")

    # Style/approach hints
    if style_hint:
        parts.append(style_hint)
    if obs_hint:
        parts.append(obs_hint.strip())

    # Concrete next steps if available
    if action_items:
        parts.append(f"Suggested steps: {'; '.join(action_items[:3])}.")

    # The critical instruction: DO SOMETHING, don't just report
    parts.append(
        "ACT NOW: Read the relevant code, make changes, run tests. "
        "Do not describe what you would do — actually do it. "
        "Do not ask for permission — decide and execute."
    )

    return " ".join(parts)


def _default_ask(task: str) -> str:
    """The escalation text used when the policy abstained and named no reason.

    Deliberately built from the task and nothing else.  There is a separate field carrying
    the advisor's prose, and it has one reader that writes it to a diagnostic column; wiring
    a model's sentence into the one surface the human reads as a question is the failure this
    module exists to prevent.
    """

    objective = " ".join(str(task or "").split()).strip()
    if objective:
        return f"I do not have enough to decide this one on {objective}. Which way do you want to go?"
    return "I do not have enough to decide this one. Which way do you want to go?"


def ensure_takeover_response(
    mode: TakeoverMode,
    text: str | None,
    task: str,
    takeover_context: dict[str, Any],
    advice: dict[str, Any] | None = None,
    *,
    semantic_enabled: bool | None = None,
    semantic_threshold: float | None = None,
    semantic_margin: float | None = None,
    policy: DecisionResult | None = None,
) -> tuple[str, bool, str | None, TakeoverClassification]:
    # An EXPOSED abstention is the only thing the decision policy may do to a turn.  With no
    # qualified family - the state of every family today - `exposed` is False, this branch is
    # unreachable, and the turn below is byte-for-byte what it was before P4.  That is a gate
    # and not a promise: `policy=<unexposed>` must return exactly what `policy=None` returns.
    if policy is not None and policy.exposed and policy.status is DecisionStatus.ABSTAINED:
        return (
            policy.reason_for_asking or _default_ask(task),
            True,
            "policy_abstention",
            TakeoverClassification.HANDOFF,
        )

    is_takeover = mode == TakeoverMode.TAKEOVER
    classification = classify_text(
        text,
        takeover_active=is_takeover,
        semantic_enabled=semantic_enabled,
        semantic_threshold=semantic_threshold,
        semantic_margin=semantic_margin,
    )
    candidate = (text or "").strip()
    if not is_takeover:
        return candidate, False, None, classification

    # DECISIVE with real content — pass through as-is, with no string inspection.
    #
    # What used to be here was exactly inverted.  Text carrying either of two cold-start
    # marker phrases failed this pass-through and was rewritten through
    # build_decisive_response, so the LOWEST-evidence turns produced the MOST confident text,
    # and the resulting DECISIVE classification then scored 0.92 in the certainty heuristic
    # against 0.58 for HANDOFF — raising the very decision_confidence that gates needs_human.
    # The suppression fed the gate that would have caught the suppression.
    #
    # Both string checks and the weak-evidence rewrite are gone.  Weak evidence is the
    # decision policy's job now, and its answer is an abstention, not a louder directive.
    if classification == TakeoverClassification.DECISIVE and candidate:
        return candidate, False, None, classification

    suggested_actions = extract_action_lines(candidate) if classification == TakeoverClassification.SUGGESTION else None
    final = build_decisive_response(task=task, takeover_context=takeover_context, advice=advice, suggested_actions=suggested_actions)
    if classification == TakeoverClassification.EMPTY:
        reason = "empty_decisive_rewrite"
    elif classification == TakeoverClassification.DECISIVE:
        reason = "weak_signal_decisive_rewrite"
    elif classification == TakeoverClassification.SUGGESTION:
        reason = "suggestion_decisive_rewrite"
    elif classification == TakeoverClassification.HANDOFF:
        reason = "handoff_decisive_rewrite"
    elif classification == TakeoverClassification.QUESTION:
        reason = "question_decisive_rewrite"
    else:
        reason = "non_decisive_rewrite"
    return final, True, reason, TakeoverClassification.DECISIVE


def high_risk_reason(text: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    for matcher, reason in _HIGH_RISK_PATTERNS:
        if matcher.search(normalized):
            return reason
    tokens = _tokenize_normalized(normalized)
    token_set = set(tokens)
    if token_set & _DESTRUCTIVE_ACTION_TOKENS and token_set & _CRITICAL_RESOURCE_TOKENS:
        for idx, token in enumerate(tokens):
            if token in _DESTRUCTIVE_ACTION_TOKENS and not _has_negation_near(tokens, idx):
                return "irreversible_data_operation"
    return None


def objective_hash(value: str) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _message_specificity_score(message_norm: str) -> float:
    if not message_norm:
        return 0.35
    if is_control_message(message_norm):
        return 0.35
    if is_activation_or_mode_command(message_norm):
        content = _strip_activation_prefix(message_norm)
        if not content or is_control_message(content):
            return 0.35
        message_norm = normalize_text(content)
    token_count = len(message_norm.split())
    if token_count >= 8:
        score = 0.9
    elif token_count >= 5:
        score = 0.82
    elif token_count >= 3:
        score = 0.74
    else:
        score = 0.58
    action_fragments = (
        "add ",
        "fix ",
        "update ",
        "review ",
        "implement ",
        "create ",
        "refactor ",
        "test ",
        "run ",
        "check ",
    )
    if any(fragment in message_norm for fragment in action_fragments):
        score = min(0.92, score + 0.06)
    return max(0.0, min(1.0, score))


def _objective_clarity_score(objective: str, message: str) -> float:
    objective_norm = normalize_text(objective)
    message_norm = normalize_text(message)
    if not objective_norm or objective_norm in OBJECTIVE_PLACEHOLDER_VALUES:
        return _message_specificity_score(message_norm)
    words = objective_norm.split()
    score = 0.85 if len(words) >= 4 else 0.65
    if objective_norm in message_norm:
        score = min(1.0, score + 0.1)
    if _is_vague_objective(objective):
        score = max(0.45, min(score, _message_specificity_score(message_norm)))
    return max(0.0, min(1.0, score))


def _evidence_strength_score(working_set: dict[str, Any] | None) -> float:
    if not isinstance(working_set, dict):
        return 0.45
    evidence_count_raw = working_set.get("evidence_count", 0)
    try:
        evidence_count = int(evidence_count_raw)
    except (TypeError, ValueError):
        evidence_count = 0
    if evidence_count >= 10:
        count_score = 0.9
    elif evidence_count >= 4:
        count_score = 0.72
    elif evidence_count > 0:
        count_score = 0.55
    else:
        count_score = 0.42

    patterns = working_set.get("top_patterns", [])
    if isinstance(patterns, list) and patterns:
        confidences = [
            float(item.get("confidence", 0.5))
            for item in patterns
            if isinstance(item, dict)
        ]
        if confidences:
            pattern_score = max(0.0, min(1.0, sum(confidences[:5]) / max(1, min(len(confidences), 5))))
            blended_score = (0.6 * count_score) + (0.4 * pattern_score)
            return max(0.0, min(1.0, max(count_score, blended_score)))
    return count_score


def _outcome_stability_score(recent_outcomes: list[dict[str, Any]] | None) -> float:
    if not isinstance(recent_outcomes, list) or not recent_outcomes:
        return 0.55
    window = recent_outcomes[-12:]
    success = 0
    blocked = 0
    failure = 0
    for row in window:
        result = str(row.get("result", "")).strip().lower() if isinstance(row, dict) else ""
        if result == "success":
            success += 1
        elif result == "blocked":
            blocked += 1
        elif result:
            failure += 1
    total = max(1, success + blocked + failure)
    return max(0.0, min(1.0, (success + 0.5 * blocked) / total))


# The weights of `context_quality_score`, in one place because both backends read them and a
# fork between them is a divergence in when the system asks its owner before acting.
#
# There is no `decision_confidence` weight here and there is no parameter for one, which is the
# point.  The score used to take 0.40 of its value from `decision_confidence`, and one of that
# number's four terms is `classifier_certainty` -- how the system classified its OWN response.
# The reply therefore fed the score, and the score gates bounded retrieval and the `needs_human`
# escalation, so a turn could talk itself past its own safety gate: the lowest-evidence turns
# produced the most assertive text, the text classified as DECISIVE, DECISIVE scored 0.92 against
# HANDOFF's 0.58, and the gate that existed to catch exactly those turns went up instead of down.
#
# What is left measures context and nothing else.  Two inputs a reader might expect are absent on
# purpose:
#   * corroboration (`Adequacy.agreement_share`) is 0 unless a neighbour maps onto an offered
#     candidate option, and an ordinary takeover turn offers none, so weighting it would deflate
#     every turn by a constant and measure nothing.
#   * retrieval outcome is not knowable here: this score is computed before retrieval runs and is
#     what triggers it.  Feeding the outcome back would be the same circularity again.
CONTEXT_QUALITY_WEIGHTS: dict[str, float] = {
    "evidence_strength": 0.45,
    "recency_coverage": 0.30,
    "outcome_stability": 0.25,
}


def compute_context_quality_score(
    *,
    evidence_strength: float,
    recency_coverage: float,
    outcome_stability: float,
) -> float:
    """How good is the CONTEXT for this decision -- never how confident the answer sounds.

    Each argument is a 0..1 context measurement:

    ``evidence_strength``
        prior decisions close enough to be evidence (``Adequacy.above_floor_count``), not the
        citation count -- citations are timeline event ids, i.e. retrieval provenance.
    ``recency_coverage``
        half-life decay on the age of that evidence, floored when there is none.  A corpus with
        nothing in it has a median evidence age of zero, and must not read as perfectly fresh.
    ``outcome_stability``
        how recent execution attempts actually went.
    """

    weights = CONTEXT_QUALITY_WEIGHTS
    score = (
        (weights["evidence_strength"] * max(0.0, min(1.0, float(evidence_strength))))
        + (weights["recency_coverage"] * max(0.0, min(1.0, float(recency_coverage))))
        + (weights["outcome_stability"] * max(0.0, min(1.0, float(outcome_stability))))
    )
    return round(max(0.0, min(1.0, score)), 4)


# The weights of `decision_confidence`, in one place because both backends read them.
#
# There is no `classifier_certainty` weight here and there is no `classification` parameter on the
# function, which is the point.  The score used to take 0.15 of its value from how the system had
# classified its OWN prior text (`body.executor_output`): DECISIVE scored 0.92 against HANDOFF's
# 0.58.  `decision_confidence < needs_human_threshold` is the `low_decision_confidence` escalation
# cause, so a turn that sounded sure of itself lowered its own bar for asking its owner -- the same
# circularity `CONTEXT_QUALITY_WEIGHTS` above exists to keep out, in the last place it survived.
# How confident an answer sounds is not evidence about whether to ask.
#
# The three remaining terms are the previous 0.40/0.25/0.20 rescaled over their own sum (0.85), so
# their ratios to each other are untouched and only the removed term's mass is redistributed.
DECISION_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "objective_clarity": 0.47,
    "evidence_strength": 0.29,
    "outcome_stability": 0.24,
}


def compute_decision_confidence(
    objective: str,
    message: str,
    working_set: dict[str, Any] | None = None,
    recent_outcomes: list[dict[str, Any]] | None = None,
) -> tuple[float, dict[str, float]]:
    """How well-founded is this decision -- never how assertive the system's own text sounded."""

    objective_clarity = _objective_clarity_score(objective, message)
    evidence_strength = _evidence_strength_score(working_set)
    outcome_stability = _outcome_stability_score(recent_outcomes)
    weights = DECISION_CONFIDENCE_WEIGHTS
    confidence = (
        (weights["objective_clarity"] * objective_clarity)
        + (weights["evidence_strength"] * evidence_strength)
        + (weights["outcome_stability"] * outcome_stability)
    )
    confidence = max(0.0, min(1.0, round(confidence, 4)))
    components = {
        "objective_clarity": round(objective_clarity, 4),
        "evidence_strength": round(evidence_strength, 4),
        "outcome_stability": round(outcome_stability, 4),
    }
    return confidence, components


def recent_failure_count(recent_outcomes: list[dict[str, Any]] | None, window: int = 8) -> int:
    if not isinstance(recent_outcomes, list) or not recent_outcomes:
        return 0
    failures = 0
    for row in recent_outcomes[-window:]:
        if not isinstance(row, dict):
            continue
        result = str(row.get("result", "")).strip().lower()
        if result == "failure":
            failures += 1
    return failures


def should_trigger_deliberation(
    decision_confidence: float,
    objective_changed: bool,
    turn_count: int,
    recent_failures: int,
    message: str,
) -> bool:
    if objective_changed:
        return True
    if decision_confidence < 0.78:
        return True
    if turn_count > 0 and turn_count % 6 == 0:
        return True
    if recent_failures >= 2:
        return True
    normalized_message = normalize_text(message)
    return any(token in normalized_message for token in _DELIBERATION_HINTS)


def update_recent_outcomes(
    recent_outcomes: list[dict[str, Any]] | None,
    result: str,
    *,
    turn: int = 0,
    latency_ms: int | float | None = 0,
    max_items: int = 20,
) -> tuple[list[dict[str, Any]], float]:
    try:
        latency_value = int(float(latency_ms)) if latency_ms is not None else 0
    except (TypeError, ValueError):
        latency_value = 0
    current = recent_outcomes if isinstance(recent_outcomes, list) else []
    next_rows = list(current)
    next_rows.append(
        {
            "turn": int(turn),
            "result": result.strip().lower(),
            "latency_ms": max(0, latency_value),
            "ts": datetime.now(tz=UTC).isoformat(),
        }
    )
    next_rows = next_rows[-max(1, max_items):]
    stability = _outcome_stability_score(next_rows)
    autonomy_score = round(max(0.0, min(1.0, (0.7 * stability) + 0.3)), 4)
    return next_rows, autonomy_score


def build_next_action(
    objective: str,
    mode: TakeoverMode,
    safety_decision: SafetyDecision,
    needs_human: bool,
) -> dict[str, str]:
    if safety_decision == SafetyDecision.CONFIRM_REQUIRED:
        return {
            "kind": "confirm",
            "target": objective[:220],
            "rationale": "high-risk action detected; confirmation required",
        }
    if safety_decision == SafetyDecision.BLOCKED:
        return {
            "kind": "abort",
            "target": objective[:220],
            "rationale": "high-risk action blocked by operator policy",
        }
    if needs_human:
        return {
            "kind": "escalate",
            "target": objective[:220],
            "rationale": "confidence below autonomy threshold",
        }
    return {
        "kind": "execute" if mode == TakeoverMode.TAKEOVER else "suggest",
        "target": objective[:220],
        "rationale": "bounded-autonomy execution",
    }


# "can't"/"won't" collapse to cant/wont in _tokenize_normalized; include them
# so a negated confirmation ("I can't confirm this") never releases the gate.
_CONFIRM_NEGATION_TOKENS = {"not", "never", "no", "dont", "cant", "wont", "without", "cannot"}


def _keyword_span_indices(tokens: list[str], key_tokens: list[str]) -> list[int]:
    if not key_tokens:
        return []
    span = len(key_tokens)
    return [idx for idx in range(len(tokens) - span + 1) if tokens[idx : idx + span] == key_tokens]


def _confirmation_is_affirmative(normalized_message: str, keyword: str) -> bool:
    """True only when the confirm keyword appears as a whole word and is not negated.

    Guards against substring leaks ("confirmation") and negated forms
    ("don't confirm", "I can't confirm this", "no confirm").
    """
    key_tokens = _tokenize_normalized(normalize_text(keyword))
    if not key_tokens:
        return False
    tokens = _tokenize_normalized(normalized_message)
    for idx in _keyword_span_indices(tokens, key_tokens):
        start = max(0, idx - 3)
        if any(tokens[pos] in _CONFIRM_NEGATION_TOKENS for pos in range(start, idx)):
            continue
        return True
    return False


def _keyword_present_whole_word(normalized_message: str, keyword: str) -> bool:
    """Whole-word presence check (used for denials, which default to the safe side)."""
    key_tokens = _tokenize_normalized(normalize_text(keyword))
    if not key_tokens:
        return False
    tokens = _tokenize_normalized(normalized_message)
    return bool(_keyword_span_indices(tokens, key_tokens))


def evaluate_safety(
    policy: TakeoverPolicy,
    message: str,
    final_response: str | None,
    takeover_context: dict[str, Any],
    objective: str | None = None,
) -> tuple[SafetyDecision, str | None]:
    """Screen a turn for a high-risk action.

    ``objective`` is the resolved task the turn is working on, and it is read directly.
    Before P4 the objective reached this gate only by accident: a cold-start marker in the
    candidate text failed the DECISIVE pass-through in ``ensure_takeover_response``, was
    rewritten by ``build_decisive_response`` -- which embeds the task -- and the task text
    then arrived here inside ``final_response``.  Removing that inverted rewrite (correctly)
    removed the only route by which ``rm -rf`` in a ``task`` field ever reached a risk check,
    so a high-risk objective returned ALLOW and froze no safety opportunity.  A safety gate
    must not depend on a prose-rewrite side effect for its input, so it now reads the
    objective itself.
    """

    if policy.safety_policy != "high-risk-pause":
        return SafetyDecision.ALLOW, None
    pending = takeover_context.get("pending_safety")
    normalized_message = normalize_text(message)
    confirm_keyword = normalize_text(policy.confirm_keyword)
    deny_keyword = normalize_text(policy.deny_keyword)
    if isinstance(pending, dict):
        # Denial is checked first and wins over a co-occurring confirm; the
        # gate defaults to CONFIRM_REQUIRED so anything ambiguous stays paused.
        if deny_keyword and _keyword_present_whole_word(normalized_message, deny_keyword):
            return SafetyDecision.BLOCKED, "denied_high_risk"
        if confirm_keyword and _confirmation_is_affirmative(normalized_message, confirm_keyword):
            return SafetyDecision.ALLOW, "confirmed_high_risk"
        return SafetyDecision.CONFIRM_REQUIRED, "awaiting_high_risk_confirmation"
    risk = high_risk_reason(message) or high_risk_reason(objective) or high_risk_reason(final_response)
    if not risk:
        return SafetyDecision.ALLOW, None
    return SafetyDecision.CONFIRM_REQUIRED, risk


def expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(tz=UTC)
    except ValueError:
        return True


def next_expiry(minutes: int) -> str:
    return (datetime.now(tz=UTC) + timedelta(minutes=max(1, minutes))).isoformat()
