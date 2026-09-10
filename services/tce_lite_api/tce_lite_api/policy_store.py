"""Lite persistence for the one decision policy.

This is the Lite twin of ``services/tce_api/tce_api/policy_store.py``: same function names,
same signatures, same return shapes, so a reader can diff the two and the only differences
they find are the dialect and the two stated asymmetries below.

**Asymmetry 1 — Lite has no server-side model advisor, and P4 does not add one.**
``build_decision_request`` therefore passes ``advisor=None``, which is a legal input producing
``advisor_agreement=ABSENT``.  Because ``policy_advisor_required_families`` defaults to empty,
the decision stands rather than being forced to abstain.  The contract is identical to Full,
the body is honestly degraded, and ``policy_decision.advisor_agreement`` is the field that
says so on the wire.  It also means ``advice_visible`` — re-produced as
*"the LLM advisor ran, parsed, and its guidance replaced the fast-path payload"* — is ``False``
on every Lite turn.  That is the first honest producer of that column in the system: before
P4 both backends wrote ``bool(<eight-key dict literal>)``, which is ``True`` unconditionally.

**Asymmetry 2 — the retrieval identity differs, on purpose.**
``RETRIEVAL_VERSION`` is ``"lite-jaccard-v1"`` here and ``"full-knn-v1"`` in Full, and it is a
bound key on the qualification record.  A Full qualification therefore cannot be carried by a
Lite decision, which is the property the bound key was always supposed to have and did not
when the version was a single constant in the shared module that three loaders all claimed.

Nothing in this module decides anything.  ``decide()`` is pure and lives in
``tce_shared.decision_policy``; this module only loads what it needs, persists what it said,
and reads back whether a family has permission to have it used.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

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

logger = logging.getLogger(__name__)

# The loader's own identity, not a shared constant.  See the module docstring.
RETRIEVAL_VERSION: str = "lite-jaccard-v1"

# Lite has no model gateway, so the model identity of a Lite decision is the absence of one.
# It is spelled out rather than left empty, because a bound key of "" cannot be told apart
# from a bound key nobody filled in.
LITE_MODEL_ID: str = "none:lite"
LITE_RUNTIME_VERSION: str = "lite"

_EVIDENCE_COLUMNS = """
    id, consumer_id, workspace_id, subject_user_id, ts, situation_type, situation_summary,
    context_snapshot, user_response, response_reasoning, outcome,
    outcome_sentiment, source_event_ids, confidence, superseded_by,
    objective_text, constraints_json, available_choices_json, selected_choice,
    action_taken, correction_text, memory_class, evidence_source,
    lifecycle_status, valid_from, valid_until, contradicts_ids_json,
    confirmed_at, behavior_schema_version, redaction_applied,
    learning_eligible, storage_score, storage_decision,
    opportunity_id, origin_kind, capture_receipt_id, extraction_version,
    project_id, decision_family, task_id, episode_key
