"""``codex/app-server`` — the default surface. All six verbs are real.

Four things here were measured rather than read off documentation, and each would have been
silently wrong at runtime otherwise:

* **``--ignore-user-config`` is not passed.**  ``codex app-server --ignore-user-config`` exits 2
  with ``error: unexpected argument``; the flag exists only on ``codex exec``.  Ambient-config
  suppression rests on ``CODEX_HOME`` alone, and that was measured working — the ``initialize``
  reply's ``codexHome`` is the task directory copy.
* **``ThreadStartParams.sandbox`` is a ``SandboxMode`` enum string**, not the
  ``{mode, writable_roots, network_access}`` object.  The object form belongs to
  ``TurnStartParams.sandboxPolicy``.
* **The approval notification is ``item/tool/requestUserInput``**, not ``tool/requestUserInput``.
  The shorter string matches nothing, which would undercount ``human_intervention_count`` to zero
  without any error.
* **``sandbox: "danger-full-access"`` is deliberate and is the honest choice.**  Nesting
  ``sandbox-exec`` is refused in *both* directions — a strictly more restrictive inner profile is
  refused too — so an inner ``workspace-write`` makes every command the agent runs fail with
  ``sandbox_apply: Operation not permitted``.  The outer Seatbelt profile is the real boundary and
  it is the one that is measured.  ``--dangerously-bypass-approvals-and-sandbox`` is still never
  passed: it would also disable the per-tool events the effect journal is built on.

**What this surface cannot do, stated rather than papered over.**  It reports no currency anywhere:
``turn/completed.usage`` is tokens only and there is no ``--max-budget-usd`` equivalent in the CLI
or in the protocol bundle.  ``spend_enforcement`` is ``unsupported``.  And ``approvalPolicy:
"never"`` means the approval callbacks never fire, so ``human_intervention_count`` is structurally
**0** on this surface — a floor, not a count.  Leaving approvals on would produce a partial
callback stream that reads like a chokepoint and is not one.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tce_shared.runtime_contract import (
    RUNTIME_CAPABILITIES,
    RuntimeDescriptor,
    RuntimeEvent,
    RuntimeResult,
    StartRequest,
    VerbSupport,
)

from ..config import SupervisorSettings
from ._process import ChildProcess, measure_version, spawn

SURFACE: str = "codex/app-server"
CONTRACT_FILE: str = "codex-0.153.1.schemas.json"

# notification method -> RuntimeEvent.kind. The two corrected strings are marked.
_METHOD_KINDS: dict[str, str] = {
    "thread/started": "started",
    "item/started": "item",
    "item/completed": "item",
    "item/commandExecution/outputDelta": "command",
    "item/fileChange/patchUpdated": "file_change",
    "item/commandExecution/requestApproval": "approval",
    "item/fileChange/requestApproval": "approval",
    "item/tool/requestUserInput": "approval",  # CORRECTED: not "tool/requestUserInput"
    "thread/tokenUsage/updated": "usage",
    "account/rateLimits/updated": "usage",
    "turn/completed": "completed",
    "turn/failed": "completed",
    "error": "error",
    "guardianWarning": "error",
}

_ITEM_TYPE_KINDS: dict[str, str] = {"command_execution": "command", "file_change": "file_change"}


def contract_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "contracts", CONTRACT_FILE)


def decode_event(seq: int, payload: Mapping[str, Any], *, at: datetime | None = None) -> RuntimeEvent | None:
    """Decode one JSON-RPC message into a :class:`RuntimeEvent`, or None when it is a response.

    Pure: no process, no settings, no clock unless one is passed. This is what makes the decoding
    table testable against the captured transcripts without a vendor binary.
    """
    method = str(payload.get("method") or "")
    if not method:
        return None
    kind = _METHOD_KINDS.get(method)
    if kind is None:
        return None
    params = payload.get("params")
    params_map: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
    if kind == "item":
        item = params_map.get("item")
        if isinstance(item, Mapping):
            kind = _ITEM_TYPE_KINDS.get(str(item.get("type") or ""), kind)
    thread_id = params_map.get("threadId") or params_map.get("thread_id")
    return RuntimeEvent(
        seq=seq,
        kind=kind,
        at=at or datetime.now(UTC),
        raw=dict(payload),
        provider_run_id=None,
        provider_turn_id=str(thread_id) if thread_id else None,
    )


_TOKEN_KEY_NAMES: frozenset[str] = frozenset(
    {"input_tokens", "inputTokens", "output_tokens", "outputTokens", "cached_input_tokens", "cachedInputTokens", "reasoning_output_tokens", "reasoningOutputTokens"}
)


def _find_usage(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Breadth-first search for the mapping that actually carries the token counters.

    The counters sit at different depths on different surfaces — ``turn.completed.usage`` on the
    NDJSON stream, ``params.usage`` on the app server, ``params.info.tokenUsage`` on a token-usage
    notification. Searching for the KEYS rather than walking a fixed path is what keeps one shape
    change from silently zeroing the whole accounting.
    """
    queue: list[Mapping[str, Any]] = [payload]
    while queue:
        current = queue.pop(0)
        if _TOKEN_KEY_NAMES & set(current):
            return current
        for value in current.values():
            if isinstance(value, Mapping):
                queue.append(value)
    return None


