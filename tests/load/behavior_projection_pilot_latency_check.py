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


def post_json(
    base_url: str,
    path: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        decoded = json.loads(response.read().decode())
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{path} returned non-object JSON")
    return decoded


def main() -> int:
    base_url = os.getenv("TCE_LATENCY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.getenv("TCE_LATENCY_TOKEN", "local-dev-token")
    user = os.getenv("TCE_LATENCY_USER", "projection-pilot-latency")
    workspace = os.getenv("TCE_LATENCY_WORKSPACE", "personal")
    subject = os.getenv("TCE_LATENCY_BEHAVIOR_SUBJECT", user)
    samples = max(10, int(os.getenv("TCE_PROJECTION_PILOT_LATENCY_SAMPLES", "40")))
    warmup = max(0, int(os.getenv("TCE_PROJECTION_PILOT_LATENCY_WARMUP", "5")))
    budget_ms = float(os.getenv("TCE_PROJECTION_PILOT_P95_BUDGET_MS", "120"))
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": user,
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user,
        "X-TCE-Behavior-Subject": subject,
    }
    prefix = f"projection-pilot-latency-{uuid.uuid4().hex}"
    timings: list[float] = []
    variants: dict[str, int] = {}
    for index in range(samples):
        started = time.perf_counter()
        assignment = post_json(
            base_url,
            "/v1/behavior/projections/pilot/assign",
            headers,
            {
                "trial_key": f"{prefix}-{index}",
                "situation_type": "projection_pilot_latency",
                "situation_summary": "Choose a bounded behavior-memory context",
                "objective": "Measure prospective pilot assignment latency",
                "constraints": {"latency_budget_ms": budget_ms},
                "context_snapshot": {"sample": index},
                "candidate_choices": ["bounded context", "no context"],
            },
        )
        timings.append((time.perf_counter() - started) * 1000.0)
        if assignment.get("projection_learning_eligible") is not False:
            raise SystemExit("pilot assignment was incorrectly marked as learning evidence")
        variant = str(assignment.get("variant") or "")
        variants[variant] = variants.get(variant, 0) + 1

    measured = timings[warmup:] if len(timings) > warmup else timings
    p95_ms = percentile(measured, 0.95)
    result = {
        "samples": len(measured),
        "warmup_ignored": warmup,
        "variants": variants,
        "p50_ms": round(percentile(measured, 0.50), 2),
        "p95_ms": round(p95_ms, 2),
        "p99_ms": round(percentile(measured, 0.99), 2),
        "max_ms": round(max(measured), 2),
        "budget_p95_ms": budget_ms,
        "pass": p95_ms <= budget_ms,
    }
    print(json.dumps(result, indent=2))
    if not result["pass"]:
        raise SystemExit(
            f"behavior projection pilot latency regression: p95={p95_ms:.2f}ms > "
            f"budget={budget_ms:.2f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
