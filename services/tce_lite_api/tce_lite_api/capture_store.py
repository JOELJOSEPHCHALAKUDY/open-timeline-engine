"""Lite (SQLite) store for the P1 trusted capture channel.

Mirrors ``tce_api.capture_store`` one-for-one: trusted input receipts, decision
opportunities frozen before the human answer, extracted decision candidates,
append-only human resolutions, and the synchronous extraction fallback that the
Lite backend runs inline because it has no worker.

All identifiers are TEXT UUIDs and all timestamps are ISO-8601 strings.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.behavior_fidelity import behavior_storage_gate, normalize_behavior_evidence
from tce_shared.decision_capture import (
    EXTRACTION_VERSION,
    CandidateKind,
    DecisionCandidate,
    DecisionOpportunity,
    HumanResolution,
    OriginKind,
    Promotion,
    extract_candidates,
    is_retrospective,
    promotion_decision,
)
from tce_shared.situation import classify_situation

from .behavior_control_store import (
    append_shadow_correction,
    create_memory_review,
    mark_shadow_unresolved,
    resolve_shadow_prediction,
)
from .config import Settings

logger = logging.getLogger(__name__)

CAPTURE_DELIVERY_STATES = ("unknown", "healthy", "degraded", "spooling", "gap", "unavailable")

_RECEIPT_COLUMNS = """
    id, workspace_id, owner_id, subject_user_id, host_session_id, sequence, prompt_id, delivery_key,
    content_sha256, origin_kind, capture_principal, host_client, event_id, project_id, observed_at,
    ingested_at, original_char_count, content_truncated, redaction_applied_json, spool_depth,
    spool_failures, gap_since, queue_state, extraction_state, extraction_lease_until,
    extraction_attempts, extraction_last_error, extraction_version_done, next_extraction_at, schema_version
"""

_OPPORTUNITY_COLUMNS = """
    id, workspace_id, subject_user_id, owner_id, session_id, turn, objective_hash, task_id, project_id,
    decision_family, situation_type, question_text, alternatives_json, pre_answer_snapshot_json,
    evidence_cutoff_at, evidence_revision, advice_exposure_json, shadow_prediction_id, source_event_id,
    status, relayed_answer, relayed_at, resolved_at, expires_at, created_at, frozen_at, schema_version
"""


# --------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return default
    return decoded if decoded is not None else default


def _receipt_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["redaction_applied"] = _loads(item.pop("redaction_applied_json", "[]"), [])
    item["content_truncated"] = bool(item.get("content_truncated"))
    return item


def _opportunity_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["alternatives"] = [str(v) for v in _loads(item.pop("alternatives_json", "[]"), []) if str(v).strip()]
    item["pre_answer_snapshot"] = _loads(item.pop("pre_answer_snapshot_json", "{}"), {})
    item["advice_exposure"] = _loads(item.pop("advice_exposure_json", "{}"), {})
    return item


def opportunity_to_model(item: dict[str, Any]) -> DecisionOpportunity:
    created_at = _parse_dt(item.get("created_at")) or _now()
    return DecisionOpportunity(
        opportunity_id=str(item["id"]),
        decision_family=str(item.get("decision_family") or ""),
        situation_type=str(item.get("situation_type") or "choice_required"),
        question_text=str(item.get("question_text") or ""),
        alternatives=tuple(str(v) for v in (item.get("alternatives") or [])),
        status=str(item.get("status") or "open"),
        created_at=created_at,
        frozen_at=_parse_dt(item.get("frozen_at")) or created_at,
        expires_at=_parse_dt(item.get("expires_at")),
        session_id=str(item.get("session_id") or ""),
        objective_hash=item.get("objective_hash"),
        project_id=item.get("project_id"),
        task_id=item.get("task_id"),
        evidence_revision=item.get("evidence_revision"),
        resolved_at=_parse_dt(item.get("resolved_at")),
    )


# --------------------------------------------------------------------------- receipts


def get_receipt_by_delivery_key(
    conn: sqlite3.Connection, *, workspace_id: str, owner_id: str, delivery_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_RECEIPT_COLUMNS} FROM trusted_input_receipts WHERE workspace_id = ? AND owner_id = ? AND delivery_key = ? LIMIT 1",
        (workspace_id, owner_id, delivery_key),
    ).fetchone()
    return _receipt_from_row(row) if row is not None else None


def get_receipt(conn: sqlite3.Connection, *, receipt_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_RECEIPT_COLUMNS} FROM trusted_input_receipts WHERE id = ? LIMIT 1", (receipt_id,)).fetchone()
    return _receipt_from_row(row) if row is not None else None


def insert_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    host_session_id: str,
    sequence: int | None,
    prompt_id: str | None,
    delivery_key: str,
    content_sha256: str,
    origin_kind: str,
    capture_principal: str,
    host_client: str,
    event_id: str | None,
    project_id: str | None,
    observed_at: datetime,
    ingested_at: datetime,
    original_char_count: int,
    content_truncated: bool,
    redaction_applied: list[str],
    spool_depth: int,
    spool_failures: int,
    gap_since: datetime | None,
    queue_state: str,
) -> None:
    """Persist the durable minimal receipt. No commit."""
    conn.execute(
        """
        INSERT INTO trusted_input_receipts (
            id, workspace_id, owner_id, subject_user_id, host_session_id, sequence, prompt_id, delivery_key,
            content_sha256, origin_kind, capture_principal, host_client, event_id, project_id, observed_at,
            ingested_at, original_char_count, content_truncated, redaction_applied_json, spool_depth,
            spool_failures, gap_since, queue_state, extraction_state, extraction_attempts, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 'v1')
        """,
        (
            receipt_id,
            workspace_id,
            owner_id,
            subject_user_id,
            host_session_id,
            sequence,
            prompt_id,
            delivery_key,
            content_sha256,
            origin_kind,
            capture_principal,
            host_client,
            event_id,
            project_id,
            _iso(observed_at),
            _iso(ingested_at),
            max(0, int(original_char_count)),
            1 if content_truncated else 0,
            _json(sorted({str(v) for v in redaction_applied if str(v).strip()})),
            max(0, int(spool_depth)),
            max(0, int(spool_failures)),
            _iso(gap_since),
            queue_state,
        ),
    )


def set_receipt_queue_state(conn: sqlite3.Connection, receipt_id: str, queue_state: str) -> None:
    conn.execute("UPDATE trusted_input_receipts SET queue_state = ? WHERE id = ?", (queue_state, receipt_id))


def latest_receipt_for_subject(conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT {_RECEIPT_COLUMNS} FROM trusted_input_receipts
        WHERE workspace_id = ? AND subject_user_id = ?
        ORDER BY ingested_at DESC LIMIT 1
        """,
        (workspace_id, subject_user_id),
    ).fetchone()
    return _receipt_from_row(row) if row is not None else None


