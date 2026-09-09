"""P0 trust-boundary verification matrix against the Lite HTTP boundary.

Every case drives the real FastAPI app over ``TestClient`` with the real
SQLite store. SQL is used only to *observe* state (and, in case 2, to
reproduce a raw claim race against the fenced UPDATE) -- never as the
assertion path for behaviour the HTTP boundary must enforce.

Matrix:
  1 unclaimed permit-required directive reported successful -> rejected, no lifecycle event
  2 two workers claim one attempt concurrently -> one lease; loser cannot report
  3 stale worker reports after lease replacement -> rejected; late evidence retained (audit row)
  4 identical completion retry vs changed payload same key -> replay vs 409; no duplicate handoff
  5 Codex completes in session A; Claude resumes in session B -> correct packet; reverse too
  6 same git remote in two workspaces -> no cross-workspace events/dreams leak
  7 normal query misses owner scope -> empty within scope; no implicit peer expansion
  8 core completion without prior takeover -> explicit project binding or visible unbound
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_shared.identity import credential_fingerprint

_TOKEN = "trust-boundary-token"
# A second credential the server binds to a human claim: identity_verified even in compat mode.
_OPERATOR_TOKEN = "trust-boundary-operator-token"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "takeover_lease_strict",
    "scope_strict_tags",
    # P2 §0.5 — the durable-task-state and retrieval-deadline settings. They are guarded
    # here for the same reason as every name above: this fixture mutates the process-wide
    # Settings singleton, and an unguarded name leaks into every later test in the run.
    # getattr/_MISSING makes listing them safe before Builder C adds them to the Lite config.
    "takeover_turn_budget_ms",
    "task_state_enabled",
    "task_state_markdown_enabled",
    "task_state_markdown_max_steps",
    "planning_async_enabled",
    "planning_job_lease_seconds",
    "planning_job_max_attempts",
    "planning_job_batch_size",
    "planning_job_backoff_cap_seconds",
    "planning_pending_hint_ms",
    "retrieval_deadline_enabled",
    "retrieval_deadline_floor_ms",
    "retrieval_statement_floor_ms",
    "retrieval_advisor_min_ms",
    "sqlite_progress_instructions",
)

# A repo identity that canonical_project_context resolves to a stable project_id.
_BOUND_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_UNBOUND_APP_CONTEXT: dict[str, Any] = {"domain": "coding"}
_MUTATING_TASK = "update the changelog notes"


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
    """Compat-mode client: identity comes from X-TCE-* headers (consumer/user split matters)."""
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "trust-boundary.db")
    settings.api_tokens = f"{_TOKEN},{_OPERATOR_TOKEN}"
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    # Compat mode with ONE server-bound claim: everything asserted through X-TCE-* on _TOKEN stays
    # unverified, while _OPERATOR_TOKEN carries a human identity the server established itself.
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _OPERATOR_TOKEN): {
                "consumer": "operator-ui",
                "role": "user",
                "workspace_id": "personal",
                "user_id": "shared-user",
                "behavior_subject_id": "shared-user",
            }
        }
    )
    settings.workspace_access_mode = "compat"
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


@pytest.fixture()
def bound_client(tmp_path: Path) -> Iterator[TestClient]:
    """Enforce-mode client: three server-bound executors, two workspaces."""
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "trust-boundary-bound.db")
    settings.api_tokens = "codex-token,claude-token,other-token"
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "enforce"
    settings.workspace_access_mode = "compat"
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", "codex-token"): {
                "consumer": "codex-executor",
                "role": "executor",
                "workspace_id": "shared",
                "user_id": "codex-executor",
                "behavior_subject_id": "human",
            },
            credential_fingerprint("bearer", "claude-token"): {
                "consumer": "claude-executor",
                "role": "executor",
                "workspace_id": "shared",
                "user_id": "claude-executor",
                "behavior_subject_id": "human",
            },
            credential_fingerprint("bearer", "other-token"): {
                "consumer": "other-executor",
                "role": "executor",
                "workspace_id": "other",
                "user_id": "other-executor",
                "behavior_subject_id": "other-human",
            },
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


# --------------------------------------------------------------------------- helpers


def _headers(consumer: str = "worker-a", *, user: str | None = None, role: str = "executor", workspace: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
    }
    if user is not None:
        headers["X-TCE-User"] = user
    if workspace is not None:
        headers["X-TCE-Workspace"] = workspace
    return headers


def _verified_operator_headers() -> dict[str, str]:
    """A human identity the server bound to the credential, not one asserted in a header."""
    return {
        "Authorization": f"Bearer {_OPERATOR_TOKEN}",
        "X-TCE-Consumer": "operator-ui",
        "X-TCE-Role": "user",
        "X-TCE-User": "shared-user",
        "X-TCE-Behavior-Subject": "shared-user",
    }


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().lite_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _directive_row(directive_id: str) -> sqlite3.Row:
    conn = _db()
    try:
        row: sqlite3.Row | None = conn.execute("SELECT * FROM directive_executions WHERE directive_id = ?", (directive_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"directive {directive_id} missing"
    return row


def _count(sql: str, params: tuple[Any, ...] = ()) -> int:
    conn = _db()
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _audit_rows(action: str) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute("SELECT consumer, action, query, policy_decisions FROM audit_log WHERE action = ? ORDER BY ts ASC", (action,)).fetchall()
    finally:
        conn.close()
    return [
        {
            "consumer": str(row["consumer"]),
            "action": str(row["action"]),
            "query": json.loads(str(row["query"] or "{}")),
            "policy_decisions": json.loads(str(row["policy_decisions"] or "{}")),
        }
        for row in rows
    ]


def _request_permit(client: TestClient, session_id: str, headers: dict[str, str]) -> dict[str, Any]:
    permit = client.post(
        "/v1/takeover/permit",
        json={"session_id": session_id, "action_kind": "edit", "target_paths": ["docs/notes.md"], "estimated_change_size": 5},
        headers=headers,
    )
    assert permit.status_code == 200, permit.text
    body: dict[str, Any] = permit.json()
    assert body["decision"] == "allow"
    return body


def _step(
    client: TestClient,
    session_id: str,
    headers: dict[str, str],
    *,
    message: str = "beru take over",
    task: str | None = _MUTATING_TASK,
    app_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "persona_mode": "shadow",
        "app_context": dict(_BOUND_APP_CONTEXT if app_context is None else app_context),
        "constraints": {"k": 4},
        "allow_fallback": True,
    }
    if task is not None:
        payload["task"] = task
    step = client.post("/v1/takeover/step", json=payload, headers=headers)
    assert step.status_code == 200, step.text
    body: dict[str, Any] = step.json()
    return body


def _mint_pending_directive(client: TestClient, session_id: str, headers: dict[str, str]) -> str:
    """Permit + activation step; returns a PENDING, permit-required directive id. No claim."""
    _request_permit(client, session_id, headers)
    body = _step(client, session_id, headers)
    directive_id = body.get("directive_id")
    assert directive_id, f"expected a PENDING directive with a valid ALLOW permit: {body}"
    assert body.get("directive_state") == "pending"
    return str(directive_id)


def _claim_raw(client: TestClient, session_id: str, directive_id: str, headers: dict[str, str]) -> httpx.Response:
    response: httpx.Response = client.post("/v1/takeover/execution/claim", json={"session_id": session_id, "directive_id": directive_id}, headers=headers)
    return response


def _claim(client: TestClient, session_id: str, directive_id: str, headers: dict[str, str]) -> dict[str, Any]:
    claim = _claim_raw(client, session_id, directive_id, headers)
    assert claim.status_code == 200, claim.text
    body: dict[str, Any] = claim.json()
    assert body["state"] == "in_progress"
    return body


def _report_raw(
    client: TestClient,
    session_id: str,
    directive_id: str,
    state: str,
    *,
    headers: dict[str, str] | None = None,
    lease: int | None = None,
    idempotency_key: str | None = None,
    result: str = "success",
    failure_reason: str | None = None,
    details: dict[str, Any] | None = None,
    cancel_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "directive_id": directive_id,
        "state": state,
        "result": result,
        "failure_reason": failure_reason,
    }
    if lease is not None:
        payload["lease_generation"] = lease
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    if details is not None:
        payload["details"] = details
    if cancel_reason is not None:
        payload["cancel_reason"] = cancel_reason
    if extra:
        payload.update(extra)
    response: httpx.Response = client.post("/v1/takeover/execution/report", json=payload, headers=headers or _headers())
    return response


def _rejection(response: httpx.Response, reason: str) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["reason"] == reason, detail
    for key in ("directive_id", "state", "lease_generation", "message"):
        assert key in detail, detail
    return dict(detail)


def _pending_states(client: TestClient, session_id: str, headers: dict[str, str]) -> list[str]:
    status = client.get("/v1/takeover/execution/status", params={"session_id": session_id}, headers=headers)
    assert status.status_code == 200, status.text
    return [str(item["state"]) for item in status.json().get("pending", [])]


def _completion_body(session_id: str, key: str, *, title: str, decision: str = "fenced update", commit: str = "abc123def456", app_context: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": session_id,
        "completion_key": key,
        "source": "integration",
        "state": "succeeded",
        "title": title,
        "payload": {"files": ["services/tce_api/tce_api/main.py"]},
        "decision": decision,
        "outcome": {"status": "succeeded", "next_step": "run tests"},
        "git": {"branch": "main", "commit": commit},
        "anchors": [{"file": "services/tce_api/tce_api/main.py", "line": 10898, "symbol": "takeover_execution_claim"}],
        "milestone_schema": "v1",
    }
    if app_context is not None:
        body["app_context"] = app_context
    return body


def _resume(client: TestClient, headers: dict[str, str], **overrides: Any) -> httpx.Response:
    payload: dict[str, Any] = {"query": "wire lease fencing", "k": 5, "include_cross_user": True}
    payload.update(overrides)
    response: httpx.Response = client.post("/v1/handoff/resume", json=payload, headers=headers)
    return response


def _event_payload(title: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": "cli",
        "domain": "coding",
        "task_type": "debug",
        "event_type": "TASK_STEP",
        "title": title,
        "payload": {"summary": title},
        "context": {"project": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": ["trust-boundary"],
        "sensitivity": 1,
        "redaction_hints": [],
    }


def _search_ids(client: TestClient, headers: dict[str, str], query: str, **overrides: Any) -> tuple[set[str], dict[str, Any]]:
    payload: dict[str, Any] = {"query": query, "k": 10}
    payload.update(overrides)
    search = client.post("/v1/search", json=payload, headers=headers)
    assert search.status_code == 200, search.text
    body = search.json()
    ids = {str(hit["id"]) for hit in body["result"]["hits"]}
    return ids, dict(body["policy"]["retrieval"])


def _session() -> str:
    return f"tb-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- case 1


def test_unclaimed_permit_required_report_is_rejected(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())

    rejected = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers())
    detail = _rejection(rejected, "not_claimed")
    assert detail["directive_id"] == directive_id
    assert detail["state"] == "pending"
    # The report-phase permit check sees started_at=None on a PENDING row; that must never mask
    # the validator's ownership reason (Full regressed to 'permit_invalid:permit_expired_before_start').
    assert not detail["reason"].startswith("permit_invalid")
    assert "permit" not in detail["message"].lower()

    # No lifecycle event: the row is untouched and nothing downstream fired.
    row = _directive_row(directive_id)
    assert row["state"] == "pending"
    assert row["claimed_by"] is None
    assert row["claimed_executor"] is None
    assert row["finished_at"] is None
    assert int(row["lease_generation"]) == 0
    assert _count("SELECT COUNT(*) FROM handoff_outbox WHERE directive_id = ?", (directive_id,)) == 0
    assert _count("SELECT COUNT(*) FROM handoff_records WHERE directive_id = ?", (directive_id,)) == 0
    assert _pending_states(lite_client, session_id, _headers()) == ["pending"]

    rejections = _audit_rows("directive_report_rejected")
    assert len(rejections) == 1
    assert rejections[0]["query"]["directive_id"] == directive_id
    assert rejections[0]["query"]["reason"] == "not_claimed"
    assert rejections[0]["query"]["requested_state"] == "succeeded"
    assert _audit_rows("directive_succeeded") == []

    # failed / blocked from PENDING are equally not reports of claimed work.
    _rejection(_report_raw(lite_client, session_id, directive_id, "failed", result="failure", failure_reason="never ran"), "not_claimed")
    _rejection(_report_raw(lite_client, session_id, directive_id, "blocked", result="blocked", failure_reason="never ran"), "not_claimed")
    assert _directive_row(directive_id)["state"] == "pending"


def test_unclaimed_report_reason_wins_over_invalid_permit(lite_client: TestClient) -> None:
    """Case 1 with a permit that is ALSO invalid: the validator's ownership reason must still win."""
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    permit_id = _directive_row(directive_id)["permit_id"]
    assert permit_id, "permit-required directive must carry its attached permit"

    expired = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    conn = _db()
    try:
        conn.execute("UPDATE execution_permits SET expires_at = ? WHERE id = ?", (expired, str(permit_id)))
        conn.commit()
    finally:
        conn.close()

    rejected = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers())
    detail = _rejection(rejected, "not_claimed")
    assert not detail["reason"].startswith("permit_invalid"), detail
    assert _directive_row(directive_id)["state"] == "pending"
    audit = [row for row in _audit_rows("directive_report_rejected") if row["query"].get("directive_id") == directive_id]
    assert audit and audit[-1]["query"]["reason"] == "not_claimed"

    # Positive control: the permit really is invalid now -- the claim path reports it as such,
    # because there the validator itself rejects on the permit (no ownership reason to mask).
    claim = _claim_raw(lite_client, session_id, directive_id, _headers())
    assert claim.status_code == 409, claim.text
    claim_detail = claim.json()["detail"]
    assert isinstance(claim_detail, dict) and str(claim_detail["reason"]).startswith("permit_invalid"), claim_detail
    assert _directive_row(directive_id)["state"] == "pending"


