"""Every number that can move a P4 qualification verdict, frozen in code.

Three groups live here, and each is here for a reason that was measured rather than
preferred:

* the **qualification gate** thresholds — what a ``(project, decision_family)`` pair has to
  clear before it is allowed to expose a personalized selection;
* the **evaluation-harness parameters** — ``HOLDOUT_RATIO``, ``MIN_TRAIN``,
  ``MIN_CONFIDENCE``, ``MAX_CASES``.  They were caller keyword arguments, which meant a
  caller could sweep the split until a run passed and the qualification record could not
  tell;
* the **abstention floors** — ``SIMILARITY_FLOOR``, ``OOD_OVERLAP_FLOOR``,
  ``CONFLICT_SHARE_RATIO``, ``MIN_AGREEMENT_SHARE``, ``MIN_EFFECTIVE_SAMPLE``.  An earlier
  draft made these ``TCE_``-prefixed environment settings and did not bind them into the
  qualification record, which is a working post-hoc tuning path: sweep the OOD floor until
  coverage clears, write a QUALIFIED record with an unchanged threshold digest, revert the
  environment in production, and nothing downstream can detect it.

``THRESHOLDS_SHA`` is derived from the **constants themselves**, not from the file bytes, so
it is stable under reformatting and changes the instant any number changes.  It is a bound
key on every qualification record.

What this module deliberately does **not** do
---------------------------------------------
``THRESHOLDS_SHA`` is a change *detector*, not a pre-registration mechanism.  It can prove
that two runs used different thresholds.  It cannot prove that the thresholds preceded the
data, because ``THRESHOLDS_EFFECTIVE_AT`` below is an editable module constant with no
external anchor.  What it buys is that admitting pre-registration traffic requires an
explicit, visible edit to a constant whose digest is checked, rather than happening silently.
A cryptographically anchored registration (a signed commit timestamp checked in CI, or a
server-clock ``threshold_registrations`` row) is the next step and is named as out of scope.

This module imports stdlib only.  It must never import ``config`` or ``settings``: a
threshold a caller can move is not a threshold.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

__all__ = [
    "CONFLICT_SHARE_RATIO",
    "DEMOTION_PRECISION_LOWER_BOUND",
    "DEMOTION_WINDOW_CASES",
    "EVIDENCE_DRIFT_MAX_GROWTH",
    "HOLDOUT_RATIO",
    "MAX_CASES",
    "MAX_DUPLICATE_CONTEXT_RATIO",
    "MAX_NEIGHBOURS",
    "MIN_ADJUDICATED",
    "MIN_AGREEMENT_SHARE",
    "MIN_CONFIDENCE",
    "MIN_COVERAGE",
    "MIN_DISTINCT_EPISODES",
    "MIN_EFFECTIVE_SAMPLE",
    "MIN_PRECISION_LOWER_BOUND",
    "MIN_TRAIN",
    "OOD_OVERLAP_FLOOR",
    "PAIRED_LIFT_MAX_P_VALUE",
    "PAIRED_LIFT_MIN_LOWER_BOUND",
    "QUALIFICATION_TTL_DAYS",
    "SIMILARITY_FLOOR",
    "THRESHOLDS_EFFECTIVE_AT",
    "THRESHOLDS_SHA",
    "THRESHOLDS_VERSION",
    "UNCERTAINTY_METHOD",
    "threshold_values",
]

THRESHOLDS_VERSION = "p4-thresholds-v1"

# --- qualification gate --------------------------------------------------------------
MIN_ADJUDICATED = 100
MIN_PRECISION_LOWER_BOUND = 0.90  # Wilson 95% lower bound on non-abstained precision
MIN_COVERAGE = 0.30  # non-abstained / adjudicated
MIN_DISTINCT_EPISODES = 20
MAX_DUPLICATE_CONTEXT_RATIO = 0.25
PAIRED_LIFT_MIN_LOWER_BOUND = 0.0  # strictly greater than, against ALL THREE baselines
PAIRED_LIFT_MAX_P_VALUE = 0.05  # McNemar exact, two-sided
QUALIFICATION_TTL_DAYS = 90
DEMOTION_WINDOW_CASES = 50
DEMOTION_PRECISION_LOWER_BOUND = 0.85
EVIDENCE_DRIFT_MAX_GROWTH = 0.50
UNCERTAINTY_METHOD = "wilson_95"

# The pre-registration cutoff.  A case frozen before this instant was produced by traffic
# that existed when these numbers were chosen, so it is excluded from every qualification
# denominator and counted as ``cases_excluded_pre_registration``.  On the corpus this was
# written against that excludes every stored shadow row, and the honest first report reads
# ``adjudicated 0/100``.
THRESHOLDS_EFFECTIVE_AT = datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC)

# --- evaluation-harness parameters: were sweepable caller kwargs ---------------------
HOLDOUT_RATIO = 0.20
MIN_TRAIN = 5
MIN_CONFIDENCE = 0.55
MAX_CASES = 500

# --- abstention policy: were environment settings ------------------------------------
SIMILARITY_FLOOR = 0.30
# Measured, not chosen by taste.  Raw topical Jaccard between a query and four evidence
# rows, with the real tokenizer:
#   0.0667  "force push to prod to fix the stripe webhook" vs "ssh into the box and restart nginx"
#   0.1333  ... vs "i keep meaning to rewrite the scheduler in rust"
#   0.3077  ... vs "deploy the payment webhook fix to production now"
#   0.6667  ... vs "the stripe webhook is failing, do i force push the fix to prod"
# 0.20 sits in the gap.  0.10 admits the scheduler row; 0.30 rejects a genuine paraphrase.
OOD_OVERLAP_FLOOR = 0.20
CONFLICT_SHARE_RATIO = 0.80
MIN_AGREEMENT_SHARE = 0.60
# Kish effective sample size for ``n`` equal weights is exactly ``n`` and strictly less for
# any variation, so ``>= 2.0`` silently means "three or more" and contradicts the sibling
# clause ``above_floor_count >= 2``.  Two genuine neighbours weighted [0.473947, 0.4725]
# give ESS 1.999995 — the deficit is ~4.7e-6, not machine epsilon, so ``2.0 - 1e-9`` does
# not rescue it either.  1.5 admits any pair whose weights are within roughly 3.7:1 and
# excludes n=1 (ESS exactly 1.0).
MIN_EFFECTIVE_SAMPLE = 1.5
MAX_NEIGHBOURS = 12


def threshold_values() -> dict[str, object]:
    """The digest input: every constant that can move a verdict, by name.

    Derived from the values, never from the file bytes, so reformatting is a no-op and a
    single changed number changes ``THRESHOLDS_SHA``.
    """

    return {
        "THRESHOLDS_VERSION": THRESHOLDS_VERSION,
        "MIN_ADJUDICATED": MIN_ADJUDICATED,
        "MIN_PRECISION_LOWER_BOUND": MIN_PRECISION_LOWER_BOUND,
        "MIN_COVERAGE": MIN_COVERAGE,
        "MIN_DISTINCT_EPISODES": MIN_DISTINCT_EPISODES,
        "MAX_DUPLICATE_CONTEXT_RATIO": MAX_DUPLICATE_CONTEXT_RATIO,
        "PAIRED_LIFT_MIN_LOWER_BOUND": PAIRED_LIFT_MIN_LOWER_BOUND,
        "PAIRED_LIFT_MAX_P_VALUE": PAIRED_LIFT_MAX_P_VALUE,
        "QUALIFICATION_TTL_DAYS": QUALIFICATION_TTL_DAYS,
        "DEMOTION_WINDOW_CASES": DEMOTION_WINDOW_CASES,
        "DEMOTION_PRECISION_LOWER_BOUND": DEMOTION_PRECISION_LOWER_BOUND,
        "EVIDENCE_DRIFT_MAX_GROWTH": EVIDENCE_DRIFT_MAX_GROWTH,
        "UNCERTAINTY_METHOD": UNCERTAINTY_METHOD,
        "THRESHOLDS_EFFECTIVE_AT": THRESHOLDS_EFFECTIVE_AT.isoformat(),
        "HOLDOUT_RATIO": HOLDOUT_RATIO,
        "MIN_TRAIN": MIN_TRAIN,
        "MIN_CONFIDENCE": MIN_CONFIDENCE,
        "MAX_CASES": MAX_CASES,
        "SIMILARITY_FLOOR": SIMILARITY_FLOOR,
        "OOD_OVERLAP_FLOOR": OOD_OVERLAP_FLOOR,
        "CONFLICT_SHARE_RATIO": CONFLICT_SHARE_RATIO,
        "MIN_AGREEMENT_SHARE": MIN_AGREEMENT_SHARE,
        "MIN_EFFECTIVE_SAMPLE": MIN_EFFECTIVE_SAMPLE,
        "MAX_NEIGHBOURS": MAX_NEIGHBOURS,
    }


THRESHOLDS_SHA: str = hashlib.sha256(
    json.dumps(threshold_values(), sort_keys=True, default=str).encode("utf-8")
).hexdigest()[:32]
