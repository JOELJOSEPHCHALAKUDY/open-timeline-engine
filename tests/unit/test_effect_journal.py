"""The effect transition table, exhaustively, plus the pause rule.

Three properties here are the reason the module exists:

* ``unknown`` is **not** terminal.  An unresolved possibly-irreversible effect pauses autonomy; it
  never feeds the retry ladder.
* ``failed`` is terminal for an ordinary worker but reopenable by a system actor, so a stale worker
  cannot make ``unknown`` unreachable forever by racing to ``failed``.
* ``reversibility`` is read, not merely declared: confirming an irreversible effect requires a
  system actor.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest
from tce_shared.effect_journal import (
    ACTION_TRACING,
    DEFAULT_REVERSIBILITY_BY_KIND,
    EFFECT_ACTOR_OWNER,
    EFFECT_ACTOR_RECONCILER,
    EFFECT_ACTOR_VERIFIER,
    EFFECT_KINDS,
    EFFECT_REOPENABLE_TRANSITIONS,
    EFFECT_STATES,
    EFFECT_TERMINAL_STATES,
    EFFECT_TRANSITIONS,
    REVERSIBILITY,
    EffectIntent,
    EffectRecord,
    EffectTransitionRejected,
    effect_intent_digest,
    effect_record_from_json,
    effect_record_to_json,
    is_effect_terminal,
    pause_required,
    unresolved_effects_json,
    validate_effect_transition,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
SYSTEM_ACTORS = (EFFECT_ACTOR_RECONCILER, EFFECT_ACTOR_VERIFIER, EFFECT_ACTOR_OWNER)


def _intent(**overrides: object) -> EffectIntent:
    values: dict[str, object] = {
        "kind": "write",
        "capability": "filesystem.write",
        "resource": "services/tce_api/tce_api/main.py",
        "argv": (),
        "reversibility": "reversible",
        "description": "edit a file",
    }
    values.update(overrides)
    return EffectIntent(**values)  # type: ignore[arg-type]


def _record(**overrides: object) -> EffectRecord:
    intent = overrides.pop("intent", _intent())
    values: dict[str, object] = {
        "effect_id": "eff-1",
        "directive_id": "dir-1",
        "seq": 1,
        "state": "running",
        "intent": intent,
        "intent_digest": effect_intent_digest(intent),  # type: ignore[arg-type]
        "enforcement_tier": "os_sandbox",
        "action_tracing": "observed",
        "lease_generation": 1,
        "claimed_executor": "supervisor",
        "provider_run_id": "run-1",
        "provider_turn_id": None,
        "opened_at": NOW,
    }
    values.update(overrides)
    return EffectRecord(**values)  # type: ignore[arg-type]


# --- the table, pinned literally -----------------------------------------------------------------


def test_transitions_are_exactly_the_eight() -> None:
    assert EFFECT_TRANSITIONS == frozenset(
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
    assert EFFECT_REOPENABLE_TRANSITIONS == frozenset({("failed", "unknown")})
    assert EFFECT_STATES == ("prepared", "running", "confirmed", "failed", "unknown")
    assert EFFECT_TERMINAL_STATES == frozenset({"confirmed", "failed"})


def test_every_state_pair_is_decided_exactly_one_way() -> None:
    """Exhaustive over the 25 ordered pairs: each is accepted only when it is in one of the two
    declared sets, and rejected with a named reason otherwise."""
    allowed = EFFECT_TRANSITIONS | EFFECT_REOPENABLE_TRANSITIONS
    for current, target in itertools.product(EFFECT_STATES, repeat=2):
        try:
            validate_effect_transition(
                current_state=current, target_state=target, actor=EFFECT_ACTOR_RECONCILER, reversibility="reversible"
            )
        except EffectTransitionRejected as rejection:
            assert (current, target) not in allowed, (current, target)
            assert rejection.reason in {"terminal", "invalid_transition"}
            assert rejection.current_state == current
            assert rejection.target_state == target
        else:
            assert (current, target) in allowed, (current, target)


def test_unknown_state_names_are_rejected_first() -> None:
    with pytest.raises(EffectTransitionRejected) as excinfo:
        validate_effect_transition(current_state="banana", target_state="running", actor="x", reversibility="reversible")
    assert excinfo.value.reason == "unknown_state"


def test_confirmed_is_terminal_for_everyone() -> None:
    for actor in (*SYSTEM_ACTORS, "executor:codex"):
        with pytest.raises(EffectTransitionRejected) as excinfo:
            validate_effect_transition(current_state="confirmed", target_state="unknown", actor=actor, reversibility="reversible")
        assert excinfo.value.reason in {"terminal", "invalid_transition"}


def test_leaving_unknown_is_system_only() -> None:
    for target in ("confirmed", "failed"):
        with pytest.raises(EffectTransitionRejected) as excinfo:
            validate_effect_transition(
                current_state="unknown", target_state=target, actor="executor:codex", reversibility="reversible"
            )
        assert excinfo.value.reason == "system_only"
        for actor in SYSTEM_ACTORS:
            validate_effect_transition(current_state="unknown", target_state=target, actor=actor, reversibility="reversible")


def test_failed_may_be_reopened_only_by_a_system_actor() -> None:
    """A stale worker racing to ``failed`` must not lock the reconciler out of recording ``unknown``."""
    with pytest.raises(EffectTransitionRejected) as excinfo:
        validate_effect_transition(current_state="failed", target_state="unknown", actor="executor:codex", reversibility="unknown")
    assert excinfo.value.reason == "system_only"
    for actor in SYSTEM_ACTORS:
        validate_effect_transition(current_state="failed", target_state="unknown", actor=actor, reversibility="unknown")


def test_irreversible_confirm_requires_a_system_actor() -> None:
    """The assertion that makes ``reversibility`` load-bearing rather than decorative."""
    with pytest.raises(EffectTransitionRejected) as excinfo:
        validate_effect_transition(
            current_state="running", target_state="confirmed", actor="executor:codex", reversibility="irreversible"
        )
    assert excinfo.value.reason == "irreversible_actor"
    # Reversible is fine for an ordinary worker, and a system actor may confirm either.
    validate_effect_transition(current_state="running", target_state="confirmed", actor="executor:codex", reversibility="reversible")
    validate_effect_transition(
        current_state="running", target_state="confirmed", actor=EFFECT_ACTOR_RECONCILER, reversibility="irreversible"
    )


def test_irreversible_failure_requires_a_system_actor() -> None:
    """``failed`` is terminal and never auto-reopened for the actor who wrote it, so declaring an
    irreversible effect ``failed`` would be a one-way exit from ``unknown`` — the agent burying the
    thing the pause exists to surface.  A non-system actor gets ``irreversible_actor`` and must
    record ``unknown`` instead, which is what actually pauses autonomy."""
    for current in ("prepared", "running"):
        with pytest.raises(EffectTransitionRejected) as excinfo:
            validate_effect_transition(
                current_state=current, target_state="failed", actor="executor:codex", reversibility="irreversible"
            )
        assert excinfo.value.reason == "irreversible_actor"
        # The honest move that remains open to that actor.
        validate_effect_transition(
            current_state=current, target_state="unknown", actor="executor:codex", reversibility="irreversible"
        )
        for actor in SYSTEM_ACTORS:
            validate_effect_transition(current_state=current, target_state="failed", actor=actor, reversibility="irreversible")
    # A reversible effect may still be failed by an ordinary worker.
    validate_effect_transition(current_state="running", target_state="failed", actor="executor:codex", reversibility="reversible")


def test_unknown_to_failed_is_refused_for_a_worker_on_both_axes() -> None:
    """Leaving ``unknown`` is system-only regardless of reversibility; the irreversible rule is the
    second lock, not the first."""
    for reversibility in ("reversible", "unknown", "irreversible"):
        with pytest.raises(EffectTransitionRejected) as excinfo:
            validate_effect_transition(
                current_state="unknown", target_state="failed", actor="executor:codex", reversibility=reversibility
            )
        assert excinfo.value.reason in {"system_only", "irreversible_actor"}


def test_unknown_is_not_terminal_and_pauses() -> None:
    assert is_effect_terminal("unknown") is False
    assert is_effect_terminal("confirmed") is True
    assert is_effect_terminal("failed") is True
    assert is_effect_terminal("running") is False

    irreversible = _record(state="unknown", intent=_intent(kind="commit", reversibility="irreversible"))
    assert pause_required([irreversible]) == (True, "unresolved_irreversible_effect")


def test_an_unknown_but_reversible_effect_does_not_pause() -> None:
    """The distinction that keeps the pause from bricking every session."""
    reversible = _record(state="unknown", intent=_intent(reversibility="reversible"))
    assert pause_required([reversible]) == (False, "")


def test_an_open_effect_pauses_with_a_different_reason() -> None:
    assert pause_required([_record(state="running")]) == (True, "effect_still_open")
    assert pause_required([_record(state="prepared")]) == (True, "effect_still_open")
    assert pause_required([_record(state="confirmed")]) == (False, "")
    assert pause_required([]) == (False, "")


def test_an_unknown_irreversible_effect_outranks_a_merely_open_one() -> None:
    records = [
        _record(effect_id="a", seq=1, state="running"),
        _record(effect_id="b", seq=2, state="unknown", intent=_intent(kind="command", reversibility="unknown")),
    ]
    assert pause_required(records) == (True, "unresolved_irreversible_effect")


def test_command_defaults_to_unknown_reversibility() -> None:
    """A shell one-liner may be ``git push``; defaulting it to reversible silently retries that."""
    assert DEFAULT_REVERSIBILITY_BY_KIND["command"] == "unknown"
    assert DEFAULT_REVERSIBILITY_BY_KIND["directive"] == "unknown"
    assert DEFAULT_REVERSIBILITY_BY_KIND["external"] == "unknown"
    assert DEFAULT_REVERSIBILITY_BY_KIND["write"] == "reversible"
    assert DEFAULT_REVERSIBILITY_BY_KIND["commit"] == "reversible"
    assert set(DEFAULT_REVERSIBILITY_BY_KIND) == set(EFFECT_KINDS)
    assert set(DEFAULT_REVERSIBILITY_BY_KIND.values()) <= set(REVERSIBILITY)


# --- G4: the tier is mandatory and travels with the evidence -------------------------------------


def test_enforcement_tier_is_mandatory() -> None:
    intent = _intent()
    with pytest.raises(TypeError):
        EffectRecord(  # type: ignore[call-arg]
            effect_id="e",
            directive_id="d",
            seq=1,
            state="running",
            intent=intent,
            intent_digest="x",
            action_tracing="observed",
            lease_generation=0,
            claimed_executor="",
            provider_run_id=None,
            provider_turn_id=None,
            opened_at=NOW,
        )


def test_action_tracing_is_mandatory() -> None:
    intent = _intent()
    with pytest.raises(TypeError):
        EffectRecord(  # type: ignore[call-arg]
            effect_id="e",
            directive_id="d",
            seq=1,
            state="running",
            intent=intent,
            intent_digest="x",
            enforcement_tier="advisory",
            lease_generation=0,
            claimed_executor="",
            provider_run_id=None,
            provider_turn_id=None,
            opened_at=NOW,
        )


def test_advisory_tier_cannot_claim_observed_tracing() -> None:
    """No boundary observed the action, so the row may not say one did."""
    payload = effect_record_to_json(_record(enforcement_tier="advisory", action_tracing="observed"))
    with pytest.raises(ValueError, match="advisory"):
        effect_record_from_json(payload)
    payload["action_tracing"] = "unavailable"
    assert effect_record_from_json(payload).action_tracing == "unavailable"
    assert set(ACTION_TRACING) == {"observed", "unavailable"}


# --- the vocabulary bridge -----------------------------------------------------------------------


def test_unresolved_effects_json_emits_only_projection_kinds() -> None:
    projection_kinds = {"directive", "write", "external"}
    records = [
        _record(effect_id=f"e{index}", seq=index, state="running", intent=_intent(kind=kind))
        for index, kind in enumerate(EFFECT_KINDS)
    ]
    emitted = unresolved_effects_json(records)
    assert len(emitted) == len(EFFECT_KINDS)
    assert {entry["kind"] for entry in emitted} <= projection_kinds
    # Six projection keys plus `state`. `state` is NOT part of the projection shape -- it is the key
    # the MCP firewall's unknown-effect pause reads (tools._unknown_effect_ids), which treats a row
    # without one as not-unknown; a wire field populated from here without it silently disables the
    # pause. `effects_from_json` reads only the six and ignores `state`, so the projection is
    # unaffected -- asserted by test_state_key_is_ignored_by_the_projection below.
    assert set(emitted[0]) == {"effect_id", "kind", "description", "opened_at", "directive_id", "paths", "state"}
    assert {entry["state"] for entry in emitted} == {"running"}
    by_id = {entry["effect_id"]: entry["kind"] for entry in emitted}
    assert by_id["e2"] == "external"  # "command" folds to "external"
    assert by_id["e3"] == "write"  # "commit" folds to "write"


def test_state_key_is_ignored_by_the_projection() -> None:
    """The seventh key must not leak into the task-state projection, which is a closed dataclass."""
    from tce_shared.task_state import effects_from_json

    emitted = unresolved_effects_json([_record(effect_id="b", seq=2, state="unknown")])
    assert emitted[0]["state"] == "unknown"
    projected = effects_from_json(emitted)
    assert len(projected) == 1
    assert projected[0].effect_id == "b"
    assert not hasattr(projected[0], "state")


def test_unresolved_effects_json_omits_terminal_rows() -> None:
    records = [_record(effect_id="a", seq=1, state="confirmed"), _record(effect_id="b", seq=2, state="unknown")]
    assert [entry["effect_id"] for entry in unresolved_effects_json(records)] == ["b"]


# --- digests and codecs ---------------------------------------------------------------------------


def test_intent_digest_is_argv_order_sensitive() -> None:
    first = _intent(kind="command", argv=("git", "push"))
    second = _intent(kind="command", argv=("push", "git"))
    assert effect_intent_digest(first) != effect_intent_digest(second)
    assert effect_intent_digest(first) == effect_intent_digest(_intent(kind="command", argv=("git", "push")))


def test_intent_digest_ignores_the_description() -> None:
    assert effect_intent_digest(_intent(description="a")) == effect_intent_digest(_intent(description="b"))


def test_json_codecs_round_trip() -> None:
    rng = random.Random(909)
    for index in range(200):
        kind = rng.choice(EFFECT_KINDS)
        state = rng.choice(EFFECT_STATES)
        tier = rng.choice(["os_sandbox", "container"])
        intent = _intent(
            kind=kind,
            capability=rng.choice(["filesystem.write", "git.write", "process.execute"]),
            resource=f"services/x{index}.py",
            argv=tuple(rng.choice(["git", "push", "-f", "pytest"]) for _ in range(rng.randint(0, 3))),
            reversibility=rng.choice(REVERSIBILITY),
            description=f"effect {index}",
        )
        record = _record(
            effect_id=f"eff-{index}",
            seq=index,
            state=state,
            intent=intent,
            enforcement_tier=tier,
            action_tracing=rng.choice(ACTION_TRACING),
            resolved_at=(NOW + timedelta(minutes=index) if state in EFFECT_TERMINAL_STATES else None),
            resolution_source=("reaper" if state in EFFECT_TERMINAL_STATES else None),
            evidence=({"exit_code": index} if index % 3 == 0 else None),
        )
        assert effect_record_from_json(effect_record_to_json(record)) == record
