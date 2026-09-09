"""Derive decision candidates from trusted human-input receipts (P1 §6).

Idempotency contract:
- a receipt is claimed with a lease (`extraction_state='extracting'`, `extraction_lease_until`); a second worker
  cannot claim it until the lease expires, and a finished receipt (`extraction_version_done >= version`) is never
  re-claimed;
- candidates are unique per (receipt, extraction_version, supporting span) so a retry after a partial failure
  resumes the unfinished candidate instead of duplicating it;
- the evidence write is checkpointed (committed) before the resolution/shadow updates so a resumed candidate
  reuses its observation instead of creating a second one.

Nothing here invents options or rationale: everything persisted comes from the shared extractor.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.behavior_control_store import create_memory_review
from tce_api.behavior_store import save_behavior_evidence

# tce_api.crypto is the stable import in both states of the shared refactor: it either holds the implementation
# or re-exports tce_shared.crypto verbatim, and the worker already depends on tce_api.
from tce_api.crypto import maybe_decrypt_payload
from tce_shared.behavior_fidelity import behavior_storage_gate, normalize_behavior_evidence
from tce_shared.decision_capture import (
    EXTRACTION_VERSION,
    CandidateKind,
    DecisionCandidate,
    DecisionOpportunity,
    OriginKind,
    Promotion,
    extract_candidates,
    is_retrospective,
    promotion_decision,
    resolve_prediction_fields,
)
from tce_shared.situation import classify_situation

from ..config import get_settings
from ..db import SessionLocal

LOGGER = logging.getLogger(__name__)

_COUNT_KEYS = ("processed", "extracted", "promoted", "pending_review", "discarded", "failed", "skipped")
_MAX_BACKOFF_SECONDS = 3600

_OPPORTUNITY_COLUMNS = """
    id, session_id, turn, objective_hash, task_id, project_id, decision_family, situation_type,
    question_text, alternatives_json, pre_answer_snapshot_json, evidence_revision, shadow_prediction_id,
    status, expires_at, created_at, frozen_at, resolved_at
