# Runtime Profiles

These files contain reviewed configuration overrides for common deployments. They intentionally contain no credentials. Supply `TCE_API_TOKENS`, identity claims, database credentials, and provider secrets through the deployment environment.

Profiles:

- `local-lite`: single-user SQLite runtime with the core MCP surface.
- `local-full`: single-user PostgreSQL runtime with the core MCP surface.
- `team-secure`: strict server-bound identity, workspace, CORS, and durable audit settings.
- `research`: opt-in behavioral evaluation surfaces with autonomy gating kept off.

The installer applies a selected profile before preserving explicit user overrides. `TCE_RUNTIME_PROFILE` records the active profile, while `TCE_MCP_TOOL_PROFILE` controls which MCP capability set is exposed.

MCP profiles are monotonic capability sets:

- `core`: 10-tool timeline/continuity default.
- `continuity`: `core` plus memory maintenance and inspection.
- `autonomy`: `continuity` plus takeover and permit/claim/report.
- `research`: `autonomy` plus evaluation, drift, and experimental tools.
- `admin` / `all`: the complete compatibility surface, available only by explicit configuration.

The setup wizard maps `timeline_only` to `core` and upgrades `clone_advisor` from `core`/`continuity` to `autonomy`. Generated Codex, Claude, Cursor, and generic MCP configs carry the selected profile explicitly. Regenerate configs and restart executors after a profile change.

Security invariants:

- No profile contains a token or provider key.
- Team and research profiles fail closed on identity and workspace claims.
- No profile enables behavioral autonomy.
- Wildcard CORS is not used.
- Payload encryption is not claimed by these profiles; configure a real encryption backend or encrypted storage separately.
