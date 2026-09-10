"""B2 — the advisor contributes evidence, and a failed call is an abstention.

What this file replaces asserted, in ``test_advisor_reason_fallback_on_error``, that a dead LLM
returns ``result.get("fallback") is True`` alongside a ``decision`` string. The fabricated
outage decision — *"Proceed with safest approach for: <first 200 characters of the
situation>"*, confidence ``0.3`` — was therefore a *tested guarantee*, in the same response
slot and with the same ``decision_source`` as a decision that had evidence behind it. Two of
its three call sites checked the marker; one did not.

The replacement asserts the opposite property: a model that cannot answer contributes nothing,
and nothing is what the policy reads.
"""

from __future__ import annotations

from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock

from tce_api.clone import advisor_recommend
from tce_api.clone_prompt import ADVISOR_PROMPT_SHA
from tce_shared.decision_policy import AdvisorContribution

_OBSERVATIONS = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "situation_summary": "flaky CI test on the payments worker",
        "selected_choice": "investigate root cause",
        "outcome": "found a race in the retry loop",
        "ts": "2026-08-01T10:00:00+00:00",
        "evidence_source": "explicit",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "situation_summary": "CI red on main after a dependency bump",
        "selected_choice": "investigate root cause",
        "outcome": "pinned the dependency",
        "ts": "2026-08-05T10:00:00+00:00",
        "evidence_source": "correction",
    },
]

_OPTIONS = ["investigate root cause", "retry the job"]


def _mock_gateway(response: object) -> MagicMock:
    gw = MagicMock()
    gw.aextract_structured = AsyncMock(return_value=response)
    gw.extract_structured.return_value = response
    return gw


def _recommend(gateway: MagicMock) -> AdvisorContribution:
    return advisor_recommend(
        gateway,
        model_id="qwen3:8b",
        runtime_version="ollama",
        user_name="Joel",
        candidate_options=_OPTIONS,
        constraints={"deadline": "today"},
        observations=_OBSERVATIONS,
        situation_summary="Test suite failing on CI",
        situation_type="error_occurred",
    )


def test_advisor_recommend_returns_contribution() -> None:
    gw = _mock_gateway(
        {
            "recommended_option": "investigate root cause",
            "abstained": False,
            "abstain_reason": None,
            "evidence_ids": ["11111111-1111-4111-8111-111111111111"],
            "conflicting_evidence_ids": [],
            "advisor_note": None,
        }
    )
    contribution = _recommend(gw)

    assert contribution.recommended_option == "investigate root cause"
    assert contribution.abstained is False
    assert contribution.parse_state == "parsed"
    assert contribution.evidence_ids == ("11111111-1111-4111-8111-111111111111",)
    assert contribution.prompt_sha256 == ADVISOR_PROMPT_SHA
    assert contribution.model_id == "qwen3:8b"
    gw.aextract_structured.assert_called_once()

    # No field of the result is named for a confidence, at any nesting. The type is the
    # enforcement: a self-report has nowhere to sit, so it cannot reach a score one hop later.
    assert not [field.name for field in fields(contribution) if "confidence" in field.name]


def test_advisor_recommend_abstains_on_error() -> None:
    """The case whose old assertion guaranteed a fabricated decision."""

    gw = MagicMock()
    gw.aextract_structured = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    gw.extract_structured.side_effect = RuntimeError("LLM unavailable")
    contribution = _recommend(gw)

    assert contribution.parse_state == "call_failed"
    assert contribution.abstained is True
    assert contribution.recommended_option is None
    assert contribution.evidence_ids == ()


def test_advisor_recommend_abstains_on_unparseable_output() -> None:
    """Prose in a ``raw`` wrapper used to become the decision itself, at a hard-coded 0.5."""

    contribution = _recommend(_mock_gateway({"raw": "I think you should probably just retry it"}))

    assert contribution.parse_state == "unparseable"
    assert contribution.abstained is True
    assert contribution.recommended_option is None


def test_advisor_recommend_refuses_an_option_nobody_offered() -> None:
    """A model may not widen its own option set."""

    contribution = _recommend(
        _mock_gateway(
            {
                "recommended_option": "rewrite the scheduler in rust",
                "abstained": False,
                "evidence_ids": [],
                "conflicting_evidence_ids": [],
            }
        )
    )

    assert contribution.abstained is True
    assert contribution.abstain_reason == "out_of_scope"
    assert contribution.recommended_option is None


def test_advisor_recommend_drops_invented_ids() -> None:
    """An id the model was not shown is not evidence, and dropping it is not silent."""

    contribution = _recommend(
        _mock_gateway(
            {
                "recommended_option": "investigate root cause",
                "abstained": False,
                "evidence_ids": [
                    "11111111-1111-4111-8111-111111111111",
                    "99999999-9999-4999-8999-999999999999",
                ],
                "conflicting_evidence_ids": ["deadbeef"],
            }
        )
    )

    assert contribution.evidence_ids == ("11111111-1111-4111-8111-111111111111",)
    assert contribution.conflicting_evidence_ids == ()


def test_advisor_recommend_has_no_rationale_field() -> None:
    """``rationale`` was prompted for and read by nothing.

    A field a model fills that nothing consumes is a field that eventually finds a reader, and
    the only plausible reader here is the escalation text the human sees — the one surface the
    whole design exists to keep model-free.
    """

    names = {field.name for field in fields(AdvisorContribution)}
    assert "rationale" not in names
    assert "advisor_note" in names
