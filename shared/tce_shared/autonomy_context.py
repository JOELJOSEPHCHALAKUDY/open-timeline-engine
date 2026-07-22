from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_text

SUMMARY_VERSION = "v1"
_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")
_CONTINUITY_TOKENS = (
    "timeline",
    "memory",
    "handoff",
    "resume",
    "continue",
    "pick up",
    "read codex",
    "read claude",
)
_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "for",
    "on",
    "of",
    "in",
    "with",
    "from",
    "read",
    "work",
    "timeline",
    "memory",
    "continue",
    "resume",
    "pick",
    "up",
}
_FOCUS_DROPWORDS = _STOPWORDS | {
    "codex",
    "claude",
    "cursor",
    "executor",
    "executors",
    "agent",
    "agents",
    "stopped",
    "latest",
}


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _clip(value: Any, *, limit: int) -> str:
    token = _collapse_whitespace(value)
    if len(token) <= limit:
        return token
    return token[: max(1, limit - 3)].rstrip() + "..."


def _redact_clip(value: Any, *, limit: int) -> str:
    token, _ = redact_text(_collapse_whitespace(value))
    return _clip(token, limit=limit)


def _first_text(*values: Any) -> str:
    for value in values:
        token = _collapse_whitespace(value)
        if token:
            return token
    return ""


def _normalize_files(raw: Any, *, limit: int = 5) -> list[str]:
    if not isinstance(raw, list):
        return []
    files: list[str] = []
    seen: set[str] = set()
    for value in raw:
        token = _redact_clip(value, limit=240)
        if not token or token in seen:
            continue
        seen.add(token)
        files.append(token)
        if len(files) >= limit:
            break
    return files


