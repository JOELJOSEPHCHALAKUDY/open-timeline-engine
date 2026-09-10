"""The decision policy against the Lite HTTP boundary (P4 design §10.5, Builder C).

Every case drives the real FastAPI app over ``TestClient`` with the real SQLite store.  SQL is
used only to *observe* state, never as the assertion path for behaviour the HTTP boundary must
enforce.

What these cases are for, stated rather than implied.  Before P4 the routes that *decided* and
the harness that *evaluated* decisions were disjoint sets: every deployed decision site went
through an LLM prompt or a kNN primitive that nothing had ever scored, and the one function
with the required contract had no deployed caller.  These cases assert the Lite half of the
repair at the boundary a client actually sees:

* ``/v1/behavior/predict`` answers from ``decide()`` and publishes a ``policy_decision`` block.
* A takeover turn carries the same block, and on a corpus with no qualified family it is
  ``exposed=false`` -- the decision is computed and reported truthfully, and simply not used.
* The turn itself is unchanged by that, which is the Y1 property in miniature.
* An empty candidate set abstains rather than returning an option nobody offered.
* Lite has no server-side advisor, so ``advisor_agreement`` is ``absent`` and says so.
* The freeze site stores what a later qualification run needs: a request fingerprint, a
  candidate count, an episode key, and a contamination flag computed from the rendered text.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.db import _connect
from tce_lite_api.main import app
from tce_lite_api.policy_store import RETRIEVAL_VERSION
from tce_shared.decision_policy import DECISION_POLICY_REVISION

_TOKEN = "decision-policy-lite-token"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "behavior_prediction_enabled",
    "behavior_shadow_evaluation_enabled",
    "behavior_autonomy_gate_enabled",
    "policy_allow_unscoped_project_evidence",
    "policy_advisor_required_families",
    "scope_strict_tags",
)

_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_HIGH_RISK_TASK = "delete the build artifacts with rm -rf /tmp/build"


def _snapshot_settings() -> dict[str, Any]:
    settings = get_settings()
    return {key: getattr(settings, key, _MISSING) for key in _GUARDED_SETTINGS}


def _restore_settings(snapshot: dict[str, Any]) -> None:
    settings = get_settings()
    for key, value in snapshot.items():
        if value is _MISSING:
            if hasattr(settings, key):
                try:
                    delattr(settings, key)
                except AttributeError:
                    pass
            continue
        setattr(settings, key, value)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "decision-policy-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = _TOKEN
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    # This file measures the decision policy, not the charter gate; the charter path has its
    # own file and exercises both switch positions there.
    settings.charter_enforcement_enabled = False
    settings.behavior_prediction_enabled = True
    settings.behavior_shadow_evaluation_enabled = True
    # FN4: the guard stays and its default is False. Pinned explicitly so a change to the
    # default shows up here as a failure rather than as a silent behaviour change.
    settings.behavior_autonomy_gate_enabled = False
    settings.policy_allow_unscoped_project_evidence = True
    settings.policy_advisor_required_families = ""
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _headers(role: str = "executor", consumer: str = "codex-executor") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-TCE-Role": role,
        "X-TCE-Consumer": consumer,
    }


def _predict(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "situation_type": "choice_required",
        "situation_summary": "the stripe webhook is failing, do i force push the fix to prod",
        "objective": "get the payment webhook working again",
        "constraints": {},
        "context_snapshot": {},
        "candidate_choices": ["force push to prod", "pause and verify"],
        "min_confidence": 0.55,
    }
    payload.update(overrides)
    response = client.post("/v1/behavior/predict", json=payload, headers=_headers())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _step(client: TestClient, session_id: str, *, message: str, task: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "persona_mode": "shadow",
        "app_context": dict(_APP_CONTEXT),
        "constraints": {"k": 4},
        "allow_fallback": True,
    }
    if task is not None:
        payload["task"] = task
    response = client.post("/v1/takeover/step", json=payload, headers=_headers())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _open_safety_question(client: TestClient, session_id: str) -> dict[str, Any]:
    """Drive the turn to the safety gate, which is the one Lite freeze site that offers two
    options and can therefore ever be a qualifying case.

    The risky text is in the *message*, not only in the task: ``evaluate_safety`` reads
    ``message`` and the rendered ``final_response`` and never reads ``task``, so putting it in
    the task alone makes the freeze depend on whether some other layer happens to echo the task
    back into the response.  That is a real and separate finding, reported to the orchestrator;
    this fixture does not rely on it.
    """

    body = _step(client, session_id, message=f"beru take over and {_HIGH_RISK_TASK}", task=_HIGH_RISK_TASK)
    assert body["safety_decision"] == "confirm_required", body
    assert body.get("open_decision_opportunity_id"), (
        f"the safety question must be frozen as a decision opportunity: {body}"
    )
    return body


def test_predict_route_answers_from_the_one_policy_and_publishes_its_block(lite_client: TestClient) -> None:
    """``/v1/behavior/predict`` is a *deployed* decision site, so it must be an *evaluated* one.

    The assertion that matters is not the predicted choice -- on an empty corpus there is none --
    but that the response carries the policy's own identity.  A route that answered some other
    way would have no block to publish.
    """

    body = _predict(lite_client)
    block = body["policy_decision"]
    assert block is not None, body
    assert block["decision_policy_revision"] == DECISION_POLICY_REVISION
    # An empty corpus has nothing to go on, and saying so is the correct answer.
    assert block["status"] == "abstained"
    assert block["abstain_reason"] == "no_eligible_evidence"
    assert body["abstained"] is True
    assert body["predicted_choice"] is None
    assert body["needs_clarification"] is True
    assert body["clarification_question"], "an abstention must say what it would need to be told"


def test_an_empty_candidate_set_abstains_instead_of_inventing_an_option(lite_client: TestClient) -> None:
    """The primitive underneath returns the raw historical label when the allowed list is empty,
    so without an explicit guard the route answers with an option nobody offered.  Live, 32 of 34
    stored observations carry fewer than two options, so this is the ordinary case and not an
    edge one."""

    body = _predict(lite_client, candidate_choices=[])
    block = body["policy_decision"]
    assert block["status"] == "abstained"
    assert block["abstain_reason"] == "no_candidate_match"
    assert block["selected_option"] is None
    assert body["predicted_choice"] is None


def test_lite_reports_an_absent_advisor_rather_than_pretending_to_have_one(lite_client: TestClient) -> None:
    """Lite has no server-side model gateway and P4 does not add one.  ``absent`` is a legal
    contribution: the deterministic decision stands, and the wire says why it stood alone.  What
    must never appear is a fabricated recommendation dressed as one."""

    block = _predict(lite_client)["policy_decision"]
    assert block["advisor_agreement"] == "absent"
    assert "confidence" not in block, "no self-reported number belongs on this block"
    assert "policy_score" not in block, "an executor cannot interpret an uncalibrated number"
    assert "calibrated_score" not in block, "nothing in this system is calibrated"


def test_a_takeover_turn_carries_the_block_and_is_not_exposed(lite_client: TestClient) -> None:
    """The Y1 property, at the boundary.

    Zero families hold a qualification record, so the policy is computed, reported truthfully,
    and not used.  ``exposed`` false with ``exposure_state`` naming *why* is what keeps "we
    abstained" and "we were not allowed to speak" from collapsing into one wire value -- the
    conflation that turned an unqualified family into a human escalation in an earlier draft.
    """

    session_id = f"policy-{uuid.uuid4().hex[:8]}"
    body = _step(lite_client, session_id, message="beru take over", task="tidy the changelog")
    block = body["policy_decision"]
    assert block is not None, body
    assert block["exposed"] is False
    assert block["exposure_state"] == "no_qualification"
    assert block["status"] == "abstained"
    # An unexposed abstention must not be an escalation cause, and must not enforce a rewrite.
    assert body["enforcement_reason"] != "policy_abstention"


def test_the_freeze_site_stores_what_a_qualification_run_needs(lite_client: TestClient) -> None:
    """Point A: the safety question is frozen before the human answers it.

    Four of the columns asserted here did not exist or were constants before P4, and each
    absence made the promotion gate unsatisfiable rather than merely unmet:

    * ``request_fingerprint`` -- replay could not prove it rebuilt the same request.
    * ``candidate_option_count`` -- the gate must exclude cases offering fewer than two options
      and the option list is not retained on the row.
    * ``decision_advice_shown`` -- the column that used to carry this, ``advice_visible``, was
      written as ``bool(<eight-key dict literal>)`` at every site in both backends, so it was
      ``True`` unconditionally and the exclusion clause read a constant.
    * ``episode_key`` -- a train/holdout split may not straddle one.
    """

    session_id = f"policy-{uuid.uuid4().hex[:8]}"
    body = _open_safety_question(lite_client, session_id)
    opportunity_id = body["open_decision_opportunity_id"]

    conn: sqlite3.Connection = _connect()
    try:
        row = conn.execute(
            """
            SELECT p.request_fingerprint, p.candidate_option_count, p.decision_advice_shown,
                   p.episode_key, p.exposed, p.retrieval_version, p.decision_policy_revision,
                   p.policy_json, o.task_id, o.advice_exposure_json, o.episode_key AS opportunity_episode
            FROM decision_opportunities o
            JOIN behavior_shadow_predictions p ON p.id = o.shadow_prediction_id
            WHERE o.id = ?
            """,
            (str(opportunity_id),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the frozen opportunity must carry a shadow row"

    assert row["decision_policy_revision"] == DECISION_POLICY_REVISION
    # The loader's own identity, not a shared constant: a Full qualification must not be
    # carryable by a Lite decision.
    assert row["retrieval_version"] == RETRIEVAL_VERSION
    assert row["request_fingerprint"], "replay has nothing to compare against without this"
    assert int(row["candidate_option_count"]) == 2, "the safety question offers confirm and deny"
    assert row["episode_key"], "a split cannot be built without an episode"
    assert str(row["opportunity_episode"]) == str(row["episode_key"])
    assert int(row["exposed"]) == 0, "no family is qualified, so nothing is exposed"
    payload = json.loads(str(row["policy_json"]))
    assert payload["decision_policy_revision"] == DECISION_POLICY_REVISION
    assert payload["exposure_state"] == "no_qualification"

    exposure = json.loads(str(row["advice_exposure_json"]))
    # `prediction_shown` was the literal False at every site in both backends, which is one of
    # the reasons the deployed route and the evaluated route were disjoint. It is now the
    # policy's own exposure flag, and it becomes true the moment a qualification exists.
    assert exposure["prediction_shown"] is False
    # And `advice_visible` now means what its name says. Lite has no advisor, so it is False --
    # the first honest value this column has ever carried.
    assert exposure["advice_visible"] is False


def test_advice_shown_is_measured_from_the_text_the_human_reads(lite_client: TestClient) -> None:
    """The safety question names both options in its own text ("Type 'confirm' ... or 'abort'"),
    so the human's answer is contaminated by advice about this very choice and the case must be
    excluded from the promotion denominator.  This is exactly the property ``advice_visible``
    claimed to measure and never did."""

    session_id = f"policy-{uuid.uuid4().hex[:8]}"
    body = _open_safety_question(lite_client, session_id)
    opportunity_id = body["open_decision_opportunity_id"]
    conn: sqlite3.Connection = _connect()
    try:
        row = conn.execute(
            """
            SELECT p.decision_advice_shown, o.question_text, o.alternatives_json
            FROM decision_opportunities o
            JOIN behavior_shadow_predictions p ON p.id = o.shadow_prediction_id
            WHERE o.id = ?
            """,
            (str(opportunity_id),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    alternatives = json.loads(str(row["alternatives_json"]))
    question = str(row["question_text"])
    names_one = any(str(option).strip().casefold() in question.casefold() for option in alternatives)
    assert bool(int(row["decision_advice_shown"])) is names_one, (
        "decision_advice_shown must be a function of the rendered text and the offered options, "
        f"not a constant. question={question!r} alternatives={alternatives!r}"
    )


def test_the_fidelity_gate_reads_qualifications_and_refuses_by_default(lite_client: TestClient) -> None:
    """With no qualification record the gate answers exactly what it answered before P4: not
    passed, with a reason.  It used to be a global "the latest run passed" flag keyed on
    workspace and subject only, with no decision family and no bound keys, so one run that
    passed once granted autonomy to every family forever.  Absence of a record is the refusal;
    there is no setting that turns it into a yes."""

    gate = _predict(lite_client)["fidelity_gate"]
    assert gate["passed"] is False
    assert gate["reason"] == "no_qualification_recorded"
    assert gate["families"] == {}
