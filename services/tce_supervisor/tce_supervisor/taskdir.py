"""The per-directive task directory, and the environment the child actually gets.

Two decisions here are load-bearing and are explained where they are made rather than in a doc:

**A clone, never a worktree.**  A git worktree's ``.git`` is a pointer into the parent's
``.git/worktrees/<name>`` and everything else is shared; making one functional requires write
access to the parent ``.git``, which grants ``.git/hooks/pre-commit`` and ``core.hooksPath`` — code
that then runs in the owner's next un-sandboxed git operation.  A clone gives the agent its own
whole ``.git`` and puts the parent's hooks off-path entirely.

**A fresh environment dict, never ``os.environ.copy()``.**  Even a perfect write sandbox leaves the
agent holding push credentials through an inherited ``SSH_AUTH_SOCK``, which would make "review
before push" decoration.  ``child_environment`` builds the dict from nothing and
``assert_no_credentials`` re-checks it; self-test assertion 16 measures the same thing on the far
side of the sandbox.

**The residual this module creates, stated (D-10).**  ``provision`` copies the owner's
``~/.codex/auth.json`` into ``<taskdir>/codex-home`` because the runtime cannot authenticate
without it.  That file is inside the directory the agent may read and write, and Tier 1 leaves
port 443 open to every host.  The credential is exfiltrable and nothing here prevents it.  It
copies ``auth.json`` and **nothing else** — no ``config.toml``, so the ambient ``tce-executor`` MCP
server carrying ``TCE_API_TOKEN`` and the ``trust_level = "trusted"`` marker are structurally
absent — but that narrows the blast radius, it does not close the hole.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from tce_shared.charter import ResolvedCharter

# Names whose presence in a child environment would hand the agent a credential.
_CREDENTIAL_NAME_PATTERN = re.compile(r"(_TOKEN|_KEY|_SECRET|_PASSWORD)$")
_CREDENTIAL_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "SSH_AUTH_SOCK",
        "TCE_API_TOKEN",
        "TCE_API_TOKENS",
        "TCE_HOST_CAPTURE_TOKEN",
        "TCE_HOST_CAPTURE_TOKENS",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }
)

CHILD_PATH: str = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class TaskDirUnavailable(RuntimeError):
    """The task directory could not be provisioned; the reason names which precondition failed."""

    reason: str

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TaskDir:
    root: str  # <task_root>/<directive_id>
    clone: str  # <root>/clone
    home: str  # <root>/home
    tmp: str  # <root>/tmp
    codex_home: str  # <root>/codex-home
    logs: str  # <root>/logs
    child_env_file: str  # <root>/child.env   (Tier 2's --env-file)


def _safe_segment(value: str) -> str:
    """A directory-name-safe form of a directive id.

    A directive id reaches this from the caller, and a caller that passed ``../..`` would otherwise
    provision outside the task root.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", str(value or "").strip())
    cleaned = cleaned.strip(".-") or "directive"
    return cleaned[:120]


def plan(*, task_root: str, directive_id: str) -> TaskDir:
    """Compute the task directory paths WITHOUT creating or cloning anything.

    The dispatch sequence renders the sandbox profile and runs the self-test before it provisions,
    so that a failing sandbox refuses before a clone is made. Both functions derive the paths the
    same way, from the same segment-sanitiser, so the plan and the provisioned directory cannot
    disagree.
    """
    root = os.path.join(os.path.abspath(os.path.expanduser(task_root)), _safe_segment(directive_id))
    return TaskDir(
        root=root,
        clone=os.path.join(root, "clone"),
        home=os.path.join(root, "home"),
        tmp=os.path.join(root, "tmp"),
        codex_home=os.path.join(root, "codex-home"),
        logs=os.path.join(root, "logs"),
        child_env_file=os.path.join(root, "child.env"),
    )


