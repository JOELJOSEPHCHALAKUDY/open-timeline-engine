"""G2, G4, G15, G16, G17 and C10 — the decision may not be invented, laundered, or self-reported.

Five separate routes by which a number or a sentence that nobody earned became "what the owner would
do".  Each one is a literal or a shape, so each one is a scan rather than a promise:

* **G2** — ``clone.py::_fallback_response`` returns ``"Proceed with safest approach"`` whenever the
  advisor gateway raises.  That is a decision with no evidence behind it, rendered in the same slot
  and with the same ``decision_source`` as a decision with evidence.  D4 makes abstention the failure
  mode; this asserts the fabrication is gone rather than renamed.
* **G4 / G-X6 (Y8a)** — ``behavior_fidelity._similarity`` read ``evidence.get("confidence", 0.5)``
  and paid it ``0.05`` of a score that then became ``evidence_strength``.  The column it reads is
  written from the advisor's own number.  A model's self-report may not re-enter as evidence one hop
  later, so the literal is banned inside the scoring functions by AST rather than by review.
* **G15 (Y6)** — no calibrated score ships.  An in-sample isotonic fit on a corpus with zero
  adjudicated cases is a number that looks like a probability and is not one.  The identifier stays
  absent.
* **G16** — ``advice_visible=bool(clone_payload)`` is ``True`` at all six producing sites, because
  the payload is an eight-key dict literal.  The column therefore never measured the property the
  promotion gate reads it for.  ``bool(<payload>)`` is banned and the real question — did the text
  the human read already name one of the options — needs a named producer.
* **G17** — ``"No strong prior found"`` and ``"Cold start mode"`` are the two strings that used to
  make the *lowest*-evidence turn produce the *most* confident text.  Builder A deleted both reads
  in ``takeover.py``; this stays red until the producers go too.
* **C10** — ``CloneAdviceResponse.evidence_observations`` survives (deleting it silently pins
  ``semantic_ratio`` to 0.0 in both backends and breaks a published wire schema).  What must not
  survive is it being rendered as the evidence for a choice: the decision evidence is
  ``DecisionResult.evidence_observation_ids``, a different field with a different name.

Every scan takes its file list from git.  ``services/tce_api/build/lib/tce_api/clone.py`` contains
``Proceed with safest approach`` today and will contain it forever — see G-X8.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterator
from pathlib import Path

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


def _sources() -> Iterator[tuple[str, str]]:
    for path in tracked_files():
        try:
            yield path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover
            continue


def _trees() -> Iterator[tuple[str, ast.Module]]:
    for rel, text in _sources():
        try:
            yield rel, ast.parse(text)
        except SyntaxError:  # pragma: no cover - a broken parse is another gate's failure
            continue


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """``id()`` of every Constant that is a module, class or function docstring.

    The property these gates enforce is *cannot be emitted*, so a docstring that explains why a
    fabricated sentence was deleted is not a violation — it is the reason the deletion survives
    review.  A comment is invisible to the AST for the same reason.  Any other string constant is a
    value the code can return, and that is what is banned.
    """

    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _literal_hits(needle: str) -> list[str]:
    """Every *emittable* string constant containing ``needle``."""

    lowered = needle.casefold()
    hits: list[str] = []
    for rel, tree in _trees():
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            if lowered in node.value.casefold():
                hits.append(f"{rel}:{node.lineno}: {node.value.strip()[:110]!r}")
    return hits


# --------------------------------------------------------------------------- G2

FABRICATED_DECISION_LITERAL = "Proceed with safest approach"


def test_no_fabricated_decision_literal_survives() -> None:
    hits = _literal_hits(FABRICATED_DECISION_LITERAL)
    assert not hits, (
        "A fabricated decision is still reachable. When the advisor gateway raises, the turn must "
        "abstain (D4), not answer.\n" + "\n".join(hits)
    )


def test_fallback_response_is_gone() -> None:
    """Not renamed — gone.  A defined-but-unused fabricator is one call site from returning."""

    hits: list[str] = []
    for rel, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_fallback_response":
                hits.append(f"{rel}:{node.lineno} (definition)")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_fallback_response":
                hits.append(f"{rel}:{node.lineno} (call)")
            elif isinstance(node, ast.Attribute) and node.attr == "_fallback_response":
                hits.append(f"{rel}:{node.lineno} (attribute)")
    assert not hits, "clone.py::_fallback_response must be deleted (Builder B):\n" + "\n".join(hits)


def test_advisor_recommend_abstains_when_the_gateway_raises() -> None:
    """The behavioural half of G2: the replacement must abstain, not answer quietly.

    A gate that only checks the old string is satisfied by a rename.  This calls the real function
    with a gateway that raises and asserts the shape of the answer.
    """

    from tce_api import clone

    advisor_recommend = getattr(clone, "advisor_recommend", None)
    assert advisor_recommend is not None, (
        "services/tce_api/tce_api/clone.py::advisor_recommend does not exist yet. "
        "P4 §2 R2b assigns the advisor_reason -> advisor_recommend rename to Builder B."
    )

    def _raises(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("gateway down")

    contribution = advisor_recommend(gateway=_raises, prompt="x", model_id="m", runtime_version="r")
    assert contribution.abstained is True
    assert contribution.recommended_option is None
    assert contribution.parse_state == "call_failed"


# --------------------------------------------------------------------------- G4 / G-X6 (Y8a)

SELF_REPORT_LITERAL = "confidence"

# Every function whose output can move a score, an abstention or an OOD verdict.
SCORING_FUNCTIONS: frozenset[str] = frozenset(
    {"_similarity", "_topical_overlap", "predict_behavior", "decide", "_effective_sample_size"}
)
SCORING_PREFIXES: tuple[str, ...] = ("_adequacy", "_ood", "_conflict")

SCORING_MODULES: tuple[str, ...] = (
    "shared/tce_shared/behavior_fidelity.py",
    "shared/tce_shared/decision_policy.py",
)


def _is_scoring_function(name: str) -> bool:
    return name in SCORING_FUNCTIONS or name.startswith(SCORING_PREFIXES)


def test_no_self_report_reaches_a_score() -> None:
    scanned: list[str] = []
    hits: list[str] = []
    by_path = {rel: tree for rel, tree in _trees()}

    for rel in SCORING_MODULES:
        tree = by_path.get(rel)
        assert tree is not None, f"{rel} is missing from the tracked file list"
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _is_scoring_function(node.name):
                continue
            scanned.append(f"{rel}::{node.name}")
            for inner in ast.walk(node):
                if isinstance(inner, ast.Subscript) and isinstance(inner.slice, ast.Constant):
                    if inner.slice.value == SELF_REPORT_LITERAL:
                        hits.append(f"{rel}:{inner.lineno} subscript ['{SELF_REPORT_LITERAL}']")
                elif isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "get":
                    if inner.args and isinstance(inner.args[0], ast.Constant) and inner.args[0].value == SELF_REPORT_LITERAL:
                        hits.append(f"{rel}:{inner.lineno} .get('{SELF_REPORT_LITERAL}', ...)")

    assert "shared/tce_shared/behavior_fidelity.py::_similarity" in scanned, (
        "the gate did not find _similarity — it would have passed vacuously"
    )
    assert "shared/tce_shared/decision_policy.py::decide" in scanned, (
        "the gate did not find decide() — it would have passed vacuously"
    )
    assert not hits, (
        "A model's own confidence number reaches a score again (Y8a). "
        "clone_store.py writes the advisor's number into decision_observations.confidence; reading "
        "it back inside a scoring function makes the self-report evidence.\n" + "\n".join(hits)
    )


def test_advisor_contribution_carries_no_confidence_field() -> None:
    """The other end of the same path: nothing for a scorer to read later."""

    from tce_shared.decision_policy import AdvisorContribution

    fields = set(AdvisorContribution.__dataclass_fields__)
    assert not [name for name in fields if "confidence" in name], sorted(fields)


# --------------------------------------------------------------------------- G15 (Y6)


def _identifier_hits(name: str) -> list[str]:
    """Every binding, reference or string use of ``name`` outside a docstring."""

    hits: list[str] = []
    for rel, tree in _trees():
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                hits.append(f"{rel}:{node.lineno} (name)")
            elif isinstance(node, ast.Attribute) and node.attr == name:
                hits.append(f"{rel}:{node.lineno} (attribute)")
            elif isinstance(node, ast.arg) and node.arg == name:
                hits.append(f"{rel}:{node.lineno} (parameter)")
            elif isinstance(node, ast.keyword) and node.arg == name:
                hits.append(f"{rel}:{node.lineno} (keyword)")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == name:
                hits.append(f"{rel}:{node.lineno} (definition)")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == name:
                if id(node) not in docstrings:
                    hits.append(f"{rel}:{node.lineno} (string key)")
    return hits


def test_no_calibrated_score_symbol_exists() -> None:
    hits = _identifier_hits("calibrated_score")
    assert not hits, (
        "Y6: no calibrated score ships. What was specified was an in-sample isotonic fit with no "
        "split, on a corpus with zero adjudicated cases.\n" + "\n".join(hits)
    )


def test_the_uncalibrated_score_says_so() -> None:
    """``policy_score`` is named for what it is, and the wire field that carries it says so."""

    from tce_shared.events import CloneAdviceResponse

    field = CloneAdviceResponse.model_fields["confidence"]
    description = (field.description or "").casefold()
    assert "uncalibrated" in description, (
        "CloneAdviceResponse.confidence is set from policy_score, which is a vote-share heuristic "
        "and not a probability. Its description must say so (p45_shared §8.3). Found: "
        f"{field.description!r}"
    )


# --------------------------------------------------------------------------- G16


def test_advice_visible_is_never_bool_of_a_payload() -> None:
    """``bool(<eight-key dict literal>)`` is ``True`` at every site, so the column measured nothing."""

    hits: list[str] = []
    for rel, tree in _trees():
        for node in ast.walk(tree):
            value: ast.expr | None = None
            if isinstance(node, ast.keyword) and node.arg == "advice_visible":
                value = node.value
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "advice_visible":
                        value = node.value
                    elif isinstance(target, ast.Attribute) and target.attr == "advice_visible":
                        value = node.value
            if value is None:
                continue
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "bool":
                hits.append(f"{rel}:{value.lineno}: advice_visible=bool(...)")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, val in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == "advice_visible":
                    if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "bool":
                        hits.append(f'{rel}:{val.lineno}: "advice_visible": bool(...)')

    assert not hits, (
        "advice_visible is still bool(<payload>). The payload is a dict literal, so this is True at "
        "every site and the promotion gate's exclusion clause reads a constant.\n" + "\n".join(hits)
    )


def test_decision_advice_shown_has_a_producer_that_reads_the_rendered_text() -> None:
    """The replacement question needs a real answer, not a second constant."""

    from tce_shared.decision_policy import advice_names_a_candidate

    assert advice_names_a_candidate("take the minimal fix", ["minimal fix", "broad refactor"]) is True
    assert advice_names_a_candidate("no opinion", ["minimal fix", "broad refactor"]) is False

    callers: set[str] = set()
    for rel, tree in _trees():
        if not rel.startswith(("services/tce_api/", "services/tce_lite_api/")):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name == "advice_names_a_candidate":
                    callers.add(rel.split("/")[1])
    assert callers == {"tce_api", "tce_lite_api"}, (
        "decision_advice_shown must be produced by reading the rendered advice text in BOTH "
        f"backends. Producing backends found: {sorted(callers)}"
    )


def test_the_promotion_gate_admits_on_decision_advice_shown_not_on_advice_visible() -> None:
    """The gate's exclusion clause must read the column that answers its question.

    ``decision_advice_shown`` defaults to **true**, so a prospective row written by a producer
    older than 20260909_0040 is excluded from the denominator.  ``advice_visible`` defaults to
    **false** and is a report diagnostic in both backends.  A gate consumer that filters on
    ``advice_visible`` therefore admits the entire pre-P4 corpus as uncontaminated — the
    conservative default inverted, in the one filter that exists to be conservative.
    """

    consumers = ("scripts/p4_qualification_report.py", "tests/integration/test_policy_parity.py")
    for rel in consumers:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'row.get("decision_advice_shown")' in text, (
            f"{rel}: the promotion gate's contamination filter must read decision_advice_shown"
        )
        assert 'row.get("advice_visible") is True' not in text, (
            f"{rel}: filtering the gate denominator on advice_visible admits every legacy row"
        )


# --------------------------------------------------------------------------- G17

COLD_START_LITERALS = ("No strong prior found", "Cold start mode")


@pytest.mark.parametrize("literal", COLD_START_LITERALS)
def test_cold_start_marker_literals_are_gone(literal: str) -> None:
    hits = _literal_hits(literal)
    assert not hits, (
        f"{literal!r} still exists. Builder A deleted the two reads in takeover.py that turned this "
        "marker into a decisive rewrite; the producers must go too, or the same string comes back "
        "as an input to something else.\n" + "\n".join(hits)
    )


# --------------------------------------------------------------------------- C10


def test_evidence_observations_is_never_rendered_as_decision_evidence() -> None:
    """C10: the field survives; its meaning does not drift back.

    ``CloneAdviceResponse.evidence_observations`` feeds ``semantic_ratio`` ->
    ``adjust_consultative_threshold``: it is retrieval provenance.  The evidence for a *choice* is
    ``DecisionResult.evidence_observation_ids``.  Wiring one into the other is how "the model cited
    these" becomes "the owner decided this before".
    """

    from tce_shared.events import CloneAdviceResponse, PolicyDecisionBlock

    description = (CloneAdviceResponse.model_fields["evidence_observations"].description or "").casefold()
    assert "provenance" in description, (
        "evidence_observations must document itself as retrieval provenance and never as the "
        f"evidence for a choice (p45_shared C10). Found: {description!r}"
    )
    assert "evidence_observation_ids" in PolicyDecisionBlock.model_fields

    hits: list[str] = []
    for rel, tree in _trees():
        for node in ast.walk(tree):
            value: ast.expr | None = None
            if isinstance(node, ast.keyword) and node.arg == "evidence_observation_ids":
                value = node.value
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "evidence_observation_ids":
                        value = node.value
                    elif isinstance(target, ast.Name) and target.id == "evidence_observation_ids":
                        value = node.value
            if value is None:
                continue
            for inner in ast.walk(value):
                if isinstance(inner, ast.Name) and inner.id == "evidence_observations":
                    hits.append(f"{rel}:{value.lineno}")
                elif isinstance(inner, ast.Attribute) and inner.attr == "evidence_observations":
                    hits.append(f"{rel}:{value.lineno}")
                elif isinstance(inner, ast.Constant) and inner.value == "evidence_observations":
                    hits.append(f"{rel}:{value.lineno}")

    assert not hits, (
        "retrieval provenance is being assigned to the decision-evidence field:\n" + "\n".join(hits)
    )
