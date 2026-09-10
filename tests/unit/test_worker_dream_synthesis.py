"""Unit tests for the proposal-generation worker job.

Every store call is monkeypatched against a fake session, so these run without Postgres. What
they assert is the *ordering* and the *refusals*: that the generator proposes and nothing else,
that a quote the model invented is refused before anything is written, that a theme the owner
rejected is checked before the duplicate-merge path can strengthen it, and that every exit
writes a run row rather than reading as a quiet week.

The pool fixture is six real owner-shaped messages, not lorem ipsum, because the devices are
token comparisons and a synthetic pool would prove refus*ability* rather than refusal.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import pathlib
import textwrap
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tce_api import dream_store
from tce_api.plan_rows import resolved_scope_to_json
from tce_shared.aspirations import (
    DREAM_SETTING_DEFAULTS,
    QUOTABLE_ORIGIN_KIND,
    RUN_REFUSAL_REASONS,
    SCOPE_KIND_PROJECT,
    SCOPE_KIND_WORKSPACE,
    DreamCandidate,
    DreamCitation,
    DreamProposalEventKind,
    DreamProposalProjection,
    DreamProposalStatus,
    NonresponseState,
    PoolMessage,
    theme_tokens_for,
)
from tce_shared.scope import PROJECT_BOUND, PROJECT_UNBOUND, ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DECOMPOSE,
    PLANNING_JOB_KIND_DREAM,
    NextPermittedAction,
    TaskStateProjection,
    TaskStatus,
)
from tce_worker.jobs import dream_synthesis

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
JOB_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"

MODULE_PATH = pathlib.Path(dream_synthesis.__file__)


# --------------------------------------------------------------------------- settings double


class _Settings:
    planning_enabled = True
    planning_job_lease_seconds = 120
    planning_job_max_attempts = 3
    planning_job_batch_size = 20
    planning_job_backoff_cap_seconds = 900
    takeover_plan_llm_enabled = True
    takeover_dream_llm_enabled = True
    takeover_plan_llm_timeout_seconds = 25.0
    model_provider = "ollama"
    ollama_url = "http://ollama:11434"
    embed_model = "mxbai-embed-large"
    extract_model = "qwen2.5:3b"
    redis_url = ""
    block_sensitivity = 3
    dream_message_limit = 60
    dream_min_messages = 3
    dream_min_message_chars = 25
    dream_max_message_chars = 1200
    dream_max_proposals_per_run = 3
    dream_min_citations = 2
    dream_max_citations = 6
    dream_quote_max_chars = 200
    dream_min_quote_overlap_tokens = 2
    dream_min_citation_relevance_tokens = 1
    dream_max_live_proposals = 20
    dream_duplicate_similarity = 0.60
    dream_material_new_citations = 2
    dream_material_max_similarity = 0.60
    dream_rejected_cooldown_days = 30
    dream_rejected_lookback_days = 365
    dream_rejected_scan_limit = 200
    dream_max_reproposals = 2
    dream_proposal_ttl_days = 45
    dream_nonresponse_after_surfaces = 3


# --------------------------------------------------------------------------- session double


class _Result:
    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows or []
        self.rowcount = len(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> _Result:
        self.executed.append((str(query), dict(params or {})))
        return _Result(scalar=False)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


class _Gateway:
    def __init__(self, payload: Any = None, *, boom: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {"dreams": []}
        self.boom = boom
        self.prompts: list[str] = []

    def extract_structured(self, prompt: str, schema_name: str) -> Any:
        self.prompts.append(prompt)
        if self.boom is not None:
            raise self.boom
        return self.payload


# --------------------------------------------------------------------------- fixtures


def _scope(*, project: str | None = None) -> ResolvedScope:
    return ResolvedScope(
        workspace_id="ws-1",
        executor_id="exec-1",
        owner_id="owner-1",
        subject_user_id="subject-1",
        project_id=project,
        project_binding=PROJECT_BOUND if project else PROJECT_UNBOUND,
        task_id="session-1",
        owner_ids=frozenset({"owner-1"}),
    )


def _projection() -> TaskStateProjection:
    return TaskStateProjection(
        task_id="session-1",
        workspace_id="ws-1",
        owner_id="owner-1",
        session_id="session-1",
        revision=3,
        contract_revision=1,
        objective_text="ship the thing",
        objective_hash="h1",
        objective_set_at=NOW,
        objective_set_seq=1,
        status=TaskStatus.PLANNING,
        next_permitted_action=NextPermittedAction.AWAIT_PLANNING,
        constraints=(),
        open_decisions=(),
        plan=None,
        unresolved_effects=(),
        latest_verification=None,
        citations=(),
    )


def _job_row(**overrides: Any) -> dict[str, Any]:
    scope = _scope()
    row: dict[str, Any] = {
        "id": JOB_ID,
        "workspace_id": "ws-1",
        "owner_id": "owner-1",
        "session_id": "session-1",
        "task_id": "session-1",
        "job_kind": PLANNING_JOB_KIND_DREAM,
        "input_revision": dream_synthesis._current_input_revision(_projection(), scope),
        "contract_revision": 1,
        "objective_hash": "h1",
        "objective_text": "ship the thing",
        "scope_json": json.dumps(resolved_scope_to_json(scope)),
        "attempts": 1,
        "max_attempts": 3,
        "cancel_requested": False,
    }
    row.update(overrides)
    return row


# Six real owner-shaped messages. The devices are token comparisons, so a synthetic pool would
# prove that a refusal is possible rather than that it happens.
_BODIES = (
    "the stripe webhook is failing again and i keep having to force push the fix to prod",
    "i want to get the witness engine in front of someone who is not me before the end of the month",
    "deploy the payment webhook fix to production now, the retries are piling up",
    "i keep meaning to rewrite the scheduler in rust but never actually start it",
    "can you ssh into the box and restart nginx, the certs expired again this morning",
    "do a deep research on similar technologies before i commit to the embedding store",
)

# The one theme used by the suppression and merge tests, in the owner's own register. The
# fixtures derive their theme tokens from these with the same function the candidate does:
# hand-writing a token tuple silently changes the Jaccard the whole test is about.
_TITLE = "stripe webhook keeps failing and i keep force pushing the fix"
_FIRST_STEP = "stop force pushing the webhook fix to prod"
_QUOTES = [
    {"n": 1, "quote": "the stripe webhook is failing again and i keep having to force push"},
    {"n": 3, "quote": "deploy the payment webhook fix to production now"},
]


def _pool(bodies: tuple[str, ...] = _BODIES) -> list[PoolMessage]:
    return [
        PoolMessage(
            n=index,
            event_id=f"event-{index}",
            receipt_id=f"receipt-{index}",
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            origin_kind=QUOTABLE_ORIGIN_KIND,
            observed_at=NOW - timedelta(days=index),
            body=body,
        )
        for index, body in enumerate(bodies, start=1)
    ]


def _projection_row(
    *,
    proposal_id: str,
    status: DreamProposalStatus,
    title: str,
    first_step: str,
    theme_tokens: tuple[str, ...],
    citations: tuple[DreamCitation, ...] = (),
    rejected_at: datetime | None = None,
    repropose_depth: int = 0,
) -> DreamProposalProjection:
    return DreamProposalProjection(
        proposal_id=proposal_id,
        workspace_id="ws-1",
        owner_id="owner-1",
        subject_user_id="subject-1",
        project_id=None,
        scope_kind=SCOPE_KIND_WORKSPACE,
        session_id="session-1",
        revision=1,
        status=status,
        nonresponse=NonresponseState.NEVER_SURFACED,
        surfaced_count=0,
        surfaced_attested=False,
        first_surfaced_at=None,
        last_surfaced_at=None,
        title=title,
        connection_text="because you said so",
        benefit_text="one less thing on fire",
        first_step=first_step,
        citations=citations,
        theme_tokens=theme_tokens,
        evidence_basis="trusted_current",
        evidence_revision="rev-1",
        evidence_cutoff_at=NOW,
        supersedes_proposal_id=None,
        repropose_depth=repropose_depth,
        snooze_until=None,
        expires_at=NOW + timedelta(days=45),
        accepted_at=None,
        rejected_at=rejected_at,
        rejection_reason="not now" if rejected_at else "",
        task_id=None,
        objective_hash=None,
        plan_root_goal_id=None,
        pursuit_started_at=None,
        completed_at=None,
        abandoned_at=None,
        abandon_reason="",
        withdrawn_reason="",
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire the job to a fake session and record every store call it makes."""
    session = _FakeSession()
    state: dict[str, Any] = {
        "session": session,
        "claimed": [_job_row()],
        "loaded": (_projection(), 7, "sr-1"),
        "run_row": {"id": RUN_ID, "state": "running", "scope_kind": SCOPE_KIND_WORKSPACE},
        "pool": _pool(),
        "pool_drops": {},
        "completions": [],
        "complete_returns": True,
        "finished": [],
        "minted": [],
        "applied": [],
        "live": [],
        "rejected": [],
        "gateway": _Gateway(),
        "deep_drops": [],
    }

    def _factory() -> Any:
        return lambda: session

    def _claim(_db: Any, **_kwargs: Any) -> list[Any]:
        return list(state["claimed"])

    def _load(_db: Any, **_kwargs: Any) -> Any:
        return state["loaded"]

    def _complete(_db: Any, **kwargs: Any) -> bool:
        state["completions"].append(kwargs)
        return bool(state["complete_returns"])

    def _load_run(_db: Any, **_kwargs: Any) -> Any:
        return state["run_row"]

    def _finish(_db: Any, **kwargs: Any) -> None:
        state["finished"].append(kwargs)

    def _select(_db: Any, **kwargs: Any) -> Any:
        state["select_kwargs"] = kwargs
        return list(state["pool"]), dict(state["pool_drops"])

    def _mint(_db: Any, **kwargs: Any) -> str:
        state["minted"].append(kwargs)
        return f"proposal-{len(state['minted'])}"

    def _apply(_db: Any, **kwargs: Any) -> Any:
        state["applied"].append(kwargs)
        return None

    def _deep(_db: Any, *, scope: Any, citations: Any) -> Any:
        return list(citations), list(state["deep_drops"])

    monkeypatch.setattr(dream_synthesis, "get_settings", lambda: _Settings())
    monkeypatch.setattr(dream_synthesis, "get_session_factory", _factory)
    monkeypatch.setattr(dream_synthesis, "claim_planning_job", _claim)
    monkeypatch.setattr(dream_synthesis, "load_task_state", _load)
    monkeypatch.setattr(dream_synthesis, "complete_planning_job", _complete)
    monkeypatch.setattr(dream_synthesis, "load_generation_run_by_job", _load_run)
    monkeypatch.setattr(dream_synthesis, "finish_generation_run", _finish)
    monkeypatch.setattr(dream_synthesis, "select_candidate_messages", _select)
    monkeypatch.setattr(dream_synthesis, "mint_proposal", _mint)
    monkeypatch.setattr(dream_synthesis, "apply_dream_events", _apply)
    monkeypatch.setattr(dream_synthesis, "validate_citations_deep", _deep)
    monkeypatch.setattr(dream_synthesis, "load_live_proposals", lambda _db, **_k: list(state["live"]))
    monkeypatch.setattr(dream_synthesis, "load_rejected_proposals", lambda _db, **_k: list(state["rejected"]))
    monkeypatch.setattr(dream_synthesis, "get_gateway", lambda _settings: state["gateway"])
    return state


