from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_text

_VALID_MODES = {"shadow", "warn", "enforce"}
_VALID_OUTCOME_STATUS = {"succeeded", "failed", "blocked"}


def normalize_handoff_mode(value: Any) -> str:
    mode = str(value or "shadow").strip().lower()
    return mode if mode in _VALID_MODES else "shadow"


def _redact_string(value: Any, *, max_len: int) -> tuple[str, bool]:
    text = str(value or "").strip()
    clipped = text[:max_len]
    redacted, applied = redact_text(clipped)
    return redacted.strip(), bool(applied)


def _normalize_string_list(values: Any, *, max_items: int, max_item_len: int) -> tuple[list[str], bool]:
    if not isinstance(values, list):
        return [], False
    out: list[str] = []
    redacted_any = False
    seen: set[str] = set()
    for raw in values:
        value, redacted = _redact_string(raw, max_len=max_item_len)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        redacted_any = redacted_any or redacted
        if len(out) >= max_items:
            break
    return out, redacted_any


def _normalize_git(raw: Any) -> tuple[dict[str, str], bool]:
    if not isinstance(raw, dict):
        raw = {}
    redacted_any = False
    out: dict[str, str] = {}
    for key in ("repo", "branch", "commit", "pr"):
        value, redacted = _redact_string(raw.get(key), max_len=200)
        if value:
            out[key] = value
        redacted_any = redacted_any or redacted
    return out, redacted_any


def _normalize_anchor_item(raw: Any) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(raw, dict):
        return None, False
    file_value, redacted_file = _redact_string(raw.get("file"), max_len=240)
    if not file_value:
        return None, redacted_file
    line_raw = raw.get("line")
    line_value: int | None = None
    if line_raw is not None:
        try:
            parsed = int(line_raw)
            if parsed > 0:
                line_value = parsed
        except (TypeError, ValueError):
            line_value = None
    symbol_value, redacted_symbol = _redact_string(raw.get("symbol"), max_len=160)
    anchor: dict[str, Any] = {"file": file_value}
    if line_value is not None:
        anchor["line"] = line_value
    if symbol_value:
        anchor["symbol"] = symbol_value
    return anchor, (redacted_file or redacted_symbol)


def normalize_anchor_list(values: Any, *, max_items: int = 40) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(values, list):
        return [], False
    anchors: list[dict[str, Any]] = []
    seen: set[str] = set()
    redacted_any = False
    for raw in values:
        normalized, redacted = _normalize_anchor_item(raw)
        redacted_any = redacted_any or redacted
        if not normalized:
            continue
        key = f"{normalized.get('file')}:{normalized.get('line')}:{normalized.get('symbol')}"
        if key in seen:
            continue
        seen.add(key)
        anchors.append(normalized)
        if len(anchors) >= max_items:
            break
    return anchors, redacted_any


