"""The verifier: it runs the frozen checks and reports **evidence only**.

The single most important property of this module is what it does *not* have.
:class:`VerifierReport` carries no ``outcome`` and no ``verdict`` field, and nothing here computes
one.  The API grades the evidence against the criteria row it froze; the supervisor never decides
whether its own run passed.  That is the whole of U5, and it is enforced structurally rather than
by convention: there is no field to put a verdict in.

**Where the checks come from.**  ``CHECK_TEMPLATES`` is a closed dict in this file, keyed by the
tokens already present in ``directive_executions.meta["verification_required"]`` (default
``build,test,lint``).  It is checked-in source in the supervisor's package, outside every task
directory, and its argv entries are absolute paths resolved from ``TCE_SUP_PYTHON_BINARY`` and
``TCE_SUP_GIT_BINARY`` — so the executing agent can reach neither the template nor the binaries it
names.  An unknown token maps to no check and is dropped; an empty resulting list makes the caller
refuse **before any child process exists**.

**Why it runs in Tier 2 by default.**  ``git`` is installed in no service image and no repository is
bind-mounted, so the server-side change summary returns ``{}`` silently — an empty summary
indistinguishable from "no changes".  The verifier therefore runs host-side, in a container with
``--network none``, as a principal distinct from the implementing agent.

**And what a Tier-2 verification does not prove.**  It is produced on ``linux/aarch64`` and does
**not** transfer to the owner's ``darwin/arm64`` shell.  ``platform`` records which one produced it
precisely so nobody has to guess.
"""

from __future__ import annotations

import hashlib
import os
import platform as platform_module
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tce_shared.charter import ResolvedCharter
from tce_shared.redaction import redact_text
from tce_shared.verification import (
    DECIDING_FILE_PATHS,
    AcceptanceCheck,
    AcceptanceCriteria,
    CheckResult,
    VerificationEvidence,
    build_corpus_manifest,
    corpus_digest,
)

from . import sandbox
from .config import SupervisorSettings
from .taskdir import TaskDir, child_environment

# The interpreter path inside the pinned container image. The HOST interpreter path does not exist
# there, so the criteria are frozen with the interpreter of the tier that will run them.
CONTAINER_PYTHON: str = "/usr/local/bin/python3"
DEFAULT_VERIFICATION_TOKENS: tuple[str, ...] = ("build", "test", "lint")
_EXCERPT_LIMIT: int = 2000


class VerifierUnavailable(RuntimeError):
    """The verification could not be run. Never silently downgraded to "inconclusive" here."""

    reason: str  # "no_python_binary"|"no_git_binary"|"clone_missing"|"check_binary_missing"|"no_container"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class VerifierReport:
    """EVIDENCE ONLY. There is deliberately no ``outcome`` field: the supervisor does not decide."""

    results: tuple[CheckResult, ...]
    observed_corpus_digest: str
    observed_corpus_manifest: tuple[tuple[str, str], ...]
    platform: str
    commit_sha: str | None
    tree_sha: str | None
    change_summary: Mapping[str, Any]
    reviewer_model: Mapping[str, Any] | None = None


def _check(check_id: str, argv: Sequence[str], *, cwd_rel: str = ".", timeout: int = 900) -> AcceptanceCheck:
    return AcceptanceCheck(check_id=check_id, argv=tuple(str(item) for item in argv), cwd_rel=cwd_rel, expect_exit_code=0, timeout_seconds=timeout)


# token -> a factory taking the absolute interpreter path for the tier that will run the check.
CHECK_TEMPLATES: dict[str, Any] = {
    "build": lambda python: _check("build", [python, "-m", "compileall", "-q", "."], timeout=600),
    "test": lambda python: _check("test", [python, "-m", "pytest", "-q", "tests/unit"], timeout=900),
    "lint": lambda python: _check("lint", [python, "-m", "ruff", "check", "."], timeout=300),
}


