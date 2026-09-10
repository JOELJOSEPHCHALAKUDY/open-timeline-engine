"""G21 and the citation-binding invariant, on the Full backend's assembly point.

Two properties, both about what a *bound key* is worth.

**G21 — the identity on the request is the identity that ran.**  ``model_id`` and
``runtime_version`` are bound keys on a qualification: change the model and the qualification
stops applying.  The first draft had them defaulting to ``""`` with nothing validating them
against the gateway actually invoked, which means a stale constant would keep a qualification
alive across a real model upgrade — a bound key that binds nothing.  ``build_decision_request``
takes them from the ``AdvisorContribution`` when the advisor ran, and from the resolved gateway
identity otherwise, and never leaves them empty.

**The evidence for a choice is bound to that choice.**  ``evidence_observation_ids`` holds the
neighbours whose mapped choice equals the selected option, and nothing else.  Before P4 the
``citations`` a decision came back with were timeline ``events.id`` values while the evidence
the model saw was a separate, unlinked list — so "here is why" and "here is what it read" were
two different sets that happened to be rendered next to each other.

Neither test needs a database: ``build_decision_request`` is exercised through a stub session
that returns the rows, because what is under test is the assembly, not the SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from tce_api import policy_store
from tce_shared.decision_policy import (
    AdvisorContribution,
    DecisionStatus,
    decide,
)
from tce_shared.scope import PROJECT_BOUND, PROJECT_UNBOUND, ResolvedScope

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _scope(*, project: str | None = None) -> ResolvedScope:
    return ResolvedScope(
        workspace_id="ws",
        executor_id="codex-executor",
        owner_id="owner",
        subject_user_id="subject",
        project_id=project,
        project_binding=PROJECT_BOUND if project else PROJECT_UNBOUND,
        task_id="task-1",
        owner_ids=frozenset({"owner"}),
    )


def _row(choice: str, *, index: int, days_ago: int, project_id: str | None = "proj-1") -> dict[str, Any]:
    # `project_id` is set on purpose. A NULL-project row is admissible evidence — it is the
    # same subject's decision — but it does not count toward `Adequacy.above_floor_count`, so a
    # corpus of them can inform a ranking and can never, alone, make a family adequate. Every
    # pre-P4 row is NULL forever, which is why that rule exists; a test that left it NULL would
    # be measuring the rule instead of the binding.
    return {
        "id": f"{index:08d}-0000-4000-8000-000000000000",
        "project_id": project_id,
        "situation_type": "blocker_encountered",
        "situation_summary": "the stripe webhook is failing in production after a deploy",
        "objective_text": "get the stripe webhook working again",
        "selected_choice": choice,
        "user_response": choice,
        "action_taken": choice,
        "available_choices_json": ["roll back first", "force push the fix"],
        "constraints_json": {},
        "contradicts_ids_json": [],
        "context_snapshot": {},
        "evidence_source": "explicit",
        "learning_eligible": True,
        "lifecycle_status": "active",
        "ts": _NOW - timedelta(days=days_ago),
        "confidence": 1.0,
    }


class _StubResult:
    def __init__(self, rows: list[dict[str, Any]] | None, scalar: Any = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> _StubResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows or [])

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def scalar(self) -> Any:
        return self._scalar

    def scalars(self) -> _StubResult:
        return self


class _StubSession:
    """Answers the four statements ``build_decision_request`` issues, and nothing else.

    Deliberately not a mock that accepts anything: a stub that silently answers a query the
    code did not intend to make is how an assembly point grows a second evidence loader.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def get_bind(self) -> Any:  # pragma: no cover - identity only
        return self

    def execute(self, statement: Any, params: Any = None) -> _StubResult:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        if "information_schema.columns" in sql:
            return _StubResult(None, scalar=1)
        if "COUNT(*) FROM decision_observations" in sql:
            return _StubResult(None, scalar=len(self.rows))
        if "FROM decision_observations" in sql:
            return _StubResult(self.rows)
        if "FROM memory_rules" in sql:
            return _StubResult([])
        if "FROM policy_qualifications" in sql:
            return _StubResult([])
        raise AssertionError(f"unexpected statement: {sql[:160]}")


