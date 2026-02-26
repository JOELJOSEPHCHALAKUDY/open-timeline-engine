from tce_mcp import server


def test_cursor_tools_present() -> None:
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
        "tce.ingest_observations",
        "tce.search_entities",
        "tce.get_event_graph",
        "tce.get_team_memberships",
        "tce.get_activity_summary",
        "tce.check_context",
        "tce.takeover_step",
        "tce.takeover_preload",
        "tce.takeover_feedback",
        "tce.get_takeover_state",
        "tce.reset_takeover_state",
    }
    assert expected.issubset(tool_names)
