"""Lite dispatch records and sandbox self-tests — the twin of ``tce_api.dispatch_store``.

``open_dispatch`` is the SOLE caller of :func:`tce_shared.budget.reserve` in Lite.  The supervisor
does not compute its own reservation; it reads the number back from ``DispatchResponse.cap_applied``.
That keeps the concurrency property — the "what is already reserved" SELECT and the INSERT run
inside one write-locked section, so two concurrent dispatches cannot both fit under the cap — while
still handing the runtime the figure it needs.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from tce_shared.budget import BudgetExceeded, reconciliation_from_json, reserve
from tce_shared.charter import READ_ONLY_TASK_FAMILIES, ResolvedCharter
from tce_shared.events import DispatchOpenRequest, DispatchReconcileRequest, SandboxSelfTestRequest
from tce_shared.runtime_contract import capability_matrix_json

from .auth import AuthContext
from .config import Settings
from .task_state_store import run_cas_section

_DISPATCH_COLUMNS = """
    id, workspace_id, owner_id, session_id, task_id, directive_id, attempt, charter_id,
    charter_digest, claimed_by, enforcement_tier, sandbox_provider, sandbox_profile_digest,
    sandbox_self_test_id, runtime_id, runtime_version, surface, model_id, contract_digest,
    capability_matrix_json, provider_run_id, provider_turn_id, task_family,
    budget_reserved_minor_units, budget_currency, spend_enforcement, cap_applied_json,
    cost_minor_units, cost_source, tokens_input, tokens_output, tokens_cached_input,
    tokens_reasoning, human_intervention_count, outcome, terminal_reason, wall_ms, started_at,
    finished_at, reconciled_at, created_at, updated_at, schema_version
"""


def _dispatch_response(row: sqlite3.Row, *, now: datetime) -> dict[str, Any]:
    cap_applied_raw = row["cap_applied_json"]
    cap_applied = json.loads(str(cap_applied_raw)) if cap_applied_raw else None
    return {
        "dispatch_id": str(row["id"]),
        "directive_id": str(row["directive_id"]),
        "attempt": int(row["attempt"] or 1),
        "charter_id": str(row["charter_id"]),
        "charter_digest": str(row["charter_digest"] or ""),
        "enforcement_tier": str(row["enforcement_tier"]),
        "sandbox_provider": str(row["sandbox_provider"] or ""),
        "sandbox_profile_digest": (str(row["sandbox_profile_digest"]) if row["sandbox_profile_digest"] else None),
        "sandbox_self_test_id": (str(row["sandbox_self_test_id"]) if row["sandbox_self_test_id"] else None),
        "runtime_id": str(row["runtime_id"] or ""),
        "runtime_version": str(row["runtime_version"] or ""),
        "surface": str(row["surface"] or ""),
        "model_id": str(row["model_id"] or ""),
        "contract_digest": str(row["contract_digest"] or ""),
        "capability_matrix": json.loads(str(row["capability_matrix_json"] or "{}")),
        "provider_run_id": (str(row["provider_run_id"]) if row["provider_run_id"] else None),
        "provider_turn_id": (str(row["provider_turn_id"]) if row["provider_turn_id"] else None),
        "task_family": str(row["task_family"] or "unspecified"),
        "budget_reserved_minor_units": int(row["budget_reserved_minor_units"] or 0),
        "budget_currency": str(row["budget_currency"] or "USD"),
        "spend_enforcement": str(row["spend_enforcement"] or "unsupported"),
        "cap_applied": cap_applied,
        "cost_minor_units": (int(row["cost_minor_units"]) if row["cost_minor_units"] is not None else None),
        "cost_source": (str(row["cost_source"]) if row["cost_source"] else None),
        "tokens_input": int(row["tokens_input"] or 0),
        "tokens_output": int(row["tokens_output"] or 0),
        "tokens_cached_input": int(row["tokens_cached_input"] or 0),
        "tokens_reasoning": int(row["tokens_reasoning"] or 0),
        "human_intervention_count": int(row["human_intervention_count"] or 0),
        "outcome": (str(row["outcome"]) if row["outcome"] else None),
        "terminal_reason": (str(row["terminal_reason"]) if row["terminal_reason"] else None),
        "wall_ms": (int(row["wall_ms"]) if row["wall_ms"] is not None else None),
        "started_at": (str(row["started_at"]) if row["started_at"] else None),
        "finished_at": (str(row["finished_at"]) if row["finished_at"] else None),
        "reconciled_at": (str(row["reconciled_at"]) if row["reconciled_at"] else None),
        "generated_at": now.isoformat(),
        "schema_version": "v1",
    }


def open_reservation_total(conn: sqlite3.Connection, *, workspace_id: str, owner_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(budget_reserved_minor_units), 0) AS total
        FROM dispatch_records
        WHERE workspace_id = ? AND owner_id = ? AND finished_at IS NULL
        """,
        (workspace_id, owner_id),
    ).fetchone()
    return int(row["total"] or 0)


