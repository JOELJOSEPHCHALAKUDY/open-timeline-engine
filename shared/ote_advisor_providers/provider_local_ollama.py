from __future__ import annotations

import time
from typing import Any

import httpx

from .base import ProviderAttemptResult, ProviderError, ProviderMetadata, ProviderRequest


class LocalOllamaProvider:
    def __init__(self, metadata: ProviderMetadata) -> None:
        self.metadata = metadata

    def _base_url(self, request: ProviderRequest) -> str:
        base_url = (request.base_url or self.metadata.default_base_url or "http://localhost:11434").strip()
        return base_url.rstrip("/")

    def list_models(self, request: ProviderRequest) -> list[str]:
        base_url = self._base_url(request)
        timeout_s = max(0.2, float(request.timeout_ms) / 1000.0)
        try:
            response = httpx.get(f"{base_url}/api/tags", timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "timeout reaching local ollama", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network_unreachable", f"network error reaching local ollama: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise ProviderError("provider_error", f"local ollama returned {response.status_code}", retryable=response.status_code >= 500)
        payload: Any = response.json() if response.content else {}
        rows = payload.get("models") if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    name = str(row.get("name") or "").strip()
                    if name:
                        models.append(name)
        return sorted(set(models))

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
                    message=f"Model '{requested_model}' is not available in local ollama",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    models=models,
                )
            return ProviderAttemptResult(
                provider_id=self.metadata.provider_id,
                ok=True,
                code="ok",
                message="local ollama verified",
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