def _normalize_anchors(raw: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    anchors: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_value = _redact_clip(item.get("file"), limit=240)
        if not file_value:
            continue
        raw_line = item.get("line")
        try:
            line_value = int(raw_line) if raw_line is not None else None
        except Exception:
            line_value = None
        symbol_value = _redact_clip(item.get("symbol"), limit=120) or None
        anchors.append({"file": file_value, "line": line_value, "symbol": symbol_value})
        if len(anchors) >= limit:
            break
    return anchors


def _anchor_from_checkpoint(payload: dict[str, Any]) -> list[dict[str, Any]]:
    file_value = _redact_clip(payload.get("active_file"), limit=240)
    if not file_value:
        return []
    raw_line = payload.get("line")
    try:
        line_value = int(raw_line) if raw_line is not None else None
    except Exception:
        line_value = None
    symbol_value = _redact_clip(payload.get("symbol"), limit=120) or None
    return [{"file": file_value, "line": line_value, "symbol": symbol_value}]


def summarize_event_record(
    *,
    title: Any,
    task_type: Any,
    domain: Any,
    payload: Any,
    decision: Any,
    outcome: Any,
) -> tuple[str, dict[str, Any]]:
    payload_map: dict[str, Any] = payload if isinstance(payload, dict) else {}
    decision_map: dict[str, Any] = decision if isinstance(decision, dict) else {}
    outcome_map: dict[str, Any] = outcome if isinstance(outcome, dict) else {}
    payload_outcome_value = payload_map.get("outcome")
    payload_outcome: dict[str, Any] = payload_outcome_value if isinstance(payload_outcome_value, dict) else {}

    status = _first_text(
        payload_outcome.get("status"),
        outcome_map.get("status"),
        "succeeded" if outcome_map.get("success") is True else "",
        "failed" if outcome_map.get("success") is False else "",
    )
    next_step = _first_text(
        payload_outcome.get("next_step"),
        outcome_map.get("next_step"),
        (outcome_map.get("followups") or [None])[0] if isinstance(outcome_map.get("followups"), list) else "",
    )
    decision_text = _first_text(
        payload_map.get("decision"),
        decision_map.get("rationale"),
        decision_map.get("choice"),
        payload_map.get("reasoning_summary"),
    )
    files = _normalize_files(
        payload_map.get("files")
        if isinstance(payload_map.get("files"), list)
        else payload_map.get("files_modified")
    )
    anchors = _normalize_anchors(payload_map.get("anchors"))
    if not anchors:
        anchors = _anchor_from_checkpoint(payload_map)

    task_summary = _redact_clip(
        _first_text(
            payload_map.get("task"),
            payload_map.get("objective"),
            payload_map.get("summary"),
            title,
            f"{domain} {task_type}",
        ),
        limit=220,
    )
    summary_l1: dict[str, Any] = {
        "task": task_summary,
        "decision": _redact_clip(decision_text, limit=220),
        "outcome": _redact_clip(status, limit=64),
        "next_step": _redact_clip(next_step, limit=180),
        "files": files,
        "anchors": anchors,
    }
    summary_parts: list[str] = [_redact_clip(title or task_summary, limit=90)]
    if summary_l1["outcome"]:
        summary_parts.append(summary_l1["outcome"])
    if summary_l1["next_step"]:
        summary_parts.append(f"next: {summary_l1['next_step']}")
    elif summary_l1["decision"]:
        summary_parts.append(summary_l1["decision"])
    summary_l0 = _clip(" | ".join(part for part in summary_parts if part), limit=160)
    if not summary_l0:
        summary_l0 = _clip(task_summary or f"{domain}:{task_type}", limit=160)
    return summary_l0, summary_l1


def coerce_summary_l1(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    files = _normalize_files(value.get("files"), limit=5)
    anchors = _normalize_anchors(value.get("anchors"), limit=3)
    return {
        "task": _redact_clip(value.get("task"), limit=220),
        "decision": _redact_clip(value.get("decision"), limit=220),
        "outcome": _redact_clip(value.get("outcome"), limit=64),
        "next_step": _redact_clip(value.get("next_step"), limit=180),
        "files": files,
        "anchors": anchors,
    }


def summarize_hit_text(title: Any, summary_l0: Any, ts_value: Any) -> str:
    stamp = ""
    if isinstance(ts_value, datetime):
        stamp = ts_value.astimezone(UTC).isoformat()
    else:
        raw = _collapse_whitespace(ts_value)
        if raw:
            stamp = raw
    summary = _collapse_whitespace(summary_l0) or _collapse_whitespace(title)
    if stamp:
        return f"{summary} ({stamp})"
    return summary


def summary_coverage_ratio(hits: list[Any]) -> float:
    if not hits:
        return 0.0
    covered = 0
    for item in hits:
        if isinstance(item, dict):
            token = _collapse_whitespace(item.get("summary_l0"))
        else:
            token = _collapse_whitespace(getattr(item, "summary_l0", ""))
        if token:
            covered += 1
    return round(float(covered) / float(max(1, len(hits))), 4)


def continuity_query(query_text: str) -> bool:
    lowered = _collapse_whitespace(query_text).lower()
    if not lowered:
        return False
    return any(token in lowered for token in _CONTINUITY_TOKENS)


def plan_retrieval_subqueries(
    query_text: str,
    *,
    enabled: bool,
    match_all: bool = False,
) -> list[dict[str, str]]:
    collapsed = _collapse_whitespace(query_text)
    if not collapsed or match_all:
        return [{"label": "objective", "query": collapsed}]
    if not enabled:
        return [{"label": "objective", "query": collapsed}]
    lowered = collapsed.lower()
    tokens = [
        token
        for token in _TOKEN_RE.findall(lowered)
        if token and token not in _STOPWORDS and len(token) > 1
    ][:8]
    keyword_phrase = " ".join(tokens[:6]) or lowered
    focus_tokens = [
        token
        for token in _TOKEN_RE.findall(lowered)
        if token and token not in _FOCUS_DROPWORDS and len(token) > 1
    ][:6]
    focus_phrase = " ".join(focus_tokens) or keyword_phrase or lowered
    objective_query = focus_phrase if continuity_query(lowered) and focus_phrase else collapsed
    queries = [{"label": "objective", "query": objective_query}]
    if continuity_query(lowered) or any(token in lowered for token in ("decision", "failed", "fix", "debug", "why")):
        queries.append(
            {
                "label": "decision_history",
                "query": _clip(f"decision outcome {focus_phrase}", limit=160),
            }
        )
    if continuity_query(lowered) or any(token in lowered for token in ("rule", "workflow", "pattern", "constraint")):
        queries.append(
            {
                "label": "constraints_workflow",
                "query": _clip(f"workflow constraint pattern {focus_phrase}", limit=160),
            }
        )
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in queries:
        label = _collapse_whitespace(item.get("label")).lower()
        query = _collapse_whitespace(item.get("query"))
        key = (label, query.lower())
        if not label or not query or key in seen:
            continue
        seen.add(key)
        deduped.append({"label": label, "query": query})
        if len(deduped) >= 3:
            break
    return deduped or [{"label": "objective", "query": collapsed}]


def propagate_episode_score(base_score: float, episode_score: float) -> float:
    episode_value = max(0.0, min(1.0, float(episode_score)))
    if episode_value <= 0.0:
        return float(base_score)
    return (0.75 * float(base_score)) + (0.25 * episode_value)


def build_retry_feedback(
    *,
    action_kind: Any,
    failure_class: Any,
    failure_reason: Any,
    retry_strategy: Any,
) -> dict[str, Any]:
    failure_label = _redact_clip(getattr(failure_class, "value", failure_class), limit=64)
    strategy_label = _redact_clip(getattr(retry_strategy, "value", retry_strategy), limit=64)
    return {
        "failure_class": failure_label,
        "failure_reason_short": _redact_clip(failure_reason, limit=220),
        "last_attempt_action": _redact_clip(action_kind, limit=80),
        "recommended_retry_strategy": strategy_label,
    }


def autonomy_profile_tuning(profile: Any) -> dict[str, Any]:
    normalized = str(getattr(profile, "value", profile) or "").strip().lower()
    baseline = {
        "retrieval_trigger_delta": 0.0,
        "needs_human_delta": 0.0,
        "evidence_floor_delta": 0,
        "advisor_cadence_turns": 2,
    }
    if normalized == "human_safe":
        return {
            "retrieval_trigger_delta": 0.06,
            "needs_human_delta": 0.08,
            "evidence_floor_delta": 1,
            "advisor_cadence_turns": 1,
        }
    if normalized == "human_aggressive":
        return {
            "retrieval_trigger_delta": -0.06,
            "needs_human_delta": -0.08,
            "evidence_floor_delta": -1,
            "advisor_cadence_turns": 3,
        }
    return baseline