"""


class _ExtractionError(RuntimeError):
    """Raised for a receipt-level failure that must be recorded on the receipt row."""


@dataclass(slots=True)
class _Knobs:
    batch_size: int
    lease_seconds: int
    max_attempts: int
    ttl_seconds: int
    storage_min_score: float
    encryption_secret: str
    version: str = EXTRACTION_VERSION


def _knobs(settings: Any, batch_size: int | None) -> _Knobs:
    return _Knobs(
        batch_size=max(1, min(1000, int(batch_size or getattr(settings, "decision_extraction_batch_size", 100) or 100))),
        lease_seconds=max(30, int(getattr(settings, "decision_extraction_lease_seconds", 300) or 300)),
        max_attempts=max(1, int(getattr(settings, "decision_extraction_max_attempts", 10) or 10)),
        ttl_seconds=max(60, int(getattr(settings, "capture_opportunity_ttl_seconds", 3600) or 3600)),
        storage_min_score=float(getattr(settings, "behavior_storage_min_score", 0.55) or 0.55),
        encryption_secret=str(getattr(settings, "security_encryption_secret", "") or ""),
    )


# --------------------------------------------------------------------------- row helpers


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _opportunity_from_row(row: dict[str, Any]) -> DecisionOpportunity:
    created_at = _utc(row.get("created_at")) or datetime.now(tz=UTC)
    return DecisionOpportunity(
        opportunity_id=str(row["id"]),
        decision_family=str(row.get("decision_family") or ""),
        situation_type=str(row.get("situation_type") or "choice_required"),
        question_text=str(row.get("question_text") or ""),
        alternatives=tuple(str(item) for item in _as_list(row.get("alternatives_json")) if str(item).strip()),
        status=str(row.get("status") or "open"),
        created_at=created_at,
        frozen_at=_utc(row.get("frozen_at")) or created_at,
        expires_at=_utc(row.get("expires_at")),
        session_id=str(row.get("session_id") or ""),
        objective_hash=row.get("objective_hash"),
        project_id=row.get("project_id"),
        task_id=row.get("task_id"),
        evidence_revision=row.get("evidence_revision"),
        resolved_at=_utc(row.get("resolved_at")),
    )


# --------------------------------------------------------------------------- entry point


def run(receipt_id: str | None = None, batch_size: int | None = None) -> dict[str, Any]:
    """Extract decision candidates for one receipt (``receipt_id``) or sweep a batch of pending receipts.

    Never raises; per-receipt failures are recorded on the receipt row with exponential backoff.
    """

    settings = get_settings()
    counts: dict[str, Any] = dict.fromkeys(_COUNT_KEYS, 0)
    if not bool(getattr(settings, "decision_extraction_enabled", True)):
        return {"status": "disabled", **counts}
    knobs = _knobs(settings, batch_size)
    now = datetime.now(tz=UTC)

    if receipt_id:
        parsed = _uuid_or_none(receipt_id)
        if parsed is None:
            return {"status": "missing", "receipt_id": receipt_id, **counts}
        ids = [parsed]
    else:
        try:
            ids = _sweep(knobs, now)
        except Exception:
            LOGGER.warning("decision extraction sweep selection failed", exc_info=True)
            return {"status": "ok", "reason": "sweep_failed", **counts}

    last_outcome = "ok"
    for item_id in ids:
        try:
            outcome = _process_receipt(item_id, knobs, now)
        except Exception:
            LOGGER.warning("decision extraction crashed for receipt %s", item_id, exc_info=True)
            outcome = {"result": "failed"}
        last_outcome = str(outcome.get("result") or "ok")
        if last_outcome == "missing":
            continue
        if last_outcome == "skipped":
            counts["skipped"] += 1
            continue
        counts["processed"] += 1
        for key in _COUNT_KEYS:
            counts[key] += int(outcome.get(key, 0) or 0)
        if last_outcome == "failed":
            counts["failed"] += 1

    if receipt_id and last_outcome == "missing":
        return {"status": "missing", "receipt_id": receipt_id, **counts}
    if receipt_id and last_outcome == "skipped":
        return {"status": "skipped", "reason": "already_claimed", "receipt_id": receipt_id, **counts}
    return {"status": "ok", **counts}


# --------------------------------------------------------------------------- selection + claim


def _sweep(knobs: _Knobs, now: datetime) -> list[UUID]:
    with SessionLocal() as db:
        try:
            _housekeeping(db, knobs, now)
            db.commit()
        except Exception:
            LOGGER.warning("decision opportunity housekeeping failed", exc_info=True)
            db.rollback()
        rows = (
            db.execute(
                text(
                    """
                    SELECT id FROM trusted_input_receipts
                    WHERE (
                            extraction_state IN ('pending', 'failed')
                         OR (extraction_state = 'extracting' AND extraction_lease_until < :now)
                          )
                      AND (next_extraction_at IS NULL OR next_extraction_at <= :now)
                      AND (extraction_lease_until IS NULL OR extraction_lease_until < :now)
                      AND extraction_attempts < :max_attempts
                      AND (extraction_version_done IS NULL OR extraction_version_done < :version)
                    ORDER BY observed_at ASC
                    LIMIT :batch
                    """
                ),
                {"now": now, "max_attempts": knobs.max_attempts, "version": knobs.version, "batch": knobs.batch_size},
            )
            .mappings()
            .all()
        )
    return [item for item in (_uuid_or_none(row.get("id")) for row in rows) if item is not None]


def _housekeeping(db: Session, knobs: _Knobs, now: datetime) -> None:
    """Close stale opportunities so their shadow rows land in the right (separate) denominators."""

    relay_cutoff = now - timedelta(seconds=knobs.ttl_seconds)
    relayed = (
        db.execute(
            text(
                """
                UPDATE decision_opportunities
                SET status = 'expired', resolved_at = :now
                WHERE status = 'open' AND relayed_at IS NOT NULL AND relayed_at < :cutoff
                RETURNING shadow_prediction_id
                """
            ),
            {"now": now, "cutoff": relay_cutoff},
        )
        .mappings()
        .all()
    )
    _mark_shadow_rows(db, [row.get("shadow_prediction_id") for row in relayed], resolution_state="missing_label", resolution_source="executor_relayed")
    expired = (
        db.execute(
            text(
                """
                UPDATE decision_opportunities
                SET status = 'expired', resolved_at = :now
                WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at < :now
                RETURNING shadow_prediction_id
                """
            ),
            {"now": now},
        )
        .mappings()
        .all()
    )
    _mark_shadow_rows(db, [row.get("shadow_prediction_id") for row in expired], resolution_state="unanswered", resolution_source="sweep")


def _mark_shadow_rows(db: Session, raw_ids: list[Any], *, resolution_state: str, resolution_source: str) -> None:
    ids = [item for item in (_uuid_or_none(value) for value in raw_ids) if item is not None]
    if not ids:
        return
    db.execute(
        text(
            """
            UPDATE behavior_shadow_predictions
            SET resolution_state = :resolution_state, resolution_source = :resolution_source
            WHERE id = ANY(:ids) AND resolution_state = 'pending'
            """
        ),
        {"resolution_state": resolution_state, "resolution_source": resolution_source, "ids": ids},
    )


def _claim(db: Session, receipt_id: UUID, knobs: _Knobs, now: datetime) -> dict[str, Any] | None:
    row = (
        db.execute(
            text(
                """
                UPDATE trusted_input_receipts
                SET extraction_state = 'extracting',
                    extraction_lease_until = :lease,
                    extraction_attempts = extraction_attempts + 1
                WHERE id = :id
                  AND (extraction_state IN ('pending', 'failed') OR extraction_lease_until < :now)
                  AND (extraction_version_done IS NULL OR extraction_version_done < :version)
                RETURNING id, workspace_id, owner_id, subject_user_id, host_session_id, event_id, origin_kind,
                          capture_principal, project_id, observed_at, ingested_at, content_sha256, extraction_attempts
                """
            ),
            {"id": receipt_id, "lease": now + timedelta(seconds=knobs.lease_seconds), "now": now, "version": knobs.version},
        )
        .mappings()
        .first()
    )
    db.commit()
    return dict(row) if row is not None else None


def _process_receipt(receipt_id: UUID, knobs: _Knobs, now: datetime) -> dict[str, Any]:
    with SessionLocal() as db:
        receipt = _claim(db, receipt_id, knobs, now)
        if receipt is None:
            exists = db.execute(text("SELECT id FROM trusted_input_receipts WHERE id = :id"), {"id": receipt_id}).mappings().first()
            return {"result": "missing" if exists is None else "skipped"}
        try:
            counts = _extract_receipt(db, receipt, knobs, now)
        except Exception as exc:
            _mark_failed(db, receipt, knobs, now, exc)
            return {"result": "failed"}
        return {"result": "extracted", **counts}


# --------------------------------------------------------------------------- load


def _load_content(db: Session, receipt: dict[str, Any], knobs: _Knobs) -> str:
    event_id = _uuid_or_none(receipt.get("event_id"))
    if event_id is None:
        raise _ExtractionError("content: receipt has no source event")
    row = db.execute(text("SELECT payload, context FROM events WHERE id = :event_id"), {"event_id": event_id}).mappings().first()
    if row is None:
        raise _ExtractionError("content: source event missing")
    try:
        payload = maybe_decrypt_payload(_as_dict(row.get("payload")), secret=knobs.encryption_secret)
    except Exception as exc:
        raise _ExtractionError(f"decrypt: {exc}") from exc
    return str(payload.get("input_excerpt") or "")


def _load_opportunities(
    db: Session, receipt: dict[str, Any], *, status: str, since: datetime, observed_at: datetime, expiry_at: datetime
) -> list[dict[str, Any]]:
    time_column = "resolved_at" if status == "resolved" else "created_at"
    # A question frozen after the human spoke cannot be the question this message answers: mirrors the shared
    # `answer_precedes_question` rule (promotion_decision) so the SQL load and the promotion agree. Expiry is
    # judged at server time (`expiry_at`) for the same reason promotion_decision does: a host clock running
    # behind must not rewind a stale answer back inside an expired question's window.
    freeze_bound = "AND frozen_at <= :observed_at AND (expires_at IS NULL OR expires_at > :expiry_at)" if status == "open" else ""
    rows = (
        db.execute(
            text(
                f"""
                SELECT {_OPPORTUNITY_COLUMNS}
                FROM decision_opportunities
                WHERE workspace_id = :workspace_id
                  AND subject_user_id = :subject_user_id
                  AND status = :status
                  AND {time_column} >= :since
                  {freeze_bound}
                  AND (project_id IS NULL OR CAST(:project_id AS TEXT) IS NULL OR project_id = :project_id)
                ORDER BY created_at DESC
                """
            ),
            {
                "workspace_id": str(receipt.get("workspace_id") or ""),
                "subject_user_id": str(receipt.get("subject_user_id") or ""),
                "status": status,
                "since": since,
                "observed_at": observed_at,
                "expiry_at": expiry_at,
                "project_id": receipt.get("project_id"),
            },
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _answer_moment(receipt: dict[str, Any], fallback: datetime) -> datetime:
    """The moment the human answered, clamped to server time.

    `observed_at` is host-supplied; a host clock running ahead would otherwise make a prediction frozen AFTER the
    answer look prospective (inflating the exit-gate denominator) and let a message promote as the answer to a
    question that did not exist yet. `ingested_at` is server time on the same receipt.
    """

    observed_at = _utc(receipt.get("observed_at")) or fallback
    ingested_at = _utc(receipt.get("ingested_at"))
    return min(observed_at, ingested_at) if ingested_at is not None else observed_at


# --------------------------------------------------------------------------- extract + persist


def _extract_receipt(db: Session, receipt: dict[str, Any], knobs: _Knobs, now: datetime) -> dict[str, int]:
    content = _load_content(db, receipt, knobs)
    observed_at = _answer_moment(receipt, now)
    # Server time on the receipt: when the answer actually reached us. Expiry is judged here, never at the
    # host-supplied moment, so a host clock running behind cannot revive an already-expired question.
    expiry_at = max(observed_at, _utc(receipt.get("ingested_at")) or now)
    since = observed_at - timedelta(seconds=knobs.ttl_seconds)
    open_rows = _load_opportunities(db, receipt, status="open", since=since, observed_at=observed_at, expiry_at=expiry_at)
    resolved_rows = _load_opportunities(db, receipt, status="resolved", since=since, observed_at=observed_at, expiry_at=expiry_at)
    rows_by_id = {str(row["id"]): row for row in [*open_rows, *resolved_rows]}
    open_opps = [_opportunity_from_row(row) for row in open_rows]
    resolved_opps = [_opportunity_from_row(row) for row in resolved_rows]
    opps_by_id = {opp.opportunity_id: opp for opp in [*open_opps, *resolved_opps]}

    message = {
        "content": content,
        "origin_kind": str(receipt.get("origin_kind") or OriginKind.HUMAN_INPUT.value),
        "observed_at": observed_at,
        "project_id": receipt.get("project_id"),
        "task_id": None,
    }
    candidates = extract_candidates(message, {"recently_resolved": resolved_opps, "now": observed_at}, open_opps, knobs.version)

    counts = dict.fromkeys(_COUNT_KEYS, 0)
    for candidate in candidates:
        # The human answered at observed_at; the question's liveness is judged at server time (expiry_at).
        promotion = promotion_decision(candidate, open_opps, now=observed_at, expiry_now=expiry_at)
        counts["extracted"] += 1
        outcome = _persist_candidate(db, receipt, candidate, promotion, open_opps, rows_by_id, opps_by_id, knobs, now, observed_at)
        counts[outcome] += 1

    db.execute(
        text(
            """
            UPDATE trusted_input_receipts
            SET extraction_state = 'extracted', extraction_version_done = :version,
                extraction_lease_until = NULL, extraction_last_error = NULL
            WHERE id = :id
            """
        ),
        {"id": receipt["id"], "version": knobs.version},
    )
    db.commit()
    return counts


def _promotion_reason(candidate: DecisionCandidate, promotion: Promotion) -> str:
    if candidate.contradicted:
        return "contradictory"
    if promotion is Promotion.DISCARD:
        return candidate.kind.value
    if promotion is Promotion.PROMOTE:
        return "correction" if candidate.kind is CandidateKind.CORRECTION else "unique_open_opportunity"
    if candidate.origin_kind is OriginKind.IMPORTED_TRANSCRIPT:
        return "imported_transcript"
    if candidate.kind is CandidateKind.ANSWER:
        return "ambiguous_or_expired_target"
    if candidate.kind is CandidateKind.CORRECTION:
        return "correction_without_unique_target"
    return candidate.kind.value


def _insert_candidate(
    db: Session,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    promotion: Promotion,
    reason: str,
    now: datetime,
) -> tuple[UUID | None, UUID | None]:
    """Insert the candidate; on conflict resume it only if it is still unfinished. Returns (candidate_id, existing_observation_id)."""

    initial_status = "discarded" if promotion is Promotion.DISCARD else "new"
    row = (
        db.execute(
            text(
                """
                INSERT INTO decision_candidates (
                    id, receipt_id, source_event_id, workspace_id, subject_user_id, opportunity_id, candidate_kind,
                    supporting_span, span_sha256, observed_alternatives_json, selected_option, stated_rationale,
                    is_negated, is_correction, project_id, task_id, origin_kind, extraction_version, promotion,
                    promotion_reason, status, created_at, schema_version
                ) VALUES (
                    :id, :receipt_id, :source_event_id, :workspace_id, :subject_user_id, :opportunity_id, :candidate_kind,
                    :supporting_span, :span_sha256, CAST(:observed_alternatives_json AS jsonb), :selected_option, :stated_rationale,
                    :is_negated, :is_correction, :project_id, :task_id, :origin_kind, :extraction_version, :promotion,
                    :promotion_reason, :status, :created_at, 'v1'
                )
                ON CONFLICT (receipt_id, extraction_version, span_sha256) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": uuid.uuid4(),
                "receipt_id": receipt["id"],
                "source_event_id": _uuid_or_none(receipt.get("event_id")),
                "workspace_id": str(receipt.get("workspace_id") or ""),
                "subject_user_id": str(receipt.get("subject_user_id") or ""),
                "opportunity_id": _uuid_or_none(candidate.opportunity_id),
                "candidate_kind": candidate.kind.value,
                "supporting_span": candidate.supporting_span,
                "span_sha256": candidate.span_sha256,
                "observed_alternatives_json": json.dumps(list(candidate.observed_alternatives)),
                "selected_option": candidate.selected_option,
                "stated_rationale": candidate.stated_rationale,
                "is_negated": bool(candidate.is_negated),
                "is_correction": bool(candidate.is_correction),
                "project_id": candidate.project_id,
                "task_id": candidate.task_id,
                "origin_kind": candidate.origin_kind.value,
                "extraction_version": candidate.extraction_version,
                "promotion": promotion.value,
                "promotion_reason": reason,
                "status": initial_status,
                "created_at": now,
            },
        )
        .mappings()
        .first()
    )
    if row is not None:
        return _uuid_or_none(row.get("id")), None
    existing = (
        db.execute(
            text(
                """
                SELECT id, status, promoted_observation_id FROM decision_candidates
                WHERE receipt_id = :receipt_id AND extraction_version = :extraction_version AND span_sha256 = :span_sha256
                """
            ),
            {"receipt_id": receipt["id"], "extraction_version": candidate.extraction_version, "span_sha256": candidate.span_sha256},
        )
        .mappings()
        .first()
    )
    if existing is None or str(existing.get("status") or "") != "new":
        return None, None
    return _uuid_or_none(existing.get("id")), _uuid_or_none(existing.get("promoted_observation_id"))


