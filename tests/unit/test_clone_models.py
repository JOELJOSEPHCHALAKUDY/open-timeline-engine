from __future__ import annotations

from tce_api.models import BehavioralFingerprint, CloneFeedback, DecisionObservation


def test_decision_observation_defaults():
    obs = DecisionObservation.__table__
    assert obs.name == "decision_observations"
    assert "situation_type" in {c.name for c in obs.columns}
    assert "user_response" in {c.name for c in obs.columns}
    assert "context_snapshot" in {c.name for c in obs.columns}


def test_behavioral_fingerprint_defaults():
    fp = BehavioralFingerprint.__table__
    assert fp.name == "behavioral_fingerprints"
    assert "fingerprint" in {c.name for c in fp.columns}
    assert "consumer_id" in {c.name for c in fp.columns}


def test_clone_feedback_defaults():
    fb = CloneFeedback.__table__
    assert fb.name == "clone_feedback"
    assert "feedback_type" in {c.name for c in fb.columns}
