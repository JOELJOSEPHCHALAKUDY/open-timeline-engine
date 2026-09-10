"""``python -m tce_supervisor`` -> :func:`tce_supervisor.supervise.main`."""

from __future__ import annotations

import sys

from .supervise import main

if __name__ == "__main__":  # pragma: no cover - exercised by scripts/p3_exit_gate.sh
    sys.exit(main())
