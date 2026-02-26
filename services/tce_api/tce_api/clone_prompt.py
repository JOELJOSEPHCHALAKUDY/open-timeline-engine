from __future__ import annotations

from typing import Any


def _format_fingerprint(fingerprint: dict[str, Any]) -> str:
    dm = fingerprint.get("decision_making", {})
    comm = fingerprint.get("communication", {})
    pri = fingerprint.get("priorities", {})
    ls = fingerprint.get("learning_style", {})
    lines = [
        f"- Decision style: risk_tolerance={dm.get('risk_tolerance', 'moderate')}, "
        f"speed_vs_thoroughness={dm.get('speed_vs_thoroughness', 0.5):.1f}",
        f"- Communication: verbosity={comm.get('verbosity', 'moderate')}, "
        f"formality={comm.get('formality', 'neutral')}, "
        f"typical response length: {comm.get('preferred_response_length', 'medium')}",
        f"- Priorities: speed_vs_quality={pri.get('speed_vs_quality', 0.5):.1f}, "
        f"top concerns: {', '.join(pri.get('top_recurring_concerns', [])) or 'none observed yet'}",
        f"- Under pressure: {comm.get('tone_under_pressure', 'calm')}",
        f"- When uncertain: exploration_tendency={ls.get('exploration_vs_exploitation', 0.5):.1f}",
    ]
    return "\n".join(lines)


def _format_observations(observations: list[dict[str, Any]]) -> str:
    if not observations:
        return "No similar past situations found. Use behavioral patterns and defaults."
    lines = []
    for i, obs in enumerate(observations[:5], 1):
        situation = obs.get("situation_summary", "unknown")
        response = obs.get("user_response", "unknown")
        outcome = obs.get("outcome", "unknown")
        lines.append(f"  {i}. Situation: {situation}")
        lines.append(f"     Decision: {response}")
        lines.append(f"     Outcome: {outcome}")
    return "\n".join(lines)


def _format_session(session_context: dict[str, Any]) -> str:
    objective = session_context.get("objective", "not set")
    turn_count = session_context.get("turn_count", 0)
    turns = session_context.get("turns", [])
    unresolved = session_context.get("unresolved_threads", [])
    lines = [
        f"- Objective: {objective}",
        f"- Turn {turn_count} of this session",
    ]
    if turns:
        lines.append("- Previous decisions this session:")
        for turn in turns[-3:]:
            lines.append(f"  - {turn.get('situation', '?')} -> {turn.get('decision', '?')}")
    if unresolved:
        lines.append(f"- Unresolved threads: {', '.join(str(t) for t in unresolved[:3])}")
    return "\n".join(lines)


def build_clone_prompt(
    user_name: str,
    fingerprint: dict[str, Any],
    similar_observations: list[dict[str, Any]],
    session_context: dict[str, Any],
    current_situation: str,
    situation_type: str,
    extra_context: str = "",
) -> str:
    layer_1 = (
        f"You are {user_name}. You are not an AI assistant — you ARE this person.\n"
        "Your job is to make the exact decision this person would make in this situation.\n"
        "Do not hedge, do not offer alternatives unless this person typically does."
    )

    layer_2 = (
        "Here are similar situations this person faced before, and what they decided:\n"
        + _format_observations(similar_observations)
    )

    layer_3 = (
        "This person's behavioral profile:\n"
        + _format_fingerprint(fingerprint)
    )

    layer_4 = (
        "Current session state:\n"
        + _format_session(session_context)
    )

    layer_5 = (
        f"Current situation (classified as {situation_type}):\n"
        f"{current_situation}"
    )
    if extra_context:
        layer_5 += f"\n\nAdditional context:\n{extra_context}"

    layer_6 = (
        "Think step by step:\n"
        "1. How would this person perceive this situation?\n"
        "2. What past experiences would they draw on?\n"
        "3. What would they prioritize?\n"
        "4. What would they actually do (not what's \"optimal\")?\n"
        "5. How would they communicate their decision?\n\n"
        "Respond as this person would — same words, same level of detail, same style.\n\n"
        "IMPORTANT: Return a JSON object with these fields:\n"
        '  {"reasoning": "your step-by-step thinking", '
        '"decision": "the actual response/directive", '
        '"confidence": 0.0-1.0, '
        '"communication_style": "terse|moderate|verbose"}\n\n'
        "Do NOT do these:\n"
        "- Do NOT list multiple options unless this person typically does\n"
        "- Do NOT ask clarifying questions unless this person would\n"
        "- Do NOT hedge with \"it depends\" unless that's their pattern\n"
        "- Do NOT be more cautious than this person would be"
    )

    sections = [
        "=== IDENTITY ===",
        layer_1,
        "",
        "=== HISTORICAL EVIDENCE ===",
        layer_2,
        "",
        "=== BEHAVIORAL PATTERNS ===",
        layer_3,
        "",
        "=== SESSION CONTEXT ===",
        layer_4,
        "",
        "=== CURRENT SITUATION ===",
        layer_5,
        "",
        "=== REASONING INSTRUCTION ===",
        layer_6,
    ]

    return "\n".join(sections)
