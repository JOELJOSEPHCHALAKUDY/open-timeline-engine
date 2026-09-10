"""``claude/oneshot`` — a deliberately cheaper, non-interruptible surface.

It exists so a charter can name a cheap non-interruptible surface **on purpose**, not as a silent
fallback when something better fails.  ``--output-format json`` returns the terminal object and
nothing else, so ``observe`` is ``DEGRADED`` and almost every effect it produces is
``action_tracing="unavailable"`` — the journal row says so rather than implying the adapter watched
something it did not.

``interrupt`` is ``UNSUPPORTED``: single-message input mode has no interruption channel.  Only the
wall clock and ``kill`` apply, and a killed run resolves to ``unknown``.

``< /dev/null`` is correct **here and only here**.  With ``--input-format stream-json`` it produces
a silent no-op; this surface does not pass that flag.
"""

from __future__ import annotations

from tce_shared.runtime_contract import UnsupportedVerb, require_verb

from .claude_stream import SURFACE as STREAM_SURFACE  # noqa: F401  (kept for symmetry of imports)
from .claude_stream import ClaudeStreamAdapter

SURFACE: str = "claude/oneshot"


class ClaudeOneshotAdapter(ClaudeStreamAdapter):
    surface: str = SURFACE
    _oneshot: bool = True

    def interrupt(self, handle: str, reason: str) -> None:
        """Always raises. The supervisor never kills a process to simulate an interrupt."""
        require_verb(SURFACE, "interrupt")
        raise UnsupportedVerb(SURFACE, "interrupt")
