from __future__ import annotations

from tce_api.graph import llm_extract_facts


class _FakeGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        return {
            "facts": [
                {"subject": "database", "predicate": "version", "object": "PostgreSQL 16", "confidence": 0.85},
                {"subject": "deploy_target", "predicate": "environment", "object": "production", "confidence": 0.9},
            ]
        }


class _FailingGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        raise RuntimeError("LLM down")


def test_llm_extract_facts_returns_tuples() -> None:
    results = llm_extract_facts("We deployed PostgreSQL 16 to production", _FakeGateway())
    assert len(results) == 2
    assert results[0] == ("database__version", "PostgreSQL 16", 0.85)
    assert results[1] == ("deploy_target__environment", "production", 0.9)


def test_llm_extract_facts_returns_empty_on_failure() -> None:
    results = llm_extract_facts("text", _FailingGateway())
    assert results == []
