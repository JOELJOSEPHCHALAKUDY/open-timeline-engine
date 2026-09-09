from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BoundIdentity:
    consumer: str
    role: str
    workspace_id: str
    user_id: str
    behavior_subject_id: str
    capabilities: tuple[str, ...] = ()


def credential_fingerprint(kind: str, credential: str) -> str:
    digest = hashlib.sha256(str(credential or "").encode("utf-8")).hexdigest()
    return f"{kind.strip().lower()}:{digest}"


def parse_identity_claims(raw: str | dict[str, Any] | None) -> dict[str, BoundIdentity]:
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            decoded = json.loads(str(raw or "{}").strip() or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        payload = decoded if isinstance(decoded, dict) else {}
    claims: dict[str, BoundIdentity] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        consumer = str(value.get("consumer") or "bound-client").strip() or "bound-client"
        user_id = str(value.get("user_id") or consumer).strip() or consumer
        claims[str(key).strip().lower()] = BoundIdentity(
            consumer=consumer[:160],
            role=str(value.get("role") or "executor").strip().lower()[:32],
            workspace_id=str(value.get("workspace_id") or "personal").strip()[:160] or "personal",
            user_id=user_id[:160],
            behavior_subject_id=str(value.get("behavior_subject_id") or user_id).strip()[:160] or user_id[:160],
            capabilities=tuple(sorted({str(c) for c in (value.get("capabilities") or []) if str(c) in {"host_capture"}})) if isinstance(value.get("capabilities"), list) else (),
        )
    return claims


def resolve_bound_identity(
    *,
    claims: dict[str, BoundIdentity],
    kind: str,
    credential: str,
) -> BoundIdentity | None:
    normalized_kind = str(kind or "").strip().lower()
    digest_key = credential_fingerprint(normalized_kind, credential)
    direct_key = f"{normalized_kind}:{str(credential or '').strip()}".lower()
    return claims.get(digest_key) or claims.get(direct_key)


def claim_conflicts(identity: BoundIdentity, asserted: dict[str, str | None]) -> list[str]:
    expected = {
        "consumer": identity.consumer,
        "role": identity.role,
        "workspace_id": identity.workspace_id,
        "user_id": identity.user_id,
        "behavior_subject_id": identity.behavior_subject_id,
    }
    conflicts: list[str] = []
    for key, value in asserted.items():
        token = str(value or "").strip()
        if token and token != expected[key]:
            conflicts.append(key)
    return conflicts
