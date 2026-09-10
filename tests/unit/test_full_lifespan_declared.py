"""G6(a) — the FULL app declares a lifespan, and it is wired to the FastAPI construction.

Pure ``ast`` analysis: this test must run in the fast unit job, so it never imports
``tce_api.main`` (which would open a DB engine) and never touches a database.

Before P3 the FULL backend had no startup hook of any kind — no ``lifespan``, no ``on_event`` —
so a restarted manager resumed serving with stuck ``in_progress`` directives and unresolved
effects still in the database and nothing to notice them.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MAIN = Path(__file__).resolve().parents[2] / "services" / "tce_api" / "tce_api" / "main.py"


def _module() -> ast.Module:
    return ast.parse(_MAIN.read_text(encoding="utf-8"))


def test_full_app_declares_a_lifespan() -> None:
    module = _module()
    lifespans = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lifespan"
    ]
    assert len(lifespans) == 1, "services/tce_api/tce_api/main.py must declare exactly one _lifespan"

    wired = False
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "FastAPI"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "lifespan" and isinstance(keyword.value, ast.Name) and keyword.value.id == "_lifespan":
                wired = True
    assert wired, "FastAPI(...) must be constructed with lifespan=_lifespan"


def test_lifespan_calls_startup_reconcile() -> None:
    """A declared-but-empty lifespan would satisfy the shape and none of the purpose."""
    module = _module()
    lifespan = next(
        node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lifespan"
    )
    called = {
        node.func.id
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "startup_reconcile" in called


def test_dispatch_refuses_while_reconcile_is_pending() -> None:
    """The refusal, not the reconcile, is what stops work restarting without a charter."""
    source = _MAIN.read_text(encoding="utf-8")
    assert "reconcile_pending" in source or "reconcile_complete" in source
    dispatch_store = _MAIN.parent / "dispatch_store.py"
    text = dispatch_store.read_text(encoding="utf-8")
    assert "reconcile_complete()" in text
    assert "reconcile_pending" in text
