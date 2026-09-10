"""The effect journal — the epistemic axis, kept separate from the authority axis.

``execution_transitions.py`` answers an **authority/lease** question: *may this actor act, and did a
receipt arrive?*  This module answers an **epistemic** one: *did the world change, and do we know?*
They are different axes and must not be collapsed.  A directive may be terminal while its effects
are ``running`` or ``unknown`` — exactly the situation the directive table cannot express.

``unknown`` is a real state, not a synonym for failure.  A killed or crashed run whose effect might
be irreversible resolves to ``unknown``, which **pauses** autonomy; it never enters the retry ladder.
Commands therefore default to ``reversibility="unknown"``, never ``"reversible"``: a shell one-liner
may be ``git push``.

This module composes with ``execution_transitions.py`` and never contradicts it.
``DirectiveExecutionState``, ``ALLOWED_TRANSITIONS``, ``TERMINAL_STATES`` and ``validate_transition``
are untouched.

Standard library only.  It must not import ``.events`` — the supervisor loads this module and
``.events`` pulls in pydantic.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

EFFECT_SCHEMA_VERSION: str = "v1"
EFFECT_STATES: tuple[str, ...] = ("prepared", "running", "confirmed", "failed", "unknown")
EFFECT_TERMINAL_STATES: frozenset[str] = frozenset({"confirmed", "failed"})
EFFECT_KINDS: tuple[str, ...] = ("directive", "write", "command", "commit", "external")
REVERSIBILITY: tuple[str, str, str] = ("reversible", "irreversible", "unknown")
ACTION_TRACING: tuple[str, str] = ("observed", "unavailable")
RESOLUTION_SOURCES: tuple[str, ...] = ("runtime_result", "provider_read", "tree_inspection", "reaper", "owner")
EFFECT_ACTOR_RECONCILER: str = "system:reconciler"
EFFECT_ACTOR_VERIFIER: str = "system:verifier"
EFFECT_ACTOR_OWNER: str = "system:owner"

EFFECT_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("prepared", "running"),
        ("prepared", "failed"),
        ("prepared", "unknown"),
        ("running", "confirmed"),
        ("running", "failed"),
        ("running", "unknown"),
        ("unknown", "confirmed"),
        ("unknown", "failed"),
    }
)

_SYSTEM_ONLY_SOURCES: frozenset[str] = frozenset({EFFECT_ACTOR_RECONCILER, EFFECT_ACTOR_VERIFIER, EFFECT_ACTOR_OWNER})

# A stale worker resolving to 'failed' must not make 'unknown' unreachable forever.  'failed' is
# terminal for a NORMAL actor; the reconciler, the verifier and the owner may reopen it.  For an
# IRREVERSIBLE effect a normal actor cannot write 'failed' at all (validate_effect_transition rule
# 5), so this reopen is the second line of defence rather than the only one.
EFFECT_REOPENABLE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({("failed", "unknown")})

# The default reversibility per kind when the adapter cannot tell.  Commands default to "unknown",
# NOT "reversible" — a shell one-liner may be `git push`.
DEFAULT_REVERSIBILITY_BY_KIND: dict[str, str] = {
    "directive": "unknown",
    "write": "reversible",
    "command": "unknown",
    "commit": "reversible",
    "external": "unknown",
}

# P3's EFFECT_KINDS is a superset of the projection vocabulary used by the task-state fold, which
# knows only "directive" | "write" | "external".  This map is the whole vocabulary bridge.
_KIND_TO_PROJECTION: dict[str, str] = {
    "directive": "directive",
    "write": "write",
    "commit": "write",
    "command": "external",
    "external": "external",
}


class EffectTransitionRejected(ValueError):
    reason: str  # "unknown_state"|"terminal"|"invalid_transition"|"system_only"|"irreversible_actor"
    current_state: str
    target_state: str

    def __init__(self, reason: str, current_state: str, target_state: str) -> None:
        super().__init__(f"effect transition {current_state!r} -> {target_state!r} rejected: {reason}")
        self.reason = reason
        self.current_state = current_state
        self.target_state = target_state


@dataclass(frozen=True, slots=True)
class EffectIntent:
    kind: Literal["directive", "write", "command", "commit", "external"]
    capability: str
    resource: str
    argv: tuple[str, ...]
    reversibility: Literal["reversible", "irreversible", "unknown"]
    description: str = ""


@dataclass(frozen=True, slots=True)
class EffectRecord:
    effect_id: str
    directive_id: str
    seq: int
    state: Literal["prepared", "running", "confirmed", "failed", "unknown"]
    intent: EffectIntent
    intent_digest: str
    enforcement_tier: Literal["os_sandbox", "container", "advisory"]
    action_tracing: Literal["observed", "unavailable"]
    lease_generation: int
    claimed_executor: str
    provider_run_id: str | None
    provider_turn_id: str | None
    opened_at: datetime
    resolved_at: datetime | None = None
    resolution_source: str | None = None
    evidence: Mapping[str, Any] | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def effect_intent_digest(intent: EffectIntent) -> str:
    """The reconciliation handle.  ``UNIQUE (directive_id, intent_digest)`` makes "exactly one row
    for that intent" a database guarantee rather than an application convention."""
    return _sha256_hex(
        _canonical_json(
            {
                "kind": intent.kind,
                "capability": intent.capability,
                "resource": intent.resource,
                "argv": list(intent.argv),
                "reversibility": intent.reversibility,
            }
        )
    )


