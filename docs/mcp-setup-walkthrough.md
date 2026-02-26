# MCP Setup Walkthrough

> Public release track: `v0.3.0` (pre-1.0).
> `V7.2`/`V8`/`V9.x` terms in this walkthrough are internal milestones.

This walkthrough configures Open Timeline Engine MCP for Codex Desktop, Claude Desktop, Cursor, and generic MCP clients.

## 1. Start Open Timeline Engine

Default full stack:

```bash
./scripts/start.sh full --detach
```

Optional lightweight:

```bash
./scripts/start.sh lite --detach
```

## 2. Pick config pack

Fast path (auto-generate + install):

```bash
./scripts/configure_mcp_clients.sh --client all
```

You can select multiple executor clients explicitly:

```bash
./scripts/configure_mcp_clients.sh --client codex,claude,cursor
```

For Lovable, use generic MCP output:

```bash
./scripts/configure_mcp_clients.sh --client generic --no-install
```

Manual templates:

- `<repo-root>/docs/mcp-config/codex_desktop_mcp.json`
- `<repo-root>/docs/mcp-config/claude_desktop_config.json`
- `<repo-root>/docs/mcp-config/cursor_mcp.json`
- `<repo-root>/docs/mcp-config/generic_mcp.json`

Replace:

- `/ABSOLUTE/PATH/TO/open-timeline-engine`
- `TCE_MCP_USER_ID`

If you use Docker-managed MCP services, assign roles once with:

```bash
./scripts/assign_ai_roles.sh
```

This updates `.env` so users can configure multiple MCP executors while keeping advisor routing in API configuration.

Generic MCP clients:

```bash
./scripts/configure_mcp_clients.sh --client generic --no-install
```

Then import `<repo-root>/docs/mcp-config/generated/generic_mcp.json` into your client.

If you change `.env` values that affect API runtime behavior (timeouts, retrieval flags, etc.), recreate API container so new env is loaded:

```bash
docker compose -f infra/docker-compose.yml up -d --force-recreate tce-api
```

## 3. Configure identities

MCP executor identity:

- `TCE_MCP_ROLE=executor`
- consumer id like `codex-executor` or `claude-executor`
- set distinct `TCE_MCP_USER_ID` per executor (usually same value as consumer id)

Shared values:

- `TCE_API_BASE_URL=http://localhost:8080`
- `TCE_API_TOKEN=local-dev-token`
- `TCE_MCP_WORKSPACE_ID=personal`

Note:

- MCP executors do not magically call a cloud advisor model by themselves.
- Advisor model routing is configured in TCE with `/v1/setup/advisor/*`.
- You can configure one primary advisor provider plus fallback chain, including China providers and custom OpenAI-compatible gateways.
- Local advisor flow: pull local Ollama model first, then verify through `/v1/setup/advisor/verify`.

## 4. Validate tool discovery

Expected tool set includes:

- timeline tools (`search_events`, `get_context_bundle`, `get_patterns`, `record_event`)
- clone tools (`get_clone_advice`, `arbitrate_with_clone`, mode controls)
- graph/team tools (`search_entities`, `get_event_graph`, `get_team_memberships`)
- summary tool (`get_activity_summary`)
- takeover tools (`takeover_step`, `get_takeover_state`, `reset_takeover_state`)
- autonomy tools (`takeover_discover_goals`, `get_takeover_goals`, `select_takeover_goal`)
- goal cache tools (`takeover_precompute_goals`, `get_takeover_goal_cache_status`, `invalidate_takeover_goal_cache`)
- permit tools (`request_execution_permit`, `resolve_execution_permit`, `get_autonomy_status`)
- autonomy execution tools (`takeover_autonomy_tick`, `get_takeover_notices`, `ack_takeover_notice`, `claim_execution`, `report_execution`, `get_execution_status`)
- guard + learning tools (`check_context`, `ingest_observations`)
- meaningful memory tools (`get_context_brief`, `annotate_event`, `get_episodes`, `get_episode`)
- rule tools (`get_memory_rules`, `upsert_memory_rule`, `deprecate_memory_rule`, `forget_memory`)
- eval tools (`get_retrieval_eval_status`, `run_retrieval_eval`)

