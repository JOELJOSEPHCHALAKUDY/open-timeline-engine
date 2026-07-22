#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CORE_ROOTS = ("shared", "services", "tests")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIAGNOSTIC_RE = re.compile(r"^(.*?):(\d+): (error|note): (.*)$")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _event_payload() -> dict:
    event_path = os.getenv("GITHUB_EVENT_PATH", "")
    if not event_path:
        return {}
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _diff_ref() -> str:
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return "HEAD"
    payload = _event_payload()
    if os.getenv("GITHUB_EVENT_NAME") == "pull_request":
        base = str(payload.get("pull_request", {}).get("base", {}).get("sha", "")).strip()
        if base:
            _git("fetch", "--no-tags", "--depth", "1", "origin", base)
            return f"{base}...HEAD"
    if os.getenv("GITHUB_EVENT_NAME") == "push":
        before = str(payload.get("before", "")).strip()
        if before and before != "0" * 40:
            return f"{before}...HEAD"
    return "HEAD~1..HEAD"


def _changed_targets(diff_ref: str) -> list[str]:
    result = _git("diff", "--name-only", "--diff-filter=ACMR", diff_ref)
    if result.returncode != 0:
        return []
    return sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().endswith(".py")
            and any(line.strip() == root or line.strip().startswith(f"{root}/") for root in CORE_ROOTS)
        }
    )


def _changed_lines(path: str, diff_ref: str) -> set[int]:
    result = _git("diff", "--unified=0", diff_ref, "--", path)
    if result.returncode != 0:
        return set()
    changed: set[int] = set()
    for line in result.stdout.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        changed.update(range(start, start + count))
    return changed


def main() -> int:
    diff_ref = _diff_ref()
    targets = _changed_targets(diff_ref)
    if not targets:
        print("No changed Python files in typed packages; skipping mypy.")
        return 0
    print("Running mypy on changed Python files:")
    for target in targets:
        print(f" - {target}")
    completed = subprocess.run(
        [sys.executable, "-m", "mypy", "--show-error-codes", "--no-error-summary", *targets],
        capture_output=True,
        text=True,
        check=False,
    )
    changed_by_file = {str(Path(path).resolve()): _changed_lines(path, diff_ref) for path in targets}
    relevant: list[str] = []
    for line in completed.stdout.splitlines():
        match = _DIAGNOSTIC_RE.match(line)
        if not match or match.group(3) != "error":
            continue
        filename = str(Path(match.group(1)).resolve())
        row = int(match.group(2))
        if row in changed_by_file.get(filename, set()):
            relevant.append(line)
    if not relevant:
        print("No mypy errors on changed lines.")
        return 0
    print(f"mypy found {len(relevant)} error(s) on changed lines:")
    print("\n".join(relevant))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
