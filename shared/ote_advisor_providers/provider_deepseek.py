from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="deepseek",
            label="DeepSeek",
            provider_category="china",
            protocol="openai_compatible",
            default_base_url="https://api.deepseek.com/v1",
            region_hint="cn",
            default_models=["deepseek-chat", "deepseek-reasoner"],
            api_key_env="TCE_DEEPSEEK_API_KEY",
        )
    )
