from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

_CACHE_LOCK = threading.Lock()
_L1_CACHE: dict[str, dict[str, Any]] = {}
_L1_STATS = {"hits": 0, "misses": 0}


def cache_key(
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    objective_hash: str,
    state_version: str,
    contract_revision: int,
    scope_digest: str,
    policy_revision: str,
) -> str:
    """Key a cached goal queue to the contract and the scope it was computed under.

    The three P2 arguments are required and have no defaults on purpose: a cached queue
    served across a contract bump or across a different resolved scope is a correctness
    bug, and the compile break at every call site is the only reliable way to find them.
    The ``goalq:`` prefix is unchanged so ``invalidate_l1("goalq:")`` keeps working.
    """
    raw = (
        f"{workspace_id}|{user_id}|{session_id}|{objective_hash}|{state_version}"
        f"|{contract_revision}|{scope_digest}|{policy_revision}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"goalq:{digest}"


def get_l1(key: str) -> tuple[dict[str, Any] | None, str]:
    now = time.time()
    with _CACHE_LOCK:
        item = _L1_CACHE.get(key)
        if not item:
            _L1_STATS["misses"] += 1
            return None, "miss"
        expires_at = float(item.get("expires_at", 0.0) or 0.0)
        if expires_at and expires_at < now:
            _L1_CACHE.pop(key, None)
            _L1_STATS["misses"] += 1
            return None, "expired"
        _L1_STATS["hits"] += 1
        item["last_accessed_at"] = now
        return item, "hit"


def put_l1(key: str, payload: dict[str, Any], ttl_seconds: int, source: str = "computed") -> None:
    ttl = max(1, int(ttl_seconds))
    now = time.time()
    with _CACHE_LOCK:
        _L1_CACHE[key] = {
            "payload": payload,
            "source": source,
            "created_at": now,
            "expires_at": now + ttl,
            "last_accessed_at": now,
        }


def invalidate_l1(prefix: str | None = None) -> int:
    with _CACHE_LOCK:
        if not prefix:
            count = len(_L1_CACHE)
            _L1_CACHE.clear()
            return count
        keys = [key for key in _L1_CACHE if key.startswith(prefix)]
        for key in keys:
            _L1_CACHE.pop(key, None)
        return len(keys)


def l1_status() -> dict[str, Any]:
    with _CACHE_LOCK:
        return {
            "entries": len(_L1_CACHE),
            "hits": int(_L1_STATS["hits"]),
            "misses": int(_L1_STATS["misses"]),
        }


def serialize_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def deserialize_payload(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        return {}
    return {}


# Backward-compatible names used by API/lite integrations.
def goal_cache_key(
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    objective_hash: str,
    state_version: str,
    contract_revision: int,
    scope_digest: str,
    policy_revision: str,
) -> str:
    """Thin re-export of :func:`cache_key`; this is the name both backends import."""
    return cache_key(
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        objective_hash=objective_hash,
        state_version=state_version,
        contract_revision=contract_revision,
        scope_digest=scope_digest,
        policy_revision=policy_revision,
    )


def goal_cache_get_l1(key: str) -> tuple[dict[str, Any] | None, str]:
    return get_l1(key)


def goal_cache_put_l1(key: str, payload: dict[str, Any], ttl_seconds: int, source: str = "computed") -> None:
    put_l1(key, payload, ttl_seconds=ttl_seconds, source=source)


def goal_cache_invalidate_l1(prefix: str | None = None) -> int:
    return invalidate_l1(prefix)


def goal_cache_l1_status() -> dict[str, Any]:
    return l1_status()


def goal_cache_serialize(payload: dict[str, Any]) -> str:
    return serialize_payload(payload)


def goal_cache_deserialize(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    return deserialize_payload(raw)