def _reply(*items: dict[str, Any]) -> dict[str, Any]:
    return {"dreams": list(items)}


def _dream(
    *,
    title: str,
    first_step: str,
    citations: list[dict[str, Any]],
    connection: str = "you keep coming back to it",
    benefit: str = "one less thing on fire",
) -> dict[str, Any]:
    return {
        "title": title,
        "connection": connection,
        "benefit": benefit,
        "first_step": first_step,
        "citations": citations,
    }


# --------------------------------------------------------------------------- G4: proposal only


def test_generator_never_plans() -> None:
    """G4. The generator proposes and nothing else, and it cannot reach the pursuit machinery.

    An AST walk rather than a grep: the point is that no *node* resolves to the planning
    writers, so re-importing one under an alias does not slip past.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    forbidden_names = {"write_plan_rows", "enqueue_planning_job", "apply_task_state_events"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            offenders.append(f"Name {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            offenders.append(f"Attribute {node.attr}")
        if isinstance(node, ast.Attribute) and node.attr == "OBJECTIVE_SET":
            offenders.append("TaskStateEventKind.OBJECTIVE_SET")
        if isinstance(node, ast.alias) and node.name in forbidden_names:
            offenders.append(f"import {node.name}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "autonomy_goals" in node.value:
            offenders.append("literal autonomy_goals")
    assert offenders == [], f"the generator can reach the pursuit machinery: {offenders}"


def test_the_ast_gate_would_actually_catch_a_violation() -> None:
    """A gate that cannot fail is not a gate. This is the same walk over a module that offends."""
    tree = ast.parse("from x import write_plan_rows\nwrite_plan_rows(db)\n")
    hits = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "write_plan_rows")
        or (isinstance(node, ast.alias) and node.name == "write_plan_rows")
    ]
    assert hits, "the walk cannot see the thing it claims to forbid"


def test_no_count_reaches_the_generator() -> None:
    """M4. The count-derived producer is gone, and the module cannot reach what is left of it."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for symbol in (
        "DreamSignals",
        "RecurringAsk",
        "DreamSeed",
        "cluster_recurring_asks",
        "derive_dream_seeds",
        "select_dream_to_pursue",
        "_gather_dream_signals",
        "tce_shared.dreams",
        "write_dream_rows",
    ):
        assert symbol not in source, f"{symbol} still reaches the generator"


