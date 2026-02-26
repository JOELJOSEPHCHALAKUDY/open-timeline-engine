from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="zhipu",
            label="Zhipu GLM",
            provider_category="china",
            protocol="openai_compatible",
            default_base_url="https://open.bigmodel.cn/api/paas/v4",
            region_hint="cn",
            default_models=["glm-4-plus", "glm-4-flash"],
            api_key_env="TCE_ZHIPU_API_KEY",
        )
    )
