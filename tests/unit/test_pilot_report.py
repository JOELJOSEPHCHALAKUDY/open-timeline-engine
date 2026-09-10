"""G1, G5, G7, G8, G9 — the P6 report tells the truth about an empty corpus, and cannot flatter one.

The P6 deliverable is not a result.  The pilot is calendar time and the owner is one person, so
what ships today is instrumentation, a runbook and a read-only report — and the report's job is to
say ``NOT ENOUGH EVIDENCE`` legibly, per project, per family, per arm, in the vocabulary P4's
promotion gate already uses.  These are the gates on that.

Five assertions, and each one is paired with a **discrimination twin** that runs the same scanner
over a synthetic violation and asserts it goes red.  A scanner that finds nothing is worth
something only if it can see something; three of these gates scan a file that does not exist yet,
so the twin is the only evidence today that the gate is not simply vacuous.

* **G1** — the report runs, exits 0 whatever the verdict, and exits **2** (never 0, never a
  traceback) when it could not be produced at all.  There is no exit code for *nothing qualified*:
  a gate that measured nothing must not read as a pass and must not read as a crash.
* **G5** — no clause reads an ``agent_asserted`` field.  ``pilot_episode_observations`` is the
  executor's own account of its own run, it lives in its own table precisely so that no clause
  function can reach it, and the report may not join it.
* **G7** — ``human_intervention_count`` and the cost fields acquire a reader.  Both are today
  declared-produced-unread; P6 either aggregates them or does not add a third such generation.
  A zero that means *we do not know* must never render as a zero that means *free*.
* **G8** — the two claims cannot be computed from one query, and ``ClaimBVerdict`` has nowhere to
  put a ``supported`` flag.
* **G9** — with no randomized human baseline the REDUCED SUPERVISION sentence is emitted verbatim,
  in the text and in ``--json`` alike, and it is still emitted when human rows exist but were
  *elected* rather than randomized.

THE SEAM this file uses.  ``scripts/p6_pilot_report.py`` splits loading from computing:

    def load_context(query: QueryFn, *, project: str | None = None) -> tuple[list[EpisodeFacts], dict]
    def build_report(episodes: Sequence[EpisodeFacts], **context: Any) -> dict[str, Any]
    def render_text(report: dict[str, Any]) -> str
    def main(argv: list[str] | None = None) -> int

So the clause cases run over ``EpisodeFacts`` with no database, and the SQL cases run
``load_context`` over ``_FakeDatabase`` — which answers exactly the statements the report is
known to issue and **raises on any other**, so a query the report grows (a join onto
``pilot_episode_observations``, say) fails here rather than going unexercised.

The computation itself belongs to ``tce_shared.pilot_enrollment``: the script renders
``Clause.render()`` and ``ClaimAVerdict.to_payload()`` / ``ClaimBVerdict.to_payload()`` rather
than re-implementing the clause text, so ``GET /v1/pilot/report`` and this script stay one
computation with two renderers.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from tce_shared.pilot_enrollment import (
    NO_ENROLLED_EPISODES_SENTENCE,
    POOLING_REFUSED_SENTENCE,
    RANDOMIZED_ARMS,
    REDUCED_SUPERVISION_SENTENCE,
    SINGLE_PARTICIPANT_SENTENCE,
    AllocationKind,
    ClaimBVerdict,
    ClauseState,
    CloseFacts,
    CompletionBasis,
    EpisodeFacts,
    PilotArm,
    RescueLevel,
    ReviewVerdict,
    arm_class_of,
    claim_b,
    cost_clause,
    human_intervention_clause,
)
from tce_shared.pilot_thresholds import MIN_ACTIVE_DAYS

REPO = Path(__file__).resolve().parents[2]
REPORT_PATH = REPO / "scripts" / "p6_pilot_report.py"

_ABSENT = (
    "scripts/p6_pilot_report.py does not exist. This gate is LIVE: it fails until Builder F's "
    "report half lands. The contract it must satisfy is stated in this module's docstring — "
    "build_report(episodes, **context) -> dict, render_text(report) -> str, main(argv) -> int."
)

# The executor's own account of its own run.  None of these may appear anywhere in the report.
AGENT_ASSERTED = (
    "pilot_episode_observations",
    "agent_notes",
    "agent_declared_steps_json",
    "agent_declared_steps",
    "agent_self_rated_difficulty",
    "agent_confidence",
)

# §2.3 / §2.4: the three columns that today have a writer, a one-row reader and no aggregator.
ORPHAN_COLUMNS = ("human_intervention_count", "cost_minor_units", "cost_source")


# --------------------------------------------------------------------------------------
# Loading the artifact under test
# --------------------------------------------------------------------------------------


def _report_source() -> str:
    if not REPORT_PATH.exists():
        pytest.fail(_ABSENT)
    return REPORT_PATH.read_text(encoding="utf-8")


def _report_module() -> ModuleType:
    source = _report_source()
    spec = importlib.util.spec_from_file_location("p6_pilot_report_under_test", REPORT_PATH)
    assert spec is not None and spec.loader is not None, REPORT_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("build_report", "render_text", "main"):
        if not callable(getattr(module, name, None)):
            pytest.fail(f"scripts/p6_pilot_report.py defines no callable {name!r}; see this module's docstring for the contract. source_len={len(source)}")
    return module


def _string_literals(source: str) -> Iterator[str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _docstrings(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(first.value.value)
    return found


# --------------------------------------------------------------------------------------
# The scanners, as pure predicates over source text.  Each is applied twice: to the real
# artifact (the gate) and to a synthetic violation (the discrimination twin).
# --------------------------------------------------------------------------------------


def scan_agent_assertions(source: str) -> list[str]:
    """G5.  Every reference to the agent's own account of itself, wherever it hides."""

    docstrings = _docstrings(source)
    offences: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in AGENT_ASSERTED:
            offences.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.Name) and node.id in AGENT_ASSERTED:
            offences.append(f"line {node.lineno}: {node.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings:
            for forbidden in AGENT_ASSERTED:
                if forbidden in node.value:
                    offences.append(f"line {node.lineno}: string literal names {forbidden}")
    return offences


def scan_orphan_readers(source: str) -> list[str]:
    """G7.  Which of the three orphan columns the report's SQL actually selects."""

    blob = "\n".join(_string_literals(source))
    return [column for column in ORPHAN_COLUMNS if column not in blob]


def scan_unseparated_claims(source: str) -> list[str]:
    """G8.  A SELECT over ``pilot_episodes`` that neither filters nor groups by ``arm_class``."""

    offences: list[str] = []
    for literal in _string_literals(source):
        if "pilot_episodes" not in literal:
            continue
        if not re.search(r"\bSELECT\b", literal, re.IGNORECASE):
            continue
        if "arm_class" not in literal:
            offences.append(" ".join(literal.split())[:120])
    return offences




# --------------------------------------------------------------------------------------
# The corpus.  Two shapes: EpisodeFacts for the clause cases, rows for the SQL cases.
# --------------------------------------------------------------------------------------

ALL_TABLES = ("pilot_strata", "pilot_episodes", "pilot_episode_closes", "dream_proposals", "dream_relevance_adjudications", "dispatch_records")


class _FakeDatabase:
    """The report's ``QueryFn``, answering exactly the statements the report is known to issue."""

    def __init__(
        self,
        *,
        tables: Sequence[str] = ALL_TABLES,
        head: str = "20260909_0043",
        episodes: Sequence[dict[str, Any]] = (),
        strata: Sequence[dict[str, Any]] = (),
        dispatch: Sequence[dict[str, Any]] = (),
        dream_status: Sequence[dict[str, Any]] = (),
        dream_adjudications: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.tables = tuple(tables)
        self.head = head
        self.episodes = list(episodes)
        self.strata = list(strata)
        self.dispatch = list(dispatch)
        self.dream_status = list(dream_status)
        self.dream_adjudications = list(dream_adjudications)
        self.seen: list[str] = []

    def __call__(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self.seen.append(" ".join(sql.split()))
        if "information_schema.tables" in sql:
            wanted = list(params[0]) if params else []
            return [{"table_name": name} for name in self.tables if name in wanted]
        if "alembic_version" in sql:
            return [{"version_num": self.head}]
        if "FROM pilot_episodes" in sql:
            arm_class = str(params[0]) if params else ""
            return [dict(row) for row in self.episodes if row["arm_class"] == arm_class]
        if "FROM pilot_strata" in sql:
            return [dict(row) for row in self.strata]
        if "FROM dispatch_records" in sql:
            return [dict(row) for row in self.dispatch]
        if "FROM dream_proposals" in sql:
            return [dict(row) for row in self.dream_status]
        if "FROM dream_relevance_adjudications" in sql:
            return [dict(row) for row in self.dream_adjudications]
        raise AssertionError(f"the report issued a statement this gate does not model: {sql.strip()[:200]}")


def _close(arm: PilotArm, *, when: datetime) -> CloseFacts:
    return CloseFacts(
        adjudication_independent=True,
        executed_arm=arm,
        deviated=False,
        rescue_level=RescueLevel.NONE,
        completion_basis=CompletionBasis.VERIFIED,
        review_verdict=ReviewVerdict.ACCEPTED_AS_IS,
        closed_at=when + timedelta(hours=4),
        review_minutes=11,
    )


def _facts(
    arm: PilotArm,
    *,
    index: int = 0,
    kind: AllocationKind = AllocationKind.RANDOMIZED,
    closed: bool = True,
    project: str = "proj_a",
    family: str = "needs_human",
) -> EpisodeFacts:
    allocated = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(days=index)
    return EpisodeFacts(
        episode_id=f"ep-{arm.value}-{index}",
        project_id=project,
        decision_family=family,
        arm_id=arm,
        arm_class=arm_class_of(arm),
        allocation_kind=kind,
        stratum_id="stratum-a",
        slot=index,
        block_ordinal=index // 3,
        allocation_salt_sha256="salt-a",
        arm_set_sha="arms-a",
        allocated_at=allocated,
        close=_close(arm, when=allocated) if closed else None,
    )


def _episode_row(arm: PilotArm, *, index: int = 0, kind: AllocationKind = AllocationKind.RANDOMIZED, project: str = "proj_a") -> dict[str, Any]:
    """The same episode as a database row, for the cases that exercise the SQL seam."""

    allocated = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(days=index)
    return {
        "id": f"ep-{arm.value}-{index}",
        "workspace_id": "personal",
        "subject_user_id": "owner-1",
        "project_id": project,
        "decision_family": "needs_human",
        "session_id": f"sess-{index}",
        "arm_id": arm.value,
        "arm_class": arm_class_of(arm),
        "allocation_kind": kind.value,
        "stratum_id": "stratum-a",
        "slot": index,
        "block_ordinal": index // 3,
        "allocation_salt_sha256": "salt-a",
        "arm_set_sha": "arms-a",
        "allocated_at": allocated,
        "adjudication_independent": True,
        "executed_arm": arm.value,
        "deviated": False,
        "rescue_level": RescueLevel.NONE.value,
        "completion_basis": CompletionBasis.VERIFIED.value,
        "review_verdict": ReviewVerdict.ACCEPTED_AS_IS.value,
        "review_minutes": 11,
        "late_close": False,
        "closed_at": allocated + timedelta(hours=4),
    }


def _elected_human_facts() -> list[EpisodeFacts]:
    """Human-workflow episodes that exist but were CHOSEN.  Descriptive, never a baseline."""

    return [_facts(PilotArm.OWNER_UNASSISTED, index=index, kind=AllocationKind.ELECTED) for index in range(3)]


# --------------------------------------------------------------------------------------
# G1 — legible at zero, exit 2 when it could not be produced at all
# --------------------------------------------------------------------------------------


def test_report_is_legible_at_zero() -> None:
    """G1.  The empty corpus is the corpus. It must read as a refusal, not as a zero."""

    module = _report_module()
    report = module.build_report([])
    text = module.render_text(report)

    assert NO_ENROLLED_EPISODES_SENTENCE in text, text
    assert POOLING_REFUSED_SENTENCE in text, "the refusal to pool is unconditional, not printed only when there are cells"
    assert SINGLE_PARTICIPANT_SENTENCE in text, "one owner participated; that caveat is not conditional on the verdict"
    assert f"active_days=0/{MIN_ACTIVE_DAYS}" in text, "the window clock prints the floor beside the count"
    assert "NOT ENOUGH EVIDENCE" in text, text

    blob = json.dumps(report, sort_keys=True, default=str)
    assert '"supported": true' not in blob.lower(), "an empty corpus supported a claim"
    assert report["claim_a_supported_cells"] == [], report["claim_a_supported_cells"]
    assert report["worst_cell"] is None, "there is no worst cell when there are no cells"
    assert report["dreams"]["computable"] is False, "no database was read, so the dream block is NOT_COMPUTABLE"


def test_missing_tables_exit_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """G1's other half.  ``could not be produced`` is exit 2 — not 0, not 1, not a traceback.

    Three arms, because all three are the same failure to a reader: a database at the wrong
    alembic head, no database configured at all, and a database that will not answer. Exit 1
    would be indistinguishable from an unhandled exception; exit 0 would let a weekly cron report
    a green pilot that never ran.
    """

    module = _report_module()

    with pytest.raises(LookupError) as absent:
        module.load_context(_FakeDatabase(tables=("dispatch_records",), head="20260909_0041"))
    message = str(absent.value)
    assert "20260909_0043" in message and "not a pass" in message, message
    assert "pilot_episodes" in message, message

    monkeypatch.delenv("TCE_DATABASE_URL", raising=False)
    assert _exit_code(module) == 2, "with TCE_DATABASE_URL unset the report must exit 2"

    # Loopback, closed port: reachable host, unreachable database.
    monkeypatch.setenv("TCE_DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:1/tce")
    assert _exit_code(module) == 2, "an unreachable database must exit 2, not raise"


def test_the_report_exits_zero_when_it_was_produced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity for the two exit-2 arms above: exit 2 is a state, not the only state.

    There is no exit code for *nothing qualified*. A corpus of three closed episodes falls far
    short of every floor and the command still exits 0, because a shortfall is a report and not
    a crash.
    """

    module = _report_module()
    database = _FakeDatabase(episodes=[_episode_row(arm, index=index) for index, arm in enumerate(RANDOMIZED_ARMS)])
    monkeypatch.setattr(module, "_connect_query", lambda: database)
    assert _exit_code(module) == 0, "a corpus that measures almost nothing still produces a report"


def _exit_code(module: ModuleType) -> int:
    try:
        result = module.main([])
    except SystemExit as exit_signal:  # a bare ``raise SystemExit("message")`` exits 1
        code = exit_signal.code
        return code if isinstance(code, int) else 1
    except Exception as error:  # noqa: BLE001 - a traceback IS the failure being asserted against
        pytest.fail(f"the report raised {type(error).__name__} instead of exiting: {error}")
    return int(result)


# --------------------------------------------------------------------------------------
# G5 — no clause reads an agent_asserted field
# --------------------------------------------------------------------------------------


def test_no_clause_reads_agent_assertions() -> None:
    """G5.  The party under test does not grade itself, enforced in the report as well as the type."""

    offences = scan_agent_assertions(_report_source())
    assert not offences, (
        "the report reaches the executor's own account of its own run:\n  " + "\n  ".join(offences)
    )

    # And at run time: the report never asks for the observations table at all.
    database = _FakeDatabase(episodes=[_episode_row(arm, index=index) for index, arm in enumerate(RANDOMIZED_ARMS)])
    module = _report_module()
    episodes, context = module.load_context(database)
    assert database.seen, "the report issued no statements at all"
    assert not any("pilot_episode_observations" in statement for statement in database.seen), database.seen
    assert len(episodes) == len(RANDOMIZED_ARMS), "the loader read nothing; the scan above would be vacuous"
    assert context["alembic_head"] == "20260909_0043", context


def test_the_agent_assertion_scanner_discriminates() -> None:
    """G5's twin.  The scanner above is only evidence if it can see a violation."""

    assert scan_agent_assertions('ROWS = "SELECT agent_confidence FROM pilot_episode_observations"')
    assert scan_agent_assertions("def clause(row):\n    return row.agent_self_rated_difficulty\n")
    assert not scan_agent_assertions('ROWS = "SELECT arm_class FROM pilot_episodes"')
    # A docstring may NAME the forbidden table — that is how the rule is documented.
    assert not scan_agent_assertions('"""pilot_episode_observations is diagnostic and unread."""\nX = 1\n')


# --------------------------------------------------------------------------------------
# G7 — the orphan columns acquire a reader, and a zero denominator never renders as zero
# --------------------------------------------------------------------------------------


def test_cost_and_intervention_have_a_reader() -> None:
    """G7.  Declared-produced-unread is the pattern P6 was told not to add a third generation of."""

    missing = scan_orphan_readers(_report_source())
    assert not missing, (
        f"the report's SQL never selects {missing}. Each already has a writer and no aggregator; "
        "P6 either reads them or has added another orphan field."
    )

    module = _report_module()
    text = module.render_text(module.build_report([]))
    for label in ("cost", "human_intervention"):
        line = next((row.strip() for row in text.splitlines() if row.strip().startswith(f"{label}:")), "")
        assert line, f"the report prints no {label!r} line at all"
        assert "NOT_COMPUTABLE" in line, f"{line!r} — zero matched dispatch rows must render NOT_COMPUTABLE"
        assert not re.match(rf"^{label}:\s*0\b", line), f"{line!r} — a zero that means 'we do not know' rendered as a zero that means 'free'"

    # The whole path, from the dispatch SQL to the clause: a readable cost moves the clause off
    # NOT_COMPUTABLE, so the refusal above is a fact about the corpus and not a clause that can
    # only refuse.
    rows = [_episode_row(arm, index=index) for index, arm in enumerate(RANDOMIZED_ARMS)]
    dispatch_rows = [
        {
            "workspace_id": "personal",
            "session_id": row["session_id"],
            "surface": "codex/exec",
            "cost_minor_units": 250,
            "cost_source": "provider_reported",
            "human_intervention_count": 2,
        }
        for row in rows
    ]
    episodes, context = module.load_context(_FakeDatabase(episodes=rows, dispatch=dispatch_rows))
    report = module.build_report(episodes, **context)
    assert report["cost"]["state"] == ClauseState.PASS.value, report["cost"]
    assert report["human_intervention"]["state"] == ClauseState.PASS.value, report["human_intervention"]
    assert report["cost_known"] == len(rows) and report["intervention_known"] == len(rows), report


def test_a_zero_denominator_is_not_computable_in_the_shared_arithmetic() -> None:
    """G7's twin, and the half that runs today: the clause itself refuses, so no renderer can lie."""

    assert cost_clause(cost_known=0, episodes=0).state is ClauseState.NOT_COMPUTABLE
    assert cost_clause(cost_known=0, episodes=5).state is ClauseState.NOT_COMPUTABLE
    assert cost_clause(cost_known=3, episodes=5).state is ClauseState.PASS
    assert human_intervention_clause(known=0, episodes=0).state is ClauseState.NOT_COMPUTABLE
    assert human_intervention_clause(known=2, episodes=5).state is ClauseState.PASS
    assert "NOT_COMPUTABLE" in cost_clause(cost_known=0, episodes=0).render()


def test_the_orphan_reader_scanner_discriminates() -> None:
    """G7's twin.  Deleting the SQL that reads the column must go red."""

    good = 'SQL = "SELECT human_intervention_count, cost_minor_units, cost_source FROM dispatch_records"'
    assert not scan_orphan_readers(good)
    assert scan_orphan_readers('SQL = "SELECT cost_minor_units, cost_source FROM dispatch_records"') == ["human_intervention_count"]
    assert sorted(scan_orphan_readers('SQL = "SELECT 1"')) == sorted(ORPHAN_COLUMNS)


# --------------------------------------------------------------------------------------
# G8 — the two claims cannot be computed from one query
# --------------------------------------------------------------------------------------


def test_claims_are_structurally_separate() -> None:
    """G8.  Claim A reads ``runtime`` rows; Claim B reads ``human_workflow`` rows; never one query."""

    offences = scan_unseparated_claims(_report_source())
    assert not offences, (
        "a SELECT over pilot_episodes neither filters nor groups by arm_class, so one query "
        "returns both claims' rows:\n  " + "\n  ".join(offences)
    )

    assert not hasattr(ClaimBVerdict, "supported"), "ClaimBVerdict acquired a supported attribute"
    assert "supported" not in ClaimBVerdict.__annotations__, "ClaimBVerdict acquired a supported field"
    assert "supported" not in claim_b([]).to_payload(), "the Claim B payload acquired a supported key"

    # At run time: every statement that touched pilot_episodes carried an arm_class filter, and
    # the two claims were served by two separate calls.
    module = _report_module()
    database = _FakeDatabase(episodes=[_episode_row(arm, index=index) for index, arm in enumerate(RANDOMIZED_ARMS)])
    module.load_context(database)
    touching = [statement for statement in database.seen if "FROM pilot_episodes" in statement]
    assert len(touching) >= 2, f"the two claims were served by {len(touching)} query/queries: {touching}"
    assert all("arm_class = %s" in statement for statement in touching), touching


def test_the_claim_separation_scanner_discriminates() -> None:
    """G8's twin."""

    assert scan_unseparated_claims('SQL = "SELECT id, arm_id FROM pilot_episodes WHERE workspace_id = %s"')
    assert not scan_unseparated_claims('SQL = "SELECT id FROM pilot_episodes WHERE arm_class = %s"')
    assert not scan_unseparated_claims('SQL = "SELECT arm_class, count(*) FROM pilot_episodes GROUP BY arm_class"')
    assert not scan_unseparated_claims('SQL = "INSERT INTO pilot_episodes (id) VALUES (%s)"')


# --------------------------------------------------------------------------------------
# G9 — REDUCED SUPERVISION, verbatim, in both renderings
# --------------------------------------------------------------------------------------


def test_reduced_supervision_sentence_is_emitted() -> None:
    """G9.  No adequate human baseline was collected, and the report says exactly that.

    Twice: over a corpus with no human rows at all, and over one with human rows that were
    ELECTED.  The second is the case that would otherwise read as a baseline — the owner did
    some work himself, so the rows exist — and it is the one the sentence exists for.
    """

    module = _report_module()
    for label, episodes in (("empty", []), ("elected human rows", _elected_human_facts())):
        report = module.build_report(episodes)
        text = module.render_text(report)
        blob = json.dumps(report, sort_keys=True, default=str)
        assert REDUCED_SUPERVISION_SENTENCE in text, f"{label}: the text rendering dropped the sentence"
        assert REDUCED_SUPERVISION_SENTENCE in blob, f"{label}: --json dropped the sentence"
        assert report["claim_b"]["state"] == "not_computable", f"{label}: {report['claim_b']}"

    # The elected corpus is not silently empty: the rows arrived and were counted as elected.
    counted = module.build_report(_elected_human_facts())["claim_b"]
    assert counted["elected"] == 3 and counted["randomized"] == 0, counted


def test_the_sentence_travels_with_the_verdict_not_with_the_renderer() -> None:
    """G9's twin, and the half that runs today.

    The sentence is emitted by ``claim_b`` itself rather than by a renderer, so a second renderer
    cannot forget it.  If this ever fails, G9 above has become a test of one renderer's politeness.
    """

    assert claim_b([]).sentence == REDUCED_SUPERVISION_SENTENCE
    assert REDUCED_SUPERVISION_SENTENCE in json.dumps(claim_b([]).to_payload())


# --------------------------------------------------------------------------------------
# The report never pools, and the summary names the worst cell
# --------------------------------------------------------------------------------------


def test_the_report_names_the_worst_cell_and_refuses_to_pool() -> None:
    """C10.  Two projects with different evidence must not average into one number."""

    module = _report_module()
    episodes = [_facts(arm, index=index, project="proj_a") for index, arm in enumerate(RANDOMIZED_ARMS)]
    episodes += [_facts(arm, index=index + 10, project="proj_b", closed=False) for index, arm in enumerate(RANDOMIZED_ARMS)]
    report = module.build_report(episodes)
    text = module.render_text(report)

    assert POOLING_REFUSED_SENTENCE in text
    assert "proj_a" in text and "proj_b" in text, "each cell is printed; neither is averaged away"
    assert len(report["claim_a_cells"]) == 2, report["claim_a_cells"]
    assert report["worst_cell"] is not None and report["worst_cell"]["supported"] is False
    assert report["worst_cell"]["project_id"] == "proj_b", f"{report['worst_cell']} — the cell that closed nothing is the worst one"


def test_there_is_no_flag_that_pools() -> None:
    """A flag that widens the cell axis is the one flag this report must not grow."""

    module = _report_module()
    for flag in ("--pool", "--all-projects", "--aggregate", "--mean"):
        with pytest.raises(SystemExit) as refusal:
            module.main([flag])
        assert refusal.value.code == 2, f"{flag} was accepted: {refusal.value.code}"


def test_the_corpus_helpers_build_what_they_claim() -> None:
    """Non-vacuity for every case above that injects a corpus: none of them is silently empty."""

    elected = _elected_human_facts()
    assert len(elected) == 3
    assert {episode.arm_class for episode in elected} == {"human_workflow"}
    assert {episode.allocation_kind for episode in elected} == {AllocationKind.ELECTED}
    runtime = _facts(PilotArm.TCE_ASSISTED)
    assert runtime.arm_class == "runtime" and runtime.close is not None
    assert _episode_row(PilotArm.TCE_ASSISTED)["arm_class"] == "runtime"
    with pytest.raises(AssertionError, match="does not model"):
        _FakeDatabase()("SELECT * FROM pilot_episode_observations")
