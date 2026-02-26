from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import requests
import typer
from platformdirs import user_config_dir

app = typer.Typer(help="TCE CLI capture plugin")

CONFIG_DIR = Path(user_config_dir("open-timeline-engine", "tce")) / "cli_capture"
QUEUE_PATH = CONFIG_DIR / "queue.jsonl"
DEFAULT_SESSION_ID = "default"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _api_config() -> tuple[str, str]:
    api = os.getenv("TCE_API_BASE_URL", "http://localhost:8080").rstrip("/")
    token = os.getenv("TCE_API_TOKEN", "local-dev-token")
    return api, token


def _enqueue(event: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _send_event(event: dict) -> bool:
    api, token = _api_config()
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
        _enqueue(event)
        return False


def _post_event_once(api: str, headers: dict[str, str], event: dict) -> bool:
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
        return False


def _safe_session_id(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    cleaned = cleaned.strip("-")
    return cleaned or DEFAULT_SESSION_ID


def _advisor_state_path(session_id: str) -> Path:
    return CONFIG_DIR / f"advisor_state_{_safe_session_id(session_id)}.json"


def _load_advisor_state(
    session_id: str,
    activation_keyword: str,
    stop_keyword: str,
    timeout_minutes: int,
) -> dict:
    path = _advisor_state_path(session_id)
    state = {
        "session_id": _safe_session_id(session_id),
        "active": False,
        "activation_keyword": activation_keyword,
        "stop_keyword": stop_keyword,
        "timeout_minutes": timeout_minutes,
        "activated_at": None,
        "expires_at": None,
        "last_message_at": None,
        "history": [],
        "takeover_context": {},
    }
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            pass
    state["activation_keyword"] = activation_keyword
    state["stop_keyword"] = stop_keyword
    state["timeout_minutes"] = timeout_minutes

    expires_at = state.get("expires_at")
    if state.get("active") and isinstance(expires_at, str):
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(tz=UTC):
                state["active"] = False
                state["expires_at"] = None
        except Exception:
            state["active"] = False
            state["expires_at"] = None

    if not isinstance(state.get("history"), list):
        state["history"] = []
    if not isinstance(state.get("takeover_context"), dict):
        state["takeover_context"] = {}

    return state


def _save_advisor_state(session_id: str, state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _advisor_state_path(session_id).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _state_snapshot(state: dict, action: str, tool_called: bool, note: str | None = None) -> dict:
    payload = {
        "session_id": state.get("session_id"),
        "active": bool(state.get("active")),
        "activation_keyword": state.get("activation_keyword"),
        "stop_keyword": state.get("stop_keyword"),
        "timeout_minutes": state.get("timeout_minutes"),
        "activated_at": state.get("activated_at"),
        "expires_at": state.get("expires_at"),
        "last_message_at": state.get("last_message_at"),
        "action": action,
        "tool_called": tool_called,
    }
    if note:
        payload["note"] = note
    return payload


def _advice_headers() -> dict[str, str]:
    api_token = os.getenv("TCE_API_TOKEN", "local-dev-token")
    consumer = os.getenv("TCE_ADVISOR_CONSUMER_ID", "cli-advisor")
    workspace = os.getenv("TCE_MCP_WORKSPACE_ID", "personal")
    user_id = os.getenv("TCE_ADVISOR_USER_ID", consumer)
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "advisor",
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user_id,
    }


def _user_headers() -> dict[str, str]:
    api_token = os.getenv("TCE_API_TOKEN", "local-dev-token")
    consumer = (
        os.getenv("TCE_USER_CONSUMER_ID")
        or os.getenv("TCE_MCP_EXECUTOR_CONSUMER_ID")
        or os.getenv("TCE_MCP_CONSUMER_ID")
        or "codex-executor"
    )
    workspace = (
        os.getenv("TCE_USER_WORKSPACE_ID")
        or os.getenv("TCE_MCP_WORKSPACE_ID")
        or "personal"
    )
    user_id = (
        os.getenv("TCE_USER_ID")
        or os.getenv("TCE_MCP_EXECUTOR_USER_ID")
        or os.getenv("TCE_MCP_USER_ID")
        or consumer
    )
    role = os.getenv("TCE_USER_ROLE", "executor")
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user_id,
    }


def _json_option(value: str, option_name: str) -> dict:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{option_name} must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{option_name} must be a JSON object")
    return data


def _trim_text(value: str | None, max_chars: int) -> str:
    if value is None:
        return ""
    text = value.strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _append_history_entry(
    state: dict,
    message: str,
    task: str,
    executor_output: str | None,
    max_items: int,
    max_chars: int,
) -> None:
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "ts": datetime.now(tz=UTC).isoformat(),
            "message": _trim_text(message, max_chars),
            "task": _trim_text(task, max_chars),
            "executor_output": _trim_text(executor_output, max_chars) if executor_output else "",
        }
    )
    if len(history) > max_items:
        history = history[-max_items:]
    state["history"] = history


def _history_summary(state: dict, window: int, max_chars: int) -> str:
    history = state.get("history")
    if not isinstance(history, list) or not history:
        return ""
    entries = history[-window:]
    lines: list[str] = []
    for idx, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            continue
        message = _trim_text(str(item.get("message", "")), 240)
        task = _trim_text(str(item.get("task", "")), 240)
        executor_output = _trim_text(str(item.get("executor_output", "")), 240)
        line = f"{idx}. user={message}"
        if task:
            line += f" | task={task}"
        if executor_output:
            line += f" | executor={executor_output}"
        lines.append(line)
    return _trim_text(" ; ".join(lines), max_chars)