"""


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return loaded


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _parse_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _evidence_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """Coerce a sqlite row into the mapping ``decide()``'s neighbour primitives read.

    Every JSON column is decoded here and not at the call site, so the policy never sees a
    string where it expects a list.
    """

    item: dict[str, Any] = dict(row)
    item["context_snapshot"] = _json_loads(item.get("context_snapshot"), {})
    item["constraints"] = _json_loads(item.pop("constraints_json", "{}"), {})
    item["available_choices"] = _json_loads(item.pop("available_choices_json", "[]"), [])
    item["source_event_ids"] = _json_loads(item.get("source_event_ids"), [])
    item["contradicts_observation_ids"] = _json_loads(item.pop("contradicts_ids_json", "[]"), [])
    item["learning_eligible"] = bool(item.get("learning_eligible"))
    return item


def _policy_tuning(settings: Any) -> PolicyTuning:
    """The two settings, read once per turn, and hashed into a bound key.

    Anything that can move an abstention lives in ``policy_thresholds`` instead; these two are
    scoping and advisor-presence switches.  ``tuning_sha()`` covers them, so even they cannot
    be moved after a qualification is written without invalidating it.
    """

    raw_families = str(getattr(settings, "policy_advisor_required_families", "") or "")
    families = frozenset(item.strip().lower() for item in raw_families.split(",") if item.strip())
    return PolicyTuning(
        allow_unscoped_project_evidence=bool(getattr(settings, "policy_allow_unscoped_project_evidence", True)),
        advisor_required_families=families,
    )


def load_policy_evidence(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    decision_family: str,
    situation_type: str,
    limit: int = MAX_NEIGHBOURS * 8,
) -> tuple[tuple[Mapping[str, Any], ...], str, int]:
    """The single evidence loader for the policy on Lite.

    Returns ``(rows, retrieval_version, learning_eligible_total)``.  The version is this
    loader's own identity; the count is the input to the evidence-drift test in ``_exposure``.

    The scope clauses are *rendered* by ``observations_scope_predicate`` rather than
    hand-written, which is the point of adding the renderer: a hand-written query that forgets
    a clause compiles, type-checks and leaks.  ``allow_null_project`` is on because
    ``decision_observations.project_id`` is new in this revision and every pre-P4 row keeps
    ``NULL`` forever — a bare equality would silently become ``AND false`` for the whole
    historical corpus.  Those rows are admissible evidence and never count toward
    ``Adequacy.above_floor_count``, so they can inform a ranking and can never, alone, make a
    family adequate.
    """

    predicate = observations_scope_predicate(
        scope,
        dialect=ScopeDialect.SQLITE,
        include_project=True,
        allow_null_project=True,
    )
    clauses = list(predicate.clauses)
    params: list[Any] = list(predicate.positional_params)
    clauses.append("superseded_by IS NULL")
    clauses.append("lifecycle_status = 'active'")
    clauses.append("learning_eligible = 1")
    family = str(decision_family or "").strip()
    if family:
        # A NULL decision_family is a pre-P4 row: admissible, since it is the same subject's
        # decision, and it cannot be excluded without emptying the corpus on day one.
        clauses.append("(decision_family = ? OR decision_family IS NULL)")
        params.append(family)
    situation = str(situation_type or "").strip()
    if situation:
        clauses.append("situation_type = ?")
        params.append(situation)
    where = " AND ".join(clauses)
    bounded = max(1, min(int(limit), 500))
    rows = conn.execute(
        f"""
        SELECT {_EVIDENCE_COLUMNS}
        FROM decision_observations
        WHERE {where}
        ORDER BY ts DESC
        LIMIT ?
        """,
        (*params, bounded),
    ).fetchall()
    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM decision_observations
        WHERE {where}
        """,
        tuple(params),
    ).fetchone()
    learning_eligible_total = int((total_row["total"] if total_row is not None else 0) or 0)
    evidence = tuple(_evidence_from_row(row) for row in reversed(rows))
    return evidence, RETRIEVAL_VERSION, learning_eligible_total


def load_explicit_rules(conn: sqlite3.Connection, *, scope: ResolvedScope) -> tuple[ExplicitRule, ...]:
    """Active ``memory_rules`` for this subject, as the deterministic explicit-rule layer.

    A stated instruction the human wrote down is not personalization, so a rule that maps onto
    an offered option is applied and exposed regardless of qualification.  The table is empty
    on every deployment measured so far, which is why the rule layer changes nothing today —
    but it is the one unconditional new behaviour in P4, and it is named as such rather than
    left to be discovered when the first row is written.
    """

    try:
        rows = conn.execute(
            """
            SELECT id, scope, statement, priority
            FROM memory_rules
            WHERE workspace_id = ? AND user_id = ? AND active = 1
            ORDER BY priority ASC, updated_at DESC
            LIMIT 24
            """,
            (scope.workspace_id, scope.owner_id),
        ).fetchall()
    except sqlite3.Error:
        logger.warning("explicit rule load failed", exc_info=True)
        return ()
    rules: list[ExplicitRule] = []
    for row in rows:
        rule_scope = _json_loads(row["scope"], {})
        project = rule_scope.get("project_id") if isinstance(rule_scope, dict) else None
        rules.append(
            ExplicitRule(
                rule_id=str(row["id"]),
                statement=str(row["statement"] or ""),
                priority=int(row["priority"] or 2),
                scope_project_id=str(project) if project else None,
            )
        )
    return tuple(rules)


