"""The Full backend's side of the one decision policy: evidence in, decision recorded.

Everything that *decides* lives in ``tce_shared.decision_policy``.  This module does the three
things a pure module cannot: it loads the evidence, it assembles the one ``DecisionRequest``
per turn, and it writes what was decided next to the frozen row the human is about to answer.

Three properties are worth stating because they are the ones a reviewer will check.

**One assembly point.**  ``build_decision_request`` is the only place in this backend that
builds a ``DecisionRequest``.  Before P4 there were four independent producers of
``candidate_options`` and three of the evidence corpus, and nothing reconciled them, so the
route that decided and the harness that scored decisions were reading different inputs through
identically-named fields.  ``DecisionRequest.fingerprint()`` is what makes a divergence visible
rather than merely unlikely.

**The retrieval version is the loader's own identity, not a shared constant.**
``RETRIEVAL_VERSION`` is declared here and a different string is declared in the Lite twin, so
a qualification earned against this loader cannot be carried by a decision that a different
retriever answered.  A module-level constant in ``decision_policy`` — which is what the first
draft had — would have let three retrievers diverge arbitrarily while all three stamped the
same bound key.

**Refusal is the default.**  ``lookup_qualification`` returns ``None`` unless a live QUALIFIED
row exists whose bound keys all match, and ``None`` means personalization is not used.  There
is no setting that turns that off; the absence of a qualification record *is* the refusal.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.decision_capture import evidence_revision
from tce_shared.decision_policy import (
    DECISION_POLICY_REVISION,
    DEFAULT_TUNING,
    AdvisorContribution,
    DecisionRequest,
    DecisionResult,
    ExplicitRule,
    PolicyTuning,
    Qualification,
    QualificationState,
    validate_cited_ids,
)
from tce_shared.policy_evaluation import assert_qualification_is_earned
from tce_shared.policy_evaluation import episode_key as build_episode_key
from tce_shared.policy_thresholds import (
    EVIDENCE_DRIFT_MAX_GROWTH,
    MAX_NEIGHBOURS,
    QUALIFICATION_TTL_DAYS,
    THRESHOLDS_SHA,
    THRESHOLDS_VERSION,
)
from tce_shared.scope import ResolvedScope, ScopeDialect, observations_scope_predicate

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


RETRIEVAL_VERSION: str = "full-knn-v1"
"""This loader's identity, and a bound key on every qualification it feeds.

Lite declares ``lite-jaccard-v1`` and the replay assembly declares ``replay-frozen-v1``.  The
point of three distinct strings is that a Full qualification cannot silently authorise a Lite
decision, and a replay cannot claim to be either.
"""

_EVIDENCE_COLUMNS = """
    id, consumer_id, workspace_id, subject_user_id, ts, situation_type, situation_summary,
    context_snapshot, user_response, response_reasoning, outcome,
    outcome_sentiment, source_event_ids, confidence, superseded_by,
    objective_text, constraints_json, available_choices_json, selected_choice,
    action_taken, correction_text, memory_class, evidence_source,
    lifecycle_status, valid_from, valid_until, contradicts_ids_json,
    confirmed_at, behavior_schema_version, redaction_applied,
    learning_eligible, storage_score, storage_decision,
    opportunity_id, origin_kind, capture_receipt_id, extraction_version
