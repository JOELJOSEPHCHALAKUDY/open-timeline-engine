from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from tce_api.continuity_store import deliver_handoff_safely, enqueue_handoff
from tce_api.db import get_session_factory
from tce_worker.jobs.handoff_outbox import run

pytestmark = pytest.mark.e2e


def test_full_worker_replays_committed_outbox() -> None:
    if not os.getenv("TCE_E2E_BASE_URL"):
        pytest.skip("TCE_E2E_BASE_URL is not configured")
    session_factory = get_session_factory()
    completion_key = f"e2e:worker:{uuid.uuid4()}"
    with session_factory() as db:
        row = enqueue_handoff(
            db,
            workspace_id="e2e-workspace",
            owner_id="codex-executor",
            behavior_subject_id="e2e-human",
            session_id="e2e-worker-replay",
            directive_id=None,
            completion_key=completion_key,
            terminal_state="succeeded",
            milestone={
                "title": "Full worker outbox replay",
                "payload": {"files": ["services/tce_worker/tce_worker/jobs/handoff_outbox.py"]},
                "decision": "Commit pending completion before worker projection",
                "outcome": {"status": "succeeded", "next_step": "Verify event and handoff rows"},
                "milestone_schema": "v1",
            },
            source="full-worker-e2e",
            redaction_applied=False,
        )
        outbox_id = str(row.id)
        db.commit()

    assert run(outbox_id=outbox_id) == {"processed": 1, "delivered": 1, "pending": 0, "dead": 0}
    with session_factory() as db:
        state = db.execute(
            text(
                """
                SELECT o.status,
                       EXISTS(SELECT 1 FROM handoff_records h WHERE h.id = o.handoff_record_id) AS has_handoff,
                       EXISTS(SELECT 1 FROM events e WHERE e.id = o.event_id) AS has_event
                FROM handoff_outbox o WHERE o.id = :id
                """
            ),
            {"id": outbox_id},
        ).mappings().one()
    assert state["status"] == "delivered"
    assert state["has_handoff"] is True
    assert state["has_event"] is True


def test_full_outbox_failure_is_atomic_then_recoverable() -> None:
    if not os.getenv("TCE_E2E_BASE_URL"):
        pytest.skip("TCE_E2E_BASE_URL is not configured")
    session_factory = get_session_factory()
    completion_key = f"e2e:worker:failure:{uuid.uuid4()}"
    with session_factory() as db:
        row = enqueue_handoff(
            db,
            workspace_id="e2e-workspace",
            owner_id="codex-executor",
            behavior_subject_id="e2e-human",
            session_id="e2e-worker-failure",
            directive_id=None,
            completion_key=completion_key,
            terminal_state="succeeded",
            milestone={
                "title": "Full worker failure injection",
                "payload": {"files": ["services/tce_api/tce_api/continuity_store.py"]},
                "decision": "Verify atomic rollback and durable replay",
                "outcome": {"status": "succeeded", "next_step": "Repair the payload and retry"},
                "git": ["invalid"],
                "milestone_schema": "v1",
            },
            source="full-worker-failure-e2e",
            redaction_applied=False,
        )
        outbox_id = row.id
        event_id = row.event_id
        handoff_id = row.handoff_record_id
        db.commit()

    with session_factory() as db:
        failed = deliver_handoff_safely(db, outbox_id=outbox_id, retention_days=90)
        assert failed.status == "pending"
        assert failed.attempts == 1
        assert failed.last_error
    with session_factory() as db:
        projection_counts = db.execute(
            text(
                """
                SELECT EXISTS(SELECT 1 FROM events WHERE id = :event_id) AS has_event,
                       EXISTS(SELECT 1 FROM handoff_records WHERE id = :handoff_id) AS has_handoff
                """
            ),
            {"event_id": event_id, "handoff_id": handoff_id},
        ).mappings().one()
        assert projection_counts["has_event"] is False
        assert projection_counts["has_handoff"] is False
        milestone = db.execute(
            text("SELECT milestone_json FROM handoff_outbox WHERE id = :id"),
            {"id": outbox_id},
        ).scalar_one()
        milestone_payload = dict(milestone)
        milestone_payload["git"] = {"branch": "recovery"}
        db.execute(
            text(
                """
                UPDATE handoff_outbox
                SET milestone_json = CAST(:milestone AS jsonb),
                    next_attempt_at = NOW() - INTERVAL '1 second'
                WHERE id = :id
                """
            ),
            {"milestone": json.dumps(milestone_payload), "id": outbox_id},
        )
        db.commit()

    assert run(outbox_id=str(outbox_id)) == {
        "processed": 1,
        "delivered": 1,
        "pending": 0,
        "dead": 0,
    }
    with session_factory() as db:
        recovered = db.execute(
            text(
                """
                SELECT status, attempts, last_error,
                       EXISTS(SELECT 1 FROM events WHERE id = :event_id) AS has_event,
                       EXISTS(SELECT 1 FROM handoff_records WHERE id = :handoff_id) AS has_handoff
                FROM handoff_outbox WHERE id = :id
                """
            ),
            {"id": outbox_id, "event_id": event_id, "handoff_id": handoff_id},
        ).mappings().one()
        assert recovered["status"] == "delivered"
        assert recovered["attempts"] == 2
        assert recovered["last_error"] is None
        assert recovered["has_event"] is True
        assert recovered["has_handoff"] is True
