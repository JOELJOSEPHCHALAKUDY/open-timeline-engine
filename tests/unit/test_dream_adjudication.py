"""P6 §5 — relevance is adjudicated, blindness is checked, and acceptance feeds no numerator.

The properties under test here are the load-bearing ones, and each is proved rather than
asserted in prose:

* ``blind_verified`` is the **server's** answer.  A caller that claims blindness on an
  adjudication written after the owner already rejected the proposal gets ``blind_verified=false``
  out of the store, whatever ``blind_claimed`` said (G11's ``test_blind_is_server_checked``).
* A rejected proposal with zero adjudications yields ``false_positive_relevance =
  NOT_COMPUTABLE`` — a rejection is a preference, not a claim about fit — and one
  ``not_relevant`` adjudication on an **accepted** proposal yields ``1.0`` (G11's
  ``test_false_positive_needs_an_adjudication``).
* An ``ignored`` proposal moves neither number.  Nonresponse is not rejection.
* Nothing in the module reads an ``agent_asserted`` field, proved by AST walk, and nothing in
  either store issues a DELETE or an UPDATE, proved by SQL-literal scan.
* The renderer prints NOT_COMPUTABLE with a reason at n=0 and never ``0.0``.
"""

from __future__ import annotations

import ast
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tce_api import dream_adjudication_store as full_store
from tce_lite_api import dream_adjudication_store as lite_store
from tce_shared import dream_adjudication as da
from tce_shared.aspirations import DreamProposalStatus, NonresponseState
from tce_shared.pilot_enrollment import ClauseState, DeliveryUsefulness, RelevanceVerdict
from tce_shared.pilot_thresholds import DELIVERY_LOOKBACK_DAYS, MIN_ADJUDICATED_DREAMS
from tce_shared.scope import PROJECT_BOUND, ResolvedScope

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# 1. Blindness is checked against P5's log, never taken on the caller's word
# --------------------------------------------------------------------------------------


def test_blind_is_false_when_it_was_not_claimed() -> None:
    """The server never invents blindness.  No claim, no verified flag."""

    verdict = da.verify_blind(blind_claimed=False, adjudicator_id="owner", adjudicated_at=T0, verdict_events=[])
    assert verdict.verified is False
    assert verdict.reason == "not_claimed"
    assert verdict.reason in da.BLIND_REASONS


def test_blind_is_verified_when_no_verdict_exists_yet() -> None:
    """A proposal with no verdict has no status to have been revealed."""

    verdict = da.verify_blind(blind_claimed=True, adjudicator_id="owner", adjudicated_at=T0, verdict_events=[])
    assert verdict.verified is True
    assert verdict.reason == "verified_no_verdict_yet"
    assert verdict.first_verdict_at is None


def test_blind_is_verified_when_the_verdict_is_strictly_later() -> None:
    later = da.VerdictEvent(kind="rejected", actor="owner", occurred_at=T0 + timedelta(hours=1))
    verdict = da.verify_blind(
        blind_claimed=True, adjudicator_id="owner", adjudicated_at=T0, verdict_events=[later]
    )
    assert verdict.verified is True
    assert verdict.reason == "verified_before_verdict"
    assert verdict.first_verdict_kind == "rejected"


def test_blind_is_refused_when_the_verdict_came_first() -> None:
    """The claim is refused, not honoured with a note.  This is the whole point of the column."""

    earlier = da.VerdictEvent(kind="accepted", actor="owner", occurred_at=T0 - timedelta(hours=1))
    verdict = da.verify_blind(
        blind_claimed=True, adjudicator_id="someone-else", adjudicated_at=T0, verdict_events=[earlier]
    )
    assert verdict.verified is False
    assert verdict.reason == "verdict_precedes_adjudication"


def test_the_judge_who_gave_the_verdict_is_never_blind_to_it_even_on_a_tie() -> None:
    """An equal timestamp must not certify the author of the verdict as blind to it."""

    tie = da.VerdictEvent(kind="rejected", actor="owner", occurred_at=T0)
    verdict = da.verify_blind(
        blind_claimed=True, adjudicator_id="owner", adjudicated_at=T0, verdict_events=[tie]
    )
    assert verdict.verified is False
    assert verdict.reason == "adjudicator_gave_prior_verdict"


