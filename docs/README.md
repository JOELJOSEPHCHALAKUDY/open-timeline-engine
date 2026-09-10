# Open Timeline Engine Docs

> Public release track: `v0.4.0` (pre-1.0).
> `V4`–`V9.x` labels in this docs set are internal milestones.

This is the canonical documentation index for Open Timeline Engine.

## Start here

1. Install and run in minutes: [setup.md](setup.md)
2. Choose your mode:
   - Timeline only: [setup.md](setup.md)
   - Clone advisor: [clone-advisor.md](clone-advisor.md)
3. Connect your MCP client: [mcp-setup-walkthrough.md](mcp-setup-walkthrough.md)

## Dashboard control plane

- User dashboard guide: [../dashboard/README.md](../dashboard/README.md)
- Primary URL: `http://localhost:8080/dashboard/`
- Compatibility fallback: `http://localhost:8080/ui`
- Goal intelligence page: `http://localhost:8080/dashboard/goal-intelligence`
- Retrieval telemetry on takeover page:
  - context quality score
  - retrieval trigger/source/reason
  - retrieval latency/hit count
  - backend retrieval status

## Install and setup

- Main setup guide: [setup.md](setup.md)
- One-command install/start/doctor flow: [setup.md](setup.md)
- Lightweight vs full stack choices: [setup.md](setup.md)

## Runtime modes

- Clone advisor mode operations: [clone-advisor.md](clone-advisor.md)
- MCP clients and compatibility notes: [mcp-clients.md](mcp-clients.md)

## MCP client setup

- Step-by-step walkthrough: [mcp-setup-walkthrough.md](mcp-setup-walkthrough.md)
- Client config reference: [mcp-clients.md](mcp-clients.md)

## Capture plugins

- CLI capture plugin: [../plugins/tce_cli_capture/README.md](../plugins/tce_cli_capture/README.md)
- Git capture plugin: [../plugins/tce_git_capture/README.md](../plugins/tce_git_capture/README.md)
- VSCode extension: [../plugins/tce_vscode/README.md](../plugins/tce_vscode/README.md)
- Browser extension: [../plugins/tce_browser/README.md](../plugins/tce_browser/README.md)
- Host capture hook (trusted human input, separate credential, the only source of receipted human evidence): [plugin-setup.md §4](plugin-setup.md#4-host-capture-hook-trusted-human-input)

## Autonomy and governance

Read [charter.md](charter.md) first — the other documents in this group assume it.

- **Authority charters**: what an autonomous run is permitted to do, the three enforcement tiers, and a control-by-control table of what the operating system enforces, what TCE refuses, what is merely cooperative, and what is not enforced at all — including the eleven stated deviations this host cannot close. [charter.md](charter.md)
- **The supervisor**: how to run the separate host process that dispatches, watches, verifies and reconciles an autonomous runtime; why it is deliberately not a compose service; the two distinct credentials it needs; and the operator escape hatches for a stuck effect or a dead handoff. [supervisor.md](supervisor.md)
- **Task state, planning and retrieval deadlines**: the append-only log and the projection folded from it, why a 409 under concurrency is expected, what an executor must do while planning is pending, and what the new deadline labels mean on a dashboard. [task-state.md](task-state.md)
- **The decision policy**: how one turn's answer is selected, when it abstains, how a decision family earns (and loses) permission to use personalization, and why nothing in this system is calibrated. [decision-policy.md](decision-policy.md)
- **Aspiration proposals**: what a proposal is, why every word in one must be quoted from a message the owner is receipted as having typed, the accept/reject/snooze vocabulary, and why the system refuses to generate any on the current corpus. [dreams.md](dreams.md)

## Clone and advisor operations

- Dual-AI behavior, setup, and troubleshooting: [clone-advisor.md](clone-advisor.md)
- Takeover behavior notes: [takeover-scenarios.md](takeover-scenarios.md)
- Retrieval status endpoint: `GET /v1/context/retrieval/status`
- Retrieval policy visibility:
  - default: `policy_profile=user-only`
  - explicit/eligible cross-user queries: `policy_profile=workspace-shared`

## Meaningful memory (Milestone V8)

- Episode abstraction and annotation:
  - `POST /v1/events/annotate`
  - `GET /v1/episodes`
  - `GET /v1/episodes/{episode_id}`
- Deterministic context composer:
  - `POST /v1/context/brief`
- Negative memory / boundary rules:
  - `GET /v1/memory/rules`
  - `POST /v1/memory/rules`
  - `POST /v1/memory/rules/{rule_id}/deprecate`
  - `POST /v1/memory/forget`
- Retrieval evaluation harness:
  - `GET /v1/retrieval/eval/status`
  - `POST /v1/retrieval/eval/run`
  - script: `scripts/run_eval_suite.sh`

## Graph and pattern features

- Graph features and relationships: [graph.md](graph.md)
- Pattern extraction and confidence behavior: [clone-advisor.md](clone-advisor.md)

## Runbooks

- Backup and restore: [runbooks/backup-restore.md](runbooks/backup-restore.md)
- Disaster recovery: [runbooks/disaster-recovery.md](runbooks/disaster-recovery.md)
- Operational proof pilot: enrolling and closing episodes, adjudicating proposals, and reading a report that says `NOT ENOUGH EVIDENCE` honestly rather than rounding up: [runbooks/operational-proof-pilot.md](runbooks/operational-proof-pilot.md)

## API and OpenAPI

- OpenAPI references: [openapi/README.md](openapi/README.md)
- Release notes and compatibility: [releases.md](releases.md)

## Security

- Threat model and security notes: [threat-model/README.md](threat-model/README.md)
- Enforcement tiers, the eleven stated deviations, and the credential a mutating sandbox holds (D-10): [charter.md](charter.md)
- Supervisor credential separation — two distinct tokens, why a verifier that grades its own work returns `inconclusive`: [supervisor.md](supervisor.md)
- Trusted human-input capture and its separate host credential: [plugin-setup.md §4](plugin-setup.md#4-host-capture-hook-trusted-human-input)
- Measured rather than claimed, for your own installation: `GET /v1/governance/status`

## Historical and internal plans

`docs/plans/*` contains historical implementation plans and validation logs.  
Treat these as internal history, not primary onboarding docs.

## Project root

Back to root landing page: [../README.md](../README.md)

## Current implementation notes

- Milestone V7.2 takeover includes `workflow_hints` in MCP takeover outputs when applicable.
- Full and lite both expose `GET /v1/workflow/templates`.
- Dashboard currently has partial workflow-memory UX (takeover card), with additional workflow/retrieval UI still pending.
