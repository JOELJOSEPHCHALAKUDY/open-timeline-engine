"""Lite effect journal — the twin of ``tce_api.effect_store``.

``directive_executions`` is one row per attempt of one directive and its ``meta`` blob is rewritten
by the report.  This table is the append-only 1:N ledger of what was *about to happen*, written
before the effect, and only ``state``/``resolved_at``/``resolution_source``/``resolved_by_actor``/
``evidence_json`` are ever updated — through the fenced CAS below and nowhere else.

**U4 — the fence is ``directive_executions.lease_generation``, reached by a correlated subquery.**
``effect_journal.lease_generation`` is a copy the same worker wrote at open time; comparing against
it fences nothing.  SQLite has no ``UPDATE ... FROM`` on every build in use, so the portable
``EXISTS`` form below is the Lite twin of Full's join.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tce_shared.effect_journal import (
    _SYSTEM_ONLY_SOURCES,
    ACTION_TRACING,
    EFFECT_SCHEMA_VERSION,
    EffectIntent,
    EffectRecord,
    effect_intent_digest,
    effect_record_from_json,
    validate_effect_transition,
)

_TIERS = ("os_sandbox", "container", "advisory")

_EFFECT_COLUMNS = """
    effect_id, workspace_id, owner_id, session_id, task_id, directive_id, dispatch_id, seq, state,
    kind, reversibility, capability, resource, argv_json, description, intent_digest,
    enforcement_tier, action_tracing, lease_generation, claimed_executor, provider_run_id,
    provider_turn_id, runtime_id, runtime_version, model_id, opened_at, resolved_at,
    resolution_source, resolved_by_actor, evidence_json, created_at, schema_version
"""


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return list(decoded) if isinstance(decoded, list) else []


def _json_obj(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def record_from_row(row: sqlite3.Row) -> EffectRecord:
    """The single row → :class:`EffectRecord` adapter.  Coerces every field; ``warn_return_any``
    makes a pass-through an error, and a silently mistyped state would defeat the transition table.
    """
    payload = {
        "effect_id": str(row["effect_id"]),
        "directive_id": str(row["directive_id"]),
        "seq": int(row["seq"] or 0),
        "state": str(row["state"]),
        "intent": {
            "kind": str(row["kind"]),
            "capability": str(row["capability"] or ""),
            "resource": str(row["resource"] or ""),
            "argv": _json_list(row["argv_json"]),
            "reversibility": str(row["reversibility"] or "unknown"),
            "description": str(row["description"] or ""),
        },
        "intent_digest": str(row["intent_digest"] or ""),
        "enforcement_tier": str(row["enforcement_tier"] or "advisory"),
        "action_tracing": str(row["action_tracing"] or "unavailable"),
        "lease_generation": int(row["lease_generation"] or 0),
        "claimed_executor": str(row["claimed_executor"] or ""),
        "provider_run_id": (str(row["provider_run_id"]) if row["provider_run_id"] else None),
        "provider_turn_id": (str(row["provider_turn_id"]) if row["provider_turn_id"] else None),
        "opened_at": str(row["opened_at"]),
        "resolved_at": (str(row["resolved_at"]) if row["resolved_at"] else None),
        "resolution_source": (str(row["resolution_source"]) if row["resolution_source"] else None),
        "evidence": _json_obj(row["evidence_json"]),
    }
    return effect_record_from_json(payload)


def _reject_forbidden_pair(enforcement_tier: str, action_tracing: str) -> None:
    """G13d — the store path must reject the advisory/observed pair BEFORE the INSERT.

    Enforcing it only in the codec would let ``open_effect`` write a row claiming per-command
    tracing under a tier that applies no OS boundary at all — exactly the claim D-3 says this host
    cannot make.
    """
    if enforcement_tier not in _TIERS:
        raise ValueError(f"enforcement_tier must be one of {_TIERS!r}")
    if action_tracing not in ACTION_TRACING:
        raise ValueError(f"action_tracing must be one of {ACTION_TRACING!r}")
    if enforcement_tier == "advisory" and action_tracing != "unavailable":
        raise ValueError("enforcement_tier='advisory' cannot claim observed action tracing")


def open_effect(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str | None,
    directive_id: str,
    dispatch_id: str | None,
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
    """The pre-effect durability point.  Returns the ``effect_id``.

    Idempotent on ``(directive_id, intent_digest)``: a retried ``POST /v1/effects`` yields the same
    row rather than a duplicate, the same idiom ``enqueue_handoff`` uses.

    Carries the U4 fence: a worker whose lease has been bumped by the reaper cannot open brand-new
    effect rows against the directive it no longer holds.  Raises ``PermissionError`` when fenced
    out; the route maps that to ``409 stale_lease``.
    """
    _reject_forbidden_pair(str(enforcement_tier), str(action_tracing))
    digest = effect_intent_digest(intent)
    existing = conn.execute(
        f"SELECT {_EFFECT_COLUMNS} FROM effect_journal WHERE directive_id = ? AND intent_digest = ? LIMIT 1",
        (str(directive_id), digest),
    ).fetchone()
    if existing is not None:
        return str(existing["effect_id"])
    fenced = conn.execute(
        """
        SELECT 1 FROM directive_executions
        WHERE directive_id = ? AND lease_generation = ? AND (claimed_executor = ? OR ? = 1)
        LIMIT 1
        """,
        (str(directive_id), int(lease_generation), str(claimed_executor), 1 if str(claimed_executor) in _SYSTEM_ONLY_SOURCES else 0),
    ).fetchone()
    if fenced is None:
        raise PermissionError("stale_lease")
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM effect_journal WHERE directive_id = ?",
        (str(directive_id),),
    ).fetchone()
    effect_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO effect_journal (
            effect_id, workspace_id, owner_id, session_id, task_id, directive_id, dispatch_id, seq,
            state, kind, reversibility, capability, resource, argv_json, description, intent_digest,
            enforcement_tier, action_tracing, lease_generation, claimed_executor, provider_run_id,
            provider_turn_id, runtime_id, runtime_version, model_id, opened_at, resolved_at,
            resolution_source, resolved_by_actor, evidence_json, created_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, NULL, NULL, NULL, '{}', ?, ?)
        ON CONFLICT (directive_id, intent_digest) DO NOTHING
        """,
        (
            effect_id,
            workspace_id,
            owner_id,
            session_id,
            task_id,
            str(directive_id),
            (str(dispatch_id) if dispatch_id else None),
            int(seq_row["max_seq"] or 0) + 1,
            intent.kind,
            intent.reversibility,
            intent.capability,
            intent.resource,
            json.dumps(list(intent.argv)),
            intent.description,
            digest,
            str(enforcement_tier),
            str(action_tracing),
            int(lease_generation),
            str(claimed_executor),
            provider_run_id,
            provider_turn_id,
            str(runtime_id or ""),
            str(runtime_version or ""),
            str(model_id or ""),
            now.isoformat(),
            now.isoformat(),
            EFFECT_SCHEMA_VERSION,
        ),
    )
    settled = conn.execute(
        "SELECT effect_id FROM effect_journal WHERE directive_id = ? AND intent_digest = ? LIMIT 1",
        (str(directive_id), digest),
    ).fetchone()
    return str(settled["effect_id"]) if settled is not None else effect_id