def test_a_display_event_is_not_a_verdict() -> None:
    """``surfaced`` reveals the proposal, not its standing.  Only the verdict axis blinds."""

    surfaced = da.VerdictEvent(kind="surfaced", actor="executor", occurred_at=T0 - timedelta(days=3))
    verdict = da.verify_blind(
        blind_claimed=True, adjudicator_id="owner", adjudicated_at=T0, verdict_events=[surfaced]
    )
    assert verdict.verified is True
    assert verdict.reason == "verified_no_verdict_yet"


def test_unsnoozed_counts_as_a_verdict_event() -> None:
    """The stated deviation from §5.2's three-kind list: P5's own HUMAN_VERDICT_ACTIONS is used.

    Stricter, never looser — it can only lower ``blind_verified``.
    """

    assert "unsnoozed" in da.VERDICT_EVENT_KINDS
    event = da.VerdictEvent(kind="unsnoozed", actor="owner", occurred_at=T0 - timedelta(minutes=1))
    assert da.verify_blind(
        blind_claimed=True, adjudicator_id="other", adjudicated_at=T0, verdict_events=[event]
    ).verified is False


def test_naive_timestamps_are_read_as_utc_not_crashed_on() -> None:
    naive = da.VerdictEvent(kind="rejected", actor="owner", occurred_at=datetime(2026, 9, 11, 13, 0, 0))
    verdict = da.verify_blind(
        blind_claimed=True, adjudicator_id="other", adjudicated_at=T0, verdict_events=[naive]
    )
    assert verdict.verified is True  # 13:00Z is later than 12:00Z


# --------------------------------------------------------------------------------------
# 2. Later useful delivery — and the word *later* is a clock
# --------------------------------------------------------------------------------------


def test_an_unanswered_delivery_is_stored_as_nothing() -> None:
    admission = da.admit_delivery(claimed=None, completed_at=T0, adjudicated_at=T0)
    assert admission.stored is None
    assert admission.counted is False
    assert admission.reason == "not_answered"


def test_a_usefulness_answer_before_delivery_is_stored_and_never_counted() -> None:
    admission = da.admit_delivery(claimed=DeliveryUsefulness.USEFUL, completed_at=None, adjudicated_at=T0)
    assert admission.stored is DeliveryUsefulness.USEFUL  # the owner's word is kept verbatim
    assert admission.counted is False
    assert admission.reason == "not_delivered"


def test_an_answer_inside_the_lookback_is_stored_and_never_counted() -> None:
    completed = T0 - timedelta(days=DELIVERY_LOOKBACK_DAYS - 1)
    admission = da.admit_delivery(claimed=DeliveryUsefulness.USEFUL, completed_at=completed, adjudicated_at=T0)
    assert admission.stored is DeliveryUsefulness.USEFUL
    assert admission.counted is False
    assert admission.reason == "too_early"
    assert admission.eligible_at == completed + timedelta(days=DELIVERY_LOOKBACK_DAYS)


def test_an_answer_exactly_at_the_lookback_boundary_counts() -> None:
    completed = T0 - timedelta(days=DELIVERY_LOOKBACK_DAYS)
    admission = da.admit_delivery(claimed=DeliveryUsefulness.NOT_USEFUL, completed_at=completed, adjudicated_at=T0)
    assert admission.counted is True
    assert admission.reason == "counted"


def test_a_caller_who_says_too_early_is_never_counted_however_late_it_is() -> None:
    completed = T0 - timedelta(days=365)
    admission = da.admit_delivery(claimed=DeliveryUsefulness.TOO_EARLY, completed_at=completed, adjudicated_at=T0)
    assert admission.counted is False
    assert admission.reason == "answered_too_early_by_caller"


def test_delivery_distinguishes_nothing_delivered_from_nothing_answered() -> None:
    """Two emptinesses, two reasons, two different things for the owner to do."""

    rate, clause = da.later_useful_delivery(useful=0, not_useful=0, completed_proposals=0)
    assert rate is None
    assert clause.state is ClauseState.NOT_COMPUTABLE
    assert clause.reason == "no_completed_proposals"
    assert "lookback 30d" in clause.detail

    rate, clause = da.later_useful_delivery(useful=0, not_useful=0, completed_proposals=4, too_early=3)
    assert rate is None
    assert clause.reason == "no_adjudications"
    assert "3 answered inside the 30d lookback" in clause.detail


