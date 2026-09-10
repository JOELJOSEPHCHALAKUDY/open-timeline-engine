"""What each runtime surface can actually be asked to do, stated once and measured.

A "runtime surface" is a concrete way of driving a coding agent: ``codex/app-server`` (the JSON-RPC
app server), ``codex/exec`` (flat NDJSON), ``claude/stream`` (stream-json with stdin held open) and
``claude/oneshot``.  They are not interchangeable.  ``codex/exec`` and ``claude/oneshot`` have **no
interrupt**: the only way to stop them is to kill the process, and a killed run's effects resolve to
``unknown``, never to ``failed``.

``RUNTIME_CAPABILITIES`` records that per surface so the manager refuses a verb the surface lacks
instead of silently degrading.  ``DEGRADED`` is permitted but must be recorded on the dispatch row.

``kill`` is deliberately **not** in ``VERBS`` and not in ``RUNTIME_CAPABILITIES``.  It is a process
operation, not a protocol one, and it is supported everywhere.  Killing is never a way to *simulate*
an interrupt: on wall-clock expiry the supervisor calls ``adapter.kill(handle,
"wall_clock_exceeded")`` and the root effect resolves to ``unknown`` with
``resolution_source="reaper"``.  That is the honest record — we killed it, we do not know what it did.

Standard library only.  Nothing else imports from here in the backends; the supervisor does.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

RUNTIME_CONTRACT_VERSION: str = "v1"
VERBS: tuple[str, ...] = ("start", "observe", "resume", "interrupt", "read_result", "capabilities")
SURFACES: tuple[str, ...] = ("codex/app-server", "codex/exec", "claude/stream", "claude/oneshot")


class VerbSupport(StrEnum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class UnsupportedVerb(RuntimeError):
    surface: str
    verb: str

    def __init__(self, surface: str, verb: str) -> None:
        super().__init__(f"runtime surface {surface!r} does not support verb {verb!r}")
        self.surface = surface
        self.verb = verb


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    runtime_id: str
    runtime_version: str
    surface: str
    model_id: str
    contract_digest: str
    available: bool
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    seq: int
    kind: str  # "started"|"item"|"command"|"file_change"|"approval"|"usage"|"completed"|"error"
    at: datetime
    raw: Mapping[str, Any]
    provider_run_id: str | None = None
    provider_turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    outcome: str  # "succeeded"|"failed"|"interrupted"|"unknown"
    terminal_reason: str
    is_error: bool
    text: str
    provider_run_id: str | None
    provider_turn_id: str | None
    tokens_input: int
    tokens_output: int
    tokens_cached_input: int
    tokens_reasoning: int
    cost_minor_units: int | None
    cost_source: str
    permission_denials: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class StartRequest:
    task_dir: str
    prompt: str
    provider_run_id: str
    env: Mapping[str, str]
    argv_prefix: tuple[str, ...]
    max_budget_minor_units: int | None
    wall_seconds: int


RUNTIME_CAPABILITIES: dict[str, dict[str, VerbSupport]] = {
    "codex/app-server": {
        "start": VerbSupport.SUPPORTED,  # initialize -> thread/start -> turn/start
        "observe": VerbSupport.SUPPORTED,  # server notifications incl. item/* and turn/*
        "resume": VerbSupport.SUPPORTED,  # thread/resume, thread/fork
        "interrupt": VerbSupport.SUPPORTED,  # turn/interrupt -> status "interrupted"; turn/steer
        "read_result": VerbSupport.SUPPORTED,  # turn/completed + thread/read + thread/items/list
        "capabilities": VerbSupport.SUPPORTED,  # initialize handshake + model/list
    },
    "codex/exec": {
        "start": VerbSupport.SUPPORTED,
        "observe": VerbSupport.SUPPORTED,  # flat NDJSON, coarser than app-server
        "resume": VerbSupport.SUPPORTED,  # `exec --json ... resume <thread_id>`
        "interrupt": VerbSupport.UNSUPPORTED,  # kill only -> an unknown effect state
        "read_result": VerbSupport.SUPPORTED,  # turn.completed + --output-last-message
        "capabilities": VerbSupport.DEGRADED,  # --version only; no pre-flight negotiation
    },
    "claude/stream": {
        "start": VerbSupport.SUPPORTED,  # --session-id chosen by the MANAGER before spawn
        "observe": VerbSupport.SUPPORTED,  # NDJSON: system/init, assistant, user, result
        "resume": VerbSupport.SUPPORTED,  # --resume [--fork-session]
        "interrupt": VerbSupport.SUPPORTED,  # only in --input-format stream-json WITH stdin open
        "read_result": VerbSupport.SUPPORTED,  # exactly one terminal `result` object
        "capabilities": VerbSupport.DEGRADED,  # system/init arrives only AFTER start
    },
    "claude/oneshot": {
        "start": VerbSupport.SUPPORTED,
        "observe": VerbSupport.DEGRADED,  # --output-format json: terminal object only
        "resume": VerbSupport.SUPPORTED,
        "interrupt": VerbSupport.UNSUPPORTED,  # single-message input mode has no interruption
        "read_result": VerbSupport.SUPPORTED,
        "capabilities": VerbSupport.DEGRADED,
    },
}


def require_verb(surface: str, verb: str) -> None:
    """Raise ``UnsupportedVerb`` when the surface cannot honour the verb.

    A ``KeyError`` on an unknown surface is deliberate: fail closed rather than assume support.
    """
    if RUNTIME_CAPABILITIES[surface][verb] is VerbSupport.UNSUPPORTED:
        raise UnsupportedVerb(surface, verb)


def capability_matrix_json(surface: str) -> dict[str, str]:
    return {verb: support.value for verb, support in RUNTIME_CAPABILITIES[surface].items()}


def runtime_result_to_json(result: RuntimeResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome,
        "terminal_reason": result.terminal_reason,
        "is_error": result.is_error,
        "text": result.text,
        "provider_run_id": result.provider_run_id,
        "provider_turn_id": result.provider_turn_id,
        "tokens_input": result.tokens_input,
        "tokens_output": result.tokens_output,
        "tokens_cached_input": result.tokens_cached_input,
        "tokens_reasoning": result.tokens_reasoning,
        "cost_minor_units": result.cost_minor_units,
        "cost_source": result.cost_source,
        "permission_denials": [dict(entry) for entry in result.permission_denials],
        "schema_version": RUNTIME_CONTRACT_VERSION,
    }


def runtime_result_from_json(value: Mapping[str, Any]) -> RuntimeResult:
    denials = value.get("permission_denials") or ()
    cost = value.get("cost_minor_units")
    return RuntimeResult(
        outcome=str(value.get("outcome") or "unknown"),
        terminal_reason=str(value.get("terminal_reason") or ""),
        is_error=bool(value.get("is_error")),
        text=str(value.get("text") or ""),
        provider_run_id=(str(value["provider_run_id"]) if value.get("provider_run_id") else None),
        provider_turn_id=(str(value["provider_turn_id"]) if value.get("provider_turn_id") else None),
        tokens_input=int(value.get("tokens_input") or 0),
        tokens_output=int(value.get("tokens_output") or 0),
        tokens_cached_input=int(value.get("tokens_cached_input") or 0),
        tokens_reasoning=int(value.get("tokens_reasoning") or 0),
        cost_minor_units=(int(cost) if cost is not None else None),
        cost_source=str(value.get("cost_source") or "unavailable"),
        permission_denials=tuple(dict(entry) for entry in denials),
    )


def runtime_descriptor_to_json(descriptor: RuntimeDescriptor) -> dict[str, Any]:
    return {
        "runtime_id": descriptor.runtime_id,
        "runtime_version": descriptor.runtime_version,
        "surface": descriptor.surface,
        "model_id": descriptor.model_id,
        "contract_digest": descriptor.contract_digest,
        "available": descriptor.available,
        "unavailable_reason": descriptor.unavailable_reason,
        "schema_version": RUNTIME_CONTRACT_VERSION,
    }


def runtime_descriptor_from_json(value: Mapping[str, Any]) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id=str(value.get("runtime_id") or ""),
        runtime_version=str(value.get("runtime_version") or ""),
        surface=str(value.get("surface") or ""),
        model_id=str(value.get("model_id") or ""),
        contract_digest=str(value.get("contract_digest") or ""),
        available=bool(value.get("available")),
        unavailable_reason=str(value.get("unavailable_reason") or ""),
    )


@runtime_checkable
class RuntimeAdapter(Protocol):
    """The contract every adapter satisfies.

    NOTE for anyone writing a runtime check against this: it has a **non-method member**
    (``surface``), so ``issubclass(cls, RuntimeAdapter)`` raises ``TypeError``.  Use
    ``isinstance(obj, RuntimeAdapter)``.
    """

    surface: str

    def describe(self) -> RuntimeDescriptor: ...
    def capabilities(self) -> dict[str, VerbSupport]: ...
    def start(self, request: StartRequest) -> str: ...
    def observe(self, handle: str) -> Iterator[RuntimeEvent]: ...
    def resume(self, handle: str, prompt: str) -> str: ...
    def interrupt(self, handle: str, reason: str) -> None: ...
    def read_result(self, handle: str) -> RuntimeResult: ...
    def read_provider_record(self, provider_run_id: str) -> RuntimeResult | None: ...
    def kill(self, handle: str, reason: str) -> None: ...
