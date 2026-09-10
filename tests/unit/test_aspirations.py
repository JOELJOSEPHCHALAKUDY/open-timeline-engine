"""The refusal devices, tested against the text that is actually in the live database.

The previous dreaming implementation was rejected because it produced generic,
consultant-sounding text that traced to nothing the owner had ever said.  The devices in
``tce_shared.aspirations`` exist to make that impossible **by construction**, and a device
that only rejects strings a test author invented proves nothing about that.  So the fixtures
here are real: the nine titles are the distinct ``autonomy_goals`` titles sitting in the live
Postgres today (``SELECT title FROM autonomy_goals WHERE step_index = -1``), and the frozen
six-message pool is real message bodies from the live ``events`` table.

The property under test is **not** "these nine strings are refused".  Three of them trace to
words the owner really typed, and refusing those would be broken in the other direction.  The
property is: *a claim whose content tokens do not appear in any quotable message cannot be
emitted*, with the model allowed to pick the most favourable admissible quote it can find.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tce_shared.aspirations import (
    ADMISSIBLE_ORIGIN_KINDS,
    ATTRIBUTION_IMPORTED,
    ATTRIBUTION_SAID,
    BACKFILL_ORIGIN_KIND,
    CANDIDATE_REFUSAL_REASONS,
    DREAM_SETTING_DEFAULTS,
    DREAM_SETTING_NAMES,
    EVIDENCE_BASIS_BACKFILL,
    EVIDENCE_BASIS_MIXED,
    EVIDENCE_BASIS_TRUSTED,
    QUOTABLE_ORIGIN_KIND,
    CandidateRefusal,
    DreamCandidate,
    DreamCitation,
    DreamProposalProjection,
    DreamProposalStatus,
    NonresponseState,
    PoolMessage,
    content_token_overlap,
    content_tokens,
    dedupe_by_content_sha256,
    dream_proposal_summary_fields,
    evidence_basis_for,
    is_duplicate_theme,
    is_harness_text,
    material_change,
    nonresponse_state,
    normalise_for_containment,
    parse_dream_payload,
    quote_is_contained,
    resurface_interval_hours,
    suppressed_by_rejection,
    theme_tokens_for,
    validate_candidate,
    voice_violations,
)
from tce_shared.events import TrustedInputOriginKind

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 1, 1, tzinfo=UTC)

MIN_CITATIONS = 2
MIN_QUOTE_OVERLAP = 2
QUOTE_MAX_CHARS = 200
RELEVANCE_FLOOR = 1


# --------------------------------------------------------------------------- the file list


def _git_files(patterns: tuple[str, ...]) -> list[Path]:
    """``git ls-files --cached --others --exclude-standard``, with ``build/`` excluded.

    ``--others`` matters: the modules this gate is about are untracked while the phase is
    being built, and a scanner that could not see them would go green at exactly the moment
    it is supposed to bite.  ``build/`` matters because four stale untracked trees live there
    on a developer machine and would make every tree-walking gate red locally.
    """
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    seen: dict[str, Path] = {}
    for line in completed.stdout.splitlines():
        rel = line.strip()
        # A file git still has in the index but that has been deleted in the working tree
        # cannot be scanned; skipping it is what lets a deletion land before its commit.
        if not rel or "/build/" in rel or not (REPO_ROOT / rel).is_file():
            continue
        seen.setdefault(rel, REPO_ROOT / rel)
    return sorted(seen.values())


# Resolved at import time: tests/conftest.py blocks subprocess inside an unmarked test.
_SOURCE_FILES: list[Path] = _git_files(("shared/*.py", "services/*.py"))
_MIGRATION_FILES: list[Path] = _git_files(("infra/alembic/versions/*.py",))
_SCANNED_FILES: list[Path] = _SOURCE_FILES + _MIGRATION_FILES

_DREAM_STORE_FILES: list[Path] = [
    REPO_ROOT / "services/tce_api/tce_api/dream_store.py",
    REPO_ROOT / "services/tce_lite_api/tce_lite_api/dream_store.py",
]


def _string_literals(path: Path) -> Iterator[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - a file we cannot read
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


# --------------------------------------------------------------------------- live fixtures

# The nine distinct titles in `autonomy_goals` with `step_index = -1` on the live stack.
# Five are count-derived (the old producer read a row count and wrote a sentence about it),
# one is the Title-Case consultant line whose rejection is recorded in git as db45a79, and
# three are things the owner typed himself.
LIVE_COUNT_DERIVED_TITLES: tuple[str, ...] = (
    "Make the whole history searchable",
    "Move hosted-cv-angular decisively forward",
    "Move ai-experiments-apps decisively forward",
    "Move open-engineering decisively forward",
    "Stop accumulating abandoned intentions",
)
LIVE_CONSULTANT_TITLE = "Deep Research and Improvement of Technologies for Real-World Applications"
LIVE_OWNER_TITLES: tuple[str, ...] = (
    "do deep research on open timeline engine and witness engine",
    "build the open timeline engine and witness engine",
    "do deep research on similar technologies and features for the witness and timeline engines",
)

# Six real message bodies from the live `events` table, verbatim including the typos.  The
# typos are the point: they are the strongest single signal that a quote is really his, which
# is why `normalise_for_containment` collapses whitespace and case and repairs nothing.
LIVE_POOL_BODIES: tuple[str, ...] = (
    "hey in setup script can you give restart timeline engine optyions which rebuilds docker build",
    "i want you to do a deep research and find all such similar technologies",
    "hey just asking shoun't we provide a mac and wndows easy install file ie exe and dmg ? for timeline engine ?",
    "is thefre any cleanup left in our project repo i mean open timeline engine",
    "now do a deep research on this project and explore the ways to improve it",
    "hey one more though currently i know timeline engine is accessed by both executer and advisor and the context is passed",
)

# Real pasted prose and a real harness compaction line, both from the live corpus.
LIVE_MARKETING_PASTE = (
    "Here's my comprehensive review: --- ## V6 Plan Review The plan is well structured and "
    "covers the major gaps in the current implementation."
)
LIVE_HARNESS_COMPACTION = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The conversation is summarized below."
)
LIVE_HARNESS_INTERRUPT = "[Request interrupted by user for tool use]"


def _pool(bodies: Sequence[str] = LIVE_POOL_BODIES, *, origin_kind: str = QUOTABLE_ORIGIN_KIND) -> tuple[PoolMessage, ...]:
    return tuple(
        PoolMessage(
            n=index + 1,
            event_id=f"event-{index + 1}",
            receipt_id=f"receipt-{index + 1}",
            content_sha256=f"sha-{index + 1}",
            origin_kind=origin_kind,
            observed_at=BASE + timedelta(minutes=index),
            body=body,
        )
        for index, body in enumerate(bodies)
    )


def _citation(message: PoolMessage, quote: str) -> DreamCitation:
    return DreamCitation(
        event_id=message.event_id,
        receipt_id=message.receipt_id,
        content_sha256=message.content_sha256,
        origin_kind=message.origin_kind,
        observed_at=message.observed_at,
        quote=quote,
        quote_sha256=hashlib.sha256(normalise_for_containment(quote).encode("utf-8")).hexdigest(),
    )


def _candidate(title: str, citations: Sequence[DreamCitation], *, first_step: str = "start with the first item") -> DreamCandidate:
    return DreamCandidate(
        title=title,
        connection_text="this is what they keep coming back to",
        benefit_text="one less thing hanging over them",
        first_step=first_step,
        citations=tuple(citations),
        theme_tokens=theme_tokens_for(title, first_step),
        evidence_basis=evidence_basis_for(citations),
    )


def _validate(candidate: DreamCandidate, pool: Sequence[PoolMessage]) -> tuple[DreamCandidate | None, CandidateRefusal | None]:
    return validate_candidate(
        candidate,
        pool=pool,
        min_citations=MIN_CITATIONS,
        min_quote_overlap_tokens=MIN_QUOTE_OVERLAP,
        quote_max_chars=QUOTE_MAX_CHARS,
        min_citation_relevance_tokens=RELEVANCE_FLOOR,
    )


def _admissible_spans(body: str) -> Iterator[str]:
    """Every contiguous word span of a message that could legally be quoted."""
    words = body.split()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            span = " ".join(words[start:end])
            if len(span) <= QUOTE_MAX_CHARS:
                yield span


def _best_play(title: str, pool: Sequence[PoolMessage]) -> tuple[DreamCandidate | None, CandidateRefusal | None]:
    """What the most adversarial model could do with this title and this pool.

    For every message it takes the admissible span that shares the most content tokens with
    the title — that is the model's optimal move, and asserting against one hand-picked
    quote per title is why an earlier version of this gate proved refus*ability* rather than
    refusal.  ``first_step`` is a benign lower-case string, which is also the model's best
    play: M2 reads the title alone, so a longer ``first_step`` buys the model nothing and a
    florid one only trips the voice check.
    """
    picks: list[tuple[int, PoolMessage, str]] = []
    for message in pool:
        best_span: str | None = None
        best_score = -1
        for span in _admissible_spans(message.body):
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


# --------------------------------------------------------------------------- Y2 vocabulary


def test_origin_kind_literals_match_p1() -> None:
    """G19 half one.  One vocabulary, P1's, and a drift breaks the build.

    ``aspirations.py`` holds the two values as strings only because it must stay
    pydantic-free for the worker while ``events.py`` imports pydantic.  That is a layering
    workaround, not a second vocabulary, and this is what keeps the distinction honest.
    """
    assert QUOTABLE_ORIGIN_KIND == TrustedInputOriginKind.HUMAN_INPUT.value
    assert BACKFILL_ORIGIN_KIND == TrustedInputOriginKind.IMPORTED_TRANSCRIPT.value
    assert ADMISSIBLE_ORIGIN_KINDS == (QUOTABLE_ORIGIN_KIND, BACKFILL_ORIGIN_KIND)
    # Every other member of P1's enum is executor-writable or machine-written and must NOT be
    # admissible: quoting an executor's own output back to the owner as his words is the
    # forgery class the receipt rule exists to close.
    for member in TrustedInputOriginKind:
        if member.value not in ADMISSIBLE_ORIGIN_KINDS:
            assert member.value in {"manager_instruction", "executor_output", "tool_result"}


def test_backfill_basis_renders_as_imported() -> None:
    """G19 half two / T5.  Imported history is never rendered as something said today."""
    pool = _pool(origin_kind=BACKFILL_ORIGIN_KIND)
    citations = [_citation(pool[0], pool[0].body), _citation(pool[3], pool[3].body)]
    assert evidence_basis_for(citations) == EVIDENCE_BASIS_BACKFILL

    projection = _projection(evidence_basis=EVIDENCE_BASIS_BACKFILL)
    fields = dream_proposal_summary_fields(projection, source_revision="abc", citations_verified="cheap")
    assert fields["attribution"] == ATTRIBUTION_IMPORTED
    assert fields["attribution"] != ATTRIBUTION_SAID
    assert ATTRIBUTION_SAID not in str(fields)

    trusted = dream_proposal_summary_fields(
        _projection(evidence_basis=EVIDENCE_BASIS_TRUSTED), source_revision="abc", citations_verified="deep"
    )
    assert trusted["attribution"] == ATTRIBUTION_SAID


def test_evidence_basis_is_mixed_when_the_sources_are() -> None:
    trusted = _pool()[0]
    imported = _pool(origin_kind=BACKFILL_ORIGIN_KIND)[1]
    assert evidence_basis_for([_citation(trusted, trusted.body)]) == EVIDENCE_BASIS_TRUSTED
    assert evidence_basis_for([_citation(imported, imported.body)]) == EVIDENCE_BASIS_BACKFILL
    assert (
        evidence_basis_for([_citation(trusted, trusted.body), _citation(imported, imported.body)])
        == EVIDENCE_BASIS_MIXED
    )


# --------------------------------------------------------------------------- M1 containment


def test_quote_containment() -> None:
    body = LIVE_POOL_BODIES[1]  # "i want you to do a deep research and find all such similar technologies"

    assert quote_is_contained("do a deep research and find all such similar technologies", body, max_chars=QUOTE_MAX_CHARS)
    assert quote_is_contained("do  a   deep\nresearch and find all such similar", body, max_chars=QUOTE_MAX_CHARS)
    assert quote_is_contained("DO A DEEP RESEARCH AND FIND ALL SUCH SIMILAR", body, max_chars=QUOTE_MAX_CHARS)

    # A plausible sentence that is not in the message.  This is the whole of M1: a number can
    # be guessed, a verbatim span cannot.
    assert not quote_is_contained("i want a thorough investigation of comparable products", body, max_chars=QUOTE_MAX_CHARS)
    # Too short, too long, and too thin to mean anything.
    assert not quote_is_contained("dee", body, max_chars=QUOTE_MAX_CHARS)
    assert not quote_is_contained("x" * 201, body, max_chars=QUOTE_MAX_CHARS)
    assert not quote_is_contained("deep research", body, max_chars=QUOTE_MAX_CHARS)  # two content tokens


def test_a_quote_the_owner_never_typed_cannot_be_smuggled_by_tidying_it() -> None:
    """Repairing spelling would let a model quote a sentence he never typed."""
    body = LIVE_POOL_BODIES[0]  # contains "optyions"
    assert quote_is_contained("restart timeline engine optyions which rebuilds", body, max_chars=QUOTE_MAX_CHARS)
    assert not quote_is_contained("restart timeline engine options which rebuilds", body, max_chars=QUOTE_MAX_CHARS)


# --------------------------------------------------------------------------- M3 / M6


def test_quote_voice_and_harness_devices() -> None:
    """The two reproductions: pasted marketing prose, and a harness compaction summary.

    Both were emitted by an earlier draft.  The first because ``voice_violations`` ran on the
    claim and never on the quote — the model had not *written* the consultant voice, it had
    merely chosen to show it, which the reader cannot distinguish on screen.  The second
    because nothing stopped harness text entering the pool at all.
    """
    assert "comprehensive" in voice_violations(LIVE_MARKETING_PASTE)
    assert voice_violations(LIVE_CONSULTANT_TITLE) == ("real-world", "title_case")
    assert voice_violations("get the witness engine in front of someone") == ()
    assert voice_violations("Deep Research on Timeline Engines") == ("title_case",)

    assert is_harness_text(LIVE_HARNESS_COMPACTION)
    assert is_harness_text(LIVE_HARNESS_INTERRUPT)
    assert is_harness_text("  [REQUEST INTERRUPTED BY USER for tool use]  ")
    assert not is_harness_text(LIVE_POOL_BODIES[0])

    # And the two devices compose: a candidate whose only two citations are the marketing
    # paste and the compaction summary survives neither.
    pool = _pool((LIVE_MARKETING_PASTE, LIVE_HARNESS_COMPACTION))
    candidate = _candidate(
        "review the v6 plan and the previous conversation",
        [_citation(pool[0], LIVE_MARKETING_PASTE[:120]), _citation(pool[1], LIVE_HARNESS_COMPACTION[:120])],
    )
    survivor, refusal = _validate(candidate, pool)
    assert survivor is None
    assert refusal is not None
    assert refusal.reason == "insufficient_citations"


def test_harness_text_is_honestly_a_blocklist() -> None:
    """M6 catches the markers that exist; it cannot catch a paste the harness did not author.

    Stated as a test so the limitation is in the suite rather than only in a docstring.
    """
    assert not is_harness_text("some third-party prose the owner pasted into his own prompt")


# --------------------------------------------------------------------------- M2b


def test_per_citation_relevance_drops_then_recounts() -> None:
    """A citation that shares nothing with the claim is dropped, and the count is re-checked.

    This is the reproduction of two separate findings: a proposal citing evidence from an
    unrelated project, and a gamed ``first_step`` engineered to manufacture overlap.  The
    count must be re-checked **after** the drop — an earlier draft validated a candidate with
    two citations and persisted it with zero because nothing counted again.
    """
    pool = _pool()
    relevant = _citation(pool[1], "do a deep research and find all such similar technologies")
    irrelevant = _citation(pool[2], "we provide a mac and wndows easy install file")

    survivor, refusal = _validate(_candidate("deep research on similar technologies", [relevant, irrelevant]), pool)
    assert survivor is None
    assert refusal is not None
    assert refusal.reason == "insufficient_citations"
    assert "citation_not_relevant=1" in refusal.detail

    # With a second genuinely relevant citation it survives, carrying exactly two.
    second = _citation(pool[4], "do a deep research on this project and explore the ways")
    survivor, refusal = _validate(_candidate("deep research on similar technologies", [relevant, second, irrelevant]), pool)
    assert refusal is None
    assert survivor is not None
    assert len(survivor.citations) == 2
    assert all(content_token_overlap(survivor.title, citation.quote) >= RELEVANCE_FLOOR for citation in survivor.citations)


def test_first_step_cannot_manufacture_the_overlap() -> None:
    """M2 reads the TITLE alone.

    The model writes both the ``first_step`` and the choice of quote, so putting ``first_step``
    on the left side of the overlap check lets it satisfy the check against itself.  The quote
    is the only string on either side the model did not author.
    """
    pool = _pool()
    gamed_first_step = "run the failing tests in ai-experiments-apps and fix the docker build"
    citations = [
        _citation(pool[0], "restart timeline engine optyions which rebuilds docker build"),
        _citation(pool[3], "any cleanup left in our project repo i mean open timeline engine"),
    ]
    survivor, refusal = _validate(
        _candidate("Move ai-experiments-apps decisively forward", citations, first_step=gamed_first_step), pool
    )
    assert survivor is None
    assert refusal is not None
    assert refusal.reason in {"insufficient_citations", "no_quote_overlap"}
    # And the gaming really would have worked, had first_step been on the left side.
    assert content_token_overlap("Move ai-experiments-apps decisively forward", " ".join(c.quote for c in citations)) < MIN_QUOTE_OVERLAP
    assert content_token_overlap(
        "Move ai-experiments-apps decisively forward " + gamed_first_step,
        " ".join(c.quote for c in citations),
    ) >= MIN_QUOTE_OVERLAP


# --------------------------------------------------------------------------- G10, the property


def test_no_claim_survives_without_owner_words() -> None:
    """G10.  The nine live titles × the frozen live pool, with the model picking the quote.

    Five count-derived titles refuse because no admissible span of any real message shares
    enough with them.  The Title-Case consultant line refuses on voice, and it is caught
    twice — by a banned word and by the capitalisation rule — which is what "independent
    rules" means here.  And three titles are **emitted**, because the owner really did type
    those words; a design that refused them would be broken in the other direction, and
    asserting the emissions is what stops this gate from being satisfied by a device that
    refuses everything.
    """
    outcomes: dict[str, str] = {}
    for title in (*LIVE_COUNT_DERIVED_TITLES, LIVE_CONSULTANT_TITLE, *LIVE_OWNER_TITLES):
        survivor, refusal = _best_play(title, _pool())
        outcomes[title] = "EMITTED" if survivor is not None else (refusal.reason if refusal else "?")

    for title in LIVE_COUNT_DERIVED_TITLES:
        assert outcomes[title] != "EMITTED", f"count-derived title was emitted: {title}"
        assert outcomes[title] in CANDIDATE_REFUSAL_REASONS
    assert outcomes[LIVE_CONSULTANT_TITLE] == "voice_violation"
    for title in LIVE_OWNER_TITLES:
        assert outcomes[title] == "EMITTED", f"a title the owner actually typed was refused: {title}"


def test_the_five_count_derived_titles_share_no_words_with_the_corpus() -> None:
    """Why the refusals above happen, stated as the underlying fact rather than an outcome.

    ``Move open-engineering decisively forward`` is the interesting one: it shares exactly one
    content token (``open``) with the pool, which clears the per-citation relevance floor of
    one and still cannot reach two citations.  The refusal is real, not an artefact of a
    fixture that shares nothing at all.
    """
    corpus = " ".join(LIVE_POOL_BODIES)
    overlaps = {title: content_token_overlap(title, corpus) for title in LIVE_COUNT_DERIVED_TITLES}
    assert overlaps["Make the whole history searchable"] == 0
    assert overlaps["Stop accumulating abandoned intentions"] == 0
    assert overlaps["Move open-engineering decisively forward"] == 1
    for title in LIVE_OWNER_TITLES:
        assert content_token_overlap(title, corpus) >= MIN_QUOTE_OVERLAP


def test_a_fabricated_citation_number_resolves_to_nothing() -> None:
    pool = _pool()
    payload = {
        "dreams": [
            {
                "title": "deep research on similar technologies",
                "connection": "they keep asking for it",
                "benefit": "they stop re-deciding it",
                "first_step": "list the three closest ones",
                "citations": [{"n": 99, "quote": "something that was never said"}],
            }
        ]
    }
    candidates, refusals = parse_dream_payload(
        payload, pool=pool, max_proposals=3, max_citations=6, quote_max_chars=QUOTE_MAX_CHARS
    )
    assert candidates == []
    assert {refusal.reason for refusal in refusals} == {"citation_out_of_range", "no_citations"}


def test_unparseable_payload_is_a_named_refusal_not_an_exception() -> None:
    payloads: tuple[object, ...] = (None, "", [], {"nope": 1})
    for payload in payloads:
        candidates, refusals = parse_dream_payload(
            payload, pool=_pool(), max_proposals=3, max_citations=6, quote_max_chars=QUOTE_MAX_CHARS
        )
        assert candidates == []
        assert [refusal.reason for refusal in refusals] == ["unparseable_payload"]


def test_citations_are_deduplicated_by_content_hash_not_event_id() -> None:
    """D11.  The same message ingested twice is one citation.

    Two ingests are two event rows and one receipt hash, so de-duplicating on the event id
    would let a proposal look twice as well-evidenced as it is — against a floor of two, that
    is the difference between shown and refused.
    """
    first = PoolMessage(
        n=1, event_id="event-a", receipt_id="receipt-a", content_sha256="same",
        origin_kind=QUOTABLE_ORIGIN_KIND, observed_at=BASE, body=LIVE_POOL_BODIES[1],
    )
    second = PoolMessage(
        n=2, event_id="event-b", receipt_id="receipt-b", content_sha256="same",
        origin_kind=QUOTABLE_ORIGIN_KIND, observed_at=BASE, body=LIVE_POOL_BODIES[1],
    )
    quote = "do a deep research and find all such similar technologies"
    deduped = dedupe_by_content_sha256([_citation(first, quote), _citation(second, quote)])
    assert len(deduped) == 1

    survivor, refusal = _validate(
        _candidate("deep research on similar technologies", [_citation(first, quote), _citation(second, quote)]),
        (first, second),
    )
    assert survivor is None
    assert refusal is not None and refusal.reason == "insufficient_citations"


# --------------------------------------------------------------------------- suppression


def _projection(
    *,
    proposal_id: str = "prop-1",
    status: DreamProposalStatus = DreamProposalStatus.REJECTED,
    title: str = "deep research on similar technologies",
    first_step: str = "list the three closest ones",
    citations: Sequence[DreamCitation] = (),
    rejected_at: datetime | None = None,
    repropose_depth: int = 0,
    evidence_basis: str = EVIDENCE_BASIS_TRUSTED,
    project_id: str | None = "proj_a",
    scope_kind: str = "project",
) -> DreamProposalProjection:
    return DreamProposalProjection(
        proposal_id=proposal_id,
        workspace_id="ws",
        owner_id="owner",
        subject_user_id="subject",
        project_id=project_id,
        scope_kind=scope_kind,
        session_id="sess",
        revision=1,
        status=status,
        nonresponse=NonresponseState.NEVER_SURFACED,
        surfaced_count=0,
        surfaced_attested=False,
        first_surfaced_at=None,
        last_surfaced_at=None,
        title=title,
        connection_text="",
        benefit_text="",
        first_step=first_step,
        citations=tuple(citations),
        theme_tokens=theme_tokens_for(title, first_step),
        evidence_basis=evidence_basis,
        evidence_revision="rev",
        evidence_cutoff_at=None,
        supersedes_proposal_id=None,
        repropose_depth=repropose_depth,
        snooze_until=None,
        expires_at=None,
        accepted_at=None,
        rejected_at=rejected_at,
        rejection_reason="not now",
        task_id=None,
        objective_hash=None,
        plan_root_goal_id=None,
        pursuit_started_at=None,
        completed_at=None,
        abandoned_at=None,
        abandon_reason="",
        withdrawn_reason="",
    )


def _fresh_citation(sha: str, *, observed_at: datetime, quote: str = "do a deep research on this project and explore the ways") -> DreamCitation:
    return DreamCitation(
        event_id=f"event-{sha}",
        receipt_id=f"receipt-{sha}",
        content_sha256=sha,
        origin_kind=QUOTABLE_ORIGIN_KIND,
        observed_at=observed_at,
        quote=quote,
        quote_sha256=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
    )


REJECTED_AT = BASE + timedelta(days=10)


def _rejected_with(shas: Sequence[str], *, depth: int = 0, title: str = "deep research on similar technologies") -> DreamProposalProjection:
    return _projection(
        title=title,
        citations=[_fresh_citation(sha, observed_at=BASE) for sha in shas],
        rejected_at=REJECTED_AT,
        repropose_depth=depth,
    )


def _material(candidate: DreamCandidate, rejected: DreamProposalProjection, *, now: datetime) -> tuple[bool, str]:
    return material_change(
        candidate,
        rejected,
        now=now,
        min_new_citations=2,
        max_similarity=0.60,
        cooldown_days=30,
        max_reproposals=2,
    )


def test_material_change_conditions() -> None:
    """All five, independently, and rule 2 by name.

    Rule 2 is the load-bearing one: evidence that already existed when the owner said no is
    not new evidence — he rejected the theme *while* it existed.  Without it, a model that
    simply cites two older messages the first proposal happened to miss reopens every
    rejection, forever.
    """
    rejected = _rejected_with(["old-1", "old-2"])
    after = REJECTED_AT + timedelta(days=1)
    long_after = REJECTED_AT + timedelta(days=40)

    # 1 — not enough new citations.
    one_new = _candidate(
        "something quite different about install files",
        [_fresh_citation("old-1", observed_at=BASE), _fresh_citation("new-1", observed_at=after)],
    )
    assert _material(one_new, rejected, now=long_after) == (False, "new_citations")

    # 2 — two new citations, but both predate the rejection.  THE rule.
    stale_evidence = _candidate(
        "something quite different about install files",
        [_fresh_citation("new-1", observed_at=BASE), _fresh_citation("new-2", observed_at=BASE)],
    )
    assert _material(stale_evidence, rejected, now=long_after) == (False, "evidence_predates_rejection")

    # 3 — new evidence, said afterwards, but it is the same proposal in different words.
    same_theme = _candidate(
        "deep research on similar technologies",
        [_fresh_citation("new-1", observed_at=after), _fresh_citation("new-2", observed_at=after)],
        first_step="list the three closest ones",
    )
    assert _material(same_theme, rejected, now=long_after) == (False, "theme_too_similar")

    # 4 — genuinely different and genuinely new, but the ink is not dry.
    different = _candidate(
        "ship a mac installer people can double click",
        [
            _fresh_citation("new-1", observed_at=after, quote="we provide a mac and wndows easy install file"),
            _fresh_citation("new-2", observed_at=after, quote="mac and wndows easy install file ie exe and dmg"),
        ],
        first_step="build the dmg",
    )
    assert _material(different, rejected, now=REJECTED_AT + timedelta(days=5)) == (False, "cooldown_not_elapsed")

    # 5 — everything else holds, but three refusals of one theme end it.
    assert _material(different, _rejected_with(["old-1", "old-2"], depth=2), now=long_after) == (
        False,
        "repropose_limit_reached",
    )

    # And with all five satisfied, suppression lifts.
    assert _material(different, rejected, now=long_after) == (True, "")


def test_suppression_runs_before_the_duplicate_merge_path() -> None:
    """A candidate carrying a rejected theme is refused, never merged into a live neighbour.

    Ordering was left unstated in an earlier draft, which let a rejected theme come back
    *strengthened* by merging into a similar live proposal, with no refusal recorded anywhere.
    """
    rejected = _rejected_with(["old-1", "old-2"])
    live = _projection(
        proposal_id="prop-live",
        status=DreamProposalStatus.SURFACED,
        title="deep research on similar technologies and tools",
        rejected_at=None,
    )
    candidate = _candidate(
        "deep research on similar technologies",
        [_fresh_citation("old-1", observed_at=BASE), _fresh_citation("old-2", observed_at=BASE)],
        first_step="list the three closest ones",
    )

    blocking, failing = suppressed_by_rejection(
        candidate,
        [rejected],
        now=REJECTED_AT + timedelta(days=90),
        threshold=0.60,
        min_new_citations=2,
        max_similarity=0.60,
        cooldown_days=30,
        max_reproposals=2,
    )
    assert blocking is not None
    assert blocking.proposal_id == "prop-1"
    assert failing == "new_citations"

    # The merge path would have accepted it — which is exactly why the order matters.
    assert is_duplicate_theme(candidate, [live], threshold=0.60) is not None


def test_a_rejection_in_workspace_scope_suppresses_a_project_scoped_candidate() -> None:
    """The bucket is the subject, not the scope kind.

    Bucketing per scope kind made a rejection invisible to the only mode that could currently
    produce anything.  ``suppressed_by_rejection`` takes the rows the caller loaded and does
    not filter by scope kind; this pins that it does not start.
    """
    workspace_rejection = _projection(
        title="deep research on similar technologies",
        citations=[_fresh_citation(sha, observed_at=BASE) for sha in ("old-1", "old-2")],
        rejected_at=REJECTED_AT,
        project_id=None,
        scope_kind="workspace",
    )
    assert workspace_rejection.scope_kind == "workspace"
    candidate = _candidate(
        "deep research on similar technologies",
        [_fresh_citation("old-1", observed_at=BASE), _fresh_citation("old-2", observed_at=BASE)],
        first_step="list the three closest ones",
    )
    # The candidate is project-scoped; the rejection is not.  The scan must still see it.
    blocking, _ = suppressed_by_rejection(
        candidate,
        [workspace_rejection],
        now=REJECTED_AT + timedelta(days=90),
        threshold=0.60,
        min_new_citations=2,
        max_similarity=0.60,
        cooldown_days=30,
        max_reproposals=2,
    )
    assert blocking is not None


def test_only_live_proposals_can_be_duplicates() -> None:
    rejected = _projection(status=DreamProposalStatus.REJECTED, rejected_at=REJECTED_AT)
    candidate = _candidate("deep research on similar technologies", [], first_step="list the three closest ones")
    assert is_duplicate_theme(candidate, [rejected], threshold=0.60) is None


# --------------------------------------------------------------------------- nonresponse


def test_nonresponse_is_not_rejection() -> None:
    """X2 / D3.  Time alone moves nothing, and ``ignored`` has exactly one effect."""
    far_future = BASE + timedelta(days=365)
    assert (
        nonresponse_state(surfaced_count=0, last_surfaced_at=None, now=far_future, after_surfaces=3)
        is NonresponseState.NEVER_SURFACED
    )
    assert (
        nonresponse_state(surfaced_count=2, last_surfaced_at=BASE, now=far_future, after_surfaces=3)
        is NonresponseState.AWAITING_RESPONSE
    )
    assert (
        nonresponse_state(surfaced_count=3, last_surfaced_at=BASE, now=BASE, after_surfaces=3)
        is NonresponseState.IGNORED
    )

    # No status means "ignored", and nothing in the suppression path reads the axis.
    assert not any("ignor" in str(status) for status in DreamProposalStatus)
    material_source = (REPO_ROOT / "shared/tce_shared/aspirations.py").read_text(encoding="utf-8")
    body = material_source.split("def material_change(", 1)[1].split("\ndef ", 1)[0]
    assert "nonresponse" not in body
    assert "NonresponseState" not in body


def test_resurface_intervals_are_ordered() -> None:
    """``IGNORED``'s only functional reader, and the ordering that makes it one."""
    min_hours = int(DREAM_SETTING_DEFAULTS["dream_resurface_min_hours"])
    ignored_hours = int(DREAM_SETTING_DEFAULTS["dream_resurface_ignored_hours"])
    assert ignored_hours > min_hours

    awaiting = resurface_interval_hours(NonresponseState.AWAITING_RESPONSE, min_hours=min_hours, ignored_hours=ignored_hours)
    ignored = resurface_interval_hours(NonresponseState.IGNORED, min_hours=min_hours, ignored_hours=ignored_hours)
    never = resurface_interval_hours(NonresponseState.NEVER_SURFACED, min_hours=min_hours, ignored_hours=ignored_hours)
    assert never == 0
    assert ignored > awaiting > never


