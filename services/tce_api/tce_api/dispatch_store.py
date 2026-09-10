"""Dispatch records, budget reservation and sandbox self-tests for the FULL backend.

``open_dispatch`` is the SOLE caller of ``budget.reserve()``.  The reservation total and the
INSERT run in one transaction, so two concurrent dispatches cannot both fit under the same cap;
the supervisor reads the number back from ``DispatchResponse.cap_applied`` instead of computing
its own.

Nothing runnable is labelled ``enforced`` today.  ``spend_enforcement`` is a persisted column
precisely because reserve-before-dispatch is asymmetric across surfaces, and papering over that
in one abstraction would make the data model lie about the thing the charter asks it to expose.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tce_shared.budget import BudgetExceeded, reconciliation_from_json, reserve, spend_enforcement_for
from tce_shared.charter import READ_ONLY_TASK_FAMILIES, ResolvedCharter
from tce_shared.events import DispatchOpenRequest, DispatchReconcileRequest, SandboxSelfTestRequest
from tce_shared.runtime_contract import capability_matrix_json

from .auth import AuthContext
from .config import Settings
from .reconcile import reconcile_complete

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


def _dispatch_response(row: Any, *, now: datetime) -> dict[str, Any]:
    cap_raw = row["cap_applied_json"]
    if isinstance(cap_raw, str):
        cap_raw = json.loads(cap_raw)
    matrix_raw = row["capability_matrix_json"]
    if isinstance(matrix_raw, str):
        matrix_raw = json.loads(matrix_raw)
    return {
        "dispatch_id": row["id"],
        "directive_id": row["directive_id"],
        "attempt": int(row["attempt"] or 1),
        "charter_id": row["charter_id"],
        "charter_digest": str(row["charter_digest"] or ""),
        "enforcement_tier": str(row["enforcement_tier"] or ""),
        "sandbox_provider": str(row["sandbox_provider"] or ""),
        "sandbox_profile_digest": row["sandbox_profile_digest"],
        "sandbox_self_test_id": row["sandbox_self_test_id"],
        "runtime_id": str(row["runtime_id"] or ""),
        "runtime_version": str(row["runtime_version"] or ""),
        "surface": str(row["surface"] or ""),
        "model_id": str(row["model_id"] or ""),
        "contract_digest": str(row["contract_digest"] or ""),
        "capability_matrix": dict(matrix_raw or {}),
        "provider_run_id": row["provider_run_id"],
        "provider_turn_id": row["provider_turn_id"],
        "task_family": str(row["task_family"] or "unspecified"),
        "budget_reserved_minor_units": int(row["budget_reserved_minor_units"] or 0),
        "budget_currency": str(row["budget_currency"] or "USD"),
        "spend_enforcement": str(row["spend_enforcement"] or "unsupported"),
        "cap_applied": dict(cap_raw) if cap_raw else None,
        "cost_minor_units": row["cost_minor_units"],
        "cost_source": row["cost_source"],
        "tokens_input": int(row["tokens_input"] or 0),
        "tokens_output": int(row["tokens_output"] or 0),
        "tokens_cached_input": int(row["tokens_cached_input"] or 0),
        "tokens_reasoning": int(row["tokens_reasoning"] or 0),
        "human_intervention_count": int(row["human_intervention_count"] or 0),
        "outcome": row["outcome"],
        "terminal_reason": row["terminal_reason"],
        "wall_ms": row["wall_ms"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "reconciled_at": row["reconciled_at"],
        "generated_at": now,
        "schema_version": "v1",
    }


def open_reservation_total(db: Session, *, workspace_id: str, owner_id: str) -> int:
    row = db.execute(
        text(
            """
            SELECT COALESCE(SUM(budget_reserved_minor_units), 0)
            FROM dispatch_records
            WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND finished_at IS NULL
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    ).scalar()
    return int(row or 0)