def test_delivery_rate_is_computed_only_from_counted_answers() -> None:
    rate, clause = da.later_useful_delivery(useful=3, not_useful=1, completed_proposals=9, too_early=5)
    assert rate == 0.75
    assert clause.state is ClauseState.PASS


def test_adjudication_coverage_shortfall_carries_the_floor() -> None:
    clause = da.adjudication_coverage_clause(0)
    assert clause.state is ClauseState.SHORTFALL
    assert clause.measured == 0
    assert clause.floor == MIN_ADJUDICATED_DREAMS
    assert da.adjudication_coverage_clause(MIN_ADJUDICATED_DREAMS).state is ClauseState.PASS


# --------------------------------------------------------------------------------------
# 3. The block: acceptance is not adjudication, and nonresponse is not rejection
# --------------------------------------------------------------------------------------


def test_the_empty_corpus_reads_as_not_computable_and_never_as_zero() -> None:
    """The deliverable, in miniature: a legible *not enough evidence yet* at n=0."""

    block = da.dream_block(da.DreamCounts(project_id="proj_a"))
    rendered = block.render()

    assert block.false_positive_rate is None
    assert block.delivery_rate is None
    assert "NOT_COMPUTABLE" in rendered
    assert "no_adjudications" in rendered
    assert "no_completed_proposals" in rendered
    assert "0.0" not in rendered  # a rate of zero would read as "no false positives"
    assert "acceptance is not adjudication" in rendered
    assert da.NONRESPONSE_IS_NOT_REJECTION in rendered
    assert "proposals=0  surfaced=0  surfaced_attested=0" in rendered


def test_a_rejection_alone_never_produces_a_false_positive() -> None:
    """G11, first half.  A rejected proposal with zero adjudications is NOT_COMPUTABLE."""

    counts = da.DreamCounts(
        project_id="proj_a",
        proposals=1,
        by_status={DreamProposalStatus.REJECTED.value: 1},
    )
    block = da.dream_block(counts)
    assert block.false_positive_rate is None
    assert block.false_positive_clause.state is ClauseState.NOT_COMPUTABLE
    assert block.false_positive_clause.reason == "no_adjudications"
    assert "rejected=1" in block.render()  # the rejection is printed, it is just not a numerator


def test_one_not_relevant_adjudication_on_an_accepted_proposal_is_a_full_false_positive() -> None:
    """G11, second half.  Relevance is orthogonal to acceptance in both directions."""

    counts = da.DreamCounts(
        project_id="proj_a",
        proposals=1,
        by_status={DreamProposalStatus.ACCEPTED.value: 1},
        not_relevant=1,
        adjudicated_proposals=1,
    )
    block = da.dream_block(counts)
    assert block.false_positive_rate == 1.0
    assert block.false_positive_clause.state is ClauseState.PASS


def test_an_ignored_proposal_moves_neither_number() -> None:
    """P5 §1.2: ``ignored`` is counted in no rejection metric, and P6 does not start."""

    counts = da.DreamCounts(
        project_id="proj_a",
        proposals=1,
        by_status={DreamProposalStatus.SNOOZED.value: 1},
        nonresponse={NonresponseState.IGNORED.value: 1},
    )
    block = da.dream_block(counts)
    assert block.false_positive_rate is None
    assert block.delivery_rate is None
    assert "ignored=1" in block.render()
    payload = block.to_payload()
    assert payload["nonresponse"]["ignored"] == 1
    assert payload["verdicts"].get("ignored") is None  # the axes never merge


def test_cannot_judge_is_in_neither_numerator_nor_denominator() -> None:
    counts = da.DreamCounts(project_id="proj_a", cannot_judge=4, adjudicated_proposals=4)
    block = da.dream_block(counts)
    assert block.false_positive_rate is None
    assert block.false_positive_clause.reason == "no_adjudications"
    assert "cannot_judge=4" in block.render()
    # ...but the count is still evidence that judgements were attempted
    assert block.coverage_clause.measured == 4


