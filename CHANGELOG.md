# Changelog

## 0.3.0

- Reliability and security hardening pass across API, lite API, worker, MCP, and CI.
- `tce_api` database engine/session creation is now lazy (no import-time DB init failure path).
- Added configurable CORS support to both API runtimes (`TCE_CORS_ALLOW_ORIGINS`, `TCE_CORS_ALLOW_CREDENTIALS`).
- Replaced internal `requests.models.complexjson` parsing with stdlib `json.loads`.
- MCP client now uses HTTP retry/backoff for transient failures (`TCE_MCP_HTTP_RETRY_*`).
- Lite SQLite runtime now sets connection timeouts + `PRAGMA busy_timeout`, and `init_db()` always closes connection.
- Worker embedding vector dimension is now configurable (`TCE_EMBEDDING_DIMENSIONS`) instead of hardcoded.
- Crypto placeholder behavior now warns clearly when encryption is enabled without a real backend.
- CI load test now boots an API first and fails correctly when the endpoint is unavailable.
- Added env template entries for new runtime and setup controls.
- Validation run for this release:
- `pytest tests/unit -q` -> `10 passed`
- `pytest tests/security -q` -> `2 passed`
- `pytest tests/integration -q` -> `13 passed`
- Locust smoke (`30s`) -> `0% failures` against local lite API
- Known repo-wide baseline still pending: `ruff check .` and `mypy services shared` include pre-existing failures outside this release scope.

## 0.1.0

- Initial monorepo scaffold
- API, worker, and MCP service skeletons
- Canonical schema package
- CLI, Git, VSCode, and Browser capture plugins
- Docker Compose, Alembic migration, and observability baseline

## 0.2.0

- Added runtime mode switching: `timeline_only` and `clone_advisor`
- Added dual-AI endpoints: `/v1/clone/advice` and `/v1/clone/arbitrate`
- Added loop-guarded advisor interaction logging
- Added MCP tools for mode control and clone advisory workflow

## Unreleased

### Documentation

- Synced policy and docs for V7.2 workflow-memory hints (`workflow_hints` behavior and constraints precedence).
- Documented current dashboard coverage and known missing UX areas (workflow templates page, retrieval-eval UI, context-brief UI, route health history).