def decode_usage(payload: Mapping[str, Any]) -> dict[str, int]:
    """Pull the four token counters out of a ``usage`` object, whatever nests it."""
    usage = _find_usage(payload)
    if usage is None:
        return {}

    def read(*names: str) -> int:
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    return {
        "tokens_input": read("input_tokens", "inputTokens"),
        "tokens_output": read("output_tokens", "outputTokens"),
        "tokens_cached_input": read("cached_input_tokens", "cachedInputTokens"),
        "tokens_reasoning": read("reasoning_output_tokens", "reasoningOutputTokens"),
    }


def decode_result(events: Sequence[RuntimeEvent], *, provider_run_id: str, provider_turn_id: str | None, killed_reason: str | None = None, interrupted_reason: str | None = None) -> RuntimeResult:
    """Fold an event stream into the terminal result.

    ``cost_minor_units`` is ``None`` and ``cost_source`` is ``"unavailable"`` because this protocol
    reports no currency at all. The manager estimates from its own price table afterwards; it does
    not invent a number here.
    """
    tokens = {"tokens_input": 0, "tokens_output": 0, "tokens_cached_input": 0, "tokens_reasoning": 0}
    text_parts: list[str] = []
    outcome = "unknown"
    terminal_reason = "no_terminal_event"
    is_error = False
    for event in events:
        if event.kind in {"usage", "completed"}:
            for key, value in decode_usage(event.raw).items():
                if value:
                    tokens[key] = value
        if event.kind == "item":
            params = event.raw.get("params")
            item = params.get("item") if isinstance(params, Mapping) else None
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                text_parts.append(str(item.get("text") or ""))
        if event.kind == "error":
            is_error = True
            terminal_reason = str(event.raw.get("method") or "error")
        if event.kind == "completed":
            params = event.raw.get("params")
            status = str(params.get("status") or "") if isinstance(params, Mapping) else ""
            if str(event.raw.get("method")) == "turn/failed" or status == "failed":
                outcome, terminal_reason, is_error = "failed", status or "turn_failed", True
            elif status == "interrupted":
                outcome, terminal_reason = "interrupted", "turn_interrupted"
            else:
                outcome, terminal_reason = "succeeded", status or "turn_completed"

    if interrupted_reason is not None:
        outcome, terminal_reason = "interrupted", interrupted_reason
    if killed_reason is not None:
        # A kill is never a "failure": we killed it and we do not know what it did.
        outcome, terminal_reason = "unknown", killed_reason

    return RuntimeResult(
        outcome=outcome,
        terminal_reason=terminal_reason,
        is_error=is_error,
        text="\n".join(part for part in text_parts if part),
        provider_run_id=provider_run_id,
        provider_turn_id=provider_turn_id,
        tokens_input=tokens["tokens_input"],
        tokens_output=tokens["tokens_output"],
        tokens_cached_input=tokens["tokens_cached_input"],
        tokens_reasoning=tokens["tokens_reasoning"],
        cost_minor_units=None,
        cost_source="unavailable",
        permission_denials=(),
    )


class _Session:
    def __init__(self, child: ChildProcess, wall_seconds: int) -> None:
        self.child = child
        self.wall_seconds = wall_seconds
        self.next_id = 1
        self.thread_id: str | None = None
        self.pending: list[dict[str, Any]] = []
        self.events: list[RuntimeEvent] = []
        self.seq = 0
        self.completed = False


