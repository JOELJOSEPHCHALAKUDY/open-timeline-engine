"""Acceptance criteria and verification results for the FULL backend.

U5, stated once: **the API computes the verdict and is the sole writer of
``directive_executions.verification_state``.**  The runner submits raw evidence — commands, exit
codes, output hashes, a redacted excerpt, the observed corpus digest, the platform string — and
nothing else.  ``VerificationResultRequest`` carries no verdict, no reason, no digest-match flags
and no ``runner_principal``; the principal is derived from the authenticated identity.

An EMPTY criteria set FAILS.  It is checked three times: ``Field(min_length=1)`` on the wire,
``validate_checks`` at the freeze, and ``decide_verdict``'s first clause at the verdict.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
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
    CriteriaInvalid,
    VerificationEvidence,
    VerificationOutcome,
    acceptance_check_from_json,
    acceptance_check_to_json,
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
)


def _criteria_from_row(row: Any) -> AcceptanceCriteria:
    checks_raw = row["checks_json"]
    if isinstance(checks_raw, str):
        checks_raw = json.loads(checks_raw)
    manifest_raw = row["corpus_manifest_json"]
    if isinstance(manifest_raw, str):
        manifest_raw = json.loads(manifest_raw)
    return AcceptanceCriteria(
        criteria_id=str(row["id"]),
        directive_id=str(row["directive_id"]),
        checks=tuple(acceptance_check_from_json(item) for item in (checks_raw or [])),
        criteria_digest=str(row["criteria_digest"] or ""),
        corpus_digest=str(row["corpus_digest"] or ""),
        corpus_manifest=tuple((str(item[0]), str(item[1])) for item in (manifest_raw or []) if len(item) >= 2),
        frozen_at=row["frozen_at"],
        frozen_by=str(row["frozen_by"] or ""),
        policy_revision=str(row["policy_revision"] or ""),
    )


def freeze_acceptance_criteria(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str | None,
    directive_id: uuid.UUID,
    charter_id: uuid.UUID | None,
    checks: Sequence[AcceptanceCheck],
    corpus_manifest: Sequence[tuple[str, str]],
    frozen_by: str,
    settings: Settings,
    now: datetime,
) -> AcceptanceCriteria:
    """The ONLY INSERT into ``acceptance_criteria`` in the tree.

    There is no UPDATE and no DELETE path in either backend; a second freeze is
    ``CriteriaFrozen`` (409) rather than a silent overwrite, which is what keeps the criteria out
    of the implementing agent's reach.

    **Stated limitation on the corpus check, identical to Lite's.**  §7.2 asks for
    ``corpus_manifest_covers(manifest, DECIDING_FILE_PATHS)``.  The API process has no access to the
    task clone, so it cannot know which deciding files exist there; requiring all nine would refuse
    every real freeze (this repo has no ``tox.ini``).  What is enforced here is the satisfiable half:
    the manifest must be non-empty and must cover **at least one** deciding file, so a manifest that
    could not possibly detect a ``pyproject.toml`` swap is refused.  Full coverage is asserted by the
    supervisor, which builds the manifest from the clone with those prefixes.  This is weaker than
    §7.2 states and is documented as such rather than advertised as the stronger check.
    """
    if not bool(settings.verification_enabled):
        raise HTTPException(status_code=503, detail={"error": "verification_disabled"})
    try:
        normalised = validate_checks(
            checks,
            max_checks=int(settings.verification_max_checks),
            max_timeout_seconds=int(settings.verification_check_timeout_seconds),
        )
    except CriteriaInvalid as exc:
        raise HTTPException(status_code=422, detail={"error": "criteria_invalid", "field": exc.field, "message": str(exc)}) from exc
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
    criteria_id = uuid.uuid4()
    digest_value = criteria_digest(normalised)
    corpus_value = corpus_digest(manifest)
    try:
        db.execute(
            text(
                """
                INSERT INTO acceptance_criteria(
                  id, workspace_id, owner_id, task_id, directive_id, charter_id, checks_json,
                  criteria_digest, corpus_digest, corpus_manifest_json, frozen_at, frozen_by,
                  policy_revision, schema_version
                )
                VALUES(
                  :id, :workspace_id, :owner_id, :task_id, :directive_id, :charter_id,
                  CAST(:checks AS JSONB), :criteria_digest, :corpus_digest,
                  CAST(:corpus_manifest AS JSONB), :frozen_at, :frozen_by, :policy_revision,
                  :schema_version
                )
                """
            ),
            {
                "id": criteria_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "task_id": task_id,
                "directive_id": directive_id,
                "charter_id": charter_id,
                "checks": json.dumps([acceptance_check_to_json(check) for check in normalised]),
                "criteria_digest": digest_value,
                "corpus_digest": corpus_value,
                "corpus_manifest": json.dumps([list(item) for item in manifest]),
                "frozen_at": now,
                "frozen_by": frozen_by,
                "policy_revision": CHARTER_POLICY_REVISION,
                "schema_version": VERIFICATION_SCHEMA_VERSION,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CriteriaFrozen(str(directive_id)) from exc
    return AcceptanceCriteria(
        criteria_id=str(criteria_id),
        directive_id=str(directive_id),
        checks=normalised,
        criteria_digest=digest_value,
        corpus_digest=corpus_value,
        corpus_manifest=tuple(manifest),
        frozen_at=now,
        frozen_by=frozen_by,
        policy_revision=CHARTER_POLICY_REVISION,
    )


def load_acceptance_criteria(db: Session, *, directive_id: uuid.UUID) -> AcceptanceCriteria | None:
    row = db.execute(
        text(
            """
            SELECT id, directive_id, checks_json, criteria_digest, corpus_digest,
                   corpus_manifest_json, frozen_at, frozen_by, policy_revision
            FROM acceptance_criteria
            WHERE directive_id = :directive_id
            LIMIT 1
            """
        ),
        {"directive_id": directive_id},
    ).mappings().first()
    if row is None:
        return None
    return _criteria_from_row(row)


def _record_graded_task_state(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    directive_id: uuid.UUID,
    verification_id: uuid.UUID,
    outcome: VerificationOutcome,
    state: str,
    runner_principal: str,
    settings: Settings,
    now: datetime,
) -> None:
    """Carry the GRADED verdict into the task-state projection R9 reads.

    The provenance stamp comes from the graded DIRECTIVE's own recorded history
    (``directive_contract_revision``), never from the projection at grading time: a stamp read
    from the projection is true of every verification whenever it is asked, so an objective
    restated while a verification was in flight would re-badge the old contract's evidence as the
    new contract's and R9 would release an unverified objective to ``DONE``.

    ``decide_verdict`` is the only honest producer of a verification state, but until this call
    existed its verdict stopped at ``verification_results`` and
    ``directive_executions.verification_state``: the projection's ``latest_verification`` stayed
    whatever the implementing agent's own report had left there -- pinned to ``unverified`` -- so
    no task could reach ``DONE`` by any route. Emitting ``VERIFICATION_RECORDED`` here, stamped
    through the same ``stamp_verification_provenance`` the report path uses, is what closes it.

    Runs in the caller's transaction, before its ``COMMIT``, so a recorded verdict and the
    projection can never disagree: either both land or neither does.

    Every verdict is emitted, not only ``passed``. ``failed`` and ``inconclusive`` reach the
    projection as evidence and simply do not satisfy ``ref.state in VERIFICATION_PASS_STATES``.
    The response's ``advisory`` flag is a label about the reviewer model, not a gate, and is
    deliberately not consulted here.
    """
    if not bool(getattr(settings, "task_state_enabled", True)):
        return
    if not session_id:
        return
    loaded = load_task_state(db, workspace_id=workspace_id, owner_id=user_id, task_id=session_id)
    if loaded is None:
        # No projection means there is nothing for R9 to read and nothing to move. Grading must
        # not CONJURE a task here -- the verification row stands on its own. Lite bails the same
        # way, so the two backends agree on the empty case as well as the populated one.
        return
    projection = loaded[0]
    verification: dict[str, Any] = {
        "verification_id": str(verification_id),
        "state": state,
        "method": VERIFICATION_METHOD_EVIDENCE_GRADED,
        "recorded_at": now,
        "evidence_event_ids": [],
        "summary": f"{outcome.verdict}: {outcome.reason}"[:500],
    }
    stamp_verification_provenance(
        verification,
        projection=projection,
        directive_id=str(directive_id),
        # The stamp comes from the DIRECTIVE, never from the clock -- see
        # ``stamp_verification_provenance``. Grading can land arbitrarily long after the work,
        # and an objective restated in between must not silently re-badge this evidence as
        # evidence for the new contract.
        work_contract_revision=directive_contract_revision(
            db,
            workspace_id=workspace_id,
            owner_id=user_id,
            task_id=session_id,
            directive_id=str(directive_id),
        ),
    )
    insert_task_verification(
        db,
        workspace_id=workspace_id,
        owner_id=user_id,
        task_id=session_id,
        ref=verification,
        recorded_by=runner_principal,
        now=now,
    )
    apply_task_state_events(
        db,
        workspace_id=workspace_id,
        owner_id=user_id,
        session_id=session_id,
        task_id=session_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.VERIFICATION_RECORDED,
                contract_revision=int(projection.contract_revision),
                payload=verification,
                occurred_at=now,
                actor=runner_principal,
                directive_id=str(directive_id),
            )
        ],
        now=now,
        expected_revision=None,
    )


def record_verification(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    directive_id: uuid.UUID,
    evidence: VerificationEvidence,
    runner_principal: str,
    settings: Settings,
    now: datetime,
) -> tuple[VerificationOutcome, dict[str, Any]]:
    """Grade the evidence and write the verdict.  Returns ``(outcome, response_dict)``.

    Step 6 inserts the ``verification_results`` row **always**, including on a refusal, so a
    refused verification is durable evidence rather than a dropped request.
    """
    row = db.execute(
        text(
            """
            SELECT directive_id, state, claimed_executor, session_id
            FROM directive_executions
            WHERE directive_id = :directive_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"directive_id": directive_id, "workspace_id": workspace_id, "user_id": user_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=403, detail={"error": "unknown_directive"})
    if not is_terminal(str(row["state"])):
        # §7.4 step 1. Lite has always had this; Full did not, and the gap became load-bearing the
        # moment the graded verdict started moving the task-state projection: a directive still
        # `pending`/`in_progress` could be graded `passed`, and that pass reached
        # ``latest_verification`` -- a verdict on work that had not been reported yet. Measured
        # before this guard, against the live Postgres: a directive forced to `in_progress` graded
        # `200 passed` on Full and drove the projection to DONE, while the identical request on
        # Lite returned `409 directive_not_terminal`.
        raise HTTPException(
            status_code=409,
            detail={"error": "directive_not_terminal", "message": "a verification only means anything after a report"},
        )
    if not bool(settings.verification_enabled):
        raise HTTPException(status_code=503, detail={"error": "verification_disabled"})
    if str(runner_principal) != str(settings.verification_runner_principal):
        raise HTTPException(
            status_code=403,
            detail={"error": "unknown_runner", "runner_principal": str(runner_principal)},
        )
    criteria = load_acceptance_criteria(db, directive_id=directive_id)
    if criteria is None:
        raise HTTPException(status_code=409, detail={"error": "criteria_not_frozen", "directive_id": str(directive_id)})
    executing_identity = str(row["claimed_executor"] or "")
    outcome = decide_verdict(
        results=evidence.results,
        criteria=criteria,
        observed_corpus_digest=evidence.observed_corpus_digest,
        runner_principal=str(runner_principal),
        executing_identity=executing_identity,
    )
    verification_id = uuid.uuid4()
    reviewer_json = (
        json.dumps(dict(evidence.reviewer_model))
        if (evidence.reviewer_model is not None and bool(settings.verification_reviewer_model_enabled))
        else None
    )
    db.execute(
        text(
            """
            INSERT INTO verification_results(
              id, workspace_id, owner_id, task_id, directive_id, criteria_id,
              criteria_digest_at_run, corpus_digest_at_run, criteria_digest_match,
              corpus_digest_match, verdict, reason, runner_principal, executing_identity,
              platform, commit_sha, tree_sha, checks_json, reviewer_model_json, recorded_at,
              schema_version
            )
            VALUES(
              :id, :workspace_id, :owner_id, :task_id, :directive_id, :criteria_id,
              :criteria_digest_at_run, :corpus_digest_at_run, :criteria_digest_match,
              :corpus_digest_match, :verdict, :reason, :runner_principal, :executing_identity,
              :platform, :commit_sha, :tree_sha, CAST(:checks AS JSONB),
              CAST(:reviewer AS JSONB), :recorded_at, :schema_version
            )
            """
        ),
        {
            "id": verification_id,
            "workspace_id": workspace_id,
            "owner_id": user_id,
            "task_id": None,
            "directive_id": directive_id,
            "criteria_id": uuid.UUID(criteria.criteria_id),
            "criteria_digest_at_run": criteria.criteria_digest,
            "corpus_digest_at_run": str(evidence.observed_corpus_digest),
            "criteria_digest_match": bool(outcome.criteria_digest_match),
            "corpus_digest_match": bool(outcome.corpus_digest_match),
            "verdict": outcome.verdict,
            "reason": outcome.reason,
            "runner_principal": str(runner_principal),
            "executing_identity": executing_identity,
            "platform": str(evidence.platform or ""),
            "commit_sha": evidence.commit_sha,
            "tree_sha": evidence.tree_sha,
            "checks": json.dumps(
                [
                    {
                        "check_id": result.check_id,
                        "argv": list(result.argv),
                        "exit_code": int(result.exit_code),
                        "duration_ms": int(result.duration_ms),
                        "stdout_sha256": result.stdout_sha256,
                        "stderr_sha256": result.stderr_sha256,
                        "excerpt": result.excerpt,
                    }
                    for result in evidence.results
                ]
            ),
            "reviewer": reviewer_json,
            "recorded_at": now,
            "schema_version": VERIFICATION_SCHEMA_VERSION,
        },
    )
    state = VERIFICATION_STATE_FOR_VERDICT[outcome.verdict]
    # THE only site in the tree that writes any value other than 'unverified' to
    # directive_executions.verification_state.
    db.execute(
        text(
            """
            UPDATE directive_executions
               SET verification_state = :state, updated_at = :now
             WHERE directive_id = :directive_id
               AND workspace_id = :workspace_id
               AND user_id = :user_id
            """
        ),
        {
            "state": state,
            "now": now,
            "directive_id": directive_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
        },
    )
    _record_graded_task_state(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=str(row["session_id"] or ""),
        directive_id=directive_id,
        verification_id=verification_id,
        outcome=outcome,
        state=state,
        runner_principal=str(runner_principal),
        settings=settings,
        now=now,
    )
    db.commit()
    response = {
        "verification_id": verification_id,
        "directive_id": directive_id,
        "verdict": outcome.verdict,
        "reason": outcome.reason,
        "verification_state": state,
        "criteria_digest_at_run": criteria.criteria_digest,
        "corpus_digest_at_run": str(evidence.observed_corpus_digest),
        "criteria_digest_match": bool(outcome.criteria_digest_match),
        "corpus_digest_match": bool(outcome.corpus_digest_match),
        "runner_principal": str(runner_principal),
        "executing_identity": executing_identity,
        "platform": str(evidence.platform or ""),
        "commit_sha": evidence.commit_sha,
        "tree_sha": evidence.tree_sha,
        "advisory": True,
        "recorded_at": now,
        "schema_version": VERIFICATION_SCHEMA_VERSION,
    }
    return (outcome, response)


def verification_stats_for_session(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> tuple[int, int]:
    """``(runs, passed)`` read from ``verification_results`` — never from an executor's own
    ``details["verification"]`` booleans, which are the implementer's assertion about itself."""
    row = db.execute(
        text(
            """
            SELECT COUNT(1) AS runs,
                   COUNT(1) FILTER (WHERE v.verdict = 'passed') AS passed
            FROM verification_results v
            JOIN directive_executions d ON d.directive_id = v.directive_id
            WHERE v.workspace_id = :workspace_id
              AND d.user_id = :user_id
              AND d.session_id = :session_id
            """
        ),
        {"workspace_id": workspace_id, "user_id": user_id, "session_id": session_id},
    ).mappings().first()
    if row is None:
        return (0, 0)
    return (int(row["runs"] or 0), int(row["passed"] or 0))
