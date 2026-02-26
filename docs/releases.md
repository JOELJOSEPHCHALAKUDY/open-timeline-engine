# Release Policy

## Public release track

- Current pre-1.0 public version: `v0.3.0`
- Public versioning uses SemVer for release communication.
- API path version (`/v1`) is a contract version and does not imply product `1.x`.

## Internal milestone labels

- Labels such as `V4`, `V5`, `V6`, `V7`, `V8`, and `V9.x` are internal engineering milestones.
- They are kept in docs for implementation history and migration context.


## Versioning

- `0.x`: beta period, rapid iteration
- `1.x`: stable API/MCP contracts with deprecation windows
- Current beta target: `0.3.0`

## Compatibility

- Additive changes allowed in minor versions
- Breaking schema changes only in major versions

## Changelog

Track all user-visible behavior changes in `CHANGELOG.md`.

## Docs sync update

- Added Milestone V7.2 workflow-memory hint behavior documentation.
- Added dashboard known-gap documentation for workflow/retrieval/context-brief UX coverage.
