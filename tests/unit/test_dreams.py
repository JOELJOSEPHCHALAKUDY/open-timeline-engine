"""Dream formation — the aspirations TCE forms when nothing is being asked of it.

A dream is a desired future state derived from what the system can actually observe
about its own situation: work left unfinished, knowledge left unindexed, ground it
keeps returning to. Dreams are deliberately NOT derived from event titles — that is
what produced "Investigate and resolve: Historical claude user input: ..." — but from
structured signals, so every dream can be traced to a fact and none can be invented.
"""

from __future__ import annotations

from tce_shared.dreams import (
    DreamSeed,
    DreamSignals,
    RecurringAsk,
    cluster_recurring_asks,
    derive_dream_seeds,
    select_dream_to_pursue,
)


def _signals(**kw: object) -> DreamSignals:
    base: dict[str, object] = {
        "failed_unretried_directives": 0,
        "unembedded_events": 0,
        "recurring_domains": [],
        "stalled_goals": 0,
        "total_events": 0,
    }
    base.update(kw)
    return DreamSignals(**base)  # type: ignore[arg-type]


# --- nothing observed means nothing dreamt ---


def test_no_signals_produces_no_dreams() -> None:
    """An idle system with a clean slate must not invent aspirations.

    This is the guard against the failure we already saw once: goals conjured from
    noise, ranked confidently, and pursued.
    """
    assert derive_dream_seeds(_signals()) == []


def test_signals_below_threshold_are_ignored() -> None:
    """One stray failure is not an aspiration."""
    assert derive_dream_seeds(_signals(failed_unretried_directives=1)) == []
    assert derive_dream_seeds(_signals(unembedded_events=5, total_events=10_000)) == []


# --- each dream traces to a fact ---


def test_unfinished_work_becomes_a_dream() -> None:
    dreams = derive_dream_seeds(_signals(failed_unretried_directives=7))
    assert dreams
    assert any("retr" in d.title.lower() or "unfinish" in d.title.lower() for d in dreams)
    assert all(d.rationale for d in dreams), "a dream with no rationale cannot be audited"
    assert any("7" in d.rationale for d in dreams)


def test_unindexed_knowledge_becomes_a_dream() -> None:
    dreams = derive_dream_seeds(_signals(unembedded_events=800, total_events=6000))
    assert dreams
    assert any("search" in (d.title + d.description).lower()
               or "index" in (d.title + d.description).lower() for d in dreams)


def test_recurring_ground_becomes_a_dream() -> None:
    dreams = derive_dream_seeds(_signals(recurring_domains=[("open-witness-engine", 240)]))
    assert dreams
    assert any("open-witness-engine" in d.title or "open-witness-engine" in d.description
               for d in dreams)


def test_a_quiet_domain_is_not_worth_dreaming_about() -> None:
    assert derive_dream_seeds(_signals(recurring_domains=[("scratch", 2)])) == []


def test_repeated_project_asks_create_a_cited_dream() -> None:
    recurring = RecurringAsk(
        summary="fix project-scoped dream generation",
        count=3,
        evidence_event_ids=("event-1", "event-2", "event-3"),
    )
    dreams = derive_dream_seeds(
        _signals(
            project_id="proj_123",
            project_name="open-timeline-engine",
            recurring_asks=[recurring],
        )
    )

    assert dreams[0].project_id == "proj_123"
    assert dreams[0].evidence_event_ids == recurring.evidence_event_ids
    assert "fix project-scoped" in dreams[0].title


def test_uncited_or_single_ask_cannot_create_a_dream() -> None:
    assert derive_dream_seeds(
        _signals(recurring_asks=[RecurringAsk(summary="fix dream generation", count=2)])
    ) == []
    assert derive_dream_seeds(
        _signals(
            recurring_asks=[
                RecurringAsk(summary="fix dream generation", count=1, evidence_event_ids=("event-1",))
            ]
        )
    ) == []


def test_recurring_ask_clustering_is_conservative_and_deterministic() -> None:
    asks = [
        ("event-1", "Please fix project scoped dream generation now"),
        ("event-2", "fix project scoped dream generation please"),
        ("event-3", "update the dashboard colors"),
    ]

    clustered = cluster_recurring_asks(asks)

    assert len(clustered) == 1
    assert clustered[0].count == 2
    assert clustered[0].evidence_event_ids == ("event-1", "event-2")


# --- ordering, determinism, bounds ---


def test_dreams_are_ranked_by_weight() -> None:
    dreams = derive_dream_seeds(
        _signals(failed_unretried_directives=40, unembedded_events=900,
                 total_events=6000, recurring_domains=[("ote", 300)])
    )
    assert len(dreams) >= 2
    assert [d.weight for d in dreams] == sorted((d.weight for d in dreams), reverse=True)


def test_dream_derivation_is_deterministic() -> None:
    sig = _signals(failed_unretried_directives=9, unembedded_events=700, total_events=5000)
    assert derive_dream_seeds(sig) == derive_dream_seeds(sig)


def test_dreams_are_capped() -> None:
    dreams = derive_dream_seeds(
        _signals(failed_unretried_directives=50, unembedded_events=5000, total_events=6000,
                 stalled_goals=30,
                 recurring_domains=[(f"domain-{i}", 500 - i) for i in range(20)]),
        max_dreams=3,
    )
    assert len(dreams) == 3


def test_dream_weights_stay_bounded() -> None:
    dreams = derive_dream_seeds(
        _signals(failed_unretried_directives=10_000, unembedded_events=10_000,
                 total_events=10_000, stalled_goals=10_000)
    )
    assert all(0.0 <= d.weight <= 1.0 for d in dreams)


# --- pursuit ---


def test_no_dream_is_pursued_while_a_plan_is_active() -> None:
    """Dreaming happens in the gaps. It must never interrupt work in progress."""
    dreams = [DreamSeed(title="A", description="a", rationale="r", weight=0.9)]
    assert select_dream_to_pursue(dreams, has_active_plan=True) is None


def test_the_strongest_dream_is_pursued_when_idle() -> None:
    dreams = [
        DreamSeed(title="weak", description="d", rationale="r", weight=0.2),
        DreamSeed(title="strong", description="d", rationale="r", weight=0.8),
    ]
    chosen = select_dream_to_pursue(dreams, has_active_plan=False)
    assert chosen is not None and chosen.title == "strong"


def test_a_faint_dream_is_not_worth_acting_on() -> None:
    """Below the threshold TCE stays idle rather than manufacturing work."""
    dreams = [DreamSeed(title="faint", description="d", rationale="r", weight=0.05)]
    assert select_dream_to_pursue(dreams, has_active_plan=False) is None


def test_nothing_to_pursue_when_there_are_no_dreams() -> None:
    assert select_dream_to_pursue([], has_active_plan=False) is None
