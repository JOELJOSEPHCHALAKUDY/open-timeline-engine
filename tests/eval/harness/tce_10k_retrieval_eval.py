#!/usr/bin/env python3
"""Deterministic, privacy-preserving aggregate retrieval evaluation for TCE."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import random
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import requests
from dotenv import dotenv_values

SOURCES = ("tce-chat-backfill-codex", "tce-chat-backfill-claude")
VARIANTS = ("exact", "rare_keywords", "noisy_partial", "cross_user_explicit")
PREFIX_RE = re.compile(r"^Historical\s+(?:codex|claude)\s+user\s+input:\s*", re.IGNORECASE)
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:#@+\-]{1,39}")
SECRET_REPLACEMENTS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{8,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+"), "credential [REDACTED]"),
)
STOPWORDS = {
    "about", "after", "again", "also", "and", "any", "are", "because", "been", "before",
    "but", "can", "could", "did", "does", "doing", "done", "each", "for", "from", "had",
    "has", "have", "here", "how", "into", "its", "just", "like", "make", "more", "need",
    "not", "now", "of", "okay", "on", "only", "or", "our", "please", "right", "should",
    "so", "some", "that", "the", "then", "there", "these", "they", "this", "through", "to",
    "use", "used", "using", "want", "was", "we", "what", "when", "where", "which", "why",
    "will", "with", "would", "yes", "you", "your", "historical", "codex", "claude", "user",
    "input", "timeline", "memory", "read", "continue", "work",
}
_thread_local = threading.local()


def normalize_title(value: str) -> str:
    value = PREFIX_RE.sub("", value or "").strip()
    return re.sub(r"\s+", " ", value).strip().lower()


def redact(value: str) -> str:
    for pattern, replacement in SECRET_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    return value


def tokens_for(value: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in TOKEN_RE.findall(value.lower()):
        token = token.strip("._/:#@+-")
        if len(token) < 3 or token in STOPWORDS or token in seen:
            continue
        if token.count("/") > 5 or sum(ch.isdigit() for ch in token) > max(5, len(token) // 2):
            continue
        if any(marker in token for marker in ("[redacted]", "api_key", "password=", "token=")):
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return round(ordered[index], 2)


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def load_events(dsn: str) -> list[dict[str, str]]:
    query = """
        SELECT e.id::text, e.source, e.title
        FROM events e
        JOIN event_embeddings ee ON ee.event_id = e.id
        WHERE e.source = ANY(%s)
        ORDER BY e.ts, e.id
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cursor:
        cursor.execute(query, (list(SOURCES),))
        return [
            {"id": row[0], "source": row[1], "title": row[2] or ""}
            for row in cursor.fetchall()
        ]


