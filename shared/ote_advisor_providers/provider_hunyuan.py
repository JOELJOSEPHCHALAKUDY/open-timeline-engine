from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="hunyuan",
            label="Tencent Hunyuan",
            provider_category="china",
            protocol="openai_compatible",
            default_base_url="https://api.hunyuan.cloud.tencent.com/v1",
            region_hint="cn",
            default_models=["hunyuan-turbo", "hunyuan-pro"],
            api_key_env="TCE_HUNYUAN_API_KEY",
        )
    )
