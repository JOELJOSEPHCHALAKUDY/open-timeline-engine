from __future__ import annotations

import time
from typing import Any

import httpx

from .base import (
    ProviderAttemptResult,
    ProviderError,
    ProviderMetadata,
    ProviderRequest,
    normalize_http_status,
    sanitize_custom_headers,
)


class AnthropicProvider:
    def __init__(self, metadata: ProviderMetadata) -> None:
        self.metadata = metadata

    def _base_url(self, request: ProviderRequest) -> str:
        base_url = (request.base_url or self.metadata.default_base_url or "").strip()
        if not base_url:
            raise ProviderError("invalid_base_url", "missing base_url")
        return base_url.rstrip("/")

    def _headers(self, request: ProviderRequest) -> dict[str, str]:
        headers = sanitize_custom_headers(
            request.custom_headers,
            allowlist=self.metadata.custom_headers_allowlist,
        )
        token = (request.api_key or "").strip()
        if not token:
            raise ProviderError("auth_failure", "API key required for anthropic")
        headers["x-api-key"] = token
        headers["anthropic-version"] = request.api_version or self.metadata.api_version or "2023-06-01"
        return headers

    def list_models(self, request: ProviderRequest) -> list[str]:
        base_url = self._base_url(request)
        headers = self._headers(request)
        timeout_s = max(0.2, float(request.timeout_ms) / 1000.0)
        url = f"{base_url}/models"
        try:
            response = httpx.get(url, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "timeout reaching anthropic", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network_unreachable", f"network error reaching anthropic: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            code, retryable = normalize_http_status(response.status_code)
            raise ProviderError(code, f"anthropic returned {response.status_code}", retryable=retryable)
        payload: Any = response.json() if response.content else {}
        data = payload.get("data") if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    model_id = str(item.get("id") or "").strip()
                    if model_id:
                        models.append(model_id)
        return sorted(set(models)) or list(self.metadata.default_models)

    def verify(self, request: ProviderRequest) -> ProviderAttemptResult:
        started = time.perf_counter()
        try:
            models = self.list_models(request)
            requested_model = (request.model or "").strip()
            if requested_model and models and requested_model not in models:
                return ProviderAttemptResult(
                    provider_id=self.metadata.provider_id,
                    ok=False,
                    code="model_not_found",
                    message=f"Model '{requested_model}' is not available for anthropic",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    models=models,
                )
            return ProviderAttemptResult(
                provider_id=self.metadata.provider_id,
                ok=True,
                code="ok",
                message="provider verified",
                latency_ms=int((time.perf_counter() - started) * 1000),
                models=models,
            )
        except ProviderError as exc:
            return ProviderAttemptResult(
                provider_id=self.metadata.provider_id,
                ok=False,
                code=exc.code,
                message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                models=[],
            )
