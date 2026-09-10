"""G2 and G11 — the arm is frozen before it is revealed, and relevance is adjudicated, never inferred.

Both cases drive the real Lite FastAPI app over ``TestClient`` against a real SQLite file.  SQL is
used to *observe* state and to plant P5 fixture rows, never as the assertion path for behaviour the
HTTP boundary must enforce.

**G2 — the arm is frozen and unrepeatable.**  The defect this prevents was measured, not imagined:
p6_design Proof 4 edited a label four times and reached all four arms.  Any allocator whose input
a caller can vary is an allocator a caller can shop.  P6's answer is structural — the only input
the caller controls is the work itself, ``episode_key`` is P4's server-side digest, and a repeat
enrolment returns the ORIGINAL arm rather than a fresh draw.  The strongest evidence for that is
not the response body: it is ``pilot_strata.next_slot`` **not moving**.  A re-enrolment that
returned the same arm while consuming a slot would still be corrupting the block balance.

**G11 — a rejection is not a false positive.**  P5 already ships ``accepted`` / ``rejected`` and
the independent ``ignored`` nonresponse state, and P5 §1.2 already says ``ignored`` is never
counted in a rejection metric.  What P6 adds is the one thing P5 correctly did not: an adjudication
of *relevance*, independent of acceptance.  A rejection is a preference; ``not_relevant`` is a
claim about the proposal's fit; deriving the second from the first would let a report announce a
false-positive rate nobody ever judged.  With zero adjudications the answer is NOT_COMPUTABLE —
never ``0.0``, which would read as *no false positives*.

``blind_verified`` is checked against P5's own append-only ``dream_proposal_events`` log rather
than taken on the caller's word, because a client that scores its own blindness is the party under
test grading itself.

THE CONTRACT this file pins on Builders B and C (the wire shapes of §6.3, whose column names are
Builder A's migration ``20260909_0043``):

    POST /v1/pilot/episodes            {project_id, decision_family, session_id, objective_text,
                                        task_id?, elect_arm?} -> {episode_id, arm_id, arm_class,
                                        allocation_kind, slot, block_ordinal, episode_key, reused}
    POST /v1/pilot/episodes/{id}/close {executed_arm, rescue_level, finished, review_minutes,
                                        review_verdict, deviation_reason?, unfinished_reason?}
    POST /v1/dreams/{id}/adjudicate    {relevance, rationale?, blind_claimed?}
    GET  /v1/pilot/report              the same clause payloads the script renders
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_shared.identity import credential_fingerprint
from tce_shared.pilot_enrollment import ARM_CLASS, ClauseState, PilotArm, RelevanceVerdict

pytestmark = pytest.mark.integration

_EXEC_TOKEN = "pilot-exec-token"
_HOST_TOKEN = "pilot-host-token"
_HUMAN = "owner-1"
_WORKSPACE = "personal"
_MISSING = object()
_ABSENT_VALUE = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "host_capture_tokens",
    "allow_default_token",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "dream_proposals_enabled",
    "pilot_enrollment_enabled",
    "pilot_human_baseline_enabled",
)

_REQUIRED_SETTINGS = ("pilot_enrollment_enabled", "pilot_human_baseline_enabled")


# --------------------------------------------------------------------------- fixtures


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
    return tmp_path / "pilot-enrollment-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    """Compat mode with ONE server-bound human claim, so a header-asserted user stays unverified."""

    settings = get_settings()
    missing = [name for name in _REQUIRED_SETTINGS if not hasattr(settings, name)]
    if missing:
        pytest.fail(
            f"tce_lite_api.config has no {missing}. This gate is LIVE: it fails until Builder C lands "
            "the four P6 settings of p6_design §0.5 and the six routes of §6.3."
        )
    snapshot = _snapshot_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = _EXEC_TOKEN
    settings.host_capture_tokens = _HOST_TOKEN
    settings.allow_default_token = False
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.charter_enforcement_enabled = False
    settings.dream_proposals_enabled = True
    settings.pilot_enrollment_enabled = True
    settings.pilot_human_baseline_enabled = False
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _HOST_TOKEN): {
                "consumer": "host-capture-claude",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _exec_headers(consumer: str = "codex-executor") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_EXEC_TOKEN}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "executor",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _host_headers() -> dict[str, str]:
    """The verified human. Closing an episode needs this and nothing weaker (runbook step 5)."""

    return {
        "Authorization": f"Bearer {_HOST_TOKEN}",
        "X-TCE-Consumer": "host-capture-claude",
        "X-TCE-Role": "user",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


# --------------------------------------------------------------------------- helpers


def _rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _enroll(client: TestClient, *, objective: str, session: str = "sess-1", family: str = "needs_human", project: str | None = "proj_a") -> dict[str, Any]:
    body: dict[str, Any] = {
        "project_id": project,
        "decision_family": family,
        "session_id": session,
        "objective_text": objective,
    }
    response = client.post("/v1/pilot/episodes", json=body, headers=_exec_headers())
    assert response.status_code in {200, 201}, f"enrol failed {response.status_code}: {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


def _clause(payload: Any, name: str) -> dict[str, Any] | None:
    """Find one ``Clause.to_payload()`` anywhere in the report, however the block is nested.

    Looking the clause up by its own ``name`` rather than by a path keeps this test from pinning
    the report's block layout, which is the renderer's business and not this gate's.
    """

    if isinstance(payload, dict):
        if payload.get("name") == name and "state" in payload:
            return payload
        for value in payload.values():
            found = _clause(value, name)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _clause(item, name)
            if found is not None:
                return found
    return None


def _number(payload: Any, key: str) -> Any:
    """The first scalar the report files under ``key``, wherever the block is nested.

    The route and ``scripts/p6_pilot_report.py`` are two renderers of one computation and are
    free to nest their dream block differently; this gate is about the arithmetic, not the
    layout, so it looks the value up by name rather than by path.
    """

    if isinstance(payload, dict):
        for name, value in payload.items():
            if name == key and not isinstance(value, dict | list):
                return value
        for value in payload.values():
            found = _number(value, key)
            if found is not _ABSENT_VALUE:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _number(item, key)
            if found is not _ABSENT_VALUE:
                return found
    return _ABSENT_VALUE


def _count(payload: Any, group: str, key: str) -> int:
    """A census entry: ``group`` is the dict the report keeps the counts in (``nonresponse``,
    ``verdicts``/``status``, ``adjudications``), ``key`` the entry inside it."""

    if isinstance(payload, dict):
        candidate = payload.get(group)
        if isinstance(candidate, dict) and key in candidate:
            return int(candidate[key] or 0)
        for value in payload.values():
            found = _count(value, group, key)
            if found >= 0:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _count(item, group, key)
            if found >= 0:
                return found
    return -1


def _report(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/pilot/report", headers=_host_headers())
    assert response.status_code == 200, f"report failed {response.status_code}: {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


def _plant_proposal(db_path: Path, *, status: str, nonresponse: str = "awaiting_response", title: str = "a dream") -> str:
    """A P5 proposal, planted directly. P6 adds no dream column and forks no dream vocabulary."""

    proposal_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO dream_proposals (
                id, workspace_id, owner_id, subject_user_id, project_id, scope_kind, session_id,
                status, nonresponse_state, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'workspace', '', ?, ?, ?, ?, ?)
            """,
            (proposal_id, _WORKSPACE, _HUMAN, _HUMAN, status, nonresponse, title, now, now),
        )
        connection.commit()
    finally:
        connection.close()
    return proposal_id