# --------------------------------------------------------------------------- case 2


def test_second_executor_cannot_claim_or_report(lite_client: TestClient) -> None:
    worker_a = _headers("worker-a", user="shared-user")
    worker_b = _headers("worker-b", user="shared-user")
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, worker_a)

    claimed = _claim(lite_client, session_id, directive_id, worker_a)
    assert claimed["lease_generation"] == 1
    assert claimed["claimed_executor"] == "worker-a"

    # Same user, different authenticated executor: the lease is bound to auth.consumer.
    second = _claim_raw(lite_client, session_id, directive_id, worker_b)
    detail = _rejection(second, "claimed_by_other")
    assert detail["claimed_executor"] == "worker-a"
    assert detail["lease_generation"] == 1
    assert _audit_rows("directive_claim_rejected")[-1]["query"]["actor"] == "worker-b"

    loser_report = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=worker_b, lease=1)
    _rejection(loser_report, "claimed_by_other")
    assert _directive_row(directive_id)["state"] == "in_progress"

    # Re-claim by the holder is an idempotent no-op (lease unchanged).
    reclaimed = _claim(lite_client, session_id, directive_id, worker_a)
    assert reclaimed["lease_generation"] == 1

    winner_report = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=worker_a, lease=1)
    assert winner_report.status_code == 200, winner_report.text
    assert winner_report.json()["state"] == "succeeded"
    assert winner_report.json()["lease_generation"] == 1
    assert winner_report.json()["verification_state"] == "unverified"


