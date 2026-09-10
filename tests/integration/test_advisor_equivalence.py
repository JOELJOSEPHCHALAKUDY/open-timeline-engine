"""Z1/Z2 — the advisor arm of the Y1 equivalence gate, and ABSENT vs UNAVAILABLE end to end.

**Why this file exists.**  ``test_policy_parity.py`` records real turn bodies and replays them,
which is the right shape, but it drives **Lite only** and it compares **one response body per
turn**.  Both gaps were load-bearing.  P4 keyed ``advisor_runtime_used`` on the string
``"advisor_runtime_llm"`` appearing in ``conflict_flags`` and then stopped appending that string,
so on **Full** the read was ``False`` on every turn: a healthy advisor that ran and parsed
cleanly was reported as ``advisor_runtime_unavailable``, forced to ``FAST_PATH``, and counted
into ``advisor_fail_streak`` — which reaches ``advisor_unhealthy`` and escalates the turn to a
human.  A turn that worked before P4 escalated after it, which is exactly what Y1 forbids, and
nothing went red, because only AST-shape tests covered that area.

So this gate compares what the earlier one did not:

* the **Full** backend as well as Lite, and
* ``decision_source``, ``needs_human``, ``advisor_fail_streak`` and ``advice_visible`` across
  **several consecutive turns** — the state that carries across turns is the whole point, since
  the streak is what turns one silent mislabel into an escalation two turns later.

``advice_visible`` is read from the persisted ``behavior_shadow_predictions`` row, not recomputed
here, because it is the column the promotion clause reads.  The turns are driven with the
needs-human threshold pinned high so every turn freezes a row and there is something to read.

**Non-vacuity is asserted, not assumed.**  ``test_full_a_dead_advisor_still_escalates`` drives the
same script with a dead advisor and requires the streak to climb and the safety gate to fire.  A
"healthy advisor never escalates" test that also passes when the advisor is dead proves nothing.

**Z2.**  ABSENT ("never attempted": no cadence, deadline closed, advisor switched off, not
required) and UNAVAILABLE ("attempted and failed": error, timeout, unparseable, missing key) are
different facts and both backends must keep them apart.  They are observable on the wire as
``policy_decision.advisor_agreement``, and there is a test per state per backend below.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_shared.decision_policy import AdvisorContribution, failed_advisor_contribution

pytestmark = pytest.mark.integration

TASK = "review the stripe webhook retry logic"
# Four consecutive turns, none of them a mutating request: an execution-permit pause sets
# `actionable_lifecycle_pause`, and that suppresses `needs_human` outright, which would hide the
# escalation this file exists to catch.
MESSAGES: tuple[str, ...] = (
    "beru take over",
    "explain the retry logic",
    "ok continue",
    "keep going",
)
APP_CONTEXT = {
    # Bound project: without it every turn returns `confirm_required` from the safety gate and
    # no shadow row is frozen, so `advice_visible` would have nothing to be read from.
    "domain": "coding",
    "project_root": "/tmp/tce-advisor-equivalence",
    "repo_remote": "git@example.com:tce/advisor-equivalence.git",
}

HEALTHY_ADVISOR = AdvisorContribution(
    recommended_option=None,
    abstained=False,
    abstain_reason=None,
    evidence_ids=(),
    conflicting_evidence_ids=(),
    advisor_note="the advisor answered and its output parsed",
    parse_state="parsed",
    prompt_sha256="a" * 64,
    model_id="test-advisor",
    runtime_version="test-runtime",
)


def _inference_meta(*, parsed: bool, attempted: bool = True) -> dict[str, Any]:
    return {
        "used_llm": parsed,
        "advisor_attempted": attempted,
        "advisor_parse_state": "parsed" if parsed else "call_failed",
        "selected_provider": "test" if parsed else None,
        "selected_model": "test-advisor" if parsed else None,
        "elapsed_ms": 1,
        "attempt_count": 1 if attempted else 0,
        "attempts": [],
    }


# --------------------------------------------------------------------------------------
# Full backend
# --------------------------------------------------------------------------------------

_FULL_REASON = "Full-backend advisor turns need TCE_DATABASE_URL"
full_only = pytest.mark.skipif(not os.environ.get("TCE_DATABASE_URL"), reason=_FULL_REASON)

# Every Full turn below mints a real ``directive_executions`` row in a throwaway workspace, and
# nothing used to remove it.  Those rows stay ``pending`` forever, and ``startup_reconcile``
# scans ``WHERE state IN ('pending','in_progress') ORDER BY created_at ASC LIMIT
# dispatch_startup_reconcile_batch`` (200) -- so once this file's leavings passed 200, the row
# that ``test_startup_reconcile_full.py`` seeds could never appear in the batch and that test
# failed deterministically, in a file this one never touches.  The workspaces are
# ``advisor-eq-ws-<hex>`` and belong to nobody else, so this file takes them with it.
_ADVISOR_EQ_WORKSPACE_LIKE = "advisor-eq-ws-%"


@pytest.fixture(scope="module", autouse=True)
def _purge_advisor_eq_rows() -> Iterator[None]:
    """Drop this module's throwaway workspaces after it runs (and before, to catch old leavings)."""

    def _purge() -> None:
        url = os.environ.get("TCE_DATABASE_URL", "").strip()
        if not url:
            return
        import psycopg

        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as connection:
            for table in (
                "effect_journal",
                "directive_executions",
                "takeover_sessions",
                "behavior_shadow_predictions",
            ):
                try:
                    connection.execute(
                        f"DELETE FROM {table} WHERE workspace_id LIKE %s",  # noqa: S608 - fixed literals
                        (_ADVISOR_EQ_WORKSPACE_LIKE,),
                    )
                except Exception:  # pragma: no cover - a table this deployment does not have
                    connection.rollback()
            connection.commit()

    _purge()
    yield
    _purge()