def build_decision_request(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    settings: Any,
    decision_family: str,
    situation_type: str,
    situation_summary: str,
    objective_text: str,
    constraints: Mapping[str, Any],
    context_snapshot: Mapping[str, Any],
    candidate_options: Sequence[str],
    decision_at: datetime,
    session_id: str,
    objective_hash: str | None,
    cancel_epoch: int = 0,
) -> DecisionRequest:
    """The single assembly point for a Lite decision.

    One call per turn, before ``ensure_takeover_response``.  ``candidate_options`` comes from
    the same list the freeze site will persist, computed once and reused, so the frozen row
    and the turn agree by construction rather than by coincidence — four independent producers
    of that list is how a live turn and its replay came to disagree without anything noticing.
    """

    tuning = _policy_tuning(settings)
    evidence_rows, retrieval_version, learning_eligible_total = load_policy_evidence(
        conn,
        scope=scope,
        decision_family=decision_family,
        situation_type=situation_type,
    )
    revision, cutoff = evidence_revision(list(evidence_rows))
    return DecisionRequest(
        decision_family=str(decision_family or ""),
        situation_type=str(situation_type or ""),
        situation_summary=str(situation_summary or "")[:500],
        objective_text=str(objective_text or ""),
        constraints=dict(constraints or {}),
        context_snapshot=dict(context_snapshot or {}),
        candidate_options=tuple(str(item) for item in candidate_options if str(item).strip()),
        evidence_rows=evidence_rows,
        decision_at=decision_at,
        workspace_id=scope.workspace_id,
        subject_user_id=scope.subject_user_id,
        project_id=scope.project_id,
        episode_key=build_episode_key(
            workspace_id=scope.workspace_id,
            subject_user_id=scope.subject_user_id,
            project_id=scope.project_id,
            session_id=str(session_id or ""),
            objective_hash=objective_hash,
            cancel_epoch=int(cancel_epoch or 0),
        ),
        evidence_revision=revision,
        evidence_cutoff_at=cutoff,
        retrieval_version=retrieval_version,
        model_id=LITE_MODEL_ID,
        runtime_version=LITE_RUNTIME_VERSION,
        learning_eligible_total=learning_eligible_total,
        qualification=lookup_qualification(
            conn,
            scope=scope,
            decision_family=decision_family,
            decision_at=decision_at,
            learning_eligible_total=learning_eligible_total,
        ),
        explicit_rules=load_explicit_rules(conn, scope=scope),
        # Lite has no server-side advisor. ABSENT, not fabricated. See the module docstring.
        advisor=None,
        tuning=tuning,
    )


def replay_inputs_payload(request: DecisionRequest) -> dict[str, Any]:
    """The four ``20260909_0041`` values for one request, keyed by column name.

    Lite twin of ``tce_api.policy_store.replay_inputs_payload``: same name, same signature, same
    keys in the same order, so the two backends cannot come to disagree about what "the inputs a
    replay needs" means.  ``decision_at`` is returned as a ``datetime`` here exactly as in Full;
    the sqlite write site is the one place that renders it, since sqlite has no timestamp type.
    """

    return {
        # The OFFERED set, not the cited subset: `DecisionRequest.fingerprint()` hashes what the
        # decision was shown, and `policy_json` already carries what it used.
        "offered_evidence_ids_json": [str(row.get("id") or "") for row in request.evidence_rows],
        # NOT `frozen_at`.  The freeze site takes a second `now_utc()` for the freeze, so
        # `frozen_at`/`created_at` is later than this by the width of the evidence load, and a
        # sha256 does not round that off.
        "decision_at": request.decision_at,
        "advisor_present": request.advisor is not None,
        "advisor_recommended_option": (
            request.advisor.recommended_option if request.advisor is not None else None
        ),
    }


