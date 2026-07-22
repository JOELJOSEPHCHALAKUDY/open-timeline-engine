from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    position = (len(data) - 1) * q
    low = int(position)
    high = min(low + 1, len(data) - 1)
    return data[low] + ((data[high] - data[low]) * (position - low))


def get_projection(base_url: str, headers: dict[str, str], format_name: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"format": format_name})
    request = urllib.request.Request(
        f"{base_url}/v1/behavior/projections/current?{query}",
        method="GET",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError("behavior projection returned non-object JSON")
    return payload


def post_json(base_url: str, path: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
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
    user = os.getenv("TCE_LATENCY_USER", "projection-latency")
    workspace = os.getenv("TCE_LATENCY_WORKSPACE", "personal")
    subject = os.getenv("TCE_LATENCY_BEHAVIOR_SUBJECT", user)
    samples = max(10, int(os.getenv("TCE_PROJECTION_LATENCY_SAMPLES", "40")))
    warmup = max(0, int(os.getenv("TCE_PROJECTION_LATENCY_WARMUP", "5")))
    seed_count = max(0, min(100, int(os.getenv("TCE_PROJECTION_LATENCY_SEED_COUNT", "0"))))
    budget_ms = float(os.getenv("TCE_PROJECTION_P95_BUDGET_MS", "120"))
    format_name = os.getenv("TCE_PROJECTION_LATENCY_FORMAT", "markdown")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": user,
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user,
        "X-TCE-Behavior-Subject": subject,
    }

    seed_prefix = f"projection-latency-{time.time_ns()}"
    for index in range(seed_count):
        stored = post_json(
            base_url,
            "/v1/behavior/evidence",
            headers,
            {
                "situation_type": "projection_latency",
                "situation_summary": f"{seed_prefix} decision {index}",
                "objective": "Measure deterministic behavior projection latency",
                "context_snapshot": {"component": "behavior_projection", "index": index},
                "constraints": {"latency_budget_ms": budget_ms},
                "available_choices": ["bounded deterministic projection", "unbounded summary"],
                "selected_choice": "bounded deterministic projection",
                "rationale": "Keep projection cost bounded and avoid an LLM in the read path",
                "action_taken": "Render active canonical evidence",
                "outcome": "Projection generated",
                "memory_class": "decision",
                "evidence_source": "explicit",
                "confidence": 1.0,
                "schema_version": "v1",
            },
        )
        if not stored.get("stored"):
            raise SystemExit(f"latency seed {index} was not stored: {stored}")

    timings: list[float] = []
    body_hash: str | None = None
    last_payload: dict[str, Any] = {}
    for _ in range(samples):
        started = time.perf_counter()
        last_payload = get_projection(base_url, headers, format_name)
        timings.append((time.perf_counter() - started) * 1000.0)
        current_hash = str(last_payload.get("content_sha256") or "")
        if not current_hash:
            raise SystemExit("projection response did not include content_sha256")
        if body_hash is not None and current_hash != body_hash:
            raise SystemExit("projection content changed during a read-only latency run")
        body_hash = current_hash

    measured = timings[warmup:] if len(timings) > warmup else timings
    p95_ms = percentile(measured, 0.95)
    result = {
        "samples": len(measured),
        "warmup_ignored": warmup,
        "seeded_records": seed_count,
        "format": format_name,
        "evidence_count": int(last_payload.get("evidence_count", 0) or 0),
        "content_chars": len(str(last_payload.get("content") or "")),
        "p50_ms": round(percentile(measured, 0.50), 2),
        "p95_ms": round(p95_ms, 2),
        "p99_ms": round(percentile(measured, 0.99), 2),
        "max_ms": round(max(measured), 2),
        "budget_p95_ms": budget_ms,
        "pass": p95_ms <= budget_ms,
    }
    print(json.dumps(result, indent=2))
    if not result["pass"]:
        raise SystemExit(f"behavior projection latency regression: p95={p95_ms:.2f}ms > budget={budget_ms:.2f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
