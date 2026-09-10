"""Pure directive-lifecycle transition validation shared by the Full and Lite runtimes.

This module is the single source of truth for which execution-state transitions are legal, who may
drive them, and how a lease generation (fencing token) advances.  It has no I/O: runtimes evaluate a
``TransitionDecision`` against a freshly read row and then commit with a conditional UPDATE
(``WHERE state = :expected AND lease_generation = :expected_lease``) so that concurrent claim/report
attempts cannot both succeed.

Imports are limited to the standard library and ``.events`` on purpose.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .events import DirectiveExecutionState as DirectiveState

SYSTEM_ACTOR = "system:reaper"  # actor value used by reapers / stand-down; bypasses claim+lease checks

TERMINAL_STATES: frozenset[DirectiveState] = frozenset(
    {
        DirectiveState.SUCCEEDED,
        DirectiveState.FAILED,
        DirectiveState.BLOCKED,
        DirectiveState.ABANDONED,
        DirectiveState.CANCELLED,
        DirectiveState.REJECTED,
    }
)


class TransitionReason(StrEnum):
    OK = "ok"
    INVALID_TRANSITION = "invalid_transition"
    NOT_CLAIMED = "not_claimed"
    CLAIMED_BY_OTHER = "claimed_by_other"
    STALE_LEASE = "stale_lease"
    LEASE_REQUIRED = "lease_required"
    PERMIT_INVALID = "permit_invalid"
    TERMINAL = "terminal"
    SYSTEM_ONLY = "system_only"
    CONCURRENT_UPDATE = "concurrent_update"  # runtime uses this when the fenced UPDATE hit 0 rows and re-read still validates


@dataclass(frozen=True, slots=True)
class Transition:
    source: DirectiveState
    target: DirectiveState
    requires_claim: bool  # actor must equal row.claimed_executor and lease must match
    requires_permit: bool  # permit_ok must be True
    system_only: bool  # only SYSTEM_ACTOR may drive it
    bumps_lease: bool  # next_lease = current + 1
    audited_as: str  # lifecycle/audit action label


ALLOWED_TRANSITIONS: tuple[Transition, ...] = (
    Transition(DirectiveState.PENDING, DirectiveState.IN_PROGRESS, requires_claim=False, requires_permit=True, system_only=False, bumps_lease=True, audited_as="directive_claimed"),
    Transition(DirectiveState.IN_PROGRESS, DirectiveState.IN_PROGRESS, requires_claim=True, requires_permit=False, system_only=False, bumps_lease=False, audited_as="directive_reclaim_noop"),
    Transition(DirectiveState.IN_PROGRESS, DirectiveState.SUCCEEDED, requires_claim=True, requires_permit=True, system_only=False, bumps_lease=False, audited_as="directive_succeeded"),
    Transition(DirectiveState.IN_PROGRESS, DirectiveState.FAILED, requires_claim=True, requires_permit=True, system_only=False, bumps_lease=False, audited_as="directive_failed"),
    Transition(DirectiveState.IN_PROGRESS, DirectiveState.BLOCKED, requires_claim=True, requires_permit=False, system_only=False, bumps_lease=False, audited_as="directive_blocked"),
    Transition(DirectiveState.IN_PROGRESS, DirectiveState.CANCELLED, requires_claim=True, requires_permit=False, system_only=False, bumps_lease=False, audited_as="directive_cancelled"),
    Transition(DirectiveState.PENDING, DirectiveState.CANCELLED, requires_claim=False, requires_permit=False, system_only=False, bumps_lease=False, audited_as="directive_cancelled"),
    Transition(DirectiveState.PENDING, DirectiveState.REJECTED, requires_claim=False, requires_permit=False, system_only=False, bumps_lease=False, audited_as="directive_rejected"),
    Transition(DirectiveState.PENDING, DirectiveState.ABANDONED, requires_claim=False, requires_permit=False, system_only=True, bumps_lease=False, audited_as="directive_reaped"),
    Transition(DirectiveState.IN_PROGRESS, DirectiveState.ABANDONED, requires_claim=False, requires_permit=False, system_only=True, bumps_lease=True, audited_as="directive_reaped"),
)

_TRANSITION_INDEX: dict[tuple[DirectiveState, DirectiveState], Transition] = {(t.source, t.target): t for t in ALLOWED_TRANSITIONS}

# Report states that only make sense once execution has been claimed; requesting them from PENDING is
# the "unclaimed report" attack and gets the dedicated NOT_CLAIMED reason rather than INVALID_TRANSITION.
_REPORT_ONLY_TARGETS: frozenset[DirectiveState] = frozenset({DirectiveState.SUCCEEDED, DirectiveState.FAILED, DirectiveState.BLOCKED})


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    allowed: bool
    reason: TransitionReason
    next_state: DirectiveState | None
    next_lease: int
    http_status: int  # 0 when allowed, 409 otherwise
    audited_as: str  # "" when not allowed
    message: str


def _coerce_state(value: DirectiveState | str) -> DirectiveState | None:
    if isinstance(value, DirectiveState):
        return value
    try:
        return DirectiveState(str(value).strip().lower())
    except ValueError:
        return None


def is_terminal(state: DirectiveState | str) -> bool:
    coerced = _coerce_state(state)
    return coerced is not None and coerced in TERMINAL_STATES


def next_lease(current: int | None) -> int:
    return (current or 0) + 1


def find_transition(current: DirectiveState | str, requested: DirectiveState | str) -> Transition | None:
    source = _coerce_state(current)
    target = _coerce_state(requested)
    if source is None or target is None:
        return None
    return _TRANSITION_INDEX.get((source, target))


def _reject(reason: TransitionReason, message: str, lease_generation: int | None) -> TransitionDecision:
    return TransitionDecision(allowed=False, reason=reason, next_state=None, next_lease=lease_generation or 0, http_status=409, audited_as="", message=message)


def validate_transition(
    *,
    current_state: DirectiveState | str,
    claimed_by: str | None,  # row.claimed_executor (auth-bound), NOT the label
    lease_generation: int | None,  # row.lease_generation; None treated as 0
    requested: DirectiveState | str,
    actor: str,  # auth.consumer, or SYSTEM_ACTOR
    actor_lease: int | None,  # lease echoed by caller; None = legacy client
    permit_ok: bool = True,
    require_lease_echo: bool = False,  # settings.takeover_lease_strict
) -> TransitionDecision:
    """Decide whether ``actor`` may move a directive from ``current_state`` to ``requested``.

    Evaluation order (first match wins): terminal -> table lookup -> system-only -> pending-row ownership ->
    claim/lease binding -> permit -> allowed.
    """
    current_lease = lease_generation or 0
    is_system = actor == SYSTEM_ACTOR

    if is_terminal(current_state):
        return _reject(TransitionReason.TERMINAL, "directive already terminal", current_lease)

    transition = find_transition(current_state, requested)
    if transition is None:
        source = _coerce_state(current_state)
        target = _coerce_state(requested)
        if source is DirectiveState.PENDING and target in _REPORT_ONLY_TARGETS:
            return _reject(TransitionReason.NOT_CLAIMED, "directive must be claimed before it can be reported", current_lease)
        return _reject(TransitionReason.INVALID_TRANSITION, f"transition {_label(current_state)}->{_label(requested)} is not allowed", current_lease)

    if transition.system_only and not is_system:
        return _reject(TransitionReason.SYSTEM_ONLY, "transition is reserved for the system reaper", current_lease)

    if transition.source is DirectiveState.PENDING and transition.target is DirectiveState.IN_PROGRESS and claimed_by not in (None, "", actor):
        return _reject(TransitionReason.CLAIMED_BY_OTHER, "directive is already bound to another executor", current_lease)

    if transition.requires_claim and not is_system:
        if claimed_by in (None, ""):
            return _reject(TransitionReason.NOT_CLAIMED, "directive is not claimed by any executor; claim it before reporting", current_lease)
        if claimed_by != actor:
            return _reject(TransitionReason.CLAIMED_BY_OTHER, "directive is claimed by another executor", current_lease)
        if actor_lease is None and require_lease_echo:
            return _reject(TransitionReason.LEASE_REQUIRED, "lease_generation must be echoed by the reporting executor", current_lease)
        if actor_lease is not None and actor_lease != current_lease:
            return _reject(TransitionReason.STALE_LEASE, f"stale lease {actor_lease}; current lease is {current_lease}", current_lease)

    if transition.requires_permit and not permit_ok:
        return _reject(TransitionReason.PERMIT_INVALID, "execution permit is missing, expired or out of scope", current_lease)

    return TransitionDecision(
        allowed=True,
        reason=TransitionReason.OK,
        next_state=transition.target,
        next_lease=next_lease(current_lease) if transition.bumps_lease else current_lease,
        http_status=0,
        audited_as=transition.audited_as,
        message="ok",
    )


def _label(value: DirectiveState | str) -> str:
    return value.value if isinstance(value, DirectiveState) else str(value)


# --- idempotency helpers -----------------------------------------------------------------------------------------

_REPORT_FINGERPRINT_KEYS = ("state", "result", "failure_reason", "details", "step_id", "step_output", "contract_type", "rollback_performed")
_COMPLETION_VOLATILE_KEYS = frozenset({"ts", "created_at", "captured_at", "packet_id", "id", "session_id"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def report_payload_fingerprint(body: Mapping[str, Any]) -> str:
    """Hash of the report's semantic payload; transport fields (session/directive ids, lease, key) are ignored."""
    subset: dict[str, Any] = {key: body.get(key) for key in _REPORT_FINGERPRINT_KEYS}
    state = subset.get("state")
    if state is not None:
        subset["state"] = str(state)
    return _sha256_hex(canonical_json(subset))


