from datetime import datetime

from tce_api.policy import PolicyEngine


def test_policy_denies_over_sensitivity():
    engine = PolicyEngine()
    consumer = engine.resolve_consumer("bearer:user")
    decision = engine.evaluate(consumer, domain="research", sensitivity=3, ts=datetime.now())
    assert decision.allow is False
