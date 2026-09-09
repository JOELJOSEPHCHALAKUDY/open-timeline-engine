from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.continuity_store import drain_pending_handoffs, enqueue_handoff
from tce_lite_api.main import app
from tce_shared.identity import credential_fingerprint


@pytest.fixture()
def client(tmp_path) -> Generator[TestClient, None, None]:
    settings = get_settings()
    original = {
        "lite_db_path": settings.lite_db_path,
        "api_tokens": settings.api_tokens,
        "identity_claims_mode": settings.identity_claims_mode,
        "identity_claims_json": settings.identity_claims_json,
    }
    settings.lite_db_path = str(tmp_path / "continuity.db")
    settings.api_tokens = "codex-token,claude-token"
    settings.identity_claims_mode = "enforce"
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
        }
    )
    with TestClient(app) as test_client:
        yield test_client
    for key, value in original.items():
        setattr(settings, key, value)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_transactional_completion_resume_feedback_and_identity_binding(client: TestClient) -> None:
    forged = client.get(
        "/v1/auth/whoami",
        headers={**_auth("codex-token"), "X-TCE-Workspace": "forged"},
    )
    assert forged.status_code == 403

    preload = client.post(
        "/v1/takeover/preload",
        headers=_auth("codex-token"),
        json={
            "session_id": "shared-session",
            "task": "implement durable completion outbox",
            "app_context": {
                "project": "open-timeline-engine",
                "project_root": "/work/open-timeline-engine",
            },
        },
    )
    assert preload.status_code == 200, preload.text

    completion = client.post(
        "/v1/completions",
        headers=_auth("codex-token"),
        json={
            "session_id": "shared-session",
            "completion_key": "test:continuity:1",
            "source": "integration",
            "state": "succeeded",
            "title": "Implemented durable completion outbox",
            "payload": {"files": ["services/tce_api/tce_api/continuity_store.py"]},
            "decision": "Commit state and outbox before asynchronous delivery",
            "outcome": {"status": "succeeded", "next_step": "Open deliver_handoff and run integration tests"},
            "git": {"branch": "test", "commit": "abc123"},
            "anchors": [{"file": "services/tce_api/tce_api/continuity_store.py", "line": 70, "symbol": "deliver_handoff"}],
            "milestone_schema": "v1",
        },
    )
    assert completion.status_code == 200, completion.text
    assert completion.json()["delivery_status"] == "delivered"
    with sqlite3.connect(get_settings().lite_db_path) as conn:
        event_row = conn.execute(
            "SELECT context FROM events WHERE id = ?",
            (completion.json()["event_id"],),
        ).fetchone()
    assert event_row is not None
    completion_context = json.loads(str(event_row[0]))
    assert completion_context["project"] == "open-timeline-engine"
    assert completion_context["project_id"].startswith("proj_")

    completion_payload = {
        "session_id": "shared-session",
        "completion_key": "test:continuity:1",
        "source": "integration",
        "state": "succeeded",
        "title": "Implemented durable completion outbox",
        "payload": {"files": ["services/tce_api/tce_api/continuity_store.py"]},
        "decision": "Commit state and outbox before asynchronous delivery",
        "outcome": {"status": "succeeded", "next_step": "Open deliver_handoff and run integration tests"},
        "git": {"branch": "test", "commit": "abc123"},
        "anchors": [{"file": "services/tce_api/tce_api/continuity_store.py", "line": 70, "symbol": "deliver_handoff"}],
        "milestone_schema": "v1",
    }
    # Identical retry under the same completion_key replays the original receipt.
    duplicate = client.post("/v1/completions", headers=_auth("codex-token"), json=completion_payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["outbox_id"] == completion.json()["outbox_id"]

    # Same completion_key with a different payload is a conflict, never a silent replay.
    changed = client.post(
        "/v1/completions",
        headers=_auth("codex-token"),
        json={**completion_payload, "decision": "Idempotent retry", "outcome": {"status": "succeeded", "next_step": "Resume"}},
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["reason"] == "idempotency_conflict"
    with sqlite3.connect(get_settings().lite_db_path) as conn:
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM handoff_outbox WHERE completion_key = ?",
            ("test:continuity:1",),
        ).fetchone()[0]
    assert outbox_count == 1

    # Claude resumes from a *different* session: the candidate scope is the authorised
    # project/owner window, not equality with the reader's own conversation id.
    resume = client.post(
        "/v1/handoff/resume",
        headers=_auth("claude-token"),
        json={
            "query": "read codex timeline durable completion outbox",
            "target_owner": "codex-executor",
            "session_id": "claude-b",
            "k": 5,
            "include_cross_user": True,
        },
    )
    assert resume.status_code == 200, resume.text
    packet = resume.json()
    assert packet["source_session_id"] == "shared-session"
    assert packet["source_owner_id"] == "codex-executor"
    assert packet["files"][0]["path"] == "services/tce_api/tce_api/continuity_store.py"
    assert packet["files"][0]["anchors"][0]["symbol"] == "deliver_handoff"

    opened = client.post(
        "/v1/clone/check-context",
        headers=_auth("claude-token"),
        json={
            "file_path": packet["files"][0]["path"],
            "intended_action": "edit",
            "session_id": "shared-session",
        },
    )
    assert opened.status_code == 200

    feedback = client.post(
        "/v1/continuity/pilot/feedback",
        headers=_auth("claude-token"),
        json={
            "packet_id": packet["packet_id"],
            "phase": "completed",
            "opened_file": packet["files"][0]["path"],
            "correct_file": True,
            "correct_anchor": True,
            "correction_required": False,
            "archaeology_tool_calls": 2,
            "archaeology_tokens": 300,
        },
    )
    assert feedback.status_code == 200
    pilot = client.get("/v1/continuity/pilot/status?days=30", headers=_auth("claude-token"))
    assert pilot.status_code == 200
    assert pilot.json()["handoff_capture_coverage"] == 1.0
    assert pilot.json()["correct_file_rate"] == 1.0
    assert pilot.json()["correct_file_at_1_rate"] == 1.0
    assert pilot.json()["correct_anchor_rate"] == 1.0
    assert pilot.json()["productive_resume_count"] == 1
    assert pilot.json()["completed_resume_count"] == 1
    assert pilot.json()["median_time_to_first_file_ms"] is not None
    assert pilot.json()["median_active_resume_ms"] is not None
    assert pilot.json()["median_archaeology_tool_calls"] == 2
    assert pilot.json()["median_archaeology_tokens"] == 300


def test_committed_outbox_replays_after_delivery_interruption(client: TestClient) -> None:
    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        outbox = enqueue_handoff(
            conn,
            workspace_id="shared",
            owner_id="codex-executor",
            behavior_subject_id="human",
            session_id="replay-session",
            directive_id=None,
            completion_key="test:continuity:replay",
            terminal_state="succeeded",
            milestone={
                "title": "Replay interrupted completion",
                "payload": {"files": ["src/replay.py"]},
                "decision": "Persist the outbox before projection delivery",
                "outcome": {"status": "succeeded", "next_step": "Drain the pending row"},
                "anchors": [{"file": "src/replay.py", "line": 7, "symbol": "resume"}],
                "milestone_schema": "v1",
            },
            source="integration",
            redaction_applied=False,
        )
        outbox_id = str(outbox["id"])
        conn.commit()
    finally:
        conn.close()

    recovered = sqlite3.connect(settings.lite_db_path)
    recovered.row_factory = sqlite3.Row
    try:
        assert recovered.execute("SELECT status FROM handoff_outbox WHERE id = ?", (outbox_id,)).fetchone()["status"] == "pending"
        assert recovered.execute("SELECT COUNT(*) AS count FROM handoff_records WHERE event_id = (SELECT event_id FROM handoff_outbox WHERE id = ?)", (outbox_id,)).fetchone()["count"] == 0
        result = drain_pending_handoffs(recovered, retention_days=90)
        assert result == {"processed": 1, "delivered": 1, "pending": 0, "dead": 0}
        assert recovered.execute("SELECT status FROM handoff_outbox WHERE id = ?", (outbox_id,)).fetchone()["status"] == "delivered"
        assert recovered.execute("SELECT COUNT(*) AS count FROM handoff_records WHERE event_id = (SELECT event_id FROM handoff_outbox WHERE id = ?)", (outbox_id,)).fetchone()["count"] == 1
    finally:
        recovered.close()


def test_outbox_delivery_failure_is_atomic_and_recoverable(client: TestClient) -> None:
    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        outbox = enqueue_handoff(
            conn,
            workspace_id="shared",
            owner_id="codex-executor",
            behavior_subject_id="human",
            session_id="failure-injection",
            directive_id=None,
            completion_key="test:continuity:failure-injection",
            terminal_state="succeeded",
            milestone={
                "title": "Recover an interrupted projection",
                "payload": {"files": ["src/recovery.py"]},
                "decision": "Exercise the durable retry path",
                "outcome": {"status": "succeeded", "next_step": "Repair and replay"},
                # dict(["invalid"]) raises during delivery after the outbox is committed.
                "git": ["invalid"],
                "milestone_schema": "v1",
            },
            source="failure-injection",
            redaction_applied=False,
        )
        outbox_id = str(outbox["id"])
        event_id = str(outbox["event_id"])
        handoff_id = str(outbox["handoff_record_id"])
        conn.commit()

        failed = drain_pending_handoffs(conn, retention_days=90)
        assert failed == {"processed": 1, "delivered": 0, "pending": 1, "dead": 0}
        failed_row = conn.execute(
            "SELECT status, attempts, last_error FROM handoff_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        assert failed_row["status"] == "pending"
        assert failed_row["attempts"] == 1
        assert failed_row["last_error"]
        assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (event_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM handoff_records WHERE id = ?",
            (handoff_id,),
        ).fetchone()[0] == 0

        milestone = json.loads(
            conn.execute(
                "SELECT milestone_json FROM handoff_outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()[0]
        )
        milestone["git"] = {"branch": "recovery"}
        conn.execute(
            """
            UPDATE handoff_outbox
            SET milestone_json = ?, next_attempt_at = '2000-01-01T00:00:00+00:00'
            WHERE id = ?
            """,
            (json.dumps(milestone), outbox_id),
        )
        conn.commit()

        recovered = drain_pending_handoffs(conn, retention_days=90)
        assert recovered == {"processed": 1, "delivered": 1, "pending": 0, "dead": 0}
        delivered_row = conn.execute(
            "SELECT status, attempts, last_error FROM handoff_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        assert delivered_row["status"] == "delivered"
        assert delivered_row["attempts"] == 2
        assert delivered_row["last_error"] is None
        assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (event_id,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM handoff_records WHERE id = ?",
            (handoff_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_mutating_grant_requires_completion_before_next_mutation(client: TestClient) -> None:
    settings = get_settings()
    permit_id = str(uuid.uuid4())
    directive_id = str(uuid.uuid4())
    conn = sqlite3.connect(settings.lite_db_path)
    try:
        conn.execute(
            """
            INSERT INTO execution_permits(
                id, session_id, workspace_id, action_kind, target_paths, command_preview,
                estimated_change_size, decision, reason, confirmed_by, expires_at, created_at, resolved_at
            ) VALUES(?, 'obligation', 'shared', 'edit', '["src/app.py"]', NULL, 1,
                     'allow', 'test', 'codex-executor', '2099-01-01T00:00:00+00:00',
                     '2026-07-20T00:00:00+00:00', NULL)
            """,
            (permit_id,),
        )
        conn.execute(
            """
            INSERT INTO directive_executions(
                directive_id, session_id, workspace_id, user_id, goal_id, objective_hash,
                action_kind, attempt, state, requires_permit, permit_id, claimed_by,
                started_at, finished_at, expires_at, failure_class, failure_reason,
                retry_strategy, meta, created_at, updated_at
            ) VALUES(?, 'obligation', 'shared', 'codex-executor', NULL, NULL, 'edit', 1,
                     'in_progress', 1, ?, 'codex-executor', '2026-07-20T00:00:00+00:00',
                     NULL, '2099-01-01T00:00:00+00:00', NULL, NULL, NULL, '{}',
                     '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00')
            """,
            (directive_id, permit_id),
        )
        conn.commit()
    finally:
        conn.close()
    grant_request = {
        "session_id": "obligation",
        "directive_id": directive_id,
        "permit_id": permit_id,
        "capability": "filesystem.write",
        "action": "patch",
        "resource": "src/app.py",
        "arguments": {"patch_hash": "abc"},
    }
    grant = client.post("/v1/capabilities/grants", headers=_auth("codex-token"), json=grant_request)
    assert grant.status_code == 200
    grant_body = grant.json()
    consumed = client.post(
        "/v1/capabilities/consume",
        headers=_auth("codex-token"),
        json={
            "grant_id": grant_body["grant_id"],
            "token": grant_body["token"],
            "capability": "filesystem.write",
            "action": "patch",
            "resource": "src/app.py",
            "arguments": {"patch_hash": "abc"},
        },
    )
    assert consumed.json()["authorized"] is True
    blocked = client.post("/v1/capabilities/grants", headers=_auth("codex-token"), json=grant_request)
    assert blocked.json()["decision"] == "blocked"
    assert "completion capture" in blocked.json()["reason"]
    completion = client.post(
        "/v1/completions",
        headers=_auth("codex-token"),
        json={
            "session_id": "obligation",
            "completion_key": "obligation:complete",
            "source": "integration",
            "state": "succeeded",
            "title": "Patched app",
            "payload": {"files": ["src/app.py"]},
            "decision": "Applied authorized patch",
            "outcome": {"status": "succeeded", "next_step": "Run tests"},
            "milestone_schema": "v1",
        },
    )
    assert completion.json()["delivery_status"] == "delivered"
    allowed_again = client.post("/v1/capabilities/grants", headers=_auth("codex-token"), json=grant_request)
    assert allowed_again.json()["decision"] == "allow"
