# Capture Plugin Setup Guide

Open Timeline Engine ships three capture plugins that extend timeline memory beyond MCP tool calls, plus a host-side hook that captures your own prompts as trusted human input. Each sends events to the TCE API and enriches the timeline with real-world signals.

## Quick install

```bash
# Interactive menu — pick what you want
./scripts/plugin-install.sh

# Install all plugins at once
./scripts/plugin-install.sh --all

# Install a specific plugin
./scripts/plugin-install.sh --vscode
./scripts/plugin-install.sh --browser
./scripts/plugin-install.sh --git --repo /path/to/your/repo
```

Pre-built artifacts are included in the repository. No build toolchain is needed unless you pass `--build`.

---

## 1. VSCode Extension

Captures task lifecycle events, decisions, document saves, and command executions from VS Code.

### Prerequisites

- VS Code installed
- `code` CLI available (optional — enables auto-install)

### Install

```bash
./scripts/plugin-install.sh --vscode
```

The script finds the pre-built `.vsix` and installs it via `code --install-extension`. If the `code` CLI is not available, it prints the file path for manual install.

**Manual install (alternative):**

1. Open VS Code
2. Go to Extensions → `⋯` menu → **Install from VSIX...**
3. Select `plugins/tce_vscode/tce-vscode-*.vsix`

### Build from source

Requires: Node.js >= 20, npm

```bash
./scripts/plugin-install.sh --vscode --build
```

### Configure

Open VS Code Settings (`Cmd+,` / `Ctrl+,`) and set:

| Setting | Default | Description |
| --- | --- | --- |
| `tce.apiUrl` | `http://localhost:8080` | TCE API endpoint |
| `tce.apiToken` | `local-dev-token` | API authentication token |
| `tce.consumerId` | `vscode-capture` | Identifies this capture source |
| `tce.workspaceId` | `personal` | TCE workspace scope |
| `tce.userId` | (same as consumerId) | User identity |
| `tce.role` | `executor` | Role: `user`, `executor`, or `advisor` |
| `tce.personaMode` | `normal` | Persona: `normal`, `naruto`, or `shadow` |

### Commands

| Command | Event type | Description |
| --- | --- | --- |
| `TCE: Start Task` | `TASK_START` | Begin tracking a task |
| `TCE: Log Step` | `TASK_STEP` | Record a step in the current task |
| `TCE: Log Decision` | `TASK_DECISION` | Record a decision with rationale |
| `TCE: Complete Task` | `TASK_DONE` | Mark current task as complete |
| `TCE: Reflection Note` | `REFLECTION` | Capture a learning or insight |
| `TCE: Replay Queue` | — | Replay any offline-queued events |
| `TCE: Takeover Activate` | — | Start takeover session |
| `TCE: Takeover Step` | — | Send next message in takeover |
| `TCE: Takeover Stop` | — | Stop takeover session |

Auto-capture (when enabled): document saves (`DOC_EDIT`) and VS Code command runs (`COMMAND_RUN`) are captured automatically.

---

## 2. Browser Extension (Chrome / Edge)

Captures web activity from allowed sites — page visits, link clicks, and context-menu captures.

### Prerequisites

- Chrome or Edge (Chromium-based browser)

### Install

```bash
./scripts/plugin-install.sh --browser
```

The script verifies the pre-built extension is ready and prints loading instructions.

**Load into Chrome/Edge:**

1. Open `chrome://extensions` (or `edge://extensions`)
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select: `plugins/tce_browser/dist`

### Build from source

Requires: Node.js >= 18, npm

```bash
./scripts/plugin-install.sh --browser --build
```

### Configure

Click the extension icon → **Options** and set:

| Setting | Default | Description |
| --- | --- | --- |
| API URL | `http://localhost:8080` | TCE API endpoint |
| API Token | `local-dev-token` | API authentication token |
| Consumer ID | `browser-capture` | Identifies this capture source |
| Workspace ID | `personal` | TCE workspace scope |
| User ID | (same as consumer) | User identity |
| Role | `user` | Role: `user`, `executor`, or `advisor` |
| Default sensitivity | `1` | Sensitivity level for captured events |
| Allowed sites | (empty) | Hostnames to capture (per-site opt-in) |

Only hostnames listed in **Allowed sites** are captured. All other sites are ignored.

### Features

- Per-site opt-in capture
- Context-menu link capture
- Current-tab capture
- Offline queue with replay
- Workspace/user scoped headers

---

## 3. Git Hooks