def capture_delivery_state(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    stale_seconds: int,
    now: datetime,
) -> str:
    """Derive the audit-channel health for a subject from its most recent receipt."""
    row = latest_receipt_for_subject(conn, workspace_id=workspace_id, subject_user_id=subject_user_id)
    if row is None:
        return "unavailable"
    if row.get("gap_since"):
        return "gap"
    if int(row.get("spool_failures") or 0) > 0:
        return "degraded"
    if int(row.get("spool_depth") or 0) > 0:
        return "spooling"
    ingested_at = _parse_dt(row.get("ingested_at"))
    if ingested_at is None or (now - ingested_at).total_seconds() > max(0, int(stale_seconds)):
        return "unavailable"
    return "healthy"


# --------------------------------------------------------------------------- opportunities


def get_opportunity(conn: sqlite3.Connection, *, opportunity_id: str, workspace_id: str, subject_user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_OPPORTUNITY_COLUMNS} FROM decision_opportunities WHERE id = ? AND workspace_id = ? AND subject_user_id = ? LIMIT 1",
        (opportunity_id, workspace_id, subject_user_id),
    ).fetchone()
    return _opportunity_from_row(row) if row is not None else None


def open_opportunity_for_session(
    conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str, session_id: str, decision_family: str | None = None
) -> dict[str, Any] | None:
    family_clause = "AND decision_family = ?" if decision_family else ""
    params: list[Any] = [workspace_id, subject_user_id, session_id]
    if decision_family:
        params.append(decision_family)
    row = conn.execute(
        f"""
        SELECT {_OPPORTUNITY_COLUMNS} FROM decision_opportunities
        WHERE workspace_id = ? AND subject_user_id = ? AND session_id = ? AND status = 'open' {family_clause}
        ORDER BY created_at DESC LIMIT 1
        """,
        params,
    ).fetchone()
    return _opportunity_from_row(row) if row is not None else None


def resolved_opportunity_for_objective(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    session_id: str,
    decision_family: str,
    objective_hash: str,
) -> dict[str, Any] | None:
    """The already-answered question of this family for this objective, if the human answered it.

    Used to stop a second prospective prediction being frozen for a question the human has already
    resolved (the safety gate re-fires every turn while the objective still reads as high risk).
    """
    if not objective_hash:
        return None
    row = conn.execute(
        f"""
        SELECT {_OPPORTUNITY_COLUMNS} FROM decision_opportunities
        WHERE workspace_id = ? AND subject_user_id = ? AND session_id = ? AND decision_family = ?
          AND objective_hash = ? AND status = 'resolved'
        ORDER BY resolved_at DESC, created_at DESC LIMIT 1
        """,
        (workspace_id, subject_user_id, session_id, decision_family, objective_hash),
    ).fetchone()
    return _opportunity_from_row(row) if row is not None else None


def _answer_moment(receipt: dict[str, Any], fallback: datetime) -> datetime:
    """The moment the human answered, clamped to server time.

    `observed_at` is host-supplied; a host clock running ahead would otherwise make a prediction frozen AFTER the
    answer look prospective (inflating the exit-gate denominator) and let a message promote as the answer to a
    question that did not exist yet. `ingested_at` is server time on the same receipt. Same rule as
    tce_worker.jobs.decision_extraction._answer_moment.
    """

    observed_at = _parse_dt(receipt.get("observed_at")) or fallback
    ingested_at = _parse_dt(receipt.get("ingested_at"))
    return min(observed_at, ingested_at) if ingested_at is not None else observed_at