def provision(*, task_root: str, directive_id: str, repo_path: str, git_binary: str) -> TaskDir:
    """Create ``<task_root>/<directive_id>/`` and clone the repository into it."""
    if not repo_path:
        raise TaskDirUnavailable("no_repo_path", "TCE_SUP_REPO_PATH is empty; there is no repository to clone")
    source = os.path.abspath(os.path.expanduser(repo_path))
    if not os.path.isdir(os.path.join(source, ".git")):
        raise TaskDirUnavailable("repo_missing", f"{source} is not a git repository")
    if not os.path.isfile(git_binary) or not os.access(git_binary, os.X_OK):
        raise TaskDirUnavailable("no_git_binary", f"{git_binary} is not an executable file")

    taskdir = plan(task_root=task_root, directive_id=directive_id)
    root, clone, home, tmp, codex_home, logs = taskdir.root, taskdir.clone, taskdir.home, taskdir.tmp, taskdir.codex_home, taskdir.logs
    for path in (root, home, tmp, codex_home, logs):
        os.makedirs(path, exist_ok=True)

    if not os.path.isdir(os.path.join(clone, ".git")):
        if os.path.isdir(clone):
            shutil.rmtree(clone)
        # --shared --no-hardlinks: cheap, and the object store stays the parent's while the
        # agent's .git, hooks and config are entirely its own.
        completed = subprocess.run(
            [git_binary, "clone", "--shared", "--no-hardlinks", source, clone],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise TaskDirUnavailable("clone_failed", (completed.stderr or completed.stdout or "").strip()[:500])

    # The runtime credential. See the module docstring: this is the D-10 residual, deliberately
    # copied and deliberately alone.
    auth_source = os.path.expanduser("~/.codex/auth.json")
    auth_target = os.path.join(codex_home, "auth.json")
    if os.path.isfile(auth_source) and not os.path.exists(auth_target):
        shutil.copyfile(auth_source, auth_target)
        os.chmod(auth_target, 0o600)

    gitconfig = os.path.join(home, ".gitconfig")
    if not os.path.exists(gitconfig):
        with open(gitconfig, "w", encoding="utf-8") as handle:
            handle.write("[user]\n\tname = TCE Supervisor\n\temail = supervisor@tce.invalid\n[commit]\n\tgpgsign = false\n")

    return taskdir


def child_environment(taskdir: TaskDir, charter: ResolvedCharter) -> dict[str, str]:
    """The complete environment the child gets. A fresh dict; nothing is inherited.

    Every entry below is mandatory and was measured to be so:

    * ``TMPDIR`` — the xcrun shim needs a writable one or ``/usr/bin/python3`` fails to load.
    * ``PYTHONPYCACHEPREFIX`` — the verifier's clone is read-only, so bytecode must land elsewhere.
    * ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` — ``~/.gitconfig`` is denied by the profile and
      git aborts hard rather than skipping it.
    * ``CODEX_HOME`` — the only thing suppressing the ambient runtime config, since
      ``--ignore-user-config`` does not exist on ``codex app-server``.

    ``charter`` is taken so the signature can carry charter-derived environment later without a
    call-site change; today it contributes only the egress mode, recorded for the child's own logs.
    """
    return {
        "PATH": CHILD_PATH,
        "HOME": taskdir.home,
        "TMPDIR": taskdir.tmp,
        "CODEX_HOME": taskdir.codex_home,
        "PYTHONPYCACHEPREFIX": os.path.join(taskdir.tmp, "pycache"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "LANG": "en_US.UTF-8",
        "TCE_SANDBOX_EGRESS_MODE": charter.egress_mode,
    }


def credential_shaped_keys(env: Mapping[str, str]) -> list[str]:
    """Every key in ``env`` that looks like a credential. Empty is the only acceptable answer."""
    hits: list[str] = []
    for key in env:
        name = str(key)
        if name in _CREDENTIAL_EXACT_NAMES or _CREDENTIAL_NAME_PATTERN.search(name):
            hits.append(name)
    return sorted(hits)


def assert_no_credentials(env: Mapping[str, str]) -> None:
    hits = credential_shaped_keys(env)
    if hits:
        raise TaskDirUnavailable("credential_in_child_env", f"child environment carries {hits}")


def write_child_env_file(taskdir: TaskDir, env: Mapping[str, str]) -> str:
    """Write Tier 2's ``--env-file``. Refuses to write a credential into it."""
    assert_no_credentials(env)
    with open(taskdir.child_env_file, "w", encoding="utf-8") as handle:
        for key in sorted(env):
            value = str(env[key])
            if "\n" in value:
                raise TaskDirUnavailable("bad_env_value", f"{key} contains a newline; docker --env-file cannot represent it")
            handle.write(f"{key}={value}\n")
    os.chmod(taskdir.child_env_file, 0o600)
    return taskdir.child_env_file


def cleanup(taskdir: TaskDir, *, keep_logs: bool = True) -> None:
    """Remove the task directory. Logs survive by default: they are the evidence of what ran."""
    if keep_logs:
        for name in ("clone", "home", "tmp", "codex-home"):
            path = os.path.join(taskdir.root, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        if os.path.isfile(taskdir.child_env_file):
            os.unlink(taskdir.child_env_file)
        return
    shutil.rmtree(taskdir.root, ignore_errors=True)
