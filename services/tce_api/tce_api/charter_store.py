"""Authority-charter persistence for the FULL backend.

The charter sits ABOVE the existing permit ladder: ``authority_charters`` (owner-approved,
versioned, expiring) narrows ``execution_permits`` (per-directive) which narrows
``capability_grants`` (per-action).  This module is the single resolver — **nothing anywhere
resolves a charter from a request body**, because every field of an executor's request body is
client-supplied.

U3: ``revoke_charter`` invalidates already-issued permits in the same transaction.  Revocation that
only removes future enforcement stops nothing that is already running.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.charter import (
    CHARTER_POLICY_REVISION,
    CHARTER_SCHEMA_VERSION,
    CharterInvalid,
    ResolvedCharter,
    charter_from_row,
    charter_payload_digest,
    narrow_charter,
    require_charter_for_action,
    resolved_charter_to_json,
    validate_charter_payload,
)
from tce_shared.events import CharterCreateRequest, CharterNarrowingRequest

from .auth import AuthContext
from .config import Settings, get_settings


def _receipt_exists(db: Session, *, workspace_id: str, owner_id: str, receipt_id: uuid.UUID) -> bool:
    """A charter (wider) costs at least what a narrowing (stricter) costs: a trusted receipt."""
    row = db.execute(
        text(
            """
            SELECT id
            FROM trusted_input_receipts
            WHERE id = :receipt_id
              AND workspace_id = :workspace_id
              AND (owner_id = :owner_id OR subject_user_id = :owner_id)
            LIMIT 1
            """
        ),
        {"receipt_id": receipt_id, "workspace_id": workspace_id, "owner_id": owner_id},
    ).first()
    return row is not None


def _charter_response(row: Any, charter: ResolvedCharter | None, *, now: datetime) -> dict[str, Any]:
    return {
        "charter": resolved_charter_to_json(charter) if charter is not None else None,
        "charter_id": row["id"],
        "status": str(row["status"]),
        "charter_digest": str(row["charter_digest"] or ""),
        "charter_version": str(row["charter_version"] or CHARTER_SCHEMA_VERSION),
        "policy_revision": str(row["policy_revision"] or CHARTER_POLICY_REVISION),
        "enforcement_tier": str(row["enforcement_tier"] or ""),
        "credential_risk_acknowledged": bool(row["credential_risk_acknowledged"]),
        "approved_by": (str(row["approved_by"]) if row["approved_by"] else None),
        "approved_at": row["approved_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "superseded_by": row["superseded_by"],
        "narrowing_ids": list(charter.narrowing_ids) if charter is not None else [],
        "generated_at": now,
        "schema_version": CHARTER_SCHEMA_VERSION,
    }


_CHARTER_COLUMNS = """
    id, workspace_id, owner_id, project_id, charter_version, policy_revision, status,
    enforcement_tier, credential_risk_acknowledged, source_receipt_id,
    permitted_roots_json, protected_write_prefixes_json, denied_read_paths_json,
    permitted_capabilities_json, confirm_required_capabilities_json, egress_mode,
    runtime_allowlist_json, task_families_json, max_attempts, max_concurrent_dispatches,
    max_wall_seconds, budget_minor_units, budget_currency, spend_enforcement, charter_digest,
    approved_by, approved_at, created_at, expires_at, revoked_at, revoke_reason, superseded_by,
    schema_version
"""


def _load_narrowings(db: Session, *, charter_id: uuid.UUID, now: datetime) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT id, narrowing_json, created_at
            FROM charter_narrowings
            WHERE charter_id = :charter_id
              AND (expires_at IS NULL OR expires_at > :now)
            ORDER BY created_at ASC
            """
        ),
        {"charter_id": charter_id, "now": now},
    ).mappings().all()
    return [dict(row) for row in rows]


