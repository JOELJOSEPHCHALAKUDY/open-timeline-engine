# Handoff Milestone Validation Runbook

## Scope

This runbook covers malformed `report_execution` milestone payload spikes for shared continuity.

## Controls

- `TCE_HANDOFF_MILESTONE_VALIDATION_MODE=shadow|warn|enforce`
- `TCE_HANDOFF_RETENTION_DAYS` (default: `90`)
- `TCE_RESUME_PACKET_ENABLED=true|false`

## Symptoms

- `milestone_validation.valid=false` appears frequently in `report_execution` responses.
- `handoff_hits_count` drops for explicit handoff queries (`read codex timeline`, `continue claude work ...`).

## Triage

1. Run explicit search:
   - `POST /v1/search` with `query="read codex timeline"` and verify retrieval metadata fields.
2. Validate resume packet:
   - `POST /v1/handoff/resume` with explicit query and `target_owner`.
   - confirm response has prioritized `files[*].anchors` and `retrieval_meta.candidate_count > 0`.
3. Inspect recent execution report responses for:
   - `milestone_validation.mode`
   - `milestone_validation.errors`
4. Check lifecycle status:
   - `GET /v1/admin/lifecycle/status`

## Immediate mitigation

1. If `enforce` is causing widespread rejections:
   - switch to `warn` immediately.
2. If payload quality is low but requests must continue:
   - switch to `shadow`, fix executor payload shape, then re-enable `warn`.

## Required milestone payload (v1)

- `title`
- `payload.files`
- `decision`
- `outcome.status` (`succeeded|failed|blocked`)
- `outcome.next_step`
- optional: `git`, `anchors`

## Rollout gates

- move `shadow -> warn` only when validation failure rate is `<5%`
- move `warn -> enforce` only when validation failure rate remains `<5%` for at least 48h
- keep redaction violations at `0` before and after each phase
