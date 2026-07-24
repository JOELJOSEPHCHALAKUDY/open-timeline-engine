from __future__ import annotations

from unittest.mock import patch

from tce_mcp.tools import ingest_observations


def test_ingest_observations_calls_client():
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.ingest_observations.return_value = {"ingested": 2, "fingerprint_updated": True}
        result = ingest_observations(
            observations=[
                {"situation_type": "error_occurred", "situation_summary": "test", "user_response": "fixed it"},
                {"situation_type": "choice_required", "situation_summary": "pick db", "user_response": "postgres"},
            ],
            update_fingerprint=True,
        )
        assert result["kind"] == "ingest_observations"
        assert result["result"]["ingested"] == 2
        mock_client.ingest_observations.assert_called_once()