Captures commits and push events automatically via git hooks.

### Prerequisites

- Python >= 3.11
- Git repository to instrument

### Install

```bash
# Install and wire hooks into a specific repo
./scripts/plugin-install.sh --git --repo /path/to/your/repo

# Or let it prompt you for the repo path
./scripts/plugin-install.sh --git
```

This installs:
- `post-commit` hook — captures every commit with message, author, files changed
- `pre-push` hook — captures push events with branch and remote info

### Manual install (alternative)

```bash
# Install the Python package
python3 -m pip install -e plugins/tce_git_capture

# Wire hooks into your repo
tce-git-capture install --repo /path/to/your/repo

# Skip pre-push hook if not needed
tce-git-capture install --repo /path/to/your/repo --no-enable-prepush
```

### Environment variables

Set these in your shell profile or `.env`:

| Variable | Default | Description |
| --- | --- | --- |
| `TCE_API_BASE_URL` | `http://localhost:8080` | TCE API endpoint |
| `TCE_API_TOKEN` | `local-dev-token` | API authentication token |
| `TCE_USER_CONSUMER_ID` | `git-capture` | Identifies this capture source |
| `TCE_MCP_WORKSPACE_ID` | `personal` | TCE workspace scope |
| `TCE_USER_ID` | (same as consumer) | User identity |
| `TCE_REPLAY_CONCURRENCY` | `4` | Parallel replay threads |

### Commands

```bash
# Manually capture a commit (hooks do this automatically)
tce-git-capture capture-commit --repo .

# Manually capture a push event
tce-git-capture capture-prepush --repo .

# Replay any offline-queued events
tce-git-capture replay
```

---

## 4. Host capture hook (trusted human input)

`scripts/tce_capture_input.py` runs on every `UserPromptSubmit` in Claude Code (and Codex, see below) and records **what you typed, before the executor rewrites it** as a `TrustedInputCapture` receipt via `POST /v1/inputs`. This is the only path that can produce *explicit* human evidence: an executor that reports "the user said yes" is stored as inferred/pending review, a receipt from this hook is not.

`./scripts/install.sh` wires it automatically (Step 12, "Client hooks"). Nothing else is needed for Claude Code.

### What is captured

| Field | Value |
| --- | --- |
| `content` | your prompt, secret-redacted (`api_key=...`, tokens, passwords, emails, private keys become `<REDACTED:...>`), capped at `TCE_CAPTURE_MAX_CHARS` (2000) |
| `content_sha256` | hash of the **original** prompt (never sent) |
| `delivery_key` | `sha256(session_id | prompt_id-or-turn_id-or-ms-timestamp | content_sha256)`; replays are idempotent, the server answers `deduplicated=true` |
| `origin_kind` | always `human_input`; only a host-capture credential may assert it |
| `project_hint` | `cwd` basename, `project_root`, git branch/remote read from `.git/HEAD` and `.git/config` (no subprocess) |
| `spool_depth`, `spool_failures`, `gap_since` | local spool health, so the server can compute `capture_delivery_state` |

The hook never prints the prompt or the token; its only stdout is `{"suppressOutput": true}` plus an optional `systemMessage` for you (not for the model).

### Credential and files

| Path | Purpose | Mode |
| --- | --- | --- |
| `~/.config/open-timeline-engine/host_capture.token` | the host-capture bearer token; the **only** place it is stored. Bind-mounted read-only into `tce-api`/`tce-worker` at `/run/secrets/tce_host_capture_token` and read via `TCE_HOST_CAPTURE_TOKENS_FILE` | 0600 |
| `~/.config/open-timeline-engine/host_capture.env` | `TCE_CAPTURE_WORKSPACE`, `TCE_CAPTURE_USER`, `TCE_API_BASE_URL` | 0600 |
| `~/.cache/open-timeline-engine/capture-spool/` | bounded local spool (500 files / 8 MB): `<delivery_key>.json` entries, `state.json`, `gaps.jsonl` | 0700 |

The host token is a **separate capability** from the executor's `TCE_API_TOKEN`:

- `install.sh` writes it **only** to the 0600 token file, never to the repo `.env`, never to `TCE_API_TOKEN(S)`, and never passes it to `scripts/configure_mcp_clients.sh` (which aborts if it finds `HOST_CAPTURE` in a generated config or if the MCP token equals the host token). An older install that left `TCE_HOST_CAPTURE_TOKENS` in `.env` is scrubbed on the next run.
- `.env` is bind-mounted into the API container and readable from any executor shell in the workspace, so the credential must not live there. `scripts/start.sh` makes sure the token file exists (empty = capture disabled) and compose mounts it read-only at `/run/secrets/tce_host_capture_token`; the API reads it via `TCE_HOST_CAPTURE_TOKENS_FILE`. The `TCE_HOST_CAPTURE_TOKENS` CSV env still works for tests and custom deployments.
- `scripts/tce-capture` unsets `TCE_HOST_CAPTURE_TOKENS` after sourcing `.env`; the MCP server (`tce_mcp/client.py`) refuses to start if its API token equals a host token and refuses to call `/v1/inputs` at all.
- The server rejects a token that is listed in both `TCE_API_TOKENS` and `TCE_HOST_CAPTURE_TOKENS`, and disables `/v1/inputs` on default tokens (`local-dev-token`).
- `.claude/settings.json` gets `permissions.deny` rules so the executor cannot `Read` the token or the spool.
- **Limit of this separation:** an executor with a shell running as the same OS user can still read a file that user owns. Deny rules are defense in depth, not a boundary. For real separation run the executor as a dedicated OS user, or use identity mode `enforce` with a server-bound claim so a header-asserted role cannot pass as the human.

Environment overrides (all optional; the hook never reads the repo `.env`):

| Variable | Default | Description |
| --- | --- | --- |
| `TCE_HOST_CAPTURE_TOKEN` | (token file) | inline token, mainly for tests |
| `TCE_HOST_CAPTURE_TOKEN_FILE` | `~/.config/open-timeline-engine/host_capture.token` | token file location (hook side; also what `scripts/start.sh` mounts into the containers) |
| `TCE_CAPTURE_WORKSPACE` / `TCE_CAPTURE_USER` | from `host_capture.env`, else `personal` / `$USER` | scope of the receipt; `TCE_CAPTURE_USER` is sent as `X-TCE-User` **and** `X-TCE-Behavior-Subject` |
| `TCE_API_BASE_URL` | `http://127.0.0.1:8080` | API endpoint |
| `TCE_CAPTURE_SPOOL_DIR` | `~/.cache/open-timeline-engine/capture-spool` | spool location |
| `TCE_CAPTURE_MAX_CHARS` | `2000` | content cap (server caps again) |
| `TCE_CAPTURE_DISABLED` | unset | `1` disables capture; state becomes `disabled` |

### Failure states and what the `systemMessage` means

The hook is bounded to 2 s of wall time and always exits 0, so it can never block or fail a prompt. Every capture is fsync'd into the spool **before** the network call; delivery then runs with a 1.5 s timeout, and up to 5 older spooled entries are replayed while time remains. `state.json` records the outcome, and when it is anything but `delivered` you see a line like:

```
[TCE-CAPTURE] human-input capture spooled: 3 pending, last error: URLError: [Errno 61] Connection refused
```

| `capture_delivery_state` (local) | Meaning | What to do |
| --- | --- | --- |
| `delivered` | last capture reached the API | nothing |
| `spooled` | API unreachable or slow; entry kept, replayed on later prompts | start the stack (`./scripts/start.sh ...`); the backlog drains by itself |
| `failed` | 401/403/422 from the API (bad or unbound token, token listed as an API token, payload rejected); entry kept, retried at most 3 times | re-run `./scripts/install.sh` to re-provision, then restart the stack so the API re-reads the mounted token file |
| `dropped_full` | the spool hit its cap and the oldest captures were evicted; each is logged in `gaps.jsonl` and reported to the server as `gap_since` | restore the API; the gap is recorded server-side as a capture gap, not silently lost |
| `disabled` | no token, `TCE_CAPTURE_DISABLED=1`, or the shared modules could not be loaded (Python < 3.11) | re-run the installer / fix `python3` |

The warning repeats only when the state changes or every 10 minutes (hourly for `disabled`), so an outage does not spam every prompt.

Server side, `tce.takeover_step` / `GET /v1/takeover/execution/status` expose `capture_delivery_state` (`healthy`, `spooling`, `degraded`, `gap`, `unavailable`). In an unattended autonomy profile, `gap`/`unavailable` pauses mutating actions ("AUTONOMOUS MODE PAUSED: capture channel unavailable") until the hook delivers again or you switch the profile to `human_consultative`. Ordinary manual coding is never blocked by a capture gap.

### How a captured answer becomes evidence

