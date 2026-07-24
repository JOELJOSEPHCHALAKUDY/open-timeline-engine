from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from tce_mcp.tool_profiles import (
    CORE_TOOLS,
    apply_tool_profile,
    exposed_tool_names,
    normalize_tool_profile,
)


def _server() -> FastMCP:
    server = FastMCP("profile-test")
    for name in sorted(CORE_TOOLS | {"tce.admin_only", "tce.autonomy_only"}):
        server.tool(name=name)(lambda: None)
    return server


def test_unknown_profile_fails_closed_to_core() -> None:
    assert normalize_tool_profile("not-a-profile") == "core"
    exposed = exposed_tool_names("not-a-profile", CORE_TOOLS | {"tce.admin_only"})
    assert exposed == CORE_TOOLS
    assert len(exposed) <= 10


def test_core_profile_removes_non_core_tools() -> None:
    server = _server()

    status = apply_tool_profile(server, "core")

    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == CORE_TOOLS
    assert status["effective_profile"] == "core"
    assert status["exposed_count"] == len(CORE_TOOLS)
    assert "tce.admin_only" in status["removed_tools"]


def test_all_and_admin_preserve_complete_surface() -> None:
    for profile in ("all", "admin"):
        server = _server()
        before = {tool.name for tool in server._tool_manager.list_tools()}

        status = apply_tool_profile(server, profile)

        assert {tool.name for tool in server._tool_manager.list_tools()} == before
        assert status["removed_tools"] == []
