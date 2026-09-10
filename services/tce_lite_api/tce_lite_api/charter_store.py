"""Lite authority-charter store — the twin of ``tce_api.charter_store``.

The charter sits *above* the execution permit: a permit says "this one action is allowed now", a
charter says "this owner has granted this scope of authority for this window".  Nothing here
resolves a charter from a request body; the resolver is :func:`load_active_charter` and it reads
only server-side identity.

Signatures are identical to Full's except that ``conn: sqlite3.Connection`` replaces ``db: Session``
and ``charter_id`` is a ``str`` rather than a ``uuid.UUID`` — Lite stores UUIDs as TEXT.
``tests/integration/test_charter_lite.py::test_store_signatures_match_full`` compares the parameter
name sets directly.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from tce_shared.charter import (
    CHARTER_POLICY_REVISION,
    CHARTER_SCHEMA_VERSION,
    CharterInvalid,
    ResolvedCharter,
    charter_from_row,
    require_charter_for_action,
    resolved_charter_to_json,
    validate_charter_payload,
)
from tce_shared.events import CharterCreateRequest, CharterNarrowingRequest

from .auth import AuthContext
from .config import Settings, get_settings
from .task_state_store import run_cas_section

_CHARTER_COLUMNS = """
    id, workspace_id, owner_id, project_id, charter_version, policy_revision, status,
    enforcement_tier, credential_risk_acknowledged, source_receipt_id, permitted_roots_json,
    protected_write_prefixes_json, denied_read_paths_json, permitted_capabilities_json,
    confirm_required_capabilities_json, egress_mode, runtime_allowlist_json, task_families_json,
    max_attempts, max_concurrent_dispatches, max_wall_seconds, budget_minor_units, budget_currency,
    spend_enforcement, charter_digest, approved_by, approved_at, created_at, expires_at, revoked_at,
    revoke_reason, superseded_by, schema_version
"""


def _row_map(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _require_receipt(conn: sqlite3.Connection, *, auth: AuthContext, receipt_id: str) -> None:
    """U1 — a wider charter costs what a narrowing costs.

    A narrowing needs a ``trusted_input_receipts`` row; issuing a fresh, wider charter would
    otherwise bypass the trusted channel entirely.  Both routes go through here.
    """
    found = conn.execute(
        """
        SELECT id FROM trusted_input_receipts
        WHERE id = ? AND workspace_id = ?
          AND (owner_id = ? OR subject_user_id = ? OR subject_user_id = ?)
        LIMIT 1
        """,
        (str(receipt_id), auth.workspace_id, auth.user_id, auth.user_id, auth.behavior_subject_id),
    ).fetchone()
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "receipt_not_found", "message": "source_receipt_id does not name a trusted input receipt for this workspace"},
        )


def _charter_response(row: dict[str, Any], *, narrowings: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    charter = charter_from_row(row, narrowings=narrowings)
    return {
        "charter": resolved_charter_to_json(charter),
        "charter_id": str(row.get("id") or ""),
        "status": str(row.get("status") or "draft"),
        "charter_digest": str(row.get("charter_digest") or ""),
        "charter_version": str(row.get("charter_version") or CHARTER_SCHEMA_VERSION),
        "policy_revision": str(row.get("policy_revision") or CHARTER_POLICY_REVISION),
        "enforcement_tier": str(row.get("enforcement_tier") or ""),
        "credential_risk_acknowledged": bool(row.get("credential_risk_acknowledged")),
        "approved_by": (str(row["approved_by"]) if row.get("approved_by") else None),
        "approved_at": (str(row["approved_at"]) if row.get("approved_at") else None),
        "expires_at": (str(row["expires_at"]) if row.get("expires_at") else None),
        "revoked_at": (str(row["revoked_at"]) if row.get("revoked_at") else None),
        "superseded_by": (str(row["superseded_by"]) if row.get("superseded_by") else None),
        "narrowing_ids": list(charter.narrowing_ids),
        "generated_at": now.isoformat(),
        "schema_version": CHARTER_SCHEMA_VERSION,
    }


def _load_row(conn: sqlite3.Connection, *, workspace_id: str, charter_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_CHARTER_COLUMNS} FROM authority_charters WHERE id = ? AND workspace_id = ? LIMIT 1",
        (str(charter_id), workspace_id),
    ).fetchone()
    return _row_map(row) if row is not None else None


def _load_narrowings(conn: sqlite3.Connection, *, charter_id: str, now: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, narrowing_json, created_at, expires_at
        FROM charter_narrowings
        WHERE charter_id = ? AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY created_at ASC
        """,
        (str(charter_id), now.isoformat()),
    ).fetchall()
    return [_row_map(row) for row in rows]


