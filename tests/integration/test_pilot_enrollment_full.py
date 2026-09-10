"""G13 and G15 — the six P6 routes exist on both backends before the runbook claims them.

**G13** — Full and Lite expose the same wire.  ``tests/unit/test_openapi_parity.py`` and the CI
``integration-migration`` job compare the two *documents*; the documents agree with each other
today only because neither carries a single P6 path.  That is a parity check passing on an empty
set.  This file compares the two **applications**, so the gate goes live the moment Builder B
lands a route Builder C has not — which is the failure the document comparison cannot see until
someone regenerates the documents.

**G15** — every P6 corpus test fails loudly rather than vacuously without a database.
``_database_url`` below is copied verbatim from ``tests/integration/test_policy_parity.py``:
without ``TCE_DATABASE_URL`` the corpus arm **fails**, it does not skip.  Measured on the
precedent: bare -> 5 failed / 6 passed; with the variable -> 11 passed.  A skip here would mean a
green CI run that never touched the pilot tables, which is the same class of error as
``safety_passed=True`` on zero rows.

The enrolment *semantics* — a frozen arm, a repeat that returns the original, an executor that
cannot close — are asserted against Lite in ``test_pilot_enrollment_lite.py``, where a real store
runs against a real SQLite file with no external service.  This file asserts the things that are
only true of Full, plus the parity that is only visible with both apps in one process.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.integration

# The six routes of §6.3, with their path parameters normalised away: Full and Lite are free to
# name the parameter differently, and are not free to serve a different set of paths.
PILOT_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/pilot/episodes"),
    ("POST", "/v1/pilot/episodes/{}/observations"),
    ("POST", "/v1/pilot/episodes/{}/close"),
    ("GET", "/v1/pilot/episodes"),
    ("GET", "/v1/pilot/report"),
    ("POST", "/v1/dreams/{}/adjudicate"),
)

_ABSENT = (
    "The P6 routes are not served by {backend}. This gate is LIVE: it fails until Builders B and C "
    "land the six routes of p6_design §6.3. Missing: {missing}"
)


def _normalise(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _served(app: Any) -> set[tuple[str, str]]:
    """``(method, normalised path)`` for everything the application actually serves."""

    out: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not methods:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.add((method, _normalise(path)))
    return out


def _pilot_subset(app: Any) -> set[tuple[str, str]]:
    return {
        (method, path)
        for method, path in _served(app)
        if path.startswith("/v1/pilot/") or path.endswith("/adjudicate")
    }


# --------------------------------------------------------------------------------------
# G15 — the fail-loud assertion, copied verbatim from test_policy_parity.py
# --------------------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("TCE_DATABASE_URL", "")
    assert url, (
        "TCE_DATABASE_URL is unset. The Y1 corpus arm and the promotion-gate arm both read the live "
        "corpus; running them against nothing would pass vacuously."
    )
    return url.replace("postgresql+psycopg://", "postgresql://")


def _fetch(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    import psycopg

    with psycopg.connect(_database_url()) as connection:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        columns = [description[0] for description in (cursor.description or [])]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


# --------------------------------------------------------------------------------------
# G13 — both applications, one wire
# --------------------------------------------------------------------------------------


def test_routes_are_served() -> None:
    """G13.  All six paths exist on Full, and the enrolment route is off by default."""

    from tce_api.main import app as full_app

    served = _pilot_subset(full_app)
    missing = sorted(set(PILOT_ROUTES) - served)
    assert not missing, _ABSENT.format(backend="tce_api (Full)", missing=missing)


def test_full_and_lite_serve_the_same_pilot_wire() -> None:
    """G13's live half. A route on one backend and not the other is a silent fork of the pilot."""

    from tce_api.main import app as full_app
    from tce_lite_api.main import app as lite_app

    full = _pilot_subset(full_app)
    lite = _pilot_subset(lite_app)
    assert full and lite, _ABSENT.format(backend="one of the two backends", missing=sorted(set(PILOT_ROUTES) - (full | lite)))
    assert full == lite, (
        "Full and Lite disagree about the P6 wire.\n"
        f"  only on Full: {sorted(full - lite)}\n"
        f"  only on Lite: {sorted(lite - full)}"
    )