def test_absent_tables_say_so_rather_than_printing_zeros() -> None:
    block = da.dream_block_unavailable(project_id="proj_a", detail="alembic 20260909_0041")
    rendered = block.render()
    assert block.available is False
    assert "dream_tables_absent" in rendered
    assert "20260909_0041" in rendered
    assert "proposals=" not in rendered


def test_blocks_never_pool_two_readings_of_one_project() -> None:
    with pytest.raises(ValueError, match="refuses to pool"):
        da.dream_blocks([da.DreamCounts(project_id="proj_a"), da.DreamCounts(project_id="proj_a")])


def test_blocks_stay_per_project_and_the_renderer_prints_each() -> None:
    blocks = da.dream_blocks([da.DreamCounts(project_id="proj_a"), da.DreamCounts(project_id="proj_b")])
    text = da.render_dream_blocks(blocks)
    assert "project=proj_a" in text
    assert "project=proj_b" in text
    census = da.summarise_states(blocks)
    assert census["not_computable"] == 4  # two blocks x (false-positive + delivery)
    assert census["shortfall"] == 2  # two coverage clauses at zero
    assert census["pass"] == 0


def test_the_block_renders_from_clause_render_so_the_two_renderers_cannot_drift() -> None:
    """The report prints ``Clause.render()``; it does not re-implement the clause text."""

    block = da.dream_block(da.DreamCounts(project_id="proj_a"))
    rendered = block.render()
    for clause in (block.coverage_clause, block.false_positive_clause, block.delivery_clause):
        assert clause.render() in rendered


# --------------------------------------------------------------------------------------
# 4. Structural guards — no self-grading input, no destructive statement
# --------------------------------------------------------------------------------------

AGENT_ASSERTED_NAMES = frozenset(
    {
        "pilot_episode_observations",
        "agent_notes",
        "agent_declared_steps",
        "agent_declared_steps_json",
        "agent_self_rated_difficulty",
        "agent_confidence",
        "action_similarity",
        "workflow_similarity",
        "calibration_brier",
        "surfaced_count",  # the executor's claim that it displayed something
    }
)