def build_scenarios(events: list[dict[str, str]], count: int, seed: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = random.Random(seed)
    shuffled = list(events)
    rng.shuffle(shuffled)

    doc_tokens = {event["id"]: tokens_for(normalize_title(event["title"])) for event in events}
    document_frequency: Counter[str] = Counter()
    for event_tokens in doc_tokens.values():
        document_frequency.update(set(event_tokens))
    total_docs = max(1, len(events))
    idf = {
        token: math.log((total_docs + 1) / (frequency + 1)) + 1.0
        for token, frequency in document_frequency.items()
    }

    equivalent_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events:
        equivalent_ids[(event["source"], normalize_title(event["title"]))].add(event["id"])

    selected: list[dict[str, Any]] = []
    for index in range(count):
        event = shuffled[index % len(shuffled)]
        variant = VARIANTS[index % len(VARIANTS)]
        normalized = normalize_title(event["title"])
        event_tokens = doc_tokens[event["id"]]
        ranked = sorted(
            event_tokens,
            key=lambda token: (-idf.get(token, 0.0), event_tokens.index(token), token),
        )
        rare = set(ranked[: min(7, len(ranked))])
        rare_ordered = [token for token in event_tokens if token in rare]
        keyword_query = " ".join(rare_ordered) or normalized[:600] or "previous task"

        if variant == "exact":
            query = normalized[:900] or "previous task"
            caller = "codex-executor"
        elif variant == "rare_keywords":
            query = keyword_query
            caller = "codex-executor"
        elif variant == "noisy_partial":
            local_rng = random.Random(f"{seed}:{event['id']}:{index}")
            candidates = event_tokens[:24]
            keep_count = max(1, min(12, math.ceil(len(candidates) * 0.60)))
            kept_indexes = sorted(local_rng.sample(range(len(candidates)), min(keep_count, len(candidates)))) if candidates else []
            kept = [candidates[item] for item in kept_indexes]
            query = "find earlier discussion " + (" ".join(kept) or normalized[:500] or "previous task")
            caller = "codex-executor"
        else:
            query = "read codex timeline about " + keyword_query
            caller = "claude-executor"

        query = redact(re.sub(r"\s+", " ", query).strip())[:1000]
        eq_ids = equivalent_ids[(event["source"], normalized)]
        selected.append(
            {
                "index": index,
                "target_id": event["id"],
                "source": event["source"],
                "variant": variant,
                "query": query,
                "caller": caller,
                "equivalent_ids": eq_ids,
                "ambiguous": len(eq_ids) > 1,
                "scenario_hash": sha12(f"{event['id']}:{variant}:{index}"),
            }
        )

    coverage = len({item["target_id"] for item in selected})
    metadata = {
        "available_embedded_events": len(events),
        "unique_events_covered": coverage,
        "coverage_rate": round(coverage / max(1, len(events)), 6),
        "ambiguous_event_count": sum(1 for event in events if len(equivalent_ids[(event["source"], normalize_title(event["title"]))]) > 1),
        "variant_counts": dict(Counter(item["variant"] for item in selected)),
        "source_counts": dict(Counter(item["source"] for item in selected)),
    }
    return selected, metadata


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


def run_one(base_url: str, token: str, scenario: dict[str, Any], timeout: float) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": scenario["caller"],
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": "personal",
        "X-TCE-User": scenario["caller"],
        "Content-Type": "application/json",
    }
    payload = {
        "query": scenario["query"],
        "filters": {"source": scenario["source"]},
        "k": 10,
    }
    started = time.perf_counter()
    status_code = 0
    error_class = ""
    hit_ids: list[str] = []
    hit_titles: list[str] = []
    metadata: dict[str, Any] = {}
    try:
        response = get_session().post(f"{base_url}/v1/search", headers=headers, json=payload, timeout=timeout)
        status_code = response.status_code
        if status_code == 200:
            body = response.json()
            hits = ((body.get("result") or {}).get("hits") or [])
            hit_ids = [str(hit.get("id") or "") for hit in hits]
            hit_titles = [normalize_title(str(hit.get("title") or "")) for hit in hits]
            metadata = body.get("metadata") or {}
        else:
            error_class = f"http_{status_code}"
    except requests.Timeout:
        error_class = "timeout"
    except requests.RequestException:
        error_class = "request_error"
    except (TypeError, ValueError, KeyError):
        error_class = "response_parse_error"
    latency_ms = (time.perf_counter() - started) * 1000.0

    strict_rank = next((index + 1 for index, value in enumerate(hit_ids) if value == scenario["target_id"]), 0)
    equivalent_ids = scenario["equivalent_ids"]
    equivalent_rank = next((index + 1 for index, value in enumerate(hit_ids) if value in equivalent_ids), 0)
    if not equivalent_rank:
        target_normalized = normalize_title(next(iter([scenario["query"]]), ""))
        # ID matching is authoritative; title fallback is intentionally not used for transformed queries.
        _ = target_normalized, hit_titles

    return {
        "scenario_hash": scenario["scenario_hash"],
        "source": scenario["source"],
        "variant": scenario["variant"],
        "ambiguous": scenario["ambiguous"],
        "status_code": status_code,
        "error_class": error_class,
        "latency_ms": latency_ms,
        "strict_rank": strict_rank,
        "equivalent_rank": equivalent_rank,
        "cross_user_scope_applied": bool(metadata.get("cross_user_scope_applied", False)),
        "planner_used": bool(metadata.get("planner_used", False)),
        "retrieval_source": str((((metadata.get("policy") or {}).get("retrieval") or {}).get("retrieval_source") or "")),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(item["latency_ms"]) for item in results]
    total = len(results)
    status_counts = Counter(str(item["status_code"]) for item in results)
    error_counts = Counter(item["error_class"] for item in results if item["error_class"])

    def rank_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
        denominator = max(1, len(items))
        strict = [int(item["strict_rank"]) for item in items]
        equivalent = [int(item["equivalent_rank"]) for item in items]
        item_latencies = [float(item["latency_ms"]) for item in items]
        return {
            "count": len(items),
            "http_success_rate": round(sum(item["status_code"] == 200 for item in items) / denominator, 6),
            "strict_event": {
                "top1": round(sum(rank == 1 for rank in strict) / denominator, 6),
                "top5": round(sum(0 < rank <= 5 for rank in strict) / denominator, 6),
                "top10": round(sum(0 < rank <= 10 for rank in strict) / denominator, 6),
                "mrr_at_10": round(sum((1.0 / rank) if rank else 0.0 for rank in strict) / denominator, 6),
            },
            "equivalent_memory": {
                "top1": round(sum(rank == 1 for rank in equivalent) / denominator, 6),
                "top5": round(sum(0 < rank <= 5 for rank in equivalent) / denominator, 6),
                "top10": round(sum(0 < rank <= 10 for rank in equivalent) / denominator, 6),
                "mrr_at_10": round(sum((1.0 / rank) if rank else 0.0 for rank in equivalent) / denominator, 6),
            },
            "latency_ms": {
                "p50": percentile(item_latencies, 0.50),
                "p95": percentile(item_latencies, 0.95),
                "p99": percentile(item_latencies, 0.99),
                "max": round(max(item_latencies), 2) if item_latencies else 0.0,
                "mean": round(statistics.fmean(item_latencies), 2) if item_latencies else 0.0,
            },
            "cross_user_scope_rate": round(sum(item["cross_user_scope_applied"] for item in items) / denominator, 6),
            "planner_used_rate": round(sum(item["planner_used"] for item in items) / denominator, 6),
        }

    by_variant = {
        variant: rank_metrics([item for item in results if item["variant"] == variant])
        for variant in VARIANTS
    }
    by_source = {
        source: rank_metrics([item for item in results if item["source"] == source])
        for source in SOURCES
    }
    unambiguous = [item for item in results if not item["ambiguous"]]
    ambiguous = [item for item in results if item["ambiguous"]]
    misses = [item for item in results if not item["equivalent_rank"]]
    slowest = sorted(results, key=lambda item: item["latency_ms"], reverse=True)[:20]

    return {
        "overall": rank_metrics(results),
        "unambiguous": rank_metrics(unambiguous),
        "ambiguous": rank_metrics(ambiguous),
        "by_variant": by_variant,
        "by_source": by_source,
        "http_status_counts": dict(status_counts),
        "error_counts": dict(error_counts),
        "equivalent_miss_count": len(misses),
        "equivalent_miss_sample_hashes": [item["scenario_hash"] for item in misses[:25]],
        "slowest_scenarios": [
            {
                "scenario_hash": item["scenario_hash"],
                "variant": item["variant"],
                "latency_ms": round(item["latency_ms"], 2),
                "status_code": item["status_code"],
            }
            for item in slowest
        ],
        "latency_sample_count": len(latencies),
        "total": total,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--db-dsn", default="postgresql://postgres:postgres@127.0.0.1:5432/tce_eval_10k")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--scenarios", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=20260730)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    env = dotenv_values(args.env_file)
    token = str(env.get("TCE_API_TOKEN") or "").strip()
    if not token:
        raise SystemExit("TCE_API_TOKEN is missing")

    events = load_events(args.db_dsn)
    if not events:
        raise SystemExit("No embedded imported events found")
    scenarios, workload_metadata = build_scenarios(events, args.scenarios, args.seed)

    warmup_scenarios = scenarios[: min(40, len(scenarios))]
    warmup_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrency, 8)) as executor:
        warmup_results = list(
            executor.map(lambda item: run_one(args.base_url, token, item, args.timeout), warmup_scenarios)
        )
    print(json.dumps({"phase": "warmup", "summary": summarize(warmup_results)["overall"]}, sort_keys=True), flush=True)

    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(run_one, args.base_url, token, item, args.timeout) for item in scenarios]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            results.append(future.result())
            if completed % 500 == 0 or completed == len(futures):
                elapsed = time.perf_counter() - started
                print(
                    json.dumps(
                        {
                            "phase": "benchmark",
                            "completed": completed,
                            "total": len(futures),
                            "elapsed_seconds": round(elapsed, 1),
                            "requests_per_second": round(completed / max(elapsed, 0.001), 2),
                            "http_failures": sum(item["status_code"] != 200 for item in results),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

    elapsed = time.perf_counter() - started
    report = {
        "schema": "tce-retrieval-eval-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "isolation": {
            "database": "tce_eval_10k",
            "api": args.base_url,
            "redis_db": 15,
            "production_data_mutated": False,
        },
        "configuration": {
            "scenario_count": args.scenarios,
            "concurrency": args.concurrency,
            "seed": args.seed,
            "k": 10,
            "timeout_seconds": args.timeout,
            "warmup_requests_excluded": len(warmup_results),
        },
        "workload": workload_metadata,
        "wall_time_seconds": round(elapsed, 2),
        "throughput_requests_per_second": round(len(results) / max(elapsed, 0.001), 3),
        "warmup": summarize(warmup_results),
        "results": summarize(results),
        "limitations": [
            "Synthetic retrieval variants measure recall and serving performance, not longitudinal human-behavior fidelity.",
            "Strict event-id recall penalizes duplicate user inputs; equivalent-memory recall accepts identical imported memories.",
            "Source filtering isolates platform history and does not measure unrestricted corpus discovery.",
        ],
    }
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"phase": "complete", "output": str(output_path), "summary": report["results"]["overall"]}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
