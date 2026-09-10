"""G1, G8, G9, G20 — the deployed route and the evaluated route are the same route.

The reconnaissance behind P4 found thirteen decision routes in which DEPLOYED and EVALUATED are
disjoint sets.  The only route with the required contract — abstention, an OOD status, real
observation-id citations — was ``behavior_fidelity.predict_behavior``, and it had **no consumer in
any deployed decision**; it was what the evaluation harness evaluated.  Every takeover turn was
decided instead by an LLM prompt that nothing had ever scored.

A gate for that cannot be "both paths recorded the same ``policy_revision``" — both read the same
module constant, so the assertion is true by construction and stays true while the two paths build
completely different requests.  It has to be **callable identity**, resolved by import at test time,
plus (in ``tests/integration/test_policy_parity.py``) a canonical hash over the request itself.

* **G1** — the eval harness and both backends resolve ``decide`` to the same function object; no
  deployed site calls ``predict_behavior`` or ``advisor_recommend`` directly; ``derive_clone_guidance``
  is structurally unable to supply a confidence or an evidence-strength label.
* **G8** — goal selection is a different decision kind with a different label space and no
  adjudicated ground truth (R10).  It may not be described as personalization and may not read a
  ``policy_qualifications`` row.  Without this, goal selection quietly acquires a qualification
  dependency and the word "personalization" stops meaning anything measurable.
* **G9** — the pilot gate governs context arms.  It can never grant permission (R11).
* **G20** — ``RETRIEVAL_VERSION`` belongs to each loader, not to the policy.  Three loaders sharing
  one hardcoded string is how a Full qualification silently applies to a Lite decision built from a
  different evidence set.

The MCP tool *named* ``tce.predict_behavior`` is a thin HTTP client and is out of scope — the AST
scan excludes ``services/tce_mcp/``, exactly as p4_design §10.2's correction to critic 1-G10 requires.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_PATTERNS = ("shared/*.py", "services/*.py")


def _git_files(patterns: tuple[str, ...]) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, Path] = {}
    for line in completed.stdout.splitlines():
        rel = line.strip()
        # A file git still has in the index but that has been deleted in the working tree
        # cannot be scanned; skipping it is what lets a deletion land before its commit.
        if not rel or "/build/" in rel or not (REPO_ROOT / rel).is_file():
            continue
        out.setdefault(rel, REPO_ROOT / rel)
    return sorted(out.values())


# Resolved once, at import time — see tests/unit/test_cross_phase_ownership.py for why.
_TRACKED: list[Path] = _git_files(_DEFAULT_PATTERNS)


def tracked_files(*patterns: str) -> list[Path]:
    """Tracked-or-new source files, ``build/`` excluded.  The only way this module reaches disk."""

    if not patterns or tuple(patterns) == _DEFAULT_PATTERNS:
        return list(_TRACKED)
    return _git_files(tuple(patterns))


def _trees() -> Iterator[tuple[str, ast.Module]]:
    for path in tracked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            yield rel, ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue


def _called_names(node: ast.AST) -> Iterator[tuple[str, int]]:
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            yield func.id, inner.lineno
        elif isinstance(func, ast.Attribute):
            yield func.attr, inner.lineno


# --------------------------------------------------------------------------- G1: one callable

# The one module that scores.  Everything that decides a live turn is DISCOVERED, not listed: a
# hardcoded list of turn modules is satisfied by adding a fourth one.
EVALUATION_MODULE = "tce_shared.policy_evaluation"

_MODULE_FOR_PATH = {
    "services/tce_api/tce_api/": "tce_api.",
    "services/tce_lite_api/tce_lite_api/": "tce_lite_api.",
    "shared/tce_shared/": "tce_shared.",
}


def _importable_name(rel: str) -> str | None:
    for prefix, package in _MODULE_FOR_PATH.items():
        if rel.startswith(prefix):
            return package + rel[len(prefix) : -len(".py")].replace("/", ".")
    return None


def deciding_modules() -> dict[str, str]:
    """Every tracked module that calls ``decide(...)``, as ``import name -> repo path``."""

    found: dict[str, str] = {}
    for rel, tree in _trees():
        if rel.endswith("decision_policy.py"):
            continue
        calls_decide = any(name == "decide" for name, _ in _called_names(tree))
        if not calls_decide:
            continue
        name = _importable_name(rel)
        if name is not None:
            found[name] = rel
    return found


def test_the_deployed_route_is_the_evaluated_route() -> None:
    """Resolve ``decide`` from every path that can decide or score, and require one object.

    This is the whole point of P4 expressed as an assertion.  It fails loudly if a backend grows a
    second policy, wraps ``decide`` in a per-backend shim with its own defaults, or falls back to
    ``predict_behavior`` when something is missing — and it fails on a module nobody told it about,
    because the deployed set is read out of the tree rather than typed here.
    """

    from tce_shared import decision_policy

    canonical = decision_policy.decide

    deployed = deciding_modules()
    assert deployed, (
        "No module calls decide(). Either P4's Builders B and C have not landed the turn path yet, "
        "or the policy has no consumer — which is the exact defect P4 exists to fix: "
        "predict_behavior had the right contract and no deployed caller."
    )
    for backend in ("tce_api.", "tce_lite_api."):
        assert any(name.startswith(backend) for name in deployed), (
            f"no module under {backend} calls decide(); that backend still decides some other way. "
            f"Found: {sorted(deployed)}"
        )

    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for name in (EVALUATION_MODULE, *sorted(deployed)):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:  # pragma: no cover - a broken import is another gate
            missing.append(f"{name} is not importable ({exc.name})")
            continue
        found = getattr(module, "decide", None)
        if found is None:
            missing.append(f"{name} calls decide() but does not bind it at module level")
            continue
        resolved[name] = found

    assert not missing, (
        "A decision path does not resolve the policy at all, so the deployed route cannot be the "
        "evaluated route:\n  " + "\n  ".join(missing)
    )

    divergent = {name: fn for name, fn in resolved.items() if fn is not canonical}
    assert not divergent, (
        "These paths resolve a DIFFERENT callable than tce_shared.decision_policy.decide, which is "
        "exactly the deployed-vs-evaluated split P4 exists to close:\n  "
        + "\n  ".join(f"{name} -> {fn!r}" for name, fn in sorted(divergent.items()))
    )


# Where each primitive may still be called from.  Everything else is a decision site reaching around
# the policy.  `services/tce_mcp/` is excluded wholesale: `tce.predict_behavior` there is an HTTP
# client with the same name, not a decision.
PRIMITIVE_CALLERS: dict[str, frozenset[str]] = {
    "predict_behavior": frozenset(
        {
            "shared/tce_shared/decision_policy.py",  # the kNN stage of decide()
            "shared/tce_shared/behavior_fidelity.py",  # its own module
        }
    ),
    # R2c keeps the advisor router where it is: `_advisor_runtime_reason_from_routes` in main.py
    # calls the gateway and returns an `AdvisorContribution`.  That is production of an INPUT, not
    # a decision -- the contribution reaches the answer only as `DecisionRequest.advisor`, and G22
    # (Builder B's tests/unit/test_advisor_contribution.py) is what forbids `advisor_note` from
    # reaching `reason_for_asking`.  What stays banned here is calling the kNN primitive directly.
    "advisor_recommend": frozenset(
        {
            "services/tce_api/tce_api/clone.py",  # its own module
            "services/tce_api/tce_api/main.py",  # R2c: the advisor router
            "services/tce_api/tce_api/policy_store.py",
            "services/tce_lite_api/tce_lite_api/policy_store.py",
        }
    ),
}


def test_no_decision_site_calls_the_primitives_directly() -> None:
    offenders: list[str] = []
    scanned_any = False
    for rel, tree in _trees():
        if rel.startswith("services/tce_mcp/"):
            continue
        scanned_any = True
        for name, lineno in _called_names(tree):
            allowed = PRIMITIVE_CALLERS.get(name)
            if allowed is None or rel in allowed:
                continue
            offenders.append(f"{rel}:{lineno} calls {name}() directly")

    assert scanned_any
    assert not offenders, (
        "A deployed decision site reaches around decide(). Every turn must go through the one "
        "policy, or the thing that was evaluated is not the thing that ran:\n  "
        + "\n  ".join(offenders)
    )


def test_derive_clone_guidance_cannot_supply_a_confidence() -> None:
    """R2a: presentation only.

    It used to return ``(summary, actions, confidence, evidence_strength, conflict_flags)`` — a
    prose generator handing out the two numbers that decided how much the turn was trusted.  Both
    now come from the policy, and the narrowed return type is what makes that structural rather
    than a convention.
    """

    from tce_api.clone import derive_clone_guidance

    annotations = getattr(derive_clone_guidance, "__annotations__", {})
    returns = str(annotations.get("return", ""))
    assert returns, "derive_clone_guidance lost its return annotation"
    assert returns.count(",") == 2, (
        "derive_clone_guidance must narrow to a 3-tuple (summary, recommended_actions, "
        f"conflict_flags): confidence and evidence_strength come from the policy. Found: {returns}"
    )


# --------------------------------------------------------------------------- G8

GOAL_PATH_MODULES: tuple[str, ...] = (
    "shared/tce_shared/autonomy_goals.py",
    "shared/tce_shared/goal_similarity.py",
    "shared/tce_shared/goal_affect.py",
    "shared/tce_shared/goal_cache.py",
)

QUALIFICATION_TOKENS: tuple[str, ...] = (
    "policy_qualifications",
    "lookup_qualification",
    "QualificationState",
    "ExposureState",
)


def test_goal_selection_reads_no_qualification_row() -> None:
    by_path = dict(_trees())
    offenders: list[str] = []
    scanned: list[str] = []
    for rel in GOAL_PATH_MODULES:
        tree = by_path.get(rel)
        assert tree is not None, f"{rel} is missing from the tracked file list"
        scanned.append(rel)
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for token in QUALIFICATION_TOKENS:
            if token in source:
                offenders.append(f"{rel} mentions {token}")
    assert scanned == list(GOAL_PATH_MODULES)
    assert not offenders, (
        "Goal selection is a different decision kind with no adjudicated ground truth (R10). It "
        "may not read a qualification, and it may not be described as personalization:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- G9

POLICY_STORE_MODULES: tuple[str, ...] = (
    "services/tce_api/tce_api/policy_store.py",
    "services/tce_lite_api/tce_lite_api/policy_store.py",
)
POLICY_STORE_IMPORTS: tuple[str, ...] = ("tce_api.policy_store", "tce_lite_api.policy_store")


def test_the_pilot_gate_cannot_grant_permission() -> None:
    by_path = dict(_trees())
    offenders: list[str] = []
    checked = 0
    for rel in POLICY_STORE_MODULES:
        tree = by_path.get(rel)
        assert tree is not None, (
            f"{rel} does not exist. The policy has no loader in that backend, so nothing resolves "
            "a qualification there."
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("behavior_pilot_store"):
                offenders.append(f"{rel}:{node.lineno} imports the pilot store")
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "lookup_qualification":
                checked += 1
                for name, lineno in _called_names(node):
                    if "pilot" in name:
                        offenders.append(f"{rel}:{lineno} lookup_qualification calls {name}()")
    assert checked == len(POLICY_STORE_MODULES), (
        f"lookup_qualification was found in {checked} of {len(POLICY_STORE_MODULES)} policy stores; "
        "the gate would pass vacuously"
    )
    assert not offenders, (
        "The pilot gate governs context arms and can never grant permission (R11):\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- G20


def test_retrieval_version_belongs_to_the_loader_not_the_policy() -> None:
    from tce_shared import decision_policy, policy_evaluation

    assert not hasattr(decision_policy, "RETRIEVAL_VERSION"), (
        "RETRIEVAL_VERSION in decision_policy.py means three loaders claim one version string. A "
        "Full qualification would then silently apply to a Lite decision built from a different "
        "evidence set (p4_design §1.1)."
    )
    assert not hasattr(policy_evaluation, "RETRIEVAL_VERSION")

    versions: dict[str, str] = {}
    missing: list[str] = []
    for name in POLICY_STORE_IMPORTS:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            missing.append(name)
            continue
        value = getattr(module, "RETRIEVAL_VERSION", None)
        if not isinstance(value, str) or not value:
            missing.append(f"{name}.RETRIEVAL_VERSION")
            continue
        versions[name] = value

    assert not missing, "each backend's loader must declare its own RETRIEVAL_VERSION: " + ", ".join(missing)
    assert len(set(versions.values())) == len(versions), (
        "two loaders claim the same retrieval version, so a divergence in the evidence set cannot "
        f"change the bound key: {versions}"
    )


def test_the_request_carries_the_version_the_loader_returned() -> None:
    """``DecisionRequest.retrieval_version`` is data, not a default.

    A field that quietly defaults to ``""`` makes every fingerprint agree while the two paths load
    different evidence, which is the divergence G1 exists to detect.
    """

    from tce_shared.decision_policy import DecisionRequest

    field = DecisionRequest.__dataclass_fields__["retrieval_version"]
    assert field.default is __import__("dataclasses").MISSING, (
        "retrieval_version must be a required field. A default is a path by which a loader that "
        "forgot to report its version still produces a matching fingerprint."
    )
