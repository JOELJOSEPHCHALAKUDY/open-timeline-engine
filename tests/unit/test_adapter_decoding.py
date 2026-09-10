"""Table-driven decoding against the CAPTURED transcripts, with no vendor binary in sight.

Every fixture under ``tests/fixtures/p3/`` except the two ``fake_*`` programs is real output from
the real runtime on this host, scrubbed of the home directory and of session UUIDs.  Testing the
decoders against captured bytes rather than against hand-written examples is the whole point: a
hand-written example encodes what the author believed the protocol emits, which is exactly the thing
that was wrong twice — ``tool/requestUserInput`` instead of ``item/tool/requestUserInput``, and an
object where the protocol wants an enum string.

The synthetic case in :func:`test_the_corrected_approval_method_is_what_matches` is the exception,
and it is synthetic because the approval callback never fires under ``approvalPolicy: "never"`` —
so there is nothing to capture, and the corrected string has to be pinned some other way.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from tce_shared.budget import spend_enforcement_for
from tce_shared.runtime_contract import SURFACES
from tce_supervisor.adapters import claude_stream, codex_app_server, codex_exec

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "p3")


def _lines(name: str) -> list[dict[str, Any]]:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _json(name: str) -> dict[str, Any]:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


# ---------------------------------------------------------------------------------------------
# codex/exec — the captured live NDJSON turn
# ---------------------------------------------------------------------------------------------


def test_codex_exec_decodes_the_captured_turn() -> None:
    payloads = _lines("codex_exec_turn.jsonl")
    assert [p["type"] for p in payloads] == ["thread.started", "turn.started", "item.completed", "turn.completed"]
    events = [codex_exec.decode_event(index + 1, payload) for index, payload in enumerate(payloads)]
    assert [event.kind for event in events if event is not None] == ["started", "item", "item", "completed"]
    assert events[0] is not None and events[0].provider_turn_id == "01a086d7-6130-7102-8f1c-7ca4629db431"

    result = codex_exec.decode_result([e for e in events if e is not None], provider_run_id="run-1", provider_turn_id="01a086d7-6130-7102-8f1c-7ca4629db431")
    assert result.outcome == "succeeded"
    assert result.text == "OK"
    assert (result.tokens_input, result.tokens_cached_input, result.tokens_output) == (17540, 12928, 5)
    # No currency anywhere in this stream. Reporting a zero cost would say "free"; the honest
    # answer is "we were not told".
    assert result.cost_minor_units is None
    assert result.cost_source == "unavailable"


def test_the_resume_transcript_proves_history_replay() -> None:
    """The same thread id, with the cache counter jumping — that is what "resumed" means here."""
    first = _lines("codex_exec_turn.jsonl")
    resumed = _lines("codex_exec_resume.jsonl")
    assert first[0]["thread_id"] == resumed[0]["thread_id"]
    assert resumed[-1]["usage"]["cached_input_tokens"] > first[-1]["usage"]["cached_input_tokens"]


def test_codex_exec_item_types_become_effect_kinds() -> None:
    for item_type, expected in (("command_execution", "command"), ("file_change", "file_change"), ("agent_message", "item")):
        event = codex_exec.decode_event(1, {"type": "item.completed", "item": {"type": item_type}})
        assert event is not None and event.kind == expected, item_type


def test_an_unknown_line_decodes_to_nothing_rather_than_a_guess() -> None:
    assert codex_exec.decode_event(1, {"type": "something.new"}) is None
    assert codex_app_server.decode_event(1, {"method": "some/unknown/notification"}) is None
    assert claude_stream.decode_event(1, {"type": "unrecognised"}) is None
    # A JSON-RPC *response* is not an event either — it belongs to the request that asked for it.
    assert codex_app_server.decode_event(1, {"id": 1, "result": {}}) is None


# ---------------------------------------------------------------------------------------------
# codex/app-server — the decoding table, including both corrected strings
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("thread/started", "started"),
        ("item/started", "item"),
        ("item/commandExecution/outputDelta", "command"),
        ("item/fileChange/patchUpdated", "file_change"),
        ("item/commandExecution/requestApproval", "approval"),
        ("item/fileChange/requestApproval", "approval"),
        ("item/tool/requestUserInput", "approval"),
        ("thread/tokenUsage/updated", "usage"),
        ("account/rateLimits/updated", "usage"),
        ("turn/completed", "completed"),
        ("error", "error"),
        ("guardianWarning", "error"),
    ],
)
def test_app_server_decoding_table(method: str, expected: str) -> None:
    event = codex_app_server.decode_event(1, {"jsonrpc": "2.0", "method": method, "params": {"threadId": "t-1"}})
    assert event is not None
    assert event.kind == expected


def test_the_corrected_approval_method_is_what_matches() -> None:
    """``tool/requestUserInput`` matches nothing; the real notification is ``item/tool/...``.

    The consequence of getting this wrong is silent: no error, no warning, and
    ``human_intervention_count`` simply never increments.
    """
    corrected = codex_app_server.decode_event(1, {"jsonrpc": "2.0", "method": "item/tool/requestUserInput", "params": {"threadId": "t-1"}})
    assert corrected is not None and corrected.kind == "approval"
    assert codex_app_server.decode_event(1, {"jsonrpc": "2.0", "method": "tool/requestUserInput", "params": {}}) is None


def test_app_server_item_completed_carries_the_effect_kind() -> None:
    for item_type, expected in (("command_execution", "command"), ("file_change", "file_change"), ("agent_message", "item")):
        event = codex_app_server.decode_event(1, {"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": "t", "item": {"type": item_type}}})
        assert event is not None and event.kind == expected, item_type


def test_app_server_result_folds_usage_and_status() -> None:
    events = [
        codex_app_server.decode_event(1, {"method": "thread/started", "params": {"threadId": "t-1"}}),
        codex_app_server.decode_event(2, {"method": "item/completed", "params": {"item": {"type": "agent_message", "text": "OK"}}}),
        codex_app_server.decode_event(
            3,
            {"method": "turn/completed", "params": {"status": "completed", "usage": {"input_tokens": 11, "output_tokens": 2, "cached_input_tokens": 3, "reasoning_output_tokens": 1}}},
        ),
    ]
    result = codex_app_server.decode_result([e for e in events if e is not None], provider_run_id="run-1", provider_turn_id="t-1")
    assert (result.outcome, result.terminal_reason, result.is_error) == ("succeeded", "completed", False)
    assert (result.tokens_input, result.tokens_output, result.tokens_cached_input, result.tokens_reasoning) == (11, 2, 3, 1)
    assert result.text == "OK"


def test_an_interrupted_turn_is_interrupted_not_failed() -> None:
    event = codex_app_server.decode_event(1, {"method": "turn/completed", "params": {"status": "interrupted"}})
    assert event is not None
    result = codex_app_server.decode_result([event], provider_run_id="r", provider_turn_id="t")
    assert result.outcome == "interrupted"
    assert result.is_error is False


def test_a_killed_turn_is_unknown_even_when_it_completed() -> None:
    """A kill overrides everything: we stopped it, so what it did is not ours to assert."""
    event = codex_app_server.decode_event(1, {"method": "turn/completed", "params": {"status": "completed"}})
    assert event is not None
    result = codex_app_server.decode_result([event], provider_run_id="r", provider_turn_id="t", killed_reason="wall_clock_exceeded")
    assert result.outcome == "unknown"
    assert result.terminal_reason == "wall_clock_exceeded"


def test_the_pinned_contract_bundle_is_present_and_large() -> None:
    """The generated protocol bundle is the contract; its sha256 is the drift check."""
    path = codex_app_server.contract_path()
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        bundle = json.load(handle)
    assert len(bundle["definitions"]) == 740, "the pinned bundle is the 740-definition v2 schema set"
    for name in ("ThreadStartParams", "TurnStartParams", "SandboxMode", "SandboxPolicy"):
        assert name in bundle["definitions"], name


def test_thread_start_sandbox_is_an_enum_string_in_the_pinned_contract() -> None:
    """The correction that would otherwise have been silently wrong at runtime.

    ``ThreadStartParams.sandbox`` is a ``SandboxMode`` enum string; the ``{mode, writable_roots,
    network_access}`` object belongs to ``TurnStartParams.sandboxPolicy``.
    """
    with open(codex_app_server.contract_path(), encoding="utf-8") as handle:
        definitions = json.load(handle)["definitions"]
    rendered = json.dumps(definitions["ThreadStartParams"]["properties"]["sandbox"])
    assert "SandboxMode" in rendered, rendered[:200]
    assert "writableRoots" not in rendered and "writable_roots" not in rendered
    # And SandboxMode really is an enum of exactly the three strings the adapter may send.
    assert definitions["SandboxMode"]["enum"] == ["read-only", "workspace-write", "danger-full-access"]
    # The object form belongs to the OTHER params type, which is the confusion this pins.
    assert "SandboxPolicy" in json.dumps(definitions["TurnStartParams"]["properties"]["sandboxPolicy"])


# ---------------------------------------------------------------------------------------------
# claude/stream — the captured terminal object
# ---------------------------------------------------------------------------------------------


def test_claude_decodes_the_captured_terminal_object() -> None:
    payload = _json("claude_stream_result.json")
    result = claude_stream.decode_result(payload, provider_run_id="run-1")
    assert result.is_error is True
    assert result.terminal_reason == "api_error"
    assert result.outcome == "failed"
    # total_cost_usd is an explicit CLIENT-SIDE estimate. It is never "provider_reported".
    assert result.cost_source == "estimated"
    assert result.permission_denials == ()


def test_the_captured_transcript_has_exactly_one_terminal_result() -> None:
    payloads = _lines("claude_stream_transcript.jsonl")
    assert [p["type"] for p in payloads] == ["system", "system", "system", "assistant", "result"]
    assert len([p for p in payloads if p["type"] == "result"]) == 1
    kinds = [claude_stream.decode_event(index + 1, payload) for index, payload in enumerate(payloads)]
    assert [event.kind for event in kinds if event is not None] == ["item", "item", "started", "item", "completed"]


def test_the_golden_contract_pins_key_sets_not_values() -> None:
    """Values vary between runs (session ids, timings, cwd); pinning them would make every run drift."""
    key_sets = claude_stream.golden_key_sets()
    assert set(key_sets) == {"system/init", "result"}
    assert "session_id" in key_sets["result"]
    assert "permission_denials" in key_sets["result"]
    assert "usage" in key_sets["result"]
    digest = claude_stream.contract_digest_from(key_sets)
    assert len(digest) == 64
    # Stable across calls, and different when a key is added.
    assert digest == claude_stream.contract_digest_from(key_sets)
    widened = {name: list(keys) + ["a_new_key"] for name, keys in key_sets.items()}
    assert claude_stream.contract_digest_from(widened) != digest


def test_no_observed_budget_error_subtype_anywhere_in_the_fixtures() -> None:
    """The whole basis of ``estimated`` rather than ``enforced``.

    The day a captured transcript here contains ``error_max_budget_usd``, this test fails and the
    spend table can honestly be flipped. Until then the only budget evidence in the capture is
    ``subagent_stats.refused.budget`` — a subagent counter, not the top-level cap.
    """
    blobs = []
    for name in os.listdir(FIXTURES):
        if name.endswith((".json", ".jsonl")):
            with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
                blobs.append(handle.read())
    assert blobs
    assert not [blob for blob in blobs if "error_max_budget_usd" in blob]
    assert any("subagent_stats" in blob for blob in blobs), "the capture that grounds this claim is missing"
    assert "enforced" not in {spend_enforcement_for(surface) for surface in SURFACES}
    assert spend_enforcement_for("claude/stream") == "estimated"
    assert spend_enforcement_for("codex/app-server") == "unsupported"
