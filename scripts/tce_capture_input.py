#!/usr/bin/env python3
"""Trusted human-input capture hook for Claude Code / Codex ``UserPromptSubmit``.

Reads the hook JSON from stdin, redacts and caps the prompt, durably spools a
``TrustedInputCapture`` body under ``~/.cache/open-timeline-engine/capture-spool``
and then POSTs it to ``/v1/inputs`` with the *host capture* credential (never the
executor API token). It always exits 0, never prints the prompt or the token, and
never runs longer than ``WALL_BUDGET_SECONDS`` so it cannot block the prompt.

Failure is visible, not silent: whenever the spool state is anything other than
``delivered`` the hook emits a ``systemMessage`` (shown to the user, not the model).

Stdlib only and syntax-compatible with Python 3.9 so that an old interpreter still
exits cleanly with a "disabled" marker (the shared spool module itself needs 3.11+).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = ROOT / "shared" / "tce_shared"

# Vendored from shared/tce_shared/decision_capture.py (the hook must not import the package).
CAPTURE_SCHEMA_VERSION = "v1"
DEFAULT_CAPTURE_MAX_CHARS = 2000
ORIGIN_HUMAN_INPUT = "human_input"
SERVER_CONTENT_MAX_CHARS = 8000  # TrustedInputCapture.content max_length

DEFAULT_API_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_HOOK_EVENT = "UserPromptSubmit"
WALL_BUDGET_SECONDS = 2.0
PRIMARY_TIMEOUT_SECONDS = 1.5
DRAIN_TIMEOUT_SECONDS = 0.8
DRAIN_BUDGET_SECONDS = 1.8
DRAIN_MAX_ENTRIES = 5
WARN_INTERVAL_SECONDS = 600.0
DISABLED_WARN_INTERVAL_SECONDS = 3600.0
NOISY_STATES = ("spooled", "failed", "dropped_full", "disabled")
MESSAGE_PREFIX = "[TCE-CAPTURE]"

_START = time.monotonic()
_DEADLINE = _START + WALL_BUDGET_SECONDS


# --- small helpers ----------------------------------------------------------------------


def _remaining() -> float:
    return _DEADLINE - time.monotonic()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 - keep 3.9-compatible syntax


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _epoch_to_iso(value: Any) -> str | None:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    return _iso(datetime.fromtimestamp(stamp, tz=timezone.utc))  # noqa: UP017


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "open-timeline-engine"


def _default_spool_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(_home() / ".cache")
    return Path(base) / "open-timeline-engine" / "capture-spool"


def _read_kv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _strip_control(text: str) -> str:
    return "".join(ch for ch in text if ch in ("\n", "\r", "\t") or ord(ch) >= 0x20)


# --- shared module loading (bypasses tce_shared/__init__, which imports pydantic) ----------


def _load_shared(name: str) -> Any:
    path = SHARED_DIR / f"{name}.py"
    module_name = f"_tce_hook_{name}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot locate {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --- configuration --------------------------------------------------------------------------


class Config:
    def __init__(self, client: str) -> None:
        env = os.environ
        file_values = _read_kv_file(Path(env.get("TCE_HOST_CAPTURE_ENV_FILE") or _config_dir() / "host_capture.env"))

        def pick(key: str, default: str) -> str:
            value = env.get(key)
            if value is None or not value.strip():
                value = file_values.get(key, "")
            return value.strip() or default

        self.client = client
        self.disabled = env.get("TCE_CAPTURE_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")
        self.api_base_url = pick("TCE_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
        self.workspace_id = pick("TCE_CAPTURE_WORKSPACE", "personal")
        self.user_id = pick("TCE_CAPTURE_USER", env.get("USER", "").strip() or "local-user")
        self.spool_dir = Path(env.get("TCE_CAPTURE_SPOOL_DIR") or _default_spool_dir())
        try:
            max_chars = int(pick("TCE_CAPTURE_MAX_CHARS", str(DEFAULT_CAPTURE_MAX_CHARS)))
        except ValueError:
            max_chars = DEFAULT_CAPTURE_MAX_CHARS
        self.max_chars = max(1, min(SERVER_CONTENT_MAX_CHARS, max_chars))
        self.token = self._resolve_token(env, file_values)

    @staticmethod
    def _resolve_token(env: Mapping[str, str], file_values: dict[str, str]) -> str:
        direct = (env.get("TCE_HOST_CAPTURE_TOKEN") or "").strip()
        if direct:
            return direct
        token_file = Path(env.get("TCE_HOST_CAPTURE_TOKEN_FILE") or file_values.get("TCE_HOST_CAPTURE_TOKEN_FILE") or _config_dir() / "host_capture.token")
        try:
            return token_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            return ""

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-TCE-Consumer": f"host-capture-{self.client}",
            "X-TCE-Workspace": self.workspace_id,
            "X-TCE-User": self.user_id,
            "X-TCE-Behavior-Subject": self.user_id,
        }


# --- hook payload -> capture body ------------------------------------------------------------


def _safe_git_remote(url: str) -> str:
    """Return the remote URL with any credential userinfo removed.

    Remote URLs routinely embed secrets (``https://user:ghp_xxx@github.com/...``,
    ``https://oauth2:<token>@gitlab/...``). The hint is spooled to disk and stored
    verbatim in the (unencrypted) event context, so the credential must never leave
    ``.git/config``. Anything we cannot confidently rewrite is dropped entirely.
    """
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        # scp-style (git@host:path) or a local path: the bare ``user@`` form is
        # credential-free, a ``user:secret@`` form is dropped whole.
        if "@" in url and ":" in url.split("@", 1)[0]:
            return ""
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if "@" not in parsed.netloc:
        return url
    userinfo, _, host = parsed.netloc.rpartition("@")
    if not host:
        return ""
    if ":" not in userinfo and parsed.scheme in ("ssh", "git"):
        # A bare ssh login name (ssh://git@host/...) is not a credential; an http(s)
        # bare userinfo is (GitHub accepts https://<token>@github.com/...).
        return url
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _git_hint(cwd: str) -> dict[str, str]:
    hint: dict[str, str] = {}
    if not cwd:
        return hint
    git_dir = Path(cwd) / ".git"
    try:
        if not git_dir.is_dir():
            return hint
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: refs/heads/"):
            hint["branch"] = head[len("ref: refs/heads/") :][:200]
        in_origin = False
        for raw in (git_dir / "config").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_origin = line.replace(" ", "") in ('[remote"origin"]',)
                continue
            if in_origin and line.startswith("url") and "=" in line:
                remote = _safe_git_remote(line.split("=", 1)[1].strip())
                if remote:
                    hint["git_remote"] = remote[:500]
                break
    except OSError:
        return hint
    return hint


def build_body(hook: dict[str, Any], config: Config, redact_text: Any, idempotent_key: Any, state: dict[str, Any], pending_before: int) -> dict[str, Any]:
    prompt = str(hook.get("prompt") or "")
    session_id = str(hook.get("session_id") or "unknown-session")[:200]
    prompt_id = hook.get("prompt_id") or hook.get("turn_id")
    prompt_ref = str(prompt_id) if prompt_id else str(int(time.time() * 1000))
    content_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    redacted, applied = redact_text(prompt)
    redacted = _strip_control(str(redacted))
    content = redacted[: config.max_chars]
    cwd = str(hook.get("cwd") or "")
    project_hint: dict[str, Any] = {}
    if cwd:
        project_hint = {"project": os.path.basename(cwd.rstrip("/")) or cwd, "project_root": cwd}
        project_hint.update(_git_hint(cwd))
    return {
        "session_id": session_id,
        "delivery_key": str(idempotent_key(session_id, prompt_ref, content_sha256)),
        "content_sha256": content_sha256,
        "content": content,
        "origin_kind": ORIGIN_HUMAN_INPUT,
        "observed_at": _iso(_utc_now()),
        "original_char_count": len(prompt),
        "content_truncated": len(redacted) > config.max_chars,
        "redaction_applied": sorted({str(item) for item in applied}),
        "sequence": None,
        "prompt_id": str(prompt_id)[:200] if prompt_id else None,
        "hook_event_name": str(hook.get("hook_event_name") or DEFAULT_HOOK_EVENT)[:64],
        "host_client": config.client,
        "cwd": cwd[:1024] or None,
        "project_hint": project_hint,
        "spool_depth": max(0, int(pending_before)),
        "spool_failures": max(0, int(state.get("failures_since_delivery") or 0)),
        "gap_since": _epoch_to_iso(state.get("gap_since")),
        "schema_version": CAPTURE_SCHEMA_VERSION,
    }


# --- delivery -----------------------------------------------------------------------------------


def post_capture(config: Config, body: dict[str, Any], timeout: float) -> tuple[int | None, str | None]:
    """Returns (http_status, error_text). Never raises. Never includes the token or the prompt in error_text."""
    if timeout <= 0.05:
        return None, "deadline exceeded before request"
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(f"{config.api_base_url}/v1/inputs", data=data, method="POST", headers=config.headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
            return int(response.status), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return int(exc.code), f"http {exc.code}: {detail}"[:300]
    except Exception as exc:  # URLError, socket.timeout, ConnectionRefusedError, ...
        reason = getattr(exc, "reason", None)
        text = str(reason) if reason is not None else str(exc)
        return None, f"{type(exc).__name__}: {text}"[:300]


def _deliver(spool: Any, config: Config, entry: Any, timeout: float) -> tuple[Any, str | None]:
    status, error = post_capture(config, entry.body, timeout)
    outcome = spool.classify_http(status, error)
    updated = spool.record_attempt(config.spool_dir, entry, outcome, error=error)
    return updated, error


def _drain_backlog(spool: Any, config: Config, skip_key: str) -> tuple[str | None, str | None]:
    """Redeliver up to DRAIN_MAX_ENTRIES older spooled entries; stop on the first non-delivered one."""
    last_state: str | None = None
    last_error: str | None = None
    try:
        entries = spool.read_entries(config.spool_dir, limit=DRAIN_MAX_ENTRIES + 1)
    except Exception as exc:
        return "failed", f"spool read: {type(exc).__name__}"
    drained = 0
    for entry in entries:
        if entry.delivery_key == skip_key or not spool.retryable(entry):
            continue
        if drained >= DRAIN_MAX_ENTRIES or (time.monotonic() - _START) >= DRAIN_BUDGET_SECONDS:
            break
        timeout = min(DRAIN_TIMEOUT_SECONDS, _remaining() - 0.1)
        if timeout <= 0.05:
            break
        updated, error = _deliver(spool, config, entry, timeout)
        drained += 1
        last_state = str(updated.state)
        last_error = error
        if last_state != str(spool.SpoolEntryState.DELIVERED):
            break
    return last_state, last_error


# --- user-visible state ------------------------------------------------------------------------


def _format_message(state: dict[str, Any]) -> str:
    delivery_state = str(state.get("capture_delivery_state") or "unknown")
    pending = int(state.get("pending_count") or 0)
    error = state.get("last_error") or "none"
    return f"{MESSAGE_PREFIX} human-input capture {delivery_state}: {pending} pending, last error: {error}"


def _should_warn(state: dict[str, Any], now: float) -> bool:
    delivery_state = str(state.get("capture_delivery_state") or "unknown")
    if delivery_state not in NOISY_STATES:
        return False
    if state.get("last_warned_state") != delivery_state:
        return True
    last = state.get("last_warned_at")
    try:
        last_stamp = float(last) if last is not None else 0.0
    except (TypeError, ValueError):
        last_stamp = 0.0
    interval = DISABLED_WARN_INTERVAL_SECONDS if delivery_state == "disabled" else WARN_INTERVAL_SECONDS
    return (now - last_stamp) >= interval


def _finish(spool: Any, config: Config, state: dict[str, Any], *, message: str | None = None) -> None:
    now = time.time()
    output: dict[str, Any] = {"suppressOutput": True}
    warn = _should_warn(state, now)
    if warn:
        state["last_warned_at"] = now
        state["last_warned_state"] = state.get("capture_delivery_state")
        output["systemMessage"] = message or _format_message(state)
    try:
        spool.save_state(config.spool_dir, state)
    except Exception as exc:
        output["systemMessage"] = f"{MESSAGE_PREFIX} capture spool unwritable ({type(exc).__name__}); human-input capture is NOT being recorded"
    _emit(output)


def _mark_disabled(spool: Any, config: Config, reason: str) -> None:
    try:
        state = spool.load_state(config.spool_dir)
    except Exception:
        state = {}
    state["capture_delivery_state"] = "disabled"
    state["last_error"] = reason
    state["last_attempt_at"] = time.time()
    try:
        state["pending_count"] = int(spool.pending_count(config.spool_dir))
    except Exception:
        state["pending_count"] = int(state.get("pending_count") or 0)
    _finish(spool, config, state, message=f"{MESSAGE_PREFIX} human-input capture disabled: {reason}")


# --- entry point -------------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TCE trusted human-input capture hook")
    parser.add_argument("--client", choices=("claude", "codex", "cursor"), default="claude")
    parser.add_argument("--dry-run", action="store_true", help="build the capture body and print it; no spool, no network")
    return parser.parse_args(argv)


def _read_hook_stdin() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw.strip():
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def run(argv: list[str]) -> int:
    args = _parse_args(argv)
    config = Config(str(args.client))

    hook = _read_hook_stdin()
    if hook is None or not str(hook.get("prompt") or "").strip():
        _emit({"suppressOutput": True})
        return 0

    try:
        spool = _load_shared("capture_spool")
        redaction = _load_shared("redaction")
    except Exception as exc:
        # Old interpreter or a moved checkout: stay quiet for the model, loud for the user.
        try:
            spool_state_dir = config.spool_dir
            spool_state_dir.mkdir(parents=True, exist_ok=True)
            (spool_state_dir / "state.json").write_text(
                json.dumps({"capture_delivery_state": "disabled", "last_error": f"shared modules unavailable: {type(exc).__name__}", "pending_count": 0, "schema_version": CAPTURE_SCHEMA_VERSION}),
                encoding="utf-8",
            )
        except Exception:
            pass
        _emit({"suppressOutput": True, "systemMessage": f"{MESSAGE_PREFIX} capture hook disabled: shared modules unavailable"})
        return 0

    if config.disabled:
        _mark_disabled(spool, config, "TCE_CAPTURE_DISABLED is set")
        return 0
    if not config.token:
        _mark_disabled(spool, config, "host capture token missing (expected ~/.config/open-timeline-engine/host_capture.token); re-run scripts/install.sh")
        return 0

    state = spool.load_state(config.spool_dir)
    pending_before = int(spool.pending_count(config.spool_dir))
    body = build_body(hook, config, redaction.redact_text, spool.idempotent_key, state, pending_before)

    if args.dry_run:
        headers = {key: ("Bearer <redacted>" if key == "Authorization" else value) for key, value in config.headers().items()}
        _emit({"suppressOutput": True, "dry_run": True, "url": f"{config.api_base_url}/v1/inputs", "headers": headers, "body": body})
        return 0

    # 1. durable spool entry first (fsync) - the receipt exists locally before any network I/O
    entry = spool.SpoolEntry(delivery_key=body["delivery_key"], body=body, created_at=time.time())
    try:
        spool.write_entry(config.spool_dir, entry)
    except Exception as exc:
        state["capture_delivery_state"] = "failed"
        state["last_error"] = f"spool write: {type(exc).__name__}"
        state["last_attempt_at"] = time.time()
        _finish(spool, config, state)
        return 0

    # 2. bounded spool
    evicted: list[str] = []
    try:
        evicted = list(spool.enforce_cap(config.spool_dir))
    except Exception:
        evicted = []

    # 3./4. deliver this capture with the primary timeout
    timeout = min(PRIMARY_TIMEOUT_SECONDS, _remaining() - 0.2)
    updated, error = _deliver(spool, config, entry, timeout)
    outcome = spool.SpoolEntryState(str(updated.state))

    # 5. opportunistically drain older entries
    drain_state: str | None = None
    drain_error: str | None = None
    if outcome is spool.SpoolEntryState.DELIVERED and _remaining() > 0.3:
        drain_state, drain_error = _drain_backlog(spool, config, entry.delivery_key)

    # 6. fold outcomes into state
    pending_after = int(spool.pending_count(config.spool_dir))
    if evicted:
        spool.apply_delivery_outcome(state, spool.SpoolEntryState.DROPPED_FULL, pending_count=pending_after, error=f"spool full: dropped {len(evicted)} capture(s)")
    spool.apply_delivery_outcome(state, outcome, pending_count=pending_after, error=error)
    if evicted and outcome is not spool.SpoolEntryState.DELIVERED:
        state["capture_delivery_state"] = "dropped_full"
    if drain_state is not None and drain_state != str(spool.SpoolEntryState.DELIVERED) and state.get("capture_delivery_state") == "delivered":
        state["last_error"] = drain_error
        state["capture_delivery_state"] = drain_state if drain_state in ("spooled", "failed") else "spooled"

    # 7./8. visible state, always exit 0
    _finish(spool, config, state)
    return 0


def main() -> None:
    try:
        code = run(sys.argv[1:])
    except SystemExit as exc:  # argparse errors must not become a blocking hook exit code
        if exc.code not in (None, 0):
            _emit({"suppressOutput": True, "systemMessage": f"{MESSAGE_PREFIX} capture hook misconfigured (bad arguments); human input is NOT being captured"})
        code = 0
    except BaseException as exc:  # a hook must never propagate
        _emit({"suppressOutput": True, "systemMessage": f"{MESSAGE_PREFIX} capture hook error ({type(exc).__name__}); human input may NOT have been captured"})
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