def test_rejected_lookback_covers_cooldown() -> None:
    """A rejection must not age out of the scan window before its cooldown expires.

    If it does, suppression lifts by amnesia rather than by ``material_change``, and rule 5's
    hard stop never engages because ``repropose_depth`` only increments when suppression
    lifts deliberately.
    """
    assert (
        int(DREAM_SETTING_DEFAULTS["dream_rejected_lookback_days"])
        >= int(DREAM_SETTING_DEFAULTS["dream_rejected_cooldown_days"])
    )


def test_setting_names_and_defaults_agree() -> None:
    """Three ``config.py`` files declare these; two silently different defaults is a parity bug."""
    assert set(DREAM_SETTING_NAMES) == set(DREAM_SETTING_DEFAULTS)
    assert len(DREAM_SETTING_NAMES) == len(set(DREAM_SETTING_NAMES))


def test_every_setting_has_a_reader() -> None:
    """A setting nobody reads is a knob that does nothing, which is worse than no knob.

    Self-arming: until ``dream_store.py`` exists there are no readers to find, so the check
    holds its fire rather than asserting a thing that cannot be true yet.  It goes live the
    moment the store lands, with no edit to this file.
    """
    if not any(path.exists() for path in _DREAM_STORE_FILES):
        return
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _SOURCE_FILES
        if path.name not in {"config.py", "aspirations.py"}
    )
    missing = [
        name
        for name in DREAM_SETTING_NAMES
        if not re.search(rf"(getattr\(\s*settings\s*,\s*[\"']{name}[\"']|settings\.{name}\b)", haystack)
    ]
    assert missing == [], f"settings with no reader outside a config module: {missing}"


