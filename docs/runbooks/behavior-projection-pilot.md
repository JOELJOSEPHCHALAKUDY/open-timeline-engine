# Behavior Projection Pilot Runbook

## Purpose

Run the four-arm prospective comparison without treating generated memory as evidence or claiming efficacy before real longitudinal data exists.

## Preconditions

- Apply Alembic revision `20260722_0031` for Full. Lite creates the same schema during database initialization.
- Bind the executor to the intended workspace and behavior subject.
- Keep `TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED=false` while collecting the pilot.
- Set a stable deployment-specific `TCE_BEHAVIOR_PILOT_ASSIGNMENT_SALT` before the first trial. Changing it changes future arm allocation.
- Enable `TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=true` only for the enrolled deployment.

## Trial Flow

1. Before an agent decides, call `tce.assign_behavior_projection_pilot` with a unique stable `trial_key`, situation, objective, and candidate choices.
2. Give the returned `context_payload` to the agent unchanged. Do not fetch behavioral memory separately for that trial.
3. After the independent human decision or observed outcome is known, call `tce.report_behavior_projection_pilot_outcome` once.
4. Include the agent top choice, top three choices, confidence, actual choice, action/workflow similarity, corrections, regret, personalization errors, malicious activation, and evidence IDs actually used.
5. Read `tce.get_behavior_projection_pilot_status` for aggregate progress. Do not infer success while it reports `collecting`.

Assignment and outcome retries are safe when the payload is identical. Reusing a trial key or assignment with a different payload returns HTTP `409` and must be investigated rather than overwritten.

## Gate Interpretation

`collecting` means one or more pre-registered collection gates are incomplete:

- 28 real elapsed days
- 30 completed outcomes in every arm
- at least 80% overall completion coverage
- maximum arm p95 retrieval latency of 120 ms
- zero malicious-memory activations

`ready_for_review` means the collection and pre-registered quality comparisons passed. It does not enable autonomy automatically.

`failed_quality` means the collection is sufficient but a projection arm missed the quality comparison against no-memory or canonical structured retrieval.

`failed_safety` means at least one malicious-memory activation was reported. Disable the pilot read path, preserve the records, and review the implicated assignment citations and canonical evidence before resuming.

## Integrity Checks

For Full:

```sql
SELECT variant, COUNT(*) AS assigned, COUNT(o.id) AS completed
FROM behavior_projection_pilot_assignments a
LEFT JOIN behavior_projection_pilot_outcomes o ON o.assignment_id = a.id
GROUP BY variant
ORDER BY variant;
```

For Lite, run the same query against the SQLite database.

Verify these invariants:

- each assignment has one stable variant and one context SHA-256
- the no-memory arm has an empty `citations_json`
- every assignment has at most one outcome
- persisted `request_json`, `context_json`, and outcome notes contain no test secret markers
- `decision_observations` count does not change when only pilot outcomes are reported

## Performance Check

Run against the active API:

```bash
TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=true \
  uv run python tests/load/behavior_projection_pilot_latency_check.py
```

The default gate is p95 `<=120ms`. Full and Lite CI also exercise pilot assignment under mixed Locust traffic.

## Retention And Rollback

Assignments and their cascading outcomes use `TCE_BEHAVIOR_CONTROL_RETENTION_DAYS`, default 365 days. Assignment expiry prevents late outcome mutation but does not delete the longitudinal record early.

Set `TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=false` to remove assignment, outcome, and status access immediately. Keep the tables for audit and later analysis; no schema rollback is required.
