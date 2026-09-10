"""The effect journal for the FULL backend.

``directive_executions`` is one row per attempt of one directive; a claimed directive performs an
unbounded number of material effects.  The journal is the 1:N append-only ledger under it, and the
only place ``unknown`` — "we do not know whether this happened" — is representable.

U4: both the INSERT and the resolving CAS are fenced against
``directive_executions.lease_generation`` **by join**, never against the journal row's own copy of
the lease.  That copy is written by the same worker that is being fenced, so comparing against it
fences nothing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.effect_journal import (
    _SYSTEM_ONLY_SOURCES,
    ACTION_TRACING,
    EFFECT_KINDS,
    EFFECT_SCHEMA_VERSION,
    EffectIntent,
    EffectRecord,
    effect_intent_digest,
    effect_record_from_json,
    validate_effect_transition,
)

_EFFECT_COLUMNS = """
    effect_id, workspace_id, owner_id, session_id, task_id, directive_id, dispatch_id, seq, state,
    kind, reversibility, capability, resource, argv_json, description, intent_digest,
    enforcement_tier, action_tracing, lease_generation, claimed_executor, provider_run_id,
    provider_turn_id, runtime_id, runtime_version, model_id, opened_at, resolved_at,
    resolution_source, resolved_by_actor, evidence_json, created_at, schema_version
