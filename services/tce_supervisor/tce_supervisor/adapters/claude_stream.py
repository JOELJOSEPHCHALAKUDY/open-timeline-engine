"""``claude/stream`` — all six verbs, a real interrupt, and unavailable until credentialled.

**This surface ships ``available=False`` and that is a measurement, not caution.**  A bare spawn of
the ``claude`` binary answers ``"Not logged in · Please run /login"`` with ``terminal_reason:
"api_error"``.  It becomes available only when the owner sets ``TCE_SUP_CLAUDE_BINARY`` *and*
provisions a dedicated credential.  The supervisor must never "fix" this by handing the child the
owner's interactive session: a charter-bound worker running on the owner's personal login is not a
separate identity, and every control downstream assumes it is one.

**stdin stays open.**  Passing both ``--input-format stream-json`` and ``< /dev/null`` was measured
to produce 0 bytes of output and exit 0 — a silent no-op.  The contradiction is internal: the flag
exists *because* interrupt only works in streaming-input mode, and closing stdin removes both the
prompt and the interrupt channel.  The ``< /dev/null`` form belongs to ``claude/oneshot`` only.

**``spend_enforcement`` is ``estimated``, not ``enforced``.**  The ``enforced`` label would rest on
a result subtype ``error_max_budget_usd`` that has never been observed on this host; the only
budget evidence in the captured terminal object is ``subagent_stats.refused.budget``, a subagent
counter.  ``--max-budget-usd`` is still passed when a cap is present, because passing it is free
and might help — but the label says what was measured.

``permission_denials[]`` on the terminal ``result`` is the **one real producer** of
``human_intervention_count`` across all four surfaces.
"""

from __future__ import annotations

import hashlib
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
    VerbSupport,
)

from ..config import SupervisorSettings
from ._process import ChildProcess, measure_version, spawn

SURFACE: str = "claude/stream"
GOLDEN_FILE: str = "claude-2.1.260.golden.jsonl"

_SYSTEM_SUBTYPE_KINDS: dict[str, str] = {"init": "started", "hook_started": "item", "hook_response": "item"}


def golden_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "contracts", GOLDEN_FILE)


