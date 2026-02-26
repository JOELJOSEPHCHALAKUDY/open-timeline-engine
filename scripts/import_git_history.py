#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import requests


def run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def parse_commits(raw: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for chunk in raw.strip("\n\x1e").split("\x1e"):
        if not chunk.strip():
            continue
        fields = chunk.strip().split("\x1f")
        if len(fields) < 4:
            continue
        commits.append(
            {
                "sha": fields[0].strip(),
                "ts": fields[1].strip(),
                "author": fields[2].strip(),
                "subject": fields[3].strip(),
            }
        )
    return commits


def file_list_for_commit(repo: Path, sha: str, max_files: int = 100) -> list[str]:
    out = run_git(repo, ["show", "--pretty=format:", "--name-only", sha])
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return files[:max_files]


def build_event(repo: Path, commit: dict[str, str], files: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": commit["ts"],
        "actor": "user",
        "source": "git",
        "domain": "coding",
        "task_type": "commit",
        "event_type": "CODE_CHANGE",
        "title": f"Git commit: {commit['subject']}",
        "payload": {
            "git": {
                "sha": commit["sha"],
                "author": commit["author"],
                "subject": commit["subject"],
                "files": files,
                "file_count": len(files),
            }
        },
        "context": {
            "repo": str(repo),
            "project": repo.name,
            "branch": run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
        },
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": ["bootstrap", "git-history"],
        "sensitivity": 1,
        "redaction_hints": [],
    }


def ingest_events(
    api_base_url: str,
    token: str,
    consumer: str,
    role: str,
    workspace: str,
    user: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user,
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{api_base_url.rstrip('/')}/v1/events/batch",
        headers=headers,
        json={"events": events},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap Open Timeline Engine with existing git commit history."
    )
    parser.add_argument("--repo", default=".", help="Path to git repository")
    parser.add_argument("--api-url", default="http://localhost:8080", help="TCE API base URL")
    parser.add_argument("--token", default="local-dev-token", help="Bearer token")
    parser.add_argument("--consumer", default="git-bootstrap", help="Consumer ID")
    parser.add_argument("--role", default="user", help="Role header")
    parser.add_argument("--workspace", default="personal", help="Workspace scope")
    parser.add_argument("--user", default="git-bootstrap", help="User scope")
    parser.add_argument("--max-commits", type=int, default=150, help="Number of latest commits to import")
    parser.add_argument("--since", default=None, help="Optional git rev-list date, e.g. '2025-01-01'")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"Not a git repo: {repo}")

    log_args = [
        "log",
        f"-n{max(1, args.max_commits)}",
        "--date=iso-strict",
        "--pretty=format:%H%x1f%aI%x1f%an%x1f%s%x1e",
    ]
    if args.since:
        log_args.insert(1, f"--since={args.since}")
    commits = parse_commits(run_git(repo, log_args))
    if not commits:
        print("No commits found for import.")
        return

    events = []
    for commit in commits:
        files = file_list_for_commit(repo, commit["sha"])
        events.append(build_event(repo, commit, files))

    result = ingest_events(
        args.api_url,
        args.token,
        args.consumer,
        args.role,
        args.workspace,
        args.user,
        events,
    )
    print(json.dumps({"imported_commits": len(commits), "api_result": result}, indent=2))


if __name__ == "__main__":
    main()
