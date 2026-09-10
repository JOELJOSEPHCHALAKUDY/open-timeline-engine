from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from tce_shared.project_context import canonical_project_context
from tce_shared.scope import (
    PROJECT_BOUND,
    PROJECT_INHERITED,
    PROJECT_UNBOUND,
    SCOPE_POLICY_REVISION,
    ResolvedScope,
    ScopeDialect,
    events_scope_predicate,
    goals_scope_predicate,
    handoffs_scope_predicate,
    is_bound,
    normalize_owner_token,
    resolve_scope,
)


@dataclass(frozen=True, slots=True)
class _Auth:
    consumer: str
    workspace_id: str
    user_id: str
    behavior_subject_id: str


AUTH = _Auth(consumer="codex-executor", workspace_id="shared", user_id="codex", behavior_subject_id="joel")
BOUND_HINT = {"project_root": "/repos/ote", "repo_remote": "https://user:secret@github.com/acme/ote.git"}


# --- identity comes from credentials, never from the body -------------------


def test_scope_identity_is_derived_from_auth_not_body() -> None:
    spoofed_body = {
        **BOUND_HINT,
        "workspace_id": "victim-workspace",
        "user_id": "victim",
        "owner_id": "victim",
        "consumer": "victim-executor",
        "behavior_subject_id": "victim",
    }

    scope = resolve_scope(AUTH, project_hint=spoofed_body)

    assert scope.workspace_id == "shared"
    assert scope.executor_id == "codex-executor"
    assert scope.owner_id == "codex"
    assert scope.subject_user_id == "joel"
    assert scope.owner_ids == frozenset({"codex"})
    assert scope.policy_revision == SCOPE_POLICY_REVISION == "p0-2026-09"
    assert scope.task_id is None
    assert scope.continuity_intent is False
    assert scope.legacy_session_scope is False
    assert scope.retention_days == 90


def test_target_owner_only_broadens_scope_with_explicit_continuity_intent() -> None:
    silent = resolve_scope(AUTH, target_owner="Claude-Executor")
    assert silent.owner_ids == frozenset({"codex"})
    assert silent.sql_owner_ids() == ["codex"]

    explicit = resolve_scope(AUTH, target_owner="  Claude-Executor ", continuity_intent=True, source_session_id="codex-a")
    # Raw owner ids are stored as-is in the DB; only surrounding whitespace is dropped.
    assert explicit.owner_ids == frozenset({"codex", "Claude-Executor"})
    assert explicit.sql_owner_ids() == ["Claude-Executor", "codex"]
    assert explicit.continuity_intent is True
    assert explicit.source_session_id == "codex-a"

    blank_target = resolve_scope(AUTH, target_owner="   ", continuity_intent=True)
    assert blank_target.owner_ids == frozenset({"codex"})


def test_normalize_owner_token() -> None:
    assert normalize_owner_token(None) == ""
    assert normalize_owner_token("  Codex-Executor ") == "codex-executor"


# --- project binding --------------------------------------------------------


def test_explicit_project_hint_binds_scope() -> None:
    scope = resolve_scope(AUTH, project_hint=BOUND_HINT, task_id="sess-1")

    assert scope.project_binding == PROJECT_BOUND
    assert scope.project_id == canonical_project_context(dict(BOUND_HINT))["project_id"]
    assert scope.is_bound() is True
    assert is_bound(scope) is True
    assert scope.task_id == "sess-1"
    assert "secret" not in str(scope)


def test_session_project_is_inherited_when_hint_has_no_identity() -> None:
    session_project = canonical_project_context(dict(BOUND_HINT))

    scope = resolve_scope(AUTH, project_hint={"domain": "coding"}, session_project=session_project)

    assert scope.project_binding == PROJECT_INHERITED
    assert scope.project_id == session_project["project_id"]
    assert scope.is_bound() is True


def test_explicit_hint_wins_over_session_project() -> None:
    session_project = canonical_project_context({"project_root": "/repos/one"})

    scope = resolve_scope(AUTH, project_hint={"project_root": "/repos/two"}, session_project=session_project)

    assert scope.project_binding == PROJECT_BOUND
    assert scope.project_id == canonical_project_context({"project_root": "/repos/two"})["project_id"]
    assert scope.project_id != session_project["project_id"]


