# Team and Workspace Setup

Open Timeline Engine (TCE) supports per-workspace timeline isolation with membership-aware access control.

## Scope model

Each request can include:

- `X-TCE-Workspace`: workspace scope id
- `X-TCE-User`: user id in that workspace

Each event is stamped with internal scope markers:

- `_tce_workspace`
- `_tce_owner`

## Membership model

Team membership API:

- `GET /v1/team/memberships`
- `POST /v1/team/memberships`

MCP tool:

- `tce.get_team_memberships`

On first event write in a workspace, owner membership is auto-created.

## Access behavior

- if workspace has no memberships yet, access defaults open (bootstrap)
- once members exist, access requires active membership
- advisor/executor flows still respect workspace/user scoping in retrieval paths
- default retrieval profile is `user-only`
- explicit/eligible cross-user memory queries can return `workspace-shared` policy metadata for that request

## Recommended defaults

- Personal mode:
  - `X-TCE-Workspace: personal`
  - `X-TCE-User: <your-id>`
- Team mode:
  - separate workspace id per team/project
  - explicit memberships for all users and service identities

## Shared memory handoff convention

Use explicit cross-executor timeline reads before continuation:

- `read codex timeline`
- `read claude timeline`
- `continue codex work on <task>`
- `continue claude work on <task>`

This keeps the default profile `user-only` for normal queries while activating `workspace-shared` only for explicit handoff requests.

## One-time history backfill (recommended)

If your history is mostly older auto-capture logs, run one focused handoff session per executor to seed richer milestone events:

1. Read the other executor timeline explicitly (`read codex timeline` / `read claude timeline`).
2. Complete one real task and report execution with milestone details:
   - `title`
   - `payload.files`
   - `decision`
   - `outcome.status` + `outcome.next_step`
3. Repeat for 3-5 tasks per executor to establish high-signal retrieval anchors.

## Security notes

- workspace scoping is in addition to sensitivity/policy checks
- sensitivity `3` remains blocked by default from output paths
- audit log captures consumer, action, returned citations, and policy outcomes

## Continuity metadata

Search and bundle retrieval metadata includes:

- `handoff_hits_count`
- `top_handoff_record_ids`
- `resume_packet_available`
- `cross_user_scope_applied`
- `cross_user_scope_owners`

For direct continuation, call:
- `POST /v1/handoff/resume`
- or MCP `tce.get_resume_packet`
