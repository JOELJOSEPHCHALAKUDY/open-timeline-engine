from __future__ import annotations

import os
import secrets

import keyring

SERVICE_NAME = "open-timeline-engine"
INDEX_KEY = "token-index"


def _index() -> list[str]:
    try:
        raw = keyring.get_password(SERVICE_NAME, INDEX_KEY) or ""
    except Exception:
        raw = ""
    return [name for name in raw.split(",") if name]


def _save_index(names: list[str]) -> None:
    try:
        keyring.set_password(SERVICE_NAME, INDEX_KEY, ",".join(sorted(set(names))))
    except Exception:
        return


def issue_token(name: str) -> str:
    token = secrets.token_urlsafe(32)
    try:
        keyring.set_password(SERVICE_NAME, f"token:{name}", token)
    except Exception:
        pass
    names = _index()
    names.append(name)
    _save_index(names)
    return token


def revoke_token(name: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, f"token:{name}")
    except Exception:
        pass
    names = [item for item in _index() if item != name]
    _save_index(names)


def list_tokens() -> list[str]:
    return _index()


def resolve_active_tokens(env_tokens: set[str]) -> set[str]:
    tokens = set(env_tokens)
    for name in _index():
        try:
            token = keyring.get_password(SERVICE_NAME, f"token:{name}")
        except Exception:
            token = None
        if token:
            tokens.add(token)
    extra = os.getenv("TCE_EXTRA_TOKENS", "")
    tokens.update({x.strip() for x in extra.split(",") if x.strip()})
    return tokens