def persist_policy_decision(
    conn: sqlite3.Connection,
    *,
    prediction_id: str,
    result: DecisionResult,
    request: DecisionRequest,
    project_id: str | None,
    episode_key: str,
    decision_advice_shown: bool,
    candidate_option_count: int,
) -> None:
    """Write the decision onto its shadow row.  No commit; the caller owns the transaction.

    ``validate_cited_ids`` runs here for the second time — the first was on the advisor's
    claimed ids — because an observation can be superseded or fall out of scope between
    retrieval and persistence, and a citation that no longer resolves is not a citation.  Its
    ``offered`` argument is the *evidence* set, exactly as in Full: this module used to pass
    ``[item.option for item in result.ranked_options]``, which intersects observation ids with
    option strings and is therefore empty whenever the decision ranked anything — so a Lite
    row's ``policy_json.evidence_observation_ids`` was blanked precisely on the decisions that
    had citations to record.

    ``candidate_option_count`` is a *gate* input and not a replay input.  The list a replay
    needs is retained on the ``decision_opportunities`` row the caller writes in this same
    transaction; see :data:`REPLAY_CANDIDATE_COLUMN`.  The evidence set has no such twin —
    nothing else retains it — so it is written here, on the shadow row, under
    :data:`EVIDENCE_OFFERED_COLUMN`.
    """

    payload = result.to_payload()
    replay_inputs = replay_inputs_payload(request)
    kept, _dropped = validate_cited_ids(
        list(result.evidence_observation_ids),
        list(replay_inputs["offered_evidence_ids_json"]),
    )
    payload["evidence_observation_ids"] = list(kept)
    conn.execute(
        """
        UPDATE behavior_shadow_predictions
           SET decision_policy_revision = ?,
               model_id = ?,
               runtime_version = ?,
               prompt_sha256 = ?,
               retrieval_version = ?,
               episode_key = ?,
               project_id = ?,
               abstain_reason = ?,
               ood_status = ?,
               conflict_status = ?,
               policy_score = ?,
               exposed = ?,
               decision_advice_shown = ?,
               candidate_option_count = ?,
               request_fingerprint = ?,
               offered_evidence_ids_json = ?,
               decision_at = ?,
               advisor_present = ?,
               advisor_recommended_option = ?,
               policy_json = ?
         WHERE id = ?
        """,
        (
            result.decision_policy_revision,
            LITE_MODEL_ID,
            LITE_RUNTIME_VERSION,
            result.prompt_sha256,
            result.retrieval_version,
            str(episode_key or ""),
            project_id,
            result.abstain_reason.value if result.abstain_reason else None,
            result.ood_status.value,
            result.conflict_status.value,
            float(result.policy_score),
            1 if result.exposed else 0,
            1 if decision_advice_shown else 0,
            max(0, int(candidate_option_count)),
            result.request_fingerprint,
            _json_dumps(list(replay_inputs["offered_evidence_ids_json"])),
            _iso(replay_inputs["decision_at"]),
            1 if replay_inputs["advisor_present"] else 0,
            replay_inputs["advisor_recommended_option"],
            _json_dumps(payload),
            str(prediction_id),
        ),
    )


# --------------------------------------------------------------------------------------
# Replay: reconstructing the candidate set a frozen turn actually offered
# --------------------------------------------------------------------------------------

REPLAY_CANDIDATE_COLUMN: str = "o.alternatives_json AS candidate_options_json"
"""The select-list item every Lite replay must add to the ``decision_opportunities`` join.

Identical to the Full constant of the same name (``tce_api.policy_store``), and identical on
purpose: the two backends must reconstruct the same candidate set from equivalent rows, and a
string that differs between them is a divergence nothing would catch.  It is dialect-neutral —
``alternatives_json`` is ``JSONB`` in Full and ``TEXT`` holding JSON here, and
:func:`replay_candidate_options` decodes both.

Why the join and not a new column.  The shadow row stores ``candidate_option_count``, a scalar,
and ``query_json`` — which ``_freeze_decision_opportunity_lite`` builds from ``situation_type``,
``situation_summary``, ``objective_text``, ``constraints`` and ``context_snapshot`` and *never*
from the options.  A replay that read ``query_json['available_choices']`` recovered ``()`` on
every row ever written, and an empty candidate set abstains (Y7), so the harness scored a
decision the turn never made.  The list itself was already persisted:
``_freeze_decision_opportunity_lite`` calls ``freeze_shadow_prediction`` and
``insert_opportunity(alternatives=...)`` on the same connection inside one transaction, keyed by
the ``opportunity_id`` generated in that block, so the shadow row and its opportunity commit
together or not at all.  Reading it back is therefore the whole fix — no column, no Lite DDL
guard, and it works on rows that already exist, which a new column would not.
"""


