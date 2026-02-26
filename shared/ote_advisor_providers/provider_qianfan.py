from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="qianfan",
            label="Baidu Qianfan (ERNIE)",
            provider_category="china",
            protocol="openai_compatible",
            default_base_url="https://qianfan.baidubce.com/v2",
            region_hint="cn",
            default_models=["ernie-4.0-8k", "ernie-3.5-8k"],
            api_key_env="TCE_QIANFAN_API_KEY",
        )
    )
