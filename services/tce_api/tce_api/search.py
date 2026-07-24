from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from typing import Any, TypedDict

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_model_gateway import get_gateway
from tce_shared.autonomy_context import (
    coerce_summary_l1,
    plan_retrieval_subqueries,
    propagate_episode_score,
    summary_coverage_ratio,
)
from tce_shared.events import EventSearchHit, EventSearchRequest
from tce_shared.handoff import handoff_intent, task_overlap_score
from tce_shared.policy import ConsumerContext

from .cache_clients import get_redis_client
from .config import get_settings
from .policy import PolicyEngine

logger = logging.getLogger(__name__)

_EMBED_CACHE_TTL = 600  # 10 minutes
_EMBED_MEMO_MAX_SIZE = 256
_EMBED_MEMO: dict[str, tuple[float, list[float]]] = {}
_EMBED_TIMEOUT_COOLDOWN_MEMO_MAX_SIZE = 256
_EMBED_TIMEOUT_COOLDOWN_MEMO: dict[str, float] = {}
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_RETRIEVAL_COUNTERS: dict[str, int] = {
    "none": 0,
    "pgvector_ann": 0,
    "lexical_only": 0,
    "hybrid_fallback": 0,
    "qdrant": 0,
}
_QUERY_EXPANSION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "bug": ("error", "issue", "defect"),
    "fix": ("repair", "resolve"),
    "implement": ("build", "create"),
    "refactor": ("cleanup", "simplify"),
    "test": ("verify", "validation"),
    "deploy": ("release", "rollout"),
}
_RETRYABLE_ERROR_TOKENS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection refused",
    "connection reset",
    "network",
    "unavailable",
    "rate limit",
    "too many requests",
    "503",
    "502",
    "504",
)
_CROSS_USER_HINT_RE = re.compile(r"(?:@|user[:=]|owner[:=]|from\s+)([a-z0-9][a-z0-9._-]{1,63})")
_CROSS_USER_TRIGGER_RE = re.compile(r"\b(cross[-\s]?user|across users?|same workspace|shared workspace|other user)\b")
_CROSS_USER_HISTORY_RE = re.compile(
    r"\b(history|chat|conversation|session|discuss|discussion|recent work|recent changes|what did)\b"
)
_CROSS_USER_HANDOFF_RE = re.compile(
    r"\b(continue|resume|pick up|handoff|hand off|follow up)\b.*\b(codex|claude)\b"
)
_CROSS_USER_DIRECT_NAMES = ("codex", "claude")


class _MMRCandidate(TypedDict):
    score: float
    row: dict[str, Any]
    tokens: set[str]


def _normalize_title(value: Any) -> str:
    token = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return token


def _error_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_retryable_error(value: Any) -> bool:
    text = _error_text(value)
    if not text:
        return False
    return any(token in text for token in _RETRYABLE_ERROR_TOKENS)


def _normalize_owner_id_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _query_requests_cross_user_memory(query_text: str) -> bool:
    lowered = _collapse_whitespace(query_text.lower())
    if not lowered:
        return False
    if _CROSS_USER_TRIGGER_RE.search(lowered):
        return True
    if _CROSS_USER_HANDOFF_RE.search(lowered):
        return True
    if "memory" in lowered and "from " in lowered:
        return True
    if "timeline" in lowered and "from " in lowered:
        return True
    if "vice versa" in lowered:
        return True
    if any(name in lowered for name in _CROSS_USER_DIRECT_NAMES):
        if "memory" in lowered:
            return True
        if "timeline" in lowered:
            return True
        if _CROSS_USER_HISTORY_RE.search(lowered):
            return True
    return False


def _extract_owner_hints(query_text: str) -> set[str]:
    lowered = _collapse_whitespace(query_text.lower())
    hints = {_normalize_owner_id_token(match.group(1)) for match in _CROSS_USER_HINT_RE.finditer(lowered)}
    for token in re.split(r"[^a-z0-9._-]+", lowered):
        normalized = _normalize_owner_id_token(token)
        if normalized in _CROSS_USER_DIRECT_NAMES:
            hints.add(normalized)
    return {hint for hint in hints if hint}


def _owner_matches_hint(owner_id: str, hint: str) -> bool:
    owner = _normalize_owner_id_token(owner_id)
    token = _normalize_owner_id_token(hint)
    if not owner or not token:
        return False
    return token == owner or token in owner or owner in token


