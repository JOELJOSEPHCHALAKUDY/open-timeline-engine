"""Qualification records against the Lite store (P4 design §10.5, Builder C).

The subject here is *permission*: which ``(project, decision_family)`` pairs may have a
personalized selection shown to a human, and what it takes to lose that permission again.

The honest state of every deployment measured so far is that **no family holds one**, and these
cases are written so that fact is a property rather than an accident.  The record's absence is
the refusal; there is no setting that turns it into a yes, and there is no code path that
treats "we have not measured this" as "it is fine".

The cases that write a ``QUALIFIED`` row do so directly through ``policy_store.write_qualification``
rather than by driving a report -- there are zero adjudicated prospective cases in any family,
so a report run cannot produce one, and asserting that a *revoked* qualification stops granting
permission requires first having one.  Everything the HTTP boundary must enforce is asserted at
the HTTP boundary; SQL and the store functions are used to set up and to observe.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.db import _connect
from tce_lite_api.main import app
from tce_lite_api.policy_store import (
    LITE_MODEL_ID,
    LITE_RUNTIME_VERSION,
    RETRIEVAL_VERSION,
    _policy_tuning,
    lookup_qualification,
    qualification_gate,
    write_qualification,
)
from tce_shared.decision_policy import (
    DECISION_POLICY_REVISION,
    QualificationState,
)
from tce_shared.policy_thresholds import (
    EVIDENCE_DRIFT_MAX_GROWTH,
    MIN_ADJUDICATED,
    MIN_COVERAGE,
    MIN_DISTINCT_EPISODES,
    MIN_PRECISION_LOWER_BOUND,
    THRESHOLDS_SHA,
    THRESHOLDS_VERSION,
)
from tce_shared.scope import PROJECT_BOUND, ResolvedScope

_TOKEN = "policy-qualification-lite-token"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "workspace_access_mode",
    "behavior_autonomy_gate_enabled",
    "policy_allow_unscoped_project_evidence",
    "policy_advisor_required_families",
)

_WORKSPACE = "personal"
_SUBJECT = "codex-executor"
_FAMILY = "safety_confirmation"


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
    return tmp_path / "policy-qualification-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = _TOKEN
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.behavior_autonomy_gate_enabled = False
    settings.policy_allow_unscoped_project_evidence = True
    settings.policy_advisor_required_families = ""
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _scope(project_id: str | None = "open-timeline-engine") -> ResolvedScope:
    return ResolvedScope(
        workspace_id=_WORKSPACE,
        executor_id=_SUBJECT,
        owner_id=_SUBJECT,
        subject_user_id=_SUBJECT,
        project_id=project_id,
        project_binding=PROJECT_BOUND,
        task_id=None,
        owner_ids=frozenset({_SUBJECT}),
    )


def _write(
    conn: Any,
    *,
    state: QualificationState = QualificationState.QUALIFIED,
    scope: ResolvedScope | None = None,
    learning_eligible_at_qualification: int = 0,
    shortfalls: tuple[str, ...] = (),
    now: datetime | None = None,
) -> str:
    """Write one attempt.  A QUALIFIED attempt carries numbers that clear every frozen clause.

    ``write_qualification`` refuses to stamp ``qualified`` on a record whose own metrics fall
    short (``assert_qualification_is_earned``), so a helper that wrote zeros beside a
    QUALIFIED verdict would be asking the store to record a claim nothing measured.  The
    values below are the smallest set that clears the gate's numeric clauses; a failing
    attempt keeps its real zeros.
    """

    earned = state is QualificationState.QUALIFIED
    qualification_id = write_qualification(
        conn,
        scope=scope or _scope(),
        decision_family=_FAMILY,
        state=state,
        settings=get_settings(),
        metrics={"coverage": MIN_COVERAGE if earned else 0.0},
        gate={"passed": earned},
        shortfalls=shortfalls,
        baselines={},
        exclusions={},
        adjudicated_count=MIN_ADJUDICATED if earned else 0,
        non_abstained_count=round(MIN_ADJUDICATED * MIN_COVERAGE) if earned else 0,
        coverage=MIN_COVERAGE if earned else 0.0,
        precision_lower_bound=MIN_PRECISION_LOWER_BOUND if earned else 0.0,
        distinct_episodes=MIN_DISTINCT_EPISODES if earned else 0,
        duplicate_context_ratio=0.0,
        learning_eligible_at_qualification=learning_eligible_at_qualification,
        now=now,
    )
    conn.commit()
    return qualification_id


def test_the_table_exists_and_no_family_starts_out_qualified(lite_client: TestClient) -> None:
    """The state P4 ships in.  Zero adjudicated prospective cases exist in any family, so
    ``NOT_QUALIFIED`` is the honest result and not a failure to run the machinery."""

    conn = _connect()
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='policy_qualifications'"
        ).fetchone() is not None, "the Lite guard must mirror the Full revision"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='threshold_registrations'"
        ).fetchone() is not None
        assert int(conn.execute("SELECT COUNT(*) FROM policy_qualifications").fetchone()[0]) == 0
        assert (
            lookup_qualification(
                conn,
                scope=_scope(),
                decision_family=_FAMILY,
                decision_at=datetime.now(tz=UTC),
                learning_eligible_total=0,
            )
            is None
        )
        gate = qualification_gate(conn, workspace_id=_WORKSPACE, subject_user_id=_SUBJECT)
    finally:
        conn.close()
    assert gate == {"passed": False, "reason": "no_qualification_recorded", "families": {}, "evaluated_at": None}


def test_a_failed_run_is_a_record_with_shortfalls_not_an_absence(lite_client: TestClient) -> None:
    """A ``NOT_QUALIFIED`` row is a first-class artifact.  Without it, "this family cannot
    produce a qualifying case" is a mystery instead of a readable number -- which is how the
    previous gate's unsatisfiable clause went unnoticed."""

    conn = _connect()
    try:
        _write(
            conn,
            state=QualificationState.NOT_QUALIFIED,
            shortfalls=("adjudicated_below_minimum", "no_candidate_set_at_freeze_site"),
        )
        row = conn.execute(
            "SELECT state, shortfalls_json, thresholds_sha, thresholds_version, tuning_sha, "
            "decision_policy_revision, retrieval_version, model_id, runtime_version "
            "FROM policy_qualifications LIMIT 1"
        ).fetchone()
        # It is recorded, and it still does not grant anything.
        assert (
            lookup_qualification(
                conn,
                scope=_scope(),
                decision_family=_FAMILY,
                decision_at=datetime.now(tz=UTC),
                learning_eligible_total=0,
            )
            is None
        )
        gate = qualification_gate(conn, workspace_id=_WORKSPACE, subject_user_id=_SUBJECT)
    finally:
        conn.close()
    assert row is not None
    assert str(row["state"]) == QualificationState.NOT_QUALIFIED.value
    assert "no_candidate_set_at_freeze_site" in str(row["shortfalls_json"])
    # Every bound key is stamped on the record, including the two that cover the numbers and the
    # switches. Without them a threshold could be lowered, a record written, and the threshold
    # put back, with nothing downstream able to tell.
    assert str(row["thresholds_sha"]) == THRESHOLDS_SHA
    assert str(row["thresholds_version"]) == THRESHOLDS_VERSION
    assert str(row["tuning_sha"]) == _policy_tuning(get_settings()).tuning_sha()
    assert str(row["decision_policy_revision"]) == DECISION_POLICY_REVISION
    # The loader's own identity: a Full qualification cannot be carried by a Lite decision.
    assert str(row["retrieval_version"]) == RETRIEVAL_VERSION
    assert str(row["model_id"]) == LITE_MODEL_ID
    assert str(row["runtime_version"]) == LITE_RUNTIME_VERSION
    assert gate["passed"] is False
    assert gate["families"][_FAMILY] == QualificationState.NOT_QUALIFIED.value


