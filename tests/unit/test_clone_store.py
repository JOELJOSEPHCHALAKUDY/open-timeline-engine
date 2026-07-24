from __future__ import annotations

from tce_api.clone_store import (
    build_session_context_from_state,
)


def test_build_session_context_empty():
    ctx = build_session_context_from_state({})
    assert "objective" in ctx
    assert "turn_count" in ctx
    assert "turns" in ctx
    assert "unresolved_threads" in ctx


def test_build_session_context_with_data():
    ctx = build_session_context_from_state({
        "objective": "fix auth",
        "turn_count": 5,
        "session_turns": [
            {"situation": "error", "decision": "investigate"},
        ],
        "unresolved_threads": ["update docs"],
    })
    assert ctx["objective"] == "fix auth"
    assert ctx["turn_count"] == 5
    assert len(ctx["turns"]) == 1