def open_dispatch_count(conn: sqlite3.Connection, *, workspace_id: str, owner_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM dispatch_records
        WHERE workspace_id = ? AND owner_id = ? AND finished_at IS NULL
        """,
        (workspace_id, owner_id),
    ).fetchone()
    return int(row["n"] or 0)


def latest_self_test(conn: sqlite3.Connection, *, workspace_id: str, sandbox_provider: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, workspace_id, host_id, sandbox_provider, provider_version, profile_digest,
               assertions_json, passed, uid_separation, ran_at, schema_version
        FROM sandbox_self_tests
        WHERE workspace_id = ? AND sandbox_provider = ?
        ORDER BY ran_at DESC
        LIMIT 1
        """,
        (workspace_id, str(sandbox_provider)),
    ).fetchone()
    if row is None:
        return None
    return {
        "self_test_id": str(row["id"]),
        "sandbox_provider": str(row["sandbox_provider"]),
        "provider_version": str(row["provider_version"] or ""),
        "profile_digest": str(row["profile_digest"] or ""),
        "assertions": json.loads(str(row["assertions_json"] or "[]")),
        "passed": bool(row["passed"]),
        "uid_separation": bool(row["uid_separation"]),
        "ran_at": str(row["ran_at"]),
        "schema_version": str(row["schema_version"] or "v1"),
    }


def record_self_test(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    body: SandboxSelfTestRequest,
    now: datetime,
) -> dict[str, Any]:
    """Record a boot-time sandbox measurement.  ``open_dispatch`` later reads it back for freshness
    and digest match — a self-test nobody checks is a log line, not a control."""
    self_test_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sandbox_self_tests (
            id, workspace_id, host_id, sandbox_provider, provider_version, profile_digest,
            assertions_json, passed, uid_separation, ran_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            self_test_id,
            auth.workspace_id,
            str(body.host_id or ""),
            str(body.sandbox_provider),
            str(body.provider_version or ""),
            str(body.profile_digest or ""),
            json.dumps([assertion.model_dump() for assertion in body.assertions]),
            1 if body.passed else 0,
            1 if body.uid_separation else 0,
            now.isoformat(),
        ),
    )
    conn.commit()
    return {
        "self_test_id": self_test_id,
        "sandbox_provider": str(body.sandbox_provider),
        "profile_digest": str(body.profile_digest or ""),
        "passed": bool(body.passed),
        "uid_separation": bool(body.uid_separation),
        "assertions": [assertion.model_dump() for assertion in body.assertions],
        "ran_at": now,
        "schema_version": "v1",
    }


