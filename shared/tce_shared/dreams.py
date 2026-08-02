"""Dream formation: what TCE wants to do when nothing is being asked of it.

A dream is a desired future state, one level above a plan. It is not directly
actionable — it becomes actionable by being decomposed into ordered steps, at which
point it stops being a dream and becomes the root of a plan.

The important design choice is where dreams come from. Deriving them from event
titles is what produced goals like "Investigate and resolve: Historical claude user
input: ...", because an event is evidence that something happened, not something to
achieve. Dreams here are derived from *structured signals about the system's own
situation* — work abandoned, knowledge unindexed, ground repeatedly revisited. Every
dream therefore carries a rationale citing the number that produced it, and a system
with nothing to report dreams nothing at all.

Pure: no I/O. The caller gathers the signals; this decides what they mean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Below these, a signal is noise rather than an aspiration. A single failed directive
# or a handful of unindexed rows is normal operation, not something to want.
MIN_FAILED_DIRECTIVES = 3
MIN_STALLED_GOALS = 5
MIN_UNEMBEDDED_EVENTS = 100
MIN_UNEMBEDDED_RATIO = 0.02
MIN_DOMAIN_ACTIVITY = 25

# A dream fainter than this is not worth acting on; staying idle is the better move.
MIN_PURSUIT_WEIGHT = 0.25

MAX_DREAMS = 5


@dataclass(frozen=True)
class DreamSignals:
    """Observable facts about the system's own situation."""

    failed_unretried_directives: int = 0
    unembedded_events: int = 0
    recurring_domains: Sequence[tuple[str, int]] = ()
    stalled_goals: int = 0
    total_events: int = 0


@dataclass(frozen=True)
class DreamSeed:
    """An aspiration, with the fact that produced it."""

    title: str
    description: str
    rationale: str
    weight: float


def _scaled(value: int, full: int) -> float:
    """Map a count onto 0..1, saturating at ``full`` so no signal can dominate."""
    if full <= 0:
        return 0.0
    return max(0.0, min(1.0, value / full))


def derive_dream_seeds(
    signals: DreamSignals, *, max_dreams: int = MAX_DREAMS
) -> list[DreamSeed]:
    """Turn observations into ranked aspirations. Returns [] when nothing warrants one."""
    seeds: list[DreamSeed] = []

    if signals.failed_unretried_directives >= MIN_FAILED_DIRECTIVES:
        n = signals.failed_unretried_directives
        seeds.append(
            DreamSeed(
                title="Leave no work unfinished",
                description=(
                    "Drive every failed directive to a resolution or an explicit decision "
                    "to abandon it, so nothing is silently dropped."
                ),
                rationale=f"{n} directives failed and were never retried.",
                weight=0.45 + (0.45 * _scaled(n, 25)),
            )
        )

    if (
        signals.unembedded_events >= MIN_UNEMBEDDED_EVENTS
        and signals.total_events > 0
        and (signals.unembedded_events / signals.total_events) >= MIN_UNEMBEDDED_RATIO
    ):
        n = signals.unembedded_events
        seeds.append(
            DreamSeed(
                title="Make the whole history searchable",
                description=(
                    "Index the events that carry no embedding, so retrieval can reach the "
                    "entire record rather than the part that happens to be indexed."
                ),
                rationale=f"{n} of {signals.total_events} events have no embedding.",
                weight=0.35 + (0.45 * _scaled(n, 2000)),
            )
        )

    if signals.stalled_goals >= MIN_STALLED_GOALS:
        n = signals.stalled_goals
        seeds.append(
            DreamSeed(
                title="Stop accumulating abandoned intentions",
                description=(
                    "Resolve or retire goals that were raised and never progressed, so the "
                    "queue reflects intent rather than history."
                ),
                rationale=f"{n} goals are stalled with no progress.",
                weight=0.30 + (0.40 * _scaled(n, 40)),
            )
        )

    for domain, activity in signals.recurring_domains:
        name = str(domain or "").strip()
        if not name or activity < MIN_DOMAIN_ACTIVITY:
            continue
        seeds.append(
            DreamSeed(
                title=f"Move {name} decisively forward",
                description=(
                    f"Advance {name} to a materially better state rather than "
                    "revisiting it in small increments."
                ),
                rationale=f"{activity} recorded events concentrate on {name}.",
                weight=0.30 + (0.50 * _scaled(activity, 400)),
            )
        )

    # Stable ordering: weight first, then title, so equal weights never reshuffle
    # between runs and the same situation always yields the same dream.
    seeds.sort(key=lambda seed: (-round(seed.weight, 6), seed.title))
    return seeds[:max_dreams]


def select_dream_to_pursue(
    dreams: Sequence[DreamSeed], *, has_active_plan: bool
) -> DreamSeed | None:
    """Pick the dream to turn into a plan, or None to stay idle.

    Dreaming fills the gaps between work; it must never interrupt a plan in flight.
    A faint dream is not acted on either — manufacturing work from a weak signal is
    how an autonomous system starts wasting effort on its own noise.
    """
    if has_active_plan:
        return None
    best: DreamSeed | None = None
    for dream in dreams:
        if dream.weight < MIN_PURSUIT_WEIGHT:
            continue
        if best is None or dream.weight > best.weight:
            best = dream
    return best