# --------------------------------------------------------------------------- tree gates


def test_no_delete_no_blind_update_no_drop() -> None:
    """G3.  Refresh can never delete, and no downgrade can drop a table of human answers.

    Three separate statements, one scan.  An earlier draft's own ``downgrade()`` was the one
    statement in the tree that could erase every verdict the owner ever recorded, and its
    scanner looked only for ``DELETE`` — so the check was blind to exactly the statement that
    mattered.  ``infra/alembic/versions/`` is therefore in the scan.
    """
    delete_pattern = re.compile(r"DELETE\s+FROM\s+dream_proposal", re.IGNORECASE)
    drop_pattern = re.compile(
        r"DROP\s+TABLE(\s+IF\s+EXISTS)?\s+(dream_proposals|dream_proposal_events|dream_generation_runs|policy_qualifications)\b",
        re.IGNORECASE,
    )
    update_pattern = re.compile(r"UPDATE\s+dream_proposals\b", re.IGNORECASE)
    cas_pattern = re.compile(r"revision\s*=\s*(:expected_revision|\?)", re.IGNORECASE)

    offenders: list[str] = []
    updates_seen = 0
    for path in _SCANNED_FILES:
        rel = path.relative_to(REPO_ROOT)
        for literal in _string_literals(path):
            if delete_pattern.search(literal):
                offenders.append(f"{rel}: DELETE FROM dream_proposal*")
            if drop_pattern.search(literal):
                offenders.append(f"{rel}: DROP TABLE of a human-answer table")
            if update_pattern.search(literal):
                updates_seen += 1
                if not cas_pattern.search(literal):
                    offenders.append(f"{rel}: UPDATE dream_proposals with no revision CAS")
    assert offenders == [], "; ".join(offenders)
    # Recorded rather than asserted: until Builders B and C land there is no UPDATE to check,
    # and a gate that claimed otherwise would be lying about what it verified.
    assert updates_seen >= 0


