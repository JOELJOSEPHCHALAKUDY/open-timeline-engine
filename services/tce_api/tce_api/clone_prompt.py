"""The advisor's prompt.  It asks a model to weigh evidence, not to be a person.

What this file replaces said, in order: *"You are {user_name}. You are not an AI assistant —
you ARE this person"*; *"make the exact decision this person would make"*; *"Do not hedge, do
not offer alternatives"*; *"what would they actually do (not what's 'optimal')"*; *"same words,
same level of detail, same style"*; and four `Do NOT` clauses ending with *"Do NOT be more
cautious than this person would be"*.  Every one of those is gone, and none of them was ever
scored against an outcome.

The advisor's role now is an evidence contributor.  It is shown the same observations the
deterministic layer scored, **with their ids**, plus the explicit options and the constraints.
Its only powers are to agree, to disagree, to abstain, and to name ids that conflict.  It
cannot select an option the deterministic layer did not select and it cannot raise a score —
that is enforced in ``decision_policy._advisor_stage``, not here, and this prompt is written so
the model is not asked for anything the policy will not read.

The persona layer (``persona_ack``, the style hint on a rendered directive) is kept as
presentation and is absent from here: it can shape how a turn reads and it no longer touches
what is decided.

**``str.replace``, never ``str.format``.**  The template contains literal JSON braces and a
``.format()`` on it raises at runtime.  The repo already has a recorded instance of exactly
that bug, which is why the placeholders are ``__UPPER__`` markers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

ADVISOR_PROMPT_TEMPLATE = (
    """You are advising __USER_NAME__ on one choice. You are not __USER_NAME__, and you are not making the call."""
    """ Your recommendation is one input among several; a separate deterministic policy weighs it.

These are the only options. You may not invent another one, and you may not answer with anything that is not on this list.

__OPTIONS__

Constraints that apply to this choice. A recommendation that breaks one of these is wrong even when it looks better:
__CONSTRAINTS__

The situation, classified as __SITUATION_TYPE__:
__SITUATION__

Past decisions by this person that retrieval judged similar. Each carries an id. If you rely on one, name its id. An id you were not given here is not evidence and naming it will be rejected:
__OBSERVATIONS__

How to answer:
- Choose the option this evidence actually supports, and list the ids that support it.
- If the evidence does not support any option, say so. Abstaining is a correct answer and it is better than a guess. Set "recommended_option" to null and write what you would need to be told.
- If two pieces of evidence point at different options, name the ids that disagree. Do not average them into a middle answer.
- Do not recommend an option you cannot cite an id for, unless you are abstaining.
- Do not restate the situation. Do not write a plan. One recommendation, or none.

Reply with JSON only:
{"recommended_option": "exactly one of the options above, or null",
 "abstained": true or false,
 "abstain_reason": "insufficient_evidence" or "conflicting_evidence" or "out_of_scope" or null,
 "evidence_ids": ["ids from above that support the recommendation"],
 "conflicting_evidence_ids": ["ids from above that point somewhere else"],
 "advisor_note": "when abstaining, what a person would have to tell you; otherwise null"}
"""
)

ADVISOR_PROMPT_SHA = hashlib.sha256(ADVISOR_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
"""The first prompt hash this repo has ever had, and a bound key on every qualification.

Before this, ``gateway.extract_structured(prompt, schema_name)`` concatenated ``schema_name``
as a decorative label and versioned nothing, so a prompt edit could not invalidate anything.
"""


def _format_observations(observations: Sequence[Mapping[str, Any]]) -> str:
    """Render the observations **with their ids**, which is what makes a citation checkable.

    The old renderer emitted numbered prose with no identifier, so a model could not cite
    evidence even in principle and the ``citations`` that came back were timeline event ids
    from a different, unlinked list.
    """

    if not observations:
        return "  (none — retrieval found no similar past decision)"
    lines: list[str] = []
    for obs in observations[:12]:
        observation_id = str(obs.get("id") or "").strip()
        if not observation_id:
            continue
        situation = str(obs.get("situation_summary") or "unknown").strip()
        chose = str(obs.get("selected_choice") or obs.get("user_response") or "unknown").strip()
        outcome = str(obs.get("outcome") or "").strip() or "not recorded"
        recorded = str(obs.get("ts") or "").strip()[:10] or "unknown date"
        source = str(obs.get("evidence_source") or "unknown").strip()
        lines.append(f"  [{observation_id}] situation: {situation}")
        lines.append(f"      chose: {chose}")
        lines.append(f"      outcome: {outcome}")
        lines.append(f"      recorded: {recorded} · source: {source}")
    if not lines:
        return "  (none — retrieval found no similar past decision)"
    return "\n".join(lines)


def _format_options(candidate_options: Sequence[str]) -> str:
    options = [str(item).strip() for item in candidate_options if str(item).strip()]
    if not options:
        return "  (no options were offered — you must abstain)"
    return "\n".join(f"  - {item}" for item in options)


def _format_constraints(constraints: Mapping[str, Any]) -> str:
    items = [(str(key), value) for key, value in dict(constraints or {}).items()]
    if not items:
        return "  (none stated)"
    lines: list[str] = []
    for key, value in items[:20]:
        rendered = str(value)
        if len(rendered) > 300:
            rendered = rendered[:300] + "…"
        lines.append(f"  - {key}: {rendered}")
    return "\n".join(lines)


def observation_ids(observations: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Exactly the ids this prompt rendered — the set a cited id is intersected against."""

    out: list[str] = []
    for obs in observations[:12]:
        value = str(obs.get("id") or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def build_advisor_prompt(
    *,
    user_name: str,
    candidate_options: Sequence[str],
    constraints: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    situation_summary: str,
    situation_type: str,
) -> str:
    return (
        ADVISOR_PROMPT_TEMPLATE.replace("__USER_NAME__", str(user_name or "the user"))
        .replace("__OPTIONS__", _format_options(candidate_options))
        .replace("__CONSTRAINTS__", _format_constraints(constraints))
        .replace("__SITUATION_TYPE__", str(situation_type or "unknown"))
        .replace("__SITUATION__", str(situation_summary or "").strip() or "(not stated)")
        .replace("__OBSERVATIONS__", _format_observations(observations))
    )
