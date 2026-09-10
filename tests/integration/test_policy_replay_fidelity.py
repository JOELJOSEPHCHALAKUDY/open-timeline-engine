"""Z3: a replayed ``DecisionRequest`` must reproduce the frozen row's own fingerprint.

P4's central claim is that the evaluated route *is* the deployed route.  The only instrument that
can settle it is ``DecisionRequest.fingerprint()``: it is computed on the live turn, persisted as
``behavior_shadow_predictions.request_fingerprint``, and recomputable in replay.  If a replayed
request hashes to the stored value, the harness scored the decision that was made.  If it does
not, the harness scored *a different decision that resembles it*, and every number the promotion
gate prints is about that other decision.

**Why this file exists rather than one more assertion in test_policy_parity.**  Every fingerprinted
row in the live corpus today has an EMPTY evidence set, so ``sorted(evidence_ids)`` is ``[]`` on
both sides no matter what the replay does.  A fidelity test driven only by live rows would pass
today *and would have passed before any of the Z3 fixes* -- it cannot tell a correct replay from
one that re-retrieves, invents an identity, or drops the evidence set entirely.  That is a test
worth nothing, and saying so out loud is part of the point.  So the load-bearing arm here seeds a
request WITH evidence and asserts on that, and every arm carries a negative control that fails
when the corresponding fix is reverted.

Arm 1 (``test_a_seeded_decision_with_evidence_replays_to_its_own_fingerprint`` and the controls
below it) needs no database and no migration.  It builds a request with six evidence rows, freezes
what the two real writers would store, and replays through ``tce_api.policy_store``.

Arm 2 (``test_every_stored_row_with_offered_evidence_replays_to_its_own_fingerprint``) runs the
same assertion against real stored rows.  It requires alembic ``20260909_0041``; without it the
offered evidence ids were never stored, the arm SKIPS with that reason, and the skip is the honest
answer -- not a pass.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_WORKSPACE = "p4-replay-fidelity-ws"
_SUBJECT = "p4-replay-fidelity-subject"


# --------------------------------------------------------------------------------------
# Arm 1 — seeded, with evidence, no database required
# --------------------------------------------------------------------------------------


def _evidence_db_rows(count: int = 6) -> list[dict[str, Any]]:
    """DB-shaped observation rows, i.e. what a ``SELECT`` hands back before coercion.

    The last two are deliberately off-topic: a decision is OFFERED every row the loader returns
    and CITES only the neighbours that clear the similarity floor, and a seed where those two sets
    coincide cannot tell the fix from the mistake it replaces.  See
    ``test_replaying_the_cited_subset_instead_of_the_offered_set_breaks_the_replay``.
    """

    base = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    off_topic = {count - 1, count - 2}
    return [
        {
            "id": f"11111111-1111-4111-8111-00000000000{index}",
            "consumer_id": "cli",
            "workspace_id": _WORKSPACE,
            "subject_user_id": _SUBJECT,
            "ts": base - timedelta(days=index),
            "situation_type": "routine_task" if index in off_topic else "choice_required",
            "situation_summary": (
                f"rotate the log files {index}" if index in off_topic else f"deploy window {index}"
            ),
            "context_snapshot": {"branch": "main"},
            "user_response": "ship it",
            "response_reasoning": None,
            "outcome": None,
            "outcome_sentiment": None,
            "source_event_ids": [],
            "confidence": 0.9,
            "superseded_by": None,
            "objective_text": (
                "tidy the log directory" if index in off_topic else "pick a deploy window"
            ),
            "constraints_json": {} if index in off_topic else {"freeze": False},
            "available_choices_json": (
                ["gzip", "delete"] if index in off_topic else ["ship now", "wait for morning"]
            ),
            "selected_choice": (
                "gzip" if index in off_topic else ("ship now" if index % 2 == 0 else "wait for morning")
            ),
            "action_taken": "deployed",
            "correction_text": "",
            "memory_class": "decision",
            "evidence_source": "stated",
            "lifecycle_status": "active",
            "valid_from": base - timedelta(days=index),
            "valid_until": None,
            "contradicts_ids_json": [],
            "confirmed_at": None,
            "behavior_schema_version": "v1",
            "redaction_applied": False,
            "learning_eligible": True,
            "storage_score": 0.8,
            "storage_decision": "eligible",
            "opportunity_id": None,
            "origin_kind": "trusted_capture",
            "capture_receipt_id": None,
            "extraction_version": "v1",
        }
        for index in range(count)
    ]


def _seeded_request(evidence_db_rows: list[dict[str, Any]]) -> Any:
    """The request a live turn would have assembled, with a non-empty evidence set."""

    from tce_api.policy_store import RETRIEVAL_VERSION, _evidence_from_row
    from tce_shared.decision_policy import DecisionRequest

    return DecisionRequest(
        decision_family="needs_human",
        situation_type="choice_required",
        situation_summary="pick the deploy window",
        objective_text="decide when to deploy",
        constraints={"freeze": False},
        context_snapshot={"objective_hash": "abc123", "turn": 3},
        candidate_options=("ship now", "wait for morning"),
        evidence_rows=tuple(_evidence_from_row(row) for row in evidence_db_rows),
        decision_at=datetime(2026, 9, 9, 8, 30, 15, 123456, tzinfo=UTC),
        workspace_id=_WORKSPACE,
        subject_user_id=_SUBJECT,
        project_id="proj-alpha",
        episode_key="episode-alpha-7",
        evidence_revision="rev-seeded-001",
        evidence_cutoff_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        retrieval_version=RETRIEVAL_VERSION,
        model_id="test-model",
        runtime_version="test-runtime",
    )


def _stored_row(request: Any, result: Any) -> dict[str, Any]:
    """The row the two real writers produce for this decision, as a ``SELECT`` returns it.

    The replay-input values come from ``policy_store.replay_inputs_payload`` -- the same call
    ``persist_policy_decision`` makes -- rather than from a hand-rolled copy, so this cannot pass
    by agreeing with itself.  The rest mirrors ``freeze_shadow_prediction`` (``query_json``,
    ``created_at = frozen_at``, ``evidence_revision``, ``evidence_cutoff_at``),
    ``persist_policy_decision`` (identity columns, ``request_fingerprint``, ``policy_json``) and
    ``insert_opportunity`` (``alternatives_json``).
    """

    from tce_api.policy_store import REPLAY_INPUT_COLUMN_NAMES, replay_inputs_payload

    replay_inputs = replay_inputs_payload(request)
    assert tuple(replay_inputs) == REPLAY_INPUT_COLUMN_NAMES, (
        "the writer's payload keys no longer match the migration's column names; one of the two "
        f"moved without the other: {tuple(replay_inputs)} vs {REPLAY_INPUT_COLUMN_NAMES}"
    )
    # `created_at` is deliberately LATER than `decision_at`: the write path calls
    # `datetime.now()` a second time for the freeze, and a replay that reads `created_at` as the
    # decision time therefore diverges by construction.  See the negative control below.
    frozen_at = request.decision_at + timedelta(milliseconds=41)
    return {
        "id": str(uuid.uuid4()),
        "session_id": "sess-1",
        "workspace_id": request.workspace_id,
        "subject_user_id": request.subject_user_id,
        "decision_family": request.decision_family,
        "query_json": {
            "situation_type": request.situation_type,
            "situation_summary": request.situation_summary,
            "objective_text": request.objective_text,
            "constraints": dict(request.constraints),
            "context_snapshot": dict(request.context_snapshot),
        },
        "actual_choice": "ship now",
        "frozen_at": frozen_at,
        "created_at": frozen_at,
        "resolution_state": "resolved",
        "decision_advice_shown": False,
        "advice_visible": False,
        "evidence_revision": request.evidence_revision,
        "evidence_cutoff_at": request.evidence_cutoff_at,
        "retrieval_version": result.retrieval_version,
        "model_id": request.model_id,
        "runtime_version": request.runtime_version,
        "episode_key": request.episode_key,
        "project_id": request.project_id,
        "request_fingerprint": result.request_fingerprint,
        "policy_json": result.to_payload(),
        "objective_hash": "abc123",
        "opportunity_project_id": request.project_id,
        "opportunity_episode_key": request.episode_key,
        "candidate_options_json": list(request.candidate_options),
        **replay_inputs,
    }


def _fetch_stub(rows: list[dict[str, Any]]) -> Any:
    """A ``(sql, params) -> rows`` callable standing in for the psycopg fetch, id-filtered."""

    by_id = {str(row["id"]): row for row in rows}

    def fetch(_sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        wanted = list(params[0]) if params else []
        return [by_id[str(key)] for key in wanted if str(key) in by_id]

    return fetch


def _replay(row: dict[str, Any], evidence_db_rows: list[dict[str, Any]]) -> tuple[Any, tuple[str, ...]]:
    from tce_api.policy_store import replay_evidence_rows, replay_request_from_row

    rows, missing = replay_evidence_rows(_fetch_stub(evidence_db_rows), row)
    return replay_request_from_row(row, rows), missing


def _diff(live: Any, replayed: Any) -> dict[str, Any]:
    from tce_api.policy_store import replay_fingerprint_terms

    live_terms = replay_fingerprint_terms(live)
    replay_terms = replay_fingerprint_terms(replayed)
    return {
        key: {"live": live_terms[key], "replayed": replay_terms[key]}
        for key in live_terms
        if live_terms[key] != replay_terms[key]
    }


def test_a_seeded_decision_with_evidence_replays_to_its_own_fingerprint() -> None:
    """The load-bearing arm: four evidence rows, one decision, one exact reconstruction.

    Every live row today has zero evidence, so this arm would be vacuous if it were driven from
    the corpus -- ``sorted([]) == sorted([])`` regardless of what the replay does.  Seeding the
    evidence is what makes ``evidence_ids`` a term that can actually disagree.
    """

    from tce_shared.decision_policy import decide

    evidence_db_rows = _evidence_db_rows()
    request = _seeded_request(evidence_db_rows)
    assert request.evidence_rows, "the seed has no evidence; this arm would be vacuous"

    result = decide(request)
    row = _stored_row(request, result)

    replayed, missing = _replay(row, evidence_db_rows)
    assert not missing, f"replay could not resolve offered evidence ids: {missing}"
    assert len(replayed.evidence_rows) == len(request.evidence_rows), (
        f"replay recovered {len(replayed.evidence_rows)} of {len(request.evidence_rows)} evidence rows"
    )
    assert replayed.fingerprint() == row["request_fingerprint"], (
        "the replayed request does not reproduce the stored fingerprint, so the promotion gate "
        "would be scoring a different decision than the one that was frozen. Divergent terms:\n"
        + json.dumps(_diff(request, replayed), indent=2, sort_keys=True, default=str)
    )


def test_dropping_the_offered_evidence_ids_breaks_the_replay() -> None:
    """The negative control for Z3(c): without the stored offered ids the fingerprint diverges.

    This is what makes the arm above meaningful.  Before ``20260909_0041`` the offered set was not
    persisted at all -- only the CITED subset, in ``policy_json`` -- so this is precisely the state
    every replay was in, on every row, for every decision that had evidence.
    """

    from tce_shared.decision_policy import decide

    evidence_db_rows = _evidence_db_rows()
    request = _seeded_request(evidence_db_rows)
    row = _stored_row(request, decide(request))
    row["offered_evidence_ids_json"] = None

    replayed, _missing = _replay(row, evidence_db_rows)
    assert replayed.evidence_rows == ()
    assert replayed.fingerprint() != row["request_fingerprint"], (
        "the fingerprint is blind to the evidence set, so storing the offered ids buys nothing "
        "and this whole file is testing a tautology"
    )


def test_replaying_at_created_at_instead_of_decision_at_breaks_the_replay() -> None:
    """The negative control for the ``decision_at`` column.

    The write path takes ``datetime.now()`` once for the request and again for the freeze, so
    ``created_at`` is strictly later than the decision time.  A replay that used ``created_at``
    -- which is what every replay assembly did -- can never reproduce the fingerprint of a row
    whose decision had any evidence to load.
    """

    from tce_shared.decision_policy import decide

    evidence_db_rows = _evidence_db_rows()
    request = _seeded_request(evidence_db_rows)
    row = _stored_row(request, decide(request))
    assert row["created_at"] > row["decision_at"], "the seed no longer models the two-clock write path"
    row["decision_at"] = None

    replayed, _missing = _replay(row, evidence_db_rows)
    assert replayed.decision_at == row["created_at"]
    assert replayed.fingerprint() != row["request_fingerprint"]


def test_stamped_identity_literals_break_the_replay() -> None:
    """The negative control for Z3(b): ``evidence_revision`` and ``retrieval_version`` are hashed.

    The three replay assemblies stamped ``"live-corpus-replay"`` / ``"replay-v1"`` /
    ``"lite-replay"`` over fields the row already carried.  Two of those are fingerprint terms, so
    the stamping alone guaranteed that no replay could ever match its own row.
    """

    from tce_shared.decision_policy import decide

    evidence_db_rows = _evidence_db_rows()
    request = _seeded_request(evidence_db_rows)
    result = decide(request)
    row = _stored_row(request, result)

    for field, literal in (("evidence_revision", "live-corpus-replay"), ("retrieval_version", "replay-v1")):
        stamped = dict(row)
        stamped[field] = literal
        # `policy_json` carries the same two fields and is the documented fallback, so it has to
        # be stamped too or the fallback would quietly rescue the field this control is removing.
        payload = dict(result.to_payload())
        payload[field] = literal
        stamped["policy_json"] = payload
        replayed, _missing = _replay(stamped, evidence_db_rows)
        assert replayed.fingerprint() != row["request_fingerprint"], (
            f"a replay that invents {field}={literal!r} still matches the stored fingerprint; "
            "the fingerprint is not binding this term"
        )


def test_the_cited_ids_are_not_a_safe_substitute_for_the_offered_ids() -> None:
    """``policy_json['evidence_observation_ids']`` is the cited list, and it is not the offered one.

    This is the most plausible wrong fix -- the cited ids were already on the row, so reaching for
    them looks like it costs no migration.  It fails in the worst possible way: the two lists are
    *equal* on an abstention, which cites everything it considered, and a strict subset whenever
    the policy selects.  A replay built on them would therefore reproduce some fingerprints and
    not others, with nothing in the output to say which.

    Both halves are asserted here, on one seed: the abstention's cited list equals the offered
    list (so the substitution looks correct), and a strict subset of the same size the selection
    path would produce diverges (so it is not).
    """

    from tce_shared.decision_policy import DecisionStatus, decide

    evidence_db_rows = _evidence_db_rows()
    request = _seeded_request(evidence_db_rows)
    result = decide(request)
    row = _stored_row(request, result)
    offered = list(row["offered_evidence_ids_json"])

    assert result.status is DecisionStatus.ABSTAINED
    assert list(result.to_payload()["evidence_observation_ids"]) == offered, (
        "this seed no longer abstains over its whole evidence set, so the first half of this "
        "control -- 'the substitution looks correct' -- is no longer demonstrated"
    )

    # The shape the cited list takes on any non-abstaining decision: the neighbours that
    # supported the selected option, which is a strict subset of what was offered.
    subset = offered[:-2]
    assert subset and subset != offered
    replayed, _missing = _replay({**row, "offered_evidence_ids_json": subset}, evidence_db_rows)
    assert replayed.fingerprint() != row["request_fingerprint"], (
        "the fingerprint does not distinguish the offered evidence set from a subset of it, so "
        "nothing here is measuring which rows the decision actually saw"
    )


def test_the_replay_select_list_and_the_migration_agree() -> None:
    """The select list, the writer payload and the migration name the same four columns.

    Cheap, and it catches the failure this whole area is prone to: a column added on one side and
    silently substituted for on the other.
    """

    from pathlib import Path

    from tce_api.policy_store import (
        REPLAY_INPUT_COLUMN_NAMES,
        REPLAY_ROW_COLUMNS,
        replay_row_columns,
    )

    for name in REPLAY_INPUT_COLUMN_NAMES:
        assert f"p.{name}" in REPLAY_ROW_COLUMNS, f"{name} is not selected by REPLAY_ROW_COLUMNS"
        assert f"AS {name}" in replay_row_columns(replay_inputs_present=False), (
            f"{name} has no literal stand-in in the degraded select list, so a database one "
            "revision behind would fail the SELECT instead of reporting 'not replayable'"
        )

    migration = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "alembic"
        / "versions"
        / "20260909_0041_shadow_replay_inputs.py"
    ).read_text(encoding="utf-8")
    for name in REPLAY_INPUT_COLUMN_NAMES:
        assert f'"{name} ' in migration, f"{name} is not added by alembic 20260909_0041"


# --------------------------------------------------------------------------------------
# Arm 2 — the same assertion against real stored rows
# --------------------------------------------------------------------------------------

_REPLAY_MIGRATION_SKIP = (
    "alembic 20260909_0041 is not applied, so behavior_shadow_predictions.offered_evidence_ids_json "
    "does not exist and no stored row carries the evidence set its decision was offered. This arm "
    "SKIPS rather than passing: a replay against an evidence set that was never stored proves "
    "nothing. Apply the revision and it runs."
)


@pytest.fixture()
def engine() -> Iterator[Any]:
    url = os.environ.get("TCE_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TCE_DATABASE_URL is not set")
    import sqlalchemy as sa

    eng = sa.create_engine(url, future=True)
    with eng.connect() as conn:
        present = conn.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM information_schema.columns
                 WHERE table_name = 'behavior_shadow_predictions'
                   AND column_name = 'offered_evidence_ids_json'
                """
            )
        ).scalar()
    if not present:
        eng.dispose()
        pytest.skip(_REPLAY_MIGRATION_SKIP)
    yield eng
    eng.dispose()


