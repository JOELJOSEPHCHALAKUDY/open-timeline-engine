# Capture Plugin Setup Guide

Open Timeline Engine ships three capture plugins that extend timeline memory beyond MCP tool calls. Each plugin sends events to the TCE API and enriches the timeline with real-world signals.

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

### General

Run the doctor script for automated diagnostics:

```bash
./scripts/doctor.sh auto
```
