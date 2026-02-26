from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from tce_api.clone import advisor_reason
from tce_shared.fingerprint import DEFAULT_FINGERPRINT


def _mock_gateway(response: dict) -> MagicMock:
    gw = MagicMock()
    gw.aextract_structured = AsyncMock(return_value=response)
    gw.extract_structured.return_value = response
    return gw


def test_advisor_reason_returns_structured_response():
    gw = _mock_gateway({
        "reasoning": "User typically investigates root cause",
        "decision": "Investigate the failing test root cause before fixing",
        "confidence": 0.85,
        "communication_style": "moderate",
    })
    result = advisor_reason(
        gateway=gw,
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "fix tests", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Test suite failing on CI",
        situation_type="error_occurred",
    )
    assert result["decision"] == "Investigate the failing test root cause before fixing"
    assert result["confidence"] == 0.85
    gw.aextract_structured.assert_called_once()


def test_advisor_reason_fallback_on_error():
    gw = MagicMock()
    gw.aextract_structured = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    gw.extract_structured.side_effect = RuntimeError("LLM unavailable")
    result = advisor_reason(
        gateway=gw,
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "fix tests", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Test suite failing on CI",
        situation_type="error_occurred",
    )
    assert "decision" in result
    assert result.get("fallback") is True
