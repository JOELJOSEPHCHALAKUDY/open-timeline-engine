"""A cold-corpus takeover turn must ask its owner -- on Full and on Lite, with the same number.

``tests/unit/test_context_quality_not_self_reported.py`` pins the formula.  This pins the
consequence: driven through the real ``POST /v1/takeover/step``, a session whose workspace holds
no decision evidence scores below ``context_retrieval_escalate_score``, fires bounded retrieval,
and comes back with ``needs_human`` set and ``decision_source`` on the safety gate.

Both backends are driven with identical inputs and their scores are compared to each other,
because the divergence this closes was exactly a Full/Lite fork: Lite floored the recency term
when there was no eligible evidence and Full did not, so Full read a corpus containing nothing as
*perfectly fresh* and scored it 0.16 higher on every cold turn -- enough to carry it over the
escalation line while Lite stayed under it.  Asserting each backend separately would have let
that fork stand; asserting they agree is what catches it.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

# ``Settings.context_retrieval_escalate_score`` / ``context_retrieval_trigger_score``, the two
# lines the score is read against.  Both backends ship these defaults.
ESCALATE_SCORE = 0.52
TRIGGER_SCORE = 0.68

_TOKEN = "cold-corpus-token"
# Two turns: an activation, then a concrete mutating objective.  Neither has any prior decision
# in the corpus to lean on, which is the condition under test.
_TURNS = (
    {"message": "beru take over", "task": "ship the stripe webhook fix"},
    {
        "message": "the stripe webhook is failing, do i force push the fix to prod",
        "task": "ship the stripe webhook fix",
    },
)


def _headers(workspace: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-TCE-Consumer": "cold-corpus",
        "X-TCE-Role": "user",
        "X-TCE-Workspace": workspace,
        "X-TCE-User": "cold-corpus-user",
        "X-TCE-Behavior-Subject": "cold-corpus-user",
    }


def _drive(client: TestClient, workspace: str, session_id: str) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    for index, turn in enumerate(_TURNS):
        response = client.post(
            "/v1/takeover/step",
            json={
                "message": turn["message"],
                "session_id": session_id,
                "persona_mode": "shadow",
                "task": turn["task"],
                "app_context": {"domain": "coding"},
                "constraints": {"k": 4},
                "interaction_id": f"{session_id}-{index}",
                "allow_fallback": True,
            },
            headers=_headers(workspace),
        )
        assert response.status_code == 200, response.text[:400]
        bodies.append(response.json())
    return bodies


@pytest.fixture()
def lite_turns() -> Iterator[list[dict[str, Any]]]:
    from tce_lite_api.config import get_settings
    from tce_lite_api.main import app

    settings = get_settings()
    keys = ("lite_db_path", "api_tokens", "workspace_access_mode", "identity_claims_mode")
    original = {key: getattr(settings, key) for key in keys}
    with tempfile.TemporaryDirectory() as tmp:
        settings.lite_db_path = str(Path(tmp) / "cold-corpus.db")
        settings.api_tokens = _TOKEN
        settings.workspace_access_mode = "compat"
        settings.identity_claims_mode = "compat"
        try:
            with TestClient(app) as client:
                yield _drive(client, "cold-corpus-workspace", f"cold-lite-{uuid.uuid4().hex[:8]}")
        finally:
            for key, value in original.items():
                setattr(settings, key, value)


@pytest.fixture()
def full_turns() -> Iterator[list[dict[str, Any]]]:
    if not os.environ.get("TCE_DATABASE_URL"):
        pytest.skip("Full-backend integration tests need TCE_DATABASE_URL")

    from tce_api.config import get_settings
    from tce_api.main import app

    settings = get_settings()
    keys = ("api_tokens", "workspace_access_mode", "identity_claims_mode")
    original = {key: getattr(settings, key) for key in keys}
    settings.api_tokens = _TOKEN
    settings.workspace_access_mode = "compat"
    settings.identity_claims_mode = "compat"
    # A fresh workspace per run: "cold" has to mean cold, and a workspace this suite has already
    # written decisions into is not.
    suffix = uuid.uuid4().hex[:8]
    try:
        with TestClient(app) as client:
            yield _drive(client, f"cold-corpus-full-{suffix}", f"cold-full-{suffix}")
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


def _assert_the_turn_asks_its_owner(backend: str, turns: list[dict[str, Any]]) -> None:
    first = turns[0]
    score = float(first["context_quality_score"])

    assert score < TRIGGER_SCORE, (
        f"{backend}: a cold corpus scored {score}, at or above the {TRIGGER_SCORE} retrieval "
        "trigger, so the turn would not even look for more context."
    )
    assert score <= ESCALATE_SCORE - 0.15, (
        f"{backend}: a cold-corpus turn scored {score}, within 0.15 of the {ESCALATE_SCORE} "
        "escalation line. The score has drifted back up and the owner stops being asked."
    )
    assert bool(first["retrieval_triggered"]) is True
    assert first["retrieval_reason"] == "low_context_quality"

    for index, body in enumerate(turns):
        assert bool(body["needs_human"]) is True, (
            f"{backend} turn {index}: a turn with no evidence behind it continued without asking."
        )
        assert body["decision_source"] == "safety_gate", (
            f"{backend} turn {index}: decision_source is {body['decision_source']!r}, not the "
            "safety gate."
        )
        assert str(body.get("final_response") or body.get("response") or "").strip(), (
            f"{backend} turn {index}: escalated with nothing to show the owner."
        )


def test_a_cold_corpus_turn_asks_the_owner_on_lite(lite_turns: list[dict[str, Any]]) -> None:
    _assert_the_turn_asks_its_owner("lite", lite_turns)


def test_a_cold_corpus_turn_asks_the_owner_on_full(full_turns: list[dict[str, Any]]) -> None:
    _assert_the_turn_asks_its_owner("full", full_turns)


def test_both_backends_score_the_same_empty_corpus_the_same_way(
    lite_turns: list[dict[str, Any]],
    full_turns: list[dict[str, Any]],
) -> None:
    """The parity arm -- and the one that fails if Full's recency floor is removed again."""

    lite_scores = [float(body["context_quality_score"]) for body in lite_turns]
    full_scores = [float(body["context_quality_score"]) for body in full_turns]
    assert lite_scores == full_scores, (
        "Full and Lite disagree on the quality of the same empty corpus. The last time this "
        f"happened it was Full reading a median evidence age of zero as perfect freshness: "
        f"lite={lite_scores} full={full_scores}"
    )
