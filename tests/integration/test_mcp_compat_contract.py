from tce_mcp import server


def test_mcp_contract_tools_are_exposed() -> None:
    tool_names = {tool.name for tool in server.mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    expected = {
        "tce.search_events",
        "tce.get_context_bundle",
        "tce.get_patterns",
        "tce.record_event",
        "tce.get_mode",
        "tce.set_mode",
        "tce.get_clone_advice",
        "tce.arbitrate_with_clone",
        "tce.takeover_step",
        "tce.takeover_preload",
        "tce.takeover_feedback",
        "tce.get_takeover_state",
        "tce.reset_takeover_state",
        "tce.search_entities",
        "tce.get_event_graph",
        "tce.get_team_memberships",
        "tce.get_activity_summary",
    }
    assert expected.issubset(tool_names)
