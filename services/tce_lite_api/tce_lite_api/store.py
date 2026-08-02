from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from fastapi import HTTPException
from tce_shared.autonomy_context import (
    SUMMARY_VERSION,
    autonomy_profile_tuning,
    build_retry_feedback,
    coerce_summary_l1,
    plan_retrieval_subqueries,
    propagate_episode_score,
    summarize_event_record,
    summarize_hit_text,
    summary_coverage_ratio,
)
from tce_shared.autonomy_goals import (
    adjust_consultative_threshold,
    classify_risk_tier,
    continuity_health,
    evaluate_execution_permit,
    score_goal,
)
from tce_shared.behavior_fidelity import behavior_storage_gate, normalize_behavior_evidence
from tce_shared.events import (
    AgentRole,
    AutonomyGoalSource,
    AutonomyGoalStatus,
    AutonomyNotice,
    AutonomyPolicyProfile,
    AutonomyRiskTier,
    CloneAdviceRequest,
    CloneAdviceResponse,
    DirectiveExecution,
    DirectiveExecutionState,
    EventEnvelope,
    EventFilter,
    EventSearchHit,
    EventSearchRequest,
    EventSearchResponse,
    EventType,
    ExecutionClaimRequest,
    ExecutionPermitDecision,
    ExecutionPermitRequest,
    ExecutionPermitResolveRequest,
    ExecutionPermitResponse,
    ExecutionReportRequest,
    ExecutionStatusResponse,
    FailureClass,
    GoalKind,
    OperationMode,
    PatternFeedbackRequest,
    ResumePacketAnchor,
    ResumePacketChangeSummary,
    ResumePacketFileItem,
    ResumePacketRequest,
    ResumePacketResponse,
    ResumePacketRetrievalMeta,
    RetryStrategy,
    RuntimeModeConfig,
    SafetyDecision,
    TakeoverAutonomyStatusResponse,
    TakeoverAutonomyTickRequest,
    TakeoverAutonomyTickResponse,
    TakeoverClassification,
    TakeoverDecisionSource,
    TakeoverFeedbackRequest,
    TakeoverFeedbackResponse,
    TakeoverGoal,
    TakeoverLatencyBreakdown,
    TakeoverMode,
    TakeoverNextAction,
    TakeoverNoticeAckRequest,
    TakeoverNoticesResponse,
    TakeoverPreloadRequest,
    TakeoverPreloadResponse,
    TakeoverState,
    TakeoverStepRequest,
    TakeoverStepResponse,
)
from tce_shared.failure_classifier import classify_failure, retry_strategy_for_attempt
from tce_shared.fingerprint import (
    DEFAULT_FINGERPRINT,
    apply_feedback_to_fingerprint,
    feedback_adjusted_alpha,
    merge_observation_into_fingerprint,
)
from tce_shared.goal_affect import classify_goal_kind, compute_affective_scores, score_goal_affective
from tce_shared.goal_cache import (
    goal_cache_deserialize,
    goal_cache_get_l1,
    goal_cache_invalidate_l1,
    goal_cache_key,
    goal_cache_put_l1,
)
from tce_shared.goal_similarity import dedupe_candidates_by_similarity
from tce_shared.handoff import (
    handoff_intent,
    latest_checkpoint_anchor,
    merge_anchors,
    normalize_anchor_list,
    normalize_handoff_mode,
    normalize_milestone_v1,
    normalize_objective_text,
    rank_resume_candidates,
    task_overlap_score,
)
from tce_shared.redaction import apply_redaction_zones, redact_payload, redact_text
from tce_shared.situation import SITUATION_TYPES, classify_situation
from tce_shared.takeover import (
    build_decisive_response,
    build_next_action,
    classify_text,
    compute_decision_confidence,
    contains_phrase,
    ensure_takeover_response,
    evaluate_safety,
    mode_override,
    next_expiry,
    normalize_text,
    objective_hash,
    persona_defaults,
    recent_failure_count,
    resolve_objective,
    sanitize_untrusted_objective,
    should_trigger_deliberation,
    update_recent_outcomes,
)

from .auth import AuthContext
from .config import Settings, get_settings
from .continuity_store import (
    deliver_handoff_safely,
    enqueue_handoff,
    record_resume_attempt,
    record_resume_progress,
)
from .store_graph import (
    _ensure_owner_membership,
    _index_graph,
    _scope_match,
    graph_snapshot_for_events,
)
from .types import (
    CloneArbitrationRequest,
    CloneArbitrationResponse,
    ContextBundleRequest,
    ContextBundleResponse,
    EvidenceEvent,
    PatternItem,
)

_FTS5_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_FTS5_MAX_TERMS = 32
_FTS5_MAX_TOKEN_LEN = 40


def _build_fts5_query(query_text: str) -> str:
    """Return an operator-safe, bounded OR query for SQLite FTS5."""
    terms: list[str] = []
    for token in _FTS5_TOKEN_RE.findall(query_text or ""):
        normalized = token.lower()
        if len(normalized) < 2 or len(normalized) > _FTS5_MAX_TOKEN_LEN:
            continue
        if normalized in terms:
            continue
        terms.append(normalized)
        if len(terms) >= _FTS5_MAX_TERMS:
            break
    return " OR ".join(f'"{term}"' for term in terms)

_RETRIEVAL_COUNTERS_LITE: dict[str, int] = {
    "none": 0,
    "pgvector_ann": 0,
    "lexical_only": 0,
    "hybrid_fallback": 0,
    "qdrant": 0,
}
_ACTIVATION_MEMO_LITE: dict[tuple[str, str, str], tuple[int, float]] = {}
_SITUATION_ALIAS_MAP: dict[str, str] = {
    "restart_safety": "escalation_point",
    "uncertainty": "unknown_territory",
    "fallback_reliability": "error_occurred",
    "provider_selection": "choice_required",
    "decision_required": "choice_required",
    "goal_conflict": "conflict_detected",
    "setup_configuration": "routine_task",
    "opportunity_detected": "prioritization_needed",
    "planning": "prioritization_needed",
    "risk_assessment": "escalation_point",
}
_QUERY_EXPANSION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "bug": ("error", "issue", "defect"),
    "fix": ("repair", "resolve"),
    "implement": ("build", "create"),
    "refactor": ("cleanup", "simplify"),
    "test": ("verify", "validation"),
    "deploy": ("release", "rollout"),
}
_RETRYABLE_DB_TOKENS = (
    "database is locked",
    "database is busy",
    "locked",
    "busy",
    "timeout",
    "temporarily unavailable",
)
_CROSS_USER_HINT_RE_LITE = re.compile(r"(?:@|user[:=]|owner[:=]|from\s+)([a-z0-9][a-z0-9._-]{1,63})")
_CROSS_USER_TRIGGER_RE_LITE = re.compile(r"\b(cross[-\s]?user|across users?|same workspace|shared workspace|other user)\b")
_CROSS_USER_HISTORY_RE_LITE = re.compile(
    r"\b(history|chat|conversation|session|discuss|discussion|recent work|recent changes|what did)\b"
)
_CROSS_USER_HANDOFF_RE_LITE = re.compile(
    r"\b(continue|resume|pick up|handoff|hand off|follow up)\b.*\b(codex|claude)\b"
)
_CROSS_USER_DIRECT_NAMES_LITE = ("codex", "claude")


class _LiteMMRCandidate(TypedDict):
    score: float
    hit: EventSearchHit
    row: sqlite3.Row
    tokens: set[str]


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _is_retryable_db_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    return any(token in text for token in _RETRYABLE_DB_TOKENS)


def _run_sql_retry[T](
    fn: Callable[[], T],
    *,
    settings: Settings,
) -> T:
    retry_enabled = bool(getattr(settings, "search_retry_enabled", True))
    if not retry_enabled:
        return fn()
    attempts = max(1, int(getattr(settings, "search_retry_max_attempts", 2)))
    backoff_ms = max(1, int(getattr(settings, "search_retry_backoff_ms", 8)))
    retry_budget_ms = max(0, int(getattr(settings, "search_retry_budget_ms", 40)))
    started = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if attempt >= attempts - 1 or not _is_retryable_db_error(exc):
                raise
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            remaining_ms = retry_budget_ms - elapsed_ms
            if remaining_ms <= 0:
                raise
            delay_ms = min(remaining_ms, backoff_ms * (2 ** attempt))
            time.sleep(float(delay_ms) / 1000.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("sqlite retry loop exited unexpectedly")


def _bump_retrieval_counter(source: str) -> None:
    key = source if source in _RETRIEVAL_COUNTERS_LITE else "hybrid_fallback"
    _RETRIEVAL_COUNTERS_LITE[key] = int(_RETRIEVAL_COUNTERS_LITE.get(key, 0)) + 1


def summary_window(period: str) -> tuple[datetime, datetime]:
    now = now_utc()
    normalized = period.strip().lower()
    if normalized == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if normalized in {"week", "this_week"}:
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6), now
    raise HTTPException(status_code=400, detail="period must be 'today' or 'week'")


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _sanitize_outcome_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = dict(raw)
    if "success" not in value:
        token = str(
            value.get("status")
            or value.get("label")
            or value.get("result")
            or value.get("outcome")
            or ""
        ).strip().lower()
        sentiment = str(value.get("sentiment") or "").strip().lower()
        success = False
        if token in {"success", "succeeded", "ok", "accepted", "done", "completed", "pass", "improved"}:
            success = True
        elif token in {"failure", "failed", "error", "blocked", "timeout", "rejected"}:
            success = False
        elif sentiment == "positive":
            success = True
        elif sentiment == "negative":
            success = False
        value["success"] = success
    if not isinstance(value.get("metrics"), dict):
        metrics: dict[str, Any] = {}
        for key in ("status", "label", "result", "outcome", "sentiment"):
            if key in value:
                metrics[key] = value.get(key)
        value["metrics"] = metrics
    followups = value.get("followups")
    if not isinstance(followups, list):
        if isinstance(followups, str) and followups.strip():
            value["followups"] = [followups.strip()]
        else:
            value["followups"] = []
    return value


def _sanitize_decision_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    choice = str(raw.get("choice") or "").strip()
    rationale = str(raw.get("rationale") or "").strip()
    if not choice or not rationale:
        return None
    alternatives_raw = raw.get("alternatives")
    signals_raw = raw.get("signals_used")
    return {
        "choice": choice,
        "alternatives": [str(item).strip() for item in alternatives_raw] if isinstance(alternatives_raw, list) else [],
        "rationale": rationale,
        "signals_used": [str(item).strip() for item in signals_raw] if isinstance(signals_raw, list) else [],
    }


def _sanitize_style_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = dict(raw)
    do_first = value.get("do_first")
    do_last = value.get("do_last")
    value["do_first"] = [str(item).strip() for item in do_first] if isinstance(do_first, list) else []
    value["do_last"] = [str(item).strip() for item in do_last] if isinstance(do_last, list) else []
    return value


def _sanitize_links_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = dict(raw)
    docs = value.get("docs")
    value["docs"] = [str(item).strip() for item in docs] if isinstance(docs, list) else []
    return value


def _sanitize_steps_payload(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if isinstance(item, dict):
            description = str(item.get("description") or "").strip()
            if not description:
                continue
            out.append(
                {
                    "order": _safe_int(item.get("order")) if item.get("order") is not None else idx,
                    "description": description,
                    "tool": item.get("tool"),
                    "output_ref": item.get("output_ref"),
                }
            )
            continue
        text_value = str(item).strip()
        if text_value:
            out.append({"order": idx, "description": text_value})
    return out


def max_read_sensitivity(settings: Settings) -> int:
    return max(0, min(3, settings.block_sensitivity - 1))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return default
        return parsed
    except (TypeError, ValueError):
        return default


def _collapse_text_lite(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _redacted_excerpt_lite(value: Any, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    normalized = _collapse_text_lite(value)
    if not normalized:
        return ""
    redacted, _ = redact_text(normalized)
    return redacted[:max_chars].strip()


def _normalize_string_list_lite(value: Any, *, max_items: int, item_max_chars: int) -> list[str]:
    if not isinstance(value, list) or max_items <= 0:
        return []
    normalized: list[str] = []
    for item in value:
        text = _redacted_excerpt_lite(item, max_chars=item_max_chars)
        if not text:
            continue
        normalized.append(text)
        if len(normalized) >= max_items:
            break
    return normalized


def _citation_excerpt_from_row_lite(row: sqlite3.Row, *, snippet_chars: int) -> str:
    title = _collapse_text_lite(row["title"])
    payload = json_loads(row["payload"], {})
    summary = ""
    if isinstance(payload, dict):
        for key in ("summary", "message", "result", "decision"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                summary = candidate
                break
    source = f"{title} | {summary}" if summary else title
    return _redacted_excerpt_lite(source, max_chars=snippet_chars)


def _build_citation_snippets_lite(
    conn: sqlite3.Connection,
    *,
    citation_ids: list[UUID],
    settings: Settings,
    evidence_events: list[Any] | None = None,
) -> list[dict[str, str]]:
    max_items = max(1, int(getattr(settings, "takeover_citation_snippet_max_items", 20)))
    snippet_chars = max(32, int(getattr(settings, "takeover_citation_snippet_max_chars", 100)))
    if not citation_ids:
        return []
    ordered_ids = citation_ids[:max_items]

    evidence_lookup: dict[str, str] = {}
    if isinstance(evidence_events, list):
        for event in evidence_events:
            event_id = str(getattr(event, "id", "") or "").strip()
            title = str(getattr(event, "title", "") or "").strip()
            key_fields = getattr(event, "key_payload_fields", None)
            if not event_id or not title:
                continue
            details: list[str] = []
            if isinstance(key_fields, dict):
                for key, value in key_fields.items():
                    if isinstance(value, (str, int, float, bool)):
                        details.append(f"{key}={value}")
                    if len(details) >= 2:
                        break
            source = f"{title} | {'; '.join(details)}" if details else title
            evidence_excerpt = _redacted_excerpt_lite(source, max_chars=snippet_chars)
            if evidence_excerpt:
                evidence_lookup[event_id] = evidence_excerpt

    missing_ids: list[UUID] = []
    snippets: list[dict[str, str]] = []
    for citation_id in ordered_ids:
        event_id = str(citation_id)
        cached_excerpt = evidence_lookup.get(event_id)
        if cached_excerpt:
            snippets.append({"id": event_id, "excerpt": cached_excerpt})
        else:
            missing_ids.append(citation_id)

    if missing_ids:
        placeholders = ",".join("?" for _ in missing_ids)
        rows = conn.execute(
            f"SELECT id, title, payload FROM events WHERE id IN ({placeholders})",  # noqa: S608
            tuple(str(item) for item in missing_ids),
        ).fetchall()
        row_lookup = {str(row["id"]): row for row in rows}
        for citation_id in missing_ids:
            row = row_lookup.get(str(citation_id))
            if row is None:
                continue
            row_excerpt = _citation_excerpt_from_row_lite(row, snippet_chars=snippet_chars)
            if row_excerpt:
                snippets.append({"id": str(citation_id), "excerpt": row_excerpt})

    snippet_lookup = {item["id"]: item["excerpt"] for item in snippets if item.get("id") and item.get("excerpt")}
    ordered: list[dict[str, str]] = []
    for citation_id in ordered_ids:
        event_id = str(citation_id)
        ordered_excerpt = snippet_lookup.get(event_id)
        if ordered_excerpt:
            ordered.append({"id": event_id, "excerpt": ordered_excerpt})
    return ordered


def _normalize_execution_transcript_details_lite(
    details_map: dict[str, Any], *, settings: Settings
) -> tuple[dict[str, Any], list[str]]:
    normalized: dict[str, Any] = {}
    issues: list[str] = []

    tools_raw = details_map.get("tools_called", details_map.get("tool_calls"))
    if tools_raw is not None:
        if isinstance(tools_raw, list):
            normalized["tools_called"] = _normalize_string_list_lite(tools_raw, max_items=12, item_max_chars=120)
        else:
            issues.append("tools_called must be a list")

    findings_raw = details_map.get("key_findings", details_map.get("findings"))
    if findings_raw is not None:
        if isinstance(findings_raw, list):
            normalized["key_findings"] = _normalize_string_list_lite(findings_raw, max_items=12, item_max_chars=180)
        else:
            issues.append("key_findings must be a list")

    files_raw = details_map.get("files_modified", details_map.get("files"))
    if files_raw is not None:
        if isinstance(files_raw, list):
            normalized["files_modified"] = _normalize_string_list_lite(files_raw, max_items=20, item_max_chars=180)
        else:
            issues.append("files_modified must be a list")

    reasoning_raw = details_map.get("reasoning_summary", details_map.get("summary"))
    if reasoning_raw is not None:
        if isinstance(reasoning_raw, str):
            normalized["reasoning_summary"] = _redacted_excerpt_lite(
                reasoning_raw,
                max_chars=max(120, int(getattr(settings, "takeover_execution_reasoning_summary_max_chars", 500))),
            )
        else:
            issues.append("reasoning_summary must be a string")

    return normalized, issues


def _cap_snapshot_payload_lite(snapshot: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    if max_chars <= 0:
        return snapshot
    working = dict(snapshot)

    def _encoded_len(payload: dict[str, Any]) -> int:
        try:
            return len(json_dumps(payload))
        except Exception:
            return 0

    if _encoded_len(working) <= max_chars:
        return working

    for key in ("evidence_snippets", "alternatives_considered"):
        values = working.get(key)
        while isinstance(values, list) and values and _encoded_len(working) > max_chars:
            values.pop()
        if isinstance(values, list) and not values:
            working.pop(key, None)

    if _encoded_len(working) > max_chars and isinstance(working.get("reasoning_summary"), str):
        working["reasoning_summary"] = _redacted_excerpt_lite(working.get("reasoning_summary", ""), max_chars=160)
    if _encoded_len(working) > max_chars and isinstance(working.get("user_question"), str):
        working["user_question"] = _redacted_excerpt_lite(working.get("user_question", ""), max_chars=160)
    if _encoded_len(working) > max_chars:
        working.pop("reasoning_summary", None)
    if _encoded_len(working) > max_chars:
        working.pop("alternatives_considered", None)
    if _encoded_len(working) > max_chars:
        working.pop("evidence_snippets", None)
    return working


def _build_enriched_execution_context_snapshot_lite(
    *,
    settings: Settings,
    base_snapshot: dict[str, Any],
    takeover_context: dict[str, Any],
    working_set: dict[str, Any],
    details_map: dict[str, Any],
    transcript: dict[str, Any],
) -> dict[str, Any]:
    if not bool(getattr(settings, "takeover_rationale_enrichment_enabled", True)):
        return base_snapshot
    if not bool(getattr(settings, "takeover_context_snapshot_enrichment_enabled", True)):
        return base_snapshot

    max_evidence = max(1, int(getattr(settings, "takeover_context_snapshot_max_evidence_snippets", 3)))
    max_alternatives = max(1, int(getattr(settings, "takeover_context_snapshot_max_alternatives", 3)))
    max_chars = max(240, int(getattr(settings, "takeover_context_snapshot_max_chars", 1000)))

    user_question = (
        details_map.get("user_question")
        or takeover_context.get("last_user_message")
        or takeover_context.get("objective")
        or ""
    )
    alternatives = _normalize_string_list_lite(
        details_map.get("alternatives_considered", details_map.get("alternatives")),
        max_items=max_alternatives,
        item_max_chars=160,
    )
    evidence_snippets: list[str] = []
    for item in working_set.get("citation_snippets", []) if isinstance(working_set.get("citation_snippets"), list) else []:
        if not isinstance(item, dict):
            continue
        excerpt = _redacted_excerpt_lite(
            item.get("excerpt"),
            max_chars=max(32, int(getattr(settings, "takeover_citation_snippet_max_chars", 100))),
        )
        if excerpt:
            evidence_snippets.append(excerpt)
        if len(evidence_snippets) >= max_evidence:
            break
    if not evidence_snippets:
        evidence_snippets = _normalize_string_list_lite(
            transcript.get("key_findings"),
            max_items=max_evidence,
            item_max_chars=max(48, int(getattr(settings, "takeover_citation_snippet_max_chars", 100))),
        )

    enriched = dict(base_snapshot)
    if user_question:
        enriched["user_question"] = _redacted_excerpt_lite(user_question, max_chars=280)
    if alternatives:
        enriched["alternatives_considered"] = alternatives
    if evidence_snippets:
        enriched["evidence_snippets"] = evidence_snippets
    if isinstance(transcript.get("reasoning_summary"), str) and transcript.get("reasoning_summary"):
        enriched["reasoning_summary"] = _redacted_excerpt_lite(
            transcript["reasoning_summary"],
            max_chars=max(120, int(getattr(settings, "takeover_execution_reasoning_summary_max_chars", 500))),
        )
    return _cap_snapshot_payload_lite(enriched, max_chars=max_chars)


def _canonical_situation_type(raw: str | None) -> str:
    token = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    valid = set(SITUATION_TYPES)
    if token in valid:
        return token
    if token in _SITUATION_ALIAS_MAP:
        mapped = _SITUATION_ALIAS_MAP[token]
        if mapped in valid:
            return mapped
    if token:
        inferred = classify_situation(token)
        if inferred in valid:
            return inferred
    return "routine_task"


def _read_activation_scores_lite(
    *,
    workspace_id: str,
    owner_id: str,
    event_ids: list[Any],
    settings: Settings,
) -> dict[str, float]:
    if not bool(getattr(settings, "memory_activation_enabled", True)) or not event_ids:
        return {}
    half_life_days = max(1, int(getattr(settings, "memory_activation_half_life_days", 45)))
    now_epoch = time.time()
    out: dict[str, float] = {}
    for event_id in event_ids:
        key = (workspace_id, owner_id, str(event_id))
        hit = _ACTIVATION_MEMO_LITE.get(key)
        if not hit:
            continue
        access_count, last_access_epoch = hit
        days_since_access = max(0.0, (now_epoch - float(last_access_epoch)) / 86400.0)
        freq_score = min(1.0, math.log1p(float(max(0, access_count))) / math.log(16.0))
        recency_score = math.exp(-days_since_access / float(half_life_days))
        activation = max(0.0, min(1.0, (0.6 * freq_score) + (0.4 * recency_score)))
        out[str(event_id)] = activation
    return out


def _bump_activation_scores_lite(
    *,
    workspace_id: str,
    owner_id: str,
    event_ids: list[Any],
    settings: Settings,
) -> None:
    if not bool(getattr(settings, "memory_activation_enabled", True)) or not event_ids:
        return
    now_epoch = time.time()
    for event_id in event_ids:
        key = (workspace_id, owner_id, str(event_id))
        prev_count, _ = _ACTIVATION_MEMO_LITE.get(key, (0, 0.0))
        _ACTIVATION_MEMO_LITE[key] = (int(prev_count) + 1, now_epoch)


def _normalize_title_lite(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _collapse_whitespace_lite(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_owner_token_lite(value: Any) -> str:
    return str(value or "").strip().lower()


def _query_requests_cross_user_memory_lite(query_text: str) -> bool:
    lowered = _collapse_whitespace_lite(query_text).lower()
    if not lowered:
        return False
    if _CROSS_USER_TRIGGER_RE_LITE.search(lowered):
        return True
    if _CROSS_USER_HANDOFF_RE_LITE.search(lowered):
        return True
    if "memory" in lowered and "from " in lowered:
        return True
    if "timeline" in lowered and "from " in lowered:
        return True
    if "vice versa" in lowered:
        return True
    if any(name in lowered for name in _CROSS_USER_DIRECT_NAMES_LITE):
        if "memory" in lowered:
            return True
        if "timeline" in lowered:
            return True
        if _CROSS_USER_HISTORY_RE_LITE.search(lowered):
            return True
    return False


def _extract_owner_hints_lite(query_text: str) -> set[str]:
    lowered = _collapse_whitespace_lite(query_text).lower()
    hints = {_normalize_owner_token_lite(match.group(1)) for match in _CROSS_USER_HINT_RE_LITE.finditer(lowered)}
    for token in re.split(r"[^a-z0-9._-]+", lowered):
        normalized = _normalize_owner_token_lite(token)
        if normalized in _CROSS_USER_DIRECT_NAMES_LITE:
            hints.add(normalized)
    return {hint for hint in hints if hint}


def _owner_matches_hint_lite(owner_id: str, hint: str) -> bool:
    owner = _normalize_owner_token_lite(owner_id)
    token = _normalize_owner_token_lite(hint)
    if not owner or not token:
        return False
    return token == owner or token in owner or owner in token


def _milestone_score_boost_lite(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    if str(payload.get("milestone_schema") or "").strip().lower() == "v1":
        return 0.12
    return 0.0


def _load_handoff_records_map_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_ids: list[str],
    max_records: int,
) -> dict[str, dict[str, Any]]:
    if not owner_ids:
        return {}
    placeholders = ",".join("?" for _ in owner_ids)
    try:
        rows = conn.execute(
            f"""
            SELECT id, event_id, anchors_json, schema_version
            FROM handoff_records
            WHERE workspace_id = ?
              AND owner_id IN ({placeholders})
            ORDER BY ts DESC
            LIMIT ?
            """,
            (workspace_id, *owner_ids, max(1, int(max_records))),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    by_event: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = str(row["event_id"] or "").strip()
        if not event_id:
            continue
        anchors = json_loads(row["anchors_json"], [])
        by_event[event_id] = {
            "id": str(row["id"] or ""),
            "has_anchors": isinstance(anchors, list) and len(anchors) > 0,
            "schema_version": str(row["schema_version"] or "").strip().lower(),
        }
    return by_event


def _resolve_owner_scope_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    query_text: str,
) -> tuple[set[str], bool, list[str]]:
    normalized_owner = _normalize_owner_token_lite(owner_id)
    default_scope = {normalized_owner} if normalized_owner else set()
    if not _query_requests_cross_user_memory_lite(query_text):
        return default_scope, False, sorted(default_scope)

    owner_candidates = set(default_scope)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT json_extract(context, '$._tce_owner') AS owner_id
            FROM events
            WHERE json_extract(context, '$._tce_workspace') = ?
              AND json_extract(context, '$._tce_owner') IS NOT NULL
            LIMIT 400
            """,
            (workspace_id,),
        ).fetchall()
        for row in rows:
            normalized = _normalize_owner_token_lite(row["owner_id"] if row and "owner_id" in row.keys() else None)
            if normalized:
                owner_candidates.add(normalized)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return default_scope, False, sorted(default_scope)

    if not owner_candidates:
        return default_scope, False, sorted(default_scope)

    hints = _extract_owner_hints_lite(query_text)
    if hints:
        matched = {
            owner_candidate
            for owner_candidate in owner_candidates
            if any(_owner_matches_hint_lite(owner_candidate, hint) for hint in hints)
        }
    else:
        matched = set(owner_candidates)

    if normalized_owner:
        matched.add(normalized_owner)
    if not matched:
        return default_scope, False, sorted(default_scope)
    applied = matched != default_scope
    return matched, applied, sorted(matched)[:6]


def _scope_match_with_owner_scope_lite(
    context: dict[str, Any],
    *,
    workspace_id: str,
    owner_scope: set[str],
) -> bool:
    event_workspace = _normalize_owner_token_lite(context.get("_tce_workspace"))
    if event_workspace and event_workspace != _normalize_owner_token_lite(workspace_id):
        return False
    event_owner = _normalize_owner_token_lite(context.get("_tce_owner"))
    if event_owner and owner_scope and event_owner not in owner_scope:
        return False
    return True


def _expand_owner_scope_from_rows_lite(
    rows: list[sqlite3.Row],
    *,
    workspace_id: str,
    current_scope: set[str],
) -> set[str]:
    normalized_workspace = _normalize_owner_token_lite(workspace_id)
    expanded_scope = set(current_scope)
    for row in rows:
        context = json_loads(row["context"], {})
        if not isinstance(context, dict):
            continue
        event_workspace = _normalize_owner_token_lite(context.get("_tce_workspace"))
        if event_workspace and event_workspace != normalized_workspace:
            continue
        event_owner = _normalize_owner_token_lite(context.get("_tce_owner"))
        if event_owner:
            expanded_scope.add(event_owner)
    return expanded_scope


def _citation_dup_ratio_lite(rows: list[sqlite3.Row]) -> float:
    if not rows:
        return 0.0
    counts = Counter(_normalize_title_lite(row["title"]) for row in rows if row["title"])
    duplicates = sum(max(0, count - 1) for count in counts.values())
    return max(0.0, min(1.0, float(duplicates) / float(max(1, len(rows)))))


def _expand_query_terms_lite(query_text: str, *, max_terms: int) -> list[str]:
    tokens = [token.strip().lower() for token in re.split(r"\W+", query_text) if token.strip()]
    expanded: list[str] = []
    for token in tokens:
        for synonym in _QUERY_EXPANSION_SYNONYMS.get(token, ()):
            if synonym not in tokens and synonym not in expanded:
                expanded.append(synonym)
            if len(expanded) >= max_terms:
                return expanded
    return expanded[:max_terms]


def _search_scale_trigger_met_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
) -> bool:
    try:
        row = conn.execute(
            """
            SELECT COUNT(1) AS total
            FROM events
            WHERE (json_extract(context, '$._tce_workspace') IS NULL OR json_extract(context, '$._tce_workspace') = ?)
              AND (json_extract(context, '$._tce_owner') IS NULL OR json_extract(context, '$._tce_owner') = ?)
            """,
            (workspace_id, owner_id),
        ).fetchone()
        if int((row["total"] if row else 0) or 0) >= 10000:
            return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        pass
    try:
        cutoff = (now_utc() - timedelta(days=7)).isoformat()
        row = conn.execute(
            """
            SELECT
              COUNT(1) AS samples,
              AVG((COALESCE(style_alignment, 0.0) + COALESCE(decision_traceability, 0.0)) / 2.0) AS avg_score
            FROM retrieval_eval_runs
            WHERE workspace_id = ? AND user_id = ? AND completed_at >= ?
            """,
            (workspace_id, owner_id, cutoff),
        ).fetchone()
        if row and int(row["samples"] or 0) >= 3 and float(row["avg_score"] or 1.0) < 0.72:
            return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        pass
    return False


def _read_feedback_signals_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    event_ids: list[str],
    settings: Settings,
) -> dict[str, float]:
    if not bool(getattr(settings, "search_feedback_enabled", True)) or not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    if not placeholders:
        return {}
    try:
        rows = conn.execute(
            f"""
            SELECT
              je.value AS event_id,
              SUM(
                CASE WHEN LOWER(cf.feedback_type) IN ('helpful', 'positive', 'correct') THEN 1 ELSE 0 END
              ) AS positive_count,
              SUM(
                CASE WHEN LOWER(cf.feedback_type) IN ('unhelpful', 'negative', 'wrong') THEN 1 ELSE 0 END
              ) AS negative_count
            FROM decision_observations dobs
            JOIN clone_feedback cf ON cf.observation_id = dobs.id
            JOIN json_each(dobs.source_event_ids) je
            WHERE dobs.workspace_id = ?
              AND je.value IN ({placeholders})
            GROUP BY je.value
            """,
            (workspace_id, *event_ids),
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for row in rows:
        event_id = str(row["event_id"])
        positive = int(row["positive_count"] or 0)
        negative = int(row["negative_count"] or 0)
        if positive <= 0 and negative <= 0:
            continue
        total = max(1, positive + negative)
        out[event_id] = max(-1.0, min(1.0, float(positive - negative) / float(total)))
    return out


def _load_episode_score_lookup_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_ids: list[str],
    query_text: str,
    max_rows: int = 180,
) -> dict[str, float]:
    if not owner_ids or not str(query_text or "").strip():
        return {}
    placeholders = ",".join("?" for _ in owner_ids)
    try:
        rows = conn.execute(
            f"""
            SELECT eel.event_id, ep.goal, ep.context, ep.outcome
            FROM episodes ep
            JOIN episode_event_links eel ON eel.episode_id = ep.id
            WHERE ep.workspace_id = ?
              AND ep.user_id IN ({placeholders})
            ORDER BY ep.updated_at DESC
            LIMIT ?
            """,
            (workspace_id, *owner_ids, max(20, int(max_rows))),
        ).fetchall()
    except Exception:
        return {}
    scores: dict[str, float] = {}
    for row in rows:
        episode_text = " ".join(
            [str(row["goal"] or ""), str(row["context"] or ""), str(row["outcome"] or "")]
        ).strip()
        overlap = task_overlap_score(query_text, episode_text)
        if overlap <= 0:
            continue
        event_id = str(row["event_id"] or "").strip()
        if not event_id:
            continue
        scores[event_id] = max(float(scores.get(event_id, 0.0)), float(overlap))
    return scores


def _token_set_for_hit_lite(row: sqlite3.Row) -> set[str]:
    blob = " ".join(
        [
            str(row["title"] if "title" in row.keys() else ""),
            str(row["domain"] if "domain" in row.keys() else ""),
            str(row["task_type"] if "task_type" in row.keys() else ""),
        ]
    ).lower()
    return {token for token in re.split(r"\W+", blob) if token}


def _jaccard_similarity_lite(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return float(len(left & right)) / float(len(union))


def _mmr_select_lite(
    scored: list[tuple[float, EventSearchHit, sqlite3.Row]],
    *,
    top_k: int,
    mmr_lambda: float,
) -> list[tuple[float, EventSearchHit, sqlite3.Row]]:
    if top_k <= 0:
        return []
    candidates: list[_LiteMMRCandidate] = [
        {
            "score": score,
            "hit": hit,
            "row": row,
            "tokens": _token_set_for_hit_lite(row),
        }
        for score, hit, row in scored
    ]
    selected: list[_LiteMMRCandidate] = []
    while candidates and len(selected) < top_k:
        best_index = 0
        best_value = float("-inf")
        for index, item in enumerate(candidates):
            novelty = 0.0
            if selected:
                novelty = max(
                    _jaccard_similarity_lite(item["tokens"], chosen["tokens"]) for chosen in selected
                )
            mmr_score = (mmr_lambda * float(item["score"])) - ((1.0 - mmr_lambda) * novelty)
            if mmr_score > best_value:
                best_value = mmr_score
                best_index = index
        selected.append(candidates.pop(best_index))
    return [(float(item["score"]), item["hit"], item["row"]) for item in selected]


def _mmr_dedupe_key_lite(row: sqlite3.Row) -> str:
    title = _normalize_title_lite(row["title"] if "title" in row.keys() else "")
    domain = str(row["domain"] if "domain" in row.keys() else "").strip().lower()
    task_type = str(row["task_type"] if "task_type" in row.keys() else "").strip().lower()
    if title:
        return f"{title}|{domain}|{task_type}"
    event_id = str(row["id"] if "id" in row.keys() else "")
    return f"{domain}|{task_type}|{event_id}"


def _dedupe_scored_for_mmr_lite(
    scored: list[tuple[float, EventSearchHit, sqlite3.Row]],
    *,
    candidate_pool: int,
) -> list[tuple[float, EventSearchHit, sqlite3.Row]]:
    pool_limit = max(1, int(candidate_pool))
    best_by_key: dict[str, tuple[float, int, EventSearchHit, sqlite3.Row]] = {}
    for index, (score, hit, row) in enumerate(scored[:pool_limit]):
        key = _mmr_dedupe_key_lite(row)
        current = best_by_key.get(key)
        if current is None or float(score) > float(current[0]):
            best_by_key[key] = (float(score), index, hit, row)
    deduped = list(best_by_key.values())
    deduped.sort(key=lambda item: (-float(item[0]), int(item[1])))
    return [(float(score), hit, row) for score, _, hit, row in deduped[:pool_limit]]


def _should_skip_query_expansion_lite(
    *,
    initial_candidate_count: int,
    requested_k: int,
    dup_ratio: float,
    candidate_multiplier: int,
    dup_ratio_threshold: float,
) -> bool:
    min_candidate_gate = max(1, int(requested_k)) * max(1, int(candidate_multiplier))
    if int(initial_candidate_count) >= min_candidate_gate:
        return True
    return float(dup_ratio) >= float(dup_ratio_threshold)


def write_audit(
    conn: sqlite3.Connection,
    consumer: str,
    action: str,
    query: dict[str, Any],
    result_event_ids: Sequence[UUID | str],
    policy_decisions: dict[str, Any],
    latency_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log(id, ts, consumer, action, query, result_event_ids, policy_decisions, latency_ms)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            now_utc().isoformat(),
            consumer,
            action,
            json_dumps(query),
            json_dumps([str(v) for v in result_event_ids]),
            json_dumps(policy_decisions),
            latency_ms,
        ),
    )
    conn.commit()


def runtime_mode(conn: sqlite3.Connection, settings: Settings) -> RuntimeModeConfig:
    row = conn.execute(
        "SELECT value, updated_at FROM runtime_settings WHERE key = ?",
        ("runtime_mode",),
    ).fetchone()
    if not row:
        now = now_utc().isoformat()
        payload = {
            "mode": settings.default_operation_mode,
            "clone_enabled": settings.default_operation_mode == OperationMode.CLONE_ADVISOR.value,
            "updated_by": "system",
        }
        conn.execute(
            "INSERT INTO runtime_settings(key, value, updated_at) VALUES(?, ?, ?)",
            ("runtime_mode", json_dumps(payload), now),
        )
        conn.commit()
        return RuntimeModeConfig(
            mode=OperationMode(str(payload["mode"])),
            clone_enabled=bool(payload["clone_enabled"]),
            updated_at=datetime.fromisoformat(now),
            updated_by="system",
        )
    value = json_loads(row["value"], {})
    mode = OperationMode(str(value.get("mode", settings.default_operation_mode)))
    return RuntimeModeConfig(
        mode=mode,
        clone_enabled=bool(value.get("clone_enabled", mode == OperationMode.CLONE_ADVISOR)),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        updated_by=str(value.get("updated_by", "system")),
    )


def set_runtime_mode(
    conn: sqlite3.Connection, mode: OperationMode, updated_by: str
) -> RuntimeModeConfig:
    payload = {
        "mode": mode.value,
        "clone_enabled": mode == OperationMode.CLONE_ADVISOR,
        "updated_by": updated_by,
    }
    now = now_utc().isoformat()
    conn.execute(
        """
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("runtime_mode", json_dumps(payload), now),
    )
    conn.commit()
    return RuntimeModeConfig(
        mode=mode,
        clone_enabled=mode == OperationMode.CLONE_ADVISOR,
        updated_at=datetime.fromisoformat(now),
        updated_by=updated_by,
    )


def event_hash(event: EventEnvelope, payload: dict[str, Any]) -> str:
    digest_input = {
        "schema_version": event.schema_version,
        "ts": event.ts.isoformat(),
        "actor": event.actor,
        "source": event.source,
        "domain": event.domain,
        "task_type": event.task_type,
        "event_type": event.event_type.value,
        "title": event.title,
        "payload": payload,
    }
    return hashlib.sha256(json_dumps(digest_input).encode("utf-8")).hexdigest()


def _authority_level(value: str | None) -> str:
    normalized = str(value or "incidental").strip().lower()
    if normalized in {"policy", "standard", "preferred", "observed", "incidental"}:
        return normalized
    return "incidental"


def _authority_score(level: str | None) -> float:
    normalized = _authority_level(level)
    return {
        "policy": 1.0,
        "standard": 0.9,
        "preferred": 0.75,
        "observed": 0.5,
        "incidental": 0.2,
    }[normalized]


def _ingest_source_allowed(settings: Settings, source: str) -> bool:
    allowlist = settings.ingest_source_allowlist_set
    if not allowlist:
        return True
    normalized = source.strip().lower()
    if normalized in allowlist:
        return True
    return normalized.startswith("api-") or normalized.startswith("tce-")


def _pattern_status(confidence: float) -> str:
    if confidence >= 0.8:
        return "active"
    if confidence >= 0.5:
        return "needs_review"
    return "suppressed"


def _feedback_score(conn: sqlite3.Connection, pattern_id: str) -> float:
    row = conn.execute(
        """
        SELECT AVG(CASE WHEN approved = 1 THEN 1.0 ELSE -1.0 END) AS score
        FROM pattern_feedback
        WHERE pattern_id = ?
        """,
        (pattern_id,),
    ).fetchone()
    if not row or row["score"] is None:
        return 0.5
    return max(0.0, min(1.0, (float(row["score"]) + 1.0) / 2.0))


def _update_patterns_from_event(
    conn: sqlite3.Connection, event_id: str, event: EventEnvelope, settings: Settings
) -> None:
    statement = f"In {event.domain}, you commonly run {event.task_type} tasks via {event.source}."
    pattern_type = "workflow"

    existing = conn.execute(
        """
        SELECT id, confidence
        FROM patterns
        WHERE domain = ? AND pattern_type = ? AND statement = ?
        """,
        (event.domain, pattern_type, statement),
    ).fetchone()

    total = conn.execute(
        "SELECT COUNT(1) AS c FROM events WHERE domain = ? AND task_type = ?",
        (event.domain, event.task_type),
    ).fetchone()["c"]
    cutoff = (now_utc() - timedelta(days=30)).isoformat()
    recent = conn.execute(
        "SELECT COUNT(1) AS c FROM events WHERE domain = ? AND task_type = ? AND ts >= ?",
        (event.domain, event.task_type, cutoff),
    ).fetchone()["c"]
    source_count = conn.execute(
        """
        SELECT COUNT(1) AS c
        FROM events
        WHERE domain = ? AND task_type = ? AND source = ?
        """,
        (event.domain, event.task_type, event.source),
    ).fetchone()["c"]
    evidence_rows = conn.execute(
        """
        SELECT id
        FROM events
        WHERE domain = ? AND task_type = ?
        ORDER BY ts DESC
        LIMIT 12
        """,
        (event.domain, event.task_type),
    ).fetchall()
    evidence_ids = [row["id"] for row in evidence_rows]

    frequency = min(1.0, float(total) / 25.0)
    recency = min(1.0, float(recent) / max(1.0, float(total)))
    consistency = min(1.0, float(source_count) / max(1.0, float(total)))
    feedback = _feedback_score(conn, existing["id"]) if existing else 0.5
    confidence = (0.35 * frequency) + (0.25 * recency) + (0.20 * consistency) + (0.20 * feedback)
    status = _pattern_status(confidence)
    if total < settings.pattern_min_frequency or confidence < settings.pattern_min_confidence:
        status = "suppressed"

    now = now_utc().isoformat()
    pattern_id = existing["id"] if existing else str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO patterns(id, domain, pattern_type, statement, evidence_event_ids, confidence, status, updated_at, version)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain, pattern_type, statement)
        DO UPDATE SET
            evidence_event_ids = excluded.evidence_event_ids,
            confidence = excluded.confidence,
            status = excluded.status,
            updated_at = excluded.updated_at,
            version = patterns.version + 1
        """,
        (
            pattern_id,
            event.domain,
            pattern_type,
            statement,
            json_dumps(evidence_ids),
            confidence,
            status,
            now,
            1,
        ),
    )
    conn.commit()


def _upsert_semantic_pattern_lite(
    conn: sqlite3.Connection,
    *,
    domain: str,
    pattern_type: str,
    statement: str,
    evidence_event_ids: list[str],
    confidence: float,
) -> None:
    now = now_utc().isoformat()
    existing = conn.execute(
        """
        SELECT id, confidence, version, evidence_event_ids
        FROM patterns
        WHERE domain = ? AND pattern_type = ? AND statement = ?
        LIMIT 1
        """,
        (domain, pattern_type, statement),
    ).fetchone()
    if existing:
        old_conf = float(existing["confidence"] or 0.0)
        merged_conf = max(0.0, min(1.0, (0.70 * old_conf) + (0.30 * confidence)))
        existing_ids = [str(v) for v in json_loads(existing["evidence_event_ids"], [])]
        merged_ids = list(dict.fromkeys([*existing_ids, *evidence_event_ids]))[:200]
        conn.execute(
            """
            UPDATE patterns
            SET confidence = ?, status = ?, evidence_event_ids = ?, updated_at = ?, version = ?
            WHERE id = ?
            """,
            (
                merged_conf,
                _pattern_status(merged_conf),
                json_dumps(merged_ids),
                now,
                int(existing["version"] or 1) + 1,
                existing["id"],
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO patterns(id, domain, pattern_type, statement, evidence_event_ids, confidence, status, updated_at, version)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            str(uuid.uuid4()),
            domain,
            pattern_type,
            statement,
            json_dumps(evidence_event_ids[:200]),
            max(0.0, min(1.0, confidence)),
            _pattern_status(confidence),
            now,
        ),
    )


def _run_semantic_consolidation_lite(
    conn: sqlite3.Connection,
    *,
    event: EventEnvelope,
    settings: Settings,
    workspace_id: str,
    user_id: str,
) -> None:
    if not bool(getattr(settings, "semantic_consolidation_enabled", True)):
        return
    if event.event_type.value not in {"TASK_DONE", "TASK_DECISION", "TASK_STEP", "FIX", "ERROR"}:
        return
    lookback_days = max(1, int(getattr(settings, "semantic_consolidation_lookback_days", 21)))
    cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
    stats = conn.execute(
        """
        SELECT COUNT(1) AS support_count,
               SUM(CASE WHEN lower(COALESCE(json_extract(outcome, '$.success'), 'false')) IN ('true','1') THEN 1 ELSE 0 END) AS success_count
        FROM events
        WHERE domain = ?
          AND task_type = ?
          AND ts >= ?
          AND json_extract(context, '$._tce_workspace') = ?
          AND json_extract(context, '$._tce_owner') = ?
        """,
        (event.domain, event.task_type, cutoff, workspace_id, user_id),
    ).fetchone()
    support_count = int(stats["support_count"] or 0) if stats else 0
    if support_count < 3:
        return
    success_count = int(stats["success_count"] or 0) if stats else 0
    success_ratio = float(success_count) / float(max(1, support_count))
    evidence_rows = conn.execute(
        """
        SELECT id
        FROM events
        WHERE domain = ?
          AND task_type = ?
          AND ts >= ?
          AND json_extract(context, '$._tce_workspace') = ?
          AND json_extract(context, '$._tce_owner') = ?
        ORDER BY ts DESC
        LIMIT 80
        """,
        (event.domain, event.task_type, cutoff, workspace_id, user_id),
    ).fetchall()
    evidence_ids = [str(row["id"]) for row in evidence_rows]
    if not evidence_ids:
        return
    base_fact = max(0.0, min(1.0, 0.35 + min(0.45, support_count / 20.0) + (0.20 * success_ratio)))
    _upsert_semantic_pattern_lite(
        conn,
        domain=event.domain,
        pattern_type="semantic_fact",
        statement=f"Semantic memory: in {event.domain}/{event.task_type}, iterative evidence-backed execution is reliable.",
        evidence_event_ids=evidence_ids,
        confidence=base_fact,
    )
    _upsert_semantic_pattern_lite(
        conn,
        domain=event.domain,
        pattern_type="skill",
        statement=f"Skill: for {event.domain}/{event.task_type}, diagnose -> narrow scope -> apply minimal change -> verify.",
        evidence_event_ids=evidence_ids,
        confidence=max(0.0, min(1.0, 0.30 + min(0.40, support_count / 25.0) + (0.25 * success_ratio))),
    )
    _upsert_semantic_pattern_lite(
        conn,
        domain=event.domain,
        pattern_type="experience",
        statement=f"Experience: {event.domain}/{event.task_type} success_ratio={success_ratio:.2f} over {support_count} events.",
        evidence_event_ids=evidence_ids,
        confidence=max(0.0, min(1.0, 0.25 + min(0.45, support_count / 30.0))),
    )
    opinion_rows = conn.execute(
        """
        SELECT scope, statement, priority
        FROM memory_rules
        WHERE workspace_id = ? AND user_id = ? AND active = 1
        ORDER BY priority ASC, updated_at DESC
        LIMIT 12
        """,
        (workspace_id, user_id),
    ).fetchall()
    for row in opinion_rows:
        scope = json_loads(row["scope"], {})
        scoped_domain = str(scope.get("domain") or event.domain).strip().lower()[:64] or event.domain
        priority = int(row["priority"] or 2)
        conf = max(0.35, min(0.95, 0.90 - (priority * 0.12)))
        _upsert_semantic_pattern_lite(
            conn,
            domain=scoped_domain,
            pattern_type="opinion",
            statement=str(row["statement"])[:500],
            evidence_event_ids=evidence_ids,
            confidence=conf,
        )


def _run_reflection_lite(
    conn: sqlite3.Connection,
    *,
    event: EventEnvelope,
    event_id: str,
    settings: Settings,
    workspace_id: str,
    user_id: str,
) -> None:
    if not bool(getattr(settings, "reflection_enabled", True)):
        return
    if event.event_type.value not in {"TASK_DONE", "TASK_DECISION"}:
        return
    context_payload = event.context if isinstance(event.context, dict) else {}
    session_id = str(context_payload.get("session_id") or "default")
    outcome_payload: dict[str, Any] = event.outcome.model_dump(mode="json") if event.outcome else {}
    success = bool(outcome_payload.get("success")) if outcome_payload else False
    goal = str(context_payload.get("objective") or event.title or event.task_type or "Untitled goal")[:240]
    now = now_utc().isoformat()
    episode = conn.execute(
        """
        SELECT id
        FROM episodes
        WHERE workspace_id = ? AND user_id = ? AND session_id = ? AND goal = ?
          AND status IN ('open','in_progress','blocked')
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (workspace_id, user_id, session_id, goal),
    ).fetchone()
    if episode:
        episode_id = str(episode["id"])
        conn.execute(
            "UPDATE episodes SET status = ?, outcome = ?, confidence = ?, updated_at = ? WHERE id = ?",
            (
                "done" if success and event.event_type.value == "TASK_DONE" else "in_progress",
                "success" if success else "failure",
                0.78 if success else 0.42,
                now,
                episode_id,
            ),
        )
    else:
        episode_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO episodes(
                id, workspace_id, user_id, session_id, goal, context, outcome, confidence,
                status, authority_score, stability_score, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                workspace_id,
                user_id,
                session_id,
                goal,
                "",
                "success" if success else "failure",
                0.78 if success else 0.42,
                "done" if success and event.event_type.value == "TASK_DONE" else "in_progress",
                _authority_score(getattr(event, "authority_level", "observed")),
                0.40,
                now,
                now,
            ),
        )
    link = conn.execute(
        "SELECT id FROM episode_event_links WHERE episode_id = ? AND event_id = ? LIMIT 1",
        (episode_id, event_id),
    ).fetchone()
    if not link:
        conn.execute(
            "INSERT INTO episode_event_links(id, episode_id, event_id, created_at) VALUES(?, ?, ?, ?)",
            (str(uuid.uuid4()), episode_id, event_id, now),
        )
    lesson_row = conn.execute(
        "SELECT do_more_json, do_less_json, avoid_json FROM episode_lessons WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    do_more = list(json_loads(lesson_row["do_more_json"], [])) if lesson_row else []
    do_less = list(json_loads(lesson_row["do_less_json"], [])) if lesson_row else []
    avoid = list(json_loads(lesson_row["avoid_json"], [])) if lesson_row else []
    if success:
        do_more.append("Keep narrow-scope iterative execution with explicit verification.")
    else:
        do_less.append("Avoid broad unverified edits before narrowing scope.")
        avoid.append("Do not continue without clarifying constraints after repeated failures.")
    do_more = list(dict.fromkeys([str(v).strip() for v in do_more if str(v).strip()]))[:20]
    do_less = list(dict.fromkeys([str(v).strip() for v in do_less if str(v).strip()]))[:20]
    avoid = list(dict.fromkeys([str(v).strip() for v in avoid if str(v).strip()]))[:20]
    if lesson_row:
        conn.execute(
            "UPDATE episode_lessons SET do_more_json = ?, do_less_json = ?, avoid_json = ?, updated_at = ? WHERE episode_id = ?",
            (json_dumps(do_more), json_dumps(do_less), json_dumps(avoid), now, episode_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO episode_lessons(episode_id, do_more_json, do_less_json, avoid_json, updated_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (episode_id, json_dumps(do_more), json_dumps(do_less), json_dumps(avoid), now),
        )
    situation_seed = str(context_payload.get("situation_type") or event.title or event.task_type or "")
    obs = {
        "consumer_id": "executor",
        "workspace_id": workspace_id,
        "situation_type": _canonical_situation_type(situation_seed),
        "situation_summary": f"reflection:{event.event_type.value.lower()}:{goal[:120]}",
        "user_response": "continue" if success else "retry_with_adjustment",
        "response_reasoning": str(outcome_payload.get("error") or outcome_payload.get("reason") or "")[:300] or None,
        "outcome": "success" if success else "failure",
        "outcome_sentiment": "positive" if success else "negative",
        "confidence": 0.72 if success else 0.62,
        "source_event_ids": [event_id],
        "context_snapshot": {
            "session_id": session_id,
            "goal": goal[:180],
            "task_type": event.task_type,
            "domain": event.domain,
        },
    }
    save_observation_lite(conn, obs)
    existing_fp = load_fingerprint_lite(conn, "executor", workspace_id) or {
        "fingerprint": json_loads(json_dumps(DEFAULT_FINGERPRINT), {}),
        "observation_count": 0,
    }
    merged_fp = merge_observation_into_fingerprint(existing_fp["fingerprint"], obs)
    save_fingerprint_lite(
        conn=conn,
        consumer_id="executor",
        workspace_id=workspace_id,
        fingerprint=merged_fp,
        observation_count=int(existing_fp.get("observation_count", 0)) + 1,
    )


def _run_memory_loops_lite(
    conn: sqlite3.Connection,
    *,
    event: EventEnvelope,
    event_id: str,
    settings: Settings,
    workspace_id: str,
    user_id: str,
) -> None:
    _run_semantic_consolidation_lite(
        conn,
        event=event,
        settings=settings,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    _run_reflection_lite(
        conn,
        event=event,
        event_id=event_id,
        settings=settings,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    conn.commit()


def _seed_episode_from_event_lite(
    conn: sqlite3.Connection,
    *,
    event: EventEnvelope,
    event_id: str,
    settings: Settings,
    workspace_id: str,
    user_id: str,
) -> None:
    if not bool(getattr(settings, "episode_extraction_enabled", True)):
        return
    context_payload = event.context if isinstance(event.context, dict) else {}
    session_id = str(context_payload.get("session_id") or "default")
    goal = str(context_payload.get("objective") or event.title or event.task_type or "Untitled goal").strip()[:240]
    if not goal:
        goal = "Untitled goal"
    now = now_utc().isoformat()
    authority_score = _authority_score(getattr(event, "authority_level", "observed"))
    episode = conn.execute(
        """
        SELECT id
        FROM episodes
        WHERE workspace_id = ? AND user_id = ? AND session_id = ? AND goal = ?
          AND status IN ('open','in_progress','blocked')
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (workspace_id, user_id, session_id, goal),
    ).fetchone()
    if episode:
        episode_id = str(episode["id"])
        conn.execute(
            """
            UPDATE episodes
            SET updated_at = ?,
                authority_score = CASE WHEN authority_score < ? THEN ? ELSE authority_score END
            WHERE id = ?
            """,
            (now, authority_score, authority_score, episode_id),
        )
    else:
        episode_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO episodes(
                id, workspace_id, user_id, session_id, goal, context, outcome, confidence,
                status, authority_score, stability_score, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, '', '', ?, 'open', ?, ?, ?, ?)
            """,
            (
                episode_id,
                workspace_id,
                user_id,
                session_id,
                goal,
                max(0.0, min(1.0, 0.5 + (authority_score * 0.3))),
                authority_score,
                0.2,
                now,
                now,
            ),
        )
    link = conn.execute(
        "SELECT id FROM episode_event_links WHERE episode_id = ? AND event_id = ? LIMIT 1",
        (episode_id, event_id),
    ).fetchone()
    if not link:
        conn.execute(
            "INSERT INTO episode_event_links(id, episode_id, event_id, created_at) VALUES(?, ?, ?, ?)",
            (str(uuid.uuid4()), episode_id, event_id, now),
        )


def store_event(
    conn: sqlite3.Connection, event: EventEnvelope, settings: Settings, auth: AuthContext
) -> UUID:
    if not _ingest_source_allowed(settings, event.source):
        raise HTTPException(status_code=403, detail=f"event source '{event.source}' is not allowed for ingest")

    idempotency_key = str(getattr(event, "idempotency_key", "") or "").strip() or None
    source_id = str(getattr(event, "source_id", "") or "").strip() or None
    source_seq_raw = getattr(event, "source_seq", None)
    source_seq = int(source_seq_raw) if source_seq_raw is not None else None
    vector_clock = getattr(event, "vector_clock", {})
    if not isinstance(vector_clock, dict):
        vector_clock = {}
    authority_level = _authority_level(getattr(event, "authority_level", None))

    if idempotency_key:
        existing = conn.execute(
            """
            SELECT id
            FROM events
            WHERE idempotency_key = ?
              AND json_extract(context, '$._tce_workspace') = ?
              AND json_extract(context, '$._tce_owner') = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (idempotency_key, auth.workspace_id, auth.user_id),
        ).fetchone()
        if existing:
            return UUID(str(existing["id"]))

    if source_id and source_seq is not None:
        existing = conn.execute(
            """
            SELECT id
            FROM events
            WHERE source_id = ?
              AND source_seq = ?
              AND json_extract(context, '$._tce_workspace') = ?
              AND json_extract(context, '$._tce_owner') = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (source_id, source_seq, auth.workspace_id, auth.user_id),
        ).fetchone()
        if existing:
            return UUID(str(existing["id"]))

    zones = [zone.strip() for zone in settings.redaction_zone_paths.split(",") if zone.strip()]
    payload_input = event.payload if isinstance(event.payload, dict) else {}
    if event.task_type == "editor_checkpoint" and event.event_type == EventType.TASK_STEP:
        payload_input = {
            "active_file": payload_input.get("active_file"),
            "line": payload_input.get("line"),
            "symbol": payload_input.get("symbol"),
            "selection_range": payload_input.get("selection_range"),
            "workspace_path": payload_input.get("workspace_path"),
            "ts": payload_input.get("ts") or event.ts.isoformat(),
        }
    payload, _ = apply_redaction_zones(payload_input, event.context, zones)
    payload, _ = redact_payload(payload, hints=event.redaction_hints)
    if isinstance(payload, dict):
        payload.setdefault("workspace_id", auth.workspace_id)
        payload.setdefault("user_id", auth.user_id)
    context, _ = redact_payload(event.context, hints=event.redaction_hints)
    if isinstance(context, dict):
        context["_tce_workspace"] = auth.workspace_id
        context["_tce_owner"] = auth.user_id
    inputs, _ = redact_payload(event.inputs, hints=event.redaction_hints)
    decision = redact_payload(event.decision.model_dump(), hints=event.redaction_hints)[0] if event.decision else None
    outcome = redact_payload(event.outcome.model_dump(), hints=event.redaction_hints)[0] if event.outcome else None
    style = redact_payload(event.style.model_dump(), hints=event.redaction_hints)[0] if event.style else None
    links = redact_payload(event.links.model_dump(), hints=event.redaction_hints)[0] if event.links else None
    summary_l0, summary_l1 = summarize_event_record(
        title=event.title,
        task_type=event.task_type,
        domain=event.domain,
        payload=payload,
        decision=decision,
        outcome=outcome,
    )

    event_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO events(
            id, ts, actor, source, domain, task_type, event_type, title,
            summary_l0, summary_l1_json, summary_version, summary_updated_at,
            payload, context, inputs, steps, decision, outcome, style, links,
            tags, sensitivity, redaction_hints, hash, schema_version,
            source_id, source_seq, vector_clock, idempotency_key, authority_level
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(event_id),
            event.ts.astimezone(UTC).isoformat(),
            event.actor,
            event.source,
            event.domain,
            event.task_type,
            event.event_type.value,
            event.title,
            summary_l0,
            json_dumps(summary_l1),
            SUMMARY_VERSION,
            event.ts.astimezone(UTC).isoformat(),
            json_dumps(payload),
            json_dumps(context),
            json_dumps(inputs),
            json_dumps([step.model_dump() for step in event.steps]),
            json_dumps(decision) if decision else None,
            json_dumps(outcome) if outcome else None,
            json_dumps(style) if style else None,
            json_dumps(links) if links else None,
            json_dumps(event.tags),
            event.sensitivity,
            json_dumps(event.redaction_hints),
            event_hash(event, payload),
            event.schema_version,
            source_id,
            source_seq,
            json_dumps(vector_clock),
            idempotency_key,
            authority_level,
        ),
    )
    conn.commit()
    _ensure_owner_membership(conn, auth.workspace_id, auth.user_id)
    _index_graph(
        conn=conn,
        event_id=str(event_id),
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        domain=event.domain,
        task_type=event.task_type,
        title=event.title,
        tags=event.tags,
        context=context,
        payload=payload,
    )
    _update_patterns_from_event(conn, str(event_id), event, settings)
    _seed_episode_from_event_lite(
        conn,
        event=event,
        event_id=str(event_id),
        settings=settings,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    _run_memory_loops_lite(
        conn,
        event=event,
        event_id=str(event_id),
        settings=settings,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    return event_id


def event_row_to_envelope(row: sqlite3.Row) -> EventEnvelope:
    event_type = EventType(row["event_type"])
    return EventEnvelope.model_validate(
        {
            "schema_version": row["schema_version"],
            "ts": datetime.fromisoformat(row["ts"]),
            "actor": row["actor"],
            "source": row["source"],
            "domain": row["domain"],
            "task_type": row["task_type"],
            "event_type": event_type,
            "title": row["title"],
            "payload": json_loads(row["payload"], {}),
            "context": json_loads(row["context"], {}),
            "inputs": json_loads(row["inputs"], {}),
            "steps": _sanitize_steps_payload(json_loads(row["steps"], [])),
            "decision": _sanitize_decision_payload(json_loads(row["decision"], None)),
            "outcome": _sanitize_outcome_payload(json_loads(row["outcome"], None)),
            "style": _sanitize_style_payload(json_loads(row["style"], None)),
            "links": _sanitize_links_payload(json_loads(row["links"], None)),
            "tags": json_loads(row["tags"], []),
            "sensitivity": row["sensitivity"],
            "redaction_hints": json_loads(row["redaction_hints"], []),
            "source_id": row["source_id"] if "source_id" in row.keys() else None,
            "source_seq": row["source_seq"] if "source_seq" in row.keys() else None,
            "vector_clock": json_loads(row["vector_clock"], {}) if "vector_clock" in row.keys() else {},
            "idempotency_key": row["idempotency_key"] if "idempotency_key" in row.keys() else None,
            "authority_level": row["authority_level"] if "authority_level" in row.keys() else "incidental",
        }
    )


def get_event(
    conn: sqlite3.Connection,
    event_id: UUID,
    settings: Settings,
    workspace_id: str,
    owner_id: str,
) -> EventEnvelope | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (str(event_id),)).fetchone()
    if not row:
        return None
    if int(row["sensitivity"]) > max_read_sensitivity(settings):
        raise HTTPException(status_code=403, detail="blocked by sensitivity policy")
    context = json_loads(row["context"], {})
    if isinstance(context, dict) and not _scope_match(context, workspace_id, owner_id):
        raise HTTPException(status_code=403, detail="event not in workspace/user scope")
    return event_row_to_envelope(row)


def search_events(
    conn: sqlite3.Connection,
    body: EventSearchRequest,
    settings: Settings,
    workspace_id: str,
    owner_id: str,
) -> tuple[EventSearchResponse, int, dict[str, Any]]:
    retrieval_started = time.perf_counter()
    query_text = body.query.strip().lower()
    match_all = bool(body.match_all) or query_text in {"", "*"}
    intent_retrieval_enabled = bool(getattr(settings, "intent_retrieval_enabled", False))
    planned_queries = plan_retrieval_subqueries(
        query_text,
        enabled=intent_retrieval_enabled,
        match_all=match_all,
    )
    planner_used = bool(len(planned_queries) > 1)
    subquery_labels = [str(item.get("label") or "") for item in planned_queries if str(item.get("label") or "").strip()]
    owner_scope, cross_user_scope_applied, cross_user_scope_owners = _resolve_owner_scope_lite(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        query_text=query_text,
    )
    query_expansion_used = False
    query_expansion_terms: list[str] = []
    rerank_strategy = "none"
    feedback_adjustment_applied = False
    expansion_ms = 0
    mmr_ms = 0
    mmr_candidates = 0
    activation_boost_applied = False
    episode_boost_applied = False
    subquery_labels_by_event: dict[str, set[str]] = {}

    max_allowed = max_read_sensitivity(settings)
    filters = body.filters

    clauses = ["sensitivity <= ?"]
    params: list[Any] = [max_allowed]

    if filters.domain:
        clauses.append("domain = ?")
        params.append(filters.domain)
    if filters.task_type:
        clauses.append("task_type = ?")
        params.append(filters.task_type)
    if filters.actor:
        clauses.append("actor = ?")
        params.append(filters.actor)
    if filters.source:
        clauses.append("source = ?")
        params.append(filters.source)
    if filters.min_sensitivity is not None:
        clauses.append("sensitivity >= ?")
        params.append(filters.min_sensitivity)
    if filters.max_sensitivity is not None:
        clauses.append("sensitivity <= ?")
        params.append(min(filters.max_sensitivity, max_allowed))
    if body.time_start:
        clauses.append("ts >= ?")
        params.append(body.time_start.astimezone(UTC).isoformat())
    if body.time_end:
        clauses.append("ts <= ?")
        params.append(body.time_end.astimezone(UTC).isoformat())

    candidate_pool_multiplier = max(
        1, int(getattr(settings, "search_candidate_pool_multiplier", 8))
    )
    candidate_pool_max = max(
        body.k, int(getattr(settings, "search_candidate_pool_max", 400))
    )
    limit = max(min(body.k * candidate_pool_multiplier, candidate_pool_max), body.k)
    legacy_limit = max(body.k * 4, body.k)
    lexical_channel = "none"
    fts_candidate_count = 0
    base_clauses = list(clauses)
    base_params = list(params)

    def _query_rows(
        search_patterns: list[str] | None = None,
        *,
        expansion_mode: bool = False,
    ) -> list[sqlite3.Row]:
        local_clauses = list(base_clauses)
        local_params = list(base_params)
        if search_patterns:
            term_conditions = []
            for _ in search_patterns:
                if expansion_mode:
                    term_conditions.append("(LOWER(title) LIKE ? OR LOWER(domain) LIKE ? OR LOWER(task_type) LIKE ?)")
                else:
                    term_conditions.append("(LOWER(title) LIKE ? OR LOWER(payload) LIKE ? OR LOWER(tags) LIKE ?)")
            local_clauses.append("(" + " OR ".join(term_conditions) + ")")
            for pattern in search_patterns:
                local_params.extend([pattern, pattern, pattern])
        local_params.append(legacy_limit)
        def _query() -> list[sqlite3.Row]:
            return conn.execute(
                f"""
                SELECT id, ts, title, summary_l0, summary_l1_json, domain, task_type, sensitivity, payload, tags, context, authority_level
                FROM events
                WHERE {' AND '.join(local_clauses)}
                ORDER BY ts DESC
                LIMIT ?
                """,
                local_params,
            ).fetchall()

        return _run_sql_retry(_query, settings=settings)

    def _query_fts_rows(fts_query: str) -> list[sqlite3.Row]:
        local_params = [fts_query, *base_params, limit]

        def _query() -> list[sqlite3.Row]:
            return conn.execute(
                f"""
                SELECT e.id, e.ts, e.title, e.summary_l0, e.summary_l1_json,
                       e.domain, e.task_type, e.sensitivity, e.payload, e.tags,
                       e.context, e.authority_level
                FROM events_fts
                JOIN events e ON e.id = events_fts.event_id
                WHERE events_fts MATCH ?
                  AND {' AND '.join(f'e.{clause}' for clause in base_clauses)}
                ORDER BY events_fts.rank, e.ts DESC
                LIMIT ?
                """,
                local_params,
            ).fetchall()

        return _run_sql_retry(_query, settings=settings)

    search_patterns: list[str] = []
    expansion_enabled = bool(getattr(settings, "search_query_expansion_enabled", False))
    enhancement_budget_ms = max(1, int(getattr(settings, "search_enhancement_budget_ms", 120)))
    expansion_timeout_ms = max(1, int(getattr(settings, "search_query_expansion_timeout_ms", 25)))
    expansion_max_terms = max(1, int(getattr(settings, "search_query_expansion_max_terms", 4)))
    expansion_skip_multiplier = max(
        1, int(getattr(settings, "search_query_expansion_skip_candidate_multiplier", 4))
    )
    expansion_skip_dup_ratio = max(
        0.0, min(1.0, float(getattr(settings, "search_query_expansion_skip_dup_ratio", 0.80)))
    )

    def _enhancement_budget_exhausted() -> bool:
        elapsed_ms = int((time.perf_counter() - retrieval_started) * 1000)
        return elapsed_ms >= enhancement_budget_ms

    trigger_met = False
    if not match_all:
        search_patterns = [f"%{query_text}%"]
        mmr_enabled = bool(getattr(settings, "search_mmr_enabled", False))
        if expansion_enabled or mmr_enabled:
            trigger_met = _search_scale_trigger_met_lite(
                conn,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )

    if match_all:
        rows = _query_rows(None, expansion_mode=False)
    else:
        fts_query = _build_fts5_query(query_text)
        rows = []
        if fts_query:
            try:
                rows = _query_fts_rows(fts_query)
                lexical_channel = "fts5_primary"
            except sqlite3.OperationalError:
                rows = []
                lexical_channel = "like_fallback_error"
        fts_candidate_count = len(rows)
        if len(rows) < body.k:
            fallback_rows = _query_rows(search_patterns, expansion_mode=False)
            merged = {str(row["id"]): row for row in rows}
            for row in fallback_rows:
                merged.setdefault(str(row["id"]), row)
            rows = list(merged.values())
            if fts_candidate_count:
                lexical_channel = "fts5_plus_like_fill"
            elif lexical_channel != "like_fallback_error":
                lexical_channel = "like_only"
    preview_dup_ratio = _citation_dup_ratio_lite(rows)
    initial_candidate_count = len(rows)
    expansion_triggered = bool(trigger_met or preview_dup_ratio > 0.35)
    expansion_skip = _should_skip_query_expansion_lite(
        initial_candidate_count=initial_candidate_count,
        requested_k=body.k,
        dup_ratio=preview_dup_ratio,
        candidate_multiplier=expansion_skip_multiplier,
        dup_ratio_threshold=expansion_skip_dup_ratio,
    )
    if (
        expansion_enabled
        and (not match_all)
        and expansion_triggered
        and not expansion_skip
        and not _enhancement_budget_exhausted()
    ):
        started_expand = time.perf_counter()
        expanded_terms = _expand_query_terms_lite(query_text, max_terms=expansion_max_terms)
        elapsed_ms = int((time.perf_counter() - started_expand) * 1000)
        expansion_ms += elapsed_ms
        if (
            elapsed_ms <= expansion_timeout_ms
            and expanded_terms
            and not _enhancement_budget_exhausted()
        ):
            search_patterns = [f"%{query_text}%", *[f"%{token}%" for token in expanded_terms]]
            rerun_started = time.perf_counter()
            rows = _query_rows(search_patterns, expansion_mode=True)
            lexical_channel = "like_expansion"
            expansion_ms += int((time.perf_counter() - rerun_started) * 1000)
            query_expansion_used = True
            query_expansion_terms = expanded_terms

    if not match_all:
        merged_rows: dict[str, sqlite3.Row] = {}

        def _merge_rows(batch: list[sqlite3.Row], label: str) -> None:
            for row in batch:
                key = str(row["id"])
                if key not in merged_rows:
                    merged_rows[key] = row
                subquery_labels_by_event.setdefault(key, set()).add(label)

        _merge_rows(rows, planned_queries[0]["label"] if planned_queries else "objective")
        if planner_used and not _enhancement_budget_exhausted():
            for index, planned in enumerate(planned_queries):
                remaining_budget_ms = enhancement_budget_ms - int((time.perf_counter() - retrieval_started) * 1000)
                if remaining_budget_ms < 40:
                    break
                planned_query = str(planned.get("query") or "").strip().lower()
                if not planned_query:
                    continue
                if index == 0 and planned_query == query_text:
                    continue
                extra_rows = _query_rows([f"%{planned_query}%"], expansion_mode=False)
                _merge_rows(extra_rows, str(planned.get("label") or "objective"))
        rows = list(merged_rows.values())

    citation_dup_ratio = _citation_dup_ratio_lite(rows)

    entity_hit_ids: set[str] = set()
    if not match_all:
        if bool(getattr(settings, "memory_multi_hop_enabled", True)):
            def _entity_multi_hop() -> list[sqlite3.Row]:
                return conn.execute(
                    """
                    WITH matched_entities AS (
                        SELECT en.id
                        FROM entity_nodes en
                        WHERE en.workspace_id = ?
                          AND en.owner_id = ?
                          AND (
                            en.entity_key LIKE ?
                            OR en.display_name LIKE ?
                          )
                        LIMIT 160
                    ),
                    seed_events AS (
                        SELECT DISTINCT eel.event_id
                        FROM event_entity_links eel
                        JOIN matched_entities me ON me.id = eel.entity_id
                        LIMIT 220
                    ),
                    neighbor_entities AS (
                        SELECT DISTINCT eel.entity_id
                        FROM event_entity_links eel
                        JOIN seed_events se ON se.event_id = eel.event_id
                        LIMIT 220
                    ),
                    expanded_events AS (
                        SELECT DISTINCT eel.event_id
                        FROM event_entity_links eel
                        JOIN neighbor_entities ne ON ne.entity_id = eel.entity_id
                        LIMIT ?
                    )
                    SELECT event_id FROM expanded_events
                    """,
                    (
                        workspace_id,
                        owner_id,
                        f"%{query_text}%",
                        f"%{query_text}%",
                        max(100, int(getattr(settings, "memory_multi_hop_limit", 600))),
                    ),
                ).fetchall()

            entity_hit_rows = _run_sql_retry(_entity_multi_hop, settings=settings)
        else:
            def _entity_direct() -> list[sqlite3.Row]:
                return conn.execute(
                    """
                    SELECT DISTINCT eel.event_id
                    FROM entity_nodes en
                    JOIN event_entity_links eel ON eel.entity_id = en.id
                    WHERE en.workspace_id = ?
                      AND en.owner_id = ?
                      AND (
                        en.entity_key LIKE ?
                        OR en.display_name LIKE ?
                      )
                    LIMIT 200
                    """,
                    (workspace_id, owner_id, f"%{query_text}%", f"%{query_text}%"),
                ).fetchall()

            entity_hit_rows = _run_sql_retry(_entity_direct, settings=settings)
        entity_hit_ids = {row["event_id"] for row in entity_hit_rows}

    tokens = [token for token in query_text.split() if token and token != "*"]
    recency_lambda = max(0.0, float(settings.search_recency_lambda)) if settings.search_enable_recency else 0.0
    weight_relevance = max(0.0, float(settings.search_weight_relevance))
    weight_stability = max(0.0, float(settings.search_weight_stability))
    weight_authority = max(0.0, float(settings.search_weight_authority))
    weight_recency = max(0.0, float(settings.search_weight_recency))
    weight_total = weight_relevance + weight_stability + weight_authority + weight_recency
    if weight_total <= 0.0:
        weight_relevance, weight_stability, weight_authority, weight_recency = (0.45, 0.25, 0.20, 0.10)
        weight_total = 1.0
    weight_relevance /= weight_total
    weight_stability /= weight_total
    weight_authority /= weight_total
    weight_recency /= weight_total
    graph_bonus = max(0.0, float(settings.search_graph_bonus))
    activation_weight = max(0.0, float(getattr(settings, "memory_activation_weight", 0.0)))
    if planner_used and intent_retrieval_enabled:
        multiplier = max(1.0, float(getattr(settings, "memory_activation_autonomy_weight_multiplier", 1.0)))
        boosted_weight = activation_weight * multiplier
        activation_boost_applied = boosted_weight > activation_weight
        activation_weight = boosted_weight
    recurrence = Counter((str(row["domain"]), str(row["task_type"])) for row in rows)
    activation_lookup = _read_activation_scores_lite(
        workspace_id=workspace_id,
        owner_id=owner_id,
        event_ids=[row["id"] for row in rows],
        settings=settings,
    )
    feedback_weight = max(0.0, float(getattr(settings, "search_feedback_weight", 0.08)))
    feedback_lookup = _read_feedback_signals_lite(
        conn,
        workspace_id=workspace_id,
        event_ids=[str(row["id"]) for row in rows],
        settings=settings,
    )
    handoff_map = _load_handoff_records_map_lite(
        conn,
        workspace_id=workspace_id,
        owner_ids=sorted(owner_scope),
        max_records=max(body.k * 6, 30),
    )
    handoff_query_intent = handoff_intent(query_text)
    episode_score_lookup = _load_episode_score_lookup_lite(
        conn,
        workspace_id=workspace_id,
        owner_ids=sorted(owner_scope),
        query_text=query_text,
        max_rows=max(body.k * 12, 120),
    ) if (planner_used and intent_retrieval_enabled) else {}
    normalized_workspace_id = _normalize_owner_token_lite(workspace_id)

    def _score_rows_for_scope(
        active_owner_scope: set[str],
    ) -> tuple[list[tuple[float, EventSearchHit, sqlite3.Row]], int, int, bool]:
        local_scored: list[tuple[float, EventSearchHit, sqlite3.Row]] = []
        local_blocked = 0
        local_owner_scope_blocked = 0
        local_feedback_applied = False
        for row in rows:
            context = json_loads(row["context"], {})
            if isinstance(context, dict):
                event_workspace = _normalize_owner_token_lite(context.get("_tce_workspace"))
                if event_workspace and event_workspace != normalized_workspace_id:
                    local_blocked += 1
                    continue
                event_owner = _normalize_owner_token_lite(context.get("_tce_owner"))
                if event_owner and active_owner_scope and event_owner not in active_owner_scope:
                    local_blocked += 1
                    local_owner_scope_blocked += 1
                    continue
            blob = f"{row['title']} {row['payload']} {row['tags']}".lower()
            lexical = 0.0
            if tokens:
                lexical = sum(1 for token in tokens if token in blob) / float(len(tokens))
            relevance = lexical
            stability = 0.0
            pair_count = int(recurrence.get((str(row["domain"]), str(row["task_type"])), 0))
            if pair_count > 0:
                stability = max(0.0, min(1.0, math.log1p(float(pair_count)) / math.log(8.0)))
            authority = _authority_score(row["authority_level"] if "authority_level" in row.keys() else "incidental")
            ts_raw = row["ts"]
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    ts = now_utc()
            else:
                ts = now_utc()
            days_old = max(0.0, (now_utc() - ts).total_seconds() / 86400.0)
            recency = math.exp(-recency_lambda * days_old) if recency_lambda > 0 else 1.0
            graph_score = graph_bonus if row["id"] in entity_hit_ids else 0.0
            activation_score = max(0.0, min(1.0, float(activation_lookup.get(str(row["id"]), 0.0))))
            feedback_signal = max(-1.0, min(1.0, float(feedback_lookup.get(str(row["id"]), 0.0))))
            feedback_delta = feedback_weight * feedback_signal
            query_labels = subquery_labels_by_event.get(str(row["id"]), set())
            query_label_boost = 0.04 if "decision_history" in query_labels else 0.0
            if "constraints_workflow" in query_labels:
                query_label_boost += 0.03
            score = (
                query_label_boost
                + (weight_relevance * relevance)
                + (weight_stability * stability)
                + (weight_authority * authority)
                + (weight_recency * recency)
                + graph_score
                + (activation_weight * activation_score)
                + feedback_delta
                + _milestone_score_boost_lite(json_loads(row["payload"], {}) if "payload" in row.keys() else {})
            )
            handoff_meta = handoff_map.get(str(row["id"] or ""))
            if handoff_meta:
                score += 0.24
                if str(handoff_meta.get("schema_version") or "") == "v1":
                    score += 0.06
                if handoff_query_intent and bool(handoff_meta.get("has_anchors")):
                    score += 0.08
            episode_score = max(0.0, min(1.0, float(episode_score_lookup.get(str(row["id"]), 0.0))))
            if episode_score > 0.0:
                score = propagate_episode_score(score, episode_score)
            if abs(feedback_delta) > 1e-9:
                local_feedback_applied = True
            local_scored.append(
                (
                    score,
                    EventSearchHit(
                        id=UUID(row["id"]),
                        ts=datetime.fromisoformat(row["ts"]),
                        title=row["title"],
                        domain=row["domain"],
                        task_type=row["task_type"],
                        score=score,
                        sensitivity=row["sensitivity"],
                        summary_l0=str(row["summary_l0"] or "").strip() if "summary_l0" in row.keys() else "",
                        summary_l1=coerce_summary_l1(
                            json_loads(row["summary_l1_json"], {}) if "summary_l1_json" in row.keys() else {}
                        ),
                    ),
                    row,
                )
            )
        local_scored.sort(key=lambda item: item[0], reverse=True)
        return local_scored, local_blocked, local_owner_scope_blocked, local_feedback_applied

    scored, blocked, owner_scope_blocked, feedback_adjustment_applied = _score_rows_for_scope(owner_scope)
    owner_scope_blocked_ratio = float(owner_scope_blocked) / float(max(1, len(rows)))
    normalized_owner_id = _normalize_owner_token_lite(owner_id)
    auto_expand_on_blocked_ratio = bool(
        (normalized_owner_id.endswith("-executor") or normalized_owner_id.endswith("-executer"))
        and owner_scope_blocked_ratio >= 0.95
    )
    if (
        not cross_user_scope_applied
        and owner_scope_blocked > 0
        and (not scored or auto_expand_on_blocked_ratio)
    ):
        expanded_scope = _expand_owner_scope_from_rows_lite(
            rows,
            workspace_id=workspace_id,
            current_scope=owner_scope,
        )
        if expanded_scope != owner_scope:
            owner_scope = expanded_scope
            cross_user_scope_applied = True
            cross_user_scope_owners = sorted(owner_scope)[:6]
            handoff_map = _load_handoff_records_map_lite(
                conn,
                workspace_id=workspace_id,
                owner_ids=sorted(owner_scope),
                max_records=max(body.k * 6, 30),
            )
            scored, blocked, _, feedback_adjustment_applied = _score_rows_for_scope(owner_scope)
    if not match_all:
        rerank_strategy = "score_sort"
    mmr_candidate_pool = max(
        body.k,
        int(getattr(settings, "search_mmr_candidate_pool", 60)),
    )
    mmr_should_run = bool(
        (not match_all)
        and bool(getattr(settings, "search_mmr_enabled", False))
        and (
            citation_dup_ratio > 0.35
            or trigger_met
        )
        and len(scored) > 1
        and (int((time.perf_counter() - retrieval_started) * 1000) < enhancement_budget_ms)
    )
    if mmr_should_run:
        mmr_input = _dedupe_scored_for_mmr_lite(
            scored,
            candidate_pool=mmr_candidate_pool,
        )
        mmr_candidates = len(mmr_input)
        mmr_lambda = max(0.0, min(1.0, float(getattr(settings, "search_mmr_lambda", 0.65))))
        if mmr_candidates > 1 and int((time.perf_counter() - retrieval_started) * 1000) < enhancement_budget_ms:
            rerank_strategy = "mmr"
            mmr_started = time.perf_counter()
            selected_scored = _mmr_select_lite(mmr_input, top_k=body.k, mmr_lambda=mmr_lambda)
            mmr_ms = int((time.perf_counter() - mmr_started) * 1000)
        else:
            selected_scored = scored[: body.k]
    else:
        selected_scored = scored[: body.k]
    hits = [item[1] for item in selected_scored]
    top_handoff_record_ids: list[str] = []
    for hit in hits:
        handoff_meta = handoff_map.get(str(hit.id))
        if not handoff_meta:
            continue
        rid = str(handoff_meta.get("id") or "").strip()
        if rid and rid not in top_handoff_record_ids:
            top_handoff_record_ids.append(rid)
        if len(top_handoff_record_ids) >= 5:
            break
    _bump_activation_scores_lite(
        workspace_id=workspace_id,
        owner_id=owner_id,
        event_ids=[hit.id for hit in hits],
        settings=settings,
    )
    response = EventSearchResponse(hits=hits, citations=[hit.id for hit in hits])
    retrieval_source = "none" if match_all else "lexical_only"
    _bump_retrieval_counter(retrieval_source)
    episode_event_id_keys = {
        str(key) for key, value in episode_score_lookup.items() if float(value or 0.0) > 0.0
    }
    if episode_score_lookup:
        episode_boost_applied = any(str(hit.id) in episode_event_id_keys for hit in hits)
    retrieval_meta = {
        "source": retrieval_source,
        "reason": None,
        "latency_ms": int((time.perf_counter() - retrieval_started) * 1000),
        "candidate_count": len(rows),
        "lexical_channel": lexical_channel,
        "lexical_candidate_count": len(rows),
        "fts_candidate_count": int(fts_candidate_count),
        "trigram_candidate_count": 0,
        "rrf_applied": False,
        "candidate_pool_limit": int(limit),
        "scope_prefilter_applied": False,
        "scope_requery_applied": False,
        "score_components_version": 2,
        "hit_count": len(hits),
        "vector_used": False,
        "activation_enabled": bool(getattr(settings, "memory_activation_enabled", True)),
        "activation_hits": sum(1 for hit in hits if str(hit.id) in activation_lookup),
        "feedback_adjustment_applied": bool(feedback_adjustment_applied),
        "cross_user_scope_applied": bool(cross_user_scope_applied),
        "cross_user_scope_owners": cross_user_scope_owners,
        "handoff_hits_count": len(top_handoff_record_ids),
        "top_handoff_record_ids": top_handoff_record_ids,
        "resume_packet_available": bool(top_handoff_record_ids),
        "context_tier_used": "l1" if bool(getattr(settings, "context_tiers_enabled", False)) else "l2",
        "summary_coverage": summary_coverage_ratio(hits),
        "planner_used": bool(planner_used),
        "subquery_count": len(subquery_labels),
        "subquery_labels": subquery_labels,
        "episode_boost_applied": bool(episode_boost_applied),
        "activation_boost_applied": bool(activation_boost_applied),
        "query_expansion_used": bool(query_expansion_used),
        "query_expansion_terms": query_expansion_terms[: max(0, int(getattr(settings, "search_query_expansion_max_terms", 4)))],
        "expansion_ms": int(expansion_ms),
        "rerank_strategy": rerank_strategy,
        "mmr_ms": int(mmr_ms),
        "mmr_candidates": int(mmr_candidates),
        "citation_dup_ratio": float(round(citation_dup_ratio, 4)),
    }
    return response, blocked, retrieval_meta


def _resume_file_items_from_record_lite(record: dict[str, Any], *, query_text: str) -> list[ResumePacketFileItem]:
    files_value = record.get("files_json")
    files_raw: list[Any] = files_value if isinstance(files_value, list) else []
    anchors_value = record.get("anchors_json")
    anchors_raw: list[Any] = anchors_value if isinstance(anchors_value, list) else []
    change_value = record.get("change_summary_json")
    change_raw: dict[str, Any] = change_value if isinstance(change_value, dict) else {}
    anchors_by_file: dict[str, list[ResumePacketAnchor]] = {}
    for item in anchors_raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file") or "").strip()
        if not path:
            continue
        line = item.get("line")
        line_value = int(line) if isinstance(line, int) and line > 0 else None
        anchors_by_file.setdefault(path, []).append(
            ResumePacketAnchor(file=path, line=line_value, symbol=str(item.get("symbol") or "").strip() or None)
        )
    lowered_query = str(query_text or "").lower()
    output: list[ResumePacketFileItem] = []
    for path in [str(item).strip() for item in files_raw if str(item).strip()][:40]:
        anchors = anchors_by_file.get(path, [])
        summary_raw = change_raw.get(path) if isinstance(change_raw, dict) else {}
        if not isinstance(summary_raw, dict):
            summary_raw = {}
        priority = min(1.0, 0.55 + (0.25 if anchors else 0.0) + (0.12 if path.lower() in lowered_query else 0.0))
        output.append(
            ResumePacketFileItem(
                path=path,
                priority_score=round(priority, 3),
                anchors=anchors[:10],
                change_summary=ResumePacketChangeSummary(
                    added_lines=max(0, int(summary_raw.get("added") or summary_raw.get("added_lines") or 0)),
                    removed_lines=max(0, int(summary_raw.get("removed") or summary_raw.get("removed_lines") or 0)),
                    intent=str(summary_raw.get("intent") or "")[:160],
                ),
            )
        )
    output.sort(key=lambda item: item.priority_score, reverse=True)
    return output[:40]


def get_resume_packet(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: ResumePacketRequest,
    settings: Settings,
) -> ResumePacketResponse:
    started = time.perf_counter()
    if body.include_cross_user:
        owner_scope, cross_user_scope_applied, cross_user_scope_owners = _resolve_owner_scope_lite(
            conn,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            query_text=body.query,
        )
    else:
        owner_scope = {_normalize_owner_token_lite(auth.user_id)}
        cross_user_scope_applied = False
        cross_user_scope_owners = sorted(owner_scope)
    target_owner = _normalize_owner_token_lite(body.target_owner)
    if target_owner:
        filtered = {owner for owner in owner_scope if _owner_matches_hint_lite(owner, target_owner)}
        if filtered:
            owner_scope = filtered.union({_normalize_owner_token_lite(auth.user_id)})
            cross_user_scope_owners = sorted(owner_scope)[:6]
            cross_user_scope_applied = owner_scope != {_normalize_owner_token_lite(auth.user_id)}
    owner_ids = sorted([item for item in owner_scope if item])
    if not owner_ids:
        owner_ids = [_normalize_owner_token_lite(auth.user_id)]
    placeholders = ",".join("?" for _ in owner_ids)
    rows = conn.execute(
        f"""
        SELECT id, owner_id, session_id, ts, title, decision, next_step, status,
               files_json, anchors_json, git_json, change_summary_json, objective_text,
               source, event_id, schema_version
        FROM handoff_records
        WHERE workspace_id = ?
          AND owner_id IN ({placeholders})
          AND session_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (auth.workspace_id, *owner_ids, body.session_id, max(20, int(body.k) * 20)),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        row_map: dict[str, Any] = {
            "id": str(row["id"]),
            "owner_id": str(row["owner_id"]),
            "session_id": str(row["session_id"]),
            "ts": datetime.fromisoformat(str(row["ts"])),
            "title": str(row["title"] or ""),
            "decision": str(row["decision"] or ""),
            "next_step": str(row["next_step"] or ""),
            "status": str(row["status"] or "failed"),
            "files_json": json_loads(row["files_json"], []),
            "anchors_json": json_loads(row["anchors_json"], []),
            "git_json": json_loads(row["git_json"], {}),
            "change_summary_json": json_loads(row["change_summary_json"], {}),
            "objective_text": str(row["objective_text"] or ""),
            "source": str(row["source"] or "native"),
            "event_id": str(row["event_id"] or ""),
            "schema_version": str(row["schema_version"] or ""),
        }
        if not row_map["objective_text"]:
            objective_text, _ = normalize_objective_text(
                title=row_map.get("title"),
                decision=row_map.get("decision"),
                next_step=row_map.get("next_step"),
            )
            row_map["objective_text"] = objective_text
        candidates.append(row_map)
    selected, alternates, candidate_count = rank_resume_candidates(
        candidates,
        query_text=body.query,
        k=max(1, int(body.k)),
    )
    if not selected:
        raise HTTPException(status_code=404, detail="no matching handoff records found")
    selected_id = str(selected.get("id") or "").strip()
    selected_uuid = UUID(selected_id)
    files = _resume_file_items_from_record_lite(selected, query_text=body.query)
    packet_id = uuid.uuid4()
    response = ResumePacketResponse(
        packet_id=packet_id,
        selected_record_id=selected_uuid,
        selection_reason="task_overlap_then_recency",
        cross_user_scope_applied=bool(cross_user_scope_applied),
        cross_user_scope_owners=list(cross_user_scope_owners),
        task_summary=str(selected.get("title") or "")[:160],
        decision=str(selected.get("decision") or "")[:500],
        next_step=str(selected.get("next_step") or "")[:300],
        status=str(selected.get("status") or "failed"),
        contract_valid=str(selected.get("schema_version") or "").strip().lower() == "v1" and bool(files),
        git=dict(selected.get("git_json") or {}),
        files=files,
        continuation_steps=[
            "Open top file anchor",
            "Run scoped verification",
            "Report execution with milestone_schema=v1",
        ],
        retrieval_meta=ResumePacketRetrievalMeta(
            latency_ms=int((time.perf_counter() - started) * 1000),
            candidate_count=int(candidate_count),
            alternates=alternates,
        ),
    )
    selected_ts = selected.get("ts")
    if isinstance(selected_ts, datetime):
        returned_at = datetime.now(tz=UTC)
        record_resume_attempt(
            conn,
            packet_id=packet_id,
            workspace_id=auth.workspace_id,
            requesting_owner_id=auth.user_id,
            target_owner_id=str(selected.get("owner_id") or body.target_owner or auth.user_id),
            session_id=body.session_id,
            selected_record_id=selected_id,
            query_text=body.query,
            top_file=files[0].path if files else None,
            recommended_files=[item.path for item in files],
            requested_at=returned_at - timedelta(milliseconds=response.retrieval_meta.latency_ms),
            returned_at=returned_at,
            handoff_ts=selected_ts,
        )
    return response


def list_patterns(
    conn: sqlite3.Connection,
    domain: str | None,
    min_confidence: float,
    workspace_id: str,
    owner_id: str,
    limit: int = 100,
) -> list[PatternItem]:
    clauses = ["confidence >= ?", "status != 'suppressed'"]
    params: list[Any] = [min_confidence]
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, domain, pattern_type, statement, confidence, status, evidence_event_ids
        FROM patterns
        WHERE {' AND '.join(clauses)}
        ORDER BY confidence DESC, updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    items: list[PatternItem] = []
    for row in rows:
        raw_evidence_ids = [UUID(value) for value in json_loads(row["evidence_event_ids"], [])]
        evidence_ids: list[UUID] = []
        for evidence_id in raw_evidence_ids[:10]:
            evidence_row = conn.execute(
                "SELECT context FROM events WHERE id = ?",
                (str(evidence_id),),
            ).fetchone()
            if not evidence_row:
                continue
            evidence_context = json_loads(evidence_row["context"], {})
            if isinstance(evidence_context, dict) and _scope_match(evidence_context, workspace_id, owner_id):
                evidence_ids.append(evidence_id)
        if raw_evidence_ids and not evidence_ids:
            continue
        items.append(
            PatternItem(
                id=UUID(row["id"]),
                domain=row["domain"],
                pattern_type=row["pattern_type"],
                statement=row["statement"],
                confidence=float(row["confidence"]),
                status=row["status"],
                evidence_event_ids=evidence_ids,
            )
        )
    return items


def _workflows_for_domain(domain: str | None) -> list[dict[str, Any]]:
    if domain == "coding":
        return [
            {
                "id": "lite-debug-cycle",
                "name": "Debug cycle",
                "domain": "coding",
                "graph": {
                    "steps": ["reproduce", "inspect_logs", "hypothesis", "fix", "verify"],
                    "edges": [
                        ["reproduce", "inspect_logs"],
                        ["inspect_logs", "hypothesis"],
                        ["hypothesis", "fix"],
                        ["fix", "verify"],
                    ],
                },
                "triggers": {"task_type": ["debug", "fix_bug"]},
                "version": 1,
            }
        ]
    return []


def context_bundle(
    conn: sqlite3.Connection,
    request: ContextBundleRequest,
    settings: Settings,
    workspace_id: str,
    owner_id: str,
) -> tuple[ContextBundleResponse, int]:
    query_hash = hashlib.sha256(
        json_dumps(
            {
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "body": request.model_dump(mode="json"),
            }
        ).encode("utf-8")
    ).hexdigest()
    cached = conn.execute(
        "SELECT bundle, created_at, ttl_seconds FROM context_bundles WHERE query_hash = ?",
        (query_hash,),
    ).fetchone()
    if cached:
        expiry = datetime.fromisoformat(cached["created_at"]).timestamp() + int(cached["ttl_seconds"])
        if datetime.now().timestamp() < expiry:
            return ContextBundleResponse.model_validate(json_loads(cached["bundle"], {})), 0

    domain = request.app_context.get("domain") if isinstance(request.app_context, dict) else None
    task_type = request.app_context.get("task_type") if isinstance(request.app_context, dict) else None
    min_conf = float(request.constraints.get("min_confidence", 0.5)) if request.constraints else 0.5
    k = int(request.constraints.get("k", 12)) if request.constraints else 12

    search_response, blocked, retrieval_meta = search_events(
        conn,
        EventSearchRequest(
            query=request.task,
            filters=EventFilter(domain=domain, task_type=task_type),
            k=k,
        ),
        settings=settings,
        workspace_id=workspace_id,
        owner_id=owner_id,
    )
    evidence_events = [
        EvidenceEvent(
            id=hit.id,
            title=hit.title,
            ts=hit.ts,
            key_payload_fields={"domain": hit.domain, "task_type": hit.task_type},
            summary_l0=hit.summary_l0,
            summary_l1=hit.summary_l1,
        )
        for hit in search_response.hits
    ]
    top_patterns = list_patterns(
        conn,
        domain=domain,
        min_confidence=min_conf,
        workspace_id=workspace_id,
        owner_id=owner_id,
        limit=8,
    )
    typed_memory: dict[str, list[str]] = {
        "facts": [],
        "opinions": [],
        "experiences": [],
        "skills": [],
    }
    for pattern in top_patterns:
        text_value = str(pattern.statement or "").strip()
        if not text_value:
            continue
        ptype = str(pattern.pattern_type or "").strip().lower()
        if ptype in {"semantic_fact", "fact", "world_fact"}:
            typed_memory["facts"].append(text_value)
        elif ptype in {"opinion", "preference", "rule"}:
            typed_memory["opinions"].append(text_value)
        elif ptype in {"experience", "reflection", "summary"}:
            typed_memory["experiences"].append(text_value)
        elif ptype in {"skill", "workflow", "procedure"}:
            typed_memory["skills"].append(text_value)
    workflows = _workflows_for_domain(domain)

    do_rules = [pattern.statement for pattern in top_patterns[:5]]
    if not do_rules:
        do_rules = [
            "Capture TASK_DECISION and TASK_DONE events for faster personalization.",
            "Use timeline search immediately while patterns are still warming up.",
        ]
    dont_rules = [pattern.statement for pattern in top_patterns if pattern.pattern_type == "anti-pattern"][:5]
    if not dont_rules:
        dont_rules = ["Do not treat low-evidence suggestions as hard constraints."]

    cold_start = (
        len(evidence_events) < settings.cold_start_min_events
        or len(top_patterns) < settings.cold_start_min_patterns
    )
    context_tiers_enabled = bool(getattr(settings, "context_tiers_enabled", False))
    summary = (
        f"Bundle generated at {now_utc().isoformat()} with "
        f"{len(evidence_events)} evidence events and {len(top_patterns)} patterns."
    )
    if context_tiers_enabled and evidence_events:
        summary = " | ".join(
            [
                f"{len(evidence_events)} evidence events",
                *[
                    summarize_hit_text(event.title, event.summary_l0, event.ts)
                    for event in evidence_events[:3]
                ],
            ]
        )[:500]
    if cold_start:
        summary = (
            "Cold start mode: limited historical signal; running timeline log/search behavior with cautious guidance."
        )

    bundle = ContextBundleResponse(
        summary=summary,
        top_patterns=top_patterns,
        relevant_workflows=workflows,
        evidence_events=evidence_events,
        do_dont={"do": do_rules, "dont": dont_rules},
        citations=search_response.citations,
        policy={
            "blocked_count": blocked,
            "applied_redactions": [],
            "cold_start": cold_start,
            "evidence_count": len(evidence_events),
            "pattern_count": len(top_patterns),
            "typed_memory_counts": {key: len(value) for key, value in typed_memory.items()},
            "blocked_sensitivity": settings.block_sensitivity,
            "handoff_hits_count": int(retrieval_meta.get("handoff_hits_count", 0) or 0),
            "top_handoff_record_ids": list(retrieval_meta.get("top_handoff_record_ids") or []),
            "resume_packet_available": bool(retrieval_meta.get("resume_packet_available", False)),
            "cross_user_scope_applied": bool(retrieval_meta.get("cross_user_scope_applied", False)),
            "cross_user_scope_owners": list(retrieval_meta.get("cross_user_scope_owners") or []),
            "context_tier_used": str(retrieval_meta.get("context_tier_used") or "l2"),
            "summary_coverage": float(retrieval_meta.get("summary_coverage", 0.0) or 0.0),
            "planner_used": bool(retrieval_meta.get("planner_used", False)),
            "subquery_count": int(retrieval_meta.get("subquery_count", 0) or 0),
            "subquery_labels": list(retrieval_meta.get("subquery_labels") or []),
            "episode_boost_applied": bool(retrieval_meta.get("episode_boost_applied", False)),
            "activation_boost_applied": bool(retrieval_meta.get("activation_boost_applied", False)),
            "retrieval": retrieval_meta,
        },
        structured_context={
            "time_window": {
                "from": min((event.ts for event in evidence_events), default=None),
                "to": max((event.ts for event in evidence_events), default=None),
            },
            "graph": graph_snapshot_for_events(
                conn=conn,
                workspace_id=workspace_id,
                owner_id=owner_id,
                event_ids=search_response.citations,
            ),
            "typed_memory": typed_memory,
            "retrieval": retrieval_meta,
        },
        context_tier_used=str(retrieval_meta.get("context_tier_used") or "l2"),
        summary_coverage=float(retrieval_meta.get("summary_coverage", 0.0) or 0.0),
        planner_used=bool(retrieval_meta.get("planner_used", False)),
        subquery_count=int(retrieval_meta.get("subquery_count", 0) or 0),
        subquery_labels=list(retrieval_meta.get("subquery_labels") or []),
        episode_boost_applied=bool(retrieval_meta.get("episode_boost_applied", False)),
        activation_boost_applied=bool(retrieval_meta.get("activation_boost_applied", False)),
    )
    conn.execute(
        """
        INSERT INTO context_bundles(query_hash, bundle, created_at, ttl_seconds)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(query_hash) DO UPDATE SET
            bundle = excluded.bundle,
            created_at = excluded.created_at,
            ttl_seconds = excluded.ttl_seconds
        """,
        (
            query_hash,
            json_dumps(bundle.model_dump(mode="json")),
            now_utc().isoformat(),
            settings.context_bundle_cache_ttl_seconds,
        ),
    )
    conn.commit()
    return bundle, blocked


def _rule_scope_matches(
    scope: dict[str, Any],
    *,
    workspace_id: str,
    user_id: str,
    session_id: str | None = None,
    domain: str | None = None,
    task_type: str | None = None,
) -> bool:
    if scope.get("workspace_id") and str(scope.get("workspace_id")) != workspace_id:
        return False
    if scope.get("user_id") and str(scope.get("user_id")) != user_id:
        return False
    if session_id and scope.get("session_id") and str(scope.get("session_id")) != session_id:
        return False
    if domain and scope.get("domain") and str(scope.get("domain")) != domain:
        return False
    if task_type and scope.get("task_type") and str(scope.get("task_type")) != task_type:
        return False
    return True


def annotate_event(
    conn: sqlite3.Connection,
    *,
    event_id: UUID,
    session_id: str,
    workspace_id: str,
    user_id: str,
    goal: str | None = None,
    decision: str | None = None,
    alternatives: list[str] | None = None,
    constraints: list[str] | None = None,
    avoid: list[str] | None = None,
    authority_level: str | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, title, task_type, domain, context, authority_level
        FROM events
        WHERE id = ?
          AND json_extract(context, '$._tce_workspace') = ?
          AND json_extract(context, '$._tce_owner') = ?
        LIMIT 1
        """,
        (str(event_id), workspace_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="event not found")

    normalized_authority = _authority_level(authority_level or row["authority_level"])
    conn.execute("UPDATE events SET authority_level = ? WHERE id = ?", (normalized_authority, str(event_id)))

    link = conn.execute(
        "SELECT episode_id FROM episode_event_links WHERE event_id = ? LIMIT 1",
        (str(event_id),),
    ).fetchone()
    now = now_utc().isoformat()
    if link:
        episode_id = link["episode_id"]
        if goal:
            conn.execute("UPDATE episodes SET goal = ?, updated_at = ? WHERE id = ?", (goal[:240], now, episode_id))
    else:
        episode_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO episodes(
                id, workspace_id, user_id, session_id, goal, context, outcome, confidence,
                status, authority_score, stability_score, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, '', '', ?, 'open', ?, ?, ?, ?)
            """,
            (
                episode_id,
                workspace_id,
                user_id,
                session_id,
                (goal or row["title"] or row["task_type"] or "Untitled goal")[:240],
                max(0.0, min(1.0, 0.5 + (_authority_score(normalized_authority) * 0.3))),
                _authority_score(normalized_authority),
                0.2,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO episode_event_links(id, episode_id, event_id, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), episode_id, str(event_id), now),
        )

    if decision:
        conn.execute(
            """
            INSERT INTO episode_decisions(id, episode_id, decision, why, alternatives_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                episode_id,
                decision[:500],
                "user_annotation",
                json_dumps(list(alternatives or [])),
                now,
            ),
        )
    if constraints:
        merged_constraints = [item.strip() for item in constraints if isinstance(item, str) and item.strip()]
        if merged_constraints:
            episode_row = conn.execute("SELECT context FROM episodes WHERE id = ?", (episode_id,)).fetchone()
            current = str(episode_row["context"] or "") if episode_row else ""
            addendum = "\n".join(f"- {value}" for value in merged_constraints)
            updated = (f"{current}\nConstraints:\n{addendum}".strip())[:2000]
            conn.execute("UPDATE episodes SET context = ?, updated_at = ? WHERE id = ?", (updated, now, episode_id))

    existing_lesson = conn.execute(
        "SELECT episode_id, do_more_json, do_less_json, avoid_json FROM episode_lessons WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    if existing_lesson:
        avoid_list = list(json_loads(existing_lesson["avoid_json"], []))
        if avoid:
            avoid_list.extend([item for item in avoid if item])
        avoid_list = list(dict.fromkeys(avoid_list))
        conn.execute(
            "UPDATE episode_lessons SET avoid_json = ?, updated_at = ? WHERE episode_id = ?",
            (json_dumps(avoid_list), now, episode_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO episode_lessons(episode_id, do_more_json, do_less_json, avoid_json, updated_at)
            VALUES(?, '[]', '[]', ?, ?)
            """,
            (episode_id, json_dumps(list(avoid or [])), now),
        )
    conn.commit()
    return {
        "event_id": str(event_id),
        "episode_id": episode_id,
        "authority_level": normalized_authority,
        "updated": True,
    }


def list_episodes(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str = "default",
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    clauses = ["workspace_id = ?", "user_id = ?", "session_id = ?"]
    params: list[Any] = [workspace_id, user_id, session_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(limit, 300)))
    rows = conn.execute(
        f"""
        SELECT *
        FROM episodes
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    episodes: list[dict[str, Any]] = []
    for row in rows:
        episode_id = row["id"]
        decisions = conn.execute(
            "SELECT decision, why, alternatives_json FROM episode_decisions WHERE episode_id = ? ORDER BY created_at ASC",
            (episode_id,),
        ).fetchall()
        lesson_row = conn.execute(
            "SELECT do_more_json, do_less_json, avoid_json FROM episode_lessons WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        event_rows = conn.execute(
            "SELECT event_id FROM episode_event_links WHERE episode_id = ? ORDER BY created_at ASC",
            (episode_id,),
        ).fetchall()
        episodes.append(
            {
                "id": row["id"],
                "workspace_id": row["workspace_id"],
                "user_id": row["user_id"],
                "session_id": row["session_id"],
                "goal": row["goal"],
                "context": row["context"],
                "outcome": row["outcome"],
                "confidence": float(row["confidence"] or 0.0),
                "status": row["status"],
                "authority_score": float(row["authority_score"] or 0.0),
                "stability_score": float(row["stability_score"] or 0.0),
                "decisions": [
                    {
                        "decision": item["decision"],
                        "why": item["why"],
                        "alternatives": json_loads(item["alternatives_json"], []),
                    }
                    for item in decisions
                ],
                "constraints": [],
                "lessons": {
                    "do_more": json_loads(lesson_row["do_more_json"], []) if lesson_row else [],
                    "do_less": json_loads(lesson_row["do_less_json"], []) if lesson_row else [],
                    "avoid": json_loads(lesson_row["avoid_json"], []) if lesson_row else [],
                },
                "artifacts": [],
                "entities": [],
                "source_event_ids": [item["event_id"] for item in event_rows],
                "created_at": datetime.fromisoformat(row["created_at"]),
                "updated_at": datetime.fromisoformat(row["updated_at"]),
            }
        )
    return {"episodes": episodes, "total": len(episodes)}


def get_episode(
    conn: sqlite3.Connection,
    *,
    episode_id: UUID,
    workspace_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    session_row = conn.execute(
        "SELECT session_id FROM episodes WHERE id = ? AND workspace_id = ? AND user_id = ? LIMIT 1",
        (str(episode_id), workspace_id, user_id),
    ).fetchone()
    if not session_row:
        return None
    rows = list_episodes(
        conn,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=str(session_row["session_id"]),
        limit=500,
    )["episodes"]
    for item in rows:
        if isinstance(item, dict) and item.get("id") == str(episode_id):
            return dict(item)
    return None


def context_brief(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    workspace_id: str,
    user_id: str,
    task: str,
    session_id: str = "default",
    app_context: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    max_items: int = 15,
) -> dict[str, Any]:
    context_req = ContextBundleRequest(
        task=task,
        app_context=app_context or {},
        constraints=constraints or {},
    )
    bundle, _ = context_bundle(conn, context_req, settings, workspace_id=workspace_id, owner_id=user_id)
    typed_memory = bundle.structured_context.get("typed_memory", {}) if isinstance(bundle.structured_context, dict) else {}
    typed_facts = [str(item).strip() for item in typed_memory.get("facts", []) if str(item).strip()]
    typed_opinions = [str(item).strip() for item in typed_memory.get("opinions", []) if str(item).strip()]
    typed_skills = [str(item).strip() for item in typed_memory.get("skills", []) if str(item).strip()]
    domain = (app_context or {}).get("domain")
    task_type = (app_context or {}).get("task_type")
    limit = max(5, min(max_items, 30))

    rules_rows = conn.execute(
        """
        SELECT id, scope, rule_type, statement, priority, active, evergreen, expires_at, source_episode_id, created_at, updated_at
        FROM memory_rules
        WHERE workspace_id = ? AND user_id = ? AND active = 1
          AND (evergreen = 1 OR expires_at IS NULL OR expires_at >= ?)
        ORDER BY priority ASC, updated_at DESC
        LIMIT 100
        """,
        (workspace_id, user_id, now_utc().isoformat()),
    ).fetchall()
    if not rules_rows:
        fallback_rows = conn.execute(
            """
            SELECT id, scope, rule_type, statement, priority, active, evergreen, expires_at, source_episode_id, created_at, updated_at, user_id
            FROM memory_rules
            WHERE workspace_id = ? AND active = 1
              AND (evergreen = 1 OR expires_at IS NULL OR expires_at >= ?)
            ORDER BY priority ASC, updated_at DESC
            LIMIT 100
            """,
            (workspace_id, now_utc().isoformat()),
        ).fetchall()
        fallback_users = {str(row["user_id"]).strip() for row in fallback_rows if str(row["user_id"]).strip()}
        if len(fallback_users) == 1:
            rules_rows = fallback_rows
    scoped_rules = [
        row for row in rules_rows
        if _rule_scope_matches(
            json_loads(row["scope"], {}),
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            domain=domain,
            task_type=task_type,
        )
    ]
    hard_rules = [row for row in scoped_rules if int(row["priority"]) <= 1]

    standard_approach = [{"text": text, "citations": []} for text in bundle.do_dont.get("do", [])[:limit]]
    for item in typed_skills[: max(0, limit - len(standard_approach))]:
        standard_approach.append({"text": item, "citations": []})
    context_tiers_enabled = bool(getattr(settings, "context_tiers_enabled", False))
    current_state = [
        {
            "text": (
                summarize_hit_text(event.title, event.summary_l0, event.ts)
                if context_tiers_enabled
                else f"{event.title} ({event.ts.isoformat()})"
            ),
            "citations": [event.id],
        }
        for event in bundle.evidence_events[:limit]
    ]
    for item in typed_facts[: max(0, limit - len(current_state))]:
        current_state.append({"text": item, "citations": []})
    constraints_preferences: list[dict[str, Any]] = []
    if settings.memory_rule_p0_always_include:
        for row in hard_rules[:limit]:
            constraints_preferences.append(
                {"text": f"[P{int(row['priority'])}] {row['statement']}", "citations": []}
            )
    for item in typed_opinions[: max(0, limit - len(constraints_preferences))]:
        constraints_preferences.append({"text": item, "citations": []})
    for text in bundle.do_dont.get("dont", [])[: max(0, limit - len(constraints_preferences))]:
        constraints_preferences.append({"text": text, "citations": []})

    open_rows = conn.execute(
        """
        SELECT id, goal, status
        FROM episodes
        WHERE workspace_id = ? AND user_id = ? AND session_id = ?
          AND status IN ('open', 'in_progress', 'blocked')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (workspace_id, user_id, session_id, limit),
    ).fetchall()
    open_loops = []
    for row in open_rows:
        citations = [
            item["event_id"]
            for item in conn.execute(
                "SELECT event_id FROM episode_event_links WHERE episode_id = ? ORDER BY created_at DESC LIMIT 2",
                (row["id"],),
            ).fetchall()
        ]
        open_loops.append({"text": f"{row['goal']} [{row['status']}]", "citations": citations})

    artifacts = [{"text": f"event:{value}", "citations": [value]} for value in bundle.citations[:limit]]
    merged_citations: list[Any] = []
    seen: set[str] = set()
    for section in (standard_approach, current_state, constraints_preferences, open_loops, artifacts):
        for section_item in section:
            for citation in section_item["citations"]:
                key = str(citation)
                if key in seen:
                    continue
                seen.add(key)
                merged_citations.append(citation)
    return {
        "summary": bundle.summary,
        "standard_approach": standard_approach[:limit],
        "current_state": current_state[:limit],
        "constraints_preferences": constraints_preferences[:limit],
        "open_loops": open_loops[:limit],
        "artifacts": artifacts[:limit],
        "citations": merged_citations,
        "context_tier_used": bundle.context_tier_used,
        "summary_coverage": bundle.summary_coverage,
        "planner_used": bundle.planner_used,
        "subquery_count": bundle.subquery_count,
        "subquery_labels": bundle.subquery_labels,
        "episode_boost_applied": bundle.episode_boost_applied,
        "activation_boost_applied": bundle.activation_boost_applied,
        "generated_at": now_utc(),
    }


def list_memory_rules(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    include_inactive: bool = False,
) -> dict[str, Any]:
    clauses = ["workspace_id = ?", "user_id = ?"]
    params: list[Any] = [workspace_id, user_id]
    if not include_inactive:
        clauses.append("active = 1")
        clauses.append("(evergreen = 1 OR expires_at IS NULL OR expires_at >= ?)")
        params.append(now_utc().isoformat())
    rows = conn.execute(
        f"""
        SELECT id, workspace_id, user_id, scope, rule_type, statement, priority, active, evergreen, expires_at, source_episode_id, created_at, updated_at
        FROM memory_rules
        WHERE {' AND '.join(clauses)}
        ORDER BY priority ASC, updated_at DESC
        """,
        params,
    ).fetchall()
    if not rows:
        fallback_clauses = ["workspace_id = ?"]
        fallback_params: list[Any] = [workspace_id]
        if not include_inactive:
            fallback_clauses.append("active = 1")
            fallback_clauses.append("(evergreen = 1 OR expires_at IS NULL OR expires_at >= ?)")
            fallback_params.append(now_utc().isoformat())
        fallback_rows = conn.execute(
            f"""
            SELECT id, workspace_id, user_id, scope, rule_type, statement, priority, active, evergreen, expires_at, source_episode_id, created_at, updated_at
            FROM memory_rules
            WHERE {' AND '.join(fallback_clauses)}
            ORDER BY priority ASC, updated_at DESC
            """,
            fallback_params,
        ).fetchall()
        fallback_users = {str(row["user_id"]).strip() for row in fallback_rows if str(row["user_id"]).strip()}
        if len(fallback_users) == 1:
            rows = fallback_rows
    rules = [
        {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "user_id": row["user_id"],
            "scope": json_loads(row["scope"], {}),
            "rule_type": row["rule_type"],
            "statement": row["statement"],
            "priority": int(row["priority"]),
            "active": bool(row["active"]),
            "evergreen": bool(row["evergreen"]) if "evergreen" in row.keys() else True,
            "expires_at": datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            "source_episode_id": row["source_episode_id"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
        }
        for row in rows
    ]
    return {"rules": rules, "total": len(rules)}


def upsert_memory_rule(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    scope: dict[str, Any],
    rule_type: str,
    statement: str,
    priority: int,
    evergreen: bool = True,
    expires_at: datetime | None = None,
    source_episode_id: UUID | None,
) -> dict[str, Any]:
    now = now_utc().isoformat()
    rule_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_rules(
            id, workspace_id, user_id, scope, rule_type, statement, priority, active, evergreen, expires_at, source_episode_id, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            workspace_id,
            user_id,
            json_dumps(scope or {}),
            str(rule_type),
            statement.strip(),
            int(priority),
            1 if evergreen else 0,
            expires_at.isoformat() if isinstance(expires_at, datetime) else None,
            str(source_episode_id) if source_episode_id else None,
            now,
            now,
        ),
    )
    conn.commit()
    rules = list_memory_rules(
        conn,
        workspace_id=workspace_id,
        user_id=user_id,
        include_inactive=True,
    ).get("rules")
    if not isinstance(rules, list) or not rules or not isinstance(rules[0], dict):
        raise RuntimeError("memory rule was persisted but could not be reloaded")
    return dict(rules[0])


def deprecate_memory_rule(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    rule_id: UUID,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id FROM memory_rules WHERE id = ? AND workspace_id = ? AND user_id = ? LIMIT 1",
        (str(rule_id), workspace_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="memory rule not found")
    conn.execute(
        "UPDATE memory_rules SET active = 0, updated_at = ? WHERE id = ?",
        (now_utc().isoformat(), str(rule_id)),
    )
    conn.commit()
    return {"rule_id": str(rule_id), "active": False, "updated": True}


def forget_memory(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    requested_by: str,
    target_type: str,
    target_ids: list[str],
    reason: str,
    hard_delete: bool,
) -> dict[str, Any]:
    ids = [str(value).strip() for value in target_ids if str(value).strip()]
    deleted = 0
    if target_type == "event":
        if ids:
            placeholders = ",".join("?" for _ in ids)
            deleted = int(
                conn.execute(
                    f"""
                    DELETE FROM events
                    WHERE id IN ({placeholders})
                      AND json_extract(context, '$._tce_workspace') = ?
                      AND json_extract(context, '$._tce_owner') = ?
                    """,
                    (*ids, workspace_id, user_id),
                ).rowcount
                or 0
            )
    elif target_type == "episode":
        if ids:
            placeholders = ",".join("?" for _ in ids)
            deleted = int(
                conn.execute(
                    f"DELETE FROM episodes WHERE id IN ({placeholders}) AND workspace_id = ? AND user_id = ?",
                    (*ids, workspace_id, user_id),
                ).rowcount
                or 0
            )
    elif target_type == "rule":
        if ids:
            placeholders = ",".join("?" for _ in ids)
            deleted = int(
                conn.execute(
                    f"DELETE FROM memory_rules WHERE id IN ({placeholders}) AND workspace_id = ? AND user_id = ?",
                    (*ids, workspace_id, user_id),
                ).rowcount
                or 0
            )
    else:
        raise HTTPException(status_code=400, detail="unsupported target_type")
    tombstone_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO memory_tombstones(
            id, workspace_id, user_id, target_type, target_ids, reason, requested_by, deleted_at, meta
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tombstone_id,
            workspace_id,
            user_id,
            target_type,
            json_dumps(ids),
            reason,
            requested_by,
            now_utc().isoformat(),
            json_dumps({"hard_delete": bool(hard_delete), "deleted_count": deleted}),
        ),
    )
    conn.commit()
    return {
        "deleted_count": deleted,
        "tombstone_id": tombstone_id,
        "target_type": target_type,
        "target_ids": ids,
    }


def _autonomy_quality_snapshot_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    lookback: int,
) -> dict[str, Any]:
    limit = max(10, int(lookback or 120))
    action_rows = conn.execute(
        """
        SELECT result, meta
        FROM takeover_action_log
        WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND action_kind = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (session_id, workspace_id, user_id, "takeover_step", limit),
    ).fetchall()
    decision_confidence_values: list[float] = []
    context_quality_values: list[float] = []
    needs_human_count = 0
    retrieval_trigger_count = 0
    for row in action_rows:
        meta = json_loads(row["meta"], {})
        if not isinstance(meta, dict):
            meta = {}
        if str(row["result"] or "").strip().lower() == "needs_human" or bool(meta.get("needs_human")):
            needs_human_count += 1
        if bool(meta.get("retrieval_triggered")):
            retrieval_trigger_count += 1
        decision_confidence_values.append(_safe_float(meta.get("decision_confidence"), 0.0))
        context_quality_values.append(_safe_float(meta.get("context_quality_score"), 0.0))

    directive_rows = conn.execute(
        """
        SELECT state, meta
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (session_id, workspace_id, user_id, limit),
    ).fetchall()
    terminal_states = {
        DirectiveExecutionState.SUCCEEDED.value,
        DirectiveExecutionState.FAILED.value,
        DirectiveExecutionState.BLOCKED.value,
        DirectiveExecutionState.ABANDONED.value,
    }
    execution_states = {
        DirectiveExecutionState.SUCCEEDED.value,
        DirectiveExecutionState.FAILED.value,
        DirectiveExecutionState.BLOCKED.value,
    }
    terminal_count = 0
    execution_count = 0
    success_count = 0
    retry_count = 0
    calibration_abs_errors: list[float] = []
    outcome_feedback_count = 0
    for row in directive_rows:
        state_value = str(row["state"] or "").strip().lower()
        if state_value in terminal_states:
            terminal_count += 1
        if state_value in execution_states:
            execution_count += 1
            if state_value == DirectiveExecutionState.SUCCEEDED.value:
                success_count += 1
        meta = json_loads(row["meta"], {})
        if isinstance(meta, dict) and meta.get("retry_of"):
            retry_count += 1
        if state_value in execution_states and isinstance(meta, dict):
            if bool(meta.get("outcome_recorded")) or bool(meta.get("observation_id")):
                outcome_feedback_count += 1
            conf_value = _safe_float(meta.get("decision_confidence"), -1.0)
            if conf_value >= 0.0:
                conf_value = max(0.0, min(1.0, conf_value))
                actual = 1.0 if state_value == DirectiveExecutionState.SUCCEEDED.value else 0.0
                calibration_abs_errors.append(abs(conf_value - actual))

    calibration_sample_count = len(calibration_abs_errors)
    confidence_alignment = (
        max(0.0, min(1.0, 1.0 - (sum(calibration_abs_errors) / max(1, calibration_sample_count))))
        if calibration_sample_count > 0
        else 0.0
    )

    eval_rows = conn.execute(
        """
        SELECT style_alignment, constraint_compliance, decision_traceability, followup_reduction
        FROM retrieval_eval_runs
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (session_id, workspace_id, user_id),
    ).fetchone()
    eval_floor = 0.0
    if eval_rows is not None:
        eval_values = [
            _safe_float(eval_rows["style_alignment"], 0.0),
            _safe_float(eval_rows["constraint_compliance"], 0.0),
            _safe_float(eval_rows["decision_traceability"], 0.0),
            _safe_float(eval_rows["followup_reduction"], 0.0),
        ]
        eval_floor = max(0.0, min(1.0, min(eval_values)))

    turn_count = len(action_rows)
    return {
        "turn_count": turn_count,
        "terminal_directive_count": terminal_count,
        "execution_success_rate": (
            max(0.0, min(1.0, float(success_count) / max(1, execution_count)))
            if execution_count > 0
            else 0.0
        ),
        "needs_human_rate": (
            max(0.0, min(1.0, float(needs_human_count) / max(1, turn_count)))
            if turn_count > 0
            else 1.0
        ),
        "avg_decision_confidence": (
            max(0.0, min(1.0, sum(decision_confidence_values) / max(1, len(decision_confidence_values))))
            if decision_confidence_values
            else 0.0
        ),
        "avg_context_quality": (
            max(0.0, min(1.0, sum(context_quality_values) / max(1, len(context_quality_values))))
            if context_quality_values
            else 0.0
        ),
        "retry_rate": max(0.0, min(1.0, float(retry_count) / max(1, len(directive_rows)))),
        "retrieval_trigger_rate": (
            max(0.0, min(1.0, float(retrieval_trigger_count) / max(1, turn_count)))
            if turn_count > 0
            else 0.0
        ),
        "eval_floor": eval_floor,
        "confidence_alignment": confidence_alignment,
        "calibration_sample_count": calibration_sample_count,
        "outcome_feedback_rate": (
            max(0.0, min(1.0, float(outcome_feedback_count) / max(1, execution_count)))
            if execution_count > 0
            else 0.0
        ),
        "outcome_feedback_count": outcome_feedback_count,
        "outcome_feedback_sample_count": execution_count,
    }


def autonomy_readiness(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    lookback = max(20, int(getattr(settings, "autonomy_gate_lookback_turns", 120)))
    metrics = _autonomy_quality_snapshot_lite(
        conn,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        lookback=lookback,
    )
    thresholds = {
        "min_turns": int(getattr(settings, "autonomy_gate_min_turns", 20)),
        "min_terminal_directives": int(getattr(settings, "autonomy_gate_min_terminal_directives", 8)),
        "min_execution_success_rate": float(getattr(settings, "autonomy_gate_min_execution_success_rate", 0.88)),
        "max_needs_human_rate": float(getattr(settings, "autonomy_gate_max_needs_human_rate", 0.20)),
        "min_avg_decision_confidence": float(getattr(settings, "autonomy_gate_min_avg_decision_confidence", 0.72)),
        "min_avg_context_quality": float(getattr(settings, "autonomy_gate_min_avg_context_quality", 0.70)),
        "min_eval_floor": float(getattr(settings, "autonomy_gate_min_eval_floor", 0.70)),
        "min_confidence_alignment": float(getattr(settings, "autonomy_gate_min_confidence_alignment", 0.55)),
        "min_outcome_feedback_rate": float(getattr(settings, "autonomy_gate_min_outcome_feedback_rate", 0.60)),
        "min_calibration_samples": int(getattr(settings, "autonomy_gate_min_calibration_samples", 20)),
        "min_outcome_feedback_samples": int(getattr(settings, "autonomy_gate_min_outcome_feedback_samples", 20)),
    }
    legacy_outcome_feedback = (
        int(metrics.get("outcome_feedback_sample_count", 0) or 0) > 0
        and int(metrics.get("outcome_feedback_count", 0) or 0) == 0
        and int(metrics.get("calibration_sample_count", 0) or 0) == 0
    )
    confidence_alignment_for_score = float(metrics.get("confidence_alignment", 0.0) or 0.0)
    if int(metrics.get("calibration_sample_count", 0) or 0) < int(thresholds["min_calibration_samples"]):
        confidence_alignment_for_score = float(metrics.get("avg_decision_confidence", 0.0) or 0.0)
    outcome_feedback_for_score = float(metrics.get("outcome_feedback_rate", 0.0) or 0.0)
    if legacy_outcome_feedback or (
        int(metrics.get("outcome_feedback_sample_count", 0) or 0) < int(thresholds["min_outcome_feedback_samples"])
    ):
        outcome_feedback_for_score = float(metrics.get("execution_success_rate", 0.0) or 0.0)
    eval_missing = float(metrics.get("eval_floor", 0.0) or 0.0) <= 0.0
    if eval_missing and int(metrics.get("turn_count", 0) or 0) > 0:
        bootstrap_eval = max(
            0.0,
            min(
                1.0,
                (
                    (0.50 * float(metrics.get("avg_decision_confidence", 0.0) or 0.0))
                    + (0.50 * float(metrics.get("avg_context_quality", 0.0) or 0.0))
                ),
            ),
        )
        metrics["eval_floor"] = round(bootstrap_eval, 4)
    checks = {
        "sample_size": (
            int(metrics["turn_count"]) >= int(thresholds["min_turns"])
            and int(metrics["terminal_directive_count"]) >= int(thresholds["min_terminal_directives"])
        ),
        "execution_success_rate": float(metrics["execution_success_rate"]) >= float(thresholds["min_execution_success_rate"]),
        "needs_human_rate": float(metrics["needs_human_rate"]) <= float(thresholds["max_needs_human_rate"]),
        "avg_decision_confidence": float(metrics["avg_decision_confidence"]) >= float(thresholds["min_avg_decision_confidence"]),
        "avg_context_quality": float(metrics["avg_context_quality"]) >= float(thresholds["min_avg_context_quality"]),
        "eval_floor": float(metrics["eval_floor"]) >= float(thresholds["min_eval_floor"]),
        "confidence_alignment": (
            int(metrics.get("calibration_sample_count", 0) or 0) < int(thresholds["min_calibration_samples"])
            or float(metrics.get("confidence_alignment", 0.0) or 0.0) >= float(thresholds["min_confidence_alignment"])
        ),
        "outcome_feedback_rate": (
            legacy_outcome_feedback
            or int(metrics.get("outcome_feedback_sample_count", 0) or 0) < int(thresholds["min_outcome_feedback_samples"])
            or float(metrics.get("outcome_feedback_rate", 0.0) or 0.0) >= float(thresholds["min_outcome_feedback_rate"])
        ),
    }
    if not checks["sample_size"] and eval_missing:
        checks["eval_floor"] = True
    if not checks["sample_size"]:
        checks["confidence_alignment"] = True
        checks["outcome_feedback_rate"] = True
    gate_passed = all(bool(value) for value in checks.values())
    score = int(
        round(
            max(
                0.0,
                min(
                    100.0,
                    100.0
                    * (
                        (0.25 * float(metrics["execution_success_rate"]))
                        + (0.15 * (1.0 - float(metrics["needs_human_rate"])))
                        + (0.15 * float(metrics["avg_decision_confidence"]))
                        + (0.15 * float(metrics["avg_context_quality"]))
                        + (0.10 * float(metrics["eval_floor"]))
                        + (0.10 * confidence_alignment_for_score)
                        + (0.10 * outcome_feedback_for_score)
                    ),
                ),
            )
        )
    )
    if gate_passed and score >= 85:
        band = "full_product_ready"
    elif score >= 70:
        band = "pilot_ready"
    else:
        band = "not_ready"
    if not checks["sample_size"]:
        status_phase = "warmup"
    elif gate_passed:
        status_phase = "ready"
    else:
        status_phase = "stabilizing"
    failing_checks = [name for name, passed in checks.items() if not passed]
    check_reasons = {
        "sample_size": (
            f"turns={int(metrics['turn_count'])}/{int(thresholds['min_turns'])}, "
            f"directives={int(metrics['terminal_directive_count'])}/{int(thresholds['min_terminal_directives'])}"
        ),
        "execution_success_rate": (
            f"{float(metrics['execution_success_rate']):.2f} >= {float(thresholds['min_execution_success_rate']):.2f}"
        ),
        "needs_human_rate": (
            f"{float(metrics['needs_human_rate']):.2f} <= {float(thresholds['max_needs_human_rate']):.2f}"
        ),
        "avg_decision_confidence": (
            f"{float(metrics['avg_decision_confidence']):.2f} >= {float(thresholds['min_avg_decision_confidence']):.2f}"
        ),
        "avg_context_quality": (
            f"{float(metrics['avg_context_quality']):.2f} >= {float(thresholds['min_avg_context_quality']):.2f}"
        ),
        "eval_floor": (
            "missing retrieval eval window, using bootstrap estimate"
            if eval_missing
            else f"{float(metrics['eval_floor']):.2f} >= {float(thresholds['min_eval_floor']):.2f}"
        ),
        "confidence_alignment": (
            "insufficient calibration samples; collecting live confidence/outcome pairs"
            if int(metrics.get("calibration_sample_count", 0) or 0) < int(thresholds["min_calibration_samples"])
            else (
                f"{float(metrics.get('confidence_alignment', 0.0) or 0.0):.2f} >= "
                f"{float(thresholds['min_confidence_alignment']):.2f}"
            )
        ),
        "outcome_feedback_rate": (
            "legacy session without outcome meta; pass is deferred while new traces are collected"
            if legacy_outcome_feedback
            else (
                "insufficient outcome-feedback samples; collecting execution outcome traces"
                if int(metrics.get("outcome_feedback_sample_count", 0) or 0)
                < int(thresholds["min_outcome_feedback_samples"])
                else (
                    f"{float(metrics.get('outcome_feedback_rate', 0.0) or 0.0):.2f} >= "
                    f"{float(thresholds['min_outcome_feedback_rate']):.2f}"
                )
            )
        ),
    }
    return {
        "session_id": session_id,
        "gate_passed": gate_passed,
        "score": score,
        "band": band,
        "status_phase": status_phase,
        "metrics": metrics,
        "thresholds": thresholds,
        "checks": checks,
        "check_reasons": check_reasons,
        "failing_checks": failing_checks,
        "eval_missing": eval_missing,
        "lookback_turns": lookback,
        "generated_at": now_utc().isoformat(),
    }


def _update_structured_plan_progress_context_lite(
    context: dict[str, Any],
    *,
    execution_state: DirectiveExecutionState,
    updated_at: datetime,
) -> None:
    plan = context.get("structured_plan")
    if not isinstance(plan, dict):
        return
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "pending").strip().lower()
        if status != "pending":
            continue
        if execution_state == DirectiveExecutionState.SUCCEEDED:
            step["status"] = "done"
        elif execution_state == DirectiveExecutionState.ABANDONED:
            step["status"] = "abandoned"
        elif execution_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}:
            step["status"] = "blocked"
        step["updated_at"] = updated_at.isoformat()
        break
    plan["updated_at"] = updated_at.isoformat()
    if all(
        isinstance(step, dict)
        and str(step.get("status") or "").strip().lower() in {"done", "skipped"}
        for step in steps
    ):
        plan["completed_at"] = updated_at.isoformat()
    context["structured_plan"] = plan


def _update_objective_quality_context_lite(
    context: dict[str, Any],
    *,
    execution_state: DirectiveExecutionState,
    retry_scheduled: bool,
    updated_at: datetime,
    details: dict[str, Any] | None = None,
    required_verifications: list[str] | None = None,
) -> None:
    stats = context.get("objective_quality")
    if not isinstance(stats, dict):
        stats = {}
    stats["total_reports"] = int(stats.get("total_reports", 0) or 0) + 1
    state_key = f"{execution_state.value}_count"
    stats[state_key] = int(stats.get(state_key, 0) or 0) + 1
    if retry_scheduled:
        stats["retry_scheduled_count"] = int(stats.get("retry_scheduled_count", 0) or 0) + 1
    terminal_total = sum(
        int(stats.get(f"{state.value}_count", 0) or 0)
        for state in (
            DirectiveExecutionState.SUCCEEDED,
            DirectiveExecutionState.FAILED,
            DirectiveExecutionState.BLOCKED,
        )
    )
    abandoned_total = int(stats.get(f"{DirectiveExecutionState.ABANDONED.value}_count", 0) or 0)
    success_total = int(stats.get(f"{DirectiveExecutionState.SUCCEEDED.value}_count", 0) or 0)
    stats["execution_terminal_count"] = terminal_total
    stats["terminal_directive_count"] = terminal_total + abandoned_total
    stats["abandoned_directive_count"] = abandoned_total
    stats["execution_success_rate"] = round(float(success_total) / max(1, terminal_total), 4) if terminal_total else 0.0
    stats["retry_pressure"] = round(
        float(int(stats.get("retry_scheduled_count", 0) or 0)) / max(1, int(stats.get("total_reports", 1) or 1)),
        4,
    )
    required_checks = [str(item).strip().lower() for item in (required_verifications or []) if str(item).strip()]
    details_map = details if isinstance(details, dict) else {}
    verification_map = details_map.get("verification")
    if isinstance(verification_map, dict):
        normalized_verification: dict[str, bool] = {}
        for key, value in verification_map.items():
            key_name = str(key).strip().lower()
            if not key_name:
                continue
            normalized_verification[key_name] = bool(value)
        if normalized_verification:
            stats["verification_runs"] = int(stats.get("verification_runs", 0) or 0) + 1
            verification_ok = (
                all(bool(normalized_verification.get(item, False)) for item in required_checks)
                if required_checks
                else all(bool(v) for v in normalized_verification.values())
            )
            if verification_ok:
                stats["verification_passed"] = int(stats.get("verification_passed", 0) or 0) + 1
            stats["verification_pass_rate"] = round(
                float(int(stats.get("verification_passed", 0) or 0))
                / max(1, int(stats.get("verification_runs", 0) or 0)),
                4,
            )
            stats["last_verification"] = {
                "checks": normalized_verification,
                "required": required_checks,
                "passed": bool(verification_ok),
                "updated_at": updated_at.isoformat(),
            }
    stats["updated_at"] = updated_at.isoformat()
    context["objective_quality"] = stats


def _required_verification_checks_lite(settings: Settings) -> list[str]:
    raw = str(getattr(settings, "autonomy_verification_required_checks", "build,test,lint") or "")
    checks = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return checks or ["build", "test", "lint"]


def _ensure_objective_contract_context_lite(
    context: dict[str, Any],
    *,
    objective: str,
    settings: Settings,
    updated_at: datetime,
) -> None:
    objective_text = str(objective or "").strip()
    if not objective_text:
        return
    current = context.get("objective_contract")
    if isinstance(current, dict):
        same_objective = str(current.get("objective") or "").strip().lower() == objective_text.lower()
        if same_objective:
            return
    contract = {
        "objective": objective_text[:260],
        "completion_state": "in_progress",
        "definition_of_done": [
            {"id": "scope", "label": "Scope accepted", "status": "done"},
            {"id": "implementation", "label": "Implementation completed", "status": "pending"},
            {"id": "verification", "label": "Build/test/lint verification passed", "status": "pending"},
            {"id": "docs", "label": "Docs or handoff notes updated", "status": "pending"},
        ],
        "verification": {
            "required": _required_verification_checks_lite(settings),
            "status": "pending",
            "passed_checks": [],
            "failed_checks": [],
            "last_run_at": None,
        },
        "created_at": updated_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }
    context["objective_contract"] = contract


def _update_objective_contract_context_lite(
    context: dict[str, Any],
    *,
    execution_state: DirectiveExecutionState,
    details: dict[str, Any] | None,
    updated_at: datetime,
) -> None:
    contract = context.get("objective_contract")
    if not isinstance(contract, dict):
        return
    items = contract.get("definition_of_done")
    if not isinstance(items, list):
        return
    item_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip().lower()
        if item_id:
            item_by_id[item_id] = item
    details_map = details if isinstance(details, dict) else {}
    if execution_state == DirectiveExecutionState.SUCCEEDED and "implementation" in item_by_id:
        item_by_id["implementation"]["status"] = "done"
    docs_updated = bool(details_map.get("docs_updated") or details_map.get("handoff_notes_updated"))
    if docs_updated and "docs" in item_by_id:
        item_by_id["docs"]["status"] = "done"
    verification_block = contract.get("verification")
    if not isinstance(verification_block, dict):
        verification_block = {}
    required_checks = [
        str(item).strip().lower()
        for item in verification_block.get("required", [])
        if str(item).strip()
    ]
    verification_map = details_map.get("verification")
    if isinstance(verification_map, dict):
        normalized: dict[str, bool] = {}
        for key, value in verification_map.items():
            key_name = str(key).strip().lower()
            if key_name:
                normalized[key_name] = bool(value)
        if normalized:
            passed_checks = [key for key, value in normalized.items() if value]
            failed_checks = [key for key, value in normalized.items() if not value]
            verification_ok = (
                all(bool(normalized.get(item, False)) for item in required_checks)
                if required_checks
                else not failed_checks
            )
            verification_block["status"] = "passed" if verification_ok else "failed"
            verification_block["passed_checks"] = passed_checks
            verification_block["failed_checks"] = failed_checks
            verification_block["last_run_at"] = updated_at.isoformat()
            if "verification" in item_by_id:
                item_by_id["verification"]["status"] = "done" if verification_ok else "blocked"
    contract["verification"] = verification_block
    all_done = all(
        isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "done"
        for item in items
    )
    if all_done:
        contract["completion_state"] = "completed"
        contract["completed_at"] = updated_at.isoformat()
    elif execution_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}:
        contract["completion_state"] = "blocked"
    else:
        contract["completion_state"] = "in_progress"
    contract["updated_at"] = updated_at.isoformat()
    context["objective_contract"] = contract


def autonomy_project_kpis(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    lookback = max(20, int(getattr(settings, "autonomy_gate_lookback_turns", 120)))
    limit = max(20, lookback)
    action_rows = conn.execute(
        """
        SELECT result
        FROM takeover_action_log
        WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND action_kind = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (session_id, workspace_id, user_id, "takeover_step", limit),
    ).fetchall()
    needs_human_turns = sum(1 for row in action_rows if str(row["result"] or "").strip().lower() == "needs_human")
    directive_rows = conn.execute(
        """
        SELECT objective_hash, state, meta, updated_at
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (session_id, workspace_id, user_id, limit),
    ).fetchall()
    terminal_states = {
        DirectiveExecutionState.SUCCEEDED.value,
        DirectiveExecutionState.FAILED.value,
        DirectiveExecutionState.BLOCKED.value,
        DirectiveExecutionState.ABANDONED.value,
    }
    project_state: dict[str, dict[str, Any]] = {}
    verification_runs = 0
    verification_passed = 0
    terminal_directive_count = 0
    for row in directive_rows:
        objective_hash_value = str(row["objective_hash"] or "").strip()
        state_value = str(row["state"] or "").strip().lower()
        if state_value in terminal_states:
            terminal_directive_count += 1
        if not objective_hash_value:
            continue
        state_bucket = project_state.setdefault(
            objective_hash_value,
            {"has_success": False, "reopened_after_success": False},
        )
        if state_value == DirectiveExecutionState.SUCCEEDED.value:
            state_bucket["has_success"] = True
        elif state_value in {
            DirectiveExecutionState.FAILED.value,
            DirectiveExecutionState.BLOCKED.value,
            DirectiveExecutionState.ABANDONED.value,
        } and bool(state_bucket.get("has_success")):
            state_bucket["reopened_after_success"] = True
        meta = json_loads(row["meta"], {})
        if not isinstance(meta, dict):
            meta = {}
        verification_summary = meta.get("verification_summary")
        verification_map = verification_summary if isinstance(verification_summary, dict) else meta.get("verification")
        if isinstance(verification_map, dict):
            required_checks = _required_verification_checks_lite(settings)
            verification_runs += 1
            verification_ok = all(bool(verification_map.get(check, False)) for check in required_checks)
            if verification_ok:
                verification_passed += 1
    project_count = len(project_state)
    completed_projects = sum(1 for item in project_state.values() if bool(item.get("has_success")))
    reopened_projects = sum(1 for item in project_state.values() if bool(item.get("reopened_after_success")))
    completion_rate = (float(completed_projects) / max(1, project_count)) if project_count > 0 else 0.0
    manual_interventions_per_project = (
        float(needs_human_turns) / max(1, project_count)
        if project_count > 0
        else float(needs_human_turns)
    )
    reopen_rate = (float(reopened_projects) / max(1, completed_projects)) if completed_projects > 0 else 0.0
    verification_pass_rate = (float(verification_passed) / max(1, verification_runs)) if verification_runs > 0 else 1.0
    thresholds = {
        "min_project_completion_rate": float(getattr(settings, "autonomy_kpi_min_project_completion_rate", 0.80)),
        "max_manual_interventions_per_project": float(
            getattr(settings, "autonomy_kpi_max_manual_interventions_per_project", 1.0)
        ),
        "max_reopen_rate_after_completion": float(
            getattr(settings, "autonomy_kpi_max_reopen_rate_after_completion", 0.10)
        ),
        "min_verification_pass_rate": float(getattr(settings, "autonomy_kpi_min_verification_pass_rate", 0.80)),
    }
    checks = {
        "project_completion_rate": completion_rate >= thresholds["min_project_completion_rate"],
        "manual_interventions_per_project": manual_interventions_per_project <= thresholds["max_manual_interventions_per_project"],
        "reopen_rate_after_completion": reopen_rate <= thresholds["max_reopen_rate_after_completion"],
        "verification_pass_rate": (
            verification_pass_rate >= thresholds["min_verification_pass_rate"]
            if verification_runs > 0
            else True
        ),
    }
    passed = all(bool(value) for value in checks.values())
    band = "project_autonomy_ready" if passed else "project_autonomy_blocked"
    failing_checks = [name for name, ok in checks.items() if not ok]
    return {
        "session_id": session_id,
        "passed": passed,
        "band": band,
        "metrics": {
            "project_count": project_count,
            "completed_project_count": completed_projects,
            "terminal_directive_count": terminal_directive_count,
            "needs_human_turns": needs_human_turns,
            "project_completion_rate": round(max(0.0, min(1.0, completion_rate)), 4),
            "manual_interventions_per_project": round(max(0.0, manual_interventions_per_project), 4),
            "reopen_rate_after_completion": round(max(0.0, min(1.0, reopen_rate)), 4),
            "verification_pass_rate": round(max(0.0, min(1.0, verification_pass_rate)), 4),
            "verification_runs": verification_runs,
        },
        "thresholds": thresholds,
        "checks": checks,
        "failing_checks": failing_checks,
        "generated_at": now_utc().isoformat(),
    }


def run_retrieval_eval(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    tasks: list[str],
    with_brief: bool,
    settings: Settings | None = None,
) -> dict[str, Any]:
    started_at = now_utc()
    task_count = max(1, len(tasks))
    rule_count = _safe_int(
        conn.execute(
            "SELECT COUNT(1) AS c FROM memory_rules WHERE workspace_id = ? AND user_id = ? AND active = 1",
            (workspace_id, user_id),
        ).fetchone()["c"]
    )
    episode_count = _safe_int(
        conn.execute(
            "SELECT COUNT(1) AS c FROM episodes WHERE workspace_id = ? AND user_id = ? AND session_id = ?",
            (workspace_id, user_id, session_id),
        ).fetchone()["c"]
    )
    quality_snapshot = _autonomy_quality_snapshot_lite(
        conn,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        lookback=max(30, int(getattr(settings, "autonomy_gate_lookback_turns", 120))),
    )
    style_alignment = max(
        0.0,
        min(
            1.0,
            0.30
            + (0.04 * min(task_count, 10))
            + (0.15 * min(rule_count, 20) / 20.0)
            + (0.20 * float(quality_snapshot["avg_decision_confidence"]))
            + (0.10 * float(quality_snapshot["avg_context_quality"])),
        ),
    )
    constraint_compliance = max(
        0.0,
        min(
            1.0,
            0.30
            + (0.15 * min(rule_count, 20) / 20.0)
            + (0.20 * float(quality_snapshot["execution_success_rate"]))
            + (0.15 * (1.0 - float(quality_snapshot["needs_human_rate"])))
            + (0.10 * (1.0 - float(quality_snapshot["retry_rate"])))
            + (0.08 if with_brief else 0.0),
        ),
    )
    decision_traceability = max(
        0.0,
        min(
            1.0,
            0.25
            + (0.15 * min(episode_count, 20) / 20.0)
            + (0.20 * float(quality_snapshot["avg_context_quality"]))
            + (0.20 * float(quality_snapshot["eval_floor"]))
            + (0.10 * float(quality_snapshot["execution_success_rate"])),
        ),
    )
    followup_reduction = max(
        0.0,
        min(
            1.0,
            0.20
            + (0.04 * min(task_count, 10))
            + (0.20 * (1.0 - float(quality_snapshot["needs_human_rate"])))
            + (0.20 * float(quality_snapshot["execution_success_rate"]))
            + (0.06 * (1.0 - float(quality_snapshot["retrieval_trigger_rate"])))
            + (0.10 if with_brief else 0.0),
        ),
    )
    run_id = str(uuid.uuid4())
    completed_at = now_utc()
    conn.execute(
        """
        INSERT INTO retrieval_eval_runs(
            id, session_id, workspace_id, user_id,
            style_alignment, constraint_compliance, decision_traceability, followup_reduction,
            started_at, completed_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            session_id,
            workspace_id,
            user_id,
            round(style_alignment, 4),
            round(constraint_compliance, 4),
            round(decision_traceability, 4),
            round(followup_reduction, 4),
            started_at.isoformat(),
            completed_at.isoformat(),
        ),
    )
    conn.commit()
    return {
        "run_id": run_id,
        "session_id": session_id,
        "style_alignment": round(style_alignment, 4),
        "constraint_compliance": round(constraint_compliance, 4),
        "decision_traceability": round(decision_traceability, 4),
        "followup_reduction": round(followup_reduction, 4),
        "started_at": started_at,
        "completed_at": completed_at,
    }


def retrieval_eval_status(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, session_id, style_alignment, constraint_compliance, decision_traceability,
               followup_reduction, started_at, completed_at
        FROM retrieval_eval_runs
        WHERE workspace_id = ? AND user_id = ? AND session_id = ?
        ORDER BY completed_at DESC
        LIMIT 20
        """,
        (workspace_id, user_id, session_id),
    ).fetchall()
    history = [
        {
            "run_id": row["id"],
            "session_id": row["session_id"],
            "style_alignment": float(row["style_alignment"]),
            "constraint_compliance": float(row["constraint_compliance"]),
            "decision_traceability": float(row["decision_traceability"]),
            "followup_reduction": float(row["followup_reduction"]),
            "started_at": datetime.fromisoformat(row["started_at"]),
            "completed_at": datetime.fromisoformat(row["completed_at"]),
        }
        for row in rows
    ]
    return {"session_id": session_id, "latest": history[0] if history else None, "history": history}


def activity_summary(
    conn: sqlite3.Connection,
    settings: Settings,
    workspace_id: str,
    owner_id: str,
    period: str,
    domain: str | None,
    max_events: int,
) -> dict[str, Any]:
    start_ts, end_ts = summary_window(period)
    clauses = [
        "ts >= ?",
        "ts <= ?",
        "sensitivity <= ?",
    ]
    params: list[Any] = [
        start_ts.isoformat(),
        end_ts.isoformat(),
        max_read_sensitivity(settings),
    ]
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    params.append(max(10, min(max_events, 2000)))
    rows = conn.execute(
        f"""
        SELECT id, ts, domain, task_type, event_type, title, context
        FROM events
        WHERE {' AND '.join(clauses)}
        ORDER BY ts DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    domain_counter: Counter[str] = Counter()
    task_counter: Counter[str] = Counter()
    event_type_counter: Counter[str] = Counter()
    highlights: list[str] = []
    citations: list[str] = []
    blocked = 0

    for row in rows:
        context = json_loads(row["context"], {})
        if isinstance(context, dict) and not _scope_match(context, workspace_id, owner_id):
            blocked += 1
            continue
        domain_counter[row["domain"]] += 1
        task_counter[row["task_type"]] += 1
        event_type_counter[row["event_type"]] += 1
        citations.append(row["id"])
        title = str(row["title"]).strip()
        if title and title not in highlights:
            highlights.append(title)
        if len(highlights) >= 12:
            break

    total = sum(domain_counter.values())
    top_domains = ", ".join([f"{name} ({count})" for name, count in domain_counter.most_common(3)]) or "none"
    top_tasks = ", ".join([f"{name} ({count})" for name, count in task_counter.most_common(3)]) or "none"
    summary = (
        f"Activity summary for {period}: {total} events captured. "
        f"Top domains: {top_domains}. Top task types: {top_tasks}."
    )
    return {
        "period": period,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "total_events": total,
        "by_domain": dict(domain_counter),
        "by_task_type": dict(task_counter),
        "by_event_type": dict(event_type_counter),
        "highlights": highlights,
        "summary": summary,
        "citations": citations,
        "policy": {
            "blocked_count": blocked,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "blocked_sensitivity": settings.block_sensitivity,
        },
    }


def submit_pattern_feedback(
    conn: sqlite3.Connection, body: PatternFeedbackRequest
) -> dict[str, Any]:
    row = conn.execute("SELECT confidence FROM patterns WHERE id = ?", (str(body.pattern_id),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="pattern not found")

    conn.execute(
        """
        INSERT INTO pattern_feedback(id, pattern_id, approved, note, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            str(body.pattern_id),
            1 if body.approved else 0,
            body.note,
            now_utc().isoformat(),
        ),
    )
    new_score = _feedback_score(conn, str(body.pattern_id))
    confidence = (0.85 * float(row["confidence"])) + (0.15 * new_score)
    status = _pattern_status(confidence)
    conn.execute(
        "UPDATE patterns SET confidence = ?, status = ?, updated_at = ? WHERE id = ?",
        (confidence, status, now_utc().isoformat(), str(body.pattern_id)),
    )
    conn.commit()
    return {"status": "ok", "pattern_id": str(body.pattern_id), "confidence": confidence, "state": status}


def interaction_count(conn: sqlite3.Connection, interaction_id: str, action: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(1) AS c
        FROM agent_interactions
        WHERE interaction_id = ? AND action = ?
        """,
        (interaction_id, action),
    ).fetchone()
    return int(row["c"]) if row else 0


def record_interaction(
    conn: sqlite3.Connection,
    interaction_id: str,
    source_consumer: str,
    source_role: str,
    target_role: str,
    action: str,
    citations: list[UUID],
    payload: dict[str, Any],
    allowed: bool,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_interactions(
            id, ts, interaction_id, source_consumer, source_role, target_role, action,
            citations, payload, allowed, reason
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            now_utc().isoformat(),
            interaction_id,
            source_consumer,
            source_role,
            target_role,
            action,
            json_dumps([str(value) for value in citations]),
            json_dumps(payload),
            1 if allowed else 0,
            reason,
        ),
    )
    conn.commit()


def _tokenize_text(value: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return {token for token in cleaned.split() if len(token) > 2}


def _semantic_gate_open(conn: sqlite3.Connection, workspace_id: str, settings: Settings) -> bool:
    if settings.obs_semantic_enabled:
        return True
    if not settings.obs_semantic_autogate:
        return False
    row = conn.execute(
        """
        SELECT COUNT(1) AS c
        FROM decision_observations
        WHERE workspace_id = ?
          AND superseded_by IS NULL
        """,
        (workspace_id,),
    ).fetchone()
    count = int(row["c"]) if row else 0
    return count >= int(settings.obs_semantic_min_observations)


def _query_similar_observations_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    situation_type: str,
    situation_text: str,
    settings: Settings,
    limit: int = 5,
) -> list[dict[str, Any]]:
    canonical = _canonical_situation_type(situation_type)
    aliases = [key for key, value in _SITUATION_ALIAS_MAP.items() if value == canonical]
    candidates = [canonical, *aliases]
    placeholders = ",".join("?" for _ in candidates)
    current_ts = now_utc().isoformat()
    exact_rows = conn.execute(
        f"""
        SELECT id, ts, situation_type, situation_summary, user_response, response_reasoning,
               outcome, outcome_sentiment, confidence, source_event_ids, context_snapshot
        FROM decision_observations
        WHERE workspace_id = ? AND subject_user_id = ?
          AND situation_type IN ({placeholders}) AND superseded_by IS NULL
          AND learning_eligible = 1
          AND lifecycle_status = 'active'
          AND (valid_from = '' OR valid_from <= ?)
          AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY ts DESC
        LIMIT ?
        """,
        (workspace_id, subject_user_id, *candidates, current_ts, current_ts, limit),
    ).fetchall()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in exact_rows:
        row_id = str(row["id"])
        seen.add(row_id)
        results.append(
            {
                "id": row_id,
                "ts": row["ts"],
                "situation_type": row["situation_type"],
                "situation_summary": row["situation_summary"],
                "user_response": row["user_response"],
                "response_reasoning": row["response_reasoning"],
                "outcome": row["outcome"],
                "outcome_sentiment": row["outcome_sentiment"],
                "confidence": float(row["confidence"] or 0.5),
                "source_event_ids": json_loads(row["source_event_ids"], []),
                "context_snapshot": json_loads(row["context_snapshot"], {}),
                "recall_source": "exact",
                "similarity": None,
            }
        )
    if len(results) >= limit or not situation_text.strip():
        return results[:limit]
    if len(results) >= int(settings.obs_semantic_fallback_min_results):
        return results[:limit]
    if not _semantic_gate_open(conn, workspace_id, settings):
        return results[:limit]

    query_tokens = _tokenize_text(situation_text)
    if not query_tokens:
        return results[:limit]
    candidate_rows = conn.execute(
        """
        SELECT id, ts, situation_type, situation_summary, user_response, response_reasoning,
               outcome, outcome_sentiment, confidence, source_event_ids, context_snapshot
        FROM decision_observations
        WHERE workspace_id = ? AND subject_user_id = ? AND superseded_by IS NULL
          AND learning_eligible = 1
          AND lifecycle_status = 'active'
          AND (valid_from = '' OR valid_from <= ?)
          AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY ts DESC
        LIMIT 200
        """,
        (workspace_id, subject_user_id, current_ts, current_ts),
    ).fetchall()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in candidate_rows:
        row_id = str(row["id"])
        if row_id in seen:
            continue
        candidate_text = " ".join(
            [
                str(row["situation_summary"] or ""),
                str(row["user_response"] or ""),
                str(row["response_reasoning"] or ""),
                str(row["outcome"] or ""),
            ]
        )
        candidate_tokens = _tokenize_text(candidate_text)
        if not candidate_tokens:
            continue
        overlap = len(query_tokens.intersection(candidate_tokens))
        if overlap == 0:
            continue
        similarity = overlap / max(len(query_tokens), len(candidate_tokens))
        scored.append((similarity, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    for similarity, row in scored[: max(limit, int(settings.obs_semantic_top_k))]:
        results.append(
            {
                "id": str(row["id"]),
                "ts": row["ts"],
                "situation_type": row["situation_type"],
                "situation_summary": row["situation_summary"],
                "user_response": row["user_response"],
                "response_reasoning": row["response_reasoning"],
                "outcome": row["outcome"],
                "outcome_sentiment": row["outcome_sentiment"],
                "confidence": float(row["confidence"] or 0.5),
                "source_event_ids": json_loads(row["source_event_ids"], []),
                "context_snapshot": json_loads(row["context_snapshot"], {}),
                "recall_source": "semantic",
                "similarity": round(float(similarity), 4),
            }
        )
        if len(results) >= limit:
            break
    return results[:limit]


def _insert_clone_feedback_row(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    session_id: str,
    feedback_type: str,
    correction_text: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO clone_feedback(id, observation_id, session_id, feedback_type, correction_text, ts)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            observation_id,
            session_id,
            feedback_type,
            correction_text,
            now_utc().isoformat(),
        ),
    )


def build_clone_advice(
    conn: sqlite3.Connection,
    body: CloneAdviceRequest,
    auth: AuthContext,
    settings: Settings,
) -> CloneAdviceResponse:
    mode = runtime_mode(conn, settings=settings)
    interaction_id = body.interaction_id or str(uuid.uuid4())
    fallback_used = False
    if mode.mode != OperationMode.CLONE_ADVISOR:
        if not body.allow_fallback or not settings.clone_fallback_to_timeline:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "clone_mode_disabled",
                    "current_mode": mode.mode.value,
                    "hint": "set /v1/runtime/mode to clone_advisor or allow fallback",
                },
            )
        fallback_used = True

    loop_guard_max_turns = int(settings.clone_max_turns_per_interaction)
    if isinstance(body.takeover_context, dict) and body.takeover_context:
        loop_guard_max_turns = max(
            loop_guard_max_turns,
            int(getattr(settings, "takeover_clone_max_turns_per_interaction", 80)),
        )
    advisor_turns = interaction_count(conn, interaction_id, "clone_advice")
    if advisor_turns >= loop_guard_max_turns:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "loop_guard_triggered",
                "interaction_id": interaction_id,
                "max_turns": loop_guard_max_turns,
            },
        )

    effective_constraints = dict(body.constraints or {})
    if body.takeover_context:
        effective_constraints["takeover_context"] = body.takeover_context
    if body.message_delta:
        effective_constraints["message_delta"] = body.message_delta
    if body.executor_output and "latest_executor_output" not in effective_constraints:
        effective_constraints["latest_executor_output"] = body.executor_output

    situation_type = classify_situation(
        body.task,
        semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
        semantic_threshold=float(getattr(settings, "semantic_classifier_situation_threshold", 0.61)),
        semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
    )
    similar_observations = _query_similar_observations_lite(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        situation_type=situation_type,
        situation_text=body.task,
        settings=settings,
        limit=5,
    )

    bundle, blocked = context_bundle(
        conn,
        ContextBundleRequest(
            task=body.task,
            app_context=body.app_context,
            constraints=effective_constraints,
        ),
        settings=settings,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
    )
    pattern_conf = [pattern.confidence for pattern in bundle.top_patterns]
    confidence = sum(pattern_conf) / len(pattern_conf) if pattern_conf else 0.45
    citation_count = len(bundle.citations)
    evidence_strength = "medium"
    if citation_count >= 5 and confidence >= 0.65:
        evidence_strength = "high"
    elif citation_count < 2 or confidence < 0.45:
        evidence_strength = "low"

    conflict_flags: list[str] = []
    if fallback_used:
        conflict_flags.append("fallback_to_timeline_only")

    response = CloneAdviceResponse(
        interaction_id=interaction_id,
        guidance_summary=bundle.summary,
        recommended_actions=bundle.do_dont.get("do", [])[:5],
        do=bundle.do_dont.get("do", [])[:5],
        dont=bundle.do_dont.get("dont", [])[:5],
        confidence=round(confidence, 4),
        evidence_strength=evidence_strength,
        citations=bundle.citations,
        conflict_flags=conflict_flags,
        loop_guard={
            "turns_used": advisor_turns + 1,
            "turns_remaining": max(0, loop_guard_max_turns - advisor_turns - 1),
            "max_turns": loop_guard_max_turns,
        },
        policy={
            "mode": mode.mode.value,
            "fallback_used": fallback_used,
            "blocked": blocked,
            "observation_recall_count": len(similar_observations),
            "semantic_recall_used": any(
                item.get("recall_source") == "semantic" for item in similar_observations
            ),
        },
        clone_context={
            "situation_type": situation_type,
            "similar_observations": similar_observations,
            "current_task": body.task,
        },
        evidence_observations=similar_observations,
    )

    record_interaction(
        conn,
        interaction_id=interaction_id,
        source_consumer=auth.consumer,
        source_role=auth.role.value,
        target_role=AgentRole.EXECUTOR.value,
        action="clone_advice",
        citations=response.citations,
        payload=response.model_dump(mode="json"),
        allowed=True,
        reason="ok",
    )
    return response


def build_clone_arbitration(
    body: CloneArbitrationRequest,
) -> CloneArbitrationResponse:
    if body.human_override:
        final_guidance = body.human_override.strip()
        decision_source = "human_override"
    else:
        final_guidance = f"{body.executor_plan.strip()} | Advisor input: {body.advisor_input.strip()}"
        decision_source = "arbitrated_merge"

    return CloneArbitrationResponse(
        interaction_id=body.interaction_id,
        final_guidance=final_guidance,
        decision_source=decision_source,
        citations=[],
        conflict_resolved=body.executor_plan.strip() != body.advisor_input.strip(),
    )


def takeover_state(
    conn: sqlite3.Connection,
    session_id: str,
    workspace_id: str,
    user_id: str,
    persona_mode: str = "normal",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
) -> TakeoverState:
    row = conn.execute(
        """
        SELECT session_id, workspace_id, user_id, active, mode, persona_mode, activation_keywords,
               stop_keywords, expires_at, activated_at, last_message_at, takeover_context,
               objective_hash, working_set_json, last_deliberation_at, recent_outcomes_json,
               autonomy_score, autonomy_policy_profile, active_goal_id, goal_queue_size,
               last_discovery_at, continuity_violation_count, enforcement_mode, last_tick_at,
               pending_directive_count, retry_backlog_count,
               last_classification, last_safety_decision, updated_at
        FROM takeover_sessions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (session_id, workspace_id, user_id),
    ).fetchone()
    default_activation, default_stop = persona_defaults(persona_mode)
    resolved_activation = activation_keywords if activation_keywords is not None else default_activation
    resolved_stop = stop_keywords if stop_keywords is not None else default_stop
    now = now_utc()
    if row is None:
        return TakeoverState(
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
            active=False,
            mode=TakeoverMode.TAKEOVER,
            persona_mode=persona_mode,
            activation_keywords=resolved_activation,
            stop_keywords=resolved_stop,
            takeover_context={},
            objective_hash=None,
            working_set_json={},
            last_deliberation_at=None,
            recent_outcomes_json=[],
            autonomy_score=0.5,
            autonomy_policy_profile=AutonomyPolicyProfile.HUMAN_CONSULTATIVE,
            active_goal_id=None,
            goal_queue_size=0,
            last_discovery_at=None,
            continuity_violation_count=0,
            enforcement_mode="strict_takeover",
            last_tick_at=None,
            pending_directive_count=0,
            retry_backlog_count=0,
            last_safety_decision=SafetyDecision.ALLOW,
            updated_at=now,
        )
    return TakeoverState(
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        active=bool(row["active"]),
        mode=TakeoverMode(str(row["mode"])),
        persona_mode=row["persona_mode"],
        activation_keywords=row["activation_keywords"],
        stop_keywords=row["stop_keywords"],
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        activated_at=datetime.fromisoformat(row["activated_at"]) if row["activated_at"] else None,
        last_message_at=datetime.fromisoformat(row["last_message_at"]) if row["last_message_at"] else None,
        takeover_context=json_loads(row["takeover_context"], {}),
        objective_hash=row["objective_hash"],
        working_set_json=json_loads(row["working_set_json"], {}),
        last_deliberation_at=datetime.fromisoformat(row["last_deliberation_at"]) if row["last_deliberation_at"] else None,
        recent_outcomes_json=json_loads(row["recent_outcomes_json"], []),
        autonomy_score=float(row["autonomy_score"] if row["autonomy_score"] is not None else 0.5),
        autonomy_policy_profile=AutonomyPolicyProfile(
            str(row["autonomy_policy_profile"] or AutonomyPolicyProfile.HUMAN_CONSULTATIVE.value)
        ),
        active_goal_id=UUID(row["active_goal_id"]) if row["active_goal_id"] else None,
        goal_queue_size=int(row["goal_queue_size"] or 0),
        last_discovery_at=datetime.fromisoformat(row["last_discovery_at"]) if row["last_discovery_at"] else None,
        continuity_violation_count=int(row["continuity_violation_count"] or 0),
        enforcement_mode=str(row["enforcement_mode"] or "strict_takeover"),
        last_tick_at=datetime.fromisoformat(row["last_tick_at"]) if row["last_tick_at"] else None,
        pending_directive_count=int(row["pending_directive_count"] or 0),
        retry_backlog_count=int(row["retry_backlog_count"] or 0),
        last_classification=TakeoverClassification(str(row["last_classification"]))
        if row["last_classification"]
        else None,
        last_safety_decision=SafetyDecision(str(row["last_safety_decision"] or "allow")),
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else now,
    )


def save_takeover_state(conn: sqlite3.Connection, state: TakeoverState) -> TakeoverState:
    conn.execute(
        """
        INSERT INTO takeover_sessions(
            session_id, workspace_id, user_id, active, mode, persona_mode, activation_keywords,
            stop_keywords, expires_at, activated_at, last_message_at, takeover_context, objective_hash,
            working_set_json, last_deliberation_at, recent_outcomes_json, autonomy_score, autonomy_policy_profile,
            active_goal_id, goal_queue_size, last_discovery_at, continuity_violation_count,
            enforcement_mode, last_tick_at, pending_directive_count, retry_backlog_count,
            last_classification, last_safety_decision, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, workspace_id, user_id) DO UPDATE SET
            active = excluded.active,
            mode = excluded.mode,
            persona_mode = excluded.persona_mode,
            activation_keywords = excluded.activation_keywords,
            stop_keywords = excluded.stop_keywords,
            expires_at = excluded.expires_at,
            activated_at = excluded.activated_at,
            last_message_at = excluded.last_message_at,
            takeover_context = excluded.takeover_context,
            objective_hash = excluded.objective_hash,
            working_set_json = excluded.working_set_json,
            last_deliberation_at = excluded.last_deliberation_at,
            recent_outcomes_json = excluded.recent_outcomes_json,
            autonomy_score = excluded.autonomy_score,
            autonomy_policy_profile = excluded.autonomy_policy_profile,
            active_goal_id = excluded.active_goal_id,
            goal_queue_size = excluded.goal_queue_size,
            last_discovery_at = excluded.last_discovery_at,
            continuity_violation_count = excluded.continuity_violation_count,
            enforcement_mode = excluded.enforcement_mode,
            last_tick_at = excluded.last_tick_at,
            pending_directive_count = excluded.pending_directive_count,
            retry_backlog_count = excluded.retry_backlog_count,
            last_classification = excluded.last_classification,
            last_safety_decision = excluded.last_safety_decision,
            updated_at = excluded.updated_at
        """,
        (
            state.session_id,
            state.workspace_id,
            state.user_id,
            1 if state.active else 0,
            state.mode.value,
            state.persona_mode,
            state.activation_keywords,
            state.stop_keywords,
            state.expires_at.isoformat() if state.expires_at else None,
            state.activated_at.isoformat() if state.activated_at else None,
            state.last_message_at.isoformat() if state.last_message_at else None,
            json_dumps(state.takeover_context),
            state.objective_hash,
            json_dumps(state.working_set_json),
            state.last_deliberation_at.isoformat() if state.last_deliberation_at else None,
            json_dumps(state.recent_outcomes_json),
            float(state.autonomy_score),
            state.autonomy_policy_profile.value,
            str(state.active_goal_id) if state.active_goal_id else None,
            int(state.goal_queue_size),
            state.last_discovery_at.isoformat() if state.last_discovery_at else None,
            int(state.continuity_violation_count),
            state.enforcement_mode,
            state.last_tick_at.isoformat() if state.last_tick_at else None,
            int(state.pending_directive_count),
            int(state.retry_backlog_count),
            state.last_classification.value if state.last_classification else None,
            state.last_safety_decision.value,
            state.updated_at.isoformat(),
        ),
    )
    conn.commit()
    return state


def _snapshot_payload_from_state_lite(state: TakeoverState, *, reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "objective_hash": state.objective_hash,
        "takeover_context": state.takeover_context if isinstance(state.takeover_context, dict) else {},
        "working_set_json": state.working_set_json if isinstance(state.working_set_json, dict) else {},
        "recent_outcomes_json": (
            state.recent_outcomes_json if isinstance(state.recent_outcomes_json, list) else []
        ),
        "autonomy_score": float(state.autonomy_score),
        "saved_at": now_utc().isoformat(),
    }


def _save_session_memory_snapshot_lite(
    conn: sqlite3.Connection,
    *,
    state: TakeoverState,
    reason: str,
    max_per_session: int,
) -> str | None:
    objective_hash_value = str(state.objective_hash or "").strip()
    takeover_context = state.takeover_context if isinstance(state.takeover_context, dict) else {}
    working_set = state.working_set_json if isinstance(state.working_set_json, dict) else {}
    if not objective_hash_value or (not takeover_context and not working_set):
        return None
    snapshot_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO session_memory_snapshots(
            id, workspace_id, user_id, session_id, objective_hash, snapshot_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            state.workspace_id,
            state.user_id,
            state.session_id,
            objective_hash_value,
            json_dumps(_snapshot_payload_from_state_lite(state, reason=reason)),
            now_utc().isoformat(),
        ),
    )
    keep_limit = max(1, int(max_per_session))
    conn.execute(
        """
        DELETE FROM session_memory_snapshots
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY workspace_id, user_id, session_id
                           ORDER BY created_at DESC
                       ) AS rn
                FROM session_memory_snapshots
                WHERE workspace_id = ? AND user_id = ? AND session_id = ?
            ) ranked
            WHERE rn > ?
        )
        """,
        (state.workspace_id, state.user_id, state.session_id, keep_limit),
    )
    return snapshot_id


def _load_recent_session_memory_snapshot_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    objective_hash: str,
    max_age_days: int,
) -> dict[str, Any] | None:
    objective_hash_value = str(objective_hash or "").strip()
    if not objective_hash_value:
        return None
    cutoff = (now_utc() - timedelta(days=max(1, int(max_age_days)))).isoformat()
    row = conn.execute(
        """
        SELECT id, snapshot_json, created_at
        FROM session_memory_snapshots
        WHERE workspace_id = ? AND user_id = ? AND session_id = ?
          AND objective_hash = ?
          AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workspace_id, user_id, session_id, objective_hash_value, cutoff),
    ).fetchone()
    if row is None:
        return None
    payload = json_loads(row["snapshot_json"], {})
    created_at_raw = str(row["created_at"] or "").strip()
    try:
        created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else now_utc()
    except ValueError:
        created_at = now_utc()
    age_hours = int(max(0.0, (now_utc() - created_at).total_seconds() / 3600.0))
    return {
        "snapshot_id": str(row["id"]),
        "payload": payload if isinstance(payload, dict) else {},
        "age_hours": age_hours,
    }


def reset_takeover_state(
    conn: sqlite3.Connection,
    session_id: str,
    workspace_id: str,
    user_id: str,
    persona_mode: str = "normal",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
) -> TakeoverState:
    existing_state = takeover_state(
        conn=conn,
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )
    if (
        str(existing_state.objective_hash or "").strip()
        and (
            (isinstance(existing_state.takeover_context, dict) and bool(existing_state.takeover_context))
            or (isinstance(existing_state.working_set_json, dict) and bool(existing_state.working_set_json))
        )
    ):
        _save_session_memory_snapshot_lite(
            conn,
            state=existing_state,
            reason="takeover_reset",
            max_per_session=max(1, int(getattr(get_settings(), "snapshot_max_per_session", 10))),
        )
    conn.execute(
        """
        DELETE FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (session_id, workspace_id, user_id),
    )
    conn.execute(
        """
        UPDATE autonomy_notices
        SET acknowledged_at = COALESCE(acknowledged_at, ?)
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (now_utc().isoformat(), session_id, workspace_id, user_id),
    )
    conn.execute(
        "DELETE FROM takeover_sessions WHERE session_id = ? AND workspace_id = ? AND user_id = ?",
        (session_id, workspace_id, user_id),
    )
    conn.commit()
    return takeover_state(
        conn=conn,
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


def _build_takeover_working_set(
    conn: sqlite3.Connection,
    auth: AuthContext,
    settings: Settings,
    *,
    task: str,
    app_context: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    bundle, _blocked = context_bundle(
        conn,
        ContextBundleRequest(task=task, app_context=app_context, constraints=constraints),
        settings=settings,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
    )
    citation_limit = max(1, int(getattr(settings, "takeover_citation_snippet_max_items", 20)))
    citation_ids = list(bundle.citations[:citation_limit])
    citation_snippets = (
        _build_citation_snippets_lite(
            conn,
            citation_ids=citation_ids,
            settings=settings,
            evidence_events=list(bundle.evidence_events),
        )
        if bool(getattr(settings, "takeover_rationale_enrichment_enabled", True))
        and bool(getattr(settings, "takeover_citation_snippets_enabled", True))
        else []
    )
    return {
        "summary": bundle.summary,
        "do": bundle.do_dont.get("do", [])[:5],
        "dont": bundle.do_dont.get("dont", [])[:5],
        "top_patterns": [
            {
                "id": str(pattern.id),
                "statement": pattern.statement,
                "confidence": float(pattern.confidence),
                "pattern_type": pattern.pattern_type,
            }
            for pattern in bundle.top_patterns[:8]
        ],
        "evidence_count": len(bundle.citations),
        "citations": [str(v) for v in citation_ids],
        "citation_snippets": citation_snippets,
        "graph_entities": len((bundle.structured_context.get("graph", {}) or {}).get("entities", []))
        if isinstance(bundle.structured_context, dict)
        else 0,
        "context_tier_used": bundle.context_tier_used,
        "summary_coverage": bundle.summary_coverage,
        "planner_used": bundle.planner_used,
        "subquery_count": bundle.subquery_count,
        "subquery_labels": bundle.subquery_labels,
        "episode_boost_applied": bundle.episode_boost_applied,
        "activation_boost_applied": bundle.activation_boost_applied,
        "refreshed_at": now_utc().isoformat(),
    }


def _record_takeover_action(
    conn: sqlite3.Connection,
    *,
    state: TakeoverState,
    turn: int,
    action_kind: str,
    result: str,
    latency_ms: int,
    meta: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO takeover_action_log(
            id, session_id, workspace_id, user_id, turn, objective_hash, action_kind, result, latency_ms, meta, ts
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            state.session_id,
            state.workspace_id,
            state.user_id,
            max(0, int(turn)),
            state.objective_hash,
            action_kind,
            result,
            max(0, int(latency_ms)),
            json_dumps(meta),
            now_utc().isoformat(),
        ),
    )
    conn.commit()


def _is_mutating_intent(message: str, task: str | None, final_response: str | None) -> bool:
    joined = " ".join([message or "", task or "", final_response or ""]).lower()
    hints = (
        "edit ",
        "change ",
        "fix ",
        "implement ",
        "update ",
        "refactor ",
        "rename ",
        "delete ",
        "write ",
        "create ",
    )
    return any(token in joined for token in hints)


def _clamp_confidence_lite(value: float, low: float = 0.05, high: float = 0.98) -> float:
    return max(low, min(high, float(value)))


def _workflow_domain_lite(workspace_id: str) -> str:
    return f"{workspace_id}:takeover"


def _pattern_status_from_confidence_lite(confidence: float) -> str:
    if confidence >= 0.72:
        return "active"
    if confidence >= 0.45:
        return "needs_review"
    return "suppressed"


def _evolve_execution_pattern_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    action_kind: str,
    objective_hash_value: str | None,
    execution_state: str,
) -> None:
    domain = _workflow_domain_lite(workspace_id)
    statement = f"procedure:{action_kind}:{(objective_hash_value or 'general')[:48]}"
    row = conn.execute(
        """
        SELECT id, confidence, version
        FROM patterns
        WHERE domain = ? AND pattern_type = ? AND statement = ?
        LIMIT 1
        """,
        (domain, "procedure", statement),
    ).fetchone()
    is_success = execution_state == DirectiveExecutionState.SUCCEEDED.value
    if execution_state in {DirectiveExecutionState.FAILED.value, DirectiveExecutionState.BLOCKED.value}:
        delta = -0.10
    elif is_success:
        delta = 0.08
    else:
        delta = -0.04
    now = now_utc().isoformat()
    if row:
        current_confidence = float(row["confidence"] or 0.5)
        next_confidence = _clamp_confidence_lite(current_confidence + delta)
        conn.execute(
            """
            UPDATE patterns
            SET confidence = ?, status = ?, updated_at = ?, version = ?
            WHERE id = ?
            """,
            (
                next_confidence,
                _pattern_status_from_confidence_lite(next_confidence),
                now,
                int(row["version"] or 1) + 1,
                row["id"],
            ),
        )
        return
    seed_confidence = 0.62 if is_success else 0.38
    conn.execute(
        """
        INSERT INTO patterns(id, domain, pattern_type, statement, evidence_event_ids, confidence, status, updated_at, version)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            str(uuid.uuid4()),
            domain,
            "procedure",
            statement,
            "[]",
            seed_confidence,
            _pattern_status_from_confidence_lite(seed_confidence),
            now,
        ),
    )


def _upsert_workflow_skill_template_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    action_kind: str,
    objective_hash_value: str | None,
    execution_state: str,
) -> None:
    domain = _workflow_domain_lite(workspace_id)
    name = f"{action_kind}::{(objective_hash_value or 'general')[:24]}"
    row = conn.execute(
        """
        SELECT id, graph, triggers, version
        FROM workflow_templates
        WHERE name = ? AND domain = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (name, domain),
    ).fetchone()
    is_success = execution_state == DirectiveExecutionState.SUCCEEDED.value
    now = now_utc().isoformat()
    default_step_templates = _default_step_templates_lite(action_kind)
    default_steps = [str(item.get("task") or "").strip() for item in default_step_templates if str(item.get("task") or "").strip()]
    if row:
        graph = json_loads(row["graph"], {})
        triggers = json_loads(row["triggers"], {})
        graph["success_count"] = int(graph.get("success_count") or 0) + (1 if is_success else 0)
        graph["failure_count"] = int(graph.get("failure_count") or 0) + (0 if is_success else 1)
        graph["last_result"] = execution_state
        raw_step_templates = graph.get("step_templates", [])
        normalized_step_templates: list[dict[str, Any]] = []
        if isinstance(raw_step_templates, list):
            for raw_template in raw_step_templates:
                normalized = _normalize_step_template_lite(raw_template)
                if normalized is not None:
                    normalized_step_templates.append(normalized)
        if not normalized_step_templates:
            normalized_step_templates = list(default_step_templates)
        graph["step_templates"] = normalized_step_templates
        graph["steps"] = graph.get("steps") or [str(item.get("task") or "").strip() for item in normalized_step_templates]
        triggers["objective_hash"] = objective_hash_value or triggers.get("objective_hash")
        triggers["action_kind"] = action_kind
        conn.execute(
            """
            UPDATE workflow_templates
            SET graph = ?, triggers = ?, version = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_dumps(graph), json_dumps(triggers), int(row["version"] or 1) + 1, now, row["id"]),
        )
        return
    graph = {
        "steps": default_steps,
        "step_templates": default_step_templates,
        "success_count": 1 if is_success else 0,
        "failure_count": 0 if is_success else 1,
        "last_result": execution_state,
    }
    triggers = {
        "workspace_id": workspace_id,
        "action_kind": action_kind,
        "objective_hash": objective_hash_value,
    }
    conn.execute(
        """
        INSERT INTO workflow_templates(id, name, domain, graph, triggers, version, updated_at)
        VALUES(?, ?, ?, ?, ?, 1, ?)
        """,
        (str(uuid.uuid4()), name, domain, json_dumps(graph), json_dumps(triggers), now),
    )


def _default_step_templates_lite(action_kind: str) -> list[dict[str, Any]]:
    normalized_action = str(action_kind or "takeover_step").strip() or "takeover_step"
    return [
        {
            "id": "research",
            "task": f"Analyze scope for {normalized_action}",
            "contract_type": "research_result",
            "expected_keys": ["findings", "sources"],
        },
        {
            "id": "change",
            "task": "Apply minimal change",
            "contract_type": "change_plan",
            "expected_keys": ["changes", "files"],
        },
        {
            "id": "verify",
            "task": "Validate outcome and capture feedback",
            "contract_type": "verification_result",
            "expected_keys": ["checks", "passed"],
        },
    ]


def _normalize_step_template_lite(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    task = str(raw.get("task") or "").strip()
    if not task:
        return None
    step_template: dict[str, Any] = {
        "id": str(raw.get("id") or "step").strip() or "step",
        "task": task[:220],
    }
    contract_type = str(raw.get("contract_type") or "").strip().lower()
    if contract_type in {"research_result", "change_plan", "verification_result"}:
        step_template["contract_type"] = contract_type
    expected_keys_raw = raw.get("expected_keys")
    if isinstance(expected_keys_raw, list):
        expected_keys = [
            str(item).strip()
            for item in expected_keys_raw
            if isinstance(item, str) and str(item).strip()
        ][:6]
        if expected_keys:
            step_template["expected_keys"] = expected_keys
    return step_template


def _template_reliability_lite(graph: dict[str, Any]) -> float:
    success_count = int(graph.get("success_count") or 0)
    failure_count = int(graph.get("failure_count") or 0)
    total = success_count + failure_count
    if total <= 0:
        return 0.0
    return float(success_count) / float(total)


def _load_learned_workflow_step_templates_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    limit: int = 3,
    min_reliability: float = 0.70,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT graph
        FROM workflow_templates
        WHERE domain = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (_workflow_domain_lite(workspace_id), max(1, limit)),
    ).fetchall()
    learned_templates: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    threshold = max(0.0, min(1.0, float(min_reliability)))
    for row in rows:
        graph = json_loads(row["graph"], {})
        if _template_reliability_lite(graph) < threshold:
            continue
        candidates: list[dict[str, Any]] = []
        raw_templates = graph.get("step_templates", [])
        if isinstance(raw_templates, list):
            for raw_template in raw_templates:
                normalized = _normalize_step_template_lite(raw_template)
                if normalized is not None:
                    candidates.append(normalized)
        if not candidates:
            fallback_steps = graph.get("steps", [])
            if isinstance(fallback_steps, list):
                for index, raw_step in enumerate(fallback_steps):
                    step = str(raw_step or "").strip()
                    if not step:
                        continue
                    template: dict[str, Any] = {"id": f"learned_{index + 1}", "task": step[:220]}
                    if index == 0:
                        template["contract_type"] = "research_result"
                        template["expected_keys"] = ["findings", "sources"]
                    elif index == 1:
                        template["contract_type"] = "change_plan"
                        template["expected_keys"] = ["changes", "files"]
                    elif index == 2:
                        template["contract_type"] = "verification_result"
                        template["expected_keys"] = ["checks", "passed"]
                    candidates.append(template)
        for template in candidates:
            task = str(template.get("task") or "").strip()
            key = task.lower()
            if not task or key in seen_tasks:
                continue
            seen_tasks.add(key)
            learned_templates.append(template)
            if len(learned_templates) >= 6:
                return learned_templates
    return learned_templates


def _is_complex_objective_text_lite(objective: str) -> bool:
    text_value = str(objective or "").strip().lower()
    if not text_value:
        return False
    if len(text_value) >= 90:
        return True
    separators = [",", ";", " and ", " then ", " after ", " while ", " -> "]
    signal_count = sum(1 for token in separators if token in text_value)
    if signal_count >= 2:
        return True
    action_verbs = ("fix", "implement", "migrate", "refactor", "configure", "verify", "deploy", "test")
    verb_hits = sum(1 for verb in action_verbs if f"{verb} " in text_value)
    return verb_hits >= 3


def _has_explicit_objective_signal_lite(message: str) -> bool:
    normalized = normalize_text(message)
    if not normalized:
        return False
    if normalized in {"ok", "hmm", "sure", "continue", "go on", "next"}:
        return False
    explicit_markers = ("new objective", "next objective", "objective:", "goal:")
    if any(marker in normalized for marker in explicit_markers):
        return True
    objective_verbs = (
        "fix ",
        "build ",
        "decide ",
        "define ",
        "implement ",
        "add ",
        "create ",
        "update ",
        "refactor ",
        "debug ",
        "investigate ",
        "research ",
        "design ",
        "write ",
        "review ",
        "test ",
    )
    if normalized.startswith(objective_verbs):
        return True
    return len(normalized.split()) >= 10 and any(token in normalized for token in objective_verbs)


def _objective_tokens_lite(value: str) -> set[str]:
    normalized = normalize_text(value)
    if not normalized:
        return set()
    parts = re.split(r"[^a-z0-9]+", normalized)
    return {part for part in parts if len(part) >= 3}


def _objective_overlap_score_lite(left: str, right: str) -> float:
    left_tokens = _objective_tokens_lite(left)
    right_tokens = _objective_tokens_lite(right)
    if not left_tokens or not right_tokens:
        return 0.0
    inter = len(left_tokens.intersection(right_tokens))
    union = len(left_tokens.union(right_tokens))
    if union <= 0:
        return 0.0
    return inter / union


def _build_structured_plan_payload_lite(
    *,
    objective: str,
    suggested_steps: list[str],
    learned_templates: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_steps: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for raw_template in learned_templates:
        template = _normalize_step_template_lite(raw_template)
        if template is None:
            continue
        task = str(template.get("task") or "").strip()
        key = task.lower()
        if not task or key in seen_tasks:
            continue
        seen_tasks.add(key)
        step: dict[str, Any] = {
            "task": task[:220],
            "status": "pending",
        }
        contract_type = str(template.get("contract_type") or "").strip().lower()
        if contract_type in {"research_result", "change_plan", "verification_result"}:
            step["contract_type"] = contract_type
        expected_keys_raw = template.get("expected_keys")
        if isinstance(expected_keys_raw, list):
            expected_keys = [
                str(value).strip()
                for value in expected_keys_raw
                if isinstance(value, str) and str(value).strip()
            ][:6]
            if expected_keys:
                step["expected_keys"] = expected_keys
        ordered_steps.append(step)
        if len(ordered_steps) >= 4:
            break
    for candidate in suggested_steps:
        text_value = str(candidate or "").strip()
        key = text_value.lower()
        if not text_value or key in seen_tasks:
            continue
        seen_tasks.add(key)
        ordered_steps.append({"task": text_value[:220], "status": "pending"})
        if len(ordered_steps) >= 4:
            break
    if not ordered_steps:
        ordered_steps = [
            {
                "task": "Scope the task and constraints",
                "status": "pending",
                "contract_type": "research_result",
                "expected_keys": ["findings", "sources"],
            },
            {
                "task": "Implement minimal, reversible changes",
                "status": "pending",
                "contract_type": "change_plan",
                "expected_keys": ["changes", "files"],
            },
            {
                "task": "Run build/test/lint verification and capture results",
                "status": "pending",
                "contract_type": "verification_result",
                "expected_keys": ["checks", "passed"],
            },
            {
                "task": "Update docs/notes and report execution outcome",
                "status": "pending",
            },
        ]
    return {
        "objective": str(objective or "")[:260],
        "steps": [
            {"order": index + 1, **step}
            for index, step in enumerate(ordered_steps)
        ],
        "generated_at": now_utc().isoformat(),
    }


def _goal_from_row(row: sqlite3.Row) -> TakeoverGoal:
    evidence_ids: list[UUID] = []
    for value in json_loads(row["evidence_event_ids"], []):
        try:
            evidence_ids.append(UUID(str(value)))
        except Exception:
            continue
    return TakeoverGoal(
        id=UUID(str(row["id"])),
        session_id=str(row["session_id"]),
        workspace_id=str(row["workspace_id"]),
        user_id=str(row["user_id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        source=AutonomyGoalSource(str(row["source"] or AutonomyGoalSource.OPEN_DISCOVERY.value)),
        priority_score=float(row["priority_score"] or 0.0),
        risk_tier=AutonomyRiskTier(str(row["risk_tier"] or AutonomyRiskTier.MEDIUM.value)),
        confidence=float(row["confidence"] or 0.0),
        reasoning=str(row["reasoning"] or ""),
        evidence_event_ids=evidence_ids,
        goal_kind=GoalKind(str(row["goal_kind"] or GoalKind.NORMAL.value)),
        affective_scores=json_loads(row["affective_scores"], {}),
        selection_score=float(row["selection_score"] or 0.0),
        cache_hit=bool(int(row["cache_hit"] or 0)),
        cache_source=str(row["cache_source"]) if row["cache_source"] is not None else None,
        status=AutonomyGoalStatus(str(row["status"] or AutonomyGoalStatus.CANDIDATE.value)),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _notice_from_row(row: sqlite3.Row) -> AutonomyNotice:
    return AutonomyNotice(
        id=UUID(str(row["id"])),
        session_id=str(row["session_id"]),
        workspace_id=str(row["workspace_id"]),
        user_id=str(row["user_id"]),
        goal_id=UUID(str(row["goal_id"])) if row["goal_id"] else None,
        title=str(row["title"] or ""),
        reason=str(row["reason"] or ""),
        priority=float(row["priority"] or 0.0),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        acknowledged_at=datetime.fromisoformat(row["acknowledged_at"]) if row["acknowledged_at"] else None,
    )


def _directive_from_row(row: sqlite3.Row) -> DirectiveExecution:
    return DirectiveExecution(
        directive_id=UUID(str(row["directive_id"])),
        session_id=str(row["session_id"]),
        workspace_id=str(row["workspace_id"]),
        user_id=str(row["user_id"]),
        goal_id=UUID(str(row["goal_id"])) if row["goal_id"] else None,
        objective_hash=str(row["objective_hash"]) if row["objective_hash"] else None,
        action_kind=str(row["action_kind"] or "execute"),
        attempt=int(row["attempt"] or 1),
        state=DirectiveExecutionState(str(row["state"] or DirectiveExecutionState.PENDING.value)),
        requires_permit=bool(int(row["requires_permit"] or 0)),
        permit_id=UUID(str(row["permit_id"])) if row["permit_id"] else None,
        claimed_by=str(row["claimed_by"]) if row["claimed_by"] else None,
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        failure_class=FailureClass(str(row["failure_class"])) if row["failure_class"] else None,
        failure_reason=str(row["failure_reason"]) if row["failure_reason"] else None,
        retry_strategy=RetryStrategy(str(row["retry_strategy"])) if row["retry_strategy"] else None,
        meta=json_loads(row["meta"], {}),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _load_pending_directive(
    conn: sqlite3.Connection,
    *,
    state: TakeoverState,
) -> DirectiveExecution | None:
    row = conn.execute(
        """
        SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
               attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
               failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
          AND state IN (?, ?)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (
            state.session_id,
            state.workspace_id,
            state.user_id,
            DirectiveExecutionState.PENDING.value,
            DirectiveExecutionState.IN_PROGRESS.value,
        ),
    ).fetchone()
    if row is None:
        return None
    directive = _directive_from_row(row)
    now_stamp = now_utc()
    # expires_at is the CLAIM-WINDOW deadline: it only reaps a PENDING
    # directive that was never claimed. An IN_PROGRESS directive is actively
    # being worked and may legitimately run past the claim TTL; it is reaped
    # only by the stale-work window below (keyed off started_at/updated_at).
    if (
        directive.state == DirectiveExecutionState.PENDING
        and directive.expires_at
        and directive.expires_at < now_stamp
    ):
        conn.execute(
            """
            UPDATE directive_executions
            SET state = ?, updated_at = ?, finished_at = ?, failure_reason = COALESCE(failure_reason, ?)
            WHERE directive_id = ?
            """,
            (
                DirectiveExecutionState.ABANDONED.value,
                now_stamp.isoformat(),
                now_stamp.isoformat(),
                "claim window expired",
                str(directive.directive_id),
            ),
        )
        conn.commit()
        return None
    stale_seconds = 900
    stale_anchor = directive.started_at or directive.updated_at or directive.created_at
    if (
        directive.state == DirectiveExecutionState.IN_PROGRESS
        and stale_anchor is not None
        and stale_anchor < (now_stamp - timedelta(seconds=stale_seconds))
    ):
        conn.execute(
            """
            UPDATE directive_executions
            SET state = ?, updated_at = ?, finished_at = ?, failure_reason = COALESCE(failure_reason, ?)
            WHERE directive_id = ?
            """,
            (
                DirectiveExecutionState.ABANDONED.value,
                now_stamp.isoformat(),
                now_stamp.isoformat(),
                "directive stale timeout",
                str(directive.directive_id),
            ),
        )
        conn.commit()
        return None
    return directive


def _sync_enforcement_counters(conn: sqlite3.Connection, state: TakeoverState) -> None:
    pending_directive_count_row = conn.execute(
        """
        SELECT COUNT(1) AS total
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
          AND state IN (?, ?)
        """,
        (
            state.session_id,
            state.workspace_id,
            state.user_id,
            DirectiveExecutionState.PENDING.value,
            DirectiveExecutionState.IN_PROGRESS.value,
        ),
    ).fetchone()
    retry_backlog_row = conn.execute(
        """
        SELECT COUNT(1) AS total
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
          AND state = ?
        """,
        (
            state.session_id,
            state.workspace_id,
            state.user_id,
            DirectiveExecutionState.PENDING.value,
        ),
    ).fetchone()
    state.pending_directive_count = int((pending_directive_count_row["total"] if pending_directive_count_row else 0) or 0)
    state.retry_backlog_count = int((retry_backlog_row["total"] if retry_backlog_row else 0) or 0)


def _goal_cache_key_for_state(state: TakeoverState, auth: AuthContext) -> str:
    objective_hash_value = state.objective_hash or objective_hash(str(state.takeover_context.get("objective", "")))
    state_version = f"{state.mode.value}:{state.autonomy_policy_profile.value}"
    return goal_cache_key(
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=state.session_id,
        objective_hash=objective_hash_value or "no-objective",
        state_version=state_version,
    )


def _load_goal_queue_cache(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    state: TakeoverState,
    settings: Settings,
) -> tuple[list[TakeoverGoal], str | None]:
    if not settings.takeover_goal_cache_enabled:
        return [], None
    key = _goal_cache_key_for_state(state, auth)
    l1_item, l1_state = goal_cache_get_l1(key)
    if l1_item:
        payload = l1_item.get("payload", {})
        goals_raw = (payload or {}).get("goals", [])
        goals = [TakeoverGoal.model_validate(item) for item in goals_raw if isinstance(item, dict)]
        return goals, "l1"
    if l1_state == "miss":
        row = conn.execute(
            """
            SELECT payload, expires_at
            FROM autonomy_goal_cache
            WHERE cache_key = ? AND expires_at >= ?
            LIMIT 1
            """,
            (key, now_utc().isoformat()),
        ).fetchone()
        if row:
            payload = goal_cache_deserialize(row["payload"])
            try:
                expires_at = datetime.fromisoformat(str(row["expires_at"]))
                ttl_seconds = max(1, int((expires_at - now_utc()).total_seconds()))
            except Exception:
                ttl_seconds = max(1, int(settings.takeover_goal_cache_l1_ttl_seconds))
            goal_cache_put_l1(key, payload, ttl_seconds=ttl_seconds, source="l2")
            goals_raw = payload.get("goals", [])
            goals = [TakeoverGoal.model_validate(item) for item in goals_raw if isinstance(item, dict)]
            return goals, "l2"
    return [], None


def _save_goal_queue_cache(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    state: TakeoverState,
    goals: list[TakeoverGoal],
    settings: Settings,
) -> None:
    if not settings.takeover_goal_cache_enabled:
        return
    key = _goal_cache_key_for_state(state, auth)
    created_at = now_utc()
    l2_ttl = max(1, int(settings.takeover_goal_cache_l2_ttl_seconds))
    expires_at = created_at + timedelta(seconds=l2_ttl)
    payload = {
        "goals": [goal.model_dump(mode="json") for goal in goals],
        "session_id": state.session_id,
        "workspace_id": auth.workspace_id,
        "user_id": auth.user_id,
        "objective_hash": state.objective_hash,
        "generated_at": created_at.isoformat(),
    }
    goal_cache_put_l1(
        key,
        payload,
        ttl_seconds=max(1, int(settings.takeover_goal_cache_l1_ttl_seconds)),
        source="computed",
    )
    conn.execute(
        """
        INSERT INTO autonomy_goal_cache(
            cache_key, session_id, workspace_id, user_id, payload, cache_version,
            created_at, expires_at, last_accessed_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            payload = excluded.payload,
            cache_version = excluded.cache_version,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at,
            last_accessed_at = excluded.last_accessed_at
        """,
        (
            key,
            state.session_id,
            auth.workspace_id,
            auth.user_id,
            json_dumps(payload),
            1,
            created_at.isoformat(),
            expires_at.isoformat(),
            created_at.isoformat(),
        ),
    )


def invalidate_takeover_goal_cache(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
    objective_hash_value: str | None = None,
) -> dict[str, Any]:
    _ = objective_hash_value
    removed_l1 = goal_cache_invalidate_l1(None)
    removed_l2 = conn.execute(
        """
        DELETE FROM autonomy_goal_cache
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (session_id, auth.workspace_id, auth.user_id),
    ).rowcount
    conn.commit()
    return {
        "session_id": session_id,
        "removed": int((removed_l1 or 0) + (removed_l2 or 0)),
        "invalidated_at": now_utc().isoformat(),
    }


def takeover_goal_cache_status(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
) -> dict[str, Any]:
    state = takeover_state(
        conn=conn,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    key = _goal_cache_key_for_state(state, auth)
    now = now_utc()
    l1_item, l1_state = goal_cache_get_l1(key)
    if l1_item:
        try:
            created_at = datetime.fromisoformat(str(l1_item.get("created_at")))
        except Exception:
            created_at = now
        try:
            expires_at = datetime.fromisoformat(str(l1_item.get("expires_at")))
        except Exception:
            expires_at = now
        l1 = {
            "state": "hit",
            "source": l1_item.get("source", "l1"),
            "fresh": expires_at >= now,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "age_seconds": max(0, int((now - created_at).total_seconds())),
            "ttl_seconds": max(0, int((expires_at - now).total_seconds())),
        }
    else:
        l1 = {"state": l1_state or "miss", "source": None, "fresh": False}

    row = conn.execute(
        """
        SELECT payload, cache_version, created_at, expires_at, last_accessed_at
        FROM autonomy_goal_cache
        WHERE cache_key = ?
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    if row:
        payload = goal_cache_deserialize(row["payload"])
        try:
            created_at = datetime.fromisoformat(str(row["created_at"]))
        except Exception:
            created_at = now
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
        except Exception:
            expires_at = now
        try:
            last_accessed = datetime.fromisoformat(str(row["last_accessed_at"]))
        except Exception:
            last_accessed = now
        l2 = {
            "present": True,
            "fresh": expires_at >= now,
            "goal_count": len((payload or {}).get("goals", [])),
            "cache_version": int(row["cache_version"] or 0),
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_accessed_at": last_accessed.isoformat(),
            "age_seconds": max(0, int((now - created_at).total_seconds())),
            "ttl_seconds": max(0, int((expires_at - now).total_seconds())),
        }
    else:
        l2 = {"present": False, "fresh": False}

    return {
        "session_id": state.session_id,
        "objective_hash": state.objective_hash,
        "cache_key": key,
        "l1": l1,
        "l2": l2,
        "generated_at": now.isoformat(),
    }


def discover_takeover_goals(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
    include_open_discovery: bool = True,
    settings: Settings | None = None,
    force_recompute: bool = False,
) -> list[TakeoverGoal]:
    effective_settings = settings or Settings()
    now = now_utc()
    state = takeover_state(
        conn=conn,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    if not force_recompute:
        cached_goals, cache_source = _load_goal_queue_cache(conn, auth=auth, state=state, settings=effective_settings)
        if cached_goals:
            state.takeover_context["_goal_cache_hit"] = True
            state.takeover_context["_goal_cache_source"] = cache_source or "l2"
            state.goal_queue_size = len(cached_goals)
            state.last_discovery_at = now
            save_takeover_state(conn, state)
            conn.commit()
            return cached_goals
    state.takeover_context["_goal_cache_hit"] = False
    state.takeover_context["_goal_cache_source"] = "computed"

    fingerprint: dict[str, Any] | None = None
    fp_row = conn.execute(
        """
        SELECT fingerprint
        FROM behavioral_fingerprints
        WHERE workspace_id = ?
        ORDER BY CASE WHEN consumer_id = ? THEN 1 ELSE 0 END DESC, last_updated_at DESC
        LIMIT 1
        """,
        (auth.workspace_id, auth.consumer),
    ).fetchone()
    if fp_row is not None:
        fingerprint = json_loads(fp_row["fingerprint"], {})

    candidates: dict[str, dict[str, Any]] = {}
    objective = str(state.takeover_context.get("objective", "")).strip()
    if objective:
        urgency = 0.9
        recency = 0.85
        blocker_impact = 0.8
        success_probability = 0.75
        candidates[f"user:{objective.lower()}"] = {
            "title": objective[:140],
            "description": objective[:240],
            "source": AutonomyGoalSource.USER_OBJECTIVE.value,
            "priority_score": score_goal(urgency, recency, blocker_impact, success_probability),
            "risk_tier": AutonomyRiskTier.MEDIUM.value,
            "confidence": 0.88,
            "reasoning": "Active user objective preserved as top priority.",
            "evidence_event_ids": [],
            "urgency": urgency,
            "recency": recency,
            "blocker_impact": blocker_impact,
            "success_probability": success_probability,
        }
    if include_open_discovery:
        rows = conn.execute(
            """
            SELECT id, ts, event_type, title
            FROM events
            WHERE (json_extract(context, '$._tce_workspace') IS NULL OR json_extract(context, '$._tce_workspace') = ?)
              AND sensitivity <= ?
            ORDER BY ts DESC
            LIMIT 120
            """,
            (auth.workspace_id, 2),
        ).fetchall()
        for row in rows:
            title = str(row["title"] or "").strip()
            if not title:
                continue
            event_type = str(row["event_type"] or "").upper()
            try:
                ts_val = datetime.fromisoformat(str(row["ts"]))
            except Exception:
                ts_val = now
            age_days = max(0.0, (now - ts_val).total_seconds() / 86400.0)
            recency = max(0.0, min(1.0, 1.0 - (age_days / 14.0)))
            urgency = 0.9 if event_type == "ERROR" else 0.65
            blocker_impact = 0.85 if event_type == "ERROR" else 0.55
            success_probability = 0.68
            confidence = 0.7 if event_type == "ERROR" else 0.58
            key = f"event:{title.lower()}"
            safe_title = sanitize_untrusted_objective(title, max_len=140)
            candidates[key] = {
                "title": safe_title,
                "description": sanitize_untrusted_objective(
                    f"Investigate and resolve: {safe_title}", max_len=180
                ),
                "source": AutonomyGoalSource.OPEN_DISCOVERY.value,
                "priority_score": score_goal(urgency, recency, blocker_impact, success_probability),
                "risk_tier": AutonomyRiskTier.HIGH.value if event_type == "ERROR" else AutonomyRiskTier.MEDIUM.value,
                "confidence": confidence,
                "reasoning": f"Derived from recent {event_type or 'event'} in workspace timeline.",
                "evidence_event_ids": [str(row["id"])],
                "urgency": urgency,
                "recency": recency,
                "blocker_impact": blocker_impact,
                "success_probability": success_probability,
            }
        action_rows = conn.execute(
            """
            SELECT action_kind, result
            FROM takeover_action_log
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
            ORDER BY ts DESC
            LIMIT 40
            """,
            (session_id, auth.workspace_id, auth.user_id),
        ).fetchall()
        for row in action_rows:
            result = str(row["result"] or "").strip().lower()
            if result not in {"blocked", "failure", "needs_human"}:
                continue
            action_kind = str(row["action_kind"] or "work").replace("_", " ").strip()
            title = f"Unblock {action_kind}"[:140]
            key = f"block:{title.lower()}"
            if key in candidates:
                continue
            urgency = 0.86
            recency = 0.78
            blocker_impact = 0.9
            success_probability = 0.52
            candidates[key] = {
                "title": title,
                "description": f"Address repeated blocker in takeover flow: {action_kind}.",
                "source": AutonomyGoalSource.OPEN_DISCOVERY.value,
                "priority_score": score_goal(urgency, recency, blocker_impact, success_probability),
                "risk_tier": AutonomyRiskTier.MEDIUM.value,
                "confidence": 0.74,
                "reasoning": "Created from repeated blocked/failed takeover actions.",
                "evidence_event_ids": [],
                "urgency": urgency,
                "recency": recency,
                "blocker_impact": blocker_impact,
                "success_probability": success_probability,
            }
    conn.execute(
        """
        UPDATE autonomy_goals
        SET status = ?, updated_at = ?
        WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND status = ?
          -- Plan rows are authored once and walked to completion, so discovery must
          -- never drop them. Mirrors the Full backend.
          AND step_index IS NULL
        """,
        (
            AutonomyGoalStatus.DROPPED.value,
            now.isoformat(),
            session_id,
            auth.workspace_id,
            auth.user_id,
            AutonomyGoalStatus.CANDIDATE.value,
        ),
    )
    prepared = dedupe_candidates_by_similarity(list(candidates.values()))
    for item in prepared:
        affective_scores = compute_affective_scores(
            source=str(item.get("source", AutonomyGoalSource.OPEN_DISCOVERY.value)),
            evidence_count=len(item.get("evidence_event_ids", []) or []),
            rehearsal_count=len(item.get("evidence_event_ids", []) or []),
            confidence=float(item.get("confidence", 0.6)),
            urgency=float(item.get("urgency", 0.65)),
            recency=float(item.get("recency", 0.65)),
            blocker_impact=float(item.get("blocker_impact", 0.55)),
            success_probability=float(item.get("success_probability", 0.68)),
            recent_outcomes=state.recent_outcomes_json,
            fingerprint=fingerprint,
            similarity={
                "context_relevance": float(item.get("confidence", 0.6)),
                "recent_event_affinity": float(item.get("recency", 0.6)),
                "entity_overlap": min(1.0, len(item.get("evidence_event_ids", []) or []) / 8.0),
                "pattern_confidence": float(item.get("confidence", 0.6)),
                "nearest_goal_distance": 0.7,
                "evidence_depth": min(1.0, len(item.get("evidence_event_ids", []) or []) / 12.0),
            },
        )
        affective_priority, _ = score_goal_affective(
            urgency=float(item.get("urgency", 0.65)),
            recency=float(item.get("recency", 0.65)),
            blocker_impact=float(item.get("blocker_impact", 0.55)),
            success_probability=float(item.get("success_probability", 0.68)),
            affective_scores=affective_scores,
        )
        item["selection_score"] = affective_priority
        item["goal_kind"] = classify_goal_kind(affective_scores)
        item["affective_scores"] = affective_scores

    ranked = sorted(
        prepared,
        key=lambda item: (float(item.get("selection_score", item["priority_score"])), float(item["confidence"])),
        reverse=True,
    )[:20]
    created: list[TakeoverGoal] = []
    for item in ranked:
        goal_id = str(uuid.uuid4())
        goal_signature = hashlib.sha256(
            f"{item.get('title', '')}|{item.get('description', '')}".encode()
        ).hexdigest()[:24]
        conn.execute(
            """
            INSERT INTO autonomy_goals(
                id, session_id, workspace_id, user_id, title, description, source,
                priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                goal_kind, affective_scores, selection_score, goal_signature, cache_hit, cache_source,
                status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                goal_id,
                session_id,
                auth.workspace_id,
                auth.user_id,
                item["title"],
                item["description"],
                item["source"],
                float(item["priority_score"]),
                item["risk_tier"],
                float(item["confidence"]),
                item["reasoning"],
                json_dumps(item["evidence_event_ids"]),
                str(item.get("goal_kind", GoalKind.NORMAL.value)),
                json_dumps(item.get("affective_scores", {})),
                float(item.get("selection_score", item["priority_score"])),
                goal_signature,
                0,
                "computed",
                AutonomyGoalStatus.CANDIDATE.value,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        evidence_uuid: list[UUID] = []
        for value in item["evidence_event_ids"]:
            try:
                evidence_uuid.append(UUID(str(value)))
            except Exception:
                continue
        created.append(
            TakeoverGoal(
                id=UUID(goal_id),
                session_id=session_id,
                workspace_id=auth.workspace_id,
                user_id=auth.user_id,
                title=item["title"],
                description=item["description"],
                source=AutonomyGoalSource(str(item["source"])),
                priority_score=float(item["priority_score"]),
                risk_tier=AutonomyRiskTier(str(item["risk_tier"])),
                confidence=float(item["confidence"]),
                reasoning=item["reasoning"],
                evidence_event_ids=evidence_uuid,
                goal_kind=GoalKind(str(item.get("goal_kind", GoalKind.NORMAL.value))),
                affective_scores=item.get("affective_scores", {}),
                selection_score=float(item.get("selection_score", item["priority_score"])),
                cache_hit=False,
                cache_source="computed",
                status=AutonomyGoalStatus.CANDIDATE,
                created_at=now,
                updated_at=now,
            )
        )
    state.goal_queue_size = len(created)
    state.last_discovery_at = now
    save_takeover_state(conn, state)
    _save_goal_queue_cache(conn, auth=auth, state=state, goals=created, settings=effective_settings)
    conn.commit()
    return created


def list_takeover_goals(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
    status: AutonomyGoalStatus | None = None,
) -> list[TakeoverGoal]:
    if status is None:
        rows = conn.execute(
            """
            SELECT id, session_id, workspace_id, user_id, title, description, source,
                   priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                   goal_kind, affective_scores, selection_score, cache_hit, cache_source,
                   status, created_at, updated_at
            FROM autonomy_goals
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
            ORDER BY selection_score DESC, priority_score DESC, updated_at DESC
            LIMIT 50
            """,
            (session_id, auth.workspace_id, auth.user_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, session_id, workspace_id, user_id, title, description, source,
                   priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                   goal_kind, affective_scores, selection_score, cache_hit, cache_source,
                   status, created_at, updated_at
            FROM autonomy_goals
            WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND status = ?
            ORDER BY selection_score DESC, priority_score DESC, updated_at DESC
            LIMIT 50
            """,
            (session_id, auth.workspace_id, auth.user_id, status.value),
        ).fetchall()
    return [_goal_from_row(row) for row in rows]


def select_takeover_goal(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
    goal_id: UUID,
) -> TakeoverGoal:
    now = now_utc().isoformat()
    conn.execute(
        """
        UPDATE autonomy_goals
        SET status = ?, updated_at = ?
        WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND status IN (?, ?)
        """,
        (
            AutonomyGoalStatus.CANDIDATE.value,
            now,
            session_id,
            auth.workspace_id,
            auth.user_id,
            AutonomyGoalStatus.SELECTED.value,
            AutonomyGoalStatus.EXECUTING.value,
        ),
    )
    conn.execute(
        """
        UPDATE autonomy_goals
        SET status = ?, updated_at = ?
        WHERE id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (
            AutonomyGoalStatus.SELECTED.value,
            now,
            str(goal_id),
            session_id,
            auth.workspace_id,
            auth.user_id,
        ),
    )
    row = conn.execute(
        """
        SELECT id, session_id, workspace_id, user_id, title, description, source,
               priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
               goal_kind, affective_scores, selection_score, cache_hit, cache_source,
               status, created_at, updated_at
        FROM autonomy_goals
        WHERE id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
        LIMIT 1
        """,
        (str(goal_id), session_id, auth.workspace_id, auth.user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="goal not found for session")
    state = takeover_state(conn, session_id, auth.workspace_id, auth.user_id)
    state.active_goal_id = goal_id
    save_takeover_state(conn, state)
    goal_cache_invalidate_l1(None)
    conn.execute(
        """
        DELETE FROM autonomy_goal_cache
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (session_id, auth.workspace_id, auth.user_id),
    )
    conn.commit()
    return _goal_from_row(row)


def request_execution_permit_lite(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: ExecutionPermitRequest,
    policy_profile: AutonomyPolicyProfile,
    permit_ttl_seconds: int,
    confirm_keyword: str = "confirm",
) -> ExecutionPermitResponse:
    now = now_utc()
    risk_tier = classify_risk_tier(
        action_kind=body.action_kind,
        target_paths=body.target_paths,
        command_preview=body.command_preview,
        estimated_change_size=body.estimated_change_size,
    )
    sensitive_hit = any(
        any(token in path.lower() for token in ("services/tce_mcp", "infra/", "secrets", ".env"))
        for path in body.target_paths
    )
    decision, reason = evaluate_execution_permit(
        policy_profile=policy_profile,
        risk_tier=risk_tier,
        estimated_change_size=body.estimated_change_size,
        role=auth.role.value,
        sensitive_path_hit=sensitive_hit,
    )
    permit_id = uuid.uuid4()
    expires_at = now + timedelta(seconds=max(30, int(permit_ttl_seconds)))
    conn.execute(
        """
        INSERT INTO execution_permits(
            id, session_id, workspace_id, action_kind, target_paths, command_preview,
            estimated_change_size, decision, reason, confirmed_by, expires_at, created_at, resolved_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(permit_id),
            body.session_id,
            auth.workspace_id,
            body.action_kind,
            json_dumps(body.target_paths),
            body.command_preview,
            int(body.estimated_change_size),
            decision.value,
            reason,
            None,
            expires_at.isoformat(),
            now.isoformat(),
            None,
        ),
    )
    conn.commit()
    return ExecutionPermitResponse(
        decision=decision,
        reason=reason,
        permit_id=permit_id,
        expires_at=expires_at,
        required_confirmation=confirm_keyword if decision == ExecutionPermitDecision.CONFIRM_REQUIRED else None,
    )


def resolve_execution_permit_lite(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: ExecutionPermitResolveRequest,
    confirm_keyword: str = "confirm",
) -> ExecutionPermitResponse:
    row = conn.execute(
        """
        SELECT id, session_id, decision, reason, expires_at
        FROM execution_permits
        WHERE id = ? AND workspace_id = ?
        LIMIT 1
        """,
        (str(body.permit_id), auth.workspace_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="permit not found")
    expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
    if expires_at and expires_at < now_utc():
        decision = ExecutionPermitDecision.BLOCKED
        reason = "permit expired"
    else:
        decision = ExecutionPermitDecision.ALLOW if body.approved else ExecutionPermitDecision.BLOCKED
        reason = "confirmed by operator" if body.approved else "denied by operator"
    conn.execute(
        """
        UPDATE execution_permits
        SET decision = ?, reason = ?, confirmed_by = ?, resolved_at = ?
        WHERE id = ?
        """,
        (
            decision.value,
            reason,
            body.confirmed_by or auth.user_id,
            now_utc().isoformat(),
            str(body.permit_id),
        ),
    )
    goal_cache_invalidate_l1(None)
    conn.execute(
        """
        DELETE FROM autonomy_goal_cache
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (str(row["session_id"]), auth.workspace_id, auth.user_id),
    )
    conn.commit()
    return ExecutionPermitResponse(
        decision=decision,
        reason=reason,
        permit_id=body.permit_id,
        expires_at=expires_at,
        required_confirmation=confirm_keyword if decision == ExecutionPermitDecision.CONFIRM_REQUIRED else None,
    )


def takeover_autonomy_status(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
) -> TakeoverAutonomyStatusResponse:
    state = takeover_state(conn, session_id, auth.workspace_id, auth.user_id)
    active_goal = None
    if state.active_goal_id:
        goal_row = conn.execute(
            """
            SELECT id, session_id, workspace_id, user_id, title, description, source,
                   priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                   goal_kind, affective_scores, selection_score, cache_hit, cache_source,
                   status, created_at, updated_at
            FROM autonomy_goals
            WHERE id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
            LIMIT 1
            """,
            (str(state.active_goal_id), session_id, auth.workspace_id, auth.user_id),
        ).fetchone()
        if goal_row is not None:
            active_goal = _goal_from_row(goal_row)
    pending_row = conn.execute(
        """
        SELECT COUNT(1) AS total
        FROM execution_permits
        WHERE session_id = ? AND workspace_id = ? AND decision = ?
          AND (expires_at IS NULL OR expires_at >= ?)
        """,
        (
            session_id,
            auth.workspace_id,
            ExecutionPermitDecision.CONFIRM_REQUIRED.value,
            now_utc().isoformat(),
        ),
    ).fetchone()
    return TakeoverAutonomyStatusResponse(
        session_id=session_id,
        state=state,
        active_goal=active_goal,
        queue_size=int(state.goal_queue_size or 0),
        pending_permit_count=int((pending_row["total"] if pending_row else 0) or 0),
        pending_directive_count=int(state.pending_directive_count or 0),
        open_notice_count=int(
            (
                conn.execute(
                    """
                    SELECT COUNT(1) AS total
                    FROM autonomy_notices
                    WHERE session_id = ? AND workspace_id = ? AND user_id = ?
                      AND acknowledged_at IS NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """,
                    (session_id, auth.workspace_id, auth.user_id, now_utc().isoformat()),
                ).fetchone()["total"]
            )
            or 0
        ),
        retry_backlog_count=int(state.retry_backlog_count or 0),
        enforcement_mode=str(state.enforcement_mode or "strict_takeover"),
        continuity_ok=int(state.continuity_violation_count or 0) < 3,
        generated_at=now_utc(),
    )


def takeover_autonomy_tick(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: TakeoverAutonomyTickRequest,
    settings: Settings,
) -> TakeoverAutonomyTickResponse:
    now = now_utc()
    session_rows: list[sqlite3.Row]
    if body.session_id:
        session_rows = conn.execute(
            """
            SELECT session_id
            FROM takeover_sessions
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
            LIMIT 1
            """,
            (body.session_id, auth.workspace_id, auth.user_id),
        ).fetchall()
    else:
        session_rows = conn.execute(
            """
            SELECT session_id
            FROM takeover_sessions
            WHERE workspace_id = ? AND user_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (auth.workspace_id, auth.user_id, int(body.max_sessions)),
        ).fetchall()
    sessions = [str(row["session_id"]) for row in session_rows]
    goals_refreshed = 0
    notices_created = 0
    dedupe_cutoff = (now - timedelta(minutes=max(1, int(settings.takeover_notice_dedupe_minutes)))).isoformat()
    threshold = float(settings.takeover_notice_threshold)

    for session_id in sessions:
        state = takeover_state(conn, session_id, auth.workspace_id, auth.user_id)
        goals = discover_takeover_goals(
            conn,
            auth=auth,
            session_id=session_id,
            include_open_discovery=body.include_open_discovery,
            settings=settings,
        )
        goals_refreshed += 1
        top_goal = goals[0] if goals else None
        if (
            top_goal is not None
            and not state.active_goal_id
            and float(top_goal.selection_score or top_goal.priority_score or 0.0) >= threshold
        ):
            existing = conn.execute(
                """
                SELECT id
                FROM autonomy_notices
                WHERE session_id = ? AND workspace_id = ? AND user_id = ?
                  AND goal_id = ?
                  AND acknowledged_at IS NULL
                  AND created_at >= ?
                LIMIT 1
                """,
                (
                    session_id,
                    auth.workspace_id,
                    auth.user_id,
                    str(top_goal.id),
                    dedupe_cutoff,
                ),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO autonomy_notices(
                        id, session_id, workspace_id, user_id, goal_id, title, reason,
                        priority, expires_at, created_at, acknowledged_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        session_id,
                        auth.workspace_id,
                        auth.user_id,
                        str(top_goal.id),
                        f"Next suggested goal: {top_goal.title[:140]}",
                        "Proactive discovery found a high-priority actionable goal.",
                        float(top_goal.selection_score or top_goal.priority_score or 0.0),
                        (now + timedelta(minutes=max(10, int(settings.takeover_notice_dedupe_minutes)))).isoformat(),
                        now.isoformat(),
                        None,
                    ),
                )
                notices_created += 1
        state.last_tick_at = now
        state.enforcement_mode = settings.takeover_enforcement_mode
        _sync_enforcement_counters(conn, state)
        save_takeover_state(conn, state)
    conn.commit()
    return TakeoverAutonomyTickResponse(
        sessions_scanned=len(sessions),
        goals_refreshed=goals_refreshed,
        notices_created=notices_created,
        generated_at=now,
    )


def list_takeover_notices(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
) -> TakeoverNoticesResponse:
    rows = conn.execute(
        """
        SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
               expires_at, created_at, acknowledged_at
        FROM autonomy_notices
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
          AND acknowledged_at IS NULL
          AND (expires_at IS NULL OR expires_at >= ?)
        ORDER BY priority DESC, created_at DESC
        LIMIT 50
        """,
        (session_id, auth.workspace_id, auth.user_id, now_utc().isoformat()),
    ).fetchall()
    return TakeoverNoticesResponse(
        session_id=session_id,
        notices=[_notice_from_row(row) for row in rows],
        generated_at=now_utc(),
    )


def acknowledge_takeover_notice(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    notice_id: UUID,
    body: TakeoverNoticeAckRequest,
) -> AutonomyNotice:
    row = conn.execute(
        """
        SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
               expires_at, created_at, acknowledged_at
        FROM autonomy_notices
        WHERE id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
        LIMIT 1
        """,
        (str(notice_id), body.session_id, auth.workspace_id, auth.user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="notice not found")
    now = now_utc()
    conn.execute(
        """
        UPDATE autonomy_notices
        SET acknowledged_at = ?
        WHERE id = ?
        """,
        (now.isoformat(), str(notice_id)),
    )
    if body.select_goal and row["goal_id"]:
        select_takeover_goal(
            conn,
            auth=auth,
            session_id=body.session_id,
            goal_id=UUID(str(row["goal_id"])),
        )
    updated = conn.execute(
        """
        SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
               expires_at, created_at, acknowledged_at
        FROM autonomy_notices
        WHERE id = ?
        LIMIT 1
        """,
        (str(notice_id),),
    ).fetchone()
    conn.commit()
    return _notice_from_row(updated)


def claim_execution(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: ExecutionClaimRequest,
    settings: Settings,
) -> DirectiveExecution:
    state = takeover_state(conn, body.session_id, auth.workspace_id, auth.user_id)
    if body.directive_id:
        row = conn.execute(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE directive_id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
            LIMIT 1
            """,
            (str(body.directive_id), body.session_id, auth.workspace_id, auth.user_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND state = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (body.session_id, auth.workspace_id, auth.user_id, DirectiveExecutionState.PENDING.value),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no pending directive found")
    directive = _directive_from_row(row)
    now = now_utc()
    claimer = body.claimed_by or auth.user_id
    if directive.state == DirectiveExecutionState.IN_PROGRESS:
        if directive.claimed_by and directive.claimed_by != claimer:
            raise HTTPException(status_code=409, detail="directive already claimed by another actor")
        return directive
    if directive.state in {
        DirectiveExecutionState.SUCCEEDED,
        DirectiveExecutionState.FAILED,
        DirectiveExecutionState.BLOCKED,
        DirectiveExecutionState.ABANDONED,
    }:
        return directive

    permit_id = directive.permit_id
    if directive.requires_permit:
        if permit_id is None:
            permit_lookup = conn.execute(
                """
                SELECT id
                FROM execution_permits
                WHERE session_id = ? AND workspace_id = ? AND decision = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY COALESCE(resolved_at, created_at) DESC
                LIMIT 1
                """,
                (
                    body.session_id,
                    auth.workspace_id,
                    ExecutionPermitDecision.ALLOW.value,
                    now.isoformat(),
                ),
            ).fetchone()
            if permit_lookup is None:
                raise HTTPException(status_code=409, detail="directive requires permit but no permit_id attached")
            permit_id = UUID(str(permit_lookup["id"]))
            conn.execute(
                """
                UPDATE directive_executions
                SET permit_id = ?, updated_at = ?
                WHERE directive_id = ?
                """,
                (str(permit_id), now.isoformat(), str(directive.directive_id)),
            )
            directive.permit_id = permit_id
        permit_row = conn.execute(
            """
            SELECT decision, expires_at
            FROM execution_permits
            WHERE id = ? AND session_id = ? AND workspace_id = ?
            LIMIT 1
            """,
            (str(permit_id), body.session_id, auth.workspace_id),
        ).fetchone()
        if permit_row is None or str(permit_row["decision"]) != ExecutionPermitDecision.ALLOW.value:
            raise HTTPException(status_code=409, detail="permit not approved")
        permit_expires = datetime.fromisoformat(permit_row["expires_at"]) if permit_row["expires_at"] else None
        if permit_expires and permit_expires <= now:
            raise HTTPException(status_code=409, detail="permit expired")
    else:
        permit_expires = None

    claim_expires = now + timedelta(seconds=max(30, int(settings.takeover_execution_claim_ttl_seconds)))
    if permit_expires is not None and permit_expires < claim_expires:
        claim_expires = permit_expires
    conn.execute(
        """
        UPDATE directive_executions
        SET state = ?, claimed_by = ?, started_at = COALESCE(started_at, ?), expires_at = ?, updated_at = ?
        WHERE directive_id = ?
        """,
        (
            DirectiveExecutionState.IN_PROGRESS.value,
            claimer,
            now.isoformat(),
            claim_expires.isoformat(),
            now.isoformat(),
            str(directive.directive_id),
        ),
    )
    state.enforcement_mode = settings.takeover_enforcement_mode
    _sync_enforcement_counters(conn, state)
    save_takeover_state(conn, state)
    updated = conn.execute(
        """
        SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
               attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
               failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
        FROM directive_executions
        WHERE directive_id = ?
        LIMIT 1
        """,
        (str(directive.directive_id),),
    ).fetchone()
    conn.commit()
    return _directive_from_row(updated)


def _merge_execution_report_details_lite(body: ExecutionReportRequest) -> dict[str, Any]:
    details_map = dict(body.details or {}) if isinstance(body.details, dict) else {}
    if body.step_id and "step_id" not in details_map:
        details_map["step_id"] = body.step_id
    if body.step_output is not None and "step_output" not in details_map:
        details_map["step_output"] = body.step_output
    if body.contract_type and "contract_type" not in details_map:
        details_map["contract_type"] = body.contract_type
    return details_map


def _validate_dependency_plan_lite(dependency_plan: Any) -> tuple[bool, str | None]:
    if not isinstance(dependency_plan, dict):
        return False, "dependency_plan must be an object"
    steps = dependency_plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return False, "dependency_plan.steps must be a non-empty array"
    ids: set[str] = set()
    graph: dict[str, set[str]] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return False, f"dependency_plan.steps[{index}] must be an object"
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            return False, f"dependency_plan.steps[{index}].id is required"
        if step_id in ids:
            return False, f"duplicate dependency step id: {step_id}"
        ids.add(step_id)
        depends = step.get("depends_on", [])
        if depends is None:
            depends = []
        if not isinstance(depends, list):
            return False, f"dependency_plan.steps[{index}].depends_on must be an array"
        graph[step_id] = {str(item).strip() for item in depends if str(item).strip()}
    for step_id, deps in graph.items():
        dangling = sorted(dep for dep in deps if dep not in ids)
        if dangling:
            return False, f"dangling dependency for {step_id}: {', '.join(dangling)}"
    visiting: set[str] = set()
    visited: set[str] = set()

    def _has_cycle(node: str) -> bool:
        if node in visited:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in graph.get(node, set()):
            if _has_cycle(dep):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for node in graph:
        if _has_cycle(node):
            return False, "dependency cycle detected"
    return True, None


def _validate_typed_contract_lite(settings: Settings, details_map: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(getattr(settings, "typed_contract_enabled", False)):
        return None
    contract_type = str(details_map.get("contract_type") or "").strip().lower()
    if not contract_type:
        return None
    step_id = str(details_map.get("step_id") or "").strip()
    step_output = details_map.get("step_output")
    errors: list[str] = []
    if not step_id:
        errors.append("step_id is required")
    if not isinstance(step_output, dict):
        errors.append("step_output must be an object")
    if contract_type not in {"research_result", "change_plan", "verification_result"}:
        errors.append(f"unsupported contract_type: {contract_type}")
    elif isinstance(step_output, dict):
        if contract_type == "research_result":
            if not isinstance(step_output.get("findings"), list):
                errors.append("research_result.step_output.findings must be a list")
            if not isinstance(step_output.get("sources"), list):
                errors.append("research_result.step_output.sources must be a list")
        elif contract_type == "change_plan":
            if not isinstance(step_output.get("changes"), list):
                errors.append("change_plan.step_output.changes must be a list")
            if not isinstance(step_output.get("files"), list):
                errors.append("change_plan.step_output.files must be a list")
        elif contract_type == "verification_result":
            checks = step_output.get("checks")
            if not isinstance(checks, dict):
                errors.append("verification_result.step_output.checks must be an object")
            if "passed" not in step_output:
                errors.append("verification_result.step_output.passed is required")
    return {
        "enabled": True,
        "contract_type": contract_type,
        "valid": len(errors) == 0,
        "errors": errors,
    }


def _latest_editor_checkpoint_anchor_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT ts, payload
        FROM events
        WHERE task_type = ?
          AND event_type = ?
          AND json_extract(context, '$._tce_workspace') = ?
          AND json_extract(context, '$._tce_owner') = ?
          AND json_extract(context, '$.session_id') = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        ("editor_checkpoint", EventType.TASK_STEP.value, workspace_id, owner_id, session_id),
    ).fetchone()
    if row is None:
        return None
    payload = json_loads(row["payload"], {}) if "payload" in row.keys() else {}
    ts_value: datetime | None = None
    try:
        ts_value = datetime.fromisoformat(str(row["ts"]))
    except Exception:
        ts_value = None
    anchor = latest_checkpoint_anchor(payload=payload if isinstance(payload, dict) else None, event_ts=ts_value)
    if not anchor:
        return None
    anchor.pop("ts", None)
    return dict(anchor)


def _normalize_execution_milestone_lite(
    *,
    details_map: dict[str, Any],
    fallback_title: str,
    state_value: str,
    checkpoint_anchor: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = normalize_milestone_v1(
        details=details_map,
        state=state_value,
        fallback_title=fallback_title,
    )
    milestone = dict(normalized.get("normalized") or {})
    anchors, _ = normalize_anchor_list(milestone.get("anchors"), max_items=40)
    milestone["anchors"] = merge_anchors(
        plugin_checkpoint=checkpoint_anchor,
        reported_anchors=anchors,
        max_items=40,
    )
    return {
        "milestone": milestone,
        "valid": bool(normalized.get("valid", False)),
        "errors": [str(item) for item in (normalized.get("errors") or []) if str(item).strip()],
        "redaction_applied": bool(normalized.get("redaction_applied", False)),
    }


def _normalize_change_summary_map_lite(raw: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(raw, dict):
        return {}, False
    normalized: dict[str, dict[str, Any]] = {}
    redacted_any = False
    for file_path, item in raw.items():
        file_text = _redacted_excerpt_lite(file_path, max_chars=240)
        if not file_text:
            continue
        payload = item if isinstance(item, dict) else {}
        try:
            added = max(0, int(payload.get("added") or payload.get("added_lines") or 0))
        except Exception:
            added = 0
        try:
            removed = max(0, int(payload.get("removed") or payload.get("removed_lines") or 0))
        except Exception:
            removed = 0
        intent = _redacted_excerpt_lite(payload.get("intent"), max_chars=160)
        if intent and str(intent) != str(payload.get("intent") or ""):
            redacted_any = True
        normalized[file_text] = {"added": added, "removed": removed, "intent": intent}
    return normalized, redacted_any


def _compute_git_change_summary_lite(
    *,
    files: list[str],
    git_payload: dict[str, Any],
    decision_text: str,
) -> dict[str, dict[str, Any]]:
    if not files:
        return {}
    repo = str(git_payload.get("repo") or "").strip() or str(Path.cwd())
    if not Path(repo).exists():
        repo = str(Path.cwd())
    commit = str(git_payload.get("commit") or "").strip()
    if commit:
        args = ["git", "-C", repo, "show", "--numstat", "--format=", commit]
    else:
        args = ["git", "-C", repo, "diff", "--numstat", "HEAD~1", "HEAD"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=1.5, check=False)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    stats: dict[str, dict[str, Any]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        added_raw, removed_raw, path = parts
        if not path:
            continue
        try:
            added = 0 if added_raw == "-" else max(0, int(added_raw))
        except Exception:
            added = 0
        try:
            removed = 0 if removed_raw == "-" else max(0, int(removed_raw))
        except Exception:
            removed = 0
        stats[str(path).strip()] = {"added": added, "removed": removed}
    if not stats:
        return {}
    intent = _redacted_excerpt_lite(decision_text, max_chars=160)
    summary: dict[str, dict[str, Any]] = {}
    for file_path in files:
        token = str(file_path or "").strip()
        if not token:
            continue
        item = stats.get(token) or {"added": 0, "removed": 0}
        summary[token] = {"added": int(item.get("added", 0)), "removed": int(item.get("removed", 0)), "intent": intent}
    return summary


def _persist_handoff_record_lite(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
    directive_id: UUID,
    event_id: UUID | None,
    milestone: dict[str, Any],
    redaction_applied: bool,
    recorded_at: datetime,
    settings: Settings,
) -> str:
    payload_raw = milestone.get("payload")
    payload: dict[str, Any] = payload_raw if isinstance(payload_raw, dict) else {}
    outcome_raw = milestone.get("outcome")
    outcome: dict[str, Any] = outcome_raw if isinstance(outcome_raw, dict) else {}
    files = list(payload.get("files") or [])[:40]
    decision_text = str(milestone.get("decision") or "")[:500]
    next_step_text = str(outcome.get("next_step") or "")[:300]
    objective_text, redacted_objective = normalize_objective_text(
        title=milestone.get("title"),
        decision=decision_text,
        next_step=next_step_text,
        fallback=str(milestone.get("task") or ""),
    )
    git_payload = dict(milestone.get("git") or {})
    change_summary = milestone.get("change_summary_json")
    if not isinstance(change_summary, dict) or not change_summary:
        change_summary = _compute_git_change_summary_lite(files=files, git_payload=git_payload, decision_text=decision_text)
    change_summary, redacted_change_summary = _normalize_change_summary_map_lite(change_summary)
    record_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO handoff_records(
            id, workspace_id, owner_id, session_id, directive_id, ts,
            title, decision, next_step, status, files_json, anchors_json, git_json,
            change_summary_json, objective_text, source, event_id, schema_version, redaction_applied, expires_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            auth.workspace_id,
            auth.user_id,
            session_id,
            str(directive_id),
            recorded_at.isoformat(),
            str(milestone.get("title") or "")[:160],
            decision_text,
            next_step_text,
            str(outcome.get("status") or "failed")[:32],
            json_dumps(files),
            json_dumps(list(milestone.get("anchors") or [])[:40]),
            json_dumps(git_payload),
            json_dumps(change_summary),
            objective_text[:300],
            str(milestone.get("source") or "native")[:24],
            str(event_id) if event_id else None,
            str(milestone.get("milestone_schema") or "v1"),
            1 if (redaction_applied or redacted_objective or redacted_change_summary) else 0,
            (recorded_at + timedelta(days=max(1, int(getattr(settings, "handoff_retention_days", 90))))).isoformat(),
        ),
    )
    conn.commit()
    return record_id


_TERMINAL_DIRECTIVE_STATES = {
    DirectiveExecutionState.SUCCEEDED,
    DirectiveExecutionState.FAILED,
    DirectiveExecutionState.BLOCKED,
    DirectiveExecutionState.ABANDONED,
}


def report_execution(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: ExecutionReportRequest,
    settings: Settings,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
               attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
               failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
        FROM directive_executions
        WHERE directive_id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
        LIMIT 1
        """,
        (str(body.directive_id), body.session_id, auth.workspace_id, auth.user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="directive not found")
    current = _directive_from_row(row)
    now = now_utc()
    rollback_available = bool(body.rollback_performed or bool((body.details or {}).get("rollback_available")))
    failure_class = None
    retry_strategy = None
    retry_feedback: dict[str, Any] | None = None
    retry_scheduled = False
    retry_directive_id = None
    execution_observation_id: str | None = None
    current_meta = dict(current.meta or {}) if isinstance(current.meta, dict) else {}

    # Idempotent replay: the MCP client auto-retries POSTs, so a duplicate
    # report of an already-terminal directive must replay the recorded result
    # rather than mint a second retry directive, re-run side effects, or flip
    # the terminal state.
    if current.state in _TERMINAL_DIRECTIVE_STATES:
        prior = current_meta.get("report_result")
        prior = prior if isinstance(prior, dict) else {}
        return {
            "directive_id": str(body.directive_id),
            "state": current.state.value,
            "retry_scheduled": bool(prior.get("retry_scheduled", False)),
            "retry_directive_id": prior.get("retry_directive_id"),
            "failure_class": prior.get("failure_class"),
            "retry_strategy": prior.get("retry_strategy"),
            "retry_feedback": prior.get("retry_feedback", {}),
            "idempotent_replay": True,
            "updated_at": (
                current.updated_at.isoformat()
                if hasattr(current.updated_at, "isoformat")
                else now.isoformat()
            ),
        }

    merged_meta: dict[str, Any] = dict(current_meta)
    details_map = _merge_execution_report_details_lite(body)
    checkpoint_anchor = _latest_editor_checkpoint_anchor_lite(
        conn,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        session_id=body.session_id,
    )
    milestone_result = _normalize_execution_milestone_lite(
        details_map=details_map,
        fallback_title=f"Directive {body.state.value}: {current.action_kind}",
        state_value=body.state.value,
        checkpoint_anchor=checkpoint_anchor,
    )
    milestone = dict(milestone_result["milestone"])
    milestone["change_summary_json"] = details_map.get("change_summary_json") or details_map.get("change_summary") or {}
    milestone["source"] = "native"
    details_map.update(milestone)
    milestone_mode = normalize_handoff_mode(getattr(settings, "handoff_milestone_validation_mode", "shadow"))
    merged_meta["milestone_validation"] = {
        "mode": milestone_mode,
        "valid": bool(milestone_result["valid"]),
        "errors": milestone_result["errors"],
        "redaction_applied": bool(milestone_result["redaction_applied"]),
    }
    if milestone_mode == "enforce" and not milestone_result["valid"]:
        raise HTTPException(status_code=422, detail={"message": "invalid milestone payload", "errors": milestone_result["errors"]})
    if details_map:
        merged_meta.update(details_map)
    execution_transcript: dict[str, Any] = {}
    if bool(getattr(settings, "takeover_rationale_enrichment_enabled", True)) and bool(
        getattr(settings, "takeover_execution_details_contract_enabled", True)
    ):
        execution_transcript, transcript_issues = _normalize_execution_transcript_details_lite(
            details_map, settings=settings
        )
        merged_meta["execution_transcript_contract"] = {
            "enabled": True,
            "valid": len(transcript_issues) == 0,
            "issues": transcript_issues,
            "present": bool(execution_transcript),
        }
        if execution_transcript:
            merged_meta["execution_transcript"] = execution_transcript
    validation_errors: list[str] = []
    dependency_plan = details_map.get("dependency_plan") or current_meta.get("dependency_plan")
    if dependency_plan is not None:
        plan_valid, plan_error = _validate_dependency_plan_lite(dependency_plan)
        merged_meta["dependency_preflight"] = {
            "valid": bool(plan_valid),
            "error": plan_error,
        }
        if not plan_valid and plan_error:
            validation_errors.append(plan_error)
    contract_validation = _validate_typed_contract_lite(settings, details_map)
    if contract_validation is not None:
        merged_meta["contract_validation"] = contract_validation
        if not bool(contract_validation.get("valid", True)):
            validation_errors.extend([str(item) for item in (contract_validation.get("errors") or []) if str(item)])
    effective_state = body.state
    effective_failure_reason = body.failure_reason
    if validation_errors:
        effective_state = DirectiveExecutionState.FAILED
        effective_failure_reason = f"validation_failure: {'; '.join(validation_errors)}"[:500]
    milestone_outcome_raw = milestone.get("outcome")
    milestone_outcome: dict[str, Any] = (
        milestone_outcome_raw if isinstance(milestone_outcome_raw, dict) else {}
    )
    milestone_outcome["status"] = effective_state.value
    milestone["outcome"] = milestone_outcome
    merged_meta["outcome_recorded"] = True
    merged_meta["reported_state"] = effective_state.value
    merged_meta["reported_at"] = now.isoformat()

    if effective_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED, DirectiveExecutionState.ABANDONED}:
        failure_class = classify_failure(
            result=body.result,
            failure_reason=effective_failure_reason,
            details=details_map,
        )
        retry_strategy = retry_strategy_for_attempt(
            attempt=int(current.attempt) + 1,
            failure_class=failure_class,
            rollback_available=rollback_available,
        )
        if bool(getattr(settings, "retry_feedback_enabled", False)):
            retry_feedback = build_retry_feedback(
                action_kind=current.action_kind,
                failure_class=failure_class,
                failure_reason=effective_failure_reason,
                retry_strategy=retry_strategy,
            )
            merged_meta["retry_feedback"] = retry_feedback
    conn.execute(
        """
        UPDATE directive_executions
        SET state = ?, finished_at = ?, failure_class = ?, failure_reason = ?, retry_strategy = ?, meta = ?, updated_at = ?
        WHERE directive_id = ?
        """,
        (
            effective_state.value,
            now.isoformat(),
            failure_class.value if failure_class else None,
                effective_failure_reason,
                retry_strategy.value if retry_strategy else None,
                json_dumps(merged_meta),
                now.isoformat(),
            str(body.directive_id),
        ),
    )
    completion_outbox = enqueue_handoff(
        conn,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        behavior_subject_id=auth.behavior_subject_id,
        session_id=body.session_id,
        directive_id=body.directive_id,
        completion_key=f"directive:{body.directive_id}:{effective_state.value}",
        terminal_state=effective_state.value,
        milestone=milestone,
        source="native",
        redaction_applied=bool(milestone_result.get("redaction_applied", False)),
        now=now,
    )

    if (
        settings.takeover_retry_enabled
        and effective_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}
        and int(current.attempt) < int(settings.takeover_retry_max_attempts)
        and retry_strategy not in {None, RetryStrategy.ESCALATE}
    ):
        window_start = now - timedelta(minutes=max(1, int(settings.takeover_retry_window_minutes)))
        retry_count_row = conn.execute(
            """
            SELECT COUNT(1) AS total
            FROM directive_executions
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
              AND created_at >= ?
              AND state IN (?, ?)
            """,
            (
                body.session_id,
                auth.workspace_id,
                auth.user_id,
                window_start.isoformat(),
                DirectiveExecutionState.PENDING.value,
                DirectiveExecutionState.IN_PROGRESS.value,
            ),
        ).fetchone()
        retry_count = int((retry_count_row["total"] if retry_count_row else 0) or 0)
        if retry_count < int(settings.takeover_retry_window_limit):
            retry_directive_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO directive_executions(
                    directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                    attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                    failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(retry_directive_id),
                    current.session_id,
                    current.workspace_id,
                    current.user_id,
                    str(current.goal_id) if current.goal_id else None,
                    current.objective_hash,
                    current.action_kind,
                    int(current.attempt) + 1,
                    DirectiveExecutionState.PENDING.value,
                    1 if current.requires_permit else 0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    retry_strategy.value if retry_strategy else RetryStrategy.NARROW_SCOPE.value,
                    json_dumps(
                        {
                            "retry_of": str(body.directive_id),
                            "previous_failure": effective_failure_reason,
                            "retry_feedback": retry_feedback or {},
                        }
                    ),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            retry_scheduled = True
    state_for_snapshot = takeover_state(conn, body.session_id, auth.workspace_id, auth.user_id)
    takeover_context_for_snapshot = (
        dict(state_for_snapshot.takeover_context or {})
        if isinstance(state_for_snapshot.takeover_context, dict)
        else {}
    )
    working_set_for_snapshot = (
        dict(state_for_snapshot.working_set_json or {})
        if isinstance(state_for_snapshot.working_set_json, dict)
        else {}
    )
    try:
        execution_state = effective_state.value
        if effective_state == DirectiveExecutionState.SUCCEEDED:
            situation_type = "routine_task"
            outcome_sentiment = "positive"
        elif effective_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}:
            situation_type = "error_occurred"
            outcome_sentiment = "negative"
        else:
            situation_type = "escalation_point"
            outcome_sentiment = "neutral"
        summary_key = str(current.objective_hash or current.action_kind or "execution")
        citation_ids = (
            [str(item) for item in working_set_for_snapshot.get("citations", []) if isinstance(item, str)][:20]
            if isinstance(working_set_for_snapshot.get("citations"), list)
            else []
        )
        base_context_snapshot = {
            "session_id": body.session_id,
            "directive_id": str(body.directive_id),
            "attempt": int(current.attempt),
            "retry_scheduled": retry_scheduled,
            "failure_class": failure_class.value if failure_class else None,
            "retry_strategy": retry_strategy.value if retry_strategy else None,
            "decision_confidence": _safe_float(merged_meta.get("decision_confidence"), 0.0),
            "context_quality_score": _safe_float(merged_meta.get("context_quality_score"), 0.0),
            "citation_ids": citation_ids,
        }
        context_snapshot = _build_enriched_execution_context_snapshot_lite(
            settings=settings,
            base_snapshot=base_context_snapshot,
            takeover_context=takeover_context_for_snapshot,
            working_set=working_set_for_snapshot,
            details_map=details_map,
            transcript=execution_transcript,
        )
        execution_observation_id = save_observation_lite(
            conn,
            {
                "consumer_id": auth.consumer,
                "workspace_id": auth.workspace_id,
                "situation_type": situation_type,
                "situation_summary": f"execution:{current.action_kind}:{summary_key}"[:500],
                "user_response": str(body.result or effective_failure_reason or execution_state)[:1000],
                "response_reasoning": "execution_report",
                "outcome": execution_state,
                "outcome_sentiment": outcome_sentiment,
                "confidence": 0.82 if effective_state == DirectiveExecutionState.SUCCEEDED else 0.66,
                "context_snapshot": context_snapshot,
            },
        )
        merged_meta["observation_id"] = execution_observation_id
        conn.execute(
            """
            UPDATE directive_executions
            SET meta = ?, updated_at = ?
            WHERE directive_id = ?
            """,
            (
                json_dumps(merged_meta),
                now.isoformat(),
                str(body.directive_id),
            ),
        )
    except Exception:
        pass
    try:
        execution_state = effective_state.value
        _evolve_execution_pattern_lite(
            conn,
            workspace_id=auth.workspace_id,
            action_kind=str(current.action_kind or "execute"),
            objective_hash_value=current.objective_hash,
            execution_state=execution_state,
        )
        _upsert_workflow_skill_template_lite(
            conn,
            workspace_id=auth.workspace_id,
            action_kind=str(current.action_kind or "execute"),
            objective_hash_value=current.objective_hash,
            execution_state=execution_state,
        )
    except Exception:
        pass

    state = takeover_state(conn, body.session_id, auth.workspace_id, auth.user_id)
    context = dict(state.takeover_context or {})
    _update_structured_plan_progress_context_lite(
        context,
        execution_state=effective_state,
        updated_at=now,
    )
    _update_objective_quality_context_lite(
        context,
        execution_state=effective_state,
        retry_scheduled=retry_scheduled,
        updated_at=now,
        details=details_map,
        required_verifications=[
            str(item).strip().lower()
            for item in ((current.meta or {}).get("verification_required") or _required_verification_checks_lite(settings))
            if str(item).strip()
        ],
    )
    _update_objective_contract_context_lite(
        context,
        execution_state=effective_state,
        details=details_map,
        updated_at=now,
    )
    feedback_result = (
        "success"
        if effective_state == DirectiveExecutionState.SUCCEEDED
        else "blocked"
        if effective_state == DirectiveExecutionState.BLOCKED
        else "failure"
    )
    updated_outcomes, updated_autonomy = update_recent_outcomes(
        state.recent_outcomes_json,
        feedback_result,
        turn=int(state.takeover_context.get("turn_count", 0) or 0),
        latency_ms=int((details_map or {}).get("latency_ms") or 0) or None,
    )
    state.recent_outcomes_json = updated_outcomes
    state.autonomy_score = updated_autonomy
    if effective_state == DirectiveExecutionState.SUCCEEDED:
        goal_to_complete = current.goal_id or state.active_goal_id
        if goal_to_complete:
            conn.execute(
                """
                UPDATE autonomy_goals
                SET status = ?, updated_at = ?
                WHERE id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
                """,
                (
                    AutonomyGoalStatus.DONE.value,
                    now.isoformat(),
                    str(goal_to_complete),
                    state.session_id,
                    state.workspace_id,
                    state.user_id,
                ),
            )
            if state.active_goal_id and str(state.active_goal_id) == str(goal_to_complete):
                state.active_goal_id = None
        previous_completed_hash = str(context.get("last_completed_objective_hash") or "")
        current_completed_hash = str(current.objective_hash or "")
        if previous_completed_hash and previous_completed_hash == current_completed_hash:
            same_streak = int(context.get("completed_same_objective_streak", 0) or 0) + 1
        else:
            same_streak = 1
        context["completed_same_objective_streak"] = same_streak
        if same_streak >= 3:
            context["force_user_objective_refresh"] = True
        context["awaiting_next_objective"] = True
        context["awaiting_next_objective_turns"] = 0
        context["last_completed_directive_id"] = str(current.directive_id)
        context["last_completed_objective_hash"] = current_completed_hash
        context["last_completed_at"] = now.isoformat()
        context.pop("_pending_directive_locked", None)
        context.pop("objective", None)
        state.objective_hash = ""
        state.goal_queue_size = max(0, int(state.goal_queue_size or 0) - 1)
        goal_cache_invalidate_l1(None)
        conn.execute(
            """
            DELETE FROM autonomy_goal_cache
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (state.session_id, state.workspace_id, state.user_id),
        )
    state.takeover_context = context
    _sync_enforcement_counters(conn, state)
    save_takeover_state(conn, state)
    conn.commit()
    delivered_outbox = deliver_handoff_safely(
        conn,
        outbox_id=str(completion_outbox["id"]),
        retention_days=int(settings.handoff_retention_days),
    )
    record_resume_progress(
        conn,
        workspace_id=auth.workspace_id,
        requesting_owner_id=auth.user_id,
        session_id=body.session_id,
        phase="completed",
        outcome_status=effective_state.value,
        progress_source="report_execution",
    )
    # Persist a compact replay payload so a retried POST (auto-retried by the
    # MCP client) replays instead of re-processing. The terminal-state guard
    # at the top of this function reads report_result back.
    replay_meta = dict(merged_meta)
    replay_meta["report_result"] = {
        "retry_scheduled": retry_scheduled,
        "retry_directive_id": str(retry_directive_id) if retry_directive_id else None,
        "failure_class": failure_class.value if failure_class else None,
        "retry_strategy": retry_strategy.value if retry_strategy else None,
        "retry_feedback": retry_feedback or {},
    }
    conn.execute(
        "UPDATE directive_executions SET meta = ? WHERE directive_id = ?",
        (json_dumps(replay_meta), str(body.directive_id)),
    )
    conn.commit()
    return {
        "directive_id": str(body.directive_id),
        "state": effective_state.value,
        "retry_scheduled": retry_scheduled,
        "retry_directive_id": str(retry_directive_id) if retry_directive_id else None,
        "failure_class": failure_class.value if failure_class else None,
        "retry_strategy": retry_strategy.value if retry_strategy else None,
        "retry_feedback": retry_feedback or {},
        "contract_validation": merged_meta.get("contract_validation"),
        "dependency_preflight": merged_meta.get("dependency_preflight"),
        "milestone_validation": merged_meta.get("milestone_validation"),
        "handoff_record_id": str(delivered_outbox["handoff_record_id"]),
        "completion_outbox_id": str(delivered_outbox["id"]),
        "completion_delivery_status": str(delivered_outbox["status"]),
        "objective_quality": context.get("objective_quality", {}),
        "objective_contract": context.get("objective_contract", {}),
        "updated_at": now.isoformat(),
    }


def execution_status(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
) -> ExecutionStatusResponse:
    pending_rows = conn.execute(
        """
        SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
               attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
               failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
          AND state IN (?, ?)
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (
            session_id,
            auth.workspace_id,
            auth.user_id,
            DirectiveExecutionState.PENDING.value,
            DirectiveExecutionState.IN_PROGRESS.value,
        ),
    ).fetchall()
    recent_rows = conn.execute(
        """
        SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
               attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
               failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
        FROM directive_executions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        ORDER BY updated_at DESC
        LIMIT 30
        """,
        (session_id, auth.workspace_id, auth.user_id),
    ).fetchall()
    return ExecutionStatusResponse(
        session_id=session_id,
        pending=[_directive_from_row(row) for row in pending_rows],
        recent=[_directive_from_row(row) for row in recent_rows],
        generated_at=now_utc(),
    )


def takeover_preload(
    conn: sqlite3.Connection,
    body: TakeoverPreloadRequest,
    auth: AuthContext,
    settings: Settings,
) -> TakeoverPreloadResponse:
    state = takeover_state(
        conn=conn,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=body.persona_mode,
        activation_keywords=body.activation_keywords,
        stop_keywords=body.stop_keywords,
    )
    now = now_utc()
    resolved_task = resolve_objective(message=body.task, task=body.task, takeover_context=state.takeover_context)
    working_set = _build_takeover_working_set(
        conn,
        auth,
        settings,
        task=resolved_task,
        app_context=body.app_context,
        constraints=body.constraints,
    )
    state.takeover_context["objective"] = resolved_task
    state.objective_hash = objective_hash(resolved_task)
    state.working_set_json = working_set
    state.updated_at = now
    save_takeover_state(conn, state)
    return TakeoverPreloadResponse(
        session_id=state.session_id,
        objective_hash=state.objective_hash or "",
        working_set_json=working_set,
        refreshed_at=now,
        decision_source=TakeoverDecisionSource.DELIBERATION,
    )


def takeover_feedback(
    conn: sqlite3.Connection,
    body: TakeoverFeedbackRequest,
    auth: AuthContext,
    settings: Settings,
) -> TakeoverFeedbackResponse:
    state = takeover_state(
        conn=conn,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    outcomes, autonomy = update_recent_outcomes(
        state.recent_outcomes_json,
        body.result,
        turn=body.turn,
        latency_ms=body.latency_ms,
    )
    feedback_type = (body.clone_feedback_type.value if body.clone_feedback_type else "").strip().lower()
    feedback_details = dict(body.details or {})
    if feedback_type:
        feedback_details.setdefault("clone_feedback_type", feedback_type)
    if body.correction_text:
        feedback_details.setdefault("correction_text", body.correction_text)
    normalized_situation_type = _canonical_situation_type(body.situation_type) if body.situation_type else None
    if body.situation_type:
        feedback_details.setdefault("situation_type", normalized_situation_type)
    if body.observation_ids:
        feedback_details.setdefault("observation_ids", [str(value) for value in body.observation_ids])

    behavior_evidence_id: str | None = None
    if normalized_situation_type and body.correction_text:
        supersedes_id = str(body.observation_ids[0]) if body.observation_ids else None
        correction_evidence = normalize_behavior_evidence(
            {
                "situation_type": normalized_situation_type,
                "situation_summary": f"Human correction during {body.action_kind}",
                "objective": str(state.takeover_context.get("objective") or f"feedback:{normalized_situation_type}"),
                "selected_choice": body.correction_text,
                "rationale": str(feedback_details.get("reasoning_summary") or "Human correction after takeover feedback"),
                "action_taken": body.correction_text,
                "outcome": body.result,
                "outcome_sentiment": "negative" if feedback_type == "unhelpful" else "neutral",
                "correction_text": body.correction_text if supersedes_id else "",
                "evidence_source": "correction" if supersedes_id else "explicit",
                "memory_class": "preference",
                "supersedes_observation_id": supersedes_id,
                "confidence": 1.0,
                "confirmed_at": now_utc(),
            }
        )
        correction_gate = behavior_storage_gate(
            correction_evidence,
            threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)),
        )
        behavior_evidence_id = save_behavior_evidence_lite(
            conn,
            consumer_id=auth.consumer,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            evidence=correction_evidence,
            storage_gate=correction_gate,
        )
        feedback_details["behavior_evidence_id"] = behavior_evidence_id

    feedback_observation_ids: list[str] = [str(value) for value in (body.observation_ids or [])]
    if behavior_evidence_id is not None and not feedback_observation_ids:
        feedback_observation_ids.append(behavior_evidence_id)
    if normalized_situation_type and body.correction_text and not feedback_observation_ids:
        feedback_obs_id = save_observation_lite(
            conn,
            {
                "consumer_id": auth.consumer,
                "workspace_id": auth.workspace_id,
                "situation_type": normalized_situation_type,
                "situation_summary": f"feedback:{normalized_situation_type}",
                "user_response": body.correction_text,
                "response_reasoning": "takeover_feedback",
                "outcome": body.result,
                "outcome_sentiment": "negative" if feedback_type == "unhelpful" else "neutral",
                "confidence": max(0.1, min(1.0, state.autonomy_score)),
            },
        )
        feedback_observation_ids.append(feedback_obs_id)

    if feedback_type in {"helpful", "unhelpful", "neutral"} or body.correction_text:
        fp_data = load_fingerprint_lite(conn, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
        fingerprint = (fp_data or {}).get("fingerprint", DEFAULT_FINGERPRINT.copy())
        observation_count = int((fp_data or {}).get("observation_count", 0))
        adjusted_alpha = feedback_adjusted_alpha(
            settings.feedback_base_alpha,
            feedback_type or None,
            alpha_min=settings.feedback_alpha_min,
            alpha_max=settings.feedback_alpha_max,
        )
        feedback_details.setdefault("feedback_alpha", adjusted_alpha)
        if body.correction_text or feedback_type in {"unhelpful", "negative", "wrong"}:
            fingerprint = apply_feedback_to_fingerprint(
                fingerprint,
                feedback_type=feedback_type or "unhelpful",
                correction_text=body.correction_text,
                base_alpha=settings.feedback_base_alpha,
                alpha_min=settings.feedback_alpha_min,
                alpha_max=settings.feedback_alpha_max,
            )
            save_fingerprint_lite(
                conn,
                consumer_id=auth.behavior_subject_id,
                workspace_id=auth.workspace_id,
                fingerprint=fingerprint,
                observation_count=max(observation_count, 1),
            )

    if feedback_observation_ids and feedback_type:
        for observation_id in feedback_observation_ids:
            _insert_clone_feedback_row(
                conn,
                observation_id=observation_id,
                session_id=body.session_id,
                feedback_type=feedback_type,
                correction_text=body.correction_text,
            )

    state.recent_outcomes_json = outcomes
    state.autonomy_score = autonomy
    state.updated_at = now_utc()
    if body.objective_hash:
        state.objective_hash = body.objective_hash
    save_takeover_state(conn, state)
    goal_cache_invalidate_l1(None)
    conn.execute(
        """
        DELETE FROM autonomy_goal_cache
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (state.session_id, state.workspace_id, state.user_id),
    )
    _record_takeover_action(
        conn,
        state=state,
        turn=body.turn,
        action_kind=body.action_kind,
        result=body.result,
        latency_ms=body.latency_ms,
        meta=feedback_details,
    )
    return TakeoverFeedbackResponse(
        session_id=state.session_id,
        autonomy_score=state.autonomy_score,
        recent_outcomes_json=state.recent_outcomes_json,
        updated_at=state.updated_at,
    )


def takeover_step(
    conn: sqlite3.Connection,
    body: TakeoverStepRequest,
    auth: AuthContext,
    settings: Settings,
) -> TakeoverStepResponse:
    started_total = time.perf_counter()
    state_started = time.perf_counter()
    state = takeover_state(
        conn=conn,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=body.persona_mode,
        activation_keywords=body.activation_keywords,
        stop_keywords=body.stop_keywords,
    )
    state_ms = int((time.perf_counter() - state_started) * 1000)
    snapshot_rehydrated = False
    snapshot_age_hours: int | None = None
    now = now_utc()
    if state.expires_at and state.expires_at <= now:
        state.active = False
        state.expires_at = None
    if not getattr(state, "autonomy_policy_profile", None):
        state.autonomy_policy_profile = AutonomyPolicyProfile(settings.takeover_autonomy_policy_default)
    continuity_violations, continuity_ok = continuity_health(
        was_active=bool(state.active),
        last_message_at=state.last_message_at,
        now=now,
        previous_violations=int(state.continuity_violation_count or 0),
        max_gap_seconds=int(settings.takeover_continuity_gap_seconds),
    )
    state.continuity_violation_count = continuity_violations
    if not continuity_ok and state.mode == TakeoverMode.TAKEOVER:
        state.mode = TakeoverMode.SUGGEST

    message = body.message
    normalized_message = normalize_text(message)
    stop_hit = contains_phrase(normalized_message, state.stop_keywords)
    if stop_hit:
        state.active = False
        state.last_message_at = now
        state.expires_at = None
        state.updated_at = now
        state.pending_directive_count = 0
        state.retry_backlog_count = 0
        goal_cache_invalidate_l1(None)
        conn.execute(
            """
            DELETE FROM autonomy_goal_cache
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (state.session_id, state.workspace_id, state.user_id),
        )
        conn.execute(
            """
            DELETE FROM directive_executions
            WHERE session_id = ? AND workspace_id = ? AND user_id = ?
              AND state IN (?, ?)
            """,
            (
                state.session_id,
                state.workspace_id,
                state.user_id,
                DirectiveExecutionState.PENDING.value,
                DirectiveExecutionState.IN_PROGRESS.value,
            ),
        )
        conn.execute(
            """
            UPDATE autonomy_notices
            SET acknowledged_at = COALESCE(acknowledged_at, ?)
            WHERE session_id = ? AND workspace_id = ? AND user_id = ? AND acknowledged_at IS NULL
            """,
            (now.isoformat(), state.session_id, state.workspace_id, state.user_id),
        )
        _save_session_memory_snapshot_lite(
            conn,
            state=state,
            reason="stand_down",
            max_per_session=max(1, int(getattr(settings, "snapshot_max_per_session", 10))),
        )
        save_takeover_state(conn, state)
        return TakeoverStepResponse(
            state=state,
            action="stopped",
            classification=TakeoverClassification.DECISIVE,
            enforced=False,
            safety_decision=SafetyDecision.ALLOW,
            final_response=None,
            note="takeover stopped",
            decision_confidence=0.0,
            decision_source=TakeoverDecisionSource.FAST_PATH,
            next_action=TakeoverNextAction(kind="stop", target="", rationale="stand-down requested"),
            latency_breakdown_ms=TakeoverLatencyBreakdown(
                state=state_ms,
                total=int((time.perf_counter() - started_total) * 1000),
            ),
            needs_human=False,
            continuity_ok=continuity_ok,
        )

    activation_hit = contains_phrase(normalized_message, state.activation_keywords)
    override_mode = mode_override(state.persona_mode, normalized_message)
    if activation_hit:
        state.active = True
        state.activated_at = now
        state.mode = override_mode or body.activation_mode_default
        state.expires_at = datetime.fromisoformat(next_expiry(body.policy.timeout_minutes))
    elif override_mode is not None:
        state.active = True
        state.mode = override_mode
        state.expires_at = datetime.fromisoformat(next_expiry(body.policy.timeout_minutes))
    if (not state.active) and body.activation_mode_default and body.activation_mode_default == TakeoverMode.TAKEOVER:
        state.active = True
        state.mode = TakeoverMode.TAKEOVER
        state.activated_at = now
        state.expires_at = datetime.fromisoformat(next_expiry(body.policy.timeout_minutes))

    if not state.active:
        state.last_message_at = now
        state.updated_at = now
        save_takeover_state(conn, state)
        return TakeoverStepResponse(
            state=state,
            action="inactive",
            classification=TakeoverClassification.EMPTY,
            enforced=False,
            safety_decision=SafetyDecision.ALLOW,
            final_response=None,
            decision_confidence=0.0,
            decision_source=TakeoverDecisionSource.FAST_PATH,
            next_action=TakeoverNextAction(kind="inactive", target="", rationale="takeover inactive"),
            latency_breakdown_ms=TakeoverLatencyBreakdown(
                state=state_ms,
                total=int((time.perf_counter() - started_total) * 1000),
            ),
            needs_human=False,
            continuity_ok=continuity_ok,
        )

    classify_started = time.perf_counter()
    state.last_message_at = now
    state.updated_at = now
    state.expires_at = datetime.fromisoformat(next_expiry(body.policy.timeout_minutes))

    if body.takeover_context:
        merged = dict(state.takeover_context)
        merged.update(body.takeover_context)
        state.takeover_context = merged
    state.takeover_context["last_user_message"] = message[:280]
    state.takeover_context["turn_count"] = int(state.takeover_context.get("turn_count", 0)) + 1
    turn_count = int(state.takeover_context.get("turn_count", 0))
    awaiting_next_objective = bool(state.takeover_context.get("awaiting_next_objective"))
    has_new_objective_signal = bool(str(body.task or "").strip()) or _has_explicit_objective_signal_lite(normalized_message)
    if awaiting_next_objective and has_new_objective_signal:
        state.takeover_context.pop("awaiting_next_objective", None)
        state.takeover_context.pop("awaiting_next_objective_turns", None)
        awaiting_next_objective = False
        state.takeover_context.pop("force_user_objective_refresh", None)
        state.takeover_context.pop("objective_needs_refresh", None)
    elif awaiting_next_objective:
        state.takeover_context["awaiting_next_objective_turns"] = int(
            state.takeover_context.get("awaiting_next_objective_turns", 0)
        ) + 1

    handoff_source_text = body.executor_output if (body.executor_output or "").strip() else body.message
    if (
        state.mode == TakeoverMode.SUGGEST
        and body.policy.auto_handoff_on_question
        and classify_text(
            handoff_source_text,
            semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
            semantic_threshold=float(getattr(settings, "semantic_classifier_intent_threshold", 0.67)),
            semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
        )
        in {TakeoverClassification.QUESTION, TakeoverClassification.HANDOFF}
    ):
        state.mode = TakeoverMode.TAKEOVER

    resolved_task = resolve_objective(
        message=message,
        task=body.task,
        takeover_context=state.takeover_context,
    )
    pending_execution = _load_pending_directive(conn, state=state)
    if pending_execution is not None:
        pending_objective = str((pending_execution.meta or {}).get("objective") or "").strip()
        if pending_objective:
            resolved_task = pending_objective
            state.takeover_context["objective"] = pending_objective
            state.objective_hash = pending_execution.objective_hash or objective_hash(pending_objective)
            state.takeover_context["_pending_directive_locked"] = True
        retry_feedback_payload = (pending_execution.meta or {}).get("retry_feedback") if isinstance(pending_execution.meta, dict) else None
        if bool(getattr(settings, "retry_feedback_enabled", False)) and isinstance(retry_feedback_payload, dict):
            state.takeover_context["retry_feedback"] = retry_feedback_payload
        else:
            state.takeover_context.pop("retry_feedback", None)
    if pending_execution is not None and awaiting_next_objective:
        state.takeover_context.pop("awaiting_next_objective", None)
        state.takeover_context.pop("awaiting_next_objective_turns", None)
        awaiting_next_objective = False
    pinned_objective = str(state.takeover_context.get("pinned_user_objective") or "").strip()
    pinned_active = bool(pinned_objective)
    pin_should_update = bool(
        str(body.task or "").strip()
        or ("new objective" in normalized_message)
        or ("next objective" in normalized_message)
        or (not pinned_objective)
    )
    if has_new_objective_signal and pin_should_update and not pending_execution:
        pinned_objective = str(resolved_task or "").strip()
        if pinned_objective:
            state.takeover_context["pinned_user_objective"] = pinned_objective
            state.takeover_context["pinned_user_objective_hash"] = objective_hash(pinned_objective)
            state.takeover_context["pinned_user_objective_turn"] = turn_count
            state.takeover_context["pinned_user_objective_until_turn"] = turn_count + 200
            pinned_active = True
    if pinned_active and not pending_execution and not body.task:
        resolved_task = pinned_objective
    classifier_input = body.executor_output if (body.executor_output or "").strip() else message
    classifier_for_conf = classify_text(
        classifier_input,
        takeover_active=state.mode == TakeoverMode.TAKEOVER,
        semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
        semantic_threshold=float(getattr(settings, "semantic_classifier_intent_threshold", 0.67)),
        semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
    )
    classify_ms = int((time.perf_counter() - classify_started) * 1000)

    retrieval_started = time.perf_counter()
    objective_hash_value = objective_hash(resolved_task)
    objective_changed = objective_hash_value != (state.objective_hash or "")
    selected_goal: TakeoverGoal | None = None
    if state.active_goal_id:
        maybe_goal = conn.execute(
            """
            SELECT id, session_id, workspace_id, user_id, title, description, source,
                   priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                   goal_kind, affective_scores, selection_score, cache_hit, cache_source,
                   status, created_at, updated_at
            FROM autonomy_goals
            WHERE id = ? AND session_id = ? AND workspace_id = ? AND user_id = ?
            LIMIT 1
            """,
            (str(state.active_goal_id), state.session_id, state.workspace_id, state.user_id),
        ).fetchone()
        if maybe_goal is not None:
            selected_goal = _goal_from_row(maybe_goal)
    goal_discovery_every_n_turns = max(6, int(getattr(settings, "takeover_goal_discovery_every_n_turns", 24)))
    periodic_goal_discovery_due = turn_count > 0 and (turn_count % goal_discovery_every_n_turns == 0)
    should_discover = bool(
        state.active
        and (not awaiting_next_objective or has_new_objective_signal)
        and (
            selected_goal is None
            or objective_changed
            or periodic_goal_discovery_due
            or recent_failure_count(state.recent_outcomes_json) >= 2
            or any(token in normalized_message for token in ("explore", "research", "what next"))
        )
    )
    if should_discover:
        discovered_goals = discover_takeover_goals(
            conn,
            auth=auth,
            session_id=state.session_id,
            include_open_discovery=settings.takeover_goal_source == "open_discovery",
            settings=settings,
        )
        if selected_goal is None and discovered_goals:
            chosen_goal: TakeoverGoal | None = discovered_goals[0]
            if pinned_active and pinned_objective:
                pinned_match: TakeoverGoal | None = None
                best_overlap = 0.0
                for goal_candidate in discovered_goals:
                    overlap = _objective_overlap_score_lite(
                        pinned_objective,
                        f"{goal_candidate.title} {goal_candidate.description}",
                    )
                    if overlap > best_overlap:
                        best_overlap = overlap
                        pinned_match = goal_candidate
                if pinned_match is not None and best_overlap >= 0.28:
                    chosen_goal = pinned_match
                else:
                    chosen_goal = None
                    state.takeover_context["force_user_objective_refresh"] = True
            if chosen_goal is not None and chosen_goal.confidence >= float(settings.takeover_goal_selection_min_confidence):
                state.active_goal_id = chosen_goal.id
                selected_goal = chosen_goal
    if selected_goal is not None:
        state.takeover_context["selected_goal_title"] = str(selected_goal.title or "").strip()
        resolved_norm = normalize_text(resolved_task)
        if (
            not resolved_norm
            or resolved_norm in {"current objective", "continue active objective", "follow latest concrete objective"}
        ):
            resolved_task = str(selected_goal.description or selected_goal.title or resolved_task)
    if selected_goal is not None and not body.task and not awaiting_next_objective and not pinned_active:
        resolved_task = selected_goal.description or selected_goal.title
    state.takeover_context["objective"] = resolved_task
    state.objective_hash = objective_hash(resolved_task)
    working_set = state.working_set_json if isinstance(state.working_set_json, dict) else {}
    if (
        bool(getattr(settings, "snapshot_rehydrate_enabled", True))
        and objective_hash_value
        and (objective_changed or not working_set)
    ):
        snapshot_record = _load_recent_session_memory_snapshot_lite(
            conn,
            workspace_id=state.workspace_id,
            user_id=state.user_id,
            session_id=state.session_id,
            objective_hash=objective_hash_value,
            max_age_days=max(1, int(getattr(settings, "snapshot_max_age_days", 14))),
        )
        if snapshot_record is not None:
            payload = snapshot_record.get("payload", {})
            snapshot_working_set = payload.get("working_set_json") if isinstance(payload, dict) else None
            if isinstance(snapshot_working_set, dict) and snapshot_working_set:
                working_set = dict(snapshot_working_set)
                working_set["rehydrated_from_snapshot_id"] = snapshot_record.get("snapshot_id")
                working_set["rehydrated_at"] = now.isoformat()
                state.working_set_json = working_set
                snapshot_rehydrated = True
                snapshot_age_hours = int(snapshot_record.get("age_hours") or 0)
                state.takeover_context["snapshot_rehydrated"] = {
                    "snapshot_id": snapshot_record.get("snapshot_id"),
                    "age_hours": snapshot_age_hours,
                    "objective_hash": objective_hash_value,
                    "at": now.isoformat(),
                }
    refresh_every_n_turns = max(6, int(getattr(settings, "takeover_working_set_refresh_every_n_turns", 24)))
    periodic_working_set_refresh_due = turn_count > 0 and (turn_count % refresh_every_n_turns == 0)
    refresh_working_set = (
        objective_changed or (not working_set) or periodic_working_set_refresh_due
    ) and not snapshot_rehydrated
    if refresh_working_set:
        working_set = _build_takeover_working_set(
            conn,
            auth,
            settings,
            task=resolved_task,
            app_context=body.app_context,
            constraints=body.constraints,
        )
        state.working_set_json = working_set
    retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)

    decision_confidence, confidence_components = compute_decision_confidence(
        objective=resolved_task,
        message=message,
        classification=classifier_for_conf,
        working_set=working_set,
        recent_outcomes=state.recent_outcomes_json,
    )
    recent_failures = recent_failure_count(state.recent_outcomes_json)
    run_deliberation = should_trigger_deliberation(
        decision_confidence=decision_confidence,
        objective_changed=objective_changed,
        turn_count=turn_count,
        recent_failures=recent_failures,
        message=message,
    )
    evidence_count = int(working_set.get("evidence_count", 0) or 0)
    evidence_strength_score = max(0.0, min(1.0, evidence_count / 6.0))
    recency_coverage_score = 1.0 if evidence_count > 0 else 0.2
    outcome_stability_score = max(0.0, 1.0 - min(1.0, recent_failures / 3.0))
    context_quality_score = round(
        (0.40 * float(decision_confidence))
        + (0.25 * evidence_strength_score)
        + (0.20 * recency_coverage_score)
        + (0.15 * outcome_stability_score),
        4,
    )
    profile_tuning = (
        autonomy_profile_tuning(state.autonomy_policy_profile)
        if bool(getattr(settings, "profile_tuning_enabled", False))
        else autonomy_profile_tuning("human_consultative")
    )
    trigger_threshold = float(settings.context_retrieval_trigger_score) + float(
        profile_tuning.get("retrieval_trigger_delta", 0.0)
    )
    trigger_threshold = max(0.35, min(0.92, trigger_threshold))
    confidence_trigger = float(getattr(settings, "takeover_retrieval_confidence_trigger", 0.70))
    evidence_threshold = int(getattr(settings, "takeover_retrieval_low_evidence_threshold", 2)) + int(
        profile_tuning.get("evidence_floor_delta", 0)
    )
    evidence_threshold = max(1, evidence_threshold)
    min_turn_for_evidence = int(getattr(settings, "takeover_retrieval_min_turn_for_evidence_gate", 4))
    cooldown_turns = max(0, int(getattr(settings, "takeover_retrieval_cooldown_turns", 2)))
    deep_intent = any(token in normalized_message for token in ("research", "deep", "explore", "investigate"))
    forced_trigger = deep_intent or (recent_failures >= 2)
    quality_gate = context_quality_score < trigger_threshold
    confidence_gate = decision_confidence < confidence_trigger
    evidence_gate = turn_count >= min_turn_for_evidence and evidence_count < evidence_threshold
    last_trigger_turn_raw = state.takeover_context.get("last_retrieval_trigger_turn")
    try:
        last_trigger_turn = int(last_trigger_turn_raw) if last_trigger_turn_raw is not None else -10_000
    except Exception:
        last_trigger_turn = -10_000
    cooldown_gate = True if cooldown_turns <= 0 else ((turn_count - last_trigger_turn) >= cooldown_turns)
    retrieval_reason: str | None = None
    if forced_trigger:
        retrieval_triggered = True
        retrieval_reason = "explicit_deep_intent" if deep_intent else "recent_failures"
    else:
        gated_trigger = quality_gate or (confidence_gate and evidence_gate)
        if gated_trigger and cooldown_gate:
            retrieval_triggered = True
            if quality_gate:
                retrieval_reason = "low_context_quality"
            elif confidence_gate and evidence_gate:
                retrieval_reason = "low_confidence"
            elif evidence_gate:
                retrieval_reason = "low_evidence"
        elif gated_trigger and not cooldown_gate:
            retrieval_triggered = False
            retrieval_reason = "cooldown_suppressed"
        else:
            retrieval_triggered = False
            retrieval_reason = None
    if retrieval_triggered:
        state.takeover_context["last_retrieval_trigger_turn"] = turn_count
    retrieval_source = "none"

    decision_source = TakeoverDecisionSource.FAST_PATH
    citation_values: list[UUID] = []
    for value in working_set.get("citations", []):
        if not isinstance(value, str):
            continue
        try:
            citation_values.append(UUID(value))
        except Exception:
            continue
    clone_payload: dict[str, Any] = {
        "guidance_summary": str(working_set.get("summary", "")),
        "do": working_set.get("do", []),
        "dont": working_set.get("dont", []),
        "confidence": decision_confidence,
        "evidence_strength": (
            "strong"
            if int(working_set.get("evidence_count", 0) or 0) >= 5
            else "medium"
            if int(working_set.get("evidence_count", 0) or 0) >= 2
            else "weak"
        ),
        "citations": citation_values,
        "citation_snippets": (
            working_set.get("citation_snippets", [])
            if isinstance(working_set.get("citation_snippets"), list)
            else []
        ),
        "confidence_components": confidence_components,
    }
    candidate = str(working_set.get("summary", "")).strip()
    citations: list[UUID] = citation_values
    deliberation_ms = 0
    fast_path_candidate = candidate
    fast_path_payload = dict(clone_payload)
    fast_path_citations = list(citations)
    advisor_fail_streak = max(0, int(state.takeover_context.get("advisor_fail_streak", 0) or 0))
    advisor_failure_reason: str | None = None
    fast_path_reason = "fast_path_default"
    advisor_required_in_takeover = bool(state.mode == TakeoverMode.TAKEOVER)
    advisor_cadence_turns = max(
        1,
        int(profile_tuning.get("advisor_cadence_turns", getattr(settings, "takeover_advisor_every_n_turns", 2))),
    )
    advisor_cadence_due = turn_count <= 2 or (turn_count % advisor_cadence_turns == 0)
    stable_context_threshold = max(trigger_threshold + 0.08, 0.82)
    stable_evidence_floor = max(2, evidence_threshold)
    high_confidence_context = bool(
        context_quality_score >= stable_context_threshold
        and evidence_count >= stable_evidence_floor
        and decision_confidence >= max(confidence_trigger - 0.04, 0.66)
    )
    if advisor_required_in_takeover and advisor_cadence_due and high_confidence_context and not retrieval_triggered:
        advisor_cadence_due = False
        if run_deliberation and retrieval_reason != "explicit_deep_intent":
            run_deliberation = False
    should_call_clone_advice = bool(
        (
            advisor_required_in_takeover
            and (
                run_deliberation
                or retrieval_triggered
                or advisor_cadence_due
                or retrieval_reason == "explicit_deep_intent"
            )
        )
        or (
            (not advisor_required_in_takeover)
            and run_deliberation
            and (
                decision_confidence < confidence_trigger
                or bool(getattr(settings, "advisor_router_v2_enabled", False))
                or retrieval_reason == "explicit_deep_intent"
            )
        )
    )
    remaining_budget_ms = max(0, int(getattr(settings, "context_retrieval_budget_ms", 120)) - retrieval_ms)
    backend_budget_ms = max(1, int(getattr(settings, "context_backend_timeout_ms", 60)))
    budget_guard_enabled = bool(getattr(settings, "takeover_retrieval_budget_guard_enabled", True))
    budget_exceeded = bool(
        budget_guard_enabled and retrieval_triggered and should_call_clone_advice and (remaining_budget_ms < backend_budget_ms)
    )
    retrieval_source_override: str | None = None
    if budget_exceeded:
        should_call_clone_advice = False
        retrieval_source_override = "hybrid_fallback"
        retrieval_reason = "budget_exceeded_skip_deliberation"
        fast_path_reason = "budget_exceeded_skip_deliberation"
    elif not should_call_clone_advice:
        if advisor_required_in_takeover and not advisor_cadence_due and not run_deliberation and not retrieval_triggered:
            fast_path_reason = "advisor_cadence_skip"
        elif run_deliberation and decision_confidence >= confidence_trigger:
            fast_path_reason = "confidence_above_trigger"
        elif not run_deliberation:
            fast_path_reason = "run_deliberation_false"
    if should_call_clone_advice:
        advisor_call_succeeded = False
        deliberation_started = time.perf_counter()
        try:
            clone_advice = build_clone_advice(
                conn=conn,
                body=CloneAdviceRequest(
                    task=resolved_task,
                    app_context=body.app_context,
                    constraints=body.constraints,
                    takeover_context=state.takeover_context,
                    message_delta=body.message_delta,
                    executor_output=body.executor_output,
                    interaction_id=body.interaction_id,
                    allow_fallback=(body.allow_fallback if not advisor_required_in_takeover else False),
                ),
                auth=auth,
                settings=settings,
            )
            clone_payload = clone_advice.model_dump(mode="json")
            candidate = clone_advice.guidance_summary
            citations = clone_advice.citations
            state.last_deliberation_at = now
            citation_limit = max(1, int(getattr(settings, "takeover_citation_snippet_max_items", 20)))
            refreshed_citation_ids = list(clone_advice.citations[:citation_limit])
            refreshed_citation_snippets = (
                _build_citation_snippets_lite(
                    conn,
                    citation_ids=refreshed_citation_ids,
                    settings=settings,
                )
                if bool(getattr(settings, "takeover_rationale_enrichment_enabled", True))
                and bool(getattr(settings, "takeover_citation_snippets_enabled", True))
                else []
            )
            state.working_set_json = {
                **working_set,
                "summary": clone_advice.guidance_summary,
                "do": clone_advice.do[:5],
                "dont": clone_advice.dont[:5],
                "evidence_count": len(clone_advice.citations),
                "citations": [str(v) for v in refreshed_citation_ids],
                "citation_snippets": refreshed_citation_snippets,
                "refreshed_at": now.isoformat(),
            }
            decision_source = TakeoverDecisionSource.DELIBERATION
            advisor_call_succeeded = True
            fast_path_reason = ""
        except Exception as exc:
            advisor_failure_reason = str(exc).strip()[:220] or exc.__class__.__name__
            fast_path_reason = "advisor_exception"
        deliberation_ms = int((time.perf_counter() - deliberation_started) * 1000)
        deliberation_timeout_ms = max(
            600,
            int(getattr(settings, "effective_advisor_attempt_timeout_ms", settings.advisor_attempt_timeout_ms)),
        )
        if deliberation_ms > deliberation_timeout_ms:
            candidate = fast_path_candidate
            clone_payload = fast_path_payload
            citations = fast_path_citations
            decision_source = TakeoverDecisionSource.FAST_PATH
            advisor_call_succeeded = False
            if not advisor_failure_reason:
                advisor_failure_reason = f"advisor_timeout_{deliberation_ms}ms"
            fast_path_reason = "advisor_timeout"
        if advisor_call_succeeded:
            advisor_fail_streak = 0
            state.takeover_context["advisor_fail_streak"] = 0
            state.takeover_context.pop("advisor_last_error", None)
        else:
            advisor_fail_streak += 1
            state.takeover_context["advisor_fail_streak"] = advisor_fail_streak
            if advisor_failure_reason:
                state.takeover_context["advisor_last_error"] = advisor_failure_reason
                if not fast_path_reason:
                    fast_path_reason = "advisor_error"
    if decision_source == TakeoverDecisionSource.DELIBERATION:
        fast_path_reason = ""
    clone_payload["fast_path_reason"] = fast_path_reason or None
    clone_payload["advisor_failure_reason"] = advisor_failure_reason
    state.takeover_context["last_fast_path_reason"] = fast_path_reason or None
    policy_retrieval_meta: dict[str, Any] = {}
    policy_value = working_set.get("policy")
    if isinstance(policy_value, dict):
        retrieval_value = policy_value.get("retrieval")
        if isinstance(retrieval_value, dict):
            policy_retrieval_meta = retrieval_value
    if retrieval_triggered:
        raw_source = str(policy_retrieval_meta.get("source") or "").strip()
        if raw_source in {"pgvector_ann", "lexical_only", "hybrid_fallback", "qdrant"}:
            retrieval_source = raw_source
        elif decision_source == TakeoverDecisionSource.DELIBERATION:
            retrieval_source = "hybrid_fallback"
        else:
            retrieval_source = "pgvector_ann" if evidence_count > 0 else "lexical_only"
    if retrieval_source_override is not None:
        retrieval_source = retrieval_source_override
    feedback_adjustment_applied = bool(policy_retrieval_meta.get("feedback_adjustment_applied", False))
    query_expansion_used_meta = bool(policy_retrieval_meta.get("query_expansion_used", False))
    raw_query_expansion_terms = policy_retrieval_meta.get("query_expansion_terms", [])
    query_expansion_terms_meta = [
        str(value).strip()
        for value in raw_query_expansion_terms
        if isinstance(value, str) and str(value).strip()
    ][: max(0, int(getattr(settings, "search_query_expansion_max_terms", 4)))]
    rerank_strategy_meta = str(policy_retrieval_meta.get("rerank_strategy") or "none")
    retrieval_latency_ms = retrieval_ms + deliberation_ms
    retrieval_hit_count = evidence_count
    objective_for_plan = str(state.takeover_context.get("objective") or resolved_task or "")
    if objective_for_plan and state.mode == TakeoverMode.TAKEOVER:
        _ensure_objective_contract_context_lite(
            state.takeover_context,
            objective=objective_for_plan,
            settings=settings,
            updated_at=now,
        )
    if _is_complex_objective_text_lite(objective_for_plan):
        learned_step_templates = _load_learned_workflow_step_templates_lite(
            conn,
            workspace_id=auth.workspace_id,
            limit=3,
            min_reliability=float(getattr(settings, "workflow_template_reuse_min_reliability", 0.70)),
        )
        suggested_steps = [
            str(item).strip()
            for item in clone_payload.get("do", [])
            if isinstance(item, str) and str(item).strip()
        ]
        structured_plan = _build_structured_plan_payload_lite(
            objective=objective_for_plan,
            suggested_steps=suggested_steps,
            learned_templates=learned_step_templates,
        )
        clone_payload["structured_plan"] = structured_plan
        state.takeover_context["structured_plan"] = structured_plan
    contract_payload = state.takeover_context.get("objective_contract")
    if isinstance(contract_payload, dict):
        clone_payload["objective_contract"] = contract_payload

    response_text, enforced, enforcement_reason, classification = ensure_takeover_response(
        mode=state.mode,
        text=candidate,
        task=resolved_task,
        takeover_context=state.takeover_context,
        advice=clone_payload,
        semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
        semantic_threshold=float(getattr(settings, "semantic_classifier_intent_threshold", 0.67)),
        semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
    )
    final_response: str | None = response_text
    # If ensure_takeover_response returned empty (edge case), build a
    # decisive fallback so the executor always gets actionable guidance
    # during active takeover instead of null (which causes loops/stops).
    if not final_response and state.active:
        final_response = build_decisive_response(
            task=resolved_task,
            takeover_context=state.takeover_context,
            advice=clone_payload,
        )
        enforced = True
        enforcement_reason = "empty_fallback_decisive"
    elif not final_response:
        final_response = None
    safety_started = time.perf_counter()
    safety_decision, safety_reason = evaluate_safety(
        policy=body.policy,
        message=message,
        final_response=final_response,
        takeover_context=state.takeover_context,
    )
    safety_ms = int((time.perf_counter() - safety_started) * 1000)
    if safety_decision != SafetyDecision.ALLOW:
        decision_source = TakeoverDecisionSource.SAFETY_GATE
    takeover_enforcement: dict[str, Any] = {}
    if enforced:
        note = "enforced decisive takeover response"
        if enforcement_reason and "weak_evidence" in enforcement_reason:
            note = "weak evidence — enforced explore-and-implement directive"
        elif enforcement_reason and "empty" in enforcement_reason:
            note = "empty advisor output — enforced decisive fallback"
        takeover_enforcement = {
            "trigger": enforcement_reason,
            "mode": state.mode.value,
            "note": note,
        }
    if safety_decision == SafetyDecision.CONFIRM_REQUIRED:
        state.takeover_context["pending_safety"] = {
            "reason": safety_reason or "high-risk-action",
            "pending_response": final_response,
        }
        confirm_phrase = body.policy.confirm_keyword
        deny_phrase = body.policy.deny_keyword
        final_response = (
            f"Safety pause: high-risk action detected ({safety_reason or 'high-risk'}). "
            f"Type '{confirm_phrase}' to continue or '{deny_phrase}' to abort."
        )
    elif safety_decision == SafetyDecision.BLOCKED:
        state.takeover_context.pop("pending_safety", None)
        final_response = "High-risk action aborted by operator decision."
    elif safety_reason == "confirmed_high_risk":
        state.takeover_context.pop("pending_safety", None)

    autonomy_value = max(0.0, min(1.0, float(state.autonomy_score or 0.5)))
    low_threshold = max(0.0, min(1.0, float(settings.takeover_needs_human_threshold_cold)))
    high_threshold = max(0.0, min(1.0, float(settings.takeover_needs_human_threshold_hot)))
    ramp_start = max(0.0, min(1.0, float(settings.takeover_needs_human_ramp_start)))
    ramp_end = max(0.0, min(1.0, float(settings.takeover_needs_human_ramp_end)))
    if ramp_end <= ramp_start:
        needs_human_threshold = high_threshold
    elif autonomy_value <= ramp_start:
        needs_human_threshold = low_threshold
    elif autonomy_value >= ramp_end:
        needs_human_threshold = high_threshold
    else:
        ratio = (autonomy_value - ramp_start) / (ramp_end - ramp_start)
        needs_human_threshold = low_threshold + ((high_threshold - low_threshold) * ratio)
    needs_human_threshold = round(max(0.0, min(1.0, needs_human_threshold)), 4)
    evidence_observations = clone_payload.get("evidence_observations", [])
    semantic_ratio = 0.0
    if isinstance(evidence_observations, list) and evidence_observations:
        semantic_hits = sum(
            1
            for item in evidence_observations
            if isinstance(item, dict) and str(item.get("recall_source", "")).strip().lower() == "semantic"
        )
        semantic_ratio = semantic_hits / max(1, len(evidence_observations))
    if state.autonomy_policy_profile == AutonomyPolicyProfile.HUMAN_CONSULTATIVE:
        needs_human_threshold = adjust_consultative_threshold(
            needs_human_threshold,
            recent_outcomes=state.recent_outcomes_json,
            semantic_ratio=semantic_ratio,
        )
    needs_human_threshold = round(
        max(
            0.0,
            min(1.0, needs_human_threshold + float(profile_tuning.get("needs_human_delta", 0.0))),
        ),
        4,
    )
    suppress_auto_directive = bool(
        (
            awaiting_next_objective
            or bool(state.takeover_context.get("force_user_objective_refresh"))
        )
        and not has_new_objective_signal
        and pending_execution is None
        and safety_decision == SafetyDecision.ALLOW
    )
    if suppress_auto_directive:
        if bool(state.takeover_context.get("force_user_objective_refresh")):
            final_response = "Objective drift detected. Provide one concrete next objective so I can continue correctly."
            state.takeover_context["objective_needs_refresh"] = True
        else:
            final_response = "Previous objective completed. Tell me the next concrete task to continue."
        takeover_enforcement = {}
        enforced = False
        enforcement_reason = None
    execution_permit_required = (
        (not suppress_auto_directive)
        and state.mode == TakeoverMode.TAKEOVER
        and _is_mutating_intent(
            message,
            resolved_task,
            final_response,
        )
    )
    execution_permit_id: UUID | None = None
    if execution_permit_required:
        permit_row = conn.execute(
            """
            SELECT id
            FROM execution_permits
            WHERE session_id = ? AND workspace_id = ? AND decision = ?
              AND (expires_at IS NULL OR expires_at >= ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                state.session_id,
                state.workspace_id,
                ExecutionPermitDecision.ALLOW.value,
                now_utc().isoformat(),
            ),
        ).fetchone()
        if permit_row:
            execution_permit_id = UUID(str(permit_row["id"]))
    if execution_permit_required and execution_permit_id is None and safety_decision == SafetyDecision.ALLOW:
        final_response = (
            "Execution permit required for mutating action. "
            "Call tce.request_execution_permit before continuing."
        )
        decision_source = TakeoverDecisionSource.SAFETY_GATE
    dependency_plan_meta = None
    dependency_preflight: dict[str, Any] = {"valid": True, "error": None}
    if isinstance(body.constraints, dict):
        candidate_dependency_plan = body.constraints.get("dependency_plan")
        if isinstance(candidate_dependency_plan, dict):
            dependency_plan_meta = candidate_dependency_plan
            plan_valid, plan_error = _validate_dependency_plan_lite(candidate_dependency_plan)
            dependency_preflight = {"valid": bool(plan_valid), "error": plan_error}
            if not plan_valid and safety_decision == SafetyDecision.ALLOW:
                final_response = (
                    f"Dependency preflight failed: {plan_error}. "
                    "Fix dependency_plan and retry."
                )
                decision_source = TakeoverDecisionSource.SAFETY_GATE
    directive_id: UUID | None = pending_execution.directive_id if pending_execution is not None else None
    directive_state: DirectiveExecutionState | None = pending_execution.state if pending_execution is not None else None
    retry_scheduled = False
    execution_claim_required = False
    if (
        state.mode == TakeoverMode.TAKEOVER
        and final_response
        and safety_decision == SafetyDecision.ALLOW
        and pending_execution is None
        and not suppress_auto_directive
        and bool(dependency_preflight.get("valid", True))
    ):
        directive_id = uuid.uuid4()
        directive_state = DirectiveExecutionState.PENDING
        now_for_directive = now_utc()
        claim_expires = now_for_directive + timedelta(seconds=max(30, int(settings.takeover_execution_claim_ttl_seconds)))
        if execution_permit_id is not None:
            permit_expiry_row = conn.execute(
                """
                SELECT expires_at
                FROM execution_permits
                WHERE id = ? AND session_id = ? AND workspace_id = ?
                LIMIT 1
                """,
                (str(execution_permit_id), state.session_id, state.workspace_id),
            ).fetchone()
            if permit_expiry_row and permit_expiry_row["expires_at"]:
                try:
                    permit_expiry_dt = datetime.fromisoformat(str(permit_expiry_row["expires_at"]))
                    if permit_expiry_dt < claim_expires:
                        claim_expires = permit_expiry_dt
                except Exception:
                    pass
        conn.execute(
            """
            INSERT INTO directive_executions(
                directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(directive_id),
                state.session_id,
                state.workspace_id,
                state.user_id,
                str(selected_goal.id) if selected_goal else None,
                state.objective_hash,
                "takeover_step",
                1,
                DirectiveExecutionState.PENDING.value,
                1 if execution_permit_required else 0,
                str(execution_permit_id) if execution_permit_id else None,
                None,
                None,
                None,
                claim_expires.isoformat(),
                None,
                None,
                None,
                json_dumps(
                    {
                        "objective": resolved_task,
                        "final_response": final_response,
                        "verification_required": _required_verification_checks_lite(settings),
                        "decision_confidence": float(round(decision_confidence, 4)),
                        "context_quality_score": float(round(context_quality_score, 4)),
                        "retrieval_triggered": bool(retrieval_triggered),
                        "dependency_plan": dependency_plan_meta,
                        "dependency_preflight": dependency_preflight,
                        "objective_contract_state": (
                            state.takeover_context.get("objective_contract", {}).get("completion_state")
                            if isinstance(state.takeover_context.get("objective_contract"), dict)
                            else None
                        ),
                    }
                ),
                now_for_directive.isoformat(),
                now_for_directive.isoformat(),
            ),
        )
        pending_execution = conn.execute(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE directive_id = ?
            LIMIT 1
            """,
            (str(directive_id),),
        ).fetchone()
        if pending_execution is not None:
            pending_execution = _directive_from_row(pending_execution)
    if execution_permit_required and execution_permit_id is not None:
        if pending_execution is None:
            execution_claim_required = True
        elif pending_execution.state != DirectiveExecutionState.IN_PROGRESS:
            execution_claim_required = True
            directive_id = pending_execution.directive_id
            directive_state = pending_execution.state
    if execution_claim_required and safety_decision == SafetyDecision.ALLOW:
        final_response = (
            "Execution claim required for this mutating directive. "
            "Call tce.claim_execution and then continue."
        )
        decision_source = TakeoverDecisionSource.SAFETY_GATE
    actionable_lifecycle_pause = (
        safety_decision == SafetyDecision.ALLOW
        and ((execution_permit_required and execution_permit_id is None) or execution_claim_required)
    )
    advisor_fail_streak_limit = max(1, int(getattr(settings, "takeover_advisor_fail_streak_escalate", 2)))
    advisor_unhealthy = bool(
        state.mode == TakeoverMode.TAKEOVER
        and advisor_required_in_takeover
        and should_call_clone_advice
        and advisor_fail_streak >= advisor_fail_streak_limit
    )
    behavior_fidelity_gate = {"enabled": False, "passed": True, "reason": "gate_disabled"}
    behavior_gate_blocked = False
    if bool(getattr(settings, "behavior_autonomy_gate_enabled", False)):
        behavior_fidelity_gate = {
            "enabled": True,
            **latest_fidelity_gate_lite(
                conn,
                workspace_id=auth.workspace_id,
                subject_user_id=auth.behavior_subject_id,
            ),
        }
        behavior_gate_blocked = not bool(behavior_fidelity_gate.get("passed", False))
    needs_human = (
        (decision_confidence < needs_human_threshold)
        or (retrieval_triggered and context_quality_score < float(settings.context_retrieval_escalate_score))
        or advisor_unhealthy
        or behavior_gate_blocked
        or (safety_decision != SafetyDecision.ALLOW)
    ) and not actionable_lifecycle_pause
    if advisor_unhealthy and safety_decision == SafetyDecision.ALLOW and not actionable_lifecycle_pause:
        decision_source = TakeoverDecisionSource.SAFETY_GATE
        if not final_response:
            final_response = (
                "Advisor runtime is unavailable in takeover mode. "
                "Fix advisor route/model connectivity, then continue execution."
            )
    if behavior_gate_blocked and safety_decision == SafetyDecision.ALLOW and not actionable_lifecycle_pause:
        decision_source = TakeoverDecisionSource.SAFETY_GATE
        final_response = (
            "Behavior fidelity is not validated for autonomous continuation. "
            "Confirm the preferred choice or run a behavior fidelity evaluation."
        )
    quality_history = state.takeover_context.get("quality_history")
    if not isinstance(quality_history, list):
        quality_history = []
    quality_history.append(
        {
            "turn": int(turn_count),
            "decision_confidence": float(round(decision_confidence, 4)),
            "context_quality_score": float(round(context_quality_score, 4)),
            "retrieval_triggered": bool(retrieval_triggered),
            "needs_human": bool(needs_human),
            "decision_source": decision_source.value,
            "advisor_fail_streak": int(advisor_fail_streak),
            "advisor_unhealthy": bool(advisor_unhealthy),
            "advisor_required_in_takeover": bool(advisor_required_in_takeover),
            "advisor_last_error": state.takeover_context.get("advisor_last_error"),
            "ts": now_utc().isoformat(),
        }
    )
    quality_history = quality_history[-120:]
    state.takeover_context["quality_history"] = quality_history
    avg_conf = (
        sum(_safe_float(item.get("decision_confidence"), 0.0) for item in quality_history) / max(1, len(quality_history))
    )
    avg_ctx = (
        sum(_safe_float(item.get("context_quality_score"), 0.0) for item in quality_history) / max(1, len(quality_history))
    )
    needs_human_rate_hist = (
        sum(1 for item in quality_history if bool(item.get("needs_human"))) / max(1, len(quality_history))
    )
    state.takeover_context["quality_rollup"] = {
        "window": len(quality_history),
        "avg_decision_confidence": round(max(0.0, min(1.0, avg_conf)), 4),
        "avg_context_quality": round(max(0.0, min(1.0, avg_ctx)), 4),
        "needs_human_rate": round(max(0.0, min(1.0, needs_human_rate_hist)), 4),
        "updated_at": now_utc().isoformat(),
    }
    state.last_classification = classification
    state.last_safety_decision = safety_decision
    state.autonomy_score = round((0.8 * float(state.autonomy_score)) + (0.2 * decision_confidence), 4)
    state.enforcement_mode = settings.takeover_enforcement_mode
    _sync_enforcement_counters(conn, state)
    save_takeover_state(conn, state)

    total_ms = int((time.perf_counter() - started_total) * 1000)
    _record_takeover_action(
        conn,
        state=state,
        turn=turn_count,
        action_kind="takeover_step",
        result="needs_human" if needs_human else "success",
        latency_ms=total_ms,
        meta={
            "decision_source": decision_source.value,
            "decision_confidence": decision_confidence,
            "needs_human_threshold": needs_human_threshold,
            "classification": classification.value,
            "needs_human": needs_human,
            "context_quality_score": context_quality_score,
            "retrieval_triggered": retrieval_triggered,
            "retrieval_source": retrieval_source,
            "retrieval_reason": retrieval_reason,
            "retrieval_latency_ms": retrieval_latency_ms,
            "retrieval_hit_count": retrieval_hit_count,
            "feedback_adjustment_applied": feedback_adjustment_applied,
            "snapshot_rehydrated": snapshot_rehydrated,
            "snapshot_age_hours": snapshot_age_hours,
            "query_expansion_used": query_expansion_used_meta,
            "query_expansion_terms": query_expansion_terms_meta,
            "rerank_strategy": rerank_strategy_meta,
            "execution_permit_required": execution_permit_required,
            "execution_permit_id": str(execution_permit_id) if execution_permit_id else None,
            "directive_id": str(directive_id) if directive_id else None,
            "directive_state": directive_state.value if directive_state else None,
            "retry_scheduled": retry_scheduled,
            "continuity_ok": continuity_ok,
        },
    )

    goal_score_breakdown: dict[str, float] = {}
    if selected_goal is not None:
        goal_score_breakdown = {
            "selection_score": float(selected_goal.selection_score or 0.0),
            "priority_score": float(selected_goal.priority_score or 0.0),
            "confidence": float(selected_goal.confidence or 0.0),
        }

    open_notice_row = conn.execute(
        """
        SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority, expires_at, created_at, acknowledged_at
        FROM autonomy_notices
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
          AND acknowledged_at IS NULL
          AND (expires_at IS NULL OR expires_at >= ?)
        ORDER BY priority DESC, created_at DESC
        LIMIT 1
        """,
        (state.session_id, state.workspace_id, state.user_id, now_utc().isoformat()),
    ).fetchone()
    autonomy_notice = _notice_from_row(open_notice_row) if open_notice_row is not None else None

    return TakeoverStepResponse(
        state=state,
        action="advisor_takeover" if state.mode == TakeoverMode.TAKEOVER else "advisor_suggest",
        classification=classification,
        enforced=enforced,
        enforcement_reason=enforcement_reason,
        final_response=final_response,
        safety_decision=safety_decision,
        takeover_enforcement=takeover_enforcement,
        clone_advice=clone_payload,
        citations=citations,
        note=None if not enforced else final_response,
        decision_confidence=decision_confidence,
        decision_source=decision_source,
        next_action=TakeoverNextAction.model_validate(
            build_next_action(
                objective=resolved_task,
                mode=state.mode,
                safety_decision=safety_decision,
                needs_human=needs_human,
            )
        ),
        latency_breakdown_ms=TakeoverLatencyBreakdown(
            state=state_ms,
            classify=classify_ms,
            retrieval=retrieval_ms + deliberation_ms,
            safety=safety_ms,
            total=total_ms,
        ),
        needs_human=needs_human,
        selected_goal=selected_goal,
        execution_permit_required=execution_permit_required,
        execution_permit_id=str(execution_permit_id) if execution_permit_id else None,
        continuity_ok=continuity_ok,
        directive_id=directive_id,
        directive_state=directive_state,
        pending_execution=pending_execution if isinstance(pending_execution, DirectiveExecution) else None,
        retry_scheduled=retry_scheduled,
        autonomy_notice=autonomy_notice,
        goal_cache_hit=bool(state.takeover_context.get("_goal_cache_hit", False)),
        goal_cache_source=str(state.takeover_context.get("_goal_cache_source"))
        if state.takeover_context.get("_goal_cache_source") is not None
        else None,
        selected_goal_score_breakdown=goal_score_breakdown,
        context_quality_score=context_quality_score,
        retrieval_triggered=retrieval_triggered,
        retrieval_source=retrieval_source,
        retrieval_reason=retrieval_reason,
        retrieval_latency_ms=retrieval_latency_ms,
        retrieval_hit_count=retrieval_hit_count,
        feedback_adjustment_applied=feedback_adjustment_applied,
        snapshot_rehydrated=snapshot_rehydrated,
        snapshot_age_hours=snapshot_age_hours,
        query_expansion_used=query_expansion_used_meta,
        query_expansion_terms=query_expansion_terms_meta,
        rerank_strategy=rerank_strategy_meta,
        context_tier_used=str(working_set.get("context_tier_used") or "l2"),
        summary_coverage=float(working_set.get("summary_coverage", 0.0) or 0.0),
        planner_used=bool(working_set.get("planner_used", False)),
        subquery_count=int(working_set.get("subquery_count", 0) or 0),
        subquery_labels=list(working_set.get("subquery_labels") or []),
        episode_boost_applied=bool(working_set.get("episode_boost_applied", False)),
        activation_boost_applied=bool(working_set.get("activation_boost_applied", False)),
        behavior_fidelity_gate=behavior_fidelity_gate,
    )


# ---------------------------------------------------------------------------
def context_retrieval_status(settings: Settings) -> dict[str, Any]:
    return {
        "mode": str(getattr(settings, "vector_backend", "pgvector")),
        "qdrant_enabled": bool(getattr(settings, "qdrant_enabled", False)),
        "qdrant_timeout_ms": int(getattr(settings, "qdrant_timeout_ms", 60)),
        "trigger_threshold": float(getattr(settings, "context_retrieval_trigger_score", 0.68)),
        "escalate_threshold": float(getattr(settings, "context_retrieval_escalate_score", 0.52)),
        "turn_budget_ms": int(getattr(settings, "context_retrieval_budget_ms", 120)),
        "backend_timeout_ms": int(getattr(settings, "context_backend_timeout_ms", 60)),
        "query_expansion_enabled": bool(getattr(settings, "search_query_expansion_enabled", False)),
        "query_expansion_timeout_ms": int(getattr(settings, "search_query_expansion_timeout_ms", 25)),
        "query_expansion_max_terms": int(getattr(settings, "search_query_expansion_max_terms", 4)),
        "query_expansion_skip_candidate_multiplier": int(
            getattr(settings, "search_query_expansion_skip_candidate_multiplier", 4)
        ),
        "query_expansion_skip_dup_ratio": float(getattr(settings, "search_query_expansion_skip_dup_ratio", 0.80)),
        "mmr_enabled": bool(getattr(settings, "search_mmr_enabled", False)),
        "mmr_lambda": float(getattr(settings, "search_mmr_lambda", 0.65)),
        "mmr_candidate_pool": int(getattr(settings, "search_mmr_candidate_pool", 60)),
        "enhancement_budget_ms": int(getattr(settings, "search_enhancement_budget_ms", 120)),
        "fallback_counters": dict(_RETRIEVAL_COUNTERS_LITE),
        "generated_at": now_utc().isoformat(),
    }


# ---------------------------------------------------------------------------
def _refresh_graph_health_status_lite(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    workspace_id: str,
    owner_id: str,
) -> dict[str, Any]:
    now = now_utc()
    window_minutes = max(5, int(getattr(settings, "graph_health_alert_threshold_zero_entities_minutes", 30)))
    window_start = (now - timedelta(minutes=window_minutes)).isoformat()
    entity_total_row = conn.execute(
        "SELECT COUNT(1) AS total FROM entity_nodes WHERE workspace_id = ? AND owner_id = ?",
        (workspace_id, owner_id),
    ).fetchone()
    entity_total = int((entity_total_row["total"] if entity_total_row else 0) or 0)
    link_total_row = conn.execute(
        "SELECT COUNT(1) AS total FROM event_entity_links WHERE workspace_id = ? AND owner_id = ?",
        (workspace_id, owner_id),
    ).fetchone()
    link_total = int((link_total_row["total"] if link_total_row else 0) or 0)
    searchable_row = conn.execute(
        """
        SELECT COUNT(DISTINCT en.id) AS total
        FROM entity_nodes en
        JOIN event_entity_links eel ON eel.entity_id = en.id
        WHERE en.workspace_id = ? AND en.owner_id = ?
          AND eel.workspace_id = ? AND eel.owner_id = ?
        """,
        (workspace_id, owner_id, workspace_id, owner_id),
    ).fetchone()
    searchable_entity_count = int((searchable_row["total"] if searchable_row else 0) or 0)
    entity_window_row = conn.execute(
        """
        SELECT COUNT(1) AS total
        FROM entity_nodes
        WHERE workspace_id = ? AND owner_id = ? AND created_at >= ?
        """,
        (workspace_id, owner_id, window_start),
    ).fetchone()
    entity_created_window = int((entity_window_row["total"] if entity_window_row else 0) or 0)
    link_window_row = conn.execute(
        """
        SELECT COUNT(1) AS total
        FROM event_entity_links
        WHERE workspace_id = ? AND owner_id = ? AND created_at >= ?
        """,
        (workspace_id, owner_id, window_start),
    ).fetchone()
    link_created_window = int((link_window_row["total"] if link_window_row else 0) or 0)
    setting_key = f"graph_health_status:{workspace_id}:{owner_id}"
    previous_row = conn.execute(
        "SELECT value FROM runtime_settings WHERE key = ? LIMIT 1",
        (setting_key,),
    ).fetchone()
    previous_payload = json_loads(previous_row["value"] if previous_row else None, {})
    zero_since: str | None
    if searchable_entity_count <= 0:
        prior_zero_since = str(previous_payload.get("zero_entities_since") or "").strip()
        zero_since = prior_zero_since or now.isoformat()
    else:
        zero_since = None
    zero_minutes = 0.0
    if zero_since:
        try:
            zero_minutes = max(0.0, (now - datetime.fromisoformat(zero_since)).total_seconds() / 60.0)
        except ValueError:
            zero_minutes = 0.0
    alert = bool(searchable_entity_count <= 0 and zero_minutes >= float(window_minutes))
    status = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "window_minutes": window_minutes,
        "entity_total": entity_total,
        "link_total": link_total,
        "searchable_entity_count": searchable_entity_count,
        "entity_creation_rate_per_min": round(float(entity_created_window) / float(window_minutes), 4),
        "link_creation_rate_per_min": round(float(link_created_window) / float(window_minutes), 4),
        "entity_created_in_window": entity_created_window,
        "link_created_in_window": link_created_window,
        "zero_entities_since": zero_since,
        "zero_entities_minutes": round(zero_minutes, 2),
        "alert": alert,
        "generated_at": now.isoformat(),
    }
    conn.execute(
        """
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (setting_key, json_dumps(status), now.isoformat()),
    )
    return status


def _flush_session_snapshots_before_lifecycle_lite(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
) -> int:
    rows = conn.execute(
        """
        SELECT session_id, workspace_id, user_id
        FROM takeover_sessions
        WHERE objective_hash IS NOT NULL
          AND (active = 1 OR working_set_json <> '{}')
        ORDER BY updated_at DESC
        """
    ).fetchall()
    created = 0
    for row in rows:
        state = takeover_state(
            conn=conn,
            session_id=str(row["session_id"]),
            workspace_id=str(row["workspace_id"]),
            user_id=str(row["user_id"]),
        )
        snapshot_id = _save_session_memory_snapshot_lite(
            conn,
            state=state,
            reason="pre_lifecycle_flush",
            max_per_session=max(1, int(getattr(settings, "snapshot_max_per_session", 10))),
        )
        if snapshot_id:
            created += 1
    return created


# ---------------------------------------------------------------------------
def lifecycle_status(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    status_row = conn.execute(
        "SELECT value FROM runtime_settings WHERE key = ?",
        ("lifecycle_status",),
    ).fetchone()
    retention_row = conn.execute(
        "SELECT value FROM runtime_settings WHERE key = ?",
        ("event_retention",),
    ).fetchone()
    return {
        "enabled": bool(settings.event_lifecycle_enabled),
        "retention": json_loads(
            retention_row["value"] if retention_row else None,
            {
                "retention_days": int(settings.event_retention_days),
                "handoff_retention_days": int(getattr(settings, "handoff_retention_days", 90)),
                "behavior_control_retention_days": int(
                    getattr(settings, "behavior_control_retention_days", 365)
                ),
                "archive_enabled": bool(settings.archive_enabled),
                "archive_path": settings.archive_path,
            },
        ),
        "last_run": json_loads(status_row["value"] if status_row else None, {}),
        "generated_at": now_utc().isoformat(),
    }


def graph_health_status_lite(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    workspace_id: str,
    owner_id: str,
    refresh: bool = True,
) -> dict[str, Any]:
    setting_key = f"graph_health_status:{workspace_id}:{owner_id}"
    if refresh:
        status = _refresh_graph_health_status_lite(
            conn,
            settings=settings,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        conn.commit()
        return status
    row = conn.execute("SELECT value FROM runtime_settings WHERE key = ? LIMIT 1", (setting_key,)).fetchone()
    payload = json_loads(row["value"] if row else None, {})
    if isinstance(payload, dict) and payload:
        return payload
    return _refresh_graph_health_status_lite(
        conn,
        settings=settings,
        workspace_id=workspace_id,
        owner_id=owner_id,
    )


def run_lifecycle_maintenance(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    retention_days: int | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    if not settings.event_lifecycle_enabled:
        return {
            "status": "disabled",
            "reason": "event_lifecycle_enabled=false",
            "generated_at": now_utc().isoformat(),
        }

    now = now_utc()
    retention = int(retention_days if retention_days is not None else settings.event_retention_days)
    effective_dry_run = bool(settings.lifecycle_dry_run if dry_run is None else dry_run)
    cutoff = now - timedelta(days=retention)
    max_rows = max(1, int(settings.lifecycle_max_delete_rows_per_run))
    archive_enabled = bool(settings.archive_enabled)
    archive_path = Path(settings.archive_path)

    summary: dict[str, Any] = {
        "status": "ok",
        "ran_at": now.isoformat(),
        "retention_days": retention,
        "handoff_retention_days": int(getattr(settings, "handoff_retention_days", 90)),
        "behavior_control_retention_days": int(getattr(settings, "behavior_control_retention_days", 365)),
        "dry_run": effective_dry_run,
        "archive_enabled": archive_enabled,
        "archive_path": str(archive_path),
        "cutoff": cutoff.isoformat(),
        "events_deleted": 0,
        "handoff_rows_deleted": 0,
        "behavior_control_rows_deleted": 0,
        "audit_rows_deleted": 0,
        "interaction_rows_deleted": 0,
        "patterns_pruned": 0,
        "archived_rows": 0,
        "archive_file": None,
        "session_snapshots_created": 0,
        "graph_health_scopes_updated": 0,
    }
    summary["session_snapshots_created"] = _flush_session_snapshots_before_lifecycle_lite(
        conn,
        settings=settings,
    )

    old_events = conn.execute(
        """
        SELECT id, ts, actor, source, domain, task_type, event_type, title, payload, context
        FROM events
        WHERE ts < ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (cutoff.isoformat(), max_rows),
    ).fetchall()
    old_event_ids = [str(row["id"]) for row in old_events]

    if old_events and archive_enabled:
        archive_file = archive_path / f"lite-events-{now.strftime('%Y%m%d%H%M%S')}.jsonl"
        summary["archive_file"] = str(archive_file)
        summary["archived_rows"] = len(old_events)
        if not effective_dry_run:
            archive_path.mkdir(parents=True, exist_ok=True)
            with archive_file.open("w", encoding="utf-8") as handle:
                for row in old_events:
                    handle.write(json.dumps(dict(row), sort_keys=True, default=str))
                    handle.write("\n")

    if old_event_ids and not effective_dry_run:
        conn.executemany("DELETE FROM events WHERE id = ?", [(event_id,) for event_id in old_event_ids])
        summary["events_deleted"] = len(old_event_ids)

    audit_cutoff = (now - timedelta(days=int(settings.audit_retention_days))).isoformat()
    interaction_cutoff = (now - timedelta(days=int(settings.interaction_retention_days))).isoformat()
    if not effective_dry_run:
        deleted_audit = conn.execute("DELETE FROM audit_log WHERE ts < ?", (audit_cutoff,))
        summary["audit_rows_deleted"] = int(deleted_audit.rowcount or 0)
        deleted_interactions = conn.execute("DELETE FROM agent_interactions WHERE ts < ?", (interaction_cutoff,))
        summary["interaction_rows_deleted"] = int(deleted_interactions.rowcount or 0)
        handoff_cutoff = (now - timedelta(days=max(1, int(getattr(settings, "handoff_retention_days", 90))))).isoformat()
        deleted_handoff = conn.execute(
            """
            DELETE FROM handoff_records
            WHERE expires_at < ?
               OR ts < ?
            """,
            (now.isoformat(), handoff_cutoff),
        )
        summary["handoff_rows_deleted"] = int(deleted_handoff.rowcount or 0)
        behavior_cutoff = (
            now - timedelta(days=max(1, int(getattr(settings, "behavior_control_retention_days", 365))))
        ).isoformat()
        behavior_deleted = 0
        for statement in (
            "DELETE FROM capability_grants WHERE created_at < ?",
            "DELETE FROM behavior_shadow_predictions WHERE created_at < ?",
            "DELETE FROM behavior_memory_reviews WHERE resolved_at IS NOT NULL AND resolved_at < ?",
            "DELETE FROM behavior_counterfactuals WHERE resolved_at IS NOT NULL AND resolved_at < ?",
            "DELETE FROM behavior_process_models WHERE status = 'rejected' AND updated_at < ?",
            "DELETE FROM behavior_projection_pilot_assignments WHERE assigned_at < ?",
        ):
            deleted = conn.execute(statement, (behavior_cutoff,))
            behavior_deleted += int(deleted.rowcount or 0)
        summary["behavior_control_rows_deleted"] = behavior_deleted

        patterns = conn.execute("SELECT id, evidence_event_ids FROM patterns").fetchall()
        pruned = 0
        for row in patterns:
            evidence_ids = json_loads(row["evidence_event_ids"], [])
            if not evidence_ids:
                continue
            placeholders = ",".join("?" for _ in evidence_ids)
            existing_rows = conn.execute(
                f"SELECT id FROM events WHERE id IN ({placeholders})",
                tuple(str(value) for value in evidence_ids),
            ).fetchall()
            existing = {str(item["id"]) for item in existing_rows}
            filtered = [str(value) for value in evidence_ids if str(value) in existing]
            if len(filtered) != len(evidence_ids):
                conn.execute(
                    "UPDATE patterns SET evidence_event_ids = ?, updated_at = ? WHERE id = ?",
                    (json_dumps(filtered), now.isoformat(), row["id"]),
                )
                pruned += 1
        summary["patterns_pruned"] = pruned

    retention_payload = {
        "retention_days": retention,
        "handoff_retention_days": int(getattr(settings, "handoff_retention_days", 90)),
        "behavior_control_retention_days": int(getattr(settings, "behavior_control_retention_days", 365)),
        "archive_enabled": archive_enabled,
        "archive_path": str(archive_path),
    }
    conn.execute(
        """
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("event_retention", json_dumps(retention_payload), now.isoformat()),
    )
    scopes = conn.execute(
        """
        SELECT DISTINCT workspace_id, owner_id
        FROM entity_nodes
        UNION
        SELECT DISTINCT workspace_id, user_id AS owner_id
        FROM takeover_sessions
        """
    ).fetchall()
    scopes_updated = 0
    for scope in scopes:
        workspace_id = str(scope["workspace_id"] or "").strip()
        owner_id = str(scope["owner_id"] or "").strip()
        if not workspace_id or not owner_id:
            continue
        _refresh_graph_health_status_lite(
            conn,
            settings=settings,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        scopes_updated += 1
    summary["graph_health_scopes_updated"] = scopes_updated
    conn.execute(
        """
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("lifecycle_status", json_dumps(summary), now.isoformat()),
    )
    conn.commit()
    return summary


# Dashboard helpers (fingerprint, observations, clone score)
# ---------------------------------------------------------------------------


def load_fingerprint_lite(
    conn: sqlite3.Connection, consumer_id: str, workspace_id: str
) -> dict[str, Any] | None:
    """Load a behavioral fingerprint from SQLite."""
    row = conn.execute(
        "SELECT fingerprint, observation_count, last_updated_at FROM behavioral_fingerprints WHERE consumer_id = ? AND workspace_id = ?",
        (consumer_id, workspace_id),
    ).fetchone()
    if not row:
        return None
    return {
        "fingerprint": json_loads(row["fingerprint"], {}),
        "observation_count": row["observation_count"],
        "last_updated_at": row["last_updated_at"],
    }


def save_fingerprint_lite(
    conn: sqlite3.Connection,
    consumer_id: str,
    workspace_id: str,
    fingerprint: dict[str, Any],
    observation_count: int,
) -> None:
    """Save a behavioral fingerprint to SQLite."""
    now = now_utc().isoformat()
    conn.execute(
        """
        INSERT INTO behavioral_fingerprints(id, consumer_id, workspace_id, fingerprint, observation_count, last_updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(consumer_id, workspace_id) DO UPDATE SET
            fingerprint = excluded.fingerprint,
            observation_count = excluded.observation_count,
            last_updated_at = excluded.last_updated_at
        """,
        (str(uuid.uuid4()), consumer_id, workspace_id, json_dumps(fingerprint), observation_count, now),
    )
    conn.commit()


def _sentiment_polarity_lite(value: str | None) -> int:
    token = str(value or "").strip().lower()
    if not token:
        return 0
    positive_tokens = (
        "positive",
        "success",
        "succeeded",
        "resolved",
        "fixed",
        "helpful",
        "approved",
        "allow",
        "completed",
        "done",
    )
    negative_tokens = (
        "negative",
        "fail",
        "failed",
        "error",
        "blocked",
        "unhelpful",
        "denied",
        "timeout",
        "abandon",
        "rollback",
    )
    if any(item in token for item in positive_tokens):
        return 1
    if any(item in token for item in negative_tokens):
        return -1
    return 0


def _observation_polarity_lite(outcome: str | None, outcome_sentiment: str | None) -> int:
    sentiment_polarity = _sentiment_polarity_lite(outcome_sentiment)
    if sentiment_polarity != 0:
        return sentiment_polarity
    return _sentiment_polarity_lite(outcome)


def _mark_superseded_observations_lite(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    workspace_id: str,
    consumer_id: str,
    situation_type: str,
    situation_summary: str,
    outcome: str | None,
    outcome_sentiment: str | None,
) -> None:
    new_polarity = _observation_polarity_lite(outcome, outcome_sentiment)
    normalized_summary = " ".join(str(situation_summary or "").strip().lower().split())
    if new_polarity == 0 or not normalized_summary:
        return
    rows = conn.execute(
        """
        SELECT id, outcome, outcome_sentiment
        FROM decision_observations
        WHERE workspace_id = ?
          AND consumer_id = ?
          AND situation_type = ?
          AND superseded_by IS NULL
          AND id <> ?
          AND lower(trim(situation_summary)) = ?
        ORDER BY ts DESC
        LIMIT 50
        """,
        (workspace_id, consumer_id, situation_type, observation_id, normalized_summary),
    ).fetchall()
    stale_ids = [
        str(row["id"])
        for row in rows
        if _observation_polarity_lite(row["outcome"], row["outcome_sentiment"]) == (new_polarity * -1)
    ]
    for stale_id in stale_ids:
        conn.execute(
            """
            UPDATE decision_observations
            SET superseded_by = ?
            WHERE id = ? AND superseded_by IS NULL
            """,
            (observation_id, stale_id),
        )


def save_observation_lite(conn: sqlite3.Connection, obs: dict[str, Any]) -> str:
    """Save a decision observation to SQLite and return observation id."""
    observation_id = str(uuid.uuid4())
    situation_type = _canonical_situation_type(str(obs.get("situation_type") or ""))
    normalized_summary = " ".join(str(obs.get("situation_summary", "")).strip().split())
    conn.execute(
        """
        INSERT INTO decision_observations(
            id, ts, consumer_id, workspace_id, situation_type, situation_summary,
            user_response, response_reasoning, outcome, outcome_sentiment,
            confidence, source_event_ids, context_snapshot, embedding, superseded_by
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            observation_id,
            now_utc().isoformat(),
            obs.get("consumer_id", "unknown"),
            obs.get("workspace_id", "default"),
            situation_type,
            normalized_summary,
            obs.get("user_response", ""),
            obs.get("response_reasoning"),
            obs.get("outcome"),
            obs.get("outcome_sentiment"),
            float(obs.get("confidence", 0.5)),
            json_dumps(obs.get("source_event_ids", [])),
            json_dumps(obs.get("context_snapshot", {})),
            json_dumps(obs.get("embedding", [])) if obs.get("embedding") else None,
        ),
    )
    _mark_superseded_observations_lite(
        conn,
        observation_id=observation_id,
        workspace_id=obs.get("workspace_id", "default"),
        consumer_id=obs.get("consumer_id", "unknown"),
        situation_type=situation_type,
        situation_summary=normalized_summary,
        outcome=obs.get("outcome"),
        outcome_sentiment=obs.get("outcome_sentiment"),
    )
    conn.commit()
    return observation_id


def save_behavior_evidence_lite(
    conn: sqlite3.Connection,
    *,
    consumer_id: str,
    workspace_id: str,
    subject_user_id: str,
    evidence: dict[str, Any],
    storage_gate: dict[str, Any],
) -> str:
    observation_id = str(uuid.uuid4())
    now = now_utc()
    valid_from = evidence.get("valid_from") or now
    if isinstance(valid_from, datetime):
        valid_from = valid_from.isoformat()
    valid_until = evidence.get("valid_until")
    if isinstance(valid_until, datetime):
        valid_until = valid_until.isoformat()
    confirmed_at = evidence.get("confirmed_at")
    if isinstance(confirmed_at, datetime):
        confirmed_at = confirmed_at.isoformat()
    conn.execute(
        """
        INSERT INTO decision_observations(
            id, ts, consumer_id, workspace_id, subject_user_id, situation_type, situation_summary,
            user_response, response_reasoning, outcome, outcome_sentiment,
            confidence, source_event_ids, context_snapshot, embedding, superseded_by,
            objective_text, constraints_json, available_choices_json, selected_choice,
            action_taken, correction_text, memory_class, evidence_source,
            lifecycle_status, valid_from, valid_until, contradicts_ids_json,
            confirmed_at, behavior_schema_version, redaction_applied,
            learning_eligible, storage_score, storage_decision
        ) VALUES(
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            observation_id,
            now.isoformat(),
            consumer_id,
            workspace_id,
            subject_user_id,
            str(evidence.get("situation_type") or "routine_task"),
            str(evidence.get("situation_summary") or ""),
            str(evidence.get("selected_choice") or ""),
            str(evidence.get("rationale") or "") or None,
            str(evidence.get("outcome") or "") or None,
            evidence.get("outcome_sentiment"),
            float(evidence.get("confidence", 1.0) or 0.0),
            json_dumps(evidence.get("source_event_ids") or []),
            json_dumps(evidence.get("context_snapshot") or {}),
            str(evidence.get("objective_text") or ""),
            json_dumps(evidence.get("constraints") or {}),
            json_dumps(evidence.get("available_choices") or []),
            str(evidence.get("selected_choice") or ""),
            str(evidence.get("action_taken") or ""),
            str(evidence.get("correction_text") or ""),
            str(evidence.get("memory_class") or "decision"),
            str(evidence.get("evidence_source") or "explicit"),
            str(evidence.get("lifecycle_status") or "active"),
            valid_from,
            valid_until,
            json_dumps(evidence.get("contradicts_observation_ids") or []),
            confirmed_at,
            str(evidence.get("schema_version") or "v1"),
            1 if evidence.get("redaction_applied") else 0,
            1 if storage_gate.get("learning_eligible") else 0,
            float(storage_gate.get("score", 0.0) or 0.0),
            str(storage_gate.get("decision") or "audit_only"),
        ),
    )
    supersedes = evidence.get("supersedes_observation_id")
    if supersedes:
        conn.execute(
            """
            UPDATE decision_observations
            SET superseded_by = ?, lifecycle_status = 'superseded'
            WHERE id = ? AND workspace_id = ? AND subject_user_id = ? AND superseded_by IS NULL
            """,
            (observation_id, str(supersedes), workspace_id, subject_user_id),
        )
    conn.commit()
    return observation_id


_BEHAVIOR_EVIDENCE_COLUMNS = """
    id, consumer_id, workspace_id, subject_user_id, ts, situation_type, situation_summary,
    context_snapshot, user_response, response_reasoning, outcome,
    outcome_sentiment, source_event_ids, confidence, superseded_by,
    objective_text, constraints_json, available_choices_json, selected_choice,
    action_taken, correction_text, memory_class, evidence_source,
    lifecycle_status, valid_from, valid_until, contradicts_ids_json,
    confirmed_at, behavior_schema_version, redaction_applied,
    learning_eligible, storage_score, storage_decision
"""


def _behavior_evidence_from_row_lite(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["context_snapshot"] = json_loads(item.get("context_snapshot"), {})
    item["constraints"] = json_loads(item.pop("constraints_json", "{}"), {})
    item["available_choices"] = json_loads(item.pop("available_choices_json", "[]"), [])
    item["source_event_ids"] = json_loads(item.get("source_event_ids"), [])
    item["contradicts_observation_ids"] = json_loads(item.pop("contradicts_ids_json", "[]"), [])
    item["learning_eligible"] = bool(item.get("learning_eligible"))
    return item


def load_behavior_evidence_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    limit: int = 5000,
    eligible_only: bool = True,
) -> list[dict[str, Any]]:
    eligibility = "AND learning_eligible = 1" if eligible_only else ""
    rows = conn.execute(
        f"""
        SELECT {_BEHAVIOR_EVIDENCE_COLUMNS}
        FROM decision_observations
        WHERE workspace_id = ? AND subject_user_id = ? {eligibility}
        ORDER BY ts DESC
        LIMIT ?
        """,
        (workspace_id, subject_user_id, max(1, min(limit, 5000))),
    ).fetchall()
    return [_behavior_evidence_from_row_lite(row) for row in reversed(rows)]


def load_behavior_evidence_by_id_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    observation_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT {_BEHAVIOR_EVIDENCE_COLUMNS}
        FROM decision_observations
        WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
        LIMIT 1
        """,
        (observation_id, workspace_id, subject_user_id),
    ).fetchone()
    return _behavior_evidence_from_row_lite(row) if row is not None else None


def save_fidelity_run_lite(
    conn: sqlite3.Connection,
    *,
    consumer_id: str,
    workspace_id: str,
    subject_user_id: str,
    config: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, datetime]:
    run_id = str(uuid.uuid4())
    created_at = now_utc()
    metrics = dict(result.get("metrics") or {})
    conn.execute(
        """
        INSERT INTO behavior_fidelity_runs(
            id, consumer_id, workspace_id, subject_user_id, created_at, status, config_json,
            metrics_json, gate_json, case_results_json, evidence_count,
            duration_ms, schema_version
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            run_id,
            consumer_id,
            workspace_id,
            subject_user_id,
            created_at.isoformat(),
            str(result.get("status") or "completed"),
            json_dumps(config),
            json_dumps(metrics),
            json_dumps(result.get("gate") or {}),
            json_dumps(result.get("case_results") or []),
            int(metrics.get("eligible_evidence_count", 0) or 0),
            int(result.get("duration_ms", 0) or 0),
        ),
    )
    conn.commit()
    return run_id, created_at


def list_fidelity_runs_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, status, config_json, metrics_json, gate_json, case_results_json,
               created_at, duration_ms, schema_version
        FROM behavior_fidelity_runs
        WHERE workspace_id = ? AND subject_user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (workspace_id, subject_user_id, max(1, min(limit, 100))),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["config"] = json_loads(item.pop("config_json", "{}"), {})
        item["metrics"] = json_loads(item.pop("metrics_json", "{}"), {})
        item["gate"] = json_loads(item.pop("gate_json", "{}"), {})
        item["case_results"] = json_loads(item.pop("case_results_json", "[]"), [])
        output.append(item)
    return output


def latest_fidelity_gate_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT gate_json, metrics_json, created_at
        FROM behavior_fidelity_runs
        WHERE workspace_id = ? AND subject_user_id = ? AND status = 'completed'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workspace_id, subject_user_id),
    ).fetchone()
    if row is None:
        return {"passed": False, "reason": "no_completed_fidelity_run"}
    return {
        **json_loads(row["gate_json"], {}),
        "metrics": json_loads(row["metrics_json"], {}),
        "evaluated_at": row["created_at"],
    }
