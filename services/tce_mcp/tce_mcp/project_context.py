"""Best-effort, cached project discovery for MCP requests."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from tce_shared.project_context import canonical_project_context

from .config import get_settings


def _git(root: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@lru_cache(maxsize=1)
def discover_project_context() -> dict[str, str]:
    settings = get_settings()
    configured_root = str(settings.mcp_project_root or "").strip()
    candidate_root = configured_root or os.getcwd()
    git_root = _git(candidate_root, "rev-parse", "--show-toplevel")
    root = configured_root or git_root
    discovered = {
        "project_id": settings.mcp_project_id,
        "project": settings.mcp_project_name or (Path(root).name if root else ""),
        "project_root": root,
        "project_remote": settings.mcp_project_remote or (_git(root, "config", "--get", "remote.origin.url") if root else ""),
        "project_branch": settings.mcp_project_branch or (_git(root, "branch", "--show-current") if root else ""),
        "project_source": "mcp_config" if configured_root or settings.mcp_project_id else "mcp_git",
    }
    return canonical_project_context(discovered, source=discovered["project_source"])


def with_project_context(app_context: dict[str, Any] | None) -> dict[str, Any]:
    explicit = dict(app_context or {})
    explicit_identity = any(
        explicit.get(key)
        for key in ("project_id", "project", "project_name", "project_root", "workspace_path", "repo", "project_remote")
    )
    project = canonical_project_context(
        explicit,
        discover_project_context(),
        source="explicit" if explicit_identity else "mcp_git",
    )
    return {**explicit, **project}
