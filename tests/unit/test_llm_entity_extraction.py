from __future__ import annotations

from tce_api.graph import llm_extract_entities


class _FakeGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        return {
            "entities": [
                {"name": "Joel", "type": "person", "confidence": 0.9},
                {"name": "PostgreSQL", "type": "technology", "confidence": 0.85},
                {"name": "Acme Corp", "type": "organization", "confidence": 0.7},
            ]
        }


class _FailingGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        raise RuntimeError("LLM unavailable")


def test_llm_extract_returns_entities() -> None:
    results = llm_extract_entities("Joel deployed PostgreSQL for Acme Corp", _FakeGateway())
    assert len(results) == 3
    assert results[0] == ("person", "joel", 0.9)
    assert results[1] == ("technology", "postgresql", 0.85)
    assert results[2] == ("organization", "acme corp", 0.7)


def test_llm_extract_returns_empty_on_failure() -> None:
    results = llm_extract_entities("some text", _FailingGateway())
    assert results == []


def test_llm_extract_clamps_confidence() -> None:
    class _HighConfGateway:
        def extract_structured(self, prompt, schema_name):
            return {"entities": [{"name": "X", "type": "concept", "confidence": 5.0}]}

    results = llm_extract_entities("text", _HighConfGateway())
    assert results[0][2] == 1.0