def resolve_effect(
    conn: sqlite3.Connection,
    *,
    effect_id: str,
    target_state: str,
    actor: str,
    resolution_source: str,
    evidence: Mapping[str, Any],
    expected_lease: int,
    now: datetime,
) -> bool:
    """The fenced CAS.  ``False`` means the caller lost — a concurrent resolve won, or the reaper
    bumped the lease and this actor is stale.  The route re-reads and returns ``409 stale_lease``.

    ``actor`` is derived server-side by the caller; there is no ``actor`` field on the wire.
    """
    row = conn.execute(
        f"SELECT {_EFFECT_COLUMNS} FROM effect_journal WHERE effect_id = ? LIMIT 1",
        (str(effect_id),),
    ).fetchone()
    if row is None:
        return False
    current_state = str(row["state"])
    validate_effect_transition(
        current_state=current_state,
        target_state=str(target_state),
        actor=str(actor),
        reversibility=str(row["reversibility"] or "unknown"),
    )
    actor_is_system = 1 if str(actor) in _SYSTEM_ONLY_SOURCES else 0
    # G3 -- the fence compares the DIRECTIVE's claimed_executor against the AUTHENTICATED caller,
    # never against the journal row's own copy of it. Binding `row["claimed_executor"]` made the
    # comparison a row comparing itself: any executor could resolve any other executor's effect,
    # because the two columns were written by the same claim. Parity with Full's `executor_id`.
    executor_id = str(actor).split("executor:", 1)[1] if str(actor).startswith("executor:") else str(actor)
    updated = conn.execute(
        """
        UPDATE effect_journal
           SET state = ?, resolved_at = ?, resolution_source = ?, resolved_by_actor = ?, evidence_json = ?
         WHERE effect_id = ?
           AND state = ?
           AND EXISTS (
                 SELECT 1 FROM directive_executions d
                  WHERE d.directive_id = effect_journal.directive_id
                    AND d.lease_generation = ?
                    AND (d.claimed_executor = ? OR ? = 1)
               )
        """,
        (
            str(target_state),
            now.isoformat(),
            str(resolution_source),
            str(actor),
            json.dumps(dict(evidence or {})),
            str(effect_id),
            current_state,
            int(expected_lease),
            executor_id,
            actor_is_system,
        ),
    )
    return int(updated.rowcount) == 1


def list_open_effects(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
) -> list[EffectRecord]:
    rows = conn.execute(
        f"""
        SELECT {_EFFECT_COLUMNS} FROM effect_journal
        WHERE workspace_id = ? AND owner_id = ? AND session_id = ?
          AND state IN ('prepared', 'running', 'unknown')
        ORDER BY opened_at ASC, seq ASC
        """,
        (workspace_id, owner_id, session_id),
    ).fetchall()
    return [record_from_row(row) for row in rows]


def load_effects_for_directive(conn: sqlite3.Connection, *, directive_id: str) -> list[EffectRecord]:
    rows = conn.execute(
        f"SELECT {_EFFECT_COLUMNS} FROM effect_journal WHERE directive_id = ? ORDER BY seq ASC",
        (str(directive_id),),
    ).fetchall()
    return [record_from_row(row) for row in rows]


def open_effect_count_for_session(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    exclude_directive_id: str | None,
) -> int:
    """The count behind the §6.4 completion obligation.

    ``exclude_directive_id`` is mandatory in practice, not an optimisation: the dispatch's own root
    effect is ``running`` for the whole of a healthy run, so without the exclusion every dispatch
    would block its own capability grants.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM effect_journal
        WHERE workspace_id = ? AND owner_id = ? AND session_id = ?
          AND state IN ('prepared', 'running', 'unknown')
          AND (? IS NULL OR directive_id <> ?)
        """,
        (
            workspace_id,
            owner_id,
            session_id,
            (str(exclude_directive_id) if exclude_directive_id else None),
            (str(exclude_directive_id) if exclude_directive_id else ""),
        ),
    ).fetchone()
    return int(row["n"] or 0)
