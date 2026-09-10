"""Lite startup reconcile and the pause guard — the twin of ``tce_api.reconcile``.

Two things this module exists to stop:

* **Silently restarting work after a crash.**  Today's reaper reads one session's newest directive
  from inside ``takeover_step``; a directive in an abandoned session is never reaped at all.  This
  enumerates workspace-wide at boot.
* **Restarting work over an effect nobody resolved.**  An open effect whose reversibility is
  ``irreversible`` or ``unknown`` becomes ``state='unknown'`` — *we do not know what it did* — and
  :func:`pause_guard_for_session` then refuses to mint new work for that session until a human or
  the provider record resolves it.

**Lite has no supervisor and no worker.**  §6.5 step 2 has the supervisor resolve an effect by
reading the provider's own record.  Lite has no runtime adapter, so it cannot do that: it records a
``reconcile_pending`` marker in ``evidence_json`` for every open effect that carries a
``provider_run_id`` and leaves the row open for whoever can read it, rather than guessing an outcome
and writing it down as fact.  That is the stated Lite behaviour, not a degraded copy of Full's.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.effect_journal import (
    EFFECT_ACTOR_RECONCILER,
    EffectRecord,
    pause_required,
)

from .config import Settings

_RECONCILE_DONE = False


def reconcile_complete() -> bool:
    """The reader of the module flag.  ``POST /v1/dispatch`` returns ``409 reconcile_pending``
    while this is False.

    It is deliberately fail-closed: with ``dispatch_startup_reconcile_enabled=0`` no reconcile runs,
    the flag stays False, and dispatch is refused.  Turning off the sweep that finds unresolved
    effects is not a licence to start new ones.
    """
    return _RECONCILE_DONE


def pause_guard_for_session(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    settings: Settings,
) -> tuple[bool, str, str]:
    """``(paused, reason, effect_id)``.  The single reader of ``effect_unknown_pause_enabled``.

    Read at the two request paths that can mint a directive, exactly as Full reads it: the
    ``report_execution`` block that classifies a failure and schedules the retry ladder, and
    ``takeover_step``'s own mint.  ``takeover_step`` is the load-bearing one -- that is where work
    actually restarts after a reap, and a guard on the retry ladder alone would never fire because
    a reaped directive is ABANDONED and the ladder only looks at FAILED/BLOCKED.
    """
    if not bool(settings.effect_unknown_pause_enabled):
        return (False, "", "")
    from .effect_store import list_open_effects

    records: list[EffectRecord] = list_open_effects(
        conn, workspace_id=workspace_id, owner_id=owner_id, session_id=session_id
    )
    paused, reason = pause_required(records)
    if not paused:
        return (False, "", "")
    if reason == "unresolved_irreversible_effect":
        for record in records:
            if record.state == "unknown" and record.intent.reversibility in ("irreversible", "unknown"):
                return (True, reason, record.effect_id)
    # `effect_still_open` is a healthy live dispatch, not a pause: prepared/running rows are
    # non-empty for the whole of a working run. Only the unknown case blocks a new mint.
    return (False, "", "")


def _stale_window_seconds(settings: Settings) -> int:
    """Full's derivation, adopted here in place of Lite's hardcoded 900."""
    return max(120, int(settings.takeover_execution_claim_ttl_seconds) * 3)


def resolve_effects_for_directive(
    conn: sqlite3.Connection,
    *,
    directive_id: str,
    bumped_lease: int,
    settings: Settings,
    now: datetime,
) -> dict[str, int]:
    """The effect-resolution half of a reap.  Shared by the boot sweep and the LIVE in-turn reaper.

    G1: ``_load_pending_directive`` used to bump the lease and leave every open effect exactly as
    it was, so ``pause_guard_for_session`` -- which runs a few lines later in the same
    ``takeover_step`` -- found nothing to pause on.  The unknown-effect pause therefore only ever
    fired after a restart, which is the branch production never takes.  Both reapers now run this
    same body, and the caller runs it inside the transaction that carries the lease bump so the
    fenced CAS below sees ``bumped_lease`` and a returning worker does not.

    Does not commit: the caller owns the transaction boundary.
    """
    counters = {"effects_unknown": 0, "effects_resolved": 0}
    if not bool(settings.effect_journal_enabled):
        return counters
    from .effect_store import load_effects_for_directive, resolve_effect

    for record in load_effects_for_directive(conn, directive_id=str(directive_id)):
        if record.state in ("confirmed", "failed", "unknown"):
            continue
        if record.provider_run_id:
            # 2. RESOLVE BY READING FIRST -- but Lite has no adapter to read with. Record the
            #    obligation durably instead of inventing an outcome.
            conn.execute(
                "UPDATE effect_journal SET evidence_json = ? WHERE effect_id = ?",
                (
                    json.dumps({"reconcile_pending": True, "provider_run_id": record.provider_run_id, "marked_at": now.isoformat()}),
                    record.effect_id,
                ),
            )
            if resolve_effect(
                conn,
                effect_id=record.effect_id,
                target_state="unknown",
                actor=EFFECT_ACTOR_RECONCILER,
                resolution_source="reaper",
                evidence={"reconcile_pending": True, "provider_run_id": record.provider_run_id},
                expected_lease=int(bumped_lease),
                now=now,
            ):
                counters["effects_unknown"] += 1
            continue
        if record.intent.reversibility == "reversible":
            # A reversible effect is safe to retry; the ladder proceeds as it does today.
            if resolve_effect(
                conn,
                effect_id=record.effect_id,
                target_state="failed",
                actor=EFFECT_ACTOR_RECONCILER,
                resolution_source="reaper",
                evidence={"reason": "reaped_without_provider_record"},
                expected_lease=int(bumped_lease),
                now=now,
            ):
                counters["effects_resolved"] += 1
            continue
        if resolve_effect(
            conn,
            effect_id=record.effect_id,
            target_state="unknown",
            actor=EFFECT_ACTOR_RECONCILER,
            resolution_source="reaper",
            evidence={"reason": "reaped_without_provider_record"},
            expected_lease=int(bumped_lease),
            now=now,
        ):
            counters["effects_unknown"] += 1
    return counters


def startup_reconcile(conn: sqlite3.Connection, *, settings: Settings) -> dict[str, int]:
    """Sweep stuck directives and their effects.  Returns the six counters.

    Ordering matters and is asserted in both backends: enumerate, resolve-by-reading where a
    provider record exists, bump the lease, mark the unresolvable ``unknown``, drain the outbox,
    then flip the flag that lets dispatch open.
    """
    global _RECONCILE_DONE
    from .continuity_store import drain_pending_handoffs

    now = datetime.now(tz=UTC)
    counters = {"scanned": 0, "reaped": 0, "effects_unknown": 0, "effects_resolved": 0, "paused": 0, "drained": 0}
    rows = conn.execute(
        """
        SELECT directive_id, workspace_id, user_id, session_id, state, lease_generation,
               lease_expires_at, claimed_executor
        FROM directive_executions
        WHERE state IN ('pending', 'in_progress')
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (max(1, int(settings.dispatch_startup_reconcile_batch)),),
    ).fetchall()
    stale_before = now - timedelta(seconds=_stale_window_seconds(settings))
    paused_sessions: set[tuple[str, str, str]] = set()
    for row in rows:
        counters["scanned"] += 1
        directive_id = str(row["directive_id"])
        expires_raw = row["lease_expires_at"]
        expires_at = datetime.fromisoformat(str(expires_raw)) if expires_raw else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        is_stale = expires_at is None or expires_at < stale_before
        if not is_stale:
            continue
        # 3. Bump the lease exactly as the live reaper does, so a returning worker's later report
        #    (and, with U4, a late effect resolve) presents an old lease and is rejected.
        bumped = int(row["lease_generation"] or 0) + 1
        conn.execute(
            """
            UPDATE directive_executions
               SET state = 'abandoned', lease_generation = ?, lease_expires_at = NULL, updated_at = ?
             WHERE directive_id = ? AND lease_generation = ?
            """,
            (bumped, now.isoformat(), directive_id, int(row["lease_generation"] or 0)),
        )
        counters["reaped"] += 1
        resolved = resolve_effects_for_directive(
            conn,
            directive_id=directive_id,
            bumped_lease=bumped,
            settings=settings,
            now=now,
        )
        counters["effects_unknown"] += resolved["effects_unknown"]
        counters["effects_resolved"] += resolved["effects_resolved"]
        if resolved["effects_unknown"] > 0:
            paused_sessions.add((str(row["workspace_id"]), str(row["user_id"]), str(row["session_id"])))
    conn.commit()
    for workspace_id, owner_id, session_id in sorted(paused_sessions):
        paused, _reason, _effect_id = pause_guard_for_session(
            conn, workspace_id=workspace_id, owner_id=owner_id, session_id=session_id, settings=settings
        )
        if paused:
            counters["paused"] += 1
    drained: dict[str, int] = drain_pending_handoffs(conn, retention_days=int(settings.handoff_retention_days))
    counters["drained"] = int(drained.get("processed", 0))
    conn.commit()
    _RECONCILE_DONE = True
    return counters


def reset_for_tests() -> None:
    """Clear the module flag.  Used only by the Lite test fixtures, which build a fresh app per test."""
    global _RECONCILE_DONE
    _RECONCILE_DONE = False


def reconcile_state() -> dict[str, Any]:
    """Operator view of the flag, for the governance surface."""
    return {"reconcile_complete": _RECONCILE_DONE}
