# MCP Compatibility

Target clients:

- Codex Desktop
- Claude Desktop
- Cursor
- Generic MCP clients

## Compatibility contract

- Stable tool names: `tce.search_events`, `tce.get_context_bundle`, `tce.get_patterns`, `tce.record_event`, `tce.get_mode`, `tce.set_mode`, `tce.get_clone_advice`, `tce.arbitrate_with_clone`
- Tool responses include `tool_schema_version`
- Evidence-backed claims include `citations`
- Returned payloads are redaction-safe

## Verification coverage

- Codex tool presence: `tests/mcp/test_codex_contract.py`
- Claude tool presence: `tests/mcp/test_claude_contract.py`
- Cursor tool presence: `tests/mcp/test_cursor_contract.py`
- Generic baseline compatibility: `tests/integration/test_mcp_compat_contract.py`
- Config generation for Codex/Claude/Cursor/Generic: `tests/integration/test_mcp_config_generation.py`
