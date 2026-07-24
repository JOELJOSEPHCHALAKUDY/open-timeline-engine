from tce_mcp import server


def test_codex_tools_present():
    tool_names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "tce.search_events" in tool_names
    assert "tce.get_context_bundle" in tool_names
    assert "tce.get_patterns" in tool_names
    assert "tce.get_mode" in tool_names
    assert "tce.get_clone_advice" in tool_names
    assert "tce.takeover_step" in tool_names
    assert "tce.get_takeover_state" in tool_names
    assert "tce.reset_takeover_state" in tool_names
    assert "tce.search_entities" in tool_names
    assert "tce.get_event_graph" in tool_names
    assert "tce.get_team_memberships" in tool_names
    assert "tce.get_activity_summary" in tool_names
