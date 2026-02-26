# API Contract (`/v1`)

## Endpoints

- `POST /v1/events`
- `POST /v1/events/batch`
- `GET /v1/events/{id}`
- `POST /v1/search`
- `POST /v1/context_bundle`
- `GET /v1/patterns`
- `POST /v1/patterns/feedback`
- `GET /v1/runtime/mode`
- `PUT /v1/runtime/mode`
- `POST /v1/clone/advice`
- `POST /v1/clone/arbitrate`
- `GET /v1/health`
- `GET /v1/metrics`

## Compatibility

- Schema version field required in all event payloads
- Additive changes only for minor versions
- Breaking changes only in major versions

## Security defaults

- Bearer + optional mTLS (`dual`)
- Sensitivity level `3` blocked by default in output paths
- Redaction applied before embedding and response serialization
- Advisor role is read-only on mutation endpoints

## Optional identity headers

- `X-TCE-Consumer`: logical consumer ID (for audit separation between AIs)
- `X-TCE-Role`: `user`, `executor`, or `advisor`

## Additive endpoint reference

### `GET /v1/workflow/templates`

- Availability: full and lite.
- Purpose: list learned workflow templates used by takeover/MCP hinting.
- Notes: response is additive and backward compatible with existing clients.
