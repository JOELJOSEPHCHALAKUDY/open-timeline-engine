"""Supervisor settings.

The env prefix is ``TCE_SUP_`` and that is deliberate, not cosmetic: the manager's own settings
use ``TCE_``, so a supervisor knob can never be set by the manager's ``.env``.  It is also why
the mutating opt-in of design §0.5 is a **charter field** and not a ``TCE_SUP_*`` variable —
``validate_charter_payload`` runs inside the API process and cannot see one.

Every setting below has a named read site.  A setting with no read site is deleted, not
documented.  Three settings here are additions the spec's §0.7 table does not name, each marked
NOT IN §0.7 with the site that reads it; they are reported rather than smuggled.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# design §3.8: the supervisor reads its credential from a 0600 file in the owner's config dir,
# the same convention the P1 host-capture token file already uses. The path is expanded at
# import time so ``SettingsConfigDict`` gets a concrete value.
SUPERVISOR_ENV_FILE: str = os.path.expanduser("~/.config/open-timeline-engine/supervisor.env")


class SupervisorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TCE_SUP_", env_file=(SUPERVISOR_ENV_FILE, ".env"), extra="ignore")

    # --- the narrow HTTP credential (apiclient.py) -------------------------------------------
    api_base_url: str = "http://127.0.0.1:18080"
    api_token: str = ""
    consumer_id: str = "supervisor"
    role: str = "executor"
    workspace_id: str = "default"
    user_id: str = ""
    behavior_subject_id: str = ""
    session_id: str = ""

    # The verifier's own credential. design §3.3/§1.4: ``decide_verdict`` returns
    # ``inconclusive`` when the runner principal equals the claiming consumer, so without a
    # DISTINCT credential every verification is honestly inconclusive and no task reaches DONE.
    # Empty is the honest default: it means "not provisioned", not "same as the executor".
    verifier_token: str = ""
    verifier_consumer_id: str = ""

    # --- task directories and the profile (taskdir.py, sandbox.py) ---------------------------
    task_root: str = "/private/tmp/tce-tasks"
    # NOT IN §0.7. Read site: ``supervise.dispatch_once`` -> ``taskdir.provision(repo_path=...)``.
    # ``provision`` needs a repository to clone and §0.7 names no setting for it. Empty means
    # "no repository configured", and ``provision`` refuses rather than cloning something it
    # guessed at.
    repo_path: str = ""
    sandbox_provider: str = "seatbelt"
    profile_template_path: str = ""
    sandbox_self_test_required: bool = True
    # NOT IN §0.7's supervisor table (it is in the backend table, read by ``open_dispatch``).
    # Read site: ``supervise.dispatch_once`` step 3, which refuses to reuse a self-test row older
    # than this rather than POSTing a stale measurement the backend would then reject.
    sandbox_self_test_max_age_seconds: int = 3600

    # --- Tier 2 (sandbox.container_argv, verifier.run) ---------------------------------------
    container_image: str = "python:3.12-bookworm"
    docker_binary: str = "/usr/local/bin/docker"
    container_network: str = "none"
    verification_tier: str = "container"

    # --- pinned binaries ----------------------------------------------------------------------
    git_binary: str = "/opt/homebrew/bin/git"
    # Empty => ``verifier.run`` raises ``VerifierUnavailable("no_python_binary")``. It is empty by
    # default because neither system python on this host is executable inside the profile
    # (design §5.0 F5) and silently picking one would produce a verification that never ran.
    python_binary: str = ""
    # The interpreter's install root, allowed read+exec by the rendered profile. On this host the
    # only interpreter satisfying requires-python>=3.12 is uv-managed INSIDE the denied $HOME, so
    # this carve-out is what makes the verifier possible at all.
    python_root: str = ""

    codex_binary: str = "/Applications/ChatGPT.app/Contents/Resources/codex"
    codex_version_pin: str = "codex-cli 0.153.1"
    claude_binary: str = ""
    claude_version_pin: str = ""

    # --- dispatch loop -------------------------------------------------------------------------
    default_runtime_surface: str = "codex/app-server"
    max_concurrent_dispatches: int = 1
    dispatch_wall_seconds: int = 1800
    observe_poll_ms: int = 500
    token_price_table_json: str = "{}"

    @property
    def token_price_table(self) -> dict[str, dict[str, int]]:
        """Parse ``token_price_table_json`` for ``budget.reconcile(price_table=...)``.

        A malformed table yields an empty one, which makes ``reconcile`` report
        ``cost_source="unavailable"`` — "we do not know what it cost" — rather than an invented
        zero.
        """
        try:
            parsed: Any = json.loads(self.token_price_table_json or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, Mapping):
            return {}
        table: dict[str, dict[str, int]] = {}
        for model, prices in parsed.items():
            if not isinstance(prices, Mapping):
                continue
            row: dict[str, int] = {}
            for key, value in prices.items():
                try:
                    row[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue
            table[str(model)] = row
        return table

    @property
    def effective_verifier_consumer_id(self) -> str:
        """The consumer id the verifier presents, or "" when it has not been provisioned."""
        return str(self.verifier_consumer_id or "").strip()


@lru_cache(maxsize=1)
def get_supervisor_settings() -> SupervisorSettings:
    return SupervisorSettings()
