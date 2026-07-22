from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from typing import Any


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    position = (len(data) - 1) * q
    low = int(position)
    high = min(low + 1, len(data) - 1)
    return data[low] + ((data[high] - data[low]) * (position - low))


def post(base_url: str, path: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{path} returned non-object JSON")
        return decoded


def main() -> int:
    base_url = os.getenv("TCE_LATENCY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.getenv("TCE_LATENCY_TOKEN", "local-dev-token")
    owner = os.getenv("TCE_LATENCY_USER", "ci-latency")
    workspace = os.getenv("TCE_LATENCY_WORKSPACE", "personal")
    session_id = os.getenv("TCE_LATENCY_SESSION", "continuity-latency")
    samples = max(10, int(os.getenv("TCE_CONTINUITY_LATENCY_SAMPLES", "40")))
    warmup = max(0, int(os.getenv("TCE_CONTINUITY_LATENCY_WARMUP", "5")))
    budget_ms = float(os.getenv("TCE_CONTINUITY_P95_BUDGET_MS", "120"))
    bound_identity = os.getenv("TCE_LATENCY_BOUND_IDENTITY", "false").lower() in {"1", "true", "yes"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if not bound_identity:
        headers.update(
            {
                "X-TCE-Consumer": owner,
                "X-TCE-Role": "executor",
                "X-TCE-Workspace": workspace,
                "X-TCE-User": owner,
            }
        )

    title = f"Continuity latency probe {uuid.uuid4().hex}"
    completion = post(
        base_url,
        "/v1/completions",
        headers,
        {
            "session_id": session_id,
            "completion_key": f"latency:{uuid.uuid4()}",
            "source": "latency-check",
            "state": "succeeded",
            "title": title,
            "payload": {"files": ["tests/load/continuity_latency_check.py"]},
            "decision": "Measure the persisted resume path under its production contract",
            "outcome": {"status": "succeeded", "next_step": "Retrieve this exact handoff"},
            "anchors": [{"file": "tests/load/continuity_latency_check.py", "line": 1, "symbol": "main"}],
            "milestone_schema": "v1",
        },
    )
    if completion.get("delivery_status") != "delivered":
        raise SystemExit(f"completion was not delivered: {completion}")

    timings: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        packet = post(
            base_url,
            "/v1/handoff/resume",
            headers,
            {
                "query": f"continue {owner} work {title}",
                "target_owner": owner,
                "session_id": session_id,
                "k": 5,
                "include_cross_user": True,
            },
        )
        timings.append((time.perf_counter() - started) * 1000.0)
        if packet.get("task_summary") != title:
            raise SystemExit(f"resume selector returned the wrong task: {packet.get('task_summary')!r}")

    measured = timings[warmup:] if len(timings) > warmup else timings
    result = {
        "samples": len(measured),
        "warmup_ignored": warmup,
        "p50_ms": round(percentile(measured, 0.50), 2),
        "p95_ms": round(percentile(measured, 0.95), 2),
        "p99_ms": round(percentile(measured, 0.99), 2),
        "max_ms": round(max(measured), 2),
        "budget_p95_ms": budget_ms,
        "pass": percentile(measured, 0.95) <= budget_ms,
    }
    print(json.dumps(result, indent=2))
    if not result["pass"]:
        raise SystemExit(f"continuity latency regression: p95={result['p95_ms']}ms > budget={budget_ms}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