def _persona_defaults(mode: str) -> tuple[str, str]:
    normalized = mode.strip().lower()
    if normalized == "naruto":
        return "hey kurama take over", "kurama stand down"
    if normalized in {"shadow", "shadowmode", "shadow_mode"}:
        return "hey igris take over,hey beru take over", "shadow stand down"
    return "hey advisor take over", "advisor stand down"


def _persona_activation_ack(persona_mode: str, matched_phrase: str | None) -> str:
    normalized = persona_mode.strip().lower()
    phrase = (matched_phrase or "").lower()
    if normalized in {"shadow", "shadowmode", "shadow_mode"}:
        if "beru" in phrase:
            return "Yes, My liege."
        if "igris" in phrase:
            return "My liege"
        return "My liege"
    if normalized == "naruto":
        return "Ok brat, I got it."
    return "Advisor mode enabled."


def _normalize_trigger_text(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in value)
    return " ".join(cleaned.split())


def _persona_mode_override(persona_mode: str, message_text: str) -> str | None:
    normalized_mode = persona_mode.strip().lower()
    normalized_text = _normalize_trigger_text(message_text)
    if not normalized_text:
        return None

    if normalized_mode in {"shadow", "shadowmode", "shadow_mode"}:
        names = ("beru", "igris")
    elif normalized_mode == "naruto":
        names = ("kurama",)
    else:
        names = ("advisor",)

    for name in names:
        if f"{name} suggest" in normalized_text:
            return "suggest"
        if f"{name} take over" in normalized_text or f"{name} takeover" in normalized_text:
            return "takeover"
    return None


def _self_heal_requested(persona_mode: str, message_text: str, custom_keywords: str | None) -> bool:
    normalized_message = _normalize_trigger_text(message_text)
    if not normalized_message:
        return False
    if isinstance(custom_keywords, str) and custom_keywords.strip():
        for item in custom_keywords.split(","):
            candidate = _normalize_trigger_text(item.strip())
            if candidate and candidate in normalized_message:
                return True
    normalized_mode = persona_mode.strip().lower()
    names: tuple[str, ...]
    if normalized_mode in {"shadow", "shadowmode", "shadow_mode"}:
        names = ("beru", "igris")
    elif normalized_mode == "naruto":
        names = ("kurama",)
    else:
        names = ("advisor",)
    for name in names:
        if f"{name} self heal" in normalized_message:
            return True
    fallback_keywords = (
        "self heal takeover",
        "self heal and continue",
        "takeover self heal",
    )
    return any(token in normalized_message for token in fallback_keywords)


def _should_keep_prior_objective(message_text: str) -> bool:
    normalized = _normalize_trigger_text(message_text)
    if not normalized:
        return True
    control_fragments = (
        "why did you stop",
        "stopped",
        "again stopped",
        "still stopped",
        "continue",
        "go on",
        "resume",
        "take over",
        "takeover",
        "self heal",
        "what happened",
        "status",
    )
    if any(fragment in normalized for fragment in control_fragments):
        return True
    if normalized.endswith(" ?") or "?" in message_text[-120:]:
        return True
    return False


def _strip_activation_prefix(message_text: str, activation_keywords: str) -> str:
    text = message_text.strip()
    if not text:
        return ""
    normalized_text = _normalize_trigger_text(text)
    for item in activation_keywords.split(","):
        phrase = _normalize_trigger_text(item.strip())
        if not phrase:
            continue
        if normalized_text.startswith(phrase):
            remainder = normalized_text[len(phrase) :].strip()
            remainder = re.sub(r"^(and|then)\s+", "", remainder)
            return remainder.strip()
    return ""


def _project_root_for_self_heal() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "scripts" / "install.sh").exists():
            return parent
    return Path.cwd()


def _run_self_heal_fix(stack_target: str, timeout_seconds: int) -> dict:
    requested_target = (stack_target or "auto").strip().lower()
    if requested_target not in {"full", "lite", "auto", "all"}:
        requested_target = "auto"
    repo_root = _project_root_for_self_heal()
    cmd = ["/bin/bash", "-lc", f'cd {shlex.quote(str(repo_root))} && ./scripts/install.sh fix {requested_target}']
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=max(60, timeout_seconds),
        )
        combined = f"{result.stdout}\n{result.stderr}".strip()
        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stack_target": requested_target,
            "repo_root": str(repo_root),
            "log_tail": _trim_text(combined, 6000),
        }
    except subprocess.TimeoutExpired as exc:
        timed_out_log = f"{exc.stdout or ''}\n{exc.stderr or ''}".strip()
        return {
            "ok": False,
            "exit_code": -1,
            "stack_target": requested_target,
            "repo_root": str(repo_root),
            "error": f"self-heal timeout after {max(60, timeout_seconds)}s",
            "log_tail": _trim_text(timed_out_log, 6000),
        }


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _looks_like_question(value: str | None) -> bool:
    if value is None:
        return False
    text = value.strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    normalized = _normalize_trigger_text(text)
    if not normalized:
        return False
    soft_question_prefixes = (
        "if you want",
        "if you d like",
        "if you would like",
        "do you want",
        "would you like",
        "would you want",
        "shall i",
        "should i",
        "can i",
        "can we",
        "want me to",
        "need me to",
        "do you want me to",
        "would you like me to",
        "want us to",
        "need us to",
        "let me know if you want",
        "let me know if you d like",
        "let me know if you would like",
        "tell me if you want",
        "tell me if you d like",
        "tell me if you would like",
        "say the word and i ll",
        "say the word and i will",
        "your call",
        "up to you",
    )
    if any(normalized.startswith(prefix) for prefix in soft_question_prefixes):
        return True
    soft_question_fragments = (
        "let me know if you want",
        "if you want i can",
        "if you d like i can",
        "if you would like i can",
        "if you want we can",
        "if you d like we can",
        "if you would like we can",
        "i can do that if you want",
        "i can continue if you want",
        "i can handle that if you want",
        "i can take over if you want",
        "happy to continue if you want",
        "happy to do that if you want",
        "would you like me to",
        "do you want me to",
        "want me to",
        "need me to",
        "your call",
        "up to you",
    )
    if any(fragment in normalized for fragment in soft_question_fragments):
        return True
    soft_question_patterns = (
        r"\b(if you (want|d like|would like))(?:\s+i can)?\b",
        r"\b(let me know|tell me)\s+if you (want|d like|would like)\b",
        r"\b(do|would|should|shall|can)\s+you\s+\w+",
        r"\b(do|would)\s+you\s+want\s+me\s+to\b",
        r"\bwould\s+you\s+like\s+me\s+to\b",
        r"\b(your call|up to you)\b",
    )
    if any(re.search(pattern, normalized) for pattern in soft_question_patterns):
        return True
    tail = text[-240:]
    return "?" in tail