def _persist_candidate(
    db: Session,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    promotion: Promotion,
    open_opps: list[DecisionOpportunity],
    rows_by_id: dict[str, dict[str, Any]],
    opps_by_id: dict[str, DecisionOpportunity],
    knobs: _Knobs,
    now: datetime,
    observed_at: datetime,
) -> str:
    reason = _promotion_reason(candidate, promotion)
    candidate_id, existing_observation_id = _insert_candidate(db, receipt, candidate, promotion, reason, now)
    if candidate_id is None:
        return "skipped"
    if promotion is Promotion.DISCARD:
        return "discarded"
    target_row = rows_by_id.get(candidate.opportunity_id or "")
    target = opps_by_id.get(candidate.opportunity_id or "")
    resumed = existing_observation_id is not None
    if promotion is Promotion.PROMOTE and target_row is not None and target is not None:
        if candidate.kind is CandidateKind.ANSWER:
            _promote_answer(db, receipt, candidate, candidate_id, target_row, target, existing_observation_id, resumed, knobs, now, observed_at)
            return "promoted"
        if candidate.kind is CandidateKind.CORRECTION:
            _promote_correction(db, receipt, candidate, candidate_id, target_row, target, existing_observation_id, resumed, knobs, now)
            return "promoted"
    # PENDING_REVIEW, or a PROMOTE whose target vanished between extraction and persistence: never guess.
    _queue_review(db, receipt, candidate, candidate_id, target_row, target, promotion, reason, existing_observation_id, knobs, now)
    return "pending_review"


