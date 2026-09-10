from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.autonomy_context import summarize_hit_text
from tce_shared.deadline import Deadline, RetrievalLedger
from tce_shared.events import EventFilter, EventSearchRequest, EventSearchResponse
from tce_shared.policy import ConsumerContext
from tce_shared.scope import PROJECT_BOUND, ResolvedScope

from .config import get_settings
from .db import get_session_factory
from .deadline_pg import absorb_pg_deadline, begin_pg_deadline, finish_pg_deadline
from .graph import graph_snapshot_for_events
from .policy import PolicyEngine
from .schemas import ContextBundleRequest, ContextBundleResponse, EvidenceEvent, PatternItem
from .search import run_search, scope_from_consumer

_BUNDLE_EXECUTOR = ThreadPoolExecutor(max_workers=3)


def visible_requested_domain(scope: ResolvedScope, domain: str | None) -> str | None:
    """Drop a body-supplied ``<other-workspace>:takeover`` domain.

    Learned patterns and workflow templates have no workspace column; per-workspace rows are
    keyed only by ``f"{workspace_id}:takeover"``, so honouring a foreign takeover domain from the
    request would read another workspace's learned rows. Only the caller's own takeover domain or
    a non-takeover domain survives.
    """
    if not domain:
        return None
    if domain.endswith(":takeover") and domain != f"{scope.workspace_id}:takeover":
        return None
    return domain


def _pattern_rows_in_scope(
    db: Session,
    pattern_rows: list[Any],
    scope: ResolvedScope,
    *,
    domain: str | None,
) -> list[Any]:
    """Keep only patterns whose evidence sits inside ``scope``.

    A pattern with no evidence events is visible only when its domain is the caller's own
    takeover domain or the domain explicitly requested; otherwise at least one evidence event
    must carry this workspace and one of the scope's owners.
    """
    if not pattern_rows:
        return []
    domain = visible_requested_domain(scope, domain)
    takeover_domain = f"{scope.workspace_id}:takeover"
    visible_unevidenced_domains = {takeover_domain}
    if domain:
        visible_unevidenced_domains.add(domain)
    all_evidence_ids: list[Any] = []
    for row in pattern_rows:
        if row["evidence_event_ids"]:
            all_evidence_ids.extend(row["evidence_event_ids"][:10])
    evidence_contexts: dict[Any, dict[str, Any]] = {}
    if all_evidence_ids:
        ev_rows = db.execute(
            text("SELECT id, context FROM events WHERE id = ANY(:ids)"),
            {"ids": list(set(all_evidence_ids))},
        ).fetchall()
        for ev in ev_rows:
            evidence_contexts[ev[0]] = ev[1] if isinstance(ev[1], dict) else {}
    owners = {str(owner).strip().lower() for owner in scope.owner_ids}
    kept: list[Any] = []
    for row in pattern_rows:
        evidence_ids = list(row["evidence_event_ids"] or [])
        if not evidence_ids:
            if str(row["domain"] or "") in visible_unevidenced_domains:
                kept.append(row)
            continue
        scoped = False
        for eid in evidence_ids[:10]:
            ctx = evidence_contexts.get(eid)
            if ctx is None:
                continue
            workspace = str(ctx.get("_tce_workspace") or "").strip()
            owner = str(ctx.get("_tce_owner") or "").strip().lower()
            if workspace == scope.workspace_id and owner in owners:
                scoped = True
                break
        if scoped:
            kept.append(row)
    return kept


