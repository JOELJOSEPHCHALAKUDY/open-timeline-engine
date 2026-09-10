#!/usr/bin/env python3
"""Read the P6 operational-proof pilot's state and say, legibly, how much evidence exists.

This command **ships no result**.  The pilot it reads is calendar time that no implementation can
compress -- four to six weeks is a *minimum observation window, not a completion date* -- and on
this corpus the binding constraint is not the calendar at all but owner activity: six active days
in the last 196, four in the last 61.  So what this command produces today is a **recorded
refusal**: `NOT ENOUGH EVIDENCE`, per project, per decision family, per arm, with the clause that
fell short named in words.  That is the deliverable.  A verdict manufactured out of an empty
corpus would be the defect, not the feature.

Four rules are load-bearing here, and each of them is a rule this repository learned by finding
its own violation:

**The party under test does not grade itself.**  Every P6 field carries one of three producer
classes -- `server_derived`, `human_attested`, `agent_asserted` -- and no clause on this report
reads the third.  The owner's own review minutes are adjudication and are read; an executor's
account of its own run is diagnostic and is not.  The table holding that account is not queried
here and not named here, because a reader that cannot reach a field cannot be tempted by it.

**No metric reports success without evidence.**  Every clause resolves to exactly one of `PASS`,
`SHORTFALL(measured, floor)` or `NOT_COMPUTABLE(reason)`, and a zero denominator resolves to the
third.  `0.0` is never printed as a measurement.  The precedent is the defect this phase deleted:
the existing behaviour pilot reported `safety_passed=True` over zero completed trials.

**Claim B is structurally unreportable without a human baseline.**  `ClaimBVerdict` has no field
named `supported`, and the REDUCED SUPERVISION sentence is emitted by `claim_b` itself rather than
by this renderer, so it travels into `--json` and into the text alike.  An allocator cannot
randomise a human into doing the work himself; until a genuinely randomised human-baseline block
exists, the honest word is *reduced supervision* and it is the only word available.

**Unlike projects are never pooled.**  The cell is `(project_id, decision_family, arm_id)`.  There
is no cross-project average, no cross-family average and no "overall" number.  The summary names
the **worst** cell.  `--project` narrows; nothing widens; `--pool` is refused by name.

One owner participated in this pilot.  Every statement this report can ever make applies to that
owner and to the projects actually observed, and generalises to nobody else -- printed on every
run, unconditionally, because a caveat that disappears when the numbers look good is not a caveat.

This command is read-only.  It writes no row, creates no qualification record, and grants no
authority: no promotion, exposure, personalization or permit path reads it (asserted by AST walk
in `tests/unit/test_pilot_authority_fence.py`).  It is the same fence `p4_design.md` R11 puts
around `behavior_pilot_status`, borrowed together with the vocabulary.

Usage:

    TCE_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/tce \
      PYTHONPATH=shared .venv/bin/python scripts/p6_pilot_report.py [--json] [--project X]

Exit code is **0** when the report was produced, whatever the verdict -- `NOT ENOUGH EVIDENCE` is
a produced report.  It is **2** only when the report could not be produced at all: no database, an
unreachable database, or the pilot tables absent.  There is no exit code for *nothing qualified*,
because a gate that measured nothing must read neither as a pass nor as a crash.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

# `scripts/` is protected in the project CLAUDE.md; the owner granted an explicit override for
# THIS FILE as the P6 deliverable.  Nothing outside `scripts/` is touched from here.
from tce_shared.aspirations import DreamProposalStatus, NonresponseState
from tce_shared.pilot_enrollment import (
    ARM_CLASS_HUMAN_WORKFLOW,
    ARM_CLASS_RUNTIME,
    EXCLUSION_KEYS,
    NO_ENROLLED_EPISODES_SENTENCE,
    POOLING_REFUSED_SENTENCE,
    RANDOMIZED_ARMS,
    SINGLE_PARTICIPANT_SENTENCE,
    SUCCESS_DEFINITION,
    AllocationKind,
    ClaimAVerdict,
    ClaimBVerdict,
    CloseFacts,
    CompletionBasis,
    CostSource,
    EpisodeFacts,
    PilotArm,
    RescueLevel,
    ReviewVerdict,
    claim_a,
    claim_b,
    clause_not_computable,
    clause_pass,
    cost_clause,
    false_positive_relevance,
    human_intervention_clause,
    window_clock,
    worst_cell,
)
from tce_shared.pilot_thresholds import (
    DELIVERY_LOOKBACK_DAYS,
    MIN_ACTIVE_DAYS,
    MIN_CLOSE_COVERAGE,
    MIN_CLOSED_PER_ARM,
    P6_THRESHOLDS_EFFECTIVE_AT,
    P6_THRESHOLDS_SHA,
    P6_THRESHOLDS_VERSION,
    enrolments_required_per_cell,
)

QueryFn = Callable[..., list[dict[str, Any]]]

# The bare table NAMES below are an existence probe, not SQL: they carry no SELECT and no FROM.
# The one SQL literal in this file that reads `pilot_episodes` is `_EPISODES_SQL`, and it filters
# `arm_class = ` (G8) so that no single query can ever return both claims' rows.
#: The five tables `20260909_0043` creates.  The first three are required for the report to mean
#: anything; their absence is the one condition that exits 2 rather than reporting a shortfall.
REQUIRED_TABLES: tuple[str, ...] = ("pilot_strata", "pilot_episodes", "pilot_episode_closes")
DREAM_TABLES: tuple[str, ...] = ("dream_proposals", "dream_relevance_adjudications")

#: The density line §4.4 exists to make unmissable.  Measured on this corpus, printed every run.
_DENSITY_NOTE = (
    "at the density measured on this corpus -- 4 active days in the last 61 -- 28 active days is "
    "roughly 427 calendar days. The 4-6 week figure in the plan is a MINIMUM observation window, "
    "not a completion date."
)

# --------------------------------------------------------------------------------------
# Database access.  Read-only: every statement below is a SELECT.
# --------------------------------------------------------------------------------------

_EPISODES_SQL = """
    SELECT e.id,
           e.workspace_id,
           e.subject_user_id,
           e.project_id,
           e.decision_family,
           e.session_id,
           e.arm_id,
           e.arm_class,
           e.allocation_kind,
           e.stratum_id,
           e.slot,
           e.block_ordinal,
           e.allocation_salt_sha256,
           e.arm_set_sha,
           e.allocated_at,
           c.adjudication_independent,
           c.executed_arm,
           c.deviated,
           c.rescue_level,
           c.completion_basis,
           c.review_verdict,
           c.review_minutes,
           c.late_close,
           c.closed_at
      FROM pilot_episodes e
      LEFT JOIN pilot_episode_closes c ON c.episode_id = e.id
     WHERE e.arm_class = %s
       {project_filter}
     ORDER BY e.allocated_at
