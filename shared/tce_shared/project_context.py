"""Canonical, redaction-safe project identity for executor continuity."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .redaction import redact_text

PROJECT_CONTEXT_KEYS = (
    "project_id",
    "project",
    "project_root",
    "project_remote",
    "project_branch",
    "project_source",
)


def _clean(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:limit]
    redacted, _ = redact_text(text)
    return redacted.strip()


def sanitize_project_remote(value: Any) -> str:
    """Return a stable remote identifier without embedded credentials."""
    remote = re.sub(r"\s+", "", str(value or "")).strip()[:512]
    if not remote:
        return ""
    if "://" in remote:
        parsed = urlsplit(remote)
        hostname = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            hostname = f"{hostname}:{port}"
        remote = urlunsplit((parsed.scheme.lower(), hostname, parsed.path, "", ""))
    elif "@" in remote and ":" in remote:
        # Git SCP syntax contains an identity but no secret. Drop the identity so
        # HTTPS and SSH remotes for the same repository normalize similarly.
        remote = remote.split("@", 1)[1]
    return _clean(remote.removesuffix(".git").rstrip("/"), limit=512)


def _project_name(root: str, remote: str) -> str:
    candidate = remote.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if remote else root
    return PurePath(candidate.rstrip("/\\")).name[:160]


def _remote_identity(remote: str) -> str:
    if not remote:
        return ""
    if "://" in remote:
        parsed = urlsplit(remote)
        return f"{parsed.hostname or ''}/{parsed.path.lstrip('/')}".lower()
    if ":" in remote:
        host, path = remote.split(":", 1)
        return f"{host}/{path.lstrip('/')}".lower()
    return remote.lower()


def canonical_project_context(
    incoming: dict[str, Any] | None,
    inherited: dict[str, Any] | None = None,
    *,
    source: str = "explicit",
) -> dict[str, str]:
    """Resolve explicit project metadata over a previously bound session context."""
    supplied = incoming if isinstance(incoming, dict) else {}
    prior = inherited if isinstance(inherited, dict) else {}

    explicit_project = _clean(supplied.get("project") or supplied.get("project_name"), limit=160)
    explicit_root = _clean(
        supplied.get("project_root") or supplied.get("workspace_path") or supplied.get("repo"),
        limit=512,
    )
    explicit_remote = sanitize_project_remote(
        supplied.get("project_remote") or supplied.get("repo_remote") or supplied.get("remote")
    )
    explicit_id = _clean(supplied.get("project_id"), limit=96)
    strong_explicit_identity = bool(explicit_root or explicit_remote or explicit_id)
    has_explicit_identity = bool(explicit_project or strong_explicit_identity)

    prior_root = _clean(prior.get("project_root"), limit=512)
    same_bound_root = bool(explicit_root and prior_root and explicit_root == prior_root)
    root = explicit_root if strong_explicit_identity else prior_root
    prior_remote = sanitize_project_remote(prior.get("project_remote"))
    if strong_explicit_identity:
        remote = explicit_remote or (prior_remote if same_bound_root else "")
    else:
        remote = prior_remote
    inherited_project = (
        _clean(prior.get("project"), limit=160)
        if not strong_explicit_identity or same_bound_root
        else ""
    )
    project = explicit_project or inherited_project or _project_name(root, remote)
    if not (project or root or remote or explicit_id):
        return {}

    identity_basis = explicit_id or _remote_identity(remote) or root.rstrip("/\\").lower() or project.lower()
    if explicit_id and re.fullmatch(r"proj_[a-f0-9]{24}", explicit_id):
        project_id = explicit_id
    else:
        project_id = f"proj_{hashlib.sha256(identity_basis.encode()).hexdigest()[:24]}"

    branch = _clean(
        supplied.get("project_branch")
        or supplied.get("branch")
        or (prior.get("project_branch") if not strong_explicit_identity or same_bound_root else ""),
        limit=160,
    )
    resolved = {
        "project_id": project_id,
        "project": project or project_id,
        "project_source": _clean(
            supplied.get("project_source") if has_explicit_identity else prior.get("project_source"),
            limit=40,
        )
        or source,
    }
    if root:
        resolved["project_root"] = root
    if remote:
        resolved["project_remote"] = remote
    if branch:
        resolved["project_branch"] = branch
    return resolved


def project_context_from_payload(payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    app_context = payload.get("app_context")
    if not isinstance(app_context, dict):
        return {}
    return canonical_project_context(app_context)
