#!/usr/bin/env python3
"""Compare deterministic retrieval reports and enforce rollout gates."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VARIANTS = ("exact", "rare_keywords", "noisy_partial", "cross_user_explicit")
ARTIFACT_TOP10 = {
    "exact": 0.8656,
    "rare_keywords": 0.8048,
    "noisy_partial": 0.5252,
    "cross_user_explicit": 0.5940,
}


@dataclass(frozen=True)
class Gate:
    name: str
    observed: str
    requirement: str
    passed: bool
    halt: bool = True


def load_report(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"report must be a JSON object: {path}")
    return value


def nested(report: dict[str, Any], *path: str) -> Any:
    value: Any = report
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise KeyError(".".join(path))
        value = value[key]
    return value


def number(report: dict[str, Any], *path: str) -> float:
    return float(nested(report, *path))


def variant_top10(report: dict[str, Any], variant: str) -> float:
    return number(report, "results", "by_variant", variant, "equivalent_memory", "top10")


def common_workload_gates(candidate: dict[str, Any]) -> list[Gate]:
    available = int(nested(candidate, "workload", "available_embedded_events"))
    gates = [Gate("embedded corpus", str(available), "== 4649", available == 4649)]
    counts = nested(candidate, "workload", "variant_counts")
    for variant in VARIANTS:
        observed = int(counts.get(variant, 0)) if isinstance(counts, dict) else 0
        gates.append(Gate(f"variant count: {variant}", str(observed), "== 2500", observed == 2500))
    return gates


def source_counts_gate(candidate: dict[str, Any]) -> Gate:
    counts = nested(candidate, "results", "overall", "retrieval_source_counts")
    nonempty = sum(int(count) for key, count in counts.items() if key != "<empty>") if isinstance(counts, dict) else 0
    return Gate("retrieval source attribution", str(counts), "at least one non-empty source", nonempty > 0)


def baseline_gates(candidate: dict[str, Any]) -> list[Gate]:
    gates = common_workload_gates(candidate)
    overall = number(candidate, "results", "overall", "equivalent_memory", "top10")
    gates.append(Gate("overall equivalent top10", f"{overall:.6f}", "0.6974 +/- 0.015", abs(overall - 0.6974) <= 0.015))
    for variant, expected in ARTIFACT_TOP10.items():
        observed = variant_top10(candidate, variant)
        gates.append(
            Gate(
                f"{variant} equivalent top10",
                f"{observed:.6f}",
                f"{expected:.4f} +/- 0.025",
                abs(observed - expected) <= 0.025,
            )
        )
    success = number(candidate, "results", "overall", "http_success_rate")
    planner = number(candidate, "results", "overall", "planner_used_rate")
    gates.extend(
        [
            Gate("HTTP success", f"{success:.6f}", "== 1.0", success == 1.0),
            Gate("planner rate", f"{planner:.6f}", "== 0.0", planner == 0.0),
            source_counts_gate(candidate),
        ]
    )
    return gates


def postfix_gates(reference: dict[str, Any], candidate: dict[str, Any]) -> list[Gate]:
    gates = common_workload_gates(candidate)
    before = {variant: variant_top10(reference, variant) for variant in VARIANTS}
    after = {variant: variant_top10(candidate, variant) for variant in VARIANTS}
    delta = {variant: after[variant] - before[variant] for variant in VARIANTS}
    gates.extend(
        [
            Gate(
                "noisy_partial equivalent top10",
                f"{after['noisy_partial']:.6f} ({delta['noisy_partial']:+.4f})",
                ">= 0.62 and delta >= +0.05",
                after["noisy_partial"] >= 0.62 and delta["noisy_partial"] >= 0.05,
            ),
            Gate(
                "cross_user_explicit equivalent top10",
                f"{after['cross_user_explicit']:.6f} ({delta['cross_user_explicit']:+.4f})",
                ">= 0.68 and delta >= +0.04",
                after["cross_user_explicit"] >= 0.68 and delta["cross_user_explicit"] >= 0.04,
            ),
            Gate(
                "rare_keywords equivalent top10",
                f"{after['rare_keywords']:.6f} ({delta['rare_keywords']:+.4f})",
                "delta >= -0.03 (no-regression floor)",
                delta["rare_keywords"] >= -0.03,
            ),
            Gate(
                "exact equivalent top10",
                f"{after['exact']:.6f} ({delta['exact']:+.4f})",
                "delta >= -0.03",
                delta["exact"] >= -0.03,
            ),
        ]
    )

    overall = number(candidate, "results", "overall", "equivalent_memory", "top10")
    overall_mrr = number(candidate, "results", "overall", "equivalent_memory", "mrr_at_10")
    baseline_mrr = number(reference, "results", "overall", "equivalent_memory", "mrr_at_10")
    cross_scope = number(candidate, "results", "overall", "cross_user_scope_rate")
    baseline_cross_scope = number(reference, "results", "overall", "cross_user_scope_rate")
    p95 = number(candidate, "results", "overall", "latency_ms", "p95")
    success = number(candidate, "results", "overall", "http_success_rate")
    planner = number(candidate, "results", "overall", "planner_used_rate")
    gates.extend(
        [
            Gate("overall equivalent top10 target", f"{overall:.6f}", ">= 0.75", overall >= 0.75, halt=False),
            Gate(
                "overall equivalent MRR@10",
                f"{overall_mrr:.6f} ({overall_mrr - baseline_mrr:+.4f})",
                "delta >= 0",
                overall_mrr >= baseline_mrr,
            ),
            Gate("cross-user scope privacy", f"{cross_scope:.6f}", "<= 0.28", cross_scope <= 0.28),
            Gate(
                "cross-user scope non-increase",
                f"{cross_scope:.6f} ({cross_scope - baseline_cross_scope:+.4f})",
                "prefer <= baseline",
                cross_scope <= baseline_cross_scope,
                halt=False,
            ),
            Gate("p95 latency", f"{p95:.2f} ms", "<= 600 ms", p95 <= 600.0),
            Gate("HTTP success", f"{success:.6f}", "== 1.0", success == 1.0),
            Gate("planner rate", f"{planner:.6f}", "== 0.0", planner == 0.0),
            source_counts_gate(candidate),
        ]
    )

    lexical_counts = nested(candidate, "results", "overall", "lexical_channel_counts")
    total = sum(int(value) for value in lexical_counts.values()) if isinstance(lexical_counts, dict) else 0
    fts_channels = {
        "fts_primary",
        "fts_plus_ilike_fill",
        "fts_union_ilike",
        "rrf_fts_trgm_vector",
    }
    fts_rate = sum(int(lexical_counts.get(key, 0)) for key in fts_channels) / max(1, total)
    fallback_rate = int(lexical_counts.get("ilike_fallback_error", 0)) / max(1, total)
    gates.extend(
        [
            Gate("FTS lexical channel", f"{fts_rate:.2%} {lexical_counts}", ">= 95%", fts_rate >= 0.95),
            Gate("FTS error fallback", f"{fallback_rate:.2%}", "<= 0.5%", fallback_rate <= 0.005),
        ]
    )

    blocked = number(candidate, "results", "overall", "blocked_mean")
    baseline_blocked = number(reference, "results", "overall", "blocked_mean")
    gates.append(
        Gate(
            "blocked mean",
            f"{blocked:.6f} ({blocked - baseline_blocked:+.4f})",
            "prefer <= baseline",
            blocked <= baseline_blocked,
            halt=False,
        )
    )
    return gates


def print_gates(gates: list[Gate]) -> None:
    widths = (42, 36, 30)
    print(f"{'Gate':<{widths[0]}} {'Observed':<{widths[1]}} {'Requirement':<{widths[2]}} Result")
    print("-" * 122)
    for gate in gates:
        status = "PASS" if gate.passed else ("HALT" if gate.halt else "WARN")
        print(f"{gate.name:<{widths[0]}} {gate.observed:<{widths[1]}} {gate.requirement:<{widths[2]}} {status}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--mode", choices=("baseline", "postfix"), required=True)
    args = parser.parse_args()
    reference = load_report(args.reference)
    candidate = load_report(args.candidate)
    if candidate.get("schema") != "tce-retrieval-eval-v2":
        print(f"candidate schema must be tce-retrieval-eval-v2, got {candidate.get('schema')!r}")
        return 2
    gates = baseline_gates(candidate) if args.mode == "baseline" else postfix_gates(reference, candidate)
    print_gates(gates)
    return 1 if any(not gate.passed and gate.halt for gate in gates) else 0


if __name__ == "__main__":
    raise SystemExit(main())
