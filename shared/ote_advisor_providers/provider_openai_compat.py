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


class OpenAICompatibleProvider:
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
        if token:
            headers["authorization"] = f"Bearer {token}"
        elif not self.metadata.key_optional:
            raise ProviderError("auth_failure", f"API key required for {self.metadata.provider_id}")
        return headers

    def list_models(self, request: ProviderRequest) -> list[str]:
        if not self.metadata.supports_model_listing:
            return list(self.metadata.default_models)
        base_url = self._base_url(request)
        headers = self._headers(request)
        timeout_s = max(0.2, float(request.timeout_ms) / 1000.0)
        url = f"{base_url}/models"
        try:
            response = httpx.get(url, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"timeout reaching {self.metadata.provider_id}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "network_unreachable",
                f"network error reaching {self.metadata.provider_id}: {exc}",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            code, retryable = normalize_http_status(response.status_code)
            message = f"{self.metadata.provider_id} returned {response.status_code}"
            raise ProviderError(code, message, retryable=retryable)

        payload: Any = response.json() if response.content else {}
        data = payload.get("data") if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    model_id = str(item.get("id") or item.get("name") or "").strip()
                    if model_id:
                        models.append(model_id)
        if models:
            return sorted(set(models))
        return list(self.metadata.default_models)

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
                    message=f"Model '{requested_model}' is not available for {self.metadata.provider_id}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    models=models,
                )
            message = "provider verified"
            if requested_model:
                message = f"provider verified with model '{requested_model}'"
            return ProviderAttemptResult(
                provider_id=self.metadata.provider_id,
                ok=True,
                code="ok",
                message=message,
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