def _looks_like_handoff(value: str | None) -> bool:
    if value is None:
        return False
    normalized = _normalize_trigger_text(value)
    if not normalized:
        return False
    handoff_fragments = (
        "ask for human confirmation",
        "ask the user",
        "ask user",
        "need your confirmation",
        "need your input",
        "need more info from you",
        "please confirm",
        "confirm before proceeding",
        "wait for confirmation",
        "user confirmation required",
        "no strong prior found proceed cautiously and ask",
        "if you want",
        "up to you",
        "your call",
    )
    return any(fragment in normalized for fragment in handoff_fragments)


def _extract_action_lines(value: str, max_items: int = 3) -> list[str]:
    actions: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        normalized = _normalize_trigger_text(line)
        if not normalized:
            continue
        if normalized in {"next steps", "recommended actions", "options"}:
            continue
        if any(
            marker in normalized
            for marker in (
                "next exploration targets",
                "pick your top",
                "choose one",
                "choose your",
                "which one",
                "i can execute immediately",
            )
        ):
            continue
        if len(line) < 5:
            continue
        actions.append(_trim_text(line, 140))
        if len(actions) >= max_items:
            break
    return actions


def _looks_like_suggestion(value: str | None) -> bool:
    if value is None:
        return False
    normalized = _normalize_trigger_text(value)
    if not normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "next exploration targets",
            "recommended next steps",
            "recommended actions",
            "options i can",
            "i can execute immediately",
            "pick your top",
            "choose one",
            "choose your top",
            "here are",
        )
    ):
        return True
    numbered_or_bullets = re.search(r"(^|\n)\s*(?:\d+[.)]|[-*])\s+\S+", value)
    if numbered_or_bullets is not None and "takeover active" not in normalized:
        return True
    return False


def _build_takeover_fallback_response(
    task: str,
    takeover_context: dict,
    advice: dict | None,
    suggested_actions: list[str] | None = None,
) -> str:
    objective = takeover_context.get("objective", task)
    if not isinstance(objective, str) or not objective.strip():
        objective = task
    objective = _trim_text(objective, 280)

    action_items: list[str] = []
    if suggested_actions:
        for item in suggested_actions:
            if isinstance(item, str) and item.strip():
                action_items.append(_trim_text(item.strip(), 140))
                if len(action_items) >= 3:
                    break
    if isinstance(advice, dict):
        do_dont = advice.get("do_dont")
        if isinstance(do_dont, dict):
            do_items = do_dont.get("do")
            if isinstance(do_items, list):
                for item in do_items:
                    if isinstance(item, str) and item.strip():
                        action_items.append(_trim_text(item.strip(), 140))
                        if len(action_items) >= 3:
                            break
        if not action_items:
            workflows = advice.get("relevant_workflows")
            if isinstance(workflows, list):
                for item in workflows:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    if isinstance(name, str) and name.strip():
                        action_items.append(f"apply workflow: {_trim_text(name.strip(), 100)}")
                        if len(action_items) >= 2:
                            break

    if action_items:
        return (
            f"Takeover active. Proceeding without clarification questions for '{objective}'. "
            f"Next actions: {'; '.join(action_items)}."
        )
    return (
        f"Takeover active. Proceeding with safest defaults for '{objective}' "
        "and continuing execution without clarification questions."
    )


