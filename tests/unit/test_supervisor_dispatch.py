"""What the dispatcher must never do, asserted one property per test.

Most of these are negative assertions, and that is deliberate: the failures they guard against are
all of the shape "the code did something reasonable-looking that quietly removed a control".

* the child environment carries no credential (an inherited ``SSH_AUTH_SOCK`` makes "review before
  push" decoration);
* no adapter ever backgrounds its child (a backgrounded agent outlives the supervisor that is
  supposed to be able to stop it);
* the supervisor never computes a budget reservation (two producers in two processes means the
  concurrency cap holds in neither);
* a kill resolves to ``unknown``, never ``failed``;
* the profile digest is re-verified before *both* the adapter start and the verifier run;
* the codex surfaces are dispatched with ``-s danger-full-access`` — because nesting Seatbelt is
  refused in both directions, so an inner sandbox breaks every command while the outer profile,
  the measured one, is the real boundary;
* ``codex app-server`` is not passed ``--ignore-user-config``, which does not exist there;
* ``claude/stream`` keeps stdin open, because closing it removes both the prompt and the interrupt.
"""

from __future__ import annotations

import ast
import os
from typing import Any

import pytest
from tce_shared.runtime_contract import StartRequest, UnsupportedVerb
from tce_supervisor import supervise
from tce_supervisor import taskdir as taskdir_mod
from tce_supervisor.adapters import ADAPTERS, build_adapter
from tce_supervisor.adapters.claude_oneshot import ClaudeOneshotAdapter
from tce_supervisor.adapters.claude_stream import ClaudeStreamAdapter
from tce_supervisor.adapters.codex_app_server import CodexAppServerAdapter, decode_result
from tce_supervisor.adapters.codex_exec import CodexExecAdapter
from tce_supervisor.apiclient import ApiClient
from tce_supervisor.config import SupervisorSettings
from tce_supervisor.taskdir import TaskDir

SUPERVISOR_PACKAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services",
    "tce_supervisor",
    "tce_supervisor",
)


def _taskdir(root: str) -> TaskDir:
    return TaskDir(
        root=root,
        clone=os.path.join(root, "clone"),
        home=os.path.join(root, "home"),
        tmp=os.path.join(root, "tmp"),
        codex_home=os.path.join(root, "codex-home"),
        logs=os.path.join(root, "logs"),
        child_env_file=os.path.join(root, "child.env"),
    )


def _start_request(root: str, *, argv_prefix: tuple[str, ...] = ()) -> StartRequest:
    return StartRequest(
        task_dir=root,
        prompt="do the thing",
        provider_run_id="run-1",
        env={"PATH": "/usr/bin:/bin"},
        argv_prefix=argv_prefix,
        max_budget_minor_units=200,
        wall_seconds=60,
    )


# ---------------------------------------------------------------------------------------------
# the child environment
# ---------------------------------------------------------------------------------------------


