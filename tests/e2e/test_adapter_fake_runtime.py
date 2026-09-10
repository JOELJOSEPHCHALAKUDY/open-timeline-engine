"""Adapter lifecycle against a FAKE executor: a real process, no network, no vendor binary.

The fake in ``tests/fixtures/p3/fake_codex_app_server.py`` speaks the same JSON-RPC on stdout that
the real runtime does.  That gives these tests everything that matters about process handling —
spawn, the stdout pump, exit codes, ``SIGKILL`` of a process group, an orphan — with nothing that
could reach a real credential.

One difference from ``tests/e2e/test_mcp_continuity_live.py``, which this is otherwise modelled on,
is deliberate and is the point of :func:`test_the_child_receives_no_inherited_environment`: that
file spawns with ``env={**os.environ, ...}``.  This one spawns with a constructed dict and asserts
the child received nothing else — because an adapter that inherits the supervisor's environment
hands the agent whatever credentials the supervisor holds, and every downstream control assumes it
did not.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest
from tce_shared.runtime_contract import StartRequest
from tce_supervisor.adapters.codex_app_server import CodexAppServerAdapter
from tce_supervisor.adapters.codex_exec import CodexExecAdapter
from tce_supervisor.config import SupervisorSettings
from tce_supervisor.taskdir import credential_shaped_keys

pytestmark = [pytest.mark.e2e, pytest.mark.subprocess]

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "p3")
FAKE_APP_SERVER = os.path.join(FIXTURES, "fake_codex_app_server.py")
FAKE_EXEC = os.path.join(FIXTURES, "fake_codex_exec.py")


def _taskdir(tmp_path: Any) -> str:
    root = str(tmp_path / "task")
    for name in ("clone", "home", "tmp", "codex-home", "logs"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    return root


def _env(root: str, mode: str = "clean") -> dict[str, str]:
    """The complete child environment: a constructed dict, nothing inherited."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": os.path.join(root, "home"),
        "TMPDIR": os.path.join(root, "tmp"),
        "CODEX_HOME": os.path.join(root, "codex-home"),
        "TCE_FAKE_MODE": mode,
    }


def _request(root: str, *, mode: str = "clean", wall_seconds: int = 30) -> StartRequest:
    return StartRequest(
        task_dir=root,
        prompt="write a file",
        provider_run_id="run-fake-1",
        env=_env(root, mode),
        argv_prefix=(),
        max_budget_minor_units=None,
        wall_seconds=wall_seconds,
    )


def _settings(binary: str) -> SupervisorSettings:
    return SupervisorSettings(codex_binary=binary, codex_version_pin="codex-cli 0.153.1", task_root="/private/tmp/tce-tasks")


def test_a_clean_turn_runs_to_completion(tmp_path: Any) -> None:
    root = _taskdir(tmp_path)
    adapter = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER))
    handle = adapter.start(_request(root))
    kinds = [event.kind for event in adapter.observe(handle)]
    assert kinds[0] == "started"
    assert "command" in kinds and "file_change" in kinds and "approval" in kinds
    assert kinds[-1] == "completed"

    result = adapter.read_result(handle)
    assert result.outcome == "succeeded"
    assert result.provider_turn_id == "01a086d7-6130-7102-8f1c-7ca4629db431"
    assert result.tokens_input == 17540
    adapter.cleanup(handle)


def test_a_failing_turn_is_failed_not_unknown(tmp_path: Any) -> None:
    """``failed`` is a claim we CAN make here: the runtime told us the turn failed."""
    root = _taskdir(tmp_path)
    adapter = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER))
    handle = adapter.start(_request(root, mode="fail"))
    list(adapter.observe(handle))
    result = adapter.read_result(handle)
    assert result.outcome == "failed"
    assert result.is_error is True
    adapter.cleanup(handle)


def test_the_child_receives_no_inherited_environment(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The assertion that matters: a poisoned parent environment reaches the child in no form."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("TCE_API_TOKEN", "manager-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    monkeypatch.setenv("MY_DEPLOY_SECRET", "value")

    root = _taskdir(tmp_path)
    adapter = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER))
    handle = adapter.start(_request(root))
    list(adapter.observe(handle))
    adapter.cleanup(handle)

    with open(os.path.join(root, "clone", "fake_child_env.json"), encoding="utf-8") as file:
        seen = set(json.load(file))

    # Everything we passed arrived.
    assert set(_env(root)) <= seen, sorted(set(_env(root)) - seen)
    # Nothing from the poisoned parent did.
    for banned in ("SSH_AUTH_SOCK", "TCE_API_TOKEN", "ANTHROPIC_API_KEY", "MY_DEPLOY_SECRET"):
        assert banned not in seen
    assert credential_shaped_keys(dict.fromkeys(seen, "")) == []

    # MEASURED, and stated rather than asserted away: the child's environment is NOT byte-for-byte
    # the dict we passed. On macOS the `/usr/bin/env python3` shebang goes through the xcrun shim,
    # which injects SDKROOT, CPATH, LIBRARY_PATH and MANPATH, and CoreFoundation adds LC_CTYPE and
    # __CF_USER_TEXT_ENCODING. None of them is credential-shaped and none comes from the parent
    # environment, so the control holds — but "the child gets exactly what we passed" would be a
    # stronger claim than the operating system actually allows, and this test does not make it.
    unexpected = seen - set(_env(root)) - {"SDKROOT", "CPATH", "LIBRARY_PATH", "MANPATH", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert unexpected == set(), sorted(unexpected)


def test_the_adapter_refuses_to_spawn_with_a_credential_in_the_environment(tmp_path: Any) -> None:
    """Belt and braces: even if a caller builds a bad dict, the spawn refuses."""
    from tce_supervisor.adapters._process import ChildSpawnRefused

    root = _taskdir(tmp_path)
    env = _env(root)
    env["GITHUB_TOKEN"] = "x"
    adapter = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER))
    with pytest.raises(ChildSpawnRefused) as caught:
        adapter.start(
            StartRequest(task_dir=root, prompt="p", provider_run_id="r", env=env, argv_prefix=(), max_budget_minor_units=None, wall_seconds=10)
        )
    assert caught.value.reason == "credential_in_child_env"


