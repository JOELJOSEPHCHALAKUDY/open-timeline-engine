#!/usr/bin/env python3
"""A fake ``codex exec --json``: flat NDJSON on stdout, no stdin, no network, no credential.

The line shapes are copied from the captured live transcript in ``codex_exec_turn.jsonl``.
``TCE_FAKE_MODE`` selects ``clean`` (default), ``fail`` or ``hang``.
"""

from __future__ import annotations

import json
import os
import sys
import time

VERSION = os.environ.get("TCE_FAKE_VERSION", "codex-cli 0.153.1")
THREAD_ID = "01a086d7-6130-7102-8f1c-7ca4629db431"


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    if "--version" in sys.argv[1:]:
        print(VERSION)
        return 0
    try:
        with open("fake_child_env.json", "w", encoding="utf-8") as handle:
            json.dump(sorted(os.environ), handle)
    except OSError:
        pass
    mode = os.environ.get("TCE_FAKE_MODE", "clean")
    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})
    if mode == "hang":
        while True:
            time.sleep(0.2)
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "OK"}})
    usage = {"input_tokens": 17540, "cached_input_tokens": 12928, "cache_write_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}
    if mode == "fail":
        emit({"type": "turn.failed", "usage": usage})
        return 1
    emit({"type": "turn.completed", "usage": usage})
    return 0


if __name__ == "__main__":
    sys.exit(main())
