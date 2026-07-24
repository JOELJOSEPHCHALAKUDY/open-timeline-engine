from __future__ import annotations

import importlib

import pytest
from tce_model_gateway import OllamaGateway
from tce_model_gateway.factory import create_gateway


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
