"""The TCE dispatch supervisor.

A separate host process, not part of ``services/tce_api``.  Three independent reasons, all in
design §3.1: the manager's container holds ``/var/run/docker.sock``, the repo ``.env`` and the
enforcement database, and a dispatcher hosted there would hand the dispatched agent a parent
holding all three; Seatbelt is macOS-only and cannot be applied from inside a Linux container;
and the verifier must run on a platform whose identity is recorded.

The package imports ``tce_shared`` plus the standard library, ``requests`` and
``pydantic-settings``, and talks HTTP.  It imports no backend module and no database driver;
``tests/unit/test_supervisor_startup.py::test_supervisor_imports_no_backend`` walks every module
here with ``ast`` and asserts it.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.4.0"
