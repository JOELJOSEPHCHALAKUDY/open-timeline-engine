"""P3 exit gates G13a, G13b and G16 (design §0.10, §10.7).

**What is here and what is not, stated plainly.**

``G16`` is implemented below, with one deliberate correction to its wording. §0.10 says to assert
that the literal ``alembic heads`` appears exactly once in ``.github/workflows/ci.yml``. It appears
**zero** times and always did: P2 wrote the assertion as ``alembic -c infra/alembic.ini heads``, so
the literal check fails *after* the work is complete, which is the failure mode this whole priority
exists to avoid. The assertion below matches the real command form. It still does the job it was
specified to do -- catch a second, duplicate head check, and catch its removal -- and it is paired
with a direct check that the revision chain itself has exactly one head, which is the property the
CI step is a proxy for.

``G13a`` (``test_full_cycle_reconciles_by_reading``) and ``G13b``'s first half
(``test_lost_provider_record_pauses``) are **NOT implemented here**, and this file does not pretend
otherwise. Both require the supervisor's ``reconcile`` driving a Lite backend with a fake
``read_provider_record`` -- an integration between ``services/tce_supervisor`` and
``services/tce_lite_api`` that no builder landed, and Lite deliberately never produces
``resolution_source="provider_read"`` at all (see ``tce_lite_api/reconcile.py``: without a runtime
adapter it writes a ``reconcile_pending`` marker and resolves to ``unknown``/``reaper`` rather than
inventing an outcome). Writing a test here that asserted the ``provider_read`` path against Lite
would assert a behaviour that backend does not have.

The parts of G13a/G13b that ARE reachable without the supervisor are already covered, and are named
here so nobody re-derives the map:

* the stale-lease refusal (G13b, second half) --
  ``test_effect_reconcile_lite.py::test_a_stale_lease_cannot_land_an_effect``
* the unresolved/pause path (G13b, first half, minus the provider read) --
  ``test_effect_reconcile_lite.py::test_reaped_irreversible_effect_pauses_instead_of_restarting``
* "exactly one effect row per intent" (G13a's duplicate-effect clause) --
  ``test_effect_reconcile_lite.py::test_effect_open_is_idempotent_on_the_intent``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_VERSIONS_DIR = _REPO_ROOT / "infra" / "alembic" / "versions"

# `alembic ... heads`, allowing the `-c infra/alembic.ini` that the real step uses, but NOT
# matching `upgrade head` / `downgrade <rev>`, which are different commands on adjacent lines.
_HEADS_ASSERTION = re.compile(r"\balembic\b[^\n]*\bheads\b")


def test_ci_has_exactly_one_alembic_head_assertion() -> None:
    """G16. A second head is silent: `alembic upgrade head` fails only at deploy time, and a
    mis-chained P2/P3 revision is exactly how that happens. One check must exist; two means someone
    added a duplicate that can drift out of agreement with the first."""
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    matches = _HEADS_ASSERTION.findall(text)
    assert len(matches) == 1, f"expected exactly one alembic heads assertion, found {len(matches)}: {matches}"


def test_the_migration_chain_has_exactly_one_head() -> None:
    """The property the CI step above is a proxy for, checked directly against the files on disk so
    a mis-chained revision fails here rather than at deploy time. Parses the revision identifiers
    statically; it does not run alembic and does not touch a database."""
    revision_re = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.M)
    down_re = re.compile(r"^down_revision(?::[^=]+)?\s*=\s*(.+)$", re.M)

    down_by_revision: dict[str, str | None] = {}
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision_match = revision_re.search(source)
        assert revision_match is not None, f"{path.name} declares no revision"
        raw_down = down_re.search(source)
        value = raw_down.group(1).strip() if raw_down else "None"
        down_by_revision[revision_match.group(1)] = None if value == "None" else value.strip("\"'")

    parents = {down for down in down_by_revision.values() if down}
    heads = sorted(set(down_by_revision) - parents)
    roots = sorted(rev for rev, down in down_by_revision.items() if down is None)

    assert len(heads) == 1, f"expected exactly one alembic head, found {len(heads)}: {heads}"
    assert len(roots) == 1, f"expected exactly one root revision, found {len(roots)}: {roots}"

    # Every declared down_revision must name a revision that exists, or the chain is broken in a
    # way a head count alone would not reveal.
    dangling = sorted(down for down in parents if down not in down_by_revision)
    assert not dangling, f"down_revision points at revisions that do not exist: {dangling}"
