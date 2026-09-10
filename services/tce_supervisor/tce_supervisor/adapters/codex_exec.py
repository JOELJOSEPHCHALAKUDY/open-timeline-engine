"""``codex/exec`` — fire-and-forget flat NDJSON. No interrupt.

``interrupt`` is ``UNSUPPORTED`` here and that is not a shortcoming to be worked around: the only
way to stop this surface is to kill the process, and a killed run's effects resolve to ``unknown``.
:func:`require_verb` raises before any kill is attempted, the refusal is journalled, and step 4 of
the dispatch sequence will not select this surface for a task whose plan needs interruption.  The
wall clock is still enforced — through ``kill``, recorded honestly as ``unknown``.

``--ignore-user-config`` **is** valid on ``codex exec`` (unlike ``codex app-server``), and
``-s danger-full-access`` is passed for the same measured reason as the app-server surface: nesting
Seatbelt is refused in both directions, so an inner sandbox makes every agent command fail while
the outer profile — the one that is actually measured — is the real boundary.

Resume takes its flags **before** the ``resume`` subcommand; placing them after fails with
"unexpected argument".  Verified live: the resumed thread returns the same ``thread_id`` with
``cached_input_tokens`` jumping, which is the proof that history was replayed rather than a fresh
thread started.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tce_shared.runtime_contract import (
    RUNTIME_CAPABILITIES,
    RuntimeDescriptor,
    RuntimeEvent,
    RuntimeResult,
    StartRequest,
    UnsupportedVerb,
    VerbSupport,
    require_verb,
)

from ..config import SupervisorSettings
from ._process import ChildProcess, measure_version, spawn
from .codex_app_server import CodexAppServerAdapter, decode_usage

SURFACE: str = "codex/exec"

_TYPE_KINDS: dict[str, str] = {
    "thread.started": "started",
    "turn.started": "item",
    "item.started": "item",
    "item.completed": "item",
    "turn.completed": "completed",
    "turn.failed": "completed",
    "error": "error",
}

_ITEM_TYPE_KINDS: dict[str, str] = {"command_execution": "command", "file_change": "file_change"}


def decode_event(seq: int, payload: Mapping[str, Any], *, at: datetime | None = None) -> RuntimeEvent | None:
    """Decode one NDJSON line. Pure — the captured live transcript is its test corpus."""
    raw_type = str(payload.get("type") or "")
    kind = _TYPE_KINDS.get(raw_type)
    if kind is None:
        return None
    if kind == "item":
        item = payload.get("item")
        if isinstance(item, Mapping):
            kind = _ITEM_TYPE_KINDS.get(str(item.get("type") or ""), kind)
    thread_id = payload.get("thread_id") or payload.get("threadId")
    return RuntimeEvent(
        seq=seq,
        kind=kind,
        at=at or datetime.now(UTC),
        raw=dict(payload),
        provider_run_id=None,
        provider_turn_id=str(thread_id) if thread_id else None,
    )


def decode_result(events: Sequence[RuntimeEvent], *, provider_run_id: str, provider_turn_id: str | None, killed_reason: str | None = None) -> RuntimeResult:
    tokens = {"tokens_input": 0, "tokens_output": 0, "tokens_cached_input": 0, "tokens_reasoning": 0}
    text_parts: list[str] = []
    outcome, terminal_reason, is_error = "unknown", "no_terminal_event", False
    for event in events:
        if event.kind == "completed":
            for key, value in decode_usage(event.raw).items():
                if value:
                    tokens[key] = value
            if str(event.raw.get("type")) == "turn.failed":
                outcome, terminal_reason, is_error = "failed", "turn_failed", True
            else:
                outcome, terminal_reason = "succeeded", "turn_completed"
        if event.kind == "error":
            is_error = True
            terminal_reason = str(event.raw.get("message") or "error")
        item = event.raw.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            text_parts.append(str(item.get("text") or ""))
    if killed_reason is not None:
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


class _Run:
    def __init__(self, child: ChildProcess, wall_seconds: int, *, clone: str, argv_prefix: Sequence[str], env: Mapping[str, str]) -> None:
        self.child = child
        self.wall_seconds = wall_seconds
        self.clone = clone
        self.argv_prefix = tuple(str(item) for item in argv_prefix)
        self.env = dict(env)
        self.events: list[RuntimeEvent] = []
        self.seq = 0
        self.thread_id: str | None = None


class CodexExecAdapter:
    surface: str = SURFACE

    def __init__(self, *, settings: SupervisorSettings) -> None:
        self.settings = settings
        self._runs: dict[str, _Run] = {}
        self._descriptor: RuntimeDescriptor | None = None

    def build_argv(self, argv_prefix: Sequence[str], *, clone: str, prompt: str, resume_thread_id: str | None = None) -> tuple[str, ...]:
        base = [self.settings.codex_binary, "exec", "--json", "--ignore-user-config", "-s", "danger-full-access", "-C", clone, "--skip-git-repo-check"]
        if resume_thread_id:
            # Flags MUST precede the `resume` subcommand.
            return tuple(argv_prefix) + tuple(base) + ("resume", resume_thread_id, prompt)
        return tuple(argv_prefix) + tuple(base) + (prompt,)

    def describe(self) -> RuntimeDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        version = measure_version(self.settings.codex_binary)
        pin = str(self.settings.codex_version_pin or "")
        reason = ""
        if not version:
            reason = f"{self.settings.codex_binary} is not present or did not answer --version"
        elif pin and version != pin:
            reason = f"version mismatch: measured {version!r}, pinned {pin!r}"
        # The contract digest is shared with the app-server surface: same binary, same protocol
        # bundle. The NDJSON stream itself has no generator.
        digest = CodexAppServerAdapter(settings=self.settings).contract_digest()
        self._descriptor = RuntimeDescriptor(
            runtime_id="codex",
            runtime_version=version or "unknown",
            surface=SURFACE,
            model_id="",
            contract_digest=digest,
            available=not reason,
            unavailable_reason=reason,
        )
        return self._descriptor

    def capabilities(self) -> dict[str, VerbSupport]:
        return dict(RUNTIME_CAPABILITIES[SURFACE])

    def start(self, request: StartRequest) -> str:
        clone = os.path.join(request.task_dir, "clone")
        child = spawn(self.build_argv(request.argv_prefix, clone=clone, prompt=request.prompt), cwd=clone, env=dict(request.env), provider_run_id=request.provider_run_id, stdin_open=False)
        self._runs[child.handle] = _Run(child, request.wall_seconds, clone=clone, argv_prefix=request.argv_prefix, env=request.env)
        return child.handle

    def observe(self, handle: str) -> Iterator[RuntimeEvent]:
        run = self._runs[handle]
        deadline = run.child.started_at + float(run.wall_seconds)
        for line in run.child.lines(deadline=deadline):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, Mapping):
                continue
            run.seq += 1
            event = decode_event(run.seq, payload)
            if event is None:
                run.seq -= 1
                continue
            if event.provider_turn_id and run.thread_id is None:
                run.thread_id = event.provider_turn_id
                run.child.provider_turn_id = event.provider_turn_id
            run.events.append(event)
            yield event
            if event.kind == "completed":
                return

    def resume(self, handle: str, prompt: str) -> str:
        run = self._runs[handle]
        if run.thread_id is None:
            raise RuntimeError(f"{SURFACE}: cannot resume before a thread id is bound")
        # A resume re-uses the SAME wrapper prefix, which means the same rendered profile path --
        # precisely the second exec that the F10 profile-rewrite attack targets.
        #
        # NOTHING IN THIS BUILD CALLS THIS METHOD. `supervise.resume_once` re-resolves the charter
        # and calls `sandbox.verify_profile`, then raises `resume_requires_live_handle` (D-2) and
        # never reaches an adapter. So this method spawns a sandboxed child WITHOUT a
        # `verify_profile` of its own, and the first caller to wire it up must add one here -- do
        # not rely on `resume_once` having done it, because on the day this becomes reachable the
        # re-hash will be several frames away from the exec it is supposed to guard.
        child = spawn(
            self.build_argv(run.argv_prefix, clone=run.clone, prompt=prompt, resume_thread_id=run.thread_id),
            cwd=run.clone,
            env=run.env,
            provider_run_id=run.child.provider_run_id,
            stdin_open=False,
        )
        resumed = _Run(child, run.wall_seconds, clone=run.clone, argv_prefix=run.argv_prefix, env=run.env)
        resumed.thread_id = run.thread_id
        self._runs[child.handle] = resumed
        return child.handle

    def interrupt(self, handle: str, reason: str) -> None:
        """Always raises. There is no protocol interrupt on this surface, and a kill is not one."""
        require_verb(SURFACE, "interrupt")
        raise UnsupportedVerb(SURFACE, "interrupt")

    def read_result(self, handle: str) -> RuntimeResult:
        run = self._runs[handle]
        return decode_result(run.events, provider_run_id=run.child.provider_run_id, provider_turn_id=run.thread_id, killed_reason=run.child.killed_reason)

    def read_provider_record(self, provider_run_id: str) -> RuntimeResult | None:
        """Delegates to the app-server surface: the durable record is the thread, not the stream."""
        return CodexAppServerAdapter(settings=self.settings).read_provider_record(provider_run_id)

    def kill(self, handle: str, reason: str) -> None:
        run = self._runs.get(handle)
        if run is not None:
            run.child.kill_group(reason)

    def cleanup(self, handle: str) -> None:
        run = self._runs.pop(handle, None)
        if run is not None and run.child.running:
            run.child.kill_group("adapter_cleanup")