def create_charter(db: Session, *, auth: AuthContext, body: CharterCreateRequest, now: datetime) -> dict[str, Any]:
    """Create a charter in ``draft``.  Route auth is ``_require_verified_human`` (U1)."""
    settings = get_settings()
    if not _receipt_exists(db, workspace_id=auth.workspace_id, owner_id=auth.user_id, receipt_id=body.source_receipt_id):
        raise HTTPException(status_code=404, detail={"error": "receipt_not_found", "source_receipt_id": str(body.source_receipt_id)})
    payload = body.model_dump(mode="python")
    payload["caps"] = body.caps.model_dump(mode="python")
    try:
        normalized = validate_charter_payload_with_ttl(payload, settings=settings)
    except CharterInvalid as exc:
        raise HTTPException(status_code=422, detail={"error": "charter_invalid", "field": exc.field, "message": str(exc)}) from exc
    ttl_seconds = int(body.ttl_seconds or settings.charter_default_ttl_seconds)
    charter_id = uuid.uuid4()
    caps = normalized["caps"]
    db.execute(
        text(
            f"""
            INSERT INTO authority_charters({_CHARTER_COLUMNS})
            VALUES(
              :id, :workspace_id, :owner_id, :project_id, :charter_version, :policy_revision, 'draft',
              :enforcement_tier, :credential_risk_acknowledged, :source_receipt_id,
              CAST(:permitted_roots AS JSONB), CAST(:protected_write_prefixes AS JSONB),
              CAST(:denied_read_paths AS JSONB), CAST(:permitted_capabilities AS JSONB),
              CAST(:confirm_required_capabilities AS JSONB), :egress_mode,
              CAST(:runtime_allowlist AS JSONB), CAST(:task_families AS JSONB),
              :max_attempts, :max_concurrent_dispatches, :max_wall_seconds, :budget_minor_units,
              :budget_currency, :spend_enforcement, :charter_digest,
              NULL, NULL, :created_at, :expires_at, NULL, NULL, NULL, :schema_version
            )
            """
        ),
        {
            "id": charter_id,
            "workspace_id": auth.workspace_id,
            "owner_id": auth.user_id,
            "project_id": normalized["project_id"],
            "charter_version": normalized["charter_version"],
            "policy_revision": normalized["policy_revision"],
            "enforcement_tier": normalized["enforcement_tier"],
            "credential_risk_acknowledged": bool(normalized["credential_risk_acknowledged"]),
            "source_receipt_id": body.source_receipt_id,
            "permitted_roots": json.dumps(normalized["permitted_roots"]),
            "protected_write_prefixes": json.dumps(normalized["protected_write_prefixes"]),
            "denied_read_paths": json.dumps(normalized["denied_read_paths"]),
            "permitted_capabilities": json.dumps(normalized["permitted_capabilities"]),
            "confirm_required_capabilities": json.dumps(normalized["confirm_required_capabilities"]),
            "egress_mode": normalized["egress_mode"],
            "runtime_allowlist": json.dumps(normalized["runtime_allowlist"]),
            "task_families": json.dumps(normalized["task_families"]),
            "max_attempts": int(caps["max_attempts"]),
            "max_concurrent_dispatches": int(caps["max_concurrent_dispatches"]),
            "max_wall_seconds": int(caps["max_wall_seconds"]),
            "budget_minor_units": int(caps["budget_minor_units"]),
            "budget_currency": str(caps["budget_currency"]),
            "spend_enforcement": str(caps["spend_enforcement"]),
            "charter_digest": str(normalized["charter_digest"]),
            "created_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
            "schema_version": CHARTER_SCHEMA_VERSION,
        },
    )
    db.commit()
    row = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id"),
        {"id": charter_id},
    ).mappings().first()
    return _charter_response(row, charter_from_row(dict(row or {})), now=now)


