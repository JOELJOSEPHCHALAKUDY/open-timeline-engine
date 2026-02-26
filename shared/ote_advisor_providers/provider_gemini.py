from __future__ import annotations

import time
from typing import Any

import httpx

from .base import ProviderAttemptResult, ProviderError, ProviderMetadata, ProviderRequest


class GeminiProvider:
    def __init__(self, metadata: ProviderMetadata) -> None:
        self.metadata = metadata

    def _base_url(self, request: ProviderRequest) -> str:
        base_url = (request.base_url or self.metadata.default_base_url or "").strip()
        if not base_url:
            raise ProviderError("invalid_base_url", "missing base_url")
        return base_url.rstrip("/")

    def list_models(self, request: ProviderRequest) -> list[str]:
        token = (request.api_key or "").strip()
        if not token:
            raise ProviderError("auth_failure", "API key required for gemini")
        base_url = self._base_url(request)
        timeout_s = max(0.2, float(request.timeout_ms) / 1000.0)
        url = f"{base_url}/models"
        try:
            response = httpx.get(url, params={"key": token}, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "timeout reaching gemini", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network_unreachable", f"network error reaching gemini: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                raise ProviderError("auth_failure", f"gemini returned {response.status_code}")
            if response.status_code == 429:
                raise ProviderError("rate_limit", "gemini rate limit", retryable=True)
            raise ProviderError("provider_error", f"gemini returned {response.status_code}", retryable=response.status_code >= 500)
        payload: Any = response.json() if response.content else {}
        data = payload.get("models") if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    if name:
                        models.append(name.replace("models/", ""))
        return sorted(set(models)) or list(self.metadata.default_models)

    def verify(self, request: ProviderRequest) -> ProviderAttemptResult:
        started = time.perf_counter()
        try:
            models = self.list_models(request)
            requested_model = (request.model or "").replace("models/", "").strip()
            if requested_model and models and requested_model not in models:
                return ProviderAttemptResult(
                    provider_id=self.metadata.provider_id,
                    ok=False,
                    code="model_not_found",
                    message=f"Model '{requested_model}' is not available for gemini",
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