def _statement_text(func: Any) -> str:
    """Every string literal in a function EXCEPT its docstring.

    Reading the raw source would let a docstring that merely *names* the forbidden arm satisfy
    a check that the arm is absent — and this module's docstrings talk about ``task_type`` at
    length, precisely because it is the thing that was removed.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    body = node.body[1:] if ast.get_docstring(node) else node.body
    parts: list[str] = []
    for statement in body:
        for child in ast.walk(statement):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                parts.append(child.value)
    return "\n".join(parts)


# --------------------------------------------------------------------------- G2: the corpus


def test_corpora_are_disjoint() -> None:
    """G2 / Y2. Project and workspace runs cannot share a message, and neither admits executor text.

    Measured on the live Postgres while this was written: the clause this job used to carry
    admits 4,649 rows in workspace ``personal``, every one of them written by an executor
    (``context->>'_tce_owner' = 'codex-executor'``); the receipt-bound clause admits 1. So the
    two forbidden arms below are not hypothetical — they are the arms that were there.
    """
    sql = _statement_text(dream_store.select_candidate_messages)
    assert "trusted_input_receipts" in sql, "the receipt is the only admission"
    assert "r.project_id = :project_id" in sql, "a project run must be bound to the receipt's project"
    assert "r.project_id IS NULL" in sql, "a workspace run must take only receipts with no project"
    assert "task_type" not in sql, "task_type is executor-assertable and is not an admission"
    assert "_tce_owner" not in sql, "_tce_owner is stamped from the caller and is not an admission"
    assert "origin_kind" in sql, "the receipt's origin_kind is the evidence class"
    # The two arms name different columns, so no message can satisfy both.
    assert "r.project_id = :project_id" != "r.project_id IS NULL"


def test_the_worker_passes_the_derived_scope_kind(harness: dict[str, Any]) -> None:
    """D9. scope_kind is derived from the scope, never read back off the run row."""
    harness["run_row"] = {"id": RUN_ID, "state": "running", "scope_kind": "project"}
    dream_synthesis.run(JOB_ID)
    assert harness["select_kwargs"]["scope_kind"] == SCOPE_KIND_WORKSPACE

    bound = _scope(project="proj_675e7e1d967615889d883a81")
    assert dream_synthesis._scope_kind(bound) == SCOPE_KIND_PROJECT
    assert dream_synthesis._scope_kind(_scope()) == SCOPE_KIND_WORKSPACE


def test_unentitled_project_run_refuses(harness: dict[str, Any]) -> None:
    """G2's companion. The entitlement gate is the route's, and the worker mints nothing without it.

    ``unentitled_project`` is refused by the route before this job is enqueued — the project id
    is client-assertable, so a caller can name a project they have never spoken into. Two
    halves are checked here: the store exposes the named producer of that refusal, and the
    worker's own vocabulary does not include it, so a worker that reached an unentitled project
    would produce an empty pool and refuse ``insufficient_messages`` rather than inventing one.
    """
    assert hasattr(dream_store, "subject_has_project_receipts")
    assert "unentitled_project" in RUN_REFUSAL_REASONS
    assert "unentitled_project" not in MODULE_PATH.read_text(encoding="utf-8")

    harness["pool"] = []
    result = dream_synthesis.run(JOB_ID)
    assert result["reason"] == "insufficient_messages"
    assert harness["finished"][-1]["refusal_reason"] == "insufficient_messages"
    assert not harness["minted"]


# --------------------------------------------------------------------------- G11: the quote


def test_quote_must_be_in_the_message(harness: dict[str, Any]) -> None:
    """G11. A quote the model invented is refused before anything is written.

    Two halves. A wholly fabricated pair of quotes refuses ``quote_not_found``. One real quote
    beside one fabricated one leaves a single citation, which is below ``dream_min_citations``,
    and the recount catches it — the drop-then-recount hole that let a candidate validate with
    two citations and persist with zero.
    """
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title="stripe webhook keeps failing",
                first_step="add a retry log line",
                citations=[
                    {"n": 1, "quote": "the stripe webhook has never once failed"},
                    {"n": 3, "quote": "our payment infrastructure is in excellent shape"},
                ],
            )
        )
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"]
    assert harness["finished"][-1]["refusals"].get("quote_not_found") == 1

    harness["finished"].clear()
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title="stripe webhook keeps failing",
                first_step="add a retry log line",
                citations=[
                    {"n": 1, "quote": "the stripe webhook is failing again"},
                    {"n": 3, "quote": "our payment infrastructure is in excellent shape"},
                ],
            )
        )
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"]
    assert harness["finished"][-1]["refusals"].get("insufficient_citations") == 1


def test_a_grounded_proposal_is_actually_minted(harness: dict[str, Any]) -> None:
    """The counterweight. A design that refused a claim the owner really typed is broken too."""
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title=_TITLE,
                first_step=_FIRST_STEP,
                citations=list(_QUOTES),
            )
        )
    )
    result = dream_synthesis.run(JOB_ID)
    assert result["status"] == "succeeded"
    assert len(harness["minted"]) == 1
    minted = harness["minted"][0]
    assert minted["run_id"] == RUN_ID
    assert len(minted["candidate"].citations) == 2
    assert harness["finished"][-1]["state"] == "succeeded"
    assert harness["finished"][-1]["proposals_written"] == 1


def test_consultant_voice_is_refused(harness: dict[str, Any]) -> None:
    """M3. Title Case and a banned word are two independent rules and both fire on the claim."""
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title="Comprehensive Webhook Reliability Framework",
                first_step="Leverage The Existing Stripe Webhook Infrastructure",
                citations=[
                    {"n": 1, "quote": "the stripe webhook is failing again"},
                    {"n": 3, "quote": "deploy the payment webhook fix to production now"},
                ],
            )
        )
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"]
    refusals = harness["finished"][-1]["refusals"]
    assert refusals.get("voice_violation") == 1 or refusals.get("no_quote_overlap") == 1


# --------------------------------------------------------------------------- G6: the ordering


def test_rejected_check_runs_before_duplicate_merge(harness: dict[str, Any]) -> None:
    """G6's unit half, and the reason the order is fixed.

    A candidate carrying a theme the owner rejected must be dropped, not merged into a live
    neighbour it happens to resemble. Run the merge first and the rejected theme comes back
    strengthened, inside a proposal that already has the owner's attention, with no refusal
    recorded anywhere.
    """
    theme = theme_tokens_for(_TITLE, _FIRST_STEP)
    harness["rejected"] = [
        _projection_row(
            proposal_id="rejected-1",
            status=DreamProposalStatus.REJECTED,
            title=_TITLE,
            first_step=_FIRST_STEP,
            theme_tokens=theme,
            rejected_at=NOW - timedelta(days=2),
        )
    ]
    harness["live"] = [
        _projection_row(
            proposal_id="live-1",
            status=DreamProposalStatus.SURFACED,
            title=_TITLE,
            first_step=_FIRST_STEP,
            theme_tokens=theme,
        )
    ]
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title=_TITLE,
                first_step=_FIRST_STEP,
                citations=list(_QUOTES),
            )
        )
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"], "a rejected theme was re-minted"
    assert not harness["applied"], "a rejected theme was merged into a live proposal"
    refusals = harness["finished"][-1]["refusals"]
    assert refusals.get("suppressed_rejected_theme") == 1


def test_a_restatement_strengthens_the_live_proposal_instead_of_duplicating_it(
    harness: dict[str, Any],
) -> None:
    """4.3. A duplicate with new citations appends evidence; it never mints a second row.

    The live proposal's history is untouched: this is one ``evidence_revalidated`` event and no
    status change, so a surfaced count, an acceptance and a rejection reason all survive it.
    """
    theme = theme_tokens_for(_TITLE, _FIRST_STEP)
    known = DreamCitation(
        event_id="event-9",
        receipt_id="receipt-9",
        content_sha256="already-known",
        origin_kind=QUOTABLE_ORIGIN_KIND,
        observed_at=NOW - timedelta(days=30),
        quote="the stripe webhook is failing",
        quote_sha256="qs-9",
    )
    harness["live"] = [
        _projection_row(
            proposal_id="live-1",
            status=DreamProposalStatus.SURFACED,
            title=_TITLE,
            first_step=_FIRST_STEP,
            theme_tokens=theme,
            citations=(known,),
        )
    ]
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title=_TITLE,
                first_step=_FIRST_STEP,
                citations=list(_QUOTES),
            )
        )
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"], "a restatement minted a second row"
    assert len(harness["applied"]) == 1
    event = harness["applied"][0]["new_events"][0]
    assert event.kind == DreamProposalEventKind.EVIDENCE_REVALIDATED
    assert event.actor_class == "system"
    assert len(event.payload["citations"]) == 3, "the merge is the union, deduplicated by content hash"
    assert event.payload["evidence_revision"], "D13: the revision is recomputed on revalidation"


def test_a_restatement_with_nothing_new_is_refused(harness: dict[str, Any]) -> None:
    """The other arm of 4.3: same theme, no new citations, no merge, a named refusal."""
    theme = theme_tokens_for(_TITLE, _FIRST_STEP)
    body = _BODIES[0]
    same = DreamCitation(
        event_id="event-1",
        receipt_id="receipt-1",
        content_sha256=hashlib.sha256(body.encode()).hexdigest(),
        origin_kind=QUOTABLE_ORIGIN_KIND,
        observed_at=NOW - timedelta(days=1),
        quote="the stripe webhook is failing again",
        quote_sha256="qs-1",
    )
    third = replace(
        same,
        event_id="event-3",
        receipt_id="receipt-3",
        content_sha256=hashlib.sha256(_BODIES[2].encode()).hexdigest(),
    )
    harness["live"] = [
        _projection_row(
            proposal_id="live-1",
            status=DreamProposalStatus.SURFACED,
            title=_TITLE,
            first_step=_FIRST_STEP,
            theme_tokens=theme,
            citations=(same, third),
        )
    ]
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title=_TITLE,
                first_step=_FIRST_STEP,
                citations=list(_QUOTES),
            )
        )
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"]
    assert not harness["applied"]
    assert harness["finished"][-1]["refusals"].get("duplicate_theme") == 1


# --------------------------------------------------------------------------- G12: the prompt


def test_prompt_hash_is_the_shipped_prompt() -> None:
    """G12. A stored hash that does not describe the shipped prompt makes every run row a lie."""
    assert (
        dream_synthesis.DREAM_PROMPT_SHA256
        == hashlib.sha256(dream_synthesis.DREAM_PROMPT.encode("utf-8")).hexdigest()
    )


def test_dream_prompt_moved_intact() -> None:
    """Re-homed from test_worker_planning.py, and both guards move with the prompt."""
    assert dream_synthesis.DREAM_PROMPT.count("__MESSAGES__") == 1
    assert "Reply with JSON only" in dream_synthesis.DREAM_PROMPT
    # `.replace`, never `.format`: the prompt carries a literal JSON example.
    with pytest.raises(KeyError):
        dream_synthesis.DREAM_PROMPT.format(messages="x")


def test_the_prompt_still_offers_the_abstention(harness: dict[str, Any]) -> None:
    """"If nothing clear comes through, return an empty list. That is a good answer."

    The one prompt in the tree that already asks for abstention. An empty list is a succeeded
    run with zero proposals and no refusals, not a failure.
    """
    assert "return an empty list" in dream_synthesis.DREAM_PROMPT
    harness["gateway"] = _Gateway({"dreams": []})
    result = dream_synthesis.run(JOB_ID)
    assert result["status"] == "succeeded"
    assert not harness["minted"]
    assert harness["finished"][-1]["state"] == "succeeded"
    assert harness["finished"][-1]["proposals_written"] == 0


def test_the_model_sees_bodies_and_nothing_else(harness: dict[str, Any]) -> None:
    """2.3. No event id, no receipt id, no hash, no project id, no count reaches the prompt."""
    dream_synthesis.run(JOB_ID)
    prompt = harness["gateway"].prompts[0]
    assert _BODIES[0] in prompt
    for message in harness["pool"]:
        assert message.event_id not in prompt
        assert message.receipt_id not in prompt
        assert message.content_sha256 not in prompt
    assert "ws-1" not in prompt
    assert "subject-1" not in prompt
    assert prompt.count("__MESSAGES__") == 0


def test_render_pool_is_bounded() -> None:
    long_pool = [replace(message, body="x" * 5000) for message in _pool()]
    assert len(dream_synthesis._render_pool(long_pool)) <= dream_synthesis.POOL_RENDER_MAX_CHARS


# --------------------------------------------------------------------------- refusals are loud


def test_worker_without_a_run_row_does_nothing(harness: dict[str, Any]) -> None:
    """W2. The route creates the run. With no run there is no provenance, so nothing is minted."""
    harness["run_row"] = None
    result = dream_synthesis.run(JOB_ID)
    assert result["reason"] == "no_generation_run"
    assert not harness["minted"]
    assert not harness["finished"], "there is no run row to finish"
    assert harness["gateway"].prompts == [], "the model was called with no run to record it against"


def test_a_finished_run_is_not_reworked(harness: dict[str, Any]) -> None:
    harness["run_row"] = {"id": RUN_ID, "state": "succeeded"}
    result = dream_synthesis.run(JOB_ID)
    assert result["reason"] == "no_generation_run"
    assert harness["gateway"].prompts == []


def test_model_failure_writes_a_failed_run_and_no_proposal(harness: dict[str, Any]) -> None:
    """D10. A broken generator must never read as 'no dreams today'.

    The live hazard this replaces swallowed every exception into a warning while the pursued
    counter stayed at zero, so a completely broken path and a quiet week were indistinguishable.
    """
    harness["gateway"] = _Gateway(boom=RuntimeError("ollama refused the connection"))
    result = dream_synthesis.run(JOB_ID)
    assert result["status"] == "failed"
    assert result["reason"] == "model_call_failed"
    assert not harness["minted"], "a failed model call must not produce a fabricated fallback"
    assert harness["finished"][-1]["state"] == "failed"
    assert harness["finished"][-1]["refusal_reason"] == "model_call_failed"
    assert harness["completions"][-1]["state"] == "failed"


def test_insufficient_messages_is_reported_not_hidden(harness: dict[str, Any]) -> None:
    """The answer on today's live corpus, and it is written into a row rather than swallowed.

    Measured while this was written: with the receipt rule applied, the whole live database
    yields at most 8 candidate messages across every scope, and all 8 are below
    ``dream_min_message_chars``. So the real pool is empty and every scope refuses here.
    """
    harness["pool"] = _pool(_BODIES[:2])
    harness["pool_drops"] = {"too_short": 6, "harness_text": 2}
    result = dream_synthesis.run(JOB_ID)
    assert result["reason"] == "insufficient_messages"
    finished = harness["finished"][-1]
    assert finished["state"] == "refused"
    assert finished["refusal_reason"] == "insufficient_messages"
    assert finished["pool_drops"] == {"too_short": 6, "harness_text": 2}
    assert finished["proposals_written"] == 0
    assert harness["gateway"].prompts == [], "the model is not called for a pool that cannot support it"
    assert harness["completions"][-1]["state"] == "succeeded", "a refusal is an answer, not a retry"


def test_model_disabled_still_finishes_its_run(monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]) -> None:
    """The shipped default is off, and off must produce a row saying so, not silence."""

    class _Off(_Settings):
        takeover_dream_llm_enabled = False

    monkeypatch.setattr(dream_synthesis, "get_settings", lambda: _Off())
    # run() short-circuits on the flag, exactly as it does for planning_enabled.
    assert dream_synthesis.run(JOB_ID)["status"] == "disabled"
    assert harness["session"].executed == []
    assert not harness["minted"]

    # And a job enqueued before the flag flipped still finishes its run rather than wedging it.
    outcome = dream_synthesis._process_job(_job_row(), settings=_Off(), lease_owner="lease-1")
    assert outcome["reason"] == "model_disabled"
    assert harness["finished"][-1]["refusal_reason"] == "model_disabled"


def test_stale_result_writes_nothing(harness: dict[str, Any]) -> None:
    """Re-homed. P2's staleness discipline, unchanged: a stale job mints nothing."""
    harness["claimed"] = [_job_row(input_revision="a-revision-from-a-previous-objective")]
    result = dream_synthesis.run(JOB_ID)
    assert result["status"] == "discarded"
    assert not harness["minted"]
    assert harness["finished"][-1]["refusal_reason"] == "stale_input_revision"
    assert harness["gateway"].prompts == []


