#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CORE_ROOTS = [
    "shared",
    "services/tce_api",
    "services/tce_lite_api",
    "services/tce_mcp",
    "services/tce_worker",
    "tests",
]


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _event_payload() -> dict:
    event_path = os.getenv("GITHUB_EVENT_PATH", "")
    if not event_path:
        return {}
    try:
        return json.loads(Path(event_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _determine_base_sha(payload: dict) -> str | None:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    if event_name == "pull_request":
        return str(payload.get("pull_request", {}).get("base", {}).get("sha", "") or "").strip() or None
    if event_name == "push":
        before = str(payload.get("before", "") or "").strip()
        if before and before != "0" * 40:
            return before
    return None


def _changed_files() -> list[str]:
    payload = _event_payload()
    base_sha = _determine_base_sha(payload)

    if base_sha:
        _run_git(["fetch", "--no-tags", "--depth", "1", "origin", base_sha])
        diff = _run_git(["diff", "--name-only", f"{base_sha}...HEAD"])
        if diff.returncode == 0:
            return [line.strip() for line in diff.stdout.splitlines() if line.strip()]

    fallback = _run_git(["diff", "--name-only", "HEAD~1..HEAD"])
    if fallback.returncode == 0:
        return [line.strip() for line in fallback.stdout.splitlines() if line.strip()]

    return []


def _targets_from_changed(files: list[str]) -> list[str]:
    targets: set[str] = set()
    for path in files:
        if not path.endswith(".py"):
            continue
        if path.startswith("stress_test_"):
            targets.add(path)
            continue
        for root in CORE_ROOTS:
            if path == root or path.startswith(f"{root}/"):
                targets.add(root)
                break
    return sorted(targets)


def main() -> int:
    changed = _changed_files()
    targets = _targets_from_changed(changed)

    if not targets:
        print("No changed Python files in core packages; skipping ruff.")
        return 0

    print("Running ruff on:")
    for target in targets:
        print(f" - {target}")

    cmd = [sys.executable, "-m", "ruff", "check", *targets]
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