1. When takeover asks you something (a safety confirmation, a needs-human pause, "what next?"), the server freezes a **decision opportunity** with the alternatives and a hidden shadow prediction *before* your answer exists.
2. Your reply arrives through this hook as a receipt; a background extraction job derives decision candidates from it.
3. A plain `yes`/`confirm`/`abort` is promoted to explicit, learning-eligible evidence **only when it resolves exactly one still-open question** with matching alternatives. A generic acknowledgement ("ok", "sure", "continue"), an ack while two questions are open, or an ack to an open-ended question is discarded, never stored as a preference. Quoted text, pasted agent output, negations ("don't confirm, abort") and corrections ("actually, go with ...") are handled by deterministic rules; anything ambiguous lands in the review queue instead of being learned.
4. The shadow prediction is scored against your authenticated answer and labelled `prospective` (frozen before the answer) or `retrospective` (made after the answer was known); the two are never mixed in precision metrics, and unanswered / missed-capture / extraction-error cases are counted in separate denominators.

### Codex

Codex reads hooks from `<workspace>/.codex/hooks.json` and only when `hooks = true` is set in the `[features]` table of `<workspace>/.codex/config.toml`. `install.sh` writes both when it detects Codex; to do it by hand:

```bash
python3 scripts/generate_client_hooks.py --self-heal "$PWD/scripts/self-heal.sh" \
  --capture-script "$PWD/scripts/tce_capture_input.py" --client codex --merge-into .codex/hooks.json
python3 scripts/generate_client_hooks.py --ensure-codex-config .codex/config.toml
```

The Codex payload carries `turn_id` instead of `prompt_id`; the hook accepts both.

### Manual check

```bash
echo '{"session_id":"11111111-2222-3333-4444-555555555555","transcript_path":"/tmp/t.jsonl","cwd":"'"$PWD"'","permission_mode":"default","hook_event_name":"UserPromptSubmit","prompt":"confirm"}' \
  | python3 scripts/tce_capture_input.py --client claude --dry-run     # prints the body, no spool, no network
echo '{...same...}' | python3 scripts/tce_capture_input.py --client claude; echo "exit=$?"
cat ~/.cache/open-timeline-engine/capture-spool/state.json
```

### Lite backend parity gap

The Full backend encrypts receipt payloads at rest (`TCE_SECURITY_ENCRYPTION_SECRET`, required for `/v1/inputs`). The Lite (SQLite) backend stores the redacted content in plaintext; the redaction, caps, capability checks and extraction rules are identical.

---

## Troubleshooting

### Plugin not sending events

1. Check TCE API is running: `curl -sS http://localhost:8080/v1/health`
2. Verify API token matches between plugin config and `.env`
3. Check workspace ID matches your TCE setup

### VSCode extension not visible

- Ensure the `.vsix` was installed: `code --list-extensions | grep tce`
- Reinstall: `code --install-extension plugins/tce_vscode/tce-vscode-*.vsix`

### Browser extension not loading

- Verify `plugins/tce_browser/dist/manifest.json` exists
- Rebuild: `./scripts/plugin-install.sh --browser --build`
- Chrome may need a page refresh after loading the extension

### Git hooks not firing

- Check hooks exist: `ls -la /path/to/repo/.git/hooks/post-commit`
- Check `tce-git-capture` is on PATH: `which tce-git-capture`
- Reinstall: `python3 -m pip install -e plugins/tce_git_capture`

### VSIX build fails (Node version)

The `@vscode/vsce` packaging tool requires Node.js >= 20. If you see `ReferenceError: File is not defined`, upgrade Node:

```bash
# Using nvm
nvm install 20
nvm use 20

# Then rebuild
./scripts/plugin-install.sh --vscode --build
```

### Host capture hook keeps saying `spooled` / `failed` / `disabled`

- `spooled`: the API is down or unreachable at `TCE_API_BASE_URL`; the spool replays on the next prompts once it is back
- `failed` with `http 403`: the token is not a host-capture token (or is also listed in `TCE_API_TOKENS`, or is a default token); re-run `./scripts/install.sh` and restart the API so it re-reads `/run/secrets/tce_host_capture_token`
- `failed` with `http 503`: the Full backend needs `TCE_SECURITY_ENCRYPTION_SECRET` set
- `disabled`: no `~/.config/open-timeline-engine/host_capture.token`, or `python3` is older than 3.11
- Inspect: `cat ~/.cache/open-timeline-engine/capture-spool/state.json` and `ls ~/.cache/open-timeline-engine/capture-spool/`

### General

Run the doctor script for automated diagnostics:

```bash
./scripts/doctor.sh auto
```