def open_dispatch(
    conn: sqlite3.Connection,
    *,
    auth: AuthContext,
    charter: ResolvedCharter,
    body: DispatchOpenRequest,
    idempotency_key: str,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """Open a dispatch record.  Every refusal below happens BEFORE the INSERT.

    ``409 reconcile_pending`` is the ordering guarantee that matters most: a service that restarted
    with unresolved effects must finish reading what happened before it starts anything new.
    """
    from . import reconcile

    if not reconcile.reconcile_complete():
        raise HTTPException(
            status_code=409,
            detail={"error": "reconcile_pending", "message": "startup reconcile has not completed; no new dispatch may open"},
        )
    tier = str(body.enforcement_tier)
    if tier == "advisory" and str(body.task_family) not in READ_ONLY_TASK_FAMILIES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "task_family_not_read_only",
                "message": "enforcement_tier='advisory' applies no OS boundary and may only run a read-only task family",
                "read_only_task_families": sorted(READ_ONLY_TASK_FAMILIES),
            },
        )
    self_test_id: str | None = None
    if tier != "advisory":
        # S16: an unmeasured sandbox is a claim, not a control. The row must exist, have passed, be
        # fresh, and describe the same profile the caller says it is about to run under.
        latest = latest_self_test(conn, workspace_id=auth.workspace_id, sandbox_provider=str(body.sandbox_provider))
        if latest is None:
            raise HTTPException(status_code=409, detail={"error": "sandbox_self_test_missing"})
        if not bool(latest["passed"]):
            raise HTTPException(status_code=409, detail={"error": "sandbox_self_test_missing", "message": "the most recent self-test did not pass"})
        ran_at = datetime.fromisoformat(str(latest["ran_at"]))
        if ran_at + timedelta(seconds=max(1, int(settings.sandbox_self_test_max_age_seconds))) < now:
            raise HTTPException(status_code=409, detail={"error": "sandbox_self_test_stale", "ran_at": str(latest["ran_at"])})
        if str(latest["profile_digest"]) != str(body.sandbox_profile_digest or ""):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "sandbox_self_test_digest_mismatch",
                    "measured": str(latest["profile_digest"]),
                    "requested": str(body.sandbox_profile_digest or ""),
                },
            )
        self_test_id = str(latest["self_test_id"])

    def _body() -> dict[str, Any]:
        existing = conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE directive_id = ? AND attempt = ? LIMIT 1",
            (str(body.directive_id), int(body.attempt)),
        ).fetchone()
        if existing is not None:
            # Idempotent on (directive_id, attempt) — the UNIQUE index is the guarantee, and a
            # retried POST with the same Idempotency-Key must not mint a second reservation.
            return _dispatch_response(existing, now=now)
        if open_dispatch_count(conn, workspace_id=auth.workspace_id, owner_id=auth.user_id) >= int(
            charter.caps.max_concurrent_dispatches
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "dispatch_concurrency_exceeded",
                    "max_concurrent_dispatches": int(charter.caps.max_concurrent_dispatches),
                },
            )
        request_minor_units = (
            int(body.request_minor_units)
            if body.request_minor_units is not None
            else int(settings.budget_default_minor_units)
        )
        try:
            reservation = reserve(
                cap_minor_units=int(charter.caps.budget_minor_units),
                already_reserved_minor_units=open_reservation_total(
                    conn, workspace_id=auth.workspace_id, owner_id=auth.user_id
                ),
                request_minor_units=request_minor_units,
                currency=str(settings.budget_currency),
                surface=str(body.surface),
            )
        except BudgetExceeded as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "budget_exceeded",
                    "reserved_minor_units": exc.reserved_minor_units,
                    "requested_minor_units": exc.requested_minor_units,
                    "cap_minor_units": exc.cap_minor_units,
                },
            ) from exc
        dispatch_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO dispatch_records (
                id, workspace_id, owner_id, session_id, task_id, directive_id, attempt, charter_id,
                charter_digest, claimed_by, enforcement_tier, sandbox_provider,
                sandbox_profile_digest, sandbox_self_test_id, runtime_id, runtime_version, surface,
                model_id, contract_digest, capability_matrix_json, provider_run_id, provider_turn_id,
                task_family, budget_reserved_minor_units, budget_currency, spend_enforcement,
                cap_applied_json, cost_minor_units, cost_source, tokens_input, tokens_output,
                tokens_cached_input, tokens_reasoning, human_intervention_count, outcome,
                terminal_reason, wall_ms, started_at, finished_at, reconciled_at, created_at,
                updated_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      NULL, NULL, 0, 0, 0, 0, 0, NULL, NULL, NULL, ?, NULL, NULL, ?, ?, 'v1')
            """,
            (
                dispatch_id,
                auth.workspace_id,
                auth.user_id,
                str(body.session_id or "default"),
                body.task_id,
                str(body.directive_id),
                int(body.attempt),
                charter.charter_id,
                charter.charter_digest,
                auth.consumer,
                tier,
                str(body.sandbox_provider),
                body.sandbox_profile_digest,
                self_test_id,
                str(body.runtime_id),
                str(body.runtime_version),
                str(body.surface),
                str(body.model_id or ""),
                str(body.contract_digest or ""),
                json.dumps(capability_matrix_json(str(body.surface))),
                str(body.provider_run_id),
                body.provider_turn_id,
                str(body.task_family or "unspecified"),
                int(reservation.reserved_minor_units),
                reservation.currency,
                reservation.spend_enforcement,
                (json.dumps(dict(reservation.cap_applied)) if reservation.cap_applied is not None else None),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        conn.execute(
            "UPDATE directive_executions SET dispatch_id = ?, charter_id = ?, updated_at = ? WHERE directive_id = ?",
            (dispatch_id, charter.charter_id, now.isoformat(), str(body.directive_id)),
        )
        fresh = conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = ? LIMIT 1",
            (dispatch_id,),
        ).fetchone()
        return _dispatch_response(fresh, now=now)

    del idempotency_key  # the (directive_id, attempt) UNIQUE index is the idempotency key that holds
    return run_cas_section(conn, _body)


def bind_provider_run(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    provider_run_id: str,
    provider_turn_id: str | None,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE dispatch_records
           SET provider_run_id = ?, provider_turn_id = ?, updated_at = ?
         WHERE id = ?
        """,
        (str(provider_run_id), provider_turn_id, now.isoformat(), str(dispatch_id)),
    )
    conn.commit()


