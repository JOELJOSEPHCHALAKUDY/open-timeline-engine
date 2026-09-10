"""Repository-wide pytest fixtures (P3 §10.6, Builder F).

This is the repo's first ``conftest.py``. It holds exactly three autouse fixtures and
nothing else — no helpers other tests are expected to import, no shared clients, no
database bootstrap. Anything richer belongs in the test file that needs it.

What the three guards actually enforce, stated plainly so nobody reads more into them
than they do:

``_restore_settings``
    Snapshots and restores the Full and Lite ``Settings`` singletons around every test.
    Many tests mutate the process-wide singleton in place (``settings.api_tokens = ...``)
    and clean up only the names they remember to list; an unlisted name leaks into every
    later test in the run. This restores the whole object, so no per-file guard list can
    fall behind. It does NOT isolate the environment variables those settings were built
    from, and it does not touch the MCP or worker settings caches.

``_no_subprocess``
    Fails a test that reaches ``subprocess.run``/``Popen``/``check_output`` without the
    ``subprocess`` marker. The reason this exists is concrete: a P3 adapter test that
    accidentally spawns the real ``codex`` or ``claude`` binary would otherwise simply do
    so, in CI, with whatever credentials the environment holds. It is NOT a sandbox — it
    patches three names on the ``subprocess`` module and nothing else. ``os.system``,
    ``os.execv``, ``asyncio.create_subprocess_exec`` and anyio's ``open_process`` all go
    around it. Do not describe it as "tests cannot spawn processes".

``_no_socket``
    Fails a test that *connects* an AF_INET/AF_INET6 socket to a non-loopback address
    without the ``network`` marker. It wraps ``socket.socket.connect``/``connect_ex``.
    It does NOT block ``bind``, ``listen``, unix sockets, DNS resolution, raw
    ``_socket.socket`` use, or anything that reaches the network without going through
    ``socket.socket``. Loopback is deliberately allowed: the integration suite talks to a
    local API and a local PostgreSQL, and ``tests/unit/test_capture_hook.py`` runs a
    ``ThreadingHTTPServer`` on 127.0.0.1.
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

# --------------------------------------------------------------------------------------
# _restore_settings
# --------------------------------------------------------------------------------------

# Both backends expose ``get_settings`` as an ``@lru_cache(maxsize=1)`` factory over a
# pydantic ``Settings`` model. The cached instance is process-wide and mutable.
_SETTINGS_MODULES: tuple[str, ...] = ("tce_api.config", "tce_lite_api.config")


def _import_settings_module(dotted: str) -> ModuleType | None:
    try:
        module = __import__(dotted, fromlist=["get_settings"])
    except Exception:  # pragma: no cover - a backend that is not installed in this leg
        return None
    return module if hasattr(module, "get_settings") else None


def _cached_settings(module: ModuleType) -> Any | None:
    """Return the cached Settings instance, or None if nothing is cached yet.

    Deliberately does not *call* ``get_settings()``: constructing the singleton mid-test
    would build it from whatever environment that test happens to have set, and then
    leave that instance cached for every later test. Only an already-warm cache is
    snapshotted — ``_warm_settings`` is what guarantees it is warm.
    """
    getter = module.get_settings
    cache_info = getattr(getter, "cache_info", None)
    if cache_info is None or cache_info().currsize != 1:
        return None
    try:
        return getter()
    except Exception:  # pragma: no cover - a settings model that cannot rebuild
        return None


@pytest.fixture(scope="session", autouse=True)
def _warm_settings() -> None:
    """Materialise both settings singletons once, from the pristine session environment.

    Without this, the first test to touch ``get_settings()`` builds the singleton itself,
    and ``_restore_settings`` has nothing to snapshot for that test — so that one test's
    mutations leak into the whole run, which is exactly the failure this fixture pair
    exists to stop. Building here means the baseline is the environment pytest started
    with, not one a test arranged.
    """
    for name in _SETTINGS_MODULES:
        module = _import_settings_module(name)
        if module is None:
            continue
        try:
            module.get_settings()
        except Exception:  # pragma: no cover - a backend whose settings cannot be built
            pass


def _snapshot(instance: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"__dict__": dict(vars(instance))}
    fields_set = getattr(instance, "__pydantic_fields_set__", None)
    if isinstance(fields_set, set):
        snapshot["__pydantic_fields_set__"] = set(fields_set)
    return snapshot


def _apply(instance: Any, snapshot: dict[str, Any]) -> None:
    instance.__dict__.clear()
    instance.__dict__.update(snapshot["__dict__"])
    fields_set = snapshot.get("__pydantic_fields_set__")
    if fields_set is not None:
        object.__setattr__(instance, "__pydantic_fields_set__", set(fields_set))


@pytest.fixture(autouse=True)
def _restore_settings() -> Iterator[None]:
    """Restore the Full and Lite settings singletons to their pre-test values."""
    modules = [m for m in (_import_settings_module(name) for name in _SETTINGS_MODULES) if m is not None]
    before: list[tuple[ModuleType, Any, dict[str, Any]]] = []
    for module in modules:
        instance = _cached_settings(module)
        if instance is not None:
            before.append((module, instance, _snapshot(instance)))

    try:
        yield
    finally:
        for module, original, snapshot in before:
            # Restore the object the test may have mutated in place.
            _apply(original, snapshot)
            # If the test called ``get_settings.cache_clear()`` a *different* instance is
            # now cached, built from the test's environment. Restore the pre-test values
            # onto it too, so both the old references and the live singleton agree.
            current = _cached_settings(module)
            if current is not None and current is not original:
                _apply(current, snapshot)


# --------------------------------------------------------------------------------------
# _no_subprocess
# --------------------------------------------------------------------------------------

_SUBPROCESS_NAMES = ("run", "Popen", "check_output")

_SUBPROCESS_MESSAGE = (
    "subprocess.{name}() called from a test without @pytest.mark.subprocess. "
    "A test that spawns a real process can reach a real vendor binary with real "
    "credentials. If the spawn is intended, mark the test; do not remove this guard. "
    "argv={argv!r}"
)


def _blocked_subprocess(name: str) -> Any:
    def _blocked(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        raise pytest.fail.Exception(_SUBPROCESS_MESSAGE.format(name=name, argv=argv))

    return _blocked


@pytest.fixture(autouse=True)
def _no_subprocess(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any test that spawns a process without the ``subprocess`` marker."""
    if request.node.get_closest_marker("subprocess") is not None:
        yield
        return
    for name in _SUBPROCESS_NAMES:
        monkeypatch.setattr(subprocess, name, _blocked_subprocess(name), raising=True)
    yield


