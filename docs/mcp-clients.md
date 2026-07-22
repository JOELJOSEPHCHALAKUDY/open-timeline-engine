# MCP Client Setup Packs

Open Timeline Engine MCP tools are client-agnostic. Full stack is the default runtime; lite remains optional.

`TCE` naming in tool IDs and headers is kept for compatibility and refers to **Timeline Context Engine**.

Detailed walkthrough:

- `<repo-root>/docs/mcp-setup-walkthrough.md`

## 1. Start Open Timeline Engine

```bash
./scripts/start.sh
```

For lite mode:

```bash
./scripts/start.sh lite
```

## 2. Copy-paste client configs

Auto-configure from script:

```bash
./scripts/configure_mcp_clients.sh --client all
```

Use these example packs and replace `/ABSOLUTE/PATH/TO/open-timeline-engine` with your local path.

- `docs/mcp-config/claude_desktop_config.json`
- `docs/mcp-config/codex_desktop_mcp.json`
- `docs/mcp-config/cursor_mcp.json`
- `docs/mcp-config/generic_mcp.json`

For generic MCP clients that do not have an installer path, generate config only:

```bash
./scripts/configure_mcp_clients.sh --client generic --no-install
```

## 3. Common environment values

- `TCE_API_BASE_URL=http://localhost:8080`
- `TCE_API_TOKEN=local-dev-token`
- `TCE_MCP_WORKSPACE_ID=personal`
- `TCE_MCP_USER_ID=<your-user-id>`
- `TCE_MCP_BEHAVIOR_SUBJECT_ID=<human-profile-id>`

Dual-AI mode:

- MCP servers use executor identity: `TCE_MCP_ROLE=executor`
- advisor model routing remains API-side via `/v1/setup/advisor/*`

## 4. Tool schemas and compatibility

All tool responses include `tool_schema_version` from `tce_shared.version.MCP_SCHEMA_VERSION`.

Supported tools:

- `tce.search_events`
- `tce.get_context_bundle`
- `tce.get_patterns`
- `tce.record_event`
- `tce.get_mode`
- `tce.set_mode`
- `tce.get_clone_advice`
- `tce.arbitrate_with_clone`
- `tce.search_entities`
- `tce.get_event_graph`
- `tce.get_team_memberships`
- `tce.get_activity_summary`
- `tce.complete_task`
- `tce.get_resume_packet`
- `tce.report_resume_feedback`
- `tce.get_continuity_pilot`
- `tce.record_behavior_evidence`
- `tce.predict_behavior`
- `tce.run_behavior_fidelity_eval`
- `tce.get_behavior_fidelity`
- `tce.request_capability_grant`
- `tce.consume_capability_grant`
- `tce.mine_behavior_processes`
- `tce.get_behavior_processes`
- `tce.get_behavior_shadow_status`
- `tce.get_behavior_memory_reviews`
- `tce.resolve_behavior_memory_review`
- `tce.record_behavior_counterfactual`
- `tce.get_behavior_counterfactuals`
- `tce.resolve_behavior_counterfactual`

Validation coverage:

- Codex contract: `tests/mcp/test_codex_contract.py`
- Claude contract: `tests/mcp/test_claude_contract.py`
- Cursor contract: `tests/mcp/test_cursor_contract.py`
- Generic compatibility baseline: `tests/integration/test_mcp_compat_contract.py`
- Real Full/MCP transport continuity: `tests/e2e/test_mcp_continuity_live.py`
> Docs index: [Open Timeline Engine Docs](README.md)
>
> Use this page for client-specific MCP details after the docs index overview.

## Client handling update (V7.2)

- If takeover result includes `workflow_hints`, consume them as optional execution guidance.
- Do not let hints bypass `next_step`, `safety_decision`, or execution permit/claim/report requirements.