def _adjudicate(client: TestClient, proposal_id: str, *, relevance: RelevanceVerdict, blind_claimed: bool = False) -> dict[str, Any]:
    response = client.post(
        f"/v1/dreams/{proposal_id}/adjudicate",
        json={"relevance": relevance.value, "rationale": "judged on its own terms", "blind_claimed": blind_claimed},
        headers=_host_headers(),
    )
    assert response.status_code in {200, 201}, f"adjudicate failed {response.status_code}: {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


# --------------------------------------------------------------------------- G2


def test_arm_is_frozen_and_unrepeatable(lite_client: TestClient, db_path: Path) -> None:
    """G2.  One draw per piece of work, and the counter proves it."""

    first = _enroll(lite_client, objective="fix the dashboard takeover scroll")
    arm = str(first["arm_id"])
    assert arm in {member.value for member in PilotArm}, first
    assert first["arm_class"] == ARM_CLASS[PilotArm(arm)], first
    assert first.get("reused") is False, "a first enrolment is not a reuse"

    episode_id = str(first["episode_id"])
    rows = _rows(db_path, "SELECT * FROM pilot_episodes WHERE id = ?", (episode_id,))
    assert len(rows) == 1, "the row must commit before the response carrying the arm is built"
    row = rows[0]
    assert bool(row["enrolled_before_execution"]) is True
    assert row["allocated_at"], row
    assert row["revealed_at"] is None or str(row["revealed_at"]) >= str(row["allocated_at"]), (
        "revealed_at precedes allocated_at: the arm was revealed before it was frozen"
    )

    strata = _rows(db_path, "SELECT stratum_id, next_slot FROM pilot_strata")
    assert len(strata) == 1, strata
    slot_after_first = int(strata[0]["next_slot"])

    # The same work, enrolled again. The ORIGINAL arm comes back and NO slot is consumed.
    repeat = _enroll(lite_client, objective="fix the dashboard takeover scroll")
    assert repeat["arm_id"] == arm, f"a repeat enrolment redrew the arm: {arm} -> {repeat['arm_id']}"
    assert repeat["episode_key"] == first["episode_key"]
    assert repeat["episode_id"] == episode_id, "a repeat enrolment minted a second episode row"
    assert repeat.get("reused") is True, "a repeat enrolment must say so"
    assert int(_rows(db_path, "SELECT next_slot FROM pilot_strata")[0]["next_slot"]) == slot_after_first, (
        "the repeat consumed a randomised slot. Returning the same arm while advancing the counter still "
        "corrupts the block balance the paired test draws its matched sets from."
    )
    assert len(_rows(db_path, "SELECT id FROM pilot_episodes")) == 1

    # Different work is a different episode, and it draws.
    other = _enroll(lite_client, objective="fix the dashboard takeover scrollbar")
    assert other["episode_key"] != first["episode_key"], "a one-word objective change was not a different episode"
    assert other["episode_id"] != episode_id
    assert int(_rows(db_path, "SELECT next_slot FROM pilot_strata")[0]["next_slot"]) == slot_after_first + 1