"""

_P4_EVIDENCE_COLUMNS = ",\n    project_id, decision_family, task_id, episode_key"

# The four decision families a takeover turn can freeze, plus the one P4 adds.  Named here so
# the report and the gate agree on the vocabulary without importing main.
POLICY_ABSTENTION_FAMILY = "policy_abstention"


# --- migration-state probe ------------------------------------------------------------
#
# The policy columns land in alembic 20260909_0040.  Every write below is additive and is
# skipped when that revision has not been applied yet, so a service running ahead of its
# database records less rather than failing the turn.  This is a deployment-order guard, not
# a feature switch: there is no setting that reaches it and no way to leave it off once the
# revision is applied.
_POLICY_COLUMNS_PRESENT: dict[str, bool] = {}
_POLICY_COLUMNS_RECHECK_AT: dict[str, float] = {}
_POLICY_COLUMNS_NEGATIVE_TTL_SECONDS = 60.0


def policy_columns_available(db: Session) -> bool:
    """True once alembic ``20260909_0040`` has been applied to this database.

    A positive answer is cached for the life of the process — a column does not un-exist.  A
    negative answer is cached for a minute and then re-probed, so a service that started before
    its migration ran starts recording policy rows once the migration lands, instead of staying
    silently degraded until someone notices and restarts it.
    """

    bind = db.get_bind()
    key = str(getattr(bind, "url", "default"))
    cached = _POLICY_COLUMNS_PRESENT.get(key)
    if cached is True:
        return True
    if cached is False and time.monotonic() < _POLICY_COLUMNS_RECHECK_AT.get(key, 0.0):
        return False
    present = False
    try:
        row = db.execute(
            text(
                """
                SELECT COUNT(*) FROM information_schema.columns
                 WHERE table_name = 'behavior_shadow_predictions'
                   AND column_name = 'policy_json'
                """
            )
        ).scalar()
        present = bool(int(row or 0))
    except Exception:  # pragma: no cover - a probe failure is reported as "not available"
        logger.warning("policy column probe failed; policy columns treated as absent", exc_info=True)
        present = False
    _POLICY_COLUMNS_PRESENT[key] = present
    if not present:
        _POLICY_COLUMNS_RECHECK_AT[key] = time.monotonic() + _POLICY_COLUMNS_NEGATIVE_TTL_SECONDS
        if cached is None:
            logger.info(
                "decision policy columns absent (alembic 20260909_0040 not applied); "
                "the policy still decides, and its rows are not persisted"
            )
    return present


_REPLAY_COLUMNS_PRESENT: dict[str, bool] = {}
_REPLAY_COLUMNS_RECHECK_AT: dict[str, float] = {}


def replay_columns_available(db: Session) -> bool:
    """True once alembic ``20260909_0041`` has been applied to this database.

    Same shape and same reasoning as :func:`policy_columns_available`, one revision later.  It
    is a separate probe rather than a widened one because the two revisions deploy separately:
    a database that has ``policy_json`` but not ``offered_evidence_ids_json`` must still record
    everything 20260909_0040 gave it.
    """

    bind = db.get_bind()
    key = str(getattr(bind, "url", "default"))
    cached = _REPLAY_COLUMNS_PRESENT.get(key)
    if cached is True:
        return True
    if cached is False and time.monotonic() < _REPLAY_COLUMNS_RECHECK_AT.get(key, 0.0):
        return False
    present = False
    try:
        row = db.execute(
            text(
                """
                SELECT COUNT(*) FROM information_schema.columns
                 WHERE table_name = 'behavior_shadow_predictions'
                   AND column_name = 'offered_evidence_ids_json'
                """
            )
        ).scalar()
        present = bool(int(row or 0))
    except Exception:  # pragma: no cover - a probe failure is reported as "not available"
        logger.warning("replay column probe failed; replay columns treated as absent", exc_info=True)
        present = False
    _REPLAY_COLUMNS_PRESENT[key] = present
    if not present:
        _REPLAY_COLUMNS_RECHECK_AT[key] = time.monotonic() + _POLICY_COLUMNS_NEGATIVE_TTL_SECONDS
        if cached is None:
            logger.info(
                "replay input columns absent (alembic 20260909_0041 not applied); the policy "
                "still decides and still records, and its rows are not independently replayable"
            )
    return present


def reset_policy_column_cache() -> None:
    """Test hook: forget what the probe learned about every bind."""

    _POLICY_COLUMNS_PRESENT.clear()
    _POLICY_COLUMNS_RECHECK_AT.clear()
    _REPLAY_COLUMNS_PRESENT.clear()
    _REPLAY_COLUMNS_RECHECK_AT.clear()


# --- tuning ---------------------------------------------------------------------------


def _policy_tuning(settings_obj: Settings | None = None) -> PolicyTuning:
    """The two settings, read once, in one place.

    They are hashed into ``tuning_sha()`` and that hash is a bound key on the qualification
    record, so moving either one after a family qualifies invalidates the qualification.
    """

    cfg = settings_obj if settings_obj is not None else get_settings()
    return PolicyTuning(
        allow_unscoped_project_evidence=bool(getattr(cfg, "policy_allow_unscoped_project_evidence", True)),
        advisor_required_families=frozenset(
            getattr(cfg, "policy_advisor_required_family_set", frozenset())
        ),
    )


# --- evidence -------------------------------------------------------------------------


def _evidence_from_row(row: Any) -> dict[str, Any]:
    """Coerce one DB row into the plain mapping the policy scores.

    The rename is not cosmetic: ``eligible_behavior_evidence`` and ``_similarity`` read
    ``available_choices`` / ``contradicts_observation_ids``, and the columns are named
    ``*_json``.  Doing this at the boundary is what keeps the pure module free of column names.
    """

    item = dict(row)
    item["constraints"] = item.pop("constraints_json", {}) or {}
    item["available_choices"] = item.pop("available_choices_json", []) or []
    item["contradicts_observation_ids"] = item.pop("contradicts_ids_json", []) or []
    item["learning_eligible"] = bool(item.get("learning_eligible"))
    item["id"] = str(item.get("id") or "")
    return item


def load_policy_evidence(
    db: Session,
    *,
    scope: ResolvedScope,
    decision_family: str | None = None,
    situation_type: str | None = None,
    limit: int = MAX_NEIGHBOURS * 8,
    tuning: PolicyTuning = DEFAULT_TUNING,
) -> tuple[tuple[Mapping[str, Any], ...], str, int]:
    """Return ``(rows, retrieval_version, learning_eligible_total)``.

    The version is this loader's own identity (see ``RETRIEVAL_VERSION``).  The total is the
    count of learning-eligible observations in scope, which is the input to the evidence-drift
    test — the check that replaces an ``evidence_revision`` equality that compared a hash of
    the *evaluation corpus* with a hash of *this turn's twelve neighbours* and therefore never
    held in either direction.

    ``situation_type`` widens rather than narrows: it orders same-situation rows first and
    still admits the rest, because a hard filter on it is how the old advisor query returned
    nothing at all in exactly the low-evidence cases where abstention matters most.
    """

    # The project clause and the four P4 columns exist only after alembic 20260909_0040.
    # Before it, the loader scopes by workspace and subject alone rather than issuing SQL the
    # database will reject: one rejected statement aborts the surrounding transaction, and this
    # runs on the turn path.
    columns_present = policy_columns_available(db)
    predicate = observations_scope_predicate(
        scope,
        dialect=ScopeDialect.POSTGRES,
        include_project=columns_present,
        allow_null_project=bool(tuning.allow_unscoped_project_evidence),
    )
    params: dict[str, Any] = dict(predicate.named_params)
    params["limit"] = max(1, min(int(limit), 2000))
    params["situation_type"] = str(situation_type or "")
    family_clause = ""
    if decision_family and columns_present:
        params["decision_family"] = str(decision_family)
        # NULL is admitted: every pre-P4 row has no family and dropping them would throw the
        # historical corpus away on the day the column landed.
        family_clause = "AND (decision_family = :decision_family OR decision_family IS NULL)"
    rows = db.execute(
        text(
            f"""
            SELECT {_EVIDENCE_COLUMNS}{_P4_EVIDENCE_COLUMNS if columns_present else ""}
            FROM decision_observations
            WHERE {predicate.where_sql}
              AND learning_eligible = true
              AND superseded_by IS NULL
              AND lifecycle_status = 'active'
              {family_clause}
            ORDER BY (situation_type = :situation_type) DESC, ts DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    total = int(
        db.execute(
            text(
                f"""
                SELECT COUNT(*) FROM decision_observations
                 WHERE {predicate.where_sql}
                   AND learning_eligible = true
                   AND superseded_by IS NULL
                   AND lifecycle_status = 'active'
                """
            ),
            dict(predicate.named_params),
        ).scalar()
        or 0
    )
    return tuple(_evidence_from_row(row) for row in rows), RETRIEVAL_VERSION, total