def _save_evidence(
    db: Session,
    *,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: UUID,
    situation_type: str,
    situation_summary: str,
    snapshot: dict[str, Any],
    available_choices: list[str],
    selected_choice: str,
    evidence_source: str,
    confirmed_at: datetime | None,
    opportunity_id: UUID | None,
    knobs: _Knobs,
    now: datetime,
    correction_text: str = "",
    supersedes_observation_id: UUID | None = None,
    pending_review: bool = False,
) -> tuple[UUID, dict[str, Any]]:
    """Write one observation through the single evidence writer, stamp provenance, and checkpoint-commit."""

    raw: dict[str, Any] = {
        "situation_type": situation_type,
        "situation_summary": situation_summary[:500],
        "objective": str(snapshot.get("objective") or ""),
        "constraints": {},
        "context_snapshot": snapshot,
        "available_choices": available_choices,
        "selected_choice": selected_choice,
        "rationale": candidate.stated_rationale or "",
        "action_taken": "",
        "correction_text": correction_text,
        "confidence": 0.9,
        "evidence_source": evidence_source,
        "memory_class": "decision",
        "lifecycle_status": "active",
        "source_event_ids": [str(receipt["event_id"])] if receipt.get("event_id") else [],
        "supersedes_observation_id": supersedes_observation_id,
    }
    normalized = normalize_behavior_evidence(raw, now=now)
    # Server-only provenance keys are re-applied after normalization; they are never caller-supplied.
    normalized.update(
        {
            "confirmed_at": confirmed_at,
            "opportunity_id": opportunity_id,
            "origin_kind": candidate.origin_kind.value,
            "capture_receipt_id": receipt["id"],
            "extraction_version": knobs.version,
            "redaction_applied": True,
        }
    )
    gate = behavior_storage_gate(normalized, threshold=knobs.storage_min_score)
    if pending_review:
        gate = {"learning_eligible": False, "score": gate["score"], "decision": "pending_review", "reasons": gate["reasons"]}
    observation_id = save_behavior_evidence(
        db,
        consumer_id=f"extraction:{receipt.get('capture_principal') or 'unknown'}",
        workspace_id=str(receipt.get("workspace_id") or ""),
        subject_user_id=str(receipt.get("subject_user_id") or ""),
        evidence=normalized,
        storage_gate=gate,
    )
    db.execute(
        text(
            """
            UPDATE decision_observations
            SET opportunity_id = :opportunity_id, origin_kind = :origin_kind,
                capture_receipt_id = :capture_receipt_id, extraction_version = :extraction_version
            WHERE id = :id
            """
        ),
        {
            "id": observation_id,
            "opportunity_id": opportunity_id,
            "origin_kind": candidate.origin_kind.value,
            "capture_receipt_id": receipt["id"],
            "extraction_version": knobs.version,
        },
    )
    db.execute(
        text("UPDATE decision_candidates SET promoted_observation_id = :observation_id WHERE id = :id"),
        {"observation_id": observation_id, "id": candidate_id},
    )
    db.commit()
    return observation_id, gate


