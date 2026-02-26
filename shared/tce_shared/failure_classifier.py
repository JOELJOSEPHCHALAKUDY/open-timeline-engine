from __future__ import annotations

from typing import Any

from .events import FailureClass, RetryStrategy


def classify_failure(
    *,
    result: str,
    failure_reason: str | None,
    details: dict[str, Any] | None = None,
) -> FailureClass:
    normalized_result = str(result or "").strip().lower()
    reason_text = str(failure_reason or "").strip().lower()
    detail_text = str(details or {}).lower()
    haystack = " ".join(part for part in (normalized_result, reason_text, detail_text) if part)

    if any(token in haystack for token in ("timeout", "timed out", "deadline exceeded")):
        return FailureClass.TIMEOUT
    if any(token in haystack for token in ("check_context", "constraint", "protected file", "policy block")):
        return FailureClass.CONSTRAINT_BLOCK
    if any(token in haystack for token in ("safety", "permit", "confirm_required", "high-risk", "blocked by operator")):
        return FailureClass.SAFETY_BLOCK
    if any(token in haystack for token in ("validation", "invalid", "schema", "pydantic", "json decode")):
        return FailureClass.VALIDATION_FAILURE
    if any(token in haystack for token in ("traceback", "exception", "tool error", "command failed", "non-zero exit")):
        return FailureClass.TOOL_ERROR
    if normalized_result in {"blocked"}:
        return FailureClass.CONSTRAINT_BLOCK
    if normalized_result in {"failure", "error"}:
        return FailureClass.TOOL_ERROR
    return FailureClass.UNKNOWN


def retry_strategy_for_attempt(
    *,
    attempt: int,
    failure_class: FailureClass,
    rollback_available: bool,
) -> RetryStrategy:
    if attempt <= 1:
        return RetryStrategy.NARROW_SCOPE
    if attempt == 2:
        if failure_class in {FailureClass.TOOL_ERROR, FailureClass.TIMEOUT, FailureClass.UNKNOWN}:
            return RetryStrategy.READ_ONLY_DIAGNOSE
        return RetryStrategy.ALTERNATE_PATH
    if attempt == 3 and rollback_available:
        return RetryStrategy.ROLLBACK_THEN_RETRY
    return RetryStrategy.ESCALATE