def _milestone_score_boost(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    if str(payload.get("milestone_schema") or "").strip().lower() == "v1":
        return 0.12
    return 0.0


def _load_handoff_records_map(
    db: Session,
    *,
    workspace_id: str,
    owner_ids: list[str],
    max_records: int,
) -> dict[str, dict[str, Any]]:
    if not owner_ids:
        return {}
    try:
        rows = db.execute(
            text(
                """
                SELECT id, event_id, anchors_json, schema_version
                FROM handoff_records
                WHERE workspace_id = :workspace_id
                  AND owner_id = ANY(CAST(:owner_ids AS TEXT[]))
                ORDER BY ts DESC
                LIMIT :record_limit
                """
            ),
            {
                "workspace_id": workspace_id,
                "owner_ids": owner_ids,
                "record_limit": max(1, int(max_records)),
            },
        ).mappings().all()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return {}
    by_event: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            continue
        by_event[event_id] = {
            "id": str(row.get("id") or ""),
            "has_anchors": isinstance(row.get("anchors_json"), list) and len(row.get("anchors_json") or []) > 0,
            "schema_version": str(row.get("schema_version") or "").strip().lower(),
        }
    return by_event


def _resolve_owner_scope(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    query_text: str,
) -> tuple[set[str], bool, list[str]]:
    normalized_owner = _normalize_owner_id_token(owner_id)
    default_scope = {normalized_owner} if normalized_owner else set()
    if not _query_requests_cross_user_memory(query_text):
        return default_scope, False, sorted(default_scope)

    owner_candidates = set(default_scope)
    try:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT context->>'_tce_owner' AS owner_id
                FROM events
                WHERE context->>'_tce_workspace' = :workspace_id
                  AND context->>'_tce_owner' IS NOT NULL
                LIMIT 400
                """
            ),
            {"workspace_id": workspace_id},
        ).mappings().all()
        for row in rows:
            normalized = _normalize_owner_id_token(row.get("owner_id"))
            if normalized:
                owner_candidates.add(normalized)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return default_scope, False, sorted(default_scope)

    if not owner_candidates:
        return default_scope, False, sorted(default_scope)

    hints = _extract_owner_hints(query_text)
    if hints:
        matched = {
            owner_candidate
            for owner_candidate in owner_candidates
            if any(_owner_matches_hint(owner_candidate, hint) for hint in hints)
        }
    else:
        matched = set(owner_candidates)

    if normalized_owner:
        matched.add(normalized_owner)
    if not matched:
        return default_scope, False, sorted(default_scope)
    applied = matched != default_scope
    return matched, applied, sorted(matched)[:6]


def _expand_owner_scope_from_rows(
    rows: list[dict[str, Any]],
    *,
    workspace_id: str,
    current_scope: set[str],
) -> set[str]:
    normalized_workspace = _normalize_owner_id_token(workspace_id)
    expanded_scope = set(current_scope)
    for row in rows:
        context = row.get("context") if isinstance(row, dict) else None
        if not isinstance(context, dict):
            continue
        event_workspace = _normalize_owner_id_token(context.get("_tce_workspace"))
        if event_workspace and event_workspace != normalized_workspace:
            continue
        event_owner = _normalize_owner_id_token(context.get("_tce_owner"))
        if event_owner:
            expanded_scope.add(event_owner)
    return expanded_scope


def _consumer_is_executor_consumer(consumer: str) -> bool:
    normalized = _normalize_owner_id_token(consumer)
    return normalized.endswith("-executor") or normalized.endswith("-executer") or "-executor" in normalized or "-executer" in normalized


def _collapse_whitespace(text: str) -> str:
    return " ".join(str(text or "").split())


def _run_with_retry[T](
    fn: Callable[[], T],
    *,
    attempts: int,
    base_backoff_ms: int,
    max_retry_budget_ms: int,
    jitter_ratio: float = 0.0,
) -> T:
    attempts = max(1, int(attempts))
    jitter_ratio = max(0.0, min(1.0, float(jitter_ratio)))
    started = monotonic()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - exercised through callers
            last_exc = exc
            if attempt >= attempts - 1 or not _is_retryable_error(exc):
                raise
            elapsed_ms = int((monotonic() - started) * 1000)
            remaining_ms = int(max_retry_budget_ms) - elapsed_ms
            if remaining_ms <= 0:
                raise
            base_delay_ms = max(1, int(base_backoff_ms)) * (2 ** attempt)
            if jitter_ratio > 0.0:
                jitter_window_ms = int(base_delay_ms * jitter_ratio)
                if jitter_window_ms > 0:
                    base_delay_ms += random.randint(-jitter_window_ms, jitter_window_ms)
                    base_delay_ms = max(1, base_delay_ms)
            delay_ms = min(remaining_ms, base_delay_ms)
            sleep(float(delay_ms) / 1000.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop exited unexpectedly")


def _citation_dup_ratio(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    counts = Counter(_normalize_title(row.get("title")) for row in rows if row.get("title"))
    duplicates = sum(max(0, count - 1) for count in counts.values())
    return max(0.0, min(1.0, float(duplicates) / float(max(1, len(rows)))))


def _expand_query_terms(query_text: str, *, max_terms: int) -> list[str]:
    tokens = [token.strip().lower() for token in re.split(r"\W+", query_text) if token.strip()]
    expanded: list[str] = []
    for token in tokens:
        for synonym in _QUERY_EXPANSION_SYNONYMS.get(token, ()):
            if synonym not in tokens and synonym not in expanded:
                expanded.append(synonym)
            if len(expanded) >= max_terms:
                return expanded
    return expanded[:max_terms]


def _search_scale_trigger_met(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
) -> bool:
    now = datetime.now(tz=UTC)
    try:
        event_count = int(
            db.execute(
                text(
                    """
                    SELECT COUNT(1)
                    FROM events
                    WHERE (context->>'_tce_workspace' IS NULL OR context->>'_tce_workspace' = :workspace_id)
                      AND (context->>'_tce_owner' IS NULL OR context->>'_tce_owner' = :owner_id)
                    """
                ),
                {"workspace_id": workspace_id, "owner_id": owner_id},
            ).scalar()
            or 0
        )
        if event_count >= 10000:
            return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        pass
    try:
        row = db.execute(
            text(
                """
                SELECT
                    COUNT(1) AS samples,
                    AVG((COALESCE(style_alignment, 0) + COALESCE(decision_traceability, 0)) / 2.0) AS avg_score
                FROM retrieval_eval_runs
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND completed_at >= :cutoff
                """
            ),
            {
                "workspace_id": workspace_id,
                "user_id": owner_id,
                "cutoff": now - timedelta(days=7),
            },
        ).mappings().first()
        if row is not None and int(row.get("samples") or 0) >= 3:
            if float(row.get("avg_score") or 1.0) < 0.72:
                return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        pass
    return False


def _embed_cache_key(query: str) -> str:
    normalized_query = _collapse_whitespace(query).strip().lower()
    return f"tce:embed:{hashlib.sha256(normalized_query.encode()).hexdigest()[:16]}"


def _embed_timeout_cooldown_key(query: str) -> str:
    normalized_query = _collapse_whitespace(query).strip().lower()
    return f"tce:embed:cooldown:{hashlib.sha256(normalized_query.encode()).hexdigest()[:16]}"


def _is_embedding_timeout_cooldown_active(query: str, *, settings: Any) -> bool:
    if not bool(getattr(settings, "search_embedding_timeout_cooldown_enabled", True)):
        return False
    key = _embed_timeout_cooldown_key(query)
    now = monotonic()
    memo_expiry = _EMBED_TIMEOUT_COOLDOWN_MEMO.get(key)
    if memo_expiry is not None:
        if memo_expiry > now:
            return True
        _EMBED_TIMEOUT_COOLDOWN_MEMO.pop(key, None)
    try:
        redis_client = get_redis_client(settings.redis_url)
        if redis_client is None:
            return False
        if not redis_client.get(key):
            return False
        ttl_seconds = int(redis_client.ttl(key) or 1)
        _EMBED_TIMEOUT_COOLDOWN_MEMO[key] = now + float(max(1, ttl_seconds))
        return True
    except Exception:
        return False


def _mark_embedding_timeout_cooldown(query: str, *, settings: Any, cooldown_seconds: float) -> None:
    if cooldown_seconds <= 0:
        return
    key = _embed_timeout_cooldown_key(query)
    if len(_EMBED_TIMEOUT_COOLDOWN_MEMO) >= _EMBED_TIMEOUT_COOLDOWN_MEMO_MAX_SIZE:
        _EMBED_TIMEOUT_COOLDOWN_MEMO.pop(next(iter(_EMBED_TIMEOUT_COOLDOWN_MEMO)))
    _EMBED_TIMEOUT_COOLDOWN_MEMO[key] = monotonic() + cooldown_seconds
    try:
        redis_client = get_redis_client(settings.redis_url)
        if redis_client is not None:
            redis_client.setex(key, max(1, int(math.ceil(cooldown_seconds))), "1")
    except Exception:
        pass


def _get_cached_embedding(query: str) -> list[float] | None:
    """Try to get a cached embedding from Redis."""
    key = _embed_cache_key(query)
    now = monotonic()

    memo_hit = _EMBED_MEMO.get(key)
    if memo_hit:
        expires_at, embedding = memo_hit
        if expires_at > now:
            return embedding
        _EMBED_MEMO.pop(key, None)

    try:
        settings = get_settings()
        redis_client = get_redis_client(settings.redis_url)
        if redis_client is None:
            return None
        raw = redis_client.get(key)
        if raw:
            decoded = json.loads(raw)
            if not isinstance(decoded, list):
                return None
            embedding = [float(value) for value in decoded]
            _EMBED_MEMO[key] = (now + _EMBED_CACHE_TTL, embedding)
            return embedding
    except Exception:
        pass
    return None


def _set_cached_embedding(query: str, embedding: list[float]) -> None:
    """Cache an embedding in Redis with TTL."""
    key = _embed_cache_key(query)
    if len(_EMBED_MEMO) >= _EMBED_MEMO_MAX_SIZE:
        _EMBED_MEMO.pop(next(iter(_EMBED_MEMO)))
    _EMBED_MEMO[key] = (monotonic() + _EMBED_CACHE_TTL, embedding)

    try:
        settings = get_settings()
        redis_client = get_redis_client(settings.redis_url)
        if redis_client is not None:
            redis_client.setex(key, _EMBED_CACHE_TTL, json.dumps(embedding))
    except Exception:
        pass


def _activation_cache_key(workspace_id: str, owner_id: str) -> str:
    return f"tce:activation:{workspace_id}:{owner_id}"


def _read_activation_scores(
    *,
    workspace_id: str,
    owner_id: str,
    event_ids: list[Any],
    settings: Any,
) -> dict[Any, float]:
    if not bool(getattr(settings, "memory_activation_enabled", True)) or not event_ids:
        return {}
    redis_client = get_redis_client(settings.redis_url)
    if redis_client is None:
        return {}
    half_life_days = max(1, int(getattr(settings, "memory_activation_half_life_days", 45)))
    try:
        key = _activation_cache_key(workspace_id, owner_id)
        event_keys = [str(event_id) for event_id in event_ids]
        raw_values = redis_client.hmget(key, event_keys)
    except Exception:
        return {}

    now_epoch = datetime.now(tz=UTC).timestamp()
    out: dict[Any, float] = {}
    for event_id, raw in zip(event_ids, raw_values, strict=False):
        if not raw:
            continue
        try:
            token = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            count_raw, ts_raw = token.split("|", 1)
            access_count = max(0, int(count_raw))
            last_access_epoch = float(ts_raw)
        except Exception:
            continue
        days_since_access = max(0.0, (now_epoch - last_access_epoch) / 86400.0)
        freq_score = min(1.0, math.log1p(float(access_count)) / math.log(16.0))
        recency_score = math.exp(-days_since_access / float(half_life_days))
        activation = max(0.0, min(1.0, (0.6 * freq_score) + (0.4 * recency_score)))
        out[event_id] = activation
    return out


def _bump_activation_scores(
    *,
    workspace_id: str,
    owner_id: str,
    event_ids: list[Any],
    settings: Any,
) -> None:
    if not bool(getattr(settings, "memory_activation_enabled", True)) or not event_ids:
        return
    redis_client = get_redis_client(settings.redis_url)
    if redis_client is None:
        return
    now_epoch = int(datetime.now(tz=UTC).timestamp())
    key = _activation_cache_key(workspace_id, owner_id)
    try:
        event_keys = [str(event_id) for event_id in event_ids]
        existing = redis_client.hmget(key, event_keys)
        pipe = redis_client.pipeline()
        for event_key, raw in zip(event_keys, existing, strict=False):
            count = 0
            if raw:
                try:
                    token = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
                    count_raw, _ = token.split("|", 1)
                    count = max(0, int(count_raw))
                except Exception:
                    count = 0
            count += 1
            pipe.hset(key, event_key, f"{count}|{now_epoch}")
        pipe.expire(key, 60 * 60 * 24 * 120)
        pipe.execute()
    except Exception:
        return


def _read_feedback_signals(
    db: Session,
    *,
    workspace_id: str,
    event_ids: list[Any],
    settings: Any,
) -> dict[Any, float]:
    if not bool(getattr(settings, "search_feedback_enabled", True)) or not event_ids:
        return {}
    try:
        rows = db.execute(
            text(
                """
                SELECT source_event_id AS event_id,
                       SUM(
                           CASE
                             WHEN LOWER(cf.feedback_type) IN ('helpful', 'positive', 'correct') THEN 1
                             ELSE 0
                           END
                       ) AS positive_count,
                       SUM(
                           CASE
                             WHEN LOWER(cf.feedback_type) IN ('unhelpful', 'negative', 'wrong') THEN 1
                             ELSE 0
                           END
                       ) AS negative_count
                FROM decision_observations dobs
                JOIN clone_feedback cf ON cf.observation_id = dobs.id
                CROSS JOIN LATERAL unnest(dobs.source_event_ids) AS source_event_id
                WHERE dobs.workspace_id = :workspace_id
                  AND source_event_id = ANY(CAST(:event_ids AS UUID[]))
                GROUP BY source_event_id
                """
            ),
            {"workspace_id": workspace_id, "event_ids": [str(value) for value in event_ids]},
        ).mappings().all()
    except Exception:
        return {}

    out: dict[Any, float] = {}
    for row in rows:
        event_id = row.get("event_id")
        positive = int(row.get("positive_count") or 0)
        negative = int(row.get("negative_count") or 0)
        if positive <= 0 and negative <= 0:
            continue
        total = max(1, positive + negative)
        signal = max(-1.0, min(1.0, float(positive - negative) / float(total)))
        out[event_id] = signal
        out[str(event_id)] = signal
    return out


def _load_episode_score_lookup(
    db: Session,
    *,
    workspace_id: str,
    owner_ids: list[str],
    query_text: str,
    max_rows: int = 180,
) -> dict[Any, float]:
    if not owner_ids or not str(query_text or "").strip():
        return {}
    try:
        rows = db.execute(
            text(
                """
                SELECT eel.event_id,
                       ep.goal,
                       ep.context,
                       ep.outcome
                FROM episodes ep
                JOIN episode_event_links eel ON eel.episode_id = ep.id
                WHERE ep.workspace_id = :workspace_id
                  AND ep.user_id = ANY(CAST(:owner_ids AS TEXT[]))
                ORDER BY ep.updated_at DESC
                LIMIT :row_limit
                """
            ),
            {
                "workspace_id": workspace_id,
                "owner_ids": owner_ids,
                "row_limit": max(20, int(max_rows)),
            },
        ).mappings().all()
    except Exception:
        return {}
    scores: dict[Any, float] = {}
    for row in rows:
        episode_text = " ".join(
            [
                str(row.get("goal") or ""),
                str(row.get("context") or ""),
                str(row.get("outcome") or ""),
            ]
        ).strip()
        overlap = task_overlap_score(query_text, episode_text)
        if overlap <= 0:
            continue
        event_id = row.get("event_id")
        if event_id is None:
            continue
        scores[event_id] = max(float(scores.get(event_id, 0.0)), float(overlap))
    return scores


def _token_set_for_row(row: dict[str, Any]) -> set[str]:
    blob = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("domain") or ""),
            str(row.get("task_type") or ""),
        ]
    ).lower()
    return {token for token in re.split(r"\W+", blob) if token}


def _jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return float(len(left & right)) / float(len(union))


def _mmr_select(
    scored_events: list[tuple[float, dict[str, Any]]],
    *,
    top_k: int,
    mmr_lambda: float,
) -> list[tuple[float, dict[str, Any]]]:
    if top_k <= 0:
        return []
    candidates: list[_MMRCandidate] = [
        {
            "score": score,
            "row": row,
            "tokens": _token_set_for_row(row),
        }
        for score, row in scored_events
    ]
    selected: list[_MMRCandidate] = []
    while candidates and len(selected) < top_k:
        best_idx = 0
        best_mmr = float("-inf")
        for index, candidate in enumerate(candidates):
            novelty = 0.0
            if selected:
                novelty = max(
                    _jaccard_similarity(candidate["tokens"], chosen["tokens"]) for chosen in selected
                )
            mmr_score = (mmr_lambda * float(candidate["score"])) - ((1.0 - mmr_lambda) * novelty)
            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = index
        selected.append(candidates.pop(best_idx))
    return [(float(item["score"]), dict(item["row"])) for item in selected]


def _mmr_dedupe_key(row: dict[str, Any]) -> str:
    title = _normalize_title(row.get("title"))
    domain = str(row.get("domain") or "").strip().lower()
    task_type = str(row.get("task_type") or "").strip().lower()
    if title:
        return f"{title}|{domain}|{task_type}"
    return f"{domain}|{task_type}|{row.get('id')}"


def _dedupe_scored_events_for_mmr(
    scored_events: list[tuple[float, dict[str, Any]]],
    *,
    candidate_pool: int,
) -> list[tuple[float, dict[str, Any]]]:
    pool_limit = max(1, int(candidate_pool))
    best_by_key: dict[str, tuple[float, int, dict[str, Any]]] = {}
    for index, (score, row) in enumerate(scored_events[:pool_limit]):
        key = _mmr_dedupe_key(row)
        current = best_by_key.get(key)
        if current is None or float(score) > float(current[0]):
            best_by_key[key] = (float(score), index, row)
    deduped = list(best_by_key.values())
    deduped.sort(key=lambda item: (-float(item[0]), int(item[1])))
    return [(float(score), dict(row)) for score, _, row in deduped[:pool_limit]]


def _should_skip_query_expansion(
    *,
    initial_candidate_count: int,
    requested_k: int,
    dup_ratio: float,
    candidate_multiplier: int,
    dup_ratio_threshold: float,
) -> bool:
    min_candidate_gate = max(1, int(requested_k)) * max(1, int(candidate_multiplier))
    if int(initial_candidate_count) >= min_candidate_gate:
        return True
    return float(dup_ratio) >= float(dup_ratio_threshold)


def _embedding_as_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def _qdrant_base_url(settings: Any) -> str:
    explicit = str(getattr(settings, "qdrant_url", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    ollama_url = str(getattr(settings, "ollama_url", "") or "").strip().lower()
    if "://ollama:" in ollama_url:
        return "http://qdrant:6333"
    return "http://localhost:6333"


def _qdrant_collection(settings: Any) -> str:
    return str(getattr(settings, "qdrant_collection", "tce_event_embeddings") or "tce_event_embeddings").strip()


def _qdrant_timeout_seconds(settings: Any) -> float:
    timeout_ms = max(20, int(getattr(settings, "qdrant_timeout_ms", 60)))
    return float(timeout_ms) / 1000.0


def _qdrant_search_longterm(
    *,
    query_embedding: list[float],
    search_limit: int,
    settings: Any,
    consumer_ctx: ConsumerContext,
) -> tuple[list[str], dict[str, float], str | None]:
    base_url = _qdrant_base_url(settings)
    timeout_seconds = _qdrant_timeout_seconds(settings)
    collection = _qdrant_collection(settings)
    retry_attempts = max(1, int(getattr(settings, "qdrant_retry_max_attempts", 2)))
    retry_backoff_ms = max(1, int(getattr(settings, "qdrant_retry_backoff_ms", 12)))
    retry_budget_ms = max(0, int(getattr(settings, "qdrant_retry_budget_ms", 80)))
    longterm_days = max(0, int(getattr(settings, "qdrant_longterm_age_days", 0)))
    cutoff_epoch = int((datetime.now(tz=UTC) - timedelta(days=longterm_days)).timestamp())
    payload = {
        "vector": query_embedding,
        "limit": max(1, int(search_limit)),
        "with_payload": True,
        "filter": {
            "must": [
                {"key": "workspace_id", "match": {"value": str(consumer_ctx.workspace_id or "")}},
                {"key": "owner_id", "match": {"value": str(consumer_ctx.owner_id or "")}},
                {"key": "longterm", "match": {"value": True}},
                {"key": "ts_epoch", "range": {"lte": cutoff_epoch}},
            ]
        },
    }
    started = monotonic()
    body: dict[str, Any] = {}
    for attempt in range(retry_attempts):
        try:
            with httpx.Client(base_url=base_url, timeout=timeout_seconds) as client:
                response = client.post(f"/collections/{collection}/points/search", json=payload)
            status_code = int(response.status_code)
            if status_code >= 300:
                is_retryable_status = status_code in {429, 500, 502, 503, 504}
                if attempt < (retry_attempts - 1) and is_retryable_status:
                    elapsed_ms = int((monotonic() - started) * 1000)
                    remaining_ms = retry_budget_ms - elapsed_ms
                    if remaining_ms > 0:
                        delay_ms = min(remaining_ms, retry_backoff_ms * (2 ** attempt))
                        sleep(float(delay_ms) / 1000.0)
                        continue
                return [], {}, f"qdrant_search_http_{status_code}"
            body = response.json() if response.content else {}
            break
        except Exception as exc:
            if attempt < (retry_attempts - 1) and _is_retryable_error(exc):
                elapsed_ms = int((monotonic() - started) * 1000)
                remaining_ms = retry_budget_ms - elapsed_ms
                if remaining_ms > 0:
                    delay_ms = min(remaining_ms, retry_backoff_ms * (2 ** attempt))
                    sleep(float(delay_ms) / 1000.0)
                    continue
            return [], {}, f"qdrant_search_error:{exc}"

    result_rows = body.get("result", []) if isinstance(body, dict) else []
    out_ids: list[str] = []
    out_scores: dict[str, float] = {}
    for item in result_rows if isinstance(result_rows, list) else []:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        score = float(item.get("score") or 0.0)
        out_ids.append(item_id)
        out_scores[item_id] = max(score, float(out_scores.get(item_id, 0.0)))
    return out_ids, out_scores, None


def _authority_score(level: str | None) -> float:
    normalized = str(level or "incidental").strip().lower()
    return {
        "policy": 1.0,
        "standard": 0.9,
        "preferred": 0.75,
        "observed": 0.5,
        "incidental": 0.2,
    }.get(normalized, 0.2)


def _bump_retrieval_counter(source: str) -> None:
    key = source if source in _RETRIEVAL_COUNTERS else "hybrid_fallback"
    _RETRIEVAL_COUNTERS[key] = int(_RETRIEVAL_COUNTERS.get(key, 0)) + 1


def retrieval_status_snapshot() -> dict[str, Any]:
    settings = get_settings()
    return {
        "mode": str(getattr(settings, "vector_backend", "pgvector")),
        "qdrant_enabled": bool(getattr(settings, "qdrant_enabled", False)),
        "qdrant_url": _qdrant_base_url(settings),
        "qdrant_collection": _qdrant_collection(settings),
        "qdrant_longterm_age_days": int(getattr(settings, "qdrant_longterm_age_days", 0)),
        "qdrant_timeout_ms": int(getattr(settings, "qdrant_timeout_ms", 60)),
        "trigger_threshold": float(getattr(settings, "context_retrieval_trigger_score", 0.68)),
        "escalate_threshold": float(getattr(settings, "context_retrieval_escalate_score", 0.52)),
        "turn_budget_ms": int(getattr(settings, "context_retrieval_budget_ms", 120)),
        "backend_timeout_ms": int(getattr(settings, "context_backend_timeout_ms", 60)),
        "query_expansion_enabled": bool(getattr(settings, "search_query_expansion_enabled", False)),
        "query_expansion_timeout_ms": int(getattr(settings, "search_query_expansion_timeout_ms", 25)),
        "query_expansion_max_terms": int(getattr(settings, "search_query_expansion_max_terms", 4)),
        "query_expansion_skip_candidate_multiplier": int(
            getattr(settings, "search_query_expansion_skip_candidate_multiplier", 4)
        ),
        "query_expansion_skip_dup_ratio": float(getattr(settings, "search_query_expansion_skip_dup_ratio", 0.80)),
        "mmr_enabled": bool(getattr(settings, "search_mmr_enabled", False)),
        "mmr_lambda": float(getattr(settings, "search_mmr_lambda", 0.65)),
        "mmr_candidate_pool": int(getattr(settings, "search_mmr_candidate_pool", 60)),
        "enhancement_budget_ms": int(getattr(settings, "search_enhancement_budget_ms", 120)),
        "fallback_counters": dict(_RETRIEVAL_COUNTERS),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


def run_search(
    db: Session,
    search_request: EventSearchRequest,
    consumer_ctx: ConsumerContext,
    policy_engine: PolicyEngine,
) -> tuple[list[EventSearchHit], list[Any], int, dict[str, Any]]:
    settings = get_settings()
    retrieval_started = monotonic()
    query_text = search_request.query.strip()
    match_all = bool(search_request.match_all) or query_text in {"", "*"}
    intent_retrieval_enabled = bool(getattr(settings, "intent_retrieval_enabled", False))
    planned_queries = plan_retrieval_subqueries(
        query_text,
        enabled=intent_retrieval_enabled,
        match_all=match_all,
    )
    planner_used = bool(len(planned_queries) > 1)
    subquery_labels = [str(item.get("label") or "") for item in planned_queries if str(item.get("label") or "").strip()]
    owner_scope, cross_user_scope_applied, cross_user_scope_owners = _resolve_owner_scope(
        db,
        workspace_id=consumer_ctx.workspace_id,
        owner_id=consumer_ctx.owner_id,
        query_text=query_text,
    )
    owner_scope_ids = sorted(owner_scope)

    # Base filters for both lexical and ANN candidate queries.
    where_parts: list[str] = ["sensitivity <= :max_sensitivity"]
    params: dict[str, Any] = {"max_sensitivity": consumer_ctx.max_sensitivity}

    filters = search_request.filters
    if filters.domain:
        where_parts.append("domain = :f_domain")
        params["f_domain"] = filters.domain
    if filters.task_type:
        where_parts.append("task_type = :f_task_type")
        params["f_task_type"] = filters.task_type
    if filters.actor:
        where_parts.append("actor = :f_actor")
        params["f_actor"] = filters.actor
    if filters.source:
        where_parts.append("source = :f_source")
        params["f_source"] = filters.source
    if filters.min_sensitivity is not None:
        where_parts.append("sensitivity >= :f_min_sens")
        params["f_min_sens"] = filters.min_sensitivity
    if filters.max_sensitivity is not None:
        where_parts.append("sensitivity <= :f_max_sens")
        params["f_max_sens"] = filters.max_sensitivity
    if search_request.time_start:
        where_parts.append("ts >= :time_start")
        params["time_start"] = search_request.time_start
    if search_request.time_end:
        where_parts.append("ts <= :time_end")
        params["time_end"] = search_request.time_end

    limit = search_request.k * 3
    base_where_sql = " AND ".join(where_parts)

    rows: list[dict[str, Any]] = []
    vector_similarity_lookup: dict[Any, float] = {}
    query_embedding: list[float] | None = None
    entity_event_ids: set[Any] = set()
    retrieval_source = "none"
    retrieval_reason: str | None = None
    feedback_adjustment_applied = False
    query_expansion_used = False
    query_expansion_terms: list[str] = []
    rerank_strategy = "none"
    citation_dup_ratio = 0.0
    embed_timeout = 0.0
    embed_state: dict[str, Any] = {
        "reason": None,
        "cooldown_applied": False,
    }
    expansion_ms = 0
    mmr_ms = 0
    mmr_candidates = 0
    activation_boost_applied = False
    episode_boost_applied = False
    subquery_labels_by_event: dict[Any, set[str]] = {}

    if match_all:
        sql = f"""
            SELECT id, ts, actor, source, domain, task_type, event_type,
                   title, summary_l0, summary_l1_json, sensitivity, context, authority_level
            FROM events
            WHERE {base_where_sql}
            ORDER BY ts DESC
            LIMIT :row_limit
        """
        rows = [
            dict(row)
            for row in db.execute(text(sql), {**params, "row_limit": limit}).mappings().all()
        ]
    else:
        text_query = f"%{query_text}%"
        lexical_terms = [text_query]
        expansion_enabled = bool(getattr(settings, "search_query_expansion_enabled", False))
        mmr_enabled = bool(getattr(settings, "search_mmr_enabled", False))
        scale_trigger_met = False
        enhancement_budget_ms = max(1, int(getattr(settings, "search_enhancement_budget_ms", 120)))
        expansion_timeout_ms = max(1, int(getattr(settings, "search_query_expansion_timeout_ms", 25)))
        expansion_max_terms = max(1, int(getattr(settings, "search_query_expansion_max_terms", 4)))
        expansion_skip_multiplier = max(
            1, int(getattr(settings, "search_query_expansion_skip_candidate_multiplier", 4))
        )
        expansion_skip_dup_ratio = max(
            0.0, min(1.0, float(getattr(settings, "search_query_expansion_skip_dup_ratio", 0.80)))
        )

        def _enhancement_budget_exhausted() -> bool:
            elapsed_ms = int((monotonic() - retrieval_started) * 1000)
            return elapsed_ms >= enhancement_budget_ms

        if expansion_enabled or mmr_enabled:
            scale_trigger_met = _search_scale_trigger_met(
                db,
                workspace_id=consumer_ctx.workspace_id,
                owner_id=consumer_ctx.owner_id,
            )

        lexical_predicate_default = """
            title ILIKE ANY(:lexical_terms)
            OR CAST(payload AS TEXT) ILIKE ANY(:lexical_terms)
            OR CAST(tags AS TEXT) ILIKE ANY(:lexical_terms)
        """
        lexical_predicate_expansion = """
            title ILIKE ANY(:lexical_terms)
            OR domain ILIKE ANY(:lexical_terms)
            OR task_type ILIKE ANY(:lexical_terms)
        """

        def _run_lexical_query(terms: list[str], *, expansion_mode: bool) -> list[dict[str, Any]]:
            lexical_predicate = lexical_predicate_expansion if expansion_mode else lexical_predicate_default
            lexical_sql = f"""
                SELECT id, ts, actor, source, domain, task_type, event_type,
                       title, summary_l0, summary_l1_json, sensitivity, context, authority_level
                FROM events
                WHERE {base_where_sql}
                  AND ({lexical_predicate})
                ORDER BY ts DESC
                LIMIT :row_limit
            """
            result = db.execute(
                text(lexical_sql),
                {**params, "lexical_terms": terms, "row_limit": limit},
            ).mappings().all()
            return [dict(row) for row in result]

        retry_enabled = bool(getattr(settings, "search_retry_enabled", True))
        retry_attempts = max(1, int(getattr(settings, "search_retry_max_attempts", 2)))
        retry_backoff_ms = max(1, int(getattr(settings, "search_retry_backoff_ms", 8)))
        retry_budget_ms = max(0, int(getattr(settings, "search_retry_budget_ms", 40)))
        embed_retry_enabled = bool(getattr(settings, "search_embedding_retry_enabled", retry_enabled))
        embed_retry_attempts = max(
            1, int(getattr(settings, "search_embedding_retry_max_attempts", retry_attempts))
        )
        embed_retry_backoff_ms = max(
            1, int(getattr(settings, "search_embedding_retry_backoff_ms", retry_backoff_ms))
        )
        embed_retry_budget_ms = max(
            0, int(getattr(settings, "search_embedding_retry_budget_ms", retry_budget_ms))
        )
        embed_retry_jitter_ratio = max(
            0.0,
            min(1.0, float(getattr(settings, "search_embedding_retry_jitter_ratio", 0.25))),
        )
        embed_timeout_cooldown_enabled = bool(
            getattr(settings, "search_embedding_timeout_cooldown_enabled", True)
        )
        embed_timeout_cooldown_seconds = max(
            0.0,
            float(getattr(settings, "search_embedding_timeout_cooldown_seconds", 20.0)),
        )
        def _embed_once() -> list[float]:
            cached = _get_cached_embedding(query_text)
            if cached:
                return cached
            gateway = get_gateway(settings)
            result: list[float] | None = None
            if hasattr(gateway, "embed"):
                try:
                    result = gateway.embed(query_text)
                except Exception as sync_exc:
                    if not hasattr(gateway, "aembed"):
                        raise RuntimeError(f"embed_unavailable:{sync_exc}") from sync_exc
                    try:
                        result = asyncio.run(gateway.aembed(query_text))
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        try:
                            result = loop.run_until_complete(gateway.aembed(query_text))
                        finally:
                            loop.close()
                    except Exception as async_exc:
                        raise RuntimeError(f"embed_unavailable:sync={sync_exc};async={async_exc}") from async_exc
            elif hasattr(gateway, "aembed"):
                try:
                    result = asyncio.run(gateway.aembed(query_text))
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    try:
                        result = loop.run_until_complete(gateway.aembed(query_text))
                    finally:
                        loop.close()
            else:
                raise RuntimeError("gateway has no embed/aembed method")
            if not result:
                raise RuntimeError("empty embedding result")
            _set_cached_embedding(query_text, result)
            return result

        def _do_embed() -> list[float] | None:
            if _is_embedding_timeout_cooldown_active(query_text, settings=settings):
                embed_state["reason"] = "embedding_timeout_cooldown"
                return None
            try:
                if embed_retry_enabled:
                    return _run_with_retry(
                        _embed_once,
                        attempts=embed_retry_attempts,
                        base_backoff_ms=embed_retry_backoff_ms,
                        max_retry_budget_ms=embed_retry_budget_ms,
                        jitter_ratio=embed_retry_jitter_ratio,
                    )
                return _embed_once()
            except Exception as exc:
                if _is_retryable_error(exc):
                    embed_state["reason"] = "embedding_retry_exhausted"
                    if embed_timeout_cooldown_enabled:
                        _mark_embedding_timeout_cooldown(
                            query_text,
                            settings=settings,
                            cooldown_seconds=embed_timeout_cooldown_seconds,
                        )
                        embed_state["cooldown_applied"] = True
                else:
                    embed_state["reason"] = "embedding_error"
                logger.warning("vector embedding unavailable; continuing lexical-only search: %s", exc)
                return None

        def _entity_query_once() -> set[Any]:
            from .db import get_session_factory

            session_factory = get_session_factory()
            with session_factory() as graph_db:
                if bool(getattr(settings, "memory_multi_hop_enabled", True)):
                    rows = graph_db.execute(
                        text(
                            """
                            WITH matched_entities AS (
                                SELECT en.id
                                FROM entity_nodes en
                                WHERE en.workspace_id = :workspace_id
                                  AND en.owner_id = ANY(CAST(:owner_ids AS TEXT[]))
                                  AND (
                                        en.entity_key ILIKE :q
                                        OR en.display_name ILIKE :q
                                  )
                                LIMIT 160
                            ),
                            seed_events AS (
                                SELECT DISTINCT eel.event_id
                                FROM event_entity_links eel
                                JOIN matched_entities me ON me.id = eel.entity_id
                                LIMIT 220
                            ),
                            neighbor_entities AS (
                                SELECT DISTINCT eel.entity_id
                                FROM event_entity_links eel
                                JOIN seed_events se ON se.event_id = eel.event_id
                                LIMIT 220
                            ),
                            expanded_events AS (
                                SELECT DISTINCT eel.event_id
                                FROM event_entity_links eel
                                JOIN neighbor_entities ne ON ne.entity_id = eel.entity_id
                                LIMIT :multi_hop_limit
                            )
                            SELECT event_id FROM expanded_events
                            """
                        ),
                        {
                            "workspace_id": consumer_ctx.workspace_id,
                            "owner_ids": owner_scope_ids,
                            "q": f"%{query_text}%",
                            "multi_hop_limit": max(100, int(getattr(settings, "memory_multi_hop_limit", 600))),
                        },
                    ).mappings()
                    return {row["event_id"] for row in rows}
                rows = graph_db.execute(
                    text(
                        """
                        SELECT DISTINCT eel.event_id
                        FROM entity_nodes en
                        JOIN event_entity_links eel ON eel.entity_id = en.id
                        WHERE en.workspace_id = :workspace_id
                          AND en.owner_id = ANY(CAST(:owner_ids AS TEXT[]))
                          AND (
                            en.entity_key ILIKE :q
                            OR en.display_name ILIKE :q
                          )
                        LIMIT 200
                        """
                    ),
                    {
                        "workspace_id": consumer_ctx.workspace_id,
                        "owner_ids": owner_scope_ids,
                        "q": f"%{query_text}%",
                    },
                ).mappings()
                return {row["event_id"] for row in rows}

        def _do_entity_query() -> set[Any]:
            try:
                if retry_enabled:
                    return _run_with_retry(
                        _entity_query_once,
                        attempts=retry_attempts,
                        base_backoff_ms=retry_backoff_ms,
                        max_retry_budget_ms=retry_budget_ms,
                    )
                return _entity_query_once()
            except Exception as exc:
                logger.warning("entity-graph boost unavailable for search: %s", exc)
                return set()

        embed_future: Future[list[float] | None] = _SEARCH_EXECUTOR.submit(_do_embed)
        entity_future: Future[set[Any]] = _SEARCH_EXECUTOR.submit(_do_entity_query)
        rows = _run_lexical_query(lexical_terms, expansion_mode=False)
        preview_dup_ratio = _citation_dup_ratio([dict(row) for row in rows])
        initial_candidate_count = len(rows)
        expansion_triggered = bool(scale_trigger_met or preview_dup_ratio > 0.35)
        expansion_skip = _should_skip_query_expansion(
            initial_candidate_count=initial_candidate_count,
            requested_k=search_request.k,
            dup_ratio=preview_dup_ratio,
            candidate_multiplier=expansion_skip_multiplier,
            dup_ratio_threshold=expansion_skip_dup_ratio,
        )
        if (
            expansion_enabled
            and expansion_triggered
            and not expansion_skip
            and not _enhancement_budget_exhausted()
        ):
            expansion_started = monotonic()
            expanded_terms = _expand_query_terms(query_text, max_terms=expansion_max_terms)
            expansion_elapsed_ms = int((monotonic() - expansion_started) * 1000)
            expansion_ms += expansion_elapsed_ms
            if (
                expansion_elapsed_ms <= expansion_timeout_ms
                and expanded_terms
                and not _enhancement_budget_exhausted()
            ):
                lexical_terms = [text_query, *[f"%{token}%" for token in expanded_terms]]
                rerun_started = monotonic()
                rows = _run_lexical_query(lexical_terms, expansion_mode=True)
                expansion_ms += int((monotonic() - rerun_started) * 1000)
                query_expansion_terms = expanded_terms
                query_expansion_used = True
        merged_rows: dict[Any, dict[str, Any]] = {}

        def _merge_rows(batch: list[dict[str, Any]], label: str) -> None:
            for row in batch:
                merged_rows.setdefault(row["id"], dict(row))
                subquery_labels_by_event.setdefault(row["id"], set()).add(label)

        _merge_rows(rows, planned_queries[0]["label"] if planned_queries else "objective")
        if planner_used and not _enhancement_budget_exhausted():
            for index, planned in enumerate(planned_queries):
                remaining_budget_ms = enhancement_budget_ms - int((monotonic() - retrieval_started) * 1000)
                if remaining_budget_ms < 40:
                    break
                planned_query = str(planned.get("query") or "").strip().lower()
                if not planned_query:
                    continue
                if index == 0 and planned_query == query_text.lower():
                    continue
                secondary_terms = [f"%{planned_query}%"]
                if not secondary_terms[0].strip("%"):
                    continue
                extra_rows = _run_lexical_query(secondary_terms, expansion_mode=False)
                _merge_rows(extra_rows, str(planned.get("label") or "objective"))

        configured_embed_timeout = max(
            0.05,
            float(
                getattr(
                    settings,
                    "effective_search_embedding_timeout_seconds",
                    settings.search_embedding_timeout_seconds,
                )
            ),
        )
        embed_quick_timeout = max(
            0.05,
            float(getattr(settings, "search_embedding_quick_timeout_seconds", configured_embed_timeout)),
        )
        embed_timeout = min(configured_embed_timeout, embed_quick_timeout)
        try:
            entity_event_ids = entity_future.result(timeout=embed_timeout)
        except TimeoutError:
            logger.warning("entity-graph query timed out after %.2fs; continuing without graph bonus", embed_timeout)
            entity_event_ids = set()
            entity_future.cancel()

        try:
            query_embedding = embed_future.result(timeout=embed_timeout)
        except TimeoutError:
            logger.warning("vector embedding timed out after %.2fs; continuing lexical-only search", embed_timeout)
            query_embedding = None
            retrieval_reason = "embedding_timeout"
            if embed_timeout_cooldown_enabled:
                _mark_embedding_timeout_cooldown(
                    query_text,
                    settings=settings,
                    cooldown_seconds=embed_timeout_cooldown_seconds,
                )
                embed_state["cooldown_applied"] = True
            embed_future.cancel()
        except Exception:
            query_embedding = None
            retrieval_reason = "embedding_error"
        if query_embedding is None and not retrieval_reason and embed_state.get("reason"):
            retrieval_reason = str(embed_state["reason"])

        if query_embedding:
            ann_limit = max(search_request.k * 4, limit)
            ann_sql = f"""
                SELECT e.id, e.ts, e.actor, e.source, e.domain, e.task_type, e.event_type,
                       e.title, e.summary_l0, e.summary_l1_json, e.sensitivity, e.context, e.authority_level,
                       1 - (ee.embedding <=> CAST(:query_embedding AS vector)) AS ann_similarity
                FROM event_embeddings ee
                JOIN events e ON e.id = ee.event_id
                WHERE {base_where_sql}
                ORDER BY ee.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :ann_limit
            """
            try:
                ann_rows = db.execute(
                    text(ann_sql),
                    {
                        **params,
                        "query_embedding": _embedding_as_vector_literal(query_embedding),
                        "ann_limit": ann_limit,
                    },
                ).mappings().all()
                for ann_row in ann_rows:
                    ann_similarity = float(ann_row.get("ann_similarity") or 0.0)
                    vector_similarity_lookup[ann_row["id"]] = max(
                        ann_similarity,
                        float(vector_similarity_lookup.get(ann_row["id"], 0.0)),
                    )
                    merged_rows.setdefault(ann_row["id"], dict(ann_row))
                retrieval_source = "pgvector_ann"
            except Exception as exc:
                logger.warning("pgvector ANN query failed; continuing lexical candidates only: %s", exc)
                try:
                    db.rollback()
                except Exception:
                    pass
                retrieval_source = "hybrid_fallback"
                retrieval_reason = "ann_query_failed"

            qdrant_enabled = bool(getattr(settings, "qdrant_enabled", False))
            if qdrant_enabled:
                low_score_threshold = max(0.0, float(getattr(settings, "qdrant_low_score_threshold", 0.42)))
                low_hit_threshold = max(1, int(getattr(settings, "qdrant_low_hit_threshold", 2)))
                pg_best_score = max((float(value) for value in vector_similarity_lookup.values()), default=0.0)
                pg_hit_count = len(vector_similarity_lookup)
                should_try_qdrant = (pg_best_score < low_score_threshold) or (pg_hit_count < low_hit_threshold)
                if should_try_qdrant:
                    qdrant_ids, qdrant_scores, qdrant_error = _qdrant_search_longterm(
                        query_embedding=query_embedding,
                        search_limit=ann_limit,
                        settings=settings,
                        consumer_ctx=consumer_ctx,
                    )
                    if qdrant_error:
                        if not retrieval_reason:
                            retrieval_reason = qdrant_error
                    elif qdrant_ids:
                        qdrant_rows_sql = f"""
                            SELECT id, ts, actor, source, domain, task_type, event_type,
                                   title, summary_l0, summary_l1_json, sensitivity, context, authority_level
                            FROM events
                            WHERE {base_where_sql}
                              AND CAST(id AS TEXT) = ANY(:event_ids)
                            LIMIT :row_limit
                        """
                        qdrant_rows = db.execute(
                            text(qdrant_rows_sql),
                            {
                                **params,
                                "event_ids": qdrant_ids,
                                "row_limit": ann_limit,
                            },
                        ).mappings().all()
                        for q_row in qdrant_rows:
                            event_id_key = str(q_row.get("id") or "")
                            q_score = float(qdrant_scores.get(event_id_key, 0.0))
                            if q_score > 0:
                                vector_similarity_lookup[q_row["id"]] = max(
                                    q_score,
                                    float(vector_similarity_lookup.get(q_row["id"], 0.0)),
                                )
                            merged_rows.setdefault(q_row["id"], dict(q_row))
                        if qdrant_rows:
                            retrieval_source = "qdrant" if retrieval_source in {"none", "lexical_only"} else "hybrid_fallback"
                            retrieval_reason = "low_pgvector_score_qdrant_fallback"

        if not retrieval_source:
            retrieval_source = "lexical_only"
        rows = list(merged_rows.values())
        citation_dup_ratio = _citation_dup_ratio(rows)

    scored_events: list[tuple[float, dict[str, Any]]] = []
    now = datetime.now(tz=UTC)
    if settings.search_enable_recency:
        recency_lambda = max(0.0, float(settings.search_recency_lambda))
    else:
        recency_lambda = 0.0
    weight_relevance = max(0.0, float(getattr(settings, "search_weight_relevance", 0.45)))
    weight_stability = max(0.0, float(getattr(settings, "search_weight_stability", 0.25)))
    weight_authority = max(0.0, float(getattr(settings, "search_weight_authority", 0.20)))
    weight_recency = max(0.0, float(settings.search_weight_recency))
    weight_sum = weight_relevance + weight_stability + weight_authority + weight_recency
    if weight_sum <= 0:
        weight_relevance, weight_stability, weight_authority, weight_recency = (0.45, 0.25, 0.20, 0.10)
        weight_sum = 1.0
    weight_relevance /= weight_sum
    weight_stability /= weight_sum
    weight_authority /= weight_sum
    weight_recency /= weight_sum
    graph_bonus = max(0.0, float(settings.search_graph_bonus))
    activation_weight = max(0.0, float(getattr(settings, "memory_activation_weight", 0.0)))
    if planner_used and intent_retrieval_enabled:
        multiplier = max(1.0, float(getattr(settings, "memory_activation_autonomy_weight_multiplier", 1.0)))
        boosted_weight = activation_weight * multiplier
        activation_boost_applied = boosted_weight > activation_weight
        activation_weight = boosted_weight
    stability_counts = Counter((str(row["domain"]), str(row["task_type"])) for row in rows)
    activation_lookup = _read_activation_scores(
        workspace_id=consumer_ctx.workspace_id,
        owner_id=consumer_ctx.owner_id,
        event_ids=[row["id"] for row in rows],
        settings=settings,
    )
    feedback_weight = max(0.0, float(getattr(settings, "search_feedback_weight", 0.08)))
    feedback_lookup = _read_feedback_signals(
        db,
        workspace_id=consumer_ctx.workspace_id,
        event_ids=[row["id"] for row in rows],
        settings=settings,
    )
    handoff_map = _load_handoff_records_map(
        db,
        workspace_id=consumer_ctx.workspace_id,
        owner_ids=owner_scope_ids,
        max_records=max(search_request.k * 6, 30),
    )
    handoff_query_intent = handoff_intent(query_text)
    episode_score_lookup = _load_episode_score_lookup(
        db,
        workspace_id=consumer_ctx.workspace_id,
        owner_ids=owner_scope_ids,
        query_text=query_text,
        max_rows=max(search_request.k * 12, 120),
    ) if (planner_used and intent_retrieval_enabled) else {}

    def _score_rows(active_owner_scope: set[str]) -> tuple[list[tuple[float, dict[str, Any]]], int, int, bool]:
        local_scored: list[tuple[float, dict[str, Any]]] = []
        local_blocked = 0
        local_owner_scope_blocked = 0
        local_feedback_applied = False
        for rank, row in enumerate(rows):
            ctx = row["context"]
            event_workspace = ctx.get("_tce_workspace") if isinstance(ctx, dict) else None
            event_owner = ctx.get("_tce_owner") if isinstance(ctx, dict) else None
            if event_workspace and event_workspace != consumer_ctx.workspace_id:
                local_blocked += 1
                continue
            if event_owner and _normalize_owner_id_token(event_owner) not in active_owner_scope:
                local_blocked += 1
                local_owner_scope_blocked += 1
                continue

            ts_val = row["ts"]
            if not isinstance(ts_val, datetime):
                ts_val = datetime.fromisoformat(str(ts_val))
            decision = policy_engine.evaluate(consumer_ctx, row["domain"], row["sensitivity"], ts_val.astimezone(UTC))
            if not decision.allow:
                local_blocked += 1
                continue

            lexical_score = max(0.0, 1.0 - (rank * 0.03))
            vector_score = max(0.0, float(vector_similarity_lookup.get(row["id"], 0.0)))
            if query_embedding is None:
                relevance_score = lexical_score
            else:
                relevance_score = max(0.0, min(1.0, (0.65 * lexical_score) + (0.35 * vector_score)))
            recurrence = int(stability_counts.get((str(row["domain"]), str(row["task_type"])), 0))
            stability_score = (
                0.0
                if recurrence <= 0
                else max(0.0, min(1.0, math.log1p(float(recurrence)) / math.log(8.0)))
            )
            authority_score = _authority_score(row.get("authority_level"))
            days_old = max(0.0, (now - ts_val.astimezone(UTC)).total_seconds() / 86400.0)
            recency_score = math.exp(-recency_lambda * days_old) if recency_lambda > 0 else 1.0
            graph_score = graph_bonus if row["id"] in entity_event_ids else 0.0
            activation_score = max(0.0, min(1.0, float(activation_lookup.get(row["id"], 0.0))))
            feedback_signal = max(-1.0, min(1.0, float(feedback_lookup.get(row["id"], 0.0))))
            feedback_delta = feedback_weight * feedback_signal
            query_labels = subquery_labels_by_event.get(row["id"], set())
            query_label_boost = 0.04 if "decision_history" in query_labels else 0.0
            if "constraints_workflow" in query_labels:
                query_label_boost += 0.03
            score = (
                query_label_boost
                + (weight_relevance * relevance_score)
                + (weight_stability * stability_score)
                + (weight_authority * authority_score)
                + (weight_recency * recency_score)
                + graph_score
                + (activation_weight * activation_score)
                + feedback_delta
                + _milestone_score_boost(row.get("payload"))
            )
            handoff_meta = handoff_map.get(str(row.get("id") or ""))
            if handoff_meta:
                score += 0.24
                if str(handoff_meta.get("schema_version") or "") == "v1":
                    score += 0.06
                if handoff_query_intent and bool(handoff_meta.get("has_anchors")):
                    score += 0.08
            episode_score = max(0.0, min(1.0, float(episode_score_lookup.get(row["id"], 0.0))))
            if episode_score > 0.0:
                score = propagate_episode_score(score, episode_score)
            if abs(feedback_delta) > 1e-9:
                local_feedback_applied = True
            local_scored.append((score, dict(row)))
        local_scored.sort(key=lambda item: item[0], reverse=True)
        return local_scored, local_blocked, local_owner_scope_blocked, local_feedback_applied

    scored_events, blocked, owner_scope_blocked, feedback_adjustment_applied = _score_rows(owner_scope)
    owner_scope_blocked_ratio = float(owner_scope_blocked) / float(max(1, len(rows)))
    auto_expand_on_blocked_ratio = bool(
        _consumer_is_executor_consumer(consumer_ctx.consumer) and owner_scope_blocked_ratio >= 0.95
    )
    if (
        not cross_user_scope_applied
        and owner_scope_blocked > 0
        and (not scored_events or auto_expand_on_blocked_ratio)
    ):
        expanded_scope = _expand_owner_scope_from_rows(
            rows,
            workspace_id=consumer_ctx.workspace_id,
            current_scope=owner_scope,
        )
        if expanded_scope != owner_scope:
            owner_scope = expanded_scope
            owner_scope_ids = sorted(owner_scope)
            cross_user_scope_applied = True
            cross_user_scope_owners = owner_scope_ids[:6]
            handoff_map = _load_handoff_records_map(
                db,
                workspace_id=consumer_ctx.workspace_id,
                owner_ids=owner_scope_ids,
                max_records=max(search_request.k * 6, 30),
            )
            if not retrieval_reason:
                retrieval_reason = (
                    "owner_scope_auto_expand_no_hits"
                    if not scored_events
                    else "owner_scope_auto_expand_high_block_ratio"
                )
            scored_events, blocked, _, feedback_adjustment_applied = _score_rows(owner_scope)
    if not match_all:
        rerank_strategy = "score_sort"
    mmr_candidate_pool = max(
        search_request.k,
        int(getattr(settings, "search_mmr_candidate_pool", 60)),
    )
    mmr_should_run = bool(
        (not match_all)
        and bool(getattr(settings, "search_mmr_enabled", False))
        and (citation_dup_ratio > 0.35 or scale_trigger_met)
        and len(scored_events) > 1
        and (int((monotonic() - retrieval_started) * 1000) < enhancement_budget_ms)
    )
    if mmr_should_run:
        mmr_input = _dedupe_scored_events_for_mmr(
            scored_events,
            candidate_pool=mmr_candidate_pool,
        )
        mmr_candidates = len(mmr_input)
        mmr_lambda = max(0.0, min(1.0, float(getattr(settings, "search_mmr_lambda", 0.65))))
        if mmr_candidates > 1 and int((monotonic() - retrieval_started) * 1000) < enhancement_budget_ms:
            rerank_strategy = "mmr"
            mmr_started = monotonic()
            selected_events = _mmr_select(mmr_input, top_k=search_request.k, mmr_lambda=mmr_lambda)
            mmr_ms = int((monotonic() - mmr_started) * 1000)
        else:
            selected_events = scored_events[: search_request.k]
    else:
        selected_events = scored_events[: search_request.k]

    hits: list[EventSearchHit] = []
    for score, event in selected_events:
        summary_l0 = str(event.get("summary_l0") or "").strip()
        summary_l1 = coerce_summary_l1(event.get("summary_l1_json"))
        hits.append(
            EventSearchHit(
                id=event["id"],
                ts=event["ts"],
                title=event["title"],
                domain=event["domain"],
                task_type=event["task_type"],
                score=score,
                sensitivity=event["sensitivity"],
                summary_l0=summary_l0,
                summary_l1=summary_l1,
            )
        )

    citations = [hit.id for hit in hits]
    top_handoff_record_ids: list[str] = []
    for hit in hits:
        handoff_meta = handoff_map.get(str(hit.id))
        if not handoff_meta:
            continue
        rid = str(handoff_meta.get("id") or "").strip()
        if rid and rid not in top_handoff_record_ids:
            top_handoff_record_ids.append(rid)
        if len(top_handoff_record_ids) >= 5:
            break
    _bump_activation_scores(
        workspace_id=consumer_ctx.workspace_id,
        owner_id=consumer_ctx.owner_id,
        event_ids=citations,
        settings=settings,
    )
    if match_all:
        retrieval_source = "none"
    _bump_retrieval_counter(retrieval_source)
    episode_event_id_keys = {
        str(key) for key, value in episode_score_lookup.items() if float(value or 0.0) > 0.0
    }
    if episode_score_lookup:
        episode_boost_applied = any(str(hit.id) in episode_event_id_keys for hit in hits)
    retrieval_meta = {
        "source": retrieval_source,
        "reason": retrieval_reason,
        "latency_ms": int((monotonic() - retrieval_started) * 1000),
        "candidate_count": len(rows),
        "hit_count": len(hits),
        "vector_used": bool(query_embedding),
        "embedding_timeout_seconds": float(round(embed_timeout, 4)) if not match_all else 0.0,
        "embedding_timeout_cooldown_applied": bool(
            (not match_all) and bool(embed_state.get("cooldown_applied"))
        ),
        "activation_enabled": bool(getattr(settings, "memory_activation_enabled", True)),
        "activation_hits": sum(1 for value in citations if value in activation_lookup),
        "feedback_adjustment_applied": bool(feedback_adjustment_applied),
        "cross_user_scope_applied": bool(cross_user_scope_applied),
        "cross_user_scope_owners": cross_user_scope_owners,
        "handoff_hits_count": len(top_handoff_record_ids),
        "top_handoff_record_ids": top_handoff_record_ids,
        "resume_packet_available": bool(top_handoff_record_ids),
        "context_tier_used": "l1" if bool(getattr(settings, "context_tiers_enabled", False)) else "l2",
        "summary_coverage": summary_coverage_ratio(hits),
        "planner_used": bool(planner_used),
        "subquery_count": len(subquery_labels),
        "subquery_labels": subquery_labels,
        "episode_boost_applied": bool(episode_boost_applied),
        "activation_boost_applied": bool(activation_boost_applied),
        "query_expansion_used": bool(query_expansion_used),
        "query_expansion_terms": query_expansion_terms[: max(0, int(getattr(settings, "search_query_expansion_max_terms", 4)))],
        "expansion_ms": int(expansion_ms),
        "rerank_strategy": rerank_strategy,
        "mmr_ms": int(mmr_ms),
        "mmr_candidates": int(mmr_candidates),
        "citation_dup_ratio": float(round(citation_dup_ratio, 4)),
    }
    return hits, citations, blocked, retrieval_meta
