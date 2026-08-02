# TCE 10k Retrieval Evaluation: Indexed-Primary FTS

Generated: 2026-08-02

## Decision

Accept the indexed-primary Full retrieval path. Keep RRF available behind
`TCE_SEARCH_RRF_ENABLED=false`; its isolated 1k arm reduced MRR and noisy-query
recall while adding latency.

The rare-keyword gate is a no-regression floor (`delta >= -3pp`). Its previous
upper bound rejected genuine indexed relevance gains and did not identify a
privacy, corpus, planner, or HTTP confound.

## Frozen Workload

- Embedded events: 4,649
- Scenarios: 10,000, with 2,500 per variant
- Seed: `20260730`
- Concurrency: 8
- Planner and query expansion: disabled
- HTTP failures: 0

## Results

| Metric | Baseline B | Indexed-primary | Delta |
|---|---:|---:|---:|
| Overall equivalent top-10 | 0.6817 | 0.9093 | +0.2276 |
| Overall equivalent MRR@10 | 0.4215 | 0.7617 | +0.3402 |
| Noisy partial top-10 | 0.4996 | 0.8868 | +0.3872 |
| Cross-user explicit top-10 | 0.5896 | 0.8664 | +0.2768 |
| Rare keywords top-10 | 0.7816 | 0.9376 | +0.1560 |
| Exact top-10 | 0.8560 | 0.9464 | +0.0904 |
| p95 latency | 451.61 ms | 382.30 ms | -69.31 ms |
| Throughput | 32.84 req/s | 34.39 req/s | +4.7% |
| Cross-user scope rate | 0.2519 | 0.2519 | unchanged |

## Retrieval Channels

- `fts_primary`: 9,740 requests
- `fts_plus_ilike_fill`: 135 requests
- `ilike_only`: 125 requests
- FTS query error fallbacks: 0

The optimization avoids running the legacy ILIKE full scan when FTS already
fills the requested result set. This removes the sequential query cost that
caused the rejected experiment's `686.99ms` p95.

## Verification

- Ruff: pass
- mypy: 206 source files, pass
- Unit/security/MCP: 229 passed
- Integration: 57 passed, 1 skipped
- Alembic upgrade/downgrade/upgrade: pass
- Full/Lite OpenAPI parity: pass
- Lite Docker E2E: pass
- Full Docker smoke E2E: pass
- Full continuity/outbox/MCP live E2E: 6 passed
