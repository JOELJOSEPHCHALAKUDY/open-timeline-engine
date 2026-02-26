# Open Timeline Engine Browser Extension

Production browser capture plugin for Open Timeline Engine.

## Features

- Per-site opt-in capture
- Context-menu link capture
- Current-tab capture
- Offline queue replay
- Workspace/user scoped headers
- Configurable role (`user`, `executor`, `advisor`)

## Configure

Open extension settings and configure:

- API URL + token
- consumer id + role
- workspace id + user id
- default sensitivity
- allowed site hostnames

Only hostnames in allowed sites are captured.

## Build

`npm run -w plugins/tce_browser build`
