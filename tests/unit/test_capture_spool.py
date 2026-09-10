from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tce_shared import capture_spool
from tce_shared.capture_spool import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    GAP_FILE,
    MAX_FAILED_ATTEMPTS,
    SPOOL_SCHEMA_VERSION,
    STATE_FILE,
    SpoolEntry,
    SpoolEntryState,
    apply_delivery_outcome,
    classify_http,
    enforce_cap,
    idempotent_key,
    load_state,
    pending_count,
    read_entries,
    record_attempt,
    remove_entry,
    retryable,
    save_state,
    spool_path,
    write_entry,
)

_SPOOL_MODULE = Path(__file__).resolve().parents[2] / "shared" / "tce_shared" / "capture_spool.py"
# The hook loads this file with importlib and must run without pydantic or any tce_shared package import.
_ALLOWED_IMPORTS = {"__future__", "json", "os", "hashlib", "time", "tempfile", "pathlib", "dataclasses", "datetime", "typing", "enum"}


def _key(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _entry(seed: str, created_at: float, **body_extra: Any) -> SpoolEntry:
    key = _key(seed)
    body = {
        "session_id": "sess-1",
        "delivery_key": key,
        "content_sha256": _key(f"content-{seed}"),
        "content": f"prompt {seed}",
        "observed_at": f"2026-09-09T10:00:{int(created_at) % 60:02d}+00:00",
        **body_extra,
    }
    return SpoolEntry(delivery_key=key, body=body, created_at=created_at)


def _read_gaps(root: Path) -> list[dict[str, Any]]:
    gap_file = root / GAP_FILE
    if not gap_file.exists():
        return []
    return [json.loads(line) for line in gap_file.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- module isolation -----------------------------------------------------------------


def test_module_only_imports_allowed_stdlib_modules() -> None:
    tree = ast.parse(_SPOOL_MODULE.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports break importlib loading from the hook"
            assert node.module is not None
            seen.add(node.module.split(".")[0])
    assert seen <= _ALLOWED_IMPORTS, f"unexpected imports: {sorted(seen - _ALLOWED_IMPORTS)}"


@pytest.mark.subprocess  # spawns a child interpreter; see tests/conftest.py::_no_subprocess
def test_module_loads_standalone_without_pydantic_or_package_init() -> None:
    script = (
        "import sys, importlib.util\n"
        "sys.modules['pydantic'] = None\n"
        "sys.modules['tce_shared'] = None\n"
        f"spec = importlib.util.spec_from_file_location('capture_spool_standalone', {str(_SPOOL_MODULE)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print(mod.idempotent_key('s', 'p', 'c'))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == hashlib.sha256(b"s|p|c").hexdigest()


def test_standalone_load_matches_package_import(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("capture_spool_file", _SPOOL_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SPOOL_SCHEMA_VERSION == SPOOL_SCHEMA_VERSION
    assert module.idempotent_key("a", "b", "c") == idempotent_key("a", "b", "c")


# --- idempotent key -------------------------------------------------------------------


def test_idempotent_key_matches_delivery_key_algorithm() -> None:
    expected = hashlib.sha256(b"sess|p1|abc").hexdigest()
    assert idempotent_key("sess", "p1", "abc") == expected
    assert idempotent_key("sess", "p1", "abc") == idempotent_key("sess", "p1", "abc")
    assert idempotent_key("sess", "p2", "abc") != expected
    assert idempotent_key("other", "p1", "abc") != expected
    try:
        from tce_shared.decision_capture import compute_delivery_key
    except ImportError:  # decision_capture lands from a sibling builder; parity is asserted once it exists
        return
    assert compute_delivery_key("sess", "p1", "abc") == idempotent_key("sess", "p1", "abc")


# --- spool paths ----------------------------------------------------------------------


def test_spool_path_uses_delivery_key_filename(tmp_path: Path) -> None:
    key = _key("x")
    assert spool_path(tmp_path, key) == tmp_path / f"{key}.json"


@pytest.mark.parametrize("bad_key", ["", "../escape", "a/b", "a\\b", "state", "with space", "UPPER"])
def test_spool_path_rejects_unsafe_keys(tmp_path: Path, bad_key: str) -> None:
    with pytest.raises(ValueError):
        spool_path(tmp_path, bad_key)


# --- write / read ---------------------------------------------------------------------


def test_write_read_round_trip_oldest_first(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    written = [_entry("c", 3.0), _entry("a", 1.0), _entry("b", 2.0)]
    for entry in written:
        path = write_entry(root, entry)
        assert path == spool_path(root, entry.delivery_key)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    entries = read_entries(root, limit=10)
    assert [e.created_at for e in entries] == [1.0, 2.0, 3.0]
    assert [e.delivery_key for e in entries] == [_key("a"), _key("b"), _key("c")]
    assert entries[0].body == written[1].body
    assert entries[0].attempts == 0
    assert entries[0].last_error is None
    assert entries[0].state == SpoolEntryState.SPOOLED
    assert read_entries(root, limit=2)[-1].created_at == 2.0
    assert read_entries(root, limit=0) == []
    assert pending_count(root) == 3


def test_write_entry_is_atomic_when_the_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "spool"
    original = _entry("a", 1.0)
    write_entry(root, original)

    def boom(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(capture_spool.os, "fsync", boom)
    updated = SpoolEntry(delivery_key=original.delivery_key, body={**original.body, "content": "changed"}, created_at=1.0, attempts=1)
    with pytest.raises(OSError):
        write_entry(root, updated)

    # the previous good file is intact and no temp file leaks
    assert json.loads(spool_path(root, original.delivery_key).read_text(encoding="utf-8"))["body"] == original.body
    assert sorted(p.name for p in root.iterdir()) == [f"{original.delivery_key}.json"]


def test_same_delivery_key_maps_to_one_file(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    entry = _entry("a", 1.0)
    write_entry(root, entry)
    write_entry(root, entry)
    assert pending_count(root) == 1
    remove_entry(root, entry.delivery_key)
    assert pending_count(root) == 0
    remove_entry(root, entry.delivery_key)  # idempotent


def test_read_entries_tolerates_corrupt_and_foreign_files(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    good = _entry("good", 2.0)
    write_entry(root, good)
    corrupt_key = _key("corrupt")
    (root / f"{corrupt_key}.json").write_text("{not json", encoding="utf-8")
    wrong_shape_key = _key("shape")
    (root / f"{wrong_shape_key}.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    (root / STATE_FILE).write_text(json.dumps({"capture_delivery_state": "delivered"}), encoding="utf-8")
    (root / "notes.txt").write_text("ignored", encoding="utf-8")
    (root / ".tmp-partial").write_text("{", encoding="utf-8")

    entries = read_entries(root, limit=10)
    assert [e.delivery_key for e in entries] == [good.delivery_key]
    # corrupt files are quarantined so they never block the queue again, and the loss is visible
    assert not (root / f"{corrupt_key}.json").exists()
    assert not (root / f"{wrong_shape_key}.json").exists()
    assert (root / f"{corrupt_key}.json.corrupt").exists()
    reasons = {(g["delivery_key"], g["reason"]) for g in _read_gaps(root)}
    assert reasons == {(corrupt_key, "corrupt"), (wrong_shape_key, "corrupt")}
    assert pending_count(root) == 1
    assert read_entries(tmp_path / "missing", limit=5) == []


# --- bounded size ---------------------------------------------------------------------


def test_enforce_cap_noop_under_limits(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    write_entry(root, _entry("a", 1.0))
    assert enforce_cap(root) == []
    assert not (root / GAP_FILE).exists()
    assert enforce_cap(tmp_path / "missing") == []
    assert DEFAULT_MAX_FILES == 500
    assert DEFAULT_MAX_BYTES == 8 * 1024 * 1024


def test_enforce_cap_by_file_count_evicts_oldest_and_records_gaps(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    for seed, created in (("e", 5.0), ("a", 1.0), ("c", 3.0), ("b", 2.0), ("d", 4.0), ("f", 6.0)):
        write_entry(root, _entry(seed, created))
    evicted = enforce_cap(root, max_files=4)
    assert evicted == [_key("a"), _key("b")]
    assert not spool_path(root, _key("a")).exists()
    assert not spool_path(root, _key("b")).exists()
    assert pending_count(root) == 4
    gaps = _read_gaps(root)
    assert [g["delivery_key"] for g in gaps] == [_key("a"), _key("b")]
    for gap in gaps:
        assert gap["reason"] == "dropped_full"
        assert len(gap["content_sha256"]) == 64
        assert gap["observed_at"].startswith("2026-09-09T10:00:")
        assert gap["dropped_at"].endswith("+00:00")
    assert gaps[0]["content_sha256"] == _key("content-a")


def test_enforce_cap_by_bytes(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    for seed, created in (("a", 1.0), ("b", 2.0), ("c", 3.0)):
        write_entry(root, _entry(seed, created))
    one_file = spool_path(root, _key("c")).stat().st_size
    evicted = enforce_cap(root, max_files=100, max_bytes=one_file * 2)
    assert evicted == [_key("a")]
    assert pending_count(root) == 2


def test_gap_file_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "spool"
    monkeypatch.setattr(capture_spool, "GAP_MAX_BYTES", 400)
    monkeypatch.setattr(capture_spool, "GAP_KEEP_LINES", 2)
    for i in range(12):
        write_entry(root, _entry(f"k{i}", float(i)))
        enforce_cap(root, max_files=1)
    gaps = _read_gaps(root)
    assert len(gaps) <= 3
    assert gaps[-1]["delivery_key"] == _key("k10")
    assert (root / GAP_FILE).stat().st_size <= 400 + 400


# --- classification -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exc", "expected"),
    [
        (200, None, SpoolEntryState.DELIVERED),
        (201, None, SpoolEntryState.DELIVERED),
        (204, None, SpoolEntryState.DELIVERED),
        (409, None, SpoolEntryState.DELIVERED),
        (401, None, SpoolEntryState.FAILED),
        (403, None, SpoolEntryState.FAILED),
        (422, None, SpoolEntryState.FAILED),
        (400, None, SpoolEntryState.SPOOLED),
        (404, None, SpoolEntryState.SPOOLED),
        (429, None, SpoolEntryState.SPOOLED),
        (500, None, SpoolEntryState.SPOOLED),
        (503, None, SpoolEntryState.SPOOLED),
        (None, "timed out", SpoolEntryState.SPOOLED),
        (None, None, SpoolEntryState.SPOOLED),
        (200, "ignored when status says delivered", SpoolEntryState.DELIVERED),
    ],
)
def test_classify_http(status: int | None, exc: str | None, expected: SpoolEntryState) -> None:
    assert classify_http(status, exc) is expected


def test_spool_entry_state_values_are_plain_strings() -> None:
    assert SpoolEntryState.SPOOLED == "spooled"
    assert SpoolEntryState.DELIVERED == "delivered"
    assert SpoolEntryState.FAILED == "failed"
    assert SpoolEntryState.DROPPED_FULL == "dropped_full"
    assert json.dumps({"s": SpoolEntryState.FAILED}) == '{"s": "failed"}'


# --- state file -----------------------------------------------------------------------


def test_load_state_defaults_and_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    state = load_state(root)
    assert state["capture_delivery_state"] == "unknown"
    assert state["pending_count"] == 0
    assert state["schema_version"] == SPOOL_SCHEMA_VERSION
    assert state["gap_since"] is None
    assert state["failures_since_delivery"] == 0
    for key in ("last_delivered_at", "last_error", "last_attempt_at"):
        assert state[key] is None

    state["capture_delivery_state"] = "spooled"
    state["pending_count"] = 3
    state["last_error"] = "connection refused"
    save_state(root, state)
    path = root / STATE_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = load_state(root)
    assert reloaded["capture_delivery_state"] == "spooled"
    assert reloaded["pending_count"] == 3
    assert reloaded["last_error"] == "connection refused"
    assert reloaded["schema_version"] == SPOOL_SCHEMA_VERSION
    assert sorted(p.name for p in root.iterdir()) == [STATE_FILE]


def test_load_state_tolerates_corruption(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    (root / STATE_FILE).write_text("{oops", encoding="utf-8")
    assert load_state(root)["capture_delivery_state"] == "unknown"
    (root / STATE_FILE).write_text("[]", encoding="utf-8")
    assert load_state(root)["capture_delivery_state"] == "unknown"
    (root / STATE_FILE).write_text(json.dumps({"capture_delivery_state": "failed"}), encoding="utf-8")
    partial = load_state(root)
    assert partial["capture_delivery_state"] == "failed"
    assert partial["pending_count"] == 0  # missing keys are filled in


def test_save_state_rejects_unknown_delivery_state(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    state["capture_delivery_state"] = "bogus"
    with pytest.raises(ValueError):
        save_state(tmp_path, state)


def test_state_transitions_are_visible(tmp_path: Path) -> None:
    state = load_state(tmp_path)

    state = apply_delivery_outcome(state, SpoolEntryState.SPOOLED, pending_count=1, error="connection refused", now=100.0)
    assert state["capture_delivery_state"] == "spooled"
    assert state["pending_count"] == 1
    assert state["failures_since_delivery"] == 1
    assert state["last_error"] == "connection refused"
    assert state["last_attempt_at"] == 100.0
    assert state["gap_since"] is None

    state = apply_delivery_outcome(state, SpoolEntryState.DROPPED_FULL, pending_count=500, error="spool full", now=101.0)
    assert state["capture_delivery_state"] == "dropped_full"
    assert state["gap_since"] == 101.0
    assert state["failures_since_delivery"] == 2

    state = apply_delivery_outcome(state, SpoolEntryState.DROPPED_FULL, pending_count=500, error="spool full", now=102.0)
    assert state["gap_since"] == 101.0  # first gap timestamp is kept while gaps persist

    state = apply_delivery_outcome(state, SpoolEntryState.FAILED, pending_count=500, error="403 forbidden", now=103.0)
    assert state["capture_delivery_state"] == "failed"
    assert state["last_error"] == "403 forbidden"
    assert state["gap_since"] == 101.0

    state = apply_delivery_outcome(state, SpoolEntryState.DELIVERED, pending_count=0, now=104.0)
    assert state["capture_delivery_state"] == "delivered"
    assert state["failures_since_delivery"] == 0
    assert state["last_error"] is None
    assert state["last_delivered_at"] == 104.0
    assert state["gap_since"] is None
    assert state["pending_count"] == 0
    save_state(tmp_path, state)
    assert load_state(tmp_path)["capture_delivery_state"] == "delivered"


def test_disabled_state_is_persistable(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    state["capture_delivery_state"] = "disabled"
    save_state(tmp_path, state)
    assert load_state(tmp_path)["capture_delivery_state"] == "disabled"


# --- redelivery -----------------------------------------------------------------------


def test_record_attempt_keeps_file_until_delivered(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    entry = _entry("a", 1.0)
    write_entry(root, entry)

    spooled = record_attempt(root, entry, SpoolEntryState.SPOOLED, error="connection refused")
    assert spooled.attempts == 1
    assert spooled.state == SpoolEntryState.SPOOLED
    assert spooled.last_error == "connection refused"
    assert retryable(spooled)
    on_disk = read_entries(root, limit=1)[0]
    assert on_disk.attempts == 1 and on_disk.last_error == "connection refused"
    assert on_disk.delivery_key == entry.delivery_key  # same key => same file => idempotent redelivery

    delivered = record_attempt(root, on_disk, SpoolEntryState.DELIVERED)
    assert delivered.state == SpoolEntryState.DELIVERED
    assert delivered.attempts == 2
    assert not spool_path(root, entry.delivery_key).exists()
    assert pending_count(root) == 0


def test_failed_entries_stop_retrying_after_max_attempts(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    entry = _entry("a", 1.0)
    write_entry(root, entry)
    assert MAX_FAILED_ATTEMPTS == 3
    for attempt in range(1, MAX_FAILED_ATTEMPTS + 1):
        entry = record_attempt(root, entry, SpoolEntryState.FAILED, error="403 forbidden")
        assert entry.attempts == attempt
        assert entry.state == SpoolEntryState.FAILED
        assert retryable(entry) is (attempt < MAX_FAILED_ATTEMPTS)
    # permanent failure keeps the file (visible), but is no longer retried
    assert spool_path(root, entry.delivery_key).exists()
    assert retryable(read_entries(root, limit=1)[0]) is False
    # a transient failure never hits the cap
    transient = SpoolEntry(delivery_key=_key("t"), body={}, created_at=0.0, attempts=50, state=SpoolEntryState.SPOOLED)
    assert retryable(transient)


def test_record_attempt_dropped_full_removes_file_and_records_gap(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    entry = _entry("a", 1.0)
    write_entry(root, entry)
    dropped = record_attempt(root, entry, SpoolEntryState.DROPPED_FULL, error="spool full")
    assert dropped.state == SpoolEntryState.DROPPED_FULL
    assert not spool_path(root, entry.delivery_key).exists()
    assert [g["reason"] for g in _read_gaps(root)] == ["dropped_full"]


def test_spool_files_are_private(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    write_entry(root, _entry("a", 1.0))
    enforce_cap(root, max_files=0)
    save_state(root, load_state(root))
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert os.access(root, os.R_OK | os.W_OK | os.X_OK)
