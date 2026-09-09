from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any

LOGGER = logging.getLogger(__name__)
_WARNED = False
AESGCM: Any

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM

    AESGCM = _AESGCM
except Exception:  # pragma: no cover - optional dependency fallback
    AESGCM = None


def _derive_key(secret: str, key_id: str) -> bytes:
    seed = f"{secret}:{key_id}".encode()
    return hashlib.sha256(seed).digest()


def maybe_encrypt_payload(
    payload: dict[str, Any],
    enabled: bool,
    min_sensitivity: int,
    sensitivity: int,
    required: bool = False,
    key_id: str = "local-dev",
    secret: str = "",
) -> dict[str, Any]:
    global _WARNED
    if not enabled and not required:
        return payload
    if sensitivity < min_sensitivity:
        return payload
    if AESGCM is None:
        if required:
            raise RuntimeError("AESGCM backend unavailable but sensitive encryption is required")
        if not _WARNED:
            LOGGER.warning(
                "Payload encryption enabled but cryptography backend unavailable; storing plaintext."
            )
            _WARNED = True
        return payload

    effective_secret = secret.strip() or os.getenv("TCE_SECURITY_ENCRYPTION_SECRET", "").strip()
    if not effective_secret:
        if required:
            raise RuntimeError("Sensitive encryption required but no encryption secret configured")
        if not _WARNED:
            LOGGER.warning("Payload encryption enabled but no secret configured; storing plaintext.")
            _WARNED = True
        return payload

    key = _derive_key(effective_secret, key_id)
    nonce = os.urandom(12)
    plaintext = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return {
        "_tce_encrypted": True,
        "alg": "aesgcm",
        "key_id": key_id,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def maybe_decrypt_payload(payload: dict[str, Any], secret: str = "") -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if not payload.get("_tce_encrypted"):
        return payload
    if AESGCM is None:
        raise RuntimeError("AESGCM backend unavailable for decrypt")
    key_id = str(payload.get("key_id") or "local-dev")
    nonce_b64 = str(payload.get("nonce") or "")
    ciphertext_b64 = str(payload.get("ciphertext") or "")
    if not nonce_b64 or not ciphertext_b64:
        raise RuntimeError("Invalid encrypted payload envelope")
    effective_secret = secret.strip() or os.getenv("TCE_SECURITY_ENCRYPTION_SECRET", "").strip()
    if not effective_secret:
        raise RuntimeError("Cannot decrypt payload without encryption secret")
    key = _derive_key(effective_secret, key_id)
    nonce = base64.b64decode(nonce_b64.encode("ascii"))
    ciphertext = base64.b64decode(ciphertext_b64.encode("ascii"))
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    decoded = json.loads(plaintext.decode("utf-8"))
    return decoded if isinstance(decoded, dict) else {}