def _opportunities_for_subject(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    since: datetime,
    status: str,
    time_column: str,
    answer_moment: datetime | None = None,
    expiry_at: datetime | None = None,
) -> list[dict[str, Any]]:
    # A question frozen after the human spoke cannot be the question this message answers: mirrors the shared
    # `answer_precedes_question` rule (promotion_decision) so the SQL load and the promotion agree. Expiry is
    # judged at server time (`expiry_at`), never at the host-supplied moment, so a host clock running behind
    # cannot rewind a stale answer back inside an expired question's window.
    freeze_bound = "AND frozen_at <= ?" if (status == "open" and answer_moment is not None) else ""
    expiry_bound = "AND (expires_at IS NULL OR expires_at > ?)" if (status == "open" and expiry_at is not None) else ""
    params: list[Any] = [workspace_id, subject_user_id, status, _iso(since)]
    if freeze_bound:
        params.append(_iso(answer_moment))
    if expiry_bound:
        params.append(_iso(expiry_at))
    params.extend([project_id, project_id])
    rows = conn.execute(
        f"""
        SELECT {_OPPORTUNITY_COLUMNS} FROM decision_opportunities
        WHERE workspace_id = ? AND subject_user_id = ? AND status = ? AND {time_column} >= ?
          {freeze_bound}
          {expiry_bound}
          AND (project_id IS NULL OR ? IS NULL OR project_id = ?)
        ORDER BY created_at DESC
        """,
        params,
    ).fetchall()
    return [_opportunity_from_row(row) for row in rows]


