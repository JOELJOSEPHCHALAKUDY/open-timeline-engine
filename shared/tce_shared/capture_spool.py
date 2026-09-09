"""Bounded, protected local spool for trusted human-input captures.

This module is loaded by the host capture hook (``scripts/tce_capture_input.py``) with
``importlib.util.spec_from_file_location`` so it bypasses ``tce_shared/__init__`` (which
imports pydantic). It must therefore stay stdlib-only, with no relative imports and no
package dependencies. ``tests/unit/test_capture_spool.py`` enforces that.

Layout of a spool directory (mode 0700, every file 0600)::

    <root>/<delivery_key>.json          one spooled TrustedInputCapture body per delivery key
    <root>/<delivery_key>.json.corrupt  quarantined unreadable entry (never retried, visible)
    <root>/state.json                   last known delivery state for the hook's systemMessage
    <root>/gaps.jsonl                   append-only, size-bounded record of captures that were lost

Idempotent redelivery: the same ``delivery_key`` always maps to the same file, and the
server deduplicates on the same key, so replaying a spooled entry is always safe.
Failure visibility: ``state.json["capture_delivery_state"] != "delivered"`` is the only
thing the hook needs to know in order to warn the user.
"""

# No `from __future__ import annotations`: dataclass(slots=True) resolves string annotations through
# sys.modules[cls.__module__], which does not exist when the hook loads this file via importlib.
import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

SPOOL_SCHEMA_VERSION = "v1"
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
STATE_FILE = "state.json"
GAP_FILE = "gaps.jsonl"
ENTRY_SUFFIX = ".json"
CORRUPT_SUFFIX = ".corrupt"
TMP_PREFIX = ".tmp-"

# Permanent (401/403/422) failures keep their file for visibility but are retried at most this many times.
MAX_FAILED_ATTEMPTS = 3

# gaps.jsonl is rotated down to the newest GAP_KEEP_LINES lines whenever it grows past GAP_MAX_BYTES.
GAP_MAX_BYTES = 1024 * 1024
GAP_KEEP_LINES = 2000

# Values allowed in state.json["capture_delivery_state"]; "unknown" is the never-attempted default.
DELIVERY_STATES = ("unknown", "delivered", "spooled", "failed", "dropped_full", "disabled")

_HEX_DIGITS = frozenset("0123456789abcdef")
_MIN_KEY_LEN = 16
_MAX_KEY_LEN = 128


class SpoolEntryState(StrEnum):
    SPOOLED = "spooled"
    DELIVERED = "delivered"
    FAILED = "failed"
    DROPPED_FULL = "dropped_full"


@dataclass(slots=True)
class SpoolEntry:
    delivery_key: str
    body: dict[str, Any]  # exact TrustedInputCapture JSON body (already redacted/capped)
    created_at: float  # epoch seconds
    attempts: int = 0
    last_error: str | None = None
    state: str = "spooled"


# --- keys and paths ---------------------------------------------------------------------


def idempotent_key(host_session_id: str, prompt_ref: str, content_sha256: str) -> str:
    """Identical algorithm to ``tce_shared.decision_capture.compute_delivery_key``."""
    return hashlib.sha256(f"{host_session_id}|{prompt_ref}|{content_sha256}".encode()).hexdigest()


def _validate_key(delivery_key: str) -> str:
    if not isinstance(delivery_key, str) or not (_MIN_KEY_LEN <= len(delivery_key) <= _MAX_KEY_LEN) or not set(delivery_key) <= _HEX_DIGITS:
        raise ValueError("delivery_key must be a lowercase hex digest")
    return delivery_key


def spool_path(root: Path, delivery_key: str) -> Path:
    return Path(root) / f"{_validate_key(delivery_key)}{ENTRY_SUFFIX}"


def _ensure_root(root: Path) -> Path:
    root = Path(root)
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)  # mkdir's mode is subject to umask
    return root


