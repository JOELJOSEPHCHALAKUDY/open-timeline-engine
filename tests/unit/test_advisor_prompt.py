"""G3 — the prompt asks a model to weigh evidence, and no longer asks it to be a person.

The file this replaces (``tests/unit/test_clone_prompt.py``) asserted, at line 43,
``"ANTI-PATTERN" in prompt or "Do NOT" in prompt`` — a test that *fails* if the anti-hedging
block is removed. It could not be adapted, only replaced.

What was in the prompt, line by line, and is now absent:

* *"You are {user_name}. You are not an AI assistant — you ARE this person."*
* *"Your job is to make the exact decision this person would make in this situation."*
* *"Do not hedge, do not offer alternatives unless this person typically does."*
* *"What would they actually do (not what's 'optimal')?"* — the single line that made
  behavioural agreement and correctness the same objective.
* *"Respond as this person would — same words, same level of detail, same style."*
* four ``Do NOT`` clauses, ending with *"Do NOT be more cautious than this person would be."*

None of it was ever scored against an outcome. The scan is over the literals rather than over
the shape, because a paraphrase of the same instruction is the same instruction.
"""

from __future__ import annotations

import json

import pytest
from tce_api.clone_prompt import (
    ADVISOR_PROMPT_SHA,
    ADVISOR_PROMPT_TEMPLATE,
    _format_observations,
    build_advisor_prompt,
    observation_ids,
)

_OBSERVATIONS = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "situation_summary": "prod webhook failing after a deploy",
        "selected_choice": "roll back first",
        "outcome": "webhook recovered",
        "ts": "2026-07-14T09:30:00+00:00",
        "evidence_source": "explicit",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "situation_summary": "payment retries backing up",
        "user_response": "roll back first",
        "ts": "2026-07-20T09:30:00+00:00",
        "evidence_source": "correction",
    },
]

_OPTIONS = ["roll back first", "force push the fix"]


def _prompt(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "user_name": "Joel",
        "candidate_options": _OPTIONS,
        "constraints": {"deadline": "20 minutes"},
        "observations": _OBSERVATIONS,
        "situation_summary": "the stripe webhook is failing in production",
        "situation_type": "blocker_encountered",
    }
    kwargs.update(overrides)
    return build_advisor_prompt(**kwargs)  # type: ignore[arg-type]


IMPERSONATION_LITERALS = (
    "you ARE this person",
    "You are not an AI assistant",
    "the exact decision this person would make",
    "Do not hedge",
    "Do NOT hedge",
    "Do NOT ask clarifying questions",
    "Do NOT be more cautious",
    "same words, same level of detail",
    "not what's \"optimal\"",
)


@pytest.mark.parametrize("literal", IMPERSONATION_LITERALS)
def test_impersonation_instructions_are_absent(literal: str) -> None:
    assert literal.casefold() not in _prompt().casefold(), literal


def test_the_prompt_says_the_model_is_not_the_user() -> None:
    """Not merely the absence of the old line — the presence of its contradiction."""

    prompt = _prompt()
    assert "You are advising Joel" in prompt
    assert "You are not Joel" in prompt
    assert "you are not making the call" in prompt


def test_the_prompt_offers_abstention_as_a_correct_answer() -> None:
    """D4 in the prompt: a guess is worse than "I do not know", and the model is told so."""

    prompt = _prompt()
    assert "Abstaining is a correct answer" in prompt
    assert '"abstained"' in prompt
    assert '"abstain_reason"' in prompt


def test_the_prompt_renders_observation_ids_a_citation_can_be_checked_against() -> None:
    """The old renderer emitted numbered prose with no identifier.

    A model could not cite evidence even in principle, so the ``citations`` that came back were
    timeline event ids from a separate, unlinked list.
    """

    prompt = _prompt()
    for observation in _OBSERVATIONS:
        assert f"[{observation['id']}]" in prompt
    assert observation_ids(_OBSERVATIONS) == (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )
    assert "an id you were not given here is not evidence" in prompt.casefold()


def test_the_prompt_names_the_only_options() -> None:
    prompt = _prompt()
    for option in _OPTIONS:
        assert f"- {option}" in prompt
    assert "You may not invent another one" in prompt


def test_an_empty_option_set_is_stated_rather_than_hidden() -> None:
    assert "you must abstain" in _prompt(candidate_options=[])


def test_no_observations_renders_as_none_not_as_an_instruction_to_guess() -> None:
    """The old renderer said *"Use behavioral patterns and defaults"* — an instruction to
    answer anyway, in exactly the case where there is nothing to answer from."""

    rendered = _format_observations([])
    assert "none" in rendered.casefold()
    assert "default" not in rendered.casefold()


def test_the_template_is_filled_with_replace_and_would_raise_under_format() -> None:
    """The template carries literal JSON braces.

    ``str.format`` on it raises, and the repo already has a recorded instance of that bug in a
    sibling prompt, which is why the placeholders are ``__UPPER__`` markers.
    """

    assert "__USER_NAME__" in ADVISOR_PROMPT_TEMPLATE
    assert "__OBSERVATIONS__" in ADVISOR_PROMPT_TEMPLATE
    with pytest.raises((KeyError, IndexError, ValueError)):
        ADVISOR_PROMPT_TEMPLATE.format(user_name="Joel")


def test_the_reply_schema_is_valid_json_and_asks_for_nothing_unread() -> None:
    """Every key the model is asked to fill has a reader.

    ``rationale`` was prompted for by the old design and read by nothing.  A field a model
    fills that nothing consumes eventually finds a reader, and the only plausible reader is the
    escalation text a human sees.
    """

    start = ADVISOR_PROMPT_TEMPLATE.index('{"recommended_option"')
    end = ADVISOR_PROMPT_TEMPLATE.rindex("}") + 1
    schema = json.loads(
        ADVISOR_PROMPT_TEMPLATE[start:end]
        .replace("true or false", "false")
        .replace('"exactly one of the options above, or null"', "null")
        .replace(
            '"insufficient_evidence" or "conflicting_evidence" or "out_of_scope" or null',
            "null",
        )
        .replace('"when abstaining, what a person would have to tell you; otherwise null"', "null")
    )
    assert set(schema) == {
        "recommended_option",
        "abstained",
        "abstain_reason",
        "evidence_ids",
        "conflicting_evidence_ids",
        "advisor_note",
    }
    assert "rationale" not in schema
    assert "confidence" not in schema


def test_the_prompt_hash_is_stable_and_moves_with_the_prompt() -> None:
    """``ADVISOR_PROMPT_SHA`` is a bound key on every qualification record.

    It is the first prompt hash this repo has ever had: the gateway's
    ``extract_structured(prompt, schema_name)`` concatenated the schema name as a decorative
    label and versioned nothing, so a prompt edit could not invalidate a measurement.
    """

    import hashlib

    assert ADVISOR_PROMPT_SHA == hashlib.sha256(ADVISOR_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
    assert len(ADVISOR_PROMPT_SHA) == 64
    edited = hashlib.sha256((ADVISOR_PROMPT_TEMPLATE + " ").encode("utf-8")).hexdigest()
    assert edited != ADVISOR_PROMPT_SHA
