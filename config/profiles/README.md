# Runtime Profiles

These files contain reviewed configuration overrides for common deployments. They intentionally contain no credentials. Supply `TCE_API_TOKENS`, identity claims, database credentials, and provider secrets through the deployment environment.

Profiles:

- `local-lite`: single-user SQLite runtime with the core MCP surface.
- `local-full`: single-user PostgreSQL runtime with the core MCP surface.
- `team-secure`: strict server-bound identity, workspace, CORS, and durable audit settings.
- `research`: opt-in behavioral evaluation surfaces with autonomy gating kept off.

The installer applies a selected profile before preserving explicit user overrides. `TCE_RUNTIME_PROFILE` records the active profile, while `TCE_MCP_TOOL_PROFILE` controls which MCP capability set is exposed.

Security invariants:

- No profile contains a token or provider key.
- Team and research profiles fail closed on identity and workspace claims.
- No profile enables behavioral autonomy.
- Wildcard CORS is not used.
- Payload encryption is not claimed by these profiles; configure a real encryption backend or encrypted storage separately.
