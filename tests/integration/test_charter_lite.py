"""P3 authority charter, against the Lite HTTP boundary.

Every case drives the real FastAPI app over ``TestClient`` with the real SQLite store.  ``sqlite3``
is used only to *observe* — never as the assertion path for behaviour the HTTP boundary must
enforce.

Matrix:
  1 a bare ``X-TCE-Role: user`` header cannot create a charter (U1)
  2 a wider charter costs what a narrowing costs: both need a trusted input receipt (U1)
  3 approve activates and supersedes the prior active charter in one step
  4 revoke invalidates already-issued live permits (U3/S11)
  5 the claim gate refuses without an active charter, in both switch positions (U2/G6d)
  6 a client-supplied policy cannot widen the charter (G11, live half)
  7 the Lite store signatures match Full's, parameter name for parameter name
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_shared.decision_capture import compute_delivery_key
from tce_shared.identity import credential_fingerprint
from tce_shared.redaction import redact_text

_EXEC_TOKEN = "charter-exec-token"
_OPERATOR_TOKEN = "charter-operator-token"
_HOST_TOKEN = "charter-host-token"
_HUMAN = "human-1"
_WORKSPACE = "personal"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "host_capture_tokens",
    "allow_default_token",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "charter_default_ttl_seconds",
    "charter_min_ttl_seconds",
    "charter_max_ttl_seconds",
    "effect_unknown_pause_enabled",
    "permit_scope_digest_enforced",
    "dispatch_startup_reconcile_enabled",
)

_BOUND_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_ALL_CAPABILITIES = [
    "filesystem.read",
    "filesystem.write",
    "git.read",
    "git.write",
    "network.read",
    "network.write",
    "process.execute",
    "process.inspect",
    "tce.memory.review",
]


# --------------------------------------------------------------------------- fixtures


def _snapshot_settings() -> dict[str, Any]:
    settings = get_settings()
    return {key: getattr(settings, key, _MISSING) for key in _GUARDED_SETTINGS}


def _restore_settings(snapshot: dict[str, Any]) -> None:
    settings = get_settings()
    for key, value in snapshot.items():
        if value is _MISSING:
            if hasattr(settings, key):
                try:
                    delattr(settings, key)
                except AttributeError:
                    pass
            continue
        setattr(settings, key, value)


@pytest.fixture()
def lite_client(tmp_path: Path) -> Iterator[TestClient]:
    """Compat mode with ONE server-bound human claim, so a header-asserted user stays unverified."""
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "charter-lite.db")
    settings.api_tokens = f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}"
    settings.host_capture_tokens = _HOST_TOKEN
    settings.allow_default_token = False
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.charter_enforcement_enabled = False
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _OPERATOR_TOKEN): {
                "consumer": "operator-ui",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


# --------------------------------------------------------------------------- helpers


def _exec_headers(consumer: str = "worker-a") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_EXEC_TOKEN}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "executor",
        "X-TCE-User": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _header_only_user_headers() -> dict[str, str]:
    """`X-TCE-Role: user` on the executor's OWN bearer. Compat mode accepts the header; the charter
    routes must not treat it as a human."""
    return {
        "Authorization": f"Bearer {_EXEC_TOKEN}",
        "X-TCE-Consumer": "worker-a",
        "X-TCE-Role": "user",
        "X-TCE-User": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _operator_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_OPERATOR_TOKEN}",
        "X-TCE-Consumer": "operator-ui",
        "X-TCE-Role": "user",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
    }


def _host_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_HOST_TOKEN}",
        "X-TCE-Consumer": "host-capture-claude",
        "X-TCE-User": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _capture_receipt(client: TestClient, content: str = "please narrow the charter") -> str:
    """A real trusted-input receipt through the host-capture credential."""
    host_session_id = str(uuid.uuid4())
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    redacted, applied = redact_text(content)
    body = {
        "session_id": host_session_id,
        "delivery_key": compute_delivery_key(host_session_id, "p1", content_sha256),
        "content_sha256": content_sha256,
        "content": redacted,
        "origin_kind": "human_input",
        "observed_at": datetime.now(tz=UTC).isoformat(),
        "original_char_count": len(content),
        "content_truncated": False,
        "redaction_applied": applied,
        "prompt_id": "p1",
        "hook_event_name": "UserPromptSubmit",
        "host_client": "claude",
        "cwd": "/work/open-timeline-engine",
        "project_hint": {"project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"},
        "schema_version": "v1",
    }
    response = client.post("/v1/inputs", json=body, headers=_host_headers())
    assert response.status_code == 201, response.text
    return str(response.json()["receipt_id"])


def _charter_body(receipt_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_receipt_id": receipt_id,
        "enforcement_tier": "container",
        "permitted_roots": ["docs/", "src/"],
        "protected_write_prefixes": ["tests/", ".github/"],
        "permitted_capabilities": _ALL_CAPABILITIES,
        "confirm_required_capabilities": [],
        "caps": {"max_attempts": 3, "max_concurrent_dispatches": 1, "max_wall_seconds": 1800, "budget_minor_units": 500},
        "ttl_seconds": 3600,
    }
    body.update(overrides)
    return body


def _create_and_approve(client: TestClient, **overrides: Any) -> dict[str, Any]:
    receipt_id = _capture_receipt(client)
    created = client.post("/v1/charters", json=_charter_body(receipt_id, **overrides), headers=_operator_headers())
    assert created.status_code == 200, created.text
    charter_id = created.json()["charter_id"]
    approved = client.post(f"/v1/charters/{charter_id}/approve", headers=_operator_headers())
    assert approved.status_code == 200, approved.text
    payload: dict[str, Any] = approved.json()
    assert payload["status"] == "active"
    return payload


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().lite_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = _db()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _mint_pending_directive(client: TestClient, session_id: str) -> str:
    permit = client.post(
        "/v1/takeover/permit",
        json={
            "session_id": session_id,
            "action_kind": "edit",
            "target_paths": ["docs/notes.md"],
            "estimated_change_size": 5,
        },
        headers=_exec_headers(),
    )
    assert permit.status_code == 200, permit.text
    assert permit.json()["decision"] == "allow", permit.text
    step = client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_exec_headers(),
    )
    assert step.status_code == 200, step.text
    directive_id = step.json().get("directive_id")
    assert directive_id, step.text
    return str(directive_id)


# --------------------------------------------------------------------------- cases


def test_role_header_alone_cannot_create_a_charter(lite_client: TestClient) -> None:
    """U1 — case 1. Compat mode lets the caller assert `X-TCE-Role: user`; the charter routes must
    require an identity the SERVER established, or the whole authority layer is self-service."""
    receipt_id = _capture_receipt(lite_client)
    response = lite_client.post(
        "/v1/charters", json=_charter_body(receipt_id), headers=_header_only_user_headers()
    )
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "charter_authority_required"
    assert "identity_unverified" in detail["reasons"]
    assert _rows("SELECT id FROM authority_charters") == []


def test_a_wider_charter_also_needs_a_receipt(lite_client: TestClient) -> None:
    """U1 — case 2, first half. Without this, the receipt requirement on narrowings is bypassed by
    issuing a fresh, wider charter."""
    response = lite_client.post(
        "/v1/charters",
        json=_charter_body(str(uuid.uuid4())),
        headers=_operator_headers(),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"] == "receipt_not_found"
    assert _rows("SELECT id FROM authority_charters") == []


def test_a_narrowing_without_a_receipt_is_refused(lite_client: TestClient) -> None:
    """U1 — case 2, second half."""
    active = _create_and_approve(lite_client)
    response = lite_client.post(
        "/v1/charters/narrowings",
        json={
            "charter_id": active["charter_id"],
            "source_receipt_id": str(uuid.uuid4()),
            "remove_roots": ["src/"],
        },
        headers=_operator_headers(),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"] == "receipt_not_found"
    assert _rows("SELECT id FROM charter_narrowings") == []


def test_approve_supersedes_the_prior_active_charter(lite_client: TestClient) -> None:
    """Case 3. Two active charters for one owner would make "the" active charter ambiguous, and the
    resolver would silently pick one."""
    first = _create_and_approve(lite_client)
    second = _create_and_approve(lite_client, permitted_roots=["docs/"])
    assert first["charter_id"] != second["charter_id"]
    statuses = {str(row["id"]): str(row["status"]) for row in _rows("SELECT id, status FROM authority_charters")}
    assert statuses[first["charter_id"]] == "superseded"
    assert statuses[second["charter_id"]] == "active"
    active = lite_client.get("/v1/charters/active", headers=_operator_headers())
    assert active.status_code == 200, active.text
    assert active.json()["charter_id"] == second["charter_id"]


def test_a_narrowing_is_applied_at_resolve_time(lite_client: TestClient) -> None:
    """The narrowing row is the reader of `charter_narrowings`; without it the trusted narrowing
    channel is write-only."""
    active = _create_and_approve(lite_client)
    receipt_id = _capture_receipt(lite_client, "drop the src root")
    response = lite_client.post(
        "/v1/charters/narrowings",
        json={
            "charter_id": active["charter_id"],
            "source_receipt_id": receipt_id,
            # Either form works: validate_charter_payload normalises "src/" to "src" on the charter,
            # and narrow_charter now matches remove_roots on the normalised form as well as the raw
            # one, so a narrowing naming "src/" removes the same root rather than silently no-opping.
            "remove_roots": ["src"],
            "add_protected_write_prefixes": ["infra/"],
        },
        headers=_operator_headers(),
    )
    assert response.status_code == 200, response.text
    resolved = lite_client.get("/v1/charters/active", headers=_operator_headers()).json()
    assert resolved["charter"]["permitted_roots"] == ["docs"]
    assert "infra/" in resolved["charter"]["protected_write_prefixes"]
    assert len(resolved["narrowing_ids"]) == 1


def test_revoke_invalidates_live_permits(lite_client: TestClient) -> None:
    """U3/S11 — case 4. v1's revocation removed future enforcement rather than in-flight work: an
    `allow` permit minted a second before the revoke would still let a claim through."""
    active = _create_and_approve(lite_client)
    session_id = f"revoke-{uuid.uuid4().hex[:8]}"
    permit = lite_client.post(
        "/v1/takeover/permit",
        json={"session_id": session_id, "action_kind": "edit", "target_paths": ["docs/a.md"], "estimated_change_size": 3},
        headers=_exec_headers(),
    )
    assert permit.status_code == 200, permit.text
    permit_id = permit.json()["permit_id"]
    stamped = _rows("SELECT charter_id, decision FROM execution_permits WHERE id = ?", (permit_id,))
    assert str(stamped[0]["charter_id"]) == active["charter_id"]
    assert str(stamped[0]["decision"]) == "allow"

    revoked = lite_client.post(
        f"/v1/charters/{active['charter_id']}/revoke",
        json={"reason": "scope changed"},
        headers=_operator_headers(),
    )
    assert revoked.status_code == 200, revoked.text
    after = _rows("SELECT decision, reason, resolved_at FROM execution_permits WHERE id = ?", (permit_id,))
    assert str(after[0]["decision"]) == "blocked"
    assert str(after[0]["reason"]) == "charter_revoked"
    assert after[0]["resolved_at"] is not None
    assert lite_client.get("/v1/charters/active", headers=_operator_headers()).json()["charter"] is None


def test_claim_is_refused_without_a_charter(lite_client: TestClient) -> None:
    """U2/G6(d) — case 5, both switch positions.

    With enforcement ON and no active charter the claim is a 409 that names the reason, the
    directive stays `pending`, and the refusal is durably audited.  With
    `charter_enforcement_enabled` OFF the same call succeeds exactly as it does today.
    """
    settings = get_settings()
    session_id = f"claim-{uuid.uuid4().hex[:8]}"
    directive_id = _mint_pending_directive(lite_client, session_id)

    settings.charter_enforcement_enabled = True
    refused = lite_client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_exec_headers(),
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "no_active_charter"
    assert detail["reason"] == "no_active_charter"
    assert detail["message"].startswith("AUTONOMOUS MODE PAUSED: ")
    states = [str(row["state"]) for row in _rows("SELECT state FROM directive_executions WHERE directive_id = ?", (directive_id,))]
    assert states == ["pending"]
    audits = _rows("SELECT action FROM audit_log WHERE action = ?", ("claim_refused_no_charter",))
    assert len(audits) == 1

    settings.charter_enforcement_enabled = False
    allowed = lite_client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_exec_headers(),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["state"] == "in_progress"


def test_claim_succeeds_under_an_active_charter(lite_client: TestClient) -> None:
    """The other half of case 5: enforcement ON *with* a charter is not a wall, it is a gate."""
    settings = get_settings()
    _create_and_approve(lite_client)
    settings.charter_enforcement_enabled = True
    session_id = f"claim-ok-{uuid.uuid4().hex[:8]}"
    directive_id = _mint_pending_directive(lite_client, session_id)
    claim = lite_client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_exec_headers(),
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["state"] == "in_progress"


def test_step_response_carries_the_charter_to_the_executor(lite_client: TestClient) -> None:
    """§9.3 — the five charter/effect fields on TakeoverStepResponse are POPULATED, not just declared.

    This is the only channel that carries the charter's machine-readable scope to an executor. Left
    unwired, `charter_constraints()` has no production caller, the MCP firewall merges an empty
    backend list and shows the executor its own floor and nothing about this charter, and
    `unresolved_effects` is permanently `[]` so the firewall's unknown-effect pause never fires.
    Every assertion below failed before the fields were wired.
    """
    approved = _create_and_approve(lite_client, permitted_roots=["docs/"])
    session_id = f"wire-{uuid.uuid4().hex[:8]}"
    response = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "activation_mode_default": "takeover",
        },
        headers=_exec_headers(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["charter_active"] is True
    assert payload["charter_version"]
    # The tier reported to the executor is the APPROVED charter's, not a constant.
    assert payload["enforcement_tier"] == approved["charter"]["enforcement_tier"]

    rules = {rule["rule_id"]: rule for rule in payload["constraints"]}
    # The floor a charter may add to but never delete.
    assert "no-edit-protected-dirs" in rules
    assert "no-edit-firewall-null-response" in rules
    # The charter's OWN scope, which is the whole reason the field exists.
    assert "charter-roots-only" in rules
    assert rules["charter-roots-only"]["polarity"] == "allow_only"
    assert rules["charter-roots-only"]["scope"]["path_prefixes"] == ["docs"]
    # polarity is mandatory on every rule: without it a conforming executor reads the one allow
    # list as a deny list and inverts the charter.
    assert all("polarity" in rule for rule in payload["constraints"])

    # No open effects yet, but the key must be present and list-shaped rather than absent.
    assert payload["unresolved_effects"] == []


def test_step_response_says_so_when_no_charter_is_active(lite_client: TestClient) -> None:
    """The negative half: no charter means charter_active=false and NO charter-derived rule. The
    executor must not be told a scope that nobody granted."""
    session_id = f"nocharter-{uuid.uuid4().hex[:8]}"
    response = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "activation_mode_default": "takeover",
        },
        headers=_exec_headers(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["charter_active"] is False
    assert payload["charter_version"] == ""
    assert payload["enforcement_tier"] is None
    assert payload["constraints"] == []


def test_client_supplied_policy_cannot_widen(lite_client: TestClient) -> None:
    """G11, live half — case 6.

    The charter is resolved server-side from auth + session.  A step that turns the safety policy
    off and declares `permitted_roots: ["/"]` in its own constraints changes nothing about the
    permit decision for a path the charter does not cover.
    """
    _create_and_approve(lite_client, permitted_roots=["docs/"])
    session_id = f"widen-{uuid.uuid4().hex[:8]}"
    lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "policy": {"safety_policy": "off"},
            "constraints": {"permitted_roots": ["/"]},
            "allow_fallback": True,
        },
        headers=_exec_headers(),
    )
    outside = lite_client.post(
        "/v1/takeover/permit",
        json={
            "session_id": session_id,
            "action_kind": "edit",
            "target_paths": ["services/tce_api/tce_api/main.py"],
            "estimated_change_size": 3,
        },
        headers=_exec_headers(),
    )
    assert outside.status_code == 200, outside.text
    body = outside.json()
    assert body["decision"] == "blocked", body
    assert "outside every permitted root" in body["reason"] or "protected write prefix" in body["reason"]


def test_store_signatures_match_full() -> None:
    """The parity assertion: the Lite modules expose the same keyword names as Full's, so a caller
    written against one reads correctly against the other."""
    from tce_api import charter_store as full_charter
    from tce_api import dispatch_store as full_dispatch
    from tce_api import effect_store as full_effect
    from tce_api import verification_store as full_verification
    from tce_lite_api import charter_store as lite_charter
    from tce_lite_api import dispatch_store as lite_dispatch
    from tce_lite_api import effect_store as lite_effect
    from tce_lite_api import verification_store as lite_verification

    pairs = [
        (full_charter, lite_charter, ["create_charter", "approve_charter", "revoke_charter", "apply_narrowing",
                                      "load_active_charter", "resolve_charter_or_refuse", "load_charter_by_id"]),
        (full_effect, lite_effect, ["open_effect", "resolve_effect", "list_open_effects",
                                    "load_effects_for_directive", "open_effect_count_for_session"]),
        (full_verification, lite_verification, ["freeze_acceptance_criteria", "load_acceptance_criteria",
                                                "record_verification"]),
        (full_dispatch, lite_dispatch, ["open_dispatch", "bind_provider_run", "reconcile_dispatch", "load_dispatch",
                                        "open_reservation_total", "open_dispatch_count", "record_self_test",
                                        "latest_self_test"]),
    ]
    for full_module, lite_module, names in pairs:
        for name in names:
            full_params = list(inspect.signature(getattr(full_module, name)).parameters)
            lite_params = list(inspect.signature(getattr(lite_module, name)).parameters)
            # The first positional differs by design (db: Session vs conn: sqlite3.Connection).
            assert full_params[1:] == lite_params[1:], f"{name}: {full_params} != {lite_params}"


def test_both_backends_populate_every_new_step_field() -> None:
    """The bug class this catches: a field DECLARED on TakeoverStepResponse that no backend ever
    passes. Five of them shipped that way -- `constraints` among them, which is the only channel
    carrying the charter's machine-readable scope to an executor, so an unwired `constraints` means
    `charter_constraints()` has no production caller at all. A model default of `[]` makes that
    failure completely silent on the wire, which is why this is an AST assertion and not a
    round-trip.
    """
    import ast

    required = {"charter_active", "charter_version", "enforcement_tier", "unresolved_effects", "constraints"}
    sites = {
        "services/tce_api/tce_api/main.py": "tce_api",
        "services/tce_lite_api/tce_lite_api/store.py": "tce_lite_api",
    }
    for path, label in sites.items():
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        populated: set[str] = set()
        found_any = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "TakeoverStepResponse":
                continue
            found_any = True
            populated |= {kw.arg for kw in node.keywords if kw.arg}
        assert found_any, f"{label}: no TakeoverStepResponse construction found"
        # Early-return responses (stand-down, inactive) legitimately omit them; the union across
        # every construction site is what must cover the set.
        missing = required - populated
        assert not missing, f"{label}: TakeoverStepResponse never populates {sorted(missing)}"

    # And the charter's own scope rule must actually be reachable from a backend.
    for path in sites:
        assert "charter_constraints" in Path(path).read_text(encoding="utf-8"), (
            f"{path} never calls charter_constraints(); the charter's scope never reaches an executor"
        )


def test_drain_pending_handoffs_key_sets_match() -> None:
    """§10.4's second parity assertion: both backends' drain returns the same four counters."""
    from tce_api import continuity_store as full_continuity
    from tce_lite_api import continuity_store as lite_continuity

    full_params = set(inspect.signature(full_continuity.drain_pending_handoffs).parameters) - {"db"}
    lite_params = set(inspect.signature(lite_continuity.drain_pending_handoffs).parameters) - {"conn"}
    assert full_params == lite_params
