# Dual-AI Architecture

## Roles

- `executor`: performs implementation steps
- `advisor lane`: API-side guidance path that reads timeline and provides "act-as-user" guidance
- `user`: retains final control

MCP clients are expected to run as `executor` identities; the advisor lane is routed API-side.

## Flow

1. Set runtime mode to `clone_advisor`.
2. Executor retrieves context with `tce.get_context_bundle`.
3. Any executor lane calls `tce.get_clone_advice` using the same `interaction_id`.
4. If guidance conflicts, call `tce.arbitrate_with_clone`.
5. Human override is always final.

## Safety Controls

- Advisory tools are read-only for mutation endpoints.
- Loop guard limits advisor turns per interaction.
- Weak evidence forces caution summary.
- Audit and agent_interactions tables record every advisory turn.
