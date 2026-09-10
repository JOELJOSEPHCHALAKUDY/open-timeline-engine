#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CORE_ROOTS = [
    "shared",
    "services/tce_api",
    "services/tce_lite_api",
    "services/tce_mcp",
    "services/tce_supervisor",
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
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
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


def _diff_base() -> tuple[str, str]:
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return "HEAD", "HEAD"
    payload = _event_payload()
    base_sha = _determine_base_sha(payload)
    if base_sha:
        _run_git(["fetch", "--no-tags", "--depth", "1", "origin", base_sha])
        return f"{base_sha}...HEAD", base_sha
    return "HEAD~1..HEAD", "HEAD~1"


def _changed_files() -> tuple[list[str], str]:
    diff_ref, _ = _diff_base()
    diff = _run_git(["diff", "--name-only", "--diff-filter=ACMR", diff_ref])
    if diff.returncode == 0:
        return [line.strip() for line in diff.stdout.splitlines() if line.strip()], diff_ref
    return [], diff_ref


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _targets_from_changed(files: list[str]) -> list[str]:
    targets: list[str] = []
    for path in files:
        if not path.endswith(".py"):
            continue
        if path.startswith("stress_test_") or any(path == root or path.startswith(f"{root}/") for root in CORE_ROOTS):
            targets.append(path)
    return sorted(set(targets))


def _changed_lines(path: str, diff_ref: str) -> set[int]:
    diff = _run_git(["diff", "--unified=0", diff_ref, "--", path])
    if diff.returncode != 0:
        return set()
    changed: set[int] = set()
    for line in diff.stdout.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        changed.update(range(start, start + count))
    return changed


def _display_diagnostic(item: dict) -> None:
    filename = item.get("filename", "")
    location = item.get("location", {})
    code = item.get("code", "")
    message = item.get("message", "")
    print(f"{filename}:{location.get('row', 0)}:{location.get('column', 0)}: {code} {message}")


def main() -> int:
    changed, diff_ref = _changed_files()
    targets = _targets_from_changed(changed)

    if not targets:
        print("No changed Python files in core packages; skipping ruff.")
        return 0

    print("Running ruff on changed Python files:")
    for target in targets:
        print(f" - {target}")

    cmd = [sys.executable, "-m", "ruff", "check", "--output-format=json", *targets]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        diagnostics = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        return completed.returncode

    changed_by_file = {str(Path(path).resolve()): _changed_lines(path, diff_ref) for path in targets}
    relevant: list[dict] = []
    for item in diagnostics:
        filename = str(Path(str(item.get("filename", ""))).resolve())
        row = int((item.get("location") or {}).get("row", 0) or 0)
        if row in changed_by_file.get(filename, set()):
            relevant.append(item)

    if not relevant:
        print("No Ruff violations on changed lines.")
        return 0
    print(f"Ruff found {len(relevant)} violation(s) on changed lines:")
    for item in relevant:
        _display_diagnostic(item)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
