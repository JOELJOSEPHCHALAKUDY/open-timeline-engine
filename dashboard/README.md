# Open Timeline Engine Dashboard

> Public release track: `v0.3.0` (pre-1.0).
> `V9.6` references in this file are internal dashboard milestones.

Angular dashboard for Open Timeline Engine control-plane operations.

`TCE` (used in env vars and internal tool IDs) stands for **Timeline Context Engine**.

## Canonical URL

- Primary: `http://localhost:8080/dashboard/`
- Legacy fallback: `http://localhost:8080/ui` (kept for compatibility, limited features)
- Lite runtime uses the same dashboard path (`/dashboard/`) through `tce-lite-api` on the configured API port.

## What this dashboard controls

- Overview and health telemetry
- Human-level score card + trend
- Timeline search (`match_all` + filters + policy visibility)
- Takeover controls (activate/suggest/stand-down/reset/preload)
- Retrieval telemetry for takeover turns:
  - context quality score
  - retrieval trigger/source/reason
  - retrieval latency and hit count
  - retrieval backend status
- Goal intelligence page (`/goal-intelligence`) with:
  - long-term goal detection
  - goal-event-emotion relation edges
  - decision-path edge table
- Episodes page (`/episodes`) with:
  - episode list by session/status
  - authority/stability signals and recent updates
- Memory Rules page (`/memory-rules`) with:
  - create/deprecate rules
  - scoped rule visibility for boundary management
- Super Export Center page (`/export-center`) with:
  - preset-driven snapshot collection (`full`, `audit`, `autonomy`)
  - section selection across operational datasets
  - one-click export to JSON, Markdown, CSV, or all formats
- Goal intelligence and episodes now consume typed memory outputs (`semantic_fact`, `experience`, `skill`, `opinion`) produced by consolidation/reflection loops.
- Graph explorer (`/v1/graph/entities`, `/v1/graph/event/{id}`)
- Pattern review + feedback (`/v1/patterns`, `/v1/patterns/feedback`)
- Runtime/auth header settings for dashboard API calls
- Advisor route settings (Milestone V9.6):
  - primary `Local/Web` mode with explicit platform/provider steps
  - separate ordered fallback route cards
  - route-level verify and live model load
  - Ollama model pull job + progress
  - save-to-env + restart-required workflow

Additional dashboard endpoints used by intelligence panels:

- `GET /v1/dashboard/goals/intelligence`
- `GET /v1/dashboard/human-score`
- `GET /v1/dashboard/human-score/history`
- `POST /v1/dashboard/human-score/recompute`
- `GET /v1/context/retrieval/status` (retrieval backend and fallback counters)
- `GET /v1/episodes`
- `GET /v1/episodes/{episode_id}`
- `POST /v1/events/annotate`
- `GET /v1/memory/rules`
- `POST /v1/memory/rules`
- `POST /v1/memory/rules/{rule_id}/deprecate`
- `POST /v1/memory/forget`
- `POST /v1/context/brief`
- `GET /v1/retrieval/eval/status`
- `POST /v1/retrieval/eval/run`
- `POST /v1/setup/advisor/models/live`
- `POST /v1/setup/advisor/route/verify`
- `POST /v1/setup/advisor/local/ollama/pull`
- `GET /v1/setup/advisor/local/ollama/pull/{job_id}`
- `POST /v1/dashboard/stack/restart`
- `GET /v1/dashboard/stack/restart/{restart_id}`

## Local development

```bash
cd dashboard
npm install
npm run start
```

Dev server runs on `http://localhost:4200`.

## Build

```bash
cd dashboard
npm run build
```

Production output is copied into API images and served from `/dashboard/`.

## Tests

Unit:

```bash
cd dashboard
npm test
```

Integration/E2E from repo root:

```bash
pytest tests/integration/test_dashboard_api.py
```

```bash
npx playwright test tests/e2e/dashboard.spec.ts
```

## Related docs

- Root quickstart: `../README.md`
- Setup guide: `../docs/setup.md`
- Docs index: `../docs/README.md`

## Current dashboard coverage and gaps

### Implemented

- Takeover page shows learned workflow hints.
- Dedicated workflow templates page (`/workflow-templates`) for learned template inspection.
- Dedicated retrieval/context page (`/retrieval-context`) for:
  - context brief generation
  - retrieval eval run/status/history
  - retrieval backend status and fallback counters
- Settings supports advisor route configuration, verify/probe, save, restart-required flow, and in-dashboard route probe history (success rate, p50/p95 latency, circuit state, recent attempts).
- Super Export Center (`/export-center`) supports multi-source data export with preset and format controls for ops handoff/audit snapshots.
