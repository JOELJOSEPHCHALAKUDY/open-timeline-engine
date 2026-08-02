# Continuity Pilot Runbook

## Purpose

Measure whether shared memory reduces handoff archaeology instead of only counting stored events.

## Identity Setup

Use distinct tokens for each executor. Generate a claim key without storing the token:

```bash
python3 -c 'import hashlib; print("bearer:" + hashlib.sha256(input().encode()).hexdigest())'
```

Configure `TCE_API_TOKENS`, set `TCE_IDENTITY_CLAIMS_MODE=enforce`, and map each hashed credential in `TCE_IDENTITY_CLAIMS_JSON`. Verify the resolved identity with `GET /v1/auth/whoami`; conflicting headers must return `403`.

## Capture Contract

Every mutating completion must use one path:

- takeover directive: `tce.report_execution`
- ordinary executor task: `tce.complete_task`
- Git commit: installed `post-commit` hook
- VS Code task: `TCE: Complete Task`

Use a stable completion key. Repeating a key returns the original outbox, event, and handoff IDs.

## Pilot Procedure

1. Capture a pre-rollout baseline, then run at least four weeks with Codex and Claude using distinct bound identities in the same workspace.
2. Use `tce.get_resume_packet` before manually searching the repository.
3. Open the first suggested file and call `tce.check_context` with the same session ID. This records `file_opened` automatically. If that hook is unavailable, call `tce.report_resume_feedback` with `phase=file_opened`.
4. When enough context is recovered to make productive progress, report `phase=productive`.
5. At task completion, report `phase=completed`, the opened file/rank, anchor result, correction result, archaeology tool calls/tokens, and outcome status.
6. Mark `correct_file=true` only when the first file was actionable; record `correct_anchor` separately.
7. Mark `correction_required=true` when the receiving executor had to replace material context, files, or next steps.
8. Review `GET /v1/continuity/pilot/status?days=30` and `/dashboard/behavior-review` weekly.

## Latency gates

Run the dedicated resume-path check against both runtimes:

```bash
TCE_LATENCY_BASE_URL=http://127.0.0.1:8080 \
TCE_LATENCY_TOKEN="$TCE_API_TOKEN" \
python tests/load/continuity_latency_check.py
```

The default gate is resume p95 `<=120ms`. Keep the existing takeover p95 gate at `<=300ms`; completion delivery and pilot telemetry must not run on takeover's normal hot path.

## Metrics And Gates

- `handoff_capture_coverage`: delivered handoffs / eligible terminal directives, consumed mutation obligations, and standalone completion submissions. Target `>=0.80`, then `>=0.95`.
- `correct_file_rate`: first selected file was correct. Target `>=0.80`.
- `correct_file_at_1_rate` and `correct_file_at_3_rate`: recommended-file rank quality.
- `correct_anchor_rate`: recommended anchor was actionable.
- `correction_rate`: resume required material correction. Target `<=0.20`.
- `median_handoff_age_at_resume_ms` and `p95_handoff_age_at_resume_ms`: age of the handoff when resume was requested.
- `median_time_to_first_file_ms` and `p95_time_to_first_file_ms`: resume request to first file open.
- `median_active_resume_ms` and `p95_active_resume_ms`: resume request to productive work. Use these for time-to-resume comparisons.
- `median_completion_after_resume_ms` and `p95_completion_after_resume_ms`: resume request to reported completion.
- `median_archaeology_tool_calls` and `median_archaeology_tokens`: repository archaeology cost after requesting a packet.
- `median_retrieval_latency_ms`: server retrieval latency, not human time-to-resume.
- `outbox_dead_count`: must remain `0`.

`median_time_to_resume_ms` and `p95_time_to_resume_ms` are deprecated compatibility aliases for handoff age, not active resume duration.

Do not claim behavioral cloning or continuity improvement from event volume alone. Promote only after longitudinal metrics improve without increasing correction rate.

## Incidents

- Pending Full outbox: verify Redis and worker health.
- Pending Lite outbox: restart Lite to invoke its durable drain.
- Dead outbox: inspect `handoff_outbox.last_error`, repair the persistence fault, reset the row to `pending`, and run `tce_worker.jobs.handoff_outbox.run`.
- Coverage drop: verify Stop hooks, Git hooks, MCP tool availability, and unresolved capability completion obligations.
- Drift alert: review recent shadow predictions and pending memory candidates before promoting behavioral rules.
