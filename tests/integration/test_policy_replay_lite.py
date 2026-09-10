"""Replay against the Lite store: the candidate set a frozen turn actually offered.

The Lite half of P4 exit gate G1's substance arm.  ``behavior_shadow_predictions`` stores
``candidate_option_count`` — a scalar — and a ``query_json`` that the freeze site builds from
``situation_type``, ``situation_summary``, ``objective_text``, ``constraints`` and
``context_snapshot`` and **never** from the options.  A replay that rebuilt the request from
``query_json['available_choices']`` therefore recovered ``()`` on every row ever written, and an
empty candidate set abstains (Y7).  "The deployed route is the evaluated route" was true in
letter and false in substance: the harness replayed a decision the turn never made.

The list was persisted the whole time.  ``_freeze_decision_opportunity_lite`` calls
``freeze_shadow_prediction`` and ``insert_opportunity(alternatives=...)`` on the same connection,
inside one transaction, keyed by the ``opportunity_id`` generated in that block — so the join
from a prospective shadow row to its opportunity is total, and reading
``o.alternatives_json`` back is the whole fix.  No column, no migration, no Lite DDL guard, and
it works on rows that already exist, which a new column would not.

Every case here drives the real FastAPI app over ``TestClient`` against a real SQLite store and
then replays what that turn wrote.  SQL observes; it never stands in for the boundary.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_api.policy_store import REPLAY_CANDIDATE_COLUMN as FULL_REPLAY_CANDIDATE_COLUMN
from tce_api.policy_store import replay_candidate_options as full_replay_candidate_options
from tce_lite_api.config import get_settings
from tce_lite_api.db import _connect
from tce_lite_api.main import app
from tce_lite_api.policy_store import (
    REPLAY_CANDIDATE_COLUMN,
    REPLAY_INPUT_COLUMN_NAMES,
    REPLAY_ROW_COLUMNS,
    replay_candidate_options,
    replay_evidence_rows,
    replay_fingerprint_terms,
    replay_offered_evidence_ids,
    replay_request_from_row,
)
from tce_shared.decision_policy import DECISION_POLICY_REVISION, AbstainReason, DecisionRequest, decide
from tce_shared.identity import credential_fingerprint

_TOKEN = "policy-replay-lite-token"
# A second, identity-bound credential.  The evidence writer must be a *verified human* or the
# rows it writes are `learning_eligible = 0` and `load_policy_evidence` cannot see them -- which
# would leave the fingerprint case seeded with evidence it never actually replays.
_HUMAN_TOKEN = "policy-replay-lite-verified-token"
_WORKSPACE = "policy-replay-lite-workspace"
_HUMAN = "policy-replay-lite-human"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "behavior_prediction_enabled",
    "behavior_shadow_evaluation_enabled",
    "behavior_autonomy_gate_enabled",
    "behavior_storage_gate_mode",
    "behavior_memory_review_enabled",
    "policy_allow_unscoped_project_evidence",
    "policy_advisor_required_families",
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
    return tmp_path / "policy-replay-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = ",".join([_TOKEN, _HUMAN_TOKEN])
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _HUMAN_TOKEN): {
                "consumer": _HUMAN,
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    settings.workspace_access_mode = "compat"
    settings.behavior_storage_gate_mode = "shadow"
    settings.behavior_memory_review_enabled = False
    settings.charter_enforcement_enabled = False
    settings.behavior_prediction_enabled = True
    settings.behavior_shadow_evaluation_enabled = True
    settings.behavior_autonomy_gate_enabled = False
    settings.policy_allow_unscoped_project_evidence = True
    settings.policy_advisor_required_families = ""
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _headers() -> dict[str, str]:
    """The executor that drives the turn, scoped to the workspace the human writes evidence in.

    Same workspace and same behavior subject as :func:`_human_headers`, because
    ``load_policy_evidence`` scopes on exactly that pair: a turn in one workspace cannot see
    evidence written in another, and a fingerprint case seeded across the boundary would
    replay an empty evidence set and pass without measuring anything.
    """

    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-TCE-Role": "executor",
        "X-TCE-Consumer": "codex-executor",
        "X-TCE-Workspace": _WORKSPACE,
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
    }


def _human_headers() -> dict[str, str]:
    """The identity-bound human credential that mints learning-eligible evidence."""

    return {
        "Authorization": f"Bearer {_HUMAN_TOKEN}",
        "X-TCE-Role": "user",
        "X-TCE-Consumer": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
    }


def _seed_approval_evidence(client: TestClient, count: int = 4) -> list[str]:
    """Write learning-eligible observations the safety question's own retrieval will return.

    ``situation_type`` is ``approval_requested`` and the family is left NULL, which is what the
    freeze site's loader asks for (``decision_family = 'safety_confirmation' OR IS NULL`` AND
    ``situation_type = 'approval_requested'``).  Without these rows every prospective row in
    this module has an empty evidence set, and ``fingerprint()`` hashes ``sorted([])`` on both
    sides -- so the fingerprint case would pass no matter how badly the offered evidence set
    were reconstructed.  That is the vacuity this seeding exists to remove.
    """

    ids: list[str] = []
    for index in range(count):
        response = client.post(
            "/v1/behavior/evidence",
            json={
                "situation_type": "approval_requested",
                "situation_summary": f"approve a destructive build cleanup {index}",
                "objective": "remove the build artifacts without losing source",
                "context_snapshot": dict(_APP_CONTEXT),
                "constraints": {"risk": "destructive"},
                "available_choices": ["confirm", "deny"],
                "selected_choice": "confirm" if index % 2 == 0 else "deny",
                "rationale": "The path is scoped to a scratch directory and the change is reversible",
                "action_taken": "delete the scratch build tree and re-run the build",
                "outcome": "build succeeded",
                "outcome_sentiment": "positive",
                "memory_class": "preference",
                "evidence_source": "explicit",
                "confidence": 0.9,
            },
            headers=_human_headers(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["learning_eligible"] is True, (
            "the seeded evidence is not learning-eligible, so `load_policy_evidence` will not "
            f"return it and the fingerprint case would be vacuous: {body}"
        )
        ids.append(str(body["observation_id"]))
    return ids


def _open_safety_question(client: TestClient) -> str:
    """Drive a turn to the safety gate, which is the Lite freeze site that offers two options.

    Returns the frozen opportunity id.
    """

    session_id = f"replay-{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/v1/takeover/step",
        json={
            "message": f"beru take over and {_HIGH_RISK_TASK}",
            "session_id": session_id,
            "task": _HIGH_RISK_TASK,
            "persona_mode": "shadow",
            "app_context": dict(_APP_CONTEXT),
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["safety_decision"] == "confirm_required", body
    opportunity_id = body.get("open_decision_opportunity_id")
    assert opportunity_id, f"the safety question must be frozen as a decision opportunity: {body}"
    return str(opportunity_id)


def _replay_rows(limit: int = 40) -> list[dict[str, Any]]:
    """Prospective shadow rows joined to the opportunity that carries their candidate set.

    The select list is :data:`tce_lite_api.policy_store.REPLAY_ROW_COLUMNS` verbatim — the one
    list every Lite replay uses — so this test cannot drift away from what a replay actually
    reads.  It carries the turn's own identity (``evidence_revision``, ``retrieval_version``,
    ``project_id``, ``episode_key``, ``model_id``, ``runtime_version``) and the four
    ``20260909_0041`` replay inputs, all of which replay used to substitute literals for.
    """

    conn: sqlite3.Connection = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT {REPLAY_ROW_COLUMNS}
              FROM behavior_shadow_predictions p
              JOIN decision_opportunities o ON o.id = p.opportunity_id
             WHERE p.prediction_stage = 'prospective'
             ORDER BY p.frozen_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _offered_evidence(row: dict[str, Any]) -> tuple[Any, ...]:
    """The observations this row's decision was offered, loaded by stored id.

    Not an unscoped ``LIMIT 40`` over ``decision_observations``: that answers "what evidence
    exists now", whose answer changes every time anything is written, so two replays of one row
    could disagree with each other and neither need match the turn.
    """

    conn: sqlite3.Connection = _connect()
    try:
        rows, missing = replay_evidence_rows(conn, row)
        assert not missing, f"offered evidence no longer resolves and the replay is incomplete: {missing}"
        return tuple(rows)
    finally:
        conn.close()


def _request_from_row(
    row: dict[str, Any],
    candidate_options: tuple[str, ...],
    *,
    evidence_rows: tuple[Any, ...] | None = None,
) -> DecisionRequest:
    """The replay assembly, parameterised on the candidate set.

    ``replay_request_from_row`` is the real assembly and reads the candidate set from the row
    itself; this wrapper overrides just that one field so a single case can build the request
    both ways — the way replay used to, and the way it must — and compare what ``decide()``
    does with each.  Everything else, identity included, comes from the row.
    """

    request = replay_request_from_row(
        row,
        _offered_evidence(row) if evidence_rows is None else evidence_rows,
    )
    return replace(request, candidate_options=candidate_options)


def test_replay_reconstructs_the_candidate_set_the_turn_offered(lite_client: TestClient) -> None:
    """Non-empty, equal to what was offered, and consistent with the scalar the write site derived.

    Three separate claims, because two of them can hold while the third fails: a reconstruction
    that is non-empty but wrong is worse than one that is empty, and a scalar that disagrees with
    the list is the defect ``candidate_option_count`` already exhibits on the Full corpus.
    """

    opportunity_ids = {_open_safety_question(lite_client) for _ in range(2)}
    rows = _replay_rows()
    assert rows, "no prospective shadow row joined to an opportunity; this case would pass vacuously"
    assert opportunity_ids <= {str(row["opportunity_id"]) for row in rows}

    non_empty: list[dict[str, Any]] = []
    problems: list[str] = []
    for row in rows:
        offered = tuple(
            str(item) for item in json.loads(str(row["candidate_options_json"] or "[]")) if str(item).strip()
        )
        rebuilt = replay_candidate_options(row)
        if rebuilt != offered:
            problems.append(f"{row['id']}: replayed {rebuilt!r} != offered {offered!r}")
        count = int(row.get("candidate_option_count") or 0)
        if count != len(offered):
            problems.append(f"{row['id']}: candidate_option_count={count} but offered {len(offered)}")
        if offered:
            non_empty.append(row)

    assert not problems, "replay rebuilt a different candidate set than the turn offered:\n  " + "\n  ".join(problems)
    assert non_empty, (
        "every prospective row joined to an empty alternatives_json, so this case cannot tell a "
        "working reconstruction from the () replay used to return"
    )

    # The regression pin: the key the broken replay read is absent on every one of these rows, so
    # a revert to `query_json['available_choices']` cannot pass here by coincidence.
    leaked = [
        row["id"]
        for row in non_empty
        if (json.loads(str(row["query_json"])) if isinstance(row["query_json"], str) else (row["query_json"] or {})).get(
            "available_choices"
        )
    ]
    assert not leaked, (
        "query_json now carries available_choices; if the Lite write site started writing it, this "
        f"case's regression pin must be re-derived rather than deleted: {leaked}"
    )

    sample = non_empty[0]
    print(  # noqa: T201 - the measurement is this gate's deliverable
        json.dumps(
            {
                "prospective_rows_joined": len(rows),
                "rows_with_non_empty_candidate_set": len(non_empty),
                "sample_shadow_id": str(sample["id"]),
                "sample_opportunity_id": str(sample["opportunity_id"]),
                "sample_candidate_options": list(replay_candidate_options(sample)),
                "sample_candidate_option_count": int(sample["candidate_option_count"] or 0),
                "sample_query_json_available_choices": json.loads(str(sample["query_json"])).get("available_choices"),
            },
            indent=2,
            sort_keys=True,
        )
    )


def test_the_old_replay_route_abstained_on_a_set_it_manufactured_itself(lite_client: TestClient) -> None:
    """The substance of the gate, not just the plumbing.

    Same live row, two assemblies.  Reading ``query_json['available_choices']`` — what replay did
    — yields an empty candidate set, and ``decide()`` abstains with ``no_candidate_match`` under
    Y7: an abstention manufactured by the harness, attributed to the turn.  Reading the joined
    ``alternatives_json`` yields the two options the human was actually shown, and the abstention
    that remains is about the *evidence corpus*, which is the honest answer on a store with no
    observations in it.
    """

    _open_safety_question(lite_client)
    rows = _replay_rows()
    assert rows

    row = next(
        (item for item in rows if json.loads(str(item["candidate_options_json"] or "[]"))),
        None,
    )
    assert row is not None, "no row carries a candidate set; the contrast below would be vacuous"

    query = json.loads(str(row["query_json"]))
    old_route = tuple(str(item) for item in (query.get("available_choices") or []) if str(item).strip())
    assert old_route == (), "the premise of this case is that the write site never wrote that key"

    before = decide(_request_from_row(row, old_route))
    after = decide(_request_from_row(row, replay_candidate_options(row)))

    assert before.abstain_reason is AbstainReason.NO_CANDIDATE_MATCH, (
        "the old replay route must be shown abstaining on its own empty set, or this case is not "
        f"measuring the defect: {before.abstain_reason}"
    )
    assert "no_candidate_options" in before.adequacy.shortfalls

    assert after.abstain_reason is not AbstainReason.NO_CANDIDATE_MATCH, (
        "the repaired replay still abstains for want of candidates, so the join recovered nothing"
    )
    assert "no_candidate_options" not in after.adequacy.shortfalls
    assert len(replay_candidate_options(row)) >= 2, "the safety question offers confirm and deny"
    # The fingerprint is what a qualification run compares; it must move when the candidate set does.
    assert (
        _request_from_row(row, old_route).fingerprint() != _request_from_row(row, replay_candidate_options(row)).fingerprint()
    ), "the request fingerprint is blind to the candidate set, which is one of the inputs it exists to bind"


def test_a_row_with_nothing_retained_reconstructs_to_empty_rather_than_to_a_guess(
    lite_client: TestClient,
) -> None:
    """Absence is reported as absence.

    A retrospective shadow row has no opportunity to join to, so nothing retained its candidate
    set and the reconstruction is ``()``.  That must stay ``()``: the promotion gate excludes a
    case offering fewer than two options, and a reader that invented a list to avoid an empty one
    would smuggle exactly the rows the gate exists to keep out.
    """

    assert replay_candidate_options({}) == ()
    assert replay_candidate_options({"candidate_options_json": None, "query_json": "{}"}) == ()
    assert replay_candidate_options({"candidate_options_json": "[]"}) == ()
    # Malformed JSON is not a candidate set either, and must not raise inside a replay loop.
    assert replay_candidate_options({"candidate_options_json": "not json"}) == ()
    assert replay_candidate_options({"candidate_options_json": '"confirm"'}) == ()
    # The write site's own blank filter, applied on the way back out.
    assert replay_candidate_options({"candidate_options_json": '["confirm", "   ", "abort"]'}) == ("confirm", "abort")


def test_full_and_lite_reconstruct_the_same_candidate_set(lite_client: TestClient) -> None:
    """Parity, on the two things that can diverge: the join and the normalisation.

    ``REPLAY_CANDIDATE_COLUMN`` is the select-list item both backends splice into their replay
    query; if the two strings differ, the two backends are reading different columns and no
    downstream comparison means anything.  ``replay_candidate_options`` then has to agree on
    rows rendered the way each backend's driver renders them — psycopg hands back ``JSONB`` as a
    decoded ``list``, sqlite3 hands back ``TEXT`` as a ``str`` — so the same logical row is fed
    in both shapes and both readers must return the same tuple.
    """

    assert REPLAY_CANDIDATE_COLUMN == FULL_REPLAY_CANDIDATE_COLUMN, (
        "Full and Lite splice different select-list items into their replay join, so they are not "
        f"reading the same column: lite={REPLAY_CANDIDATE_COLUMN!r} full={FULL_REPLAY_CANDIDATE_COLUMN!r}"
    )
    assert list(inspect.signature(replay_candidate_options).parameters) == list(
        inspect.signature(full_replay_candidate_options).parameters
    )

    _open_safety_question(lite_client)
    rows = _replay_rows()
    row = next((item for item in rows if json.loads(str(item["candidate_options_json"] or "[]"))), None)
    assert row is not None

    lite_shape = dict(row)
    # The same row as Postgres would hand it back: JSONB decoded, query_json decoded.
    full_shape = dict(row)
    full_shape["candidate_options_json"] = json.loads(str(row["candidate_options_json"]))
    full_shape["query_json"] = json.loads(str(row["query_json"]))

    expected = replay_candidate_options(lite_shape)
    assert expected, "the sample row must carry a candidate set for this comparison to bite"
    for name, shape in (("lite_shape", lite_shape), ("full_shape", full_shape)):
        assert replay_candidate_options(shape) == expected, name
        assert full_replay_candidate_options(shape) == expected, name

    # And the fallback ordering is the same on both sides: an explicitly empty joined list does
    # not silently fall through to a populated query_json on one backend and not the other.
    fallback_row = {"candidate_options_json": [], "query_json": {"available_choices": ["a", "b"]}}
    assert replay_candidate_options(fallback_row) == full_replay_candidate_options(fallback_row) == ("a", "b")


# --------------------------------------------------------------------------------------
# Z3(c) — a stored row replays to its own fingerprint, and does so with evidence in it
# --------------------------------------------------------------------------------------


def test_a_stored_row_replays_to_the_fingerprint_the_turn_recorded(lite_client: TestClient) -> None:
    """G1's Lite runtime arm, against a row with evidence in it.

    ``request_fingerprint`` is written by the live turn and recomputed here from the stored row
    alone.  Equality is the whole property P4 claims: the evaluated route is the deployed route.
    Comparing ``decision_policy_revision`` instead — which an earlier draft did — compares a
    module constant to itself and would hold while every input diverged.

    The seeding is not decoration.  ``fingerprint()`` hashes the sorted **offered** evidence ids,
    and until ``20260909_0041`` that set was not persisted at all: only the subset the decision
    *cited* reached ``policy_json``.  On a corpus where every prospective row has zero evidence
    both sides hash ``sorted([])``, and the case passes however wrong the reconstruction is.  So
    this drives a turn whose retrieval actually returns rows, and asserts that it did.
    """

    seeded = _seed_approval_evidence(lite_client, count=4)
    opportunity_id = _open_safety_question(lite_client)

    rows = _replay_rows()
    row = next((item for item in rows if str(item["opportunity_id"]) == opportunity_id), None)
    assert row is not None, f"the frozen opportunity {opportunity_id} has no prospective shadow row"

    stored_fingerprint = str(row["request_fingerprint"] or "")
    assert stored_fingerprint, "the live turn stored no request_fingerprint; there is nothing to replay against"

    offered_ids = replay_offered_evidence_ids(row)
    assert offered_ids, (
        "the row retained no offered evidence ids, so this case would compare sorted([]) with "
        f"sorted([]) and pass vacuously. Seeded observations: {seeded}"
    )
    assert set(offered_ids) <= set(seeded), (
        f"the turn was offered evidence this case did not seed: {sorted(set(offered_ids) - set(seeded))}"
    )

    replayed = replay_request_from_row(row, _offered_evidence(row))
    assert replayed.evidence_rows, "the offered ids resolved to no observations; the replay has no evidence"

    if replayed.fingerprint() != stored_fingerprint:
        # The live request is not available here by construction — that is the point — so the
        # diff is between what the row says and what the replay rebuilt from it, term by term.
        fields = replay_fingerprint_terms(replayed)
        fields["stored_fingerprint"] = stored_fingerprint
        fields["replayed_fingerprint"] = replayed.fingerprint()
        fields["stored_candidate_option_count"] = int(row["candidate_option_count"] or 0)
        fields["stored_frozen_at"] = str(row["frozen_at"])
        fields["stored_decision_at"] = str(row["decision_at"])
        fields["stored_offered_evidence_ids"] = sorted(offered_ids)
        pytest.fail(
            "the replayed request does not reproduce the fingerprint the turn recorded "
            f"(revision {DECISION_POLICY_REVISION}): "
            + json.dumps(fields, indent=2, sort_keys=True, default=str)
        )

    # The negatives.  Each is one of the substitutions the pre-Z3 replay actually made; if any of
    # them still reproduces the stored fingerprint, this case cannot detect that substitution.
    assert replay_request_from_row(row, ()).fingerprint() != stored_fingerprint, (
        "an empty evidence set reproduces the stored fingerprint, so this case cannot tell a "
        "replay that reconstructed the offered evidence from one that dropped it"
    )
    for field, substitute in (
        ("retrieval_version", "replay-v1"),
        ("evidence_revision", "lite-replay"),
        ("decision_at", None),
    ):
        stamped = {**row, field: substitute}
        assert replay_request_from_row(stamped, _offered_evidence(row)).fingerprint() != stored_fingerprint, (
            f"substituting {substitute!r} for the turn's own {field} still reproduces the stored "
            "fingerprint, so that substitution would have gone undetected"
        )

    print(  # noqa: T201 - the measurement is this gate's deliverable
        json.dumps(
            {
                "shadow_id": str(row["id"]),
                "opportunity_id": str(row["opportunity_id"]),
                "stored_fingerprint": stored_fingerprint,
                "replayed_fingerprint": replayed.fingerprint(),
                "offered_evidence_ids": sorted(offered_ids),
                "candidate_options": list(replay_candidate_options(row)),
                "retrieval_version": replayed.retrieval_version,
                "evidence_revision": replayed.evidence_revision,
                "episode_key": replayed.episode_key,
                "project_id": replayed.project_id,
                "decision_at": replayed.decision_at.isoformat(),
                "frozen_at": str(row["frozen_at"]),
            },
            indent=2,
            sort_keys=True,
        )
    )


def test_replay_decides_over_the_evidence_the_turn_was_offered(lite_client: TestClient) -> None:
    """The substance behind the fingerprint: the cited subset is not the offered set.

    ``policy_json.evidence_observation_ids`` holds what the decision *cited*.  Rebuilding from it
    — the only thing available before ``20260909_0041`` — hands ``decide()`` a strictly smaller
    corpus than the turn saw, so the adequacy numbers, the ranking and the abstention are all
    computed against evidence the turn did not have.
    """

    _seed_approval_evidence(lite_client, count=4)
    opportunity_id = _open_safety_question(lite_client)
    row = next(item for item in _replay_rows() if str(item["opportunity_id"]) == opportunity_id)

    policy_json = json.loads(str(row["policy_json"]))
    cited = [str(item) for item in (policy_json.get("evidence_observation_ids") or [])]
    offered = list(replay_offered_evidence_ids(row))
    assert offered, "nothing was retained; the comparison below would be vacuous"
    assert set(cited) <= set(offered), (
        f"the decision cited evidence it was never offered: {sorted(set(cited) - set(offered))}"
    )

    from_offered = decide(replay_request_from_row(row, _offered_evidence(row)))
    cited_row = {**row, "offered_evidence_ids_json": json.dumps(cited)}
    from_cited = decide(replay_request_from_row(cited_row, _offered_evidence(cited_row)))

    assert from_offered.adequacy.neighbour_count == len(offered), (
        "replaying from the offered set did not put the turn's own evidence in front of decide(): "
        f"{from_offered.adequacy.neighbour_count} != {len(offered)}"
    )
    assert from_cited.adequacy.neighbour_count == len(cited)
    if len(cited) != len(offered):
        assert from_cited.adequacy.neighbour_count != from_offered.adequacy.neighbour_count, (
            "the cited subset and the offered set produced the same corpus, so this case is not "
            "measuring the difference between them"
        )


def test_full_and_lite_reconstruct_the_same_request_from_an_equivalent_row(
    lite_client: TestClient,
) -> None:
    """Parity on the reconstruction, not just on the join.

    Four things can diverge here and none of them raises: the two backends can name the replay
    columns differently, they can *decode the same column differently* (psycopg hands ``JSONB``
    back as a decoded list, sqlite3 hands ``TEXT`` back as a ``str``), they can read a different
    set of fields off the row, and their sqlite/postgres select lists can fall out of step with
    the migration.  The column names are asserted against Full's own tuple and against Full's
    **ORM**, because that is the artifact the Full migration and the Full replay both read: if
    it carries no column of this name, the Lite corpus is unreadable from Full and every
    cross-backend comparison downstream is meaningless.
    """

    from tce_api.models import BehaviorShadowPrediction
    from tce_api.policy_store import REPLAY_INPUT_COLUMN_NAMES as FULL_REPLAY_INPUT_COLUMN_NAMES
    from tce_api.policy_store import replay_offered_evidence_ids as full_replay_offered_evidence_ids
    from tce_api.policy_store import replay_request_from_row as full_replay_request_from_row

    assert REPLAY_INPUT_COLUMN_NAMES == FULL_REPLAY_INPUT_COLUMN_NAMES, (
        "Full and Lite name the 20260909_0041 replay columns differently, so a Lite row cannot be "
        f"read by a Full replay: lite={REPLAY_INPUT_COLUMN_NAMES} full={FULL_REPLAY_INPUT_COLUMN_NAMES}"
    )
    full_columns = {column.name for column in BehaviorShadowPrediction.__table__.columns}
    missing = [name for name in REPLAY_INPUT_COLUMN_NAMES if name not in full_columns]
    assert not missing, f"Full's behavior_shadow_predictions ORM carries no column named {missing}"
    for name in REPLAY_INPUT_COLUMN_NAMES:
        assert f"p.{name}" in REPLAY_ROW_COLUMNS, (
            f"{name} is not selected by the Lite REPLAY_ROW_COLUMNS, so a Lite replay would "
            "substitute a literal for it"
        )
    assert list(inspect.signature(replay_offered_evidence_ids).parameters) == list(
        inspect.signature(full_replay_offered_evidence_ids).parameters
    )
    assert list(inspect.signature(replay_request_from_row).parameters) == list(
        inspect.signature(full_replay_request_from_row).parameters
    )

    _seed_approval_evidence(lite_client, count=3)
    opportunity_id = _open_safety_question(lite_client)
    row = next(item for item in _replay_rows() if str(item["opportunity_id"]) == opportunity_id)
    evidence = _offered_evidence(row)

    lite_shape = dict(row)
    # The same logical row as psycopg would hand it back: JSON columns decoded, timestamps as
    # aware datetimes, the boolean as a bool.
    full_shape = dict(row)
    full_shape["offered_evidence_ids_json"] = json.loads(str(row["offered_evidence_ids_json"]))
    full_shape["candidate_options_json"] = json.loads(str(row["candidate_options_json"]))
    full_shape["query_json"] = json.loads(str(row["query_json"]))
    full_shape["policy_json"] = json.loads(str(row["policy_json"]))
    full_shape["decision_at"] = datetime.fromisoformat(str(row["decision_at"]))
    full_shape["evidence_cutoff_at"] = (
        datetime.fromisoformat(str(row["evidence_cutoff_at"])) if row["evidence_cutoff_at"] else None
    )
    full_shape["advisor_present"] = bool(row["advisor_present"])

    expected_ids = replay_offered_evidence_ids(lite_shape)
    assert expected_ids, "the sample row retained no offered evidence; the comparison would be vacuous"

    stored_fingerprint = str(row["request_fingerprint"] or "")
    # The JSON readers are driver-agnostic on both sides and must agree on either rendering.
    for name, shape in (("lite_shape", lite_shape), ("full_shape", full_shape)):
        assert replay_offered_evidence_ids(shape) == expected_ids, name
        assert full_replay_offered_evidence_ids(shape) == expected_ids, name
        assert replay_candidate_options(shape) == replay_candidate_options(lite_shape), name
        assert full_replay_candidate_options(shape) == replay_candidate_options(lite_shape), name

    # The whole request, each backend reading the row shape its own driver produces — which is
    # what "equivalent rows" means, and the claim that actually matters: a Full replay of a Full
    # row and a Lite replay of the equivalent Lite row must reconstruct the same request.  A
    # reader that agrees on the ids but disagrees on the identity fields is still two
    # reconstructions.  (Lite is additionally required to read the decoded shape, since Lite's
    # own readers are the ones a cross-backend harness would point at either corpus.)
    lite_terms = replay_fingerprint_terms(replay_request_from_row(lite_shape, evidence))
    for name, terms in (
        ("full reader / decoded row", replay_fingerprint_terms(full_replay_request_from_row(full_shape, evidence))),
        ("lite reader / decoded row", replay_fingerprint_terms(replay_request_from_row(full_shape, evidence))),
    ):
        assert lite_terms == terms, (
            f"Full and Lite reconstructed different requests from equivalent rows ({name}): "
            + json.dumps(
                {key: (lite_terms[key], terms[key]) for key in lite_terms if lite_terms[key] != terms[key]},
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    assert replay_request_from_row(lite_shape, evidence).fingerprint() == stored_fingerprint
    assert replay_request_from_row(full_shape, evidence).fingerprint() == stored_fingerprint
    assert full_replay_request_from_row(full_shape, evidence).fingerprint() == stored_fingerprint

    # A row written before the column existed reconstructs to (), on both sides, rather than to a
    # guess: absence is reported as absence.
    assert replay_offered_evidence_ids({}) == full_replay_offered_evidence_ids({}) == ()
    for absent in (
        {"offered_evidence_ids_json": None},
        {"offered_evidence_ids_json": "[]"},
        {"offered_evidence_ids_json": "not json"},
        {"offered_evidence_ids_json": '"one-id"'},
    ):
        assert replay_offered_evidence_ids(absent) == full_replay_offered_evidence_ids(absent) == ()
