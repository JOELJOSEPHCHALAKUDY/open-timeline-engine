from __future__ import annotations

from tce_shared.situation import SITUATION_TYPES, classify_situation


def test_classify_error():
    result = classify_situation("Build failed with exit code 1. TypeError in module X.")
    assert result == "error_occurred"


def test_classify_choice():
    result = classify_situation("Should we use Redis or Memcached for caching?")
    assert result == "choice_required"


def test_classify_approval():
    result = classify_situation("PR #42 needs your review and approval before merge.")
    assert result == "approval_requested"


def test_classify_blocker():
    result = classify_situation("Blocked: dependency X is not available in our registry.")
    assert result == "blocker_encountered"


def test_classify_blocker_semantic_phrase():
    result = classify_situation("We can't move forward until access is granted.")
    assert result == "blocker_encountered"


def test_classify_routine():
    result = classify_situation("Deployed v2.3.1 to staging successfully.")
    assert result == "routine_task"


def test_classify_unknown_returns_valid_type():
    result = classify_situation("The sky is blue today.")
    assert result in SITUATION_TYPES


def test_all_types_present():
    assert len(SITUATION_TYPES) == 12


def test_semantic_classifier_detects_paraphrased_blocker(monkeypatch):
    monkeypatch.setenv("TCE_SEMANTIC_CLASSIFIER_ENABLED", "true")
    monkeypatch.setenv("TCE_SEMANTIC_CLASSIFIER_SITUATION_THRESHOLD", "0.56")
    message = "Progress is at a standstill until credentials arrive from upstream."
    result = classify_situation(message)
    assert result == "blocker_encountered"


def test_without_semantic_paraphrased_blocker_falls_back(monkeypatch):
    monkeypatch.delenv("TCE_SEMANTIC_CLASSIFIER_ENABLED", raising=False)
    message = "Progress is at a standstill until credentials arrive from upstream."
    result = classify_situation(message)
    assert result == "routine_task"
