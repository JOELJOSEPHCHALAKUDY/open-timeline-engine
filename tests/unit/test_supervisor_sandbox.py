"""The sandbox module's unit half.

The live half — actually running all sixteen assertions against this host's kernel — is
``tests/e2e/test_sandbox_selftest_live.py``.  What is asserted here is the part that must hold on
any machine: the assertion names are frozen, the renderer expands *every* protected prefix and
refuses an empty list, the rendered profile lands outside every task directory, the wrapper carries
no ``-D PROTECTED``, the tier is bound to the argv that actually ran, and a failing **or vacuous**
assertion refuses rather than downgrades.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tce_shared.charter import CharterCaps, CharterInvalid, ResolvedCharter
from tce_supervisor import sandbox
from tce_supervisor.config import SupervisorSettings
from tce_supervisor.taskdir import TaskDir


def _charter(**overrides: Any) -> ResolvedCharter:
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "charter_id": "charter-1",
        "workspace_id": "personal",
        "owner_id": "owner",
        "project_id": None,
        "charter_version": "v1",
        "policy_revision": "p3-2026-09",
        "status": "active",
        "enforcement_tier": "os_sandbox",
        "permitted_roots": ("src/",),
        "protected_write_prefixes": ("tests/", ".github/", ".local/"),
        "denied_read_paths": ("~/.ssh",),
        "permitted_capabilities": frozenset({"filesystem.write", "process.execute"}),
        "confirm_required_capabilities": frozenset(),
        "egress_mode": "https_only",
        "runtime_allowlist": (("codex", "codex-cli 0.153.1", "codex/app-server"),),
        "caps": CharterCaps(1, 1, 900, 0, "USD", "unsupported"),
        "approved_by": "owner",
        "approved_at": now,
        "expires_at": now + timedelta(hours=1),
        "revoked_at": None,
        "narrowing_ids": (),
        "charter_digest": "",
        "credential_risk_acknowledged": True,
    }
    fields.update(overrides)
    return ResolvedCharter(**fields)


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


def _passing_self_test(names: tuple[str, ...] = sandbox.SELF_TEST_ASSERTION_NAMES) -> dict[str, Any]:
    return {"passed": True, "assertions": [{"name": name, "passed": True, "vacuous": False} for name in names]}


def test_selftest_assertion_names_are_frozen() -> None:
    """Sixteen, in order. The list is a contract: the exit-gate probe asserts the count."""
    assert sandbox.SELF_TEST_ASSERTION_NAMES == (
        "write_in_scope",
        "read_in_scope",
        "write_outside_scope_denied",
        "ssh_key_read_denied",
        "ambient_config_read_denied",
        "docker_socket_denied",
        "enforcement_ports_denied",
        "egress_matches_mode",
        "protected_prefixes_denied",
        "nesting_cannot_widen",
        "runtime_starts",
        "verifier_python_runs",
        "bytecode_write_ok",
        "git_commit_in_clone",
        "no_out_of_tree_launch",
        "child_env_clean",
    )
    assert len(sandbox.SELF_TEST_ASSERTION_NAMES) == 16
    assert len(set(sandbox.SELF_TEST_ASSERTION_NAMES)) == 16
    # Every required-assertion list must name assertions that exist.
    for tier, required in sandbox.TIER_REQUIRED_ASSERTIONS.items():
        assert set(required) <= set(sandbox.SELF_TEST_ASSERTION_NAMES), tier


def test_render_expands_every_protected_prefix(tmp_path: Any) -> None:
    task_root = str(tmp_path / "tasks")
    taskdir = str(tmp_path / "tasks" / "d1")
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=taskdir, template_path="")
    body = open(rendered.path, encoding="utf-8").read()
    assert sandbox.PROTECTED_BLOCK_MARKER not in body
    for prefix in ("tests", ".github", ".local"):
        assert f'(deny file-write* (subpath "{os.path.join(taskdir, "clone", prefix)}"))' in body
    # Three prefixes, three clauses. A loop that stopped at the first was the original defect.
    assert body.count("(deny file-write* (subpath ") == 3
    assert rendered.protected_prefixes == ("tests", ".github", ".local")


def test_render_refuses_an_empty_prefix_list(tmp_path: Any) -> None:
    """A zero-clause render silently allows. Measured, and the reason this is a hard refusal."""
    with pytest.raises(CharterInvalid) as caught:
        sandbox.render_profile(_charter(protected_write_prefixes=()), task_root=str(tmp_path), taskdir=str(tmp_path / "d1"), template_path="")
    assert caught.value.field == "protected_write_prefixes"


def test_rendered_profile_is_outside_every_taskdir(tmp_path: Any) -> None:
    """The F10 property: the agent's writable tree must not contain the profile it runs under."""
    task_root = str(tmp_path / "tasks")
    taskdir = os.path.join(task_root, "d1")
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=taskdir, template_path="")
    assert rendered.path.startswith(sandbox.profile_directory(task_root) + os.sep)
    assert not rendered.path.startswith(taskdir + os.sep)
    assert os.path.basename(rendered.path) == f"{rendered.digest}.sb"