def open_opportunities_for_subject(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    since: datetime,
    answer_moment: datetime | None = None,
    expiry_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Subject-scoped lookup (host session ids never match takeover session ids).

    `answer_moment` bounds the load to questions that already existed when the human spoke; `expiry_at` is
    server time and bounds it to questions still live when the answer reached us.
    """
    return _opportunities_for_subject(
        conn,
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        since=since,
        status="open",
        time_column="created_at",
        answer_moment=answer_moment,
        expiry_at=expiry_at,
    )


def resolved_opportunities_for_subject(
    conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str, project_id: str | None, since: datetime
) -> list[dict[str, Any]]:
    return _opportunities_for_subject(
        conn,
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        since=since,
        status="resolved",
        time_column="resolved_at",
    )


def insert_opportunity(
    conn: sqlite3.Connection,
    *,
    opportunity_id: str,
    workspace_id: str,
    subject_user_id: str,
    owner_id: str,
    session_id: str,
    turn: int | None,
    objective_hash: str | None,
    task_id: str | None,
    project_id: str | None,
    decision_family: str,
    situation_type: str,
    question_text: str,
    alternatives: list[str],
    pre_answer_snapshot: dict[str, Any],
    evidence_cutoff_at: datetime | None,
    evidence_revision: str | None,
    advice_exposure: dict[str, Any],
    shadow_prediction_id: str | None,
    source_event_id: str | None,
    expires_at: datetime | None,
    created_at: datetime,
    frozen_at: datetime,
    episode_key: str = "",
) -> None:
    """Immutable after insert except status/relayed_*/resolved_at. No commit.

    ``episode_key`` (P4) is the unit a train/holdout split may not straddle.  It is stored here
    rather than derived later because the three inputs that make it -- session, objective hash
    and the task's cancel epoch -- are all live at freeze time and one of them (the cancel
    epoch) is not recoverable from the row afterwards.  Defaulted so an un-upgraded caller
    keeps compiling; such a row simply carries no episode and is excluded from a split.
    """
    conn.execute(
        """
        INSERT INTO decision_opportunities (
            id, workspace_id, subject_user_id, owner_id, session_id, turn, objective_hash, task_id, project_id,
            decision_family, situation_type, question_text, alternatives_json, pre_answer_snapshot_json,
            evidence_cutoff_at, evidence_revision, advice_exposure_json, shadow_prediction_id, source_event_id,
            status, expires_at, created_at, frozen_at, episode_key, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 'v1')
        """,
        (
            opportunity_id,
            workspace_id,
            subject_user_id,
            owner_id,
            session_id,
            turn,
            objective_hash,
            task_id,
            project_id,
            decision_family,
            situation_type,
            question_text[:4000],
            _json([str(v) for v in alternatives]),
            _json(pre_answer_snapshot),
            _iso(evidence_cutoff_at),
            evidence_revision,
            _json(advice_exposure),
            shadow_prediction_id,
            source_event_id,
            _iso(expires_at),
            _iso(created_at),
            _iso(frozen_at),
            str(episode_key or ""),
        ),
    )


def mark_opportunity_relayed(conn: sqlite3.Connection, opportunity_id: str, relayed_answer: str, relayed_at: datetime) -> None:
    """An executor-relayed answer is recorded but never resolves the opportunity."""
    conn.execute(
        "UPDATE decision_opportunities SET relayed_answer = ?, relayed_at = ? WHERE id = ? AND status = 'open'",
        (str(relayed_answer or "")[:500], _iso(relayed_at), opportunity_id),
    )


def resolve_opportunity(conn: sqlite3.Connection, *, opportunity_id: str, resolved_at: datetime) -> bool:
    cursor = conn.execute(
        "UPDATE decision_opportunities SET status = 'resolved', resolved_at = ? WHERE id = ? AND status = 'open'",
        (_iso(resolved_at), opportunity_id),
    )
    return int(cursor.rowcount or 0) == 1


def close_opportunities(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    session_id: str,
    status: str,
    resolved_at: datetime,
) -> list[str]:
    """Abandon/expire every open opportunity of a session; linked shadow rows leave the pending pool."""
    rows = conn.execute(
        """
        SELECT id, shadow_prediction_id FROM decision_opportunities
        WHERE workspace_id = ? AND subject_user_id = ? AND session_id = ? AND status = 'open'
        """,
        (workspace_id, subject_user_id, session_id),
    ).fetchall()
    closed: list[str] = []
    shadow_state = "abandoned" if status == "abandoned" else "unanswered"
    for row in rows:
        conn.execute(
            "UPDATE decision_opportunities SET status = ?, resolved_at = ? WHERE id = ? AND status = 'open'",
            (status, _iso(resolved_at), str(row["id"])),
        )
        if row["shadow_prediction_id"]:
            mark_shadow_unresolved(conn, prediction_id=str(row["shadow_prediction_id"]), resolution_state=shadow_state, resolution_source="sweep")
        closed.append(str(row["id"]))
    return closed


# --------------------------------------------------------------------------- resolutions


def insert_human_resolution(
    conn: sqlite3.Connection,
    *,
    resolution: HumanResolution,
    workspace_id: str,
    subject_user_id: str,
    receipt_id: str | None,
    source_event_id: str | None,
    candidate_id: str | None,
    observation_id: str | None,
) -> str:
    resolution_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO human_resolutions (
            id, opportunity_id, workspace_id, subject_user_id, receipt_id, source_event_id, candidate_id,
            selected_choice, correction_text, stated_rationale, resolution_source, human_source_ref,
            observation_id, supersedes_resolution_id, resolved_at, created_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            resolution_id,
            resolution.opportunity_id,
            workspace_id,
            subject_user_id,
            receipt_id,
            source_event_id,
            candidate_id,
            str(resolution.selected_choice or "")[:500],
            str(resolution.correction_text or "")[:1000],
            resolution.stated_rationale,
            resolution.resolution_source,
            resolution.human_source_ref,
            observation_id,
            resolution.supersedes_resolution_id,
            _iso(resolution.resolved_at),
            _iso(_now()),
        ),
    )
    return resolution_id


def latest_resolution_for_opportunity(conn: sqlite3.Connection, opportunity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, opportunity_id, observation_id, selected_choice, resolution_source, human_source_ref, resolved_at
        FROM human_resolutions WHERE opportunity_id = ? ORDER BY resolved_at DESC, created_at DESC LIMIT 1
        """,
        (opportunity_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def validate_source_event_provenance(
    conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str, event_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """A source-event id is meaningful only when the server validates its ownership, origin and scope."""
    out: dict[str, dict[str, Any]] = {}
    for raw_id in event_ids:
        event_id = str(raw_id)
        info: dict[str, Any] = {"owned": False, "origin_kind": None, "receipt_id": None, "capture_principal": None}
        event_row = conn.execute("SELECT context FROM events WHERE id = ? LIMIT 1", (event_id,)).fetchone()
        if event_row is None:
            out[event_id] = info
            continue
        context = _loads(event_row["context"], {})
        workspace_ok = isinstance(context, dict) and str(context.get("_tce_workspace") or "") == workspace_id
        receipt_row = conn.execute(
            "SELECT id, subject_user_id, origin_kind, capture_principal FROM trusted_input_receipts WHERE event_id = ? LIMIT 1",
            (event_id,),
        ).fetchone()
        if receipt_row is None:
            info["owned"] = bool(workspace_ok)
            out[event_id] = info
            continue
        principal = str(receipt_row["capture_principal"] or "")
        info["receipt_id"] = str(receipt_row["id"])
        info["capture_principal"] = principal
        info["owned"] = bool(workspace_ok and str(receipt_row["subject_user_id"]) == subject_user_id)
        info["origin_kind"] = str(receipt_row["origin_kind"]) if principal.startswith("host:") else None
        out[event_id] = info
    return out


# --------------------------------------------------------------------------- extraction (inline fallback)


def sweep_stale_opportunities(conn: sqlite3.Connection, *, now: datetime, ttl_seconds: int) -> dict[str, int]:
    """Expire open opportunities; relayed-but-unlabelled and never-answered ones land in separate denominators."""
    cutoff = now - timedelta(seconds=max(0, int(ttl_seconds)))
    missing_label = 0
    relayed_rows = conn.execute(
        "SELECT id, shadow_prediction_id FROM decision_opportunities WHERE status = 'open' AND relayed_at IS NOT NULL AND relayed_at < ?",
        (_iso(cutoff),),
    ).fetchall()
    for row in relayed_rows:
        # Mirror the worker (_housekeeping): the opportunity itself leaves 'open' too, or a later receipt
        # could still promote against a question that already lost its label.
        conn.execute(
            "UPDATE decision_opportunities SET status = 'expired', resolved_at = ? WHERE id = ? AND status = 'open'",
            (_iso(now), str(row["id"])),
        )
        if row["shadow_prediction_id"]:
            mark_shadow_unresolved(
                conn, prediction_id=str(row["shadow_prediction_id"]), resolution_state="missing_label", resolution_source="executor_relayed"
            )
            missing_label += 1
    expired_rows = conn.execute(
        "SELECT id, shadow_prediction_id FROM decision_opportunities WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at < ?",
        (_iso(now),),
    ).fetchall()
    expired = 0
    for row in expired_rows:
        conn.execute(
            "UPDATE decision_opportunities SET status = 'expired', resolved_at = ? WHERE id = ? AND status = 'open'",
            (_iso(now), str(row["id"])),
        )
        if row["shadow_prediction_id"]:
            mark_shadow_unresolved(conn, prediction_id=str(row["shadow_prediction_id"]), resolution_state="unanswered", resolution_source="sweep")
        expired += 1
    conn.commit()
    return {"expired": expired, "missing_label": missing_label}


def _load_receipt_content(conn: sqlite3.Connection, receipt: dict[str, Any]) -> str:
    event_id = receipt.get("event_id")
    if not event_id:
        raise RuntimeError("receipt has no source event")
    row = conn.execute("SELECT payload FROM events WHERE id = ? LIMIT 1", (str(event_id),)).fetchone()
    if row is None:
        raise RuntimeError("source event missing")
    payload = _loads(row["payload"], {})
    if not isinstance(payload, dict):
        raise RuntimeError("decrypt: payload is not an object")
    if "_tce_encrypted" in payload:
        raise RuntimeError("decrypt: encrypted payloads are not supported by the lite backend")
    return str(payload.get("input_excerpt") or "")


def _situation_type_for(candidate: DecisionCandidate, opportunity: dict[str, Any] | None) -> str:
    if opportunity is not None:
        return str(opportunity.get("situation_type") or "choice_required")
    try:
        return classify_situation(candidate.supporting_span)
    except Exception:  # pragma: no cover - defensive
        return "choice_required"


def _discard_reason(candidate: DecisionCandidate) -> str:
    if candidate.contradicted:
        return "contradictory"
    return str(candidate.kind.value)


def _pending_reason(candidate: DecisionCandidate, opportunity: dict[str, Any] | None) -> str:
    if candidate.origin_kind == OriginKind.IMPORTED_TRANSCRIPT:
        return "imported_transcript"
    if candidate.kind in {CandidateKind.PREFERENCE, CandidateKind.REJECTION, CandidateKind.FREE_TEXT}:
        return str(candidate.kind.value)
    if candidate.opportunity_id is None:
        return "no_unique_target"
    if opportunity is None or str(opportunity.get("status")) != "open":
        return "target_not_open"
    return "ambiguous_target"


def _update_candidate(conn: sqlite3.Connection, candidate_id: str, *, status: str, reason: str, observation_id: str | None = None, review_id: str | None = None) -> None:
    conn.execute(
        """
        UPDATE decision_candidates
        SET status = ?, promotion_reason = ?, promoted_observation_id = COALESCE(?, promoted_observation_id), review_id = COALESCE(?, review_id)
        WHERE id = ?
        """,
        (status, reason, observation_id, review_id, candidate_id),
    )


def _evidence_payload(
    *,
    candidate: DecisionCandidate,
    opportunity: dict[str, Any] | None,
    receipt: dict[str, Any],
    evidence_source: str,
    confidence: float,
) -> dict[str, Any]:
    snapshot = dict(opportunity.get("pre_answer_snapshot") or {}) if opportunity is not None else {}
    question = str(opportunity.get("question_text") or "") if opportunity is not None else candidate.supporting_span
    objective = str(snapshot.get("objective") or "") if snapshot else ""
    alternatives = list(opportunity.get("alternatives") or []) if opportunity is not None else list(candidate.observed_alternatives)
    return {
        "situation_type": _situation_type_for(candidate, opportunity),
        "situation_summary": question[:500] or candidate.supporting_span[:500],
        "objective": objective or question[:500] or candidate.supporting_span[:500],
        "objective_text": objective,
        "constraints": {},
        "context_snapshot": snapshot,
        "available_choices": alternatives,
        "selected_choice": (candidate.selected_option or candidate.supporting_span)[:500],
        "rationale": candidate.stated_rationale or "",
        "action_taken": "",
        "confidence": confidence,
        "evidence_source": evidence_source,
        "memory_class": "decision",
        "lifecycle_status": "active",
        "source_event_ids": [str(receipt["event_id"])] if receipt.get("event_id") else [],
        "redaction_applied": True,
    }


def _server_only_keys(normalized: dict[str, Any], *, confirmed_at: datetime | None, opportunity_id: str | None, receipt_id: str) -> dict[str, Any]:
    # normalize_behavior_evidence drops unknown keys: the trusted-path-only fields are re-applied afterwards.
    normalized["confirmed_at"] = confirmed_at
    normalized["opportunity_id"] = opportunity_id
    normalized["origin_kind"] = OriginKind.HUMAN_INPUT.value
    normalized["capture_receipt_id"] = receipt_id
    normalized["extraction_version"] = EXTRACTION_VERSION
    return normalized


def _promote_answer(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: str,
    opportunity: dict[str, Any],
    now: datetime,
) -> str:
    from .store import save_behavior_evidence_lite  # noqa: PLC0415 - circular import guard

    shadow_id = str(opportunity["shadow_prediction_id"]) if opportunity.get("shadow_prediction_id") else None
    frozen_at: datetime | None = None
    if shadow_id:
        shadow_row = conn.execute("SELECT frozen_at FROM behavior_shadow_predictions WHERE id = ?", (shadow_id,)).fetchone()
        frozen_at = _parse_dt(shadow_row["frozen_at"]) if shadow_row is not None else None
    retrospective = is_retrospective(frozen_at, _answer_moment(receipt, now))

    raw = _evidence_payload(candidate=candidate, opportunity=opportunity, receipt=receipt, evidence_source="explicit", confidence=0.9)
    raw["supersedes_observation_id"] = None
    normalized = _server_only_keys(
        normalize_behavior_evidence(raw),
        confirmed_at=now,
        opportunity_id=str(opportunity["id"]),
        receipt_id=str(receipt["id"]),
    )
    gate = behavior_storage_gate(normalized, threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)))
    observation_id = save_behavior_evidence_lite(
        conn,
        consumer_id=f"extraction:{receipt.get('capture_principal') or 'host'}",
        workspace_id=str(receipt["workspace_id"]),
        subject_user_id=str(receipt["subject_user_id"]),
        evidence=normalized,
        storage_gate=gate,
    )
    selected = str(candidate.selected_option or "")
    insert_human_resolution(
        conn,
        resolution=HumanResolution(
            opportunity_id=str(opportunity["id"]),
            selected_choice=selected,
            resolution_source="host_capture",
            human_source_ref=str(receipt["id"]),
            resolved_at=now,
            stated_rationale=candidate.stated_rationale,
        ),
        workspace_id=str(receipt["workspace_id"]),
        subject_user_id=str(receipt["subject_user_id"]),
        receipt_id=str(receipt["id"]),
        source_event_id=str(receipt["event_id"]) if receipt.get("event_id") else None,
        candidate_id=candidate_id,
        observation_id=observation_id,
    )
    if shadow_id:
        resolve_shadow_prediction(
            conn,
            prediction_id=shadow_id,
            actual_choice=selected,
            observation_id=observation_id,
            resolution_source="host_capture",
            human_source_ref=str(receipt["id"]),
            resolution_source_event_id=str(receipt["event_id"]) if receipt.get("event_id") else None,
            resolved_at=now,
            retrospective=retrospective,
        )
    resolve_opportunity(conn, opportunity_id=str(opportunity["id"]), resolved_at=now)
    _update_candidate(conn, candidate_id, status="promoted", reason="unique_open_opportunity", observation_id=observation_id)
    return observation_id


