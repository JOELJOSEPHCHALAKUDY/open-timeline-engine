# Setup

> Public release track: `v0.4.0` (pre-1.0).
> `V9`/`V9.4`/`V9.6` terms in this guide are internal milestones.

> Docs index: [Open Timeline Engine Docs](README.md)

## Fast path (recommended)

```bash
cp .env.example .env
./scripts/install.sh
./scripts/start.sh full --detach
./scripts/doctor.sh full
```

## Two first-time paths

`install.sh` now supports two first-time setup paths:

1. `Setup here` (default): runs the existing interactive wizard.
2. `I already configured .env, just start stack`: skips setup prompts and starts from existing `.env`.

Interactive usage:

```bash
./scripts/install.sh
```

Non-interactive env-first start:

```bash
./scripts/install.sh install full --setup-mode env --yes
```

Reviewed runtime profiles are available through `--profile local-lite|local-full|team-secure|research`. Timeline-only installs expose the 10-tool MCP `core` profile. A wizard install with `--behavior clone_advisor` selects the `autonomy` MCP profile; `research` is the broader evaluation profile. After changing `TCE_MCP_TOOL_PROFILE`, regenerate client config and restart the executor:

```bash
./scripts/configure_mcp_clients.sh --client all
```

If `.env` is missing in env-first mode, installer will prompt to create it from `.env.example` (or abort).

For clone-advisor setups, `install.sh` now captures executor MCP identity plus advisor API routing:

- choose one or more executor clients (`codex`, `claude`, `cursor`, `lovable`)
- configure MCP for selected executors (Lovable uses generated generic MCP config)
- choose advisor provider category (`local`, `global`, `china`, `custom`)
- choose primary model
- optional fallback chain
- optional custom OpenAI-compatible `base_url`
- verify advisor provider credentials before final save
- for `local` advisor, pull local Ollama model before verification

Lite runtime:

```bash
./scripts/start.sh lite --detach
./scripts/doctor.sh lite
```

## Dashboard access

- Canonical dashboard: `http://localhost:8080/dashboard/`
- Legacy fallback: `http://localhost:8080/ui` (minimal, compatibility-focused)

## Dashboard restart behavior (containerized full runtime)

- `Save advisor config` and `Save executor config` write host `.env` first.
- `Restart now` in dashboard calls `POST /v1/dashboard/stack/restart`.
- In full containerized runtime, restart is executed through Docker Engine API (no manual `install.sh` required).
- During restart, API may briefly reconnect; dashboard polling handles this transient window automatically.

If you changed runtime env values in `.env` (for example embedding timeout knobs), recreate API instead of plain restart so containers load new env:

```bash
docker compose --env-file .env -f infra/docker-compose.yml up -d --force-recreate tce-api
```

## Docker compose path

```bash
docker compose --env-file .env -f infra/docker-compose.yml up --build
```

The compose stack includes two MCP services by default:

- `tce-mcp` (`executor` identity)
- `tce-mcp-secondary` (additional `executor` identity endpoint for another client)

## Host install path

1. Install dependencies with scripts under `scripts/install`.
2. Install Python packages:

```bash
pip install -e shared -e services/tce_api -e services/tce_worker -e services/tce_mcp
```

3. Run migrations:

```bash
./scripts/run_migrations.sh
```

4. Start API, worker, MCP from `Makefile` targets.

## End-to-end verification

```bash
./scripts/e2e_smoke.sh
```

Lite mode verification:

```bash
./scripts/e2e_lite_smoke.sh
```

## Feature-specific guides

- Graph features: [graph.md](graph.md)
- Clone advisor: [clone-advisor.md](clone-advisor.md)
- Team/workspace: [team-workspace.md](team-workspace.md)
- MCP setup walkthrough: [mcp-setup-walkthrough.md](mcp-setup-walkthrough.md)

## Correct role flow (Milestone V9)

1. Executors do the work. You can select multiple local executors.
2. Install MCP server config for each selected executor client.
3. Configure exactly one primary advisor provider/model (local or cloud).
4. Optionally configure fallback chain.
5. Verify advisor connectivity and credentials before final save.
6. Run takeover/clone mode with executor MCP clients plus advisor API routing.

## Milestone V9.4 profile routing and category parity

Runtime routing now uses advisor profiles with baseline category coverage (`global`, `china`, `custom`).

Primary APIs:

- `GET /v1/setup/advisor/providers`
- `GET /v1/setup/advisor/models?provider=<id>`
- `POST /v1/setup/advisor/verify`
- `PUT /v1/setup/advisor/config`
- `POST /v1/setup/advisor/switch`
- `GET /v1/setup/advisor/runtime/status`
- `POST /v1/setup/advisor/runtime/probe`

