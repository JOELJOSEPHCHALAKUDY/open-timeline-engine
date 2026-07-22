from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.e2e


def _text_payload(result: Any) -> dict[str, Any]:
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                return decoded
            raise AssertionError("MCP tool returned non-object JSON")
    raise AssertionError("MCP tool returned no JSON text content")


async def _call_tool(token: str, user: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    base = os.getenv("TCE_E2E_BASE_URL", "").rstrip("/")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tce_mcp.server"],
        env={
            **os.environ,
            "TCE_API_BASE_URL": base,
            "TCE_API_TOKEN": token,
            "TCE_MCP_CONSUMER_ID": user,
            "TCE_MCP_ROLE": "executor",
            "TCE_MCP_WORKSPACE_ID": "e2e-workspace",
            "TCE_MCP_USER_ID": user,
            "TCE_MCP_BEHAVIOR_SUBJECT_ID": "e2e-human",
            "TCE_MCP_SESSION_ID": "e2e-shared",
            "TCE_MCP_TRANSPORT": "stdio",
        },
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            return _text_payload(await session.call_tool(tool, arguments=arguments))


def test_real_stdio_mcp_cross_executor_takeover() -> None:
    if not os.getenv("TCE_E2E_BASE_URL"):
        pytest.skip("TCE_E2E_BASE_URL is not configured")
    codex_token = os.getenv("TCE_E2E_CODEX_TOKEN", "")
    claude_token = os.getenv("TCE_E2E_CLAUDE_TOKEN", "")
    completion_key = f"e2e:mcp:{uuid.uuid4()}"
    captured = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.complete_task",
            {
                "completion_key": completion_key,
                "title": "MCP transport continuity handoff",
                "files": ["services/tce_mcp/tce_mcp/server.py"],
                "decision": "Expose completion through the MCP transport",
                "next_step": "Open the complete_task tool",
                "session_id": "e2e-shared",
                "git": {"branch": "e2e", "commit": "mcp123"},
                "anchors": [{"file": "services/tce_mcp/tce_mcp/server.py", "line": 40, "symbol": "complete_task"}],
            },
        )
    )
    assert captured["result"]["delivery_status"] == "delivered"
    resumed = asyncio.run(
        _call_tool(
            claude_token,
            "claude-executor",
            "tce.get_resume_packet",
            {
                "query": "read codex timeline MCP transport continuity handoff",
                "target_owner": "codex-executor",
                "session_id": "e2e-shared",
                "k": 5,
                "include_cross_user": True,
            },
        )
    )
    packet = resumed["result"]
    assert packet["files"][0]["path"] == "services/tce_mcp/tce_mcp/server.py"
    feedback = asyncio.run(
        _call_tool(
            claude_token,
            "claude-executor",
            "tce.report_resume_feedback",
            {
                "packet_id": packet["packet_id"],
                "opened_file": packet["files"][0]["path"],
                "correct_file": True,
                "correction_required": False,
            },
        )
    )
    assert feedback["result"]["recorded"] is True
    pilot = asyncio.run(_call_tool(claude_token, "claude-executor", "tce.get_continuity_pilot", {"days": 30}))
    assert pilot["result"]["handoff_capture_coverage"] == 1.0
