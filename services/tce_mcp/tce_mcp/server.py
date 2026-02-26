from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import tools
from .otel import setup_otel

mcp = FastMCP("tce")


@mcp.tool(name="tce.search_events", description="Search timeline events with filters and citations")
def search_events(query: str, filters: dict | None = None, k: int = 10, time_range: dict | None = None) -> dict:
    return tools.search_events(query=query, filters=filters, k=k, time_range=time_range)


@mcp.tool(name="tce.get_context_bundle", description="Get redaction-safe context bundle for a task")
def get_context_bundle(task: str, app_context: dict | None = None, constraints: dict | None = None) -> dict:
    return tools.get_context_bundle(task=task, app_context=app_context, constraints=constraints)


@mcp.tool(name="tce.get_resume_packet", description="Get deterministic handoff resume packet for cross-executor continuation")
def get_resume_packet(
    query: str,
    target_owner: str | None = None,
    session_id: str = "default",
    k: int = 5,
    include_cross_user: bool = True,
) -> dict:
    return tools.get_resume_packet(
        query=query,
        target_owner=target_owner,
        session_id=session_id,
        k=k,
        include_cross_user=include_cross_user,
    )


@mcp.tool(name="tce.get_context_brief", description="Get deterministic context brief with citations")
def get_context_brief(
    task: str,
    session_id: str = "default",
    app_context: dict | None = None,
    constraints: dict | None = None,
    max_items: int = 15,
) -> dict:
    return tools.get_context_brief(
        task=task,
        session_id=session_id,
        app_context=app_context,
        constraints=constraints,
        max_items=max_items,
    )


@mcp.tool(name="tce.annotate_event", description="Quick-tag an event/episode with goal, decision, avoid rules, and authority")
def annotate_event(
    event_id: str,
    session_id: str = "default",
    goal: str | None = None,
    decision: str | None = None,
    alternatives: list[str] | None = None,
    constraints: list[str] | None = None,
    avoid: list[str] | None = None,
    authority_level: str | None = None,
) -> dict:
    return tools.annotate_event(
        event_id=event_id,
        session_id=session_id,
        goal=goal,
        decision=decision,
        alternatives=alternatives,
        constraints=constraints,
        avoid=avoid,
        authority_level=authority_level,
    )


@mcp.tool(name="tce.get_episodes", description="List episodes for a session")
def get_episodes(session_id: str = "default", status: str | None = None, limit: int = 100) -> dict:
    return tools.get_episodes(session_id=session_id, status=status, limit=limit)


@mcp.tool(name="tce.get_episode", description="Get a single episode by id")
def get_episode(episode_id: str) -> dict:
    return tools.get_episode(episode_id=episode_id)


@mcp.tool(name="tce.get_memory_rules", description="List memory rules in current workspace scope")
def get_memory_rules(include_inactive: bool = False) -> dict:
    return tools.get_memory_rules(include_inactive=include_inactive)


@mcp.tool(name="tce.upsert_memory_rule", description="Create a memory rule for boundaries/preferences")
def upsert_memory_rule(
    scope: dict | None = None,
    rule_type: str = "prefer",
    statement: str = "",
    priority: int = 2,
    source_episode_id: str | None = None,
) -> dict:
    return tools.upsert_memory_rule(
        scope=scope,
        rule_type=rule_type,
        statement=statement,
        priority=priority,
        source_episode_id=source_episode_id,
    )


@mcp.tool(name="tce.deprecate_memory_rule", description="Deprecate a memory rule")
def deprecate_memory_rule(rule_id: str) -> dict:
    return tools.deprecate_memory_rule(rule_id=rule_id)


@mcp.tool(name="tce.forget_memory", description="Physically delete memory targets and record a tombstone")
def forget_memory(
    target_type: str,
    target_ids: list[str],
    reason: str = "user_requested",
    hard_delete: bool = True,
) -> dict:
    return tools.forget_memory(
        target_type=target_type,
        target_ids=target_ids,
        reason=reason,
        hard_delete=hard_delete,
    )


@mcp.tool(name="tce.get_retrieval_eval_status", description="Get retrieval evaluation run history")
def get_retrieval_eval_status(session_id: str = "default") -> dict:
    return tools.retrieval_eval_status(session_id=session_id)