"""

def _record_from_row(row: Mapping[str, Any]) -> EffectRecord:
    argv_raw = row.get("argv_json")
    if isinstance(argv_raw, str):
        try:
            argv_raw = json.loads(argv_raw)
        except Exception:
            argv_raw = []
    evidence_raw = row.get("evidence_json")
    if isinstance(evidence_raw, str):
        try:
            evidence_raw = json.loads(evidence_raw)
        except Exception:
            evidence_raw = {}
    return effect_record_from_json(
        {
            "effect_id": str(row.get("effect_id") or ""),
            "directive_id": str(row.get("directive_id") or ""),
            "seq": int(row.get("seq") or 0),
            "state": str(row.get("state") or "prepared"),
            "intent": {
                "kind": str(row.get("kind") or "external"),
                "capability": str(row.get("capability") or ""),
                "resource": str(row.get("resource") or ""),
                "argv": list(argv_raw or []),
                "reversibility": str(row.get("reversibility") or "unknown"),
                "description": str(row.get("description") or ""),
            },
            "intent_digest": str(row.get("intent_digest") or ""),
            "enforcement_tier": str(row.get("enforcement_tier") or "advisory"),
            "action_tracing": str(row.get("action_tracing") or "unavailable"),
            "lease_generation": int(row.get("lease_generation") or 0),
            "claimed_executor": str(row.get("claimed_executor") or ""),
            "provider_run_id": row.get("provider_run_id"),
            "provider_turn_id": row.get("provider_turn_id"),
            "opened_at": row.get("opened_at"),
            "resolved_at": row.get("resolved_at"),
            "resolution_source": row.get("resolution_source"),
            "evidence": evidence_raw or None,
        }
    )


def _reject_forbidden_pair(enforcement_tier: str, action_tracing: str) -> None:
    """Refuse the advisory/observed pairing BEFORE the INSERT.

    Enforcing it only in the JSON codec leaves the store path free to write a row claiming
    per-command tracing under a tier that applies no boundary at all.
    """
    if enforcement_tier not in ("os_sandbox", "container", "advisory"):
        raise ValueError(f"unknown enforcement_tier {enforcement_tier!r}")
    if action_tracing not in ACTION_TRACING:
        raise ValueError(f"unknown action_tracing {action_tracing!r}")
    if enforcement_tier == "advisory" and action_tracing != "unavailable":
        raise ValueError("enforcement_tier='advisory' applies no OS boundary; action_tracing must be 'unavailable'")


def open_effect(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str | None,
    directive_id: uuid.UUID,
    dispatch_id: uuid.UUID | None,
    intent: EffectIntent,
    enforcement_tier: str,
    action_tracing: str,
    lease_generation: int,
    claimed_executor: str,
    provider_run_id: str | None,
    provider_turn_id: str | None,
    runtime_id: str,
    runtime_version: str,
    model_id: str,
    now: datetime,
) -> str:
    """The pre-effect durability point.  Returns the effect id.

    Idempotent on ``(directive_id, intent_digest)``: a retried POST yields the same row rather
    than a duplicate.  Raises ``LookupError`` when the fence rejects the caller — a worker whose
    lease was bumped by the reaper must not be able to open brand-new rows against the directive
    it no longer holds.
    """
    if str(intent.kind) not in EFFECT_KINDS:
        raise ValueError(f"unknown effect kind {intent.kind!r}")
    _reject_forbidden_pair(str(enforcement_tier), str(action_tracing))
    digest = effect_intent_digest(intent)
    fenced = db.execute(
        text(
            """
            SELECT lease_generation, claimed_executor
            FROM directive_executions
            WHERE directive_id = :directive_id
              AND workspace_id = :workspace_id
            LIMIT 1
            """
        ),
        {"directive_id": directive_id, "workspace_id": workspace_id},
    ).mappings().first()
    if fenced is None:
        raise LookupError("unknown_directive")
    actor_is_system = str(claimed_executor) in _SYSTEM_ONLY_SOURCES
    if not actor_is_system:
        if int(fenced["lease_generation"] or 0) != int(lease_generation):
            raise LookupError("stale_lease")
        if str(fenced["claimed_executor"] or "") != str(claimed_executor):
            raise LookupError("stale_lease")
    effect_id = uuid.uuid4()
    db.execute(
        text(
            f"""
            INSERT INTO effect_journal({_EFFECT_COLUMNS})
            SELECT
              :effect_id, :workspace_id, :owner_id, :session_id, :task_id, :directive_id,
              :dispatch_id, COALESCE(MAX(e.seq), 0) + 1, 'prepared',
              :kind, :reversibility, :capability, :resource, CAST(:argv AS JSONB), :description,
              :intent_digest, :enforcement_tier, :action_tracing, :lease_generation,
              :claimed_executor, :provider_run_id, :provider_turn_id, :runtime_id,
              :runtime_version, :model_id, :now, NULL, NULL, NULL, CAST('{{}}' AS JSONB), :now,
              :schema_version
            FROM effect_journal AS e
            WHERE e.directive_id = :directive_id
            ON CONFLICT (directive_id, intent_digest) DO NOTHING
            """
        ),
        {
            "effect_id": effect_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "session_id": session_id,
            "task_id": task_id,
            "directive_id": directive_id,
            "dispatch_id": dispatch_id,
            "kind": intent.kind,
            "reversibility": intent.reversibility,
            "capability": intent.capability,
            "resource": intent.resource,
            "argv": json.dumps(list(intent.argv)),
            "description": intent.description,
            "intent_digest": digest,
            "enforcement_tier": str(enforcement_tier),
            "action_tracing": str(action_tracing),
            "lease_generation": int(lease_generation),
            "claimed_executor": str(claimed_executor),
            "provider_run_id": provider_run_id,
            "provider_turn_id": provider_turn_id,
            "runtime_id": runtime_id,
            "runtime_version": runtime_version,
            "model_id": model_id,
            "now": now,
            "schema_version": EFFECT_SCHEMA_VERSION,
        },
    )
    row = db.execute(
        text("SELECT effect_id FROM effect_journal WHERE directive_id = :directive_id AND intent_digest = :digest"),
        {"directive_id": directive_id, "digest": digest},
    ).first()
    if row is None:
        raise LookupError("effect_insert_failed")
    return str(row[0])


def load_effect(db: Session, *, effect_id: uuid.UUID) -> dict[str, Any] | None:
    row = db.execute(
        text(f"SELECT {_EFFECT_COLUMNS} FROM effect_journal WHERE effect_id = :effect_id"),
        {"effect_id": effect_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def resolve_effect(
    db: Session,
    *,
    effect_id: uuid.UUID,
    target_state: str,
    actor: str,
    resolution_source: str,
    evidence: Mapping[str, Any],
    expected_lease: int,
    now: datetime,
) -> bool:
    """The fenced CAS.  ``False`` means the caller lost the race or is fenced out (409 stale_lease).

    ``actor`` is derived server-side from the authenticated identity; the request body has no
    ``actor`` field, because a caller that can name itself ``system:reconciler`` satisfies the
    "system actors only" rule the pure validator advertises.
    """
    row = db.execute(
        text(f"SELECT {_EFFECT_COLUMNS} FROM effect_journal WHERE effect_id = :effect_id FOR UPDATE"),
        {"effect_id": effect_id},
    ).mappings().first()
    if row is None:
        raise LookupError("unknown_effect")
    current_state = str(row["state"])
    validate_effect_transition(
        current_state=current_state,
        target_state=str(target_state),
        actor=str(actor),
        reversibility=str(row["reversibility"] or "unknown"),
    )
    actor_is_system = str(actor) in _SYSTEM_ONLY_SOURCES
    # The fence compares the DIRECTIVE's claimed_executor against the AUTHENTICATED caller, never
    # against the journal row's own copy of it — that copy was written by the worker being fenced.
    executor_id = str(actor).split("executor:", 1)[1] if str(actor).startswith("executor:") else str(actor)
    result = db.execute(
        text(
            """
            UPDATE effect_journal AS e
               SET state = :target,
                   resolved_at = :now,
                   resolution_source = :source,
                   resolved_by_actor = :actor,
                   evidence_json = CAST(:evidence AS JSONB)
              FROM directive_executions AS d
             WHERE e.effect_id = :effect_id
               AND e.state = :expected_current
               AND d.directive_id = e.directive_id
               AND d.lease_generation = :expected_lease
               AND (d.claimed_executor = :executor_id OR :actor_is_system)
            """
        ),
        {
            "target": str(target_state),
            "now": now,
            "source": str(resolution_source),
            "actor": str(actor),
            "evidence": json.dumps(dict(evidence or {})),
            "effect_id": effect_id,
            "expected_current": current_state,
            "expected_lease": int(expected_lease),
            "executor_id": executor_id,
            "actor_is_system": actor_is_system,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0


def list_open_effects(db: Session, *, workspace_id: str, owner_id: str, session_id: str) -> list[EffectRecord]:
    """The record set ``pause_guard_for_session`` decides the autonomy pause from.

    ``unknown`` rows are ordered FIRST, ahead of ``opened_at``, because of the ``LIMIT 200``.
    The cap bounds the payload; without this ordering it also bounds what the pause can see.
    Measured against the live Postgres: 200 ``running`` rows opened before a single ``unknown``
    irreversible row filled the window, ``pause_required`` saw only healthy in-flight work, and
    ``pause_guard_for_session`` returned ``(False, '', '')`` — the pause silently did not fire on
    an unresolved effect that was sitting in the table.  That window is agent-reachable: nothing
    forces a ``prepared`` effect to be resolved, so opening cheap effects early in a session and
    the risky one afterwards buys exactly the ordering the cap then hides.  ``pause_required``
    stops at the first ``unknown`` it finds, so putting them at the front makes the pause depend on
    the table rather than on how many rows happen to precede it.  Lite's twin has no cap and needs
    no twin of this clause.
    """
    rows = db.execute(
        text(
            f"""
            SELECT {_EFFECT_COLUMNS}
            FROM effect_journal
            WHERE workspace_id = :workspace_id
              AND owner_id = :owner_id
              AND session_id = :session_id
              AND state IN ('prepared', 'running', 'unknown')
            ORDER BY (state = 'unknown') DESC, opened_at ASC
            LIMIT 200
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "session_id": session_id},
    ).mappings().all()
    return [_record_from_row(dict(row)) for row in rows]