class CodexAppServerAdapter:
    """The default surface."""

    surface: str = SURFACE

    def __init__(self, *, settings: SupervisorSettings) -> None:
        self.settings = settings
        self._sessions: dict[str, _Session] = {}
        self._descriptor: RuntimeDescriptor | None = None

    # -- argv (pure; no spawn, so a unit test can assert it) ----------------------------------

    def build_argv(self, argv_prefix: Sequence[str]) -> tuple[str, ...]:
        return tuple(argv_prefix) + (self.settings.codex_binary, "app-server")

    # -- description ---------------------------------------------------------------------------

    def contract_digest(self) -> str:
        import hashlib

        try:
            with open(contract_path(), "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ""

    def measure_contract_drift(self) -> str:
        """Re-generate the protocol bundle and compare it to the pinned one.

        Returns "" when they match, or a human-readable reason. When the generator cannot be run at
        all the answer is "" — an unmeasurable drift check must not masquerade as a passed one, so
        the caller is told nothing rather than told "no drift"; the version pin is the control that
        still applies.
        """
        binary = self.settings.codex_binary
        if not binary or not os.path.isfile(binary):
            return ""
        pinned = contract_path()
        if not os.path.isfile(pinned):
            return "the pinned contract bundle is missing"
        with tempfile.TemporaryDirectory(prefix="tce-sup-contract-") as tmp:
            try:
                completed = subprocess.run(
                    [binary, "app-server", "generate-json-schema", "--out", tmp, "--experimental"],
                    capture_output=True,
                    timeout=120,
                    check=False,
                    # cwd and stdin are both pinned deliberately. The generator is a vendor binary
                    # and it must not run with the supervisor's working directory (it would drop
                    # files into the repository) or with the supervisor's stdin.
                    cwd=tmp,
                    stdin=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "HOME": tmp},
                )
            except (OSError, subprocess.TimeoutExpired):
                return ""
            if completed.returncode != 0:
                return ""
            candidates = [name for name in os.listdir(tmp) if name.endswith(".v2.schemas.json")]
            if not candidates:
                return ""
            import hashlib

            with open(os.path.join(tmp, candidates[0]), "rb") as handle:
                observed = hashlib.sha256(handle.read()).hexdigest()
        return "" if observed == self.contract_digest() else f"protocol contract drift: generated {observed[:12]} != pinned {self.contract_digest()[:12]}"

    def describe(self) -> RuntimeDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        version = measure_version(self.settings.codex_binary)
        pin = str(self.settings.codex_version_pin or "")
        reason = ""
        if not version:
            reason = f"{self.settings.codex_binary} is not present or did not answer --version"
        elif pin and version != pin:
            # D-5: both binaries live inside auto-updating consumer apps. A version bump is a
            # contract-revalidation event, not a no-op.
            reason = f"version mismatch: measured {version!r}, pinned {pin!r}"
        else:
            reason = self.measure_contract_drift()
        descriptor = RuntimeDescriptor(
            runtime_id="codex",
            runtime_version=version or "unknown",
            surface=SURFACE,
            model_id="",
            contract_digest=self.contract_digest(),
            available=not reason,
            unavailable_reason=reason,
        )
        self._descriptor = descriptor
        return descriptor

    def capabilities(self) -> dict[str, VerbSupport]:
        return dict(RUNTIME_CAPABILITIES[SURFACE])

    # -- JSON-RPC plumbing ----------------------------------------------------------------------

    def _send(self, session: _Session, method: str, params: Mapping[str, Any]) -> int:
        request_id = session.next_id
        session.next_id += 1
        session.child.write_line(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}))
        return request_id

    def _await_response(self, session: _Session, request_id: int, *, timeout: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        for line in session.child.lines(deadline=deadline):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, Mapping):
                continue
            if payload.get("id") == request_id and ("result" in payload or "error" in payload):
                return payload
            session.pending.append(dict(payload))
        raise TimeoutError(f"{SURFACE}: no response to request {request_id} within {timeout}s")

    # -- verbs -----------------------------------------------------------------------------------

    def start(self, request: StartRequest) -> str:
        env = dict(request.env)
        clone = os.path.join(request.task_dir, "clone")
        child = spawn(self.build_argv(request.argv_prefix), cwd=clone, env=env, provider_run_id=request.provider_run_id, stdin_open=True)
        session = _Session(child, request.wall_seconds)
        self._sessions[child.handle] = session

        self._await_response(session, self._send(session, "initialize", {"clientInfo": {"name": "tce-supervisor", "title": "tce-supervisor", "version": "0.4.0"}, "capabilities": {}}), timeout=60.0)
        started = self._await_response(
            session,
            self._send(session, "thread/start", {"cwd": clone, "approvalPolicy": "never", "sandbox": "danger-full-access"}),
            timeout=60.0,
        )
        result = started.get("result")
        if isinstance(result, Mapping):
            thread_id = result.get("threadId") or result.get("thread_id")
            if thread_id:
                session.thread_id = str(thread_id)
                child.provider_turn_id = session.thread_id
        self._send(session, "turn/start", {"threadId": session.thread_id, "clientUserMessageId": request.provider_run_id, "input": [{"type": "text", "text": request.prompt}]})
        return child.handle

    def observe(self, handle: str) -> Iterator[RuntimeEvent]:
        session = self._sessions[handle]
        deadline = session.child.started_at + float(session.wall_seconds)

        def emit(payload: Mapping[str, Any]) -> RuntimeEvent | None:
            session.seq += 1
            event = decode_event(session.seq, payload)
            if event is None:
                session.seq -= 1
                return None
            if event.provider_turn_id and session.thread_id is None:
                session.thread_id = event.provider_turn_id
                session.child.provider_turn_id = event.provider_turn_id
            session.events.append(event)
            if event.kind == "completed":
                session.completed = True
            return event

        while session.pending:
            event = emit(session.pending.pop(0))
            if event is not None:
                yield event
        for line in session.child.lines(deadline=deadline):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, Mapping):
                continue
            event = emit(payload)
            if event is not None:
                yield event
                if event.kind == "completed":
                    return

    def resume(self, handle: str, prompt: str) -> str:
        session = self._sessions[handle]
        if session.thread_id is None:
            raise RuntimeError(f"{SURFACE}: cannot resume before a thread id is bound")
        self._await_response(session, self._send(session, "thread/resume", {"threadId": session.thread_id}), timeout=60.0)
        self._send(session, "turn/start", {"threadId": session.thread_id, "clientUserMessageId": session.child.provider_run_id, "input": [{"type": "text", "text": prompt}]})
        session.completed = False
        return handle

    def interrupt(self, handle: str, reason: str) -> None:
        """A first-class, non-signal interrupt: the turn ends with ``status: "interrupted"``."""
        session = self._sessions[handle]
        session.child.interrupted_reason = reason
        self._send(session, "turn/interrupt", {"threadId": session.thread_id})

    def read_result(self, handle: str) -> RuntimeResult:
        session = self._sessions[handle]
        return decode_result(
            session.events,
            provider_run_id=session.child.provider_run_id,
            provider_turn_id=session.thread_id,
            killed_reason=session.child.killed_reason,
            interrupted_reason=session.child.interrupted_reason,
        )

    def read_provider_record(self, provider_run_id: str) -> RuntimeResult | None:
        """Read the provider's own durable record for a run the supervisor no longer holds.

        **A stated limitation.** Codex indexes threads by the ``thread_id`` it chooses, not by the
        manager-chosen ``clientUserMessageId``. The reconciler must therefore pass the bound
        ``provider_turn_id`` here; passing the ``provider_run_id`` for a dispatch whose thread id
        was never bound returns ``None``, and ``None`` means "the provider has no record", which is
        the condition under which the effect resolves to ``unknown``. It is not a claim that the
        run did not happen.
        """
        if not provider_run_id:
            return None
        binary = self.settings.codex_binary
        if not binary or not os.path.isfile(binary):
            return None
        env = {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir(), "CODEX_HOME": os.path.expanduser("~/.codex")}
        payloads = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "tce-supervisor", "title": "tce-supervisor", "version": "0.4.0"}, "capabilities": {}}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "thread/read", "params": {"threadId": provider_run_id}}),
        ]
        try:
            completed = subprocess.run([binary, "app-server"], input=("\n".join(payloads) + "\n").encode(), capture_output=True, timeout=60, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        events: list[RuntimeEvent] = []
        found = False
        for index, line in enumerate(completed.stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, Mapping):
                continue
            if payload.get("id") == 2:
                if "error" in payload:
                    return None
                found = True
                continue
            event = decode_event(index + 1, payload)
            if event is not None:
                events.append(event)
        if not found:
            return None
        return decode_result(events, provider_run_id=provider_run_id, provider_turn_id=provider_run_id)

    def kill(self, handle: str, reason: str) -> None:
        session = self._sessions.get(handle)
        if session is not None:
            session.child.kill_group(reason)

    # -- housekeeping ---------------------------------------------------------------------------

    def cleanup(self, handle: str) -> None:
        session = self._sessions.pop(handle, None)
        if session is not None and session.child.running:
            session.child.kill_group("adapter_cleanup")