def reconcile_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    body: DispatchReconcileRequest,
    now: datetime,
) -> dict[str, Any]:
    """Write the post-run accounting.

    Overshoot is recorded, never clamped: ``cost_minor_units`` above ``budget_reserved_minor_units``
    on an ``unsupported`` surface is the honest outcome of a runtime that reports spend only after
    the call, and quietly clamping it would make the row lie about the one thing the charter asks it
    to make visible.
    """

    def _body() -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = ? LIMIT 1",
            (str(dispatch_id),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "dispatch_not_found"})
        reconciliation = reconciliation_from_json(dict(body.reconciliation or {}))
        conn.execute(
            """
            UPDATE dispatch_records
               SET outcome = ?, terminal_reason = ?, wall_ms = ?, human_intervention_count = ?,
                   cost_minor_units = ?, cost_source = ?, tokens_input = ?, tokens_output = ?,
                   tokens_cached_input = ?, tokens_reasoning = ?,
                   finished_at = COALESCE(finished_at, ?), reconciled_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                str(body.outcome),
                str(body.terminal_reason or ""),
                int(body.wall_ms),
                int(body.human_intervention_count),
                int(reconciliation.cost_minor_units),
                str(reconciliation.cost_source),
                int(reconciliation.tokens_input),
                int(reconciliation.tokens_output),
                int(reconciliation.tokens_cached_input),
                int(reconciliation.tokens_reasoning),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
                str(dispatch_id),
            ),
        )
        fresh = conn.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = ? LIMIT 1",
            (str(dispatch_id),),
        ).fetchone()
        return _dispatch_response(fresh, now=now)

    return run_cas_section(conn, _body)


def load_dispatch(conn: sqlite3.Connection, *, dispatch_id: str, workspace_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = ? AND workspace_id = ? LIMIT 1",
        (str(dispatch_id), workspace_id),
    ).fetchone()
    if row is None:
        return None
    return _dispatch_response(row, now=datetime.fromisoformat(str(row["updated_at"])))
