from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tce_shared.redaction import (
    DEFAULT_ALLOWLIST_KEYS,
    SECRET_PATTERNS,
)
from tce_shared.redaction import (
    apply_redaction_zones as shared_apply_redaction_zones,
)
from tce_shared.redaction import (
    redact_payload as shared_redact_payload,
)
from tce_shared.redaction import (
    redact_text as shared_redact_text,
)

ALLOWLIST_KEYS = DEFAULT_ALLOWLIST_KEYS


def redact_text(text: str, hints: Iterable[str] | None = None) -> tuple[str, list[str]]:
    return shared_redact_text(text, hints=hints)


def redact_payload(payload: Any, hints: Iterable[str] | None = None) -> tuple[Any, list[str]]:
    return shared_redact_payload(payload, hints=hints, allowlist_keys=ALLOWLIST_KEYS)


def apply_redaction_zones(
    payload: dict[str, Any], context: dict[str, Any], zones: Iterable[str]
) -> tuple[dict[str, Any], bool]:
    return shared_apply_redaction_zones(payload, context, zones)


__all__ = [
    "ALLOWLIST_KEYS",
    "SECRET_PATTERNS",
    "apply_redaction_zones",
    "redact_payload",
    "redact_text",
]
