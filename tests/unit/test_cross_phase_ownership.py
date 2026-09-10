"""G-X7 and G-X8 — one owner per symbol, and no gate that only passes on a dev machine.

**G-X7 (Y9).**  P2, P3, P4 and P5 all edit ``events.py``, ``models.py``, ``main.py``, ``store.py``
and ``config.py``.  The failure mode that costs a day is not a merge conflict — git reports those.
It is two phases declaring the same name in two modules, both green in isolation, and the wrong one
winning an import.  ``ResolvedScope.policy_revision`` (the *scope* policy revision) versus P4's
decision-policy revision is the live example: p45_shared §7.2 renames P4's to
``DECISION_POLICY_REVISION`` for exactly this reason, and this gate is what makes the rename stick
rather than being a comment somebody deletes.

So: for each name on a frozen list, count its **definition** sites and pin the count.  A phase that
adds a definition updates the pin in the same commit, which turns a second declaration into a red
test at the moment it is written instead of a surprise at integration time.

Two of the names — ``policy_revision`` and ``evidence_revision`` — are attribute names carried by
ORM columns and dataclass fields, so a bare count is brittle for a reason that has nothing to do
with ownership (adding a second column to a table that already owns the name is not a second
owner).  Those two are pinned by *owning module set* instead: a new module declaring them fires,
a second field inside a module that already owns the name does not.

**G-X8.**  ``services/*/build/`` holds four stale untracked trees.
``services/tce_api/build/lib/tce_api/clone.py`` still contains ``Proceed with safest approach`` and
``.../clone_prompt.py`` still contains ``you ARE this person`` — the two literals P4's G2 and G3
forbid — and no edit any builder makes will ever remove them, because nothing tracks those files.
A gate that walks the tree with ``Path.rglob`` is therefore red on every developer machine and
green in CI, which is the fastest way to teach a team to ignore a gate.  Every gate module in this
repo takes its file list from git, and this asserts it of each one by name.

The file list itself is ``git ls-files --cached --others --exclude-standard``, not plain
``git ls-files``: P4's own new modules are untracked until the phase commits, and a gate that
cannot see the code it was written to police is worse than no gate.  ``build/`` is gitignored, so
``--exclude-standard`` drops it.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

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
    seen: dict[str, Path] = {}
    for line in completed.stdout.splitlines():
        rel = line.strip()
        # A file git still has in the index but that has been deleted in the working tree
        # cannot be scanned; skipping it is what lets a deletion land before its commit.
        if not rel or "/build/" in rel or not (REPO_ROOT / rel).is_file():
            continue
        seen.setdefault(rel, REPO_ROOT / rel)
    return sorted(seen.values())


# Resolved once, at import time: tests/conftest.py blocks ``subprocess`` inside an unmarked test,
# and a gate that needs a marker to read its own file list is a gate people stop writing.
_TRACKED: list[Path] = _git_files(_DEFAULT_PATTERNS)


def tracked_files(*patterns: str) -> list[Path]:
    """Every tracked-or-new source file matching ``patterns``, with ``build/`` trees excluded.

    This is the helper G-X8 checks for.  Nothing in this repo's gates may reach the filesystem
    another way.
    """

    if not patterns or tuple(patterns) == _DEFAULT_PATTERNS:
        return list(_TRACKED)
    return _git_files(tuple(patterns))


# --------------------------------------------------------------------------- G-X7

# Exact counts, measured against the tree with P4 Builder A landed.  A phase that adds a
# definition of one of these updates the number here, in the same commit that adds it.
PINNED_DEFINITION_COUNTS: dict[str, int] = {
    # P5's DreamProposalEventKind member is renamed OBJECTIVE_BOUND precisely so this stays 1
    # (p45_shared C1).  Two enums with the same member name and the same value in one function
    # body is a bug no reviewer catches.
    "OBJECTIVE_SET": 1,
    # p45_shared C2: P4 owns it, in decision_policy.py, and P5 does not share it.
    "validate_cited_ids": 1,
    # p45_shared §10 predicted 5 after P3 ("P3 adds 3 local redefinitions by design").  P3 has
    # landed and the measured count is 2 — the tree wins over the design document, per the
    # precedence rule in p45_shared §0.
    "canonical_json": 2,
    # Y6 / G-X5.  Zero, and it stays zero: no calibrated score ships.
    "calibrated_score": 0,
    # p45_shared C4: added to scope.py only because BOTH backends call it.
    "observations_scope_predicate": 1,
    # P2's, Lite-only.  Full has no generic CAS bracket to reuse (C9).
    "run_cas_section": 1,
    # The one policy entry point.  A second `decide` is the whole failure P4 exists to end.
    "decide": 1,
    # ---- P5 ----
    # p45_shared C1: P5's kind is OBJECTIVE_BOUND, so OBJECTIVE_SET above stays at 1 and this
    # is the name that must stay unique instead.  The two enums are held in one function body
    # by the pursuit reconciler; different member names AND different values is what lets an
    # AST gate tell them apart.
    "OBJECTIVE_BOUND": 1,
    # The fold, and the one scope predicate the three read routes share (D22).  A second
    # definition of either is a second answer to "what does this proposal say" or "who may
    # read it", which are the two questions P5 exists to make unambiguous.
    "fold_dream_proposal": 1,
    # Two, one per backend: the Full store and its Lite twin, as the owner hint below says.
    "_dream_scope_sql": 2,
}

# Attribute-name families: pinned by owning module, not by count.  See the module docstring.
PINNED_OWNING_MODULES: dict[str, frozenset[str]] = {
    "policy_revision": frozenset(
        {
            "services/tce_api/tce_api/models.py",
            "shared/tce_shared/aspirations.py",
            "shared/tce_shared/charter.py",
            "shared/tce_shared/events.py",
            "shared/tce_shared/scope.py",
            "shared/tce_shared/task_state.py",
            "shared/tce_shared/verification.py",
        }
    ),
    "evidence_revision": frozenset(
        {
            "services/tce_api/tce_api/models.py",
            "shared/tce_shared/aspirations.py",
            "shared/tce_shared/decision_capture.py",
            "shared/tce_shared/decision_policy.py",
            "shared/tce_shared/events.py",
        }
    ),
}

# P5 additions, listed rather than folded in so the reason survives.  ``aspirations.py`` carries
# ``DREAM_POLICY_REVISION`` and a ``policy_revision`` *field* on its projection, and an
# ``evidence_revision`` field on the projection and the citation payload -- fields on a new
# dataclass, not a second implementation of P1's ``evidence_revision()`` function, which stays
# the sole producer in ``decision_capture.py``.  ``events.py`` gains the same two as pydantic
# fields for the same reason.  The distinction this gate exists to protect -- one *owner* per
# symbol -- is unaffected: nothing here computes either value, both are carried.

_OWNER_HINT: dict[str, str] = {
    "OBJECTIVE_SET": "shared/tce_shared/task_state.py (P2). P5 uses OBJECTIVE_BOUND — p45_shared C1.",
    "validate_cited_ids": "shared/tce_shared/decision_policy.py (P4 Builder A) — p45_shared C2.",
    "canonical_json": "shared/tce_shared/task_state.py + execution_transitions.py (P2/P3).",
    "calibrated_score": "nobody. Y6 deleted it; it may not come back — p45_shared §8.3.",
    "observations_scope_predicate": "shared/tce_shared/scope.py (P4 Builder A) — p45_shared C4/C7.",
    "run_cas_section": "services/tce_lite_api/tce_lite_api/task_state_store.py (P2, frozen).",
    "decide": "shared/tce_shared/decision_policy.py (P4 Builder A).",
    "policy_revision": "ResolvedScope's, in scope.py. P4's is DECISION_POLICY_REVISION — p45_shared §7.2.",
    "evidence_revision": "shared/tce_shared/decision_capture.py (P1).",
    "OBJECTIVE_BOUND": "shared/tce_shared/aspirations.py (P5 Builder A) — p45_shared C1.",
    "fold_dream_proposal": "shared/tce_shared/aspirations.py (P5 Builder A).",
    "_dream_scope_sql": (
        "services/tce_api/tce_api/dream_store.py and its Lite twin (P5 Builders B and C) — "
        "one per backend, which is why the pin is 2."
    ),
}


def _definition_sites(names: Iterable[str]) -> dict[str, list[str]]:
    """Module-level and class-level definitions of ``names``, as ``path:lineno`` strings.

    Function-local bindings are deliberately not definitions: ``evidence_revision = ...`` inside a
    loader body is a local variable, not a second declaration of the symbol.
    """

    wanted = set(names)
    found: dict[str, list[str]] = defaultdict(list)

    def scan(body: list[ast.stmt], rel: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name in wanted:
                    found[node.name].append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ClassDef):
                if node.name in wanted:
                    found[node.name].append(f"{rel}:{node.lineno}")
                scan(node.body, rel)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in wanted:
                        found[target.id].append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name) and target.id in wanted:
                    found[target.id].append(f"{rel}:{node.lineno}")

    for path in tracked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - a red compile is another gate's job
            continue
        scan(tree.body, rel)
    return dict(found)


def test_gate_file_list_is_not_empty_and_excludes_build_trees() -> None:
    """The gate's own instrument, checked first: an empty file list passes every scan vacuously."""

    files = tracked_files()
    assert len(files) > 100, f"only {len(files)} source files found — the git file list is broken"
    assert not [p for p in files if "/build/" in p.as_posix()]
    # The P4 modules are untracked until the phase commits; a gate that cannot see them is vacuous.
    names = {p.relative_to(REPO_ROOT).as_posix() for p in files}
    assert "shared/tce_shared/decision_policy.py" in names


