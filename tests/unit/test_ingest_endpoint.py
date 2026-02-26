from __future__ import annotations

from tce_api.schemas import IngestObservationsRequest, IngestObservationsResponse


def test_ingest_request_defaults():
    req = IngestObservationsRequest(observations=[{"situation_type": "error_occurred", "situation_summary": "test", "user_response": "fixed it"}])
    assert req.update_fingerprint is True
    assert len(req.observations) == 1


def test_ingest_response_structure():
    resp = IngestObservationsResponse(ingested=3, fingerprint_updated=True)
    assert resp.ingested == 3
    assert resp.fingerprint_updated is True
