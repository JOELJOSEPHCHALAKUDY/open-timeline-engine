# Takeover Scenarios

> Public release track: `v0.4.0` (pre-1.0).
> `V4`/`V5`/`V6`/`V7`/`V8` labels below are internal milestones.

This document defines deterministic takeover behavior across CLI, API, MCP, VSCode, and Browser flows.

## Endpoints and Tools

- API:
  - `POST /v1/takeover/step`
  - `GET /v1/takeover/state`
  - `POST /v1/takeover/reset`
  - `POST /v1/takeover/goals/discover`
  - `POST /v1/takeover/goals/precompute`
  - `GET /v1/takeover/goals`
  - `GET /v1/takeover/goals/cache/status`
  - `POST /v1/takeover/goals/cache/invalidate`
  - `POST /v1/takeover/goals/{goal_id}/select`
  - `POST /v1/takeover/permit`
  - `POST /v1/takeover/permit/resolve`
  - `GET /v1/takeover/autonomy/status`
  - `POST /v1/takeover/autonomy/tick`
  - `GET /v1/takeover/notices`
  - `POST /v1/takeover/notices/{notice_id}/ack`
  - `POST /v1/takeover/execution/claim`
  - `POST /v1/takeover/execution/report`
  - `GET /v1/takeover/execution/status`
- MCP:
  - `tce.takeover_step`
  - `tce.get_takeover_state`
  - `tce.reset_takeover_state`
  - `tce.takeover_discover_goals`
  - `tce.takeover_precompute_goals`
  - `tce.get_takeover_goals`
  - `tce.get_takeover_goal_cache_status`
  - `tce.invalidate_takeover_goal_cache`
  - `tce.select_takeover_goal`
  - `tce.request_execution_permit`
  - `tce.resolve_execution_permit`
  - `tce.get_autonomy_status`
  - `tce.takeover_autonomy_tick`
  - `tce.get_takeover_notices`
  - `tce.ack_takeover_notice`
  - `tce.claim_execution`
  - `tce.report_execution`
  - `tce.get_execution_status`

## Core state machine

1. Incoming message hits `takeover_step`.
2. Stop phrase check runs first (stop wins).
3. Activation phrase or mode-override phrase may activate/update mode.
4. If inactive, return `action=inactive`.
5. If active:
  - classify executor output (`question|handoff|suggestion|decisive|empty`)
  - enforce decisive output when mode is `takeover`
  - run high-risk safety gate (`allow|confirm_required|blocked`)
6. Persist state and return audited response.

## Milestone V4 additions

- Goal queue:
  - autonomous discovery ranks candidates from timeline errors, blockers, and active objective.
- Permit gate:
  - mutating actions in takeover mode require an execution permit.
- Continuity watchdog:
  - prolonged session gaps increment continuity violations.
  - repeated violations downgrade takeover to suggest until stabilized.

## Milestone V5 additions

- Affective goal intelligence:
  - goals now carry `goal_kind`, `selection_score`, and `affective_scores`.
- Two-tier cache:
  - L1 memory cache and L2 persisted cache accelerate repeated goal selection.
- Cache metadata in `takeover_step`:
  - `goal_cache_hit`
  - `goal_cache_source`
  - `selected_goal_score_breakdown`
- Cache invalidation triggers include:
  - feedback writes
  - goal selection
  - stand-down/reset
  - permit resolution

## Milestone V6 additions

- Proactive notice surfacing (pull-based):
  - `autonomy/tick` creates deduped session notices from high-priority goals.
  - notices are consumed by dashboard/API/MCP pulls (no push transport assumption).
- Closed-loop retry orchestration:
  - directive outcomes are reported through `execution/report`.
  - failures are normalized into `failure_class` and mapped to bounded retry strategy.
- Mandatory mutation gating:
  - mutating directives require permit + execution claim.
  - claim TTL is capped by permit TTL when permit is present.
  - unresolved directives lock objective drift until resolved or abandoned.

## Scenario matrix

- Activation phrases:
  - Persona defaults supported (`normal`, `naruto`, `shadow`)
  - Comma/no-comma/case-insensitive matching supported
  - Custom activation phrases are allowed
- Stop phrases:
  - Stop phrase in same message takes precedence
- Mode switching:
  - `beru suggest` switches to suggest mode
  - `beru take over` switches to takeover mode
  - Suggest mode auto-promotes on question/handoff when enabled
- Classification:
  - explicit `?` questions
  - implicit question forms (`if you want`, `should I`, `do you want me to`)
  - handoff language (`need your input`, `ask for confirmation`)
  - suggestion lists (bullets/numbered options/next steps)
- Safety:
  - high-risk operations trigger confirmation prompt
  - confirm keyword resumes flow
  - deny keyword blocks action and clears pending state

## Troubleshooting

- `inactive` unexpectedly:
  - check session id mismatch
  - check activation phrase and persona mode
  - inspect `expires_at` in `get_takeover_state`
- takeover not enforced:
  - verify mode is `takeover` in returned state
  - verify client is using `takeover_step` each message
- safety prompt loops:
  - send exact confirm keyword or deny keyword configured in policy
  - check pending safety in `state.takeover_context.pending_safety`
- mutation flow pauses unexpectedly:
  - check `execution_permit_required` and `execution_permit_id` in `takeover_step`
  - if permit exists but directive is pending, call `execution/claim` before continuing

## Milestone V8 additions relevant to takeover

- Low-context turns can be expanded via retrieval triggers (Milestone V7) but remain bounded by budget/timeout.
- If post-retrieval quality remains below escalation threshold, takeover sets `needs_human=true` instead of guessing.
- `context/brief` can be used before complex takeover turns to preload explicit boundaries from memory rules.
- Scoped P0/P1 memory rules act as hard preference context and should be treated as non-optional constraints.
- Episode annotations allow operators to pin corrective decisions (`decision`, `avoid`, `authority_level`) that future takeover turns can reuse.

## Scenario addendum: workflow-hint assisted takeover

- When takeover context matches known workflow patterns, response metadata may include `workflow_hints`.
- Executor should use hints to accelerate execution while still obeying safety and directive lifecycle rules.