"""

# The two claims cannot be computed from one query: this literal filters `arm_class = ` and is
# called once for 'runtime' (Claim A) and once for 'human_workflow' (Claim B).  There is no
# query in this file that returns both (G8).

_STRATA_SQL = "SELECT stratum_id, decision_family, project_id, next_slot FROM pilot_strata"

# G7: `human_intervention_count`, `cost_minor_units` and `cost_source` acquire their first
# aggregator in this repository here.  The join key is the episode's session; a cell with no
# matched dispatch row renders NOT_COMPUTABLE and never 0.
_DISPATCH_SQL = """
    SELECT d.workspace_id,
           d.session_id,
           d.surface,
           d.cost_minor_units,
           d.cost_source,
           d.human_intervention_count
      FROM dispatch_records d
     WHERE d.session_id <> ''
"""

_DREAM_STATUS_SQL = """
    SELECT p.project_id,
           p.status,
           p.nonresponse_state,
           COUNT(*) AS n,
           SUM(CASE WHEN p.surfaced_count > 0 THEN 1 ELSE 0 END) AS surfaced,
           SUM(CASE WHEN p.surfaced_attested THEN 1 ELSE 0 END) AS surfaced_attested,
           SUM(CASE WHEN p.completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed
      FROM dream_proposals p
     GROUP BY p.project_id, p.status, p.nonresponse_state
"""

# Relevance comes from adjudications and from nothing else.  A `rejected` status is a preference;
# a `not_relevant` adjudication is a claim about the proposal's fit, and the second is never
# derived from the first -- so `dream_proposals.status` appears nowhere in this literal (G11).
_DREAM_ADJUDICATION_SQL = """
    SELECT a.project_id,
           a.relevance,
           a.blind_verified,
           a.delivery_useful,
           a.counted_for_delivery,
           COUNT(*) AS n
      FROM dream_relevance_adjudications a
     GROUP BY a.project_id, a.relevance, a.blind_verified, a.delivery_useful, a.counted_for_delivery
"""


def _database_url() -> str:
    """Fail loud rather than vacuously, exactly as `test_policy_parity.py::_database_url` does."""

    url = os.environ.get("TCE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "TCE_DATABASE_URL is unset. This report reads the live corpus; running it against "
            "nothing would print a vacuous NOT ENOUGH EVIDENCE that looks exactly like a real one."
        )
    return url.replace("postgresql+psycopg://", "postgresql://")


def _connect_query() -> QueryFn:
    import psycopg

    url = _database_url()

    def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(url) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            columns = [description[0] for description in (cursor.description or [])]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    return query


def _tables_present(query: QueryFn, names: Sequence[str]) -> set[str]:
    rows = query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(names),),
    )
    return {str(row["table_name"]) for row in rows}


def _alembic_head(query: QueryFn) -> str:
    try:
        rows = query("SELECT version_num FROM alembic_version")
    except Exception:  # noqa: BLE001 - a database without alembic_version is still reportable
        return "unknown"
    return ", ".join(sorted(str(row["version_num"]) for row in rows)) or "unknown"


# --------------------------------------------------------------------------------------
# Row -> facts.  A row that does not parse is counted, never guessed at.
# --------------------------------------------------------------------------------------


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _arm_or_none(value: Any) -> PilotArm | None:
    try:
        return PilotArm(str(value))
    except ValueError:
        return None


def _close_facts(row: dict[str, Any]) -> CloseFacts | None:
    """The adjudication, or None when there is no close record **or** the record is malformed.

    A malformed close is never repaired with a default here.  Defaulting `rescue_level` to
    `none` or `review_verdict` to `accepted_as_is` would silently manufacture a success out of a
    row nobody can read; the honest handling is to leave the episode unclosed and count it.
    """

    closed_at = _aware(row.get("closed_at"))
    if closed_at is None:
        return None
    executed = _arm_or_none(row.get("executed_arm"))
    if executed is None:
        return None
    try:
        rescue = RescueLevel(str(row.get("rescue_level")))
        basis = CompletionBasis(str(row.get("completion_basis")))
        verdict = ReviewVerdict(str(row.get("review_verdict")))
    except ValueError:
        return None
    minutes = row.get("review_minutes")
    return CloseFacts(
        adjudication_independent=bool(row.get("adjudication_independent")),
        executed_arm=executed,
        deviated=bool(row.get("deviated")),
        rescue_level=rescue,
        completion_basis=basis,
        review_verdict=verdict,
        closed_at=closed_at,
        review_minutes=int(minutes) if isinstance(minutes, int) else None,
        late_close=bool(row.get("late_close")),
    )


def _episode_facts(rows: Sequence[dict[str, Any]]) -> tuple[list[EpisodeFacts], int, int]:
    """`(facts, unreadable_allocations, unreadable_closes)` -- both counters are printed."""

    facts: list[EpisodeFacts] = []
    bad_allocation = 0
    bad_close = 0
    for row in rows:
        arm = _arm_or_none(row.get("arm_id"))
        allocated_at = _aware(row.get("allocated_at"))
        if arm is None or allocated_at is None:
            bad_allocation += 1
            continue
        try:
            kind = AllocationKind(str(row.get("allocation_kind")))
        except ValueError:
            bad_allocation += 1
            continue
        close = _close_facts(row)
        if close is None and row.get("closed_at") is not None:
            bad_close += 1
        facts.append(
            EpisodeFacts(
                episode_id=str(row["id"]),
                project_id=None if row.get("project_id") in (None, "") else str(row["project_id"]),
                decision_family=str(row.get("decision_family") or "unclassified"),
                arm_id=arm,
                arm_class=str(row.get("arm_class") or ARM_CLASS_RUNTIME),
                allocation_kind=kind,
                stratum_id=str(row.get("stratum_id") or ""),
                slot=int(row.get("slot") or -1),
                block_ordinal=int(row.get("block_ordinal") or -1),
                allocation_salt_sha256=str(row.get("allocation_salt_sha256") or ""),
                arm_set_sha=str(row.get("arm_set_sha") or ""),
                allocated_at=allocated_at,
                close=close,
            )
        )
    return facts, bad_allocation, bad_close


def _load_episodes(query: QueryFn, arm_class: str, project: str | None) -> list[dict[str, Any]]:
    if project:
        return query(_EPISODES_SQL.format(project_filter="AND e.project_id = %s"), (arm_class, project))
    return query(_EPISODES_SQL.format(project_filter=""), (arm_class,))


# --------------------------------------------------------------------------------------
# Cost and human intervention.  Both are NOT_COMPUTABLE on this host, by protocol, and the
# reason is printed rather than the number.
# --------------------------------------------------------------------------------------


def _dispatch_by_session(query: QueryFn, present: set[str]) -> tuple[dict[tuple[str, str], dict[str, int]], str]:
    """Per `(workspace, session)`: how many dispatch rows, how many carry a usable cost."""

    if "dispatch_records" not in present:
        return {}, "dispatch_records is not present in this database"
    index: dict[tuple[str, str], dict[str, int]] = {}
    surfaces: set[str] = set()
    for row in query(_DISPATCH_SQL):
        key = (str(row.get("workspace_id") or ""), str(row.get("session_id") or ""))
        bucket = index.setdefault(key, {"rows": 0, "cost_known": 0, "intervention_known": 0})
        bucket["rows"] += 1
        surfaces.add(str(row.get("surface") or "unknown"))
        source = str(row.get("cost_source") or CostSource.UNAVAILABLE.value)
        if source in {CostSource.PROVIDER_REPORTED.value, CostSource.ESTIMATED.value} and row.get("cost_minor_units") is not None:
            bucket["cost_known"] += 1
        interventions = row.get("human_intervention_count")
        if isinstance(interventions, int) and interventions > 0:
            bucket["intervention_known"] += 1
    detail = (
        f"{sum(b['rows'] for b in index.values())} dispatch rows over surfaces "
        f"{sorted(surfaces) or ['none']}; codex/* is spend_enforcement=unsupported"
    )
    return index, detail


def _dispatch_totals(dispatch: Mapping[str, Mapping[str, int]], episode_ids: Sequence[str]) -> dict[str, int]:
    """Sum the dispatch facts bound to these episodes.  A cell with no matched row sums to zero,
    and a zero here means *not measured* -- which is why it is handed to `cost_clause` rather
    than printed."""

    totals = {"rows": 0, "cost_known": 0, "intervention_known": 0}
    for episode_id in episode_ids:
        bucket = dispatch.get(episode_id)
        if bucket is None:
            continue
        for name in totals:
            totals[name] += int(bucket.get(name, 0))
    return totals


# --------------------------------------------------------------------------------------
# Host arm availability (D-2).  Probed, never executed.
# --------------------------------------------------------------------------------------

_CODEX_CANDIDATES = ("/Applications/ChatGPT.app/Contents/Resources/codex",)


def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _arm_availability() -> dict[str, dict[str, Any]]:
    """Which arms could actually be run on this host, and on what evidence.

    D-2's measurement: only two of the four runtime surfaces have an executable binary here, and
    they are exactly the two whose protocol carries no currency -- so an arm being runnable and
    its cost being knowable are different questions and are answered separately.
    """

    codex = shutil.which("codex") or next((path for path in _CODEX_CANDIDATES if _executable(path)), "")
    claude = shutil.which("claude") or ""
    return {
        PilotArm.EXISTING_RUNTIME.value: {
            "allocatable": True,
            "runnable": bool(codex),
            "detail": f"codex binary at {codex}" if codex else "no codex binary on PATH or at the known app path",
            "cost_reportable": False,
        },
        PilotArm.MARKDOWN_HANDOFF.value: {
            "allocatable": True,
            "runnable": True,
            "detail": "owner-authored handoff; needs no binary on this host",
            "cost_reportable": False,
        },
        PilotArm.TCE_ASSISTED.value: {
            "allocatable": True,
            "runnable": bool(codex),
            "detail": f"wraps the same executable surface: {codex}" if codex else "no runnable executor surface on this host",
            "cost_reportable": False,
        },
        PilotArm.OWNER_UNASSISTED.value: {
            "allocatable": False,
            "runnable": True,
            "detail": (
                "an allocator cannot randomise a human into doing the work himself"
                + (f"; a claude binary is present at {claude}" if claude else "; no claude binary on this host")
            ),
            "cost_reportable": False,
        },
    }


# --------------------------------------------------------------------------------------
# Dreams (§5).  Relevance is adjudicated; a rejection is never a false positive.
# --------------------------------------------------------------------------------------


def _dream_block(query: QueryFn, present: set[str], project: str | None, head: str) -> dict[str, Any]:
    missing = [name for name in DREAM_TABLES if name not in present]
    if missing:
        clause = clause_not_computable("dreams", "dream_tables_absent", detail=f"{', '.join(missing)} absent at alembic head {head}")
        return {"computable": False, "clause": clause.to_payload(), "rendered": clause.render(), "projects": []}

    by_project: dict[str | None, dict[str, Any]] = {}

    def bucket(raw: Any) -> dict[str, Any]:
        key = None if raw in (None, "") else str(raw)
        return by_project.setdefault(
            key,
            {
                "project_id": key,
                "proposals": 0,
                "surfaced": 0,
                "surfaced_attested": 0,
                "completed": 0,
                "status": dict.fromkeys((s.value for s in DreamProposalStatus), 0),
                "nonresponse": dict.fromkeys((s.value for s in NonresponseState), 0),
                "adjudications": 0,
                "blind_verified": 0,
                "relevant": 0,
                "not_relevant": 0,
                "cannot_judge": 0,
                "delivery_useful": 0,
                "delivery_too_early": 0,
            },
        )

    for row in query(_DREAM_STATUS_SQL):
        if project and str(row.get("project_id") or "") != project:
            continue
        entry = bucket(row.get("project_id"))
        count = int(row.get("n") or 0)
        entry["proposals"] += count
        entry["surfaced"] += int(row.get("surfaced") or 0)
        entry["surfaced_attested"] += int(row.get("surfaced_attested") or 0)
        entry["completed"] += int(row.get("completed") or 0)
        status = str(row.get("status") or "")
        if status in entry["status"]:
            entry["status"][status] += count
        nonresponse = str(row.get("nonresponse_state") or "")
        if nonresponse in entry["nonresponse"]:
            entry["nonresponse"][nonresponse] += count

    for row in query(_DREAM_ADJUDICATION_SQL):
        if project and str(row.get("project_id") or "") != project:
            continue
        entry = bucket(row.get("project_id"))
        count = int(row.get("n") or 0)
        entry["adjudications"] += count
        if row.get("blind_verified"):
            entry["blind_verified"] += count
        relevance = str(row.get("relevance") or "")
        if relevance in {"relevant", "not_relevant", "cannot_judge"}:
            entry[relevance] += count
        if str(row.get("delivery_useful") or "") == "useful" and row.get("counted_for_delivery"):
            entry["delivery_useful"] += count
        if str(row.get("delivery_useful") or "") == "too_early":
            entry["delivery_too_early"] += count

    if not by_project:
        by_project[None] = bucket(None)

    projects: list[dict[str, Any]] = []
    for entry in sorted(by_project.values(), key=lambda item: str(item["project_id"] or "")):
        rate, relevance_clause = false_positive_relevance(
            relevant=int(entry["relevant"]),
            not_relevant=int(entry["not_relevant"]),
            cannot_judge=int(entry["cannot_judge"]),
        )
        if int(entry["completed"]) <= 0:
            delivery_clause = clause_not_computable(
                "later_useful_delivery",
                "no_completed_proposals",
                detail=f"0 proposals reached completed; lookback {DELIVERY_LOOKBACK_DAYS}d",
            )
        elif int(entry["delivery_useful"]) <= 0:
            delivery_clause = clause_not_computable(
                "later_useful_delivery",
                "no_adjudications",
                detail=f"{entry['completed']} completed, {entry['delivery_too_early']} answered inside the {DELIVERY_LOOKBACK_DAYS}d lookback and not counted",
            )
        else:
            delivery_clause = clause_pass("later_useful_delivery")
        entry["false_positive_relevance"] = rate
        entry["false_positive_clause"] = relevance_clause.to_payload()
        entry["false_positive_rendered"] = relevance_clause.render()
        entry["later_useful_delivery_clause"] = delivery_clause.to_payload()
        entry["later_useful_delivery_rendered"] = delivery_clause.render()
        projects.append(entry)
    return {"computable": True, "clause": None, "rendered": "", "projects": projects}


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


def dreams_not_read(reason_detail: str = "no database was read") -> dict[str, Any]:
    """The dream block when nothing was loaded.  NOT_COMPUTABLE, never an empty measurement."""

    clause = clause_not_computable("dreams", "dream_tables_absent", detail=reason_detail)
    return {"computable": False, "clause": clause.to_payload(), "rendered": clause.render(), "projects": []}


def build_report(
    episodes: Sequence[EpisodeFacts],
    *,
    now: datetime | None = None,
    project: str | None = None,
    alembic_head: str = "not_read",
    dreams: Mapping[str, Any] | None = None,
    dispatch: Mapping[str, Mapping[str, int]] | None = None,
    dispatch_detail: str = "no dispatch rows were loaded",
    arm_availability: Mapping[str, Mapping[str, Any]] | None = None,
    strata: int = 0,
    unreadable_allocations: int = 0,
    unreadable_closes: int = 0,
) -> dict[str, Any]:
    """The whole computation, over a sequence of `EpisodeFacts` and nothing else that is required.

    Callable with the episodes alone: every other input -- dreams, dispatch cost and intervention
    facts, host arm availability, the clock -- is keyword-only and optional, so the computation is
    testable without a database and cannot quietly acquire one.  Loading lives in `main`.

    Kept separate from every renderer on purpose: the text output, `--json` and (when the route
    lands) `GET /v1/pilot/report` are renderings of ONE computation, and the clause text itself
    comes from `Clause.render()` and the two `to_payload()` methods rather than being re-written
    here, so they cannot drift into three different verdicts.
    """

    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    dispatch_facts: Mapping[str, Mapping[str, int]] = dispatch or {}
    availability = dict(arm_availability or _arm_availability())
    arm_runnable = {name: bool(entry["runnable"]) for name, entry in availability.items()}

    runtime_episodes = [episode for episode in episodes if episode.arm_class == ARM_CLASS_RUNTIME]
    human_episodes = [episode for episode in episodes if episode.arm_class == ARM_CLASS_HUMAN_WORKFLOW]

    cells: dict[tuple[str | None, str], list[EpisodeFacts]] = {}
    for episode in runtime_episodes:
        cells.setdefault((episode.project_id, episode.decision_family), []).append(episode)

    verdicts: list[ClaimAVerdict] = []
    cell_payloads: list[dict[str, Any]] = []
    for (cell_project, family), cell_episodes in sorted(cells.items(), key=lambda item: (str(item[0][0] or ""), item[0][1])):
        verdict = claim_a(cell_episodes, project_id=cell_project, decision_family=family, now=moment, arm_runnable=arm_runnable)
        verdicts.append(verdict)
        totals = _dispatch_totals(dispatch_facts, [episode.episode_id for episode in cell_episodes])
        cost = cost_clause(cost_known=totals["cost_known"], episodes=totals["rows"], detail=dispatch_detail)
        intervention = human_intervention_clause(known=totals["intervention_known"], episodes=totals["rows"], detail=dispatch_detail)
        payload = verdict.to_payload()
        payload["cost"] = cost.to_payload()
        payload["cost_known"] = totals["cost_known"]
        payload["dispatch_rows"] = totals["rows"]
        payload["human_intervention"] = intervention.to_payload()
        payload["intervention_known"] = totals["intervention_known"]
        cell_payloads.append(payload)

    global_totals = _dispatch_totals(dispatch_facts, [episode.episode_id for episode in episodes])
    global_cost = cost_clause(cost_known=global_totals["cost_known"], episodes=global_totals["rows"], detail=dispatch_detail)
    global_intervention = human_intervention_clause(
        known=global_totals["intervention_known"], episodes=global_totals["rows"], detail=dispatch_detail
    )
    active_days, calendar_days = window_clock(episodes, now=moment)
    claim_b_verdict: ClaimBVerdict = claim_b(human_episodes)
    worst = worst_cell(verdicts)

    return {
        "produced": True,
        "generated_at": moment.isoformat(),
        "alembic_head": alembic_head,
        "project_filter": project,
        "thresholds_version": P6_THRESHOLDS_VERSION,
        "thresholds_sha": P6_THRESHOLDS_SHA,
        "thresholds_effective_at": P6_THRESHOLDS_EFFECTIVE_AT.isoformat(),
        "single_participant": SINGLE_PARTICIPANT_SENTENCE,
        "pooling_refused": POOLING_REFUSED_SENTENCE,
        "success_definition": SUCCESS_DEFINITION,
        "enrolments_required_per_cell": enrolments_required_per_cell(len(RANDOMIZED_ARMS)),
        "window": {
            "active_days": active_days,
            "min_active_days": MIN_ACTIVE_DAYS,
            "calendar_days": calendar_days,
            "density_note": _DENSITY_NOTE,
        },
        "enrollment": {
            "episodes": len(list(episodes)),
            "runtime_episodes": len(runtime_episodes),
            "human_workflow_episodes": len(human_episodes),
            "strata": strata,
            "arms_enabled": [arm.value for arm in RANDOMIZED_ARMS],
            "unreadable_allocations": unreadable_allocations,
            "unreadable_closes": unreadable_closes,
        },
        "arm_availability": availability,
        "cost": global_cost.to_payload(),
        "cost_known": global_totals["cost_known"],
        "dispatch_rows": global_totals["rows"],
        "human_intervention": global_intervention.to_payload(),
        "intervention_known": global_totals["intervention_known"],
        "claim_a_cells": cell_payloads,
        "claim_a_supported_cells": [payload["decision_family"] for payload in cell_payloads if payload["supported"]],
        "worst_cell": worst.to_payload() if worst is not None else None,
        "claim_b": claim_b_verdict.to_payload(),
        "dreams": dict(dreams) if dreams is not None else dreams_not_read(),
    }


def load_context(query: QueryFn, *, project: str | None = None) -> tuple[list[EpisodeFacts], dict[str, Any]]:
    """Every read this command makes, in one place, so `build_report` needs no database.

    Raises `LookupError` when the pilot tables are absent: that is the one condition in which the
    report cannot be produced at all, and it exits 2 rather than printing an empty measurement.
    """

    head = _alembic_head(query)
    present = _tables_present(query, (*REQUIRED_TABLES, *DREAM_TABLES, "dispatch_records"))
    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        raise LookupError(
            f"the pilot tables do not exist in this database (alembic head is {head}; "
            f"20260909_0043 is required; missing: {', '.join(missing)}). "
            "Nothing was measured. This is not a pass."
        )

    runtime_rows = _load_episodes(query, ARM_CLASS_RUNTIME, project)
    human_rows = _load_episodes(query, ARM_CLASS_HUMAN_WORKFLOW, project)
    runtime_episodes, bad_alloc_runtime, bad_close_runtime = _episode_facts(runtime_rows)
    human_episodes, bad_alloc_human, bad_close_human = _episode_facts(human_rows)

    by_session, dispatch_detail = _dispatch_by_session(query, present)
    dispatch: dict[str, dict[str, int]] = {}
    for row in (*runtime_rows, *human_rows):
        bucket = by_session.get((str(row.get("workspace_id") or ""), str(row.get("session_id") or "")))
        if bucket is not None:
            dispatch[str(row["id"])] = dict(bucket)

    context: dict[str, Any] = {
        "project": project,
        "alembic_head": head,
        "dreams": _dream_block(query, present, project, head),
        "dispatch": dispatch,
        "dispatch_detail": dispatch_detail,
        "strata": len(query(_STRATA_SQL)),
        "unreadable_allocations": bad_alloc_runtime + bad_alloc_human,
        "unreadable_closes": bad_close_runtime + bad_close_human,
    }
    return [*runtime_episodes, *human_episodes], context


# --------------------------------------------------------------------------------------
# Text renderer
# --------------------------------------------------------------------------------------


def _render_window(window: dict[str, Any], indent: str = "  ") -> list[str]:
    return [
        f"{indent}window: active_days={window['active_days']}/{window['min_active_days']}  calendar_days={window['calendar_days']}",
        f"{indent}        (calendar_days is a NON-QUALIFYING fact; only active_days is a clause)",
        f"{indent}        ({window['density_note']})",
    ]


def _render_exclusions(exclusions: dict[str, int], indent: str = "  ") -> list[str]:
    parts = [f"{key}={int(exclusions.get(key, 0))}" for key in EXCLUSION_KEYS]
    return [
        f"{indent}excluded: {'  '.join(parts[:4])}",
        f"{indent}          {'  '.join(parts[4:])}",
    ]


def _render_cell(payload: dict[str, Any]) -> list[str]:
    project = payload["project_id"] or "(no project)"
    status = "SUPPORTED" if payload["supported"] else "NOT ENOUGH EVIDENCE"
    lines = [f"CELL project={project}  decision_family={payload['decision_family']}: {status}"]
    lines.append(f"  enrolled={payload['enrolled']}  closed={payload['closed']}  complete_blocks={payload['complete_blocks']}")
    for arm in RANDOMIZED_ARMS:
        closed = int(payload["closed_by_arm"].get(arm.value, 0))
        success = int(payload["success_by_arm"].get(arm.value, 0))
        as_treated = int(payload["as_treated_by_arm"].get(arm.value, 0))
        rate = "NOT_COMPUTABLE(no_closed_episodes)" if closed == 0 else f"{success}/{closed}"
        lines.append(f"    arm {arm.value}: closed={closed}  success={rate}  as_treated={as_treated}")
    lines.extend(_render_window({"active_days": payload["active_days"], "min_active_days": MIN_ACTIVE_DAYS, "calendar_days": payload["calendar_days"], "density_note": _DENSITY_NOTE}))
    lines.append(f"  {payload['cost']['rendered']}  cost_known={payload['cost_known']}/{payload['dispatch_rows']}")
    lines.append(f"  {payload['human_intervention']['rendered']}  intervention_known={payload['intervention_known']}/{payload['dispatch_rows']}")
    lines.extend(_render_exclusions(payload["exclusions"]))
    lines.append("  clauses:")
    for clause in payload["clauses"]:
        lines.append(f"    {clause['rendered']}")
    if payload["shortfalls"]:
        lines.append("  shortfalls:")
        for shortfall in payload["shortfalls"]:
            lines.append(f"    - {shortfall}")
    return lines


def _render_dreams(dreams: dict[str, Any]) -> list[str]:
    if not dreams["computable"]:
        return [f"DREAMS: {dreams['rendered']}"]
    lines: list[str] = []
    for entry in dreams["projects"]:
        project = entry["project_id"] or "(no project)"
        lines.append(f"DREAMS (project={project}):")
        lines.append(f"  proposals={entry['proposals']}  surfaced={entry['surfaced']}  surfaced_attested={entry['surfaced_attested']}")
        verdicts = "  ".join(f"{name}={entry['status'].get(name, 0)}" for name in ("accepted", "rejected", "snoozed", "expired", "withdrawn"))
        lines.append(f"  verdicts: {verdicts}")
        nonresponse = " ".join(f"{name}={entry['nonresponse'].get(name, 0)}" for name in (s.value for s in NonresponseState))
        lifecycle = "  ".join(f"{name}={entry['status'].get(name, 0)}" for name in ("proposed", "surfaced", "pursued", "completed", "abandoned"))
        lines.append(f"  lifecycle: {lifecycle}")
        lines.append(f"  nonresponse: {nonresponse}   (nonresponse is NOT rejection)")
        lines.append(f"  adjudications: total={entry['adjudications']}  blind_verified={entry['blind_verified']}  cannot_judge={entry['cannot_judge']}")
        rate = entry["false_positive_relevance"]
        if rate is None:
            lines.append(f"  {entry['false_positive_rendered']}")
        else:
            lines.append(
                f"  false_positive_relevance = {rate}  "
                f"({entry['not_relevant']} not_relevant / {entry['relevant'] + entry['not_relevant']} adjudicated; "
                f"{entry['cannot_judge']} cannot_judge counted in neither)"
            )
        if entry["later_useful_delivery_clause"]["state"] == "pass":
            lines.append(
                f"  later_useful_delivery = {entry['delivery_useful']}/{entry['completed']} completed proposals "
                f"judged useful at least {DELIVERY_LOOKBACK_DAYS}d later "
                f"({entry['delivery_too_early']} answered too early and not counted)"
            )
        else:
            lines.append(f"  {entry['later_useful_delivery_rendered']}")
    return lines


def render_text(report: dict[str, Any]) -> str:
    if not report["produced"]:
        return f"P6 PILOT REPORT: {report['reason']}"

    lines: list[str] = []
    lines.append("P6 OPERATIONAL PROOF — live corpus")
    lines.append(
        f"thresholds_version={report['thresholds_version']}  thresholds_sha={report['thresholds_sha']}  "
        f"effective_at={report['thresholds_effective_at']}"
    )
    lines.append(f"alembic_head={report['alembic_head']}  generated_at={report['generated_at']}")
    lines.append(report["single_participant"])
    lines.append("")
    lines.append(f"success: {report['success_definition']}")
    lines.append(
        f"arithmetic: {len(RANDOMIZED_ARMS)} arms x {MIN_CLOSED_PER_ARM} closed / {MIN_CLOSE_COVERAGE} coverage = "
        f"{report['enrolments_required_per_cell']} enrolments PER (project, decision_family) cell."
    )
    lines.append("legend: server_derived and human_attested fields are read by clauses; agent_asserted fields are not read here at all.")
    lines.append("")

    enrollment = report["enrollment"]
    if enrollment["episodes"] == 0:
        lines.append(NO_ENROLLED_EPISODES_SENTENCE)
    lines.extend(_render_window(report["window"]))
    lines.append(
        f"  enrollment: {enrollment['episodes']} episodes "
        f"({enrollment['runtime_episodes']} runtime, {enrollment['human_workflow_episodes']} human_workflow), "
        f"{enrollment['strata']} strata, arms enabled: {', '.join(enrollment['arms_enabled'])}"
    )
    if enrollment["unreadable_allocations"] or enrollment["unreadable_closes"]:
        lines.append(
            f"  UNREADABLE ROWS: allocations={enrollment['unreadable_allocations']} closes={enrollment['unreadable_closes']} "
            "(counted, never defaulted into a success)"
        )
    lines.append("  arm availability on this host:")
    for name, entry in report["arm_availability"].items():
        if not entry["allocatable"]:
            state = "never allocated — election only"
        else:
            state = "runnable" if entry["runnable"] else "NOT RUNNABLE ON THIS HOST"
        lines.append(f"    {name}: {state} ({entry['detail']}); cost_reportable={entry['cost_reportable']}")
    lines.append(f"  {report['cost']['rendered']}  cost_known={report['cost_known']}/{report['dispatch_rows']}")
    lines.append(
        f"  {report['human_intervention']['rendered']}  "
        f"intervention_known={report['intervention_known']}/{report['dispatch_rows']}"
    )
    lines.append("")

    cells = report["claim_a_cells"]
    lines.append("CLAIM A (incremental benefit over existing runtimes):")
    if not cells:
        lines.append("  NOT ENOUGH EVIDENCE — no (project, decision_family) cell has any enrolled runtime episode.")
    else:
        for payload in cells:
            lines.append("")
            lines.extend(_render_cell(payload))
    lines.append("")

    claim_b_payload = report["claim_b"]
    state = "NOT_COMPUTABLE" if claim_b_payload["state"] == "not_computable" else "DESCRIPTIVE ONLY"
    lines.append(f"CLAIM B (performance relative to a specified human workflow): {state}")
    lines.append(
        f"  human_workflow episodes: closed={claim_b_payload['closed']}  "
        f"randomized={claim_b_payload['randomized']}  elected={claim_b_payload['elected']}"
    )
    lines.append(f"  {claim_b_payload['clause']['rendered']}")
    lines.append(f"  {claim_b_payload['sentence']}")
    lines.append("")

    lines.extend(_render_dreams(report["dreams"]))
    lines.append("")

    worst = report["worst_cell"]
    if worst is None:
        lines.append("SUMMARY: no cell exists to be worst. Nothing above is a measurement.")
    elif worst["supported"]:
        lines.append(
            f"SUMMARY: the WORST cell is project={worst['project_id'] or '(no project)'} "
            f"family={worst['decision_family']} and it clears every clause."
        )
    else:
        lines.append(
            f"SUMMARY (worst cell, never a mean): project={worst['project_id'] or '(no project)'} "
            f"family={worst['decision_family']}: NOT ENOUGH EVIDENCE — {'; '.join(worst['shortfalls'])}"
        )
    lines.append(report["pooling_refused"])
    lines.append("This report grants no authority: it writes no qualification record and no promotion, exposure or personalization path reads it.")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the raw report dict instead of the text report")
    parser.add_argument(
        "--project",
        default=None,
        help="narrow the report to one project_id. This NARROWS; nothing widens. There is no flag that pools across projects or families.",
    )
    args = parser.parse_args(argv)

    try:
        query = _connect_query()
        episodes, context = load_context(query, project=args.project)
    except LookupError as absent:
        print(f"P6 PILOT REPORT: {absent}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as error:  # noqa: BLE001 - the report must never hand the operator a traceback
        print(f"P6 PILOT REPORT: could not be produced — {error}", file=sys.stderr)  # noqa: T201
        return 2

    report = build_report(episodes, **context)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True, default=str))  # noqa: T201
    else:
        print(render_text(report))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