def test_a_caller_named_task_id_cannot_redraw_the_arm(lite_client: TestClient, db_path: Path) -> None:
    """G2, second half: the arm-shopping label of Proof 4, hunted under every name it can take.

    ``cancel_epoch`` is the sixth component of ``episode_key`` and §2.1 files it under producer
    *server*.  Its lookup key must therefore be the SESSION's own task, never a ``task_id`` the
    caller put in the body: ``pilot_strata`` is not keyed on the session, so a body field that
    moves the key hands back a fresh draw INSIDE the same ``(project, family)`` cell — Proof 4
    with a different field name, and the one shape of arm shopping the coverage clause cannot
    tell apart from honest volume until the cell is already contaminated.

    The rows planted below belong to the same owner and to an unrelated session, which is
    exactly the reach an executor holding one credential already has.
    """

    base = {
        "project_id": "proj_a",
        "decision_family": "needs_human",
        "session_id": "sess-shop",
        "objective_text": "fix the dashboard takeover scroll",
    }
    first = lite_client.post("/v1/pilot/episodes", json=base, headers=_exec_headers())
    assert first.status_code == 200, first.text
    frozen = first.json()
    slot_after_first = int(_rows(db_path, "SELECT next_slot FROM pilot_strata")[0]["next_slot"])

    now = datetime.now(tz=UTC).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        for sequence in range(1, 6):
            connection.execute(
                """
                INSERT INTO task_states (
                    id, workspace_id, owner_id, subject_user_id, session_id, task_id, project_id,
                    revision, contract_revision, highest_seq, status, last_cancel_seq,
                    created_at, updated_at, schema_version
                ) VALUES (?, ?, ?, ?, 'an-unrelated-session', ?, 'proj_elsewhere', 1, 1, 0,
                          'active', ?, ?, ?, 'v1')
                """,
                (str(uuid.uuid4()), _WORKSPACE, _HUMAN, _HUMAN, f"task-{sequence}", sequence, now, now),
            )
        connection.commit()
    finally:
        connection.close()

    for sequence in range(1, 6):
        body = dict(base)
        body["task_id"] = f"task-{sequence}"
        response = lite_client.post("/v1/pilot/episodes", json=body, headers=_exec_headers())
        assert response.status_code == 200, response.text
        again = response.json()
        assert again["episode_key"] == frozen["episode_key"], (
            f"body.task_id={body['task_id']!r} moved episode_key. A caller-varied field inside the "
            "key is an allocator a caller can shop."
        )
        assert again["arm_id"] == frozen["arm_id"], f"task_id={body['task_id']!r} redrew the arm"
        assert again["reused"] is True
        assert again["episode_id"] == frozen["episode_id"]

    body = dict(base)
    body["task_id"] = "a-task-that-was-never-written"
    unknown = lite_client.post("/v1/pilot/episodes", json=body, headers=_exec_headers())
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["episode_key"] == frozen["episode_key"], "an unknown task_id moved the key"

    assert int(_rows(db_path, "SELECT next_slot FROM pilot_strata")[0]["next_slot"]) == slot_after_first, (
        "task_id perturbation consumed randomised slots: six draws were taken for one piece of work"
    )
    assert len(_rows(db_path, "SELECT id FROM pilot_episodes")) == 1


