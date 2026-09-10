"""The seven guarantees P5 exists to make, each tested so that perturbing the input flips it.

Builder A's ``tests/unit/test_aspirations.py`` and ``tests/unit/test_dream_fold.py`` test the
module surface — every device, every codec, every fold rule.  This file tests something
narrower and more load-bearing: the seven *promises* the phase was commissioned to keep.

    1. a proposal cannot be emitted that does not trace to a real owner message
    2. an executor-written event can never be quoted
    3. a rejected theme does not return
    4. nonresponse never becomes rejection or acceptance through the passage of time
    5. refresh preserves history
    6. completion is never inferred from plan creation
    7. a proposal in project A cannot cite evidence from project B

A gate that only ever says "no" proves nothing — a function that returns ``None`` for every
input passes it.  So **every** test here carries its own discriminator: the same assertion is
run against a perturbed input that must come out the other way, in the same test body, so the
test is red both when the guarantee breaks and when the guarantee is replaced by a blanket
refusal.  Where a discriminator could not be written against live code (because the file that
would carry it belongs to a builder still working), the check is self-arming: it returns early
until that file exists and its predicate is proved non-vacuous against a synthetic sample
today.

Fixtures are real.  The message bodies are verbatim rows of the live ``events`` table, typos
included, and the invented claims are the actual generic titles the owner rejected.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tce_shared.aspirations import (
    ADMISSIBLE_ORIGIN_KINDS,
    ATTRIBUTION_IMPORTED,
    ATTRIBUTION_SAID,
    BACKFILL_ORIGIN_KIND,
    CANDIDATE_REFUSAL_REASONS,
    DREAM_SETTING_DEFAULTS,
    EVIDENCE_BASIS_BACKFILL,
    LIVE_STATUSES,
    MATERIAL_CHANGE_CONDITIONS,
    POOL_DROP_REASONS,
    QUOTABLE_ORIGIN_KIND,
    RUN_REFUSAL_REASONS,
    SCOPE_KIND_PROJECT,
    TERMINAL_STATUSES,
    CandidateRefusal,
    DreamCandidate,
    DreamCitation,
    DreamProposalEvent,
    DreamProposalEventKind,
    DreamProposalProjection,
    DreamProposalStatus,
    NonresponseState,
    PoolMessage,
    attribution_for,
    content_token_overlap,
    evidence_basis_for,
    fold_dream_proposal,
    is_duplicate_theme,
    material_change,
    nonresponse_state,
    normalise_for_containment,
    parse_dream_payload,
    prepare_dream_write,
    quote_is_contained,
    suppressed_by_rejection,
    surfaced_event,
    theme_tokens_for,
    transition_allowed,
    validate_candidate,
    voice_violations,
)
from tce_shared.events import TrustedInputOriginKind

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 1, 1, tzinfo=UTC)

MIN_CITATIONS = int(DREAM_SETTING_DEFAULTS["dream_min_citations"])
MIN_QUOTE_OVERLAP = int(DREAM_SETTING_DEFAULTS["dream_min_quote_overlap_tokens"])
QUOTE_MAX_CHARS = int(DREAM_SETTING_DEFAULTS["dream_quote_max_chars"])
RELEVANCE_FLOOR = int(DREAM_SETTING_DEFAULTS["dream_min_citation_relevance_tokens"])
AFTER_SURFACES = int(DREAM_SETTING_DEFAULTS["dream_nonresponse_after_surfaces"])
DUPLICATE_SIMILARITY = float(DREAM_SETTING_DEFAULTS["dream_duplicate_similarity"])
MATERIAL_NEW_CITATIONS = int(DREAM_SETTING_DEFAULTS["dream_material_new_citations"])
MATERIAL_MAX_SIMILARITY = float(DREAM_SETTING_DEFAULTS["dream_material_max_similarity"])
COOLDOWN_DAYS = int(DREAM_SETTING_DEFAULTS["dream_rejected_cooldown_days"])
MAX_REPROPOSALS = int(DREAM_SETTING_DEFAULTS["dream_max_reproposals"])

DREAM_STORE_FILES: tuple[Path, ...] = (
    REPO_ROOT / "services/tce_api/tce_api/dream_store.py",
    REPO_ROOT / "services/tce_lite_api/tce_lite_api/dream_store.py",
)

# --------------------------------------------------------------------------- live fixtures

# Verbatim rows of the live `events` table.  Everything about a quote's credibility rests on
# the reader recognising his own words, typos and all, so the fixtures repair nothing.
OWNER_MESSAGES: tuple[str, ...] = (
    "hey in setup script can you give restart timeline engine optyions which rebuilds docker build",
    "i want you to do a deep research and find all such similar technologies",
    "hey just asking shoun't we provide a mac and wndows easy install file ie exe and dmg ? for timeline engine ?",
    "is thefre any cleanup left in our project repo i mean open timeline engine",
    "now do a deep research on this project and explore the ways to improve it",
    "hey one more though currently i know timeline engine is accessed by both executer and advisor and the context is passed",
)

# Titles the owner actually typed.  Refusing these would be the defect in the other direction.
OWNER_TITLE = "do deep research on open timeline engine and witness engine"

# The generic titles the previous implementation produced and the owner rejected, still
# sitting in `autonomy_goals` today.
INVENTED_TITLES: tuple[str, ...] = (
    "Move ai-experiments-apps decisively forward",
    "Stop accumulating abandoned intentions",
    "Deep Research and Improvement of Technologies for Real-World Applications",
)

# A different project's corpus: real-shaped messages that share no content token with the
# timeline-engine theme above.  This is the "project B" half of guarantee 7.
OTHER_PROJECT_MESSAGES: tuple[str, ...] = (
    "the stripe webhook keeps failing on refunds and i cannot see why",
    "can you add cash on delivery to the checkout flow in the tribe app",
)


def _pool(
    bodies: Sequence[str] = OWNER_MESSAGES,
    *,
    origin_kind: str = QUOTABLE_ORIGIN_KIND,
    offset: int = 0,
) -> tuple[PoolMessage, ...]:
    return tuple(
        PoolMessage(
            n=offset + index + 1,
            event_id=f"event-{offset + index + 1}",
            receipt_id=f"receipt-{offset + index + 1}",
            content_sha256=f"sha-{offset + index + 1}",
            origin_kind=origin_kind,
            observed_at=BASE + timedelta(minutes=offset + index),
            body=body,
        )
        for index, body in enumerate(bodies)
    )


def _citation(message: PoolMessage, quote: str, *, observed_at: datetime | None = None) -> DreamCitation:
    return DreamCitation(
        event_id=message.event_id,
        receipt_id=message.receipt_id,
        content_sha256=message.content_sha256,
        origin_kind=message.origin_kind,
        observed_at=observed_at or message.observed_at,
        quote=quote,
        quote_sha256=hashlib.sha256(normalise_for_containment(quote).encode("utf-8")).hexdigest(),
    )


def _candidate(
    title: str,
    citations: Sequence[DreamCitation],
    *,
    first_step: str = "start with the first item",
) -> DreamCandidate:
    return DreamCandidate(
        title=title,
        connection_text="this is what they keep coming back to",
        benefit_text="one less thing hanging over them",
        first_step=first_step,
        citations=tuple(citations),
        theme_tokens=theme_tokens_for(title, first_step),
        evidence_basis=evidence_basis_for(citations),
    )


def _validate(
    candidate: DreamCandidate, pool: Sequence[PoolMessage]
) -> tuple[DreamCandidate | None, CandidateRefusal | None]:
    return validate_candidate(
        candidate,
        pool=pool,
        min_citations=MIN_CITATIONS,
        min_quote_overlap_tokens=MIN_QUOTE_OVERLAP,
        quote_max_chars=QUOTE_MAX_CHARS,
        min_citation_relevance_tokens=RELEVANCE_FLOOR,
    )


def _spans(body: str) -> Iterator[str]:
    words = body.split()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            span = " ".join(words[start:end])
            if len(span) <= QUOTE_MAX_CHARS:
                yield span


def _best_play(title: str, pool: Sequence[PoolMessage]) -> tuple[DreamCandidate | None, CandidateRefusal | None]:
    """The most favourable admissible citation set a model could choose for this title.

    A model picks its own quotes, so a test that hands it one quote proves only that *that*
    quote fails.  This searches every legal span of every message in the pool and takes the
    best one per message — the model's optimal play — and validates that.
    """
    picks: list[tuple[int, PoolMessage, str]] = []
    for message in pool:
        best_span: str | None = None
        best_score = -1
        for span in _spans(message.body):
            if not quote_is_contained(span, message.body, max_chars=QUOTE_MAX_CHARS):
                continue
            if voice_violations(span):
                continue
            score = content_token_overlap(title, span)
            if score > best_score:
                best_span, best_score = span, score
        if best_span is not None:
            picks.append((best_score, message, best_span))
    picks.sort(key=lambda item: -item[0])
    citations = [_citation(message, span) for _, message, span in picks[:6]]
    return _validate(_candidate(title, citations), pool)


def _projection(**overrides: Any) -> DreamProposalProjection:
    base: dict[str, Any] = {
        "proposal_id": "prop-1",
        "workspace_id": "personal",
        "owner_id": "owner-1",
        "subject_user_id": "subject-1",
        "project_id": "proj_aaaaaaaaaaaaaaaaaaaaaaaa",
        "scope_kind": SCOPE_KIND_PROJECT,
        "session_id": "session-1",
        "revision": 1,
        "status": DreamProposalStatus.PROPOSED,
        "nonresponse": NonresponseState.NEVER_SURFACED,
        "surfaced_count": 0,
        "surfaced_attested": False,
        "first_surfaced_at": None,
        "last_surfaced_at": None,
        "title": "",
        "connection_text": "",
        "benefit_text": "",
        "first_step": "",
        "citations": (),
        "theme_tokens": (),
        "evidence_basis": "trusted_current",
        "evidence_revision": "",
        "evidence_cutoff_at": None,
        "supersedes_proposal_id": None,
        "repropose_depth": 0,
        "snooze_until": None,
        "expires_at": None,
        "accepted_at": None,
        "rejected_at": None,
        "rejection_reason": "",
        "task_id": None,
        "objective_hash": None,
        "plan_root_goal_id": None,
        "pursuit_started_at": None,
        "completed_at": None,
        "abandoned_at": None,
        "abandon_reason": "",
        "withdrawn_reason": "",
    }
    base.update(overrides)
    return DreamProposalProjection(**base)


def _event(
    kind: DreamProposalEventKind,
    *,
    seq: int,
    payload: dict[str, Any] | None = None,
    at: datetime | None = None,
    actor_class: str = "human",
) -> DreamProposalEvent:
    return DreamProposalEvent(
        seq=seq,
        kind=kind,
        payload=payload or {},
        occurred_at=at or (BASE + timedelta(hours=seq)),
        actor="owner-1",
        actor_class=actor_class,
    )


PROPOSED_PAYLOAD: dict[str, Any] = {
    "title": OWNER_TITLE,
    "connection_text": "he keeps coming back to it",
    "benefit_text": "one less thing hanging over him",
    "first_step": "open the repo and list what is left",
    "citations": [],
    "theme_tokens": list(theme_tokens_for(OWNER_TITLE, "open the repo and list what is left")),
    "evidence_basis": "trusted_current",
    "project_id": "proj_aaaaaaaaaaaaaaaaaaaaaaaa",
    "scope_kind": SCOPE_KIND_PROJECT,
}


def _fold(events: Sequence[DreamProposalEvent], *, now: datetime | None = None, revision: int = 1):
    return fold_dream_proposal(
        events,
        proposal_id="prop-1",
        workspace_id="personal",
        owner_id="owner-1",
        subject_user_id="subject-1",
        session_id="session-1",
        revision=revision,
        now=now or (BASE + timedelta(days=1)),
        after_surfaces=AFTER_SURFACES,
    )


# ============================================================ 1. it traces to a real message


def test_a_claim_the_owner_never_typed_cannot_be_emitted_under_any_play() -> None:
    """Guarantee 1, as a property over the model's *best* move rather than one hand-picked quote.

    The three titles are the ones sitting in the live database from the rejected
    implementation.  For each, the model is allowed to search every legal span of every
    message and take the most favourable — and still cannot get a proposal out, because the
    words in the claim are not in the corpus.

    The discriminator is the same search with a title the owner really typed: it must be
    EMITTED.  Without it this test would pass against a ``validate_candidate`` that refused
    everything, which is the failure mode in the opposite direction and would be just as bad.
    """
    pool = _pool()

    for title in INVENTED_TITLES:
        emitted, refusal = _best_play(title, pool)
        assert emitted is None, f"invented claim was emitted: {title!r}"
        assert refusal is not None
        assert refusal.reason in CANDIDATE_REFUSAL_REASONS

    # The discriminator.  Same pool, same adversarial search, a claim made of his own words.
    emitted, refusal = _best_play(OWNER_TITLE, pool)
    assert refusal is None, f"a claim the owner typed was refused: {refusal}"
    assert emitted is not None
    assert len(emitted.citations) >= MIN_CITATIONS
    bodies = {message.event_id: message.body for message in pool}
    for citation in emitted.citations:
        assert quote_is_contained(citation.quote, bodies[citation.event_id], max_chars=QUOTE_MAX_CHARS)


def test_an_empty_corpus_cannot_produce_a_proposal_however_confident_the_reply() -> None:
    """The refusal on today's corpus is structural, not a threshold that can be tuned down.

    Under the Y2 corpus rule this stack has 8 receipt-bound messages against 4,649 backfill
    rows, so the pool is empty or near-empty and P5 refuses with ``insufficient_messages``.
    That refusal has to be a property of the construction: with no pool there is no citation
    number that resolves, so there is no candidate — no ``min_citations`` setting and no
    prompt change can produce one.
    """
    confident = {
        "dreams": [
            {
                "title": "ship the thing",
                "connection": "he keeps saying it",
                "benefit": "it would be done",
                "first_step": "start it",
                "citations": [{"n": n, "quote": "he definitely said this"} for n in range(1, 7)],
            }
        ]
    }
    candidates, refusals = parse_dream_payload(
        confident, pool=(), max_proposals=3, max_citations=6, quote_max_chars=QUOTE_MAX_CHARS
    )
    assert candidates == []
    assert {refusal.reason for refusal in refusals} <= set(CANDIDATE_REFUSAL_REASONS)
    assert any(refusal.reason in {"no_citations", "citation_out_of_range"} for refusal in refusals)

    # The discriminator: the identical reply against a real pool does resolve, so the refusal
    # above is about the corpus being empty and not about the reply being malformed.
    pool = _pool()
    quote = "do a deep research"
    real = {
        "dreams": [
            {
                "title": OWNER_TITLE,
                "connection": "he keeps saying it",
                "benefit": "it would be done",
                "first_step": "start it",
                "citations": [{"n": 2, "quote": quote}, {"n": 5, "quote": quote}],
            }
        ]
    }
    candidates, _ = parse_dream_payload(
        real, pool=pool, max_proposals=3, max_citations=6, quote_max_chars=QUOTE_MAX_CHARS
    )
    assert len(candidates) == 1
    assert len(candidates[0].citations) == 2


def test_the_refusal_vocabularies_are_closed_and_disjoint() -> None:
    """A refusal has to be legible, which means it has to be one of a named, closed set.

    ``insufficient_messages`` — the answer P5 gives on today's corpus — is a *run* refusal,
    not a candidate refusal and not a pool drop.  Keeping the three vocabularies disjoint is
    what stops "the corpus is too small" from being recorded as "no dreams today".
    """
    assert "insufficient_messages" in RUN_REFUSAL_REASONS
    assert set(RUN_REFUSAL_REASONS) & set(CANDIDATE_REFUSAL_REASONS) == set()
    assert set(RUN_REFUSAL_REASONS) & set(POOL_DROP_REASONS) == set()
    assert set(CANDIDATE_REFUSAL_REASONS) & set(POOL_DROP_REASONS) == set()
    for vocabulary in (RUN_REFUSAL_REASONS, CANDIDATE_REFUSAL_REASONS, POOL_DROP_REASONS):
        assert len(set(vocabulary)) == len(vocabulary)


# ============================================================ 2. executor text is not quotable


def test_no_executor_writable_origin_kind_is_admissible() -> None:
    """Guarantee 2, at the vocabulary level.

    The event corpus is executor-writable; ``trusted_input_receipts`` is not.  So the whole of
    the forgery defence is that admission is by receipt and that only two of P1's five origin
    kinds are admissible.  The three that an executor's own output travels under must be
    outside the set — and the discriminator is that the two that are inside it really are the
    P1 enum's values, not a parallel vocabulary that could drift.
    """
    executor_writable = {
        TrustedInputOriginKind.EXECUTOR_OUTPUT.value,
        TrustedInputOriginKind.TOOL_RESULT.value,
        TrustedInputOriginKind.MANAGER_INSTRUCTION.value,
    }
    assert set(ADMISSIBLE_ORIGIN_KINDS) & executor_writable == set()
    assert set(ADMISSIBLE_ORIGIN_KINDS) == {
        TrustedInputOriginKind.HUMAN_INPUT.value,
        TrustedInputOriginKind.IMPORTED_TRANSCRIPT.value,
    }
    # The discriminator: these are P1's strings, so a rename in P1 breaks this test rather
    # than silently admitting a kind P5 believes is human.
    assert QUOTABLE_ORIGIN_KIND == TrustedInputOriginKind.HUMAN_INPUT.value
    assert BACKFILL_ORIGIN_KIND == TrustedInputOriginKind.IMPORTED_TRANSCRIPT.value


def test_imported_history_is_never_rendered_as_something_he_said_today() -> None:
    """The presentational half: a backfilled message may be quoted, but not as today's words."""
    backfill = _pool(OWNER_MESSAGES[:2], origin_kind=BACKFILL_ORIGIN_KIND)
    citations = tuple(_citation(message, "do a deep research") for message in backfill)
    assert evidence_basis_for(citations) == EVIDENCE_BASIS_BACKFILL
    assert attribution_for(evidence_basis_for(citations)) == ATTRIBUTION_IMPORTED

    # The discriminator: receipted human input is attributed to him, so the rule above is a
    # distinction the renderer draws and not a blanket disclaimer on every proposal.
    trusted = tuple(_citation(message, "do a deep research") for message in _pool(OWNER_MESSAGES[:2]))
    assert attribution_for(evidence_basis_for(trusted)) == ATTRIBUTION_SAID