def test_the_enrollment_route_is_off_until_it_is_switched_on() -> None:
    """§0.5 — ``pilot_enrollment_enabled`` defaults to False and its reader returns 404.

    A pilot that enrols by default enrols the turns of anyone who upgrades, which is randomising
    people who never agreed to be in an experiment.
    """

    from fastapi.testclient import TestClient
    from tce_api.config import get_settings
    from tce_api.db import get_db
    from tce_api.main import app

    settings = get_settings()
    if not hasattr(settings, "pilot_enrollment_enabled"):
        pytest.fail("tce_api.config has no pilot_enrollment_enabled setting; see p6_design §0.5 (Builder B)")
    assert settings.pilot_enrollment_enabled is False, "the enrolment switch must default to off"

    token = next(iter(settings.token_set))
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": "codex-executor",
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": "pilot-workspace",
        "X-TCE-User": "codex-executor",
    }
    body = {"project_id": "proj_x", "decision_family": "needs_human", "session_id": "sess-1", "objective_text": "fix the scroll"}

    sentinel = object()

    def fake_db() -> Iterator[object]:
        yield sentinel

    app.dependency_overrides[get_db] = fake_db
    try:
        disabled = TestClient(app).post("/v1/pilot/episodes", json=body, headers=headers)
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert disabled.status_code == 404, f"the enrolment route answered {disabled.status_code} while the setting was off"


# --------------------------------------------------------------------------------------
# G15 — the corpus arm, which must fail rather than skip
# --------------------------------------------------------------------------------------


def test_the_pilot_tables_exist_in_the_live_corpus() -> None:
    """G15.  Reads the live database; without TCE_DATABASE_URL this FAILS, it does not skip.

    It asserts the tables and not a row count: P6 ships instrumentation, and the honest state of
    the live corpus on the day it lands is *five empty tables*.
    """

    rows = _fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (["pilot_strata", "pilot_episodes", "pilot_episode_closes", "pilot_episode_observations", "dream_relevance_adjudications"],),
    )
    present = {str(row["table_name"]) for row in rows}
    missing = sorted({"pilot_strata", "pilot_episodes", "pilot_episode_closes", "pilot_episode_observations", "dream_relevance_adjudications"} - present)
    assert not missing, (
        f"alembic 20260909_0043 is not applied to this database; missing {missing}. The report is "
        "required to exit 2 in this state, and it must never exit 0 with a zero."
    )


def test_no_episode_carries_an_arm_it_was_not_allocated() -> None:
    """The live invariant behind G2, asserted against whatever the corpus holds — including zero.

    ``arm_class`` is derived from ``arm_id`` by a pure function, so a row where the two disagree
    is a row somebody wrote by hand.
    """

    from tce_shared.pilot_enrollment import ARM_CLASS, PilotArm

    _database_url()  # G15 first: no database is a loud failure, never a mis-attributed one.
    try:
        rows = _fetch("SELECT id, arm_id, arm_class, allocation_kind, slot FROM pilot_episodes")
    except Exception as error:  # noqa: BLE001 - the cause is one line long and belongs in the message
        pytest.fail(f"pilot_episodes could not be read: {error}. Apply alembic 20260909_0043 to this database.")
    offences: list[str] = []
    for row in rows:
        arm = str(row["arm_id"])
        if arm not in {member.value for member in PilotArm}:
            offences.append(f"{row['id']}: arm_id={arm!r} is not in the arm set")
            continue
        if str(row["arm_class"]) != ARM_CLASS[PilotArm(arm)]:
            offences.append(f"{row['id']}: arm_class={row['arm_class']!r} disagrees with arm_id={arm!r}")
        if str(row["allocation_kind"]) == "elected" and int(row["slot"]) != -1:
            offences.append(f"{row['id']}: an elected arm consumed randomised slot {row['slot']}")
    assert not offences, offences