def replay_candidate_options(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The candidate set a frozen turn offered, normalised exactly as the write site did.

    Lite twin of ``tce_api.policy_store.replay_candidate_options``: same name, same signature,
    same normalisation, so Full and Lite reconstruct the same tuple from equivalent rows.

    ``row`` is a replay row that selected :data:`REPLAY_CANDIDATE_COLUMN` on its
    ``decision_opportunities`` join.  The blank-filter is not cosmetic: the write site stores
    the *raw* ``alternatives`` on the opportunity but decides over
    ``tuple(str(item) for item in alternatives if str(item).strip())``, so replay must apply the
    same filter or ``DecisionRequest.fingerprint()`` diverges on a whitespace entry.

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


REPLAY_INPUT_COLUMN_NAMES: tuple[str, ...] = (
    "offered_evidence_ids_json",
    "decision_at",
    "advisor_present",
    "advisor_recommended_option",
)
"""The four columns alembic ``20260909_0041`` adds, in the Full revision's own order.

Identical tuple to ``tce_api.policy_store.REPLAY_INPUT_COLUMN_NAMES``, and asserted equal to it
in ``tests/integration/test_policy_replay_lite.py``.  Full owns the physical names; this is the
mirror, and :data:`LITE_REPLAY_INPUT_DDL` is the sqlite rendering of the same four.
"""

LITE_REPLAY_INPUT_DDL: dict[str, str] = {
    "offered_evidence_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "decision_at": "TEXT",
    "advisor_present": "INTEGER NOT NULL DEFAULT 0",
    "advisor_recommended_option": "TEXT",
}
"""The Lite DDL guard's twin of the Full revision's ``_REPLAY_COLUMNS``.

Only the dialect differs: ``JSONB`` is ``TEXT`` holding JSON, ``TIMESTAMPTZ`` is an ISO-8601
``TEXT``, and ``BOOLEAN`` is ``INTEGER``.  ``db.py`` iterates this mapping rather than repeating
the names, so the guard cannot drift from :data:`REPLAY_INPUT_COLUMN_NAMES`.
"""

REPLAY_ROW_COLUMNS: str = """
    p.id                     AS id,
    p.session_id             AS session_id,
    p.workspace_id           AS workspace_id,
    p.subject_user_id        AS subject_user_id,
    p.decision_family        AS decision_family,
    p.query_json             AS query_json,
    p.actual_choice          AS actual_choice,
    p.frozen_at              AS frozen_at,
    p.created_at             AS created_at,
    p.resolution_state       AS resolution_state,
    p.decision_advice_shown  AS decision_advice_shown,
    p.advice_visible         AS advice_visible,
    p.candidate_option_count AS candidate_option_count,
    p.evidence_revision      AS evidence_revision,
    p.evidence_cutoff_at     AS evidence_cutoff_at,
    p.retrieval_version      AS retrieval_version,
    p.model_id               AS model_id,
    p.runtime_version        AS runtime_version,
    p.prompt_sha256          AS prompt_sha256,
    p.episode_key            AS episode_key,
    p.project_id             AS project_id,
    p.request_fingerprint    AS request_fingerprint,
    p.policy_json            AS policy_json,
    p.offered_evidence_ids_json AS offered_evidence_ids_json,
    p.decision_at            AS decision_at,
    p.advisor_present        AS advisor_present,
    p.advisor_recommended_option AS advisor_recommended_option,
    o.id                     AS opportunity_id,
    o.objective_hash         AS objective_hash,
    o.project_id             AS opportunity_project_id,
    o.episode_key            AS opportunity_episode_key,
    o.alternatives_json      AS candidate_options_json
"""
"""The one select list every Lite replay uses, against ``behavior_shadow_predictions p`` joined
to ``decision_opportunities o``.

The Lite twin of ``tce_api.policy_store.REPLAY_ROW_COLUMNS``, named for the same reason: a select
list that quietly omits a column is a replay that substitutes a literal for it.  Three separate
assemblies stamped ``"lite-replay"``, ``"replay-v1"``, ``None`` and ``""`` over
``evidence_revision``, ``retrieval_version``, ``project_id`` and ``episode_key`` -- all four
persisted on the row all along, two of them fingerprint terms.

There is no Lite twin of Full's degraded ``replay_row_columns(replay_inputs_present=...)``: the
Lite guard runs on every boot and adds the four columns unconditionally, so a Lite store one
revision behind its code does not exist.
"""


def replay_offered_evidence_ids(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The evidence ids the frozen decision was OFFERED, in the order it was offered them.

    Lite twin of ``tce_api.policy_store.replay_offered_evidence_ids``: same name, same signature,
    same normalisation, tolerant of both drivers' rendering (sqlite hands the column back as
    ``TEXT``, psycopg hands the Full twin back as a decoded ``list``).

    ``policy_json['evidence_observation_ids']`` is the set the decision *cited*, and
    ``fingerprint()`` hashes the set it was *shown*.  There is deliberately no fallback to the
    cited ids: it would make the fingerprint agree on every abstention -- which cites nothing and
    is offered plenty -- purely by accident.  A row written before ``20260909_0041`` returns
    ``()`` and the caller must read that as "not replayable", never as "replayed with no
    evidence".
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
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Load exactly the observations the frozen decision was offered.  Returns ``(rows, missing)``.

    Lite twin of ``tce_api.policy_store.replay_evidence_rows``, with the one dialect asymmetry
    this module already carries elsewhere: Full takes a psycopg ``(sql, params) -> rows`` callable
    because its replay callers hold no ORM session, and Lite takes the ``sqlite3.Connection`` its
    callers already have.  Same return shape, same ordering rule -- offered order, not ``ts``
    order, because that is the order the loader handed the rows to ``decide()``.

    It replaces an unscoped ``... WHERE workspace_id = ? AND subject_user_id = ? ORDER BY ts DESC
    LIMIT 40``, which is not a replay in any sense: it is a fresh retrieval under today's corpus,
    today's lifecycle states, and a limit matching neither the loader's scoping nor its ordering.
    That query answers "what would the policy conclude about this situation now"; the promotion
    gate is asking "did the policy conclude what the record says it concluded".

    ``missing`` names ids that no longer resolve -- superseded, expired or deleted since the turn.
    Returned rather than swallowed, because a replay missing one of its inputs is a replay whose
    fingerprint will not match, and the caller must be able to say which.

    The honest limit, stated rather than implied: each observation is read as it stands *now*.
    Nothing in this repo versions observation text, so the reconstruction is exact in identity and
    current in content.
    """

    offered = replay_offered_evidence_ids(row)
    if not offered:
        return (), ()
    placeholders = ",".join("?" for _ in offered)
    fetched = {
        str(item["id"]): item
        for item in conn.execute(
            f"SELECT {_EVIDENCE_COLUMNS} FROM decision_observations WHERE id IN ({placeholders})",
            tuple(offered),
        ).fetchall()
    }
    rows = tuple(_evidence_from_row(fetched[key]) for key in offered if key in fetched)
    missing = tuple(key for key in offered if key not in fetched)
    return rows, missing


def _replay_json(value: Any) -> Mapping[str, Any]:
    payload: Any = value or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, Mapping) else {}


def replay_query_json(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """``query_json`` as a mapping, whether the driver handed it back as JSON or as text."""

    return _replay_json(row.get("query_json"))


def _replay_aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return _parse_ts(value)


def replay_request_from_row(
    row: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> DecisionRequest:
    """Rebuild the ``DecisionRequest`` a frozen shadow row recorded.  Every field read, none invented.

    Lite twin of ``tce_api.policy_store.replay_request_from_row``.  The contract with the caller is
    that ``row`` selected :data:`REPLAY_ROW_COLUMNS`.

    One honest limit, the same one Full states: the advisor is reconstructed from the two terms the
    fingerprint hashes -- presence and recommended option -- and not from its note, its citations or
    its ``parse_state``, none of which is persisted.  A replayed *decision* may therefore differ from
    the live one in its advisor-agreement diagnostics on a row that had an advisor; the replayed
    *fingerprint* is exact.  Lite has no server-side advisor at all (asymmetry 1, above), so on this
    backend the reconstruction is total.
    """

    query = replay_query_json(row)
    policy = _replay_json(row.get("policy_json"))
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
        # `decision_at`, not `frozen_at`: the freeze site takes a second `now_utc()`, so the two
        # differ by the width of the evidence load and a sha256 does not round that off.  The
        # fallbacks exist only so a pre-20260909_0041 row still produces *a* request; such a row
        # cannot match its own fingerprint, and the replay-fidelity case says so out loud rather
        # than reporting a pass.
        decision_at=_replay_aware(row.get("decision_at"))
        or _replay_aware(row.get("frozen_at"))
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
    ``fingerprint()``: a diagnostic that shared the implementation would agree with it about a term
    the implementation got wrong.
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


def lookup_qualification(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    decision_family: str,
    decision_at: datetime,
    learning_eligible_total: int,
) -> Qualification | None:
    """The newest non-invalidated qualification record for this family, or ``None``.

    Expiry, evidence drift and bound-key mismatch are **computed at read time**, never stored
    as a mutation, so a record that passed once cannot keep granting permission between report
    runs and no sweeper job is needed to take it away.  ``_exposure()`` in the policy does the
    bound-key comparison; this function only refuses to return a record that is already dead
    on its own terms.

    ``None`` is the honest answer on every deployment today: no family has ever qualified, and
    the absence of a record *is* the refusal.  There is no setting that turns this into a yes.
    """

    try:
        row = conn.execute(
            """
            SELECT id, state, project_id, decision_family, decision_policy_revision, model_id,
                   runtime_version, prompt_sha256, retrieval_version, thresholds_sha, tuning_sha,
                   evidence_cutoff_at, learning_eligible_at_qualification, expires_at
            FROM policy_qualifications
            WHERE workspace_id = ? AND subject_user_id = ? AND decision_family = ?
              AND state = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (scope.workspace_id, scope.subject_user_id, str(decision_family or ""), QualificationState.QUALIFIED.value),
        ).fetchone()
    except sqlite3.Error:
        logger.warning("qualification lookup failed", exc_info=True)
        return None
    if row is None:
        return None
    expires_at = _parse_ts(row["expires_at"])
    if expires_at is None or expires_at <= decision_at:
        return None
    baseline = int(row["learning_eligible_at_qualification"] or 0)
    if baseline > 0:
        grown = int(learning_eligible_total) - baseline
        if (grown / baseline) > EVIDENCE_DRIFT_MAX_GROWTH:
            # The corpus this record was measured on is not the corpus in use. Reported as a
            # missing record rather than as a live one, so the policy reports NO_QUALIFICATION
            # instead of exposing a measurement that no longer describes anything.
            return None
    return Qualification(
        qualification_id=str(row["id"]),
        state=QualificationState(str(row["state"])),
        project_id=str(row["project_id"]) if row["project_id"] else None,
        decision_family=str(row["decision_family"] or ""),
        decision_policy_revision=str(row["decision_policy_revision"] or ""),
        model_id=str(row["model_id"] or ""),
        runtime_version=str(row["runtime_version"] or ""),
        prompt_sha256=str(row["prompt_sha256"] or ""),
        retrieval_version=str(row["retrieval_version"] or ""),
        thresholds_sha=str(row["thresholds_sha"] or ""),
        tuning_sha=str(row["tuning_sha"] or ""),
        evidence_cutoff_at=_parse_ts(row["evidence_cutoff_at"]),
        learning_eligible_at_qualification=baseline,
        expires_at=expires_at,
    )


