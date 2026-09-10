"""Exit gate G1 — no model call is reachable from the bounded control path.

This is a call-graph assertion over ``main.py``'s own AST, not a grep: a grep on a symbol that
was deleted is vacuously true forever, which is exactly how a gate stops testing anything.

The stop-set is documented, not incidental. ``clone_advice`` is the *deliberation* path: it
carries its own bounded budget, its failure already degrades to a safety gate, and
``decision_source=DELIBERATION`` is pinned by earlier tests. Moving it asynchronous would buy no
latency once it is deadline-gated and would cost those semantics. So the gate asserts that the
advisor is the ONLY reachable model call and that it is deadline-gated, rather than pretending
no model runs on the turn at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAIN_PY = _REPO_ROOT / "services" / "tce_api" / "tce_api" / "main.py"
_LITE_DIR = _REPO_ROOT / "services" / "tce_lite_api"
_FULL_DIR = _REPO_ROOT / "services" / "tce_api"

STOP_SET: frozenset[str] = frozenset({"clone_advice"})
FORBIDDEN: frozenset[str] = frozenset(
    {
        "get_model_gateway",  # the real local alias of factory.get_gateway
        "get_gateway",  # guards against a future direct import
        "create_gateway",  # guards against a future direct import
        "extract_structured",  # the only gateway method the plan/dream path ever called
        "_plan_gateway_settings",
        "_write_objective_plan_model",
        # The advisor's new entry point.  It runs a model, and it may only ever be reached
        # through `clone_advice`, which is the one deadline-gated exception in STOP_SET.
        "advisor_recommend",
    }
)

_ENTRY_POINTS = ("takeover_step", "takeover_autonomy_tick")


@pytest.fixture(scope="module")
def main_tree() -> ast.Module:
    return ast.parse(_MAIN_PY.read_text(encoding="utf-8"))


def _called_names(node: ast.AST) -> set[str]:
    """Every name that appears in call position under ``node``."""
    out: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            out.add(func.id)
        elif isinstance(func, ast.Attribute):
            out.add(func.attr)
    return out


def _module_call_graph(tree: ast.Module) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            graph[node.name] = _called_names(node)
    return graph


def _reachable(graph: dict[str, set[str]], roots: tuple[str, ...]) -> set[str]:
    """BFS from ``roots``, refusing to traverse *into* the documented stop-set.

    A stop-set member is still recorded as reached — that is what G1b asserts — but its own
    callees are not explored, because the advisor's bounded, safety-gated model call is the
    deliberate exception.
    """
    seen: set[str] = set()
    queue = [name for name in roots]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in STOP_SET:
            continue
        for callee in graph.get(current, set()):
            if callee not in seen:
                queue.append(callee)
    return seen


def test_stop_set_and_forbidden_set_are_pinned() -> None:
    """Widening either set must be a visible diff, never a silent gate weakening."""
    assert STOP_SET == frozenset({"clone_advice"})
    assert FORBIDDEN == frozenset(
        {
            "get_model_gateway",
            "get_gateway",
            "create_gateway",
            "extract_structured",
            "_plan_gateway_settings",
            "_write_objective_plan_model",
            "advisor_recommend",
        }
    )
    # _dreams_from_own_words is deliberately NOT in FORBIDDEN: the symbol no longer exists, and
    # a gate that names a non-existent symbol tests nothing. Its guarantee is the test below.
    assert "_dreams_from_own_words" not in FORBIDDEN


def test_control_path_call_graph_has_no_gateway(main_tree: ast.Module) -> None:
    graph = _module_call_graph(main_tree)
    for entry in _ENTRY_POINTS:
        assert entry in graph, f"{entry} is not a module-level function in main.py"
    reachable = _reachable(graph, _ENTRY_POINTS)
    assert reachable & FORBIDDEN == set(), (
        "a model gateway is reachable from the bounded control path: "
        f"{sorted(reachable & FORBIDDEN)}"
    )


def test_dreams_from_own_words_is_gone(main_tree: ast.Module) -> None:
    """G1a' — the deleted dream symbols are actually gone, not merely unreferenced."""
    for node in ast.walk(main_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in {"_dreams_from_own_words", "_recent_messages_for_dreaming"}
        if isinstance(node, ast.Name):
            assert node.id != "DREAM_PROMPT"
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (isinstance(target, ast.Name) and target.id == "DREAM_PROMPT")

    # The prompt's placeholder must not survive anywhere under the Full service either.
    for path in _FULL_DIR.rglob("*.py"):
        if "build/lib" in str(path):
            continue
        assert "__MESSAGES__" not in path.read_text(encoding="utf-8"), path


def test_advisor_is_deadline_gated(main_tree: ast.Module) -> None:
    """G1b — the advisor is reachable, and a deadline check sits just above the call."""
    graph = _module_call_graph(main_tree)
    reachable = _reachable(graph, _ENTRY_POINTS)
    assert "clone_advice" in reachable

    source_lines = _MAIN_PY.read_text(encoding="utf-8").splitlines()
    step = _function_node(main_tree, "takeover_step")
    call_line = _direct_call_line(step, "clone_advice")
    assert call_line is not None, "takeover_step no longer calls clone_advice directly"
    window = "\n".join(source_lines[max(0, call_line - 41) : call_line])
    assert "deadline.allows(advisor_min_ms)" in window


def test_advisor_unhealthy_survives_deadline_skip(main_tree: ast.Module) -> None:
    """A deadline skip must not silently drop the safety term out of needs_human."""
    step = _function_node(main_tree, "takeover_step")
    for node in ast.walk(step):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "advisor_unhealthy" not in targets:
            continue
        names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        assert "advisor_skipped_for_deadline" in names
        return
    pytest.fail("advisor_unhealthy assignment not found in takeover_step")


def test_clamped_timeout_does_not_increment_fail_streak(main_tree: ast.Module) -> None:
    """A timeout WE caused by clamping is latency, not advisor ill-health.

    Counting it would let a latency knob drive advisor_unhealthy -> SAFETY_GATE.
    """
    step = _function_node(main_tree, "takeover_step")
    for node in ast.walk(step):
        if not isinstance(node, ast.If):
            continue
        if not _contains_fail_streak_increment(node.orelse):
            continue
        guard_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "advisor_clamped_by_deadline" in guard_names:
            return
    pytest.fail(
        "the advisor_fail_streak increment is not guarded by advisor_clamped_by_deadline"
    )


def test_retrieval_child_is_built_at_the_call_site(main_tree: ast.Module) -> None:
    """S6 — the retrieval child's clock must start where retrieval starts.

    Built at the top of the turn it would be spent on hundreds of lines of non-retrieval work
    before the first query, degrading essentially every turn in the default configuration.
    """
    step = _function_node(main_tree, "takeover_step")
    assert _direct_call_line(step, "retrieval_child") is None, (
        "takeover_step builds the retrieval child itself; it belongs at the bundle call site"
    )

    working_set = _function_node(main_tree, "_build_takeover_working_set")
    child_line = _direct_call_line(working_set, "retrieval_child")
    bundle_line = _direct_call_line(working_set, "build_context_bundle")
    assert child_line is not None and bundle_line is not None
    statements = [
        stmt.lineno
        for stmt in working_set.body
        if child_line <= stmt.lineno <= bundle_line or bundle_line <= stmt.lineno <= child_line
    ]
    assert len(statements) <= 5, (
        f"{len(statements)} statements sit between retrieval_child and the bundle call"
    )


def test_projection_is_refreshed_after_invalidation(main_tree: ast.Module) -> None:
    """Every apply_task_state_events result must be bound to a name.

    Discarding it and re-using the stale projection makes the plan branch match the plan that
    was just invalidated, and hands the old contract revision to the very SQL filter added to
    stop that.
    """
    turn = _function_node(main_tree, "_takeover_turn_task_state")
    for node in ast.walk(turn):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if isinstance(call, ast.Call) and _call_name(call) == "apply_task_state_events":
            pytest.fail("apply_task_state_events result is discarded")
    assert _direct_call_line(turn, "apply_task_state_events") is not None

    rebinds = [
        node
        for node in ast.walk(turn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "projection" for t in node.targets)
    ]
    assert any(
        isinstance(node.value, ast.Attribute) and node.value.attr == "projection"
        for node in rebinds
    ), "the projection is never rebound from the write result"


def test_lite_has_no_gateway_import() -> None:
    """G1c — Lite has no model call at all, asserted on the import graph, not a grep."""
    forbidden_names = {"get_model_gateway", "extract_structured", "aembed", "OllamaGateway"}
    for path in _LITE_DIR.rglob("*.py"):
        if "build/lib" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("tce_model_gateway"), path
            elif isinstance(node, ast.ImportFrom):
                assert not str(node.module or "").startswith("tce_model_gateway"), path
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_names, f"{path}: {node.id}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_names, f"{path}: {node.attr}"


# --- helpers ------------------------------------------------------------------------------


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in main.py")


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _direct_call_line(node: ast.AST, name: str) -> int | None:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _call_name(child) == name:
            return int(child.lineno)
    return None


def _contains_fail_streak_increment(body: list[ast.stmt]) -> bool:
    for stmt in body:
        for child in ast.walk(stmt):
            if (
                isinstance(child, ast.AugAssign)
                and isinstance(child.target, ast.Name)
                and child.target.id == "advisor_fail_streak"
            ):
                return True
    return False
