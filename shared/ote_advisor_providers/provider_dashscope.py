from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="dashscope",
            label="Alibaba DashScope (Qwen)",
            provider_category="china",
            protocol="openai_compatible",
            default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            region_hint="cn",
            default_models=["qwen-max", "qwen-plus", "qwen-turbo"],
            api_key_env="TCE_DASHSCOPE_API_KEY",
        )
    )
