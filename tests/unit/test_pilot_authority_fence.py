"""G12 — the pilot can measure the system and can never grant it anything.

P4's R11 established the rule for the behaviour pilot: a gate that governs which *context arm* a
turn is given may never decide what a caller is *allowed to do*.  ``test_policy_route_identity.py``
is the fence for that one.  P6 borrows the vocabulary and the fence together, because P6's report
is a much more tempting thing to read from an authority path: it carries per-arm success rates and
a ``supported`` boolean, and one import from a permit handler would turn a descriptive pilot into
a promotion mechanism nobody designed.

The fence is an AST walk over ``services/tce_api``, ``services/tce_lite_api``, ``services/tce_mcp``
and ``shared/tce_shared``.  It asserts three things:

1. **No authority module imports the pilot.**  A module that defines a permit, promotion,
   exposure, personalization or qualification symbol may not import ``pilot_enrollment``,
   ``pilot_thresholds`` or either ``pilot_store``.
2. **No authority function calls a pilot symbol.**  The import fence alone is satisfied by a
   module-level alias; the call fence is what survives one.
3. **The report handler is a leaf.**  ``GET /v1/pilot/report``'s handler is called by no other
   handler, so no decision anywhere is downstream of it.

This gate is **guard-for-later**: the pilot modules exist (Builder A landed two of them) but no
route or store does yet, so it can only go red once B and C land.  That is exactly when it is
worth having — a fence built after the thing it fences is a fence built around a violation.  To
keep it from being vacuous in the meantime, every scanner here is also run over a synthetic
violating module, and the authority-symbol census is asserted non-empty against the real tree.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SCANNED_ROOTS = (
    "services/tce_api/tce_api",
    "services/tce_lite_api/tce_lite_api",
    "services/tce_mcp/tce_mcp",
    "shared/tce_shared",
)

#: The P6 modules no authority path may reach.  ``pilot_store`` is matched as a suffix so both
#: backends' stores are covered the moment Builder B or C creates one.
PILOT_MODULES = ("pilot_enrollment", "pilot_thresholds", "pilot_store")

#: Symbols only the pilot defines. A call to one of these from an authority function is the
#: violation the import fence alone would miss.
PILOT_CALLS = (
    "allocate_arm",
    "resolve_enrolment",
    "elect_arm",
    "claim_a",
    "claim_b",
    "episode_success",
    "worst_cell",
    "build_report",
    "record_pilot_close",
    "load_pilot_episodes",
)

#: What "authority" means here, stated once. A function whose name contains one of these decides
#: what a caller may do, or whether a personalization is used.
AUTHORITY_MARKERS = ("permit", "promotion", "promote", "exposure", "personaliz", "qualification", "authoriz", "charter")

#: Names that merely *describe* rather than grant. ``permitted_action`` is a wire enum
#: converter; excluding it by name keeps the census honest rather than inflated.
AUTHORITY_EXEMPT = ("to_next_permitted_action", "permit_scope_digest")


def _sources() -> Iterator[tuple[str, str]]:
    for root in SCANNED_ROOTS:
        base = REPO / root
        if not base.exists():  # pragma: no cover - a moved package is another test's problem
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8")


def _imports_pilot(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module == name or module.endswith(f".{name}") for name in PILOT_MODULES):
                found.append((node.lineno, module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == name or alias.name.endswith(f".{name}") for name in PILOT_MODULES):
                    found.append((node.lineno, alias.name))
    return found


def _authority_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        lowered = node.name.lower()
        if node.name in AUTHORITY_EXEMPT:
            continue
        if any(marker in lowered for marker in AUTHORITY_MARKERS):
            out.append(node)
    return out


def _calls(node: ast.AST) -> Iterator[tuple[str, int]]:
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            yield func.id, inner.lineno
        elif isinstance(func, ast.Attribute):
            yield func.attr, inner.lineno


# --------------------------------------------------------------------------------------
# The scanner, as one pure predicate over (path, source) — reused by the twin below.
# --------------------------------------------------------------------------------------


def scan_authority_fence(units: Iterator[tuple[str, str]]) -> tuple[list[str], int]:
    """``(offences, authority_functions_seen)``.  The second value is the non-vacuity witness."""

    offences: list[str] = []
    seen = 0
    for rel, source in units:
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        authority = _authority_functions(tree)
        seen += len(authority)
        # The module-level import fence applies to NARROW modules only. Each backend's `main.py`
        # is one file holding every handler in the service -- the permit routes and the pilot
        # routes both -- so "this module imports the pilot" is true there by construction and
        # says nothing. For `main.py` the meaningful fence is the per-function call fence below,
        # which is also the fence P4's `test_policy_route_identity.py` applies (it scans the
        # policy stores, never `main.py`).
        if authority and not rel.endswith("/main.py"):
            for lineno, module in _imports_pilot(tree):
                offences.append(f"{rel}:{lineno} defines an authority symbol and imports {module}")
        for function in authority:
            for name, lineno in _calls(function):
                if name in PILOT_CALLS:
                    offences.append(f"{rel}:{lineno} {function.name}() calls {name}()")
    return offences, seen


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def test_no_permission_path_reads_the_pilot() -> None:
    """G12.  The report can never grant authority — not by import, and not by call."""

    offences, seen = scan_authority_fence(_sources())
    main_seen = sum(len(_authority_functions(ast.parse(source))) for rel, source in _sources() if rel.endswith("/main.py"))
    assert main_seen >= 5, (
        f"only {main_seen} authority functions were found in the two main.py files, which are exempt from the "
        "module-level import fence. If the call fence sees nothing there, the exemption has gone blind."
    )
    assert seen >= 20, (
        f"only {seen} authority functions were found across {SCANNED_ROOTS}; the fence would pass "
        "vacuously. Either the packages moved or AUTHORITY_MARKERS no longer matches this tree."
    )
    assert not offences, (
        "the pilot is descriptive and may never decide what a caller is allowed to do (C12, P4 R11):\n  "
        + "\n  ".join(offences)
    )


def test_the_fence_discriminates() -> None:
    """The twin.  A fence that has never seen a violation is a fence nobody has tested."""

    importing = ("services/tce_api/tce_api/policy_store.py", "from .pilot_store import load_pilot_episodes\n\ndef request_execution_permit():\n    return True\n")
    offences, seen = scan_authority_fence(iter([importing]))
    assert seen == 1 and offences, "an authority module importing the pilot store went unnoticed"

    calling = ("shared/tce_shared/decision_capture.py", "def promotion_decision(rows):\n    return claim_a(rows).supported\n")
    offences, seen = scan_authority_fence(iter([calling]))
    assert seen == 1 and any("claim_a" in offence for offence in offences), offences

    aliased = ("shared/tce_shared/decision_policy.py", "import tce_shared.pilot_enrollment as p\n\ndef exposure_state():\n    return p\n")
    offences, _ = scan_authority_fence(iter([aliased]))
    assert offences, "a module-level alias import slipped past the fence"

    innocent = ("shared/tce_shared/autonomy_goals.py", "from .events import Thing\n\ndef request_execution_permit():\n    return Thing\n")
    offences, seen = scan_authority_fence(iter([innocent]))
    assert seen == 1 and not offences, offences

    # `main.py` is exempt from the module-level IMPORT fence and not from the CALL fence. Both
    # halves of that are asserted, because the exemption is the only place this gate could go
    # quietly blind: every route in the service lives in that one file.
    main_import = ("services/tce_api/tce_api/main.py", "from .pilot_store import load_pilot_episodes\n\ndef request_execution_permit():\n    return True\n")
    offences, seen = scan_authority_fence(iter([main_import]))
    assert seen == 1 and not offences, "the module-level exemption for main.py is what lets the pilot routes live beside the permit routes"

    main_call = ("services/tce_api/tce_api/main.py", "def request_execution_permit():\n    return worst_cell(load_pilot_episodes())\n")
    offences, seen = scan_authority_fence(iter([main_call]))
    assert seen == 1 and len(offences) == 2, f"main.py must NOT be exempt from the call fence: {offences}"


def test_the_report_handler_is_a_leaf() -> None:
    """No decision anywhere is downstream of the report.

    A handler another handler calls is a handler whose verdict can be acted on. The report's is
    reachable only from the HTTP route it decorates.
    """

    units = [(rel, source) for rel, source in _sources() if rel.endswith("/main.py")]
    handlers: dict[str, str] = {}
    for rel, source in units:
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            routed = any(
                isinstance(decorator, ast.Call)
                and any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("/v1/pilot/report") for arg in decorator.args)
                for decorator in node.decorator_list
            )
            if routed:
                handlers[node.name] = rel

    # Non-vacuity: the moment a backend serves the path, this scan must have found its handler.
    serving = [rel for rel, source in units if "/v1/pilot/report" in source]
    assert not serving or handlers, (
        f"{serving} mention /v1/pilot/report but no decorated handler was identified; the leaf check "
        "would pass vacuously against a route it cannot see"
    )

    callers: list[str] = []
    for rel, source in units:
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name not in handlers:
                for name, lineno in _calls(node):
                    if name in handlers:
                        callers.append(f"{rel}:{lineno} {node.name}() calls the report handler {name}()")
    assert not callers, "\n  ".join(callers)
