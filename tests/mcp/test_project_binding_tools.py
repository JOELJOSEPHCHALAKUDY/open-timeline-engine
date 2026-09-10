"""Core MCP calls carry project binding (design section 7).

``tce.search_events`` and ``tce.get_patterns`` accept an optional ``app_context``,
resolve it through ``with_project_context`` and forward the binding to the API,
the same way ``tce.complete_task`` already does.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from tce_mcp import server, tools
from tce_shared.project_context import canonical_project_context

# tce_mcp.project_context.discover_project_context shells out to `git rev-parse` on its
# first call (services/tce_mcp/tce_mcp/project_context.py:18). It is @lru_cache(maxsize=1),
# so WHICH test in this module pays for that spawn depends on collection order — hence a
# module-level marker rather than per-test ones. See tests/conftest.py::_no_subprocess.
pytestmark = pytest.mark.subprocess

_APP_CONTEXT: dict[str, Any] = {"domain": "coding", "project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"}
_PROJECT_ID = canonical_project_context(dict(_APP_CONTEXT))["project_id"]


def _tool_params(name: str) -> set[str]:
    registered = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    assert name in registered, name
    return set(registered[name].parameters["properties"])


def test_search_events_and_get_patterns_tools_expose_app_context() -> None:
    assert "app_context" in _tool_params("tce.search_events")
    assert "app_context" in _tool_params("tce.get_patterns")
    assert "app_context" in _tool_params("tce.complete_task")


def test_search_events_forwards_bound_project_context() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.search_events.return_value = {"result": {"citations": ["e1"]}}
        result = tools.search_events(query="needle", app_context=_APP_CONTEXT, time_range={"start": "2026-01-01T00:00:00+00:00"})
    body = mock_client.search_events.call_args.args[0]
    assert body["query"] == "needle"
    assert body["time_start"] == "2026-01-01T00:00:00+00:00"
    assert body["app_context"]["project_id"] == _PROJECT_ID
    assert body["app_context"]["project_root"] == "/work/open-timeline-engine"
    assert body["app_context"]["project_source"] == "explicit"
    assert body["app_context"]["domain"] == "coding"
    assert result["citations"] == ["e1"]


def test_search_events_without_app_context_still_carries_a_project_context_dict() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.search_events.return_value = {"result": {}}
        tools.search_events(query="needle")
    body = mock_client.search_events.call_args.args[0]
    assert isinstance(body["app_context"], dict)


def test_get_patterns_forwards_bound_project_id() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.get_patterns.return_value = [{"id": "p1", "evidence_event_ids": ["e1", "e2"]}]
        result = tools.get_patterns(domain="coding", min_confidence=0.7, app_context=_APP_CONTEXT)
    kwargs = mock_client.get_patterns.call_args.kwargs
    assert kwargs["domain"] == "coding"
    assert kwargs["min_confidence"] == 0.7
    assert kwargs["project_id"] == _PROJECT_ID
    assert result["citations"] == ["e1", "e2"]


def test_complete_task_forwards_bound_project_context() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.capture_completion.return_value = {"event_id": "e1", "handoff_record_id": "h1"}
        result = tools.complete_task(
            completion_key="ck-1",
            title="ship it",
            files=["a.py"],
            decision="done",
            next_step="none",
            app_context=_APP_CONTEXT,
        )
    body = mock_client.capture_completion.call_args.args[0]
    assert body["app_context"]["project_id"] == _PROJECT_ID
    assert body["app_context"]["project_source"] == "explicit"
    assert result["citations"] == ["e1", "h1"]


def test_server_tools_forward_app_context_to_tools_layer() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.search_events.return_value = {"result": {}}
        mock_client.get_patterns.return_value = []
        mock_client.capture_completion.return_value = {"event_id": "e1", "handoff_record_id": "h1"}
        server.search_events(query="needle", app_context=_APP_CONTEXT)
        server.get_patterns(app_context=_APP_CONTEXT)
        server.complete_task(completion_key="ck-1", title="ship it", files=["a.py"], decision="done", next_step="none", app_context=_APP_CONTEXT)
    assert mock_client.search_events.call_args.args[0]["app_context"]["project_id"] == _PROJECT_ID
    assert mock_client.get_patterns.call_args.kwargs["project_id"] == _PROJECT_ID
    assert mock_client.capture_completion.call_args.args[0]["app_context"]["project_id"] == _PROJECT_ID