def load_effects_for_directive(db: Session, *, directive_id: uuid.UUID) -> list[EffectRecord]:
    rows = db.execute(
        text(f"SELECT {_EFFECT_COLUMNS} FROM effect_journal WHERE directive_id = :directive_id ORDER BY seq ASC"),
        {"directive_id": directive_id},
    ).mappings().all()
    return [_record_from_row(dict(row)) for row in rows]


def open_effect_count_for_session(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    exclude_directive_id: uuid.UUID | None,
) -> int:
    """Count the session's open effects, EXCLUDING the caller's own directive.

    The exclusion is mandatory: a dispatch opens its root effect and moves it to ``running`` for
    the whole run, resolving it only at the end — precisely the window in which the agent requests
    its capability grants.  Without the exclusion every healthy dispatch deadlocks on itself.
    """
    row = db.execute(
        text(
            """
            SELECT COUNT(1)
            FROM effect_journal
            WHERE workspace_id = :workspace_id
              AND owner_id = :owner_id
              AND session_id = :session_id
              AND state IN ('prepared', 'running', 'unknown')
              AND (:exclude_directive_id IS NULL OR directive_id <> :exclude_directive_id)
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "session_id": session_id,
            "exclude_directive_id": exclude_directive_id,
        },
    ).scalar()
    return int(row or 0)
