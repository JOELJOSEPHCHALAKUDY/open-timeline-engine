from __future__ import annotations

from tce_shared.fingerprint import (
    DEFAULT_FINGERPRINT,
    merge_observation_into_fingerprint,
    extract_communication_signals,
)


def test_default_fingerprint_has_all_domains():
    fp = DEFAULT_FINGERPRINT.copy()
    assert "decision_making" in fp
    assert "communication" in fp
    assert "priorities" in fp
    assert "context_switching" in fp
    assert "learning_style" in fp
    assert "emotional_patterns" in fp


def test_merge_updates_observation_count():
    fp = DEFAULT_FINGERPRINT.copy()
    observation = {
        "situation_type": "error_occurred",
        "user_response": "Let me investigate the root cause first",
        "outcome_sentiment": "positive",
    }
    updated = merge_observation_into_fingerprint(fp, observation)
    assert updated is not fp  # returns new dict


def test_extract_communication_signals_short():
    signals = extract_communication_signals("ok fix it")
    assert signals["verbosity"] == "terse"


def test_extract_communication_signals_long():
    signals = extract_communication_signals(
        "I think we should investigate the root cause thoroughly. "
        "Let me explain my reasoning in detail. First, the error pattern "
        "suggests a concurrency issue. Second, we saw similar behavior last week."
    )
    assert signals["verbosity"] in ("moderate", "verbose")