@pytest.fixture(autouse=True)
def _forget_column_probe() -> Any:
    policy_store.reset_policy_column_cache()
    yield
    policy_store.reset_policy_column_cache()


def _build(session: _StubSession, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "scope": _scope(project="proj-1"),
        "decision_family": "safety_confirmation",
        "situation_type": "blocker_encountered",
        "situation_summary": "the stripe webhook is failing in production after a deploy",
        "objective_text": "get the stripe webhook working again",
        "constraints": {},
        "context_snapshot": {},
        "candidate_options": ["roll back first", "force push the fix"],
        "decision_at": _NOW,
        "session_id": "sess-1",
        "objective_hash": "abc123",
    }
    kwargs.update(overrides)
    # The stub answers the Session protocol this function actually uses (execute, get_bind) and
    # nothing else; the cast is the honest way to say so.
    return policy_store.build_decision_request(cast("Session", session), **kwargs)


# --------------------------------------------------------------------------- G21


def test_model_identity_comes_from_the_advisor_that_actually_ran() -> None:
    session = _StubSession([_row("roll back first", index=1, days_ago=3)])
    advisor = AdvisorContribution(
        recommended_option="roll back first",
        abstained=False,
        abstain_reason=None,
        evidence_ids=(),
        conflicting_evidence_ids=(),
        advisor_note=None,
        parse_state="parsed",
        prompt_sha256="b" * 64,
        model_id="claude-3-5-haiku-latest",
        runtime_version="anthropic",
    )
    request = _build(session, advisor=advisor)

    assert request.model_id == "claude-3-5-haiku-latest"
    assert request.runtime_version == "anthropic"
    assert request.retrieval_version == policy_store.RETRIEVAL_VERSION


def test_model_identity_is_never_left_empty() -> None:
    """An empty bound key cannot be told apart from a bound key nobody filled in."""

    request = _build(_StubSession([_row("roll back first", index=1, days_ago=3)]))
    assert request.model_id
    assert request.runtime_version
    assert request.retrieval_version == "full-knn-v1"


def test_the_retrieval_version_is_this_loader_and_not_a_shared_constant() -> None:
    """Three loaders, three strings.

    A single ``RETRIEVAL_VERSION`` in the shared policy module would let Full, Lite and the
    replay assembly diverge arbitrarily while all three stamped the same bound key, so a Full
    qualification could silently authorise a Lite decision.
    """

    import tce_shared.decision_policy as decision_policy
    from tce_lite_api import policy_store as lite_policy_store

    assert not hasattr(decision_policy, "RETRIEVAL_VERSION")
    assert policy_store.RETRIEVAL_VERSION != lite_policy_store.RETRIEVAL_VERSION


def test_the_two_settings_are_hashed_into_the_tuning_key() -> None:
    """Even the scoping switches cannot move after a family qualifies without invalidating it."""

    from tce_shared.decision_policy import PolicyTuning

    default = PolicyTuning()
    moved = PolicyTuning(allow_unscoped_project_evidence=False)
    listed = PolicyTuning(advisor_required_families=frozenset({"safety_confirmation"}))
    assert len({default.tuning_sha(), moved.tuning_sha(), listed.tuning_sha()}) == 3


def test_no_abstention_threshold_is_reachable_through_settings() -> None:
    """The five floors are frozen constants, not ``TCE_`` env settings.

    Sweeping ``TCE_POLICY_OOD_SIMILARITY_FLOOR`` until coverage clears, writing a QUALIFIED
    record with an unchanged ``THRESHOLDS_SHA`` and then reverting the env var is a working
    post-hoc tuning path that the exposure check cannot see.
    """

    from tce_api.config import Settings

    banned = {
        "policy_min_effective_sample",
        "policy_similarity_floor",
        "policy_ood_similarity_floor",
        "policy_conflict_share_ratio",
        "policy_min_agreement_share",
    }
    assert not banned & set(Settings.model_fields)
    assert {"policy_allow_unscoped_project_evidence", "policy_advisor_required_families"} <= set(
        Settings.model_fields
    )


# --------------------------------------------------------------------------- citation binding