def _call_clone_advice(
    task: str,
    app_context: dict,
    constraints: dict,
    takeover_context: dict,
    message_delta: dict,
    interaction_id: str | None,
    executor_output: str | None,
    allow_fallback: bool,
) -> dict:
    api, _ = _api_config()
    body = {
        "task": task,
        "app_context": app_context,
        "constraints": constraints,
        "takeover_context": takeover_context,
        "message_delta": message_delta,
        "interaction_id": interaction_id,
        "executor_output": executor_output,
        "allow_fallback": allow_fallback,
    }
    response = requests.post(
        f"{api}/v1/clone/advice",
        json=body,
        headers=_advice_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _call_takeover_state(
    session_id: str,
    persona_mode: str,
    activation_keywords: str | None,
    stop_keywords: str | None,
) -> dict:
    api, _ = _api_config()
    params: dict[str, str] = {
        "session_id": _safe_session_id(session_id),
        "persona_mode": persona_mode,
    }
    if activation_keywords is not None:
        params["activation_keywords"] = activation_keywords
    if stop_keywords is not None:
        params["stop_keywords"] = stop_keywords
    response = requests.get(
        f"{api}/v1/takeover/state",
        params=params,
        headers=_advice_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _call_takeover_reset(
    session_id: str,
    persona_mode: str,
    activation_keywords: str | None,
    stop_keywords: str | None,
) -> dict:
    api, _ = _api_config()
    params: dict[str, str] = {
        "session_id": _safe_session_id(session_id),
        "persona_mode": persona_mode,
    }
    if activation_keywords is not None:
        params["activation_keywords"] = activation_keywords
    if stop_keywords is not None:
        params["stop_keywords"] = stop_keywords
    response = requests.post(
        f"{api}/v1/takeover/reset",
        params=params,
        headers=_advice_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _call_takeover_step(body: dict) -> dict:
    api, _ = _api_config()
    response = requests.post(
        f"{api}/v1/takeover/step",
        json=body,
        headers=_advice_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _call_external_advisor_command(command: str, payload: dict, timeout_seconds: int) -> dict:
    if not command.strip():
        raise RuntimeError("advisor command is empty")
    result = subprocess.run(
        shlex.split(command),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()[-2000:]
        raise RuntimeError(f"advisor command failed (exit={result.returncode}): {stderr}")
    stdout = result.stdout.strip()
    parsed_json: dict | None = None
    response_text = stdout
    if stdout.startswith("{"):
        try:
            payload_json = json.loads(stdout)
            if isinstance(payload_json, dict):
                parsed_json = payload_json
                maybe_response = payload_json.get("response")
                if isinstance(maybe_response, str) and maybe_response.strip():
                    response_text = maybe_response.strip()
        except json.JSONDecodeError:
            parsed_json = None
    return {
        "response": response_text,
        "raw_json": parsed_json,
        "stderr": result.stderr.strip()[-2000:],
        "command": command,
    }


@app.command("run")
def run_command(
    command: Annotated[str, typer.Argument(help="Command to execute and capture")],
    domain: Annotated[str, typer.Option(help="Domain label for this command event")] = "coding",
    task_type: Annotated[str, typer.Option(help="Task type label for this command event")] = "command_execution",
    sensitivity: Annotated[int, typer.Option(min=0, max=3)] = 1,
    tags: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    tags = tags or []
    started_at = datetime.now(tz=UTC)
    result = subprocess.run(shlex.split(command), text=True, capture_output=True)
    success = result.returncode == 0
    event = {
        "schema_version": 1,
        "ts": started_at.isoformat(),
        "actor": "user",
        "source": "cli",
        "domain": domain,
        "task_type": task_type,
        "event_type": "COMMAND_RUN",
        "title": f"Command: {command}",
        "payload": {
            "command": command,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
            "exit_code": result.returncode,
        },
        "context": {"cwd": str(Path.cwd())},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": {"success": success, "metrics": {"exit_code": result.returncode}, "followups": []},
        "style": None,
        "links": None,
        "tags": tags,
        "sensitivity": sensitivity,
        "redaction_hints": [],
    }
    sent = _send_event(event)
    typer.echo("captured and sent" if sent else "captured and queued")
    raise typer.Exit(result.returncode)


@app.command("note")
def note(
    message: Annotated[str, typer.Argument(help="Reflection note")],
    domain: Annotated[str, typer.Option(help="Domain label for this reflection")] = "planning",
    task_type: Annotated[str, typer.Option(help="Task type label for this reflection")] = "reflection",
    sensitivity: Annotated[int, typer.Option(min=0, max=3)] = 1,
    tags: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    tags = tags or []
    event = {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": "cli",
        "domain": domain,
        "task_type": task_type,
        "event_type": "REFLECTION",
        "title": message[:120],
        "payload": {"note": message},
        "context": {"cwd": str(Path.cwd())},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": tags,
        "sensitivity": sensitivity,
        "redaction_hints": [],
    }
    sent = _send_event(event)
    typer.echo("note sent" if sent else "note queued")


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
    remaining: list[str] = []
    sent = 0
    with QUEUE_PATH.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    parsed: list[tuple[str, dict]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
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


@app.command("advisor-state")
def advisor_state(
    session_id: Annotated[str, typer.Option(help="Chat session id")] = DEFAULT_SESSION_ID,
    persona_mode: Annotated[str, typer.Option(help="normal|naruto|shadow")] = os.getenv(
        "TCE_ADVISOR_PERSONA_MODE", "normal"
    ),
    activation_keyword: Annotated[str | None, typer.Option(help="Activation phrase override")] = (
        os.getenv("TCE_ADVISOR_ACTIVATION_KEYWORD") or None
    ),
    stop_keyword: Annotated[str | None, typer.Option(help="Stop phrase override")] = (
        os.getenv("TCE_ADVISOR_STOP_KEYWORD") or None
    ),
    timeout_minutes: Annotated[int, typer.Option(min=1, help="Auto-disable timeout in minutes")] = 30,
) -> None:
    default_activation, default_stop = _persona_defaults(persona_mode)
    resolved_activation = activation_keyword if activation_keyword else default_activation
    resolved_stop = stop_keyword if stop_keyword else default_stop
    try:
        state = _call_takeover_state(
            session_id=session_id,
            persona_mode=persona_mode,
            activation_keywords=resolved_activation,
            stop_keywords=resolved_stop,
        )
        typer.echo(json.dumps({"action": "status", "state": state}, indent=2))
    except Exception as exc:
        fallback_state = _load_advisor_state(
            session_id=session_id,
            activation_keyword=resolved_activation,
            stop_keyword=resolved_stop,
            timeout_minutes=timeout_minutes,
        )
        fallback_state["persona_mode"] = persona_mode
        payload = _state_snapshot(fallback_state, action="status", tool_called=False)
        payload["warning"] = f"takeover API unavailable: {exc}"
        typer.echo(json.dumps(payload, indent=2))


@app.command("advisor-reset")
def advisor_reset(
    session_id: Annotated[str, typer.Option(help="Chat session id")] = DEFAULT_SESSION_ID,
) -> None:
    persona_mode = os.getenv("TCE_ADVISOR_PERSONA_MODE", "normal")
    default_activation, default_stop = _persona_defaults(persona_mode)
    resolved_activation = os.getenv("TCE_ADVISOR_ACTIVATION_KEYWORD") or default_activation
    resolved_stop = os.getenv("TCE_ADVISOR_STOP_KEYWORD") or default_stop
    try:
        state = _call_takeover_reset(
            session_id=session_id,
            persona_mode=persona_mode,
            activation_keywords=resolved_activation,
            stop_keywords=resolved_stop,
        )
        typer.echo(json.dumps({"session_id": _safe_session_id(session_id), "action": "reset", "state": state}, indent=2))
    except Exception:
        path = _advisor_state_path(session_id)
        if path.exists():
            path.unlink()
        typer.echo(json.dumps({"session_id": _safe_session_id(session_id), "action": "reset", "active": False}, indent=2))


@app.command("advisor-chat")
def advisor_chat(
    message: Annotated[str, typer.Argument(help="Incoming user message text")],
    session_id: Annotated[str, typer.Option(help="Chat session id")] = DEFAULT_SESSION_ID,
    persona_mode: Annotated[str, typer.Option(help="normal|naruto|shadow")] = os.getenv(
        "TCE_ADVISOR_PERSONA_MODE", "normal"
    ),
    activation_keyword: Annotated[str | None, typer.Option(help="Keyword override to enable continuous advisor calls")] = (
        os.getenv("TCE_ADVISOR_ACTIVATION_KEYWORD") or None
    ),
    stop_keyword: Annotated[str | None, typer.Option(help="Keyword override to disable continuous advisor calls")] = (
        os.getenv("TCE_ADVISOR_STOP_KEYWORD") or None
    ),
    timeout_minutes: Annotated[int, typer.Option(min=1, help="Auto-disable timeout in minutes")] = 30,
    task: Annotated[str | None, typer.Option(help="Task override; defaults to message text")] = None,
    app_context_json: Annotated[str, typer.Option(help="JSON object passed as app_context")] = "{}",
    constraints_json: Annotated[str, typer.Option(help="JSON object passed as constraints")] = "{}",
    interaction_id: Annotated[str | None, typer.Option(help="Interaction id override")] = None,
    executor_output: Annotated[str | None, typer.Option(help="Executor output context")] = None,
    takeover_context_json: Annotated[
        str, typer.Option(help="JSON object merged into persistent takeover context")
    ] = "{}",
    objective: Annotated[str | None, typer.Option(help="Objective summary for advisor context")] = None,
    current_state: Annotated[str | None, typer.Option(help="Current state summary for advisor context")] = None,
    open_decisions: Annotated[str | None, typer.Option(help="Open decisions summary for advisor context")] = None,
    context_window: Annotated[int, typer.Option(min=1, max=30, help="Recent message count for context summary")] = 8,
    context_max_chars: Annotated[
        int, typer.Option(min=500, max=12000, help="Max chars for serialized takeover context text fields")
    ] = 3000,
    auto_handoff_on_question: Annotated[
        bool,
        typer.Option(
            help="When active, promote to takeover mode if executor output looks like a question"
        ),
    ] = _env_bool("TCE_ADVISOR_AUTO_HANDOFF_ON_QUESTION", True),
    allow_fallback: Annotated[bool, typer.Option(help="Allow timeline fallback when clone mode is off")] = True,
    real_takeover: Annotated[bool, typer.Option(help="Enable external advisor takeover flow")] = _env_bool(
        "TCE_ADVISOR_REAL_TAKEOVER", False
    ),
    real_takeover_mode: Annotated[str, typer.Option(help="suggest|takeover")] = os.getenv(
        "TCE_ADVISOR_REAL_TAKEOVER_MODE", "suggest"
    ),
    advisor_command: Annotated[
        str | None,
        typer.Option(help="External advisor command; receives JSON via stdin and returns text/JSON"),
    ] = os.getenv("TCE_ADVISOR_COMMAND"),
    advisor_timeout_seconds: Annotated[int, typer.Option(min=5, max=300)] = _env_int(
        "TCE_ADVISOR_TIMEOUT_SECONDS", 45
    ),
    enrich_with_tce: Annotated[bool, typer.Option(help="Also call TCE clone advice in real takeover mode")] = _env_bool(
        "TCE_ADVISOR_TCE_ENRICH", True
    ),
    self_heal_enabled: Annotated[
        bool, typer.Option(help="Allow self-heal keyword to run install.sh fix and resume takeover")
    ] = _env_bool("TCE_ADVISOR_SELF_HEAL_ENABLED", True),
    self_heal_keywords: Annotated[
        str | None,
        typer.Option(help="Comma-separated self-heal phrases (example: beru self heal,igris self heal)"),
    ] = os.getenv("TCE_ADVISOR_SELF_HEAL_KEYWORDS"),
    self_heal_stack: Annotated[
        str, typer.Option(help="Stack target for self-heal fix: auto|full|lite|all")
    ] = os.getenv("TCE_ADVISOR_SELF_HEAL_STACK", "auto"),
    self_heal_timeout_seconds: Annotated[
        int, typer.Option(min=60, max=3600, help="Timeout for self-heal fix command")
    ] = _env_int("TCE_ADVISOR_SELF_HEAL_TIMEOUT_SECONDS", 900),
    auto_continue_turns: Annotated[
        int,
        typer.Option(min=0, max=25, help="Run additional internal takeover steps after this message"),
    ] = _env_int("TCE_ADVISOR_AUTO_CONTINUE_TURNS", 0),
    auto_continue_message: Annotated[
        str,
        typer.Option(help="Message used for each internal auto-continue takeover step"),
    ] = os.getenv(
        "TCE_ADVISOR_AUTO_CONTINUE_MESSAGE",
        "continue execution on the same objective with decisive progress update",
    ),
) -> None:
    original_message = message
    default_activation, default_stop = _persona_defaults(persona_mode)
    resolved_activation = activation_keyword if activation_keyword else default_activation
    resolved_stop = stop_keyword if stop_keyword else default_stop
    app_context = _json_option(app_context_json, "app_context_json")
    constraints = _json_option(constraints_json, "constraints_json")
    context_update = _json_option(takeover_context_json, "takeover_context_json")
    configured_takeover_mode = real_takeover_mode.strip().lower()
    if configured_takeover_mode not in {"suggest", "takeover"}:
        configured_takeover_mode = "suggest"

    self_heal_meta: dict | None = None
    prior_objective: str | None = None
    effective_message = message
    if self_heal_enabled and _self_heal_requested(persona_mode, original_message, self_heal_keywords):
        try:
            prior_state = _call_takeover_state(
                session_id=session_id,
                persona_mode=persona_mode,
                activation_keywords=resolved_activation,
                stop_keywords=resolved_stop,
            )
            takeover_context_prior = prior_state.get("takeover_context", {})
            if isinstance(takeover_context_prior, dict):
                maybe_objective = takeover_context_prior.get("objective")
                if isinstance(maybe_objective, str) and maybe_objective.strip():
                    prior_objective = maybe_objective.strip()
        except Exception:
            prior_objective = None

        self_heal_meta = _run_self_heal_fix(
            stack_target=self_heal_stack,
            timeout_seconds=self_heal_timeout_seconds,
        )
        if not self_heal_meta.get("ok", False):
            typer.echo(
                json.dumps(
                    {
                        "session_id": _safe_session_id(session_id),
                        "action": "self_heal_failed",
                        "tool_called": False,
                        "self_heal": self_heal_meta,
                        "note": "self-heal fix failed; takeover not resumed",
                    },
                    indent=2,
                )
            )
            raise typer.Exit(1)
        activation_candidates = [item.strip() for item in resolved_activation.split(",") if item.strip()]
        effective_message = activation_candidates[0] if activation_candidates else default_activation

    if not task and not prior_objective:
        try:
            state_probe = _call_takeover_state(
                session_id=session_id,
                persona_mode=persona_mode,
                activation_keywords=resolved_activation,
                stop_keywords=resolved_stop,
            )
            probe_context = state_probe.get("takeover_context", {})
            if isinstance(probe_context, dict):
                maybe_objective = probe_context.get("objective")
                if isinstance(maybe_objective, str) and maybe_objective.strip():
                    prior_objective = maybe_objective.strip()
        except Exception:
            prior_objective = None

    if task:
        resolved_task = task
    elif prior_objective and _should_keep_prior_objective(original_message):
        resolved_task = prior_objective
    else:
        stripped_activation_objective = _strip_activation_prefix(original_message, resolved_activation)
        resolved_task = stripped_activation_objective if stripped_activation_objective else original_message
    resolved_interaction = interaction_id or f"{_safe_session_id(session_id)}-{int(datetime.now(tz=UTC).timestamp())}"
    takeover_context_payload = dict(context_update)
    if objective:
        takeover_context_payload["objective"] = objective.strip()
    if current_state:
        takeover_context_payload["current_state"] = current_state.strip()
    if open_decisions:
        takeover_context_payload["open_decisions"] = open_decisions.strip()
    if "objective" not in takeover_context_payload:
        takeover_context_payload["objective"] = resolved_task

    message_delta = {
        "new_user_message": _trim_text(original_message, min(2000, context_max_chars)),
        "latest_executor_output": _trim_text(executor_output, min(2000, context_max_chars)) if executor_output else "",
        "new_blockers": constraints.get("new_blockers"),
        "updated_plan_step": constraints.get("updated_plan_step"),
    }
    default_mode = "takeover" if (resolved_activation != default_activation) else configured_takeover_mode
    policy = {
        "safety_policy": os.getenv("TCE_TAKEOVER_SAFETY_POLICY", "high-risk-pause"),
        "confirm_keyword": os.getenv("TCE_TAKEOVER_CONFIRM_KEYWORD", "confirm"),
        "deny_keyword": os.getenv("TCE_TAKEOVER_DENY_KEYWORD", "abort"),
        "timeout_minutes": timeout_minutes,
        "auto_handoff_on_question": auto_handoff_on_question,
    }

    try:
        step = _call_takeover_step(
            {
                "message": effective_message,
                "session_id": _safe_session_id(session_id),
                "persona_mode": persona_mode,
                "activation_keywords": resolved_activation,
                "stop_keywords": resolved_stop,
                "activation_mode_default": default_mode,
                "policy": policy,
                "task": resolved_task,
                "app_context": app_context,
                "constraints": constraints,
                "takeover_context": takeover_context_payload,
                "message_delta": message_delta,
                "interaction_id": resolved_interaction,
                "executor_output": executor_output,
                "allow_fallback": allow_fallback,
                "real_takeover": real_takeover,
                "real_takeover_mode": configured_takeover_mode,
                "advisor_command": advisor_command,
                "advisor_timeout_seconds": advisor_timeout_seconds,
                "enrich_with_tce": enrich_with_tce,
            }
        )
    except requests.HTTPError as exc:
        payload = {
            "session_id": _safe_session_id(session_id),
            "action": "advisor_call_failed",
            "tool_called": True,
            "note": str(exc),
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(1) from exc

    state = step.get("state", {})
    action = str(step.get("action", "advisor_called"))
    mode = str(state.get("mode", configured_takeover_mode))
    clone_advice = step.get("clone_advice", {})
    final_response = step.get("final_response")
    takeover_enforcement = step.get("takeover_enforcement", {})
    auto_handoff = step.get("auto_handoff", {})

    if action in {"inactive", "stopped"}:
        typer.echo(
            json.dumps(
                {
                    "session_id": _safe_session_id(session_id),
                    "action": action,
                    "tool_called": True,
                    "state": state,
                    "note": step.get("note"),
                },
                indent=2,
            )
        )
        return

    auto_continue_trace: list[dict] = []
    if (
        mode == "takeover"
        and auto_continue_turns > 0
        and step.get("safety_decision") not in {"confirm_required", "blocked"}
    ):
        current_step = step
        for idx in range(1, auto_continue_turns + 1):
            try:
                next_step = _call_takeover_step(
                    {
                        "message": auto_continue_message,
                        "session_id": _safe_session_id(session_id),
                        "persona_mode": persona_mode,
                        "activation_keywords": resolved_activation,
                        "stop_keywords": resolved_stop,
                        "activation_mode_default": default_mode,
                        "policy": policy,
                        "task": resolved_task,
                        "app_context": app_context,
                        "constraints": constraints,
                        "takeover_context": state.get("takeover_context", takeover_context_payload),
                        "message_delta": message_delta,
                        "interaction_id": f"{resolved_interaction}-auto-{idx}",
                        "executor_output": executor_output,
                        "allow_fallback": allow_fallback,
                        "real_takeover": real_takeover,
                        "real_takeover_mode": configured_takeover_mode,
                        "advisor_command": advisor_command,
                        "advisor_timeout_seconds": advisor_timeout_seconds,
                        "enrich_with_tce": enrich_with_tce,
                    }
                )
            except Exception as exc:
                auto_continue_trace.append(
                    {
                        "turn": idx,
                        "status": "error",
                        "note": str(exc),
                    }
                )
                break
            next_state = next_step.get("state", {})
            next_action = str(next_step.get("action", "advisor_called"))
            next_classification = str(next_step.get("classification", ""))
            next_safety = str(next_step.get("safety_decision", "allow"))
            auto_continue_trace.append(
                {
                    "turn": idx,
                    "status": "ok",
                    "action": next_action,
                    "classification": next_classification,
                    "safety_decision": next_safety,
                }
            )
            current_step = next_step
            state = next_state
            if next_action in {"inactive", "stopped"}:
                break
            if next_safety in {"confirm_required", "blocked"}:
                break
            # Break on weak evidence / empty responses to prevent
            # repetitive auto-continue loops with no new guidance.
            next_reason = str(next_step.get("enforcement_reason", "") or "")
            if next_reason in {"weak_evidence_decisive", "empty_fallback_decisive"}:
                break
            if not next_step.get("final_response"):
                break

        step = current_step
        action = str(step.get("action", action))
        mode = str(state.get("mode", mode))
        clone_advice = step.get("clone_advice", clone_advice)
        final_response = step.get("final_response", final_response)
        takeover_enforcement = step.get("takeover_enforcement", takeover_enforcement)
        auto_handoff = step.get("auto_handoff", auto_handoff)

    external_result: dict | None = None
    if real_takeover and advisor_command and advisor_command.strip():
        external_payload = {
            "message": message,
            "task": resolved_task,
            "session_id": _safe_session_id(session_id),
            "interaction_id": resolved_interaction,
            "persona_mode": persona_mode,
            "app_context": app_context,
            "constraints": constraints,
            "executor_output": executor_output,
            "activation_keyword": resolved_activation,
            "stop_keyword": resolved_stop,
            "takeover_context": state.get("takeover_context", takeover_context_payload),
            "message_delta": message_delta,
            "clone_advice": clone_advice,
        }
        try:
            external_result = _call_external_advisor_command(
                command=advisor_command,
                payload=external_payload,
                timeout_seconds=advisor_timeout_seconds,
            )
            if mode == "takeover":
                ext_text = external_result.get("response", "")
                ext_text = ext_text.strip() if isinstance(ext_text, str) else ""
                if not ext_text:
                    external_result["response"] = final_response or _build_takeover_fallback_response(
                        resolved_task, state.get("takeover_context", takeover_context_payload), clone_advice
                    )
                    external_result["enforced_no_questions"] = True
                elif _looks_like_question(ext_text) or _looks_like_handoff(ext_text) or _looks_like_suggestion(ext_text):
                    suggested = _extract_action_lines(ext_text) if _looks_like_suggestion(ext_text) else None
                    external_result["original_response"] = ext_text
                    external_result["response"] = _build_takeover_fallback_response(
                        resolved_task,
                        state.get("takeover_context", takeover_context_payload),
                        clone_advice,
                        suggested_actions=suggested,
                    )
                    external_result["enforced_no_questions"] = True
                    takeover_enforcement = {
                        "trigger": "external_non_decisive_response",
                        "mode": "takeover",
                        "note": "external advisor response rewritten",
                    }
                final_response = external_result.get("response", final_response)
        except Exception as exc:
            payload = {
                "session_id": _safe_session_id(session_id),
                "action": "advisor_call_failed",
                "tool_called": False,
                "state": state,
                "note": str(exc),
            }
            typer.echo(json.dumps(payload, indent=2))
            raise typer.Exit(1) from exc

    normalized_message = _normalize_trigger_text(effective_message)
    start_phrases = [
        normalized
        for normalized in (_normalize_trigger_text(item.strip()) for item in resolved_activation.split(","))
        if normalized
    ]
    matched_start_phrase = next((phrase for phrase in start_phrases if phrase in normalized_message), None)
    activation_payload = None
    if matched_start_phrase:
        persona_ack = _persona_activation_ack(persona_mode, matched_start_phrase)
        activation_payload = {
            "persona_ack": persona_ack,
            "advisor_mode": mode,
            "note": f"{persona_ack} advisor auto-call enabled for this and subsequent messages",
        }

    output = {
        "session_id": _safe_session_id(session_id),
        "action": action,
        "tool_called": True,
        "state": state,
        "external_mode": mode,
        "takeover_context": state.get("takeover_context", takeover_context_payload),
        "clone_advice": clone_advice,
        "takeover_enforcement": takeover_enforcement,
        "auto_handoff": auto_handoff,
        "safety_decision": step.get("safety_decision"),
        "citations": step.get("citations", []),
    }
    if activation_payload is not None:
        output["activation"] = activation_payload
    if external_result is not None:
        output["external_advisor"] = external_result
    if self_heal_meta is not None:
        output["self_heal"] = self_heal_meta
    if auto_continue_trace:
        output["auto_continue"] = {
            "requested_turns": auto_continue_turns,
            "completed_turns": len([item for item in auto_continue_trace if item.get("status") == "ok"]),
            "trace": auto_continue_trace,
            "final_turn_count": _safe_int(state.get("takeover_context", {}).get("turn_count"), 0),
        }

    if mode == "takeover":
        output["final_response"] = final_response
    else:
        output["advisor_suggestion"] = final_response
    typer.echo(json.dumps(output, indent=2))


@app.command("advisor-stop")
def advisor_stop(
    session_id: Annotated[str, typer.Option(help="Chat session id")] = DEFAULT_SESSION_ID,
    persona_mode: Annotated[str, typer.Option(help="normal|naruto|shadow")] = os.getenv(
        "TCE_ADVISOR_PERSONA_MODE", "normal"
    ),
) -> None:
    default_activation, default_stop = _persona_defaults(persona_mode)
    try:
        step = _call_takeover_step(
            {
                "message": default_stop,
                "session_id": _safe_session_id(session_id),
                "persona_mode": persona_mode,
                "activation_keywords": default_activation,
                "stop_keywords": default_stop,
                "activation_mode_default": "takeover",
                "policy": {
                    "safety_policy": os.getenv("TCE_TAKEOVER_SAFETY_POLICY", "high-risk-pause"),
                    "confirm_keyword": os.getenv("TCE_TAKEOVER_CONFIRM_KEYWORD", "confirm"),
                    "deny_keyword": os.getenv("TCE_TAKEOVER_DENY_KEYWORD", "abort"),
                    "timeout_minutes": 30,
                    "auto_handoff_on_question": True,
                },
                "task": "advisor stop",
            }
        )
        typer.echo(
            json.dumps(
                {
                    "session_id": _safe_session_id(session_id),
                    "action": step.get("action", "stopped"),
                    "state": step.get("state", {}),
                    "tool_called": True,
                },
                indent=2,
            )
        )
        return
    except Exception:
        state = _load_advisor_state(
            session_id=session_id,
            activation_keyword=default_activation,
            stop_keyword=default_stop,
            timeout_minutes=30,
        )
        state["active"] = False
        state["expires_at"] = None
        state["last_message_at"] = datetime.now(tz=UTC).isoformat()
        state["persona_mode"] = persona_mode
        _save_advisor_state(session_id, state)
        typer.echo(json.dumps(_state_snapshot(state, action="stopped", tool_called=False), indent=2))


@app.command("advisor-disable")
def advisor_disable(
    remove_all_sessions: Annotated[bool, typer.Option(help="Remove all stored advisor session state files")] = True,
    switch_timeline_only: Annotated[
        bool, typer.Option(help="Switch backend runtime mode to timeline_only as part of disable")
    ] = True,
) -> None:
    removed = 0
    if remove_all_sessions and CONFIG_DIR.exists():
        for path in CONFIG_DIR.glob("advisor_state_*.json"):
            try:
                path.unlink()
                removed += 1
            except Exception:
                pass

    mode_result: dict[str, str] = {"status": "not_requested"}
    if switch_timeline_only:
        api, _ = _api_config()
        try:
            response = requests.put(
                f"{api}/v1/runtime/mode",
                json={"mode": "timeline_only"},
                headers=_user_headers(),
                timeout=10,
            )
            response.raise_for_status()
            mode_result = {"status": "ok", "mode": "timeline_only"}
        except Exception as exc:
            mode_result = {"status": "failed", "error": str(exc)}

    typer.echo(
        json.dumps(
            {
                "action": "advisor_disabled",
                "removed_session_files": removed,
                "runtime_mode_update": mode_result,
                "note": "advisor keyword gate disabled and state removed",
            },
            indent=2,
        )
    )