def test_a_live_qualification_is_returned_and_an_expired_one_is_not(lite_client: TestClient) -> None:
    """Expiry is derived on read, never written back.

    An immutable record whose liveness is computed at lookup time is what removes "a run that
    passed once unlocks autonomy forever" without needing a sweeper job to come along and take
    it away.  Between report runs there is no window in which a dead record still grants.
    """

    conn = _connect()
    try:
        created = datetime.now(tz=UTC) - timedelta(days=200)
        _write(conn, state=QualificationState.QUALIFIED, now=created)
        # 90-day TTL from a 200-day-old run: expired by 110 days.
        assert (
            lookup_qualification(
                conn,
                scope=_scope(),
                decision_family=_FAMILY,
                decision_at=datetime.now(tz=UTC),
                learning_eligible_total=0,
            )
            is None
        )
        gate_now = qualification_gate(conn, workspace_id=_WORKSPACE, subject_user_id=_SUBJECT)
        # Read as of the day after it was written and the same immutable row is live.
        live = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family=_FAMILY,
            decision_at=created + timedelta(days=1),
            learning_eligible_total=0,
        )
        gate_then = qualification_gate(
            conn,
            workspace_id=_WORKSPACE,
            subject_user_id=_SUBJECT,
            now=created + timedelta(days=1),
        )
    finally:
        conn.close()
    assert gate_now["passed"] is False
    assert gate_now["families"][_FAMILY] == QualificationState.EXPIRED.value
    assert live is not None
    assert live.state is QualificationState.QUALIFIED
    assert live.thresholds_sha == THRESHOLDS_SHA
    assert live.retrieval_version == RETRIEVAL_VERSION
    assert gate_then["passed"] is True
    assert gate_then["families"][_FAMILY] == QualificationState.QUALIFIED.value


