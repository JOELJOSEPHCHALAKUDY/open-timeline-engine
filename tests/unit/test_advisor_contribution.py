"""G22 — the advisor contributes, and it never selects, never scores, and never speaks to the user.

Three separate ways a model's output used to reach a surface it had not earned, each closed by
a different mechanism and each asserted here:

* **It cannot score.** ``AdvisorContribution`` has no confidence field.  That is enforcement by
  type: there is nowhere for a self-report to sit, so it cannot be read back into a similarity
  one hop later the way ``decision_observations.confidence`` was.
* **It cannot select.** ``_advisor_stage`` can move the agreement label and can force an
  abstention.  It cannot set ``selected_option`` and it cannot raise ``policy_score``.  The
  overwrite this replaces took the model's ``decision`` string, put it in ``guidance_summary``,
  replaced ``recommended_actions`` with the model's list, replaced ``confidence`` with the
  model's own number, and then re-derived ``evidence_strength`` from that number.
* **It cannot speak.** ``advisor_note`` is the model's prose and has exactly one reader:
  persistence into ``policy_json``.  ``reason_for_asking`` — the sentence the human actually
  reads when the policy declines — is built by a pure string builder with one branch per
  abstention reason.  Two fields called ``reason_for_asking`` in one call frame is how a
  builder reading two paragraphs wires the model's prose into the one surface that must stay
  model-free, which is why the rename happened; this asserts the rename held.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tce_shared.decision_policy import (
    AdvisorAgreement,
    AdvisorContribution,
    DecisionRequest,
    DecisionStatus,
    decide,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCANNED_PATTERNS = ("services/tce_api/*.py", "services/tce_lite_api/*.py", "shared/*.py")


def _tracked() -> list[Path]:
    """Git is the file list.  ``services/*/build/`` holds stale copies of these same modules."""

    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *_SCANNED_PATTERNS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    seen: dict[str, Path] = {}
    for line in completed.stdout.splitlines():
        rel = line.strip()
        if rel and "/build/" not in rel and (REPO_ROOT / rel).is_file():
            seen.setdefault(rel, REPO_ROOT / rel)
    return sorted(seen.values())


# Resolved once, at import time: the repo's conftest fails any test that spawns a subprocess
# inside the test body, and `git ls-files` is how every gate in this suite excludes the stale
# `services/*/build/` trees that hold pre-P4 copies of these same modules.
_TRACKED: list[Path] = _tracked()


def _trees() -> list[tuple[str, ast.Module]]:
    out: list[tuple[str, ast.Module]] = []
    for path in _TRACKED:
        try:
            out.append((path.relative_to(REPO_ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
    return out


_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _evidence(choice: str, *, index: int, days_ago: int = 5) -> dict[str, object]:
    return {
        "id": f"{index:08d}-0000-4000-8000-000000000000",
        "situation_type": "blocker_encountered",
        "situation_summary": "the stripe webhook is failing in production after a deploy",
        "objective_text": "get the stripe webhook working again",
        "selected_choice": choice,
        "user_response": choice,
        "action_taken": choice,
        "available_choices": ["roll back first", "force push the fix"],
        "evidence_source": "explicit",
        "learning_eligible": True,
        "lifecycle_status": "active",
        "ts": (_NOW - timedelta(days=days_ago)).isoformat(),
        "confidence": 1.0,
    }


def _request(advisor: AdvisorContribution | None) -> DecisionRequest:
    return DecisionRequest(
        decision_family="safety_confirmation",
        situation_type="blocker_encountered",
        situation_summary="the stripe webhook is failing in production after a deploy",
        objective_text="get the stripe webhook working again",
        constraints={},
        context_snapshot={},
        candidate_options=("roll back first", "force push the fix"),
        evidence_rows=tuple(_evidence("roll back first", index=i, days_ago=3 + i) for i in range(1, 5)),
        decision_at=_NOW,
        workspace_id="ws",
        subject_user_id="subject",
        project_id=None,
        episode_key="episode",
        evidence_revision="rev",
        evidence_cutoff_at=None,
        retrieval_version="full-knn-v1",
        model_id="qwen3:8b",
        runtime_version="ollama",
        advisor=advisor,
    )


def _contribution(**overrides: object) -> AdvisorContribution:
    base: dict[str, object] = {
        "recommended_option": "roll back first",
        "abstained": False,
        "abstain_reason": None,
        "evidence_ids": (),
        "conflicting_evidence_ids": (),
        "advisor_note": None,
        "parse_state": "parsed",
        "prompt_sha256": "a" * 64,
        "model_id": "qwen3:8b",
        "runtime_version": "ollama",
    }
    base.update(overrides)
    return AdvisorContribution(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- it cannot score


def test_the_contribution_type_has_nowhere_to_put_a_confidence() -> None:
    names = {field.name for field in fields(AdvisorContribution)}
    assert not [name for name in names if "confidence" in name], sorted(names)
    assert "rationale" not in names


def test_the_advisor_cannot_raise_the_score() -> None:
    """Agreement is recorded; it does not add anything to ``policy_score``."""

    without = decide(_request(None))
    with_agreement = decide(_request(_contribution()))
    assert with_agreement.advisor_agreement is AdvisorAgreement.AGREED
    assert with_agreement.policy_score == without.policy_score


# --------------------------------------------------------------------------- it cannot select


def test_the_advisor_cannot_select_an_option_the_policy_did_not() -> None:
    """Disagreement forces an abstention.  It never swaps the selection."""

    disagreeing = decide(_request(_contribution(recommended_option="force push the fix")))
    assert disagreeing.advisor_agreement is AdvisorAgreement.DISAGREED
    assert disagreeing.status is DecisionStatus.ABSTAINED
    assert disagreeing.selected_option is None


def test_a_failed_advisor_call_abstains_without_consulting_any_setting() -> None:
    """D4 by default rather than behind a knob.

    ``policy_advisor_required_families`` defaults to empty, so a precedence that checked it
    first would mean a dead model is ignored on every family — which is the same as trusting a
    decision made with an input that never arrived.
    """

    for parse_state in ("call_failed", "unparseable", "not_run"):
        result = decide(_request(_contribution(parse_state=parse_state)))
        assert result.advisor_agreement is AdvisorAgreement.UNAVAILABLE, parse_state
        assert result.status is DecisionStatus.ABSTAINED, parse_state
        assert result.selected_option is None, parse_state


# --------------------------------------------------------------------------- it cannot speak


def test_advisor_note_never_reaches_the_sentence_the_human_reads() -> None:
    """``reason_for_asking`` is built from the abstention reason, never from the model."""

    note = "tell me whether the webhook is customer-visible"
    result = decide(_request(_contribution(abstained=True, advisor_note=note)))
    assert result.advisor_note == note
    assert result.reason_for_asking is not None
    assert note not in result.reason_for_asking


def test_advisor_note_is_never_assigned_to_a_response_field() -> None:
    """The static half: no expression containing ``advisor_note`` reaches a rendered field.

    Scanned by AST rather than by review, because the hazard is a builder reading two adjacent
    paragraphs and wiring the obvious-looking string into the obvious-looking slot.
    """

    forbidden_targets = {
        "reason_for_asking",
        "final_response",
        "guidance_summary",
        "clarification_question",
        "response_text",
        "question_text",
    }
    offenders: list[str] = []
    for rel, tree in _trees():
        for node in ast.walk(tree):
            value: ast.expr | None = None
            target_name = ""
            if isinstance(node, ast.keyword) and node.arg in forbidden_targets:
                value, target_name = node.value, str(node.arg)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    name = (
                        target.id
                        if isinstance(target, ast.Name)
                        else target.attr
                        if isinstance(target, ast.Attribute)
                        else ""
                    )
                    if name in forbidden_targets:
                        value, target_name = node.value, name
            if value is None:
                continue
            for inner in ast.walk(value):
                mentions = (
                    (isinstance(inner, ast.Name) and inner.id == "advisor_note")
                    or (isinstance(inner, ast.Attribute) and inner.attr == "advisor_note")
                    or (isinstance(inner, ast.Constant) and inner.value == "advisor_note")
                )
                if mentions:
                    offenders.append(f"{rel}:{value.lineno} -> {target_name}")

    assert not offenders, (
        "The advisor's prose is reaching a rendered surface. reason_for_asking is the one "
        "sentence the human reads when the policy declines, and it is built by a pure string "
        "builder with one branch per abstention reason:\n" + "\n".join(offenders)
    )


def test_advisor_note_has_exactly_one_kind_of_reader() -> None:
    """It is persisted and it is not rendered.

    ``DecisionResult.to_payload`` carries it into ``behavior_shadow_predictions.policy_json``;
    ``block_payload`` — the narrow projection an executor sees over MCP — does not.
    """

    result = decide(_request(_contribution(abstained=True, advisor_note="ask the owner")))
    assert result.to_payload()["advisor_note"] == "ask the owner"
    assert "advisor_note" not in result.block_payload()
