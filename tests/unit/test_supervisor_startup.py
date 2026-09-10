"""G6(b), G6(c) and G-SUP: the supervisor is separate, and it refuses before it starts anything.

The two structural assertions here are worth stating plainly, because they are what stops the
separation rotting back into the manager:

* **No backend import anywhere under ``services/tce_supervisor/``.**  An ``ast`` walk, not a grep,
  so a name inside a string or a comment cannot make it pass and a conditional import cannot make
  it fail silently.  ``sqlalchemy`` is in the same list as the three service packages: a supervisor
  that could open the enforcement database would be a supervisor that could rewrite its own
  authority.
* **The supervisor's token is not the manager's, and not the capture channel's.**  The check runs
  before the first request is built and it scrubs those variables out of the process environment
  whether or not it finds a collision, so a spawned child cannot inherit them either.
"""

from __future__ import annotations

import ast
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from tce_shared.charter import CharterCaps, ResolvedCharter
from tce_shared.runtime_contract import RuntimeDescriptor, RuntimeResult, StartRequest, VerbSupport
from tce_supervisor import sandbox, supervise
from tce_supervisor import taskdir as taskdir_mod
from tce_supervisor.apiclient import ApiClient, CredentialCollision, assert_supervisor_credential_is_distinct, is_forbidden_path
from tce_supervisor.config import SupervisorSettings

SUPERVISOR_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "services", "tce_supervisor")
FORBIDDEN_IMPORTS = {"tce_api", "tce_lite_api", "tce_worker", "sqlalchemy"}


def _python_files(root: str) -> list[str]:
    found: list[str] = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"build", "dist", "__pycache__", ".venv"} and not d.endswith(".egg-info")]
        found.extend(os.path.join(base, name) for name in names if name.endswith(".py"))
    return sorted(found)


def test_supervisor_imports_no_backend() -> None:
    assert os.path.isdir(SUPERVISOR_ROOT), SUPERVISOR_ROOT
    files = _python_files(SUPERVISOR_ROOT)
    assert files, "the AST walk found no python files; the assertion would be vacuous"
    offenders: list[str] = []
    for path in files:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".", 1)[0] in FORBIDDEN_IMPORTS:
                    offenders.append(f"{os.path.relpath(path, SUPERVISOR_ROOT)}:{node.lineno} imports {name}")
    assert offenders == [], offenders


def test_supervisor_package_declares_no_database_dependency() -> None:
    """Parsed, not grepped: the file's own comment names those packages to explain their absence."""
    import tomllib

    with open(os.path.join(SUPERVISOR_ROOT, "pyproject.toml"), "rb") as handle:
        manifest = tomllib.load(handle)
    declared = [str(item).lower() for item in manifest["project"]["dependencies"]]
    assert declared, "no dependencies declared; the assertion would be vacuous"
    for banned in ("sqlalchemy", "psycopg", "redis", "alembic"):
        assert not [item for item in declared if banned in item], (banned, declared)


def test_supervisor_token_is_not_the_manager_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TCE_API_TOKENS", "manager-token,other-token")
    monkeypatch.setenv("TCE_HOST_CAPTURE_TOKENS", "capture-token")
    with pytest.raises(CredentialCollision) as caught:
        assert_supervisor_credential_is_distinct("manager-token")
    assert caught.value.key == "TCE_API_TOKENS"

    monkeypatch.setenv("TCE_API_TOKENS", "manager-token")
    monkeypatch.setenv("TCE_HOST_CAPTURE_TOKENS", "capture-token")
    with pytest.raises(CredentialCollision):
        assert_supervisor_credential_is_distinct("capture-token")