def validate_charter_payload_with_ttl(payload: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    """``validate_charter_payload`` plus the two TTL bounds that live in settings.

    The shared validator is deliberately settings-free (Full's and Lite's ``Settings`` are
    different classes with the same name), so the bounds are enforced here.
    """
    ttl_raw = payload.get("ttl_seconds")
    if ttl_raw is not None:
        ttl = int(ttl_raw)
        if ttl < int(settings.charter_min_ttl_seconds):
            raise CharterInvalid("ttl_seconds", f"ttl_seconds must be >= {settings.charter_min_ttl_seconds}")
        if ttl > int(settings.charter_max_ttl_seconds):
            raise CharterInvalid("ttl_seconds", f"ttl_seconds must be <= {settings.charter_max_ttl_seconds}")
    return validate_charter_payload(payload)


def approve_charter(db: Session, *, auth: AuthContext, charter_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    """Move a draft to ``active`` and supersede the prior active charter, in one transaction."""
    row = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id AND workspace_id = :workspace_id FOR UPDATE"),
        {"id": charter_id, "workspace_id": auth.workspace_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "charter_not_found"})
    if str(row["status"]) == "revoked":
        raise HTTPException(status_code=409, detail={"error": "charter_revoked"})
    if str(row["status"]) == "active":
        return _charter_response(row, charter_from_row(dict(row or {}), narrowings=_load_narrowings(db, charter_id=charter_id, now=now)), now=now)
    db.execute(
        text(
            """
            UPDATE authority_charters
            SET status = 'superseded', superseded_by = :new_id
            WHERE workspace_id = :workspace_id AND owner_id = :owner_id
              AND status = 'active' AND id <> :new_id
            """
        ),
        {"new_id": charter_id, "workspace_id": auth.workspace_id, "owner_id": str(row["owner_id"])},
    )
    db.execute(
        text(
            """
            UPDATE authority_charters
            SET status = 'active', approved_by = :approved_by, approved_at = :now
            WHERE id = :id
            """
        ),
        {"id": charter_id, "approved_by": auth.user_id, "now": now},
    )
    db.commit()
    updated = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id"),
        {"id": charter_id},
    ).mappings().first()
    charter = charter_from_row(dict(updated or {}), narrowings=_load_narrowings(db, charter_id=charter_id, now=now))
    return _charter_response(updated, charter, now=now)


def revoke_charter(db: Session, *, auth: AuthContext, charter_id: uuid.UUID, reason: str, now: datetime) -> dict[str, Any]:
    """U3 — revocation stops WORK, not merely future enforcement.

    In the same transaction as the status update, every unresolved ``allow`` permit issued under
    this charter is turned into a ``blocked`` permit.  Without that, permits already in an
    executor's hand keep authorising mutating claims after the owner revoked the authority.
    """
    row = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id AND workspace_id = :workspace_id FOR UPDATE"),
        {"id": charter_id, "workspace_id": auth.workspace_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "charter_not_found"})
    db.execute(
        text(
            """
            UPDATE authority_charters
            SET status = 'revoked', revoked_at = COALESCE(revoked_at, :now), revoke_reason = :reason
            WHERE id = :id
            """
        ),
        {"id": charter_id, "now": now, "reason": str(reason or "")[:500]},
    )
    db.execute(
        text(
            """
            UPDATE execution_permits
            SET decision = 'blocked', reason = 'charter_revoked', resolved_at = :now
            WHERE charter_id = :charter_id AND decision = 'allow' AND resolved_at IS NULL
            """
        ),
        {"charter_id": charter_id, "now": now},
    )
    db.commit()
    updated = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id"),
        {"id": charter_id},
    ).mappings().first()
    return _charter_response(updated, charter_from_row(dict(updated or {})), now=now)