@pytest.fixture()
def _clone_advisor_mode() -> Iterator[None]:
    """Put the runtime mode in ``clone_advisor``, which is what makes the advisor reachable.

    This is ambient database state, not configuration: ``mode.get_runtime_mode`` reads the
    ``runtime_mode`` row of ``runtime_settings`` and only falls back to
    ``Settings.default_operation_mode`` when that row does not exist yet, so pinning the setting
    is not enough once anything in the run has materialised the row.  On a developer's database
    the row has said ``clone_advisor`` since somebody ran ``PUT /v1/runtime/mode`` months ago.
    On a database that CI creates, migrates and hands over, it says ``timeline_only``, and then
    ``clone_advice`` raises ``409 clone_mode_disabled`` on **every** turn -- before
    ``_advisor_runtime_reason_from_routes`` is ever called, so the healthy advisor these tests
    monkeypatch in is never consulted at all.  What the three Full arms then measure is the
    409: ``fast_path_reason='advisor_exception'``, a climbing ``advisor_fail_streak``,
    ``advisor_unhealthy`` by turn 2, and ``advice_visible`` False on every frozen row.

    So the mode is seeded here through ``set_runtime_mode`` -- the real writer behind that
    route -- and restored to whatever it was, on both kinds of database.
    """

    url = os.environ.get("TCE_DATABASE_URL", "").strip()
    if not url:  # the arms that need it are already skipped by `full_only`
        yield
        return

    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from tce_api.mode import get_runtime_mode, set_runtime_mode
    from tce_shared.events import OperationMode

    engine = sa.create_engine(url, future=True)
    try:
        with Session(engine) as db:
            previous = get_runtime_mode(db).mode
            set_runtime_mode(db, OperationMode.CLONE_ADVISOR, "advisor-equivalence-test")
        yield
    finally:
        with Session(engine) as db:
            set_runtime_mode(db, previous, "advisor-equivalence-test-restore")
        engine.dispose()


