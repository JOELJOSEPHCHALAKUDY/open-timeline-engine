from __future__ import annotations

from .base import ProviderMetadata
from .provider_openai_compat import OpenAICompatibleProvider


class LocalLMStudioProvider(OpenAICompatibleProvider):
    pass


def build_adapter() -> LocalLMStudioProvider:
    return LocalLMStudioProvider(
        ProviderMetadata(
            provider_id="local_lmstudio",
            label="Local LM Studio",
            provider_category="custom",
            protocol="openai_compatible",
            default_base_url="http://localhost:1234/v1",
            region_hint="local",
            default_models=[],
            api_key_env=None,
            key_optional=True,
        )
    )
