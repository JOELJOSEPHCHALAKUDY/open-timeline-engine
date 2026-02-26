# Phase 0 Baseline (pilot: `workspace=personal`, `session=phase0-canary`)

Generated at: `2026-02-25T00:45Z`

## Endpoint snapshots

### `GET /v1/dashboard/human-score?session_id=phase0-canary`
- `score`: `66`
- `band`: `advanced`
- `subscores`:
  - `clone_readiness`: `82.0`
  - `execution_quality`: `80.0`
  - `affective_alignment`: `57.19`
  - `goal_coherence`: `27.25`

### `GET /v1/takeover/autonomy/readiness?session_id=phase0-canary`
- `score`: `0`
- `band`: `not_ready`
- `gate_passed`: `false`
- `status_phase`: `warmup`
- key metrics:
  - `turn_count`: `0`
  - `terminal_directive_count`: `0`
  - `execution_success_rate`: `0.0`
  - `needs_human_rate`: `1.0`
  - `avg_decision_confidence`: `0.0`
  - `avg_context_quality`: `0.0`
  - `eval_floor`: `0.0` (`eval_missing=true`)

### `GET /v1/takeover/autonomy/project-kpis?session_id=phase0-canary`
- `passed`: `false`
- `band`: `project_autonomy_blocked`
- key metrics:
  - `project_count`: `0`
  - `completed_project_count`: `0`
  - `project_completion_rate`: `0.0`
  - `manual_interventions_per_project`: `0.0`
  - `verification_pass_rate`: `1.0`
  - `reopen_rate_after_completion`: `0.0`

### `GET /v1/retrieval/eval/status?session_id=phase0-canary`
- `latest`:
  - `run_id`: `7a30b0d7-81c6-428c-adff-24ee1028b930`
  - `style_alignment`: `0.5877`
  - `constraint_compliance`: `0.6263`
  - `decision_traceability`: `0.4265`
  - `followup_reduction`: `0.5937`
  - `completed_at`: `2026-02-25T00:48:15.617161Z`
- `history_count`: `1`

## Latency baseline

Command:

```bash
TCE_LATENCY_BASE_URL='http://127.0.0.1:8080' \
TCE_LATENCY_TOKEN='local-dev-token' \
TCE_LATENCY_SESSION='phase0-canary' \
TCE_LATENCY_USER='phase0-canary' \
TCE_LATENCY_WORKSPACE='personal' \
python3 tests/load/takeover_latency_check.py
```

Result:
- `samples`: `35` (`warmup_ignored=5`)
- `p50_ms`: `46.19`
- `p95_ms`: `4881.73`
- `p99_ms`: `5463.5`
- `max_ms`: `5616.8`
- `budget_p95_ms`: `300.0`
- `pass`: `false`

## Promotion gates (targets)

- Quality gate: human score `>=85` and `+8` absolute over baseline (`66 -> target >= 85`).
- Escalation gate: needs-human-rate improves by `>=20%` from baseline (`1.0`).
- Latency gate: `p95 <= max(300ms, baseline*1.10)` -> `p95 <= 5369.90ms` for this baseline run.