def _promote_correction(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: str,
    opportunity: dict[str, Any],
    now: datetime,
) -> str:
    from .store import save_behavior_evidence_lite  # noqa: PLC0415 - circular import guard

    previous = latest_resolution_for_opportunity(conn, str(opportunity["id"]))
    supersedes_observation = str(previous["observation_id"]) if previous and previous.get("observation_id") else None
    raw = _evidence_payload(candidate=candidate, opportunity=opportunity, receipt=receipt, evidence_source="correction", confidence=0.9)
    raw["correction_text"] = candidate.supporting_span[:1000]
    raw["supersedes_observation_id"] = supersedes_observation
    normalized = _server_only_keys(
        normalize_behavior_evidence(raw),
        confirmed_at=now,
        opportunity_id=str(opportunity["id"]),
        receipt_id=str(receipt["id"]),
    )
    gate = behavior_storage_gate(normalized, threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)))
    observation_id = save_behavior_evidence_lite(
        conn,
        consumer_id=f"extraction:{receipt.get('capture_principal') or 'host'}",
        workspace_id=str(receipt["workspace_id"]),
        subject_user_id=str(receipt["subject_user_id"]),
        evidence=normalized,
        storage_gate=gate,
    )
    selected = str(candidate.selected_option or candidate.supporting_span[:500])
    insert_human_resolution(
        conn,
        resolution=HumanResolution(
            opportunity_id=str(opportunity["id"]),
            selected_choice=selected,
            resolution_source="host_capture",
            human_source_ref=str(receipt["id"]),
            resolved_at=now,
            correction_text=candidate.supporting_span[:1000],
            stated_rationale=candidate.stated_rationale,
            supersedes_resolution_id=str(previous["id"]) if previous else None,
        ),
        workspace_id=str(receipt["workspace_id"]),
        subject_user_id=str(receipt["subject_user_id"]),
        receipt_id=str(receipt["id"]),
        source_event_id=str(receipt["event_id"]) if receipt.get("event_id") else None,
        candidate_id=candidate_id,
        observation_id=observation_id,
    )
    if opportunity.get("shadow_prediction_id"):
        append_shadow_correction(
            conn,
            prediction_id=str(opportunity["shadow_prediction_id"]),
            correction={"at": _iso(now), "receipt_id": str(receipt["id"]), "selected_choice": selected, "candidate_id": candidate_id},
        )
    _update_candidate(conn, candidate_id, status="promoted", reason="correction", observation_id=observation_id)
    return observation_id