def test_fenced_claim_update_admits_exactly_one_worker(lite_client: TestClient) -> None:
    """Raw race against the fenced claim UPDATE: two connections, expected_lease=0, one winner."""
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    db_path = get_settings().lite_db_path
    now = datetime.now(tz=UTC)
    claim_expires = (now + timedelta(seconds=300)).isoformat()
    fenced_update = """
        UPDATE directive_executions
        SET state = 'in_progress', claimed_by = ?, claimed_executor = ?, lease_generation = ?, lease_expires_at = ?,
            started_at = COALESCE(started_at, ?), expires_at = ?, updated_at = ?
        WHERE directive_id = ? AND workspace_id = ? AND user_id = ? AND state = 'pending' AND lease_generation = ?
    """

    def _params(executor: str) -> tuple[Any, ...]:
        return (executor, executor, 1, claim_expires, now.isoformat(), claim_expires, now.isoformat(), directive_id, "personal", "worker-a", 0)

    conn_a = sqlite3.connect(db_path, timeout=0.2)
    conn_b = sqlite3.connect(db_path, timeout=0.2)
    winners: list[str] = []
    try:
        conn_a.execute("BEGIN IMMEDIATE")
        if conn_a.execute(fenced_update, _params("worker-a")).rowcount == 1:
            winners.append("worker-a")
        # B races while A's fenced write is uncommitted: it must either be locked out or fence to 0 rows.
        try:
            conn_b.execute("BEGIN IMMEDIATE")
            if conn_b.execute(fenced_update, _params("worker-b")).rowcount == 1:
                winners.append("worker-b")
            conn_b.commit()
        except sqlite3.OperationalError:
            conn_b.rollback()
        conn_a.commit()
        # B retries after A commits: the lease moved, the fence must hold.
        if conn_b.execute(fenced_update, _params("worker-b")).rowcount == 1:
            winners.append("worker-b")
        conn_b.commit()
    finally:
        conn_a.close()
        conn_b.close()

    assert winners == ["worker-a"]
    row = _directive_row(directive_id)
    assert row["state"] == "in_progress"
    assert row["claimed_executor"] == "worker-a"
    assert int(row["lease_generation"]) == 1


# --------------------------------------------------------------------------- case 3


def test_stale_worker_after_lease_replacement_is_rejected_and_audited(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    assert _claim(lite_client, session_id, directive_id, _headers())["lease_generation"] == 1

    # Simulated replacement: a newer owner bumped the lease generation.
    conn = _db()
    try:
        conn.execute("UPDATE directive_executions SET lease_generation = 2 WHERE directive_id = ?", (directive_id,))
        conn.commit()
    finally:
        conn.close()

    late_details = {"evidence": "late-evidence", "files_modified": ["docs/notes.md"]}
    stale = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers(), lease=1, details=late_details)
    detail = _rejection(stale, "stale_lease")
    assert detail["lease_generation"] == 2
    assert _directive_row(directive_id)["state"] == "in_progress"
    assert _count("SELECT COUNT(*) FROM handoff_outbox WHERE directive_id = ?", (directive_id,)) == 0

    # Late evidence is retained on the audit row, never applied to the directive.
    rejections = [row for row in _audit_rows("directive_report_rejected") if row["query"].get("directive_id") == directive_id]
    assert len(rejections) == 1
    audit_query = rejections[0]["query"]
    assert audit_query["reason"] == "stale_lease"
    assert audit_query["presented_lease"] == 1
    assert audit_query["current_lease"] == 2
    assert audit_query["late_payload"]["details"] == late_details
    assert audit_query["late_payload"]["state"] == "succeeded"

    current = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers(), lease=2)
    assert current.status_code == 200, current.text
    assert current.json()["state"] == "succeeded"
    assert current.json()["lease_generation"] == 2


def test_reaped_directive_rejects_late_report_as_terminal(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    _claim(lite_client, session_id, directive_id, _headers())

    aged = (datetime.now(tz=UTC) - timedelta(seconds=1000)).isoformat()
    conn = _db()
    try:
        conn.execute("UPDATE directive_executions SET started_at = ?, updated_at = ? WHERE directive_id = ?", (aged, aged, directive_id))
        conn.commit()
    finally:
        conn.close()

    # A later turn runs the reaper; the stale in_progress directive is abandoned under the system actor.
    _step(lite_client, session_id, _headers(), message="status?", task=None)
    row = _directive_row(directive_id)
    assert row["state"] == "abandoned"
    assert int(row["lease_generation"]) == 2, "reaping an in_progress directive must bump the lease so the stale worker is fenced"
    reaped = _audit_rows("directive_reaped")
    assert any(item["query"].get("directive_id") == directive_id for item in reaped)

    late = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers(), lease=1, details={"evidence": "after-reap"})
    detail = _rejection(late, "terminal")
    assert detail["state"] == "abandoned"
    assert _directive_row(directive_id)["state"] == "abandoned"
    rejections = [item for item in _audit_rows("directive_report_rejected") if item["query"].get("directive_id") == directive_id]
    assert rejections and rejections[-1]["query"]["late_payload"]["details"] == {"evidence": "after-reap"}


def _reap_in_progress_directive(client: TestClient, session_id: str, headers: dict[str, str]) -> str:
    """Mint + claim, age the row past the stale timeout, and let the next turn's reaper abandon it."""
    directive_id = _mint_pending_directive(client, session_id, headers)
    _claim(client, session_id, directive_id, headers)
    aged = (datetime.now(tz=UTC) - timedelta(seconds=1000)).isoformat()
    conn = _db()
    try:
        conn.execute("UPDATE directive_executions SET started_at = ?, updated_at = ? WHERE directive_id = ?", (aged, aged, directive_id))
        conn.commit()
    finally:
        conn.close()
    _step(client, session_id, headers, message="status?", task=None)
    row = _directive_row(directive_id)
    assert row["state"] == "abandoned", dict(row)
    return directive_id


def test_reaped_directive_late_report_never_replays_receipt(lite_client: TestClient) -> None:
    """A reaped row has no recorded receipt: every late report shape is 409 'terminal' + audited, never a 200 replay.

    Full regressed to `{"idempotent_replay": true}` with an empty receipt for the legacy (no-key) shape and
    silently dropped the late payload; the contract is the Lite behaviour.
    """
    session_id = _session()
    directive_id = _reap_in_progress_directive(lite_client, session_id, _headers())
    assert json.loads(str(_directive_row(directive_id)["meta"] or "{}")).get("report_result") is None

    shapes: list[tuple[str, dict[str, Any]]] = [
        ("legacy-no-lease-no-key", {}),
        ("stale-lease-no-key", {"lease": 1}),
        ("keyed", {"lease": 1, "idempotency_key": "late-after-reap"}),
    ]
    for label, kwargs in shapes:
        late = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers(), details={"evidence": label}, **kwargs)
        assert late.status_code == 409, f"{label}: {late.text}"
        body = late.json()
        assert "idempotent_replay" not in body, f"{label}: a reaped row must not replay a receipt: {body}"
        assert body["detail"]["reason"] == "terminal", f"{label}: {body}"
        assert body["detail"]["state"] == "abandoned"
        assert body["detail"]["directive_id"] == directive_id

    row = _directive_row(directive_id)
    assert row["state"] == "abandoned"
    assert row["report_idempotency_key"] is None, "a rejected late report must not record its idempotency key"
    assert json.loads(str(row["meta"] or "{}")).get("report_result") is None
    assert _count("SELECT COUNT(*) FROM handoff_outbox WHERE directive_id = ?", (directive_id,)) == 0

    audited = [item for item in _audit_rows("directive_report_rejected") if item["query"].get("directive_id") == directive_id]
    assert [item["query"]["late_payload"]["details"]["evidence"] for item in audited] == [label for label, _ in shapes]
    assert all(item["query"]["reason"] == "terminal" for item in audited)


