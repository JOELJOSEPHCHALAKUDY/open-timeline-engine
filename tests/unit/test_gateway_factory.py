from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from tce_model_gateway import OllamaGateway, factory
from tce_model_gateway.factory import _GATEWAY_CACHE_MAX, _settings_signature, create_gateway
from tce_shared.deadline import advisor_timeout_bucket_ms


class _FakeSettings:
    model_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    extract_model: str = "qwen2.5:7b"
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"
    openai_extract_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_extract_model: str = "claude-haiku-4-5-20251001"


_has_openai = importlib.util.find_spec("openai") is not None
_has_anthropic = importlib.util.find_spec("anthropic") is not None


def test_factory_returns_ollama_by_default() -> None:
    settings = _FakeSettings()
    gw = create_gateway(settings)
    assert isinstance(gw, OllamaGateway)


@pytest.mark.skipif(not _has_openai, reason="openai SDK not installed")
def test_factory_returns_openai_gateway() -> None:
    settings = _FakeSettings()
    settings.model_provider = "openai"
    settings.openai_api_key = "sk-test"
    gw = create_gateway(settings)
    from tce_model_gateway.openai_gateway import OpenAIGateway
    assert isinstance(gw, OpenAIGateway)


@pytest.mark.skipif(not _has_anthropic, reason="anthropic SDK not installed")
def test_factory_returns_anthropic_gateway() -> None:
    settings = _FakeSettings()
    settings.model_provider = "anthropic"
    settings.anthropic_api_key = "sk-ant-test"
    gw = create_gateway(settings)
    from tce_model_gateway.anthropic_gateway import AnthropicGateway
    assert isinstance(gw, AnthropicGateway)


def test_factory_raises_on_unknown_provider() -> None:
    settings = _FakeSettings()
    settings.model_provider = "unknown"
    with pytest.raises(ValueError, match="Unknown model provider"):
        create_gateway(settings)


def _settings(**overrides: object) -> SimpleNamespace:
    # ollama_url / embed_model / extract_model are read as BARE attributes, so create_gateway
    # raises AttributeError without them. redis_url must be falsy, or _wrap_with_cache returns
    # a CachedGateway that exposes no timeout at all.
    base = {
        "model_provider": "ollama",
        "ollama_url": "http://ollama:11434",
        "embed_model": "mxbai-embed-large",
        "extract_model": "qwen2.5:3b",
        "redis_url": "",
        "advisor_attempt_timeout_ms": 300,
        "advisor_read_timeout_ms": 300,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_floor_is_200ms() -> None:
    gateway = create_gateway(_settings())
    assert isinstance(gateway, OllamaGateway)
    assert gateway.timeout == pytest.approx(0.3)


def test_floor_still_clamps_absurdly_small_budgets() -> None:
    gateway = create_gateway(_settings(advisor_attempt_timeout_ms=1, advisor_read_timeout_ms=1))
    assert isinstance(gateway, OllamaGateway)
    assert gateway.timeout == pytest.approx(0.2)


def test_no_timeout_candidates_falls_back_to_thirty_seconds() -> None:
    gateway = create_gateway(_settings(advisor_attempt_timeout_ms=0, advisor_read_timeout_ms=0))
    assert isinstance(gateway, OllamaGateway)
    assert gateway.timeout == pytest.approx(30.0)


def test_bucketed_clamp_reuses_the_cached_gateway() -> None:
    """A 3500 ms turn must not mint a fresh gateway (and Redis client) on every call."""
    signatures = set()
    for remaining_ms in range(0, 3500, 70):
        bucket = advisor_timeout_bucket_ms(remaining_ms)
        signatures.add(
            _settings_signature(
                _settings(
                    advisor_timeout_seconds=bucket / 1000.0,
                    advisor_attempt_timeout_ms=bucket,
                    advisor_read_timeout_ms=bucket,
                )
            )
        )
    assert len(signatures) <= 14
    assert len(signatures) < _GATEWAY_CACHE_MAX


def test_cache_max_leaves_room_for_other_consumers() -> None:
    # 14 advisor buckets plus the plan/dream/route namespaces; 16 was demonstrably too small.
    assert _GATEWAY_CACHE_MAX == 64
    assert factory._GATEWAY_CACHE_MAX == _GATEWAY_CACHE_MAX