def test_no_symbol_is_declared_twice() -> None:
    sites = _definition_sites([*PINNED_DEFINITION_COUNTS, *PINNED_OWNING_MODULES])

    problems: list[str] = []
    for name, expected in sorted(PINNED_DEFINITION_COUNTS.items()):
        actual = sites.get(name, [])
        if len(actual) != expected:
            problems.append(
                f"{name}: pinned {expected} definition(s), found {len(actual)} at {actual or '[]'}. "
                f"Owner: {_OWNER_HINT[name]} "
                "If your phase genuinely adds a definition, update the pin in the same commit."
            )

    for name, expected_modules in sorted(PINNED_OWNING_MODULES.items()):
        actual_modules = {site.rsplit(":", 1)[0] for site in sites.get(name, [])}
        added = actual_modules - expected_modules
        removed = expected_modules - actual_modules
        if added or removed:
            problems.append(
                f"{name}: owning modules changed. new owners {sorted(added)}, "
                f"gone {sorted(removed)}. Owner: {_OWNER_HINT[name]}"
            )

    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------- G-X8

# Every module in this repo that greps or AST-walks the source tree.  Adding a gate means adding it
# here; a gate module absent from this list is a gate nobody checked for the build/ trap.
GATE_MODULES: tuple[str, ...] = (
    "tests/unit/test_cross_phase_ownership.py",
    "tests/unit/test_no_fabricated_decision.py",
    "tests/unit/test_policy_route_identity.py",
    "tests/unit/test_control_path_no_model.py",
)
# tests/integration/test_policy_parity.py is deliberately absent: it drives a real HTTP surface and
# reads the live corpus, and never walks the source tree, so there is nothing for build/ to poison.


