"""G1 and G2 — who is allowed to write a verdict, and who is allowed to move the goalposts.

Both are pure source analysis over ``shared/`` and ``services/``: no database, no imports of the
backends, so they run in the fast unit job.

G1 exists because ``verification_state`` held exactly one value — the literal ``'unverified'`` —
from the day the column shipped, while the autonomy KPI gate treated the executor's own
``details["verification"]`` booleans as verification.  The gate is deliberately scoped to
``directive_executions``: ``handoff_records.verification_state`` is a mirror written at delivery
and is never read for a decision, so counting it would make the assertion fail after the work
lands rather than before it.

Note the shape of the detector.  The producing write is ``SET verification_state = :state`` — a
BIND PARAMETER — so a scan for non-``'unverified'`` string literals finds zero sites and would
fail forever; and loosening it to "any SET" pulls in the two report paths that pin the literal.
The rule that actually separates them: the assigned value is not the literal ``'unverified'``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = (_ROOT / "shared", _ROOT / "services")
_SKIP_PARTS = ("build", "__pycache__", ".venv", "node_modules")

_UPDATE_DIRECTIVES = re.compile(r"update\s+directive_executions\b", re.IGNORECASE)
_SET_VERIFICATION = re.compile(r"verification_state\s*=\s*('[^']*'|\"[^\"]*\"|\?|:[a-z_]+)", re.IGNORECASE)

# The one permitted writer, per backend.  Both entries are the same module; there is no third.
_PERMITTED_WRITERS = frozenset(
    {
        "services/tce_api/tce_api/verification_store.py",
        "services/tce_lite_api/tce_lite_api/verification_store.py",
    }
)


def _python_files() -> list[Path]:
    out: list[Path] = []
    for root in _SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            out.append(path)
    return sorted(out)


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_verification_store_is_the_only_writer() -> None:
    writers: set[str] = set()
    for path in _python_files():
        for literal in _string_literals(path):
            if not _UPDATE_DIRECTIVES.search(literal):
                continue
            for match in _SET_VERIFICATION.finditer(literal):
                assigned = match.group(1).strip()
                if assigned in ("'unverified'", '"unverified"'):
                    # The report path pins the literal on purpose; that is not a verdict.
                    continue
                writers.add(str(path.relative_to(_ROOT)))
    assert writers, "no verification writer exists at all — the verifier was never built"
    assert writers == set(_PERMITTED_WRITERS), f"unexpected writers of directive_executions.verification_state: {sorted(writers)}"


def test_acceptance_criteria_is_append_only() -> None:
    insert_sites: set[str] = set()
    for path in _python_files():
        rel = str(path.relative_to(_ROOT))
        for literal in _string_literals(path):
            lowered = " ".join(literal.lower().split())
            assert "update acceptance_criteria" not in lowered, f"{rel} contains an UPDATE of acceptance_criteria"
            assert "delete from acceptance_criteria" not in lowered, f"{rel} contains a DELETE of acceptance_criteria"
            if "insert into acceptance_criteria" in lowered:
                insert_sites.add(rel)
    assert insert_sites == set(_PERMITTED_WRITERS), f"acceptance_criteria INSERT sites: {sorted(insert_sites)}"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if segment:
                return segment
    raise AssertionError(f"{name} not found in {path}")


def test_the_execution_lifecycle_never_touches_the_criteria_table() -> None:
    """An executor that could reach acceptance_criteria from its own report or claim would be
    grading its own homework, whatever the route auth said."""
    full = _ROOT / "services" / "tce_api" / "tce_api" / "main.py"
    lite = _ROOT / "services" / "tce_lite_api" / "tce_lite_api" / "store.py"
    for path, names in ((full, ("takeover_execution_report", "takeover_execution_claim")), (lite, ("report_execution", "claim_execution"))):
        for name in names:
            assert "acceptance_criteria" not in _function_source(path, name), f"{name} mentions acceptance_criteria"


def _target_names_verification_state(target: ast.expr) -> bool:
    """True when this assignment TARGET names ``verification_state`` — as an attribute
    (``row.verification_state = ...``), as a string subscript (``payload["verification_state"] =
    ...``), or nested inside a tuple/list/starred unpack of either."""
    if isinstance(target, ast.Attribute):
        return target.attr == "verification_state"
    if isinstance(target, ast.Subscript):
        key = target.slice
        return isinstance(key, ast.Constant) and key.value == "verification_state"
    if isinstance(target, ast.Starred):
        return _target_names_verification_state(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return any(_target_names_verification_state(element) for element in target.elts)
    return False


def _verification_state_assignment_sites(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        else:
            continue
        if any(_target_names_verification_state(target) for target in targets):
            return True
    return False


def test_the_assignment_detector_actually_fires() -> None:
    """The file walk below finds nothing today, and would go on finding nothing if the predicate
    were broken.  Pin the predicate against synthetic source so the walk can never be vacuous."""
    for source in (
        "row.verification_state = 'passed'",
        'payload["verification_state"] = state',
        "obj.verification_state += 1",
        "a, row.verification_state = 1, 'passed'",
        "for x in y:\n    lease.verification_state = 'passed'",
    ):
        assert _verification_state_assignment_sites(ast.parse(source)), source
    for source in (
        "verification_state = 'unverified'",
        "d = {'verification_state': 'unverified'}",
        "f(verification_state='unverified')",
        "print(row.verification_state)",
        "row.verification_state_mirror = 'passed'",
    ):
        assert not _verification_state_assignment_sites(ast.parse(source)), source


def test_no_second_writer_assigns_verification_state_in_python() -> None:
    """The SQL scan above catches a second writer that speaks SQL.  This catches one that does not:
    an ORM attribute write (``execution.verification_state = 'passed'``) or a dict-row mutation
    (``row["verification_state"] = ...``) reaches the same column with none of the same authority
    checks, and the regex above would never see it."""
    offenders = {
        str(path.relative_to(_ROOT))
        for path in _python_files()
        if _verification_state_assignment_sites(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert offenders <= set(_PERMITTED_WRITERS), (
        f"verification_state is assigned outside the permitted writer: {sorted(offenders - set(_PERMITTED_WRITERS))}"
    )