def _entry_files(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.iterdir():
        name = path.name
        if name == STATE_FILE or not name.endswith(ENTRY_SUFFIX) or name.startswith(TMP_PREFIX) or not path.is_file():
            continue
        files.append(path)
    return files


def pending_count(root: Path) -> int:
    return len(_entry_files(root))


# --- atomic file helpers ----------------------------------------------------------------


def _atomic_write_text(root: Path, target: Path, text: str) -> None:
    """Write ``text`` to ``target`` via a same-directory temp file, fsync and ``os.replace``.

    A failure anywhere leaves the previous ``target`` untouched and no temp file behind.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(root))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(root)


def _fsync_dir(root: Path) -> None:
    try:
        dir_fd = os.open(str(root), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- entries ------------------------------------------------------------------------------


def write_entry(root: Path, entry: SpoolEntry) -> Path:
    root = _ensure_root(root)
    target = spool_path(root, entry.delivery_key)
    payload = {"schema_version": SPOOL_SCHEMA_VERSION, **asdict(entry)}
    payload["state"] = str(entry.state)
    _atomic_write_text(root, target, json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return target


def _parse_entry(path: Path) -> SpoolEntry | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    key = loaded.get("delivery_key")
    body = loaded.get("body")
    created_at = loaded.get("created_at")
    if not isinstance(key, str) or key != path.name[: -len(ENTRY_SUFFIX)] or not isinstance(body, dict) or not isinstance(created_at, int | float):
        return None
    attempts = loaded.get("attempts", 0)
    last_error = loaded.get("last_error")
    state = loaded.get("state", SpoolEntryState.SPOOLED)
    return SpoolEntry(
        delivery_key=key,
        body=body,
        created_at=float(created_at),
        attempts=int(attempts) if isinstance(attempts, int | float) else 0,
        last_error=str(last_error) if last_error is not None else None,
        state=str(state) if isinstance(state, str) else SpoolEntryState.SPOOLED,
    )


def _quarantine(root: Path, path: Path) -> None:
    key = path.name[: -len(ENTRY_SUFFIX)]
    try:
        os.replace(path, path.with_name(path.name + CORRUPT_SUFFIX))
    except OSError:
        try:
            path.unlink()
        except OSError:
            return
    _append_gap(root, {"delivery_key": key, "content_sha256": None, "observed_at": None, "reason": "corrupt", "dropped_at": _utc_now_iso()})


def read_entries(root: Path, *, limit: int) -> list[SpoolEntry]:
    """Oldest first by ``created_at``. Unreadable entries are quarantined (``.corrupt``) and recorded as gaps."""
    root = Path(root)
    if limit <= 0:
        return []
    entries: list[SpoolEntry] = []
    for path in _entry_files(root):
        entry = _parse_entry(path)
        if entry is None:
            _quarantine(root, path)
            continue
        entries.append(entry)
    entries.sort(key=lambda e: (e.created_at, e.delivery_key))
    return entries[:limit]


def remove_entry(root: Path, delivery_key: str) -> None:
    try:
        spool_path(root, delivery_key).unlink()
    except FileNotFoundError:
        return


def retryable(entry: SpoolEntry) -> bool:
    """Transient entries retry forever (the spool cap bounds them); permanent failures stop after MAX_FAILED_ATTEMPTS."""
    if str(entry.state) == SpoolEntryState.FAILED:
        return entry.attempts < MAX_FAILED_ATTEMPTS
    return True


def record_attempt(root: Path, entry: SpoolEntry, outcome: SpoolEntryState, *, error: str | None = None) -> SpoolEntry:
    """Persist the result of one delivery attempt for ``entry`` and return the updated entry.

    DELIVERED removes the file; DROPPED_FULL removes it and records a gap; SPOOLED/FAILED keep it
    (same delivery key, same file) so the next hook run can redeliver idempotently.
    """
    root = Path(root)
    outcome = SpoolEntryState(outcome)
    updated = replace(entry, attempts=entry.attempts + 1, state=str(outcome), last_error=None if outcome is SpoolEntryState.DELIVERED else error)
    if outcome is SpoolEntryState.DELIVERED:
        remove_entry(root, entry.delivery_key)
    elif outcome is SpoolEntryState.DROPPED_FULL:
        remove_entry(root, entry.delivery_key)
        _append_gap(root, _gap_record(entry, "dropped_full"))
    else:
        write_entry(root, updated)
    return updated


# --- bounded size -------------------------------------------------------------------------


def _gap_record(entry: SpoolEntry, reason: str) -> dict[str, Any]:
    body = entry.body if isinstance(entry.body, dict) else {}
    return {
        "delivery_key": entry.delivery_key,
        "content_sha256": body.get("content_sha256"),
        "observed_at": body.get("observed_at"),
        "reason": reason,
        "dropped_at": _utc_now_iso(),
    }


def _append_gap(root: Path, record: dict[str, Any]) -> None:
    root = _ensure_root(root)
    gap_path = root / GAP_FILE
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    fd = os.open(str(gap_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(gap_path, 0o600)
        if gap_path.stat().st_size > GAP_MAX_BYTES:
            lines = gap_path.read_text(encoding="utf-8").splitlines()
            kept = [item for item in lines[-GAP_KEEP_LINES:] if item.strip()]
            _atomic_write_text(root, gap_path, "\n".join(kept) + ("\n" if kept else ""))
    except OSError:
        pass


def enforce_cap(root: Path, *, max_files: int = DEFAULT_MAX_FILES, max_bytes: int = DEFAULT_MAX_BYTES) -> list[str]:
    """Evict oldest entries until the spool is under both caps; every eviction is recorded in ``gaps.jsonl``."""
    root = Path(root)
    files = _entry_files(root)
    sized: list[tuple[Path, int]] = []
    for path in files:
        try:
            sized.append((path, path.stat().st_size))
        except OSError:
            continue
    total = sum(size for _, size in sized)
    if len(sized) <= max_files and total <= max_bytes:
        return []

    ordered: list[tuple[float, str, Path, int, SpoolEntry | None]] = []
    for path, size in sized:
        entry = _parse_entry(path)
        key = path.name[: -len(ENTRY_SUFFIX)]
        if entry is not None:
            ordered.append((entry.created_at, key, path, size, entry))
        else:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            ordered.append((mtime, key, path, size, None))
    ordered.sort(key=lambda item: (item[0], item[1]))

    evicted: list[str] = []
    count = len(ordered)
    for _created, key, path, size, entry in ordered:
        if count <= max_files and total <= max_bytes:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            count -= 1
            total -= size
            continue
        except OSError:
            continue
        count -= 1
        total -= size
        evicted.append(key)
        record = _gap_record(entry, "dropped_full") if entry is not None else {"delivery_key": key, "content_sha256": None, "observed_at": None, "reason": "dropped_full", "dropped_at": _utc_now_iso()}
        _append_gap(root, record)
    return evicted


# --- state ----------------------------------------------------------------------------------


def _default_state() -> dict[str, Any]:
    return {
        "capture_delivery_state": "unknown",
        "pending_count": 0,
        "last_delivered_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_warned_at": None,
        "gap_since": None,
        "failures_since_delivery": 0,
        "schema_version": SPOOL_SCHEMA_VERSION,
    }


def load_state(root: Path) -> dict[str, Any]:
    state = _default_state()
    path = Path(root) / STATE_FILE
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(loaded, dict):
        state.update(loaded)
    if state.get("capture_delivery_state") not in DELIVERY_STATES:
        state["capture_delivery_state"] = "unknown"
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    if state.get("capture_delivery_state") not in DELIVERY_STATES:
        raise ValueError(f"capture_delivery_state must be one of {DELIVERY_STATES}")
    root = _ensure_root(root)
    payload = {**_default_state(), **state}
    payload.setdefault("schema_version", SPOOL_SCHEMA_VERSION)
    _atomic_write_text(root, root / STATE_FILE, json.dumps(payload, separators=(",", ":"), sort_keys=True))


def apply_delivery_outcome(state: dict[str, Any], outcome: SpoolEntryState, *, pending_count: int, error: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Fold one delivery outcome into the state dict (mutated and returned)."""
    outcome = SpoolEntryState(outcome)
    stamp = time.time() if now is None else now
    state["pending_count"] = max(0, int(pending_count))
    state["last_attempt_at"] = stamp
    if outcome is SpoolEntryState.DELIVERED:
        state["capture_delivery_state"] = "delivered"
        state["last_delivered_at"] = stamp
        state["last_error"] = None
        state["failures_since_delivery"] = 0
        state["gap_since"] = None
        return state
    state["capture_delivery_state"] = str(outcome)
    state["last_error"] = error
    state["failures_since_delivery"] = int(state.get("failures_since_delivery") or 0) + 1
    if outcome is SpoolEntryState.DROPPED_FULL and state.get("gap_since") is None:
        state["gap_since"] = stamp
    return state


# --- HTTP classification -------------------------------------------------------------------


def classify_http(status: int | None, exc: str | None) -> SpoolEntryState:
    """2xx/409 delivered (409 = server-side duplicate); 401/403/422 permanent; anything else retries."""
    if status is not None:
        if 200 <= status < 300 or status == 409:
            return SpoolEntryState.DELIVERED
        if status in (401, 403, 422):
            return SpoolEntryState.FAILED
    return SpoolEntryState.SPOOLED