def _fetch_patterns_scoped(
    db: Session,
    scope: ResolvedScope,
    *,
    domain: str | None,
    min_confidence: float,
) -> list[PatternItem]:
    """Fetch patterns with batch evidence scoping (eliminates N+1)."""
    domain = visible_requested_domain(scope, domain)
    # Raw SQL pattern query
    where_parts = [
        "confidence >= :min_conf",
        "status NOT IN ('deprecated', 'suppressed')",
    ]
    params: dict[str, Any] = {"min_conf": min_confidence}
    if domain:
        where_parts.append("domain = :domain")
        params["domain"] = domain

    pattern_rows = db.execute(
        text(f"""
            SELECT id, domain, pattern_type, statement, confidence, status, evidence_event_ids
            FROM patterns
            WHERE {' AND '.join(where_parts)}
            ORDER BY confidence DESC, updated_at DESC
            LIMIT 8
        """),
        params,
    ).mappings().all()

    top_patterns: list[PatternItem] = []
    for row in _pattern_rows_in_scope(db, list(pattern_rows), scope, domain=domain):
        top_patterns.append(
            PatternItem(
                id=row["id"],
                domain=row["domain"],
                pattern_type=row["pattern_type"],
                statement=row["statement"],
                confidence=row["confidence"],
                status=row["status"],
                evidence_event_ids=row["evidence_event_ids"],
            )
        )
    return top_patterns


def _fetch_workflows(db: Session, scope: ResolvedScope, domain: str | None) -> list[dict[str, Any]]:
    """Fetch workflow templates scoped to the caller's workspace takeover domain (plus a non-takeover requested domain)."""
    scope_domain = f"{scope.workspace_id}:takeover"
    domain = visible_requested_domain(scope, domain)
    rows = db.execute(
        text("""
            SELECT id, name, domain, graph, triggers, version
            FROM workflow_templates
            WHERE domain IN (:domain, :scope_domain)
            ORDER BY updated_at DESC
            LIMIT 5
        """),
        {"domain": domain or scope_domain, "scope_domain": scope_domain},
    ).mappings().all()
    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "domain": row["domain"],
            "graph": row["graph"],
            "triggers": row["triggers"],
            "version": row["version"],
        }
        for row in rows
    ]


