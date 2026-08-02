from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text
from tce_api.db import get_session_factory
from tce_api.policy import PolicyEngine
from tce_api.search import run_search
from tce_lite_api.config import get_settings as get_lite_settings
from tce_lite_api.db import init_db
from tce_lite_api.store import search_events
from tce_shared.events import EventFilter, EventSearchRequest
from tce_shared.policy import ConsumerContext


class _UnavailableEmbeddingGateway:
    def embed(self, _text: str) -> list[float]:
        raise RuntimeError("embedding disabled for deterministic parity test")


def test_full_and_lite_top_three_retrieval_sets_overlap(tmp_path) -> None:
    workspace = "search-parity"
    owner = "codex-executor"
    source = f"search-parity-{uuid.uuid4()}"
    query_tokens = ["parityalpha", "paritybravo", "paritycharlie", "paritydelta", "parityecho"]
    titles = [
        f"{token} implementation candidate {index}"
        for token in query_tokens
        for index in range(3)
    ]
    titles.extend(f"unrelated retrieval distractor {index}" for index in range(5))
    now = datetime.now(tz=UTC)

    lite_settings = get_lite_settings()
    lite_settings.lite_db_path = str(tmp_path / "search-parity.db")
    init_db()
    lite_conn = sqlite3.connect(lite_settings.lite_db_path)
    lite_conn.row_factory = sqlite3.Row
    full_session = get_session_factory()()
    full_ids: list[uuid.UUID] = []
    try:
        for index, title in enumerate(titles):
            event_id = uuid.uuid4()
            full_ids.append(event_id)
            ts = now - timedelta(seconds=index)
            context = {"_tce_workspace": workspace, "_tce_owner": owner}
            full_session.execute(
                text(
                    """
                    INSERT INTO events (
                        id, ts, actor, source, domain, task_type, event_type,
                        title, hash, context, sensitivity, authority_level
                    ) VALUES (
                        :id, :ts, :actor, :source, 'engineering', 'validation',
                        'TASK_STEP', :title, :hash, CAST(:context AS jsonb), 0, 'observed'
                    )
                    """
                ),
                {
                    "id": event_id,
                    "ts": ts,
                    "actor": owner,
                    "source": source,
                    "title": title,
                    "hash": f"parity:{event_id}",
                    "context": json.dumps(context),
                },
            )
            lite_conn.execute(
                """
                INSERT INTO events (
                    id, ts, actor, source, domain, task_type, event_type, title,
                    payload, context, inputs, steps, decision, outcome, style,
                    links, tags, sensitivity, redaction_hints, hash, schema_version,
                    authority_level
                ) VALUES (?, ?, ?, ?, 'engineering', 'validation', 'TASK_STEP', ?,
                          '{}', ?, '{}', '[]', NULL, NULL, NULL, NULL, '[]', 0,
                          '[]', ?, 1, 'observed')
                """,
                (
                    str(uuid.uuid4()),
                    ts.isoformat(),
                    owner,
                    source,
                    title,
                    json.dumps(context),
                    f"lite-parity:{event_id}",
                ),
            )
        full_session.commit()
        lite_conn.commit()

        consumer = ConsumerContext(
            consumer=owner,
            allowed_domains=["*"],
            max_sensitivity=2,
            workspace_id=workspace,
            owner_id=owner,
        )
        for query in query_tokens:
            request = EventSearchRequest(
                query=query,
                filters=EventFilter(source=source),
                k=3,
            )
            with patch("tce_api.search.get_gateway", return_value=_UnavailableEmbeddingGateway()):
                full_hits, _, _, _ = run_search(
                    full_session,
                    request,
                    consumer,
                    PolicyEngine(),
                )
            lite_response, _, _ = search_events(
                lite_conn,
                request,
                lite_settings,
                workspace_id=workspace,
                owner_id=owner,
            )
            full_titles = {hit.title for hit in full_hits[:3]}
            lite_titles = {hit.title for hit in lite_response.hits[:3]}
            assert len(full_titles & lite_titles) >= 2
    finally:
        full_session.rollback()
        full_session.execute(
            text("DELETE FROM events WHERE id = ANY(CAST(:event_ids AS uuid[]))"),
            {"event_ids": full_ids},
        )
        full_session.commit()
        full_session.close()
        lite_conn.close()