def test_the_forbidden_variables_are_scrubbed_even_when_there_is_no_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scrub is not conditional on finding a collision: a child must never inherit these."""
    monkeypatch.setenv("TCE_API_TOKENS", "manager-token")
    monkeypatch.setenv("TCE_HOST_CAPTURE_TOKENS", "capture-token")
    monkeypatch.setenv("TCE_HOST_CAPTURE_TOKEN", "capture-token")
    assert_supervisor_credential_is_distinct("supervisor-token")
    for key in ("TCE_API_TOKENS", "TCE_HOST_CAPTURE_TOKENS", "TCE_HOST_CAPTURE_TOKEN"):
        assert key not in os.environ


def test_forbidden_paths_cover_host_and_manager_surfaces() -> None:
    for path in ("/v1/inputs", "/v1/inputs/abc", "/v1/dashboard/summary", "/v1/system/health", "/v1/runtime/mode"):
        assert is_forbidden_path(path), path
    for path in ("/v1/charters/active", "/v1/effects", "/v1/takeover/execution/claim"):
        assert not is_forbidden_path(path), path


# ---------------------------------------------------------------------------------------------
# G6(b) and G6(c): refusal without a charter, and the order of the first three steps.
# ---------------------------------------------------------------------------------------------


class RecordingAdapter:
    """An adapter that records whether it was ever asked to start anything."""

    surface = "codex/app-server"
    started: list[StartRequest] = []

    def __init__(self, *, settings: SupervisorSettings) -> None:
        self.settings = settings
        RecordingAdapter.started = []

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(runtime_id="codex", runtime_version="codex-cli 0.153.1", surface=self.surface, model_id="", contract_digest="d", available=True)

    def capabilities(self) -> dict[str, VerbSupport]:
        return {}

    def start(self, request: StartRequest) -> str:
        RecordingAdapter.started.append(request)
        return "handle-1"

    def observe(self, handle: str) -> Any:
        return iter(())

    def resume(self, handle: str, prompt: str) -> str:
        return handle

    def interrupt(self, handle: str, reason: str) -> None:
        return None

    def read_result(self, handle: str) -> RuntimeResult:
        return RuntimeResult("succeeded", "turn_completed", False, "", "run-1", "thread-1", 0, 0, 0, 0, None, "unavailable")

    def read_provider_record(self, provider_run_id: str) -> RuntimeResult | None:
        return None

    def kill(self, handle: str, reason: str) -> None:
        return None


class FakeApi:
    """Only the methods dispatch_once actually calls, each recording that it was called."""

    def __init__(self, *, charter: ResolvedCharter | None) -> None:
        self.charter = charter
        self.calls: list[str] = []
        self.session_id = "session-1"

    def list_open_effects(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append("list_open_effects")
        return []

    def get_active_charter(self) -> ResolvedCharter | None:
        self.calls.append("get_active_charter")
        return self.charter

    def post_self_test(self, payload: Any) -> dict[str, Any]:
        self.calls.append("post_self_test")
        return {"self_test_id": "st-1"}


def _charter(*, active: bool = True) -> ResolvedCharter:
    now = datetime.now(UTC)
    return ResolvedCharter(
        charter_id="charter-1",
        workspace_id="personal",
        owner_id="owner",
        project_id=None,
        charter_version="v1",
        policy_revision="p3-2026-09",
        status="active" if active else "revoked",
        enforcement_tier="os_sandbox",
        permitted_roots=("src/",),
        protected_write_prefixes=("tests/", ".github/"),
        denied_read_paths=("~/.ssh",),
        permitted_capabilities=frozenset({"filesystem.write", "process.execute"}),
        confirm_required_capabilities=frozenset(),
        egress_mode="https_only",
        runtime_allowlist=(("codex", "codex-cli 0.153.1", "codex/app-server"),),
        caps=CharterCaps(1, 1, 900, 0, "USD", "unsupported"),
        approved_by="owner",
        approved_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=None if active else now,
        narrowing_ids=(),
        charter_digest="",
        credential_risk_acknowledged=True,
    )


@pytest.fixture(autouse=True)
def _fresh_process_state() -> Any:
    supervise.reset_process_state()
    yield
    supervise.reset_process_state()


def test_dispatch_refuses_without_an_active_charter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """G6(b): no charter, no dispatch — and the adapter's ``start`` is never reached."""
    api = FakeApi(charter=None)
    settings = SupervisorSettings(task_root=str(tmp_path))
    monkeypatch.setattr(supervise, "build_adapter", lambda surface, *, settings: RecordingAdapter(settings=settings))
    RecordingAdapter.started = []

    with pytest.raises(supervise.SupervisorRefusal) as caught:
        supervise.dispatch_once(directive_id="d-1", surface=None, settings=settings, api=cast(ApiClient, api))
    assert caught.value.reason == "no_active_charter"
    assert RecordingAdapter.started == []
    # And nothing was rendered or measured: the refusal is before the sandbox exists.
    assert "post_self_test" not in api.calls


def test_a_revoked_charter_is_the_same_refusal(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi(charter=_charter(active=False))
    settings = SupervisorSettings(task_root=str(tmp_path))
    monkeypatch.setattr(supervise, "build_adapter", lambda surface, *, settings: RecordingAdapter(settings=settings))
    RecordingAdapter.started = []
    with pytest.raises(supervise.SupervisorRefusal) as caught:
        supervise.dispatch_once(directive_id="d-1", surface=None, settings=settings, api=cast(ApiClient, api))
    assert caught.value.reason == "no_active_charter"
    assert RecordingAdapter.started == []


def test_reconcile_runs_before_first_dispatch(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """G6(c): the recorded order begins reconcile, charter, self-test — and start is after all three.

    The run is stopped at the self-test by making the measurement fail, which is the point: even a
    refusal must have done the reconcile and the charter resolution first.
    """
    api = FakeApi(charter=_charter())
    settings = SupervisorSettings(task_root=str(tmp_path))
    monkeypatch.setattr(supervise, "build_adapter", lambda surface, *, settings: RecordingAdapter(settings=settings))

    def _provision(*, task_root: str, directive_id: str, repo_path: str, git_binary: str) -> Any:
        """Stand in for the real clone. Provisioning now precedes the self-test, deliberately: the
        measurement has to be of the profile this dispatch will actually run under."""
        planned = taskdir_mod.plan(task_root=task_root, directive_id=directive_id)
        for path in (planned.root, planned.clone, planned.home, planned.tmp, planned.codex_home, planned.logs):
            os.makedirs(path, exist_ok=True)
        return planned

    monkeypatch.setattr(supervise.taskdir_mod, "provision", _provision)
    monkeypatch.setattr(
        sandbox,
        "run_self_test",
        lambda charter, *, settings, taskdir=None: {
            "passed": False,
            "self_test_id": "st-1",
            "assertions": [{"name": name, "passed": False, "vacuous": True} for name in sandbox.SELF_TEST_ASSERTION_NAMES],
        },
    )
    RecordingAdapter.started = []
    recorder: list[str] = []

    with pytest.raises(sandbox.SandboxUnavailable):
        supervise.dispatch_once(directive_id="d-1", surface=None, settings=settings, api=cast(ApiClient, api), recorder=recorder)

    assert recorder[:3] == ["startup_reconcile", "get_active_charter", "sandbox_self_test"]
    assert "start" not in recorder
    assert RecordingAdapter.started == []
    assert api.calls[0] == "list_open_effects"
