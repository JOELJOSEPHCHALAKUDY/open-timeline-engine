# Open Timeline Engine VSCode Extension

Production capture extension for Open Timeline Engine.

## Features

- Task lifecycle commands (`TASK_START`, `TASK_STEP`, `TASK_DECISION`, `TASK_DONE`)
- Reflection capture command (`REFLECTION`)
- Auto capture for VSCode commands (`COMMAND_RUN`)
- Auto capture for document saves (`DOC_EDIT`)
- Offline queue with replay command
- Workspace/user scoped headers for team mode

## Commands

- `TCE: Start Task`
- `TCE: Log Step`
- `TCE: Log Decision`
- `TCE: Complete Task`
- `TCE: Reflection Note`
- `TCE: Replay Queue`
- `TCE: Takeover Activate`
- `TCE: Takeover Step`
- `TCE: Takeover Stop`
- `TCE: Takeover State`

## Settings

- `tce.apiUrl`
- `tce.apiToken`
- `tce.consumerId`
- `tce.role`
- `tce.workspaceId`
- `tce.userId`
- `tce.defaultDomain`
- `tce.defaultTaskType`
- `tce.defaultSensitivity`
- `tce.autoReplayOnStartup`
- `tce.autoCaptureCommands`
- `tce.autoCaptureSaves`
- `tce.ignoredCommands`
- `tce.takeoverSessionId`
- `tce.takeoverPersonaMode`
- `tce.takeoverActivationModeDefault`

## Build

```bash
npm run -w plugins/tce_vscode build
```

## Package as VSIX

```bash
npm i -w plugins/tce_vscode -D @vscode/vsce
npx -w plugins/tce_vscode vsce package
```

Then install from VSIX in VSCode (`Extensions: Install from VSIX...`).
