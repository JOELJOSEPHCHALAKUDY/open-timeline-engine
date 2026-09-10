from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_model_gateway.gateway import ModelGateway
from tce_shared.behavior_fidelity import _map_choice
from tce_shared.decision_policy import AdvisorContribution, validate_cited_ids

from .clone_prompt import ADVISOR_PROMPT_SHA, build_advisor_prompt, observation_ids
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


def derive_clone_guidance(bundle: ContextBundleResponse, executor_output: str | None) -> tuple[str, list[str], list[str]]:
    """Presentation only: ``(summary, recommended_actions, conflict_flags)``.

    Two things this used to return are gone, and both were the same defect in different
    clothes.  ``confidence`` was an average of pattern confidences that nothing had ever been
    fit to, and ``evidence_strength`` was derived from ``len(bundle.citations)`` — timeline
    event ids, which are retrieval provenance and explicitly not the evidence for a choice.
    Both now come from the decision policy: ``policy_score`` and
    ``decision_policy.evidence_strength_label(adequacy)``, which read properties of the
    evidence that was actually used and nothing a model said about itself.

    The ``evidence_strength == "weak"`` branch that replaced the summary with a fixed
    "no strong prior, proceeding with conservative defaults" sentence is deleted with them.
    That sentence was pattern-matched by ``ensure_takeover_response`` two modules away, where
    it triggered a *more* decisive rewrite — so the lowest-evidence turns
    produced the most confident text, and the resulting DECISIVE classification then raised the
    very ``decision_confidence`` that gates ``needs_human``.  Weak evidence is the policy's job
    now, and the policy abstains.  The summary is always ``bundle.summary``.
    """

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

    return bundle.summary, recommended_actions, conflict_flags


def bundle_provenance_strength(bundle: ContextBundleResponse) -> tuple[float, str]:
    """The context bundle's own confidence and strength label — provenance, not a decision.

    Both numbers used to live inside ``derive_clone_guidance`` and both used to be overwritten
    by the advisor's self-reported number one call later.  The overwrite is gone.  What is left
    is a property of the retrieved bundle, and nothing reads it to steer a turn any more: the
    ``evidence_strength`` read in ``ensure_takeover_response`` is deleted, and the label a
    *decision* carries comes from ``decision_policy.evidence_strength_label(adequacy)``, which
    reads the evidence that was actually voted on.

    The labels are the shared ``{strong, moderate, weak}`` vocabulary.  Lite used to emit
    ``{high, medium, low}`` for the same field, so any branch keyed on the label could never
    fire on Lite — a policy fork hiding inside a string.
    """

    pattern_confidences = [pattern.confidence for pattern in bundle.top_patterns]
    confidence = sum(pattern_confidences) / len(pattern_confidences) if pattern_confidences else 0.0
    citation_count = len(bundle.citations)
    if citation_count >= 5 and confidence >= 0.65:
        return confidence, "strong"
    if citation_count < 2 or confidence < 0.45:
        return confidence, "weak"
    return confidence, "moderate"


def _empty_contribution(
    *,
    parse_state: str,
    abstain_reason: str | None,
    model_id: str,
    runtime_version: str,
    note: str | None = None,
) -> AdvisorContribution:
    """Everything empty, ``abstained=True``.  There is no decision string in here.

    This is what a failed, timed-out or unparseable model call produces, and under the policy's
    advisor stage ``parse_state != "parsed"`` forces an abstention *unconditionally* — before
    ``policy_advisor_required_families`` is consulted, because that setting defaults to empty
    and checking it first would mean a dead model is ignored on every family.
    """

    return AdvisorContribution(
        recommended_option=None,
        abstained=True,
        abstain_reason=abstain_reason,
        evidence_ids=(),
        conflicting_evidence_ids=(),
        advisor_note=note,
        parse_state=parse_state,
        prompt_sha256=ADVISOR_PROMPT_SHA,
        model_id=model_id,
        runtime_version=runtime_version,
    )