# --------------------------------------------------------------------------- case 4


def test_idempotent_retry_replays_and_changed_payload_conflicts(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    lease = int(_claim(lite_client, session_id, directive_id, _headers())["lease_generation"])

    first = _report_raw(lite_client, session_id, directive_id, "failed", headers=_headers(), lease=lease, idempotency_key="k1", result="failure", failure_reason="tool crashed")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["state"] == "failed"
    assert first_body.get("idempotent_replay") is not True
    pending_after_first = _pending_states(lite_client, session_id, _headers())

    replay = _report_raw(lite_client, session_id, directive_id, "failed", headers=_headers(), lease=lease, idempotency_key="k1", result="failure", failure_reason="tool crashed")
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["state"] == "failed"
    assert replay.json().get("retry_directive_id") == first_body.get("retry_directive_id")
    assert _pending_states(lite_client, session_id, _headers()) == pending_after_first

    conflict = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers(), lease=lease, idempotency_key="k1")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["reason"] == "idempotency_conflict"

    other_key = _report_raw(lite_client, session_id, directive_id, "failed", headers=_headers(), lease=lease, idempotency_key="k2", result="failure", failure_reason="tool crashed")
    assert other_key.status_code == 409, other_key.text
    assert other_key.json()["detail"]["reason"] == "idempotency_key_mismatch"

    legacy = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers())
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["idempotent_replay"] is True
    assert legacy.json()["state"] == "failed"

    assert _directive_row(directive_id)["state"] == "failed"
    assert _count("SELECT COUNT(*) FROM handoff_outbox WHERE directive_id = ?", (directive_id,)) == 1


def test_pending_directive_can_be_cancelled_without_claim(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())

    cancelled = _report_raw(lite_client, session_id, directive_id, "cancelled", headers=_headers(), result="cancelled", cancel_reason="not applicable")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["state"] == "cancelled"
    assert body["retry_scheduled"] is False
    assert body.get("retry_directive_id") is None

    row = _directive_row(directive_id)
    assert row["state"] == "cancelled"
    assert row["cancelled_at"] is not None
    assert row["cancel_reason"] == "not applicable"
    assert row["claimed_executor"] is None, "cancelling an unclaimed directive must not invent a claim"
    assert _count("SELECT COUNT(*) FROM handoff_outbox WHERE directive_id = ?", (directive_id,)) == 0
    assert _pending_states(lite_client, session_id, _headers()) == []
    assert any(item["query"].get("directive_id") == directive_id for item in _audit_rows("directive_cancelled"))

    # Terminal: a later success report is a different payload against the recorded cancellation
    # receipt (spec 4.9 terminal guard) -> conflict, never a resurrection.
    late = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers(), idempotency_key="after-cancel")
    assert late.status_code == 409, late.text
    assert late.json()["detail"]["reason"] == "idempotency_conflict"
    assert late.json()["detail"]["state"] == "cancelled"
    assert _directive_row(directive_id)["state"] == "cancelled"
    # Legacy no-key late report replays the recorded cancellation.
    legacy = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers())
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["idempotent_replay"] is True
    assert legacy.json()["state"] == "cancelled"


def test_pending_directive_can_be_rejected_and_in_progress_cannot(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())

    rejected = _report_raw(lite_client, session_id, directive_id, "rejected", headers=_headers(), result="rejected", cancel_reason="out of scope")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected"
    assert _directive_row(directive_id)["state"] == "rejected"
    assert any(item["query"].get("directive_id") == directive_id for item in _audit_rows("directive_rejected"))

    other_session = _session()
    other_id = _mint_pending_directive(lite_client, other_session, _headers())
    _claim(lite_client, other_session, other_id, _headers())
    invalid = _report_raw(lite_client, other_session, other_id, "rejected", headers=_headers(), lease=1, result="rejected")
    _rejection(invalid, "invalid_transition")
    assert _directive_row(other_id)["state"] == "in_progress"


def test_report_cannot_set_verified_complete(lite_client: TestClient) -> None:
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    lease = int(_claim(lite_client, session_id, directive_id, _headers())["lease_generation"])

    report = _report_raw(
        lite_client,
        session_id,
        directive_id,
        "succeeded",
        headers=_headers(),
        lease=lease,
        extra={"verification_state": "verified_complete", "verified": True},
    )
    assert report.status_code == 200, report.text
    assert report.json()["state"] == "succeeded"
    assert report.json()["verification_state"] == "unverified"
    assert _directive_row(directive_id)["verification_state"] == "unverified"


def test_legacy_report_without_lease_is_accepted_only_when_not_strict(lite_client: TestClient) -> None:
    settings = get_settings()
    session_id = _session()
    directive_id = _mint_pending_directive(lite_client, session_id, _headers())
    _claim(lite_client, session_id, directive_id, _headers())

    settings.takeover_lease_strict = True
    try:
        strict = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers())
        _rejection(strict, "lease_required")
        assert _directive_row(directive_id)["state"] == "in_progress"
    finally:
        settings.takeover_lease_strict = False

    legacy = _report_raw(lite_client, session_id, directive_id, "succeeded", headers=_headers())
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["state"] == "succeeded"


# --------------------------------------------------------------------------- case 5


