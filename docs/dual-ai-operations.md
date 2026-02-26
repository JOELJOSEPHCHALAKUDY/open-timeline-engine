# Running Multiple Executors with Advisor Guidance

## Goal

Use one or more executor clients with shared advisor guidance, without creating feedback loops.

## Setup

1. Set runtime mode to `clone_advisor`.
2. Use different consumer headers for each executor:
   - Executor A: `X-TCE-Consumer: codex-executor`, `X-TCE-Role: executor`
   - Executor B: `X-TCE-Consumer: claude-executor`, `X-TCE-Role: executor`
   - Advisor model routing remains API-side (`/v1/setup/advisor/*`).
3. Keep workspace shared and user identities distinct:
   - Same `X-TCE-Workspace` for shared-memory mode
   - Different `X-TCE-User` per executor identity

## Recommended Cycle

1. Executor asks for `tce.get_context_bundle(task, app_context)`.
2. Any executor lane asks for `tce.get_clone_advice(task, app_context, executor_output, interaction_id)`.
3. Executor merges or calls `tce.arbitrate_with_clone(...)`.
4. If loop guard blocks, pause and request user decision.

## Safety

- Advisory tools are read-only for write endpoints.
- Loop guard limits advisory turns per interaction.
- Weak evidence responses include explicit caution language.