def advisor_recommend(
    gateway: ModelGateway | Any,
    *,
    model_id: str,
    runtime_version: str,
    prompt: str | None = None,
    user_name: str = "",
    candidate_options: Sequence[str] = (),
    constraints: Mapping[str, Any] | None = None,
    observations: Sequence[Mapping[str, Any]] = (),
    situation_summary: str = "",
    situation_type: str = "",
) -> AdvisorContribution:
    """Ask the advisor to weigh the evidence, and take an abstention as an answer.

    What this replaces, ``advisor_reason``, could not fail: any exception, any unparseable
    reply and any missing ``decision`` key all funnelled into ``_fallback_response``, which
    invented a fixed "proceed with the safest approach for: <the first 200 characters of the
    situation>" string with a hard-coded confidence of ``0.3`` and a ``fallback: True`` marker
    that two call sites checked and one did not.  A model being down is not evidence about what the
    human would choose, and the honest output of a failed call is no output.

    Five rules, in order:

    1. Build the prompt and call the gateway exactly as before.
    2. Any exception -> ``parse_state="call_failed"``, everything else empty.
    3. A reply that is not a dict, or carries no ``recommended_option`` key, or answers with
       prose in a ``raw`` wrapper -> ``parse_state="unparseable"``.  The old path turned that
       prose into the decision itself.
    4. A recommendation that is not one of the offered options -> ``abstained``,
       ``abstain_reason="out_of_scope"``.  A model may not widen its own option set.
    5. Cited ids are intersected with the ids actually rendered into the prompt.  An id the
       model invented is dropped and counted.

    ``AdvisorContribution`` has no confidence field, so a self-report cannot reach a score
    through this type at all.  That is enforcement by construction rather than by review.
    """

    if prompt is None:
        prompt = build_advisor_prompt(
            user_name=user_name,
            candidate_options=candidate_options,
            constraints=constraints or {},
            observations=observations,
            situation_summary=situation_summary,
            situation_type=situation_type,
        )
    offered = observation_ids(observations)
    try:
        async_extract = getattr(gateway, "aextract_structured", None)
        if callable(async_extract):
            maybe_result = async_extract(prompt, "advisor_recommendation_v1")
            if asyncio.iscoroutine(maybe_result):
                result = asyncio.run(maybe_result)
            else:
                result = maybe_result
        else:
            result = gateway.extract_structured(prompt, "advisor_recommendation_v1")
    except Exception:
        logger.warning("advisor_recommend call failed; the advisor contributes an abstention", exc_info=True)
        return _empty_contribution(
            parse_state="call_failed",
            abstain_reason="advisor_unavailable",
            model_id=model_id,
            runtime_version=runtime_version,
        )

    if not isinstance(result, dict) or "recommended_option" not in result:
        return _empty_contribution(
            parse_state="unparseable",
            abstain_reason="unparseable_advisor_output",
            model_id=model_id,
            runtime_version=runtime_version,
        )

    note_raw = result.get("advisor_note")
    note = str(note_raw).strip()[:600] if isinstance(note_raw, str) and note_raw.strip() else None
    kept, _dropped = validate_cited_ids(_string_list(result.get("evidence_ids")), offered)
    conflicting, _conflict_dropped = validate_cited_ids(
        _string_list(result.get("conflicting_evidence_ids")), offered
    )
    abstain_reason_raw = result.get("abstain_reason")
    abstain_reason = (
        str(abstain_reason_raw).strip()[:80]
        if isinstance(abstain_reason_raw, str) and abstain_reason_raw.strip()
        else None
    )

    raw_option = result.get("recommended_option")
    option = str(raw_option).strip() if isinstance(raw_option, str) else ""
    abstained = bool(result.get("abstained")) or not option
    mapped = _map_choice(option, list(candidate_options)) if option else ""
    if option and not mapped:
        abstained = True
        abstain_reason = "out_of_scope"

    return AdvisorContribution(
        recommended_option=(mapped or None) if not abstained else None,
        abstained=abstained,
        abstain_reason=abstain_reason,
        evidence_ids=kept,
        conflicting_evidence_ids=conflicting,
        advisor_note=note,
        parse_state="parsed",
        prompt_sha256=ADVISOR_PROMPT_SHA,
        model_id=model_id,
        runtime_version=runtime_version,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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


def arbitrate(
    executor_plan: str,
    advisor_input: str,
    human_override: str | None,
    interaction_id: str,
    *,
    evidence_observation_ids: Sequence[str] = (),
) -> CloneArbitrationResponse:
    """Precedence is unchanged: human override, then advisor, then merge, then executor.

    What changes is that the advisor branch can now say what it stood on.  Every branch
    hard-coded ``citations=[]``, so a guidance string attributed to the advisor arrived with no
    way to check it.  ``CloneArbitrationResponse.decision_source`` is a different field from
    ``TakeoverDecisionSource`` and keeps its existing values.
    """

    advisor_citations: list[UUID] = []
    for value in list(evidence_observation_ids)[:12]:
        try:
            advisor_citations.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
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
            citations=advisor_citations,
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
