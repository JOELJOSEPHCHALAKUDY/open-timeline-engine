"""The execution-permit gate must be a property of what the OWNER asked, never of the reply.

``_is_mutating_intent`` decides ``execution_permit_required`` -- whether this turn has to stop and
call ``tce.request_execution_permit`` before it is allowed to write anything.  It used to take a
third input, ``final_response``: the system's own reply for this turn.  Two things were wrong with
that, and only the second one is obvious.

The obvious one: a gate that reads the system's own words can be talked past by changing the
wording.  Reword the reply and the permit requirement moves, which makes the owner's permission a
function of the system's prose rather than of the owner's request.

The subtle one: it was never adding information.  The reply the gate was reading was an echo --
``"[TAKEOVER ...] Your task: <the message>"`` -- so on every turn where the reply carried a
mutating verb, the message already did.  Argument-spying a 12-turn run on both backends and both
trees found ``result == result_without_final_response`` on all 12 turns, which is why dropping the
input changed no turn's behaviour.  It was a channel with no signal on it and a way past the gate
on it, which is the worst of both.

Three properties:

1. **Structural, on the function** -- there is no parameter that could carry the reply, so the term
   cannot come back without changing a signature this test reads.
2. **Structural, on the call** -- the callsite in each backend passes the message and the objective
   and nothing else.  A signature check alone would still pass if someone concatenated the reply
   into ``message`` at the call, so this reads the actual argument expressions.
3. **Behavioural, and discriminating** -- the verb matrix.  A test that only asserted "the gate
   still fires" would pass against ``return True``, which is not a gate either.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from tce_api.main import _is_mutating_intent as full_gate
from tce_lite_api.store import _is_mutating_intent as lite_gate

BACKENDS = {"full": full_gate, "lite": lite_gate}

# Anything naming an output of this turn.  `final_response` is the input that was removed;
# `executor_output` is the other self-authored text the request carries, and it must not become
# the replacement channel.
SELF_AUTHORED = ("final_response", "executor_output", "persona_ack", "advisor", "clone_advice")


@pytest.mark.parametrize("name", sorted(BACKENDS))
def test_the_gate_has_nowhere_to_read_its_own_reply_from(name: str) -> None:
    parameters = inspect.signature(BACKENDS[name]).parameters
    assert list(parameters) == ["message", "task"], (
        f"{name}: the permit gate grew an input. Whether an action needs the owner's permission "
        "is a property of what the owner asked and what the objective is -- never of how the "
        "system phrased its own reply."
    )
    assert not any(banned in key for key in parameters for banned in SELF_AUTHORED)


@pytest.mark.parametrize(
    ("module_path", "expected_arguments"),
    [
        ("services/tce_api/tce_api/main.py", ["body.message", "resolved_task"]),
        ("services/tce_lite_api/tce_lite_api/store.py", ["message", "resolved_task"]),
    ],
    ids=["full", "lite"],
)
def test_the_callsite_hands_the_gate_the_owners_words_and_nothing_else(
    module_path: str, expected_arguments: list[str]
) -> None:
    """The signature is only half of it: this reads what is actually passed."""

    source = (Path(__file__).resolve().parents[2] / module_path).read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_is_mutating_intent"
    ]
    assert len(calls) == 1, f"{module_path}: expected exactly one permit-gate call, found {len(calls)}"

    call = calls[0]
    assert not call.keywords
    arguments = [ast.get_source_segment(source, arg) for arg in call.args]
    assert arguments == expected_arguments, (
        f"{module_path}: the permit gate is being handed {arguments}. Only the owner's message and "
        "the resolved objective may decide whether a write needs the owner's permission."
    )
    for argument in arguments:
        assert argument is not None
        assert not any(banned in argument for banned in SELF_AUTHORED)


@pytest.mark.parametrize("name", sorted(BACKENDS))
@pytest.mark.parametrize(
    ("message", "task", "expected"),
    [
        # The owner asked for a write: the gate fires, from the message or from the objective.
        ("fix the retry handler", "stripe webhook reliability", True),
        ("ok continue", "implement the dead letter queue", True),
        ("delete the old retry table", None, True),
        # The verb ends the string.  This is the case the removed input was masking: the objective
        # is plainly mutating, the old matcher looked for "fix " with a trailing space and missed
        # it, and the gate fired only because the reply echoed the objective back with a space
        # after it.  Dropping the reply without fixing the matcher would have dropped the permit.
        ("beru take over", "ship the stripe webhook fix", True),
        ("ok", "webhook retries need a rewrite, then update", True),
        ("can you fix", None, True),
        # The owner asked for nothing that writes: the gate stays shut.  These are exactly the
        # turns the removed input used to flip, because the reply to them still said "update".
        ("ok continue", "stripe webhook reliability", False),
        ("keep going", "stripe webhook reliability", False),
        ("what about the alerting", None, False),
        # Discrimination: a mutating verb has to be a whole word, not a substring of one.  Without
        # this the matrix above would also pass against a gate that fires on everything, and
        # word-boundary matching is wider than the old "verb plus a space" and needs the floor.
        ("prefixed unchanged and undeleted", "webhook reliability", False),
        ("the updater rewrote nothing", "creative direction", False),
    ],
)
def test_the_verb_matrix_is_read_from_the_owner_only(
    name: str, message: str, task: str | None, expected: bool
) -> None:
    assert BACKENDS[name](message, task) is expected


def test_full_and_lite_answer_identically() -> None:
    """Parity: the two backends must not disagree about when the owner has to be asked."""

    cases = [
        ("beru take over", "stripe webhook reliability"),
        ("fix the retry handler", "stripe webhook reliability"),
        ("ok continue", "stripe webhook reliability"),
        ("should i force push this to prod", None),
        ("", None),
    ]
    assert [full_gate(m, t) for m, t in cases] == [lite_gate(m, t) for m, t in cases]
