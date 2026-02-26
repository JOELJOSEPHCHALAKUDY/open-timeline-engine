from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_configure_mcp_clients_generates_generic_pack(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "configure_mcp_clients.sh"
    assert script.exists(), "configure_mcp_clients.sh is required"

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["CODEX_HOME"] = str(tmp_path / ".codex")

    cmd = [
        "bash",
        str(script),
        "--client",
        "claude,codex,cursor,generic",
        "--no-install",
        "--api-url",
        "http://localhost:8080",
        "--token",
        "test-token",
        "--workspace",
        "personal",
        "--user-id",
        "tester",
    ]
    completed = subprocess.run(
        cmd,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "MCP client configuration complete." in completed.stdout

    generated_dir = root / "docs" / "mcp-config" / "generated"
    expected_files = {
        "claude_desktop_config.json",
        "codex_desktop_mcp.json",
        "cursor_mcp.json",
        "generic_mcp.json",
    }
    for file_name in expected_files:
        assert (generated_dir / file_name).exists(), f"missing generated config: {file_name}"

    generic = _load_json(generated_dir / "generic_mcp.json")
    servers = generic.get("mcpServers", {})
    assert "tce-executor" in servers
    assert "tce-advisor" not in servers
    assert servers["tce-executor"]["env"]["TCE_MCP_CONSUMER_ID"] == "generic-executor"
    assert servers["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "generic-executor"
    assert servers["tce-executor"]["env"]["TCE_MCP_SESSION_ID"] == "generic"
    assert servers["tce-executor"]["env"]["TCE_API_TOKEN"] == "test-token"

    claude = _load_json(generated_dir / "claude_desktop_config.json")
    codex = _load_json(generated_dir / "codex_desktop_mcp.json")
    cursor = _load_json(generated_dir / "cursor_mcp.json")
    assert claude["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "claude-executor"
    assert codex["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "codex-executor"
    assert cursor["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "cursor-executor"


def test_configure_mcp_clients_honors_identity_and_session_maps(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "configure_mcp_clients.sh"
    assert script.exists(), "configure_mcp_clients.sh is required"

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["CODEX_HOME"] = str(tmp_path / ".codex")

    cmd = [
        "bash",
        str(script),
        "--client",
        "claude,codex,cursor,generic",
        "--no-install",
        "--api-url",
        "http://localhost:8080",
        "--token",
        "test-token",
        "--workspace",
        "personal",
        "--identity-map",
        "claude=claude-executor,codex=codex-executor,cursor=cursor-executor,generic=generic-executor",
        "--session-map",
        "claude=claude,codex=codex,cursor=cursor,generic=generic",
    ]
    completed = subprocess.run(
        cmd,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "MCP client configuration complete." in completed.stdout

    generated_dir = root / "docs" / "mcp-config" / "generated"
    claude = _load_json(generated_dir / "claude_desktop_config.json")
    codex = _load_json(generated_dir / "codex_desktop_mcp.json")
    cursor = _load_json(generated_dir / "cursor_mcp.json")
    generic = _load_json(generated_dir / "generic_mcp.json")

    assert claude["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "claude-executor"
    assert codex["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "codex-executor"
    assert cursor["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "cursor-executor"
    assert generic["mcpServers"]["tce-executor"]["env"]["TCE_MCP_USER_ID"] == "generic-executor"

    assert claude["mcpServers"]["tce-executor"]["env"]["TCE_MCP_SESSION_ID"] == "claude"
    assert codex["mcpServers"]["tce-executor"]["env"]["TCE_MCP_SESSION_ID"] == "codex"
    assert cursor["mcpServers"]["tce-executor"]["env"]["TCE_MCP_SESSION_ID"] == "cursor"
    assert generic["mcpServers"]["tce-executor"]["env"]["TCE_MCP_SESSION_ID"] == "generic"