def _psycopg_fetch() -> Any:
    import psycopg

    url = os.environ.get("TCE_DATABASE_URL", "").strip().replace("postgresql+psycopg://", "postgresql://")

    def fetch(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(url) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            columns = [description[0] for description in (cursor.description or [])]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    return fetch


def test_every_stored_row_with_offered_evidence_replays_to_its_own_fingerprint(engine: Any) -> None:
    """Seed a real prospective row WITH evidence through the real writers, then replay it.

    The seed is the point.  Every row the live corpus has today was frozen with an empty evidence
    set, so an arm that only read existing rows would pass whatever the replay did.  This inserts
    four ``decision_observations``, freezes a shadow row against them through
    ``freeze_shadow_prediction`` + ``persist_policy_decision``, and then reads the row back through
    exactly the select list and assembly the qualification report uses.

    Everything it writes is scoped to a dedicated workspace and deleted in a ``finally``.
    """

    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from tce_api.behavior_control_store import freeze_shadow_prediction
    from tce_api.policy_store import (
        persist_policy_decision,
        prediction_payload,
        replay_evidence_rows,
        replay_request_from_row,
        replay_row_columns,
        reset_policy_column_cache,
    )
    from tce_shared.decision_policy import decide

    reset_policy_column_cache()
    evidence_db_rows = _evidence_db_rows()
    request = _seeded_request(evidence_db_rows)
    result = decide(request)
    opportunity_id = uuid.uuid4()
    prediction_id: uuid.UUID | None = None

    try:
        with Session(engine) as db:
            for row in evidence_db_rows:
                db.execute(
                    sa.text(
                        """
                        INSERT INTO decision_observations (
                            id, consumer_id, workspace_id, subject_user_id, ts, situation_type,
                            situation_summary, context_snapshot, user_response, source_event_ids,
                            confidence, objective_text, constraints_json, available_choices_json,
                            selected_choice, action_taken, correction_text, memory_class,
                            evidence_source, lifecycle_status, valid_from, contradicts_ids_json,
                            behavior_schema_version, redaction_applied, learning_eligible,
                            storage_score, storage_decision, origin_kind, extraction_version
                        ) VALUES (
                            CAST(:id AS uuid), :consumer_id, :workspace_id, :subject_user_id, :ts,
                            :situation_type, :situation_summary, CAST(:context_snapshot AS jsonb),
                            :user_response, '{}', :confidence, :objective_text,
                            CAST(:constraints_json AS jsonb), CAST(:available_choices_json AS jsonb),
                            :selected_choice, :action_taken, '', :memory_class, :evidence_source,
                            'active', :valid_from, '[]'::jsonb, 'v1', false, true, 0.8, 'eligible',
                            'trusted_capture', 'v1'
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": row["id"],
                        "consumer_id": row["consumer_id"],
                        "workspace_id": row["workspace_id"],
                        "subject_user_id": row["subject_user_id"],
                        "ts": row["ts"],
                        "situation_type": row["situation_type"],
                        "situation_summary": row["situation_summary"],
                        "context_snapshot": json.dumps(row["context_snapshot"]),
                        "user_response": row["user_response"],
                        "confidence": row["confidence"],
                        "objective_text": row["objective_text"],
                        "constraints_json": json.dumps(row["constraints_json"]),
                        "available_choices_json": json.dumps(row["available_choices_json"]),
                        "selected_choice": row["selected_choice"],
                        "action_taken": row["action_taken"],
                        "memory_class": row["memory_class"],
                        "evidence_source": row["evidence_source"],
                        "valid_from": row["valid_from"],
                    },
                )
            db.execute(
                sa.text(
                    """
                    INSERT INTO decision_opportunities (
                        id, workspace_id, subject_user_id, owner_id, session_id, turn,
                        objective_hash, decision_family, question_text, alternatives_json,
                        created_at, expires_at, frozen_at, status
                    ) VALUES (
                        :id, :workspace_id, :subject_user_id, :owner_id, :session_id, 1,
                        'abc123', :decision_family, 'pick the deploy window',
                        CAST(:alternatives_json AS jsonb), :created_at, :expires_at, :created_at, 'open'
                    )
                    """
                ),
                {
                    "id": opportunity_id,
                    "workspace_id": _WORKSPACE,
                    "subject_user_id": _SUBJECT,
                    "owner_id": _SUBJECT,
                    "session_id": "sess-replay-fidelity",
                    "decision_family": request.decision_family,
                    "alternatives_json": json.dumps(list(request.candidate_options)),
                    "created_at": request.decision_at,
                    "expires_at": request.decision_at + timedelta(hours=1),
                },
            )
            prediction_id = freeze_shadow_prediction(
                db,
                workspace_id=_WORKSPACE,
                subject_user_id=_SUBJECT,
                opportunity_id=opportunity_id,
                session_id="sess-replay-fidelity",
                turn=1,
                decision_family=request.decision_family,
                query={
                    "situation_type": request.situation_type,
                    "situation_summary": request.situation_summary,
                    "objective_text": request.objective_text,
                    "constraints": dict(request.constraints),
                    "context_snapshot": dict(request.context_snapshot),
                },
                prediction=prediction_payload(result),
                evidence_count=len(request.evidence_rows),
                latency_ms=7,
                evidence_cutoff_at=request.evidence_cutoff_at,
                evidence_revision=request.evidence_revision,
                advice_visible=False,
                # Deliberately later than `request.decision_at`, exactly as the write path's
                # second `datetime.now()` is.
                frozen_at=request.decision_at + timedelta(milliseconds=41),
            )
            persist_policy_decision(
                db,
                prediction_id=prediction_id,
                result=result,
                request=request,
                project_id=request.project_id,
                episode_key=request.episode_key,
                decision_advice_shown=False,
                candidate_option_count=len(request.candidate_options),
                opportunity_id=opportunity_id,
            )
            db.commit()

        fetch = _psycopg_fetch()
        columns = replay_row_columns(replay_inputs_present=True)
        rows = fetch(
            f"""
            SELECT {columns}
              FROM behavior_shadow_predictions p
              LEFT JOIN decision_opportunities o ON o.id = p.opportunity_id
             WHERE p.workspace_id = %s
            """,
            (_WORKSPACE,),
        )
        assert rows, "the seeded shadow row was not written; this arm would pass vacuously"

        mismatches: list[str] = []
        with_evidence = 0
        for row in rows:
            evidence, missing = replay_evidence_rows(fetch, row)
            if not evidence:
                continue
            with_evidence += 1
            replayed = replay_request_from_row(row, evidence)
            if missing:
                mismatches.append(f"{row['id']}: unresolved offered evidence ids {missing}")
            if replayed.fingerprint() != row["request_fingerprint"]:
                mismatches.append(
                    f"{row['id']}: {json.dumps(_diff(request, replayed), sort_keys=True, default=str)}"
                )

        assert with_evidence, (
            "no seeded row came back carrying offered evidence ids, so this arm did not exercise "
            "the term it exists to exercise"
        )
        assert not mismatches, (
            "a stored decision could not be reconstructed from its own row:\n  " + "\n  ".join(mismatches)
        )
    finally:
        import sqlalchemy as sa_cleanup
        from sqlalchemy.orm import Session as CleanupSession

        with CleanupSession(engine) as db:
            db.execute(
                sa_cleanup.text("DELETE FROM behavior_shadow_predictions WHERE workspace_id = :ws"),
                {"ws": _WORKSPACE},
            )
            db.execute(
                sa_cleanup.text("DELETE FROM decision_opportunities WHERE workspace_id = :ws"),
                {"ws": _WORKSPACE},
            )
            db.execute(
                sa_cleanup.text("DELETE FROM decision_observations WHERE workspace_id = :ws"),
                {"ws": _WORKSPACE},
            )
            db.commit()