def test_codex_completes_claude_resumes_from_new_session_and_reverse(bound_client: TestClient) -> None:
    codex, claude = _bearer("codex-token"), _bearer("claude-token")
    key = f"tb:c5:{uuid.uuid4().hex[:8]}"
    completion = bound_client.post("/v1/completions", headers=codex, json=_completion_body("codex-a", key, title="wire lease fencing into the report path", app_context=_BOUND_APP_CONTEXT))
    assert completion.status_code == 200, completion.text
    assert completion.json()["delivery_status"] == "delivered"
    assert completion.json()["project_binding"] == "bound"

    # Reader session != source session: the packet must still be found.
    resumed = _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b")
    assert resumed.status_code == 200, resumed.text
    packet = resumed.json()
    assert packet["source_session_id"] == "codex-a"
    assert packet["source_owner_id"] == "codex-executor"
    assert packet["cross_user_scope_applied"] is True
    assert packet["anchor_freshness"] == "unknown"
    assert packet["record_ts"]
    assert "project_binding" in packet
    assert packet["files"][0]["path"] == "services/tce_api/tce_api/main.py"
    assert packet["files"][0]["anchors"][0]["stale"] is None

    stale = _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b", current_git={"commit": "deadbeef0000"})
    assert stale.status_code == 200, stale.text
    assert stale.json()["anchor_freshness"] == "stale"
    assert "commit_mismatch" in stale.json()["freshness_reasons"]
    assert stale.json()["files"][0]["anchors"][0]["stale"] is True

    current = _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b", current_git={"commit": "abc123def456"})
    assert current.status_code == 200, current.text
    assert current.json()["anchor_freshness"] == "current"
    assert current.json()["files"][0]["anchors"][0]["stale"] is False

    explicit_source = _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b", source_session_id="codex-a")
    assert explicit_source.status_code == 200, explicit_source.text
    assert explicit_source.json()["source_session_id"] == "codex-a"
    assert _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b", source_session_id="no-such-session").status_code == 404

    # Legacy behaviour survives only behind the explicit flag: reader session equality finds nothing.
    assert _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b", legacy_session_scope=True).status_code == 404
    legacy_same = _resume(bound_client, claude, target_owner="codex-executor", session_id="codex-a", legacy_session_scope=True)
    assert legacy_same.status_code == 200, legacy_same.text
    assert legacy_same.json()["source_session_id"] == "codex-a"

    # The resume attempt records reader and source sessions separately.
    conn = _db()
    try:
        attempt = conn.execute("SELECT session_id, source_session_id FROM continuity_resume_attempts WHERE packet_id = ?", (packet["packet_id"],)).fetchone()
    finally:
        conn.close()
    assert attempt is not None
    assert attempt["session_id"] == "claude-b"
    assert attempt["source_session_id"] == "codex-a"

    # Reverse: Claude completes in claude-a, Codex resumes from codex-b.
    reverse_key = f"tb:c5r:{uuid.uuid4().hex[:8]}"
    reverse_body = _completion_body("claude-a", reverse_key, title="verify lease fencing under load", commit="feedface1234", app_context=_BOUND_APP_CONTEXT)
    reverse = bound_client.post("/v1/completions", headers=claude, json=reverse_body)
    assert reverse.status_code == 200, reverse.text
    codex_resume = _resume(bound_client, codex, query="verify lease fencing under load", target_owner="claude-executor", session_id="codex-b", current_git={"commit": "feedface1234"})
    assert codex_resume.status_code == 200, codex_resume.text
    assert codex_resume.json()["source_session_id"] == "claude-a"
    assert codex_resume.json()["source_owner_id"] == "claude-executor"
    assert codex_resume.json()["anchor_freshness"] == "current"


def test_resume_without_continuity_intent_stays_within_own_owner(bound_client: TestClient) -> None:
    codex, claude = _bearer("codex-token"), _bearer("claude-token")
    key = f"tb:c5s:{uuid.uuid4().hex[:8]}"
    seed = bound_client.post("/v1/completions", headers=codex, json=_completion_body("codex-a", key, title="wire lease fencing into the report path", app_context=_BOUND_APP_CONTEXT))
    assert seed.status_code == 200, seed.text

    # target_owner without include_cross_user is not continuity intent: no silent broadening.
    own_only = _resume(bound_client, claude, target_owner="codex-executor", session_id="claude-b", include_cross_user=False)
    assert own_only.status_code == 404, own_only.text
    no_target = _resume(bound_client, claude, session_id="claude-b")
    assert no_target.status_code == 404, no_target.text


# --------------------------------------------------------------------------- case 6


def test_same_git_remote_in_two_workspaces_does_not_leak(bound_client: TestClient) -> None:
    codex, other = _bearer("codex-token"), _bearer("other-token")
    shared_context = {**_BOUND_APP_CONTEXT, "project_remote": "git@github.com:acme/open-timeline-engine.git"}
    for headers, session_id in ((codex, "codex-a"), (other, "other-a")):
        preload = bound_client.post("/v1/takeover/preload", headers=headers, json={"session_id": session_id, "task": "wire lease fencing", "app_context": shared_context})
        assert preload.status_code == 200, preload.text

    shared_completion = bound_client.post("/v1/completions", headers=codex, json=_completion_body("codex-a", "tb:c6:shared", title="wire lease fencing shared workspace", app_context=shared_context))
    other_completion = bound_client.post("/v1/completions", headers=other, json=_completion_body("other-a", "tb:c6:other", title="wire lease fencing other workspace", app_context=shared_context))
    assert shared_completion.status_code == 200, shared_completion.text
    assert other_completion.status_code == 200, other_completion.text
    shared_event_id = str(shared_completion.json()["event_id"])
    other_event_id = str(other_completion.json()["event_id"])

    conn = _db()
    try:
        project_ids = {str(row[0]) for row in conn.execute("SELECT project_id FROM handoff_records WHERE project_id IS NOT NULL").fetchall()}
    finally:
        conn.close()
    assert len(project_ids) == 1, "same remote must resolve to the same project id in both workspaces"

    # Resume from the other workspace, even naming the shared owner, only ever sees its own
    # workspace. Naming a peer with nothing visible here yields NOTHING — never a silent
    # substitute of the reader's own record, which is the exact failure seen live when
    # Claude asked for codex-executor and was handed its own earlier session.
    other_resume = _resume(bound_client, other, target_owner="codex-executor", session_id="other-b")
    assert other_resume.status_code == 404, other_resume.text
    # Without an explicit target the reader's own work is still resumable, and only its own.
    own_resume = _resume(bound_client, other, include_cross_user=False, session_id="other-b")
    assert own_resume.status_code == 200, own_resume.text
    assert own_resume.json()["source_owner_id"] == "other-executor"
    assert own_resume.json()["source_session_id"] == "other-a"
    assert own_resume.json()["retrieval_meta"]["candidate_count"] == 1
    assert _resume(bound_client, other, target_owner="codex-executor", session_id="other-b", source_session_id="codex-a").status_code == 404

    # Search: neither workspace's events show up in the other, with or without explicit continuity intent.
    other_ids, _ = _search_ids(bound_client, other, "wire lease fencing")
    assert shared_event_id not in other_ids
    other_ids_intent, _ = _search_ids(bound_client, other, "wire lease fencing", continuity_intent=True, target_owner="codex-executor")
    assert shared_event_id not in other_ids_intent
    shared_ids, _ = _search_ids(bound_client, codex, "wire lease fencing", continuity_intent=True, target_owner="other-executor")
    assert other_event_id not in shared_ids
    assert bound_client.get(f"/v1/events/{shared_event_id}", headers=other).status_code == 403

    # Bundles are scoped the same way.
    bundle = bound_client.post("/v1/context_bundle", headers=other, json={"task": "wire lease fencing", "app_context": shared_context, "constraints": {"k": 8}})
    assert bundle.status_code == 200, bundle.text
    assert shared_event_id not in {str(item) for item in bundle.json().get("citations", [])}


# --------------------------------------------------------------------------- case 7