@pytest.mark.parametrize(
    ("project_hint", "session_project"),
    [
        (None, None),
        ({}, {}),
        ({"domain": "coding"}, None),
        (None, {"domain": "coding"}),
        # A stored label without an identity is ambiguous, not bound.
        (None, {"project_binding": "bound", "project_id": ""}),
        ({"project_root": "   "}, {"project_remote": ""}),
    ],
)
def test_scope_is_unbound_when_project_is_missing_or_ambiguous(project_hint: dict[str, object] | None, session_project: dict[str, object] | None) -> None:
    scope = resolve_scope(AUTH, project_hint=project_hint, session_project=session_project)

    assert scope.project_binding == PROJECT_UNBOUND
    assert scope.project_id is None
    assert scope.is_bound() is False
    assert is_bound(scope) is False


# --- value semantics --------------------------------------------------------


def test_resolved_scope_equality_and_hash() -> None:
    first = resolve_scope(AUTH, project_hint=BOUND_HINT, target_owner="claude", continuity_intent=True)
    second = resolve_scope(AUTH, project_hint=BOUND_HINT, target_owner="claude", continuity_intent=True)
    different = resolve_scope(AUTH, project_hint=BOUND_HINT, target_owner="claude")

    assert first == second
    assert hash(first) == hash(second)
    assert first != different
    assert len({first, second, different}) == 2
    with pytest.raises(AttributeError):
        first.workspace_id = "other"  # type: ignore[misc]


# --- shared SQL predicate definition ---------------------------------------


def _count_placeholders(sql: str) -> int:
    return sql.count("?")


def test_events_predicate_postgres_lenient_and_strict() -> None:
    scope = resolve_scope(AUTH, target_owner="Claude", continuity_intent=True)

    lenient = events_scope_predicate(scope, dialect=ScopeDialect.POSTGRES)
    assert len(lenient.clauses) == 2
    assert "IS NULL" in lenient.where_sql
    assert "context->>'_tce_workspace' = :scope_workspace" in lenient.where_sql
    assert "= ANY(CAST(:scope_owners AS text[]))" in lenient.where_sql
    assert lenient.named_params == {"scope_workspace": "shared", "scope_owners": ["claude", "codex"]}
    assert lenient.positional_params == ()
    assert lenient.where_sql == " AND ".join(lenient.clauses)

    strict = events_scope_predicate(scope, dialect=ScopeDialect.POSTGRES, strict=True)
    assert "IS NULL" not in strict.where_sql
    assert "= ''" not in strict.where_sql
    assert strict.named_params == lenient.named_params


def test_events_predicate_sqlite_uses_positional_params_in_clause_order() -> None:
    scope = resolve_scope(AUTH, target_owner="Claude", continuity_intent=True)

    predicate = events_scope_predicate(scope, dialect=ScopeDialect.SQLITE, context_column="e.context")

    assert "json_extract(e.context, '$._tce_workspace')" in predicate.where_sql
    assert "json_extract(e.context, '$._tce_owner')" in predicate.where_sql
    assert ":scope_" not in predicate.where_sql
    assert predicate.named_params == {}
    assert predicate.positional_params == ("shared", "claude", "codex")
    assert _count_placeholders(predicate.where_sql) == len(predicate.positional_params)


def test_events_predicate_project_filter_only_when_requested_and_bound() -> None:
    unbound = resolve_scope(AUTH)
    bound = resolve_scope(AUTH, project_hint=BOUND_HINT)

    assert "project_id" not in events_scope_predicate(bound, dialect=ScopeDialect.POSTGRES).where_sql
    assert "project_id" not in events_scope_predicate(unbound, dialect=ScopeDialect.POSTGRES, include_project=True).where_sql

    filtered = events_scope_predicate(bound, dialect=ScopeDialect.POSTGRES, include_project=True)
    assert "context->>'project_id' = :scope_project" in filtered.where_sql
    assert filtered.named_params["scope_project"] == bound.project_id


def test_goals_predicate_binds_workspace_and_owner_only() -> None:
    scope = resolve_scope(AUTH, target_owner="claude", continuity_intent=True)

    pg = goals_scope_predicate(scope, dialect=ScopeDialect.POSTGRES, session_id="sess-1")
    assert pg.clauses == ("workspace_id = :scope_workspace", "user_id = :scope_owner", "session_id = :scope_session")
    assert pg.named_params == {"scope_workspace": "shared", "scope_owner": "codex", "scope_session": "sess-1"}

    lite = goals_scope_predicate(scope, dialect=ScopeDialect.SQLITE)
    assert lite.clauses == ("workspace_id = ?", "user_id = ?")
    assert lite.positional_params == ("shared", "codex")