def completion_payload_fingerprint(milestone: Mapping[str, Any]) -> str:
    return _sha256_hex(canonical_json({key: value for key, value in milestone.items() if key not in _COMPLETION_VOLATILE_KEYS}))


def idempotency_outcome(existing_payload_hash: str | None, new_payload_hash: str) -> str:
    if existing_payload_hash is None:
        return "new"
    if existing_payload_hash == new_payload_hash:
        return "replay"
    return "conflict"


# --- permit binding helpers --------------------------------------------------------------------------------------


def permit_scope_digest(*, action_kind: str, target_paths: list[str] | tuple[str, ...], command_preview: str | None) -> str:
    return _sha256_hex(canonical_json({"action_kind": action_kind, "target_paths": sorted(set(target_paths)), "command_preview": command_preview or ""}))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def permit_binding_ok(
    *,
    permit_decision: str | None,  # row.decision
    permit_expires_at: datetime | None,
    permit_user_id: str | None,  # NULL on legacy permits => wildcard
    permit_directive_id: str | None,  # NULL => wildcard
    permit_objective_hash: str | None,  # NULL => wildcard
    permit_scope_digest: str | None = None,  # row.scope_digest
    expected_scope_digest: str | None = None,  # recomputed from the declared paths
    scope_digest_enforced: bool = False,  # a NULL row under enforcement must REFUSE, not skip
    permit_charter_id: str | None = None,  # the charter the permit was issued under
    active_charter_id: str | None = None,  # the charter active NOW
    directive_id: str,
    directive_user_id: str,
    directive_objective_hash: str | None,
    started_at: datetime | None,  # row.started_at (None at claim time)
    now: datetime,
    phase: str,  # "claim" | "report"
) -> tuple[bool, str]:
    """Check that a permit authorises this directive for ``phase``.

    At report time an expired permit is still honoured when execution demonstrably began under the
    valid grant (``started_at <= permit_expires_at``); authorisation is judged when the effect was
    attempted, not when the receipt arrives.

    Two later bindings, both defaulted so every pre-existing call form is unchanged:

    * **scope** — ``permit_scope_digest`` is the digest stored on the permit row and
      ``expected_scope_digest`` the one recomputed from the action being attempted.  When
      ``scope_digest_enforced`` is True a missing digest on either side REFUSES: a NULL that
      silently skips the check is how a written-but-never-read column stays unread.
    * **charter** — when both charter ids are present and differ, the permit was granted under an
      authority that is no longer the active one, so it no longer authorises anything.
    """
    if phase not in ("claim", "report"):
        raise ValueError(f"unknown permit phase: {phase!r}")
    if permit_decision != "allow":
        return False, "permit_not_allowed"
    if scope_digest_enforced and (permit_scope_digest is None or expected_scope_digest is None):
        return False, "permit_scope_digest_missing"
    if permit_scope_digest is not None and expected_scope_digest is not None and permit_scope_digest != expected_scope_digest:
        return False, "permit_scope_digest_mismatch"
    if permit_charter_id is not None and active_charter_id is not None and permit_charter_id != active_charter_id:
        return False, "permit_charter_superseded"
    if permit_user_id is not None and permit_user_id != directive_user_id:
        return False, "permit_scope_mismatch"
    if permit_directive_id is not None and permit_directive_id != directive_id:
        return False, "permit_scope_mismatch"
    if permit_objective_hash is not None and permit_objective_hash != directive_objective_hash:
        return False, "permit_scope_mismatch"
    if permit_expires_at is None:
        return True, "ok"
    expires_at = _as_utc(permit_expires_at)
    if phase == "claim":
        if expires_at > _as_utc(now):
            return True, "ok"
        return False, "permit_expired"
    if started_at is not None and _as_utc(started_at) <= expires_at:
        return True, "ok"
    return False, "permit_expired_before_start"
