from __future__ import annotations

import hashlib
from typing import Any

from .gateway import CachedGateway, ModelGateway, OllamaGateway

_GATEWAY_CACHE: dict[str, ModelGateway] = {}
_GATEWAY_CACHE_MAX = 16


def _settings_signature(settings: Any) -> str:
    openai_key = str(getattr(settings, "openai_api_key", "") or "")
    anthropic_key = str(getattr(settings, "anthropic_api_key", "") or "")
    openai_key_sig = hashlib.sha256(openai_key.encode("utf-8")).hexdigest()[:12] if openai_key else ""
    anthropic_key_sig = hashlib.sha256(anthropic_key.encode("utf-8")).hexdigest()[:12] if anthropic_key else ""
    return "|".join(
        [
            str(getattr(settings, "model_provider", "ollama")).strip().lower(),
            str(getattr(settings, "ollama_url", "")),
            str(getattr(settings, "embed_model", "")),
            str(getattr(settings, "extract_model", "")),
            str(getattr(settings, "openai_base_url", "")),
            str(getattr(settings, "openai_embed_model", "")),
            str(getattr(settings, "openai_extract_model", "")),
            str(getattr(settings, "anthropic_extract_model", "")),
            str(getattr(settings, "advisor_timeout_seconds", "")),
            str(getattr(settings, "advisor_attempt_timeout_ms", "")),
            str(getattr(settings, "advisor_read_timeout_ms", "")),
            openai_key_sig,
            anthropic_key_sig,
            str(bool(getattr(settings, "redis_url", ""))),
        ]
    )


def _wrap_with_cache(inner: ModelGateway, settings: Any) -> ModelGateway:
    redis_url = getattr(settings, "redis_url", "")
    if not redis_url:
        return inner
    return CachedGateway(inner, redis_url=redis_url, ttl_seconds=3600)


def create_gateway(settings: Any) -> ModelGateway:
    provider = getattr(settings, "model_provider", "ollama").strip().lower()
    timeout_candidates: list[float] = []
    for raw in (
        getattr(settings, "advisor_timeout_seconds", 0),
        float(getattr(settings, "advisor_attempt_timeout_ms", 0) or 0) / 1000.0,
        float(getattr(settings, "advisor_read_timeout_ms", 0) or 0) / 1000.0,
    ):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            timeout_candidates.append(value)
    timeout_seconds = max(timeout_candidates) if timeout_candidates else 30.0
    timeout_seconds = max(2.0, timeout_seconds)

    if provider == "ollama":
        inner: ModelGateway = OllamaGateway(
            base_url=settings.ollama_url,
            embed_model=settings.embed_model,
            extract_model=settings.extract_model,
            timeout=timeout_seconds,
        )
        return _wrap_with_cache(inner, settings)

    if provider == "openai":
        from .openai_gateway import OpenAIGateway

        inner = OpenAIGateway(
            api_key=settings.openai_api_key,
            embed_model=getattr(settings, "openai_embed_model", "text-embedding-3-small"),
            extract_model=getattr(settings, "openai_extract_model", "gpt-4o-mini"),
            base_url=getattr(settings, "openai_base_url", None),
            timeout=timeout_seconds,
        )
        return _wrap_with_cache(inner, settings)

    if provider == "anthropic":
        from .anthropic_gateway import AnthropicGateway

        return AnthropicGateway(
            api_key=settings.anthropic_api_key,
            extract_model=getattr(settings, "anthropic_extract_model", "claude-haiku-4-5-20251001"),
            timeout=timeout_seconds,
        )

    raise ValueError(f"Unknown model provider: {provider!r}. Use 'ollama', 'openai', or 'anthropic'.")


def get_gateway(settings: Any) -> ModelGateway:
    signature = _settings_signature(settings)
    cached = _GATEWAY_CACHE.get(signature)
    if cached is not None:
        return cached
    gateway = create_gateway(settings)
    if len(_GATEWAY_CACHE) >= _GATEWAY_CACHE_MAX:
        _GATEWAY_CACHE.pop(next(iter(_GATEWAY_CACHE)))
    _GATEWAY_CACHE[signature] = gateway
    return gateway