def test_search_misses_owner_scope_stays_empty(lite_client: TestClient) -> None:
    alice = _headers("alice-cli", user="alice", role="user")
    bob = _headers("bob-cli", user="bob", role="user")
    title = f"owner scoped needle {uuid.uuid4().hex[:6]}"
    ingest = lite_client.post("/v1/events", json=_event_payload(title), headers=alice)
    assert ingest.status_code == 200, ingest.text
    event_id = str(ingest.json()["event_id"])
    # Bob is a legitimate member of the same workspace: membership is not owner scope.
    membership = lite_client.post("/v1/team/memberships", json={"user_id": "bob", "role": "member", "active": True}, headers=alice)
    assert membership.status_code == 200, membership.text

    own_ids, _ = _search_ids(lite_client, alice, title)
    assert event_id in own_ids

    bob_ids, retrieval = _search_ids(lite_client, bob, title)
    assert bob_ids == set()
    assert retrieval["cross_user_scope_applied"] is False
    assert retrieval["cross_user_scope_owners"] in ([], ["bob"])

    # A phrasing that used to trigger regex-based peer expansion no longer does. Bob's own
    # api-auto-capture event from his previous search legitimately contains the needle, so the
    # owner-scope assertion is "alice's event never appears", not "bob sees nothing at all".
    bob_ids_phrased, retrieval_phrased = _search_ids(lite_client, bob, f"what did alice discuss about {title}")
    assert event_id not in bob_ids_phrased
    assert retrieval_phrased["cross_user_scope_applied"] is False

    # Explicit continuity intent is the only path that widens the owner scope.
    intent_ids, intent_retrieval = _search_ids(lite_client, bob, title, continuity_intent=True, target_owner="alice")
    assert event_id in intent_ids
    assert intent_retrieval["cross_user_scope_applied"] is True
    assert "alice" in intent_retrieval["cross_user_scope_owners"]


# --------------------------------------------------------------------------- case 8


def test_completion_without_takeover_reports_project_binding(lite_client: TestClient) -> None:
    executor = _headers("core-executor", user="core-user")
    unbound = lite_client.post("/v1/completions", headers=executor, json=_completion_body("core-unbound", "tb:c8:unbound", title="core completion without project"))
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["project_binding"] == "unbound"

    bound = lite_client.post("/v1/completions", headers=executor, json=_completion_body("core-bound", "tb:c8:bound", title="core completion with project", app_context=_BOUND_APP_CONTEXT))
    assert bound.status_code == 200, bound.text
    assert bound.json()["project_binding"] == "bound"

    conn = _db()
    try:
        unbound_row = conn.execute("SELECT project_id, executor_id FROM handoff_records WHERE id = ?", (str(unbound.json()["handoff_record_id"]),)).fetchone()
        bound_row = conn.execute("SELECT project_id, executor_id FROM handoff_records WHERE id = ?", (str(bound.json()["handoff_record_id"]),)).fetchone()
    finally:
        conn.close()
    assert unbound_row is not None and bound_row is not None
    assert unbound_row["project_id"] is None
    assert str(bound_row["project_id"]).startswith("proj_")
    assert bound_row["executor_id"] == "core-executor"


def test_mutating_step_without_project_is_visibly_unbound(lite_client: TestClient) -> None:
    session_id = _session()
    _request_permit(lite_client, session_id, _headers())
    body = _step(lite_client, session_id, _headers(), app_context=_UNBOUND_APP_CONTEXT)
    assert body["project_binding"] == "unbound"
    assert body["directive_id"] is None
    assert body["safety_decision"] == "confirm_required"
    assert body["final_response"]
    assert _count("SELECT COUNT(*) FROM directive_executions WHERE session_id = ?", (session_id,)) == 0
    assert body["state"]["takeover_context"]["project_binding"] == "unbound"

    # Positive control: the same mutating task with a bound project mints the directive.
    bound_session = _session()
    _request_permit(lite_client, bound_session, _headers())
    bound = _step(lite_client, bound_session, _headers())
    assert bound["project_binding"] == "bound"
    assert bound["directive_id"]
    assert bound["state"]["takeover_context"]["project_binding"] == "bound"


def test_completion_conflict_on_changed_payload_same_key(lite_client: TestClient) -> None:
    executor = _headers("core-executor", user="core-user")
    key = f"tb:c4b:{uuid.uuid4().hex[:8]}"
    first = lite_client.post("/v1/completions", headers=executor, json=_completion_body("core-a", key, title="capture once", decision="original decision"))
    assert first.status_code == 200, first.text

    identical = lite_client.post("/v1/completions", headers=executor, json=_completion_body("core-a", key, title="capture once", decision="original decision"))
    assert identical.status_code == 200, identical.text
    assert identical.json()["outbox_id"] == first.json()["outbox_id"]

    changed = lite_client.post("/v1/completions", headers=executor, json=_completion_body("core-a", key, title="capture once", decision="a different decision"))
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["reason"] == "idempotency_conflict"
    assert changed.json()["detail"]["completion_key"] == key
    assert _count("SELECT COUNT(*) FROM handoff_outbox WHERE completion_key = ?", (key,)) == 1
    assert _count("SELECT COUNT(*) FROM handoff_records WHERE id = ?", (str(first.json()["handoff_record_id"]),)) == 1
    assert any(item["query"].get("completion_key") == key for item in _audit_rows("completion_conflict"))


# --------------------------------------------------------------------------- review regressions
#
# Each test below pins a blocker/major from the P0 review that the matrix above did not cover:
#   - confirm_required permits are resolved by operators (USER role), never by any executor identity
#   - /v1/takeover/feedback never mints human-confirmed evidence from executor-supplied correction_text
#   - open goal discovery is owner-scoped (no other owner's event titles become this user's goals)
#   - a body-supplied '<other-workspace>:takeover' domain never reads that workspace's learned patterns


