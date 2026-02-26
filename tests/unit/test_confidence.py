from tce_worker.confidence import confidence_status, weighted_confidence


def test_confidence_thresholds():
    high = weighted_confidence(1.0, 1.0, 1.0, 1.0)
    low = weighted_confidence(0.1, 0.1, 0.1, 0.1)
    assert confidence_status(high) == "active"
    assert confidence_status(low) == "suppressed"
