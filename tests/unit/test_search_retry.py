from __future__ import annotations

from unittest.mock import patch

from tce_api.search import (
    _dedupe_scored_events_for_mmr,
    _embed_cache_key,
    _is_embedding_timeout_cooldown_active,
    _is_retryable_error,
    _mark_embedding_timeout_cooldown,
    _resolve_owner_scope,
    _run_with_retry,
    _should_skip_query_expansion,
)
from tce_shared.scope import SCOPE_POLICY_REVISION, ResolvedScope


def test_retryable_error_detection() -> None:
    assert _is_retryable_error("request timed out")
    assert _is_retryable_error("503 service unavailable")
    assert not _is_retryable_error("validation failed: bad input")


def test_run_with_retry_succeeds_after_transient_failure() -> None:
    attempts = {"count": 0}

    def _flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("timeout talking to backend")
        return "ok"

    result = _run_with_retry(
        _flaky,
        attempts=2,
        base_backoff_ms=1,
        max_retry_budget_ms=20,
    )
    assert result == "ok"
    assert attempts["count"] == 2


def test_run_with_retry_supports_jittered_backoff() -> None:
    attempts = {"count": 0}

    def _flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("timeout talking to backend")
        return "ok"

    with patch("tce_api.search.sleep", return_value=None), patch("tce_api.search.random.randint", return_value=0):
        result = _run_with_retry(
            _flaky,
            attempts=2,
            base_backoff_ms=1,
            max_retry_budget_ms=20,
            jitter_ratio=0.25,
        )
    assert result == "ok"
    assert attempts["count"] == 2


def test_should_skip_query_expansion_when_candidate_count_is_high() -> None:
    assert _should_skip_query_expansion(
        initial_candidate_count=40,
        requested_k=10,
        dup_ratio=0.2,
        candidate_multiplier=4,
        dup_ratio_threshold=0.8,
    )


def test_should_skip_query_expansion_when_duplicate_ratio_is_high() -> None:
    assert _should_skip_query_expansion(
        initial_candidate_count=20,
        requested_k=10,
        dup_ratio=0.95,
        candidate_multiplier=4,
        dup_ratio_threshold=0.8,
    )


def test_dedupe_scored_events_for_mmr_caps_and_dedupes() -> None:
    scored = [
        (
            0.95,
            {
                "id": "a",
                "title": "Fix login bug",
                "domain": "auth",
                "task_type": "bugfix",
            },
        ),
        (
            0.90,
            {
                "id": "b",
                "title": "Fix login bug",
                "domain": "auth",
                "task_type": "bugfix",
            },
        ),
        (
            0.89,
            {
                "id": "c",
                "title": "Improve deploy pipeline",
                "domain": "release",
                "task_type": "deploy",
            },
        ),
    ]
    deduped = _dedupe_scored_events_for_mmr(scored, candidate_pool=3)
    assert len(deduped) == 2
    assert deduped[0][1]["id"] == "a"


def _scope(*, owner_ids: frozenset[str], continuity_intent: bool) -> ResolvedScope:
    return ResolvedScope(
        workspace_id="personal",
        executor_id="claude-executor",
        owner_id="claude-executor",
        subject_user_id="human",
        project_id=None,
        project_binding="unbound",
        task_id=None,
        owner_ids=owner_ids,
        continuity_intent=continuity_intent,
    )


def test_resolve_owner_scope_uses_explicit_intent_only() -> None:
    # No intent: the owner scope is exactly the authenticated owner, whatever the query says.
    own_only = _resolve_owner_scope(scope=_scope(owner_ids=frozenset({"claude-executor"}), continuity_intent=False))
    assert own_only == ({"claude-executor"}, False, ["claude-executor"])

    # A second owner without intent (defensive: resolve_scope never produces this) is not "applied".
    no_intent = _resolve_owner_scope(scope=_scope(owner_ids=frozenset({"claude-executor", "codex-executor"}), continuity_intent=False))
    assert no_intent[1] is False

    # Explicit continuity intent with a target owner is the only path that widens the scope.
    widened = _resolve_owner_scope(scope=_scope(owner_ids=frozenset({"claude-executor", "codex-executor"}), continuity_intent=True))
    assert widened == ({"claude-executor", "codex-executor"}, True, ["claude-executor", "codex-executor"])
    assert _scope(owner_ids=frozenset({"claude-executor"}), continuity_intent=False).policy_revision == SCOPE_POLICY_REVISION


class _CooldownSettings:
    search_embedding_timeout_cooldown_enabled = True
    redis_url = "redis://unused"


def test_embed_cache_key_normalizes_whitespace_and_case() -> None:
    assert _embed_cache_key("Read Codex Memory") == _embed_cache_key("  read   codex   memory  ")


def test_embedding_timeout_cooldown_uses_local_memo_without_redis() -> None:
    settings = _CooldownSettings()
    with patch("tce_api.search.get_redis_client", return_value=None):
        assert not _is_embedding_timeout_cooldown_active("read codex memory", settings=settings)
        _mark_embedding_timeout_cooldown(" Read   Codex Memory ", settings=settings, cooldown_seconds=1.0)
        assert _is_embedding_timeout_cooldown_active("read codex memory", settings=settings)
