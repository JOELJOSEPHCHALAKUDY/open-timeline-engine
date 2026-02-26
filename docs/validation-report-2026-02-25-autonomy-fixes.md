# Autonomy Fix Validation Report (2026-02-25)

## Scope

This report captures validation for the autonomy hardening changes shipped on February 25, 2026:

1. Semantic classification hardening in takeover/situation/risk routing.
2. Bounded retry/backoff for retrieval paths (embedding, graph query, Qdrant, SQLite-lite).
3. Pattern feedback signal wiring from real execution outcomes and clone feedback.

## Verification Results

### Build

```bash
uv run python -m compileall shared services tests
```

Result: Passed.

### Test

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration/test_takeover_api.py tests/integration/test_graph_features.py tests/integration/test_retrieval_eval_api.py -q
```

Result: Passed.

- Unit: `93 passed`
- Integration: `5 passed`

### Lint (autonomy delta)

```bash
uvx ruff check \
  services/tce_api/tce_api/config.py \
  services/tce_api/tce_api/search.py \
  services/tce_lite_api/tce_lite_api/config.py \
  services/tce_worker/tce_worker/jobs/patterns.py \
  shared/tce_shared/autonomy_goals.py \
  shared/tce_shared/situation.py \
  shared/tce_shared/takeover.py \
  tests/unit/test_situation_classifier.py \
  tests/unit/test_takeover_classifier.py \
  tests/unit/test_worker_jobs.py \
  tests/unit/test_search_retry.py
```

Result: Passed (`All checks passed!`).

## Latency Gate

```bash
python3 tests/load/takeover_latency_check.py
```

Result:

- Samples: `35` (`5` warmup ignored)
- p50: `27.74ms`
- p95: `87.91ms`
- p99: `405.41ms`
- max: `548.34ms`
- Budget p95: `300ms`
- Gate: `pass=true`

## Outcome

The implemented autonomy fixes are validated against build/test/lint and the low-latency p95 gate.
