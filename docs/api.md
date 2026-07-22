# API Contract (`/v1`)

## Endpoints

- `POST /v1/events`
- `POST /v1/events/batch`
- `GET /v1/events/{event_id}`
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
- `GET /v1/auth/whoami`
- `POST /v1/completions`
- `POST /v1/handoff/resume`
- `POST /v1/continuity/pilot/feedback`
- `GET /v1/continuity/pilot/status`
- `POST /v1/capabilities/grants`
- `POST /v1/capabilities/consume`
- `POST /v1/behavior/evidence`
- `POST /v1/behavior/predict`
- `POST /v1/behavior/evaluate`
- `GET /v1/behavior/evaluations`
- `GET /v1/behavior/projections/current`
- `GET /v1/behavior/projections/decisions/{topic}`
- `GET /v1/behavior/projections/evidence/{observation_id}`
- `GET /v1/behavior/projections/review`
- `GET /v1/behavior/projections/review.html`
- `POST /v1/behavior/projections/pilot/assign`
- `POST /v1/behavior/projections/pilot/outcome`
- `GET /v1/behavior/projections/pilot/status`
- `GET /v1/behavior/calibration/scenarios`
- `POST /v1/behavior/calibration/answer`
- `POST /v1/behavior/processes/mine`
- `GET /v1/behavior/processes`
- `GET /v1/behavior/shadow/status`
- `GET /v1/behavior/reviews`
- `POST /v1/behavior/reviews/{review_id}/resolve`
- `POST /v1/behavior/counterfactuals`
- `GET /v1/behavior/counterfactuals`
- `POST /v1/behavior/counterfactuals/{counterfactual_id}/resolve`

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
- `X-TCE-Behavior-Subject`: human profile used by behavior evidence, prediction, and clone advice; defaults to `X-TCE-User`

## Additive endpoint reference

### `GET /v1/workflow/templates`

- Availability: full and lite.
- Purpose: list learned workflow templates used by takeover/MCP hinting.
- Notes: response is additive and backward compatible with existing clients.

### Capability broker

- Grants are short-lived, exact-operation, and one-use.
- Mutating capabilities require a claimed directive and an allowed execution permit.
- Only token hashes are persisted. Operation arguments are represented by a canonical digest, not stored in plaintext.
- Unknown capabilities and changed operation digests fail closed.

### Behavioral control plane

- Process models and inferred/backfilled evidence are review-gated before they can influence autonomy.
- Shadow predictions are prospective and remain non-authoritative.
- Counterfactual records remain separate from learning evidence.
- Full and Lite return the same typed response shapes.

### Durable continuity

- `POST /v1/completions` validates milestone v1, commits an idempotent outbox row, then materializes the linked event and handoff record.
- Pending outbox rows are retried by the worker in Full and at Lite startup. Delivery is idempotent and dead-letters after 10 attempts.
- `POST /v1/handoff/resume` records resume latency and selected-file telemetry without changing the packet response shape.
- Pilot feedback measures correct-file and correction rates. `GET /v1/continuity/pilot/status` reports these with time-to-resume and handoff capture coverage.
- `GET /v1/auth/whoami` returns the server-resolved caller identity. In `enforce` mode, bound claims cannot be overridden by request headers.
