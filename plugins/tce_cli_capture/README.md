# Open Timeline Engine CLI Capture

## Commands

- `tce-capture run "pytest -q"`
- `tce-capture note "Decision rationale" --tags architecture`
- `tce-capture replay`
- `tce-capture advisor-state --session-id my-chat`
- `tce-capture advisor-chat "hey kurama take over" --session-id my-chat`
- `tce-capture advisor-chat "continue with retry strategy" --session-id my-chat`
- `tce-capture advisor-chat "kurama stand down" --session-id my-chat`
- `tce-capture advisor-stop --session-id my-chat`
- `tce-capture advisor-reset --session-id my-chat`
- `tce-capture advisor-disable`
- `tce-capture advisor-chat "hey igris, take over" --session-id my-chat --persona-mode shadow`

## Env vars

- `TCE_API_BASE_URL`
- `TCE_API_TOKEN`
- `TCE_ADVISOR_CONSUMER_ID` (default: `cli-advisor`)
- `TCE_ADVISOR_USER_ID` (default: same as consumer)
- `TCE_MCP_WORKSPACE_ID` (default: `personal`)
- `TCE_ADVISOR_PERSONA_MODE` (default: `normal`, options: `normal|naruto|shadow`)
- `TCE_ADVISOR_REAL_TAKEOVER` (default: `false`)
- `TCE_ADVISOR_REAL_TAKEOVER_MODE` (`suggest` or `takeover`, default: `suggest`)
- `TCE_ADVISOR_COMMAND` (external advisor command; reads JSON from stdin)
- `TCE_ADVISOR_TIMEOUT_SECONDS` (default: `45`)
- `TCE_ADVISOR_TCE_ENRICH` (default: `true`)
- `TCE_TAKEOVER_SAFETY_POLICY` (default: `high-risk-pause`)
- `TCE_TAKEOVER_CONFIRM_KEYWORD` (default: `confirm`)
- `TCE_TAKEOVER_DENY_KEYWORD` (default: `abort`)
- `TCE_ADVISOR_SELF_HEAL_ENABLED` (default: `true`)
- `TCE_ADVISOR_SELF_HEAL_KEYWORDS` (optional comma-separated phrases)
- `TCE_ADVISOR_SELF_HEAL_STACK` (`auto|full|lite|all`, default: `auto`)
- `TCE_ADVISOR_SELF_HEAL_TIMEOUT_SECONDS` (default: `900`)
- `TCE_ADVISOR_AUTO_CONTINUE_TURNS` (default: `0`, range: `0..25`)
- `TCE_ADVISOR_AUTO_CONTINUE_MESSAGE` (default internal continue message)

## Optional continuous advisor mode

Default behavior is unchanged. Advisor auto-call is off until activation phrase is seen.

`advisor-state`, `advisor-reset`, `advisor-stop`, and `advisor-chat` now call takeover API endpoints:

- `GET /v1/takeover/state`
- `POST /v1/takeover/reset`
- `POST /v1/takeover/step`

Flow:

1. Call `advisor-chat` for each incoming user message.
2. If message contains activation keyword (`hey kurama take over` by default), session state becomes active.
3. Every later `advisor-chat` call in that session auto-calls `/v1/takeover/step`.
4. If message contains stop keyword (`kurama stand down` by default), auto-call stops.
5. Session auto-disables after timeout (default `30` minutes) unless refreshed by new messages.
6. Active sessions attach compact `takeover_context` + `message_delta` payloads so advisor calls receive rolling summary context.
7. For mutating directives, follow V6 lifecycle: request permit if required, claim execution before edits, then report outcome.

Persona defaults:

- `normal`: activation `hey advisor take over`, stop `advisor stand down`
- `naruto`: activation `hey kurama take over`, stop `kurama stand down`
- `shadow`: activation `hey igris, take over` or `hey beru, take over`, stop `shadow stand down`

Activation acknowledgements:

- `shadow` + `hey beru, take over` -> `Yes, My liege.`
- `shadow` + `hey igris, take over` -> `My liege`

## Self-heal takeover keyword (optional)

When enabled, advisor-chat can trigger a repair cycle and resume takeover automatically.

Default phrase examples:

- `beru self heal`
- `igris self heal`
- `kurama self heal`

Behavior:

1. Runs `./scripts/install.sh fix <stack>` from project root.
2. If fix succeeds, auto-reactivates takeover in the same session.
3. Continues using previous objective (if available) or explicit `--task`.
4. Emits `self_heal` metadata in advisor-chat JSON output.

## Auto-continue burst mode (optional)

For human-like autonomy in turn-based clients, run multiple internal takeover steps after one user message.

Example:

```bash
tce-capture advisor-chat "hey beru take over" \
  --session-id my-chat \
  --persona-mode shadow \
  --auto-continue-turns 5
```

Behavior:

1. Executes the initial takeover step from user message.
2. Runs `N` extra internal takeover steps with a configurable continue message.
3. Stops early on `inactive`, `stopped`, or safety gate pause.
4. Returns final response plus `auto_continue.trace` metadata.

## Real dual-provider takeover (optional)

`advisor-chat` can call an external advisor command on every active message.

Command contract:

1. CLI sends JSON payload via stdin.
2. Command returns text or JSON (`{"response":"..."}`) on stdout.
3. In `takeover` mode, returned response is treated as final takeover text.
4. If command is blank, takeover falls back to built-in Open Timeline Engine guidance (keyword still works).

Example:

```bash
TCE_ADVISOR_REAL_TAKEOVER=true \
TCE_ADVISOR_REAL_TAKEOVER_MODE=takeover \
TCE_ADVISOR_COMMAND="python3 /absolute/path/to/claude_bridge.py" \
tce-capture advisor-chat "hey beru, take over" --session-id my-chat --persona-mode shadow
```

## Stop and remove advisor behavior

- Stop one chat session: `tce-capture advisor-stop --session-id my-chat`
- Disable advisor mode state globally: `tce-capture advisor-disable`
- Disable advisor mode state and switch backend mode: `tce-capture advisor-disable --set-timeline-only`