def create_charter(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: CharterCreateRequest,
    now: datetime,
) -> dict[str, Any]:
    """Insert a ``draft`` charter.  Raises ``CharterInvalid`` (mapped to 422 by the route).

    TTL bounds live here, not in ``validate_charter_payload``: they are settings, and the shared
    module is deliberately settings-free because Full's and Lite's ``Settings`` are different
    classes with the same name.
    """
    settings = get_settings()
    normalized = validate_charter_payload(body.model_dump())
    ttl = int(body.ttl_seconds) if body.ttl_seconds is not None else int(settings.charter_default_ttl_seconds)
    if ttl < int(settings.charter_min_ttl_seconds):
        raise CharterInvalid("ttl_seconds", f"ttl_seconds must be >= {settings.charter_min_ttl_seconds}")
    if ttl > int(settings.charter_max_ttl_seconds):
        raise CharterInvalid("ttl_seconds", f"ttl_seconds must be <= {settings.charter_max_ttl_seconds}")

    def _body() -> dict[str, Any]:
        _require_receipt(conn, auth=auth, receipt_id=str(body.source_receipt_id))
        charter_id = str(uuid.uuid4())
        caps = dict(normalized["caps"])
        conn.execute(
            """
            INSERT INTO authority_charters (
                id, workspace_id, owner_id, project_id, charter_version, policy_revision, status,
                enforcement_tier, credential_risk_acknowledged, source_receipt_id,
                permitted_roots_json, protected_write_prefixes_json, denied_read_paths_json,
                permitted_capabilities_json, confirm_required_capabilities_json, egress_mode,
                runtime_allowlist_json, task_families_json, max_attempts, max_concurrent_dispatches,
                max_wall_seconds, budget_minor_units, budget_currency, spend_enforcement,
                charter_digest, approved_by, approved_at, created_at, expires_at, revoked_at,
                revoke_reason, superseded_by, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      NULL, NULL, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                charter_id,
                auth.workspace_id,
                auth.user_id,
                normalized["project_id"],
                str(normalized["charter_version"]),
                str(normalized["policy_revision"]),
                str(normalized["enforcement_tier"]),
                1 if normalized["credential_risk_acknowledged"] else 0,
                str(body.source_receipt_id),
                json.dumps(normalized["permitted_roots"]),
                json.dumps(normalized["protected_write_prefixes"]),
                json.dumps(normalized["denied_read_paths"]),
                json.dumps(normalized["permitted_capabilities"]),
                json.dumps(normalized["confirm_required_capabilities"]),
                str(normalized["egress_mode"]),
                json.dumps(normalized["runtime_allowlist"]),
                json.dumps(normalized["task_families"]),
                int(caps["max_attempts"]),
                int(caps["max_concurrent_dispatches"]),
                int(caps["max_wall_seconds"]),
                int(caps["budget_minor_units"]),
                str(caps["budget_currency"]),
                str(caps["spend_enforcement"]),
                str(normalized["charter_digest"]),
                now.isoformat(),
                (now + timedelta(seconds=ttl)).isoformat(),
                CHARTER_SCHEMA_VERSION,
            ),
        )
        row = _load_row(conn, workspace_id=auth.workspace_id, charter_id=charter_id)
        assert row is not None
        return _charter_response(row, narrowings=[], now=now)

    return run_cas_section(conn, _body)


def approve_charter(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    charter_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Move a ``draft`` charter to ``active`` and supersede the prior active one in the same
    transaction, so there is never a window with two active charters for one owner."""

    def _body() -> dict[str, Any]:
        row = _load_row(conn, workspace_id=auth.workspace_id, charter_id=str(charter_id))
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "charter_not_found"})
        status = str(row.get("status") or "draft")
        if status == "active":
            return _charter_response(row, narrowings=_load_narrowings(conn, charter_id=str(charter_id), now=now), now=now)
        if status != "draft":
            raise HTTPException(
                status_code=409,
                detail={"error": "charter_not_draft", "message": f"a charter in status {status!r} cannot be approved"},
            )
        conn.execute(
            """
            UPDATE authority_charters
               SET status = 'superseded', superseded_by = ?
             WHERE workspace_id = ? AND owner_id = ? AND status = 'active' AND id <> ?
            """,
            (str(charter_id), auth.workspace_id, str(row.get("owner_id") or ""), str(charter_id)),
        )
        conn.execute(
            """
            UPDATE authority_charters
               SET status = 'active', approved_by = ?, approved_at = ?
             WHERE id = ? AND workspace_id = ? AND status = 'draft'
            """,
            (auth.user_id, now.isoformat(), str(charter_id), auth.workspace_id),
        )
        fresh = _load_row(conn, workspace_id=auth.workspace_id, charter_id=str(charter_id))
        assert fresh is not None
        return _charter_response(fresh, narrowings=_load_narrowings(conn, charter_id=str(charter_id), now=now), now=now)

    return run_cas_section(conn, _body)