def test_a_hang_hits_the_wall_clock_and_the_kill_reports_unknown(tmp_path: Any) -> None:
    """The wall clock is enforced by ``kill`` — and a killed run is ``unknown``, never ``failed``."""
    root = _taskdir(tmp_path)
    adapter = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER))
    handle = adapter.start(_request(root, mode="hang_after_start", wall_seconds=2))
    started = time.monotonic()
    kinds = [event.kind for event in adapter.observe(handle)]
    elapsed = time.monotonic() - started
    assert "completed" not in kinds
    assert elapsed < 30, "observe must stop at the wall clock, not run forever"

    adapter.kill(handle, "wall_clock_exceeded")
    result = adapter.read_result(handle)
    assert result.outcome == "unknown"
    assert result.terminal_reason == "wall_clock_exceeded"
    adapter.cleanup(handle)


def test_kill_takes_the_whole_process_group(tmp_path: Any) -> None:
    """``start_new_session=True`` is what makes the sandboxed subtree go with the parent."""
    root = _taskdir(tmp_path)
    adapter = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER))
    handle = adapter.start(_request(root, mode="hang_after_start", wall_seconds=60))
    session = adapter._sessions[handle]
    pid = session.child.proc.pid
    assert os.getpgid(pid) == pid, "the child must be its own process group leader"
    adapter.kill(handle, "operator")
    assert session.child.proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_a_silent_runtime_is_unknown_rather_than_success(tmp_path: Any) -> None:
    """No terminal event is not a pass. The absence of a verdict is not a verdict."""
    root = _taskdir(tmp_path)
    adapter = CodexExecAdapter(settings=_settings(FAKE_EXEC))
    handle = adapter.start(_request(root, mode="hang", wall_seconds=2))
    list(adapter.observe(handle))
    result = adapter.read_result(handle)
    assert result.outcome == "unknown"
    assert result.terminal_reason == "no_terminal_event"
    adapter.kill(handle, "test_cleanup")
    adapter.cleanup(handle)


def test_codex_exec_runs_the_ndjson_path_for_real(tmp_path: Any) -> None:
    root = _taskdir(tmp_path)
    adapter = CodexExecAdapter(settings=_settings(FAKE_EXEC))
    handle = adapter.start(_request(root))
    kinds = [event.kind for event in adapter.observe(handle)]
    assert kinds[0] == "started" and kinds[-1] == "completed"
    result = adapter.read_result(handle)
    assert result.outcome == "succeeded"
    assert result.provider_turn_id == "01a086d7-6130-7102-8f1c-7ca4629db431"
    adapter.cleanup(handle)


def test_describe_measures_the_version_and_refuses_a_mismatch(tmp_path: Any) -> None:
    """D-5: both real binaries live in auto-updating apps, so a bump must be a refusal."""
    matching = CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER)).describe()
    assert matching.runtime_version == "codex-cli 0.153.1"
    assert matching.available is True

    mismatched = CodexAppServerAdapter(settings=SupervisorSettings(codex_binary=FAKE_APP_SERVER, codex_version_pin="codex-cli 0.999.0")).describe()
    assert mismatched.available is False
    assert "version mismatch" in mismatched.unavailable_reason

    absent = CodexAppServerAdapter(settings=SupervisorSettings(codex_binary=str(tmp_path / "nope"))).describe()
    assert absent.available is False
    assert "not present" in absent.unavailable_reason


def test_the_contract_drift_probe_writes_nothing_into_the_working_directory(tmp_path: Any) -> None:
    """A vendor binary must never run with the supervisor's working directory.

    Found by this test's absence: ``describe()`` re-runs the schema generator, and without a pinned
    ``cwd`` that generator ran in whatever directory the supervisor was started in — which, during
    a test run, is the repository itself.
    """
    before = set(os.listdir(os.getcwd()))
    CodexAppServerAdapter(settings=_settings(FAKE_APP_SERVER)).measure_contract_drift()
    assert set(os.listdir(os.getcwd())) == before
