"""Server-bound request scope shared by the Full and Lite runtimes.

A :class:`ResolvedScope` is derived from the authenticated ``AuthContext``
only - never from a request body - and is the single definition both backends
use to filter events, autonomy goals and handoff records. The predicate
helpers render that one definition for PostgreSQL (SQLAlchemy ``text()`` with
named binds) and SQLite (positional ``?`` binds) so neither runtime carries
its own copy of the scope rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .project_context import canonical_project_context

__all__ = [
    "OWNER_TAG",
    "PROJECT_BOUND",
    "PROJECT_INHERITED",
    "PROJECT_TAG",
    "PROJECT_UNBOUND",
    "SCOPE_POLICY_REVISION",
    "WORKSPACE_TAG",
    "AuthLike",
    "ResolvedScope",
    "ScopeDialect",
    "ScopePredicate",
    "events_scope_predicate",
    "goals_scope_predicate",
    "handoffs_scope_predicate",
    "is_bound",
    "observations_scope_predicate",
    "normalize_owner_token",
    "resolve_scope",
]

SCOPE_POLICY_REVISION = "p0-2026-09"
PROJECT_BOUND = "bound"
PROJECT_INHERITED = "inherited"
PROJECT_UNBOUND = "unbound"

# Event context keys stamped by the ingest path on every stored event.
WORKSPACE_TAG = "_tce_workspace"
OWNER_TAG = "_tce_owner"
PROJECT_TAG = "project_id"

_PG_WHITESPACE = r"E' \t\n\r\f\v'"
_SQLITE_WHITESPACE = "' ' || char(9, 10, 11, 12, 13)"


class AuthLike(Protocol):
    """Read-only view of an authenticated caller (Full ``AuthContext`` or Lite frozen dataclass)."""

    @property
    def consumer(self) -> str: ...

    @property
    def workspace_id(self) -> str: ...

    @property
    def user_id(self) -> str: ...

    @property
    def behavior_subject_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    workspace_id: str
    executor_id: str  # auth.consumer
    owner_id: str  # auth.user_id == events.context._tce_owner
    subject_user_id: str  # auth.behavior_subject_id (decision_observations key)
    project_id: str | None
    project_binding: str  # PROJECT_BOUND | PROJECT_INHERITED | PROJECT_UNBOUND
    task_id: str | None  # directive_id or session_id of the task at hand
    policy_revision: str = SCOPE_POLICY_REVISION
    owner_ids: frozenset[str] = field(default_factory=frozenset)  # always contains owner_id
    continuity_intent: bool = False
    source_session_id: str | None = None
    legacy_session_scope: bool = False
    retention_days: int = 90

    def is_bound(self) -> bool:
        return self.project_binding in (PROJECT_BOUND, PROJECT_INHERITED)

    def sql_owner_ids(self) -> list[str]:
        return sorted(self.owner_ids)

    def narrowed_to_owner(self, target_owner: str | None) -> ResolvedScope:
        """Restrict the owner set to one explicitly targeted owner.

        An explicit target is a hard filter, not a ranking preference: a reader asking
        for a peer executor's handoff must not get its own records back because they
        happened to score higher. Observed live before this existed — Claude requesting
        ``target_owner=codex-executor`` was handed its own earlier completion.

        This can only ever narrow. A target the scope did not already authorise leaves
        the scope unchanged, so it can never be used to widen access.
        """
        from dataclasses import replace as _dc_replace

        target = str(target_owner or "").strip()
        if not target or target not in self.owner_ids:
            return self
        return _dc_replace(self, owner_ids=frozenset({target}))


def is_bound(scope: ResolvedScope) -> bool:
    return scope.is_bound()


def normalize_owner_token(value: str | None) -> str:
    return str(value or "").strip().lower()


def _project_id_from(context: Mapping[str, Any] | None) -> str | None:
    """Return the canonical project id carried by ``context`` or ``None`` when it has no identity."""
    if not isinstance(context, Mapping) or not context:
        return None
    resolved = canonical_project_context(dict(context))
    project_id = str(resolved.get("project_id") or "").strip()
    return project_id or None


def resolve_scope(
    auth: AuthLike,
    *,
    project_hint: Mapping[str, Any] | None = None,  # body.app_context
    session_project: Mapping[str, Any] | None = None,  # takeover_sessions.takeover_context["project_context"]
    task_id: str | None = None,
    target_owner: str | None = None,
    continuity_intent: bool = False,
    source_session_id: str | None = None,
    legacy_session_scope: bool = False,
    retention_days: int = 90,
) -> ResolvedScope:
    """Build the request scope from server-bound credentials.

    Identity (workspace, executor, owner, subject) comes exclusively from
    ``auth``. The body may only contribute a project hint and an explicit
    continuity target; a target owner without ``continuity_intent`` never
    broadens the owner set.
    """
    explicit_project_id = _project_id_from(project_hint)
    if explicit_project_id:
        project_id: str | None = explicit_project_id
        project_binding = PROJECT_BOUND
    else:
        inherited_project_id = _project_id_from(session_project)
        if inherited_project_id:
            project_id = inherited_project_id
            project_binding = PROJECT_INHERITED
        else:
            project_id = None
            project_binding = PROJECT_UNBOUND

    owner_ids = {auth.user_id}
    if continuity_intent and target_owner:
        # Raw, not lowercased: the DB stores raw owner ids and runtimes compare via owner_id IN (...).
        target = target_owner.strip()
        if target:
            owner_ids.add(target)

    return ResolvedScope(
        workspace_id=auth.workspace_id,
        executor_id=auth.consumer,
        owner_id=auth.user_id,
        subject_user_id=auth.behavior_subject_id,
        project_id=project_id,
        project_binding=project_binding,
        task_id=task_id,
        owner_ids=frozenset(owner_ids),
        continuity_intent=bool(continuity_intent),
        source_session_id=source_session_id,
        legacy_session_scope=bool(legacy_session_scope),
        retention_days=int(retention_days),
    )


# --- SQL predicate rendering ---------------------------------------------------


class ScopeDialect(StrEnum):
    POSTGRES = "postgres"  # SQLAlchemy text(): named binds, JSONB ->> extraction
    SQLITE = "sqlite"  # sqlite3: positional binds, json_extract()


@dataclass(frozen=True, slots=True)
class ScopePredicate:
    """Rendered WHERE fragments for one scope on one dialect.

    ``clauses`` are self-contained boolean expressions safe to AND onto an
    existing WHERE. PostgreSQL callers bind ``named_params``; SQLite callers
    bind ``positional_params`` (already in clause order).
    """

    dialect: ScopeDialect
    clauses: tuple[str, ...]
    named_params: dict[str, Any]
    positional_params: tuple[Any, ...]

    @property
    def where_sql(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else "1 = 1"


class _PredicateBuilder:
    def __init__(self, dialect: ScopeDialect) -> None:
        self.dialect = dialect
        self._clauses: list[str] = []
        self._named: dict[str, Any] = {}
        self._positional: list[Any] = []

    def bind(self, name: str, value: Any) -> str:
        """Register one scalar bind value and return its placeholder."""
        if self.dialect is ScopeDialect.POSTGRES:
            self._named[name] = value
            return f":{name}"
        self._positional.append(value)
        return "?"

    def bind_membership(self, name: str, values: list[str]) -> str:
        """Register a list bind and return the ``= ANY(...)`` / ``IN (...)`` tail."""
        if self.dialect is ScopeDialect.POSTGRES:
            self._named[name] = list(values)
            return f"= ANY(CAST(:{name} AS text[]))"
        self._positional.extend(values)
        return "IN (" + ", ".join("?" for _ in values) + ")"

    def json_text(self, column: str, key: str) -> str:
        if self.dialect is ScopeDialect.POSTGRES:
            return f"{column}->>'{key}'"
        return f"json_extract({column}, '$.{key}')"

    def trimmed_lower(self, expression: str) -> str:
        whitespace = _PG_WHITESPACE if self.dialect is ScopeDialect.POSTGRES else _SQLITE_WHITESPACE
        return f"lower({self.trim_fn}({expression}, {whitespace}))"

    def trimmed(self, expression: str) -> str:
        whitespace = _PG_WHITESPACE if self.dialect is ScopeDialect.POSTGRES else _SQLITE_WHITESPACE
        return f"{self.trim_fn}({expression}, {whitespace})"

    @property
    def trim_fn(self) -> str:
        return "btrim" if self.dialect is ScopeDialect.POSTGRES else "trim"

    def add(self, clause: str) -> None:
        self._clauses.append(clause)

    def build(self) -> ScopePredicate:
        return ScopePredicate(
            dialect=self.dialect,
            clauses=tuple(self._clauses),
            named_params=dict(self._named),
            positional_params=tuple(self._positional),
        )


def _qualified(column: str, table_alias: str | None) -> str:
    return f"{table_alias}.{column}" if table_alias else column


def events_scope_predicate(
    scope: ResolvedScope,
    *,
    dialect: ScopeDialect,
    strict: bool = False,
    context_column: str = "context",
    include_project: bool = False,
) -> ScopePredicate:
    """Scope predicate over ``events.context`` tags.

    Lenient mode (default) admits rows whose scope tags are missing or blank
    - legacy events ingested before tagging. ``strict`` (settings
    ``scope_strict_tags``) requires exact tags. The project clause is only
    emitted when explicitly requested *and* the scope is bound; ordinary
    searches are never auto-narrowed by an inherited project.
    """
    builder = _PredicateBuilder(dialect)
    workspace_expr = builder.json_text(context_column, WORKSPACE_TAG)
    if strict:
        builder.add(f"{workspace_expr} = {builder.bind('scope_workspace', scope.workspace_id)}")
    else:
        placeholder = builder.bind("scope_workspace", scope.workspace_id)
        builder.add(f"({workspace_expr} IS NULL OR {workspace_expr} = '' OR {workspace_expr} = {placeholder})")

    owners = sorted({normalize_owner_token(owner) for owner in scope.owner_ids if normalize_owner_token(owner)})
    if owners:
        owner_expr = builder.json_text(context_column, OWNER_TAG)
        if strict:
            builder.add(f"{builder.trimmed_lower(owner_expr)} {builder.bind_membership('scope_owners', owners)}")
        else:
            membership = builder.bind_membership("scope_owners", owners)
            builder.add(f"({owner_expr} IS NULL OR {builder.trimmed(owner_expr)} = '' OR {builder.trimmed_lower(owner_expr)} {membership})")

    if include_project and scope.is_bound() and scope.project_id:
        project_expr = builder.json_text(context_column, PROJECT_TAG)
        builder.add(f"{project_expr} = {builder.bind('scope_project', scope.project_id)}")
    return builder.build()


def goals_scope_predicate(
    scope: ResolvedScope,
    *,
    dialect: ScopeDialect,
    session_id: str | None = None,
    table_alias: str | None = None,
) -> ScopePredicate:
    """Scope predicate over ``autonomy_goals`` (personal: never widened by continuity owners)."""
    builder = _PredicateBuilder(dialect)
    builder.add(f"{_qualified('workspace_id', table_alias)} = {builder.bind('scope_workspace', scope.workspace_id)}")
    builder.add(f"{_qualified('user_id', table_alias)} = {builder.bind('scope_owner', scope.owner_id)}")
    if session_id:
        builder.add(f"{_qualified('session_id', table_alias)} = {builder.bind('scope_session', session_id)}")
    return builder.build()


def handoffs_scope_predicate(
    scope: ResolvedScope,
    *,
    dialect: ScopeDialect,
    reader_session_id: str | None = None,
    now: datetime | str | None = None,
    table_alias: str | None = None,
) -> ScopePredicate:
    """Scope predicate over ``handoff_records`` for resume candidate retrieval.

    Default candidate scope is the authorised workspace/owner set (plus the
    retention window when ``now`` is given) - NOT equality with the reader's
    own session id. ``scope.source_session_id`` narrows to one source
    conversation; ``scope.legacy_session_scope`` restores the pre-P0
    reader-session equality for legacy clients.
    """
    builder = _PredicateBuilder(dialect)
    builder.add(f"{_qualified('workspace_id', table_alias)} = {builder.bind('scope_workspace', scope.workspace_id)}")
    owners = [owner for owner in scope.sql_owner_ids() if owner.strip()]
    if owners:
        builder.add(f"{_qualified('owner_id', table_alias)} {builder.bind_membership('scope_owners', owners)}")
    if now is not None:
        bound_now: Any = now.isoformat() if dialect is ScopeDialect.SQLITE and isinstance(now, datetime) else now
        builder.add(f"{_qualified('expires_at', table_alias)} > {builder.bind('scope_now', bound_now)}")
    if scope.legacy_session_scope:
        reader = reader_session_id or scope.task_id
        if not reader:
            raise ValueError("legacy_session_scope requires reader_session_id (or scope.task_id)")
        builder.add(f"{_qualified('session_id', table_alias)} = {builder.bind('scope_session', reader)}")
    elif scope.source_session_id:
        builder.add(f"{_qualified('session_id', table_alias)} = {builder.bind('scope_source_session', scope.source_session_id)}")
    if scope.project_binding == PROJECT_BOUND and scope.project_id:
        project_column = _qualified("project_id", table_alias)
        builder.add(f"({project_column} = {builder.bind('scope_project', scope.project_id)} OR {project_column} IS NULL)")
    return builder.build()


def observations_scope_predicate(
    scope: ResolvedScope,
    *,
    dialect: ScopeDialect,
    table_alias: str | None = None,
    include_project: bool = True,
    allow_null_project: bool = True,
) -> ScopePredicate:
    """Scope predicate over ``decision_observations`` — the corpus a decision is made from.

    A renderer is added to this module only when *both* backends will call it.  This one
    qualifies: ``policy_store.load_policy_evidence`` exists in Full and in Lite, so it has two
    callers and one definition of the rule.  A single-caller renderer would be indirection
    with nothing to keep in step.

    Why it exists at all.  Today the advisor's evidence query is scoped by
    ``(workspace_id, subject_user_id, situation_type)`` and nothing else, its ``consumer_id``
    parameter is accepted and never referenced in the SQL, and its semantic fallback arm drops
    the ``situation_type`` filter entirely — returning cross-situation rows in precisely the
    low-evidence cases where abstention matters most.  In the same handler the context bundle
    is project-scoped and the observation query is not.

    ``allow_null_project`` is a stated decision, not an implementation detail:
    ``decision_observations`` had no ``project_id`` before P4, so every historical row keeps
    ``NULL`` forever and a bare ``project_id = :project`` would silently become ``AND false``
    for the entire existing corpus.  NULL-project rows are therefore admissible — they are the
    same subject's decisions — but the policy does not count them toward
    ``Adequacy.above_floor_count``, so they can inform a ranking and can never, alone, make a
    family adequate.
    """

    builder = _PredicateBuilder(dialect)
    builder.add(f"{_qualified('workspace_id', table_alias)} = {builder.bind('scope_workspace', scope.workspace_id)}")
    builder.add(
        f"{_qualified('subject_user_id', table_alias)} = {builder.bind('scope_subject', scope.subject_user_id)}"
    )
    if include_project and scope.is_bound() and scope.project_id:
        project_column = _qualified("project_id", table_alias)
        placeholder = builder.bind("scope_project", scope.project_id)
        if allow_null_project:
            builder.add(f"({project_column} = {placeholder} OR {project_column} IS NULL)")
        else:
            builder.add(f"{project_column} = {placeholder}")
    return builder.build()
