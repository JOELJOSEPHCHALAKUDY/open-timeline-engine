#!/usr/bin/env python3
"""A fake ``codex app-server``: real process, real JSON-RPC on stdout, no network, no credential.

Behaviour is selected by ``TCE_FAKE_MODE`` in the child's environment:

``clean``              answer the handshake, emit one command and one file-change item, complete.
``fail``               the same, but the turn fails.
``hang_after_start``   answer the handshake, then never complete the turn (for the wall clock).
``silent``             exit immediately without answering anything.

It writes the sorted names of every environment variable it received to ``fake_child_env.json`` in
its working directory. That file is the evidence for the assertion that matters most here: the
adapter passes the child a constructed environment and **nothing** is inherited.
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


def notify(method: str, params: dict[str, object]) -> None:
    emit({"jsonrpc": "2.0", "method": method, "params": params})


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(VERSION)
        return 0
    mode = os.environ.get("TCE_FAKE_MODE", "clean")
    try:
        with open("fake_child_env.json", "w", encoding="utf-8") as handle:
            json.dump(sorted(os.environ), handle)
    except OSError:
        pass
    if mode == "silent":
        return 0

    for raw in sys.stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake-codex/0.153.1", "codexHome": os.environ.get("CODEX_HOME", "")}})
        elif method == "thread/start":
            emit({"jsonrpc": "2.0", "id": request_id, "result": {"threadId": THREAD_ID}})
            notify("thread/started", {"threadId": THREAD_ID})
        elif method == "turn/start":
            notify("item/completed", {"threadId": THREAD_ID, "item": {"id": "item_0", "type": "command_execution", "command": "/bin/echo hello", "exit_code": 0, "status": "completed"}})
            notify("item/completed", {"threadId": THREAD_ID, "item": {"id": "item_1", "type": "file_change", "path": "src/agent_wrote.txt"}})
            notify("item/tool/requestUserInput", {"threadId": THREAD_ID, "item": {"id": "item_2", "type": "tool"}})
            if mode == "hang_after_start":
                while True:
                    time.sleep(0.2)
            notify("item/completed", {"threadId": THREAD_ID, "item": {"id": "item_3", "type": "agent_message", "text": "OK"}})
            usage = {"input_tokens": 17540, "cached_input_tokens": 12928, "output_tokens": 5, "reasoning_output_tokens": 0}
            if mode == "fail":
                notify("turn/failed", {"threadId": THREAD_ID, "status": "failed", "usage": usage})
                return 1
            notify("turn/completed", {"threadId": THREAD_ID, "status": "completed", "usage": usage})
            return 0
        elif method == "turn/interrupt":
            notify("turn/completed", {"threadId": THREAD_ID, "status": "interrupted", "usage": {}})
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
