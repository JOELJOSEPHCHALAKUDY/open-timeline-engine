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
from pydantic import AnyUrl

# Every test here drives a REAL `python -m tce_mcp.server` over stdio -- that is the point of the
# module, and it is exactly the spawn tests/conftest.py::_no_subprocess exists to catch. The guard
# is right; the spawn is intended, so it is declared rather than the guard weakened.
pytestmark = [pytest.mark.e2e, pytest.mark.subprocess]


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


async def _read_resource(token: str, user: str, uri: str) -> str:
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
            result = await session.read_resource(AnyUrl(uri))
            return "\n".join(
                text
                for item in result.contents
                if isinstance((text := getattr(item, "text", None)), str)
            )


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
    # Claude resumes from a distinct session id: the reader's own session is not a candidate filter.
    resumed = asyncio.run(
        _call_tool(
            claude_token,
            "claude-executor",
            "tce.get_resume_packet",
            {
                "query": "read codex timeline MCP transport continuity handoff",
                "target_owner": "codex-executor",
                "session_id": "e2e-reader",
                "k": 5,
                "include_cross_user": True,
                "current_git": {"commit": "mcp123"},
            },
        )
    )
    packet = resumed["result"]
    assert packet["files"][0]["path"] == "services/tce_mcp/tce_mcp/server.py"
    assert packet["source_session_id"] == "e2e-shared"
    assert packet["anchor_freshness"] == "current"

    # Reverse direction over the same transport: Claude completes, Codex resumes from a new session.
    reverse_key = f"e2e:mcp:reverse:{uuid.uuid4()}"
    reverse_captured = asyncio.run(
        _call_tool(
            claude_token,
            "claude-executor",
            "tce.complete_task",
            {
                "completion_key": reverse_key,
                "title": "MCP transport reverse continuity handoff",
                "files": ["services/tce_mcp/tce_mcp/tools.py"],
                "decision": "Verify the reverse resume path over MCP",
                "next_step": "Open the get_resume_packet tool",
                "session_id": "e2e-claude-a",
                "git": {"branch": "e2e", "commit": "rev123"},
                "anchors": [{"file": "services/tce_mcp/tce_mcp/tools.py", "line": 447, "symbol": "get_resume_packet"}],
            },
        )
    )
    assert reverse_captured["result"]["delivery_status"] == "delivered"
    codex_resumed = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.get_resume_packet",
            {
                "query": "read claude timeline MCP transport reverse continuity handoff",
                "target_owner": "claude-executor",
                "session_id": "e2e-codex-b",
                "k": 5,
                "include_cross_user": True,
            },
        )
    )
    assert codex_resumed["result"]["source_session_id"] == "e2e-claude-a"
    assert codex_resumed["result"]["files"][0]["path"] == "services/tce_mcp/tce_mcp/tools.py"
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


def test_real_stdio_mcp_behavior_projection_resource() -> None:
    if not os.getenv("TCE_E2E_BASE_URL"):
        pytest.skip("TCE_E2E_BASE_URL is not configured")
    codex_token = os.getenv("TCE_E2E_CODEX_TOKEN", "")
    marker = f"MCP projection evidence {uuid.uuid4().hex}"
    recorded = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.record_behavior_evidence",
            {
                "situation_type": "mcp_projection_e2e",
                "situation_summary": marker,
                "objective": "Verify behavior projections through the real MCP resource transport",
                "selected_choice": "read canonical evidence through a read-only resource",
                "rationale": "Exercise identity binding, Full storage, projection rendering, and MCP transport",
                "action_taken": "Read current.md",
                "outcome": "Projection content returned",
                "memory_class": "decision",
                "evidence_source": "explicit",
                "confidence": 1.0,
            },
        )
    )
    observation_id = recorded["result"]["observation_id"]

    # An EXECUTOR asked for evidence_source "explicit" and the server stored it as "inferred":
    # a caller cannot certify its own evidence (P1). current.md renders only eligible evidence --
    # explicit, correction or calibration -- so the executor's row is deliberately absent from it.
    # Asserting the absence is what proves the downgrade actually happened.
    eligible_view = asyncio.run(
        _read_resource(
            codex_token,
            "codex-executor",
            "tce://workspace/e2e-workspace/behavior/e2e-human/current.md",
        )
    )
    assert marker not in eligible_view
    assert observation_id not in eligible_view

    # The transport and the renderer are proved on the evidence view, which selects by id rather
    # than by eligibility, so this still exercises exactly what the test exists for.
    content = asyncio.run(
        _read_resource(
            codex_token,
            "codex-executor",
            f"tce://workspace/e2e-workspace/behavior/e2e-human/evidence/{observation_id}.json",
        )
    )
    assert marker in content
    assert observation_id in content
    assert '"projection_learning_eligible": false' in content or "projection_learning_eligible: false" in content


def test_real_stdio_mcp_behavior_projection_pilot_and_html_review() -> None:
    if not os.getenv("TCE_E2E_BASE_URL"):
        pytest.skip("TCE_E2E_BASE_URL is not configured")
    codex_token = os.getenv("TCE_E2E_CODEX_TOKEN", "")
    marker = f"MCP pilot evidence {uuid.uuid4().hex}"
    evidence = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.record_behavior_evidence",
            {
                "situation_type": "mcp_projection_pilot_e2e",
                "situation_summary": marker,
                "objective": "Verify the prospective projection pilot through Full API and MCP",
                "selected_choice": "minimal verified change",
                "rationale": "Keep the trial deterministic and independently measurable",
                "action_taken": "Run the assigned context arm",
                "outcome": "Pilot outcome captured",
                "memory_class": "decision",
                "evidence_source": "explicit",
                "confidence": 1.0,
            },
        )
    )
    observation_id = evidence["result"]["observation_id"]
    assignment = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.assign_behavior_projection_pilot",
            {
                "trial_key": f"mcp-pilot-{uuid.uuid4().hex}",
                "situation_type": "mcp_projection_pilot_e2e",
                "situation_summary": marker,
                "objective": "Verify prospective projection evaluation",
                "candidate_choices": ["minimal verified change", "broad rewrite"],
            },
        )
    )
    assignment_result = assignment["result"]
    assert assignment_result["projection_learning_eligible"] is False
    assert assignment_result["variant"] in {
        "no_memory",
        "canonical_structured",
        "markdown_projection",
        "projection_index",
    }
    outcome = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.report_behavior_projection_pilot_outcome",
            {
                "assignment_id": assignment_result["assignment_id"],
                "agent_choice": "minimal verified change",
                "top3_choices": ["minimal verified change", "broad rewrite"],
                "actual_choice": "minimal verified change",
                "used_evidence_ids": assignment_result["citations"],
            },
        )
    )
    assert outcome["result"]["recorded"] is True
    assert outcome["result"]["stale_evidence_used"] is False
    status = asyncio.run(
        _call_tool(
            codex_token,
            "codex-executor",
            "tce.get_behavior_projection_pilot_status",
            {},
        )
    )
    assert status["result"]["status"] == "collecting"
    assert status["result"]["assignment_count"] >= 1
    review = asyncio.run(
        _read_resource(
            codex_token,
            "codex-executor",
            "tce://workspace/e2e-workspace/behavior/e2e-human/review.html",
        )
    )
    assert marker in review
    assert observation_id in review
    assert "<script" not in review.lower()