def test_handoffs_predicate_default_scope_is_not_the_reader_session() -> None:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    scope = resolve_scope(AUTH, task_id="claude-b", target_owner="codex-executor", continuity_intent=True)

    default = handoffs_scope_predicate(scope, dialect=ScopeDialect.POSTGRES, reader_session_id="claude-b", now=now)
    assert "session_id" not in default.where_sql
    assert default.clauses[0] == "workspace_id = :scope_workspace"
    assert "owner_id = ANY(CAST(:scope_owners AS text[]))" in default.where_sql
    assert "expires_at > :scope_now" in default.where_sql
    # handoff owner ids are compared raw, not lowercased
    assert default.named_params == {"scope_workspace": "shared", "scope_owners": ["codex", "codex-executor"], "scope_now": now}

    sourced = handoffs_scope_predicate(resolve_scope(AUTH, source_session_id="codex-a"), dialect=ScopeDialect.POSTGRES)
    assert "session_id = :scope_source_session" in sourced.where_sql
    assert sourced.named_params["scope_source_session"] == "codex-a"
    assert "expires_at" not in sourced.where_sql

    legacy = handoffs_scope_predicate(resolve_scope(AUTH, legacy_session_scope=True), dialect=ScopeDialect.POSTGRES, reader_session_id="claude-b")
    assert "session_id = :scope_session" in legacy.where_sql
    assert legacy.named_params["scope_session"] == "claude-b"

    with pytest.raises(ValueError, match="legacy_session_scope"):
        handoffs_scope_predicate(resolve_scope(AUTH, legacy_session_scope=True), dialect=ScopeDialect.POSTGRES)


def test_handoffs_predicate_project_clause_and_sqlite_time_binding() -> None:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    bound = resolve_scope(AUTH, project_hint=BOUND_HINT)
    inherited = resolve_scope(AUTH, session_project=canonical_project_context(dict(BOUND_HINT)))

    pg = handoffs_scope_predicate(bound, dialect=ScopeDialect.POSTGRES)
    assert "(project_id = :scope_project OR project_id IS NULL)" in pg.where_sql
    assert "project_id" not in handoffs_scope_predicate(inherited, dialect=ScopeDialect.POSTGRES).where_sql

    lite = handoffs_scope_predicate(bound, dialect=ScopeDialect.SQLITE, now=now)
    assert lite.positional_params == ("shared", "codex", now.isoformat(), bound.project_id)
    assert _count_placeholders(lite.where_sql) == len(lite.positional_params)