def _resolution_exists(db: Session, candidate_id: UUID) -> bool:
    row = db.execute(text("SELECT id FROM human_resolutions WHERE candidate_id = :candidate_id LIMIT 1"), {"candidate_id": candidate_id}).mappings().first()
    return row is not None


def _insert_resolution(
    db: Session,
    *,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: UUID,
    opportunity_id: UUID,
    selected_choice: str,
    observation_id: UUID,
    now: datetime,
    correction_text: str = "",
    supersedes_resolution_id: UUID | None = None,
) -> UUID:
    resolution_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO human_resolutions (
                id, opportunity_id, workspace_id, subject_user_id, receipt_id, source_event_id, candidate_id,
                selected_choice, correction_text, stated_rationale, resolution_source, human_source_ref,
                observation_id, supersedes_resolution_id, resolved_at, created_at, schema_version
            ) VALUES (
                :id, :opportunity_id, :workspace_id, :subject_user_id, :receipt_id, :source_event_id, :candidate_id,
                :selected_choice, :correction_text, :stated_rationale, 'host_capture', :human_source_ref,
                :observation_id, :supersedes_resolution_id, :resolved_at, :created_at, 'v1'
            )
            """
        ),
        {
            "id": resolution_id,
            "opportunity_id": opportunity_id,
            "workspace_id": str(receipt.get("workspace_id") or ""),
            "subject_user_id": str(receipt.get("subject_user_id") or ""),
            "receipt_id": receipt["id"],
            "source_event_id": _uuid_or_none(receipt.get("event_id")),
            "candidate_id": candidate_id,
            "selected_choice": selected_choice[:500],
            "correction_text": correction_text[:1000],
            "stated_rationale": candidate.stated_rationale,
            "human_source_ref": str(receipt["id"]),
            "observation_id": observation_id,
            "supersedes_resolution_id": supersedes_resolution_id,
            "resolved_at": now,
            "created_at": now,
        },
    )
    return resolution_id


def _promote_answer(
    db: Session,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: UUID,
    target_row: dict[str, Any],
    target: DecisionOpportunity,
    existing_observation_id: UUID | None,
    resumed: bool,
    knobs: _Knobs,
    now: datetime,
    observed_at: datetime,
) -> None:
    opportunity_id = UUID(target.opportunity_id)
    shadow_id = _uuid_or_none(target_row.get("shadow_prediction_id"))
    shadow: dict[str, Any] | None = None
    if shadow_id is not None:
        row = (
            db.execute(
                text("SELECT id, frozen_at, predicted_choice, abstained FROM behavior_shadow_predictions WHERE id = :id"),
                {"id": shadow_id},
            )
            .mappings()
            .first()
        )
        shadow = dict(row) if row is not None else None
    # Freeze-order check: a prediction frozen at/after the human answer is retrospective and is labelled so.
    retrospective = is_retrospective(_utc(shadow.get("frozen_at")) if shadow else None, observed_at)
    selected_choice = str(candidate.selected_option or "")
    snapshot = _as_dict(target_row.get("pre_answer_snapshot_json"))

    observation_id = existing_observation_id
    if observation_id is None:
        observation_id, _gate = _save_evidence(
            db,
            receipt=receipt,
            candidate=candidate,
            candidate_id=candidate_id,
            situation_type=target.situation_type,
            situation_summary=target.question_text,
            snapshot=snapshot,
            available_choices=list(target.alternatives),
            selected_choice=selected_choice,
            evidence_source="explicit",
            confirmed_at=now,
            opportunity_id=opportunity_id,
            knobs=knobs,
            now=now,
        )
    if not resumed or not _resolution_exists(db, candidate_id):
        _insert_resolution(
            db,
            receipt=receipt,
            candidate=candidate,
            candidate_id=candidate_id,
            opportunity_id=opportunity_id,
            selected_choice=selected_choice,
            observation_id=observation_id,
            now=now,
        )
    if shadow is not None:
        correct = resolve_prediction_fields(shadow.get("predicted_choice"), bool(shadow.get("abstained", True)), selected_choice)["correct"]
        db.execute(
            text(
                """
                UPDATE behavior_shadow_predictions
                SET actual_choice = :actual_choice, correct = :correct, observation_id = :observation_id,
                    resolved_at = :resolved_at, resolution_state = 'resolved', resolution_source = 'host_capture',
                    human_source_ref = :human_source_ref, resolution_source_event_id = :resolution_source_event_id,
                    prediction_stage = CASE WHEN CAST(:retro AS BOOLEAN) THEN 'retrospective' ELSE prediction_stage END
                WHERE id = :id AND resolution_state = 'pending'
                """
            ),
            {
                "id": shadow_id,
                "actual_choice": selected_choice,
                "correct": correct,
                "observation_id": observation_id,
                "resolved_at": now,
                "human_source_ref": str(receipt["id"]),
                "resolution_source_event_id": _uuid_or_none(receipt.get("event_id")),
                "retro": bool(retrospective),
            },
        )
    db.execute(
        text("UPDATE decision_opportunities SET status = 'resolved', resolved_at = :resolved_at WHERE id = :id AND status = 'open'"),
        {"id": opportunity_id, "resolved_at": now},
    )
    db.execute(
        text("UPDATE decision_candidates SET status = 'promoted', promoted_observation_id = :observation_id WHERE id = :id"),
        {"id": candidate_id, "observation_id": observation_id},
    )


def _promote_correction(
    db: Session,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: UUID,
    target_row: dict[str, Any],
    target: DecisionOpportunity,
    existing_observation_id: UUID | None,
    resumed: bool,
    knobs: _Knobs,
    now: datetime,
) -> None:
    opportunity_id = UUID(target.opportunity_id)
    previous = (
        db.execute(
            text("SELECT id, observation_id FROM human_resolutions WHERE opportunity_id = :opportunity_id ORDER BY resolved_at DESC LIMIT 1"),
            {"opportunity_id": opportunity_id},
        )
        .mappings()
        .first()
    )
    previous_resolution_id = _uuid_or_none(previous.get("id")) if previous is not None else None
    previous_observation_id = _uuid_or_none(previous.get("observation_id")) if previous is not None else None
    selected_choice = str(candidate.selected_option or candidate.supporting_span[:500])
    correction_text = candidate.supporting_span[:1000]
    snapshot = _as_dict(target_row.get("pre_answer_snapshot_json"))

    observation_id = existing_observation_id
    if observation_id is None:
        observation_id, _gate = _save_evidence(
            db,
            receipt=receipt,
            candidate=candidate,
            candidate_id=candidate_id,
            situation_type=target.situation_type,
            situation_summary=target.question_text,
            snapshot=snapshot,
            available_choices=list(target.alternatives),
            selected_choice=selected_choice,
            evidence_source="correction",
            confirmed_at=now,
            opportunity_id=opportunity_id,
            knobs=knobs,
            now=now,
            correction_text=correction_text,
            supersedes_observation_id=previous_observation_id,
        )
    if not resumed or not _resolution_exists(db, candidate_id):
        _insert_resolution(
            db,
            receipt=receipt,
            candidate=candidate,
            candidate_id=candidate_id,
            opportunity_id=opportunity_id,
            selected_choice=selected_choice,
            observation_id=observation_id,
            now=now,
            correction_text=correction_text,
            supersedes_resolution_id=previous_resolution_id,
        )
    shadow_id = _uuid_or_none(target_row.get("shadow_prediction_id"))
    if shadow_id is not None:
        # Append-only correction history; the original actual_choice/correct are never overwritten.
        entry = [{"at": now.isoformat(), "receipt_id": str(receipt["id"]), "selected_choice": selected_choice, "candidate_id": str(candidate_id)}]
        db.execute(
            text(
                """
                UPDATE behavior_shadow_predictions
                SET corrections_json = COALESCE(corrections_json, '[]'::jsonb) || CAST(:entry AS jsonb)
                WHERE id = :id
                """
            ),
            {"id": shadow_id, "entry": json.dumps(entry)},
        )
    db.execute(
        text("UPDATE decision_candidates SET status = 'promoted', promoted_observation_id = :observation_id WHERE id = :id"),
        {"id": candidate_id, "observation_id": observation_id},
    )


def _queue_review(
    db: Session,
    receipt: dict[str, Any],
    candidate: DecisionCandidate,
    candidate_id: UUID,
    target_row: dict[str, Any] | None,
    target: DecisionOpportunity | None,
    promotion: Promotion,
    reason: str,
    existing_observation_id: UUID | None,
    knobs: _Knobs,
    now: datetime,
) -> None:
    evidence_source = "backfill" if candidate.origin_kind is OriginKind.IMPORTED_TRANSCRIPT else "inferred"
    if target is not None:
        situation_type = target.situation_type
        situation_summary = target.question_text
        available_choices = list(target.alternatives) or list(candidate.observed_alternatives)
        opportunity_id: UUID | None = UUID(target.opportunity_id)
    else:
        situation_type = classify_situation(candidate.supporting_span, semantic_enabled=False)
        situation_summary = candidate.supporting_span
        available_choices = list(candidate.observed_alternatives)
        opportunity_id = None
    selected_choice = str(candidate.selected_option or candidate.supporting_span[:500])
    snapshot = _as_dict(target_row.get("pre_answer_snapshot_json")) if target_row is not None else {}

    observation_id = existing_observation_id
    gate_score = 0.0
    if observation_id is None:
        observation_id, gate = _save_evidence(
            db,
            receipt=receipt,
            candidate=candidate,
            candidate_id=candidate_id,
            situation_type=situation_type,
            situation_summary=situation_summary,
            snapshot=snapshot,
            available_choices=available_choices,
            selected_choice=selected_choice,
            evidence_source=evidence_source,
            confirmed_at=None,
            opportunity_id=opportunity_id,
            knobs=knobs,
            now=now,
            correction_text=candidate.supporting_span[:1000] if candidate.is_correction else "",
            pending_review=True,
        )
        gate_score = float(gate.get("score", 0.0) or 0.0)
    review_id = create_memory_review(
        db,
        workspace_id=str(receipt.get("workspace_id") or ""),
        subject_user_id=str(receipt.get("subject_user_id") or ""),
        target_type="evidence",
        target_id=observation_id,
        title=f"Extracted {candidate.kind.value}: {(candidate.selected_option or candidate.supporting_span)[:80]}",
        rationale=f"promotion={promotion.value} reason={reason}; extraction_version={knobs.version}",
        source="decision_extraction",
        score=gate_score,
    )
    db.execute(
        text("UPDATE decision_candidates SET status = 'pending_review', review_id = :review_id, promoted_observation_id = :observation_id WHERE id = :id"),
        {"id": candidate_id, "review_id": review_id, "observation_id": observation_id},
    )


# --------------------------------------------------------------------------- failure


def _mark_failed(db: Session, receipt: dict[str, Any], knobs: _Knobs, now: datetime, exc: BaseException) -> None:
    try:
        db.rollback()
        attempts = max(1, int(receipt.get("extraction_attempts") or 1))
        backoff = min(_MAX_BACKOFF_SECONDS, 2**attempts)
        error = f"{type(exc).__name__}: {exc}"[:1000]
        db.execute(
            text(
                """
                UPDATE trusted_input_receipts
                SET extraction_state = 'failed', extraction_last_error = :error,
                    extraction_lease_until = NULL, next_extraction_at = :next_extraction_at
                WHERE id = :id
                """
            ),
            {"id": receipt["id"], "error": error, "next_extraction_at": now + timedelta(seconds=backoff)},
        )
        if attempts >= knobs.max_attempts:
            _mark_extraction_error(db, receipt, knobs, now)
        db.commit()
    except Exception:
        LOGGER.warning("failed to record extraction failure for receipt %s", receipt.get("id"), exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    LOGGER.warning("decision extraction failed for receipt %s: %s", receipt.get("id"), exc)


def _mark_extraction_error(db: Session, receipt: dict[str, Any], knobs: _Knobs, now: datetime) -> None:
    """Best effort: opportunities this dead receipt could have answered land in the extraction_error denominator."""

    observed_at = _answer_moment(receipt, now)
    try:
        db.execute(
            text(
                """
                UPDATE behavior_shadow_predictions
                SET resolution_state = 'extraction_error', resolution_source = 'host_capture'
                WHERE resolution_state = 'pending'
                  AND id IN (
                      SELECT shadow_prediction_id FROM decision_opportunities
                      WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
                        AND status = 'open' AND shadow_prediction_id IS NOT NULL
                        AND created_at >= :since AND created_at <= :observed_at
                  )
                """
            ),
            {
                "workspace_id": str(receipt.get("workspace_id") or ""),
                "subject_user_id": str(receipt.get("subject_user_id") or ""),
                "since": observed_at - timedelta(seconds=knobs.ttl_seconds),
                "observed_at": observed_at,
            },
        )
    except Exception:
        LOGGER.warning("could not mark shadow rows as extraction_error for receipt %s", receipt.get("id"), exc_info=True)


__all__ = ["run"]