def apply_narrowing(db: Session, *, auth: AuthContext, body: CharterNarrowingRequest, now: datetime) -> dict[str, Any]:
    """Record a monotone narrowing.  ``source_receipt_id`` must name a trusted-capture receipt."""
    if not _receipt_exists(db, workspace_id=auth.workspace_id, owner_id=auth.user_id, receipt_id=body.source_receipt_id):
        raise HTTPException(status_code=404, detail={"error": "receipt_not_found", "source_receipt_id": str(body.source_receipt_id)})
    row = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id AND workspace_id = :workspace_id"),
        {"id": body.charter_id, "workspace_id": auth.workspace_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "charter_not_found"})
    payload = body.model_dump(mode="json", exclude={"charter_id", "session_id", "source_receipt_id", "reason"})
    narrowing_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO charter_narrowings(
              id, charter_id, workspace_id, owner_id, session_id, source_receipt_id,
              narrowing_json, created_at, expires_at, schema_version
            )
            VALUES(
              :id, :charter_id, :workspace_id, :owner_id, :session_id, :source_receipt_id,
              CAST(:narrowing AS JSONB), :created_at, :expires_at, :schema_version
            )
            """
        ),
        {
            "id": narrowing_id,
            "charter_id": body.charter_id,
            "workspace_id": auth.workspace_id,
            "owner_id": str(row["owner_id"]),
            "session_id": body.session_id,
            "source_receipt_id": body.source_receipt_id,
            "narrowing": json.dumps(payload),
            "created_at": now,
            "expires_at": body.expires_at,
            "schema_version": CHARTER_SCHEMA_VERSION,
        },
    )
    db.commit()
    charter = charter_from_row(dict(row or {}), narrowings=_load_narrowings(db, charter_id=body.charter_id, now=now))
    return _charter_response(row, charter, now=now)


def load_active_charter(db: Session, *, workspace_id: str, owner_id: str, session_id: str, now: datetime) -> ResolvedCharter | None:
    """The single resolver.  Returns ``None`` for an absent, expired or revoked charter."""
    row = db.execute(
        text(
            f"""
            SELECT {_CHARTER_COLUMNS}
            FROM authority_charters
            WHERE workspace_id = :workspace_id
              AND owner_id = :owner_id
              AND status = 'active'
              AND revoked_at IS NULL
              AND expires_at > :now
            ORDER BY approved_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "now": now},
    ).mappings().first()
    if row is None:
        return None
    # Every non-expired narrowing applies, whatever session recorded it: a narrowing can only ever
    # make the charter stricter, so scoping one to a session would let a second session escape it.
    del session_id
    return charter_from_row(dict(row or {}), narrowings=_load_narrowings(db, charter_id=row["id"], now=now))


def load_revoked_or_expired_charter(db: Session, *, workspace_id: str, owner_id: str, now: datetime) -> ResolvedCharter | None:
    """The most recent non-active charter, so a refusal can name ``charter_revoked`` rather than
    the weaker ``no_active_charter``.  Read-only; never used to authorise anything."""
    row = db.execute(
        text(
            f"""
            SELECT {_CHARTER_COLUMNS}
            FROM authority_charters
            WHERE workspace_id = :workspace_id
              AND owner_id = :owner_id
              AND status IN ('active', 'revoked')
            ORDER BY COALESCE(revoked_at, expires_at) DESC
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    ).mappings().first()
    if row is None:
        return None
    charter = charter_from_row(dict(row or {}))
    if charter.is_active(now):
        return None
    return charter


def resolve_charter_or_refuse(
    db: Session,
    *,
    auth: AuthContext,
    session_id: str,
    action_kind: str,
    settings: Settings,
    now: datetime,
) -> ResolvedCharter | None:
    """U2 — the one call-site pattern for a mutating route.

    Lets ``CharterRequired`` propagate; the route turns it into the verbatim 409 body.  When a
    revoked or expired charter exists it is passed to the shared guard so the refusal names which.
    """
    charter = load_active_charter(db, workspace_id=auth.workspace_id, owner_id=auth.user_id, session_id=session_id, now=now)
    probe = charter
    if probe is None:
        probe = load_revoked_or_expired_charter(db, workspace_id=auth.workspace_id, owner_id=auth.user_id, now=now)
    require_charter_for_action(
        probe,
        action_kind=action_kind,
        enforcement_enabled=bool(settings.charter_enforcement_enabled),
        now=now,
    )
    return charter


def load_charter_by_id(db: Session, *, workspace_id: str, charter_id: uuid.UUID) -> ResolvedCharter | None:
    row = db.execute(
        text(f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = :id AND workspace_id = :workspace_id"),
        {"id": charter_id, "workspace_id": workspace_id},
    ).mappings().first()
    if row is None:
        return None
    return charter_from_row(dict(row or {}))


def charter_row_digest(payload: dict[str, Any]) -> str:
    """Re-exported so a caller can recompute a digest without importing the shared module."""
    return charter_payload_digest(payload)


def preview_narrowed(charter: ResolvedCharter, narrowing: dict[str, Any]) -> ResolvedCharter:
    """Fold one narrowing without persisting it (used by the operator preview path)."""
    return narrow_charter(charter, narrowing)