def test_the_sql_scanner_would_actually_catch_each_violation() -> None:
    """The three regexes above, exercised against synthetic offenders.

    A scan that reports "no violations" over a tree where the statements do not exist yet is
    only worth something if the scan can see them when they arrive.
    """
    delete_pattern = re.compile(r"DELETE\s+FROM\s+dream_proposal", re.IGNORECASE)
    drop_pattern = re.compile(r"DROP\s+TABLE(\s+IF\s+EXISTS)?\s+dream_proposals\b", re.IGNORECASE)
    update_pattern = re.compile(r"UPDATE\s+dream_proposals\b", re.IGNORECASE)
    cas_pattern = re.compile(r"revision\s*=\s*(:expected_revision|\?)", re.IGNORECASE)

    assert delete_pattern.search("DELETE FROM dream_proposal_events WHERE proposal_id = :id")
    assert drop_pattern.search("op.execute('DROP TABLE IF EXISTS dream_proposals')")
    blind = "UPDATE dream_proposals SET status = :status WHERE id = :id"
    assert update_pattern.search(blind) and not cas_pattern.search(blind)
    guarded = "UPDATE dream_proposals SET status = :status WHERE id = :id AND revision = :expected_revision"
    assert update_pattern.search(guarded) and cas_pattern.search(guarded)
    assert _SCANNED_FILES, "the scan is scoped wrong — it found no files at all"


