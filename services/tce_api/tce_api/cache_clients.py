from __future__ import annotations

from typing import Any

_REDIS_CLIENT: Any | None = None
_REDIS_URL: str | None = None


def get_redis_client(redis_url: str) -> Any | None:
    global _REDIS_CLIENT, _REDIS_URL
    if not redis_url:
        return None
    if _REDIS_CLIENT is not None and _REDIS_URL == redis_url:
        return _REDIS_CLIENT
    try:
        import redis as _redis

        _REDIS_CLIENT = _redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
        _REDIS_URL = redis_url
        return _REDIS_CLIENT
    except Exception:
        _REDIS_CLIENT = None
        _REDIS_URL = redis_url
        return None