def load_explicit_rules(db: Session, *, scope: ResolvedScope, limit: int = 50) -> tuple[ExplicitRule, ...]:
    """Active ``memory_rules`` for this subject, as the deterministic rule layer sees them.

    A stated instruction the human wrote down is not personalization, which is why a rule
    result is exposed whether or not the family has ever qualified.  The table is empty today;
    the layer exists so that the day a row is written the turn changes visibly and under a
    reported ``status=rule_applied``, rather than silently.
    """

    try:
        rows = db.execute(
            text(
                """
                SELECT id, statement, priority, scope->>'project_id' AS project_id
                  FROM memory_rules
                 WHERE workspace_id = :workspace_id
                   AND user_id = :user_id
                   AND active = true
                   AND (expires_at IS NULL OR expires_at > NOW())
                 ORDER BY priority ASC, id ASC
                 LIMIT :limit
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "user_id": scope.owner_id,
                "limit": max(1, min(limit, 200)),
            },
        ).mappings().all()
    except Exception:
        logger.warning("explicit rule load failed; the rule layer contributes nothing this turn", exc_info=True)
        return ()
    rules: list[ExplicitRule] = []
    for row in rows:
        statement = str(row.get("statement") or "").strip()
        if not statement:
            continue
        rules.append(
            ExplicitRule(
                rule_id=str(row.get("id") or ""),
                statement=statement,
                priority=int(row.get("priority") or 0),
                scope_project_id=(str(row.get("project_id")) if row.get("project_id") else None),
            )
        )
    return tuple(rules)


# --- qualification --------------------------------------------------------------------


def _qualification_from_row(row: Any) -> Qualification:
    expires_at = row.get("expires_at")
    if not isinstance(expires_at, datetime):
        expires_at = datetime.now(tz=UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    cutoff = row.get("evidence_cutoff_at")
    if isinstance(cutoff, datetime) and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    raw_state = str(row.get("state") or QualificationState.NOT_QUALIFIED.value)
    try:
        state = QualificationState(raw_state)
    except ValueError:
        state = QualificationState.NOT_QUALIFIED
    return Qualification(
        qualification_id=str(row.get("id") or ""),
        state=state,
        project_id=(str(row.get("project_id")) if row.get("project_id") else None),
        decision_family=str(row.get("decision_family") or ""),
        decision_policy_revision=str(row.get("decision_policy_revision") or ""),
        model_id=str(row.get("model_id") or ""),
        runtime_version=str(row.get("runtime_version") or ""),
        prompt_sha256=str(row.get("prompt_sha256") or ""),
        retrieval_version=str(row.get("retrieval_version") or ""),
        thresholds_sha=str(row.get("thresholds_sha") or ""),
        tuning_sha=str(row.get("tuning_sha") or ""),
        evidence_cutoff_at=cutoff if isinstance(cutoff, datetime) else None,
        learning_eligible_at_qualification=int(row.get("learning_eligible_at_qualification") or 0),
        expires_at=expires_at,
    )


def lookup_qualification(
    db: Session,
    *,
    scope: ResolvedScope,
    decision_family: str,
    decision_at: datetime,
    learning_eligible_total: int = 0,
) -> Qualification | None:
    """The newest live QUALIFIED record for this family, or ``None``.

    ``None`` is not an error and is not an abstention — it means personalization is not used
    for this turn and everything else about the decision is reported unchanged.

    Expiry and evidence drift are **computed at read time**, never written back, so a record
    that passed once cannot keep granting permission between report runs and no sweeper has to
    walk the table.  ``decision_policy._exposure`` does the bound-key comparison; this function
    only refuses to hand back a record that is already dead on its own terms.

    This reads ``policy_qualifications`` and nothing else.  In particular it does not read
    ``behavior_pilot`` state: a context-arm experiment can never grant permission.
    """

    if not policy_columns_available(db):
        return None
    try:
        row = db.execute(
            text(
                """
                SELECT id, state, project_id, decision_family, decision_policy_revision,
                       model_id, runtime_version, prompt_sha256, retrieval_version,
                       thresholds_sha, tuning_sha, evidence_cutoff_at,
                       learning_eligible_at_qualification, expires_at
                  FROM policy_qualifications
                 WHERE workspace_id = :workspace_id
                   AND subject_user_id = :subject_user_id
                   AND decision_family = :decision_family
                   AND state = :state
                 ORDER BY created_at DESC
                 LIMIT 1
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "subject_user_id": scope.subject_user_id,
                "decision_family": str(decision_family or ""),
                "state": QualificationState.QUALIFIED.value,
            },
        ).mappings().first()
    except Exception:
        # A missing table means the revision has not landed, which is the same answer as "no
        # record": refusal.
        return None
    if row is None:
        return None
    record = _qualification_from_row(row)
    if record.expires_at <= decision_at:
        return None
    baseline = record.learning_eligible_at_qualification
    if baseline > 0:
        grown = int(learning_eligible_total) - baseline
        if (grown / baseline) > EVIDENCE_DRIFT_MAX_GROWTH:
            # The corpus this record was measured on is not the corpus in use.  Reported as a
            # missing record rather than a live one, so the policy reports NO_QUALIFICATION
            # instead of exposing a measurement that no longer describes anything.
            return None
    return record


def qualification_gate(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The read-only adapter behind ``behavior_store.latest_fidelity_gate``.

    With no live QUALIFIED row it returns ``{"passed": False, "reason":
    "no_qualification_recorded"}`` — the same shape and the same truth value as the answer it
    replaces (``no_completed_fidelity_run``), so with ``behavior_autonomy_gate_enabled`` at its
    ``False`` default nothing downstream moves, and with the gate on the behaviour is what it
    was.  Returning ``passed = all(families_qualified)`` instead is one of the two routes by
    which an earlier draft escalated every turn on a corpus where no family is qualified.
    """

    at = now or datetime.now(tz=UTC)
    if not policy_columns_available(db):
        return {"passed": False, "reason": "no_qualification_recorded", "families": {}, "evaluated_at": None}
    try:
        rows = db.execute(
            text(
                """
                SELECT decision_family, state, expires_at, created_at
                  FROM policy_qualifications
                 WHERE workspace_id = :workspace_id
                   AND subject_user_id = :subject_user_id
                 ORDER BY created_at DESC
                """
            ),
            {"workspace_id": workspace_id, "subject_user_id": subject_user_id},
        ).mappings().all()
    except Exception:
        rows = []
    families: dict[str, str] = {}
    live_at: datetime | None = None
    for row in rows:
        family = str(row.get("decision_family") or "")
        if family in families:
            continue
        state = str(row.get("state") or QualificationState.NOT_QUALIFIED.value)
        expires_at = row.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if state == QualificationState.QUALIFIED.value and (
            not isinstance(expires_at, datetime) or expires_at <= at
        ):
            state = QualificationState.EXPIRED.value
        families[family] = state
        if state == QualificationState.QUALIFIED.value and live_at is None:
            created = row.get("created_at")
            live_at = created if isinstance(created, datetime) else None
    qualified = [name for name, state in families.items() if state == QualificationState.QUALIFIED.value]
    if not qualified:
        return {
            "passed": False,
            "reason": "no_qualification_recorded",
            "families": families,
            "evaluated_at": None,
        }
    return {
        "passed": True,
        "reason": "qualified",
        "families": families,
        "evaluated_at": live_at.isoformat() if live_at is not None else None,
    }


def list_qualifications(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
) -> list[dict[str, Any]]:
    """Newest attempt per family, for the read-only fidelity-gate adapter and the reporter."""

    if not policy_columns_available(db):
        return []
    try:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT ON (decision_family, project_id)
                       decision_family, project_id, state, created_at, expires_at,
                       shortfalls_json, coverage, precision_lower_bound, adjudicated_count
                  FROM policy_qualifications
                 WHERE workspace_id = :workspace_id
                   AND subject_user_id = :subject_user_id
                 ORDER BY decision_family, project_id, created_at DESC
                """
            ),
            {"workspace_id": workspace_id, "subject_user_id": subject_user_id},
        ).mappings().all()
    except Exception:
        return []
    return [dict(row) for row in rows]