def _function_bodies(path: Path) -> Iterator[tuple[str, ast.AST]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _called_names(node: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                out.append((child.lineno, func.id))
            elif isinstance(func, ast.Attribute):
                out.append((child.lineno, func.attr))
    return sorted(out)


def test_dream_lock_order() -> None:
    """G8.  ``task_states`` before ``dream_proposals``; the run slot before the proposal.

    Self-arming over the two store modules.  Lock-order gates are worth writing before the
    code they guard, because the first violation is usually written by someone who never read
    the order.
    """
    for path in _DREAM_STORE_FILES:
        if not path.exists():
            continue
        for name, node in _function_bodies(path):
            calls = _called_names(node)
            for earlier, later in (("apply_dream_events", "apply_task_state_events"), ("mint_proposal", "start_generation_run")):
                first = next((line for line, called in calls if called == earlier), None)
                second = next((line for line, called in calls if called == later), None)
                if first is not None and second is not None:
                    assert second < first, (
                        f"{path.name}::{name} calls {later} after {earlier} — lock order inverted"
                    )


def test_objective_enums_are_always_qualified() -> None:
    """G4 companion.  ``reconcile_dream_pursuit`` holds two OBJECTIVE enums in one body.

    They were deliberately given different member names *and* different values so an AST gate
    can tell them apart; a bare ``OBJECTIVE_SET`` in that module would defeat the whole point
    of the rename.
    """
    for path in _DREAM_STORE_FILES:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in {"OBJECTIVE_SET", "OBJECTIVE_BOUND"}:
                pytest.fail(f"{path.name}:{node.lineno} uses a bare {node.id}; qualify it on its own enum")


def test_all_read_routes_share_one_scope_predicate() -> None:
    """D22.  One ``WHERE`` for the list route, the detail route and the transition route.

    An earlier draft's detail route keyed on ``(workspace_id, owner_id, proposal_id)`` with no
    subject and no project filter — and on this stack one ``owner_id`` really does span six
    subjects, so a verified human bound to subject A could read *and accept* subject B's
    proposal.
    """
    for path in _DREAM_STORE_FILES:
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        assert "_dream_scope_sql" in source, f"{path.name} has no shared scope predicate"
        hand_rolled = [
            literal
            for literal in _string_literals(path)
            if re.search(r"FROM\s+dream_proposals", literal, re.IGNORECASE)
            and re.search(r"\bWHERE\b", literal, re.IGNORECASE)
            and "subject_user_id" not in literal
        ]
        assert hand_rolled == [], f"{path.name} builds a dream_proposals WHERE without subject_user_id"


def test_aspirations_stays_pure() -> None:
    """The worker loads this module; pulling the API model tree in behind it would break it.

    Also the layering that makes the origin-kind literals necessary in the first place: if
    this module could import ``events``, there would be no reason for the two constants and no
    reason for the gate that pins them.
    """
    source = (REPO_ROOT / "shared/tce_shared/aspirations.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = {"pydantic", "sqlalchemy", "events", "scope", "decision_policy", "behavior_fidelity"}
    for module in imported:
        head = module.split(".")[0].lstrip(".")
        assert head not in forbidden, f"aspirations.py imports {module}"
    # Relative imports are recorded without the leading dot, so check them explicitly.
    relative = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level}
    assert relative == {"task_state"}, f"aspirations.py may only import task_state; got {relative}"


@pytest.mark.parametrize("reason", CANDIDATE_REFUSAL_REASONS)
def test_refusal_reasons_are_a_closed_vocabulary(reason: str) -> None:
    assert reason.islower() and " " not in reason


def test_content_tokens_drop_short_fragments_and_stopwords() -> None:
    assert content_tokens("Move hosted-cv-angular decisively forward") == ("move", "hosted", "angular", "decisively", "forward")
    assert content_tokens("the and for you") == ()
    assert content_tokens("ai ml a b") == ()


# --------------------------------------------------------------------------- M4


COUNT_DERIVED_SYMBOLS: tuple[str, ...] = (
    "DreamSignals",
    "RecurringAsk",
    "DreamSeed",
    "cluster_recurring_asks",
    "derive_dream_seeds",
    "select_dream_to_pursue",
)

# The five sites that import the count-derived producer today, pinned so the number can only
# go DOWN.  ``tce_shared.dreams`` and its test are deleted together with these five imports —
# a cut that spans four builders' files, so it lands at the end of the phase rather than at the
# start, and this pin is what stops a sixth appearing in the meantime.
KNOWN_DREAMS_IMPORT_SITES: frozenset[str] = frozenset(
    {
        "services/tce_worker/tce_worker/jobs/dream_synthesis.py",
        "services/tce_api/tce_api/plan_rows.py",
        "services/tce_api/tce_api/main.py",
        "services/tce_lite_api/tce_lite_api/plan_rows.py",
    }
)


def test_no_count_derived_producer_reaches_the_new_path() -> None:
    """M4.  The producer cannot see a row count, because no row count is passed to it.

    ``aspirations.py``'s inputs are the message pool and the model's reply.  There is no count
    of failed directives, unembedded events, stalled goals or domain activity anywhere in it,
    and there is no import path by which one could arrive — which is what makes "no row-count
    maintenance suggestions" a property of the construction rather than of the prompt wording.
    """
    source = (REPO_ROOT / "shared/tce_shared/aspirations.py").read_text(encoding="utf-8")
    for symbol in COUNT_DERIVED_SYMBOLS:
        assert symbol not in source, f"aspirations.py still reaches for {symbol}"


def test_the_count_derived_module_gains_no_new_importer() -> None:
    """The import closure of the deletion, pinned.

    Every site here must lose its import in the same phase; leaving one behind is an
    ``ImportError`` at collection, which takes every unit gate in the repo with it.  Pinning the
    set means a new importer is a red test at the moment it is written, and the pin shrinks to
    empty when the module goes.
    """
    importers: set[str] = set()
    for path in _SOURCE_FILES:
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("tce_shared.dreams"):
                importers.add(rel)
            elif isinstance(node, ast.Import) and any(alias.name.endswith("tce_shared.dreams") for alias in node.names):
                importers.add(rel)
    new = importers - KNOWN_DREAMS_IMPORT_SITES
    assert new == set(), f"a new importer of the deleted count-derived module: {sorted(new)}"


def test_no_count_derived_producer_survives() -> None:
    """G10, in its full form: the count-derived producer is gone from the tree, not merely bypassed.

    ``test_no_count_derived_producer_reaches_the_new_path`` is the half that was true while
    ``shared/tce_shared/dreams.py`` still existed — it says the new module cannot reach the old
    one.  This is the other half, and it only became assertable when the module and its orphan
    test were cut: none of the six symbols that derived a "dream" from a row count is defined or
    referenced anywhere under ``shared/`` or ``services/`` any more.  Re-introducing one is a red
    test rather than a quiet return of template-sounding text.
    """
    offenders: list[str] = []
    for path in _SOURCE_FILES:
        source = path.read_text(encoding="utf-8")
        for symbol in COUNT_DERIVED_SYMBOLS:
            if symbol in source:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {symbol}")
    assert offenders == [], f"the count-derived dream producer is back: {offenders}"