def _hold_for_review(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: str,
    opportunity: dict[str, Any] | None,
    promotion: Promotion,
    now: datetime,
) -> str:
    from .store import save_behavior_evidence_lite  # noqa: PLC0415 - circular import guard

    evidence_source = "backfill" if candidate.origin_kind == OriginKind.IMPORTED_TRANSCRIPT else "inferred"
    raw = _evidence_payload(candidate=candidate, opportunity=opportunity, receipt=receipt, evidence_source=evidence_source, confidence=0.6)
    raw["supersedes_observation_id"] = None
    normalized = _server_only_keys(
        normalize_behavior_evidence(raw),
        confirmed_at=None,
        opportunity_id=str(opportunity["id"]) if opportunity is not None else candidate.opportunity_id,
        receipt_id=str(receipt["id"]),
    )
    gate = behavior_storage_gate(normalized, threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)))
    reason = _pending_reason(candidate, opportunity)
    gate = {
        "learning_eligible": False,
        "score": gate["score"],
        "decision": "pending_review",
        "reasons": [*list(gate.get("reasons") or []), "human_review_required", f"extraction:{reason}"],
    }
    observation_id = save_behavior_evidence_lite(
        conn,
        consumer_id=f"extraction:{receipt.get('capture_principal') or 'host'}",
        workspace_id=str(receipt["workspace_id"]),
        subject_user_id=str(receipt["subject_user_id"]),
        evidence=normalized,
        storage_gate=gate,
    )
    review_id = create_memory_review(
        conn,
        workspace_id=str(receipt["workspace_id"]),
        subject_user_id=str(receipt["subject_user_id"]),
        target_type="evidence",
        target_id=observation_id,
        title=f"Extracted {candidate.kind.value}: {candidate.selected_option or candidate.supporting_span[:80]}",
        rationale=f"promotion={promotion.value} reason={reason}; extraction_version={EXTRACTION_VERSION}",
        source="decision_extraction",
        score=float(gate["score"]),
    )
    _update_candidate(conn, candidate_id, status="pending_review", reason=reason, observation_id=observation_id, review_id=review_id)
    return observation_id