def test_verify_profile_detects_a_rewrite(tmp_path: Any) -> None:
    task_root = str(tmp_path / "tasks")
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=os.path.join(task_root, "d1"), template_path="")
    sandbox.verify_profile(rendered)
    os.chmod(rendered.path, 0o600)
    with open(rendered.path, "w", encoding="utf-8") as handle:
        handle.write("(version 1)\n(allow default)\n")
    with pytest.raises(sandbox.ProfileTampered):
        sandbox.verify_profile(rendered)


def test_deny_all_renders_no_egress_lines(tmp_path: Any) -> None:
    task_root = str(tmp_path / "tasks")
    rendered = sandbox.render_profile(_charter(egress_mode="deny_all"), task_root=task_root, taskdir=os.path.join(task_root, "d1"), template_path="")
    body = open(rendered.path, encoding="utf-8").read()
    # No CLAUSE line grants egress. The rendered file still carries the comment explaining why
    # the two lines are absent, which is why this asserts on lines rather than on a substring.
    assert not [line for line in body.splitlines() if line.startswith("(allow network-outbound")]
    assert "(deny network*)" in body


def test_wrapper_argv_passes_every_parameter_and_no_protected_D(tmp_path: Any) -> None:
    """There is no ``-D PROTECTED``: the deny clauses are rendered, not parameterised."""
    task_root = str(tmp_path / "tasks")
    taskdir = _taskdir(os.path.join(task_root, "d1"))
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=taskdir.root, template_path="")
    settings = SupervisorSettings(task_root=task_root, python_root="/opt/py")
    plan = sandbox.wrapper_argv("os_sandbox", rendered, taskdir, _charter(), settings=settings)
    assert plan.argv[0] == "/usr/bin/sandbox-exec"
    assert "PROTECTED" not in " ".join(plan.argv)
    joined = list(plan.argv)
    for name in ("TASKDIR", "TASKROOT", "HOMEDIR", "PYROOT"):
        assert any(item.startswith(f"{name}=") for item in joined), name
    assert joined[-2:] == ["-f", rendered.path]
    assert plan.profile_digest == rendered.digest


def test_wrapper_argv0_is_bound_to_the_tier(tmp_path: Any) -> None:
    """S16: an ``enforcement_tier`` label nothing binds to the argv is a label, not a control."""
    task_root = str(tmp_path / "tasks")
    taskdir = _taskdir(os.path.join(task_root, "d1"))
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=taskdir.root, template_path="")
    good = sandbox.wrapper_argv("os_sandbox", rendered, taskdir, _charter(), settings=SupervisorSettings(task_root=task_root))
    assert sandbox.select_tier(_charter(), _passing_self_test(), good) == "os_sandbox"

    forged = sandbox.WrapperPlan(tier="os_sandbox", argv=("/bin/sh", "-c", "true"), profile_digest=rendered.digest, self_test_id=None)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        sandbox.select_tier(_charter(), _passing_self_test(), forged)
    assert caught.value.failed == ("wrapper_argv0_mismatch",)


