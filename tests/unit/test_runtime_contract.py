"""The capability matrix, and the refusal that makes it observable.

The point of this module is that surfaces are not interchangeable: two of the four have no
interrupt at all.  A manager that quietly "interrupts" by killing the process is recording a lie,
so the matrix is total and ``require_verb`` raises with both the surface and the verb named.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from tce_shared.runtime_contract import (
    RUNTIME_CAPABILITIES,
    SURFACES,
    VERBS,
    RuntimeAdapter,
    RuntimeDescriptor,
    RuntimeResult,
    StartRequest,
    UnsupportedVerb,
    VerbSupport,
    capability_matrix_json,
    require_verb,
    runtime_descriptor_from_json,
    runtime_descriptor_to_json,
    runtime_result_from_json,
    runtime_result_to_json,
)


def test_capability_matrix_is_total() -> None:
    assert set(RUNTIME_CAPABILITIES) == set(SURFACES)
    for surface in SURFACES:
        assert set(RUNTIME_CAPABILITIES[surface]) == set(VERBS)
        assert all(isinstance(support, VerbSupport) for support in RUNTIME_CAPABILITIES[surface].values())


def test_the_two_surfaces_without_an_interrupt_say_so() -> None:
    assert RUNTIME_CAPABILITIES["codex/exec"]["interrupt"] is VerbSupport.UNSUPPORTED
    assert RUNTIME_CAPABILITIES["claude/oneshot"]["interrupt"] is VerbSupport.UNSUPPORTED
    assert RUNTIME_CAPABILITIES["codex/app-server"]["interrupt"] is VerbSupport.SUPPORTED
    assert RUNTIME_CAPABILITIES["claude/stream"]["interrupt"] is VerbSupport.SUPPORTED


def test_require_verb_names_the_surface_and_the_verb() -> None:
    with pytest.raises(UnsupportedVerb) as excinfo:
        require_verb("codex/exec", "interrupt")
    message = str(excinfo.value)
    assert "codex/exec" in message
    assert "interrupt" in message
    assert excinfo.value.surface == "codex/exec"
    assert excinfo.value.verb == "interrupt"


def test_a_degraded_verb_is_permitted_but_recorded() -> None:
    require_verb("codex/exec", "capabilities")  # DEGRADED does not raise ...
    assert capability_matrix_json("codex/exec")["capabilities"] == "degraded"  # ... but is on the record.


def test_an_unknown_surface_fails_closed() -> None:
    with pytest.raises(KeyError):
        require_verb("gemini/whatever", "start")
    with pytest.raises(KeyError):
        capability_matrix_json("gemini/whatever")


def test_kill_is_not_in_verbs() -> None:
    """``kill`` is a process operation supported everywhere, and it is deliberately not a protocol
    verb: it must never be a way to *simulate* an interrupt the surface does not have."""
    assert "kill" not in VERBS
    assert all("kill" not in matrix for matrix in RUNTIME_CAPABILITIES.values())
    assert hasattr(RuntimeAdapter, "kill")


class _FakeAdapter:
    surface = "codex/exec"

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            runtime_id="codex", runtime_version="0.153.1", surface=self.surface, model_id="m", contract_digest="d", available=True
        )

    def capabilities(self) -> dict[str, VerbSupport]:
        return dict(RUNTIME_CAPABILITIES[self.surface])

    def start(self, request: StartRequest) -> str:
        return "handle"

    def observe(self, handle: str) -> Iterator[object]:
        return iter(())

    def resume(self, handle: str, prompt: str) -> str:
        return handle

    def interrupt(self, handle: str, reason: str) -> None:
        return None

    def read_result(self, handle: str) -> RuntimeResult:
        return _result()

    def read_provider_record(self, provider_run_id: str) -> RuntimeResult | None:
        return None

    def kill(self, handle: str, reason: str) -> None:
        return None


def test_protocol_is_runtime_checkable() -> None:
    """``RuntimeAdapter`` has a non-method member (``surface``), so ``issubclass`` raises
    ``TypeError``.  Runtime checks must use ``isinstance``."""
    assert isinstance(_FakeAdapter(), RuntimeAdapter)
    with pytest.raises(TypeError):
        issubclass(_FakeAdapter, RuntimeAdapter)  # type: ignore[misc]


def _result() -> RuntimeResult:
    return RuntimeResult(
        outcome="succeeded",
        terminal_reason="completed",
        is_error=False,
        text="done",
        provider_run_id="run-1",
        provider_turn_id="turn-1",
        tokens_input=10,
        tokens_output=20,
        tokens_cached_input=5,
        tokens_reasoning=1,
        cost_minor_units=None,
        cost_source="unavailable",
        permission_denials=({"tool": "Bash", "reason": "denied"},),
    )


def test_result_and_descriptor_codecs_round_trip() -> None:
    result = _result()
    assert runtime_result_from_json(runtime_result_to_json(result)) == result
    descriptor = RuntimeDescriptor(
        runtime_id="claude",
        runtime_version="",
        surface="claude/stream",
        model_id="",
        contract_digest="",
        available=False,
        unavailable_reason="no credential provisioned",
    )
    assert runtime_descriptor_from_json(runtime_descriptor_to_json(descriptor)) == descriptor


def test_start_request_carries_the_manager_chosen_run_id() -> None:
    request = StartRequest(
        task_dir="/tmp/task",
        prompt="do the thing",
        provider_run_id="chosen-before-spawn",
        env={},
        argv_prefix=("/usr/bin/sandbox-exec", "-f", "/tmp/profile.sb"),
        max_budget_minor_units=None,
        wall_seconds=1800,
    )
    assert request.provider_run_id == "chosen-before-spawn"
    assert request.argv_prefix[0] == "/usr/bin/sandbox-exec"
    assert datetime.now(UTC).tzinfo is UTC