@pytest.fixture()
def full_app(monkeypatch: pytest.MonkeyPatch, _clone_advisor_mode: None) -> Iterator[Any]:
    import tce_api.bundle as bundle
    import tce_api.main as full_main
    from tce_api.config import get_settings

    # Harness stub, unrelated to the advisor. `tce_api/graph.py`'s entity query is
    # `SELECT DISTINCT en.id, ... ORDER BY en.updated_at`, which Postgres rejects outright, and
    # its bare `except Exception: return empty` leaves the pooled session in an aborted
    # transaction -- so `finish_pg_deadline`'s RELEASE SAVEPOINT raises and the turn 500s
    # whenever search returns a citation. Pre-existing and out of this file's scope; stubbed so
    # these assertions measure the advisor path rather than that.
    monkeypatch.setattr(
        bundle,
        "graph_snapshot_for_events",
        lambda **_kwargs: {"entities": [], "relationships": [], "facts": []},
    )

    settings = get_settings()
    keys = (
        "charter_enforcement_enabled",
        "advisor_router_v2_enabled",
        "clone_reasoning_enabled",
        "takeover_advisor_runtime_cooloff_turns",
        "takeover_advisor_fail_streak_escalate",
        "takeover_needs_human_threshold_cold",
        "takeover_needs_human_threshold_hot",
    )
    original = {key: getattr(settings, key) for key in keys}
    settings.charter_enforcement_enabled = False
    # The env this suite runs in pins the escalation streak at 999, which would make "the
    # streak reaches the limit" untestable. Pin it to the code default instead of trusting it.
    settings.takeover_advisor_fail_streak_escalate = 2
    # A runtime cool-off suppresses the advisor call for N turns after one failure, so the
    # streak stops climbing and the defect hides. Disabled here: this file is measuring the
    # streak, not the cool-off.
    settings.takeover_advisor_runtime_cooloff_turns = 0
    # Thresholds are left at their configured values here. `advice_visible` needs a frozen
    # shadow row and a frozen row needs an escalation, so the one test that reads that column
    # raises them itself -- and says so, rather than having every test in the file quietly run
    # under a forced escalation that would mask `decision_source`.
    try:
        yield full_main
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


