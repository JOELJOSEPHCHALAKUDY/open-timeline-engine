from __future__ import annotations

import json

from tce_shared.identity import claim_conflicts, credential_fingerprint, parse_identity_claims, resolve_bound_identity


def test_hashed_credential_resolves_server_bound_identity() -> None:
    token = "secret-token"
    claims = parse_identity_claims(
        json.dumps(
            {
                credential_fingerprint("bearer", token): {
                    "consumer": "codex",
                    "role": "executor",
                    "workspace_id": "workspace-a",
                    "user_id": "codex-executor",
                    "behavior_subject_id": "human-a",
                }
            }
        )
    )
    identity = resolve_bound_identity(claims=claims, kind="bearer", credential=token)
    assert identity is not None
    assert identity.workspace_id == "workspace-a"
    assert identity.user_id == "codex-executor"
    assert claim_conflicts(identity, {"workspace_id": "forged", "user_id": None}) == ["workspace_id"]


def test_invalid_claim_document_fails_closed_to_no_bindings() -> None:
    assert parse_identity_claims("not-json") == {}
