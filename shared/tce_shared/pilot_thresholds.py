"""Every number that can move a P6 pilot verdict, frozen in code.

Same construction as ``policy_thresholds`` and for the same reason: a threshold a caller can
sweep is not a threshold.  ``P6_THRESHOLDS_SHA`` is derived from the **values**, not the file
bytes, so reformatting is a no-op and one changed number changes the digest.  It is a bound key
printed on every report run.

``MIN_CLOSED_PER_ARM = 30`` is deliberately the same number the existing behaviour pilot gate
already uses (``behavior_pilot_min_completed_per_arm``), and it is deliberately **not lowered to
make the report read better**.  The arithmetic that follows from it is printed by the report on
every run: three arms x 30 closed / 0.80 coverage = **113 enrolments per cell**, and the cell
axis is ``(project, decision_family)``, so a second project or a second family needs its own 113.
At the density measured on this corpus — six owner-active days in 196, four in the last 61 —
that is months, not weeks.  The correct response to that number is to say it, not to shrink it.

What this module deliberately does **not** do
---------------------------------------------
``P6_THRESHOLDS_SHA`` is a change *detector*, not a pre-registration mechanism — the same honest
caveat ``policy_thresholds`` already carries.  It can prove two runs used different thresholds.
It cannot prove the thresholds preceded the data, because ``P6_THRESHOLDS_EFFECTIVE_AT`` is an
editable module constant with no external anchor.

This module imports stdlib only.  It must never import ``config`` or ``settings``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

__all__ = [
    "CLAIM_A_MAX_P_VALUE",
    "CLAIM_A_MIN_LOWER_BOUND",
    "CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM",
    "DELIVERY_LOOKBACK_DAYS",
    "MAX_DEVIATION_RATE",
    "MIN_ACTIVE_DAYS",
    "MIN_ADJUDICATED_DREAMS",
    "MIN_CLOSED_PER_ARM",
    "MIN_CLOSE_COVERAGE",
    "MIN_MATCHED_BLOCKS",
    "P6_THRESHOLDS_EFFECTIVE_AT",
    "P6_THRESHOLDS_SHA",
    "P6_THRESHOLDS_VERSION",
    "enrolments_required_per_cell",
    "threshold_values",
]

P6_THRESHOLDS_VERSION = "p6-thresholds-v1"

# --- the pilot window ----------------------------------------------------------------
# Days on which at least one episode was ENROLLED in scope; NOT calendar days.  A calendar
# gate on this corpus expires having measured almost nothing and then reads as a failure of
# the system rather than of the schedule.
MIN_ACTIVE_DAYS = 28

# --- per (project, decision_family, arm) cell -----------------------------------------
MIN_CLOSED_PER_ARM = 30
MIN_CLOSE_COVERAGE = 0.80  # closed / enrolled, per cell
MAX_DEVIATION_RATE = 0.20  # executed_arm != assigned_arm, per cell
MIN_MATCHED_BLOCKS = 30  # complete blocks available to the paired test, per (project, family)

# --- the two claims -------------------------------------------------------------------
CLAIM_A_MIN_LOWER_BOUND = 0.0  # paired_lift_lower_bound, strictly greater than
CLAIM_A_MAX_P_VALUE = 0.05  # McNemar exact, two-sided
# An allocator cannot randomise a human into doing the work himself, so this clause is not
# satisfiable by the election branch.  It is not permanently closed: a genuinely randomised
# human-baseline block satisfies it.  Until then the honest word is REDUCED SUPERVISION.
CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM = True

# --- dreams (P5 composition, see p6_design.md §5) --------------------------------------
MIN_ADJUDICATED_DREAMS = 20  # independent relevance adjudications, per project
DELIVERY_LOOKBACK_DAYS = 30  # "later useful delivery" needs a later; this is how much later

# The pre-registration cutoff.  An episode allocated before this instant was produced by
# traffic that existed when these numbers were chosen, so it is excluded from every claim
# denominator and counted as ``pre_registration`` in the exclusion ledger.
P6_THRESHOLDS_EFFECTIVE_AT = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)


def enrolments_required_per_cell(arms: int = 3) -> int:
    """The number the runbook opens with, computed rather than quoted.

    ``arms`` closed episodes each, divided by the close-coverage floor, rounded up.  Printed by
    the report on every run so that "four to six weeks" is never mistaken for "enough".
    """

    needed = arms * MIN_CLOSED_PER_ARM
    return -(-needed * 100 // int(MIN_CLOSE_COVERAGE * 100))


def threshold_values() -> dict[str, object]:
    """The digest input: every constant that can move a verdict, by name."""

    return {
        "P6_THRESHOLDS_VERSION": P6_THRESHOLDS_VERSION,
        "MIN_ACTIVE_DAYS": MIN_ACTIVE_DAYS,
        "MIN_CLOSED_PER_ARM": MIN_CLOSED_PER_ARM,
        "MIN_CLOSE_COVERAGE": MIN_CLOSE_COVERAGE,
        "MAX_DEVIATION_RATE": MAX_DEVIATION_RATE,
        "MIN_MATCHED_BLOCKS": MIN_MATCHED_BLOCKS,
        "CLAIM_A_MIN_LOWER_BOUND": CLAIM_A_MIN_LOWER_BOUND,
        "CLAIM_A_MAX_P_VALUE": CLAIM_A_MAX_P_VALUE,
        "CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM": CLAIM_B_REQUIRES_RANDOMIZED_HUMAN_ARM,
        "MIN_ADJUDICATED_DREAMS": MIN_ADJUDICATED_DREAMS,
        "DELIVERY_LOOKBACK_DAYS": DELIVERY_LOOKBACK_DAYS,
        "P6_THRESHOLDS_EFFECTIVE_AT": P6_THRESHOLDS_EFFECTIVE_AT.isoformat(),
    }


P6_THRESHOLDS_SHA: str = hashlib.sha256(
    json.dumps(threshold_values(), sort_keys=True, default=str).encode("utf-8")
).hexdigest()[:32]