def validate_effect_transition(
    *,
    current_state: str,
    target_state: str,
    actor: str,
    reversibility: str,
) -> None:
    """Raise ``EffectTransitionRejected`` when the move is not permitted.

    Rejection order and exact reason strings:

    1. ``"unknown_state"`` — either state is not in ``EFFECT_STATES``
    2. ``"terminal"`` — ``current_state`` is terminal and the pair is not reopenable
    3. ``"invalid_transition"`` — the pair is in neither transition set
    4. ``"system_only"`` — leaving ``'unknown'``, or any reopenable pair, with a non-system actor
    5. ``"irreversible_actor"`` — resolving an irreversible effect to EITHER terminal state
       (``confirmed`` or ``failed``) with a non-system actor

    Rule 5 is what makes ``reversibility`` load-bearing rather than decorative: an ordinary worker
    may not be the sole witness that an irreversible change succeeded — nor that it did not.
    ``failed`` is covered for the same reason ``confirmed`` is, and it is the more dangerous of the
    two: both are terminal, and ``failed`` is reopened only by a system actor, so an agent allowed
    to declare its own irreversible effect ``failed`` could bury an unresolved change one way and
    walk out of ``unknown`` without any system witness.  A non-system actor that reaches for either
    terminal state gets ``EffectTransitionRejected("irreversible_actor", ...)`` and the row does not
    move; the move that stays open to it is ``-> 'unknown'``, which is what pauses autonomy and
    hands the effect to the reconciler, the verifier or the owner.
    """
    if current_state not in EFFECT_STATES or target_state not in EFFECT_STATES:
        raise EffectTransitionRejected("unknown_state", current_state, target_state)
    pair = (current_state, target_state)
    if current_state in EFFECT_TERMINAL_STATES and pair not in EFFECT_REOPENABLE_TRANSITIONS:
        raise EffectTransitionRejected("terminal", current_state, target_state)
    if pair not in (EFFECT_TRANSITIONS | EFFECT_REOPENABLE_TRANSITIONS):
        raise EffectTransitionRejected("invalid_transition", current_state, target_state)
    system_actor = str(actor) in _SYSTEM_ONLY_SOURCES
    if (current_state == "unknown" or pair in EFFECT_REOPENABLE_TRANSITIONS) and not system_actor:
        raise EffectTransitionRejected("system_only", current_state, target_state)
    if str(reversibility) == "irreversible" and target_state in EFFECT_TERMINAL_STATES and not system_actor:
        raise EffectTransitionRejected("irreversible_actor", current_state, target_state)


def is_effect_terminal(state: str) -> bool:
    return str(state) in EFFECT_TERMINAL_STATES


def pause_required(records: Sequence[EffectRecord]) -> tuple[bool, str]:
    """``(pause, reason)`` for a directive's effect set.

    An ``unknown`` effect that is demonstrably ``reversible`` does **not** pause — it is safe to
    retry.  That distinction is what keeps the pause from bricking every session.  Note the
    interaction with ``DEFAULT_REVERSIBILITY_BY_KIND``: commands default to ``unknown``
    reversibility, so a crashed shell effect **does** pause.  That is deliberate; the alternative
    default silently retries ``git push``.
    """
    for record in records:
        if record.state == "unknown" and record.intent.reversibility in ("irreversible", "unknown"):
            return (True, "unresolved_irreversible_effect")
    for record in records:
        if record.state in ("prepared", "running"):
            return (True, "effect_still_open")
    return (False, "")


def unresolved_effects_json(records: Sequence[EffectRecord]) -> list[dict[str, Any]]:
    """Emit the task-state projection's ``UnresolvedEffect`` shape — keys ``effect_id``, ``kind``,
    ``description``, ``opened_at``, ``directive_id``, ``paths`` — so the projection's own
    ``effects_from_json`` consumes it untranslated.  ``kind`` is always a projection member: this is
    the only bridge between the two vocabularies.

    ``state`` is emitted as a SEVENTH key, outside that shape.  ``effects_from_json`` reads only the
    six it names and ignores the rest, so the projection is unaffected; the key exists because the
    MCP firewall's unknown-effect pause (``tools._unknown_effect_ids``, §9.3/G15) keys off it and
    treats a row without a state as NOT unknown.  Without it, any wire field populated from this
    function silently disables that pause."""
    out: list[dict[str, Any]] = []
    for record in records:
        if record.state in EFFECT_TERMINAL_STATES:
            continue
        out.append(
            {
                "effect_id": record.effect_id,
                "kind": _KIND_TO_PROJECTION.get(record.intent.kind, "external"),
                "description": record.intent.description or record.intent.resource,
                "opened_at": record.opened_at.isoformat(),
                "directive_id": record.directive_id,
                "paths": [record.intent.resource] if record.intent.resource else [],
                "state": record.state,
            }
        )
    return out