def write_qualification(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    decision_family: str,
    state: QualificationState,
    model_id: str,
    runtime_version: str,
    prompt_sha256: str,
    retrieval_version: str,
    evidence_revision_value: str | None,
    evidence_cutoff_at: datetime | None,
    learning_eligible_at_qualification: int,
    split_sha256: str | None,
    split: Mapping[str, Any],
    metrics: Mapping[str, Any],
    gate: Mapping[str, Any],
    shortfalls: Sequence[str],
    adjudicated_count: int,
    non_abstained_count: int,
    coverage: float,
    precision_lower_bound: float,
    distinct_episodes: int,
    duplicate_context_ratio: float,
    baselines: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    now: datetime | None = None,
    tuning: PolicyTuning = DEFAULT_TUNING,
) -> UUID:
    """Record one qualification attempt, passed or failed.

    A failed attempt is a first-class row with its per-clause shortfalls, because "we ran the
    gate and fell short on these four clauses" and "nobody has ever run it" must not look the
    same from the outside.  Rows are immutable; a later attempt is a later row.
    """

    # A QUALIFIED verdict must be supported by the numbers recorded beside it.  Without this
    # the writer will stamp `qualified` on a record carrying zero adjudicated cases and a
    # seven-item shortfall list, and that row grants full exposure with every bound key honest.
    assert_qualification_is_earned(
        state=state.value,
        shortfalls=shortfalls,
        adjudicated_count=adjudicated_count,
        non_abstained_count=non_abstained_count,
        coverage=coverage,
        precision_lower_bound=precision_lower_bound,
        distinct_episodes=distinct_episodes,
        duplicate_context_ratio=duplicate_context_ratio,
    )
    created_at = now or datetime.now(tz=UTC)
    record_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO policy_qualifications (
                id, workspace_id, subject_user_id, project_id, decision_family, state,
                decision_policy_revision, model_id, runtime_version, prompt_sha256,
                retrieval_version, evidence_revision, evidence_cutoff_at,
                learning_eligible_at_qualification, thresholds_sha, thresholds_version,
                tuning_sha, split_sha256, split_json, metrics_json, gate_json,
                shortfalls_json, adjudicated_count, non_abstained_count, coverage,
                precision_lower_bound, distinct_episodes, duplicate_context_ratio,
                baselines_json, exclusions_json, created_at, expires_at, schema_version
            ) VALUES (
                :id, :workspace_id, :subject_user_id, :project_id, :decision_family, :state,
                :decision_policy_revision, :model_id, :runtime_version, :prompt_sha256,
                :retrieval_version, :evidence_revision, :evidence_cutoff_at,
                :learning_eligible_at_qualification, :thresholds_sha, :thresholds_version,
                :tuning_sha, :split_sha256, CAST(:split_json AS jsonb), CAST(:metrics_json AS jsonb),
                CAST(:gate_json AS jsonb), CAST(:shortfalls_json AS jsonb), :adjudicated_count,
                :non_abstained_count, :coverage, :precision_lower_bound, :distinct_episodes,
                :duplicate_context_ratio, CAST(:baselines_json AS jsonb), CAST(:exclusions_json AS jsonb),
                :created_at, :expires_at, 'v1'
            )
            """
        ),
        {
            "id": record_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "project_id": project_id,
            "decision_family": decision_family,
            "state": state.value,
            "decision_policy_revision": DECISION_POLICY_REVISION,
            "model_id": model_id,
            "runtime_version": runtime_version,
            "prompt_sha256": prompt_sha256,
            "retrieval_version": retrieval_version,
            "evidence_revision": evidence_revision_value,
            "evidence_cutoff_at": evidence_cutoff_at,
            "learning_eligible_at_qualification": int(learning_eligible_at_qualification),
            "thresholds_sha": THRESHOLDS_SHA,
            "thresholds_version": THRESHOLDS_VERSION,
            "tuning_sha": tuning.tuning_sha(),
            "split_sha256": split_sha256,
            "split_json": json.dumps(dict(split), default=str),
            "metrics_json": json.dumps(dict(metrics), default=str),
            "gate_json": json.dumps(dict(gate), default=str),
            "shortfalls_json": json.dumps([str(item) for item in shortfalls]),
            "adjudicated_count": int(adjudicated_count),
            "non_abstained_count": int(non_abstained_count),
            "coverage": float(coverage),
            "precision_lower_bound": float(precision_lower_bound),
            "distinct_episodes": int(distinct_episodes),
            "duplicate_context_ratio": float(duplicate_context_ratio),
            "baselines_json": json.dumps(dict(baselines), default=str),
            "exclusions_json": json.dumps(dict(exclusions), default=str),
            "created_at": created_at,
            "expires_at": created_at + timedelta(days=QUALIFICATION_TTL_DAYS),
        },
    )
    return record_id


# --- assembly -------------------------------------------------------------------------


def build_decision_request(
    db: Session,
    *,
    scope: ResolvedScope,
    decision_family: str,
    situation_type: str,
    situation_summary: str,
    objective_text: str,
    constraints: Mapping[str, Any],
    context_snapshot: Mapping[str, Any],
    candidate_options: Sequence[str],
    decision_at: datetime,
    session_id: str,
    objective_hash: str | None = None,
    cancel_epoch: int = 0,
    model_id: str = "",
    runtime_version: str = "",
    advisor: AdvisorContribution | None = None,
    settings_obj: Settings | None = None,
    evidence_limit: int = MAX_NEIGHBOURS * 8,
) -> DecisionRequest:
    """The single assembly point for this backend.  One query, one request, one fingerprint.

    Net DB work per turn goes *down* relative to what this replaces: the freeze path used to
    run ``load_behavior_evidence(limit=500)`` once per freeze site, up to three times a turn,
    and each of those fed a separate prediction.  Now there is one bounded load and one
    decision, and the frozen row and the turn agree by construction rather than by coincidence.

    ``model_id`` / ``runtime_version`` are supplied by the caller from the gateway that
    actually ran — from ``AdvisorContribution`` when the advisor answered, from the resolved
    gateway identity otherwise.  They are bound keys, and defaulting them to ``""`` would let a
    stale qualification survive a real model upgrade.
    """

    tuning = _policy_tuning(settings_obj)
    rows, retrieval_version, learning_total = load_policy_evidence(
        db,
        scope=scope,
        decision_family=decision_family,
        situation_type=situation_type,
        limit=evidence_limit,
        tuning=tuning,
    )
    revision, cutoff = evidence_revision(list(rows))
    resolved_model_id = model_id or (advisor.model_id if advisor is not None else "") or "none:full"
    resolved_runtime = runtime_version or (advisor.runtime_version if advisor is not None else "") or "full"
    return DecisionRequest(
        decision_family=str(decision_family or ""),
        situation_type=str(situation_type or ""),
        situation_summary=str(situation_summary or "")[:500],
        objective_text=str(objective_text or ""),
        constraints=dict(constraints or {}),
        context_snapshot=dict(context_snapshot or {}),
        candidate_options=tuple(str(item) for item in candidate_options if str(item).strip()),
        evidence_rows=rows,
        decision_at=decision_at,
        workspace_id=scope.workspace_id,
        subject_user_id=scope.subject_user_id,
        project_id=scope.project_id if scope.is_bound() else None,
        episode_key=build_episode_key(
            workspace_id=scope.workspace_id,
            subject_user_id=scope.subject_user_id,
            project_id=scope.project_id if scope.is_bound() else None,
            session_id=str(session_id or ""),
            objective_hash=objective_hash,
            cancel_epoch=int(cancel_epoch or 0),
        ),
        evidence_revision=revision,
        evidence_cutoff_at=cutoff,
        retrieval_version=retrieval_version,
        model_id=resolved_model_id,
        runtime_version=resolved_runtime,
        learning_eligible_total=learning_total,
        qualification=lookup_qualification(
            db,
            scope=scope,
            decision_family=decision_family,
            decision_at=decision_at,
            learning_eligible_total=learning_total,
        ),
        explicit_rules=load_explicit_rules(db, scope=scope),
        advisor=advisor,
        tuning=tuning,
    )


# --- persistence ----------------------------------------------------------------------


def prediction_payload(result: DecisionResult) -> dict[str, Any]:
    """The shadow-row view of a decision, in the shape ``freeze_shadow_prediction`` expects.

    ``abstained`` is true for an abstention and false for both a selection and a rule
    application: what the column has always meant is "did the policy commit to an option".
    """

    from tce_shared.decision_policy import DecisionStatus

    return {
        "predicted_choice": result.selected_option,
        "confidence": float(result.policy_score),
        "abstained": result.status is DecisionStatus.ABSTAINED,
        "citations": list(result.evidence_observation_ids),
    }


def replay_inputs_payload(request: DecisionRequest) -> dict[str, Any]:
    """The four ``20260909_0041`` values for one request, keyed by column name.

    Exposed rather than inlined into :func:`persist_policy_decision` so the replay-fidelity test
    can build the stored row through the same code the writer uses.  A test that hand-rolled its
    own version of this would prove that the test agrees with itself.
    """

    return {
        # The OFFERED set, not the cited subset: `DecisionRequest.fingerprint()` hashes what the
        # decision was shown, and `policy_json` already carries what it used.
        "offered_evidence_ids_json": [str(row.get("id") or "") for row in request.evidence_rows],
        # NOT `frozen_at`.  The caller takes a second `datetime.now()` for the freeze, so
        # `created_at` is later than this by the width of the evidence load.
        "decision_at": request.decision_at,
        "advisor_present": request.advisor is not None,
        "advisor_recommended_option": (
            request.advisor.recommended_option if request.advisor is not None else None
        ),
    }


def persist_policy_decision(
    db: Session,
    *,
    prediction_id: UUID | None,
    result: DecisionResult,
    request: DecisionRequest,
    project_id: str | None,
    episode_key: str,
    decision_advice_shown: bool,
    candidate_option_count: int,
    opportunity_id: UUID | None = None,
) -> None:
    """Write the decision onto its shadow row.  Never raises; never commits.

    ``validate_cited_ids`` runs here for the second time — the first was on the advisor's
    claimed ids — because an observation can be superseded or fall out of scope between
    retrieval and persistence, and a citation that no longer resolves is not a citation.
    """

    if prediction_id is None and opportunity_id is None:
        return
    if not policy_columns_available(db):
        return
    offered = [str(row.get("id") or "") for row in request.evidence_rows]
    kept, _dropped = validate_cited_ids(list(result.evidence_observation_ids), offered)
    payload = result.to_payload()
    payload["evidence_observation_ids"] = list(kept)
    # The four replay inputs (alembic 20260909_0041).  They are appended to the same UPDATE, in
    # a separate SET fragment, so the row's decision and the inputs that produced it are written
    # in one statement or not at all.  When the revision has not been applied the fragment is
    # empty and everything 20260909_0040 records still lands.
    replay_set_sql = ""
    replay_params: dict[str, Any] = {}
    if replay_columns_available(db):
        replay_set_sql = (
            ",\n                           offered_evidence_ids_json = CAST(:offered_evidence_ids_json AS jsonb)"
            ",\n                           decision_at = :decision_at"
            ",\n                           advisor_present = :advisor_present"
            ",\n                           advisor_recommended_option = :advisor_recommended_option"
        )
        replay_params = dict(replay_inputs_payload(request))
        replay_params["offered_evidence_ids_json"] = json.dumps(
            replay_params["offered_evidence_ids_json"]
        )
    try:
        if prediction_id is not None:
            db.execute(
                text(
                    f"""
                    UPDATE behavior_shadow_predictions
                       SET decision_policy_revision = :decision_policy_revision,
                           model_id = :model_id,
                           runtime_version = :runtime_version,
                           prompt_sha256 = :prompt_sha256,
                           retrieval_version = :retrieval_version,
                           episode_key = :episode_key,
                           project_id = :project_id,
                           abstain_reason = :abstain_reason,
                           ood_status = :ood_status,
                           conflict_status = :conflict_status,
                           policy_score = :policy_score,
                           exposed = :exposed,
                           decision_advice_shown = :decision_advice_shown,
                           candidate_option_count = :candidate_option_count,
                           request_fingerprint = :request_fingerprint,
                           policy_json = CAST(:policy_json AS jsonb){replay_set_sql}
                     WHERE id = :id
                    """
                ),
                {
                    **replay_params,
                    "id": prediction_id,
                    "decision_policy_revision": result.decision_policy_revision,
                    "model_id": request.model_id,
                    "runtime_version": request.runtime_version,
                    "prompt_sha256": result.prompt_sha256,
                    "retrieval_version": result.retrieval_version,
                    "episode_key": str(episode_key or ""),
                    "project_id": project_id,
                    "abstain_reason": result.abstain_reason.value if result.abstain_reason else None,
                    "ood_status": result.ood_status.value,
                    "conflict_status": result.conflict_status.value,
                    "policy_score": float(result.policy_score),
                    "exposed": bool(result.exposed),
                    "decision_advice_shown": bool(decision_advice_shown),
                    "candidate_option_count": max(0, int(candidate_option_count)),
                    "request_fingerprint": result.request_fingerprint,
                    "policy_json": json.dumps(payload, default=str),
                },
            )
        if opportunity_id is not None:
            db.execute(
                text("UPDATE decision_opportunities SET episode_key = :episode_key WHERE id = :id"),
                {"id": opportunity_id, "episode_key": str(episode_key or "")},
            )
    except Exception:
        logger.warning("policy decision persistence failed; the turn is unaffected", exc_info=True)


# --------------------------------------------------------------------------------------
# Replay: reconstructing the candidate set a frozen turn actually offered
# --------------------------------------------------------------------------------------

REPLAY_CANDIDATE_COLUMN: str = "o.alternatives_json AS candidate_options_json"
"""The select-list item every replay must add to the ``decision_opportunities`` join.

