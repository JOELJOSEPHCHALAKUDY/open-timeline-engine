from tce_mcp import server


def test_takeover_tools_present() -> None:
    tool_names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "tce.takeover_step" in tool_names
    assert "tce.takeover_preload" in tool_names
    assert "tce.takeover_feedback" in tool_names
    assert "tce.takeover_discover_goals" in tool_names
    assert "tce.get_takeover_goals" in tool_names
    assert "tce.select_takeover_goal" in tool_names
    assert "tce.request_execution_permit" in tool_names
    assert "tce.resolve_execution_permit" in tool_names
    assert "tce.get_autonomy_status" in tool_names
    assert "tce.get_takeover_state" in tool_names
    assert "tce.reset_takeover_state" in tool_names


def test_takeover_step_slim_includes_v3_fields() -> None:
    from tce_mcp.tools import _slim_takeover_result

    slim = _slim_takeover_result(
        {
            "state": {
                "session_id": "s1",
                "active": True,
                "mode": "takeover",
                "persona_mode": "shadow",
                "takeover_context": {"objective": "ship feature", "turn_count": 2},
            },
            "action": "advisor_takeover",
            "classification": "decisive",
            "enforced": True,
            "final_response": None,
            "safety_decision": "allow",
            "note": "directive",
            "decision_confidence": 0.83,
            "decision_source": "fast_path",
            "next_action": {"kind": "execute", "target": "ship feature", "rationale": "bounded-autonomy execution"},
            "needs_human": False,
            "latency_breakdown_ms": {"state": 2, "classify": 3, "retrieval": 10, "safety": 1, "total": 20},
            "selected_goal": {
                "id": "3bf0a9c4-df86-4cdf-b495-28bb5ea9ce9a",
                "session_id": "s1",
                "workspace_id": "personal",
                "user_id": "u1",
                "title": "ship feature",
                "description": "ship feature",
                "source": "user_objective",
                "priority_score": 0.88,
                "risk_tier": "medium",
                "confidence": 0.9,
                "reasoning": "active objective",
                "evidence_event_ids": [],
                "status": "selected",
                "created_at": "2026-02-20T00:00:00+00:00",
                "updated_at": "2026-02-20T00:00:00+00:00",
            },
            "execution_permit_required": True,
            "execution_permit_id": "4144cc85-f3f0-4a58-ac85-6ebf00c63635",
            "continuity_ok": True,
        }
    )
    assert "decision_confidence" in slim
    assert "decision_source" in slim
    assert "next_action" in slim
    assert "needs_human" in slim
    assert "latency_breakdown_ms" in slim
    assert "selected_goal" in slim
    assert "execution_permit_required" in slim
    assert "execution_permit_id" in slim
    assert "continuity_ok" in slim