def verification_tokens(meta: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Read the tokens from ``meta["verification_required"]``, defaulting to build,test,lint.

    ``meta`` is the same JSON blob the agent's own report merges into, which is exactly why the
    tokens are all that is read from it: the *mapping* from token to argv lives here, out of reach.
    """
    raw = (meta or {}).get("verification_required")
    if isinstance(raw, str):
        tokens = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        tokens = [str(item).strip() for item in raw]
    else:
        tokens = list(DEFAULT_VERIFICATION_TOKENS)
    return tuple(token for token in tokens if token)


def interpreter_for_tier(tier: str, *, settings: SupervisorSettings) -> str:
    if tier == "container":
        return CONTAINER_PYTHON
    if not settings.python_binary:
        raise VerifierUnavailable("no_python_binary")
    return settings.python_binary


def build_checks(tokens: Sequence[str], *, settings: SupervisorSettings, tier: str) -> tuple[AcceptanceCheck, ...]:
    """Map the tokens onto checks. Unknown tokens are dropped; the caller refuses an empty list."""
    python = interpreter_for_tier(tier, settings=settings)
    checks: list[AcceptanceCheck] = []
    seen: set[str] = set()
    for token in tokens:
        factory = CHECK_TEMPLATES.get(str(token))
        if factory is None or token in seen:
            continue
        seen.add(token)
        checks.append(factory(python))
    return tuple(checks)


def _sha256_text(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(settings: SupervisorSettings, clone: str, args: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            [settings.git_binary, "-C", clone, *args],
            capture_output=True,
            timeout=60,
            check=False,
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _change_summary(settings: SupervisorSettings, clone: str) -> dict[str, Any]:
    """``git show --numstat`` over the clone. The repo path is the CLONE, never a caller value.

    The existing server-side helper takes its repo from the request body, validated only by
    ``os.path.isdir`` and falling back to the current working directory — an arbitrary-directory
    read the moment ``git`` is added to an image. This one cannot be pointed anywhere.
    """
    raw = _git(settings, clone, ["show", "--numstat", "--format=", "HEAD"])
    files: list[dict[str, Any]] = []
    added = removed = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        plus, minus, path = parts
        entry: dict[str, Any] = {"path": path, "added": None if plus == "-" else int(plus), "removed": None if minus == "-" else int(minus)}
        files.append(entry)
        added += entry["added"] or 0
        removed += entry["removed"] or 0
    return {"files": files, "files_changed": len(files), "lines_added": added, "lines_removed": removed, "source": "git_show_numstat" if raw else "unavailable"}


def _run_host(check: AcceptanceCheck, *, clone: str, env: Mapping[str, str], wrapper: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    cwd = os.path.join(clone, check.cwd_rel) if check.cwd_rel not in {"", "."} else clone
    try:
        return subprocess.run(tuple(wrapper) + tuple(check.argv), cwd=cwd, env=dict(env), capture_output=True, timeout=check.timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(check.argv), 124, b"", b"timed out")
    except OSError as error:
        raise VerifierUnavailable("check_binary_missing") from error


def _run_container(check: AcceptanceCheck, *, taskdir: TaskDir, charter: ResolvedCharter, settings: SupervisorSettings) -> subprocess.CompletedProcess[bytes]:
    workdir = "/task" if check.cwd_rel in {"", "."} else f"/task/{check.cwd_rel.strip('/')}"
    argv = sandbox.container_argv(taskdir, charter, settings=settings, network=settings.container_network, workdir=workdir) + tuple(check.argv)
    try:
        return subprocess.run(argv, capture_output=True, timeout=check.timeout_seconds + 60, check=False, env={"PATH": "/usr/bin:/bin:/usr/local/bin"})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, b"", b"timed out")
    except OSError as error:
        raise VerifierUnavailable("no_container") from error


def _container_platform(taskdir: TaskDir, charter: ResolvedCharter, settings: SupervisorSettings) -> str:
    argv = sandbox.container_argv(taskdir, charter, settings=settings, network=settings.container_network) + (
        CONTAINER_PYTHON,
        "-c",
        "import platform,sys;print(f'{sys.platform}/{platform.machine()} python{sys.version.split()[0]}')",
    )
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=120, check=False, env={"PATH": "/usr/bin:/bin:/usr/local/bin"})
    except (OSError, subprocess.TimeoutExpired):
        return "container/unknown"
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    return text or "container/unknown"


def host_platform() -> str:
    return f"{sys.platform}/{platform_module.machine()} python{sys.version.split()[0]}"


def run(criteria: AcceptanceCriteria, taskdir: TaskDir, *, settings: SupervisorSettings, charter: ResolvedCharter) -> VerifierReport:
    """Run every frozen check and return the raw evidence.

    ``reviewer_model`` is always ``None`` in this build. The supervisor has no advisor client, and
    a reviewer model could not reach the verdict anyway — the API's ``decide_verdict`` has no
    parameter for one. Reporting an empty dict here would suggest a reviewer ran and said nothing.
    """
    if not os.path.isdir(taskdir.clone):
        raise VerifierUnavailable("clone_missing")
    if not os.path.isfile(settings.git_binary):
        raise VerifierUnavailable("no_git_binary")

    tier = str(settings.verification_tier or "container")
    env = child_environment(taskdir, charter)

    wrapper: tuple[str, ...] = ()
    rendered: sandbox.RenderedProfile | None = None
    if tier == "os_sandbox":
        if not settings.python_binary:
            raise VerifierUnavailable("no_python_binary")
        # NOT the verifier variant design §5.2 asks for. That variant expands the protected block
        # to the WHOLE clone, so the tree the checks judge cannot be edited by the checks
        # themselves. This renders the ORDINARY dispatch profile -- same charter, same prefixes,
        # byte-identical file, same sha256 -- so a check CAN write anywhere in the clone that the
        # agent could. Measured: with the charter's prefixes a check rewrote <clone>/src/*.py;
        # with the whole-clone deny rendered instead, the same write was denied and
        # `compileall -q .` still exited 0 (PYTHONPYCACHEPREFIX keeps bytecode out of the clone).
        # The variant is therefore implementable and is NOT implemented -- the honest statement of
        # the gap, rather than a comment claiming the control. Tier 2 (the default,
        # TCE_SUP_VERIFICATION_TIER=container) is unaffected: container_argv binds every protected
        # prefix read-only, which is what design §3 asks of that tier.
        rendered = sandbox.render_profile(charter, task_root=settings.task_root, taskdir=taskdir.root, template_path=settings.profile_template_path)
        sandbox.verify_profile(rendered)  # F10: re-hash immediately before the exec
        wrapper = sandbox.seatbelt_argv(rendered, taskdir, settings=settings)
    elif tier == "container":
        if not os.path.isfile(settings.docker_binary):
            raise VerifierUnavailable("no_container")

    manifest = build_corpus_manifest(taskdir.clone, tuple(charter.protected_write_prefixes) + DECIDING_FILE_PATHS)
    observed_digest = corpus_digest(manifest)

    results: list[CheckResult] = []
    for check in criteria.checks:
        started = time.monotonic()
        if tier == "container":
            completed = _run_container(check, taskdir=taskdir, charter=charter, settings=settings)
        else:
            if rendered is not None:
                sandbox.verify_profile(rendered)
            completed = _run_host(check, clone=taskdir.clone, env=env, wrapper=wrapper)
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        # redact_text returns (text, hits); only the redacted text reaches the evidence body.
        redacted, _hits = redact_text(((stdout + b"\n" + stderr).decode("utf-8", errors="replace")).strip())
        excerpt = redacted[:_EXCERPT_LIMIT]
        results.append(
            CheckResult(
                check_id=check.check_id,
                argv=tuple(check.argv),
                exit_code=int(completed.returncode),
                duration_ms=duration_ms,
                stdout_sha256=_sha256_text(stdout),
                stderr_sha256=_sha256_text(stderr),
                excerpt=excerpt,
            )
        )

    where = _container_platform(taskdir, charter, settings) if tier == "container" else host_platform()
    return VerifierReport(
        results=tuple(results),
        observed_corpus_digest=observed_digest,
        observed_corpus_manifest=tuple((path, digest) for path, digest in manifest),
        platform=where,
        commit_sha=_git(settings, taskdir.clone, ["rev-parse", "HEAD"]) or None,
        tree_sha=_git(settings, taskdir.clone, ["rev-parse", "HEAD^{tree}"]) or None,
        change_summary=_change_summary(settings, taskdir.clone),
        reviewer_model=None,
    )


def to_evidence(report: VerifierReport, *, directive_id: str) -> VerificationEvidence:
    """Wrap the report as the POST body. No verdict, no runner principal — neither exists here."""
    return VerificationEvidence(
        directive_id=directive_id,
        results=report.results,
        observed_corpus_digest=report.observed_corpus_digest,
        observed_corpus_manifest=report.observed_corpus_manifest,
        platform=report.platform,
        commit_sha=report.commit_sha,
        tree_sha=report.tree_sha,
        reviewer_model=report.reviewer_model,
    )