def build_context_bundle(
    db: Session,
    request: ContextBundleRequest,
    consumer_ctx: ConsumerContext,
    policy_engine: PolicyEngine,
    *,
    deadline: Deadline | None = None,
    ledger: RetrievalLedger | None = None,
) -> tuple[ContextBundleResponse, int, list[str]]:
    """``deadline`` is the retrieval child, built by the caller immediately above this call."""
    settings = get_settings()
    scope = consumer_ctx.scope or scope_from_consumer(consumer_ctx)
    domain = request.app_context.get("domain") if isinstance(request.app_context, dict) else None
    domain = visible_requested_domain(scope, domain)
    min_confidence = float(request.constraints.get("min_confidence", 0.5)) if isinstance(request.constraints, dict) else 0.5

    search_request = EventSearchRequest(
        query=request.task,
        filters=EventFilter(
            domain=domain,
            task_type=request.app_context.get("task_type") if isinstance(request.app_context, dict) else None,
        ),
        k=int(request.constraints.get("k", 12)) if isinstance(request.constraints, dict) else 12,
        # Only an explicitly bound project narrows the bundle; an inherited one never auto-filters.
        project_id=scope.project_id if scope.project_binding == PROJECT_BOUND else None,
    )

    # Run patterns + workflows + graph in parallel (each uses own DB session or is independent)
    backend_ms = max(1, int(getattr(settings, "context_backend_timeout_ms", 60)))
    floor_ms = max(1, int(getattr(settings, "retrieval_statement_floor_ms", 10)))

    def _in_own_session[T](name: str, run: Any, empty: T) -> T:
        """Each pool task opens its own session, so it needs its own guard: a
        ``statement_timeout`` set on the request session does not reach another thread's."""
        s = get_session_factory()()
        try:
            guard = begin_pg_deadline(
                s, deadline, name=name, ledger=ledger, requested_ms=backend_ms, floor_ms=floor_ms
            )
            if guard is None:
                return empty
            try:
                result: T = run(s)
            except Exception as exc:  # noqa: BLE001 - absorb only a cancellation
                if not absorb_pg_deadline(guard, s, exc):
                    raise
                return empty
            finish_pg_deadline(guard, s)
            return result
        finally:
            s.rollback()
            s.close()

    def _patterns_task() -> list[PatternItem]:
        empty_patterns: list[PatternItem] = []
        return _in_own_session(
            "patterns",
            lambda s: _fetch_patterns_scoped(
                s, scope, domain=domain, min_confidence=min_confidence
            ),
            empty_patterns,
        )

    def _workflows_task() -> list[dict[str, Any]]:
        empty_workflows: list[dict[str, Any]] = []
        return _in_own_session(
            "workflows", lambda s: _fetch_workflows(s, scope, domain), empty_workflows
        )

    def _graph_task(citation_ids: list[Any]) -> dict[str, Any]:
        empty: dict[str, Any] = {"entities": [], "relationships": [], "facts": []}
        if not citation_ids:
            return empty
        return _in_own_session(
            "graph_snapshot",
            lambda s: graph_snapshot_for_events(
                db=s,
                workspace_id=consumer_ctx.workspace_id,
                owner_id=consumer_ctx.owner_id,
                event_ids=citation_ids,
            ),
            empty,
        )

    patterns_future = _BUNDLE_EXECUTOR.submit(_patterns_task)
    workflows_future = _BUNDLE_EXECUTOR.submit(_workflows_task)
    hits, citations, blocked, retrieval_meta = run_search(
        db, search_request, consumer_ctx, policy_engine, deadline=deadline, ledger=ledger
    )
    graph_future = _BUNDLE_EXECUTOR.submit(_graph_task, citations)

    def _join[T](future: Any, name: str, empty: T) -> T:
        """The 5 s joins become budget-derived. ``future.cancel()`` is a no-op on a running
        future, so the abandoned work is recorded rather than pretended away (residual R-2)."""
        timeout = 5.0 if deadline is None else max(0.01, deadline.remaining_seconds())
        try:
            return future.result(timeout=timeout)  # type: ignore[no-any-return]
        except TimeoutError:
            if ledger is not None:
                ledger.record_timeout(name, budget_ms=int(timeout * 1000))
            future.cancel()
            return empty

    no_patterns: list[PatternItem] = []
    no_workflows: list[dict[str, Any]] = []
    no_graph: dict[str, Any] = {"entities": [], "relationships": [], "facts": []}
    top_patterns = _join(patterns_future, "patterns_join", no_patterns)
    relevant_workflows = _join(workflows_future, "workflows_join", no_workflows)
    graph_snapshot = _join(graph_future, "graph_join", no_graph)

    evidence_events = [
        EvidenceEvent(
            id=hit.id,
            title=hit.title,
            ts=hit.ts,
            key_payload_fields={"domain": hit.domain, "task_type": hit.task_type},
            summary_l0=hit.summary_l0,
            summary_l1=hit.summary_l1,
        )
        for hit in hits
    ]

    typed_memory: dict[str, list[str]] = {
        "facts": [],
        "opinions": [],
        "experiences": [],
        "skills": [],
    }
    style_counts: defaultdict[str, int] = defaultdict(int)
    for pattern_item in top_patterns:
        statement = str(pattern_item.statement or "").strip()
        ptype = str(pattern_item.pattern_type or "").strip().lower()
        if not statement:
            continue
        if ptype in {"semantic_fact", "world_fact", "fact"}:
            typed_memory["facts"].append(statement)
        elif ptype in {"opinion", "preference", "style"}:
            typed_memory["opinions"].append(statement)
            style_counts[statement] += 1
        elif ptype in {"experience", "reflection", "summary"}:
            typed_memory["experiences"].append(statement)
        elif ptype in {"skill", "workflow"}:
            typed_memory["skills"].append(statement)

    do_rules = [statement for statement, _ in sorted(style_counts.items(), key=lambda item: item[1], reverse=True)][:5]
    if not do_rules and typed_memory["skills"]:
        do_rules = typed_memory["skills"][:5]
    if not do_rules:
        do_rules = [
            "Capture TASK_DECISION and TASK_DONE events to accelerate personalization.",
            "Use context_bundle as a citation-backed memory layer for current tasks.",
        ]

    dont_rules = [
        pattern.statement
        for pattern in top_patterns
        if str(pattern.pattern_type or "").strip().lower() in {"anti-pattern", "risk", "avoid"}
    ][:5]
    if not dont_rules:
        dont_rules = ["Do not treat low-evidence patterns as hard rules."]

    cold_start = (
        len(evidence_events) < settings.cold_start_min_events
        or len(top_patterns) < settings.cold_start_min_patterns
    )
    context_tiers_enabled = bool(getattr(settings, "context_tiers_enabled", False))

    summary = (
        f"Bundle generated at {datetime.now(tz=UTC).isoformat()} with "
        f"{len(evidence_events)} evidence events and {len(top_patterns)} patterns"
    )
    if context_tiers_enabled and evidence_events:
        summary = " | ".join(
            [
                f"{len(evidence_events)} evidence events",
                *[
                    summarize_hit_text(event.title, event.summary_l0, event.ts)
                    for event in evidence_events[:3]
                ],
            ]
        )[:500]
    if cold_start:
        # The wording is deliberately not a marker another module can pattern-match on.  The
        # string this replaces was read two modules away as a trigger for a *more* decisive
        # rewrite, so the lowest-evidence turns produced the most confident text.  A shortage
        # of evidence now travels as policy_decision.abstain_reason, which is a field, not a
        # sentence smuggled into a summary.
        summary = (
            "Limited historical signal so far: timeline log and search are fully available, and "
            "suggestions stay cautious until more of your decisions are captured."
        )
        do_rules = [
            "Use event capture and search immediately; personalization improves after more timeline data.",
            "Optionally seed starter patterns via `PYTHONPATH=shared:services/tce_api python scripts/seed_starter_patterns.py`.",
            *do_rules,
        ][:5]

    response = ContextBundleResponse(
        summary=summary,
        top_patterns=top_patterns,
        relevant_workflows=relevant_workflows,
        evidence_events=evidence_events,
        do_dont={"do": do_rules, "dont": dont_rules},
        citations=citations,
        policy={
            **policy_engine.summarize(blocked_count=blocked, applied_redactions=[]),
            "cold_start": cold_start,
            "evidence_count": len(evidence_events),
            "pattern_count": len(top_patterns),
            "typed_memory_counts": {key: len(value) for key, value in typed_memory.items()},
            "handoff_hits_count": int(retrieval_meta.get("handoff_hits_count", 0) or 0),
            "top_handoff_record_ids": list(retrieval_meta.get("top_handoff_record_ids") or []),
            "resume_packet_available": bool(retrieval_meta.get("resume_packet_available", False)),
            "cross_user_scope_applied": bool(retrieval_meta.get("cross_user_scope_applied", False)),
            "cross_user_scope_owners": list(retrieval_meta.get("cross_user_scope_owners") or []),
            "project_binding": scope.project_binding,
            "context_tier_used": str(retrieval_meta.get("context_tier_used") or "l2"),
            "summary_coverage": float(retrieval_meta.get("summary_coverage", 0.0) or 0.0),
            "planner_used": bool(retrieval_meta.get("planner_used", False)),
            "subquery_count": int(retrieval_meta.get("subquery_count", 0) or 0),
            "subquery_labels": list(retrieval_meta.get("subquery_labels") or []),
            "episode_boost_applied": bool(retrieval_meta.get("episode_boost_applied", False)),
            "activation_boost_applied": bool(retrieval_meta.get("activation_boost_applied", False)),
            "retrieval": retrieval_meta,
        },
        structured_context={
            "time_window": {
                "from": min((event.ts for event in evidence_events), default=None),
                "to": max((event.ts for event in evidence_events), default=None),
            },
            "typed_memory": typed_memory,
            "graph": graph_snapshot,
            "retrieval": retrieval_meta,
        },
        context_tier_used=str(retrieval_meta.get("context_tier_used") or "l2"),
        summary_coverage=float(retrieval_meta.get("summary_coverage", 0.0) or 0.0),
        planner_used=bool(retrieval_meta.get("planner_used", False)),
        subquery_count=int(retrieval_meta.get("subquery_count", 0) or 0),
        subquery_labels=list(retrieval_meta.get("subquery_labels") or []),
        episode_boost_applied=bool(retrieval_meta.get("episode_boost_applied", False)),
        activation_boost_applied=bool(retrieval_meta.get("activation_boost_applied", False)),
    )
    return response, blocked, []


def search_response(hits: list, citations: list) -> EventSearchResponse:
    return EventSearchResponse(hits=hits, citations=citations)