def test_evidence_ids_are_bound_to_the_selected_option_at_the_api_boundary() -> None:
    """No id in ``evidence_observation_ids`` has a mapped choice different from the winner."""

    rows = [
        _row("roll back first", index=1, days_ago=2),
        _row("roll back first", index=2, days_ago=4),
        _row("roll back first", index=3, days_ago=6),
        _row("force push the fix", index=9, days_ago=8),
    ]
    result = decide(_build(_StubSession(rows)))

    assert result.status is DecisionStatus.SELECTED
    assert result.selected_option == "roll back first"
    losing_id = "00000009-0000-4000-8000-000000000000"
    assert losing_id not in result.evidence_observation_ids
    assert set(result.evidence_observation_ids) <= {
        "00000001-0000-4000-8000-000000000000",
        "00000002-0000-4000-8000-000000000000",
        "00000003-0000-4000-8000-000000000000",
    }

    # The wire projection carries the same list, capped, and carries no score.
    block = result.block_payload()
    assert block["evidence_observation_ids"] == list(result.evidence_observation_ids[:12])
    assert "policy_score" not in block


def test_an_empty_candidate_set_abstains_rather_than_returning_an_unoffered_label() -> None:
    """The ordinary takeover turn.

    ``_map_choice`` opens with ``if not allowed: return choice``, so without the guard the
    assembly point would hand back the raw historical label as though it were a selection.
    """

    rows = [_row("roll back first", index=i, days_ago=i) for i in (1, 2, 3)]
    result = decide(_build(_StubSession(rows), candidate_options=[]))

    assert result.status is DecisionStatus.ABSTAINED
    assert result.selected_option is None
    assert result.exposed is False


def test_no_qualification_means_unexposed_and_changes_nothing_else() -> None:
    """The Y1 property at this backend's assembly point.

    The stub returns no qualification row, which is the state of every family on every corpus
    measured so far.  ``exposed`` is False and the decision itself — status, reason, adequacy,
    OOD, conflict — is reported exactly as computed.  The turn is what it was.
    """

    rows = [
        _row("roll back first", index=1, days_ago=2),
        _row("roll back first", index=2, days_ago=4),
        _row("roll back first", index=3, days_ago=6),
    ]
    result = decide(_build(_StubSession(rows)))

    assert result.exposed is False
    assert result.exposure_state.value == "no_qualification"
    # Not downgraded to an abstention: the shadow row stays fully scoreable, which is the point
    # of running the policy before it has permission to be used.
    assert result.status is DecisionStatus.SELECTED
    assert result.abstain_reason is None
    assert result.policy_score > 0.0


# --------------------------------------------------------------------------- write interlock


def test_the_full_store_refuses_to_record_a_qualification_nothing_measured() -> None:
    """The Full twin of the Lite case, and the reason the guard is not a bound key.

    Bound keys answer *"is this measurement still about the system in use?"*.  They have never
    answered *"was there a measurement?"* — a record asserting ``qualified`` beside zero
    adjudicated cases carries an honest thresholds digest, an honest model id and an honest
    retrieval version, so it clears every one of them and grants full exposure.  The check has
    to happen where the row is written.

    No database: the guard runs before any SQL, so a session that would explode on use proves
    nothing was written.
    """

    from tce_shared.decision_policy import QualificationState

    class _NoSQL:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a refused qualification claim must not reach the database")

    with pytest.raises(ValueError) as excinfo:
        policy_store.write_qualification(
            cast("Session", _NoSQL()),
            workspace_id="ws",
            subject_user_id="subject",
            project_id=None,
            decision_family="safety_confirmation",
            state=QualificationState.QUALIFIED,
            model_id="claude-3-5-haiku-latest",
            runtime_version="anthropic",
            prompt_sha256="",
            retrieval_version=policy_store.RETRIEVAL_VERSION,
            evidence_revision_value="rev-1",
            evidence_cutoff_at=None,
            learning_eligible_at_qualification=100,
            split_sha256="split",
            split={},
            metrics={},
            gate={"passed": True},
            shortfalls=("adjudicated 0 < 100", "coverage 0.0 < 0.3"),
            adjudicated_count=0,
            non_abstained_count=0,
            coverage=0.0,
            precision_lower_bound=0.0,
            distinct_episodes=0,
            duplicate_context_ratio=0.0,
            baselines={},
            exclusions={},
        )
    assert "refusing to record state=qualified" in str(excinfo.value)