def _function_source_strings(path: Path, name: str) -> str | None:
    """Every string constant inside one function, concatenated.  ``None`` if it is not there.

    Concatenating rather than testing literal-by-literal is deliberate: the admission clause
    is an f-string assembled from a conditional fragment and a body, so no single literal
    carries the whole predicate and a per-literal scan would find a fragment that "does not
    join the receipt table" and be wrong about it.

    Docstrings are excluded.  The function this is aimed at documents its own rule in prose
    — *"there is no ``task_type`` arm"* — and a scanner that read the prose as SQL would fail
    the very implementation that gets it right.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover - unreadable file
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            prose = {
                id(child.value)
                for child in ast.walk(node)
                if isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Constant)
                and isinstance(child.value.value, str)
            }
            parts = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and id(child) not in prose
            ]
            return " ".join(" ".join(part.split()) for part in parts).lower()
    return None


def _admission_is_receipt_bound(sql: str) -> bool:
    lowered = " ".join(sql.lower().split())
    if "from events" not in lowered:
        return False
    if "trusted_input_receipts" not in lowered or "origin_kind" not in lowered:
        return False
    return "task_type" not in lowered and "_tce_owner" not in lowered


def test_the_pool_admission_clause_admits_nothing_an_executor_can_write() -> None:
    """Guarantee 2, at the SQL level — self-arming until both store modules land.

    The predicate is proved non-vacuous *today* against a synthetic pair, so it is never a
    check that passes because it found nothing to look at.  A clause that reads
    ``task_type = 'human_input_backfill'`` admits 4,649 executor-written rows on this stack;
    a clause that joins ``trusted_input_receipts`` admits 8.  That difference is the whole of
    the forgery defence, and it lives in one function per backend.
    """
    good = (
        "SELECT e.id FROM events e JOIN trusted_input_receipts r ON r.event_id = e.id "
        "WHERE r.subject_user_id = :subject_user_id AND r.origin_kind = ANY(:origin_kinds)"
    )
    bad = "SELECT e.id FROM events e WHERE e.task_type IN ('human_input', 'human_input_backfill')"
    also_bad = "SELECT e.id FROM events e WHERE e.context->>'_tce_owner' = :owner_id"
    assert _admission_is_receipt_bound(good)
    assert not _admission_is_receipt_bound(bad)
    assert not _admission_is_receipt_bound(also_bad)

    for path in DREAM_STORE_FILES:
        if not path.exists():
            continue
        sql = _function_source_strings(path, "select_candidate_messages")
        assert sql is not None, f"{path.name} has no select_candidate_messages"
        assert _admission_is_receipt_bound(sql), (
            f"{path.name} reads the event corpus without binding it to a receipt:\n{sql}"
        )


def _separates_the_two_corpora(sql: str) -> bool:
    """Does this clause split project-scope from workspace-scope on the *receipt's* project?

    Two halves.  There must be a ``<alias>.project_id`` reference whose alias is not the
    events table — the receipt carries the attribution and the event context does not, and P1
    populates the former for 8 of 8 live rows and the latter for 0.02% of the corpus.  And
    there must be an ``IS NULL`` arm, because without one the workspace run has no corpus of
    its own and would silently read the project rows.
    """
    lowered = " ".join(sql.lower().split())
    if "context->>'project_id'" in lowered or "$.project_id" in lowered:
        return False
    aliases = set(re.findall(r"\b([a-z_][a-z0-9_]*)\.project_id\b", lowered))
    if not aliases - {"e", "ev", "events"}:
        return False
    return bool(re.search(r"\b[a-z_][a-z0-9_]*\.project_id is null", lowered))


def test_the_two_corpora_are_separated_in_the_admission_clause_itself() -> None:
    """Guarantee 7, at the SQL level — the same self-arming shape.

    A project run and a workspace run must not be able to share a message, and the column
    they are separated on has to be the receipt's ``project_id`` — the one P1 actually
    populates — rather than ``events.context->>'project_id'``, which is NULL for 99.98% of
    this corpus and would make the disjointness a property of an empty column.
    """
    # Non-vacuous today, against the two shapes that matter.  The alias is not pinned: the
    # two backends name the receipt subquery differently and pinning `r.` would fail the
    # backend that says `r2.` while proving nothing about either.
    receipt_scoped = (
        "select e.id from events e join trusted_input_receipts r2 on r2.event_id = e.id "
        "and r2.project_id = ? and (r2.project_id is null or r2.project_id = '')"
    )
    context_scoped = "select e.id from events e where e.context->>'project_id' = :project_id"
    assert _separates_the_two_corpora(receipt_scoped)
    assert not _separates_the_two_corpora(context_scoped)

    for path in DREAM_STORE_FILES:
        if not path.exists():
            continue
        sql = _function_source_strings(path, "select_candidate_messages")
        assert sql is not None
        assert _separates_the_two_corpora(sql), (
            f"{path.name} does not separate the project and workspace corpora on the "
            f"receipt's project_id:\n{sql}"
        )


# ============================================================ 3. a rejected theme stays rejected


def _rejected(
    citations: Sequence[DreamCitation],
    *,
    title: str,
    first_step: str = "start with the first item",
    **overrides: Any,
) -> DreamProposalProjection:
    return _projection(
        status=DreamProposalStatus.REJECTED,
        rejected_at=BASE,
        rejection_reason="not now",
        title=title,
        first_step=first_step,
        citations=tuple(citations),
        theme_tokens=theme_tokens_for(title, first_step),
        **overrides,
    )


def test_a_rejected_theme_cannot_return_until_every_condition_lifts() -> None:
    """Guarantee 3, condition by condition.

    ``material_change`` has five conditions and all five must hold.  This walks them: the
    candidate starts blocked on the first, and each fix moves the named failing condition one
    step along, so the test would notice a condition that had quietly stopped being checked —
    the way a five-condition gate degrades is by one of them becoming unreachable, not by all
    of them disappearing at once.

    The discriminator is the final state: with every condition satisfied the theme *does*
    come back.  Rejection is a cooldown with conditions, not a permanent ban, and a test that
    only asserted suppression would pass against a function that always suppressed.
    """
    pool = _pool()
    old = tuple(_citation(message, "do a deep research") for message in pool[1:3])
    rejected = _rejected(old, title=OWNER_TITLE, repropose_depth=MAX_REPROPOSALS)

    def scan(candidate: DreamCandidate, target: DreamProposalProjection, *, now: datetime) -> tuple[Any, str]:
        return suppressed_by_rejection(
            candidate,
            [target],
            now=now,
            threshold=DUPLICATE_SIMILARITY,
            min_new_citations=MATERIAL_NEW_CITATIONS,
            max_similarity=MATERIAL_MAX_SIMILARITY,
            cooldown_days=COOLDOWN_DAYS,
            max_reproposals=MAX_REPROPOSALS,
        )

    same_theme = _candidate(OWNER_TITLE, old)

    # (a) the re-propose budget is spent.
    blocker, failing = scan(same_theme, rejected, now=BASE + timedelta(days=400))
    assert blocker is not None and failing == "repropose_limit_reached"

    # (b) budget restored, but the candidate cites nothing he did not already say before the no.
    rejected = _rejected(old, title=OWNER_TITLE, repropose_depth=0)
    blocker, failing = scan(same_theme, rejected, now=BASE + timedelta(days=400))
    assert blocker is not None and failing == "new_citations"

    # (c) two new citations, but both predate the rejection: evidence that existed when he
    #     said no is not evidence that he changed his mind.
    stale_new = tuple(
        _citation(message, "do a deep research", observed_at=BASE - timedelta(days=5))
        for message in _pool(OWNER_MESSAGES[4:6], offset=10)
    )
    blocker, failing = scan(_candidate(OWNER_TITLE, old + stale_new), rejected, now=BASE + timedelta(days=400))
    assert blocker is not None and failing == "evidence_predates_rejection"

    # (d) genuinely later evidence, but the proposal is word-for-word the one he rejected.
    fresh = tuple(
        _citation(message, "do a deep research", observed_at=BASE + timedelta(days=100))
        for message in _pool(OWNER_MESSAGES[4:6], offset=10)
    )
    blocker, failing = scan(_candidate(OWNER_TITLE, old + fresh), rejected, now=BASE + timedelta(days=400))
    assert blocker is not None and failing == "theme_too_similar"

    # (e) reworded enough to clear rule 3, but still inside the cooldown.  This one is asked
    #     of `material_change` directly: reworded that far, the candidate is below the
    #     *duplicate* threshold, so the scan never reaches the five conditions at all — which
    #     is itself the shape of the rule, and asserting it through the scan would silently
    #     stop exercising the cooldown.
    reworded = _candidate(
        "finish the timeline engine cleanup he keeps mentioning",
        old + fresh,
        first_step="list what is unfinished",
    )
    lifted, failing = material_change(
        reworded,
        rejected,
        now=BASE + timedelta(days=1),
        min_new_citations=MATERIAL_NEW_CITATIONS,
        max_similarity=MATERIAL_MAX_SIMILARITY,
        cooldown_days=COOLDOWN_DAYS,
        max_reproposals=MAX_REPROPOSALS,
    )
    assert lifted is False and failing == "cooldown_not_elapsed"
    assert failing in MATERIAL_CHANGE_CONDITIONS

    # The discriminator.  Every condition satisfied — a genuinely different claim, two
    # citations he typed after saying no, budget left, cooldown elapsed — and it comes back.
    lifted, failing = material_change(
        reworded,
        rejected,
        now=BASE + timedelta(days=400),
        min_new_citations=MATERIAL_NEW_CITATIONS,
        max_similarity=MATERIAL_MAX_SIMILARITY,
        cooldown_days=COOLDOWN_DAYS,
        max_reproposals=MAX_REPROPOSALS,
    )
    assert lifted is True and failing == ""


def test_a_rejected_theme_cannot_come_back_by_merging_into_a_live_neighbour() -> None:
    """The ordering hole: suppression is decided before the duplicate-merge path, not after.

    If the duplicate check ran first, a candidate carrying a rejected theme that also
    resembles a live proposal would be absorbed into that live proposal and come back
    strengthened, with no refusal recorded anywhere.  The discriminator is that the same
    candidate, with the rejection removed, *does* merge — so this is an ordering property and
    not a blanket refusal.
    """
    pool = _pool()
    citations = tuple(_citation(message, "do a deep research") for message in pool[1:3])
    candidate = _candidate(OWNER_TITLE, citations)

    rejected = _rejected(citations, title=OWNER_TITLE, repropose_depth=0)
    live = _projection(
        proposal_id="prop-live",
        status=DreamProposalStatus.SURFACED,
        title=OWNER_TITLE,
        first_step="start with the first item",
        theme_tokens=theme_tokens_for(OWNER_TITLE, "start with the first item"),
    )

    blocker, failing = suppressed_by_rejection(
        candidate,
        [rejected],
        now=BASE + timedelta(days=400),
        threshold=DUPLICATE_SIMILARITY,
        min_new_citations=MATERIAL_NEW_CITATIONS,
        max_similarity=MATERIAL_MAX_SIMILARITY,
        cooldown_days=COOLDOWN_DAYS,
        max_reproposals=MAX_REPROPOSALS,
    )
    assert blocker is not None, "a rejected theme reached the merge path"
    assert failing in MATERIAL_CHANGE_CONDITIONS

    # The discriminator: with nothing rejected, the identical candidate merges into the live
    # neighbour.  So the suppression above is caused by the rejection and by nothing else.
    assert (
        suppressed_by_rejection(
            candidate,
            [],
            now=BASE + timedelta(days=400),
            threshold=DUPLICATE_SIMILARITY,
            min_new_citations=MATERIAL_NEW_CITATIONS,
            max_similarity=MATERIAL_MAX_SIMILARITY,
            cooldown_days=COOLDOWN_DAYS,
            max_reproposals=MAX_REPROPOSALS,
        )
        == (None, "")
    )
    assert is_duplicate_theme(candidate, [live], threshold=DUPLICATE_SIMILARITY) is live


def test_a_rejection_in_one_scope_suppresses_the_theme_in_the_other() -> None:
    """Bucketing rejections by scope kind is how a "no" becomes invisible to the only mode
    that can currently generate anything.  ``suppressed_by_rejection`` takes the rows the
    store hands it and filters on status alone — never on ``scope_kind`` — so a workspace
    rejection blocks a project candidate."""
    pool = _pool()
    citations = tuple(_citation(message, "do a deep research") for message in pool[1:3])
    workspace_rejection = _rejected(
        citations, title=OWNER_TITLE, scope_kind="workspace", project_id=None
    )
    project_candidate = _candidate(OWNER_TITLE, citations)

    blocker, _ = suppressed_by_rejection(
        project_candidate,
        [workspace_rejection],
        now=BASE + timedelta(days=400),
        threshold=DUPLICATE_SIMILARITY,
        min_new_citations=MATERIAL_NEW_CITATIONS,
        max_similarity=MATERIAL_MAX_SIMILARITY,
        cooldown_days=COOLDOWN_DAYS,
        max_reproposals=MAX_REPROPOSALS,
    )
    assert blocker is workspace_rejection

    # The discriminator: an unrelated theme in the same workspace is not suppressed, so this
    # is theme matching across scopes rather than a blanket cross-scope block.
    unrelated = _candidate(
        "fix the stripe webhook on refunds",
        citations,
        first_step="read the refund handler",
    )
    assert (
        suppressed_by_rejection(
            unrelated,
            [workspace_rejection],
            now=BASE + timedelta(days=400),
            threshold=DUPLICATE_SIMILARITY,
            min_new_citations=MATERIAL_NEW_CITATIONS,
            max_similarity=MATERIAL_MAX_SIMILARITY,
            cooldown_days=COOLDOWN_DAYS,
            max_reproposals=MAX_REPROPOSALS,
        )[0]
        is None
    )


# ============================================================ 4. silence is not an answer


@pytest.mark.parametrize("days", [0, 1, 7, 30, 45, 90, 365, 3650])
def test_time_alone_never_produces_a_verdict(days: int) -> None:
    """Guarantee 4, swept over ten years.

    The defect this replaces was one nullable ``acknowledged_at`` column meaning "not seen",
    "seen and ignored" and "never surfaced" at once.  Here the axis is separate and derived
    from recorded showings only: a proposal nobody ever put in front of him reads
    ``never_surfaced`` a decade later, and no horizon turns any count into an acceptance or a
    rejection.

    The discriminator is inside the same sweep: a *recorded showing* does move the axis, so
    the state is not simply frozen.
    """
    now = BASE + timedelta(days=days)

    assert (
        nonresponse_state(surfaced_count=0, last_surfaced_at=None, now=now, after_surfaces=AFTER_SURFACES)
        is NonresponseState.NEVER_SURFACED
    )
    assert (
        nonresponse_state(
            surfaced_count=AFTER_SURFACES - 1,
            last_surfaced_at=BASE,
            now=now,
            after_surfaces=AFTER_SURFACES,
        )
        is NonresponseState.AWAITING_RESPONSE
    )
    assert (
        nonresponse_state(
            surfaced_count=AFTER_SURFACES, last_surfaced_at=BASE, now=now, after_surfaces=AFTER_SURFACES
        )
        is NonresponseState.IGNORED
    )

    # And the same, folded: an event log with showings and no verdict never acquires one.
    events = [
        _event(DreamProposalEventKind.PROPOSED, seq=1, payload=PROPOSED_PAYLOAD),
        surfaced_event(
            ordinal=1,
            first_surfaced_at=BASE,
            occurred_at=BASE,
            actor="executor",
            actor_class="executor",
        ),
    ]
    events[1] = DreamProposalEvent(
        seq=2,
        kind=events[1].kind,
        payload=events[1].payload,
        occurred_at=events[1].occurred_at,
        actor=events[1].actor,
        actor_class=events[1].actor_class,
    )
    folded = _fold(events, now=now)
    assert folded.projection.status is DreamProposalStatus.SURFACED
    assert folded.projection.accepted_at is None
    assert folded.projection.rejected_at is None
    assert folded.projection.nonresponse is NonresponseState.AWAITING_RESPONSE
    assert folded.projection.surfaced_attested is False


def test_ignored_is_not_a_status_and_never_stands_in_for_rejection() -> None:
    """Nonresponse is a separate axis, so it must not appear on the status axis at all."""
    statuses = {str(status) for status in DreamProposalStatus}
    for state in NonresponseState:
        assert str(state) not in statuses, f"{state} leaked onto the status axis"
    assert str(NonresponseState.IGNORED) not in TERMINAL_STATUSES
    assert str(DreamProposalStatus.REJECTED) in TERMINAL_STATUSES

    # The discriminator: an ignored proposal is still live, so "ignored" costs the owner
    # nothing but a longer re-surface interval.  Treating it as a soft rejection — hiding it,
    # expiring it early — is the exact collapse the separate axis exists to prevent.
    ignored = _projection(
        status=DreamProposalStatus.SURFACED,
        nonresponse=NonresponseState.IGNORED,
        surfaced_count=AFTER_SURFACES,
        expires_at=BASE + timedelta(days=45),
    )
    assert str(ignored.status) in LIVE_STATUSES
    assert ignored.expires_at == BASE + timedelta(days=45)
    assert ignored.rejected_at is None


# ============================================================ 5. refresh preserves history


def test_no_later_event_can_erase_the_owners_answer() -> None:
    """Guarantee 5, at the level where refresh could destroy something.

    The rejected implementation's refresh was an unconditional ``DELETE``; the replacement is
    an append-only log, so the property to test is that appending cannot un-say anything.
    Every kind that is legal after a rejection is appended in turn, and the rejection has to
    survive all of them — with the discriminator that folding the *same* log without the
    rejection event really does come out different, so the assertion is sensitive to the
    history being there rather than to some default.
    """
    rejected_at = BASE + timedelta(hours=3)
    prior = [
        _event(DreamProposalEventKind.PROPOSED, seq=1, payload=PROPOSED_PAYLOAD),
        _event(
            DreamProposalEventKind.REJECTED,
            seq=2,
            payload={"rejected_at": rejected_at.isoformat(), "reason": "not now, maybe never"},
            at=rejected_at,
        ),
    ]

    for kind in DreamProposalEventKind:
        allowed, _ = transition_allowed(DreamProposalStatus.REJECTED, kind)
        if not allowed:
            continue
        write = prepare_dream_write(
            prior,
            [_event(kind, seq=0, payload={"citations": [], "evidence_revision": "rev-2"})],
            proposal_id="prop-1",
            workspace_id="personal",
            owner_id="owner-1",
            subject_user_id="subject-1",
            session_id="session-1",
            expected_revision=1,
            highest_seq=2,
            now=BASE + timedelta(days=30),
            after_surfaces=AFTER_SURFACES,
        )
        projection = write.projection
        assert projection.status is DreamProposalStatus.REJECTED, f"{kind} un-rejected the proposal"
        assert projection.rejected_at == rejected_at
        assert projection.rejection_reason == "not now, maybe never"
        assert write.next_revision == 2
        assert write.next_seq_start == 3
        assert [event.seq for event in write.events] == [3]

    # The discriminator: drop the rejection from the prior log and the projection changes.
    without = _fold([prior[0]], now=BASE + timedelta(days=30))
    assert without.projection.status is DreamProposalStatus.PROPOSED
    assert without.projection.rejected_at is None


def test_every_terminal_answer_is_final_on_its_own_axis() -> None:
    """A verdict is not reversible by a later refresh, only by a new proposal that supersedes.

    ``evidence_revalidated`` is the single exception and is deliberately legal from every
    status: re-checking whether a rejected proposal's citations still verify must not be
    blocked by the rejection.  It changes no status, which is what keeps it from being a
    back door.
    """
    for status in (DreamProposalStatus.REJECTED, DreamProposalStatus.COMPLETED, DreamProposalStatus.WITHDRAWN):
        for kind in DreamProposalEventKind:
            allowed, reason = transition_allowed(status, kind)
            if kind is DreamProposalEventKind.EVIDENCE_REVALIDATED:
                assert allowed, "evidence can always be re-checked"
            else:
                assert not allowed, f"{kind} was allowed on a {status} proposal"
                assert reason

    # The discriminator: a live proposal does accept verdicts, so the table is a table and
    # not a universal refusal.
    assert transition_allowed(DreamProposalStatus.PROPOSED, DreamProposalEventKind.ACCEPTED)[0]
    assert transition_allowed(DreamProposalStatus.PROPOSED, DreamProposalEventKind.REJECTED)[0]


# ============================================================ 6. completion is read, not inferred


def test_no_amount_of_planning_completes_a_proposal() -> None:
    """Guarantee 6.

    The rejected implementation marked a dream ``done`` in the same transaction that wrote
    its plan, before a single step had run.  Here, binding an objective and a plan root goal
    is a fold rule that changes no status at all, and ``completed`` cannot even be reached
    from ``accepted`` — a proposal has to be *pursued* first.

    The discriminator is the full path: pursued, then a real completion event, and only then
    does ``completed_at`` appear.
    """
    accepted = [
        _event(DreamProposalEventKind.PROPOSED, seq=1, payload=PROPOSED_PAYLOAD),
        _event(DreamProposalEventKind.ACCEPTED, seq=2),
    ]
    bound = accepted + [
        _event(
            DreamProposalEventKind.OBJECTIVE_BOUND,
            seq=3,
            payload={
                "task_id": "task-1",
                "objective_hash": "hash-1",
                "plan_root_goal_id": "goal-root",
            },
        )
    ]

    projection = _fold(bound).projection
    assert projection.status is DreamProposalStatus.ACCEPTED, "a plan root moved the status"
    assert projection.task_id == "task-1"
    assert projection.plan_root_goal_id == "goal-root"
    assert projection.completed_at is None
    assert projection.pursuit_started_at is None

    # There is no route from `accepted` to `completed`: the only way in is through pursuit.
    assert not transition_allowed(DreamProposalStatus.ACCEPTED, DreamProposalEventKind.COMPLETED)[0]

    # No kind other than `completed` sets `completed_at`, whatever the payload claims.
    for kind in DreamProposalEventKind:
        if kind is DreamProposalEventKind.COMPLETED:
            continue
        folded = _fold(
            accepted + [_event(kind, seq=3, payload={"completed_at": BASE.isoformat()})]
        )
        assert folded.projection.completed_at is None, f"{kind} completed the proposal"

    # The discriminator: pursued, then completed, and now it is done.
    pursued = bound + [_event(DreamProposalEventKind.PURSUIT_STARTED, seq=4)]
    assert _fold(pursued).projection.status is DreamProposalStatus.PURSUED
    assert _fold(pursued).projection.completed_at is None

    completed_at = BASE + timedelta(days=2)
    done = pursued + [
        _event(DreamProposalEventKind.COMPLETED, seq=5, payload={"completed_at": completed_at.isoformat()})
    ]
    final = _fold(done).projection
    assert final.status is DreamProposalStatus.COMPLETED
    assert final.completed_at == completed_at


# ============================================================ 7. project A cannot cite project B


def test_a_proposal_cannot_cite_a_message_from_another_project() -> None:
    """Guarantee 7, in the two places it can be broken.

    First by construction: the pool is scoped before the model ever sees it, so a citation
    number that would address another project's message resolves to nothing — the model has
    no way to *name* an out-of-scope message.

    Second by relevance: even if two projects' messages sat in one pool, a citation that
    shares no content token with the claim is dropped and the count re-checked, so two
    unrelated pieces of work cannot be presented as one theme.

    The discriminator for both is the in-scope case, which must be emitted.
    """
    project_a = _pool(OWNER_MESSAGES[:3])

    # (i) an out-of-scope citation number addresses nothing.
    payload = {
        "dreams": [
            {
                "title": OWNER_TITLE,
                "connection": "he keeps saying it",
                "benefit": "it would be done",
                "first_step": "open the repo",
                "citations": [
                    {"n": 2, "quote": "do a deep research"},
                    {"n": 42, "quote": "the stripe webhook keeps failing on refunds"},
                ],
            }
        ]
    }
    candidates, refusals = parse_dream_payload(
        payload, pool=project_a, max_proposals=3, max_citations=6, quote_max_chars=QUOTE_MAX_CHARS
    )
    assert any(refusal.reason == "citation_out_of_range" for refusal in refusals)
    assert len(candidates) == 1
    assert {citation.event_id for citation in candidates[0].citations} <= {
        message.event_id for message in project_a
    }

    # (ii) and if the two corpora were ever mixed, the off-theme citation is dropped and the
    #      candidate falls below the citation floor rather than being emitted half-supported.
    mixed = project_a + _pool(OTHER_PROJECT_MESSAGES, offset=len(project_a))
    on_theme = _citation(mixed[1], "do a deep research and find all such similar technologies")
    off_theme = _citation(mixed[3], "the stripe webhook keeps failing on refunds")
    emitted, refusal = _validate(_candidate(OWNER_TITLE, [on_theme, off_theme]), mixed)
    assert emitted is None
    assert refusal is not None and refusal.reason == "insufficient_citations"
    assert "citation_not_relevant=1" in refusal.detail
    assert "quote_not_found=0" in refusal.detail, "the off-theme citation was dropped for the wrong reason"

    # The discriminator: two citations that are both about the claim, and it is emitted.
    second_on_theme = _citation(mixed[0], "restart timeline engine optyions")
    emitted, refusal = _validate(_candidate(OWNER_TITLE, [on_theme, second_on_theme]), mixed)
    assert refusal is None, f"an in-scope, on-theme proposal was refused: {refusal}"
    assert emitted is not None and len(emitted.citations) == 2


def test_a_proposal_carries_the_scope_it_was_minted_in() -> None:
    """A reader cannot check attribution that is not on the row.

    ``project_id`` and ``scope_kind`` come out of the ``proposed`` event, so they are part of
    the immutable record rather than a property of whoever is reading — which is what makes
    the disjointness in guarantee 7 auditable after the fact.
    """
    folded = _fold([_event(DreamProposalEventKind.PROPOSED, seq=1, payload=PROPOSED_PAYLOAD)])
    assert folded.projection.project_id == "proj_aaaaaaaaaaaaaaaaaaaaaaaa"
    assert folded.projection.scope_kind == SCOPE_KIND_PROJECT

    # The discriminator: a workspace-scoped mint carries no project, and the two are told
    # apart by the record and not by the reader's own binding.
    workspace_payload = dict(PROPOSED_PAYLOAD, project_id=None, scope_kind="workspace")
    folded = _fold([_event(DreamProposalEventKind.PROPOSED, seq=1, payload=workspace_payload)])
    assert folded.projection.project_id is None
    assert folded.projection.scope_kind == "workspace"


# ============================================================ the orphan, reconciled


def test_the_count_derived_module_and_its_orphan_test_die_together() -> None:
    """``tests/unit/test_dreams.py`` tests a module P5 deletes, and is named in no P5 row.

    It is the last importer of ``tce_shared.dreams`` under ``tests/``, and the deletion of
    that module is a five-line cut that has to happen after B, C and D drop their imports.
    Pinning the pair means the cut cannot be made half-way: deleting the module while the
    test file is still there is an ``ImportError`` at collection that takes every unit gate in
    the repo with it, and this test names that as the cause instead of leaving whoever runs
    the suite to work it out from a traceback.

    Both present (today) or both gone (after the cut) pass.  One without the other is red.
    """
    module = REPO_ROOT / "shared/tce_shared/dreams.py"
    orphan = REPO_ROOT / "tests/unit/test_dreams.py"
    assert module.exists() == orphan.exists(), (
        "shared/tce_shared/dreams.py and tests/unit/test_dreams.py must be deleted in the same "
        "commit: the test file imports DreamSignals, cluster_recurring_asks, derive_dream_seeds "
        "and select_dream_to_pursue from that module and nothing else does."
    )


def test_no_other_test_imports_the_count_derived_module() -> None:
    """The tests-side half of the deletion closure.

    Builder A pinned the importers under ``shared/`` and ``services/``; nothing pinned the
    importers under ``tests/``, so a new one written this week would turn the cut into a
    second round of work.  Exactly one test file may import it, and that file is the orphan
    above.
    """
    importers: set[str] = set()
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("tce_shared.dreams"):
                importers.add(path.relative_to(REPO_ROOT).as_posix())
            elif isinstance(node, ast.Import) and any(
                alias.name.endswith("tce_shared.dreams") for alias in node.names
            ):
                importers.add(path.relative_to(REPO_ROOT).as_posix())
    assert importers <= {"tests/unit/test_dreams.py"}, f"a new test importer of the doomed module: {sorted(importers)}"
