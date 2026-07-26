# Release Policy

## Public release track

- Current pre-1.0 public version: `v0.4.0`
- Public versioning uses SemVer for release communication.
- API path version (`/v1`) is a contract version and does not imply product `1.x`.

## Internal milestone labels

- Labels such as `V4`, `V5`, `V6`, `V7`, `V8`, and `V9.x` are internal engineering milestones.
- They are kept in docs for implementation history and migration context.


## Versioning

- `0.x`: beta period, rapid iteration
- `1.x`: stable API/MCP contracts with deprecation windows
- Current beta target: `0.4.0`

## Compatibility

- Additive changes allowed in minor versions
- Breaking schema changes only in major versions

## Changelog

Track all user-visible behavior changes in `CHANGELOG.md`.

## v0.4 evidence

- Release changes: [`../CHANGELOG.md`](../CHANGELOG.md)
- Reproducible proof and limitations: [`releases/0.4.0-proof.md`](releases/0.4.0-proof.md)
- Continuity pilot protocol: [`runbooks/continuity-pilot.md`](runbooks/continuity-pilot.md)

`v0.4.0` establishes quality, parity, security, profile, and measurement gates. It does not claim that TCE has proven human cloning or improved continuity; those claims remain gated on a longitudinal pilot.
