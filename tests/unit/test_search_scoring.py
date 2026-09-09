from __future__ import annotations

import re

import pytest
from tce_api.search import (
    _build_or_tsquery,
    _lexical_score_for_candidate,
    _normalize_fts_rank_lookup,
    _normalized_channel_weights,
    _normalized_rrf_scores,
    _scope_sql_parts,
)
from tce_lite_api.store import _build_fts5_query


def _terms(tsquery: str) -> list[str]:
    return re.findall(r"'([^']+)'", tsquery)


def test_build_or_tsquery_splits_compound_tokens_without_phrase_operators() -> None:
    value = _build_or_tsquery("fix the dashboard-takeover x.com http://x.com/a")
    assert _terms(value) == ["fix", "the", "dashboard", "takeover", "com", "http"]
    assert "<->" not in value
    assert not any(set(term) & set("-./:") for term in _terms(value))


def test_build_or_tsquery_sanitizes_operators_dedupes_and_preserves_order() -> None:
    value = _build_or_tsquery("AND near * alpha alpha can't OR beta")
    assert _terms(value) == ["and", "near", "alpha", "can", "or", "beta"]
    assert "*" not in value


def test_build_or_tsquery_bounds_terms_and_discards_long_tokens() -> None:
    long_token = "x" * 60
    source = " ".join([long_token, "a", *[f"term{index}" for index in range(40)]])
    terms = _terms(_build_or_tsquery(source))
    assert len(terms) == 32
    assert long_token not in terms
    assert "a" not in terms
    assert terms[:2] == ["term0", "term1"]


def test_build_or_tsquery_returns_empty_for_unusable_input() -> None:
    assert _build_or_tsquery("a I * -") == ""


def test_build_fts5_query_strips_operators_and_bounds_terms() -> None:
    source = 'AND near * alpha alpha "beta" path:value ' + " ".join(
        f"term{index}" for index in range(40)
    )
    query = _build_fts5_query(source)
    assert query.startswith('"and" OR "near" OR "alpha" OR "beta" OR "path" OR "value"')
    assert query.count(" OR ") == 31
    assert "*" not in query
    assert ":" not in query


def test_build_fts5_query_rejects_unusable_tokens() -> None:
    assert _build_fts5_query("a I * -") == ""
    assert "x" * 41 not in _build_fts5_query(f"valid {'x' * 41}")


def test_normalize_fts_rank_lookup_maps_top_hit_to_one() -> None:
    rows = [
        {"id": "top", "lexical_rank": 0.5},
        {"id": "middle", "lexical_rank": 0.25},
        {"id": "zero", "lexical_rank": 0.0},
    ]
    assert _normalize_fts_rank_lookup(rows) == {
        "top": 1.0,
        "middle": 0.5,
        "zero": 0.0,
    }


def test_normalize_fts_rank_lookup_handles_zero_maximum() -> None:
    assert _normalize_fts_rank_lookup([{"id": "a", "lexical_rank": 0.0}]) == {"a": 0.0}


def test_rrf_uses_one_based_ranks_and_normalizes() -> None:
    scores = _normalized_rrf_scores(
        [["shared", "lexical"], ["shared", "vector"]],
        rrf_k=60,
    )
    assert scores["shared"] == pytest.approx(1.0)
    assert scores["lexical"] == pytest.approx(0.0)
    assert scores["vector"] == pytest.approx(0.0)


def test_rrf_deduplicates_ids_within_each_channel() -> None:
    assert _normalized_rrf_scores([["a", "a"]], rrf_k=60) == {"a": 1.0}


@pytest.mark.parametrize(
    ("event_id", "merge_rank", "match_all", "fts", "ilike", "expected"),
    [
        ("fts", 19, False, {"fts": 0.75}, {"fts": 8}, 0.75),
        ("ilike", 19, False, {}, {"ilike": 2}, 0.94),
        ("all", 2, True, {}, {}, 0.94),
        ("vector", 2, False, {}, {}, 0.0),
    ],
)
def test_lexical_score_uses_only_the_candidate_channel(
    event_id: str,
    merge_rank: int,
    match_all: bool,
    fts: dict[str, float],
    ilike: dict[str, int],
    expected: float,
) -> None:
    assert _lexical_score_for_candidate(
        event_id=event_id,
        merge_rank=merge_rank,
        match_all=match_all,
        fts_rank_lookup=fts,
        ilike_rank_lookup=ilike,
    ) == pytest.approx(expected)


def test_normalized_channel_weights_are_configurable_and_safe() -> None:
    assert _normalized_channel_weights(1.0, 0.0) == (1.0, 0.0)
    assert _normalized_channel_weights(0.0, 1.0) == (0.0, 1.0)
    assert _normalized_channel_weights(0.65, 0.35) == pytest.approx((0.65, 0.35))
    assert _normalized_channel_weights(0.0, 0.0) == (0.65, 0.35)


def test_scope_sql_preserves_unscoped_rows_and_normalizes_owners() -> None:
    predicates, params = _scope_sql_parts(
        workspace_id="personal",
        owner_ids=[" Codex-Executor\n", "codex-executor", "claude-executor"],
    )
    sql = " AND ".join(predicates)
    assert "context->>'_tce_workspace' IS NULL" in sql
    assert "context->>'_tce_workspace' = ''" in sql
    assert "context->>'_tce_owner' IS NULL" in sql
    assert "btrim(context->>'_tce_owner', E' \\t\\n\\r\\f\\v') = ''" in sql
    assert "CAST(:scope_owners AS text[])" in sql
    assert params == {
        "scope_workspace": "personal",
        "scope_owners": ["claude-executor", "codex-executor"],
    }


def test_scope_sql_omits_owner_predicate_for_empty_scope() -> None:
    predicates, params = _scope_sql_parts(workspace_id="personal", owner_ids=[])
    sql = " AND ".join(predicates)
    assert "_tce_workspace" in sql
    assert "_tce_owner" not in sql
    assert ":scope_owners" not in sql
    assert params == {"scope_workspace": "personal"}


def test_scope_sql_parts_strict_mode_drops_null_branch() -> None:
    predicates, params = _scope_sql_parts(
        workspace_id="personal",
        owner_ids=["codex-executor", "claude-executor"],
        strict=True,
    )
    sql = " AND ".join(predicates)
    assert "IS NULL" not in sql
    assert "= ''" not in sql
    assert "context->>'_tce_workspace'" in sql
    assert "CAST(:scope_owners AS text[])" in sql
    assert params == {
        "scope_workspace": "personal",
        "scope_owners": ["claude-executor", "codex-executor"],
    }
