from __future__ import annotations

from unittest.mock import patch

from tce_api.search import (
    _dedupe_scored_events_for_mmr,
    _embed_cache_key,
    _expand_owner_scope_from_rows,
    _is_embedding_timeout_cooldown_active,
    _is_retryable_error,
    _mark_embedding_timeout_cooldown,
    _query_requests_cross_user_memory,
    _resolve_owner_scope,
    _run_with_retry,
    _should_skip_query_expansion,
)


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


class _MappingsResult:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, str]]:
        return self._rows


class _FakeDb:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def execute(self, *_args, **_kwargs) -> _MappingsResult:
        return _MappingsResult(self._rows)

    def rollback(self) -> None:  # pragma: no cover - compatibility stub
        return None


def test_query_requests_cross_user_memory_trigger() -> None:
    assert _query_requests_cross_user_memory("read codex memory from claude")
    assert _query_requests_cross_user_memory("what did codex discuss recently")
    assert _query_requests_cross_user_memory("show claude session history")
    assert not _query_requests_cross_user_memory("is codex better than claude")
    assert not _query_requests_cross_user_memory("summarize my latest memory")


def test_resolve_owner_scope_expands_only_on_explicit_cross_user_query() -> None:
    fake_db = _FakeDb(
        [
            {"owner_id": "codex-executor"},
            {"owner_id": "claude-executor"},
        ]
    )
    owner_scope, applied, owners = _resolve_owner_scope(
        fake_db,  # type: ignore[arg-type]
        workspace_id="personal",
        owner_id="claude-executor",
        query_text="read codex memory from claude",
    )
    assert applied
    assert "codex-executor" in owner_scope
    assert "claude-executor" in owner_scope
    assert "codex-executor" in owners


def test_resolve_owner_scope_expands_on_executor_history_query() -> None:
    fake_db = _FakeDb(
        [
            {"owner_id": "codex-executor"},
            {"owner_id": "claude-executor"},
        ]
    )
    owner_scope, applied, owners = _resolve_owner_scope(
        fake_db,  # type: ignore[arg-type]
        workspace_id="personal",
        owner_id="claude-executor",
        query_text="what did codex discuss recently",
    )
    assert applied
    assert "codex-executor" in owner_scope
    assert "claude-executor" in owner_scope
    assert "codex-executor" in owners


def test_expand_owner_scope_from_rows_uses_workspace_owners() -> None:
    rows = [
        {"context": {"_tce_workspace": "personal", "_tce_owner": "codex-executor"}},
        {"context": {"_tce_workspace": "personal", "_tce_owner": "claude-executor"}},
        {"context": {"_tce_workspace": "other", "_tce_owner": "other-user"}},
        {"context": {"_tce_workspace": "personal"}},
    ]
    expanded = _expand_owner_scope_from_rows(
        rows,
        workspace_id="personal",
        current_scope={"joeljoseph"},
    )
    assert expanded == {"joeljoseph", "codex-executor", "claude-executor"}


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
