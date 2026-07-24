"""Metrics must aggregate across uvicorn workers.

With PROMETHEUS_MULTIPROC_DIR set, /v1/metrics must serve a
MultiProcessCollector registry (values from the shared mmap dir), not the
per-process default registry — otherwise every scrape lands on a random
worker and counters appear to jump/reset.
"""

import pytest
from fastapi.testclient import TestClient


def _client() -> TestClient:
    from tce_api.main import app

    return TestClient(app)


def test_metrics_default_mode_serves_process_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    resp = _client().get("/v1/metrics")
    assert resp.status_code == 200
    # Default registry includes interpreter collectors.
    assert "python_gc_objects_collected_total" in resp.text


def test_metrics_multiprocess_mode_serves_shared_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    resp = _client().get("/v1/metrics")
    assert resp.status_code == 200
    # Multiprocess registries have no per-process interpreter collectors;
    # an empty mmap dir must yield output without them (and not crash).
    assert "python_gc_objects_collected_total" not in resp.text


def test_directive_outcome_counters_exist() -> None:
    from tce_api import main as api_main

    api_main.DIRECTIVE_REPORT_COUNT.labels(state="succeeded", failure_class="none").inc()
    api_main.DIRECTIVE_RETRY_SCHEDULED_COUNT.inc()
    api_main.PERMIT_DECISION_COUNT.labels(decision="allow", risk_tier="low").inc()
