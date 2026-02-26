from tce_shared.version import MCP_SCHEMA_VERSION


def test_mcp_schema_version_constant():
    assert MCP_SCHEMA_VERSION == "2026-02-20"
