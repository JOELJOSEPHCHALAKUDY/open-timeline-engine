"""Lite verification store — the twin of ``tce_api.verification_store`` (U5).

Two properties this module exists to hold, and neither is a convention:

1. **The API computes the verdict.**  ``decide_verdict`` is called here, from raw evidence plus the
   frozen criteria row.  The runner posts ``CheckResultPayload`` objects — exit codes and hashes —
   and has no field in which to assert a verdict.
2. **This is the only writer of a non-``'unverified'`` ``directive_executions.verification_state``.**
   The report path's ``verification_state = 'unverified'`` literal stays exactly where it is; the
   comment there ("only a verification path may set anything else") becomes true because of this
   file.  ``tests/unit/test_verification_authority.py`` asserts the owning-file set is exactly the
   two ``verification_store.py`` modules.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from tce_shared.charter import CHARTER_POLICY_REVISION
from tce_shared.execution_transitions import is_terminal
from tce_shared.task_state import (
    TaskStateEvent,
    TaskStateEventKind,
    stamp_verification_provenance,
)
from tce_shared.verification import (
    DECIDING_FILE_PATHS,
    VERIFICATION_METHOD_EVIDENCE_GRADED,
    VERIFICATION_SCHEMA_VERSION,
    VERIFICATION_STATE_FOR_VERDICT,
    AcceptanceCheck,
    AcceptanceCriteria,
    CriteriaFrozen,
    VerificationEvidence,
    VerificationOutcome,
    acceptance_check_from_json,
    acceptance_check_to_json,
    check_result_to_json,
    corpus_digest,
    criteria_digest,
    decide_verdict,
    validate_checks,
)

from .config import Settings
from .task_state_store import (
    apply_task_state_events,
    directive_contract_revision,
    insert_task_verification,
    load_task_state,
    run_cas_section,
)

_CRITERIA_COLUMNS = """
    id, workspace_id, owner_id, task_id, directive_id, charter_id, checks_json, criteria_digest,
    corpus_digest, corpus_manifest_json, frozen_at, frozen_by, policy_revision, schema_version
