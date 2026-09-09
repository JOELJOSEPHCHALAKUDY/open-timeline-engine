from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_text

CONTROL_SCHEMA_VERSION = "v1"

# The registry is deliberately closed. Unknown capabilities fail closed instead
# of trusting a caller-supplied risk classification.
CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "filesystem.read": {"mutating": False, "risk_tier": "low"},
    "filesystem.write": {"mutating": True, "risk_tier": "medium"},
    "git.read": {"mutating": False, "risk_tier": "low"},
    "git.write": {"mutating": True, "risk_tier": "high"},
    "network.read": {"mutating": False, "risk_tier": "medium"},
    "network.write": {"mutating": True, "risk_tier": "high"},
    "process.inspect": {"mutating": False, "risk_tier": "low"},
    "process.execute": {"mutating": True, "risk_tier": "high"},
    "tce.memory.review": {"mutating": True, "risk_tier": "medium"},
}

_ACTION_TOKEN_RE = re.compile(r"[^a-z0-9_.:/-]+")
_NUMBER_RE = re.compile(r"\b\d+\b")


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def redact_control_text(value: Any, *, limit: int) -> tuple[str, bool]:
    redacted, applied = redact_text(_clip(value, limit))
    return redacted.strip(), bool(applied)


def normalize_capability_operation(
    *,
    capability: str,
    action: str,
    resource: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_capability = _ACTION_TOKEN_RE.sub("", capability.strip().lower())[:80]
    normalized_action = _ACTION_TOKEN_RE.sub("_", action.strip().lower()).strip("_")[:80]
    normalized_resource, redacted = redact_control_text(resource, limit=500)
    canonical_arguments = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    digest_payload = json.dumps(
        {
            "capability": normalized_capability,
            "action": normalized_action,
            "resource": normalized_resource,
            "arguments": json.loads(canonical_arguments),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "capability": normalized_capability,
        "action": normalized_action,
        "resource": normalized_resource,
        "action_digest": hashlib.sha256(digest_payload.encode("utf-8")).hexdigest(),
        "redaction_applied": redacted,
    }


def capability_policy(capability: str) -> dict[str, Any]:
    policy = CAPABILITY_REGISTRY.get(capability)
    if policy is None:
        return {
            "known": False,
            "mutating": True,
            "risk_tier": "critical",
            "decision": "blocked",
            "reason": "unknown capability",
        }
    return {
        "known": True,
        "mutating": bool(policy["mutating"]),
        "risk_tier": str(policy["risk_tier"]),
        "decision": "allow",
        "reason": "registered capability",
    }


def hash_capability_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_token_match(token: str, expected_hash: str) -> bool:
    import hmac

    return hmac.compare_digest(hash_capability_token(token), expected_hash)


def normalize_process_action(value: Any) -> str:
    normalized = _ACTION_TOKEN_RE.sub("_", _clip(value, 120).lower()).strip("_")
    return _NUMBER_RE.sub("<n>", normalized) or "unknown"


def mine_process_models(
    rows: Iterable[dict[str, Any]],
    *,
    min_support: int = 2,
    max_steps: int = 12,
) -> list[dict[str, Any]]:
    """Mine deterministic session variants and direct-follow transitions."""

    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        session_id = _clip(row.get("session_id"), 160)
        if not session_id:
            continue
        sessions[session_id].append(row)

    variants: dict[tuple[str, ...], dict[str, Any]] = {}
    for session_id, session_rows in sessions.items():
        session_rows.sort(key=lambda item: (str(item.get("ts") or ""), str(item.get("id") or "")))
        steps: list[str] = []
        terminal_results: list[str] = []
        evidence_ids: list[str] = []
        for row in session_rows:
            action = normalize_process_action(row.get("action_kind") or row.get("action"))
            if not steps or steps[-1] != action:
                steps.append(action)
            result = _clip(row.get("result"), 40).lower()
            if result:
                terminal_results.append(result)
            if row.get("id"):
                evidence_ids.append(str(row["id"]))
        steps = steps[: max(2, min(30, max_steps))]
        if len(steps) < 2:
            continue
        signature = tuple(steps)
        bucket = variants.setdefault(
            signature,
            {"sessions": [], "results": [], "evidence_ids": []},
        )
        bucket["sessions"].append(session_id)
        bucket["results"].extend(terminal_results)
        bucket["evidence_ids"].extend(evidence_ids)

    output: list[dict[str, Any]] = []
    required_support = max(2, min(100, int(min_support)))
    for variant_steps, bucket in variants.items():
        support = len(bucket["sessions"])
        if support < required_support:
            continue
        result_counts = Counter(bucket["results"])
        terminal_count = sum(result_counts.values())
        succeeded = sum(
            count
            for result, count in result_counts.items()
            if result in {"success", "succeeded", "completed", "allow", "continue"}
        )
        success_rate = succeeded / max(1, terminal_count)
        support_factor = 1.0 - math.exp(-support / 5.0)
        reliability = round((0.65 * success_rate) + (0.35 * support_factor), 4)
        transitions = [
            {"from": variant_steps[index], "to": variant_steps[index + 1], "count": support, "probability": 1.0}
            for index in range(len(variant_steps) - 1)
        ]
        signature_text = " -> ".join(variant_steps)
        output.append(
            {
                "process_signature": hashlib.sha256(signature_text.encode("utf-8")).hexdigest(),
                "name": f"Observed workflow: {variant_steps[0]} to {variant_steps[-1]}",
                "steps": list(variant_steps),
                "transitions": transitions,
                "support": support,
                "success_rate": round(success_rate, 4),
                "reliability": reliability,
                "source_sessions": sorted(bucket["sessions"])[:40],
                "evidence_ids": list(dict.fromkeys(bucket["evidence_ids"]))[:100],
                "status": "candidate",
                "schema_version": CONTROL_SCHEMA_VERSION,
            }
        )
    output.sort(key=lambda item: (-item["reliability"], -item["support"], item["process_signature"]))
    return output[:100]


_RESOLUTION_STATES = ("pending", "unanswered", "missing_label", "extraction_error", "missed_capture", "abandoned")


def shadow_evaluation_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [dict(row) for row in rows]
    total = len(values)
    # Only a resolved row has ground truth. Prospective predictions that are still
    # pending, unanswered, or lost to a capture gap are counted separately and never
    # averaged into precision — scoring them either way would corrupt the number.
    # Rows without the key predate P1 and were always resolved at write time.
    resolved = [row for row in values if str(row.get("resolution_state") or "resolved") == "resolved"]
    non_abstained = [row for row in resolved if not bool(row.get("abstained"))]
    correct = sum(1 for row in non_abstained if bool(row.get("correct")))
    recent = resolved[: min(30, len(resolved))]
    previous = resolved[min(30, len(resolved)) : min(60, len(resolved))]
    prospective_decided = [row for row in non_abstained if str(row.get("prediction_stage") or "retrospective") == "prospective"]
    state_counts = {f"{state}_count": sum(1 for row in values if str(row.get("resolution_state") or "resolved") == state) for state in _RESOLUTION_STATES}

    def precision(window: list[dict[str, Any]]) -> float | None:
        decided = [row for row in window if not bool(row.get("abstained"))]
        if not decided:
            return None
        return sum(1 for row in decided if bool(row.get("correct"))) / len(decided)

    recent_precision = precision(recent)
    previous_precision = precision(previous)
    drift_delta = (
        recent_precision - previous_precision
        if recent_precision is not None and previous_precision is not None
        else None
    )
    return {
        "sample_count": total,
        "non_abstained_count": len(non_abstained),
        "coverage": round(len(non_abstained) / max(1, total), 4),
        "precision": round(correct / max(1, len(non_abstained)), 4),
        "abstention_rate": round((total - len(non_abstained)) / max(1, total), 4),
        "recent_precision": round(recent_precision, 4) if recent_precision is not None else None,
        "previous_precision": round(previous_precision, 4) if previous_precision is not None else None,
        "drift_delta": round(drift_delta, 4) if drift_delta is not None else None,
        "drift_alert": bool(drift_delta is not None and drift_delta <= -0.15),
        "resolved_count": len(resolved),
        **state_counts,
        "prospective_count": sum(1 for row in values if str(row.get("prediction_stage") or "retrospective") == "prospective"),
        "retrospective_count": sum(1 for row in values if str(row.get("prediction_stage") or "retrospective") != "prospective"),
        "prospective_precision": (
            round(sum(1 for row in prospective_decided if bool(row.get("correct"))) / len(prospective_decided), 4)
            if prospective_decided else None
        ),
        "schema_version": CONTROL_SCHEMA_VERSION,
    }


def normalize_counterfactual(payload: dict[str, Any]) -> dict[str, Any]:
    decision, decision_redacted = redact_control_text(payload.get("decision"), limit=500)
    alternative, alternative_redacted = redact_control_text(payload.get("alternative"), limit=500)
    expected, expected_redacted = redact_control_text(payload.get("expected_outcome"), limit=1000)
    assumptions: list[str] = []
    redacted_any = decision_redacted or alternative_redacted or expected_redacted
    for raw in (payload.get("assumptions") or [])[:20]:
        value, applied = redact_control_text(raw, limit=300)
        redacted_any = redacted_any or applied
        if value and value not in assumptions:
            assumptions.append(value)
    return {
        "decision": decision,
        "alternative": alternative,
        "expected_outcome": expected,
        "assumptions": assumptions,
        "confidence": max(0.0, min(1.0, float(payload.get("confidence", 0.5) or 0.0))),
        "redaction_applied": redacted_any,
    }


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
