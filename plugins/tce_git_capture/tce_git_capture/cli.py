from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import requests
import typer
from platformdirs import user_config_dir

app = typer.Typer(help="TCE Git capture plugin")

CONFIG_DIR = Path(user_config_dir("open-timeline-engine", "tce")) / "git_capture"
QUEUE_PATH = CONFIG_DIR / "queue.jsonl"


POST_COMMIT_HOOK = "#!/bin/sh\ntce-git-capture capture-commit --repo .\n"
PRE_PUSH_HOOK = "#!/bin/sh\ntce-git-capture capture-prepush --repo .\n"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _api_config() -> tuple[str, str]:
    return (
        os.getenv("TCE_API_BASE_URL", "http://localhost:8080").rstrip("/"),
        os.getenv("TCE_API_TOKEN", "local-dev-token"),
    )


def _user_headers() -> dict[str, str]:
    api_token = os.getenv("TCE_API_TOKEN", "local-dev-token")
    consumer = os.getenv("TCE_USER_CONSUMER_ID", "git-capture")
    workspace = os.getenv("TCE_MCP_WORKSPACE_ID", "personal")
    user_id = os.getenv("TCE_USER_ID", consumer)
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "user",
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user_id,
    }


def _send_event(event: dict) -> bool:
    api, _ = _api_config()
    headers = _user_headers()
    try:
        response = requests.post(
            f"{api}/v1/events",
            json=event,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with QUEUE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        return False


def _post_event_once(api: str, headers: dict[str, str], event: dict) -> bool:
    try:
        endpoint = str(event.get("_tce_endpoint") or "/v1/events")
        body = event.get("body") if endpoint != "/v1/events" else event
        response = requests.post(
            f"{api}{endpoint}",
            json=body,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception:
        return False


def _send_completion(completion: dict) -> bool:
    api, _ = _api_config()
    headers = _user_headers()
    wrapped = {"_tce_endpoint": "/v1/completions", "body": completion}
    if _post_event_once(api, headers, wrapped):
        return True
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(wrapped) + "\n")
    return False


def _git(repo: str, *args: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()


def _numstat(repo: str, commit: str) -> list[dict[str, str]]:
    out = _git(repo, "show", "--numstat", "--format=", commit)
    lines = [line.split("\t") for line in out.splitlines() if line.strip()]
    return [{"added": parts[0], "deleted": parts[1], "path": parts[2]} for parts in lines if len(parts) >= 3]


@app.command("install")
def install_hooks(
    repo: Annotated[str, typer.Option(".")],
    enable_prepush: Annotated[bool, typer.Option("--enable-prepush/--no-enable-prepush")] = True,
) -> None:
    hooks_dir = Path(repo) / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    post_commit = hooks_dir / "post-commit"
    post_commit.write_text(POST_COMMIT_HOOK, encoding="utf-8")
    post_commit.chmod(0o755)

    if enable_prepush:
        pre_push = hooks_dir / "pre-push"
        pre_push.write_text(PRE_PUSH_HOOK, encoding="utf-8")
        pre_push.chmod(0o755)

    typer.echo(f"hooks installed at {hooks_dir}")


@app.command("capture-commit")
def capture_commit(
    repo: Annotated[str, typer.Option(".")],
    include_full_diff: Annotated[bool, typer.Option("--include-full-diff/--no-include-full-diff")] = False,
    sensitivity: Annotated[int, typer.Option(min=0, max=3)] = 1,
) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    title = _git(repo, "log", "-1", "--pretty=%s")
    body = _git(repo, "log", "-1", "--pretty=%b")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    stats = _numstat(repo, commit)
    diff = _git(repo, "show", "--format=", commit) if include_full_diff else None

    event = {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": "github",
        "domain": "coding",
        "task_type": "code_change",
        "event_type": "CODE_CHANGE",
        "title": title,
        "payload": {
            "commit": commit,
            "message": body,
            "numstat": stats,
            "diff": diff,
            "include_full_diff": include_full_diff,
        },
        "context": {"repo": str(Path(repo).resolve()), "branch": branch},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": {"success": True, "metrics": {"files_changed": len(stats)}, "followups": []},
        "style": None,
        "links": None,
        "tags": ["git", "commit"],
        "sensitivity": sensitivity,
        "redaction_hints": [],
    }

    event_sent = _send_event(event)
    files = [str(item["path"]) for item in stats]
    change_summary = {
        str(item["path"]): {
            "added": 0 if item["added"] == "-" else int(item["added"]),
            "removed": 0 if item["deleted"] == "-" else int(item["deleted"]),
            "intent": title[:160],
        }
        for item in stats
    }
    completion_sent = _send_completion(
        {
            "session_id": os.getenv("TCE_MCP_SESSION_ID", "git"),
            "completion_key": f"git:{commit}",
            "source": "git-post-commit",
            "state": "succeeded",
            "title": title[:160],
            "payload": {"files": files},
            "decision": (body.strip() or f"Committed {title}")[:500],
            "outcome": {"status": "succeeded", "next_step": "Continue from the committed file anchors."},
            "git": {"repo": str(Path(repo).resolve()), "branch": branch, "commit": commit},
            "anchors": [{"file": path} for path in files[:40]],
            "change_summary": change_summary,
            "milestone_schema": "v1",
        }
    )
    typer.echo(
        "commit and completion captured"
        if event_sent and completion_sent
        else "capture queued; run tce-git-capture replay"
    )


@app.command("capture-prepush")
def capture_prepush(
    repo: Annotated[str, typer.Option(".")],
    sensitivity: Annotated[int, typer.Option(min=0, max=3)] = 1,
) -> None:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    event = {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": "github",
        "domain": "ops",
        "task_type": "prepush_check",
        "event_type": "TASK_STEP",
        "title": f"Pre-push hook on {branch}",
        "payload": {"branch": branch},
        "context": {"repo": str(Path(repo).resolve())},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": {"success": True, "metrics": {}, "followups": []},
        "style": None,
        "links": None,
        "tags": ["git", "prepush"],
        "sensitivity": sensitivity,
        "redaction_hints": [],
    }
    sent = _send_event(event)
    typer.echo("pre-push captured" if sent else "pre-push queued")


@app.command("replay")
def replay(
    concurrency: Annotated[int, typer.Option(min=1, max=16, help="Parallel replay requests")] = _env_int(
        "TCE_REPLAY_CONCURRENCY", 4
    ),
) -> None:
    if not QUEUE_PATH.exists():
        typer.echo("queue empty")
        return

    api, _ = _api_config()
    headers = _user_headers()
    with QUEUE_PATH.open("r", encoding="utf-8") as fh:
        lines = [line.strip() for line in fh.readlines() if line.strip()]

    remaining: list[str] = []
    sent = 0
    parsed: list[tuple[str, dict]] = []
    for line in lines:
        try:
            parsed.append((line, json.loads(line)))
        except json.JSONDecodeError:
            remaining.append(line)

    if parsed:
        workers = max(1, min(concurrency, len(parsed)))
        results = [False] * len(parsed)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map: dict[Future[bool], int] = {
                pool.submit(_post_event_once, api, headers, item[1]): idx for idx, item in enumerate(parsed)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    results[idx] = bool(future.result())
                except Exception:
                    results[idx] = False

        for idx, (line, _event) in enumerate(parsed):
            if results[idx]:
                sent += 1
            else:
                remaining.append(line)

    with QUEUE_PATH.open("w", encoding="utf-8") as fh:
        if remaining:
            fh.write("\n".join(remaining) + "\n")

    typer.echo(f"replayed={sent} remaining={len(remaining)}")