## 5. Recommended first run

1. Set mode: `clone_advisor`
2. Ask executor for a task bundle
3. Ask any executor lane for clone guidance using same `interaction_id`
4. Arbitrate if needed

Detailed operation policy: `<repo-root>/docs/clone-advisor.md`

## 6. Takeover tool-calling pattern

1. First call `tce.takeover_step` with the user message and session id.
2. If result action is `inactive` or `stopped`, do not call clone-advice tools.
3. If result action is `advisor_takeover` or `advisor_suggest`, use `final_response` as baseline.
4. Use `tce.get_takeover_state` for diagnostics and `tce.reset_takeover_state` to clear session state.
5. For mutating tasks in takeover mode, request permit first with `tce.request_execution_permit`.
6. Resolve human confirmation with `tce.resolve_execution_permit` and continue takeover.
7. For mutating directives, call `tce.claim_execution` before edits, then `tce.report_execution` after outcome.
8. Pull proactive cues with `tce.get_takeover_notices`; optionally run `tce.takeover_autonomy_tick` on schedule.
9. For low-latency sessions, run `tce.takeover_precompute_goals` at activation and verify freshness with `tce.get_takeover_goal_cache_status`.

## 7. Milestone V8 memory-oriented invocation patterns

1. After important decisions, call `tce.annotate_event` with `decision` and `avoid` fields.
2. Pull `tce.get_episodes` to inspect current memory abstraction instead of raw event-only timelines.
3. Use `tce.get_context_brief` before multi-step execution for compact rules + citations.
4. Manage constraints with `tce.upsert_memory_rule` / `tce.deprecate_memory_rule`.
5. Run `tce.run_retrieval_eval` and inspect `tce.get_retrieval_eval_status` to track memory quality over time.

## 8. Milestone V9 advisor provider routing

Use dashboard Settings or API:

1. `GET /v1/setup/advisor/providers` to discover provider pack.
2. `GET /v1/setup/advisor/models?provider=...` to list model ids.
3. `POST /v1/setup/advisor/verify` to validate key/model/base_url.
4. `PUT /v1/setup/advisor/config` to persist primary + fallback chain.

Provider categories:

- `global`: OpenAI, Anthropic, Gemini, OpenRouter, Groq, Together, xAI
- `china`: DeepSeek, DashScope, Zhipu, Moonshot, Qianfan, Hunyuan
- `custom`: any OpenAI-compatible hosted endpoint

## 9. Milestone V9.4 runtime profile operations

After initial setup, use these runtime endpoints for low-friction switching and health checks:

1. `POST /v1/setup/advisor/switch` to change active advisor profile.
2. `GET /v1/setup/advisor/runtime/status` to inspect route scoring, circuit state, and category coverage.
3. `POST /v1/setup/advisor/runtime/probe` to actively verify selected routes and refresh health state.

Notes:

- routing remains category-agnostic (`global`, `china`, `custom` treated equally by the router)
- legacy primary/fallback config remains backward compatible
- profile switch is additive and does not require API restart

## 10. Milestone V9.6 dashboard restart-required behavior

Advisor and executor config changes are persisted to `.env` first and then require restart to apply runtime state.

Dashboard routes:

- save advisor routes: `PUT /v1/setup/advisor/config`
- save executor config: `PUT /v1/dashboard/client-config/executor`
- restart stack: `POST /v1/dashboard/stack/restart`
- restart status: `GET /v1/dashboard/stack/restart/{restart_id}`

Local/web advisor route helpers:

- `POST /v1/setup/advisor/models/live` (live model listing)
- `POST /v1/setup/advisor/route/verify` (route-level verify)
- `POST /v1/setup/advisor/local/ollama/pull` (Ollama pull)
- `GET /v1/setup/advisor/local/ollama/pull/{job_id}` (pull progress)
> Docs index: [Open Timeline Engine Docs](README.md)
>
> Recommended flow: open docs index first, then follow this walkthrough step by step.

## Milestone V7.2 behavior note

- During active takeover, MCP clients may receive `workflow_hints` alongside directive context.
- Clients should treat hints as guidance only and must still follow safety decisions, constraints, and execution lifecycle requirements.
