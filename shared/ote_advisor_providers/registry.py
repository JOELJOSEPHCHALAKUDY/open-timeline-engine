from __future__ import annotations

from functools import lru_cache

from .base import ProviderAdapter, ProviderMetadata
from .provider_anthropic import AnthropicProvider
from .provider_custom_openai_compat import build_adapter as build_custom_adapter
from .provider_dashscope import build_adapter as build_dashscope_adapter
from .provider_deepseek import build_adapter as build_deepseek_adapter
from .provider_gemini import GeminiProvider
from .provider_hunyuan import build_adapter as build_hunyuan_adapter
from .provider_local_lmstudio import build_adapter as build_lmstudio_adapter
from .provider_local_ollama import LocalOllamaProvider
from .provider_moonshot import build_adapter as build_moonshot_adapter
from .provider_openai_compat import OpenAICompatibleProvider
from .provider_qianfan import build_adapter as build_qianfan_adapter
from .provider_zhipu import build_adapter as build_zhipu_adapter


@lru_cache(maxsize=1)
def get_registry() -> dict[str, ProviderAdapter]:
    providers: dict[str, ProviderAdapter] = {}

    providers["openai"] = OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="openai",
            label="OpenAI",
            provider_category="global",
            protocol="openai_compatible",
            default_base_url="https://api.openai.com/v1",
            region_hint="global",
            default_models=["gpt-4o-mini", "gpt-4.1", "o4-mini"],
            api_key_env="TCE_OPENAI_API_KEY",
        )
    )
    providers["openrouter"] = OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="openrouter",
            label="OpenRouter",
            provider_category="global",
            protocol="openai_compatible",
            default_base_url="https://openrouter.ai/api/v1",
            region_hint="global",
            default_models=["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
            api_key_env="TCE_OPENROUTER_API_KEY",
        )
    )
    providers["groq"] = OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="groq",
            label="Groq",
            provider_category="global",
            protocol="openai_compatible",
            default_base_url="https://api.groq.com/openai/v1",
            region_hint="global",
            default_models=["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
            api_key_env="TCE_GROQ_API_KEY",
        )
    )
    providers["together"] = OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="together",
            label="Together",
            provider_category="global",
            protocol="openai_compatible",
            default_base_url="https://api.together.xyz/v1",
            region_hint="global",
            default_models=["meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"],
            api_key_env="TCE_TOGETHER_API_KEY",
        )
    )
    providers["xai"] = OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="xai",
            label="xAI",
            provider_category="global",
            protocol="openai_compatible",
            default_base_url="https://api.x.ai/v1",
            region_hint="global",
            default_models=["grok-2-latest"],
            api_key_env="TCE_XAI_API_KEY",
        )
    )
    providers["anthropic"] = AnthropicProvider(
        ProviderMetadata(
            provider_id="anthropic",
            label="Anthropic",
            provider_category="global",
            protocol="native",
            default_base_url="https://api.anthropic.com/v1",
            api_version="2023-06-01",
            region_hint="global",
            default_models=["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"],
            api_key_env="TCE_ANTHROPIC_API_KEY",
        )
    )
    providers["gemini"] = GeminiProvider(
        ProviderMetadata(
            provider_id="gemini",
            label="Gemini",
            provider_category="global",
            protocol="native",
            default_base_url="https://generativelanguage.googleapis.com/v1beta",
            region_hint="global",
            default_models=["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"],
            api_key_env="TCE_GEMINI_API_KEY",
        )
    )

    providers["deepseek"] = build_deepseek_adapter()
    providers["dashscope"] = build_dashscope_adapter()
    providers["zhipu"] = build_zhipu_adapter()
    providers["moonshot"] = build_moonshot_adapter()
    providers["qianfan"] = build_qianfan_adapter()
    providers["hunyuan"] = build_hunyuan_adapter()

    providers["custom"] = build_custom_adapter()
    providers["local_ollama"] = LocalOllamaProvider(
        ProviderMetadata(
            provider_id="local_ollama",
            label="Local Ollama",
            provider_category="custom",
            protocol="native",
            default_base_url="http://localhost:11434",
            region_hint="local",
            default_models=["qwen2.5-coder:7b"],
            api_key_env=None,
            key_optional=True,
        )
    )
    providers["local_lmstudio"] = build_lmstudio_adapter()

    return providers


def get_provider(provider_id: str) -> ProviderAdapter | None:
    return get_registry().get(provider_id.strip().lower())


def list_provider_metadata() -> list[ProviderMetadata]:
    return [adapter.metadata for adapter in get_registry().values()]


def resolve_fallback_chain(primary: str, fallback_chain: list[str] | None = None) -> list[str]:
    registry = get_registry()
    out: list[str] = []
    primary_norm = primary.strip().lower()
    if primary_norm in registry:
        out.append(primary_norm)
    for raw in fallback_chain or []:
        item = str(raw).strip().lower()
        if not item or item not in registry:
            continue
        if item not in out:
            out.append(item)
    return out