def test_an_executor_cannot_close_an_episode(lite_client: TestClient) -> None:
    """The party under test does not grade itself: the close is an adjudication, not a self-report."""

    episode_id = str(_enroll(lite_client, objective="close me")["episode_id"])
    body = {
        "executed_arm": PilotArm.TCE_ASSISTED.value,
        "rescue_level": "none",
        "finished": True,
        "review_minutes": 12,
        "review_verdict": "accepted_with_edits",
    }
    refused = lite_client.post(f"/v1/pilot/episodes/{episode_id}/close", json=body, headers=_exec_headers())
    assert refused.status_code == 403, f"an executor closed its own episode: {refused.status_code} {refused.text}"

    accepted = lite_client.post(f"/v1/pilot/episodes/{episode_id}/close", json=body, headers=_host_headers())
    assert accepted.status_code in {200, 201}, accepted.text
    assert accepted.json()["adjudication_independent"] is True, (
        "the adjudicator was the enrolling principal; independence is a property of the row, not a promise"
    )


# --------------------------------------------------------------------------- G11


def test_false_positive_needs_an_adjudication(lite_client: TestClient, db_path: Path) -> None:
    """G11.  A rejection is a preference. ``not_relevant`` is a judgement. The second is never derived."""

    _plant_proposal(db_path, status="rejected")
    report = _report(lite_client)
    clause = _clause(report, "false_positive_relevance")
    assert clause is not None, f"the report prints no false_positive_relevance clause at all: {report.get('dreams')}"
    assert clause["state"] == ClauseState.NOT_COMPUTABLE.value, (
        f"a rejected proposal with zero adjudications produced {clause}. A 0.0 here reads as "
        "'no false positives' and a 1.0 reads as 'every proposal was junk'; neither was judged."
    )
    assert clause["reason"] == "no_adjudications", clause
    assert _number(report, "false_positive_relevance") is None, "a rate was printed for a corpus nobody judged"
    rejected_census = max(_count(report, "verdicts", "rejected"), _count(report, "status", "rejected"))
    assert rejected_census == 1, f"the rejection was not even counted: {report.get('dreams')}"

    # One not_relevant adjudication on an ACCEPTED proposal: acceptance and relevance are orthogonal.
    accepted = _plant_proposal(db_path, status="accepted", title="an accepted dream")
    _adjudicate(lite_client, accepted, relevance=RelevanceVerdict.NOT_RELEVANT)
    report = _report(lite_client)
    clause = _clause(report, "false_positive_relevance")
    assert clause is not None and clause["state"] == ClauseState.PASS.value, clause
    assert _number(report, "false_positive_relevance") == 1.0, "one not_relevant of one adjudication is 1.0"
    assert _number(report, "not_relevant") == 1, report.get("dreams")

    # An ignored proposal moves neither numerator nor denominator (P5 §1.2, unchanged by P6).
    _plant_proposal(db_path, status="proposed", nonresponse="ignored", title="an ignored dream")
    report = _report(lite_client)
    assert _count(report, "nonresponse", "ignored") == 1, f"the ignored proposal was not counted at all: {report.get('dreams')}"
    assert _number(report, "false_positive_relevance") == 1.0, "a nonresponse was counted as a judgement"
    assert _number(report, "not_relevant") == 1, "a nonresponse was counted as an adjudication"