"""


def _manifest_pairs(value: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    raw = value if isinstance(value, list) else json.loads(str(value or "[]"))
    for entry in raw:
        items = list(entry)
        if len(items) >= 2:
            pairs.append((str(items[0]), str(items[1])))
    return pairs


def _criteria_from_row(row: sqlite3.Row) -> AcceptanceCriteria:
    checks = tuple(acceptance_check_from_json(item) for item in json.loads(str(row["checks_json"] or "[]")))
    return AcceptanceCriteria(
        criteria_id=str(row["id"]),
        directive_id=str(row["directive_id"]),
        checks=checks,
        criteria_digest=str(row["criteria_digest"] or ""),
        corpus_digest=str(row["corpus_digest"] or ""),
        corpus_manifest=tuple(_manifest_pairs(row["corpus_manifest_json"])),
        frozen_at=datetime.fromisoformat(str(row["frozen_at"])),
        frozen_by=str(row["frozen_by"] or ""),
        policy_revision=str(row["policy_revision"] or CHARTER_POLICY_REVISION),
    )


def freeze_acceptance_criteria(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str | None,
    directive_id: str,
    charter_id: str | None,
    checks: Sequence[AcceptanceCheck],
    corpus_manifest: Sequence[tuple[str, str]],
    frozen_by: str,
    settings: Settings,
    now: datetime,
) -> AcceptanceCriteria:
    """The ONLY INSERT into ``acceptance_criteria`` in Lite.  There is no UPDATE and no DELETE path.

    ``UNIQUE (directive_id)`` turns a second freeze into ``CriteriaFrozen`` (409) rather than a
    silent overwrite — the whole point of freezing is that the implementing agent cannot move the
    goalposts after seeing the checks.

    **Stated limitation on the corpus check.**  §7.2 asks for
    ``corpus_manifest_covers(manifest, DECIDING_FILE_PATHS)``.  The API process has no access to the
    task clone, so it cannot know which deciding files exist there; requiring all nine would refuse
    every real freeze (this repo has no ``tox.ini``).  What is enforced here is the satisfiable half:
    the manifest must be non-empty and must cover **at least one** deciding file, so a manifest that
    could not possibly detect a ``pyproject.toml`` swap is refused.  Full coverage is asserted by the
    supervisor, which builds the manifest from the clone with those prefixes.  This is weaker than
    §7.2 states and is documented as such rather than advertised as the stronger check.
    """
    normalised = validate_checks(
        checks,
        max_checks=int(settings.verification_max_checks),
        max_timeout_seconds=int(settings.verification_check_timeout_seconds),
    )
    manifest = [(str(path), str(digest)) for path, digest in corpus_manifest]
    covered = {path for path, _digest in manifest}
    if not manifest or not (covered & set(DECIDING_FILE_PATHS)):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "corpus_manifest_incomplete",
                "message": "the corpus manifest must cover at least one build-deciding file",
                "deciding_file_paths": list(DECIDING_FILE_PATHS),
            },
        )

    def _body() -> AcceptanceCriteria:
        existing = conn.execute(
            "SELECT 1 FROM acceptance_criteria WHERE directive_id = ? LIMIT 1",
            (str(directive_id),),
        ).fetchone()
        if existing is not None:
            raise CriteriaFrozen(str(directive_id))
        criteria_id = str(uuid.uuid4())
        digest = criteria_digest(normalised)
        corpus = corpus_digest(manifest)
        conn.execute(
            """
            INSERT INTO acceptance_criteria (
                id, workspace_id, owner_id, task_id, directive_id, charter_id, checks_json,
                criteria_digest, corpus_digest, corpus_manifest_json, frozen_at, frozen_by,
                policy_revision, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                criteria_id,
                workspace_id,
                owner_id,
                task_id,
                str(directive_id),
                (str(charter_id) if charter_id else None),
                json.dumps([acceptance_check_to_json(check) for check in normalised]),
                digest,
                corpus,
                json.dumps([list(entry) for entry in manifest]),
                now.isoformat(),
                str(frozen_by),
                CHARTER_POLICY_REVISION,
                VERIFICATION_SCHEMA_VERSION,
            ),
        )
        return AcceptanceCriteria(
            criteria_id=criteria_id,
            directive_id=str(directive_id),
            checks=normalised,
            criteria_digest=digest,
            corpus_digest=corpus,
            corpus_manifest=tuple(manifest),
            frozen_at=now,
            frozen_by=str(frozen_by),
            policy_revision=CHARTER_POLICY_REVISION,
        )

    return run_cas_section(conn, _body)


def load_acceptance_criteria(conn: sqlite3.Connection, *, directive_id: str) -> AcceptanceCriteria | None:
    row = conn.execute(
        f"SELECT {_CRITERIA_COLUMNS} FROM acceptance_criteria WHERE directive_id = ? LIMIT 1",
        (str(directive_id),),
    ).fetchone()
    return _criteria_from_row(row) if row is not None else None




