# Changelog

## 0.4.0

- Repo-wide quality baseline achieved and enforced: `ruff check .` and full `mypy shared services scripts tests` are now clean and gate CI (previously changed-lines-only).
- CI hardening: coverage floor (`--cov-fail-under=28`), `pip-audit` dependency vulnerability gate, latest-migration round-trip check (downgrade/upgrade), OpenAPI parity gate between full and lite APIs (`.github/ci/openapi_parity.py`), and a new `lite-mcp-e2e` job running bound-identity enforce-mode checks.
- Dependency refresh across services (`fastapi>=0.139.2`, `starlette>=1.3.1`, `pydantic-settings>=2.14.2`, `requests>=2.33`) plus Dependabot configuration.
- MCP least-privilege tool profiles (`tce_mcp/tool_profiles.py`, `TCE_MCP_TOOL_PROFILE`: core/continuity/autonomy/research).
- Governance status module (`tce_shared/governance.py`): enforcement-level reporting and insecure-default-token detection.
- Continuity active resume: migration `20260723_0032` plus shared continuity metrics (`tce_shared/continuity.py`).
- Runtime environment profiles: `config/profiles/*.env`, `scripts/apply_runtime_profile.sh`, and installer integration.
- Checked-in `.env.example` template.
- Type-hygiene refactor pass across `tce_api`, `tce_lite_api`, `tce_mcp`, `tce_worker`, and `shared` to reach the mypy baseline.
- Documentation: synced policy/docs for V7.2 workflow-memory hints; documented dashboard coverage gaps (workflow templates page, retrieval-eval UI, context-brief UI, route health history).
- Hardening follow-ups (post-audit): e2e smoke scripts isolated to their own compose project (no longer able to wipe the dev database), Redis switched to `noeviction` for the RQ job store, privileged endpoints refuse the default token (`TCE_ALLOW_DEFAULT_TOKEN`), CORS default narrowed off `*`, Prometheus multiprocess metrics + directive-outcome counters, `tests/mcp` firewall sentinels wired into CI, negation-aware safety-confirm matching, idempotent `report_execution`, claim-TTL split from execution deadline, untrusted event titles sanitized and objectives delimited as data at the MCP boundary, and an opt-in nightly backup cron with rewritten DR runbooks.
- Validation run for this release:
- `ruff check .` -> clean
- `MYPYPATH=shared mypy shared services scripts tests` -> clean (203 files)
- `pytest tests/unit tests/security tests/mcp -q` -> `209 passed`
- `pytest tests/integration -q` -> `52 passed, 1 skipped`
- Coverage baseline -> `30.5%` (floor 28%)

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

## 0.2.0

- Added runtime mode switching: `timeline_only` and `clone_advisor`
- Added dual-AI endpoints: `/v1/clone/advice` and `/v1/clone/arbitrate`
- Added loop-guarded advisor interaction logging
- Added MCP tools for mode control and clone advisory workflow

## 0.1.0

- Initial monorepo scaffold
- API, worker, and MCP service skeletons
- Canonical schema package
- CLI, Git, VSCode, and Browser capture plugins
- Docker Compose, Alembic migration, and observability baseline
