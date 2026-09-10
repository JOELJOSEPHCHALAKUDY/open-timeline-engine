"""Startup reconciliation and the unresolved-effect pause guard for the FULL backend.

The manager owns restart.  ``startup_reconcile`` runs from the app lifespan **before anything can
dispatch**, enumerates stuck directives workspace-wide (today's reaper only ever looks at the most
recent directive of the session currently taking a turn), resolves what it can, and records
``unknown`` for what it cannot.

``unknown`` is the point of the whole module.  A reaped directive whose open effect may be
irreversible must not be laundered into "closed" — it PAUSES, and the pause is what stops the
system silently restarting work whose outcome nobody knows.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.effect_journal import EFFECT_ACTOR_RECONCILER, pause_required

from .config import Settings

logger = logging.getLogger(__name__)

# Read by reconcile_complete(); POST /v1/dispatch refuses with 409 reconcile_pending while False.
_RECONCILE_DONE = False

PAUSE_TEXT_PREFIX = "AUTONOMOUS MODE PAUSED: an effect from a previous run is unresolved"


def reconcile_complete() -> bool:
    return _RECONCILE_DONE


def _mark_complete() -> None:
    global _RECONCILE_DONE
    _RECONCILE_DONE = True


def reset_for_tests() -> None:
    """Reset the module flag.  Only tests call this; nothing in a request path does."""
    global _RECONCILE_DONE
    _RECONCILE_DONE = False


def pause_guard_for_session(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    settings: Settings,
) -> tuple[bool, str, str]:
    """``(paused, reason, effect_id)``.

    The ONLY reader of ``effect_unknown_pause_enabled``.  Only an ``unknown`` effect whose
    reversibility is ``irreversible`` or ``unknown`` pauses; an open ``prepared``/``running``
    effect belongs to a healthy run and must not stop the session minting work.
    """
    if not bool(settings.effect_unknown_pause_enabled):
        return (False, "", "")
    from .effect_store import list_open_effects

    records = list_open_effects(db, workspace_id=workspace_id, owner_id=owner_id, session_id=session_id)
    paused, reason = pause_required(records)
    if not paused or reason != "unresolved_irreversible_effect":
        # 'effect_still_open' is a healthy dispatch in flight, never a pause.
        return (False, "", "")
    for record in records:
        if record.state == "unknown" and record.intent.reversibility in ("irreversible", "unknown"):
            return (True, reason, record.effect_id)
    return (True, reason, "")


def pause_text_for(effect_id: str) -> str:
    return (
        f"{PAUSE_TEXT_PREFIX} and may be irreversible. "
        f"Resolve effect {effect_id} before continuing."
    )


def _stale_window_seconds(settings: Settings) -> int:
    return max(120, int(settings.takeover_execution_claim_ttl_seconds) * 3)


def startup_reconcile(db: Session, *, settings: Settings) -> dict[str, int]:
    """Enumerate stuck directives, resolve their effects by reading, and drain the outbox.

    Returns ``{"scanned","reaped","effects_unknown","effects_resolved","paused","drained"}``.
    Sets the flag ``reconcile_complete()`` reads.
    """
    counts = {"scanned": 0, "reaped": 0, "effects_unknown": 0, "effects_resolved": 0, "paused": 0, "drained": 0}
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(seconds=_stale_window_seconds(settings))
    batch = max(1, int(settings.dispatch_startup_reconcile_batch))
    rows = db.execute(
        text(
            """
            SELECT directive_id, workspace_id, owner_id, session_id, state, lease_generation,
                   claimed_executor
            FROM (
              SELECT directive_id, workspace_id, user_id AS owner_id, session_id, state,
                     lease_generation, claimed_executor, created_at, updated_at
              FROM directive_executions
              WHERE state IN ('pending', 'in_progress')
            ) AS d
            WHERE d.updated_at <= :cutoff
            ORDER BY d.created_at ASC
            LIMIT :batch
            """
        ),
        {"cutoff": cutoff, "batch": batch},
    ).mappings().all()
    for row in rows:
        counts["scanned"] += 1
        directive_id = row["directive_id"]
        if str(row["state"]) == "in_progress":
            # Bump the lease exactly as the existing reaper does, so a returning worker's later
            # report — and, with the U4 fence, its later effect resolve — presents an old lease
            # and is rejected.
            db.execute(
                text(
                    """
                    UPDATE directive_executions
                       SET state = 'abandoned',
                           lease_generation = lease_generation + 1,
                           finished_at = COALESCE(finished_at, :now),
                           failure_reason = COALESCE(failure_reason, 'startup_reconcile_reaped'),
                           updated_at = :now
                     WHERE directive_id = :directive_id
                       AND state = 'in_progress'
                    """
                ),
                {"now": now, "directive_id": directive_id},
            )
            counts["reaped"] += 1
        if bool(settings.effect_journal_enabled):
            counts_delta = resolve_effects_for_directive(db, directive_id=directive_id, now=now)
            counts["effects_unknown"] += counts_delta["unknown"]
            counts["effects_resolved"] += counts_delta["resolved"]
            if counts_delta["unknown"]:
                counts["paused"] += 1
    db.commit()
    if bool(settings.effect_journal_enabled):
        try:
            from .continuity_store import drain_pending_handoffs

            drained = drain_pending_handoffs(db, retention_days=int(settings.effect_journal_retention_days))
            counts["drained"] = int(drained.get("delivered", 0))
        except Exception:
            logger.exception("startup reconcile: outbox drain failed")
    _mark_complete()
    _audit_reconcile(counts)
    return counts


def resolve_effects_for_directive(db: Session, *, directive_id: uuid.UUID, now: datetime) -> dict[str, int]:
    """Resolve a reaped directive's open effects.

    PUBLIC because the live in-turn reaper calls it too.  ``main._load_pending_directive`` runs
    the same resolution inside the SAME transaction as its lease bump, so the pause fires on the
    turn that reaps rather than only after a restart; if the reaper resolved nothing, an open
    ``running`` effect would read as a healthy dispatch and ``takeover_step`` would mint fresh
    work over an outcome nobody knows.  It does NOT commit — the caller owns the transaction.

    RESOLVE BY READING FIRST is the supervisor's job (it holds the provider record); the manager
    marks what it cannot resolve.  A ``reversible`` open effect becomes ``failed`` and the retry
    ladder proceeds as today; an ``irreversible`` or ``unknown`` one becomes ``unknown``, which is
    NOT terminal and which pauses the session.
    """
    delta = {"unknown": 0, "resolved": 0}
    rows = db.execute(
        text(
            """
            SELECT effect_id, state, reversibility, provider_run_id
            FROM effect_journal
            WHERE directive_id = :directive_id
              AND state IN ('prepared', 'running')
            ORDER BY seq ASC
            """
        ),
        {"directive_id": directive_id},
    ).mappings().all()
    for row in rows:
        reversibility = str(row["reversibility"] or "unknown")
        target = "unknown" if reversibility in ("irreversible", "unknown") else "failed"
        evidence: dict[str, Any] = {"reconciled_at": now.isoformat()}
        if row["provider_run_id"]:
            # The supervisor resolves this by reading the provider's own durable record; the
            # marker says which rows are waiting for that read rather than for a human.
            evidence["reconcile_pending"] = True
            evidence["provider_run_id"] = str(row["provider_run_id"])
        db.execute(
            text(
                """
                UPDATE effect_journal
                   SET state = :target,
                       resolved_at = :now,
                       resolution_source = 'reaper',
                       resolved_by_actor = :actor,
                       evidence_json = CAST(:evidence AS JSONB)
                 WHERE effect_id = :effect_id
                   AND state = :current
                """
            ),
            {
                "target": target,
                "now": now,
                "actor": EFFECT_ACTOR_RECONCILER,
                "evidence": json.dumps(evidence),
                "effect_id": row["effect_id"],
                "current": str(row["state"]),
            },
        )
        if target == "unknown":
            delta["unknown"] += 1
        else:
            delta["resolved"] += 1
    return delta


def _audit_reconcile(counts: dict[str, int]) -> None:
    """A refusal or a reap that is not durably recorded is not evidence.

    This writes synchronously regardless of ``audit_write_mode`` (which defaults to ``async`` in
    Full — a daemon thread that swallows failures).
    """
    try:
        from .audit import _write_audit_sync

        _write_audit_sync("system:reconciler", "startup_reconcile", dict(counts), [], {}, 0, raise_on_error=False)
    except Exception:
        logger.warning("startup reconcile audit write failed", exc_info=True)