@mcp.tool(name="tce.run_retrieval_eval", description="Run retrieval evaluation for style/constraints/traceability")
def run_retrieval_eval(session_id: str = "default", tasks: list[str] | None = None, with_brief: bool = True) -> dict:
    return tools.retrieval_eval_run(session_id=session_id, tasks=tasks, with_brief=with_brief)


@mcp.tool(name="tce.get_patterns", description="Get extracted patterns by domain")
def get_patterns(domain: str | None = None, min_confidence: float = 0.5) -> dict:
    return tools.get_patterns(domain=domain, min_confidence=min_confidence)


@mcp.tool(name="tce.record_event", description="Record an event in the timeline engine")
def record_event(event: dict) -> dict:
    return tools.record_event(event)


@mcp.tool(name="tce.get_mode", description="Get runtime mode: timeline_only or clone_advisor")
def get_mode() -> dict:
    return tools.get_mode()


@mcp.tool(name="tce.set_mode", description="Set runtime mode: timeline_only or clone_advisor")
def set_mode(mode: str) -> dict:
    return tools.set_mode(mode)


@mcp.tool(name="tce.get_clone_advice", description="Get advisor guidance from timeline patterns for executor AI")
def get_clone_advice(
    task: str,
    app_context: dict | None = None,
    constraints: dict | None = None,
    takeover_context: dict | None = None,
    message_delta: dict | None = None,
    executor_output: str | None = None,
    interaction_id: str | None = None,
    allow_fallback: bool = True,
) -> dict:
    return tools.get_clone_advice(
        task=task,
        app_context=app_context,
        constraints=constraints,
        takeover_context=takeover_context,
        message_delta=message_delta,
        executor_output=executor_output,
        interaction_id=interaction_id,
        allow_fallback=allow_fallback,
    )


@mcp.tool(name="tce.arbitrate_with_clone", description="Resolve conflicts between executor plan and advisor input")
def arbitrate_with_clone(
    interaction_id: str,
    executor_plan: str,
    advisor_input: str,
    human_override: str | None = None,
) -> dict:
    return tools.arbitrate_with_clone(
        interaction_id=interaction_id,
        executor_plan=executor_plan,
        advisor_input=advisor_input,
        human_override=human_override,
    )


@mcp.tool(name="tce.ingest_observations", description="Ingest decision observations to update clone fingerprint")
def ingest_observations(
    observations: list[dict],
    update_fingerprint: bool = True,
) -> dict:
    return tools.ingest_observations(
        observations=observations,
        update_fingerprint=update_fingerprint,
    )


@mcp.tool(name="tce.search_entities", description="Search extracted graph entities")
def search_entities(query: str, k: int = 20) -> dict:
    return tools.search_entities(query=query, k=k)


@mcp.tool(name="tce.get_event_graph", description="Get graph relationships for a specific event")
def get_event_graph(event_id: str) -> dict:
    return tools.get_event_graph(event_id=event_id)


@mcp.tool(name="tce.get_team_memberships", description="List current workspace team memberships")
def get_team_memberships() -> dict:
    return tools.get_team_memberships()


@mcp.tool(name="tce.get_activity_summary", description="Summarize what happened today or this week")
def get_activity_summary(period: str = "today", domain: str | None = None, max_events: int = 400) -> dict:
    return tools.get_activity_summary(period=period, domain=domain, max_events=max_events)


@mcp.tool(
    name="tce.check_context",
    description=(
        "REQUIRED: Call this BEFORE editing any file. Returns 'allow', 'warn', or 'block' "
        "based on past decisions in the timeline. If signal is 'block', you MUST NOT edit "
        "the file — report the issue to the user instead. If signal is 'warn', review the "
        "past_decisions before proceeding."
    ),
)
def check_context(file_path: str, intended_action: str = "edit") -> dict:
    return tools.check_context(file_path=file_path, intended_action=intended_action)


@mcp.tool(name="tce.get_lifecycle_status", description="Get event lifecycle/retention status")
def get_lifecycle_status() -> dict:
    return tools.get_lifecycle_status()


@mcp.tool(name="tce.run_lifecycle", description="Run lifecycle cleanup/archive pass (supports dry_run)")
def run_lifecycle(retention_days: int | None = None, dry_run: bool | None = None) -> dict:
    return tools.run_lifecycle(retention_days=retention_days, dry_run=dry_run)