# --------------------------------------------------------------------------------------
# _no_socket
# --------------------------------------------------------------------------------------

_SOCKET_MESSAGE = (
    "socket connect to non-loopback address {address!r} from a test without "
    "@pytest.mark.network. Loopback is allowed; anything else in a unit or integration "
    "test is either an accident or a dependency on a host that will not exist in CI."
)


def _address_is_loopback(address: Any) -> bool:
    """True when the address is loopback, or is not an IP address we can judge.

    Unknown shapes (unix sockets, bytes paths, AF_NETLINK tuples) return True — this
    guard only claims to catch IP connections, and a guard that guesses would produce
    false failures nobody could act on.
    """
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return True
    if host in {"", "localhost", "localhost.localdomain"}:
        return True
    try:
        parsed = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        # A hostname that has not been resolved. ``socket.create_connection`` resolves
        # before connecting, so this is rare; treat it as blocked so a test that hand-rolls
        # a connect to "api.example.com" is caught.
        return False
    return bool(parsed.is_loopback)


@pytest.fixture(autouse=True)
def _no_socket(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any test that connects off-loopback without the ``network`` marker."""
    if request.node.get_closest_marker("network") is not None:
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _address_is_loopback(address):
            raise pytest.fail.Exception(_SOCKET_MESSAGE.format(address=address))
        return real_connect(self, address)

    def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if not _address_is_loopback(address):
            raise pytest.fail.Exception(_SOCKET_MESSAGE.format(address=address))
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex, raising=True)
    yield