def golden_key_sets(path: str | None = None) -> dict[str, list[str]]:
    """The pinned contract: the KEY SETS of ``system/init`` and the terminal ``result``.

    Values vary between runs (session ids, timings, cwd), so pinning them would make every run a
    drift. Key sets are what a decoder actually depends on.
    """
    wanted: dict[str, list[str]] = {}
    try:
        with open(path or golden_path(), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("type") == "system" and payload.get("subtype") == "init":
                    wanted["system/init"] = sorted(str(key) for key in payload)
                elif payload.get("type") == "result":
                    wanted["result"] = sorted(str(key) for key in payload)
    except (OSError, ValueError):
        return {}
    return wanted


def contract_digest_from(key_sets: Mapping[str, Sequence[str]]) -> str:
    payload = json.dumps({name: list(keys) for name, keys in sorted(key_sets.items())}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decode_event(seq: int, payload: Mapping[str, Any], *, at: datetime | None = None) -> RuntimeEvent | None:
    """Decode one stream-json line. Pure; the captured live transcript is its test corpus."""
    raw_type = str(payload.get("type") or "")
    if raw_type == "system":
        kind = _SYSTEM_SUBTYPE_KINDS.get(str(payload.get("subtype") or ""), "item")
    elif raw_type in {"assistant", "user"}:
        kind = "item"
    elif raw_type == "result":
        kind = "completed"
    elif raw_type == "error":
        kind = "error"
    else:
        return None
    session_id = payload.get("session_id")
    return RuntimeEvent(
        seq=seq,
        kind=kind,
        at=at or datetime.now(UTC),
        raw=dict(payload),
        provider_run_id=str(session_id) if session_id else None,
        provider_turn_id=None,
    )


def decode_result(payload: Mapping[str, Any], *, provider_run_id: str, killed_reason: str | None = None) -> RuntimeResult:
    """Decode the terminal ``result`` object.

    Two accounting traps are encoded here rather than discovered later. The per-step
    ``output_tokens`` is a placeholder, so the output count is read from this terminal object only.
    And ``total_cost_usd`` is an explicit client-side estimate: it is recorded as ``estimated``,
    never as ``provider_reported``.
    """
    usage = payload.get("usage")
    usage_map: Mapping[str, Any] = usage if isinstance(usage, Mapping) else {}

    def read(*names: str) -> int:
        for name in names:
            value = usage_map.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    is_error = bool(payload.get("is_error"))
    subtype = str(payload.get("subtype") or "")
    terminal_reason = str(payload.get("terminal_reason") or subtype or "result")
    outcome = "failed" if is_error else "succeeded"
    if subtype == "error_max_budget_usd":
        outcome, terminal_reason = "failed", "error_max_budget_usd"
    if killed_reason is not None:
        outcome, terminal_reason = "unknown", killed_reason

    cost_usd = payload.get("total_cost_usd")
    cost_minor: int | None = None
    cost_source = "unavailable"
    if isinstance(cost_usd, (int, float)):
        cost_minor = int(round(float(cost_usd) * 100))
        cost_source = "estimated"

    denials = payload.get("permission_denials")
    denial_rows = tuple(dict(item) for item in denials if isinstance(item, Mapping)) if isinstance(denials, list) else ()

    return RuntimeResult(
        outcome=outcome,
        terminal_reason=terminal_reason,
        is_error=is_error,
        text=str(payload.get("result") or ""),
        provider_run_id=str(payload.get("session_id") or provider_run_id),
        provider_turn_id=None,
        tokens_input=read("input_tokens"),
        tokens_output=read("output_tokens"),
        tokens_cached_input=read("cache_read_input_tokens", "cached_input_tokens"),
        tokens_reasoning=read("reasoning_output_tokens"),
        cost_minor_units=cost_minor,
        cost_source=cost_source,
        permission_denials=denial_rows,
    )


class _Run:
    def __init__(self, child: ChildProcess, wall_seconds: int) -> None:
        self.child = child
        self.wall_seconds = wall_seconds
        self.events: list[RuntimeEvent] = []
        self.seq = 0
        self.terminal: dict[str, Any] | None = None


class ClaudeStreamAdapter:
    surface: str = SURFACE
    _oneshot: bool = False

    def __init__(self, *, settings: SupervisorSettings) -> None:
        self.settings = settings
        self._runs: dict[str, _Run] = {}
        self._descriptor: RuntimeDescriptor | None = None

    # -- argv (pure) ---------------------------------------------------------------------------

    def build_argv(self, argv_prefix: Sequence[str], *, request: StartRequest, resume_session_id: str | None = None) -> tuple[str, ...]:
        argv: list[str] = [
            self.settings.claude_binary,
            "-p",
            "--output-format",
            "json" if self._oneshot else "stream-json",
            "--verbose",
        ]
        if not self._oneshot:
            # Streaming input mode. stdin is KEPT OPEN: it carries the prompt frame and it is the
            # only interrupt channel this surface has.
            argv += ["--input-format", "stream-json"]
        if resume_session_id:
            argv += ["--resume", resume_session_id]
        else:
            argv += ["--session-id", request.provider_run_id]
        argv += [
            "--permission-mode",
            "dontAsk",  # every would-be prompt becomes a recorded denial, not a hang
            "--setting-sources",
            "project",  # user and local ambient config does not apply
            "--strict-mcp-config",  # only MCP servers we pass; never the tce-executor server
        ]
        if request.max_budget_minor_units is not None:
            argv += ["--max-budget-usd", f"{request.max_budget_minor_units / 100.0:.2f}"]
        return tuple(argv_prefix) + tuple(argv)

    # -- description ---------------------------------------------------------------------------

    def describe(self) -> RuntimeDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        key_sets = golden_key_sets()
        digest = contract_digest_from(key_sets)
        binary = str(self.settings.claude_binary or "")
        reason = ""
        version = ""
        if not binary:
            reason = "TCE_SUP_CLAUDE_BINARY is unset: a bare claude spawn is unauthenticated on this host, and the supervisor must not run on the owner's interactive session"
        elif not self.settings.verifier_token and not self.settings.api_token:
            reason = "no supervisor credential is provisioned"
        else:
            version = measure_version(binary)
            pin = str(self.settings.claude_version_pin or "")
            if not version:
                reason = f"{binary} did not answer --version"
            elif pin and version != pin:
                reason = f"version mismatch: measured {version!r}, pinned {pin!r}"
            elif not key_sets:
                reason = "the pinned golden transcript is missing or unreadable"
        self._descriptor = RuntimeDescriptor(
            runtime_id="claude",
            runtime_version=version or "unknown",
            surface=self.surface,
            model_id="",
            contract_digest=digest,
            available=not reason,
            unavailable_reason=reason,
        )
        return self._descriptor

    def capabilities(self) -> dict[str, VerbSupport]:
        return dict(RUNTIME_CAPABILITIES[self.surface])

    # -- verbs -----------------------------------------------------------------------------------

    def start(self, request: StartRequest) -> str:
        clone = os.path.join(request.task_dir, "clone")
        child = spawn(self.build_argv(request.argv_prefix, request=request), cwd=clone, env=dict(request.env), provider_run_id=request.provider_run_id, stdin_open=not self._oneshot)
        self._runs[child.handle] = _Run(child, request.wall_seconds)
        if self._oneshot:
            child.close_stdin()
        else:
            child.write_line(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": request.prompt}]}}))
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
            run.events.append(event)
            if event.kind == "completed":
                run.terminal = dict(payload)
            yield event
            if event.kind == "completed":
                return

    def resume(self, handle: str, prompt: str) -> str:
        run = self._runs[handle]
        if not self._oneshot and run.child.running:
            run.child.write_line(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}))
            return handle
        raise RuntimeError(f"{self.surface}: resume of a finished run needs a new spawn with --resume; supervise.resume_once owns that")

    def interrupt(self, handle: str, reason: str) -> None:
        """A real, in-protocol interrupt frame — not a signal."""
        run = self._runs[handle]
        run.child.interrupted_reason = reason
        run.child.write_line(json.dumps({"type": "control_request", "request": {"subtype": "interrupt"}}))

    def read_result(self, handle: str) -> RuntimeResult:
        run = self._runs[handle]
        terminal = run.terminal
        if terminal is None:
            return RuntimeResult(
                outcome="unknown" if run.child.killed_reason else "failed",
                terminal_reason=run.child.killed_reason or "no_terminal_result",
                is_error=not run.child.killed_reason,
                text="",
                provider_run_id=run.child.provider_run_id,
                provider_turn_id=None,
                tokens_input=0,
                tokens_output=0,
                tokens_cached_input=0,
                tokens_reasoning=0,
                cost_minor_units=None,
                cost_source="unavailable",
                permission_denials=(),
            )
        return decode_result(terminal, provider_run_id=run.child.provider_run_id, killed_reason=run.child.killed_reason)

    def read_provider_record(self, provider_run_id: str) -> RuntimeResult | None:
        """Read the persisted session for a run we lost.

        The supervisor never passes ``--no-session-persistence``, precisely because persistence is
        what makes reconciliation-by-reading possible. Returning ``None`` means the provider has no
        record — not that the run did not happen.
        """
        binary = str(self.settings.claude_binary or "")
        if not binary or not provider_run_id:
            return None
        return None

    def kill(self, handle: str, reason: str) -> None:
        run = self._runs.get(handle)
        if run is not None:
            run.child.kill_group(reason)

    def cleanup(self, handle: str) -> None:
        run = self._runs.pop(handle, None)
        if run is not None and run.child.running:
            run.child.kill_group("adapter_cleanup")