def write_qualification(
    conn: sqlite3.Connection,
    *,
    scope: ResolvedScope,
    decision_family: str,
    state: QualificationState,
    settings: Any,
    metrics: Mapping[str, Any],
    gate: Mapping[str, Any],
    shortfalls: Sequence[str],
    baselines: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    split_sha256: str | None = None,
    split: Mapping[str, Any] | None = None,
    adjudicated_count: int = 0,
    non_abstained_count: int = 0,
    coverage: float = 0.0,
    precision_lower_bound: float = 0.0,
    distinct_episodes: int = 0,
    duplicate_context_ratio: float = 0.0,
    learning_eligible_at_qualification: int = 0,
    evidence_revision_value: str | None = None,
    evidence_cutoff_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Record one qualification *attempt*.  No commit; the caller owns the transaction.

    A failed run is a first-class artifact, not an absence: a ``NOT_QUALIFIED`` row carries its
    per-clause shortfalls, so "this family cannot produce a qualifying case" is a readable
    number rather than a mystery.  The record is immutable — expiry, drift and bound-key
    mismatch are derived on read (see ``lookup_qualification``), never written back.
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
    qualification_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO policy_qualifications (
            id, workspace_id, subject_user_id, project_id, decision_family, state,
            decision_policy_revision, model_id, runtime_version, prompt_sha256, retrieval_version,
            evidence_revision, evidence_cutoff_at, learning_eligible_at_qualification,
            thresholds_sha, thresholds_version, tuning_sha, split_sha256, split_json,
            metrics_json, gate_json, shortfalls_json, adjudicated_count, non_abstained_count,
            coverage, precision_lower_bound, distinct_episodes, duplicate_context_ratio,
            baselines_json, exclusions_json, created_at, expires_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            qualification_id,
            scope.workspace_id,
            scope.subject_user_id,
            scope.project_id,
            str(decision_family or ""),
            state.value,
            DECISION_POLICY_REVISION,
            LITE_MODEL_ID,
            LITE_RUNTIME_VERSION,
            "",
            RETRIEVAL_VERSION,
            evidence_revision_value,
            _iso(evidence_cutoff_at),
            max(0, int(learning_eligible_at_qualification)),
            THRESHOLDS_SHA,
            THRESHOLDS_VERSION,
            _policy_tuning(settings).tuning_sha(),
            split_sha256,
            _json_dumps(dict(split or {})),
            _json_dumps(dict(metrics)),
            _json_dumps(dict(gate)),
            _json_dumps([str(item) for item in shortfalls]),
            max(0, int(adjudicated_count)),
            max(0, int(non_abstained_count)),
            float(coverage),
            float(precision_lower_bound),
            max(0, int(distinct_episodes)),
            float(duplicate_context_ratio),
            _json_dumps(dict(baselines)),
            _json_dumps(dict(exclusions)),
            created_at.isoformat(),
            (created_at + timedelta(days=QUALIFICATION_TTL_DAYS)).isoformat(),
        ),
    )
    return qualification_id