def effect_intent_to_json(intent: EffectIntent) -> dict[str, Any]:
    return {
        "kind": intent.kind,
        "capability": intent.capability,
        "resource": intent.resource,
        "argv": list(intent.argv),
        "reversibility": intent.reversibility,
        "description": intent.description,
    }


def effect_intent_from_json(value: Mapping[str, Any]) -> EffectIntent:
    return EffectIntent(
        kind=_kind_literal(value.get("kind")),
        capability=str(value.get("capability") or ""),
        resource=str(value.get("resource") or ""),
        argv=tuple(str(item) for item in (value.get("argv") or ())),
        reversibility=_reversibility_literal(value.get("reversibility")),
        description=str(value.get("description") or ""),
    )


def effect_record_to_json(record: EffectRecord) -> dict[str, Any]:
    return {
        "effect_id": record.effect_id,
        "directive_id": record.directive_id,
        "seq": record.seq,
        "state": record.state,
        "intent": effect_intent_to_json(record.intent),
        "intent_digest": record.intent_digest,
        "enforcement_tier": record.enforcement_tier,
        "action_tracing": record.action_tracing,
        "lease_generation": record.lease_generation,
        "claimed_executor": record.claimed_executor,
        "provider_run_id": record.provider_run_id,
        "provider_turn_id": record.provider_turn_id,
        "opened_at": record.opened_at.isoformat(),
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at is not None else None,
        "resolution_source": record.resolution_source,
        "evidence": dict(record.evidence) if record.evidence is not None else None,
        "schema_version": EFFECT_SCHEMA_VERSION,
    }


def effect_record_from_json(value: Mapping[str, Any]) -> EffectRecord:
    """Rebuild a record, rejecting the one combination that would be a lie.

    ``enforcement_tier == "advisory"`` means no OS boundary was applied, so per-command action
    tracing cannot have been observed.  ``open_effect`` performs the same check BEFORE the INSERT,
    so the store path cannot write the forbidden combination either.
    """
    tier = _tier_literal(value.get("enforcement_tier"))
    tracing = _tracing_literal(value.get("action_tracing"))
    if tier == "advisory" and tracing != "unavailable":
        raise ValueError("enforcement_tier='advisory' cannot carry action_tracing='observed': no boundary observed the action")
    intent_value = value.get("intent")
    intent = effect_intent_from_json(intent_value if isinstance(intent_value, Mapping) else {})
    evidence_value = value.get("evidence")
    return EffectRecord(
        effect_id=str(value.get("effect_id") or ""),
        directive_id=str(value.get("directive_id") or ""),
        seq=int(value.get("seq") or 0),
        state=_state_literal(value.get("state")),
        intent=intent,
        intent_digest=str(value.get("intent_digest") or effect_intent_digest(intent)),
        enforcement_tier=tier,
        action_tracing=tracing,
        lease_generation=int(value.get("lease_generation") or 0),
        claimed_executor=str(value.get("claimed_executor") or ""),
        provider_run_id=(str(value["provider_run_id"]) if value.get("provider_run_id") else None),
        provider_turn_id=(str(value["provider_turn_id"]) if value.get("provider_turn_id") else None),
        opened_at=_parse_dt(value.get("opened_at")),
        resolved_at=_parse_dt_opt(value.get("resolved_at")),
        resolution_source=(str(value["resolution_source"]) if value.get("resolution_source") else None),
        evidence=(dict(evidence_value) if isinstance(evidence_value, Mapping) else None),
    )


# ---- private coercion helpers ------------------------------------------------------------------
def _state_literal(value: Any) -> Literal["prepared", "running", "confirmed", "failed", "unknown"]:
    raw = str(value or "prepared")
    if raw == "running":
        return "running"
    if raw == "confirmed":
        return "confirmed"
    if raw == "failed":
        return "failed"
    if raw == "unknown":
        return "unknown"
    if raw == "prepared":
        return "prepared"
    raise ValueError(f"unknown effect state {raw!r}")


def _kind_literal(value: Any) -> Literal["directive", "write", "command", "commit", "external"]:
    raw = str(value or "external")
    if raw == "directive":
        return "directive"
    if raw == "write":
        return "write"
    if raw == "command":
        return "command"
    if raw == "commit":
        return "commit"
    if raw == "external":
        return "external"
    raise ValueError(f"unknown effect kind {raw!r}")


def _reversibility_literal(value: Any) -> Literal["reversible", "irreversible", "unknown"]:
    raw = str(value or "unknown")
    if raw == "reversible":
        return "reversible"
    if raw == "irreversible":
        return "irreversible"
    return "unknown"


def _tier_literal(value: Any) -> Literal["os_sandbox", "container", "advisory"]:
    raw = str(value or "advisory")
    if raw == "os_sandbox":
        return "os_sandbox"
    if raw == "container":
        return "container"
    return "advisory"


def _tracing_literal(value: Any) -> Literal["observed", "unavailable"]:
    return "observed" if str(value or "unavailable") == "observed" else "unavailable"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _parse_dt_opt(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_dt(value)