def test_failure_refuses_mutating_dispatch(tmp_path: Any) -> None:
    """A failing OR VACUOUS required assertion refuses. Vacuity is not a pass."""
    task_root = str(tmp_path / "tasks")
    taskdir = _taskdir(os.path.join(task_root, "d1"))
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=taskdir.root, template_path="")
    plan = sandbox.wrapper_argv("os_sandbox", rendered, taskdir, _charter(), settings=SupervisorSettings(task_root=task_root))

    failing = _passing_self_test()
    failing["assertions"][5]["passed"] = False
    failing["passed"] = False
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        sandbox.select_tier(_charter(), failing, plan)
    assert "docker_socket_denied" in caught.value.failed

    vacuous = _passing_self_test()
    vacuous["assertions"][3]["vacuous"] = True
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        sandbox.select_tier(_charter(), vacuous, plan)
    assert "ssh_key_read_denied" in caught.value.failed

    missing = {"passed": True, "assertions": [row for row in _passing_self_test()["assertions"] if row["name"] != "child_env_clean"]}
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        sandbox.select_tier(_charter(), missing, plan)
    assert "child_env_clean" in caught.value.failed


def test_select_tier_never_downgrades(tmp_path: Any) -> None:
    """There is no path from a failed os_sandbox measurement to a quieter tier."""
    task_root = str(tmp_path / "tasks")
    taskdir = _taskdir(os.path.join(task_root, "d1"))
    rendered = sandbox.render_profile(_charter(), task_root=task_root, taskdir=taskdir.root, template_path="")
    plan = sandbox.wrapper_argv("os_sandbox", rendered, taskdir, _charter(), settings=SupervisorSettings(task_root=task_root))
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.select_tier(_charter(), {"passed": False, "assertions": []}, plan)


def test_container_argv_binds_every_prefix_readonly(tmp_path: Any) -> None:
    """The read-only bind is the mechanism, not ``chmod``: the container runs as the owner uid."""
    taskdir = _taskdir(str(tmp_path / "d1"))
    settings = SupervisorSettings(task_root=str(tmp_path), container_image="python:3.12-bookworm")
    argv = sandbox.container_argv(taskdir, _charter(), settings=settings, network="none")
    assert argv[0] == settings.docker_binary
    assert ("--network", "none") == (argv[argv.index("--network")], argv[argv.index("--network") + 1])
    for prefix in ("tests", ".github", ".local"):
        assert f"type=bind,src={taskdir.clone}/{prefix},dst=/task/{prefix},readonly" in argv
    for flag in ("--cap-drop", "--security-opt", "--read-only", "--tmpfs", "--pids-limit", "--memory", "--env-file"):
        assert flag in argv
    assert "/var/run/docker.sock" not in " ".join(argv)
    assert os.path.expanduser("~") not in " ".join(argv)


def test_container_argv_refuses_an_empty_prefix_list(tmp_path: Any) -> None:
    taskdir = _taskdir(str(tmp_path / "d1"))
    with pytest.raises(CharterInvalid):
        sandbox.container_argv(taskdir, _charter(protected_write_prefixes=()), settings=SupervisorSettings(task_root=str(tmp_path)), network="none")


def test_advisory_tier_needs_no_measurement() -> None:
    """Tier 3 is honest about being advisory: it wraps nothing, so there is nothing to measure."""
    assert sandbox.TIER_REQUIRED_ASSERTIONS["advisory"] == ()
    plan = sandbox.WrapperPlan(tier="advisory", argv=(), profile_digest=None, self_test_id=None)
    assert sandbox.select_tier(_charter(enforcement_tier="advisory"), {"passed": False, "assertions": []}, plan) == "advisory"


def test_the_shipped_template_is_the_measured_one() -> None:
    """The checked-in template is byte-for-byte the profile the sixteen assertions were run against.

    A drifted template would make every claim in ``docs/charter.md`` about what is enforced a claim
    about a file nobody measured.
    """
    import hashlib

    with open(sandbox.default_template_path(), "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    assert digest == "1597dfcdae2a79b8b1294744bbc11a0a2385f150aca3d88cc957d757de9181ec"