@mcp.tool(name="tce.takeover_step", description="Run one takeover engine step with enforcement and safety checks. IMPORTANT: When the result contains has_directive=true, you MUST immediately use tools (read files, edit files, run commands) to work on state.takeover_context.objective. Do NOT narrate or describe the result — take action. IMPORTANT: Before editing ANY file, call tce.check_context first. If result contains persona_ack, show ONLY that text to the user before taking action.")
def takeover_step(
    message: str,
    session_id: str = "default",
    persona_mode: str = "shadow",
    task: str | None = None,
    app_context: dict | None = None,
    constraints: dict | None = None,
    takeover_context: dict | None = None,
    message_delta: dict | None = None,
    interaction_id: str | None = None,
    executor_output: str | None = None,
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
    activation_mode_default: str = "takeover",
    allow_fallback: bool = True,
) -> dict:
    return tools.takeover_step(
        message=message,
        session_id=session_id,
        persona_mode=persona_mode,
        task=task,
        app_context=app_context,
        constraints=constraints,
        takeover_context=takeover_context,
        message_delta=message_delta,
        interaction_id=interaction_id,
        executor_output=executor_output,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
        activation_mode_default=activation_mode_default,
        allow_fallback=allow_fallback,
    )


@mcp.tool(name="tce.takeover_preload", description="Preload takeover working set for a session/objective")
def takeover_preload(
    session_id: str = "default",
    persona_mode: str = "shadow",
    task: str = "current objective",
    app_context: dict | None = None,
    constraints: dict | None = None,
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
) -> dict:
    return tools.takeover_preload(
        session_id=session_id,
        persona_mode=persona_mode,
        task=task,
        app_context=app_context,
        constraints=constraints,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


@mcp.tool(name="tce.takeover_feedback", description="Record takeover execution outcome for autonomy learning")
def takeover_feedback(
    session_id: str = "default",
    turn: int = 0,
    objective_hash: str | None = None,
    action_kind: str = "execute",
    result: str = "success",
    latency_ms: int = 0,
    details: dict | None = None,
    clone_feedback_type: str | None = None,
    correction_text: str | None = None,
    observation_ids: list[str] | None = None,
    situation_type: str | None = None,
) -> dict:
    return tools.takeover_feedback(
        session_id=session_id,
        turn=turn,
        objective_hash=objective_hash,
        action_kind=action_kind,
        result=result,
        latency_ms=latency_ms,
        details=details,
        clone_feedback_type=clone_feedback_type,
        correction_text=correction_text,
        observation_ids=observation_ids,
        situation_type=situation_type,
    )


@mcp.tool(name="tce.takeover_discover_goals", description="Discover and rank takeover goals for a session")
def takeover_discover_goals(
    session_id: str = "default",
    include_open_discovery: bool = True,
) -> dict:
    return tools.takeover_discover_goals(
        session_id=session_id,
        include_open_discovery=include_open_discovery,
    )


@mcp.tool(name="tce.takeover_precompute_goals", description="Precompute and warm takeover goal cache for a session")
def takeover_precompute_goals(
    session_id: str = "default",
    include_open_discovery: bool = True,
    force_recompute: bool = False,
) -> dict:
    return tools.takeover_precompute_goals(
        session_id=session_id,
        include_open_discovery=include_open_discovery,
        force_recompute=force_recompute,
    )


@mcp.tool(name="tce.get_takeover_goals", description="List takeover goal queue for a session")
def get_takeover_goals(
    session_id: str = "default",
    status: str | None = None,
) -> dict:
    return tools.get_takeover_goals(session_id=session_id, status=status)


@mcp.tool(name="tce.get_takeover_goal_cache_status", description="Get takeover goal cache freshness and hit status")
def get_takeover_goal_cache_status(session_id: str = "default") -> dict:
    return tools.get_takeover_goal_cache_status(session_id=session_id)


@mcp.tool(name="tce.invalidate_takeover_goal_cache", description="Invalidate takeover goal cache for a session")
def invalidate_takeover_goal_cache(
    session_id: str = "default",
    objective_hash: str | None = None,
) -> dict:
    return tools.invalidate_takeover_goal_cache(session_id=session_id, objective_hash=objective_hash)


@mcp.tool(name="tce.select_takeover_goal", description="Select active goal for takeover session")
def select_takeover_goal(
    goal_id: str,
    session_id: str = "default",
) -> dict:
    return tools.select_takeover_goal(goal_id=goal_id, session_id=session_id)


@mcp.tool(name="tce.request_execution_permit", description="Request execution permit for mutating actions")
def request_execution_permit(
    session_id: str = "default",
    action_kind: str = "edit",
    target_paths: list[str] | None = None,
    command_preview: str | None = None,
    estimated_change_size: int = 0,
) -> dict:
    return tools.request_execution_permit(
        session_id=session_id,
        action_kind=action_kind,
        target_paths=target_paths,
        command_preview=command_preview,
        estimated_change_size=estimated_change_size,
    )


@mcp.tool(name="tce.resolve_execution_permit", description="Resolve pending execution permit decision")
def resolve_execution_permit(
    permit_id: str,
    approved: bool,
    confirmed_by: str | None = None,
) -> dict:
    return tools.resolve_execution_permit(
        permit_id=permit_id,
        approved=approved,
        confirmed_by=confirmed_by,
    )


@mcp.tool(name="tce.get_autonomy_status", description="Get autonomy status for takeover session")
def get_autonomy_status(session_id: str = "default") -> dict:
    return tools.get_autonomy_status(session_id=session_id)


@mcp.tool(name="tce.takeover_autonomy_tick", description="Run proactive autonomy tick for goal surfacing and cache warm")
def takeover_autonomy_tick(
    session_id: str | None = "default",
    include_open_discovery: bool = True,
    max_sessions: int = 20,
) -> dict:
    return tools.takeover_autonomy_tick(
        session_id=session_id,
        include_open_discovery=include_open_discovery,
        max_sessions=max_sessions,
    )


@mcp.tool(name="tce.get_takeover_notices", description="List open proactive autonomy notices for a session")
def get_takeover_notices(session_id: str = "default") -> dict:
    return tools.get_takeover_notices(session_id=session_id)


@mcp.tool(name="tce.ack_takeover_notice", description="Acknowledge a proactive autonomy notice")
def ack_takeover_notice(notice_id: str, session_id: str = "default", select_goal: bool = False) -> dict:
    return tools.ack_takeover_notice(notice_id=notice_id, session_id=session_id, select_goal=select_goal)


@mcp.tool(name="tce.claim_execution", description="Claim a pending directive execution before mutating work")
def claim_execution(
    session_id: str = "default",
    directive_id: str | None = None,
    claimed_by: str | None = None,
) -> dict:
    return tools.claim_execution(
        session_id=session_id,
        directive_id=directive_id,
        claimed_by=claimed_by,
    )


@mcp.tool(name="tce.report_execution", description="Report directive execution outcome for retry/self-correction")
def report_execution(
    session_id: str = "default",
    directive_id: str = "",
    state: str = "succeeded",
    result: str = "success",
    failure_reason: str | None = None,
    details: dict | None = None,
    rollback_performed: bool = False,
) -> dict:
    return tools.report_execution(
        session_id=session_id,
        directive_id=directive_id,
        state=state,
        result=result,
        failure_reason=failure_reason,
        details=details,
        rollback_performed=rollback_performed,
    )


@mcp.tool(name="tce.get_execution_status", description="Get directive execution queue/status for a session")
def get_execution_status(session_id: str = "default") -> dict:
    return tools.get_execution_status(session_id=session_id)


@mcp.tool(name="tce.get_takeover_state", description="Get takeover session state")
def get_takeover_state(
    session_id: str = "default",
    persona_mode: str = "shadow",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
) -> dict:
    return tools.get_takeover_state(
        session_id=session_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


@mcp.tool(name="tce.reset_takeover_state", description="Reset takeover state for a session")
def reset_takeover_state(
    session_id: str = "default",
    persona_mode: str = "shadow",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
) -> dict:
    return tools.reset_takeover_state(
        session_id=session_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


def main() -> None:
    setup_otel("tce-mcp")
    transport = str(os.getenv("TCE_MCP_TRANSPORT", "stdio") or "stdio").strip().lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        transport = "stdio"
    mount_path = str(os.getenv("TCE_MCP_MOUNT_PATH", "") or "").strip() or None
    if mount_path is not None:
        mcp.run(transport=transport, mount_path=mount_path)
        return
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