def test_a_recorded_refusal_still_reaches_the_database() -> None:
    """The guard checks a claim, never a refusal: NOT_QUALIFIED with shortfalls still writes."""

    from tce_shared.decision_policy import QualificationState

    seen: list[Any] = []

    class _Recording:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(args)
            return None

    policy_store.write_qualification(
        cast("Session", _Recording()),
        workspace_id="ws",
        subject_user_id="subject",
        project_id=None,
        decision_family="safety_confirmation",
        state=QualificationState.NOT_QUALIFIED,
        model_id="claude-3-5-haiku-latest",
        runtime_version="anthropic",
        prompt_sha256="",
        retrieval_version=policy_store.RETRIEVAL_VERSION,
        evidence_revision_value="rev-1",
        evidence_cutoff_at=None,
        learning_eligible_at_qualification=0,
        split_sha256=None,
        split={},
        metrics={},
        gate={"passed": False},
        shortfalls=("adjudicated 0 < 100",),
        adjudicated_count=0,
        non_abstained_count=0,
        coverage=0.0,
        precision_lower_bound=0.0,
        distinct_episodes=0,
        duplicate_context_ratio=0.0,
        baselines={},
        exclusions={},
    )
    assert seen, "a NOT_QUALIFIED attempt is a first-class row, not an absence"


# --------------------------------------------------------------------------------------
# Replay's candidate set — reconstruction, and Full/Lite agreement on how it is read
# --------------------------------------------------------------------------------------


def test_replay_candidate_options_reads_the_opportunity_not_the_query_row() -> None:
    """The shadow row's ``query_json`` never carries the options; the opportunity does.

    ``_freeze_decision_opportunity`` builds ``query`` from the situation, the objective, the
    constraints and the snapshot, and passes the option list to ``insert_opportunity``.  So a
    replay that reads ``query_json['available_choices']`` reconstructs ``()`` — and an empty
    candidate set abstains, which made every replayed decision an abstention the turn never
    made.  The reader must take the joined ``alternatives_json`` and apply the *write site's*
    blank filter, or the rebuilt request fingerprints differently on a whitespace entry.
    """

    read = policy_store.replay_candidate_options

    # Postgres hands back a decoded list; SQLite (Lite) hands back the JSON text.  Same answer.
    assert read({"candidate_options_json": ["confirm", "abort"]}) == ("confirm", "abort")
    assert read({"candidate_options_json": '["confirm", "abort"]'}) == ("confirm", "abort")
    assert read({"alternatives_json": ["confirm", "abort"]}) == ("confirm", "abort")

    # The write site decides over `... if str(item).strip()`; replay must drop the same entries.
    assert read({"candidate_options_json": ["confirm", "   ", "", "abort"]}) == ("confirm", "abort")

    # No opportunity on the join (a retrospective row) is an honest empty, not a crash.
    assert read({"candidate_options_json": None, "query_json": None}) == ()
    assert read({}) == ()
    assert read({"candidate_options_json": "not json"}) == ()

    # The fallback exists for a future writer, and only for one.  It must never be reached in
    # preference to the opportunity.
    row = {"candidate_options_json": ["confirm", "abort"], "query_json": {"available_choices": ["x"]}}
    assert read(row) == ("confirm", "abort")
    assert read({"query_json": {"available_choices": ["x", "y"]}}) == ("x", "y")


def test_full_and_lite_replay_read_the_candidate_set_identically() -> None:
    """A reconstruction that differs between backends is a divergence nothing else catches.

    Both replay paths join ``decision_opportunities`` and both must select the same expression
    under the same alias, or the two harnesses score different option lists while reporting the
    same ``retrieval_version``.
    """

    from tce_lite_api import policy_store as lite_policy_store

    assert policy_store.REPLAY_CANDIDATE_COLUMN == lite_policy_store.REPLAY_CANDIDATE_COLUMN

    probes: list[dict[str, Any]] = [
        {"candidate_options_json": ["confirm", "abort"]},
        {"candidate_options_json": '["confirm", "abort"]'},
        {"candidate_options_json": ["a", " ", "b"]},
        {"candidate_options_json": None, "query_json": '{"available_choices": ["x"]}'},
        {},
    ]
    for probe in probes:
        assert policy_store.replay_candidate_options(probe) == lite_policy_store.replay_candidate_options(probe), probe
