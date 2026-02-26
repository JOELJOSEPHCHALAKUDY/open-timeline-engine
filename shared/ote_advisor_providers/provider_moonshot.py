from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="moonshot",
            label="Moonshot Kimi",
            provider_category="china",
            protocol="openai_compatible",
            default_base_url="https://api.moonshot.cn/v1",
            region_hint="cn",
            default_models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
            api_key_env="TCE_MOONSHOT_API_KEY",
        )
    )