def _extract_one(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    receipt_id: str,
    now: datetime,
    ttl_seconds: int,
) -> dict[str, Any]:
    lease_seconds = max(30, int(getattr(settings, "decision_extraction_lease_seconds", 300)))
    max_attempts = max(1, int(getattr(settings, "decision_extraction_max_attempts", 10)))
    cursor = conn.execute(
        """
        UPDATE trusted_input_receipts
        SET extraction_state = 'extracting', extraction_lease_until = ?, extraction_attempts = extraction_attempts + 1
        WHERE id = ?
          AND (extraction_state IN ('pending', 'failed') OR extraction_lease_until < ?)
          AND (extraction_version_done IS NULL OR extraction_version_done < ?)
        """,
        (_iso(now + timedelta(seconds=lease_seconds)), receipt_id, _iso(now), EXTRACTION_VERSION),
    )
    if int(cursor.rowcount or 0) != 1:
        conn.commit()
        return {"status": "skipped", "reason": "already_claimed"}
    conn.commit()
    receipt = get_receipt(conn, receipt_id=receipt_id)
    if receipt is None:
        return {"status": "missing"}
    counts = {"extracted": 0, "promoted": 0, "pending_review": 0, "discarded": 0}
    try:
        content = _load_receipt_content(conn, receipt)
        observed_at = _answer_moment(receipt, now)
        # Server time on the receipt: when the answer actually reached us. Expiry is judged here, never at the
        # host-supplied moment, so a host clock running behind cannot revive an already-expired question.
        expiry_at = max(observed_at, _parse_dt(receipt.get("ingested_at")) or now)
        since = observed_at - timedelta(seconds=max(0, int(ttl_seconds)))
        workspace_id = str(receipt["workspace_id"])
        subject_user_id = str(receipt["subject_user_id"])
        project_id = receipt.get("project_id")
        open_rows = open_opportunities_for_subject(
            conn,
            workspace_id=workspace_id,
            subject_user_id=subject_user_id,
            project_id=project_id,
            since=since,
            answer_moment=observed_at,
            expiry_at=expiry_at,
        )
        resolved_rows = resolved_opportunities_for_subject(
            conn, workspace_id=workspace_id, subject_user_id=subject_user_id, project_id=project_id, since=since
        )
        open_by_id = {str(row["id"]): row for row in open_rows}
        resolved_by_id = {str(row["id"]): row for row in resolved_rows}
        open_models = [opportunity_to_model(row) for row in open_rows]
        resolved_models = [opportunity_to_model(row) for row in resolved_rows]
        candidates = extract_candidates(
            {
                "content": content,
                "origin_kind": str(receipt.get("origin_kind") or OriginKind.HUMAN_INPUT.value),
                "observed_at": observed_at,
                "project_id": project_id,
                "task_id": None,
            },
            {"recently_resolved": resolved_models, "now": observed_at},
            open_models,
        )
        for candidate in candidates:
            # The human answered at the answer moment, not at wall clock: a message captured before its question
            # was frozen must not promote as that question's answer (shared ANSWER_PRECEDES_QUESTION). The
            # question's liveness is judged separately, at server time (expiry_at).
            promotion = promotion_decision(candidate, open_models, now=observed_at, expiry_now=expiry_at)
            candidate_id = str(uuid.uuid4())
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO decision_candidates (
                    id, receipt_id, source_event_id, workspace_id, subject_user_id, opportunity_id, candidate_kind,
                    supporting_span, span_sha256, observed_alternatives_json, selected_option, stated_rationale,
                    is_negated, is_correction, project_id, task_id, origin_kind, extraction_version, promotion,
                    promotion_reason, status, created_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'new', ?, 'v1')
                """,
                (
                    candidate_id,
                    receipt_id,
                    str(receipt["event_id"]) if receipt.get("event_id") else None,
                    workspace_id,
                    subject_user_id,
                    candidate.opportunity_id,
                    candidate.kind.value,
                    candidate.supporting_span,
                    candidate.span_sha256,
                    _json(list(candidate.observed_alternatives)),
                    candidate.selected_option,
                    candidate.stated_rationale,
                    1 if candidate.is_negated else 0,
                    1 if candidate.is_correction else 0,
                    candidate.project_id,
                    candidate.task_id,
                    candidate.origin_kind.value,
                    candidate.extraction_version,
                    promotion.value,
                    _iso(now),
                ),
            )
            if int(inserted.rowcount or 0) != 1:
                continue  # idempotent re-run: the same span was already recorded for this receipt/version
            counts["extracted"] += 1
            target = open_by_id.get(str(candidate.opportunity_id)) if candidate.opportunity_id else None
            if promotion == Promotion.PROMOTE and candidate.kind == CandidateKind.ANSWER and target is not None:
                _promote_answer(conn, settings=settings, receipt=receipt, candidate=candidate, candidate_id=candidate_id, opportunity=target, now=now)
                counts["promoted"] += 1
                # The target is now resolved: later candidates in this message must not re-promote it.
                open_models = [model for model in open_models if model.opportunity_id != str(target["id"])]
                open_by_id.pop(str(target["id"]), None)
            elif promotion == Promotion.PROMOTE and candidate.kind == CandidateKind.CORRECTION and candidate.opportunity_id in resolved_by_id:
                _promote_correction(
                    conn,
                    settings=settings,
                    receipt=receipt,
                    candidate=candidate,
                    candidate_id=candidate_id,
                    opportunity=resolved_by_id[str(candidate.opportunity_id)],
                    now=now,
                )
                counts["promoted"] += 1
            elif promotion == Promotion.DISCARD:
                _update_candidate(conn, candidate_id, status="discarded", reason=_discard_reason(candidate))
                counts["discarded"] += 1
            else:
                review_target = target or (resolved_by_id.get(str(candidate.opportunity_id)) if candidate.opportunity_id else None)
                _hold_for_review(
                    conn,
                    settings=settings,
                    receipt=receipt,
                    candidate=candidate,
                    candidate_id=candidate_id,
                    opportunity=review_target,
                    promotion=Promotion.PENDING_REVIEW,
                    now=now,
                )
                counts["pending_review"] += 1
        conn.execute(
            """
            UPDATE trusted_input_receipts
            SET extraction_state = 'extracted', extraction_version_done = ?, extraction_lease_until = NULL, extraction_last_error = NULL
            WHERE id = ?
            """,
            (EXTRACTION_VERSION, receipt_id),
        )
        conn.commit()
        return {"status": "extracted", **counts}
    except Exception as exc:
        conn.rollback()
        logger.warning("decision extraction failed for receipt %s", receipt_id, exc_info=True)
        attempts = max(1, int(receipt.get("extraction_attempts") or 1))  # row was read after the claim incremented it
        backoff = min(3600, 2**attempts)
        conn.execute(
            """
            UPDATE trusted_input_receipts
            SET extraction_state = 'failed', extraction_last_error = ?, extraction_lease_until = NULL, next_extraction_at = ?
            WHERE id = ?
            """,
            (str(exc)[:1000], _iso(now + timedelta(seconds=backoff)), receipt_id),
        )
        if attempts >= max_attempts:
            rows = conn.execute(
                "SELECT shadow_prediction_id FROM decision_opportunities WHERE workspace_id = ? AND subject_user_id = ? AND status = 'open'",
                (str(receipt["workspace_id"]), str(receipt["subject_user_id"])),
            ).fetchall()
            for row in rows:
                if row["shadow_prediction_id"]:
                    mark_shadow_unresolved(conn, prediction_id=str(row["shadow_prediction_id"]), resolution_state="extraction_error", resolution_source=None)
        conn.commit()
        return {"status": "failed", "error": str(exc)[:200]}


def extract_pending_inputs(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    limit: int,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    """Synchronous extraction fallback (Lite has no worker). Never raises."""
    result: dict[str, Any] = {
        "status": "ok",
        "processed": 0,
        "extracted": 0,
        "promoted": 0,
        "pending_review": 0,
        "discarded": 0,
        "failed": 0,
        "skipped": 0,
    }
    if not bool(getattr(settings, "capture_extraction_enabled", True)):
        result["status"] = "disabled"
        return result
    now = _now()
    ttl_seconds = int(getattr(settings, "capture_opportunity_ttl_seconds", 3600))
    max_attempts = max(1, int(getattr(settings, "decision_extraction_max_attempts", 10)))
    try:
        if receipt_id is not None:
            if get_receipt(conn, receipt_id=receipt_id) is None:
                result["status"] = "missing"
                return result
            ids = [receipt_id]
        else:
            result["sweep"] = sweep_stale_opportunities(conn, now=now, ttl_seconds=ttl_seconds)
            rows = conn.execute(
                """
                SELECT id FROM trusted_input_receipts
                WHERE extraction_state IN ('pending', 'failed')
                  AND (next_extraction_at IS NULL OR next_extraction_at <= ?)
                  AND (extraction_lease_until IS NULL OR extraction_lease_until < ?)
                  AND extraction_attempts < ?
                ORDER BY observed_at ASC
                LIMIT ?
                """,
                (_iso(now), _iso(now), max_attempts, max(1, int(limit))),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
        for current in ids:
            outcome = _extract_one(conn, settings=settings, receipt_id=current, now=now, ttl_seconds=ttl_seconds)
            status = str(outcome.get("status"))
            if status == "skipped":
                result["skipped"] += 1
                if receipt_id is not None:
                    result["status"] = "skipped"
                    result["reason"] = str(outcome.get("reason") or "already_claimed")
                continue
            if status == "missing":
                if receipt_id is not None:
                    result["status"] = "missing"
                continue
            result["processed"] += 1
            if status == "failed":
                result["failed"] += 1
                continue
            for key in ("extracted", "promoted", "pending_review", "discarded"):
                result[key] += int(outcome.get(key) or 0)
    except Exception:
        logger.warning("decision extraction sweep failed", exc_info=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        result["status"] = "error"
    return result
