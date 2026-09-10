"""Z4 — the writer's own confidence is labelled as a self-report where a human reads it.

``decision_observations.confidence`` is supplied by whoever wrote the evidence.  Nothing in this
system scores it, calibrates it or checks it against an outcome; it reaches no gate and no
threshold.  It is still worth carrying — it is auditable provenance — but the two places it is
*rendered* are the markdown projection and the HTML review card, and in both it sits in the
provenance footer beside the eligible/audit-only badge, which **is** a system judgement.  An
unlabelled "Confidence: 0.983" in the one document whose whole purpose is evidence review reads
as the system's assessment of that evidence.

These cases pin the label, not the number.  They are cheap and they are the only thing standing
between this and a revert that looks like a harmless string tidy-up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tce_shared.behavior_projection import build_behavior_projection

_SELF_REPORT = "self-report"


def _row(confidence: float = 0.983) -> dict[str, Any]:
    return {
        "id": "3f1c9d2e-0000-4000-8000-000000000001",
        "ts": datetime(2026, 9, 1, tzinfo=UTC),
        "situation_type": "approval_requested",
        "situation_summary": "approve deleting the scratch build tree",
        "objective_text": "remove the build artifacts without losing source",
        "selected_choice": "confirm",
        "response_reasoning": "the path is scoped and the change is reversible",
        "confidence": confidence,
        "evidence_source": "explicit",
        "memory_class": "preference",
        "lifecycle_status": "active",
        "learning_eligible": True,
    }


def _content(format_name: str, view: str) -> str:
    projection = build_behavior_projection(
        [_row()],
        workspace_id="z4-workspace",
        subject_user_id="z4-subject",
        view=view,
        format_name=format_name,
    )
    return str(projection["content"])


def test_the_markdown_projection_names_the_confidence_as_the_writers_own_claim() -> None:
    content = _content("markdown", "current")

    assert "0.983" in content, "the number must stay: it is auditable provenance, not noise"
    line = next(line for line in content.splitlines() if "0.983" in line)
    assert _SELF_REPORT in line, (
        "the writer-supplied confidence is rendered without saying whose claim it is, so a "
        f"reviewer reads it as the system's assessment: {line!r}"
    )
    # The label has to travel with the number, on the same line, or a reader scanning the
    # provenance list sees a bare score again.
    assert line.index(_SELF_REPORT) < line.index("0.983")


def test_the_html_review_card_names_the_confidence_as_the_writers_own_claim() -> None:
    content = _content("html", "review")

    assert "0.983" in content
    footer = content[content.index('<footer class="provenance">') : content.index("</footer>")]
    assert "0.983" in footer, "the confidence moved out of the provenance footer; re-derive this case"
    assert _SELF_REPORT in footer, (
        "the review card prints a bare 'Confidence: 0.983' beside the eligible/audit-only badge, "
        "which IS a system judgement, in the document whose whole purpose is evidence review"
    )
    assert footer.index(_SELF_REPORT) < footer.index("0.983")
    # The badge row is the system's own verdict and must stay distinguishable from the footer's
    # self-report; if the two ever merged, the label above would be describing the wrong thing.
    badges = content[content.index('<div class="badges">') : content.index("</div>")]
    assert "eligible" in badges and _SELF_REPORT not in badges
