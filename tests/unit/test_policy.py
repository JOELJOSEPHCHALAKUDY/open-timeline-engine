from datetime import datetime

from tce_api.policy import PolicyEngine


def test_blocks_sensitivity_3_by_default():
    engine = PolicyEngine()
    consumer = engine.resolve_consumer("bearer:user")
    decision = engine.evaluate(consumer, domain="coding", sensitivity=3, ts=datetime.now())
    assert decision.allow is False
