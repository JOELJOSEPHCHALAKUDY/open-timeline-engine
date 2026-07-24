from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any


def normalize_file_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    try:
        return str(PurePosixPath(text))
    except (TypeError, ValueError):
        return text


def parse_recommended_files(value: Any) -> list[str]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        return []
    output: list[str] = []
    for item in raw[:40]:
        path = normalize_file_path(item)
        if path and path not in output:
            output.append(path)
    return output


def recommended_file_rank(opened_file: Any, recommended_files: Any) -> int | None:
    opened = normalize_file_path(opened_file)
    if not opened:
        return None
    for index, candidate in enumerate(parse_recommended_files(recommended_files), start=1):
        if candidate == opened:
            return index
    return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def elapsed_ms(start: Any, end: Any) -> int | None:
    start_at = _datetime(start)
    end_at = _datetime(end)
    if start_at is None or end_at is None or end_at < start_at:
        return None
    return int((end_at - start_at).total_seconds() * 1000)


def percentile(values: Iterable[int | float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not ordered:
        return None
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "none", "null"}:
            return None
        return normalized in {"1", "true", "yes", "on"}
    return bool(value)


def _rate(values: Iterable[bool | None]) -> float | None:
    selected = [value for value in values if value is not None]
    if not selected:
        return None
    return sum(1 for value in selected if value) / len(selected)


def summarize_attempts(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    attempts = [dict(row) for row in rows]
    feedback = [row for row in attempts if row.get("feedback_at") is not None]
    first_file_ms = [
        value
        for row in attempts
        if (value := elapsed_ms(row.get("requested_at"), row.get("first_file_opened_at"))) is not None
    ]
    active_resume_ms = [
        value
        for row in attempts
        if (value := elapsed_ms(row.get("requested_at"), row.get("productive_at"))) is not None
    ]
    completion_ms = [
        value
        for row in attempts
        if (value := elapsed_ms(row.get("requested_at"), row.get("completed_at"))) is not None
    ]
    handoff_ages = [
        max(0, int(row.get("time_since_handoff_ms") or 0))
        for row in attempts
    ]
    retrieval = [max(0, int(row.get("latency_ms") or 0)) for row in attempts]
    tool_calls = [
        max(0, int(row["archaeology_tool_calls"]))
        for row in attempts
        if row.get("archaeology_tool_calls") is not None
    ]
    tokens = [
        max(0, int(row["archaeology_tokens"]))
        for row in attempts
        if row.get("archaeology_tokens") is not None
    ]
    ranks = [
        int(row["opened_file_rank"])
        for row in attempts
        if row.get("opened_file_rank") is not None and int(row["opened_file_rank"]) > 0
    ]
    return {
        "resume_attempt_count": len(attempts),
        "feedback_count": len(feedback),
        "productive_resume_count": len(active_resume_ms),
        "completed_resume_count": len(completion_ms),
        "correct_file_rate": _rate(_optional_bool(row.get("correct_file")) for row in feedback),
        "correct_file_at_1_rate": _rate(rank == 1 for rank in ranks),
        "correct_file_at_3_rate": _rate(rank <= 3 for rank in ranks),
        "correct_anchor_rate": _rate(_optional_bool(row.get("correct_anchor")) for row in feedback),
        "correction_rate": _rate(_optional_bool(row.get("correction_required")) for row in feedback),
        # Compatibility aliases retain the original definition: age of the handoff at request time.
        "median_time_to_resume_ms": percentile(handoff_ages, 0.5),
        "p95_time_to_resume_ms": percentile(handoff_ages, 0.95),
        "median_handoff_age_at_resume_ms": percentile(handoff_ages, 0.5),
        "p95_handoff_age_at_resume_ms": percentile(handoff_ages, 0.95),
        "median_time_to_first_file_ms": percentile(first_file_ms, 0.5),
        "p95_time_to_first_file_ms": percentile(first_file_ms, 0.95),
        "median_active_resume_ms": percentile(active_resume_ms, 0.5),
        "p95_active_resume_ms": percentile(active_resume_ms, 0.95),
        "median_completion_after_resume_ms": percentile(completion_ms, 0.5),
        "p95_completion_after_resume_ms": percentile(completion_ms, 0.95),
        "median_retrieval_latency_ms": percentile(retrieval, 0.5),
        "median_archaeology_tool_calls": percentile(tool_calls, 0.5),
        "median_archaeology_tokens": percentile(tokens, 0.5),
    }


def progress_patch(
    row: Mapping[str, Any],
    *,
    phase: str,
    now: datetime,
    opened_file: str | None = None,
    correct_file: bool | None = None,
    correct_anchor: bool | None = None,
    opened_file_rank: int | None = None,
    correction_required: bool | None = None,
    correction_reason: str | None = None,
    archaeology_tool_calls: int | None = None,
    archaeology_tokens: int | None = None,
    outcome_status: str | None = None,
    progress_source: str = "manual",
) -> dict[str, Any]:
    normalized_phase = str(phase or "feedback").strip().lower()
    if normalized_phase not in {"file_opened", "productive", "completed", "feedback"}:
        raise ValueError(f"unsupported continuity progress phase: {normalized_phase}")
    safe_file = normalize_file_path(opened_file)
    rank = opened_file_rank
    if rank is None and safe_file:
        rank = recommended_file_rank(safe_file, row.get("recommended_files_json"))
    patch: dict[str, Any] = {
        "progress_source": str(progress_source or "manual")[:64],
    }
    if safe_file:
        patch["opened_file"] = safe_file[:240]
        if row.get("first_file_opened_at") is None:
            patch["first_file_opened_at"] = now
    if rank is not None:
        patch["opened_file_rank"] = max(1, min(40, int(rank)))
    if correct_file is not None:
        patch["correct_file"] = bool(correct_file)
    elif rank is not None:
        patch["correct_file"] = rank == 1
    if correct_anchor is not None:
        patch["correct_anchor"] = bool(correct_anchor)
    if correction_required is not None:
        patch["correction_required"] = bool(correction_required)
    if correction_reason is not None:
        patch["correction_reason"] = str(correction_reason)[:500]
    if archaeology_tool_calls is not None:
        patch["archaeology_tool_calls"] = max(0, min(10000, int(archaeology_tool_calls)))
    if archaeology_tokens is not None:
        patch["archaeology_tokens"] = max(0, min(10_000_000, int(archaeology_tokens)))
    if outcome_status:
        patch["outcome_status"] = str(outcome_status)[:40]
    if normalized_phase in {"productive", "completed"} and row.get("productive_at") is None:
        patch["productive_at"] = now
    if normalized_phase == "completed" and row.get("completed_at") is None:
        patch["completed_at"] = now
    if normalized_phase in {"feedback", "completed"}:
        patch["feedback_at"] = now
    patch["phase"] = normalized_phase
    return patch