Expected behavior:

1. Configure routes as provider/model tuples.
2. Keep one active profile per workspace/user.
3. Switch profile without restart.
4. Use runtime status/probe for health checks and preflight validation.

## Advisor provider setup API

Use these endpoints from dashboard or scripts:

- `GET /v1/setup/advisor/providers`
- `GET /v1/setup/advisor/models?provider=<id>`
- `POST /v1/setup/advisor/verify`
- `PUT /v1/setup/advisor/config`

Important:

- Provider API keys are never returned in API responses.
- Full stack uses keychain-first storage when available.
- Lite stores config locally for local-runtime use.

## Memory-system readiness checklist (post-setup)

After stack startup, confirm memory loops are healthy:

1. Embeddings:
   - tune `TCE_SEARCH_EMBEDDING_QUICK_TIMEOUT_SECONDS` and `TCE_SEARCH_EMBEDDING_TIMEOUT_HARD_CAP_SECONDS` together.
   - for local CPU Ollama, start with `15s..30s` to avoid forced lexical-only fallback on uncached queries.
2. Identity:
   - keep one shared `X-TCE-Workspace` when you want shared memory across executors.
   - use distinct `X-TCE-User` values per executor (`codex-executor`, `claude-executor`, etc.) so ownership/audit stay correct.
   - use the same `X-TCE-Behavior-Subject` only when those executors assist the same human.
   - use explicit cross-user phrasing when querying another executor's memory.
3. Situation taxonomy:
   - feedback/observation situation types are canonicalized (`routine_task`, `choice_required`, `error_occurred`, etc.).
4. Event flow:
   - ensure takeover/feedback auto-capture is enabled so real task events accumulate beyond seed data.
5. Reflection/consolidation:
   - keep `TCE_REFLECTION_ENABLED=true` and `TCE_SEMANTIC_CONSOLIDATION_ENABLED=true`.

## Dashboard advisor route workflow (Milestone V9.6)

Use dashboard `Settings` for explicit advisor route setup:

1. Configure primary route with mode-first selection:
   - `Local`: choose `Ollama` or `LM Studio`
   - `Web`: choose provider (global/china/custom), then key, then model
2. Verify primary route before save.
3. Configure fallback routes in separate ordered cards, and verify each route.
4. Save advisor config (writes `.env` first).
5. Restart stack from dashboard when prompted.

Endpoints behind this flow:

- `POST /v1/setup/advisor/models/live`
- `POST /v1/setup/advisor/route/verify`
- `POST /v1/setup/advisor/local/ollama/pull`
- `GET /v1/setup/advisor/local/ollama/pull/{job_id}`
- `POST /v1/dashboard/stack/restart`
- `GET /v1/dashboard/stack/restart/{restart_id}`

Notes:

- `LM Studio` is list+verify only in this release.
- `Ollama` supports pull jobs from dashboard.
- Save does not auto-restart; restart is explicit user action.

## Plugin setup

Install plugin entrypoints locally from the repo root:

```bash
python3 -m pip install -e plugins/tce_cli_capture -e plugins/tce_git_capture
npm run -w plugins/tce_vscode build
npm run -w plugins/tce_browser build
```

Plugin docs:

- CLI capture: `plugins/tce_cli_capture/README.md`
- Git capture: `plugins/tce_git_capture/README.md`
- VSCode extension: `plugins/tce_vscode/README.md`
- Browser extension: `plugins/tce_browser/README.md`

## Plugin auth troubleshooting

If capture commands queue instead of sending, check identity headers first.

- Symptom: `commit queued`, `note queued`, or API `403 workspace access denied`
- Cause: plugin `X-TCE-User` / `X-TCE-Workspace` does not match allowed workspace membership
- Fix: set plugin env/config to a workspace-authorized executor identity, then replay queued events.

## Clarification: workflow memory visibility

- Setup does not require extra flags for workflow hints.
- Once workflow templates exist, takeover responses can include `workflow_hints` automatically.
- Current dashboard exposure is in takeover view; dedicated workflow management page is still pending.
## Server-Bound Identities

For production, use distinct executor tokens and bind them to server-owned identity and workspace claims. Set `TCE_IDENTITY_CLAIMS_MODE=enforce` and map `bearer:<sha256-token>` or `mtls:<sha256-subject>` keys in `TCE_IDENTITY_CLAIMS_JSON`. `compat` preserves legacy header assertions during migration.

Verify each credential with `GET /v1/auth/whoami`. In enforce mode, callers cannot override a bound workspace, user, role, or behavior subject using `X-TCE-*` headers.