def test_no_clause_function_reads_an_agent_asserted_field() -> None:
    """G5's shared half, for the dream module.  The party under test does not grade itself.

    ``surfaced_count`` is in the forbidden list on purpose: it is the executor's own claim that
    it showed the proposal.  Only ``surfaced_attested``, P5's attested-display count, may be
    read, and the SQL that derives ``surfaced`` from it lives in the stores, not here.
    """

    source = (REPO_ROOT / "shared" / "tce_shared" / "dream_adjudication.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in AGENT_ASSERTED_NAMES:
            offenders.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in AGENT_ASSERTED_NAMES:
            offenders.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # a column name reaching a clause through a string literal counts too
            if node.value in AGENT_ASSERTED_NAMES:
                offenders.append(node.value)
    assert offenders == []


def test_the_module_declares_no_dream_status_and_no_nonresponse_value() -> None:
    """G10, for this module.  P6 composes with P5's vocabulary and forks none of it."""

    source = (REPO_ROOT / "shared" / "tce_shared" / "dream_adjudication.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    enums = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id.endswith("Enum") for base in node.bases)
    ]
    assert enums == []
    for member in DreamProposalStatus:
        assert f'"{member.value}" =' not in source
    assert set(da.NONRESPONSE_KEYS) == {member.value for member in NonresponseState}


P6_DREAM_FILES = (
    "shared/tce_shared/dream_adjudication.py",
    "services/tce_api/tce_api/dream_adjudication_store.py",
    "services/tce_lite_api/tce_lite_api/dream_adjudication_store.py",
)


def test_no_dream_module_deletes_updates_or_alters_anything() -> None:
    """G4's SQL-literal half, for the files this builder owns.

    An adjudication is the owner's own answer.  There is no statement anywhere in these three
    files that can remove one, overwrite one, or add a column to a table P5 owns.
    """

    forbidden = (
        "DELETE FROM dream_relevance_",
        "DELETE FROM dream_proposals",
        "DELETE FROM pilot_",
        "UPDATE dream_relevance_adjudications",
        "UPDATE dream_proposals",
        "ALTER TABLE dream_proposals",
        "DROP TABLE",
    )
    for relative in P6_DREAM_FILES:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for pattern in forbidden:
                    assert pattern.upper() not in upper, f"{relative} contains {pattern!r}"


def test_the_two_stores_agree_on_the_adjudication_columns() -> None:
    """Full/Lite parity, at the column level, checked against the migration itself."""

    migration = (
        REPO_ROOT / "infra" / "alembic" / "versions" / "20260909_0043_pilot_enrollment.py"
    ).read_text(encoding="utf-8")
    start = migration.index("CREATE TABLE IF NOT EXISTS dream_relevance_adjudications")
    block = migration[start : migration.index(")", migration.index("schema_version", start))]
    full_columns = {
        line.strip().split()[0]
        for line in block.splitlines()[1:]
        if line.strip() and not line.strip().startswith(")")
    }
    lite_ddl = lite_store.DREAM_ADJUDICATION_DDL[0]
    lite_columns = {
        line.strip().split()[0]
        for line in lite_ddl.splitlines()
        if line.strip() and not line.strip().startswith(("CREATE", ")"))
    }
    assert full_columns == lite_columns


# --------------------------------------------------------------------------------------
# 5. The Lite store, end to end against a real SQLite database
# --------------------------------------------------------------------------------------


def _scope(project_id: str | None = "proj_a") -> ResolvedScope:
    return ResolvedScope(
        workspace_id="personal",
        executor_id="host-capture",
        owner_id="owner-1",
        subject_user_id="subject-1",
        project_id=project_id,
        project_binding=PROJECT_BOUND,
        task_id="session-1",
        owner_ids=frozenset({"owner-1"}),
    )


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dream_proposals (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            scope_kind TEXT NOT NULL DEFAULT 'project',
            status TEXT NOT NULL DEFAULT 'proposed',
            nonresponse_state TEXT NOT NULL DEFAULT 'never_surfaced',
            surfaced_count INTEGER NOT NULL DEFAULT 0,
            surfaced_attested INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT
        );
        CREATE TABLE dream_proposal_events (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            actor_class TEXT NOT NULL DEFAULT 'system',
            occurred_at TEXT NOT NULL
        );
        """
    )
    lite_store.ensure_dream_adjudication_tables(conn)
    lite_store.ensure_dream_adjudication_tables(conn)  # idempotent
    return conn


def _proposal(
    conn: sqlite3.Connection,
    *,
    status: str = "proposed",
    nonresponse: str = "never_surfaced",
    surfaced: int = 0,
    attested: int = 0,
    completed_at: datetime | None = None,
    project_id: str | None = "proj_a",
) -> str:
    identifier = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO dream_proposals (id, workspace_id, owner_id, subject_user_id, project_id, "
        "scope_kind, status, nonresponse_state, surfaced_count, surfaced_attested, completed_at) "
        "VALUES (?, 'personal', 'owner-1', 'subject-1', ?, ?, ?, ?, ?, ?, ?)",
        (
            identifier,
            project_id,
            "project" if project_id else "workspace",
            status,
            nonresponse,
            surfaced,
            attested,
            completed_at.isoformat() if completed_at else None,
        ),
    )
    return identifier


def _event(conn: sqlite3.Connection, proposal_id: str, *, kind: str, actor: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO dream_proposal_events (id, proposal_id, seq, kind, actor, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), proposal_id, 1, kind, actor, at.isoformat()),
    )


def test_lite_store_refuses_to_report_when_the_tables_are_absent() -> None:
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    with pytest.raises(lite_store.DreamTablesMissing):
        lite_store.dream_counts_for_scope(bare, scope=_scope())


def test_lite_store_writes_the_servers_blind_answer_not_the_callers_claim() -> None:
    """G11's ``test_blind_is_server_checked``, at the store boundary.

    The caller claims blindness; the owner had already rejected the proposal an hour earlier;
    the row is written with ``blind_verified = 0``.
    """

    conn = _connect()
    proposal = _proposal(conn, status="rejected")
    _event(conn, proposal, kind="rejected", actor="owner-1", at=T0 - timedelta(hours=1))

    written = lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=proposal,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.NOT_RELEVANT,
        rationale="it was about a project I closed",
        blind_claimed=True,
        now=T0,
    )
    assert written.blind_claimed is True
    assert written.blind_verified is False
    assert written.blind_reason == "adjudicator_gave_prior_verdict"

    row = conn.execute("SELECT blind_claimed, blind_verified FROM dream_relevance_adjudications").fetchone()
    assert int(row["blind_claimed"]) == 1
    assert int(row["blind_verified"]) == 0


def test_lite_store_counts_only_the_latest_adjudication_per_proposal() -> None:
    """Append-only, superseded by id.  One owner changing his mind must not move the rate twice."""

    conn = _connect()
    proposal = _proposal(conn, status="accepted")
    first = lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=proposal,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.NOT_RELEVANT,
        now=T0,
    )
    lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=proposal,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.RELEVANT,
        supersedes_adjudication_id=first.adjudication_id,
        now=T0 + timedelta(hours=2),
    )
    assert conn.execute("SELECT COUNT(*) FROM dream_relevance_adjudications").fetchone()[0] == 2

    counts = lite_store.dream_counts_for_scope(conn, scope=_scope())
    assert counts.adjudications == 1
    assert counts.relevant == 1
    assert counts.not_relevant == 0
    assert da.dream_block(counts).false_positive_rate == 0.0  # a real measurement, from one judgement


def test_lite_store_end_to_end_reads_the_shape_the_report_prints() -> None:
    conn = _connect()
    rejected = _proposal(conn, status="rejected", surfaced=2, attested=1)
    _proposal(conn, status="snoozed", nonresponse="ignored", surfaced=3, attested=1)
    accepted = _proposal(
        conn,
        status="completed",
        surfaced=1,
        attested=1,
        completed_at=T0 - timedelta(days=DELIVERY_LOOKBACK_DAYS + 1),
    )
    _proposal(conn, status="proposed", project_id=None)  # a workspace proposal, out of this scope

    lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=rejected,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.RELEVANT,  # rejected, but a fair proposal: not a false positive
        blind_claimed=True,
        now=T0,
    )
    lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=accepted,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.NOT_RELEVANT,
        delivery_useful=DeliveryUsefulness.USEFUL,
        now=T0,
    )

    counts = lite_store.dream_counts_for_scope(conn, scope=_scope())
    assert counts.proposals == 3  # the workspace proposal is out of scope
    assert counts.by_status == {"rejected": 1, "snoozed": 1, "completed": 1}
    assert counts.nonresponse["ignored"] == 1
    assert counts.surfaced == 3
    assert counts.surfaced_attested == 3
    assert counts.completed_proposals == 1
    assert counts.blind_verified == 1
    assert counts.delivery_useful == 1

    block = da.dream_block(counts)
    assert block.false_positive_rate == 0.5  # one of two adjudications, and no rejection fed it
    assert block.delivery_rate == 1.0
    assert block.coverage_clause.state is ClauseState.SHORTFALL  # 2 < 20; still not enough evidence
    rendered = block.render()
    assert "adjudications: total=2  blind_verified=1  cannot_judge=0" in rendered
    assert "adjudicated_dreams: SHORTFALL" in rendered


def test_lite_store_stores_but_does_not_count_an_answer_inside_the_lookback() -> None:
    conn = _connect()
    proposal = _proposal(
        conn, status="completed", completed_at=T0 - timedelta(days=DELIVERY_LOOKBACK_DAYS - 2)
    )
    written = lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=proposal,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.RELEVANT,
        delivery_useful=DeliveryUsefulness.USEFUL,
        now=T0,
    )
    assert written.counted_for_delivery is False
    assert written.delivery_reason == "too_early"

    counts = lite_store.dream_counts_for_scope(conn, scope=_scope())
    assert counts.delivery_too_early == 1
    assert counts.delivery_useful == 0
    block = da.dream_block(counts)
    assert block.delivery_rate is None
    assert block.delivery_clause.reason == "no_adjudications"
    assert "1 answered inside the 30d lookback and not counted" in block.delivery_clause.detail


def test_lite_store_redacts_the_rationale_it_stores() -> None:
    conn = _connect()
    proposal = _proposal(conn)
    lite_store.record_adjudication(
        conn,
        scope=_scope(),
        proposal_id=proposal,
        adjudicator_id="owner-1",
        adjudicator_verified=True,
        relevance=RelevanceVerdict.CANNOT_JUDGE,
        rationale="ask joel@example.com about it",
        now=T0,
    )
    stored = conn.execute("SELECT rationale FROM dream_relevance_adjudications").fetchone()["rationale"]
    assert "joel@example.com" not in stored


def test_lite_store_refuses_to_adjudicate_a_proposal_outside_its_scope() -> None:
    """The store re-checks scope; it does not trust the route to have done it.

    An adjudication moves a metric, so an adjudication written from outside its own scope is a
    mis-attribution nothing downstream can detect.
    """

    conn = _connect()
    mine = _proposal(conn, project_id="proj_a")
    with pytest.raises(lite_store.ProposalNotInScope):
        lite_store.record_adjudication(
            conn,
            scope=_scope(project_id="proj_b"),
            proposal_id=mine,
            adjudicator_id="owner-1",
            adjudicator_verified=True,
            relevance=RelevanceVerdict.RELEVANT,
            now=T0,
        )
    assert conn.execute("SELECT COUNT(*) FROM dream_relevance_adjudications").fetchone()[0] == 0


def test_the_delivery_clock_cannot_be_supplied_by_a_caller() -> None:
    """``completed_at`` is read from P5's row.  There is no parameter to pass a flattering one."""

    import inspect

    for module in (lite_store, full_store):
        signature = inspect.signature(module.record_adjudication)
        assert "completed_at" not in signature.parameters


def _create_block(source: str, table: str) -> str:
    """The ``CREATE TABLE`` body for one table, whitespace-normalised."""

    start = source.index(f"CREATE TABLE IF NOT EXISTS {table}")
    end = source.index(")", source.index("schema_version", start))
    return " ".join(source[start:end].split())


def test_the_lite_ddl_is_the_same_text_in_db_py_and_in_the_store() -> None:
    """Lite's schema for this table is written twice; a drift between them is caught here.

    ``db.py``'s P6 block and ``DREAM_ADJUDICATION_DDL`` must agree column for column, type for
    type and default for default. They exist separately because ``db.py`` owns Lite's schema
    bootstrap and the store owns the table's contract; this test is what keeps that from becoming
    two schemas. The preferred fix, if either changes, is for ``db.py`` to call
    ``ensure_dream_adjudication_tables``.
    """

    db_source = (REPO_ROOT / "services" / "tce_lite_api" / "tce_lite_api" / "db.py").read_text(encoding="utf-8")
    assert _create_block(db_source, "dream_relevance_adjudications") == _create_block(
        lite_store.DREAM_ADJUDICATION_DDL[0], "dream_relevance_adjudications"
    )


def test_the_wire_response_carries_the_reason_so_a_false_is_never_a_bare_false() -> None:
    """An owner told his blindness claim was refused can see *why* it was refused."""

    from tce_shared.events import DreamRelevanceAdjudicationResponse

    fields = DreamRelevanceAdjudicationResponse.model_fields
    assert "blind_reason" in fields
    assert "delivery_reason" in fields


def test_every_reason_the_stores_emit_is_in_a_closed_list() -> None:
    """A free-text reason on the wire would be a fourth vocabulary; these come from two."""

    conn = _connect()
    rejected = _proposal(conn, status="rejected")
    _event(conn, rejected, kind="rejected", actor="someone-else", at=T0 - timedelta(hours=1))
    delivered = _proposal(conn, status="completed", completed_at=T0 - timedelta(days=1))

    for proposal, claimed, useful in (
        (rejected, True, None),
        (delivered, False, DeliveryUsefulness.USEFUL),
    ):
        written = lite_store.record_adjudication(
            conn,
            scope=_scope(),
            proposal_id=proposal,
            adjudicator_id="owner-1",
            adjudicator_verified=True,
            relevance=RelevanceVerdict.RELEVANT,
            blind_claimed=claimed,
            delivery_useful=useful,
            now=T0,
        )
        assert written.blind_reason in da.BLIND_REASONS
        assert written.delivery_reason in da.DELIVERY_REASONS
