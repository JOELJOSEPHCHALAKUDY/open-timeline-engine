"""P3 effect journal, crash reconcile and the pause, against the Lite HTTP boundary.

``sqlite3`` is used to observe, and in two places to *age* a row — the only way to make a clock
pass in a test without a fake clock in production code.  It is never the assertion path for
behaviour the HTTP boundary must enforce.

Matrix:
  1 every effect row carries an enforcement tier, and advisory cannot claim observed tracing (G4)
  2 opening an effect is fenced against the DIRECTIVE's lease, not the journal's copy (U4/S3)
  3 a stale lease cannot land a resolve (409 stale_lease), and one executor cannot resolve
    another executor's effect row -- the fence binds the AUTHENTICATED caller (G3)
  4 a reaped irreversible effect pauses instead of restarting work (G5, live half), driven through
    POST /v1/takeover/step: the LIVE reaper resolves the journal in the same transaction as the
    lease bump, so the pause fires on the step that reaps and not only after a restart (G1)
  5 an open effect does not block its own directive's capability grants (G15, first half)
  6 an open effect from ANOTHER directive in the session does block them
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_shared.decision_capture import compute_delivery_key
from tce_shared.identity import credential_fingerprint
from tce_shared.redaction import redact_text

_EXEC_TOKEN = "effect-exec-token"
_OPERATOR_TOKEN = "effect-operator-token"
_HOST_TOKEN = "effect-host-token"
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
    "effect_journal_enabled",
    "effect_unknown_pause_enabled",
    "dispatch_startup_reconcile_enabled",
    "behavior_capability_broker_enabled",
)

_BOUND_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}


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
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "effect-lite.db")
    settings.api_tokens = f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}"
    settings.host_capture_tokens = _HOST_TOKEN
    settings.allow_default_token = False
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.charter_enforcement_enabled = False
    settings.effect_journal_enabled = True
    settings.effect_unknown_pause_enabled = True
    settings.behavior_capability_broker_enabled = True
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


def _capture(client: TestClient, content: str) -> None:
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
    assert client.post("/v1/inputs", json=body, headers=_host_headers()).status_code == 201


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


def _mint_claimed_directive(client: TestClient, session_id: str) -> tuple[str, int, str]:
    """Permit + activation + claim.  Returns (directive_id, lease_generation, permit_id)."""
    permit = client.post(
        "/v1/takeover/permit",
        json={"session_id": session_id, "action_kind": "edit", "target_paths": ["docs/notes.md"], "estimated_change_size": 5},
        headers=_exec_headers(),
    )
    assert permit.status_code == 200, permit.text
    assert permit.json()["decision"] == "allow", permit.text
    permit_id = str(permit.json()["permit_id"])
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
    claim = client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_exec_headers(),
    )
    assert claim.status_code == 200, claim.text
    return str(directive_id), int(claim.json()["lease_generation"]), permit_id


def _open_effect(
    client: TestClient,
    *,
    session_id: str,
    directive_id: str,
    lease: int,
    reversibility: str = "irreversible",
    tier: str = "container",
    tracing: str = "unavailable",
    resource: str = "docs/notes.md",
    provider_run_id: str | None = None,
) -> Any:
    return client.post(
        "/v1/effects",
        json={
            "session_id": session_id,
            "directive_id": directive_id,
            "kind": "write",
            "capability": "filesystem.write",
            "resource": resource,
            "argv": ["write", resource],
            "reversibility": reversibility,
            "description": "rewrite the changelog",
            "enforcement_tier": tier,
            "action_tracing": tracing,
            "lease_generation": lease,
            "provider_run_id": provider_run_id,
        },
        headers=_exec_headers(),
    )


def _age_directive(directive_id: str, *, seconds: int) -> None:
    """Arrange the passage of time.  There is no fake clock in the store, and the reconcile's whole
    subject is what a crash left behind hours ago."""
    stamp = (datetime.now(tz=UTC) - timedelta(seconds=seconds)).isoformat()
    conn = _db()
    try:
        conn.execute(
            "UPDATE directive_executions SET lease_expires_at = ?, started_at = ?, updated_at = ? WHERE directive_id = ?",
            (stamp, stamp, stamp, directive_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- cases


def test_every_effect_row_carries_a_tier(lite_client: TestClient) -> None:
    """G4, live half — case 1."""
    session_id = f"tier-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    response = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease)
    assert response.status_code == 200, response.text
    assert response.json()["enforcement_tier"] == "container"
    missing = _rows("SELECT count(*) AS n FROM effect_journal WHERE enforcement_tier IS NULL OR enforcement_tier = ''")
    assert int(missing[0]["n"]) == 0
    assert int(_rows("SELECT count(*) AS n FROM effect_journal")[0]["n"]) == 1


def test_advisory_tier_cannot_claim_observed_tracing(lite_client: TestClient) -> None:
    """G4/G13d — the store path refuses BEFORE the INSERT, not only the codec.

    ``advisory`` means no OS boundary was applied, so nothing observed the action; a row asserting
    both would be a claim the host cannot back.
    """
    session_id = f"advisory-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    response = _open_effect(
        lite_client, session_id=session_id, directive_id=directive_id, lease=lease, tier="advisory", tracing="observed"
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "effect_invalid"
    assert int(_rows("SELECT count(*) AS n FROM effect_journal")[0]["n"]) == 0


def test_open_effect_is_fenced_against_the_directive_lease(lite_client: TestClient) -> None:
    """U4/S3 — case 2.  v1 gave open_effect no lease check at all, so a fenced-out worker could
    open brand-new rows against a directive it no longer held."""
    session_id = f"fence-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    response = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease + 1)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "stale_lease"
    assert int(_rows("SELECT count(*) AS n FROM effect_journal")[0]["n"]) == 0


def test_a_stale_lease_cannot_land_an_effect(lite_client: TestClient) -> None:
    """G13b, second half — case 3.  A resolve presenting a lease the worker does not hold is
    refused by the fenced CAS and the row does not move.

    The fence is asserted directly, without a reap: since G1 the live reaper CLOSES the journal in
    the same transaction as the lease bump, so "reaped but still open" is no longer a state a
    returning worker can find.  The lease fence itself is unchanged and still load-bearing — it is
    what stops a worker holding a superseded lease from landing a result at all.
    """
    session_id = f"stale-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    opened = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease, reversibility="reversible")
    assert opened.status_code == 200, opened.text
    effect_id = opened.json()["effect_id"]
    running = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "running", "resolution_source": "runtime_result", "expected_lease": lease},
        headers=_exec_headers(),
    )
    assert running.status_code == 200, running.text
    assert running.json()["state"] == "running"

    refused = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "confirmed", "resolution_source": "runtime_result", "expected_lease": lease + 1},
        headers=_exec_headers(),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "stale_lease"
    assert refused.json()["detail"]["actual_lease"] == lease
    assert str(_rows("SELECT state FROM effect_journal WHERE effect_id = ?", (effect_id,))[0]["state"]) == "running"


def test_the_live_reap_closes_the_journal_and_fences_the_late_result(lite_client: TestClient) -> None:
    """G1 — the LIVE in-turn reaper resolves the effect journal, not only the boot sweep.

    Before this, ``_load_pending_directive`` bumped the lease and left every open effect exactly as
    it was, so the returning worker found a ``running`` row and the pause never fired in-turn.  Now
    the reversible row is resolved to ``failed`` by ``system:reconciler`` with source ``reaper``, in
    the same transaction as the bump, and the worker's late ``confirmed`` cannot move it.
    """
    session_id = f"reapclose-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    opened = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease, reversibility="reversible")
    assert opened.status_code == 200, opened.text
    effect_id = opened.json()["effect_id"]
    running = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "running", "resolution_source": "runtime_result", "expected_lease": lease},
        headers=_exec_headers(),
    )
    assert running.status_code == 200, running.text

    _age_directive(directive_id, seconds=7200)
    reap = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "still working on the changelog",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "allow_fallback": True,
        },
        headers=_exec_headers(),
    )
    assert reap.status_code == 200, reap.text
    bumped = int(_rows("SELECT lease_generation FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["lease_generation"])
    assert bumped == lease + 1
    row = _rows("SELECT state, resolution_source, resolved_by_actor FROM effect_journal WHERE effect_id = ?", (effect_id,))[0]
    assert str(row["state"]) == "failed"
    assert str(row["resolution_source"]) == "reaper"
    assert str(row["resolved_by_actor"]) == "system:reconciler"

    late = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "confirmed", "resolution_source": "runtime_result", "expected_lease": lease},
        headers=_exec_headers(),
    )
    assert late.status_code == 409, late.text
    assert str(_rows("SELECT state FROM effect_journal WHERE effect_id = ?", (effect_id,))[0]["state"]) == "failed"


def test_one_executor_cannot_resolve_another_executors_effect(lite_client: TestClient) -> None:
    """G3 — the fence compares the DIRECTIVE's ``claimed_executor`` against the AUTHENTICATED
    caller, never against the journal row's own copy of it.

    Binding the journal row's copy made the comparison a row comparing itself: both columns were
    written by the same claim, so the clause was always true and any executor could land a result
    on any other executor's effect at the current lease.
    """
    session_id = f"crossexec-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    assert str(_rows("SELECT claimed_executor FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["claimed_executor"]) == "worker-a"
    opened = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease, reversibility="reversible")
    assert opened.status_code == 200, opened.text
    effect_id = opened.json()["effect_id"]

    # Same token, a different asserted consumer: the lease is current and the intruder presents it.
    intruder = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "running", "resolution_source": "runtime_result", "expected_lease": lease},
        headers=_exec_headers(consumer="worker-b"),
    )
    assert intruder.status_code == 409, intruder.text
    assert intruder.json()["detail"]["error"] == "stale_lease"
    assert str(_rows("SELECT state FROM effect_journal WHERE effect_id = ?", (effect_id,))[0]["state"]) == "prepared"

    # The holder of the claim still can.
    owner_of_claim = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "running", "resolution_source": "runtime_result", "expected_lease": lease},
        headers=_exec_headers(),
    )
    assert owner_of_claim.status_code == 200, owner_of_claim.text
    assert owner_of_claim.json()["state"] == "running"


def test_reaped_irreversible_effect_pauses_instead_of_restarting(lite_client: TestClient) -> None:
    """G5, live half — case 4, and the one that matters.

    Work restarts at ``takeover_step``'s own mint, not at the retry ladder: a reaped directive is
    ABANDONED and the ladder only looks at FAILED/BLOCKED.  So the assertion is about what the next
    step call does, not about an attempt counter.
    """
    session_id = f"pause-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    opened = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease, reversibility="irreversible")
    assert opened.status_code == 200, opened.text
    effect_id = opened.json()["effect_id"]

    # G1 — the reap is driven through the PRODUCTION path, `POST /v1/takeover/step`, not by calling
    # `startup_reconcile` directly.  Calling the boot sweep tested a branch production only reaches
    # after a restart; the pause must fire on the very step that reaps.
    _age_directive(directive_id, seconds=7200)
    before = int(_rows("SELECT count(*) AS n FROM directive_executions WHERE session_id = ?", (session_id,))[0]["n"])
    step = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "carry on with the changelog",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "allow_fallback": True,
        },
        headers=_exec_headers(),
    )
    assert step.status_code == 200, step.text

    directive_rows = _rows("SELECT state, lease_generation FROM directive_executions WHERE directive_id = ?", (directive_id,))
    assert str(directive_rows[0]["state"]) == "abandoned"
    assert int(directive_rows[0]["lease_generation"]) == lease + 1
    effect_rows = _rows("SELECT state, resolution_source, resolved_by_actor FROM effect_journal WHERE effect_id = ?", (effect_id,))
    assert str(effect_rows[0]["state"]) == "unknown"
    assert str(effect_rows[0]["resolution_source"]) == "reaper"
    assert str(effect_rows[0]["resolved_by_actor"]) == "system:reconciler"

    assert str(step.json().get("final_response") or "").startswith(
        "AUTONOMOUS MODE PAUSED: an effect from a previous run is unresolved"
    )
    pending = int(_rows("SELECT count(*) AS n FROM directive_executions WHERE session_id = ? AND state = 'pending'", (session_id,))[0]["n"])
    assert pending == 0
    after = int(_rows("SELECT count(*) AS n FROM directive_executions WHERE session_id = ?", (session_id,))[0]["n"])
    assert after == before

    # And it stays paused on the next step: nothing about the pause depends on the reaping turn.
    again = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "carry on with the changelog",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "allow_fallback": True,
        },
        headers=_exec_headers(),
    )
    assert again.status_code == 200, again.text
    assert str(again.json().get("final_response") or "").startswith(
        "AUTONOMOUS MODE PAUSED: an effect from a previous run is unresolved"
    )


def test_only_a_verified_human_can_resolve_an_unknown_effect(lite_client: TestClient) -> None:
    """The operator escape hatch, and the rule that makes it meaningful: the ACTOR is derived from
    auth, so an executor cannot name itself a system source."""
    from tce_lite_api import reconcile

    session_id = f"owner-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    opened = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease, reversibility="irreversible")
    effect_id = opened.json()["effect_id"]
    _age_directive(directive_id, seconds=7200)
    conn = _db()
    try:
        reconcile.startup_reconcile(conn, settings=get_settings())
    finally:
        conn.close()
    bumped = lease + 1

    executor_try = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "failed", "resolution_source": "owner", "expected_lease": bumped},
        headers=_exec_headers(),
    )
    assert executor_try.status_code == 403, executor_try.text
    assert executor_try.json()["detail"]["error"] == "charter_authority_required"

    owner_try = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "failed", "resolution_source": "owner", "expected_lease": bumped},
        headers=_operator_headers(),
    )
    assert owner_try.status_code == 200, owner_try.text
    assert owner_try.json()["state"] == "failed"
    actor = str(_rows("SELECT resolved_by_actor FROM effect_journal WHERE effect_id = ?", (effect_id,))[0]["resolved_by_actor"])
    assert actor == "system:owner"


def test_open_effect_does_not_block_its_own_grants(lite_client: TestClient) -> None:
    """G15, first half — cases 5 and 6.

    §3.5 step 10 opens the root effect and moves it to ``running`` for the whole dispatch, which is
    exactly the window in which the agent asks for its grants.  Without the current-directive
    exclusion every healthy dispatch would deadlock on itself.
    """
    _capture(lite_client, "please update the changelog")
    session_id = f"grant-{uuid.uuid4().hex[:8]}"
    directive_id, lease, permit_id = _mint_claimed_directive(lite_client, session_id)
    opened = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease, reversibility="reversible")
    assert opened.status_code == 200, opened.text
    effect_id = opened.json()["effect_id"]
    running = lite_client.post(
        f"/v1/effects/{effect_id}/resolve",
        json={"target_state": "running", "resolution_source": "runtime_result", "expected_lease": lease},
        headers=_exec_headers(),
    )
    assert running.status_code == 200, running.text

    grant = lite_client.post(
        "/v1/capabilities/grants",
        json={
            "session_id": session_id,
            "directive_id": directive_id,
            "permit_id": permit_id,
            "capability": "filesystem.write",
            "action": "write",
            "resource": "docs/notes.md",
        },
        headers=_exec_headers(),
    )
    assert grant.status_code == 200, grant.text
    assert grant.json()["decision"] == "allow", grant.text

    # An open effect from a DIFFERENT directive in the same session is a real obligation.
    other_directive = str(uuid.uuid4())
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO effect_journal (
                effect_id, workspace_id, owner_id, session_id, task_id, directive_id, dispatch_id, seq,
                state, kind, reversibility, capability, resource, argv_json, description, intent_digest,
                enforcement_tier, action_tracing, lease_generation, claimed_executor, provider_run_id,
                provider_turn_id, runtime_id, runtime_version, model_id, opened_at, resolved_at,
                resolution_source, resolved_by_actor, evidence_json, created_at, schema_version
            ) VALUES (?, ?, ?, ?, NULL, ?, NULL, 1, 'running', 'write', 'unknown', 'filesystem.write',
                      'docs/other.md', '[]', '', ?, 'container', 'unavailable', 1, 'worker-b', NULL,
                      NULL, '', '', '', ?, NULL, NULL, NULL, '{}', ?, 'v1')
            """,
            (
                str(uuid.uuid4()),
                _WORKSPACE,
                _HUMAN,
                session_id,
                other_directive,
                uuid.uuid4().hex,
                datetime.now(tz=UTC).isoformat(),
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    blocked = lite_client.post(
        "/v1/capabilities/grants",
        json={
            "session_id": session_id,
            "directive_id": directive_id,
            "permit_id": permit_id,
            "capability": "filesystem.write",
            "action": "write",
            "resource": "docs/second.md",
        },
        headers=_exec_headers(),
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["decision"] == "blocked", blocked.text
    assert "unresolved" in blocked.json()["reason"]


def test_effect_open_is_idempotent_on_the_intent(lite_client: TestClient) -> None:
    """`UNIQUE (directive_id, intent_digest)` makes "exactly one row per intent" a database
    guarantee, so a retried POST yields the same effect_id rather than a duplicate."""
    session_id = f"idem-{uuid.uuid4().hex[:8]}"
    directive_id, lease, _permit = _mint_claimed_directive(lite_client, session_id)
    first = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease)
    second = _open_effect(lite_client, session_id=session_id, directive_id=directive_id, lease=lease)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["effect_id"] == second.json()["effect_id"]
    assert int(_rows("SELECT count(*) AS n FROM effect_journal")[0]["n"]) == 1