def merge_anchors(
    *,
    plugin_checkpoint: dict[str, Any] | None,
    reported_anchors: list[dict[str, Any]],
    max_items: int = 40,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    if plugin_checkpoint:
        merged.append(plugin_checkpoint)
    merged.extend(reported_anchors)
    if not merged:
        return []
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in merged:
        key = f"{item.get('file')}:{item.get('line')}:{item.get('symbol')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


def normalize_milestone_v1(
    *,
    details: dict[str, Any],
    state: str,
    fallback_title: str,
) -> dict[str, Any]:
    errors: list[str] = []
    redacted_any = False

    title, redacted = _redact_string(details.get("title") or fallback_title, max_len=160)
    redacted_any = redacted_any or redacted
    if not title:
        errors.append("title is required")
        title = fallback_title[:160]

    files: list[str] = []
    direct_files, redacted = _normalize_string_list(details.get("files"), max_items=40, max_item_len=240)
    redacted_any = redacted_any or redacted
    files.extend(direct_files)
    payload_raw = details.get("payload")
    if isinstance(payload_raw, dict):
        payload_files, redacted = _normalize_string_list(payload_raw.get("files"), max_items=40, max_item_len=240)
        redacted_any = redacted_any or redacted
        for item in payload_files:
            if item not in files:
                files.append(item)
    files = files[:40]
    if not files:
        errors.append("payload.files must include at least one file path")

    decision, redacted = _redact_string(details.get("decision") or details.get("why"), max_len=500)
    redacted_any = redacted_any or redacted
    if not decision:
        errors.append("decision is required")
        decision = "No decision rationale provided."

    outcome_raw = details.get("outcome")
    if not isinstance(outcome_raw, dict):
        outcome_raw = {}
    outcome_status, redacted = _redact_string(outcome_raw.get("status") or state, max_len=24)
    redacted_any = redacted_any or redacted
    outcome_status = outcome_status.lower()
    if outcome_status not in _VALID_OUTCOME_STATUS:
        errors.append("outcome.status must be one of succeeded|failed|blocked")
        outcome_status = str(state or "failed").strip().lower() or "failed"
        if outcome_status not in _VALID_OUTCOME_STATUS:
            outcome_status = "failed"
    next_step, redacted = _redact_string(
        outcome_raw.get("next_step") or details.get("next_step"),
        max_len=300,
    )
    redacted_any = redacted_any or redacted
    if not next_step:
        errors.append("outcome.next_step is required")
        next_step = (
            "Continue with the next objective."
            if outcome_status == "succeeded"
            else "Retry or request clarification before continuing."
        )

    anchors, redacted = normalize_anchor_list(details.get("anchors"), max_items=40)
    redacted_any = redacted_any or redacted
    git_payload, redacted = _normalize_git(details.get("git"))
    redacted_any = redacted_any or redacted

    normalized = {
        "title": title,
        "payload": {"files": files},
        "decision": decision,
        "outcome": {
            "status": outcome_status,
            "next_step": next_step,
        },
        "git": git_payload,
        "anchors": anchors,
        "milestone_schema": "v1",
    }
    return {
        "normalized": normalized,
        "valid": len(errors) == 0,
        "errors": errors,
        "redaction_applied": redacted_any,
    }


def latest_checkpoint_anchor(
    *,
    payload: dict[str, Any] | None,
    event_ts: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    file_value, _ = _redact_string(payload.get("active_file"), max_len=240)
    if not file_value:
        return None
    anchor: dict[str, Any] = {"file": file_value}
    line_raw = payload.get("line")
    if line_raw is not None:
        try:
            line_val = int(line_raw)
            if line_val > 0:
                anchor["line"] = line_val
        except (TypeError, ValueError):
            pass
    symbol_value, _ = _redact_string(payload.get("symbol"), max_len=160)
    if symbol_value:
        anchor["symbol"] = symbol_value
    if event_ts is not None:
        anchor["ts"] = event_ts.astimezone(UTC).isoformat()
    return anchor


def handoff_intent(query_text: str) -> bool:
    lowered = " ".join(str(query_text or "").strip().lower().split())
    if not lowered:
        return False
    tokens = (
        "continue",
        "resume",
        "pick up",
        "where",
        "stopped",
        "timeline",
        "memory",
        "handoff",
        "read codex timeline",
        "read claude timeline",
    )
    return any(token in lowered for token in tokens)


_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")
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
    "work",
    "timeline",
    "memory",
    "read",
    "continue",
    "resume",
    "pick",
    "up",
}


def normalize_objective_text(
    *,
    title: Any,
    decision: Any,
    next_step: Any,
    fallback: str = "",
) -> tuple[str, bool]:
    joined = " ".join(
        [
            str(title or "").strip(),
            str(decision or "").strip(),
            str(next_step or "").strip(),
            str(fallback or "").strip(),
        ]
    ).strip()
    if not joined:
        return "", False
    collapsed = " ".join(joined.split())[:300]
    redacted, applied = redact_text(collapsed)
    return redacted.strip(), bool(applied)


def task_overlap_score(query_text: str, objective_text: str) -> float:
    query_tokens = {
        token
        for token in _TOKEN_RE.findall(str(query_text or "").lower())
        if token and token not in _STOPWORDS and len(token) > 1
    }
    objective_tokens = {
        token
        for token in _TOKEN_RE.findall(str(objective_text or "").lower())
        if token and token not in _STOPWORDS and len(token) > 1
    }
    if not query_tokens or not objective_tokens:
        return 0.0
    overlap = len(query_tokens.intersection(objective_tokens))
    if overlap <= 0:
        return 0.0
    precision = float(overlap) / float(max(1, len(query_tokens)))
    recall = float(overlap) / float(max(1, len(objective_tokens)))
    return max(0.0, min(1.0, (0.6 * precision) + (0.4 * recall)))


def recency_score(ts_value: Any, *, now: datetime | None = None, half_life_days: float = 7.0) -> float:
    if not isinstance(ts_value, datetime):
        try:
            ts_value = datetime.fromisoformat(str(ts_value))
        except Exception:
            return 0.0
    now_dt = now or datetime.now(tz=UTC)
    age_days = max(0.0, (now_dt - ts_value.astimezone(UTC)).total_seconds() / 86400.0)
    half_life = max(1.0, float(half_life_days))
    return max(0.0, min(1.0, math.exp(-math.log(2.0) * (age_days / half_life))))


def score_resume_candidate(candidate: dict[str, Any], *, query_text: str, now: datetime | None = None) -> float:
    overlap = task_overlap_score(query_text, str(candidate.get("objective_text") or ""))
    recency = recency_score(candidate.get("ts"), now=now)
    score = (0.65 * overlap) + (0.35 * recency)
    anchors = candidate.get("anchors_json")
    if isinstance(anchors, list) and anchors:
        score += 0.03
    source = str(candidate.get("source") or "").strip().lower()
    if source == "native":
        score += 0.02
    status = str(candidate.get("status") or "").strip().lower()
    if status == "succeeded":
        score += 0.02
    elif status in {"blocked", "failed"}:
        score += 0.005
    return score


def rank_resume_candidates(
    candidates: list[dict[str, Any]],
    *,
    query_text: str,
    k: int,
) -> tuple[dict[str, Any] | None, list[str], int]:
    if not candidates:
        return None, [], 0
    now = datetime.now(tz=UTC)
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        scored.append((score_resume_candidate(candidate, query_text=query_text, now=now), candidate))
    scored.sort(
        key=lambda item: (
            item[0],
            1 if str(item[1].get("source") or "").strip().lower() == "native" else 0,
            1 if bool(item[1].get("anchors_json")) else 0,
            recency_score(item[1].get("ts"), now=now),
        ),
        reverse=True,
    )
    selected = scored[0][1] if scored else None
    alternates: list[str] = []
    for _, row in scored[1 : max(1, int(k))]:
        rid = str(row.get("id") or "").strip()
        if rid:
            alternates.append(rid)
    return selected, alternates, len(candidates)