Replay reconstructs a ``DecisionRequest`` from a frozen ``behavior_shadow_predictions`` row.
Its candidate set is the one input that row does not carry: the shadow row stores
``candidate_option_count``, a scalar, and ``query_json`` -- which the write site builds from
``situation_type``, ``situation_summary``, ``objective_text``, ``constraints`` and
``context_snapshot`` and *never* from the options.  A replay that read
``query_json['available_choices']`` therefore recovered ``()`` on every row ever written, and
an empty candidate set abstains, so the harness scored a decision the turn never made.

The list itself is already persisted, and has been all along.  ``_freeze_decision_opportunity``
(main.py) writes ``insert_opportunity(alternatives=...)`` and ``freeze_shadow_prediction`` inside
the *same* ``db.begin_nested()`` block, keyed by the ``opportunity_id`` generated in that block,
so a prospective shadow row and its opportunity commit together or not at all.  Reading
``o.alternatives_json`` on the join both replay callers already perform is therefore the whole
fix: no new column, no migration, no Lite DDL twin, and -- decisively -- it works on the rows
that already exist.  A new ``candidate_options_json`` column would be empty on all 83 live rows
and could only be backfilled *from this join*, and it would be a second copy of the same list,
which is exactly the failure mode ``candidate_option_count`` already exhibits: 20 live rows join
to a non-empty ``alternatives_json`` while only 3 carry ``candidate_option_count > 0``, because
the scalar was added later and defaulted to 0.
"""


def replay_candidate_options(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The candidate set a frozen turn offered, normalised exactly as the write site did.

    ``row`` is a replay row that selected :data:`REPLAY_CANDIDATE_COLUMN` on its
    ``decision_opportunities`` join.  The blank-filter here is not cosmetic: the write site
    stores the *raw* ``alternatives`` on the opportunity but decides over
    ``tuple(str(item) for item in alternatives if str(item).strip())``, so replay must apply
    the same filter or ``DecisionRequest.fingerprint()`` diverges on a whitespace entry.

    ``query_json['available_choices']`` is accepted as a fallback only because a future writer
    may populate it; it is empty on every row written to date and must never be the only source.
    """

    raw: Any = row.get("candidate_options_json")
    if raw is None:
        raw = row.get("alternatives_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raw = None
    if not raw:
        query: Any = row.get("query_json") or {}
        if isinstance(query, str):
            try:
                query = json.loads(query)
            except ValueError:
                query = {}
        raw = query.get("available_choices") if isinstance(query, Mapping) else None
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


# --------------------------------------------------------------------------------------
# Replay: reconstructing THE decision, not a similar one
# --------------------------------------------------------------------------------------

REPLAY_INPUT_COLUMN_NAMES: tuple[str, ...] = (
    "offered_evidence_ids_json",
    "decision_at",
    "advisor_present",
    "advisor_recommended_option",
)
"""The four columns alembic ``20260909_0041`` adds.  The Lite guard block uses these same names
in this same order; ``test_policy_parity`` asserts the two agree."""

_REPLAY_INPUT_ABSENT_SELECT: str = """
    NULL AS offered_evidence_ids_json,
    NULL AS decision_at,
    false AS advisor_present,
    NULL AS advisor_recommended_option
"""

REPLAY_ROW_COLUMNS: str = """
    p.id::text AS id,
    p.session_id,
    p.workspace_id,
    p.subject_user_id,
    p.decision_family,
    p.query_json,
    p.actual_choice,
    p.frozen_at,
    p.created_at,
    p.resolution_state,
    p.decision_advice_shown,
    p.advice_visible,
    p.evidence_revision,
    p.evidence_cutoff_at,
    p.retrieval_version,
    p.model_id,
    p.runtime_version,
    p.episode_key,
    p.project_id,
    p.request_fingerprint,
    p.policy_json,
    p.offered_evidence_ids_json,
    p.decision_at,
    p.advisor_present,
    p.advisor_recommended_option,
    o.objective_hash,
    o.project_id AS opportunity_project_id,
    o.episode_key AS opportunity_episode_key,
    o.alternatives_json AS candidate_options_json
"""
"""The one select list every Full replay uses, against ``behavior_shadow_predictions p``
LEFT JOINed to ``decision_opportunities o``.

Named once because the failure it prevents is a select list that quietly omits a column the
assembly then substitutes a literal for.  Before this, three separate replay assemblies stamped
``"live-corpus-replay"``, ``"replay-v1"``, ``"lite-replay"``, ``None`` and ``""`` over
``evidence_revision``, ``retrieval_version``, ``project_id`` and ``episode_key`` -- all four of
which were persisted on the row all along, and two of which are fingerprint terms.  A replay that
invents its own identity is not replaying anything.
"""


def replay_columns_present(fetch: Any) -> bool:
    """``20260909_0041`` applied?  Asked over a raw psycopg ``(sql, params) -> rows`` callable.

    The Session-bound twin is :func:`replay_columns_available`.  Replay callers are scripts and
    integration tests that hold a psycopg connection and no ORM session, and a select list that
    names a column the database does not have aborts the whole statement.
    """

    try:
        rows = fetch(
            """
            SELECT COUNT(*) AS n FROM information_schema.columns
             WHERE table_name = 'behavior_shadow_predictions'
               AND column_name = 'offered_evidence_ids_json'
            """,
            (),
        )
    except Exception:  # pragma: no cover - a probe failure is reported as "not available"
        return False
    return bool(rows and int(next(iter(rows[0].values())) or 0))


def replay_row_columns(*, replay_inputs_present: bool) -> str:
    """:data:`REPLAY_ROW_COLUMNS`, degraded to literals when ``20260909_0041`` is unapplied.

    The degraded form is not a fallback that lets a replay proceed as if nothing were missing:
    ``offered_evidence_ids_json`` comes back NULL, :func:`replay_offered_evidence_ids` returns
    ``()``, and the fidelity test reports the row as *not replayable*.  What it buys is that the
    qualification report still runs, and still prints its refusal, on a database one revision
    behind -- rather than dying on a missing column.
    """

    if replay_inputs_present:
        return REPLAY_ROW_COLUMNS
    lines = [
        line
        for line in REPLAY_ROW_COLUMNS.splitlines()
        if not any(f"p.{name}" in line for name in REPLAY_INPUT_COLUMN_NAMES)
    ]
    return "\n".join(lines) + "," + _REPLAY_INPUT_ABSENT_SELECT


_REPLAY_EVIDENCE_SELECT: str = """
    SELECT id::text AS id,
           consumer_id, workspace_id, subject_user_id, ts, situation_type, situation_summary,
           context_snapshot, user_response, response_reasoning, outcome,
           outcome_sentiment, source_event_ids, confidence, superseded_by,
           objective_text, constraints_json, available_choices_json, selected_choice,
           action_taken, correction_text, memory_class, evidence_source,
           lifecycle_status, valid_from, valid_until, contradicts_ids_json,
           confirmed_at, behavior_schema_version, redaction_applied,
           learning_eligible, storage_score, storage_decision,
           opportunity_id, origin_kind, capture_receipt_id, extraction_version,
           project_id, decision_family, task_id, episode_key
      FROM decision_observations
     WHERE id::text = ANY(%s)
"""
"""The same column set ``load_policy_evidence`` returns, including the four 20260909_0040 adds.

Unconditional rather than probed: this query is only reached when a row carries offered evidence
ids, which requires ``20260909_0041``, which chains onto ``20260909_0040``.  A database that can
answer the outer query can answer this one.

Note what is NOT here: ``learning_eligible = true``, ``superseded_by IS NULL``,
``lifecycle_status = 'active'`` and the scope predicate.  Those are *retrieval* filters, and
re-applying them would silently drop an observation that has been superseded since the turn --
which is exactly the row whose absence a replay must report rather than paper over.
"""


def replay_offered_evidence_ids(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The evidence ids the frozen decision was OFFERED, in the order it was offered them.

    Persisted by :func:`persist_policy_decision` under alembic ``20260909_0041``.  Note the
    distinction this exists to preserve: ``policy_json['evidence_observation_ids']`` is the set
    the decision *cited*, and ``DecisionRequest.fingerprint()`` hashes the set it was *shown*.
    Falling back to the cited ids would make the fingerprint agree on every abstention (which
    cites nothing and is offered plenty) purely by accident, so there is no such fallback: a row
    written before the revision returns ``()`` and the caller must treat that as "not replayable"
    rather than as "replayed successfully with no evidence".
    """

    raw: Any = row.get("offered_evidence_ids_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def replay_evidence_rows(
    fetch: Any,
    row: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Load exactly the observations the frozen decision was offered.  Returns ``(rows, missing)``.

    ``fetch`` is a ``(sql, params) -> list[dict]`` callable over a psycopg connection -- the
    replay callers each already have one.

    This replaces an unscoped ``SELECT ... WHERE workspace_id = %s AND subject_user_id = %s
    ORDER BY ts DESC LIMIT 40``, which is not a replay in any sense: it is a fresh retrieval,
    run under today's corpus, today's lifecycle states and a limit that matches neither the
    loader's scoping nor its ordering.  It answers "what would the policy conclude about this
    situation now", and the promotion gate is supposed to be asking "did the policy conclude
    what the record says it concluded".

    ``missing`` names ids that no longer resolve -- superseded, expired or deleted since the
    turn.  It is returned rather than swallowed because a replay missing one of its inputs is a
    replay whose fingerprint will not match, and the caller must be able to say which.
    """

    offered = replay_offered_evidence_ids(row)
    if not offered:
        return (), ()
    fetched = {str(item.get("id") or ""): item for item in fetch(_REPLAY_EVIDENCE_SELECT, (list(offered),))}
    rows = tuple(_evidence_from_row(fetched[key]) for key in offered if key in fetched)
    missing = tuple(key for key in offered if key not in fetched)
    return rows, missing


def _replay_policy_json(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload: Any = row.get("policy_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, Mapping) else {}


def replay_query_json(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """``query_json`` as a mapping, whether the driver handed it back as jsonb or as text."""

    query: Any = row.get("query_json") or {}
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except ValueError:
            query = {}
    return query if isinstance(query, Mapping) else {}


def _replay_aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def replay_request_from_row(
    row: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> DecisionRequest:
    """Rebuild the ``DecisionRequest`` a frozen shadow row recorded.  Every field read, none invented.

    The contract with the caller is simply that ``row`` selected :data:`REPLAY_ROW_COLUMNS`.

    One honest limit, stated rather than hidden: the advisor is reconstructed from the two terms
    the fingerprint hashes -- presence and recommended option -- and not from its note, its
    citation list or its ``parse_state``, none of which is persisted.  So a replayed *decision*
    may differ from the live one in its advisor-agreement diagnostics on a row that had an
    advisor; the replayed *fingerprint* is exact.  No live row has an advisor today, which is why
    this is a limit worth naming now rather than a bug worth fixing later.
    """

    query = replay_query_json(row)
    policy = _replay_policy_json(row)
    advisor: AdvisorContribution | None = None
    if bool(row.get("advisor_present")):
        recommended = row.get("advisor_recommended_option")
        advisor = AdvisorContribution(
            recommended_option=str(recommended) if recommended is not None else None,
            abstained=recommended is None,
            abstain_reason=None,
            evidence_ids=(),
            conflicting_evidence_ids=(),
            advisor_note=None,
            parse_state="parsed",
            prompt_sha256=str(row.get("prompt_sha256") or ""),
            model_id=str(row.get("model_id") or ""),
            runtime_version=str(row.get("runtime_version") or ""),
        )
    return DecisionRequest(
        decision_family=str(row.get("decision_family") or ""),
        situation_type=str(query.get("situation_type") or ""),
        situation_summary=str(query.get("situation_summary") or ""),
        objective_text=str(query.get("objective_text") or query.get("objective") or ""),
        constraints=dict(query.get("constraints") or {}),
        context_snapshot=dict(query.get("context_snapshot") or {}),
        candidate_options=replay_candidate_options(row),
        evidence_rows=tuple(evidence),
        # `decision_at`, not `created_at`: the write path takes a second `datetime.now()` for
        # the freeze.  The fallback is named `created_at` only so a pre-20260909_0041 row still
        # produces *a* request; such a row cannot match its own fingerprint and the replay
        # fidelity test says so out loud instead of reporting a pass.
        decision_at=_replay_aware(row.get("decision_at"))
        or _replay_aware(row.get("created_at"))
        or datetime.now(tz=UTC),
        workspace_id=str(row.get("workspace_id") or ""),
        subject_user_id=str(row.get("subject_user_id") or ""),
        project_id=row.get("project_id") or row.get("opportunity_project_id") or None,
        episode_key=str(row.get("episode_key") or row.get("opportunity_episode_key") or ""),
        evidence_revision=str(row.get("evidence_revision") or policy.get("evidence_revision") or ""),
        evidence_cutoff_at=_replay_aware(row.get("evidence_cutoff_at")),
        retrieval_version=str(row.get("retrieval_version") or policy.get("retrieval_version") or ""),
        model_id=str(row.get("model_id") or ""),
        runtime_version=str(row.get("runtime_version") or ""),
        advisor=advisor,
    )


def replay_fingerprint_terms(request: DecisionRequest) -> dict[str, Any]:
    """The fingerprint's inputs, spelled out, so a mismatch reports a field and not a hex string.

    Deliberately reconstructed from the request's public fields rather than exported from
    ``fingerprint()``: a diagnostic that shared the implementation would agree with it about a
    term the implementation got wrong.
    """

    return {
        "decision_family": request.decision_family,
        "situation_type": request.situation_type,
        "candidate_options": sorted(request.candidate_options),
        "evidence_ids": sorted(str(item.get("id") or "") for item in request.evidence_rows),
        "decision_at": request.decision_at.isoformat(),
        "evidence_revision": request.evidence_revision,
        "retrieval_version": request.retrieval_version,
        "advisor_present": request.advisor is not None,
        "advisor_option": request.advisor.recommended_option if request.advisor else None,
    }


def annotate_observation(
    db: Session,
    *,
    observation_id: UUID,
    project_id: str | None,
    decision_family: str | None,
    task_id: str | None,
    episode_key: str | None,
) -> None:
    """Fill the four P4 columns on an observation this backend just wrote.  Never raises."""

    if not policy_columns_available(db):
        return
    try:
        db.execute(
            text(
                """
                UPDATE decision_observations
                   SET project_id = COALESCE(:project_id, project_id),
                       decision_family = COALESCE(:decision_family, decision_family),
                       task_id = COALESCE(:task_id, task_id),
                       episode_key = COALESCE(:episode_key, episode_key)
                 WHERE id = :id
                """
            ),
            {
                "id": observation_id,
                "project_id": project_id,
                "decision_family": decision_family,
                "task_id": task_id,
                "episode_key": episode_key,
            },
        )
    except Exception:
        logger.warning("observation policy annotation failed", exc_info=True)


def retrieval_baseline_full(
    db: Session,
    *,
    scope: ResolvedScope,
    situation_type: str,
    limit: int = 1,
) -> tuple[str, ...]:
    """The recency baseline the qualification report compares the policy against.

    Deliberately dumb: the most recent eligible choice in scope, with no similarity term.
    A policy that cannot beat "what did you do last time" has not demonstrated anything.
    """

    predicate = observations_scope_predicate(
        scope, dialect=ScopeDialect.POSTGRES, include_project=policy_columns_available(db)
    )
    params: dict[str, Any] = dict(predicate.named_params)
    params["situation_type"] = situation_type
    params["limit"] = max(1, min(int(limit), 20))
    try:
        rows = db.execute(
            text(
                f"""
                SELECT selected_choice FROM decision_observations
                 WHERE {predicate.where_sql}
                   AND learning_eligible = true
                   AND superseded_by IS NULL
                   AND lifecycle_status = 'active'
                   AND situation_type = :situation_type
                 ORDER BY ts DESC
                 LIMIT :limit
                """
            ),
            params,
        ).scalars().all()
    except Exception:
        return ()
    return tuple(str(value) for value in rows if str(value or "").strip())
