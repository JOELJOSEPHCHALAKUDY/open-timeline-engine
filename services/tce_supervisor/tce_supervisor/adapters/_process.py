"""Child-process lifecycle shared by every adapter.

Two properties here are the reason this is one module rather than four copies:

* **The child gets an environment dict and nothing else.**  ``Popen(env=...)`` with a complete dict
  replaces the environment rather than extending it, so nothing the supervisor holds — an API
  token, ``SSH_AUTH_SOCK``, a vendor API key — reaches the agent by inheritance.  :func:`spawn`
  refuses a credential-shaped key rather than trusting the caller to have built the dict correctly.
* **The child is its own process group.**  ``start_new_session=True`` means the sandboxed subtree
  goes with the parent on ``kill``, which is what makes the wall-clock cap enforceable at all on
  the two surfaces that have no protocol interrupt.

``kill`` is never a way to *simulate* an interrupt.  A killed run's root effect resolves to
``unknown``, never to ``failed`` — we killed it, we do not know what it did.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence

from ..taskdir import credential_shaped_keys

_SENTINEL = "\x00__tce_eof__"


class ChildSpawnRefused(RuntimeError):
    """The child was not spawned, and the reason names which precondition failed."""

    reason: str

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class ChildProcess:
    """One spawned runtime, its stdout pump, and the events decoded from it."""

    def __init__(self, handle: str, proc: subprocess.Popen[bytes], argv: Sequence[str], provider_run_id: str) -> None:
        self.handle = handle
        self.proc = proc
        self.argv = tuple(str(item) for item in argv)
        self.provider_run_id = provider_run_id
        self.provider_turn_id: str | None = None
        self.started_at = time.monotonic()
        self.raw_lines: list[str] = []
        self.stderr_tail: list[str] = []
        self.killed_reason: str | None = None
        self.interrupted_reason: str | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._eof = threading.Event()
        self._stdout_thread = threading.Thread(target=self._pump_stdout, name=f"tce-sup-stdout-{handle[:8]}", daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, name=f"tce-sup-stderr-{handle[:8]}", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    # -- pumps -------------------------------------------------------------------------------

    def _pump_stdout(self) -> None:
        stream = self.proc.stdout
        if stream is not None:
            for raw in stream:
                self._queue.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        self._eof.set()
        self._queue.put(_SENTINEL)

    def _pump_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        for raw in stream:
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if len(self.stderr_tail) < 200:
                self.stderr_tail.append(text)

    # -- io ----------------------------------------------------------------------------------

    def lines(self, *, deadline: float) -> Iterator[str]:
        """Yield stdout lines until EOF or ``deadline`` (a ``time.monotonic()`` value)."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                line = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self._eof.is_set() and self._queue.empty():
                    return
                continue
            if line == _SENTINEL:
                return
            self.raw_lines.append(line)
            yield line

    def write_line(self, payload: str) -> None:
        stream = self.proc.stdin
        if stream is None:
            raise ChildSpawnRefused("stdin_closed", "this surface was spawned without a writable stdin")
        stream.write((payload + "\n").encode("utf-8"))
        stream.flush()

    def close_stdin(self) -> None:
        stream = self.proc.stdin
        if stream is not None and not stream.closed:
            stream.close()

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill_group(self, reason: str) -> None:
        """SIGKILL the child's process group. The caller records ``unknown``, never ``failed``."""
        self.killed_reason = reason
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc.wait(timeout=10)


def spawn(argv: Sequence[str], *, cwd: str, env: Mapping[str, str], provider_run_id: str, stdin_open: bool) -> ChildProcess:
    """Spawn a runtime. No inherited environment, its own process group, refuses credentials."""
    hits = credential_shaped_keys(env)
    if hits:
        raise ChildSpawnRefused("credential_in_child_env", f"the child environment carries {hits}; the adapter passes no inherited credentials")
    if not argv:
        raise ChildSpawnRefused("empty_argv", "no argv to spawn")
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE if stdin_open else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return ChildProcess(handle=uuid.uuid4().hex, proc=proc, argv=argv, provider_run_id=provider_run_id)


def measure_version(binary: str, *, args: Sequence[str] = ("--version",), timeout: float = 20.0) -> str:
    """Run ``<binary> --version`` and return its first line, or "" when it cannot be measured."""
    if not binary or not os.path.isfile(binary):
        return ""
    try:
        completed = subprocess.run([binary, *args], capture_output=True, timeout=timeout, check=False, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (completed.stdout or completed.stderr).decode("utf-8", errors="replace").strip()
    return text.splitlines()[0].strip() if text else ""
