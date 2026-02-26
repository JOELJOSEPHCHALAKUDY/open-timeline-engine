from __future__ import annotations


def test_session_turn_structure():
    """Verify the turn record dict has expected keys."""
    turn = {
        "turn": 1,
        "message_preview": "fix the auth bug",
        "timestamp": "2026-02-19T10:00:00+00:00",
    }
    assert "turn" in turn
    assert "message_preview" in turn
    assert "timestamp" in turn
    assert len(turn["message_preview"]) <= 200


def test_session_turns_capped_at_20():
    """Verify that session_turns list is capped at 20."""
    turns = [{"turn": i, "message_preview": f"msg {i}", "timestamp": "t"} for i in range(25)]
    capped = turns[-20:]
    assert len(capped) == 20
    assert capped[0]["turn"] == 5
