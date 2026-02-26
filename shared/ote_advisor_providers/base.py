from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderMetadata:
    provider_id: str
    label: str
    provider_category: str
    protocol: str
    default_base_url: str | None = None
    api_version: str | None = None
    region_hint: str | None = None
    default_models: list[str] = field(default_factory=list)
    supports_model_listing: bool = True
    api_key_env: str | None = None
    key_optional: bool = False
    required_in_baseline: bool = True
    custom_headers_allowlist: list[str] = field(
        default_factory=lambda: [
            "x-api-key",
            "x-tenant-id",
            "anthropic-version",
            "x-goog-user-project",
            "x-request-id",
        ]
    )


@dataclass
class ProviderRequest:
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api_version: str | None = None
    timeout_ms: int = 6000
    custom_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ProviderAttemptResult:
    provider_id: str
    ok: bool
    code: str
    message: str
    latency_ms: int
    models: list[str] = field(default_factory=list)


class ProviderAdapter(Protocol):
    metadata: ProviderMetadata

    def list_models(self, request: ProviderRequest) -> list[str]:
        ...

    def verify(self, request: ProviderRequest) -> ProviderAttemptResult:
        ...


def normalize_http_status(status_code: int) -> tuple[str, bool]:
    if status_code in {401, 403}:
        return "auth_failure", False
    if status_code in {429}:
        return "rate_limit", True
    if status_code in {404}:
        return "model_not_found", False
    if status_code in {408, 504}:
        return "timeout", True
    if 500 <= status_code <= 599:
        return "provider_error", True
    return "provider_error", False


def sanitize_custom_headers(
    headers: dict[str, str],
    *,
    allowlist: list[str],
) -> dict[str, str]:
    allowed = {item.strip().lower() for item in allowlist if item.strip()}
    out: dict[str, str] = {}
    for key, value in headers.items():
        k = str(key).strip().lower()
        if not k or not str(value).strip():
            continue
        if k in {"authorization", "proxy-authorization"}:
            continue
        if k in allowed:
            out[k] = str(value)
    return out
