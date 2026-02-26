from __future__ import annotations

from tce_shared.events import TakeoverClassification
from tce_shared.takeover import classify_text

# --- Suggest mode (default, takeover_active=False) ---

def test_suggest_mode_detects_question() -> None:
    assert classify_text("Should I continue with deploy?") == TakeoverClassification.QUESTION


def test_suggest_mode_detects_handoff() -> None:
    assert classify_text("Need your input before proceeding.") == TakeoverClassification.HANDOFF
    assert classify_text("Cannot proceed without your approval.") == TakeoverClassification.HANDOFF
    assert classify_text("I need your signoff before we push this change.") == TakeoverClassification.HANDOFF


def test_suggest_mode_detects_decisive() -> None:
    assert classify_text("Applying retry policy now.") == TakeoverClassification.DECISIVE
    assert classify_text("No need for confirmation, continue automatically.") == TakeoverClassification.DECISIVE


# --- Takeover mode (takeover_active=True) biases toward DECISIVE ---

def test_takeover_mode_decisive_for_numbered_lists() -> None:
    text = "Next steps:\n1. Add retry\n2. Add logs"
    assert classify_text(text, takeover_active=True) == TakeoverClassification.DECISIVE


def test_takeover_mode_decisive_for_trailing_question() -> None:
    text = "I've applied the fix. Should I continue with deploy?"
    assert classify_text(text, takeover_active=True) == TakeoverClassification.DECISIVE


def test_takeover_mode_decisive_for_polite_phrasing() -> None:
    text = "I'll proceed if you want me to refactor the module."
    assert classify_text(text, takeover_active=True) == TakeoverClassification.DECISIVE


def test_takeover_mode_still_catches_hard_handoff() -> None:
    text = "User confirmation required before deleting production data."
    assert classify_text(text, takeover_active=True) == TakeoverClassification.HANDOFF


def test_takeover_mode_still_catches_hard_suggestion() -> None:
    text = "Which option do you prefer for the database schema?"
    assert classify_text(text, takeover_active=True) == TakeoverClassification.SUGGESTION


def test_empty_and_none() -> None:
    assert classify_text(None) == TakeoverClassification.EMPTY
    assert classify_text("") == TakeoverClassification.EMPTY
    assert classify_text("   ") == TakeoverClassification.EMPTY


def test_semantic_classifier_detects_paraphrased_handoff(monkeypatch) -> None:
    monkeypatch.setenv("TCE_SEMANTIC_CLASSIFIER_ENABLED", "true")
    monkeypatch.setenv("TCE_SEMANTIC_CLASSIFIER_INTENT_THRESHOLD", "0.58")
    text = "Waiting for your green light before touching production data."
    assert classify_text(text) == TakeoverClassification.HANDOFF


def test_semantic_classifier_respects_negated_handoff(monkeypatch) -> None:
    monkeypatch.setenv("TCE_SEMANTIC_CLASSIFIER_ENABLED", "true")
    monkeypatch.setenv("TCE_SEMANTIC_CLASSIFIER_INTENT_THRESHOLD", "0.58")
    text = "Do not wait for approval; continue automatically."
    assert classify_text(text) == TakeoverClassification.DECISIVE