def revoke_charter(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    charter_id: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    """Revoke, and in the SAME transaction invalidate the charter's already-issued live permits.

    Without the permit UPDATE, revocation removes future enforcement rather than in-flight work: an
    ``allow`` permit minted five seconds before the revoke would still let a claim through.
    """

    def _body() -> dict[str, Any]:
        row = _load_row(conn, workspace_id=auth.workspace_id, charter_id=str(charter_id))
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "charter_not_found"})
        conn.execute(
            """
            UPDATE authority_charters
               SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?), revoke_reason = ?
             WHERE id = ? AND workspace_id = ?
            """,
            (now.isoformat(), str(reason or ""), str(charter_id), auth.workspace_id),
        )
        conn.execute(
            """
            UPDATE execution_permits
               SET decision = 'blocked', reason = 'charter_revoked', resolved_at = ?
             WHERE charter_id = ? AND decision = 'allow' AND resolved_at IS NULL
            """,
            (now.isoformat(), str(charter_id)),
        )
        fresh = _load_row(conn, workspace_id=auth.workspace_id, charter_id=str(charter_id))
        assert fresh is not None
        return _charter_response(fresh, narrowings=_load_narrowings(conn, charter_id=str(charter_id), now=now), now=now)

    return run_cas_section(conn, _body)


def apply_narrowing(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: CharterNarrowingRequest,
    now: datetime,
) -> dict[str, Any]:
    """Record a narrowing row.  The narrowing is applied at *resolve* time by ``charter_from_row``,
    never written back onto the charter, so the owner-approved scope stays the auditable original.
    """

    def _body() -> dict[str, Any]:
        row = _load_row(conn, workspace_id=auth.workspace_id, charter_id=str(body.charter_id))
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "charter_not_found"})
        _require_receipt(conn, auth=auth, receipt_id=str(body.source_receipt_id))
        narrowing = {
            "remove_roots": list(body.remove_roots),
            "remove_capabilities": list(body.remove_capabilities),
            "add_protected_write_prefixes": list(body.add_protected_write_prefixes),
            "add_denied_read_paths": list(body.add_denied_read_paths),
            "enforcement_tier": body.enforcement_tier,
            "egress_mode": body.egress_mode,
            "budget_minor_units": body.budget_minor_units,
            "max_attempts": body.max_attempts,
            "max_wall_seconds": body.max_wall_seconds,
            "max_concurrent_dispatches": body.max_concurrent_dispatches,
            "reason": str(body.reason or ""),
        }
        conn.execute(
            """
            INSERT INTO charter_narrowings (
                id, charter_id, workspace_id, owner_id, session_id, source_receipt_id,
                narrowing_json, created_at, expires_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                str(body.charter_id),
                auth.workspace_id,
                auth.user_id,
                str(body.session_id or "default"),
                str(body.source_receipt_id),
                json.dumps(narrowing),
                now.isoformat(),
                _iso(body.expires_at) or (str(row.get("expires_at")) if row.get("expires_at") else None),
                CHARTER_SCHEMA_VERSION,
            ),
        )
        fresh = _load_row(conn, workspace_id=auth.workspace_id, charter_id=str(body.charter_id))
        assert fresh is not None
        return _charter_response(
            fresh,
            narrowings=_load_narrowings(conn, charter_id=str(body.charter_id), now=now),
            now=now,
        )

    return run_cas_section(conn, _body)


def load_active_charter(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    now: datetime,
) -> ResolvedCharter | None:
    """The single resolver.  Nothing else in Lite turns a request body into a charter."""
    del session_id  # narrowings are session-tagged but every non-expired one applies to the charter
    row = conn.execute(
        f"""
        SELECT {_CHARTER_COLUMNS}
        FROM authority_charters
        WHERE workspace_id = ? AND owner_id = ? AND status = 'active'
          AND revoked_at IS NULL AND expires_at > ?
        ORDER BY approved_at DESC
        LIMIT 1
        """,
        (workspace_id, owner_id, now.isoformat()),
    ).fetchone()
    if row is None:
        return None
    mapped = _row_map(row)
    narrowings = _load_narrowings(conn, charter_id=str(mapped["id"]), now=now)
    return charter_from_row(mapped, narrowings=narrowings)


def resolve_charter_or_refuse(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    session_id: str,
    action_kind: str,
    settings: Settings,
    now: datetime,
) -> ResolvedCharter | None:
    """U2 — the one call-site pattern for a mutating route.

    ``CharterRequired`` propagates: the caller turns it into the verbatim 409 body so every refusal
    reads identically to an executor no matter which route produced it.
    """
    charter = load_active_charter(
        conn,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        session_id=session_id,
        now=now,
    )
    require_charter_for_action(
        charter,
        action_kind=action_kind,
        enforcement_enabled=bool(settings.charter_enforcement_enabled),
        now=now,
    )
    return charter


def load_charter_by_id(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    charter_id: str,
) -> ResolvedCharter | None:
    row = _load_row(conn, workspace_id=workspace_id, charter_id=str(charter_id))
    if row is None:
        return None
    return charter_from_row(row)