def test_sqlite_predicates_execute_and_filter_cross_scope_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY, context TEXT NOT NULL)")
    conn.execute("CREATE TABLE handoff_records (id TEXT PRIMARY KEY, workspace_id TEXT, owner_id TEXT, session_id TEXT, project_id TEXT, expires_at TEXT)")
    conn.execute("CREATE TABLE autonomy_goals (id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT, session_id TEXT)")
    bound = resolve_scope(AUTH, project_hint=BOUND_HINT, target_owner="Claude", continuity_intent=True)
    assert bound.project_id is not None
    events = [
        ("mine", '{"_tce_workspace": "shared", "_tce_owner": "codex"}'),
        ("peer", '{"_tce_workspace": "shared", "_tce_owner": " CLAUDE "}'),
        ("untagged", "{}"),
        ("other-owner", '{"_tce_workspace": "shared", "_tce_owner": "mallory"}'),
        ("other-workspace", '{"_tce_workspace": "other", "_tce_owner": "codex"}'),
        ("mine-project", f'{{"_tce_workspace": "shared", "_tce_owner": "codex", "project_id": "{bound.project_id}"}}'),
    ]
    conn.executemany("INSERT INTO events (id, context) VALUES (?, ?)", events)
    handoffs = [
        ("h-mine", "shared", "codex", "codex-a", bound.project_id, "2999-01-01T00:00:00+00:00"),
        ("h-peer-null-project", "shared", "Claude", "claude-a", None, "2999-01-01T00:00:00+00:00"),
        ("h-expired", "shared", "codex", "codex-b", bound.project_id, "2000-01-01T00:00:00+00:00"),
        ("h-other-workspace", "other", "codex", "codex-a", bound.project_id, "2999-01-01T00:00:00+00:00"),
        ("h-other-owner", "shared", "mallory", "m-a", bound.project_id, "2999-01-01T00:00:00+00:00"),
        ("h-other-project", "shared", "codex", "codex-c", "proj_ffffffffffffffffffffffff", "2999-01-01T00:00:00+00:00"),
    ]
    conn.executemany("INSERT INTO handoff_records VALUES (?, ?, ?, ?, ?, ?)", handoffs)
    conn.executemany(
        "INSERT INTO autonomy_goals VALUES (?, ?, ?, ?)",
        [("g-mine", "shared", "codex", "s1"), ("g-peer", "shared", "Claude", "s1"), ("g-other-ws", "other", "codex", "s1")],
    )

    def ids(sql: str, params: tuple[object, ...]) -> set[str]:
        return {str(row["id"]) for row in conn.execute(sql, params).fetchall()}

    lenient = events_scope_predicate(bound, dialect=ScopeDialect.SQLITE)
    assert ids(f"SELECT id FROM events WHERE {lenient.where_sql}", lenient.positional_params) == {"mine", "peer", "untagged", "mine-project"}

    strict = events_scope_predicate(bound, dialect=ScopeDialect.SQLITE, strict=True)
    assert ids(f"SELECT id FROM events WHERE {strict.where_sql}", strict.positional_params) == {"mine", "peer", "mine-project"}

    by_project = events_scope_predicate(bound, dialect=ScopeDialect.SQLITE, strict=True, include_project=True)
    assert ids(f"SELECT id FROM events WHERE {by_project.where_sql}", by_project.positional_params) == {"mine-project"}

    handoff = handoffs_scope_predicate(bound, dialect=ScopeDialect.SQLITE, now=datetime(2026, 9, 9, tzinfo=UTC))
    assert ids(f"SELECT id FROM handoff_records WHERE {handoff.where_sql}", handoff.positional_params) == {"h-mine", "h-peer-null-project"}

    sourced = handoffs_scope_predicate(resolve_scope(AUTH, source_session_id="codex-c"), dialect=ScopeDialect.SQLITE)
    assert ids(f"SELECT id FROM handoff_records WHERE {sourced.where_sql}", sourced.positional_params) == {"h-other-project"}

    goals = goals_scope_predicate(bound, dialect=ScopeDialect.SQLITE, session_id="s1")
    assert ids(f"SELECT id FROM autonomy_goals WHERE {goals.where_sql}", goals.positional_params) == {"g-mine"}


def test_predicate_where_sql_is_never_empty() -> None:
    scope = ResolvedScope(
        workspace_id="shared",
        executor_id="codex-executor",
        owner_id="",
        subject_user_id="joel",
        project_id=None,
        project_binding=PROJECT_UNBOUND,
        task_id=None,
        owner_ids=frozenset({""}),
    )

    predicate = events_scope_predicate(scope, dialect=ScopeDialect.POSTGRES, strict=True)

    # Blank owner ids never produce an empty IN-list; the workspace clause still applies.
    assert predicate.clauses == ("context->>'_tce_workspace' = :scope_workspace",)
    assert predicate.named_params == {"scope_workspace": "shared"}
    assert predicate.where_sql == predicate.clauses[0]


def test_narrowed_to_owner_restricts_to_an_authorised_peer() -> None:
    """An explicit target is a hard filter: only the peer's records remain in scope."""
    from tce_shared.scope import ResolvedScope

    scope = ResolvedScope(workspace_id="ws", executor_id="claude", owner_id="claude", subject_user_id="claude",
                          project_id=None, project_binding="unbound", task_id=None,
                          owner_ids=frozenset({"claude", "codex"}), continuity_intent=True)
    narrowed = scope.narrowed_to_owner("codex")
    assert narrowed.sql_owner_ids() == ["codex"]
    assert scope.sql_owner_ids() == ["claude", "codex"], "the original scope is immutable"


def test_narrowed_to_owner_can_never_widen_access() -> None:
    """A target the scope never authorised leaves the scope untouched."""
    from tce_shared.scope import ResolvedScope

    scope = ResolvedScope(workspace_id="ws", executor_id="claude", owner_id="claude", subject_user_id="claude",
                          project_id=None, project_binding="unbound", task_id=None,
                          owner_ids=frozenset({"claude"}))
    assert scope.narrowed_to_owner("mallory") is scope
    assert scope.narrowed_to_owner("") is scope
    assert scope.narrowed_to_owner(None) is scope
