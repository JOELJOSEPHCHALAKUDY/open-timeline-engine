from __future__ import annotations

import math
from typing import Any


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return _clamp(dot / (norm_a * norm_b))


def goal_embedding_text(title: str, description: str, reasoning: str = "") -> str:
    return f"{title.strip()}\n{description.strip()}\n{reasoning.strip()}".strip()


def dedupe_candidates_by_similarity(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        embedding = candidate.get("goal_embedding")
        if not isinstance(embedding, list):
            output.append(candidate)
            continue
        merged = False
        for existing in output:
            existing_embedding = existing.get("goal_embedding")
            sim = cosine_similarity(embedding, existing_embedding if isinstance(existing_embedding, list) else None)
            if sim > 0.90:
                existing_ids = set(existing.get("evidence_event_ids", []))
                existing_ids.update(candidate.get("evidence_event_ids", []))
                existing["evidence_event_ids"] = list(existing_ids)
                existing["confidence"] = max(float(existing.get("confidence", 0.0)), float(candidate.get("confidence", 0.0)))
                existing["similarity_merge_score"] = round(sim, 4)
                merged = True
                break
            if 0.75 <= sim <= 0.90:
                related = existing.get("related_candidates", [])
                if not isinstance(related, list):
                    related = []
                related.append(
                    {
                        "title": candidate.get("title", ""),
                        "similarity": round(sim, 4),
                    }
                )
                existing["related_candidates"] = related[:5]
        if not merged:
            output.append(candidate)
    return output


def selection_score(
    *,
    affective_priority: float,
    context_similarity: float,
    recent_event_similarity: float,
    pattern_confidence: float,
) -> float:
    score = (
        0.40 * _clamp(affective_priority)
        + 0.30 * _clamp(context_similarity)
        + 0.20 * _clamp(recent_event_similarity)
        + 0.10 * _clamp(pattern_confidence)
    )
    return round(_clamp(score), 4)
