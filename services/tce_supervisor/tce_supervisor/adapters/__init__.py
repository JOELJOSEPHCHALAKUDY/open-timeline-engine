"""The adapter registry.

``ADAPTERS`` keys are exactly ``SURFACES`` — a unit test asserts the equality in both directions,
because a registry that silently lacks a surface would make ``build_adapter`` fall through to a
``KeyError`` at dispatch time rather than at import time.

There is **no implicit fallback** between surfaces. Selection is explicit: the charter names the
``(runtime_id, runtime_version, surface)`` triples it permits and
``TCE_SUP_DEFAULT_RUNTIME_SURFACE`` picks among them. A fallback happens only after an *observed*
``available=False`` or a failed ``start``, is recorded as an effect-journal row, and re-runs the
charter check.
"""

from __future__ import annotations

from tce_shared.runtime_contract import SURFACES, RuntimeAdapter

from ..config import SupervisorSettings
from .claude_oneshot import ClaudeOneshotAdapter
from .claude_stream import ClaudeStreamAdapter
from .codex_app_server import CodexAppServerAdapter
from .codex_exec import CodexExecAdapter

ADAPTERS: dict[str, type[RuntimeAdapter]] = {
    "codex/app-server": CodexAppServerAdapter,
    "codex/exec": CodexExecAdapter,
    "claude/stream": ClaudeStreamAdapter,
    "claude/oneshot": ClaudeOneshotAdapter,
}

assert set(ADAPTERS) == set(SURFACES), "ADAPTERS must cover exactly SURFACES"


class UnknownSurface(KeyError):
    """The surface named is not one this build knows how to drive."""

    def __init__(self, surface: str) -> None:
        super().__init__(f"unknown runtime surface {surface!r}; known surfaces are {sorted(ADAPTERS)}")
        self.surface = surface


def build_adapter(surface: str, *, settings: SupervisorSettings) -> RuntimeAdapter:
    try:
        factory = ADAPTERS[surface]
    except KeyError as error:
        raise UnknownSurface(surface) from error
    return factory(settings=settings)  # type: ignore[call-arg]


__all__ = ["ADAPTERS", "UnknownSurface", "build_adapter"]
