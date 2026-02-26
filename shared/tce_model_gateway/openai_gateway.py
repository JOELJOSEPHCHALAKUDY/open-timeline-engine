from __future__ import annotations

import json
from typing import Any, cast

from .gateway import ModelGateway


class OpenAIGateway(ModelGateway):
    def __init__(
        self,
        api_key: str,
        embed_model: str,
        extract_model: str,
        timeout: int = 30,
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": 0,
        }
        if base_url and base_url.strip():
            kwargs["base_url"] = base_url.strip()
        self.client = OpenAI(**kwargs)
        self.embed_model = embed_model
        self.extract_model = extract_model

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(model=self.embed_model, input=text)
        return list(response.data[0].embedding)

    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.extract_model,
            messages=[{"role": "user", "content": f"Schema:{schema_name}\n{prompt}"}],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"raw": raw}
        except Exception:
            return {"raw": raw}
