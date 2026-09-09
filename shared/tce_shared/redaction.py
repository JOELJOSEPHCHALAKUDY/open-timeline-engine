from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Order matters: `url_credentials` must run before `email`, or 'https://user:tok@github.com/o/r' is merely
# email-mangled ('https://user:<REDACTED:EMAIL>/o/r') and the credential shape survives in the record.
SECRET_PATTERNS = {
    "url_credentials": re.compile(r"(?<=://)[^/@\s]+(?::[^/@\s]*)?@"),
    "token": re.compile(
        r"(?:gh[pousr]_[A-Za-z0-9]{16,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|glpat-[A-Za-z0-9_-]{16,}"
        r"|sk-[A-Za-z0-9_-]{16,}"
        r"|AKIA[0-9A-Z]{12,}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]+)?)"
    ),
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


def redact_project_hint(hint: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Sanitize an untrusted host-supplied `project_hint` server-side.

    The capture hook redacts the hint client-side, but the server does not trust hook redaction for content and must
    not trust it here either. Unlike `redact_payload`, this deliberately does NOT honour DEFAULT_ALLOWLIST_KEYS:
    'repo', 'project' and 'branch' are exactly the keys a credential URL hides in.
    """

    sanitized, applied = redact_payload(hint, allowlist_keys=set())
    return (sanitized if isinstance(sanitized, dict) else {}), applied


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