def test_cannot_judge_is_neither_a_false_positive_nor_a_pass(lite_client: TestClient, db_path: Path) -> None:
    """``cannot_judge`` is in neither numerator nor denominator, and is printed as its own count."""

    proposal = _plant_proposal(db_path, status="accepted", title="an unjudgeable dream")
    _adjudicate(lite_client, proposal, relevance=RelevanceVerdict.CANNOT_JUDGE)
    report = _report(lite_client)
    assert _number(report, "cannot_judge") == 1, f"the adjudication was not recorded: {report.get('dreams')}"
    clause = _clause(report, "false_positive_relevance")
    assert clause is not None and clause["state"] == ClauseState.NOT_COMPUTABLE.value, (
        f"{clause} — 'I cannot judge this' was converted into a measurement"
    )
    assert _number(report, "false_positive_relevance") is None, "cannot_judge produced a rate"


def test_blind_is_server_checked(lite_client: TestClient, db_path: Path) -> None:
    """``blind_claimed`` is the caller's word. ``blind_verified`` is the server's, read from P5's log."""

    proposal = _plant_proposal(db_path, status="proposed", title="a blind-judged dream")

    # An adjudication that genuinely precedes any verdict event may be verified blind.
    _adjudicate(lite_client, proposal, relevance=RelevanceVerdict.RELEVANT, blind_claimed=True)
    before = _rows(db_path, "SELECT blind_claimed, blind_verified FROM dream_relevance_adjudications WHERE proposal_id = ?", (proposal,))
    assert len(before) == 1, before
    assert bool(before[0]["blind_claimed"]) is True

    # Now the owner renders a verdict, and adjudicates again claiming blindness. The event log
    # says otherwise, and the server believes the log.
    verdict = lite_client.post(
        f"/v1/dreams/{proposal}/transition",
        json={"action": "rejected", "reason": "not now"},
        headers=_host_headers(),
    )
    assert verdict.status_code == 200, verdict.text

    _adjudicate(lite_client, proposal, relevance=RelevanceVerdict.NOT_RELEVANT, blind_claimed=True)
    after = _rows(
        db_path,
        "SELECT blind_claimed, blind_verified, adjudicated_at FROM dream_relevance_adjudications WHERE proposal_id = ? ORDER BY adjudicated_at",
        (proposal,),
    )
    assert len(after) == 2, after
    latest = after[-1]
    assert bool(latest["blind_claimed"]) is True, "the caller's claim is recorded verbatim"
    assert bool(latest["blind_verified"]) is False, (
        "an adjudication that POSTDATES the verdict was marked blind_verified. Blindness checked "
        "against the caller's own assertion is the party under test grading itself."
    )
