from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

SECRET_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "api_key": re.compile(r"(?i)(api[_-]?key|token|secret|password)[\"' :=]+([^\s,;]+)"),
    "private_key": re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|PGP)? ?PRIVATE KEY-----"),
}

DEFAULT_ALLOWLIST_KEYS = {"project", "repo", "branch", "stack", "env", "issue", "pr", "docs"}


def redact_text(text: str, hints: Iterable[str] | None = None) -> tuple[str, list[str]]:
    redacted = text
    applied: list[str] = []
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(redacted):
            redacted = pattern.sub(f"<REDACTED:{name.upper()}>", redacted)
            applied.append(name)
    if hints:
        for hint in hints:
            if hint and hint in redacted:
                redacted = redacted.replace(hint, "<REDACTED:HINT>")
                applied.append("hint")
    return redacted, sorted(set(applied))


def redact_payload(
    payload: Any,
    hints: Iterable[str] | None = None,
    allowlist_keys: set[str] | None = None,
) -> tuple[Any, list[str]]:
    applied: list[str] = []
    effective_allowlist = allowlist_keys if allowlist_keys is not None else DEFAULT_ALLOWLIST_KEYS

    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key in effective_allowlist:
                result[key] = value
                continue
            redacted_value, sub_applied = redact_payload(
                value, hints=hints, allowlist_keys=allowlist_keys
            )
            result[key] = redacted_value
            applied.extend(sub_applied)
        return result, sorted(set(applied))

    if isinstance(payload, list):
        items = []
        for value in payload:
            redacted_value, sub_applied = redact_payload(
                value, hints=hints, allowlist_keys=allowlist_keys
            )
            items.append(redacted_value)
            applied.extend(sub_applied)
        return items, sorted(set(applied))

    if isinstance(payload, str):
        return redact_text(payload, hints=hints)

    return payload, []


def apply_redaction_zones(
    payload: dict[str, Any], context: dict[str, Any], zones: Iterable[str]
) -> tuple[dict[str, Any], bool]:
    repo = str(context.get("repo", "")) if isinstance(context, dict) else ""
    project = str(context.get("project", "")) if isinstance(context, dict) else ""
    for zone in zones:
        if not zone:
            continue
        if repo.startswith(zone) or project.startswith(zone):
            return {"redacted_zone": True, "message": "payload omitted due to redaction zone"}, True
    return payload, False