def _project_graded_verification(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    directive_id: str,
    verification_id: str,
    outcome: VerificationOutcome,
    state: str,
    runner_principal: str,
    settings: Settings,
    now: datetime,
) -> None:
    """Carry the GRADED verdict into the durable task-state projection.

    Why this exists at all: ``derive_status`` R9 is the ONLY route to ``DONE`` and it reads
    ``latest_verification``, which is written by exactly one event kind --
    ``VERIFICATION_RECORDED``.  Before this function the only producer of that event was the
    execution report, whose state is pinned to ``'unverified'`` because it is the implementing
    agent's claim about its own work.  So the honest path graded a verdict, wrote it to
    ``verification_results`` and ``directive_executions.verification_state``, and the projection
    never heard about it: nothing could legitimately reach ``DONE``.

    Three properties, none of them incidental:

    * **Same transaction as the verification row.**  The caller already holds the
      ``BEGIN IMMEDIATE`` bracket (``record_verification`` runs its whole body inside
      ``run_cas_section``), and this runs inside it rather than opening its own.  Nothing is
      swallowed: a failure here rolls the ``verification_results`` INSERT back with it, so a
      recorded verdict and the projection can never disagree.
    * **S5 provenance comes from the DIRECTIVE, not the clock.**  The ref is stamped with the
      contract revision the graded directive's own work happened under
      (``directive_contract_revision``), through the shared ``stamp_verification_provenance``.
      Reading the projection's current revision here instead would make the ref assert "I am
      evidence for whatever the contract is now": an objective restated while this verification
      was in flight would silently re-badge old evidence as evidence for the new contract, and R9
      would release a wholly unverified objective to ``DONE``.  An objective restated after the
      work bumps the revision, the directive's stamp does not move, and the ref goes stale --
      which is the guard, not a bug.
    * **Every verdict reaches the projection, not just the passing one.**  ``failed`` and the
      ``'unverified'`` that ``inconclusive`` maps to are recorded the same way; they simply are
      not in ``VERIFICATION_PASS_STATES``, so R9 declines and the task holds at
      ``AWAITING_VERIFICATION``.  The ``advisory`` flag on the response is a label about the
      reviewer model and is deliberately not consulted here -- it gates nothing.

    A session with no ``task_states`` row has no projection to move, so there is nothing to emit;
    the verification row still stands on its own.
    """
    if not bool(getattr(settings, "task_state_enabled", True)):
        return
    if not session_id:
        return
    loaded = load_task_state(conn, workspace_id=workspace_id, owner_id=owner_id, task_id=session_id)
    if loaded is None:
        return
    projection = loaded[0]
    ref: dict[str, Any] = {
        "verification_id": verification_id,
        "state": state,
        "method": VERIFICATION_METHOD_EVIDENCE_GRADED,
        "recorded_at": now,
        "evidence_event_ids": [],
        "summary": f"{outcome.verdict}: {outcome.reason}"[:500],
    }
    stamp_verification_provenance(
        ref,
        projection=projection,
        directive_id=directive_id,
        # The stamp comes from the DIRECTIVE, never from the clock -- see
        # ``stamp_verification_provenance``. Grading can land arbitrarily long after the work,
        # and an objective restated in between must not silently re-badge this evidence as
        # evidence for the new contract.
        work_contract_revision=directive_contract_revision(
            conn,
            workspace_id=workspace_id,
            owner_id=owner_id,
            task_id=session_id,
            directive_id=directive_id,
        ),
    )
    insert_task_verification(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        task_id=session_id,
        recorded_by=runner_principal,
        ref=ref,
        now=now,
    )
    apply_task_state_events(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=(projection.session_id or session_id),
        task_id=session_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.VERIFICATION_RECORDED,
                contract_revision=int(projection.contract_revision),
                payload=ref,
                occurred_at=now,
                actor=runner_principal,
                directive_id=directive_id,
            )
        ],
        now=now,
        expected_revision=None,
        retry_once=False,
    )


