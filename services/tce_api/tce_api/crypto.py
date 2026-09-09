"""Compatibility shim: the payload encryption helpers moved to tce_shared.crypto in P1 so the
worker and both APIs share one implementation. Import from tce_shared.crypto in new code."""

from tce_shared.crypto import AESGCM, maybe_decrypt_payload, maybe_encrypt_payload  # noqa: F401

__all__ = ["AESGCM", "maybe_decrypt_payload", "maybe_encrypt_payload"]