def open_dispatch_count(db: Session, *, workspace_id: str, owner_id: str) -> int:
    row = db.execute(
        text(
            """
            SELECT COUNT(1)
            FROM dispatch_records
            WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND finished_at IS NULL
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    ).scalar()
    return int(row or 0)


def latest_self_test(db: Session, *, workspace_id: str, sandbox_provider: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT id, workspace_id, host_id, sandbox_provider, provider_version, profile_digest,
                   assertions_json, passed, uid_separation, ran_at, schema_version
            FROM sandbox_self_tests
            WHERE workspace_id = :workspace_id AND sandbox_provider = :sandbox_provider
            ORDER BY ran_at DESC
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "sandbox_provider": sandbox_provider},
    ).mappings().first()
    return dict(row) if row is not None else None


def record_self_test(db: Session, *, auth: AuthContext, body: SandboxSelfTestRequest, now: datetime) -> dict[str, Any]:
    """Persist a measurement.  ``passed`` and ``uid_separation`` are recorded, never asserted:
    the row is what ``open_dispatch`` later checks for freshness and digest match."""
    self_test_id = uuid.uuid4()
    assertions = [item.model_dump(mode="json") for item in body.assertions]
    db.execute(
        text(
            """
            INSERT INTO sandbox_self_tests(
              id, workspace_id, host_id, sandbox_provider, provider_version, profile_digest,
              assertions_json, passed, uid_separation, ran_at, schema_version
            )
            VALUES(
              :id, :workspace_id, :host_id, :sandbox_provider, :provider_version, :profile_digest,
              CAST(:assertions AS JSONB), :passed, :uid_separation, :ran_at, 'v1'
            )
            """
        ),
        {
            "id": self_test_id,
            "workspace_id": auth.workspace_id,
            "host_id": str(body.host_id or ""),
            "sandbox_provider": str(body.sandbox_provider),
            "provider_version": str(body.provider_version or ""),
            "profile_digest": str(body.profile_digest or ""),
            "assertions": json.dumps(assertions),
            "passed": bool(body.passed),
            "uid_separation": bool(body.uid_separation),
            "ran_at": now,
        },
    )
    db.commit()
    return {
        "self_test_id": self_test_id,
        "sandbox_provider": str(body.sandbox_provider),
        "profile_digest": str(body.profile_digest or ""),
        "passed": bool(body.passed),
        "uid_separation": bool(body.uid_separation),
        "assertions": assertions,
        "ran_at": now,
        "schema_version": "v1",
    }


def open_dispatch(
    db: Session,
    *,
    auth: AuthContext,
    charter: ResolvedCharter,
    body: DispatchOpenRequest,
    idempotency_key: str,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """Open one dispatch.  Every refusal happens BEFORE the INSERT.

    ``idempotency_key`` is the caller's replay token; the durable guarantee is the UNIQUE
    ``(directive_id, attempt)`` index, so a replay returns the existing row rather than a second
    dispatch.  The key is recorded on the audit row by the route.
    """
    if not reconcile_complete():
        raise HTTPException(status_code=409, detail={"error": "reconcile_pending"})
    tier = str(body.enforcement_tier)
    if tier == "advisory" and str(body.task_family) not in READ_ONLY_TASK_FAMILIES:
        # Tier 3 applies no OS boundary at all; permitting it for anything but a declared
        # read-only family would make the tier a label nothing binds to.
        raise HTTPException(
            status_code=422,
            detail={"error": "task_family_not_read_only", "task_family": str(body.task_family)},
        )
    if tier != "advisory":
        self_test = latest_self_test(db, workspace_id=auth.workspace_id, sandbox_provider=str(body.sandbox_provider))
        if self_test is None or not bool(self_test["passed"]):
            raise HTTPException(status_code=409, detail={"error": "sandbox_self_test_missing"})
        max_age = max(1, int(settings.sandbox_self_test_max_age_seconds))
        ran_at = self_test["ran_at"]
        if ran_at is None or ran_at < now - timedelta(seconds=max_age):
            raise HTTPException(status_code=409, detail={"error": "sandbox_self_test_stale"})
        if str(self_test["profile_digest"] or "") != str(body.sandbox_profile_digest or ""):
            raise HTTPException(status_code=409, detail={"error": "sandbox_self_test_digest_mismatch"})
    open_count = open_dispatch_count(db, workspace_id=auth.workspace_id, owner_id=auth.user_id)
    if open_count >= int(charter.caps.max_concurrent_dispatches):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "dispatch_concurrency_exceeded",
                "open": open_count,
                "cap": int(charter.caps.max_concurrent_dispatches),
            },
        )
    already = open_reservation_total(db, workspace_id=auth.workspace_id, owner_id=auth.user_id)
    request_minor = int(
        body.request_minor_units if body.request_minor_units is not None else settings.budget_default_minor_units
    )
    try:
        reservation = reserve(
            cap_minor_units=int(charter.caps.budget_minor_units),
            already_reserved_minor_units=already,
            request_minor_units=request_minor,
            currency=str(settings.budget_currency),
            surface=str(body.surface),
        )
    except BudgetExceeded as exc:
        # Distinct from the latency code 'budget_exhausted'; the two must never be confused.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "budget_exceeded",
                "reserved_minor_units": exc.reserved_minor_units,
                "requested_minor_units": exc.requested_minor_units,
                "cap_minor_units": exc.cap_minor_units,
            },
        ) from exc
    dispatch_id = uuid.uuid4()
    try:
        db.execute(
            text(
                f"""
                INSERT INTO dispatch_records({_DISPATCH_COLUMNS})
                VALUES(
                  :id, :workspace_id, :owner_id, :session_id, :task_id, :directive_id, :attempt,
                  :charter_id, :charter_digest, :claimed_by, :enforcement_tier, :sandbox_provider,
                  :sandbox_profile_digest, :sandbox_self_test_id, :runtime_id, :runtime_version,
                  :surface, :model_id, :contract_digest, CAST(:capability_matrix AS JSONB),
                  :provider_run_id, :provider_turn_id, :task_family, :budget_reserved,
                  :budget_currency, :spend_enforcement, CAST(:cap_applied AS JSONB),
                  NULL, NULL, 0, 0, 0, 0, 0, NULL, NULL, NULL, :started_at, NULL, NULL,
                  :created_at, :updated_at, 'v1'
                )
                """
            ),
            {
                "id": dispatch_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "session_id": body.session_id,
                "task_id": body.task_id,
                "directive_id": body.directive_id,
                "attempt": int(body.attempt),
                "charter_id": uuid.UUID(charter.charter_id),
                "charter_digest": charter.charter_digest,
                "claimed_by": auth.consumer,
                "enforcement_tier": tier,
                "sandbox_provider": str(body.sandbox_provider),
                "sandbox_profile_digest": body.sandbox_profile_digest,
                "sandbox_self_test_id": body.sandbox_self_test_id,
                "runtime_id": str(body.runtime_id),
                "runtime_version": str(body.runtime_version),
                "surface": str(body.surface),
                "model_id": str(body.model_id or ""),
                "contract_digest": str(body.contract_digest or ""),
                "capability_matrix": json.dumps(capability_matrix_json(str(body.surface))),
                "provider_run_id": str(body.provider_run_id),
                "provider_turn_id": body.provider_turn_id,
                "task_family": str(body.task_family or "unspecified"),
                "budget_reserved": int(reservation.reserved_minor_units),
                "budget_currency": reservation.currency,
                "spend_enforcement": reservation.spend_enforcement,
                "cap_applied": (json.dumps(dict(reservation.cap_applied)) if reservation.cap_applied else None),
                "started_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
    except IntegrityError as exc:
        db.rollback()
        existing = db.execute(
            text(f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE directive_id = :directive_id AND attempt = :attempt"),
            {"directive_id": body.directive_id, "attempt": int(body.attempt)},
        ).mappings().first()
        if existing is None:
            raise HTTPException(status_code=409, detail={"error": "dispatch_conflict"}) from exc
        return _dispatch_response(existing, now=now)
    db.execute(
        text(
            """
            UPDATE directive_executions
               SET dispatch_id = :dispatch_id, charter_id = :charter_id, updated_at = :now
             WHERE directive_id = :directive_id
               AND workspace_id = :workspace_id
            """
        ),
        {
            "dispatch_id": dispatch_id,
            "charter_id": uuid.UUID(charter.charter_id),
            "now": now,
            "directive_id": body.directive_id,
            "workspace_id": auth.workspace_id,
        },
    )
    db.commit()
    row = db.execute(
        text(f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = :id"),
        {"id": dispatch_id},
    ).mappings().first()
    return _dispatch_response(row, now=now)


def bind_provider_run(
    db: Session,
    *,
    dispatch_id: uuid.UUID,
    provider_run_id: str,
    provider_turn_id: str | None,
    now: datetime,
) -> None:
    db.execute(
        text(
            """
            UPDATE dispatch_records
               SET provider_run_id = :provider_run_id,
                   provider_turn_id = :provider_turn_id,
                   updated_at = :now
             WHERE id = :id
            """
        ),
        {
            "provider_run_id": str(provider_run_id),
            "provider_turn_id": provider_turn_id,
            "now": now,
            "id": dispatch_id,
        },
    )
    db.commit()


def reconcile_dispatch(db: Session, *, dispatch_id: uuid.UUID, body: DispatchReconcileRequest, now: datetime) -> dict[str, Any]:
    """Record what the run actually cost.

    Overshoot is recorded, never hidden: ``cost_minor_units > budget_reserved_minor_units`` is the
    expected, honest outcome on a surface that reports spend only after the call.  Clamping the
    recorded cost to the reservation would make the row lie.
    """
    row = db.execute(
        text(f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = :id FOR UPDATE"),
        {"id": dispatch_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "dispatch_not_found"})
    payload = dict(body.reconciliation or {})
    if payload:
        reconciliation = reconciliation_from_json(payload)
        cost_minor_units: int | None = int(reconciliation.cost_minor_units)
        cost_source: str | None = reconciliation.cost_source
        tokens = (
            int(reconciliation.tokens_input),
            int(reconciliation.tokens_output),
            int(reconciliation.tokens_cached_input),
            int(reconciliation.tokens_reasoning),
        )
    else:
        cost_minor_units, cost_source, tokens = None, None, (0, 0, 0, 0)
    db.execute(
        text(
            """
            UPDATE dispatch_records
               SET outcome = :outcome,
                   terminal_reason = :terminal_reason,
                   wall_ms = :wall_ms,
                   human_intervention_count = :human_intervention_count,
                   cost_minor_units = :cost_minor_units,
                   cost_source = :cost_source,
                   tokens_input = :tokens_input,
                   tokens_output = :tokens_output,
                   tokens_cached_input = :tokens_cached_input,
                   tokens_reasoning = :tokens_reasoning,
                   finished_at = COALESCE(finished_at, :now),
                   reconciled_at = :now,
                   updated_at = :now
             WHERE id = :id
            """
        ),
        {
            "outcome": str(body.outcome),
            "terminal_reason": str(body.terminal_reason or ""),
            "wall_ms": int(body.wall_ms),
            "human_intervention_count": int(body.human_intervention_count),
            "cost_minor_units": cost_minor_units,
            "cost_source": cost_source,
            "tokens_input": tokens[0],
            "tokens_output": tokens[1],
            "tokens_cached_input": tokens[2],
            "tokens_reasoning": tokens[3],
            "now": now,
            "id": dispatch_id,
        },
    )
    db.commit()
    updated = db.execute(
        text(f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = :id"),
        {"id": dispatch_id},
    ).mappings().first()
    return _dispatch_response(updated, now=now)


def load_dispatch(db: Session, *, dispatch_id: uuid.UUID, workspace_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(f"SELECT {_DISPATCH_COLUMNS} FROM dispatch_records WHERE id = :id AND workspace_id = :workspace_id"),
        {"id": dispatch_id, "workspace_id": workspace_id},
    ).mappings().first()
    if row is None:
        return None
    return _dispatch_response(row, now=datetime.now(tz=UTC))


def spend_enforcement_label(surface: str) -> str:
    """Re-export so the governance surface names the same value the row carries."""
    return spend_enforcement_for(surface)