def _full_turns(
    full_main: Any,
    *,
    routes: Callable[..., tuple[AdvisorContribution | None, dict[str, Any]]] | None,
    monkeypatch: pytest.MonkeyPatch,
    messages: tuple[str, ...] = MESSAGES,
) -> tuple[list[dict[str, Any]], str]:
    """Drive `messages` through the real Full `/v1/takeover/step` and return one record a turn."""

    if routes is not None:
        monkeypatch.setattr(full_main, "_advisor_runtime_reason_from_routes", routes)
    tag = uuid.uuid4().hex[:8]
    session_id = f"advisor-eq-{tag}"
    headers = {
        "Authorization": "Bearer local-dev-token",
        "X-TCE-Consumer": "advisor-eq",
        "X-TCE-Role": "user",
        # A fresh workspace and subject per run: these assertions are about one session's own
        # turns, and a warm workspace drags in another run's evidence.
        "X-TCE-Workspace": f"advisor-eq-ws-{tag}",
        "X-TCE-User": f"advisor-eq-user-{tag}",
        "X-TCE-Behavior-Subject": f"advisor-eq-user-{tag}",
    }
    records: list[dict[str, Any]] = []
    with TestClient(full_main.app) as client:
        for index, message in enumerate(messages):
            response = client.post(
                "/v1/takeover/step",
                json={
                    "message": message,
                    "session_id": session_id,
                    "persona_mode": "shadow",
                    "task": TASK,
                    "app_context": APP_CONTEXT,
                    "constraints": {"k": 4},
                    "interaction_id": f"advisor-eq-{tag}-{index}",
                    # Forced to False in takeover by the handler anyway; stated so the test does
                    # not depend on that clamp to mean what it says.
                    "allow_fallback": False,
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            records.append(_record(response.json()))
    return records, session_id


def _record(body: dict[str, Any]) -> dict[str, Any]:
    advice = body.get("clone_advice") or {}
    history = ((body.get("state") or {}).get("takeover_context") or {}).get("quality_history") or [{}]
    policy = body.get("policy_decision") or {}
    return {
        "decision_source": body.get("decision_source"),
        "needs_human": body.get("needs_human"),
        "safety_decision": body.get("safety_decision"),
        "context_quality_score": body.get("context_quality_score"),
        "retrieval_triggered": body.get("retrieval_triggered"),
        "advisor_fail_streak": history[-1].get("advisor_fail_streak"),
        "advisor_unhealthy": history[-1].get("advisor_unhealthy"),
        "fast_path_reason": advice.get("fast_path_reason"),
        "advisor_failure_reason": advice.get("advisor_failure_reason"),
        "advisor_agreement": policy.get("advisor_agreement"),
    }


def _full_advice_visible(session_id: str) -> list[bool]:
    import psycopg

    url = os.environ["TCE_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT advice_visible FROM behavior_shadow_predictions "
            "WHERE session_id = %s ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return [bool(row[0]) for row in rows]


# ``Settings.context_retrieval_escalate_score``: below this, a turn whose bounded retrieval fired
# asks its owner.  Stated here rather than imported so a config drift shows up as a red test.
_ESCALATE_SCORE = 0.52


def _cold_corpus_escalation(record: dict[str, Any]) -> bool:
    """Is this turn's pause the pre-existing ``poor_retrieval_quality`` one, and nothing else?

    Conjunctive on the advisor being healthy, so an advisor failure can never hide behind it.
    """

    return bool(
        record["retrieval_triggered"]
        and record["context_quality_score"] is not None
        and float(record["context_quality_score"]) < _ESCALATE_SCORE
        and not record["advisor_unhealthy"]
        and record["advisor_failure_reason"] is None
        and record["advisor_fail_streak"] == 0
    )


@full_only
def test_full_a_healthy_advisor_never_escalates_across_consecutive_turns(
    full_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z1, the gate.  Four consecutive turns, advisor healthy on every one of them.

    Before the fix this produced, turn by turn:
    ``decision_source=fast_path``, ``advisor_failure_reason="advisor_runtime_unavailable"``,
    ``advisor_fail_streak`` 1, 1, 2, 2, ``advisor_unhealthy=True`` on turn 2 and
    ``needs_human=True`` with it — with a healthy advisor and no configuration change.

    The escalation this file forbids is **the advisor's**.  A blanket ``decision_source ==
    "deliberation"`` on every turn is a stronger claim than that, and it is not one the tree has
    ever satisfied: the workspace here is deliberately fresh, so ``poor_retrieval_quality``
    (``retrieval_triggered and context_quality_score < context_retrieval_escalate_score``) fires
    on the turns where bounded retrieval runs, and it did so at ``e805df6`` too — 0.4672 against
    a 0.52 line, ``safety_gate``/``needs_human`` on turns 0 and 2, ``fast_path`` on 1 and 3.
    That assertion only held for as long as ``context_quality_score`` was inflated by the
    circular ``0.40 * decision_confidence`` term, which is to say it was passing *because* of
    the defect the score's rewrite removed.  So the check below is keyed to the cause instead of
    to the field: an escalation is tolerated only when it is that cold-corpus one, and every
    advisor-shaped escalation is still red.
    """

    records, session_id = _full_turns(
        full_app,
        routes=lambda **_kwargs: (HEALTHY_ADVISOR, _inference_meta(parsed=True)),
        monkeypatch=monkeypatch,
    )

    problems: list[str] = []
    for index, record in enumerate(records):
        if record["advisor_fail_streak"] != 0:
            problems.append(f"turn {index}: advisor_fail_streak={record['advisor_fail_streak']}, want 0")
        if record["advisor_unhealthy"]:
            problems.append(f"turn {index}: advisor_unhealthy=True with a healthy advisor")
        if record["advisor_failure_reason"] is not None:
            problems.append(f"turn {index}: advisor_failure_reason={record['advisor_failure_reason']!r}")
        if record["fast_path_reason"] is not None:
            problems.append(f"turn {index}: fast_path_reason={record['fast_path_reason']!r}, want None")
        if record["decision_source"] != "deliberation" and not _cold_corpus_escalation(record):
            problems.append(
                f"turn {index}: decision_source={record['decision_source']!r}, want 'deliberation' "
                f"(and not the cold-corpus pause: quality={record['context_quality_score']!r}, "
                f"retrieval_triggered={record['retrieval_triggered']!r})"
            )

    assert not problems, (
        "A healthy advisor is being reported as unavailable. That forces the turn to the fast "
        "path, counts a failure against a working advisor, and escalates to a human once the "
        "streak reaches the limit — a turn that succeeded before P4 now stops the product.\n  "
        + "\n  ".join(problems)
    )

    escalated = [
        index
        for index, record in enumerate(records)
        if record["needs_human"] and not _cold_corpus_escalation(record)
    ]
    assert not escalated, (
        f"turns {escalated} escalated to a human with a healthy advisor and no other cause"
    )
    # Non-vacuity: the tolerance above must not swallow the whole assertion.  At least one turn
    # has to reach the advisor on its own merits, or "the advisor never escalates" would be true
    # of a run in which the advisor was never consulted.
    assert any(record["decision_source"] == "deliberation" for record in records), (
        "every turn was excused as a cold-corpus pause, so nothing here measured the advisor"
    )
    assert session_id


@full_only
def test_full_a_dead_advisor_still_escalates_across_consecutive_turns(
    full_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity, and Z2's safety half: an advisor that is really dead must still stop autonomy.

    The Z1 fix must not become "never report the advisor as unavailable".  The same four turns,
    with a provider that fails every attempt, have to climb the streak, trip
    ``advisor_unhealthy`` and escalate — and ``advice_visible`` must be False throughout.
    """

    dead = failed_advisor_contribution("provider_error", model_id="test-advisor", runtime_version="test")
    records, session_id = _full_turns(
        full_app,
        routes=lambda **_kwargs: (dead, _inference_meta(parsed=False)),
        monkeypatch=monkeypatch,
    )

    streaks = [record["advisor_fail_streak"] for record in records]
    assert streaks == sorted(streaks) and streaks[-1] >= 2, (
        f"a dead advisor did not accumulate a failure streak: {streaks}"
    )
    assert any(record["advisor_unhealthy"] for record in records), (
        "a dead advisor never reached advisor_unhealthy, so the safety gate that pauses autonomy "
        "in takeover mode can no longer fire"
    )
    assert any(record["needs_human"] for record in records), (
        "a dead advisor never escalated to a human"
    )
    assert all(record["decision_source"] != "deliberation" for record in records), (
        "a turn whose advisor never answered was still labelled a deliberation"
    )

    visible = _full_advice_visible(session_id)
    assert visible and not any(visible), (
        f"`advice_visible` is True on a turn whose advisor never produced output: {visible}"
    )


@full_only
def test_full_advice_visible_tracks_whether_the_advisor_actually_answered(
    full_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth field the gate compares, read from the column rather than recomputed here.

    ``advice_visible`` is persisted on the frozen ``behavior_shadow_predictions`` row and is what
    the promotion clause reads, so it is measured from Postgres.  A frozen row needs an
    escalation, so the needs-human threshold is pinned high for this test only: every turn then
    escalates on ``low_decision_confidence`` with the safety gate on ALLOW, which is the freeze
    condition.  Before the Z1 fix the healthy run below was ``False`` on every row -- the column
    could not be ``True`` at all, in either backend, on any turn.
    """

    from tce_api.config import get_settings

    settings = get_settings()
    settings.takeover_needs_human_threshold_cold = 0.99
    settings.takeover_needs_human_threshold_hot = 0.99

    _healthy, healthy_session = _full_turns(
        full_app,
        routes=lambda **_kwargs: (HEALTHY_ADVISOR, _inference_meta(parsed=True)),
        monkeypatch=monkeypatch,
    )
    healthy_visible = _full_advice_visible(healthy_session)

    dead = failed_advisor_contribution("provider_error", model_id="test-advisor", runtime_version="test")
    monkeypatch.setattr(
        full_app,
        "_advisor_runtime_reason_from_routes",
        lambda **_kwargs: (dead, _inference_meta(parsed=False)),
    )
    _failed, failed_session = _full_turns(full_app, routes=None, monkeypatch=monkeypatch)
    failed_visible = _full_advice_visible(failed_session)

    assert healthy_visible, "no shadow row was frozen for the healthy run; this test is vacuous"
    assert failed_visible, "no shadow row was frozen for the failed run; this test is vacuous"
    assert all(healthy_visible), (
        "`advice_visible` is False on a turn where the advisor ran, parsed and replaced the "
        f"fast-path payload: {healthy_visible}"
    )
    assert not any(failed_visible), (
        f"`advice_visible` is True on a turn whose advisor never answered: {failed_visible}"
    )


@full_only
def test_full_an_advisor_that_was_never_attempted_is_absent(
    full_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z2, ABSENT.  No route runner, no legacy fallback in takeover: nothing was attempted.

    The advisor is an optional input, so the policy proceeds without it and reports ``absent``.
    """

    from tce_api.config import get_settings

    get_settings().advisor_router_v2_enabled = False
    records, _ = _full_turns(full_app, routes=None, monkeypatch=monkeypatch, messages=MESSAGES[:2])

    agreements = [record["advisor_agreement"] for record in records]
    assert agreements and all(value == "absent" for value in agreements), (
        f"a turn that never attempted an advisor did not report ABSENT: {agreements}"
    )


@full_only
def test_full_an_advisor_that_was_attempted_and_failed_is_unavailable(
    full_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z2, UNAVAILABLE.  Every route is attempted and every route fails.

    A failed call used to be dropped on the floor -- ``_advisor_runtime_reason_from_routes``
    returned ``None`` however it ended -- so a dead advisor reached the policy as ABSENT, which
    means "nobody asked".  The turn then decided where it should have abstained.
    """

    dead = failed_advisor_contribution("llm_unavailable", model_id="test-advisor", runtime_version="test")
    records, _ = _full_turns(
        full_app,
        routes=lambda **_kwargs: (dead, _inference_meta(parsed=False)),
        monkeypatch=monkeypatch,
        messages=MESSAGES[:2],
    )

    agreements = [record["advisor_agreement"] for record in records]
    assert "unavailable" in agreements, (
        "an attempted-and-failed advisor call reached the policy as something other than "
        f"UNAVAILABLE: {agreements}. ABSENT means never attempted; collapsing the two lets a "
        "dead advisor look like an advisor nobody called."
    )


@full_only
def test_the_route_runner_reports_which_of_the_two_states_it_is_in() -> None:
    """Z2 at the seam that knows the answer, rather than only at the policy that reads it.

    ``used_llm`` keeps its old meaning (a PARSED contribution exists); ``advisor_attempted`` is
    what separates "no route was tried" from "every route was tried and every one failed".
    """

    import tce_api.main as full_main
    from tce_api.auth import AuthContext
    from tce_api.config import get_settings
    from tce_api.db import get_session_factory
    from tce_shared.events import AgentRole

    settings = get_settings()
    session = get_session_factory()()
    try:
        auth = AuthContext(
            consumer="advisor-eq",
            mode=settings.default_operation_mode,
            role=AgentRole.USER,
            workspace_id="advisor-eq-ws",
            user_id="advisor-eq-user",
            behavior_subject_id="advisor-eq-user",
        )
        contribution, meta = full_main._advisor_runtime_reason_from_routes(
            db=session,
            auth=auth,
            config={},
            health={},
            routes=[],
            similar_observations=[],
            candidate_options=(),
            constraints={},
            current_situation=TASK,
            situation_type="routine_task",
            persist_health=False,
        )
    finally:
        session.rollback()
        session.close()

    assert contribution is None, "no route was offered, so nothing was attempted: that is ABSENT"
    assert meta["advisor_attempted"] is False
    assert meta["used_llm"] is False
    assert meta["advisor_parse_state"] == "not_run"


@full_only
def test_the_route_runner_returns_a_failed_call_rather_than_dropping_it() -> None:
    """Z2 at the line that used to swallow it: every route attempted, every route failed.

    ``if ok and result is not None: contribution = result`` was the whole story, so the loop
    returned ``None`` however it ended and the caller could not tell "nothing was tried" from
    "everything was tried and everything failed".  The route below is the real default profile
    with no API key configured, which is the commonest way a live advisor is dead.
    """

    import tce_api.main as full_main
    from tce_api.auth import AuthContext
    from tce_api.config import get_settings
    from tce_api.db import get_session_factory
    from tce_shared.events import AgentRole

    settings = get_settings()
    session = get_session_factory()()
    try:
        auth = AuthContext(
            consumer="advisor-eq",
            mode=settings.default_operation_mode,
            role=AgentRole.USER,
            workspace_id="advisor-eq-ws",
            user_id="advisor-eq-user",
            behavior_subject_id="advisor-eq-user",
        )
        contribution, meta = full_main._advisor_runtime_reason_from_routes(
            db=session,
            auth=auth,
            config={},
            health={},
            routes=[{"provider_id": "openai", "model": "gpt-4o-mini", "priority": 0}],
            similar_observations=[],
            candidate_options=("a", "b"),
            constraints={},
            current_situation=TASK,
            situation_type="routine_task",
            persist_health=False,
        )
    finally:
        session.rollback()
        session.close()

    assert meta["advisor_attempted"] is True, "a route was offered and no attempt was recorded"
    assert meta["used_llm"] is False
    assert contribution is not None, (
        "every route failed and the loop still returned None. The policy reads `advisor is None` "
        "as ABSENT -- never attempted -- so a dead advisor decides the turn instead of abstaining "
        "it."
    )
    assert contribution.parse_state != "parsed"


# --------------------------------------------------------------------------------------
# Lite backend
# --------------------------------------------------------------------------------------


@pytest.fixture()
def lite_app(tmp_path: Path) -> Iterator[Any]:
    from contextlib import closing

    from tce_lite_api.config import get_settings
    from tce_lite_api.db import _connect, init_db
    from tce_lite_api.main import app
    from tce_lite_api.store import runtime_mode, set_runtime_mode
    from tce_shared.events import OperationMode

    settings = get_settings()
    keys = (
        "lite_db_path",
        "api_tokens",
        "charter_enforcement_enabled",
        "takeover_advisor_fail_streak_escalate",
        "takeover_advisor_runtime_cooloff_turns",
    )
    original = {key: getattr(settings, key) for key in keys}
    settings.lite_db_path = str(tmp_path / "advisor-eq.db")
    settings.api_tokens = "lite-test-token"
    settings.charter_enforcement_enabled = False
    settings.takeover_advisor_fail_streak_escalate = 2
    settings.takeover_advisor_runtime_cooloff_turns = 0
    # The Lite twin of `_clone_advisor_mode` above, needed for exactly the same reason and
    # missing until now.  `store.runtime_mode` reads the `runtime_mode` row of Lite's
    # `runtime_settings` table and falls back to `Settings.default_operation_mode` only until
    # that row exists -- and this fixture materialises the row on a database it has just
    # created.  A developer's `.env` sets `TCE_DEFAULT_OPERATION_MODE=clone_advisor`, so the row
    # says `clone_advisor` here; a checkout without that file (which is every CI checkout, since
    # `.env` is gitignored) says `timeline_only`, and then `build_clone_advice` raises
    # `409 clone_mode_disabled` on **every** turn.  The turn records
    # `fast_path_reason='advisor_exception'`, `advisor_fail_streak` climbs 1,2,3,4 and trips
    # `advisor_unhealthy` -- for an advisor Lite does not have, which is the precise state
    # `test_lite_consecutive_turns_carry_no_advisor_failure` exists to forbid.  So the mode is
    # seeded through `set_runtime_mode`, the real writer behind `PUT /v1/runtime/mode`, and
    # restored afterwards, on a temporary database and on a persistent one alike.
    init_db()
    with closing(_connect()) as conn:
        previous_mode = runtime_mode(conn, settings).mode
        set_runtime_mode(conn, OperationMode.CLONE_ADVISOR, updated_by="advisor-equivalence-test")
    try:
        yield app
    finally:
        with closing(_connect()) as conn:
            set_runtime_mode(conn, previous_mode, updated_by="advisor-equivalence-test-restore")
        for key, value in original.items():
            setattr(settings, key, value)


def _lite_turns(app: Any, *, messages: tuple[str, ...] = MESSAGES) -> list[dict[str, Any]]:
    tag = uuid.uuid4().hex[:8]
    headers = {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": "advisor-eq",
        "X-TCE-Role": "user",
        "X-TCE-Workspace": f"advisor-eq-ws-{tag}",
        "X-TCE-User": f"advisor-eq-user-{tag}",
        "X-TCE-Behavior-Subject": f"advisor-eq-user-{tag}",
    }
    records: list[dict[str, Any]] = []
    with TestClient(app) as client:
        for index, message in enumerate(messages):
            response = client.post(
                "/v1/takeover/step",
                json={
                    "message": message,
                    "session_id": f"advisor-eq-{tag}",
                    "persona_mode": "shadow",
                    "task": TASK,
                    "app_context": APP_CONTEXT,
                    "constraints": {"k": 4},
                    "interaction_id": f"advisor-eq-{tag}-{index}",
                    "allow_fallback": False,
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            records.append(_record(response.json()))
    return records


def test_lite_consecutive_turns_carry_no_advisor_failure(lite_app: Any) -> None:
    """Z1, the Lite twin.  Lite has no server-side advisor, so it can never accrue one's failures.

    The Full defect was a *reader* that could not see the advisor it had just called.  Lite has
    nothing to read, and the property to hold across consecutive turns is that no advisor state
    accumulates: the streak stays at zero and the advisor never becomes the reason a turn stops.
    """

    records = _lite_turns(lite_app)

    problems = [
        f"turn {index}: streak={record['advisor_fail_streak']} unhealthy={record['advisor_unhealthy']} "
        f"failure={record['advisor_failure_reason']!r} fast_path={record['fast_path_reason']!r}"
        for index, record in enumerate(records)
        if record["advisor_fail_streak"] != 0
        or record["advisor_unhealthy"]
        or record["advisor_failure_reason"] is not None
    ]
    assert not problems, (
        "Lite accumulated advisor failure state for an advisor it does not have.\n  " + "\n  ".join(problems)
    )


def test_lite_an_advisor_that_was_never_attempted_is_absent(lite_app: Any) -> None:
    """Z2, ABSENT on Lite -- which is every Lite turn, and the honest answer rather than a stub."""

    records = _lite_turns(lite_app, messages=MESSAGES[:2])
    agreements = [record["advisor_agreement"] for record in records]
    assert agreements and all(value == "absent" for value in agreements), (
        f"Lite reported an advisor state other than ABSENT with no advisor to attempt: {agreements}"
    )


def test_lite_an_advisor_that_was_attempted_and_failed_is_unavailable(
    lite_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z2, UNAVAILABLE on Lite, through the seam rather than around it.

    ``LITE_HAS_SERVER_SIDE_ADVISOR`` is the single constant that says Lite has no advisor, and
    the ABSENT/UNAVAILABLE seam in ``build_takeover_step`` is written against it.  Flipping it
    here — with a deliberation that raises — is the only way to prove the seam is wired rather
    than merely present: without this, "Lite always reports ABSENT" would pass whether or not
    the failed-call branch existed at all.
    """

    import tce_lite_api.store as store

    monkeypatch.setattr(store, "LITE_HAS_SERVER_SIDE_ADVISOR", True)

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("advisor runtime unreachable")

    monkeypatch.setattr(store, "build_clone_advice", _boom)

    records = _lite_turns(lite_app, messages=MESSAGES[:2])
    agreements = [record["advisor_agreement"] for record in records]
    streaks = [record["advisor_fail_streak"] for record in records]
    assert "unavailable" in agreements, (
        f"a failed Lite deliberation reached the policy as {agreements} rather than UNAVAILABLE"
    )
    assert streaks[-1] >= 1, f"a failed Lite deliberation was not counted as a failure: {streaks}"


def test_the_two_advisor_states_are_not_the_same_object() -> None:
    """The contract, stated once so a future collapse of the two has to delete an assertion.

    Deliberately not parameterised over the backends: this is the shared policy's own rule, and
    both backends import it.
    """

    from datetime import UTC, datetime

    from tce_shared.decision_policy import DecisionRequest, decide

    def _request(advisor: AdvisorContribution | None) -> DecisionRequest:
        return DecisionRequest(
            decision_family="needs_human",
            situation_type="approval_requested",
            situation_summary=TASK,
            objective_text=TASK,
            constraints={},
            context_snapshot={},
            candidate_options=(),
            evidence_rows=(),
            decision_at=datetime.now(tz=UTC),
            workspace_id="w",
            subject_user_id="u",
            project_id=None,
            episode_key="e",
            evidence_revision="r",
            evidence_cutoff_at=None,
            retrieval_version="v",
            model_id="m",
            runtime_version="rt",
            advisor=advisor,
        )

    absent = decide(_request(None))
    unavailable = decide(_request(failed_advisor_contribution("provider_error")))

    assert absent.advisor_agreement.value == "absent"
    assert unavailable.advisor_agreement.value == "unavailable", (
        "the policy reported a failed advisor call as ABSENT. Every takeover turn today exits "
        "at the empty-candidate-set stage, so this is the only exit those turns take and "
        "collapsing the two states here erases the distinction everywhere it can be observed."
    )
    assert json.loads(json.dumps(absent.block_payload()))["advisor_agreement"] == "absent"
    assert json.loads(json.dumps(unavailable.block_payload()))["advisor_agreement"] == "unavailable"
