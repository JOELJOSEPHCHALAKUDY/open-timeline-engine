from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CORE_TOOLS = {
    "tce.check_context",
    "tce.complete_task",
    "tce.get_context_brief",
    "tce.get_context_bundle",
    "tce.get_continuity_pilot",
    "tce.get_governance_status",
    "tce.get_resume_packet",
    "tce.record_behavior_evidence",
    "tce.report_resume_feedback",
    "tce.search_events",
}

CONTINUITY_TOOLS = CORE_TOOLS | {
    "tce.annotate_event",
    "tce.deprecate_memory_rule",
    "tce.forget_memory",
    "tce.get_activity_summary",
    "tce.get_episode",
    "tce.get_episodes",
    "tce.get_memory_rules",
    "tce.get_patterns",
    "tce.record_event",
    "tce.upsert_memory_rule",
}

AUTONOMY_TOOLS = CONTINUITY_TOOLS | {
    "tce.ack_takeover_notice",
    "tce.arbitrate_with_clone",
    "tce.claim_execution",
    "tce.consume_capability_grant",
    "tce.get_autonomy_status",
    "tce.get_clone_advice",
    "tce.get_execution_status",
    "tce.get_mode",
    "tce.get_takeover_goal_cache_status",
    "tce.get_takeover_goals",
    "tce.get_takeover_notices",
    "tce.get_takeover_state",
    "tce.invalidate_takeover_goal_cache",
    "tce.report_execution",
    "tce.request_capability_grant",
    "tce.request_execution_permit",
    "tce.reset_takeover_state",
    "tce.resolve_execution_permit",
    "tce.select_takeover_goal",
    "tce.set_mode",
    "tce.takeover_autonomy_tick",
    "tce.takeover_discover_goals",
    "tce.takeover_feedback",
    "tce.takeover_precompute_goals",
    "tce.takeover_preload",
    "tce.takeover_step",
}

RESEARCH_TOOLS = CONTINUITY_TOOLS | {
    "tce.answer_behavior_calibration",
    "tce.assign_behavior_projection_pilot",
    "tce.get_behavior_calibration",
    "tce.get_behavior_counterfactuals",
    "tce.get_behavior_fidelity",
    "tce.get_behavior_memory_reviews",
    "tce.get_behavior_processes",
    "tce.get_behavior_projection_pilot_status",
    "tce.get_behavior_shadow_status",
    "tce.get_event_graph",
    "tce.get_retrieval_eval_status",
    "tce.get_team_memberships",
    "tce.ingest_observations",
    "tce.mine_behavior_processes",
    "tce.predict_behavior",
    "tce.record_behavior_counterfactual",
    "tce.report_behavior_projection_pilot_outcome",
    "tce.resolve_behavior_counterfactual",
    "tce.resolve_behavior_memory_review",
    "tce.run_behavior_fidelity_eval",
    "tce.run_retrieval_eval",
    "tce.search_entities",
}

PROFILE_TOOLS = {
    "core": CORE_TOOLS,
    "continuity": CONTINUITY_TOOLS,
    "autonomy": AUTONOMY_TOOLS,
    "research": RESEARCH_TOOLS,
}
VALID_PROFILES = {*PROFILE_TOOLS, "admin", "all"}


def normalize_tool_profile(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    return normalized if normalized in VALID_PROFILES else "core"


def exposed_tool_names(profile: str, available: Iterable[str]) -> set[str]:
    available_names = {str(name) for name in available}
    effective = normalize_tool_profile(profile)
    if effective in {"admin", "all"}:
        return available_names
    return available_names & PROFILE_TOOLS[effective]


def apply_tool_profile(mcp: Any, profile: str) -> dict[str, Any]:
    tools = list(mcp._tool_manager.list_tools())
    available = {str(tool.name) for tool in tools}
    effective = normalize_tool_profile(profile)
    exposed = exposed_tool_names(effective, available)
    for name in sorted(available - exposed):
        mcp.remove_tool(name)
    return {
        "requested_profile": str(profile or ""),
        "effective_profile": effective,
        "available_count": len(available),
        "exposed_count": len(exposed),
        "exposed_tools": sorted(exposed),
        "removed_tools": sorted(available - exposed),
    }
