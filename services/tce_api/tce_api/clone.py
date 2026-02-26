from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_model_gateway.gateway import ModelGateway

from .clone_prompt import build_clone_prompt
from .config import get_settings
from .models import AgentInteraction
from .schemas import CloneArbitrationResponse, ContextBundleResponse

logger = logging.getLogger(__name__)


def normalize_interaction_id(interaction_id: str | None) -> str:
    return interaction_id.strip() if interaction_id and interaction_id.strip() else str(uuid4())


def evaluate_loop_guard(
    db: Session,
    interaction_id: str,
    *,
    max_turns: int | None = None,
) -> tuple[bool, str, int]:
    settings = get_settings()
    window_start = datetime.now(tz=UTC) - timedelta(hours=1)
    row = db.execute(
        text(
            """
            SELECT COUNT(*) FROM agent_interactions
            WHERE interaction_id = :iid AND ts >= :window_start
            """
        ),
        {"iid": interaction_id, "window_start": window_start},
    ).scalar_one()
    turn_count = int(row)
    effective_max_turns = max(1, int(max_turns if max_turns is not None else settings.clone_max_turns_per_interaction))

    if turn_count >= effective_max_turns:
        return False, "max_turns_exceeded", turn_count
    return True, "ok", turn_count


def derive_clone_guidance(bundle: ContextBundleResponse, executor_output: str | None) -> tuple[str, list[str], float, str, list[str]]:
    pattern_confidences = [pattern.confidence for pattern in bundle.top_patterns]
    confidence = sum(pattern_confidences) / len(pattern_confidences) if pattern_confidences else 0.0

    citation_count = len(bundle.citations)
    evidence_strength = "moderate"
    if citation_count >= 5 and confidence >= 0.65:
        evidence_strength = "strong"
    elif citation_count < 2 or confidence < 0.45:
        evidence_strength = "weak"

    do_rules = bundle.do_dont.get("do", [])
    dont_rules = bundle.do_dont.get("dont", [])
    recommended_actions = do_rules[:5]
    if not recommended_actions and bundle.top_patterns:
        recommended_actions = [pattern.statement for pattern in bundle.top_patterns[:3]]

    conflict_flags: list[str] = []
    if do_rules and dont_rules:
        overlap = set(do_rules).intersection(set(dont_rules))
        if overlap:
            conflict_flags.append("do_dont_overlap")

    if executor_output and "override" in executor_output.lower() and recommended_actions:
        conflict_flags.append("executor_override_detected")

    if evidence_strength == "weak":
        summary = "No strong prior found; proceeding with conservative defaults on critical choices."
    else:
        summary = bundle.summary

    return summary, recommended_actions, confidence, evidence_strength, conflict_flags


def advisor_reason(
    gateway: ModelGateway,
    user_name: str,
    fingerprint: dict,
    similar_observations: list[dict],
    session_context: dict,
    current_situation: str,
    situation_type: str,
    extra_context: str = "",
) -> dict:
    prompt = build_clone_prompt(
        user_name=user_name,
        fingerprint=fingerprint,
        similar_observations=similar_observations,
        session_context=session_context,
        current_situation=current_situation,
        situation_type=situation_type,
        extra_context=extra_context,
    )
    try:
        async_extract = getattr(gateway, "aextract_structured", None)
        if callable(async_extract):
            maybe_result = async_extract(prompt, "clone_decision")
            if asyncio.iscoroutine(maybe_result):
                result = asyncio.run(maybe_result)
            else:
                result = maybe_result
        else:
            result = gateway.extract_structured(prompt, "clone_decision")
        if isinstance(result, dict) and "decision" in result:
            return result
        # Handle case where LLM returns {"raw": "..."} wrapper
        raw = result.get("raw", "")
        if isinstance(raw, str) and raw.strip():
            return {
                "reasoning": "",
                "decision": raw.strip(),
                "confidence": 0.5,
                "communication_style": "moderate",
            }
        return _fallback_response(current_situation, situation_type)
    except Exception:
        logger.warning("advisor_reason LLM call failed, using fallback", exc_info=True)
        return _fallback_response(current_situation, situation_type)


def _fallback_response(current_situation: str, situation_type: str) -> dict:
    return {
        "reasoning": "LLM unavailable, using conservative defaults",
        "decision": f"Proceed with safest approach for: {current_situation[:200]}",
        "confidence": 0.3,
        "communication_style": "moderate",
        "fallback": True,
    }


def _write_agent_interaction_sync(
    interaction_id: str,
    source_consumer: str,
    source_role: str,
    target_role: str,
    action: str,
    citations: list,
    payload: dict,
    allowed: bool,
    reason: str,
) -> None:
    """Write agent interaction in a background thread with its own DB session."""
    from .db import get_session_factory

    db: Session | None = None
    try:
        db = get_session_factory()()
        row = AgentInteraction(
            ts=datetime.now(tz=UTC),
            interaction_id=interaction_id,
            source_consumer=source_consumer,
            source_role=source_role,
            target_role=target_role,
            action=action,
            citations=citations,
            payload=payload,
            allowed=allowed,
            reason=reason,
        )
        db.add(row)
        db.commit()
    except Exception:
        logger.warning("agent interaction write failed", exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def write_agent_interaction(
    db: Session,
    interaction_id: str,
    source_consumer: str,
    source_role: str,
    target_role: str,
    action: str,
    citations: list[UUID],
    payload: dict,
    allowed: bool,
    reason: str,
) -> None:
    """Fire-and-forget agent interaction write in a background thread."""
    import threading

    t = threading.Thread(
        target=_write_agent_interaction_sync,
        args=(interaction_id, source_consumer, source_role, target_role, action,
              citations, payload, allowed, reason),
        daemon=True,
    )
    t.start()


def arbitrate(executor_plan: str, advisor_input: str, human_override: str | None, interaction_id: str) -> CloneArbitrationResponse:
    if human_override and human_override.strip():
        return CloneArbitrationResponse(
            interaction_id=interaction_id,
            final_guidance=human_override.strip(),
            decision_source="human_override",
            citations=[],
            conflict_resolved=True,
        )

    if advisor_input.strip() and not executor_plan.strip():
        return CloneArbitrationResponse(
            interaction_id=interaction_id,
            final_guidance=advisor_input.strip(),
            decision_source="advisor",
            citations=[],
            conflict_resolved=True,
        )

    if executor_plan.strip() and advisor_input.strip():
        final = (
            "Executor plan accepted with advisor constraints. "
            f"Executor: {executor_plan.strip()} | Advisor constraints: {advisor_input.strip()}"
        )
        return CloneArbitrationResponse(
            interaction_id=interaction_id,
            final_guidance=final,
            decision_source="merged",
            citations=[],
            conflict_resolved=True,
        )

    return CloneArbitrationResponse(
        interaction_id=interaction_id,
        final_guidance=executor_plan.strip() or "No guidance available",
        decision_source="executor",
        citations=[],
        conflict_resolved=False,
    )
