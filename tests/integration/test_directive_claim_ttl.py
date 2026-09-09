"""Claim TTL must not force-abandon actively-claimed (IN_PROGRESS) work.

`directive.expires_at` is the *claim window* deadline. An unclaimed PENDING
directive whose claim window elapses is correctly abandoned. But an
IN_PROGRESS directive the executor is actively working must survive an
elapsed claim TTL — long tasks legitimately run past it — and be reaped only
by the stale-work window keyed off started_at/updated_at.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app


def _headers(role: str = "executor", consumer: str = "claim-ttl-test") -> dict[str, str]:
    return {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
    }


@pytest.fixture()
def lite_ctx(tmp_path) -> Iterator[tuple[TestClient, str]]:
    settings = get_settings()
    db_path = str(tmp_path / "tce-lite-claim-ttl.db")
    settings.lite_db_path = db_path
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "clone_advisor"
    with TestClient(app) as client:
        yield client, db_path


def _directive_state(db_path: str, directive_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT state FROM directive_executions WHERE directive_id = ?",
            (directive_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _claim_directive(client: TestClient, session_id: str) -> str:
    permit = client.post(
        "/v1/takeover/permit",
        json={
            "session_id": session_id,
            "action_kind": "edit",
            "target_paths": ["docs/notes.md"],
            "estimated_change_size": 5,
        },
        headers=_headers(),
    )
    assert permit.status_code == 200 and permit.json()["decision"] == "allow"
    step = client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            # A mutating directive needs a bound project; an unbound context is refused.
            "app_context": {"domain": "coding", "project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"},
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert step.status_code == 200
    directive_id = step.json().get("directive_id")
    assert directive_id
    claim = client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_headers(),
    )
    assert claim.status_code == 200
    return str(directive_id)


def _age_claim_ttl(db_path: str, directive_id: str) -> None:
    """Expire the claim window while keeping the work fresh (recent started_at)."""
    now = datetime.now(tz=UTC)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE directive_executions SET expires_at = ?, started_at = ?, updated_at = ? "
            "WHERE directive_id = ?",
            (
                (now - timedelta(seconds=30)).isoformat(),
                now.isoformat(),
                now.isoformat(),
                directive_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_in_progress_survives_elapsed_claim_ttl(lite_ctx) -> None:
    client, db_path = lite_ctx
    session_id = f"claimttl-{uuid.uuid4().hex[:8]}"
    directive_id = _claim_directive(client, session_id)
    assert _directive_state(db_path, directive_id) == "in_progress"

    _age_claim_ttl(db_path, directive_id)

    # A subsequent turn runs _load_pending_directive; the actively-claimed
    # directive must NOT be abandoned just because the claim TTL elapsed.
    step = client.post(
        "/v1/takeover/step",
        json={
            "message": "continue",
            "session_id": session_id,
            "persona_mode": "shadow",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert step.status_code == 200
    assert _directive_state(db_path, directive_id) == "in_progress"


def test_pending_is_abandoned_when_claim_window_elapses(lite_ctx) -> None:
    client, db_path = lite_ctx
    session_id = f"claimttl-{uuid.uuid4().hex[:8]}"
    directive_id = _claim_directive(client, session_id)

    # Force it back to PENDING (never actually claimed) with an elapsed window.
    now = datetime.now(tz=UTC)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE directive_executions SET state = 'pending', claimed_by = NULL, "
            "started_at = NULL, expires_at = ?, updated_at = ? WHERE directive_id = ?",
            ((now - timedelta(seconds=30)).isoformat(), now.isoformat(), directive_id),
        )
        conn.commit()
    finally:
        conn.close()

    client.post(
        "/v1/takeover/step",
        json={
            "message": "continue",
            "session_id": session_id,
            "persona_mode": "shadow",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert _directive_state(db_path, directive_id) == "abandoned"