def _load_module(rel: str) -> Any:
    path = REPO_ROOT / rel
    spec = importlib.util.spec_from_file_location(f"_gate_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.subprocess  # importing a gate module re-runs its module-level `git ls-files`
@pytest.mark.parametrize("rel", GATE_MODULES)
def test_gates_exclude_untracked_build_trees(rel: str) -> None:
    path = REPO_ROOT / rel
    assert path.exists(), f"{rel} is listed as a gate module but does not exist"
    source = path.read_text(encoding="utf-8")

    uses_git = "git" in source and "ls-files" in source
    filters_build = "build/lib" in source or '"/build/"' in source
    assert uses_git or filters_build, (
        f"{rel} walks the source tree without scoping itself with `git ls-files` and without "
        "filtering build/. services/tce_api/build/lib/tce_api/clone.py still contains "
        "'Proceed with safest approach' and will contain it forever."
    )

    if not uses_git:
        return
    module = _load_module(rel)
    helper = getattr(module, "tracked_files", None)
    assert callable(helper), f"{rel} shells out to git but exposes no tracked_files() helper"
    files = helper()
    assert files, f"{rel}: tracked_files() returned nothing, so every scan in it passes vacuously"
    assert not [p for p in files if "/build/" in Path(p).as_posix()], f"{rel}: build/ leaked into tracked_files()"