def qualification_gate(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The read-only adapter behind ``latest_fidelity_gate_lite``.

    With no live ``QUALIFIED`` row it returns ``{"passed": False, "reason":
    "no_qualification_recorded"}`` — the same shape and the same truth value as the answer it
    replaces (``no_completed_fidelity_run``), so with ``behavior_autonomy_gate_enabled`` at its
    ``False`` default nothing downstream moves, and with the gate on the behaviour is what it
    was.  Returning ``passed = all(families_qualified)`` instead is one of the two routes by
    which an earlier draft escalated every turn on a corpus where no family is qualified.
    """

    at = now or datetime.now(tz=UTC)
    try:
        rows = conn.execute(
            """
            SELECT decision_family, state, expires_at, created_at
            FROM policy_qualifications
            WHERE workspace_id = ? AND subject_user_id = ?
            ORDER BY created_at DESC
            """,
            (workspace_id, subject_user_id),
        ).fetchall()
    except sqlite3.Error:
        logger.warning("qualification gate lookup failed", exc_info=True)
        rows = []
    families: dict[str, str] = {}
    live_at: str | None = None
    for row in rows:
        family = str(row["decision_family"] or "")
        if family in families:
            continue
        state = str(row["state"] or QualificationState.NOT_QUALIFIED.value)
        expires_at = _parse_ts(row["expires_at"])
        if state == QualificationState.QUALIFIED.value and (expires_at is None or expires_at <= at):
            state = QualificationState.EXPIRED.value
        families[family] = state
        if state == QualificationState.QUALIFIED.value and live_at is None:
            live_at = str(row["created_at"] or "")
    qualified = [name for name, state in families.items() if state == QualificationState.QUALIFIED.value]
    if not qualified:
        return {"passed": False, "reason": "no_qualification_recorded", "families": families, "evaluated_at": None}
    return {"passed": True, "reason": "qualified", "families": families, "evaluated_at": live_at}


def retrieval_baseline_lite(
    request: DecisionRequest,
    *,
    limit: int = 5,
) -> str | None:
    """The "ordinary retrieval" baseline: the most recent in-scope neighbour's own choice.

    Deliberately *not* the policy with a knob turned down — a baseline that shares the
    policy's machinery cannot show that the machinery earned anything.  This is what the
    system did before any of it existed: take the newest similar decision and repeat it.
    """

    if not request.candidate_options:
        return None
    from tce_shared.behavior_fidelity import _map_choice

    ordered = sorted(
        request.evidence_rows,
        key=lambda row: str(row.get("ts") or ""),
        reverse=True,
    )[: max(1, int(limit))]
    for row in ordered:
        raw_choice = str(row.get("selected_choice") or row.get("user_response") or "").strip()
        if not raw_choice:
            continue
        mapped = _map_choice(raw_choice, request.candidate_options)
        if mapped:
            return mapped
    return None


def default_tuning() -> PolicyTuning:
    """The tuning a caller gets when no settings object is at hand (replay, tests)."""

    return DEFAULT_TUNING
