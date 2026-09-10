"""Host capture hook (scripts/tce_capture_input.py) and hook generator (scripts/generate_client_hooks.py).

The hook is exercised as a real subprocess with a fake UserPromptSubmit payload on stdin,
against a recording HTTP server, a dead port, a 403 server and a server that never answers
in time. Every case asserts the hook exits 0, never prints the prompt or the token, and
leaves a visible marker (``state.json`` + ``systemMessage``) whenever delivery did not happen.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# Every test below either drives the hook through ``_run_hook`` (a real ``subprocess.run``
# of scripts/tce_capture_input.py) or runs the generator CLI the same way. The marker is
# module-level because the spawn is the point of the file, not an incidental detail —
# see tests/conftest.py::_no_subprocess.
pytestmark = pytest.mark.subprocess

ROOT = Path(__file__).parents[2]
HOOK = ROOT / "scripts" / "tce_capture_input.py"
GENERATOR = ROOT / "scripts" / "generate_client_hooks.py"

HOST_TOKEN = "host-token-for-tests"
SECRET_PROMPT = "please deploy with api_key=abc123secret and tell me when done"
CLAUDE_SESSION = "11111111-2222-3333-4444-555555555555"


class _Recorder:
    def __init__(self, status: int, delay: float) -> None:
        self.status = status
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()


def _make_handler(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = {"_raw": raw.decode("utf-8", "replace")}
            with recorder.lock:
                recorder.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            if recorder.delay:
                time.sleep(recorder.delay)
            payload = json.dumps({"receipt_id": "r-1", "deduplicated": False, "capture_delivery_state": "healthy"}).encode("utf-8")
            self.send_response(recorder.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
            return

    return Handler


class _Server:
    def __init__(self, status: int = 201, delay: float = 0.0) -> None:
        self.recorder = _Recorder(status, delay)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.recorder))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def requests(self) -> list[dict[str, Any]]:
        with self.recorder.lock:
            return list(self.recorder.requests)


@pytest.fixture()
def server() -> Iterator[_Server]:
    instance = _Server()
    try:
        yield instance
    finally:
        instance.close()


def _dead_base_url() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


def _hook_env(tmp_path: Path, base_url: str, **overrides: str | None) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env: dict[str, str | None] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TCE_CAPTURE_SPOOL_DIR": str(tmp_path / "spool"),
        "TCE_HOST_CAPTURE_TOKEN": HOST_TOKEN,
        "TCE_CAPTURE_WORKSPACE": "personal",
        "TCE_CAPTURE_USER": "human-1",
        "TCE_API_BASE_URL": base_url,
    }
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


def _claude_payload(prompt: str, session_id: str = CLAUDE_SESSION) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": str(ROOT),
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


def _run_hook(env: dict[str, str], stdin_text: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), *(args or ("--client", "claude"))],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=15,
    )


def _state(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "spool" / "state.json").read_text(encoding="utf-8")))


def _spool_entries(tmp_path: Path) -> list[Path]:
    spool = tmp_path / "spool"
    if not spool.exists():
        return []
    return sorted(path for path in spool.iterdir() if path.name.endswith(".json") and path.name != "state.json")


def _stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.stdout
    loaded = json.loads(lines[0])
    assert isinstance(loaded, dict)
    return loaded


# --- delivered path -------------------------------------------------------------------------


def test_delivered_capture_is_redacted_uses_host_token_and_leaves_no_spool(tmp_path: Path, server: _Server) -> None:
    result = _run_hook(_hook_env(tmp_path, server.base_url), json.dumps(_claude_payload(SECRET_PROMPT)))
    output = _stdout_json(result)

    assert output["suppressOutput"] is True
    assert "systemMessage" not in output
    assert "abc123secret" not in result.stdout and "abc123secret" not in result.stderr
    assert HOST_TOKEN not in result.stdout

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["path"] == "/v1/inputs"
    assert request["headers"]["authorization"] == f"Bearer {HOST_TOKEN}"
    assert "x-tce-role" not in request["headers"]
    assert request["headers"]["x-tce-consumer"] == "host-capture-claude"
    assert request["headers"]["x-tce-user"] == "human-1"
    assert request["headers"]["x-tce-behavior-subject"] == "human-1"
    assert request["headers"]["x-tce-workspace"] == "personal"

    body = request["body"]
    assert body["session_id"] == CLAUDE_SESSION
    assert len(body["delivery_key"]) == 64 and set(body["delivery_key"]) <= set("0123456789abcdef")
    assert body["content_sha256"] == hashlib.sha256(SECRET_PROMPT.encode("utf-8")).hexdigest()
    assert "<REDACTED:API_KEY>" in body["content"]
    assert "abc123secret" not in json.dumps(body)
    assert body["redaction_applied"] == ["api_key"]
    assert body["origin_kind"] == "human_input"
    assert body["host_client"] == "claude"
    assert body["hook_event_name"] == "UserPromptSubmit"
    assert body["original_char_count"] == len(SECRET_PROMPT)
    assert body["content_truncated"] is False
    assert body["project_hint"]["project_root"] == str(ROOT)
    assert body["spool_depth"] == 0
    assert body["schema_version"] == "v1"

    assert _spool_entries(tmp_path) == []
    state = _state(tmp_path)
    assert state["capture_delivery_state"] == "delivered"
    assert state["pending_count"] == 0
    assert state["last_error"] is None


def test_prompt_is_capped_and_marked_truncated(tmp_path: Path, server: _Server) -> None:
    prompt = "x" * 5000
    env = _hook_env(tmp_path, server.base_url, TCE_CAPTURE_MAX_CHARS="100")
    _stdout_json(_run_hook(env, json.dumps(_claude_payload(prompt))))
    body = server.requests[0]["body"]
    assert len(body["content"]) == 100
    assert body["content_truncated"] is True
    assert body["original_char_count"] == 5000
    assert body["content_sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# --- dead endpoint: spool path ------------------------------------------------------------------


def test_dead_endpoint_spools_durably_and_warns_without_leaking_prompt(tmp_path: Path) -> None:
    env = _hook_env(tmp_path, _dead_base_url())
    started = time.monotonic()
    result = _run_hook(env, json.dumps(_claude_payload(SECRET_PROMPT)))
    elapsed = time.monotonic() - started
    output = _stdout_json(result)

    assert elapsed < 2.5
    assert output["suppressOutput"] is True
    assert output["systemMessage"].startswith("[TCE-CAPTURE] human-input capture spooled: 1 pending")
    assert "abc123secret" not in result.stdout
    assert "deploy" not in result.stdout
    assert HOST_TOKEN not in result.stdout

    entries = _spool_entries(tmp_path)
    assert len(entries) == 1
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "spool").stat().st_mode) == 0o700
    spooled = json.loads(entries[0].read_text(encoding="utf-8"))
    assert spooled["delivery_key"] == entries[0].name[: -len(".json")]
    assert spooled["state"] == "spooled"
    assert spooled["attempts"] == 1
    assert "abc123secret" not in json.dumps(spooled)  # spool holds the redacted body only

    state = _state(tmp_path)
    assert state["capture_delivery_state"] == "spooled"
    assert state["pending_count"] == 1
    assert state["failures_since_delivery"] == 1
    assert state["last_error"]


def test_replay_delivers_spooled_entry_once_server_is_back(tmp_path: Path, server: _Server) -> None:
    dead_env = _hook_env(tmp_path, _dead_base_url())
    _stdout_json(_run_hook(dead_env, json.dumps(_claude_payload("first prompt while offline"))))
    spooled_key = _spool_entries(tmp_path)[0].name[: -len(".json")]

    live_env = _hook_env(tmp_path, server.base_url)
    output = _stdout_json(_run_hook(live_env, json.dumps(_claude_payload("second prompt while online"))))
    assert "systemMessage" not in output

    delivered_keys = [request["body"]["delivery_key"] for request in server.requests]
    assert len(delivered_keys) == 2
    assert spooled_key in delivered_keys
    replayed = next(request["body"] for request in server.requests if request["body"]["delivery_key"] == spooled_key)
    assert replayed["content"] == "first prompt while offline"
    live = next(request["body"] for request in server.requests if request["body"]["delivery_key"] != spooled_key)
    assert live["spool_depth"] == 1  # the backlog depth is reported to the server

    assert _spool_entries(tmp_path) == []
    state = _state(tmp_path)
    assert state["capture_delivery_state"] == "delivered"
    assert state["pending_count"] == 0
    assert state["failures_since_delivery"] == 0


def test_duplicate_409_counts_as_delivered(tmp_path: Path) -> None:
    dup = _Server(status=409)
    try:
        output = _stdout_json(_run_hook(_hook_env(tmp_path, dup.base_url), json.dumps(_claude_payload("hello"))))
    finally:
        dup.close()
    assert "systemMessage" not in output
    assert _spool_entries(tmp_path) == []
    assert _state(tmp_path)["capture_delivery_state"] == "delivered"


def test_forbidden_marks_failed_and_keeps_entry(tmp_path: Path) -> None:
    forbidden = _Server(status=403)
    try:
        output = _stdout_json(_run_hook(_hook_env(tmp_path, forbidden.base_url), json.dumps(_claude_payload("hello"))))
    finally:
        forbidden.close()
    assert output["systemMessage"].startswith("[TCE-CAPTURE] human-input capture failed: 1 pending")
    assert "http 403" in output["systemMessage"]
    entries = _spool_entries(tmp_path)
    assert len(entries) == 1
    assert json.loads(entries[0].read_text(encoding="utf-8"))["state"] == "failed"
    assert _state(tmp_path)["capture_delivery_state"] == "failed"


def test_repeated_failures_throttle_the_warning(tmp_path: Path) -> None:
    env = _hook_env(tmp_path, _dead_base_url())
    first = _stdout_json(_run_hook(env, json.dumps(_claude_payload("one"))))
    second = _stdout_json(_run_hook(env, json.dumps(_claude_payload("two"))))
    assert "systemMessage" in first
    assert "systemMessage" not in second  # same state within the warn interval
    assert _state(tmp_path)["pending_count"] == 2


def test_spool_cap_evicts_oldest_records_gap_and_reports_dropped_full(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    keys: list[str] = []
    for index in range(500):
        key = hashlib.sha256(f"old-{index}".encode()).hexdigest()
        keys.append(key)
        entry = {
            "schema_version": "v1",
            "delivery_key": key,
            "body": {"delivery_key": key, "content_sha256": key, "observed_at": f"2026-01-01T00:00:{index % 60:02d}+00:00", "content": "old"},
            "created_at": 1000.0 + index,
            "attempts": 1,
            "last_error": "boom",
            "state": "spooled",
        }
        (spool / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")

    output = _stdout_json(_run_hook(_hook_env(tmp_path, _dead_base_url()), json.dumps(_claude_payload("newest"))))

    entries = _spool_entries(tmp_path)
    assert len(entries) == 500
    names = {path.name[: -len(".json")] for path in entries}
    assert keys[0] not in names  # oldest evicted
    assert keys[1] in names
    gap_lines = [json.loads(line) for line in (spool / "gaps.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(line["delivery_key"] == keys[0] and line["reason"] == "dropped_full" for line in gap_lines)
    state = _state(tmp_path)
    assert state["capture_delivery_state"] == "dropped_full"
    assert state["gap_since"] is not None
    assert output["systemMessage"].startswith("[TCE-CAPTURE] human-input capture dropped_full: 500 pending")


def test_slow_server_never_blocks_the_prompt(tmp_path: Path) -> None:
    slow = _Server(status=201, delay=5.0)
    try:
        started = time.monotonic()
        result = _run_hook(_hook_env(tmp_path, slow.base_url), json.dumps(_claude_payload("slow")))
        elapsed = time.monotonic() - started
    finally:
        slow.close()
    output = _stdout_json(result)
    assert elapsed < 2.5
    assert output["systemMessage"].startswith("[TCE-CAPTURE] human-input capture spooled")
    assert len(_spool_entries(tmp_path)) == 1
    assert _state(tmp_path)["capture_delivery_state"] == "spooled"


# --- other hosts, configuration, degenerate input ---------------------------------------------------


def test_codex_payload_uses_turn_id_and_codex_consumer(tmp_path: Path, server: _Server) -> None:
    payload = {
        "session_id": "codex-session-1",
        "turn_id": "turn-42",
        "transcript_path": None,
        "cwd": str(ROOT),
        "hook_event_name": "UserPromptSubmit",
        "permission_mode": "default",
        "prompt": "confirm",
    }
    _stdout_json(_run_hook(_hook_env(tmp_path, server.base_url), json.dumps(payload), "--client", "codex"))
    request = server.requests[0]
    assert request["headers"]["x-tce-consumer"] == "host-capture-codex"
    assert request["body"]["host_client"] == "codex"
    assert request["body"]["prompt_id"] == "turn-42"
    assert request["body"]["session_id"] == "codex-session-1"
    assert request["body"]["content"] == "confirm"


def test_delivery_key_is_stable_for_the_same_prompt_ref(tmp_path: Path, server: _Server) -> None:
    payload = _claude_payload("confirm")
    payload["prompt_id"] = "p1"
    _stdout_json(_run_hook(_hook_env(tmp_path, server.base_url), json.dumps(payload)))
    _stdout_json(_run_hook(_hook_env(tmp_path, server.base_url), json.dumps(payload)))
    keys = {request["body"]["delivery_key"] for request in server.requests}
    assert len(keys) == 1  # same session + prompt_id + content hash => idempotent delivery key


def test_missing_token_marks_disabled_and_never_calls_the_api(tmp_path: Path, server: _Server) -> None:
    env = _hook_env(tmp_path, server.base_url, TCE_HOST_CAPTURE_TOKEN=None)
    output = _stdout_json(_run_hook(env, json.dumps(_claude_payload("hello"))))
    assert output["systemMessage"].startswith("[TCE-CAPTURE] human-input capture disabled: host capture token missing")
    assert server.requests == []
    assert _spool_entries(tmp_path) == []
    assert _state(tmp_path)["capture_delivery_state"] == "disabled"


def test_token_and_scope_come_from_config_files_not_repo_env(tmp_path: Path, server: _Server) -> None:
    env = _hook_env(tmp_path, "http://127.0.0.1:1", TCE_HOST_CAPTURE_TOKEN=None, TCE_CAPTURE_WORKSPACE=None, TCE_CAPTURE_USER=None, TCE_API_BASE_URL=None)
    config_dir = Path(env["HOME"]) / ".config" / "open-timeline-engine"
    config_dir.mkdir(parents=True)
    (config_dir / "host_capture.token").write_text("file-token\n", encoding="utf-8")
    (config_dir / "host_capture.env").write_text(f"TCE_CAPTURE_WORKSPACE=team-a\nTCE_CAPTURE_USER=alice\nTCE_API_BASE_URL={server.base_url}\n", encoding="utf-8")
    _stdout_json(_run_hook(env, json.dumps(_claude_payload("hello"))))
    request = server.requests[0]
    assert request["headers"]["authorization"] == "Bearer file-token"
    assert request["headers"]["x-tce-workspace"] == "team-a"
    assert request["headers"]["x-tce-user"] == "alice"
    assert request["headers"]["x-tce-behavior-subject"] == "alice"


def test_disabled_flag_writes_state_and_skips_network(tmp_path: Path, server: _Server) -> None:
    env = _hook_env(tmp_path, server.base_url, TCE_CAPTURE_DISABLED="1")
    _stdout_json(_run_hook(env, json.dumps(_claude_payload("hello"))))
    assert server.requests == []
    assert _state(tmp_path)["capture_delivery_state"] == "disabled"


def test_dry_run_prints_body_without_spool_or_network(tmp_path: Path, server: _Server) -> None:
    output = _stdout_json(_run_hook(_hook_env(tmp_path, server.base_url), json.dumps(_claude_payload(SECRET_PROMPT)), "--client", "claude", "--dry-run"))
    assert output["dry_run"] is True
    assert output["headers"]["Authorization"] == "Bearer <redacted>"
    assert "<REDACTED:API_KEY>" in output["body"]["content"]
    assert "abc123secret" not in json.dumps(output)
    assert server.requests == []
    assert not (tmp_path / "spool").exists()


@pytest.mark.parametrize("stdin_text", ["", "not json", "[]", json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "   "})])
def test_degenerate_stdin_exits_quietly(tmp_path: Path, server: _Server, stdin_text: str) -> None:
    output = _stdout_json(_run_hook(_hook_env(tmp_path, server.base_url), stdin_text))
    assert output == {"suppressOutput": True}
    assert server.requests == []


def test_bad_arguments_still_exit_zero(tmp_path: Path, server: _Server) -> None:
    result = _run_hook(_hook_env(tmp_path, server.base_url), json.dumps(_claude_payload("hello")), "--client", "not-a-client")
    output = _stdout_json(result)
    assert "misconfigured" in output["systemMessage"]


def test_hook_script_is_stdlib_only_and_py39_syntax() -> None:
    source = HOOK.read_text(encoding="utf-8")
    assert "import pydantic" not in source and "from tce_shared" not in source and "import requests" not in source
    assert "match " not in source.replace("matcher", "")
    assert "StrEnum" not in source and "datetime.UTC" not in source and "from datetime import UTC" not in source
    assert "TCE_API_TOKEN" not in source  # the executor credential is never consulted
    assert '".env"' not in source and "/.env" not in source  # never reads the repo .env (that is where the executor token lives)


# --- hook generator ---------------------------------------------------------------------------------


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("generate_client_hooks", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_orders_capture_before_policy_echo_and_quotes_paths() -> None:
    generator = _load_generator()
    capture_cmd = generator.capture_command("/opt/My Repo/scripts/tce_capture_input.py", "claude")
    document = generator.build_hooks(self_heal="/opt/My Repo/scripts/self-heal.sh", capture_cmd=capture_cmd, client="claude")
    prompt_hooks = document["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert [hook["timeout"] for hook in prompt_hooks] == [10, 10]
    assert prompt_hooks[0]["command"] == "python3 '/opt/My Repo/scripts/tce_capture_input.py' --client claude"
    assert prompt_hooks[1]["command"].startswith('echo \'{"systemMessage": "[TCE-HOOK]')
    assert document["hooks"]["PostToolUse"][0]["matcher"] == "Edit|Write"
    assert document["hooks"]["Stop"][0]["hooks"][0]["command"].startswith('echo \'{"systemMessage": "[TCE-COMPLETION]')
    assert document["permissions"]["deny"] == ["Read(~/.config/open-timeline-engine/**)", "Read(~/.cache/open-timeline-engine/**)"]
    rendered = json.dumps(document)
    assert "HOST_CAPTURE" not in rendered and "{{" not in rendered
    for event in document["hooks"].values():
        for group in event:
            for hook in group["hooks"]:
                assert json.loads(hook["command"].split("echo ", 1)[1].split("'")[1]) if hook["command"].startswith("echo '") else True

    codex = generator.build_hooks(self_heal="/x/self-heal.sh", capture_cmd=generator.capture_command("/x/tce_capture_input.py", "codex"), client="codex")
    assert codex["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("--client codex")
    assert "permissions" not in codex


def test_generator_merge_replaces_event_lists_and_unions_deny(tmp_path: Path) -> None:
    generator = _load_generator()
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps(
            {
                "model": "keep-me",
                "hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo old", "timeout": 3}]}], "Custom": [{"matcher": "*", "hooks": []}]},
                "permissions": {"allow": ["Bash(ls)"], "deny": ["Read(~/.config/open-timeline-engine/**)", "Read(/etc/**)"]},
            }
        ),
        encoding="utf-8",
    )
    document = generator.build_hooks(self_heal="/x/self-heal.sh", capture_cmd="python3 /x/tce_capture_input.py --client claude", client="claude")
    merged = generator.merge_into_file(target, document)
    assert merged["model"] == "keep-me"
    assert "Custom" in merged["hooks"]
    assert [hook["command"] for hook in merged["hooks"]["UserPromptSubmit"][0]["hooks"]][0] == "python3 /x/tce_capture_input.py --client claude"
    assert merged["permissions"]["allow"] == ["Bash(ls)"]
    assert merged["permissions"]["deny"] == ["Read(~/.config/open-timeline-engine/**)", "Read(/etc/**)", "Read(~/.cache/open-timeline-engine/**)"]
    assert json.loads(target.read_text(encoding="utf-8")) == merged


def test_generator_ensures_codex_hooks_feature(tmp_path: Path) -> None:
    generator = _load_generator()
    config = tmp_path / "config.toml"
    assert generator.ensure_codex_features(config) is True
    assert config.read_text(encoding="utf-8") == "[features]\nhooks = true\n"
    assert generator.ensure_codex_features(config) is False

    config.write_text('model = "gpt"\n\n[features]\nother = 1\n\n[mcp_servers.x]\ncommand = "y"\n', encoding="utf-8")
    assert generator.ensure_codex_features(config) is True
    text = config.read_text(encoding="utf-8")
    assert "[features]\nhooks = true\nother = 1\n" in text
    assert '[mcp_servers.x]\ncommand = "y"' in text
    assert generator.ensure_codex_features(config) is False


def test_generator_cli_outputs_json(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--self-heal", "/x/self-heal.sh", "--capture-script", "/x/tce_capture_input.py", "--client", "claude"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    document = json.loads(result.stdout)
    assert document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "python3 /x/tce_capture_input.py --client claude"


# --- MCP executor client must never hold or use the host credential ------------------------------------


def test_mcp_client_refuses_to_run_with_the_host_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from tce_mcp import client as mcp_client
    from tce_mcp.config import Settings

    monkeypatch.setenv("TCE_HOST_CAPTURE_TOKENS", "other,shared-secret")
    monkeypatch.setattr(mcp_client, "get_settings", lambda: Settings(api_token="shared-secret", api_base_url="http://127.0.0.1:1"))
    with pytest.raises(mcp_client.HostCredentialLeakError):
        mcp_client.TCEApiClient()
    assert "TCE_HOST_CAPTURE_TOKENS" not in os.environ  # scrubbed from the executor process either way


def test_mcp_client_scrubs_host_env_and_refuses_host_only_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    from tce_mcp import client as mcp_client
    from tce_mcp.config import Settings

    monkeypatch.setenv("TCE_HOST_CAPTURE_TOKEN", "host-only")
    monkeypatch.setattr(mcp_client, "get_settings", lambda: Settings(api_token="executor-token", api_base_url="http://127.0.0.1:1"))
    client = mcp_client.TCEApiClient()
    assert "TCE_HOST_CAPTURE_TOKEN" not in os.environ
    assert client.headers["Authorization"] == "Bearer executor-token"
    with pytest.raises(PermissionError):
        client._post("/v1/inputs", {"content": "forged"})
    with pytest.raises(PermissionError):
        client._get("/v1/inputs/abc")
