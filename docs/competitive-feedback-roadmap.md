# Competitive Feedback Roadmap

This document tracks external product feedback and how Open Timeline Engine responds.

## v0.3.0 update (historical)

- Completed hardening pass for reliability and setup safety:
- Lazy DB engine initialization in full API, retry/backoff in MCP client, and corrected CI load-test gating.
- CORS is now configurable for full and lite APIs.
- Lite SQLite mode improved for lock contention (`busy_timeout` + connection timeout).
- Validation completed in release flow: unit, security, integration, and load-smoke checks passing.
- Still pending in dedicated follow-up: repo-wide lint/mypy baseline cleanup.

## Differentiators (current)

- Timeline-first context model (event narrative over time, not just fact memory).
- Multi-source developer capture (CLI, Git, VSCode, Browser).
- Dual-AI executor/advisor orchestration with arbitration and loop guards.
- Pattern mining from workflow history.

## Gaps and planned response

1. Graph-based relationships between events
- Status: done (baseline).
- Delivered: `entity_nodes`, `event_entity_links`, `event_relationships`, `fact_assertions` with graph extraction on ingest and graph-aware search/bundles.
- Next: richer semantic entity extraction (LLM-assisted optional mode).

2. Cross-client MCP memory sharing
- Status: in progress.
- Done: MCP server is client-agnostic, role-aware, and supports parallel clients.
- Next: publish client setup packs for Codex Desktop, Claude Desktop, Cursor/Cline-style configurations.

3. Lightweight mode
- Status: done.
- Delivered: separate lightweight solution (`infra/docker-compose.lite.yml`) with SQLite-backed `tce_lite_api`.

4. Memory decay and conflict resolution
- Status: in progress.
- Done: validation job now applies recency decay to pattern confidence.
- Next: contradiction markers that preserve historical trail.

5. User dashboard / UI
- Status: done (v1 baseline).
- Delivered: built-in `/ui` timeline dashboard for events, patterns, and context bundles.
- Next: richer filtering, pattern review controls, and redaction management UX.

6. Faster onboarding and instant value
- Status: in progress.
- Done: cold-start bundle behavior and starter pattern seeding.
- Done: git history bootstrap importer (`scripts/import_git_history.py`).
- Next: "what changed today/this week" summaries.

7. Structured context bundle schemas
- Status: done (baseline).
- Delivered: `structured_context` section in bundle responses with time window + graph snapshot.
- Next: stronger typed sub-schemas per context section.

8. Multi-user / team support
- Status: in progress.
- Done: workspace/user scoped auth headers, event scope enforcement, and team membership APIs.
- Next: role-based workspace access enforcement for all endpoints.
