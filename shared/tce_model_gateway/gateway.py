from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, cast

import httpx
import requests

logger = logging.getLogger(__name__)


class ModelGateway(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    @abstractmethod
    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        raise NotImplementedError

    async def aembed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed, text)

    async def aextract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.extract_structured, prompt, schema_name)


class CachedGateway(ModelGateway):
    """Wraps any ModelGateway and caches embeddings in Redis."""

    def __init__(self, inner: ModelGateway, redis_url: str, ttl_seconds: int = 3600) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._redis: Any | None = None
        self._redis_url = redis_url
        self._connect_failed = False

    def _get_redis(self) -> Any | None:
        if self._connect_failed:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis
            self._redis = redis.from_url(self._redis_url, socket_connect_timeout=1, socket_timeout=1)
            self._redis.ping()
            return self._redis
        except Exception:
            self._connect_failed = True
            return None

    @staticmethod
    def _cache_key(text: str) -> str:
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        return f"tce:emb:{h}"

    def embed(self, text: str) -> list[float]:
        r = self._get_redis()
        if r is not None:
            key = self._cache_key(text)
            try:
                cached = r.get(key)
                if cached is not None:
                    return json.loads(cached)
            except Exception:
                pass
        result = self._inner.embed(text)
        if r is not None:
            try:
                r.setex(self._cache_key(text), self._ttl, json.dumps(result))
            except Exception:
                pass
        return result

    async def aembed(self, text: str) -> list[float]:
        r = await asyncio.to_thread(self._get_redis)
        if r is not None:
            key = self._cache_key(text)
            try:
                cached = await asyncio.to_thread(r.get, key)
                if cached is not None:
                    return json.loads(cached)
            except Exception:
                pass
        result = await self._inner.aembed(text)
        if r is not None:
            try:
                await asyncio.to_thread(r.setex, self._cache_key(text), self._ttl, json.dumps(result))
            except Exception:
                pass
        return result

    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        return self._inner.extract_structured(prompt, schema_name)

    async def aextract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        return await self._inner.aextract_structured(prompt, schema_name)


class OllamaGateway(ModelGateway):
    def __init__(self, base_url: str, embed_model: str, extract_model: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.embed_model = embed_model
        self.extract_model = extract_model
        self.timeout = timeout
        self._session: requests.Session | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _get_async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout)

    @staticmethod
    def _parse_embedding(body: dict[str, Any]) -> list[float]:
        embedding = body.get("embedding")
        if not isinstance(embedding, list):
            raise RuntimeError("Invalid embedding response from model gateway")
        return [float(x) for x in embedding]

    @staticmethod
    def _parse_extract(body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("response", "{}")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return cast(dict[str, Any], parsed)
                return {"raw": raw}
            except Exception:
                return {"raw": raw}
        return {"raw": str(raw)}

    def embed(self, text: str) -> list[float]:
        response = self._get_session().post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.embed_model, "prompt": text},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._parse_embedding(response.json())

    async def aembed(self, text: str) -> list[float]:
        async with self._get_async_client() as client:
            response = await client.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.embed_model, "prompt": text},
            )
        response.raise_for_status()
        return self._parse_embedding(response.json())

    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        response = self._get_session().post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.extract_model,
                "prompt": f"Schema:{schema_name}\n{prompt}",
                "stream": False,
                "format": "json",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._parse_extract(response.json())

    async def aextract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        async with self._get_async_client() as client:
            response = await client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.extract_model,
                    "prompt": f"Schema:{schema_name}\n{prompt}",
                    "stream": False,
                    "format": "json",
                },
            )
        response.raise_for_status()
        return self._parse_extract(response.json())