def test_child_env_carries_no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A fresh dict, never ``os.environ.copy()`` — asserted against a poisoned environment."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("TCE_API_TOKEN", "manager-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    monkeypatch.setenv("MY_DEPLOY_SECRET", "x")

    charter = _charter()
    env = taskdir_mod.child_environment(_taskdir(str(tmp_path)), charter)
    assert taskdir_mod.credential_shaped_keys(env) == []
    for banned in ("SSH_AUTH_SOCK", "TCE_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MY_DEPLOY_SECRET"):
        assert banned not in env
    # The mandatory entries, each of which was measured to be load-bearing.
    for required in ("PATH", "HOME", "TMPDIR", "CODEX_HOME", "PYTHONPYCACHEPREFIX", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        assert required in env, required
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_write_child_env_file_refuses_a_credential(tmp_path: Any) -> None:
    taskdir = _taskdir(str(tmp_path))
    os.makedirs(taskdir.root, exist_ok=True)
    with pytest.raises(taskdir_mod.TaskDirUnavailable) as caught:
        taskdir_mod.write_child_env_file(taskdir, {"PATH": "/usr/bin", "GITHUB_TOKEN": "x"})
    assert caught.value.reason == "credential_in_child_env"


# ---------------------------------------------------------------------------------------------
# argv properties
# ---------------------------------------------------------------------------------------------


def _settings() -> SupervisorSettings:
    return SupervisorSettings(codex_binary="/bin/codex", claude_binary="/bin/claude", task_root="/private/tmp/tce-tasks")


def test_argv_never_backgrounds(tmp_path: Any) -> None:
    settings = _settings()
    request = _start_request(str(tmp_path))
    argvs = [
        CodexAppServerAdapter(settings=settings).build_argv(()),
        CodexExecAdapter(settings=settings).build_argv((), clone=str(tmp_path), prompt="p"),
        CodexExecAdapter(settings=settings).build_argv((), clone=str(tmp_path), prompt="p", resume_thread_id="t1"),
        ClaudeStreamAdapter(settings=settings).build_argv((), request=request),
        ClaudeOneshotAdapter(settings=settings).build_argv((), request=request),
    ]
    for argv in argvs:
        assert "--bg" not in argv, argv
        assert "--background" not in argv, argv


def test_adapters_are_dispatched_with_danger_full_access(tmp_path: Any) -> None:
    """The outer profile is the boundary; an inner sandbox breaks every command the agent runs."""
    settings = _settings()
    exec_argv = CodexExecAdapter(settings=settings).build_argv((), clone=str(tmp_path), prompt="p")
    assert "-s" in exec_argv and exec_argv[exec_argv.index("-s") + 1] == "danger-full-access"
    assert "workspace-write" not in exec_argv
    for argv in (exec_argv, CodexAppServerAdapter(settings=settings).build_argv(())):
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_app_server_argv_has_no_ignore_user_config() -> None:
    """Measured: ``codex app-server --ignore-user-config`` exits 2. The flag is exec-only."""
    argv = CodexAppServerAdapter(settings=_settings()).build_argv(("/usr/bin/sandbox-exec", "-f", "/p.sb"))
    assert "--ignore-user-config" not in argv
    assert argv[:3] == ("/usr/bin/sandbox-exec", "-f", "/p.sb")
    assert argv[-2:] == ("/bin/codex", "app-server")
    # It IS valid on codex exec, and it is passed there.
    assert "--ignore-user-config" in CodexExecAdapter(settings=_settings()).build_argv((), clone="/c", prompt="p")


def test_codex_exec_resume_puts_flags_before_the_subcommand() -> None:
    argv = CodexExecAdapter(settings=_settings()).build_argv((), clone="/c", prompt="go on", resume_thread_id="thread-9")
    assert "resume" in argv
    resume_at = argv.index("resume")
    assert argv.index("-s") < resume_at, "flags must precede the resume subcommand"
    assert argv[resume_at + 1] == "thread-9"
    assert argv[-1] == "go on"


def test_claude_stream_keeps_stdin_open(tmp_path: Any) -> None:
    """Both ``--input-format stream-json`` and a closed stdin produce a silent no-op."""
    request = _start_request(str(tmp_path))
    stream = ClaudeStreamAdapter(settings=_settings()).build_argv((), request=request)
    assert "--input-format" in stream and stream[stream.index("--input-format") + 1] == "stream-json"
    assert ClaudeStreamAdapter._oneshot is False
    oneshot = ClaudeOneshotAdapter(settings=_settings()).build_argv((), request=request)
    assert "--input-format" not in oneshot
    assert ClaudeOneshotAdapter._oneshot is True
    for argv in (stream, oneshot):
        assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert "--strict-mcp-config" in argv
        assert "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == "project"


def test_claude_surfaces_are_unavailable_without_a_credential() -> None:
    """D-6: a bare spawn is unauthenticated, and the honest answer is available=False."""
    descriptor = ClaudeStreamAdapter(settings=SupervisorSettings(claude_binary="")).describe()
    assert descriptor.available is False
    assert "TCE_SUP_CLAUDE_BINARY" in descriptor.unavailable_reason


# ---------------------------------------------------------------------------------------------
# refusals and the kill policy
# ---------------------------------------------------------------------------------------------


def test_interrupt_refusal_is_journalled(tmp_path: Any) -> None:
    """A surface with no interrupt refuses AND records the refusal, rather than reaching for kill."""

    class RecordingApi:
        def __init__(self) -> None:
            self.effects: list[dict[str, Any]] = []
            self.session_id = "s"

        def open_effect(self, request: dict[str, Any]) -> dict[str, Any]:
            self.effects.append(request)
            return {"effect_id": "e-1"}

        def cancel(self, directive_id: str, *, reason: str) -> dict[str, Any]:
            raise AssertionError("cancel must not be reached on an unsupported surface")

    api = RecordingApi()
    settings = SupervisorSettings(default_runtime_surface="codex/exec", task_root=str(tmp_path))
    with pytest.raises(supervise.SupervisorRefusal) as caught:
        supervise.interrupt_once(directive_id="d-1", reason="operator", settings=settings, api=api)  # type: ignore[arg-type]
    assert caught.value.reason == "unsupported_verb"
    assert api.effects, "the refusal must be evidence, not a log line"
    assert api.effects[0]["directive_id"] == "d-1"
    assert "interrupt" in str(api.effects[0])


def test_the_two_surfaces_without_interrupt_actually_raise(tmp_path: Any) -> None:
    for adapter in (CodexExecAdapter(settings=_settings()), ClaudeOneshotAdapter(settings=_settings())):
        with pytest.raises(UnsupportedVerb) as caught:
            adapter.interrupt("handle", "reason")
        assert adapter.surface in str(caught.value)
        assert "interrupt" in str(caught.value)


def test_kill_resolves_the_root_effect_to_unknown_not_failed() -> None:
    """S18: we killed it, we do not know what it did. ``failed`` would be a claim we cannot make."""
    result = decode_result([], provider_run_id="run-1", provider_turn_id="t-1", killed_reason="wall_clock_exceeded")
    assert result.outcome == "unknown"
    assert result.terminal_reason == "wall_clock_exceeded"

    # And the dispatcher maps that outcome onto the journal the same way.
    source = ast.get_source_segment(open(os.path.join(SUPERVISOR_PACKAGE, "supervise.py"), encoding="utf-8").read(), _dispatch_once_node())
    assert source is not None
    assert 'root_state, resolution_source = "unknown", "reaper"' in source
    assert '"failed", "reaper"' not in source


def _dispatch_once_node() -> ast.AST:
    with open(os.path.join(SUPERVISOR_PACKAGE, "supervise.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_once":
            return node
    raise AssertionError("dispatch_once not found")


def test_supervisor_never_calls_budget_reserve() -> None:
    """C9/G10: one producer for the reservation, and it is not this process.

    Two producers in two processes means the concurrency property — that two dispatches cannot both
    fit under the same cap — holds in neither.
    """
    offenders: list[str] = []
    for base, dirs, names in os.walk(SUPERVISOR_PACKAGE):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "contracts", "profiles"}]
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "reserve":
                    offenders.append(f"{path}:{node.lineno}")
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("budget"):
                    offenders.extend(f"{path}:{node.lineno} imports reserve" for alias in node.names if alias.name == "reserve")
    assert offenders == [], offenders
    # And the reconciliation, which IS this process's job, is present.
    with open(os.path.join(SUPERVISOR_PACKAGE, "supervise.py"), encoding="utf-8") as handle:
        assert "budget.reconcile(" in handle.read()


def test_verify_profile_is_called_before_every_exec() -> None:
    """F10: one wired call site and one unwired one is the same as none.

    Asserted on the source of ``dispatch_once`` rather than at runtime, because the property is
    about the *order of the call sites* — a runtime assertion would pass on a run that never
    reached the verifier.
    """
    body = open(os.path.join(SUPERVISOR_PACKAGE, "supervise.py"), encoding="utf-8").read()
    source = ast.get_source_segment(body, _dispatch_once_node())
    assert source is not None
    lines = source.splitlines()
    verify_lines = [index for index, line in enumerate(lines) if "sandbox.verify_profile(rendered)" in line]
    start_line = next(index for index, line in enumerate(lines) if "adapter.start(" in line)
    verifier_line = next(index for index, line in enumerate(lines) if "verifier.run(" in line)
    assert len(verify_lines) >= 2, "verify_profile must be called before the start AND before the verifier"
    assert any(index < start_line for index in verify_lines), "no verify_profile before adapter.start"
    assert any(start_line < index < verifier_line for index in verify_lines), "no verify_profile between the start and the verifier"
    # And the resume path re-verifies too: a resume re-uses the same task directory.
    resume_source = ast.get_source_segment(body, _named_function("resume_once"))
    assert resume_source is not None and "sandbox.verify_profile(" in resume_source


def _named_function(name: str) -> ast.AST:
    with open(os.path.join(SUPERVISOR_PACKAGE, "supervise.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_fallback_requires_a_recorded_failure() -> None:
    """There is no implicit fallback between surfaces — selection is explicit, or it is nothing."""
    body = open(os.path.join(SUPERVISOR_PACKAGE, "supervise.py"), encoding="utf-8").read()
    source = ast.get_source_segment(body, _dispatch_once_node())
    assert source is not None
    # The only surface the dispatcher may choose is the one it was given or the configured default.
    assert "surface or settings.default_runtime_surface" in source
    for surface in ADAPTERS:
        assert f'"{surface}"' not in source, f"dispatch_once must not name {surface} literally"


def test_apiclient_retries_get_only() -> None:
    """A retried POST is a duplicate dispatch — a second agent process against one directive.

    Asserted on the live adapter the session actually mounts, not on the source: the MCP client this
    one is modelled on retries POST and PUT, and copying that line is exactly the mistake this test
    exists to catch.
    """
    from requests.adapters import HTTPAdapter

    client = ApiClient(SupervisorSettings(api_token="supervisor-token", api_base_url="http://127.0.0.1:18080"))
    for scheme in ("http://", "https://"):
        mounted = client.session.get_adapter(f"{scheme}example.invalid")
        assert isinstance(mounted, HTTPAdapter)
        retries = mounted.max_retries
        assert retries.allowed_methods == frozenset({"GET"}), retries.allowed_methods
        assert retries.total == 3


def test_adapters_cover_exactly_the_declared_surfaces() -> None:
    from tce_shared.runtime_contract import SURFACES, RuntimeAdapter

    assert set(ADAPTERS) == set(SURFACES)
    settings = _settings()
    for surface in SURFACES:
        adapter = build_adapter(surface, settings=settings)
        # isinstance, NOT issubclass: the protocol has a non-method member, so issubclass raises.
        assert isinstance(adapter, RuntimeAdapter), surface
        assert adapter.surface == surface


def _charter() -> Any:
    from datetime import UTC, datetime, timedelta

    from tce_shared.charter import CharterCaps, ResolvedCharter

    now = datetime.now(UTC)
    return ResolvedCharter(
        charter_id="c",
        workspace_id="w",
        owner_id="o",
        project_id=None,
        charter_version="v1",
        policy_revision="p3-2026-09",
        status="active",
        enforcement_tier="os_sandbox",
        permitted_roots=("src/",),
        protected_write_prefixes=("tests/",),
        denied_read_paths=("~/.ssh",),
        permitted_capabilities=frozenset({"filesystem.write"}),
        confirm_required_capabilities=frozenset(),
        egress_mode="https_only",
        runtime_allowlist=(),
        caps=CharterCaps(1, 1, 900, 0, "USD", "unsupported"),
        approved_by="o",
        approved_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=None,
        narrowing_ids=(),
        charter_digest="",
        credential_risk_acknowledged=True,
    )


def _verification_try_node() -> ast.Try:
    """The step-14 ``try`` inside ``dispatch_once`` — the one that POSTs the evidence."""
    for node in ast.walk(_dispatch_once_node()):
        if isinstance(node, ast.Try) and any("api.record_verification" in ast.unparse(stmt) for stmt in node.body):
            return node
    raise AssertionError("the verification try block was not found in dispatch_once")


def test_an_unbound_verifier_credential_does_not_abort_the_turn() -> None:
    """F1's consequence, handled rather than left to propagate.

    ``POST /v1/verification/results`` now derives the runner principal from a server-bound claim
    only, so an absent or unbound ``TCE_SUP_VERIFIER_TOKEN`` answers ``403 unknown_runner`` where
    it used to record an ``inconclusive`` verdict. Uncaught, that refusal escapes step 14 — after
    step 13 has already reported the execution and before step 15 reconciles the spend, which is
    the one place the supervisor owes a write it cannot skip. So the handler must exist, must be
    narrow, and must re-raise everything else.
    """
    handlers = [handler for handler in _verification_try_node().handlers if handler.type is not None and "ApiRefusal" in ast.unparse(handler.type)]
    assert handlers, "record_verification's ApiRefusal is unhandled: a 403 would abort before the spend reconciles"
    body = ast.unparse(handlers[0])

    # Narrow: only the identity refusal is absorbed.
    assert "403" in body and "unknown_runner" in body
    # Everything else still propagates — a bare `raise`, not a swallow.
    assert any(isinstance(node, ast.Raise) and node.exc is None for node in ast.walk(handlers[0])), (
        "the handler must re-raise refusals that are not 403 unknown_runner"
    )
    # And the absorbed case is recorded as skipped, never as a pass.
    assert "'skipped': True" in body
    assert "passed" not in body