def record_verification(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    directive_id: str,
    evidence: VerificationEvidence,
    runner_principal: str,
    settings: Settings,
    now: datetime,
) -> tuple[VerificationOutcome, dict[str, Any]]:
    """Grade the evidence and write the state.  ``runner_principal`` is derived from auth by the
    route; it is never read from the body.

    The ``verification_results`` INSERT happens on **every** path, including a refusal, because a
    refusal that leaves no row is a dropped request rather than durable evidence.
    """
    if not bool(settings.verification_enabled):
        raise HTTPException(status_code=503, detail={"error": "verification_disabled"})
    if str(runner_principal) != str(settings.verification_runner_principal):
        raise HTTPException(
            status_code=403,
            detail={"error": "unknown_runner", "message": "the authenticated identity is not the configured verification runner"},
        )

    def _body() -> tuple[VerificationOutcome, dict[str, Any]]:
        row = conn.execute(
            """
            SELECT directive_id, session_id, state, claimed_executor
            FROM directive_executions
            WHERE directive_id = ? AND workspace_id = ? AND user_id = ?
            LIMIT 1
            """,
            (str(directive_id), workspace_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail={"error": "unknown_directive"})
        if not is_terminal(str(row["state"])):
            raise HTTPException(
                status_code=409,
                detail={"error": "directive_not_terminal", "message": "a verification only means anything after a report"},
            )
        criteria = load_acceptance_criteria(conn, directive_id=str(directive_id))
        if criteria is None:
            raise HTTPException(status_code=409, detail={"error": "criteria_not_frozen"})
        outcome = decide_verdict(
            results=evidence.results,
            criteria=criteria,
            observed_corpus_digest=evidence.observed_corpus_digest,
            runner_principal=str(runner_principal),
            executing_identity=str(row["claimed_executor"] or ""),
        )
        verification_id = str(uuid.uuid4())
        reviewer_json: str | None = None
        if bool(settings.verification_reviewer_model_enabled) and evidence.reviewer_model is not None:
            reviewer_json = json.dumps(dict(evidence.reviewer_model))
        conn.execute(
            """
            INSERT INTO verification_results (
                id, workspace_id, owner_id, task_id, directive_id, criteria_id,
                criteria_digest_at_run, corpus_digest_at_run, criteria_digest_match,
                corpus_digest_match, verdict, reason, runner_principal, executing_identity,
                platform, commit_sha, tree_sha, checks_json, reviewer_model_json, recorded_at,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verification_id,
                workspace_id,
                user_id,
                None,
                str(directive_id),
                criteria.criteria_id,
                criteria.criteria_digest,
                str(evidence.observed_corpus_digest),
                1 if outcome.criteria_digest_match else 0,
                1 if outcome.corpus_digest_match else 0,
                outcome.verdict,
                outcome.reason,
                str(runner_principal),
                str(row["claimed_executor"] or ""),
                str(evidence.platform or ""),
                evidence.commit_sha,
                evidence.tree_sha,
                json.dumps([check_result_to_json(result) for result in evidence.results]),
                reviewer_json,
                now.isoformat(),
                VERIFICATION_SCHEMA_VERSION,
            ),
        )
        state = VERIFICATION_STATE_FOR_VERDICT[outcome.verdict]
        conn.execute(
            """
            UPDATE directive_executions
               SET verification_state = ?, updated_at = ?
             WHERE directive_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (state, now.isoformat(), str(directive_id), workspace_id, user_id),
        )
        # The projection is the only thing R9 reads. Writing the verdict to
        # ``directive_executions`` and stopping there is what left the honest path unable to
        # reach ``DONE``; this is inside the same bracket, so the two cannot disagree.
        _project_graded_verification(
            conn,
            workspace_id=workspace_id,
            owner_id=user_id,
            session_id=str(row["session_id"] or ""),
            directive_id=str(directive_id),
            verification_id=verification_id,
            outcome=outcome,
            state=state,
            runner_principal=str(runner_principal),
            settings=settings,
            now=now,
        )
        response = {
            "verification_id": verification_id,
            "directive_id": str(directive_id),
            "verdict": outcome.verdict,
            "reason": outcome.reason,
            "verification_state": state,
            "criteria_digest_at_run": criteria.criteria_digest,
            "corpus_digest_at_run": str(evidence.observed_corpus_digest),
            "criteria_digest_match": outcome.criteria_digest_match,
            "corpus_digest_match": outcome.corpus_digest_match,
            "runner_principal": str(runner_principal),
            "executing_identity": str(row["claimed_executor"] or ""),
            "platform": str(evidence.platform or ""),
            "commit_sha": evidence.commit_sha,
            "tree_sha": evidence.tree_sha,
            # The reviewer model, when recorded at all, is labelled advisory: decide_verdict has no
            # parameter for it and never saw it.
            "advisory": True,
            "recorded_at": now.isoformat(),
            "schema_version": VERIFICATION_SCHEMA_VERSION,
        }
        return (outcome, response)

    return run_cas_section(conn, _body)


def verification_stats_for_session(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> tuple[int, int]:
    """``(runs, passed)`` read from ``verification_results`` — the twin of
    ``tce_api.verification_store.verification_stats_for_session``.

    Never from an executor's own ``meta["verification"]`` booleans: those are the implementer's
    assertion about itself, and the autonomy band is exactly the number an implementer has a motive
    to move.  A run counts because a verifier posted evidence and this module graded it.
    """
    row = conn.execute(
        """
        SELECT COUNT(1) AS runs,
               SUM(CASE WHEN v.verdict = 'passed' THEN 1 ELSE 0 END) AS passed
        FROM verification_results v
        JOIN directive_executions d ON d.directive_id = v.directive_id
        WHERE v.workspace_id = ?
          AND d.user_id = ?
          AND d.session_id = ?
        """,
        (workspace_id, user_id, session_id),
    ).fetchone()
    if row is None:
        return (0, 0)
    return (int(row["runs"] or 0), int(row["passed"] or 0))