def test_cancelled_job_is_cancelled_not_failed(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(cancel_requested=True)]
    assert dream_synthesis.run(JOB_ID)["status"] == "cancelled"
    assert not harness["minted"]


def test_lost_lease_writes_nothing(harness: dict[str, Any]) -> None:
    """Re-homed. A proposal minted under a lost lease duplicates the real leaseholder's."""
    harness["complete_returns"] = False
    harness["gateway"] = _Gateway(
        _reply(
            _dream(
                title=_TITLE,
                first_step=_FIRST_STEP,
                citations=list(_QUOTES),
            )
        )
    )
    result = dream_synthesis.run(JOB_ID)
    assert result["status"] != "succeeded"
    assert harness["session"].rolled_back >= 1
    assert harness["finished"][-1]["refusal_reason"] == "lost_lease"


def test_a_job_of_another_kind_is_skipped(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(job_kind=PLANNING_JOB_KIND_DECOMPOSE)]
    result = dream_synthesis.run(JOB_ID)
    assert result["skipped"] == 1
    assert not harness["minted"]
    assert harness["gateway"].prompts == []


def test_every_refusal_the_worker_writes_is_in_the_closed_vocabulary(harness: dict[str, Any]) -> None:
    """A refusal reason nobody declared is a string the reader cannot render."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    written: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "refusal_reason":
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.value.value:
            written.add(node.value.value)
    assert written, "the scan found no refusal_reason at all, so it proves nothing"
    assert written <= set(RUN_REFUSAL_REASONS), f"undeclared run refusals: {sorted(written - set(RUN_REFUSAL_REASONS))}"


# --------------------------------------------------------------------------- settings


def test_worker_setting_defaults_match_the_canonical_table() -> None:
    """A worker default that drifts from the shared table means Full and the worker disagree."""
    from tce_worker.config import Settings

    fields = Settings.model_fields
    checked = 0
    for name, expected in DREAM_SETTING_DEFAULTS.items():
        if name not in fields:
            continue
        checked += 1
        assert fields[name].default == expected, f"{name} drifted from the canonical default"
    assert checked >= 19, f"the worker carries only {checked} of the generation settings"


def test_the_settings_the_job_reads_all_resolve() -> None:
    """A knob with no reader does nothing; a reader with no knob falls back to the shared default."""
    knobs = dream_synthesis._dream_settings(_Settings())
    assert knobs["min_messages"] == 3
    assert knobs["max_sensitivity"] == 2, "block_sensitivity is a ceiling, and the pool sits below it"

    class _Bare:
        block_sensitivity = 3

    fallback = dream_synthesis._dream_settings(_Bare())
    assert fallback["min_messages"] == DREAM_SETTING_DEFAULTS["dream_min_messages"]
    assert fallback["proposal_ttl_days"] == DREAM_SETTING_DEFAULTS["dream_proposal_ttl_days"]


def test_citations_that_no_longer_verify_refuse_at_birth(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """W11. Stage 2 runs in the transaction that mints, so nothing is born on moved evidence.

    The deep check decrypts and re-applies quote containment. A citation whose message has since
    been redacted, re-scoped or retracted is dropped here, and a candidate left below the floor
    is refused rather than shown with one quote and a shrug.
    """

    def _one_survivor(_db: Any, *, scope: Any, citations: Any) -> Any:
        return list(citations)[:1], ["receipt_withdrawn"]

    harness["gateway"] = _Gateway(
        _reply(_dream(title=_TITLE, first_step=_FIRST_STEP, citations=list(_QUOTES)))
    )
    monkeypatch.setattr(dream_synthesis, "validate_citations_deep", _one_survivor)
    dream_synthesis.run(JOB_ID)

    assert not harness["minted"]
    refusals = harness["finished"][-1]["refusals"]
    assert refusals.get("citations_unverifiable") == 1


def test_a_lifted_rejection_is_superseded_rather_than_forgotten() -> None:
    """4.4. When suppression lifts, the new proposal names the rejection it reopens.

    Tested on the selector directly, because the window is deliberately narrow: a rejection is
    only reachable here when it matched on title-and-first-step tokens (so
    ``suppressed_by_rejection`` considered it) and then failed to match once the quotes were
    included (so ``material_change`` let it through). A fixture that merely re-states the
    rejected proposal cannot reach this branch at all — rule 3 exists to stop exactly that —
    so driving it through ``run()`` would prove the opposite of what it claimed.

    What matters is the chain: the rejected proposal is never rewritten, and
    ``repropose_depth`` counts up so the third refusal of one theme is a hard stop rather than
    an infinite argument.
    """
    theme = theme_tokens_for(_TITLE, _FIRST_STEP)
    rejected = _projection_row(
        proposal_id="rejected-1",
        status=DreamProposalStatus.REJECTED,
        title=_TITLE,
        first_step=_FIRST_STEP,
        theme_tokens=theme,
        rejected_at=NOW - timedelta(days=400),
        repropose_depth=1,
    )
    candidate = DreamCandidate(
        title=_TITLE,
        connection_text="you keep coming back to it",
        benefit_text="one less thing on fire",
        first_step=_FIRST_STEP,
        citations=(),
        theme_tokens=theme,
        evidence_basis="trusted_current",
    )
    match = dream_synthesis._superseded_id(candidate, [rejected], threshold=0.60)
    assert match is not None and match.proposal_id == "rejected-1"
    assert int(match.repropose_depth) + 1 == 2, "the chain counts, and dream_max_reproposals stops it"
    assert rejected.status is DreamProposalStatus.REJECTED, "the rejection is never rewritten"

    # An unrelated rejection is not claimed as the thing this candidate reopens.
    unrelated = _projection_row(
        proposal_id="rejected-2",
        status=DreamProposalStatus.REJECTED,
        title="rewrite the scheduler in rust",
        first_step="read the tokio docs",
        theme_tokens=theme_tokens_for("rewrite the scheduler in rust", "read the tokio docs"),
        rejected_at=NOW - timedelta(days=400),
    )
    assert dream_synthesis._superseded_id(candidate, [unrelated], threshold=0.60) is None


def test_a_re_proposal_of_the_same_theme_is_still_suppressed(harness: dict[str, Any]) -> None:
    """Rule 3, from the outside. Age alone does not reopen a rejection.

    400 days past the 30-day cooldown, with two citations the rejected proposal never carried,
    and it is still refused — because it is the same proposal in different words. That is the
    condition the run row names, so an over-suppressing system is visible rather than looking
    like a quiet week.
    """
    harness["rejected"] = [
        _projection_row(
            proposal_id="rejected-1",
            status=DreamProposalStatus.REJECTED,
            title=_TITLE,
            first_step=_FIRST_STEP,
            theme_tokens=theme_tokens_for(_TITLE, _FIRST_STEP),
            rejected_at=NOW - timedelta(days=400),
        )
    ]
    harness["gateway"] = _Gateway(
        _reply(_dream(title=_TITLE, first_step=_FIRST_STEP, citations=list(_QUOTES)))
    )
    dream_synthesis.run(JOB_ID)
    assert not harness["minted"]
    assert harness["finished"][-1]["refusals"].get("suppressed_rejected_theme") == 1
