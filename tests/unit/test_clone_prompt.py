from __future__ import annotations

from tce_shared.fingerprint import DEFAULT_FINGERPRINT
from tce_api.clone_prompt import build_clone_prompt


def test_prompt_contains_all_layers():
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[
            {
                "situation_summary": "Build failed",
                "user_response": "Investigate root cause",
                "outcome": "Fixed in 10 min",
            }
        ],
        session_context={
            "objective": "refactor auth module",
            "turn_count": 3,
            "turns": [],
            "unresolved_threads": [],
        },
        current_situation="Test suite failing on CI",
        situation_type="error_occurred",
    )
    assert "Joel" in prompt
    assert "error_occurred" in prompt
    assert "Test suite failing on CI" in prompt
    assert "Build failed" in prompt
    assert "step by step" in prompt.lower()


def test_prompt_includes_anti_patterns():
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "test", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Deploy to staging",
        situation_type="routine_task",
    )
    assert "ANTI-PATTERN" in prompt or "Do NOT" in prompt


def test_prompt_empty_observations():
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "test", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Something new",
        situation_type="unknown_territory",
    )
    assert "Joel" in prompt
    assert len(prompt) > 100
