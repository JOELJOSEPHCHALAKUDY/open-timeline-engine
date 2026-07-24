from __future__ import annotations

import json
from typing import Any, cast

from .gateway import ModelGateway


class AnthropicGateway(ModelGateway):
    """Anthropic-backed gateway. Extraction-only — embed() raises NotImplementedError."""

    def __init__(self, api_key: str, extract_model: str, timeout: float = 30) -> None:
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key, timeout=timeout)
        self.extract_model = extract_model

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError(
            "AnthropicGateway does not support embeddings. "
            "Use Ollama or OpenAI for embedding, and Anthropic for extraction only."
        )

    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        response = self.client.messages.create(
            model=self.extract_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": f"Schema:{schema_name}\n{prompt}\n\nRespond ONLY with valid JSON."}],
        )
        raw = str(getattr(response.content[0], "text", "{}")) if response.content else "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"raw": raw}
        except Exception:
            return {"raw": raw}
