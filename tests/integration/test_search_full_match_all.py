from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.db import get_session_factory
from tce_api.policy import PolicyEngine
from tce_api.search import run_search
from tce_shared.events import EventFilter, EventSearchRequest
from tce_shared.policy import ConsumerContext


class _UnavailableEmbeddingGateway:
    def embed(self, _text: str) -> list[float]:
        raise RuntimeError("embedding disabled for deterministic integration test")


def _delete_events(session: Session, event_ids: list[uuid.UUID]) -> None:
    session.rollback()
    session.execute(
        text("DELETE FROM events WHERE id = ANY(CAST(:event_ids AS uuid[]))"),
        {"event_ids": event_ids},
    )
    session.commit()


def test_full_match_all_keeps_descending_timestamp_order() -> None:
    source = f"integration-match-all-{uuid.uuid4()}"
    workspace = "integration-search"
    owner = "codex-executor"
    now = datetime.now(tz=UTC)
    event_ids = [uuid.uuid4() for _ in range(3)]
    session = get_session_factory()()
    try:
        for index, event_id in enumerate(event_ids):
            session.execute(
                text(
                    """
                    INSERT INTO events (
                        id, ts, actor, source, domain, task_type, event_type,
                        title, hash, context, sensitivity, authority_level
                    ) VALUES (
                        :id, :ts, :actor, :source, :domain, :task_type, :event_type,
                        :title, :hash, CAST(:context AS jsonb), 0, 'observed'
                    )
                    """
                ),
                {
                    "id": event_id,
                    "ts": now - timedelta(minutes=index),
                    "actor": owner,
                    "source": source,
                    "domain": "engineering",
                    "task_type": "validation",
                    "event_type": "TASK_STEP",
                    "title": f"Match-all event {index}",
                    "hash": f"integration-match-all:{event_id}",
                    "context": f'{{"_tce_workspace":"{workspace}","_tce_owner":"{owner}"}}',
                },
            )
        session.commit()

        request = EventSearchRequest(
            query="*",
            match_all=True,
            filters=EventFilter(source=source),
            k=3,
        )
        consumer = ConsumerContext(
            consumer="codex-executor",
            allowed_domains=["*"],
            max_sensitivity=2,
            workspace_id=workspace,
            owner_id=owner,
        )
        hits, citations, blocked, metadata = run_search(
            session,
            request,
            consumer,
            PolicyEngine(),
        )

        assert [hit.id for hit in hits] == event_ids
        assert citations == event_ids
        assert blocked == 0
        assert metadata["source"] == "none"
        assert metadata["lexical_channel"] == "none"
        assert metadata["scope_prefilter_applied"] is True
    finally:
        _delete_events(session, event_ids)
        session.close()


def test_full_fts_prefilters_workspace_and_owner_before_ranking() -> None:
    source = f"integration-fts-{uuid.uuid4()}"
    needle = f"needle{uuid.uuid4().hex}"
    workspace = "integration-search"
    owner = "codex-executor"
    same_owner_id = uuid.uuid4()
    rows = [
        (same_owner_id, workspace, owner),
        (uuid.uuid4(), workspace, "claude-executor"),
        (uuid.uuid4(), "other-workspace", owner),
    ]
    session = get_session_factory()()
    try:
        for index, (event_id, event_workspace, event_owner) in enumerate(rows):
            session.execute(
                text(
                    """
                    INSERT INTO events (
                        id, ts, actor, source, domain, task_type, event_type,
                        title, hash, context, sensitivity, authority_level
                    ) VALUES (
                        :id, :ts, :actor, :source, :domain, :task_type, :event_type,
                        :title, :hash, CAST(:context AS jsonb), 0, 'observed'
                    )
                    """
                ),
                {
                    "id": event_id,
                    "ts": datetime.now(tz=UTC) - timedelta(seconds=index),
                    "actor": event_owner,
                    "source": source,
                    "domain": "engineering",
                    "task_type": "validation",
                    "event_type": "TASK_STEP",
                    "title": f"{needle} retrieval evidence",
                    "hash": f"integration-fts:{event_id}",
                    "context": (
                        f'{{"_tce_workspace":"{event_workspace}",'
                        f'"_tce_owner":"{event_owner}"}}'
                    ),
                },
            )
        session.commit()

        request = EventSearchRequest(
            query=f"find earlier {needle} missing",
            filters=EventFilter(source=source),
            k=10,
        )
        consumer = ConsumerContext(
            consumer="codex-executor",
            allowed_domains=["*"],
            max_sensitivity=2,
            workspace_id=workspace,
            owner_id=owner,
        )
        with patch("tce_api.search.get_gateway", return_value=_UnavailableEmbeddingGateway()):
            hits, citations, blocked, metadata = run_search(
                session,
                request,
                consumer,
                PolicyEngine(),
            )

        assert [hit.id for hit in hits] == [same_owner_id]
        assert citations == [same_owner_id]
        assert blocked == 0
        assert metadata["source"] == "lexical_only"
        assert metadata["lexical_channel"] == "fts_plus_ilike_fill"
        assert metadata["fts_candidate_count"] == 1
        assert metadata["lexical_candidate_count"] == 1
        assert metadata["scope_prefilter_applied"] is True
        assert metadata["cross_user_scope_applied"] is False
    finally:
        _delete_events(session, [event_id for event_id, _, _ in rows])
        session.close()
