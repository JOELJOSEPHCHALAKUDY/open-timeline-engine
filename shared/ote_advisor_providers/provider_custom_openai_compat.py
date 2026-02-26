from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


def build_adapter() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderMetadata(
            provider_id="custom",
            label="Custom Hosted (OpenAI-compatible)",
            provider_category="custom",
            protocol="openai_compatible",
            default_base_url="",
            region_hint="self_hosted",
            default_models=[],
            api_key_env="TCE_ADVISOR_CUSTOM_API_KEY",
            key_optional=True,
        )
    )