def test_evidence_growth_past_the_frozen_fraction_stops_granting(lite_client: TestClient) -> None:
    """Evidence drift, which replaces an equality check that never fired.

    The clause it replaces compared two hashes of different things -- the qualification's hash
    was over the evaluation corpus, the request's over that turn's twelve neighbours -- so it
    essentially never held and always fell through to a disjunct that passes automatically as
    time moves forward.  Net effect: the qualification never invalidated on evidence growth at
    all.  This one measures growth, in one direction, against one frozen constant.
    """

    baseline = 100
    grown = int(baseline * (1 + EVIDENCE_DRIFT_MAX_GROWTH)) + 1
    conn = _connect()
    try:
        _write(conn, state=QualificationState.QUALIFIED, learning_eligible_at_qualification=baseline)
        still_live = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family=_FAMILY,
            decision_at=datetime.now(tz=UTC),
            learning_eligible_total=baseline,
        )
        drifted = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family=_FAMILY,
            decision_at=datetime.now(tz=UTC),
            learning_eligible_total=grown,
        )
        # A shrinking corpus is a different problem and is deliberately not treated as drift.
        shrunk = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family=_FAMILY,
            decision_at=datetime.now(tz=UTC),
            learning_eligible_total=1,
        )
    finally:
        conn.close()
    assert still_live is not None
    assert still_live.learning_eligible_at_qualification == baseline
    assert drifted is None, "the corpus this was measured on is not the corpus in use"
    assert shrunk is not None


def test_a_qualification_for_one_family_does_not_travel_to_another(lite_client: TestClient) -> None:
    """Permission is earned per family.  A single global "the latest run passed" flag -- which is
    what this replaces -- granted every family whatever one run happened to measure."""

    conn = _connect()
    try:
        _write(conn, state=QualificationState.QUALIFIED)
        other = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family="next_objective",
            decision_at=datetime.now(tz=UTC),
            learning_eligible_total=0,
        )
        same = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family=_FAMILY,
            decision_at=datetime.now(tz=UTC),
            learning_eligible_total=0,
        )
        gate = qualification_gate(conn, workspace_id=_WORKSPACE, subject_user_id=_SUBJECT)
    finally:
        conn.close()
    assert other is None
    assert same is not None
    assert set(gate["families"]) == {_FAMILY}


def test_the_turn_is_still_not_exposed_while_the_bound_keys_disagree(lite_client: TestClient) -> None:
    """The end-to-end shape of Y1: even with a live ``QUALIFIED`` row present, a turn whose
    bound keys do not match the record is reported truthfully and left unexposed -- and the turn
    itself still succeeds.  Permission-absence is a reason not to personalize, never a reason to
    stop and ask a human."""

    conn = _connect()
    try:
        # A record for a family this turn will not use, written under a project the turn is not
        # bound to. Present, live, and inapplicable.
        _write(conn, state=QualificationState.QUALIFIED, scope=_scope(project_id="some-other-project"))
    finally:
        conn.close()

    session_id = f"qual-{uuid.uuid4().hex[:8]}"
    response = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "task": "tidy the changelog",
            "persona_mode": "shadow",
            "app_context": {"domain": "coding", "project": "open-timeline-engine"},
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers={"Authorization": f"Bearer {_TOKEN}", "X-TCE-Role": "executor", "X-TCE-Consumer": _SUBJECT},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    block = body["policy_decision"]
    assert block["exposed"] is False
    assert block["exposure_state"] == "no_qualification"
    # And the turn is a turn, not an escalation.
    assert body["enforcement_reason"] != "policy_abstention"
    assert body["final_response"] is not None


def test_the_store_refuses_to_record_a_qualification_nothing_measured(lite_client: TestClient) -> None:
    """The record is the permission.  A writer that stamps ``qualified`` on demand *is* the
    fake path, and every bound key on such a row is honest — the thresholds digest, the model,
    the retriever — so nothing downstream can tell the verdict was asserted rather than
    measured.  ``lookup_qualification`` would return it, ``_exposure`` would report EXPOSED and
    ``qualification_gate`` would flip to ``passed=True`` on a corpus with zero adjudicated
    cases.
    """

    conn = _connect()
    try:
        with pytest.raises(ValueError) as excinfo:
            write_qualification(
                conn,
                scope=_scope(),
                decision_family=_FAMILY,
                state=QualificationState.QUALIFIED,
                settings=get_settings(),
                metrics={},
                gate={"passed": True},
                shortfalls=("adjudicated 0 < 100", "coverage 0.0 < 0.3"),
                baselines={},
                exclusions={},
                adjudicated_count=0,
                non_abstained_count=0,
                coverage=0.0,
                precision_lower_bound=0.0,
                distinct_episodes=0,
            )
        conn.commit()
        rows = conn.execute("SELECT count(*) AS n FROM policy_qualifications").fetchone()
        gate = qualification_gate(conn, workspace_id=_WORKSPACE, subject_user_id=_SUBJECT)
        live = lookup_qualification(
            conn,
            scope=_scope(),
            decision_family=_FAMILY,
            decision_at=datetime.now(tz=UTC),
            learning_eligible_total=0,
        )
    finally:
        conn.close()
    assert "refusing to record state=qualified" in str(excinfo.value)
    assert int(rows["n"]) == 0, "the refused claim must leave no row behind"
    assert gate["passed"] is False
    assert live is None
