# Load Tests

This folder contains Locust scenarios for ingestion bursts and bundle latency checks.

Behavior projection read latency can be checked against a live Full or Lite API after enabling
`TCE_BEHAVIOR_PROJECTIONS_ENABLED=true`:

```bash
uv run python tests/load/behavior_projection_latency_check.py
```

The default gate is p95 `<=120ms` over 35 measured requests after five warm-up requests. Set
`TCE_PROJECTION_LATENCY_SEED_COUNT=100` to exercise the maximum projection size before measuring.

Prospective pilot assignment latency uses the same p95 `<=120ms` gate and covers deterministic arm
selection, context generation, persistence, and audit metadata:

```bash
TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=true \
  uv run python tests/load/behavior_projection_pilot_latency_check.py
```
