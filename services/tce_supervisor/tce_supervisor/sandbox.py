"""Profile rendering, the boot-time self-test, and tier selection.

Everything in this module was derived from measurement on this host, not from documentation.  The
three findings that shaped it are worth keeping next to the code:

**The rendered profile is written OUTSIDE every task directory.**  Rendering it to
``<taskdir>/executor.sb`` mode 0400 is not a control: the file is inside the ``file-write*`` allow
root and is owned by the same uid as the agent, so the agent can ``chmod`` it and overwrite it with
``(allow default)`` — after which the next exec on that path *enforces nothing while reporting
itself applied*.  Rendered profiles live at ``<task_root>/.profiles/<sha256>.sb``, a sibling of
every task directory, and :func:`verify_profile` re-hashes before **every** exec: the dispatch, the
resume, the retry and the verifier run.  Wiring it at only one of those reopens the hole.

**Seatbelt is last-match-wins.**  The ``$HOME`` deny therefore comes *first* in the read block, so
that a task root under ``$HOME`` is still readable and every later allow is an explicit,
reviewable carve-out.  With the deny last, ``write_in_scope`` passes and ``read_in_scope`` fails —
a green self-test over a dead dispatch, which is why assertion 2 exists.

**A zero-clause protected-write render silently allows.**  ``(deny file-write*)`` with no subpath
denied nothing in the measured run, and the charter DDL defaults the column to ``'[]'``.
:func:`render_profile` therefore refuses an empty prefix list rather than emitting a profile whose
deny block is decoration.

What this module does NOT claim: the profile was derived by clause-removal bisection, because this
host exposes no sandbox denial telemetry to a non-root session.  That method proves every shipped
clause is load-bearing.  It does **not** prove no clause is missing for a code path the lab never
exercised.  Assertion 11 ``runtime_starts`` is the boot-time guard that catches a missing clause at
boot rather than at dispatch.  And ``sandbox-exec`` is documented DEPRECATED in capitals; it works
today, it is what the runtime itself shells out to, and it is re-measured at every boot — but it is
not a supported API.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tce_shared.charter import CharterInvalid, ResolvedCharter

from .config import SupervisorSettings
from .taskdir import TaskDir, child_environment, credential_shaped_keys

PROFILE_TEMPLATE_NAME: str = "executor_v2.sb"
PROTECTED_BLOCK_MARKER: str = ";;PROTECTED-BLOCK;;"
SELF_TEST_SCHEMA_VERSION: str = "v2"

SELF_TEST_ASSERTION_NAMES: tuple[str, ...] = (
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

TIER_WRAPPER_ARGV0: dict[str, str] = {"os_sandbox": "/usr/bin/sandbox-exec", "container": "docker"}

TIER_REQUIRED_ASSERTIONS: dict[str, tuple[str, ...]] = {
    "os_sandbox": SELF_TEST_ASSERTION_NAMES,
    "container": (
        "write_in_scope",
        "read_in_scope",
        "egress_matches_mode",
        "docker_socket_denied",
        "protected_prefixes_denied",
        "verifier_python_runs",
        "child_env_clean",
    ),
    "advisory": (),
}

# The TCE services an agent must never reach from inside the sandbox.
ENFORCEMENT_PORTS: tuple[int, ...] = (5432, 16379, 18080, 11434)
DOCKER_SOCKET_PATH: str = "/var/run/docker.sock"
SANDBOX_EXEC_BINARY: str = "/usr/bin/sandbox-exec"

_SELF_TEST_DIR_NAME: str = ".selftest"
# Every in-scope write probe lands here, inside the clone the dispatch will use. Confining them to
# one directory is what lets the self-test measure the REAL rendered profile (see run_self_test)
# without scattering probe files through the tree the agent is about to work in.
_SCRATCH_DIR_NAME: str = ".tce-selftest"
_PROFILE_DIR_NAME: str = ".profiles"
_SUBPROCESS_TIMEOUT: float = 30.0
# Seconds to let one runtime launch settle before the next, per attempt. See _handshake: two
# `codex app-server` launches in quick succession interfere and one answers with nothing at all.
# Measured: 2 s is usually enough and sometimes is not, so the settle grows. Worst case ~21 s for
# an assertion that runs once per supervisor boot.
_RUNTIME_SETTLE_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0, 6.0, 8.0)


class SandboxUnavailable(RuntimeError):
    """The requested tier could not be established. Never downgraded silently."""

    tier: str
    failed: tuple[str, ...]

    def __init__(self, tier: str, failed: Sequence[str]) -> None:
        names = ", ".join(str(item) for item in failed) or "(none named)"
        super().__init__(f"sandbox tier {tier!r} is unavailable; failing or vacuous assertions: {names}")
        self.tier = tier
        self.failed = tuple(str(item) for item in failed)


class ProfileTampered(RuntimeError):
    """The rendered profile on disk no longer hashes to what was rendered.

    This is the F10 alarm. It fires between two execs, which is exactly the window in which an
    agent that had chmod'ed and overwritten its own profile would otherwise get a second, unbounded
    run reported as sandboxed.
    """

    def __init__(self, path: str, expected: str, observed: str) -> None:
        super().__init__(f"rendered sandbox profile {path} changed on disk: expected sha256 {expected}, observed {observed}")
        self.path = path
        self.expected = expected
        self.observed = observed


@dataclass(frozen=True, slots=True)
class RenderedProfile:
    path: str  # <task_root>/.profiles/<digest>.sb -- NEVER under any TASKDIR
    digest: str  # sha256 of the rendered bytes; -> dispatch_records.sandbox_profile_digest
    protected_prefixes: tuple[str, ...]
    egress_mode: str  # "deny_all" | "https_only"


@dataclass(frozen=True, slots=True)
class WrapperPlan:
    tier: str
    argv: tuple[str, ...]  # argv[0] is asserted against TIER_WRAPPER_ARGV0
    profile_digest: str | None
    self_test_id: str | None


# ------------------------------------------------------------------------------------------
# rendering
# ------------------------------------------------------------------------------------------


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def default_template_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles", PROFILE_TEMPLATE_NAME)


def profile_directory(task_root: str) -> str:
    """``<task_root>/.profiles`` — a SIBLING of every task directory, never inside one."""
    return os.path.join(os.path.abspath(os.path.expanduser(task_root)), _PROFILE_DIR_NAME)


def _protected_block(clone: str, prefixes: Sequence[str]) -> str:
    lines: list[str] = []
    for prefix in prefixes:
        target = os.path.join(clone, str(prefix).strip("/"))
        lines.append(f'(deny file-write* (subpath "{target}"))')
    return "\n".join(lines)


def render_profile(charter: ResolvedCharter, *, task_root: str, taskdir: str, template_path: str) -> RenderedProfile:
    """Expand the template for one dispatch and write it outside every task directory.

    ``taskdir`` is the task root directory (``TaskDir.root``); the protected-write clauses are
    expanded against ``<taskdir>/clone``.
    """
    prefixes = tuple(str(item).strip("/") for item in charter.protected_write_prefixes if str(item).strip("/"))
    if not prefixes:
        # Measured: a render with zero clauses denies nothing while looking like a deny block.
        raise CharterInvalid("protected_write_prefixes", "must be non-empty: a zero-clause render silently allows every protected path")
    if charter.egress_mode not in {"deny_all", "https_only"}:
        raise CharterInvalid("egress_mode", f"unknown egress mode {charter.egress_mode!r}")

    source = template_path or default_template_path()
    with open(source, encoding="utf-8") as handle:
        template = handle.read()
    if PROTECTED_BLOCK_MARKER not in template:
        raise CharterInvalid("protected_write_prefixes", f"template {source} carries no {PROTECTED_BLOCK_MARKER} marker to expand")

    clone = os.path.join(os.path.abspath(taskdir), "clone")
    body = template.replace(PROTECTED_BLOCK_MARKER, _protected_block(clone, prefixes))

    if charter.egress_mode == "deny_all":
        # The template ships the https_only pair; deny_all renders NEITHER line. Comments are kept
        # so the rendered file still explains why they are absent.
        body = "\n".join(line for line in body.splitlines() if not line.startswith("(allow network-outbound"))
        body += "\n;; egress_mode = deny_all: the two (allow network-outbound ...) lines are NOT rendered.\n"

    encoded = body.encode("utf-8")
    digest = _sha256_bytes(encoded)
    directory = profile_directory(task_root)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{digest}.sb")

    if os.path.abspath(path).startswith(os.path.abspath(taskdir) + os.sep):
        raise CharterInvalid("task_root", "the rendered profile would land inside the task directory the agent can write")

    if not os.path.isfile(path) or _sha256_bytes(open(path, "rb").read()) != digest:
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(encoded)
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)

    return RenderedProfile(path=path, digest=digest, protected_prefixes=prefixes, egress_mode=charter.egress_mode)


def verify_profile(rendered: RenderedProfile) -> None:
    """Re-hash the rendered profile. Call before EVERY exec — dispatch, resume, retry, verifier."""
    try:
        with open(rendered.path, "rb") as handle:
            observed = _sha256_bytes(handle.read())
    except OSError as error:
        raise ProfileTampered(rendered.path, rendered.digest, f"unreadable: {error}") from error
    if observed != rendered.digest:
        raise ProfileTampered(rendered.path, rendered.digest, observed)


# ------------------------------------------------------------------------------------------
# argv
# ------------------------------------------------------------------------------------------


def seatbelt_argv(rendered: RenderedProfile, taskdir: TaskDir, *, settings: SupervisorSettings) -> tuple[str, ...]:
    """The Tier-1 wrapper prefix.

    There is no ``-D PROTECTED``: the deny clauses are RENDERED, one per prefix. The parameterised
    form enforced only ``protected_write_prefixes[0]``, and an empty value was a hard parse error
    on one edge and a silent allow on the other.
    """
    return (
        SANDBOX_EXEC_BINARY,
        "-D",
        f"TASKDIR={taskdir.root}",
        "-D",
        f"TASKROOT={os.path.abspath(os.path.expanduser(settings.task_root))}",
        "-D",
        f"HOMEDIR={os.path.expanduser('~')}",
        "-D",
        f"PYROOT={settings.python_root or os.path.dirname(os.path.dirname(sys.executable))}",
        "-f",
        rendered.path,
    )


def container_argv(taskdir: TaskDir, charter: ResolvedCharter, *, settings: SupervisorSettings, network: str, workdir: str = "/task") -> tuple[str, ...]:
    """The Tier-2 recipe, verbatim from the measured run.

    The read-only bind per protected prefix is the mechanism, **not** ``chmod``: the container runs
    as the manager uid, which owns those directories, so mode bits are the agent's to change. The
    bind is kernel-enforced. Like :func:`render_profile`, this refuses an empty prefix list.
    """
    prefixes = tuple(str(item).strip("/") for item in charter.protected_write_prefixes if str(item).strip("/"))
    if not prefixes:
        raise CharterInvalid("protected_write_prefixes", "must be non-empty: a container with no read-only binds protects nothing")
    argv: list[str] = [
        settings.docker_binary,
        "run",
        "--rm",
        "--network",
        network,
        "--mount",
        f"type=bind,src={taskdir.clone},dst=/task",
    ]
    for prefix in prefixes:
        argv += ["--mount", f"type=bind,src={taskdir.clone}/{prefix},dst=/task/{prefix},readonly"]
    argv += [
        "--workdir",
        workdir,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env-file",
        taskdir.child_env_file,
        "--memory",
        "4g",
        "--pids-limit",
        "512",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m",
        settings.container_image,
    ]
    return tuple(argv)


def wrapper_argv(tier: str, rendered: RenderedProfile | None, taskdir: TaskDir, charter: ResolvedCharter, *, settings: SupervisorSettings, self_test_id: str | None = None) -> WrapperPlan:
    if tier == "advisory":
        return WrapperPlan(tier="advisory", argv=(), profile_digest=None, self_test_id=self_test_id)
    if tier == "os_sandbox":
        if rendered is None:
            raise SandboxUnavailable("os_sandbox", ["no_rendered_profile"])
        return WrapperPlan(tier="os_sandbox", argv=seatbelt_argv(rendered, taskdir, settings=settings), profile_digest=rendered.digest, self_test_id=self_test_id)
    if tier == "container":
        return WrapperPlan(
            tier="container",
            argv=container_argv(taskdir, charter, settings=settings, network=settings.container_network),
            profile_digest=rendered.digest if rendered is not None else None,
            self_test_id=self_test_id,
        )
    raise SandboxUnavailable(tier, ["unknown_tier"])


def _argv0_matches_tier(tier: str, argv: Sequence[str]) -> bool:
    """S16: the tier label must be bound to the argv that actually ran.

    ``TIER_WRAPPER_ARGV0["container"]`` is the bare name ``"docker"`` while ``docker_binary`` is a
    pinned absolute path, so the container comparison is on the basename. The Seatbelt entry is an
    absolute path and matches exactly either way.
    """
    if not argv:
        return False
    expected = TIER_WRAPPER_ARGV0.get(tier)
    if expected is None:
        return False
    head = str(argv[0])
    return head == expected or os.path.basename(head) == os.path.basename(expected)


def select_tier(charter: ResolvedCharter, self_test: Mapping[str, Any], plan: WrapperPlan) -> str:
    """Return the charter's tier, or raise. It NEVER silently downgrades.

    A ``vacuous`` assertion counts as not passed. That is the whole point of recording vacuity: an
    assertion that could not measure what it claims to measure is not evidence, and treating it as
    a pass is how a self-test goes green over a dead sandbox.
    """
    tier = charter.enforcement_tier
    if tier == "advisory":
        return "advisory"
    required = TIER_REQUIRED_ASSERTIONS.get(tier, ())
    assertions = self_test.get("assertions") or []
    seen: set[str] = set()
    failed: list[str] = []
    for entry in assertions:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        seen.add(name)
        if name in required and (not entry.get("passed") or entry.get("vacuous")):
            failed.append(name)
    failed.extend(sorted(name for name in required if name not in seen))
    if failed or not self_test.get("passed"):
        raise SandboxUnavailable(tier, failed or ["self_test_not_passed"])
    if not _argv0_matches_tier(tier, plan.argv):
        raise SandboxUnavailable(tier, ["wrapper_argv0_mismatch"])
    return tier


# ------------------------------------------------------------------------------------------
# the self-test
# ------------------------------------------------------------------------------------------


def _run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = _SUBPROCESS_TIMEOUT,
    stdin_bytes: bytes | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one probe.

    ``cwd`` is not optional in practice for a sandboxed probe: with the working directory left at
    whatever the supervisor was started in — typically a path under the denied ``$HOME`` — the
    runtime dies with "error loading default config after config error: Operation not permitted"
    before it reads a single argument. Measured here, not guessed: the same argv with cwd inside
    the task directory completes the handshake.
    """
    try:
        return subprocess.run(
            list(argv),
            input=stdin_bytes,
            capture_output=True,
            cwd=cwd,
            env=dict(env) if env is not None else {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return subprocess.CompletedProcess(list(argv), 126, b"", str(error).encode())


def _assertion(name: str, expected: str, observed: str, passed: bool, vacuous: bool = False) -> dict[str, Any]:
    return {"name": name, "expected": expected, "observed": observed[:400], "passed": bool(passed) and not vacuous, "vacuous": bool(vacuous)}


def _selftest_taskdir(settings: SupervisorSettings) -> TaskDir:
    root = os.path.join(os.path.abspath(os.path.expanduser(settings.task_root)), _SELF_TEST_DIR_NAME)
    clone = os.path.join(root, "clone")
    taskdir = TaskDir(
        root=root,
        clone=clone,
        home=os.path.join(root, "home"),
        tmp=os.path.join(root, "tmp"),
        codex_home=os.path.join(root, "codex-home"),
        logs=os.path.join(root, "logs"),
        child_env_file=os.path.join(root, "child.env"),
    )
    for path in (taskdir.root, taskdir.clone, taskdir.home, taskdir.tmp, taskdir.codex_home, taskdir.logs, os.path.join(clone, "src"), os.path.join(clone, "pkg")):
        os.makedirs(path, exist_ok=True)
    return taskdir


def _port_open(port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _docker_answers_unsandboxed() -> bool:
    if not os.path.exists(DOCKER_SOCKET_PATH):
        return False
    completed = _run(["/usr/bin/curl", "-s", "--max-time", "5", "--unix-socket", DOCKER_SOCKET_PATH, "http://d/v1.43/version"], timeout=10.0)
    return b"Version" in completed.stdout or b"Platform" in completed.stdout


def run_self_test(charter: ResolvedCharter, *, settings: SupervisorSettings, taskdir: TaskDir | None = None) -> dict[str, Any]:
    """Run every assertion the charter's tier requires, as real sandboxed subprocesses.

    ``passed`` means: every assertion this tier REQUIRES held, and none of them was vacuous. It is
    per-tier on purpose — the container tier cannot measure ``runtime_starts`` or
    ``nesting_cannot_widen``, and reporting those as passes would be a table lookup measuring
    nothing.

    **Pass the REAL task directory when there is one.** The rendered profile's protected-write
    clauses carry absolute paths, so a profile rendered for a throwaway directory has a different
    sha256 from the one the dispatch will actually run under — and measuring a different file than
    the one you run proves nothing about the one you run. ``dispatch_once`` therefore provisions
    first and passes its ``TaskDir`` here, so ``profile_digest`` names the exact file the agent will
    be confined by. The throwaway directory is for the ``selftest`` and ``probe-denials`` verbs,
    which have no dispatch to bind to.
    """
    tier = charter.enforcement_tier
    taskdir = taskdir or _selftest_taskdir(settings)
    rendered: RenderedProfile | None = None
    render_error = ""
    try:
        rendered = render_profile(charter, task_root=settings.task_root, taskdir=taskdir.root, template_path=settings.profile_template_path)
    except (CharterInvalid, OSError) as error:
        render_error = str(error)

    if tier == "container":
        assertions = _container_assertions(charter, taskdir, settings=settings)
    elif rendered is None:
        assertions = [_assertion(name, "measured", f"profile not rendered: {render_error}", False, vacuous=True) for name in SELF_TEST_ASSERTION_NAMES]
    else:
        assertions = _seatbelt_assertions(charter, rendered, taskdir, settings=settings)

    required = TIER_REQUIRED_ASSERTIONS.get(tier, ())
    by_name = {str(entry["name"]): entry for entry in assertions}
    passed = all(bool(by_name.get(name, {}).get("passed")) for name in required)

    return {
        "schema_version": SELF_TEST_SCHEMA_VERSION,
        "self_test_id": str(uuid.uuid4()),
        "sandbox_provider": settings.sandbox_provider,
        "provider_version": _provider_version(settings, tier),
        "enforcement_tier": tier,
        "profile_digest": rendered.digest if rendered is not None else "",
        "required_assertions": list(required),
        # Recorded, never asserted: D-1. This host has no service account (`sudo -n true` wants a
        # password), so the supervisor runs as the same uid as the manager and separation is
        # Seatbelt-only. Reporting it as True would be the claim this whole module refuses to make.
        "uid_separation": False,
        # Recorded, never asserted: D-11. No positive mach-lookup allowlist could be derived,
        # because this host exposes no sandbox denial telemetry to a non-root session.
        "mach_lookup_allowlist": "underived",
        "passed": bool(passed),
        "assertions": assertions,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _provider_version(settings: SupervisorSettings, tier: str) -> str:
    if tier == "container":
        completed = _run([settings.docker_binary, "--version"], timeout=15.0)
        return completed.stdout.decode(errors="replace").strip() or "unknown"
    # sandbox-exec has no --version; the OS build is the only version there is, and the man page
    # says DEPRECATED. Record both so the row says what interface was measured.
    completed = _run(["/usr/bin/sw_vers", "-productVersion"], timeout=10.0)
    return f"sandbox-exec (deprecated) on macOS {completed.stdout.decode(errors='replace').strip() or 'unknown'}"


def _seatbelt_assertions(charter: ResolvedCharter, rendered: RenderedProfile, taskdir: TaskDir, *, settings: SupervisorSettings) -> list[dict[str, Any]]:
    env = child_environment(taskdir, charter)
    prefix = seatbelt_argv(rendered, taskdir, settings=settings)

    def sb(argv: Sequence[str], *, timeout: float = _SUBPROCESS_TIMEOUT, stdin_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return _run(tuple(prefix) + tuple(argv), env=env, timeout=timeout, stdin_bytes=stdin_bytes, cwd=taskdir.clone)

    out: list[dict[str, Any]] = []
    scratch = os.path.join(taskdir.clone, _SCRATCH_DIR_NAME)
    os.makedirs(scratch, exist_ok=True)
    probe = os.path.join(scratch, "selftest_w1.txt")

    # 1 write_in_scope
    if os.path.exists(probe):
        os.unlink(probe)
    sb(["/bin/sh", "-c", f"echo ok > {probe}"])
    wrote = os.path.isfile(probe)
    out.append(_assertion("write_in_scope", f"writes {probe}", "wrote it" if wrote else "write denied", wrote))

    # 2 read_in_scope -- catches the HOMEDIR-ordering trap: with the deny last, 1 passes and 2 fails.
    read_back = sb(["/bin/cat", probe]).stdout.decode(errors="replace").strip()
    out.append(_assertion("read_in_scope", "reads back 'ok'", read_back or "read denied", read_back == "ok", vacuous=not wrote))

    # 3 write_outside_scope_denied
    outside = os.path.expanduser("~/.tce-supervisor-selftest")
    if os.path.exists(outside):
        os.unlink(outside)
    sb(["/usr/bin/touch", outside])
    escaped = os.path.exists(outside)
    if escaped:
        os.unlink(outside)
    out.append(_assertion("write_outside_scope_denied", "denied", f"created {outside}" if escaped else "Operation not permitted", not escaped))

    # 4 ssh_key_read_denied (+ the key must exist unsandboxed, or the assertion measures nothing)
    ssh_dir = os.path.expanduser("~/.ssh")
    keys = sorted(os.path.join(ssh_dir, name) for name in os.listdir(ssh_dir)) if os.path.isdir(ssh_dir) else []
    key = next((k for k in keys if os.path.isfile(k) and os.path.basename(k).startswith("id_")), "")
    if not key:
        out.append(_assertion("ssh_key_read_denied", "denied", "no ssh key on this host", False, vacuous=True))
    else:
        got = sb(["/bin/cat", key])
        leaked = got.returncode == 0 and bool(got.stdout)
        out.append(_assertion("ssh_key_read_denied", "denied", "read it" if leaked else "denied; key exists unsandboxed", not leaked))

    # 5 ambient_config_read_denied (+ the file must exist unsandboxed)
    ambient = os.path.expanduser("~/.codex/config.toml")
    if not os.path.isfile(ambient):
        out.append(_assertion("ambient_config_read_denied", "denied", "~/.codex/config.toml absent", False, vacuous=True))
    else:
        got = sb(["/bin/cat", ambient])
        leaked = got.returncode == 0 and bool(got.stdout)
        out.append(_assertion("ambient_config_read_denied", "denied", "read it" if leaked else "denied; file exists unsandboxed", not leaked))

    # 6 docker_socket_denied -- CONNECTS to the socket. The v1 form observed an EXEC denial on
    # /usr/local/bin/docker while the daemon answered; it would have passed with the socket open.
    unsandboxed = _docker_answers_unsandboxed()
    sandboxed = sb(["/usr/bin/curl", "-s", "--max-time", "5", "--unix-socket", DOCKER_SOCKET_PATH, "http://d/v1.43/version"], timeout=15.0).stdout
    if not unsandboxed:
        out.append(_assertion("docker_socket_denied", "sandboxed silent", "daemon does not answer unsandboxed either", False, vacuous=True))
    else:
        out.append(_assertion("docker_socket_denied", "sandboxed silent", f"answered sandboxed: {sandboxed[:40]!r}" if sandboxed else "unsandboxed answers, sandboxed silent", not sandboxed))

    # 7 enforcement_ports_denied (+ at least one port must be open unsandboxed)
    open_ports = [port for port in ENFORCEMENT_PORTS if _port_open(port)]
    reachable = [port for port in ENFORCEMENT_PORTS if sb(["/usr/bin/nc", "-z", "-G", "2", "127.0.0.1", str(port)], timeout=10.0).returncode == 0]
    if not open_ports:
        out.append(_assertion("enforcement_ports_denied", "0 reachable", "no enforcement port is open unsandboxed", False, vacuous=True))
    else:
        out.append(_assertion("enforcement_ports_denied", "0 reachable", f"{len(open_ports)}/4 open unsandboxed, {len(reachable)}/4 sandboxed", not reachable))

    # 8 egress_matches_mode. Under https_only this assertion PASSES while the credential channel is
    # wide open on 443 -- that is D-10, and it is why a mutating os_sandbox charter must
    # acknowledge the risk. The assertion describes the mode; it does not bless it.
    https = sb(["/usr/bin/curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "12", "https://example.com/"], timeout=20.0).stdout.decode(errors="replace").strip()
    http = sb(["/usr/bin/curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8", "http://example.com/"], timeout=15.0).stdout.decode(errors="replace").strip()
    if charter.egress_mode == "https_only":
        ok = https not in {"", "000"} and http == "000"
    else:
        ok = https in {"", "000"} and http in {"", "000"}
    out.append(_assertion("egress_matches_mode", charter.egress_mode, f"443={https or '000'} 80={http or '000'}", ok))

    # 9 protected_prefixes_denied -- EVERY prefix, plus a sibling control. The parameterised form
    # enforced only prefixes[0]; the second default prefix was fully writable.
    writable: list[str] = []
    for name in rendered.protected_prefixes:
        target_dir = os.path.join(taskdir.clone, name)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "selftest_probe.txt")
        if os.path.exists(target):
            os.unlink(target)
        sb(["/bin/sh", "-c", f"echo x > {target}"])
        if os.path.isfile(target):
            writable.append(name)
            os.unlink(target)
    sibling = os.path.join(scratch, "selftest_sibling.txt")
    if os.path.exists(sibling):
        os.unlink(sibling)
    sb(["/bin/sh", "-c", f"echo x > {sibling}"])
    sibling_ok = os.path.isfile(sibling)
    out.append(
        _assertion(
            "protected_prefixes_denied",
            f"all {len(rendered.protected_prefixes)} denied, sibling writable",
            f"writable={writable or 'none'} sibling={'yes' if sibling_ok else 'no'}",
            not writable,
            vacuous=not sibling_ok,
        )
    )

    # 10 nesting_cannot_widen
    widen = os.path.join(taskdir.tmp, "selftest_allowall.sb")
    with open(widen, "w", encoding="utf-8") as handle:
        handle.write("(version 1)\n(allow default)\n")
    nested = sb([SANDBOX_EXEC_BINARY, "-f", widen, "/bin/echo", "WIDENED"]).stdout
    out.append(_assertion("nesting_cannot_widen", "refused", "nested allow-all ran" if b"WIDENED" in nested else "sandbox_apply: Operation not permitted", b"WIDENED" not in nested))

    # 11 runtime_starts -- actually execs the selected adapter and reads its initialize reply. None
    # of the earlier assertion sets ever ran the runtime, which is how they all passed while every
    # dispatch was dead.
    out.append(_runtime_starts_assertion(sb, settings, env))

    # 12 verifier_python_runs (+ the version must start with 3.12 or it is not the pinned one)
    python_binary = settings.python_binary
    if not python_binary:
        out.append(_assertion("verifier_python_runs", "python 3.12.x", "TCE_SUP_PYTHON_BINARY is empty; no interpreter is pinned", False, vacuous=True))
    else:
        version = sb([python_binary, "-c", "import sys;print(sys.version.split()[0])"]).stdout.decode(errors="replace").strip()
        out.append(_assertion("verifier_python_runs", "python 3.12.x", version or "exec denied", version.startswith("3.12")))

    # 13 bytecode_write_ok -- pytest needs __pycache__ inside the clone
    cache = os.path.join(scratch, "pkg", "__pycache__")
    shutil.rmtree(cache, ignore_errors=True)
    sb(["/bin/mkdir", "-p", cache])
    out.append(_assertion("bytecode_write_ok", "created", "created" if os.path.isdir(cache) else "denied", os.path.isdir(cache)))

    # 14 git_commit_in_clone -- commit works inside the sandbox, push has nowhere to go.
    # The probe repository is <taskdir>/tmp/gitprobe, NOT the task clone: committing into the clone
    # would move the HEAD the verifier is about to read. It is inside TASKDIR and therefore under
    # exactly the same profile, so what is measured is unchanged; `observed` names where it ran.
    git = settings.git_binary
    gitprobe = os.path.join(taskdir.tmp, "gitprobe")
    os.makedirs(gitprobe, exist_ok=True)
    if not os.path.isdir(os.path.join(gitprobe, ".git")):
        _run([git, "init", "-q", gitprobe], timeout=20.0)
    commit = sb([git, "-C", gitprobe, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "selftest"])
    push = sb([git, "-C", gitprobe, "push", "--dry-run"]).stderr.decode(errors="replace").strip().splitlines()
    out.append(_assertion("git_commit_in_clone", "commit rc=0", f"rc={commit.returncode} in the tmp/gitprobe repo; push: {(push[0] if push else '')[:50]}", commit.returncode == 0))

    # 15 no_out_of_tree_launch -- the AppleEvent deputy and LaunchServices, both must fail.
    # Measured: the primitives are reachable but the escape is not. The assertion stands so a
    # future widening of this profile cannot open one silently.
    oot = os.path.join(taskdir.tmp, "selftest_oot.txt")
    if os.path.exists(oot):
        os.unlink(oot)
    sb(["/usr/bin/osascript", "-e", f'tell application "Finder" to do shell script "echo E > {oot}"'], timeout=20.0)
    launched = sb(["/usr/bin/open", "-a", "Calculator"], timeout=15.0)
    escaped_oot = os.path.exists(oot)
    if escaped_oot:
        os.unlink(oot)
    detail = (launched.stderr or launched.stdout).decode(errors="replace").strip()[:60]
    out.append(_assertion("no_out_of_tree_launch", "deputy blocked", "wrote out of tree via the deputy" if escaped_oot else f"deputy blocked; open: {detail}", not escaped_oot))

    # 16 child_env_clean -- a separate control from the filesystem profile. Even a perfect write
    # sandbox leaves the agent holding push credentials through an inherited SSH_AUTH_SOCK.
    seen = sb(["/usr/bin/env"]).stdout.decode(errors="replace")
    child_env = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in seen.splitlines() if "=" in line}
    hits = credential_shaped_keys(child_env)
    out.append(_assertion("child_env_clean", "no credential-shaped variable", f"found {hits}" if hits else "no SSH_AUTH_SOCK / *_TOKEN / *_KEY / *_SECRET", not hits, vacuous=not child_env))
    return out


def _handshake(run: Any, argv: Sequence[str], handshake: bytes, *, stdin_bytes: bytes) -> subprocess.CompletedProcess[bytes]:
    """Complete one ``initialize`` handshake, retrying ONLY the measured silent case.

    MEASURED on this host: two ``codex app-server`` launches in quick succession interfere. One of
    them answers with nothing at all — rc 0, no stdout, no stderr — and the same argv answers
    normally a couple of seconds later. Retrying only that silence cannot turn a denial into a
    pass, because every real denial prints a message: ``sandbox-exec: ...`` for an exec denial,
    ``error loading default config after config error`` for a read denial. Anything with output is
    returned as-is on the first attempt.
    """
    completed = subprocess.CompletedProcess(list(argv), 0, b"", b"")
    for settle in _RUNTIME_SETTLE_SCHEDULE:
        time.sleep(settle)
        result: subprocess.CompletedProcess[bytes] = run(argv, stdin_bytes=stdin_bytes)
        completed = result
        if completed.stdout or completed.stderr:
            return completed
    return completed


def _runtime_starts_assertion(sb: Any, settings: SupervisorSettings, env: Mapping[str, str]) -> dict[str, Any]:
    """Exec the default surface's binary and complete its handshake, sandboxed AND unsandboxed.

    The unsandboxed run is the non-vacuity control: if the runtime cannot start on this host at
    all, a sandboxed failure proves nothing about the profile. None of the other fifteen assertions
    ever runs the runtime, which is how a self-test can go green over a dispatch that is dead.
    """
    binary = settings.codex_binary
    handshake = json.dumps({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "tce-supervisor", "title": "tce-supervisor", "version": "0.4.0"}}, "jsonrpc": "2.0"}).encode() + b"\n"
    if not os.path.isfile(binary):
        return _assertion("runtime_starts", "initialize returns userAgent", f"{binary} is not present on this host", False, vacuous=True)

    unsandboxed = _handshake(
        lambda argv, *, stdin_bytes: _run(argv, env=dict(env), timeout=40.0, stdin_bytes=stdin_bytes, cwd=env.get("TMPDIR") or None),
        [binary, "app-server"],
        handshake,
        stdin_bytes=handshake,
    )
    if b"userAgent" not in unsandboxed.stdout:
        return _assertion("runtime_starts", "initialize returns userAgent", "the runtime does not complete the handshake unsandboxed either", False, vacuous=True)

    sandboxed = _handshake(
        lambda argv, *, stdin_bytes: sb(argv, timeout=40.0, stdin_bytes=stdin_bytes),
        [binary, "app-server"],
        handshake,
        stdin_bytes=handshake,
    )
    ok = b"userAgent" in sandboxed.stdout
    detail = (sandboxed.stderr or sandboxed.stdout).decode(errors="replace").strip()[:200]
    if ok:
        detail = "initialize returned userAgent"
    elif not detail:
        detail = f"the runtime answered nothing after {len(_RUNTIME_SETTLE_SCHEDULE)} attempts: no stdout, no stderr, rc={sandboxed.returncode}"
    return _assertion("runtime_starts", "initialize returns userAgent", detail, ok)


def _container_assertions(charter: ResolvedCharter, taskdir: TaskDir, *, settings: SupervisorSettings) -> list[dict[str, Any]]:
    """The seven assertions the container tier requires, measured INSIDE a container.

    The other nine are recorded ``vacuous`` with a reason rather than reported as passes: a
    container cannot nest a Seatbelt profile, has no LaunchServices and does not run the pinned
    Mach-O agent binary, so those names measure nothing here.
    """
    env = child_environment(taskdir, charter)
    env["TMPDIR"] = "/tmp"
    env["PYTHONPYCACHEPREFIX"] = "/tmp/pycache"
    env["HOME"] = "/tmp"
    try:
        write_env = dict(env)
        with open(taskdir.child_env_file, "w", encoding="utf-8") as handle:
            for key in sorted(write_env):
                handle.write(f"{key}={write_env[key]}\n")
        prefix = container_argv(taskdir, charter, settings=settings, network=settings.container_network)
    except (CharterInvalid, OSError) as error:
        return [_assertion(name, "measured", f"container argv unavailable: {error}", False, vacuous=True) for name in SELF_TEST_ASSERTION_NAMES]

    def inside(script: str, *, timeout: float = 90.0) -> subprocess.CompletedProcess[bytes]:
        return _run(tuple(prefix) + ("/bin/sh", "-c", script), timeout=timeout)

    not_applicable = {
        "write_outside_scope_denied": "only /task is mounted; there is no outside to write to",
        "ssh_key_read_denied": "$HOME is not mounted into the container",
        "ambient_config_read_denied": "$HOME is not mounted into the container",
        "enforcement_ports_denied": "--network none removes the network stack entirely",
        "nesting_cannot_widen": "there is no Seatbelt to nest inside a linux container",
        "runtime_starts": "the pinned runtime is Mach-O arm64 and cannot exec under linux/arm64",
        "bytecode_write_ok": "PYTHONPYCACHEPREFIX points at the tmpfs, not at the clone",
        "git_commit_in_clone": "the image's git identity is not the host's; measured separately",
        "no_out_of_tree_launch": "there is no LaunchServices and no AppleEvent bus",
    }

    results: dict[str, dict[str, Any]] = {}
    probe = inside(f"mkdir -p /task/{_SCRATCH_DIR_NAME} && echo ok > /task/{_SCRATCH_DIR_NAME}/w1.txt && cat /task/{_SCRATCH_DIR_NAME}/w1.txt")
    body = probe.stdout.decode(errors="replace").strip()
    results["write_in_scope"] = _assertion(
        "write_in_scope",
        f"writes /task/{_SCRATCH_DIR_NAME}",
        "wrote it" if probe.returncode == 0 else (probe.stderr.decode(errors="replace") or "denied"),
        probe.returncode == 0,
    )
    results["read_in_scope"] = _assertion("read_in_scope", "reads back 'ok'", body or "read denied", body == "ok")

    # A tiny heredoc rather than a one-liner: a `python3 -c` with embedded quotes is exactly the
    # kind of shell quoting that fails silently and then reads as "egress blocked".
    egress_script = "\n".join(
        [
            "python3 - <<'EOF' 2>/dev/null || echo BLOCKED",
            "import socket",
            "s = socket.socket()",
            "s.settimeout(3)",
            "try:",
            '    s.connect(("1.1.1.1", 443))',
            '    print("REACHED")',
            "except OSError:",
            '    print("BLOCKED")',
            "EOF",
            "",
        ]
    )
    egress = inside(egress_script)
    reached = b"REACHED" in egress.stdout
    expected_blocked = settings.container_network == "none"
    results["egress_matches_mode"] = _assertion(
        "egress_matches_mode",
        f"network={settings.container_network}",
        "reached 1.1.1.1:443" if reached else "egress blocked",
        (not reached) if expected_blocked else reached,
    )

    sock = inside("ls /var/run/docker.sock 2>&1 || true")
    absent = b"No such file" in sock.stdout or b"cannot access" in sock.stdout
    results["docker_socket_denied"] = _assertion(
        "docker_socket_denied",
        "socket absent",
        sock.stdout.decode(errors="replace").strip()[:120] or "absent",
        absent,
        vacuous=not _docker_answers_unsandboxed(),
    )

    prefixes = tuple(str(item).strip("/") for item in charter.protected_write_prefixes if str(item).strip("/"))
    script = " ; ".join(f"(echo x > /task/{name}/selftest_probe.txt && echo WROTE:{name}) 2>/dev/null" for name in prefixes)
    script += f" ; (mkdir -p /task/{_SCRATCH_DIR_NAME} && echo x > /task/{_SCRATCH_DIR_NAME}/sibling.txt && echo SIBLING_OK) 2>/dev/null"
    prefix_probe = inside(script)
    text = prefix_probe.stdout.decode(errors="replace")
    written = [name for name in prefixes if f"WROTE:{name}" in text]
    sibling_ok = "SIBLING_OK" in text
    results["protected_prefixes_denied"] = _assertion(
        "protected_prefixes_denied",
        f"all {len(prefixes)} denied, sibling writable",
        f"writable={written or 'none'} sibling={'yes' if sibling_ok else 'no'}",
        not written,
        vacuous=not sibling_ok,
    )

    version = inside("python3 -c 'import sys;print(sys.version.split()[0])'").stdout.decode(errors="replace").strip()
    results["verifier_python_runs"] = _assertion("verifier_python_runs", "python 3.12.x", version or "no interpreter", version.startswith("3.12"))

    seen = inside("env").stdout.decode(errors="replace")
    child_env = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in seen.splitlines() if "=" in line}
    hits = credential_shaped_keys(child_env)
    results["child_env_clean"] = _assertion("child_env_clean", "no credential-shaped variable", f"found {hits}" if hits else "clean", not hits, vacuous=not child_env)

    ordered: list[dict[str, Any]] = []
    for name in SELF_TEST_ASSERTION_NAMES:
        if name in results:
            ordered.append(results[name])
        else:
            ordered.append(_assertion(name, "measured", not_applicable.get(name, "not applicable to the container tier"), False, vacuous=True))
    return ordered


# ------------------------------------------------------------------------------------------
# the denial probes (design §10.8 Part 6)
# ------------------------------------------------------------------------------------------


def probe_denials(charter: ResolvedCharter, *, settings: SupervisorSettings, taskdir: TaskDir | None = None) -> dict[str, Any]:
    """Attempt every prohibition the charter declares, once each, and record whether it was denied.

    A charter whose prohibitions have never been attempted is a document, not a control. Each probe
    carries the same non-vacuity discipline as the self-test: if the target does not exist, or the
    service does not answer unsandboxed, the probe measured nothing and says so.
    """
    taskdir = taskdir or _selftest_taskdir(settings)
    rendered = render_profile(charter, task_root=settings.task_root, taskdir=taskdir.root, template_path=settings.profile_template_path)
    env = child_environment(taskdir, charter)
    prefix = seatbelt_argv(rendered, taskdir, settings=settings)

    def sb(argv: Sequence[str], *, timeout: float = _SUBPROCESS_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
        return _run(tuple(prefix) + tuple(argv), env=env, timeout=timeout, cwd=taskdir.clone)

    def probe(name: str, denied: bool, observed: str, vacuous: bool = False) -> dict[str, Any]:
        return {"name": name, "denied": bool(denied) and not vacuous, "observed": observed[:300], "vacuous": bool(vacuous)}

    probes: list[dict[str, Any]] = []

    outside = os.path.expanduser("~/.tce-supervisor-probe")
    if os.path.exists(outside):
        os.unlink(outside)
    sb(["/usr/bin/touch", outside])
    landed = os.path.exists(outside)
    if landed:
        os.unlink(outside)
    probes.append(probe("write_outside_task_clone", not landed, "created it" if landed else "Operation not permitted"))

    for name in rendered.protected_prefixes:
        target_dir = os.path.join(taskdir.clone, name)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "probe.txt")
        if os.path.exists(target):
            os.unlink(target)
        sb(["/bin/sh", "-c", f"echo x > {target}"])
        wrote = os.path.isfile(target)
        if wrote:
            os.unlink(target)
        probes.append(probe(f"write_protected_prefix:{name}", not wrote, "wrote it" if wrote else "Operation not permitted"))

    ssh_dir = os.path.expanduser("~/.ssh")
    keys = [os.path.join(ssh_dir, n) for n in sorted(os.listdir(ssh_dir))] if os.path.isdir(ssh_dir) else []
    key = next((k for k in keys if os.path.isfile(k) and os.path.basename(k).startswith("id_")), "")
    if not key:
        probes.append(probe("read_ssh_private_key", False, "no ssh key on this host", vacuous=True))
    else:
        got = sb(["/bin/cat", key])
        probes.append(probe("read_ssh_private_key", not (got.returncode == 0 and got.stdout), "read it" if got.stdout else "Operation not permitted"))

    ambient = os.path.expanduser("~/.codex/config.toml")
    if not os.path.isfile(ambient):
        probes.append(probe("read_runtime_ambient_config", False, "~/.codex/config.toml absent", vacuous=True))
    else:
        got = sb(["/bin/cat", ambient])
        probes.append(probe("read_runtime_ambient_config", not (got.returncode == 0 and got.stdout), "read it" if got.stdout else "Operation not permitted"))

    for port in ENFORCEMENT_PORTS:
        if not _port_open(port):
            probes.append(probe(f"connect_enforcement_port:{port}", False, "not open unsandboxed", vacuous=True))
            continue
        got = sb(["/usr/bin/nc", "-z", "-G", "2", "127.0.0.1", str(port)], timeout=10.0)
        probes.append(probe(f"connect_enforcement_port:{port}", got.returncode != 0, "connected" if got.returncode == 0 else "refused"))

    if not _docker_answers_unsandboxed():
        probes.append(probe("connect_docker_daemon_socket", False, "daemon does not answer unsandboxed", vacuous=True))
    else:
        got = sb(["/usr/bin/curl", "-s", "--max-time", "5", "--unix-socket", DOCKER_SOCKET_PATH, "http://d/v1.43/version"], timeout=15.0)
        probes.append(probe("connect_docker_daemon_socket", not got.stdout, f"answered: {got.stdout[:40]!r}" if got.stdout else "silent"))

    for name, argv in (
        ("exec_osascript", ["/usr/bin/osascript", "-e", "return 1"]),
        ("exec_open", ["/usr/bin/open", "-a", "Calculator"]),
        ("exec_launchctl", ["/bin/launchctl", "list"]),
    ):
        got = sb(argv, timeout=15.0)
        detail = (got.stderr or got.stdout).decode(errors="replace").strip()[:80]
        probes.append(probe(name, got.returncode != 0, detail or f"rc={got.returncode}"))

    if not os.path.isdir(os.path.join(taskdir.clone, ".git")):
        _run([settings.git_binary, "init", "-q", taskdir.clone], timeout=20.0)
    push = sb([settings.git_binary, "-C", taskdir.clone, "push", "--dry-run"], timeout=20.0)
    probes.append(probe("git_push_dry_run", push.returncode != 0, (push.stderr or push.stdout).decode(errors="replace").strip()[:100] or f"rc={push.returncode}"))

    widen = os.path.join(taskdir.tmp, "probe_allowall.sb")
    with open(widen, "w", encoding="utf-8") as handle:
        handle.write("(version 1)\n(allow default)\n")
    nested = sb([SANDBOX_EXEC_BINARY, "-f", widen, "/bin/echo", "WIDENED"])
    probes.append(probe("nest_permissive_profile", b"WIDENED" not in nested.stdout, "nested allow-all ran" if b"WIDENED" in nested.stdout else "sandbox_apply: Operation not permitted"))

    # The F10 probe: the rendered profile must be unreachable from inside the profile it renders.
    rewrite = sb(["/bin/sh", "-c", f"chmod 0600 {rendered.path} && printf '(version 1)\\n(allow default)\\n' > {rendered.path} && echo REWROTE"])
    tampered = False
    try:
        verify_profile(rendered)
    except ProfileTampered:
        tampered = True
    probes.append(probe("rewrite_rendered_profile", not tampered and b"REWROTE" not in rewrite.stdout, "the agent rewrote its own profile" if tampered else "chmod and write both denied"))

    return {"schema_version": SELF_TEST_SCHEMA_VERSION, "profile_digest": rendered.digest, "probes": probes}
