"""Adversarial coverage for the high-risk safety confirmation gate.

The gate must not release a pending high-risk action on a negated or
incidental "confirm", and a denial must win over a co-occurring confirm.
"""

from tce_shared.events import SafetyDecision, TakeoverPolicy
from tce_shared.takeover import evaluate_safety

POLICY = TakeoverPolicy(
    safety_policy="high-risk-pause",
    confirm_keyword="confirm",
    deny_keyword="abort",
    timeout_minutes=30,
    auto_handoff_on_question=True,
)
PENDING = {"pending_safety": {"reason": "destructive_filesystem", "pending_response": "rm -rf /tmp"}}


def _decide(message: str) -> SafetyDecision:
    decision, _ = evaluate_safety(POLICY, message, "rm -rf /tmp", PENDING)
    return decision


def test_bare_confirm_allows() -> None:
    assert _decide("confirm") == SafetyDecision.ALLOW


def test_affirmative_confirm_allows() -> None:
    assert _decide("yes, confirm") == SafetyDecision.ALLOW


def test_bare_abort_blocks() -> None:
    assert _decide("abort") == SafetyDecision.BLOCKED


def test_negated_confirm_does_not_allow() -> None:
    assert _decide("don't confirm yet, we need to check") == SafetyDecision.CONFIRM_REQUIRED


def test_cant_confirm_does_not_allow() -> None:
    assert _decide("I can't confirm this") == SafetyDecision.CONFIRM_REQUIRED


def test_no_confirm_does_not_allow() -> None:
    assert _decide("no confirm") == SafetyDecision.CONFIRM_REQUIRED


def test_deny_wins_when_both_present() -> None:
    # "confirm? no, abort" contains both keywords; the denial must win.
    assert _decide("confirm? no, abort") == SafetyDecision.BLOCKED


def test_confirm_substring_does_not_leak() -> None:
    # "confirmation" contains "confirm" as a substring but is not an
    # affirmative confirmation of the pending action.
    assert _decide("still awaiting confirmation from ops") == SafetyDecision.CONFIRM_REQUIRED


def test_unrelated_message_stays_pending() -> None:
    assert _decide("what does this do?") == SafetyDecision.CONFIRM_REQUIRED
