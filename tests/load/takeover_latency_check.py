from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    idx = (len(data) - 1) * q
    low = int(idx)
    high = min(low + 1, len(data) - 1)
    frac = idx - low
    if low == high:
        return data[low]
    return data[low] + ((data[high] - data[low]) * frac)


def _request(base_url: str, path: str, headers: dict[str, str], payload: dict[str, Any] | None = None) -> tuple[int, str]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, body


def main() -> int:
    base_url = os.getenv("TCE_LATENCY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.getenv("TCE_LATENCY_TOKEN", "local-dev-token")
    session_id = os.getenv("TCE_LATENCY_SESSION", "ci-latency")
    user_id = os.getenv("TCE_LATENCY_USER", "ci-latency")
    workspace = os.getenv("TCE_LATENCY_WORKSPACE", "personal")
    turns = max(10, int(os.getenv("TCE_LATENCY_TURNS", "40")))
    warmup = max(0, int(os.getenv("TCE_LATENCY_WARMUP", "5")))
    p95_budget_ms = float(os.getenv("TCE_LATENCY_P95_BUDGET_MS", "300"))

    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": user_id,
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user_id,
        "Content-Type": "application/json",
    }

    # Activate takeover session once.
    status, body = _request(
        base_url,
        "/v1/takeover/step",
        headers,
        {
            "session_id": session_id,
            "message": "beru take over for latency regression check",
            "activation_mode_default": "takeover",
        },
    )
    if status != 200:
        raise SystemExit(f"activation failed status={status} body={body[:500]}")

    samples_ms: list[float] = []
    try:
        for turn in range(1, turns + 1):
            t0 = time.perf_counter()
            status, body = _request(
                base_url,
                "/v1/takeover/step",
                headers,
                {
                    "session_id": session_id,
                    "message": f"continue latency check turn {turn}",
                    "activation_mode_default": "takeover",
                },
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if status != 200:
                raise SystemExit(f"turn {turn} failed status={status} body={body[:500]}")
            samples_ms.append(elapsed_ms)
    finally:
        params = urllib.parse.urlencode({"session_id": session_id, "persona_mode": "normal"})
        try:
            _request(base_url, f"/v1/takeover/reset?{params}", headers, payload={})
        except (urllib.error.URLError, urllib.error.HTTPError):
            pass

    measured = samples_ms[warmup:] if len(samples_ms) > warmup else samples_ms
    p95_ms = percentile(measured, 0.95)
    p50_ms = percentile(measured, 0.50)
    p99_ms = percentile(measured, 0.99)
    max_ms = max(measured) if measured else 0.0

    print(
        json.dumps(
            {
                "samples": len(measured),
                "warmup_ignored": warmup,
                "p50_ms": round(p50_ms, 2),
                "p95_ms": round(p95_ms, 2),
                "p99_ms": round(p99_ms, 2),
                "max_ms": round(max_ms, 2),
                "budget_p95_ms": p95_budget_ms,
                "pass": p95_ms <= p95_budget_ms,
            },
            indent=2,
        )
    )

    if p95_ms > p95_budget_ms:
        raise SystemExit(f"takeover latency regression: p95={p95_ms:.2f}ms > budget={p95_budget_ms:.2f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

