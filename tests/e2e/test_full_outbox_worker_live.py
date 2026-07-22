from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from tce_api.continuity_store import enqueue_handoff
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