def _permit_row(permit_id: str) -> sqlite3.Row:
    conn = _db()
    try:
        row: sqlite3.Row | None = conn.execute("SELECT * FROM execution_permits WHERE id = ?", (permit_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"permit {permit_id} missing"
    return row


def _resolve_permit(client: TestClient, permit_id: str, headers: dict[str, str], *, approved: bool = True) -> httpx.Response:
    response: httpx.Response = client.post("/v1/takeover/permit/resolve", json={"permit_id": permit_id, "approved": approved}, headers=headers)
    return response


def test_confirm_required_permit_is_resolved_only_by_operator_role(lite_client: TestClient) -> None:
    requester = _headers("codex-executor", user="shared-user")
    other_executor = _headers("claude-executor", user="shared-user")
    operator = _headers("operator-ui", user="shared-user", role="user")
    session_id = _session()

    # A sensitive target path yields confirm_required regardless of the autonomy profile.
    permit = lite_client.post(
        "/v1/takeover/permit",
        json={"session_id": session_id, "action_kind": "edit", "target_paths": ["infra/docker-compose.yml"], "estimated_change_size": 3},
        headers=requester,
    )
    assert permit.status_code == 200, permit.text
    assert permit.json()["decision"] == "confirm_required", permit.json()
    permit_id = str(permit.json()["permit_id"])

    # The requesting executor cannot self-approve ...
    assert _resolve_permit(lite_client, permit_id, requester).status_code == 403
    # ... and neither can any OTHER executor identity: the gate is the role, not the identity match.
    foreign = _resolve_permit(lite_client, permit_id, other_executor)
    assert foreign.status_code == 403, foreign.text
    row = _permit_row(permit_id)
    assert row["decision"] == "confirm_required"
    assert row["resolved_at"] is None
    assert row["resolved_by"] is None

    # Only an operator (USER role) flips it.
    approved = _resolve_permit(lite_client, permit_id, operator)
    assert approved.status_code == 200, approved.text
    assert approved.json()["decision"] == "allow"
    row = _permit_row(permit_id)
    assert row["decision"] == "allow"
    assert row["resolved_at"] is not None
    assert row["resolved_by"] == "operator-ui"

    # Once resolved the permit is returned unchanged to anyone (idempotent), never re-flipped.
    again = _resolve_permit(lite_client, permit_id, other_executor, approved=False)
    assert again.status_code == 200, again.text
    assert again.json()["decision"] == "allow"
    assert _permit_row(permit_id)["decision"] == "allow"


def _observations_for_choice(selected_choice: str) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT consumer_id, evidence_source, confirmed_at, selected_choice, learning_eligible, storage_decision FROM decision_observations WHERE selected_choice = ?",
            (selected_choice,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _feedback(client: TestClient, session_id: str, headers: dict[str, str], correction_text: str) -> None:
    response = client.post(
        "/v1/takeover/feedback",
        json={
            "session_id": session_id,
            "turn": 1,
            "action_kind": "edit",
            "result": "failure",
            "situation_type": "choice_required",
            "correction_text": correction_text,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_takeover_feedback_never_mints_human_confirmed_evidence_from_executors(lite_client: TestClient) -> None:
    executor = _headers("worker-a", user="shared-user")
    operator = _headers("operator-ui", user="shared-user", role="user")
    session_id = _session()

    executor_text = f"executor asserted correction {uuid.uuid4().hex[:6]}"
    _feedback(lite_client, session_id, executor, executor_text)
    executor_rows = _observations_for_choice(executor_text)
    assert len(executor_rows) == 1, executor_rows
    assert executor_rows[0]["confirmed_at"] is None, "confirmation is never minted from an executor-supplied correction"
    assert executor_rows[0]["evidence_source"] not in {"explicit", "correction"}, executor_rows[0]

    # A human operator may label the correction as human-origin evidence; confirmation still needs a verified source path.
    human_text = f"operator correction {uuid.uuid4().hex[:6]}"
    _feedback(lite_client, session_id, operator, human_text)
    human_rows = _observations_for_choice(human_text)
    assert len(human_rows) == 1, human_rows
    assert human_rows[0]["evidence_source"] == "explicit"
    assert human_rows[0]["confirmed_at"] is None


def test_takeover_feedback_executor_correction_is_review_gated_not_learned(lite_client: TestClient) -> None:
    """Executor correction_text is stored as inferred evidence that never feeds learning without human review."""
    executor = _headers("worker-a", user="shared-user")
    operator = _headers("operator-ui", user="shared-user", role="user")
    session_id = _session()

    executor_text = f"executor correction {uuid.uuid4().hex[:6]}"
    _feedback(lite_client, session_id, executor, executor_text)
    executor_rows = _observations_for_choice(executor_text)
    assert len(executor_rows) == 1, executor_rows
    assert executor_rows[0]["evidence_source"] == "inferred", executor_rows[0]
    assert executor_rows[0]["learning_eligible"] == 0, "an executor-supplied correction is never learning-eligible without review"
    assert executor_rows[0]["storage_decision"] == "pending_review", executor_rows[0]
    assert executor_rows[0]["confirmed_at"] is None

    # A header-asserted operator (X-TCE-Role: user on the same unbound bearer) is authored as human-origin
    # 'explicit' evidence but is held for review all the same: the role header is an assertion, not an identity.
    asserted_text = f"operator correction {uuid.uuid4().hex[:6]}"
    _feedback(lite_client, session_id, operator, asserted_text)
    asserted_rows = _observations_for_choice(asserted_text)
    assert len(asserted_rows) == 1, asserted_rows
    assert asserted_rows[0]["evidence_source"] == "explicit"
    assert asserted_rows[0]["learning_eligible"] == 0, asserted_rows[0]
    assert asserted_rows[0]["storage_decision"] == "pending_review", asserted_rows[0]
    assert asserted_rows[0]["confirmed_at"] is None

    # The server-bound human is the control: explicit, learning-eligible, still unconfirmed.
    human_text = f"verified operator correction {uuid.uuid4().hex[:6]}"
    _feedback(lite_client, session_id, _verified_operator_headers(), human_text)
    human_rows = _observations_for_choice(human_text)
    assert len(human_rows) == 1, human_rows
    assert human_rows[0]["evidence_source"] == "explicit"
    assert human_rows[0]["learning_eligible"] == 1, human_rows[0]
    assert human_rows[0]["storage_decision"] == "learn", human_rows[0]
    assert human_rows[0]["confirmed_at"] is None


def _record_evidence(
    client: TestClient,
    headers: dict[str, str],
    selected_choice: str,
    *,
    evidence_source: str = "explicit",
    supersedes: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "situation_type": "choice_required",
        "situation_summary": "pick a storage backend",
        "objective": "keep the prototype local",
        "available_choices": ["SQLite", "Postgres"],
        "selected_choice": selected_choice,
        "rationale": "no external infrastructure needed",
        "action_taken": "wired the SQLite store",
        "outcome": "success",
        "evidence_source": evidence_source,
        "confidence": 1.0,
    }
    if evidence_source == "correction":
        # The request model requires both for correction evidence.
        payload["correction_text"] = "prefer SQLite here"
        payload["supersedes_observation_id"] = supersedes
    response = client.post("/v1/behavior/evidence", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_behavior_evidence_from_executor_is_inferred_and_review_gated(lite_client: TestClient) -> None:
    """An executor cannot mint human-origin evidence: explicit/correction is downgraded to inferred and review-gated."""
    executor = _headers("worker-a", user="shared-user")
    prior_id: str | None = None
    for claimed_source in ("explicit", "correction"):
        choice = f"executor claimed {claimed_source} {uuid.uuid4().hex[:6]}"
        body = _record_evidence(lite_client, executor, choice, evidence_source=claimed_source, supersedes=prior_id)
        prior_id = str(body["observation_id"])
        assert body["stored"] is True, body
        assert body["learning_eligible"] is False, body
        assert body["storage_decision"] == "pending_review", body
        rows = _observations_for_choice(choice)
        assert len(rows) == 1, rows
        assert rows[0]["evidence_source"] == "inferred", rows[0]
        assert rows[0]["learning_eligible"] == 0, rows[0]
        assert rows[0]["storage_decision"] == "pending_review", rows[0]
        assert rows[0]["confirmed_at"] is None


def test_behavior_evidence_learning_needs_a_server_established_human(lite_client: TestClient) -> None:
    """Explicit, learning-eligible evidence needs a human the SERVER identified, not one a header claims.

    In compat mode ``X-TCE-Role: user`` rides on the executor's own bearer, so without this gate the
    executor mints its owner's preferences in one call and the system learns its own output.
    """
    asserted = _headers("operator-ui", user="shared-user", role="user")
    asserted_choice = f"asserted operator explicit {uuid.uuid4().hex[:6]}"
    asserted_body = _record_evidence(lite_client, asserted, asserted_choice)
    assert asserted_body["stored"] is True, asserted_body
    assert asserted_body["learning_eligible"] is False, asserted_body
    assert asserted_body["storage_decision"] == "pending_review", asserted_body
    assert "identity_unverified" in asserted_body["storage_reasons"], asserted_body
    asserted_rows = _observations_for_choice(asserted_choice)
    assert len(asserted_rows) == 1, asserted_rows
    assert asserted_rows[0]["evidence_source"] == "explicit", asserted_rows[0]
    assert asserted_rows[0]["learning_eligible"] == 0, asserted_rows[0]
    assert asserted_rows[0]["storage_decision"] == "pending_review", asserted_rows[0]
    assert asserted_rows[0]["confirmed_at"] is None

    # The server-bound human is the control: same payload, same role, learning-eligible.
    choice = f"operator explicit {uuid.uuid4().hex[:6]}"
    body = _record_evidence(lite_client, _verified_operator_headers(), choice)
    assert body["stored"] is True, body
    assert body["learning_eligible"] is True, body
    assert body["storage_decision"] == "learn", body
    rows = _observations_for_choice(choice)
    assert len(rows) == 1, rows
    assert rows[0]["evidence_source"] == "explicit", rows[0]
    assert rows[0]["learning_eligible"] == 1, rows[0]
    assert rows[0]["storage_decision"] == "learn", rows[0]
    # Confirmation still needs a verified source-event path, even for a human.
    assert rows[0]["confirmed_at"] is None


def _discover_goals(client: TestClient, session_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = client.post("/v1/takeover/goals/discover", json={"session_id": session_id, "include_open_discovery": True}, headers=headers)
    assert response.status_code == 200, response.text
    goals: list[dict[str, Any]] = response.json()["goals"]
    return goals


def test_open_discovery_never_surfaces_other_owners_events(lite_client: TestClient) -> None:
    alice = _headers("alice-cli", user="alice", role="user")
    bob = _headers("bob-cli", user="bob", role="user")
    needle = f"ownerneedle{uuid.uuid4().hex[:6]}"
    ingest = lite_client.post("/v1/events", json=_event_payload(f"fix the {needle} pipeline failure"), headers=alice)
    assert ingest.status_code == 200, ingest.text
    event_id = str(ingest.json()["event_id"])
    membership = lite_client.post("/v1/team/memberships", json={"user_id": "bob", "role": "member", "active": True}, headers=alice)
    assert membership.status_code == 200, membership.text

    # Positive control: the owner's own timeline feeds the owner's goals.
    alice_goals = _discover_goals(lite_client, _session(), alice)
    assert any(needle in str(goal["title"]).lower() or event_id in {str(item) for item in goal["evidence_event_ids"]} for goal in alice_goals), alice_goals

    # Same workspace member, different owner: alice's event must not become bob's goal (title, description or evidence).
    bob_goals = _discover_goals(lite_client, _session(), bob)
    for goal in bob_goals:
        assert needle not in str(goal["title"]).lower(), goal
        assert needle not in str(goal["description"]).lower(), goal
        assert event_id not in {str(item) for item in goal["evidence_event_ids"]}, goal


def _insert_pattern(domain: str, statement: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO patterns(id, domain, pattern_type, statement, evidence_event_ids, confidence, status, updated_at, version) VALUES(?, ?, 'procedure', ?, '[]', 0.95, 'active', ?, 1)",
            (str(uuid.uuid4()), domain, statement, datetime.now(tz=UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _bundle(client: TestClient, headers: dict[str, str], domain: str) -> httpx.Response:
    response: httpx.Response = client.post(
        "/v1/context_bundle",
        json={"task": "wire lease fencing", "app_context": {"domain": domain}, "constraints": {"k": 4}},
        headers=headers,
    )
    return response


def test_context_bundle_ignores_foreign_takeover_domain(lite_client: TestClient) -> None:
    caller = _headers("worker-a", user="shared-user")  # compat workspace: personal
    own_domain = "personal:takeover"
    foreign_domain = "other-ws:takeover"
    own_statement = f"own workspace learned procedure {uuid.uuid4().hex[:6]}"
    foreign_statement = f"foreign workspace learned procedure {uuid.uuid4().hex[:6]}"
    _insert_pattern(own_domain, own_statement)
    _insert_pattern(foreign_domain, foreign_statement)

    # Naming another workspace's takeover domain in the body must not read that workspace's learned rows.
    foreign = _bundle(lite_client, caller, foreign_domain)
    assert foreign.status_code == 200, foreign.text
    assert foreign_statement not in foreign.text
    assert foreign_statement not in {str(item["statement"]) for item in foreign.json()["top_patterns"]}

    listed = lite_client.get("/v1/patterns", params={"domain": foreign_domain}, headers=caller)
    assert listed.status_code == 200, listed.text
    assert foreign_statement not in {str(item["statement"]) for item in listed.json()}

    # Positive control: the caller's own takeover domain stays readable, and still never leaks the foreign row.
    own = _bundle(lite_client, caller, own_domain)
    assert own.status_code == 200, own.text
    assert own_statement in {str(item["statement"]) for item in own.json()["top_patterns"]}
    assert foreign_statement not in own.text


def _complete(client: TestClient, session_id: str, headers: dict[str, str], title: str) -> None:
    response = client.post(
        "/v1/completions",
        json={
            "session_id": session_id,
            "completion_key": f"key-{session_id}-{uuid.uuid4().hex[:6]}",
            "title": title,
            "payload": {"files": ["services/tce_api/tce_api/main.py"]},
            "decision": "fenced update",
            "outcome": {"status": "succeeded", "next_step": "run tests"},
            "git": {"branch": "main", "commit": "abc123def456"},
            "anchors": [{"file": "services/tce_api/tce_api/main.py", "line": 10}],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _resume_targeting(client: TestClient, reader_session: str, headers: dict[str, str], target_owner: str) -> dict[str, Any]:
    response = client.post(
        "/v1/handoff/resume",
        json={"query": "wire lease", "target_owner": target_owner, "session_id": reader_session, "k": 5, "include_cross_user": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_explicit_target_owner_is_a_hard_filter_on_resume(lite_client: TestClient) -> None:
    """A reader asking for the peer's handoff must never get its own record back.

    Observed live: once Claude had a completion of its own, resuming with
    target_owner=codex-executor returned Claude's own earlier session because the
    candidate set was reader+peer and the reader's row happened to rank higher.
    """
    codex = _headers("codex-executor", user="codex-executor")
    claude = _headers("claude-executor", user="claude-executor")
    codex_a, claude_a = _session(), _session()
    _complete(lite_client, codex_a, codex, "wire lease from codex")
    _complete(lite_client, claude_a, claude, "wire lease from claude")

    packet = _resume_targeting(lite_client, _session(), claude, target_owner="codex-executor")
    assert packet["source_owner_id"] == "codex-executor", packet
    assert packet["source_session_id"] == codex_a, packet
    assert packet["cross_user_scope_applied"] is True

    packet = _resume_targeting(lite_client, _session(), codex, target_owner="claude-executor")
    assert packet["source_owner_id"] == "claude-executor", packet
    assert packet["source_session_id"] == claude_a, packet


def _fingerprints_snapshot() -> list[tuple[Any, ...]]:
    conn = _db()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT consumer_id, workspace_id, fingerprint, observation_count, last_updated_at FROM behavioral_fingerprints ORDER BY consumer_id, workspace_id"
        ).fetchall()]
    finally:
        conn.close()


def test_executor_feedback_never_moves_the_behavioral_fingerprint(lite_client: TestClient) -> None:
    """Only attributable human input may move the fingerprint.

    The evidence row from an executor's correction is held for review; without this
    guard the same request still nudged the fingerprint directly, which is the loop
    where the system learns its own output and calls it the owner's preference.
    """
    executor = _headers("worker-a", user="shared-user")
    operator = _headers("operator-ui", user="shared-user", role="user")
    session_id = _session()

    # Establish a fingerprint with a human correction first so there is something to move.
    _feedback(lite_client, session_id, operator, f"operator seed correction {uuid.uuid4().hex[:6]}")
    before = _fingerprints_snapshot()
    assert before, "a human correction must create/update a fingerprint"

    _feedback(lite_client, session_id, executor, f"executor correction {uuid.uuid4().hex[:6]}")
    assert _fingerprints_snapshot() == before, "an executor correction moved the fingerprint"

    # Two generic corrections may yield identical values, so the signal that a human
    # correction still reaches the fingerprint is that the row was written at all.
    _feedback(lite_client, session_id, operator, f"operator second correction {uuid.uuid4().hex[:6]}")
    after_human = _fingerprints_snapshot()
    assert [row[-1] for row in after_human] != [row[-1] for row in before], "a human correction must still write the fingerprint"
