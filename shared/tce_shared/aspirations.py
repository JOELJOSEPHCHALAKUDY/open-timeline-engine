"""Aspirations: a proposal the owner can answer, traceable to words he actually typed.

This module replaces ``tce_shared.dreams``.  The old one derived "dreams" from row counts —
how many directives failed, how many events were unembedded — which is how the system came to
suggest *Make the whole history searchable* to a person who had never said anything of the
kind.  Nothing here can see a count.  A proposal is a short claim plus **verbatim quotes the
model had to copy out of numbered messages**, and the quotes are checked against the messages
rather than trusted, so a claim that traces to nothing cannot be emitted at all.

Four things are load-bearing and are stated once, here, because every other P5 module depends
on them being true:

* **Nonresponse is a second axis, not a status.**  :func:`nonresponse_state` reads
  ``surfaced_count`` and nothing else.  A proposal that was never put in front of the owner
  reads ``never_surfaced`` a year later, because time is not evidence of an answer and silence
  is not a "no".  ``now`` and ``last_surfaced_at`` are accepted by the signature and
  deliberately unused; see the function body.
* **The fold is total, deterministic, and lossless under truncation.**  All fourteen event
  kinds are pinned (:data:`PINNED_DREAM_KINDS`), and the one kind that is not idempotent under
  "keep the latest" — ``surfaced``, because a count is not a value — is made idempotent at the
  writer by :func:`surfaced_event`, which stamps the running ordinal into the payload.  An
  earlier draft pinned five kinds and a truncated log silently forgot that a proposal had been
  shown 800 times, and forgot which task it was bound to.
* **Refusal is the default and there is no weak-emission path.**  A candidate that fails any
  device is dropped with a named reason.  There is no confidence knob, no "surface it anyway",
  no partial credit.
* **Pure.**  No I/O, no pydantic, no SQLAlchemy.  The only import outside the standard library
  is ``task_state.canonical_json``, which is itself pydantic-free — the worker loads this
  module and must not pull the API model tree in behind it.  In particular this module must
  not import ``tce_shared.events``, ``tce_shared.scope``, ``tce_shared.decision_policy`` or
  ``tce_shared.behavior_fidelity``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .task_state import canonical_json

__all__ = [
    "ADMISSIBLE_ORIGIN_KINDS",
    "BACKFILL_ORIGIN_KIND",
    "BANNED_PROPOSAL_WORDS",
    "CANDIDATE_REFUSAL_REASONS",
    "DISPLAY_ACTIONS",
    "DREAM_POLICY_REVISION",
    "DREAM_PROMPT_ID",
    "DREAM_SCHEMA_VERSION",
    "DREAM_SETTING_DEFAULTS",
    "DREAM_SETTING_NAMES",
    "DREAM_STOPWORDS",
    "EVIDENCE_BASIS_BACKFILL",
    "EVIDENCE_BASIS_MIXED",
    "EVIDENCE_BASIS_TRUSTED",
    "HARNESS_TEXT_MARKERS",
    "HUMAN_VERDICT_ACTIONS",
    "LIVE_STATUSES",
    "MAX_DREAM_EVENTS_PER_FOLD",
    "PINNED_DREAM_KINDS",
    "PINNED_DREAM_KIND_VALUES",
    "POOL_DROP_REASONS",
    "QUOTABLE_ORIGIN_KIND",
    "RUN_REFUSAL_REASONS",
    "SCOPE_KIND_PROJECT",
    "SCOPE_KIND_WORKSPACE",
    "TERMINAL_STATUSES",
    "CandidateRefusal",
    "DreamCandidate",
    "DreamCitation",
    "DreamFoldResult",
    "DreamProposalEvent",
    "DreamProposalEventKind",
    "DreamProposalProjection",
    "DreamProposalStatus",
    "DreamProposalWrite",
    "DreamRevisionConflict",
    "DreamTransitionRefused",
    "NonresponseState",
    "PoolMessage",
    "attribution_for",
    "citations_from_json",
    "citations_to_json",
    "content_token_overlap",
    "content_tokens",
    "dedupe_by_content_sha256",
    "dream_proposal_summary_fields",
    "evidence_basis_for",
    "fold_dream_proposal",
    "is_duplicate_theme",
    "is_harness_text",
    "material_change",
    "nonresponse_state",
    "normalise_for_containment",
    "parse_dream_payload",
    "prepare_dream_write",
    "quote_is_contained",
    "resurface_interval_hours",
    "suppressed_by_rejection",
    "surfaced_event",
    "theme_similarity",
    "theme_tokens_for",
    "transition_allowed",
    "validate_candidate",
    "voice_violations",
]


# --------------------------------------------------------------------------- constants

DREAM_SCHEMA_VERSION: str = "v1"
DREAM_POLICY_REVISION: str = "p5-2026-09"
DREAM_PROMPT_ID: str = "dream_formation_v2"
MAX_DREAM_EVENTS_PER_FOLD: int = 500

# --- Y2 / p45_shared §5 T2: P1's vocabulary, reused as literals -------------------
# This module must stay pydantic-free (the worker loads it) and ``events.py`` imports
# pydantic, so the two values are declared here as strings and pinned to
# ``TrustedInputOriginKind`` by ``test_origin_kind_literals_match_p1``.  There is no
# parallel EVIDENCE_CLASS_* vocabulary: one enum, two literals, one gate that breaks the
# build on drift.
QUOTABLE_ORIGIN_KIND: str = "human_input"  # == TrustedInputOriginKind.HUMAN_INPUT.value
BACKFILL_ORIGIN_KIND: str = "imported_transcript"  # == TrustedInputOriginKind.IMPORTED_TRANSCRIPT.value
ADMISSIBLE_ORIGIN_KINDS: tuple[str, ...] = (QUOTABLE_ORIGIN_KIND, BACKFILL_ORIGIN_KIND)

EVIDENCE_BASIS_TRUSTED: str = "trusted_current"
EVIDENCE_BASIS_MIXED: str = "mixed"
EVIDENCE_BASIS_BACKFILL: str = "backfill_only"

# T5 / G19.  What a rendered proposal is allowed to say it is quoting.  Backfill is
# imported history: it is labelled, it is quotable, and it is never "you said".
ATTRIBUTION_SAID: str = "you said"
ATTRIBUTION_IMPORTED: str = "from your imported history"

SCOPE_KIND_PROJECT: str = "project"
SCOPE_KIND_WORKSPACE: str = "workspace"

BANNED_PROPOSAL_WORDS: frozenset[str] = frozenset(
    {
        "comprehensive",
        "leverage",
        "stakeholder",
        "framework",
        "roadmap",
        "strategy",
        "ecosystem",
        "robust",
        "holistic",
        "real-world",
        "best practice",
        "optimize",
        "optimise",
        "streamline",
        "synergy",
        "actionable",
        "utilize",
        "utilise",
    }
)

DREAM_STOPWORDS: frozenset[str] = frozenset(
    {
        "and", "are", "but", "can", "could", "did", "does", "for", "from", "get", "had",
        "has", "have", "how", "into", "its", "just", "like", "make", "more", "not", "now",
        "one", "out", "over", "should", "some", "than", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "those", "was", "were", "what", "when",
        "where", "which", "while", "will", "with", "would", "you", "your",
    }
)

# M6.  Lower-cased substrings, matched against the normalised body at pool admission.
# Calibrated on the live corpus rather than invented: the first matches 72 rows and the
# second 89 of the 4,649 backfill rows on this stack.  This is a BLOCKLIST and is
# honestly labelled as one — it catches the harness strings that exist and cannot catch a
# paste the harness did not author.  The structural half of that problem is the
# receipt-bound corpus; this is the presentational half.
HARNESS_TEXT_MARKERS: tuple[str, ...] = (
    "[request interrupted by user",
    "this session is being continued from a previous conversation",
    "caveat: the messages below were generated by the user while running",
    "<system-reminder>",
    "api error:",
    "[tool_use_id",
)

# Three CLOSED and DISJOINT vocabularies.  Each member has a named producer and a named
# reader; a reason that is not on one of these lists cannot be written to a run row.
CANDIDATE_REFUSAL_REASONS: tuple[str, ...] = (  # -> dream_generation_runs.refusals_json
    "unparseable_payload",
    "no_citations",
    "citation_out_of_range",
    "quote_not_found",
    "quote_voice_violation",
    "citation_not_relevant",
    "insufficient_citations",
    "no_quote_overlap",
    "voice_violation",
    "empty_required_field",
    "duplicate_theme",
    "suppressed_rejected_theme",
    "citations_unverifiable",
)
RUN_REFUSAL_REASONS: tuple[str, ...] = (  # -> dream_generation_runs.refusal_reason
    "model_disabled",
    "model_unavailable",
    "model_call_failed",
    "unentitled_project",
    "insufficient_messages",
    "too_many_open_proposals",
    "run_already_in_flight",
    "run_abandoned",
    "stale_input_revision",
    "lost_lease",
)
POOL_DROP_REASONS: tuple[str, ...] = (  # -> dream_generation_runs.pool_drops_json
    "undecryptable",
    "too_short",
    "duplicate_message",
    "harness_text",
    "truncated_paste",
)

# The five names ``material_change`` can return.  The failing condition is recorded on the
# run, so an over-suppressing system is visible in the record rather than looking like a
# quiet week.
MATERIAL_CHANGE_CONDITIONS: tuple[str, ...] = (
    "new_citations",
    "evidence_predates_rejection",
    "theme_too_similar",
    "cooldown_not_elapsed",
    "repropose_limit_reached",
)

DREAM_SETTING_NAMES: tuple[str, ...] = (
    "dream_proposals_enabled",
    "dream_message_limit",
    "dream_min_messages",
    "dream_min_message_chars",
    "dream_max_message_chars",
    "dream_max_proposals_per_run",
    "dream_min_citations",
    "dream_max_citations",
    "dream_quote_max_chars",
    "dream_min_quote_overlap_tokens",
    "dream_min_citation_relevance_tokens",
    "dream_max_live_proposals",
    "dream_duplicate_similarity",
    "dream_material_new_citations",
    "dream_material_max_similarity",
    "dream_rejected_cooldown_days",
    "dream_rejected_lookback_days",
    "dream_rejected_scan_limit",
    "dream_max_reproposals",
    "dream_nonresponse_after_surfaces",
    "dream_resurface_min_hours",
    "dream_resurface_ignored_hours",
    "dream_snooze_default_days",
    "dream_proposal_ttl_days",
    "dream_run_stale_minutes",
    "dream_list_limit",
)

# One source of truth for the defaults.  Three ``config.py`` files (Full, Lite, worker)
# declare these settings with identical names and identical defaults; a table one of them
# drifts from is a table a test can catch, and two silently different defaults across two
# backends is exactly the parity failure P5 is not allowed to introduce.
DREAM_SETTING_DEFAULTS: Mapping[str, bool | int | float] = {
    "dream_proposals_enabled": True,
    "dream_message_limit": 60,
    "dream_min_messages": 10,
    "dream_min_message_chars": 25,
    "dream_max_message_chars": 1200,
    "dream_max_proposals_per_run": 3,
    "dream_min_citations": 2,
    "dream_max_citations": 6,
    "dream_quote_max_chars": 200,
    "dream_min_quote_overlap_tokens": 2,
    "dream_min_citation_relevance_tokens": 1,
    "dream_max_live_proposals": 20,
    "dream_duplicate_similarity": 0.60,
    "dream_material_new_citations": 2,
    "dream_material_max_similarity": 0.60,
    "dream_rejected_cooldown_days": 30,
    "dream_rejected_lookback_days": 365,
    "dream_rejected_scan_limit": 200,
    "dream_max_reproposals": 2,
    "dream_nonresponse_after_surfaces": 3,
    "dream_resurface_min_hours": 24,
    "dream_resurface_ignored_hours": 168,
    "dream_snooze_default_days": 7,
    "dream_proposal_ttl_days": 45,
    "dream_run_stale_minutes": 30,
    "dream_list_limit": 10,
}


# --------------------------------------------------------------------------- vocabularies


class DreamProposalStatus(StrEnum):
    PROPOSED = "proposed"
    SURFACED = "surfaced"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    PURSUED = "pursued"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class DreamProposalEventKind(StrEnum):
    PROPOSED = "proposed"
    SURFACED = "surfaced"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    UNSNOOZED = "unsnoozed"
    # NEVER ``objective_set``: ``TaskStateEventKind.OBJECTIVE_SET`` already owns that name
    # and that value, and ``reconcile_dream_pursuit`` holds both enums in one function body.
    OBJECTIVE_BOUND = "objective_bound"
    PURSUIT_STARTED = "pursuit_started"
    PURSUIT_ABANDONED = "pursuit_abandoned"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    EVIDENCE_REVALIDATED = "evidence_revalidated"


class NonresponseState(StrEnum):
    NEVER_SURFACED = "never_surfaced"
    AWAITING_RESPONSE = "awaiting_response"
    IGNORED = "ignored"


# Every kind is pinned.  There are only fourteen, each sets a field no other kind sets, and
# the one counting kind carries its running total in the payload — so "keep the latest of
# every pinned kind plus a tail" is exactly lossless for the projection.  An earlier draft
# pinned three (copied from the task-state fold, where only three kinds change a verdict)
# and a truncated log lost 306 of 800 ``surfaced`` events and the whole task binding.
PINNED_DREAM_KINDS: frozenset[str] = frozenset(str(kind) for kind in DreamProposalEventKind)
# A frozenset has no stable iteration order and the two backends bind this list into SQL.
PINNED_DREAM_KIND_VALUES: tuple[str, ...] = tuple(sorted(PINNED_DREAM_KINDS))

HUMAN_VERDICT_ACTIONS: frozenset[str] = frozenset({"accepted", "rejected", "snoozed", "unsnoozed"})
DISPLAY_ACTIONS: frozenset[str] = frozenset({"surfaced"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "abandoned", "withdrawn", "expired", "rejected"})
LIVE_STATUSES: frozenset[str] = frozenset({"proposed", "surfaced", "snoozed", "accepted", "pursued"})

_KNOWN_DREAM_KINDS: frozenset[str] = frozenset(str(kind) for kind in DreamProposalEventKind)

# The closed transition table.  ``evidence_revalidated`` is legal from every status,
# including the terminal ones: re-checking the evidence behind a rejected proposal must not
# be blocked by the rejection.  ``proposed`` appears in no row — it is the mint, not a
# transition, and a second ``proposed`` on a live proposal is a bug, not a re-proposal.
_TRANSITIONS: Mapping[str, frozenset[str]] = {
    DreamProposalStatus.PROPOSED: frozenset(
        {"surfaced", "accepted", "rejected", "snoozed", "withdrawn", "expired", "superseded"}
    ),
    DreamProposalStatus.SURFACED: frozenset(
        {"surfaced", "accepted", "rejected", "snoozed", "withdrawn", "expired", "superseded"}
    ),
    DreamProposalStatus.SNOOZED: frozenset({"unsnoozed", "accepted", "rejected", "withdrawn", "expired"}),
    DreamProposalStatus.ACCEPTED: frozenset(
        {"objective_bound", "pursuit_started", "pursuit_abandoned", "rejected", "withdrawn"}
    ),
    DreamProposalStatus.PURSUED: frozenset({"completed", "pursuit_abandoned", "withdrawn"}),
    DreamProposalStatus.COMPLETED: frozenset(),
    DreamProposalStatus.ABANDONED: frozenset({"accepted"}),
    DreamProposalStatus.REJECTED: frozenset(),
    DreamProposalStatus.WITHDRAWN: frozenset(),
    DreamProposalStatus.EXPIRED: frozenset(),
}


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True, slots=True)
class DreamCitation:
    """One quoted message behind a proposal.

    ``receipt_id`` is not nullable: a message reaches the pool only through a
    ``trusted_input_receipts`` row, so a citation without one could not have been built.
    ``content_sha256`` is always the *receipt's* hash of the full pre-redaction text, which
    is what makes de-duplication across a re-ingested message work.
    """

    event_id: str
    receipt_id: str
    content_sha256: str
    origin_kind: str
    observed_at: datetime
    quote: str
    quote_sha256: str


@dataclass(frozen=True, slots=True)
class PoolMessage:
    """One numbered message the model is shown.  ``body`` is decrypted and NEVER persisted."""

    n: int
    event_id: str
    receipt_id: str
    content_sha256: str
    origin_kind: str
    observed_at: datetime
    body: str


@dataclass(frozen=True, slots=True)
class DreamCandidate:
    title: str
    connection_text: str
    benefit_text: str
    first_step: str
    citations: tuple[DreamCitation, ...]
    theme_tokens: tuple[str, ...]
    evidence_basis: str


@dataclass(frozen=True, slots=True)
class CandidateRefusal:
    reason: str  # a member of CANDIDATE_REFUSAL_REASONS
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DreamProposalEvent:
    """One immutable row of ``dream_proposal_events``."""

    seq: int
    kind: DreamProposalEventKind | str
    payload: Mapping[str, Any]
    occurred_at: datetime
    actor: str = ""
    actor_class: str = "system"  # "human" | "executor" | "system"
    source_event_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class DreamProposalProjection:
    proposal_id: str
    workspace_id: str
    owner_id: str
    subject_user_id: str
    project_id: str | None
    scope_kind: str
    session_id: str
    revision: int
    status: DreamProposalStatus
    nonresponse: NonresponseState
    surfaced_count: int
    surfaced_attested: bool
    first_surfaced_at: datetime | None
    last_surfaced_at: datetime | None
    title: str
    connection_text: str
    benefit_text: str
    first_step: str
    citations: tuple[DreamCitation, ...]
    theme_tokens: tuple[str, ...]
    evidence_basis: str
    evidence_revision: str
    evidence_cutoff_at: datetime | None
    supersedes_proposal_id: str | None
    repropose_depth: int
    snooze_until: datetime | None
    expires_at: datetime | None
    accepted_at: datetime | None
    rejected_at: datetime | None
    rejection_reason: str
    task_id: str | None
    objective_hash: str | None
    plan_root_goal_id: str | None
    pursuit_started_at: datetime | None
    completed_at: datetime | None
    abandoned_at: datetime | None
    abandon_reason: str
    withdrawn_reason: str
    schema_version: str = DREAM_SCHEMA_VERSION
    policy_revision: str = DREAM_POLICY_REVISION


@dataclass(frozen=True, slots=True)
class DreamFoldResult:
    projection: DreamProposalProjection
    source_revision: str
    unknown_kinds: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class DreamProposalWrite:
    expected_revision: int
    next_revision: int
    next_seq_start: int
    projection: DreamProposalProjection
    source_revision: str
    events: tuple[DreamProposalEvent, ...]


# --------------------------------------------------------------------------- exceptions


class DreamRevisionConflict(RuntimeError):
    """CAS lost the race.  Carries ``proposal_id`` so a retrying client knows which row lost."""

    proposal_id: str
    expected_revision: int
    actual_revision: int

    def __init__(self, *, proposal_id: str, expected_revision: int, actual_revision: int) -> None:
        super().__init__(
            f"dream proposal {proposal_id} revision {expected_revision} != {actual_revision}"
        )
        self.proposal_id = proposal_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class DreamTransitionRefused(RuntimeError):
    """The requested transition is not in the closed table for the proposal's current status."""

    proposal_id: str
    reason: str

    def __init__(self, *, proposal_id: str, reason: str) -> None:
        super().__init__(f"dream proposal {proposal_id}: {reason}")
        self.proposal_id = proposal_id
        self.reason = reason


# --------------------------------------------------------------------------- small coercions
#
# Deliberately local rather than imported from ``task_state``: those helpers are private to
# that module, and a fold that cannot read a payload written by an older build must degrade
# to a default rather than raise.  ``warn_return_any`` binds here, so every one of these
# returns a concrete type.


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _dt_to_json(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    return _aware(value).astimezone(UTC).isoformat()


def _dt_from_json(raw: Any, default: datetime | None = None) -> datetime | None:
    if isinstance(raw, datetime):
        return _aware(raw)
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return default


def _text(raw: Any, default: str = "") -> str:
    if isinstance(raw, str):
        return raw
    if raw is None or isinstance(raw, (dict, list, tuple, set)):
        return default
    return str(raw)


def _opt_text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = _text(raw, "")
    return value or None


def _integer(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


def _flag(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    return default


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    return payload if isinstance(payload, Mapping) else {}


def _mappings(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- M1-M6 devices

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def content_tokens(value: str) -> tuple[str, ...]:
    """Lower-cased alphanumeric tokens longer than two characters, stopwords removed.

    Order-preserving and de-duplicated, so the return value is stable enough to store in a
    column and compare later.  ``hosted-cv-angular`` yields ``("hosted", "angular")``: the
    hyphen is a separator, ``cv`` is too short to carry meaning, and the point of the floor
    is that two-letter fragments make everything look related to everything.
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in _TOKEN_RE.findall(_text(value).lower()):
        if len(token) <= 2 or token in DREAM_STOPWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


def content_token_overlap(left: str, right: str) -> int:
    """How many content tokens two strings share.  The whole of M2 and M2b rests on this."""
    return len(set(content_tokens(left)) & set(content_tokens(right)))


def theme_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Jaccard over two token sets.  Two empty themes are not similar; they are unknown."""
    left_set = {str(token).strip().lower() for token in left if str(token).strip()}
    right_set = {str(token).strip().lower() for token in right if str(token).strip()}
    if not left_set or not right_set:
        return 0.0
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)


def theme_tokens_for(title: str, first_step: str, quotes: Sequence[str] = ()) -> tuple[str, ...]:
    """The token set a proposal is compared by.

    Duplicate suppression uses ``title + first_step`` (no quotes) — that is what a reader
    would call "the same idea".  ``material_change`` rule 3 passes the quotes as well,
    because "the same proposal with different words" has to include the words it cites.
    """
    parts = [_text(title), _text(first_step), *[_text(quote) for quote in quotes]]
    return content_tokens(" ".join(part for part in parts if part))


def normalise_for_containment(value: str) -> str:
    """Lower-case and collapse whitespace.  Nothing else.

    Punctuation and spelling are left alone on purpose: the owner's typos are the strongest
    single signal that a quote is really his, and a normaliser that repaired them would let
    a model quote a cleaned-up sentence he never typed.
    """
    return _WHITESPACE_RE.sub(" ", _text(value).lower()).strip()


def quote_is_contained(quote: str, body: str, *, max_chars: int) -> bool:
    """M1.  Is ``quote`` a contiguous verbatim span of ``body``?

    Three floors, all of them refusals rather than repairs: a quote shorter than four
    characters or longer than ``max_chars`` is not a quote, and a span with fewer than three
    content tokens is not evidence of anything — ``the`` appears in every message, so
    "containment" alone would certify nothing.
    """
    raw = _text(quote).strip()
    if len(raw) < 4 or len(raw) > max(0, int(max_chars)):
        return False
    needle = normalise_for_containment(raw)
    if not needle:
        return False
    if len(content_tokens(needle)) < 3:
        return False
    return needle in normalise_for_containment(body)


def voice_violations(value: str) -> tuple[str, ...]:
    """M3.  The banned words present, plus ``"title_case"``, sorted.  Empty means clean.

    Run on the claim *and* on every quote.  Running it on the claim alone is how a proposal
    quoting pasted marketing prose — ``Here's my comprehensive review: ...`` — got emitted in
    an earlier draft: the model had not written the consultant voice, it had merely chosen to
    show it, and the reader cannot tell the difference on screen.
    """
    lowered = normalise_for_containment(value)
    if not lowered:
        return ()
    found: set[str] = set()
    for word in BANNED_PROPOSAL_WORDS:
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", lowered):
            found.add(word)
    words = [word for word in _WORD_RE.findall(_text(value)) if len(word) >= 4]
    if words:
        capitalised = sum(1 for word in words if word[:1].isupper())
        if capitalised / len(words) >= 0.60:
            found.add("title_case")
    return tuple(sorted(found))


def is_harness_text(body: str) -> bool:
    """M6.  Does this body carry a marker the coding harness — not the owner — wrote?"""
    lowered = normalise_for_containment(body)
    if not lowered:
        return False
    return any(marker in lowered for marker in HARNESS_TEXT_MARKERS)


def dedupe_by_content_sha256(citations: Sequence[DreamCitation]) -> tuple[DreamCitation, ...]:
    """D11.  The same message ingested twice is one citation, not two.

    Keyed on the receipt's hash of the full pre-redaction text rather than on the event id,
    because two ingests of one message are two event rows with one hash — and a proposal
    that looks twice as well-evidenced as it is, is exactly the thing the citation floor is
    supposed to prevent.
    """
    seen: set[str] = set()
    out: list[DreamCitation] = []
    for citation in citations:
        key = citation.content_sha256 or f"event:{citation.event_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(citation)
    return tuple(out)


def evidence_basis_for(citations: Sequence[DreamCitation]) -> str:
    """``trusted_current`` | ``backfill_only`` | ``mixed``, from the receipts' origin kinds."""
    kinds = {str(citation.origin_kind) for citation in citations}
    if not kinds:
        return EVIDENCE_BASIS_TRUSTED
    if kinds == {BACKFILL_ORIGIN_KIND}:
        return EVIDENCE_BASIS_BACKFILL
    if BACKFILL_ORIGIN_KIND in kinds:
        return EVIDENCE_BASIS_MIXED
    return EVIDENCE_BASIS_TRUSTED


def attribution_for(evidence_basis: str) -> str:
    """T5 / G19.  What the rendered proposal is allowed to claim about where the words came from.

    A ``backfill_only`` proposal is quoting imported history.  Rendering it as *you said*
    would tell the owner he typed something today that he typed — if at all — in a transcript
    somebody imported, and the whole value of a quote is that the reader can trust its
    provenance.
    """
    if str(evidence_basis) == EVIDENCE_BASIS_BACKFILL:
        return ATTRIBUTION_IMPORTED
    return ATTRIBUTION_SAID


# --------------------------------------------------------------------------- parse + validate


def parse_dream_payload(
    payload: Any,
    *,
    pool: Sequence[PoolMessage],
    max_proposals: int,
    max_citations: int,
    quote_max_chars: int,
) -> tuple[list[DreamCandidate], list[CandidateRefusal]]:
    """Resolve a model reply against the FROZEN pool.  Structure only; the devices come next.

    A citation number the model invented resolves to nothing and is dropped
    (``citation_out_of_range``).  The number is the only thing a model can guess; the quote is
    checked in :func:`validate_candidate`, which is why the two stages are separate.
    """
    refusals: list[CandidateRefusal] = []
    by_index = {int(message.n): message for message in pool}

    body = _payload_mapping(payload)
    if not body:
        return [], [CandidateRefusal("unparseable_payload", "reply was not a JSON object")]
    raw_items = body.get("dreams")
    if not isinstance(raw_items, (list, tuple)):
        return [], [CandidateRefusal("unparseable_payload", "no 'dreams' list in reply")]

    candidates: list[DreamCandidate] = []
    for item in _mappings(raw_items):
        if len(candidates) >= max(0, int(max_proposals)):
            break
        title = _text(item.get("title")).strip()
        connection = _text(item.get("connection")).strip()
        benefit = _text(item.get("benefit")).strip()
        first_step = _text(item.get("first_step")).strip()
        if not title or not first_step or not connection or not benefit:
            refusals.append(CandidateRefusal("empty_required_field", title[:120]))
            continue

        raw_citations = _mappings(item.get("citations"))
        if not raw_citations:
            refusals.append(CandidateRefusal("no_citations", title[:120]))
            continue

        resolved: list[DreamCitation] = []
        out_of_range = 0
        for raw in raw_citations:
            if len(resolved) >= max(0, int(max_citations)):
                break
            message = by_index.get(_integer(raw.get("n"), -1))
            if message is None:
                out_of_range += 1
                continue
            quote = _text(raw.get("quote")).strip()[: max(0, int(quote_max_chars))]
            if not quote:
                out_of_range += 1
                continue
            resolved.append(
                DreamCitation(
                    event_id=message.event_id,
                    receipt_id=message.receipt_id,
                    content_sha256=message.content_sha256,
                    origin_kind=message.origin_kind,
                    observed_at=_aware(message.observed_at),
                    quote=quote,
                    quote_sha256=_sha256(normalise_for_containment(quote)),
                )
            )
        if out_of_range:
            refusals.append(CandidateRefusal("citation_out_of_range", f"{title[:80]} ({out_of_range})"))
        if not resolved:
            refusals.append(CandidateRefusal("no_citations", title[:120]))
            continue

        citations = dedupe_by_content_sha256(resolved)
        candidates.append(
            DreamCandidate(
                title=title,
                connection_text=connection,
                benefit_text=benefit,
                first_step=first_step,
                citations=citations,
                theme_tokens=theme_tokens_for(title, first_step),
                evidence_basis=evidence_basis_for(citations),
            )
        )
    return candidates, refusals


def validate_candidate(
    candidate: DreamCandidate,
    *,
    pool: Sequence[PoolMessage],
    min_citations: int,
    min_quote_overlap_tokens: int,
    quote_max_chars: int,
    min_citation_relevance_tokens: int = 1,
) -> tuple[DreamCandidate | None, CandidateRefusal | None]:
    """M1, M3-on-quotes, M2b, then the re-checked count, then M2, then M3-on-the-claim.

    The order matters and the count is re-checked **after every filter**.  An earlier draft
    validated a candidate with two citations and persisted it with zero, because nothing
    counted again once the drops had happened.

    Returns the surviving candidate with its dropped citations removed, or ``(None, refusal)``.
    Returning only a refusal — as an earlier draft did — gives the caller no way to receive the
    narrowed candidate, which is precisely what drop-then-recount needs.

    ``min_citation_relevance_tokens`` is keyword-only with a default so the declared
    cross-builder signature stays callable unchanged; callers pass the setting.
    """
    bodies = {message.event_id: message.body for message in pool}

    citations = dedupe_by_content_sha256(candidate.citations)

    kept = [
        citation
        for citation in citations
        if quote_is_contained(citation.quote, bodies.get(citation.event_id, ""), max_chars=quote_max_chars)
    ]
    if len(kept) < len(citations) and not kept:
        return None, CandidateRefusal("quote_not_found", candidate.title[:120])
    dropped_not_found = len(citations) - len(kept)

    citations = tuple(kept)
    kept = [citation for citation in citations if not voice_violations(citation.quote)]
    dropped_voice = len(citations) - len(kept)

    citations = tuple(kept)
    floor = max(0, int(min_citation_relevance_tokens))
    kept = [
        citation
        for citation in citations
        if content_token_overlap(candidate.title, citation.quote) >= floor
    ]
    dropped_relevance = len(citations) - len(kept)
    citations = tuple(kept)

    if len(citations) < max(0, int(min_citations)):
        detail = (
            f"{candidate.title[:80]} kept={len(citations)} "
            f"quote_not_found={dropped_not_found} "
            f"quote_voice_violation={dropped_voice} "
            f"citation_not_relevant={dropped_relevance}"
        )
        return None, CandidateRefusal("insufficient_citations", detail)

    joined = " ".join(citation.quote for citation in citations)
    if content_token_overlap(candidate.title, joined) < max(0, int(min_quote_overlap_tokens)):
        # M2, over the TITLE ALONE.  ``first_step`` is not on the left side: the model writes
        # both the first_step and the choice of quote, so putting it there lets the model
        # manufacture its own overlap.  The quote is the only string on either side the model
        # did not author.
        return None, CandidateRefusal("no_quote_overlap", candidate.title[:120])

    violations = voice_violations(f"{candidate.title} {candidate.first_step}")
    if violations:
        return None, CandidateRefusal("voice_violation", ", ".join(violations))

    return (
        replace(
            candidate,
            citations=citations,
            theme_tokens=theme_tokens_for(candidate.title, candidate.first_step),
            evidence_basis=evidence_basis_for(citations),
        ),
        None,
    )


# --------------------------------------------------------------------------- suppression


def is_duplicate_theme(
    candidate: DreamCandidate,
    live: Sequence[DreamProposalProjection],
    *,
    threshold: float,
) -> DreamProposalProjection | None:
    """The live proposal this candidate is a restatement of, or ``None``.

    Reachable only after :func:`suppressed_by_rejection` has cleared the candidate.  Running
    it first would let a candidate carrying a rejected theme merge into a live neighbour and
    come back strengthened, with no refusal recorded anywhere.
    """
    best: DreamProposalProjection | None = None
    best_score = 0.0
    for proposal in live:
        if str(proposal.status) not in LIVE_STATUSES:
            continue
        score = theme_similarity(candidate.theme_tokens, proposal.theme_tokens)
        if score >= float(threshold) and score > best_score:
            best = proposal
            best_score = score
    return best


def material_change(
    candidate: DreamCandidate,
    rejected: DreamProposalProjection,
    *,
    now: datetime,
    min_new_citations: int,
    max_similarity: float,
    cooldown_days: int,
    max_reproposals: int,
) -> tuple[bool, str]:
    """Has enough genuinely changed to reopen a theme the owner rejected?

    All five conditions must hold.  Returns ``(False, <name of the first that failed>)`` so the
    run row can say *why* it suppressed, which is what makes an over-suppressing system visible
    instead of looking like a quiet week.

    Rule 2 is the load-bearing one.  Evidence that already existed when the owner said no is
    not new evidence — he rejected the theme *while* it existed.  Only something he said
    **after** the rejection can reopen it.  Without rule 2, a model that simply cites two
    older messages the first proposal happened to miss reopens every rejection, forever.
    """
    if int(rejected.repropose_depth) >= max(0, int(max_reproposals)):
        return False, "repropose_limit_reached"

    rejected_at = rejected.rejected_at
    if rejected_at is None:
        # Not actually rejected by a human; there is nothing to lift.
        return False, "repropose_limit_reached"
    rejected_at = _aware(rejected_at)

    known = {citation.content_sha256 for citation in rejected.citations if citation.content_sha256}
    fresh = [citation for citation in candidate.citations if citation.content_sha256 not in known]
    if len(fresh) < max(0, int(min_new_citations)):
        return False, "new_citations"

    if any(_aware(citation.observed_at) <= rejected_at for citation in fresh):
        return False, "evidence_predates_rejection"

    candidate_tokens = theme_tokens_for(
        candidate.title, candidate.first_step, [citation.quote for citation in candidate.citations]
    )
    rejected_tokens = theme_tokens_for(
        rejected.title, rejected.first_step, [citation.quote for citation in rejected.citations]
    )
    if theme_similarity(candidate_tokens, rejected_tokens) > float(max_similarity):
        return False, "theme_too_similar"

    if _aware(now) - rejected_at < timedelta(days=max(0, int(cooldown_days))):
        return False, "cooldown_not_elapsed"

    return True, ""


def suppressed_by_rejection(
    candidate: DreamCandidate,
    rejected: Sequence[DreamProposalProjection],
    *,
    now: datetime,
    threshold: float,
    min_new_citations: int,
    max_similarity: float,
    cooldown_days: int,
    max_reproposals: int,
) -> tuple[DreamProposalProjection | None, str]:
    """One call, scanning every rejection in scope, run BEFORE the duplicate-merge path.

    Returns ``(blocking_proposal, failing_condition)`` or ``(None, "")``.  The scan is over
    both scope kinds for the same subject: a theme rejected while working unbound is the same
    theme when it comes back inside a project, and bucketing by scope kind is how a rejection
    became invisible to the only mode that could currently produce anything.
    """
    for proposal in rejected:
        if str(proposal.status) != DreamProposalStatus.REJECTED:
            continue
        if theme_similarity(candidate.theme_tokens, proposal.theme_tokens) < float(threshold):
            continue
        lifted, failing = material_change(
            candidate,
            proposal,
            now=now,
            min_new_citations=min_new_citations,
            max_similarity=max_similarity,
            cooldown_days=cooldown_days,
            max_reproposals=max_reproposals,
        )
        if not lifted:
            return proposal, failing
    return None, ""


# --------------------------------------------------------------------------- nonresponse


def nonresponse_state(
    *,
    surfaced_count: int,
    last_surfaced_at: datetime | None,
    now: datetime,
    after_surfaces: int,
) -> NonresponseState:
    """X2.  Derived from explicitly recorded ``surfaced`` events, and from nothing else.

    ``last_surfaced_at`` and ``now`` are in the signature and are **deliberately unread**.  The
    passage of time may never become an answer: a proposal nobody ever put in front of the
    owner reads ``never_surfaced`` a year later, not ``ignored``, because nothing about it was
    ignored — it was never shown.  Collapsing "not seen", "seen and ignored" and "never
    surfaced" into one nullable timestamp is the defect this whole axis exists to remove, and
    the two parameters stay so that a future reader who reaches for a clock finds this
    paragraph first.
    """
    _ = (last_surfaced_at, now)
    count = max(0, int(surfaced_count))
    if count == 0:
        return NonresponseState.NEVER_SURFACED
    if count < max(1, int(after_surfaces)):
        return NonresponseState.AWAITING_RESPONSE
    return NonresponseState.IGNORED


def resurface_interval_hours(state: NonresponseState, *, min_hours: int, ignored_hours: int) -> int:
    """How long before this proposal may be shown again.  ``IGNORED``'s only functional effect.

    Note what ``ignored`` does *not* do: it does not suppress the theme, does not count in any
    rejection metric and does not shorten ``expires_at``.  It slows re-surfacing down.  That is
    the whole of it, and a state with no reader would be a label pretending to be a decision.
    """
    if state is NonresponseState.NEVER_SURFACED:
        return 0
    if state is NonresponseState.IGNORED:
        return max(0, int(ignored_hours))
    return max(0, int(min_hours))


# --------------------------------------------------------------------------- codecs


def citations_to_json(items: Sequence[DreamCitation]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": item.event_id,
            "receipt_id": item.receipt_id,
            "content_sha256": item.content_sha256,
            "origin_kind": item.origin_kind,
            "observed_at": _dt_to_json(item.observed_at),
            "quote": item.quote,
            "quote_sha256": item.quote_sha256,
        }
        for item in items
    ]


def citations_from_json(raw: Any) -> tuple[DreamCitation, ...]:
    """Total.  A row an older build wrote, or a row with a missing field, degrades — never raises."""
    out: list[DreamCitation] = []
    for item in _mappings(raw):
        event_id = _text(item.get("event_id"))
        if not event_id:
            continue
        quote = _text(item.get("quote"))
        origin_kind = _text(item.get("origin_kind"))
        out.append(
            DreamCitation(
                event_id=event_id,
                receipt_id=_text(item.get("receipt_id")),
                content_sha256=_text(item.get("content_sha256")),
                origin_kind=origin_kind if origin_kind in ADMISSIBLE_ORIGIN_KINDS else QUOTABLE_ORIGIN_KIND,
                observed_at=_dt_from_json(item.get("observed_at")) or datetime(1970, 1, 1, tzinfo=UTC),
                quote=quote,
                quote_sha256=_text(item.get("quote_sha256")) or _sha256(normalise_for_containment(quote)),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- transitions


def transition_allowed(current: DreamProposalStatus, kind: DreamProposalEventKind) -> tuple[bool, str]:
    """The closed table.  ``(True, "")`` or ``(False, <why>)``; the caller raises."""
    kind_value = str(kind)
    if kind_value == DreamProposalEventKind.EVIDENCE_REVALIDATED:
        # Legal from every status, terminal ones included: re-checking the evidence behind a
        # rejected proposal must not be blocked by the rejection.
        return True, ""
    if kind_value == DreamProposalEventKind.PROPOSED:
        return False, "a proposal is minted, not transitioned into 'proposed'"
    allowed = _TRANSITIONS.get(str(current), frozenset())
    if kind_value in allowed:
        return True, ""
    return False, f"cannot apply '{kind_value}' to a proposal that is '{current}'"


def surfaced_event(
    *,
    ordinal: int,
    first_surfaced_at: datetime,
    occurred_at: datetime,
    actor: str,
    actor_class: str,
    prior_attested: bool = False,
) -> DreamProposalEvent:
    """The ONLY constructor of a ``surfaced`` event.

    ``surfaced`` is the one kind whose contribution to the projection is a count, and a count
    is not idempotent under "keep the latest".  So the running values — the ordinal, the first
    time it was ever shown, and whether a verified human has ever attested a showing — are
    computed by the caller from the locked row and stamped into the payload here.  The fold
    then takes ``max(ordinal)`` instead of accumulating, which is what makes truncation
    lossless.

    ``attested_ever`` is a *running* flag rather than a per-event one for a measured reason: a
    per-event flag stamped on the first showing is dropped by truncation, and the projection
    then claims a proposal was never attested when it was.

    ``prior_attested`` is keyword-only with a default so the declared cross-builder signature
    stays callable unchanged; the store passes the locked row's value.
    """
    return DreamProposalEvent(
        seq=0,
        kind=DreamProposalEventKind.SURFACED,
        payload={
            "ordinal": max(1, int(ordinal)),
            "first_surfaced_at": _dt_to_json(first_surfaced_at),
            "attested_ever": bool(prior_attested) or str(actor_class) == "human",
        },
        occurred_at=_aware(occurred_at),
        actor=actor,
        actor_class=str(actor_class),
    )


# --------------------------------------------------------------------------- the fold


def _payload_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(_payload_mapping(payload)).encode("utf-8")).hexdigest()[:12]


def _sort_key(event: DreamProposalEvent) -> tuple[int, str, str]:
    return (int(event.seq), str(event.kind), canonical_json(_payload_mapping(event.payload)))


def _dedupe(events: Sequence[DreamProposalEvent]) -> list[DreamProposalEvent]:
    seen: set[tuple[int, str, str]] = set()
    out: list[DreamProposalEvent] = []
    for event in events:
        key = (int(event.seq), str(event.kind), _payload_digest(event.payload))
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _source_revision(events: Sequence[DreamProposalEvent]) -> str:
    if not events:
        return "empty"
    joined = "|".join(
        f"{int(event.seq)}:{event.kind}:{_payload_digest(event.payload)}" for event in events
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def fold_dream_proposal(
    events: Sequence[DreamProposalEvent],
    *,
    proposal_id: str,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    session_id: str,
    revision: int,
    now: datetime,
    after_surfaces: int,
    max_events: int = MAX_DREAM_EVENTS_PER_FOLD,
) -> DreamFoldResult:
    """Replay ``events`` into a projection.  Deterministic, total, and never raises.

    An unknown kind is collected into ``unknown_kinds`` and ignored, so a rollback to an older
    build cannot brick a proposal.  Ordering is ``(seq, kind, payload_digest)`` with
    de-duplication on the same key, so two writers that stamped one seq fold identically.
    """
    ordered = _dedupe(sorted(events, key=_sort_key))
    truncated = False
    if len(ordered) > max_events:
        truncated = True
        # Keep the latest event of EVERY kind, plus a tail.  A formula both the fold and a
        # SQL window can evaluate identically is the only way a rebuild and the stored row
        # provably agree; and because every kind is pinned and ``surfaced`` carries its
        # running totals, dropping the middle of the log loses nothing the projection reads.
        tail_size = max(0, max_events - len(PINNED_DREAM_KINDS))
        tail = list(ordered[-tail_size:]) if tail_size else []
        pinned: list[DreamProposalEvent] = []
        for kind in PINNED_DREAM_KIND_VALUES:
            latest: DreamProposalEvent | None = None
            for event in ordered:
                if str(event.kind) == kind:
                    latest = event
            if latest is not None:
                pinned.append(latest)
        ordered = _dedupe(sorted(pinned + tail, key=_sort_key))

    status = DreamProposalStatus.PROPOSED
    project_id: str | None = None
    scope_kind = SCOPE_KIND_PROJECT
    surfaced_count = 0
    surfaced_attested = False
    first_surfaced_at: datetime | None = None
    last_surfaced_at: datetime | None = None
    title = ""
    connection_text = ""
    benefit_text = ""
    first_step = ""
    citations: tuple[DreamCitation, ...] = ()
    theme_tokens: tuple[str, ...] = ()
    evidence_basis = EVIDENCE_BASIS_TRUSTED
    evidence_revision = ""
    evidence_cutoff_at: datetime | None = None
    supersedes_proposal_id: str | None = None
    repropose_depth = 0
    snooze_until: datetime | None = None
    expires_at: datetime | None = None
    accepted_at: datetime | None = None
    rejected_at: datetime | None = None
    rejection_reason = ""
    task_id: str | None = None
    objective_hash: str | None = None
    plan_root_goal_id: str | None = None
    pursuit_started_at: datetime | None = None
    completed_at: datetime | None = None
    abandoned_at: datetime | None = None
    abandon_reason = ""
    withdrawn_reason = ""
    unknown_kinds: list[str] = []

    for event in ordered:
        kind = str(event.kind)
        payload = _payload_mapping(event.payload)
        occurred_at = _aware(event.occurred_at)

        if kind not in _KNOWN_DREAM_KINDS:
            unknown_kinds.append(kind)
            continue

        if kind == DreamProposalEventKind.PROPOSED:
            title = _text(payload.get("title"))
            connection_text = _text(payload.get("connection_text"))
            benefit_text = _text(payload.get("benefit_text"))
            first_step = _text(payload.get("first_step"))
            citations = citations_from_json(payload.get("citations"))
            raw_tokens = payload.get("theme_tokens")
            theme_tokens = (
                tuple(_text(token) for token in raw_tokens if _text(token))
                if isinstance(raw_tokens, (list, tuple))
                else theme_tokens_for(title, first_step)
            )
            evidence_basis = _text(payload.get("evidence_basis"), EVIDENCE_BASIS_TRUSTED) or EVIDENCE_BASIS_TRUSTED
            evidence_revision = _text(payload.get("evidence_revision"))
            evidence_cutoff_at = _dt_from_json(payload.get("evidence_cutoff_at"))
            project_id = _opt_text(payload.get("project_id"))
            scope_kind = _text(payload.get("scope_kind"), SCOPE_KIND_PROJECT) or SCOPE_KIND_PROJECT
            supersedes_proposal_id = _opt_text(payload.get("supersedes_proposal_id"))
            repropose_depth = _integer(payload.get("repropose_depth"))
            expires_at = _dt_from_json(payload.get("expires_at"))
            status = DreamProposalStatus.PROPOSED
        elif kind == DreamProposalEventKind.SURFACED:
            surfaced_count = max(surfaced_count, _integer(payload.get("ordinal"), surfaced_count + 1))
            first_surfaced_at = _dt_from_json(payload.get("first_surfaced_at"), first_surfaced_at) or occurred_at
            surfaced_attested = surfaced_attested or _flag(payload.get("attested_ever"))
            last_surfaced_at = occurred_at
            if status is DreamProposalStatus.PROPOSED:
                status = DreamProposalStatus.SURFACED
        elif kind == DreamProposalEventKind.ACCEPTED:
            accepted_at = _dt_from_json(payload.get("accepted_at"), occurred_at) or occurred_at
            status = DreamProposalStatus.ACCEPTED
        elif kind == DreamProposalEventKind.REJECTED:
            rejected_at = _dt_from_json(payload.get("rejected_at"), occurred_at) or occurred_at
            rejection_reason = _text(payload.get("reason"))[:400]
            status = DreamProposalStatus.REJECTED
        elif kind == DreamProposalEventKind.SNOOZED:
            snooze_until = _dt_from_json(payload.get("snooze_until"))
            status = DreamProposalStatus.SNOOZED
        elif kind == DreamProposalEventKind.UNSNOOZED:
            snooze_until = None
            expires_at = _dt_from_json(payload.get("expires_at"), expires_at)
            status = DreamProposalStatus.PROPOSED
        elif kind == DreamProposalEventKind.OBJECTIVE_BOUND:
            # NO STATUS CHANGE.  This is the fold rule that makes "completion is never
            # inferred from plan creation" structural rather than procedural: binding an
            # objective, and later creating a plan for it, cannot move the proposal anywhere.
            task_id = _opt_text(payload.get("task_id"))
            objective_hash = _opt_text(payload.get("objective_hash"))
            plan_root_goal_id = _opt_text(payload.get("plan_root_goal_id")) or plan_root_goal_id
        elif kind == DreamProposalEventKind.PURSUIT_STARTED:
            pursuit_started_at = _dt_from_json(payload.get("pursuit_started_at"), occurred_at) or occurred_at
            plan_root_goal_id = _opt_text(payload.get("plan_root_goal_id")) or plan_root_goal_id
            status = DreamProposalStatus.PURSUED
        elif kind == DreamProposalEventKind.PURSUIT_ABANDONED:
            abandoned_at = _dt_from_json(payload.get("abandoned_at"), occurred_at) or occurred_at
            abandon_reason = _text(payload.get("reason"))[:400]
            status = DreamProposalStatus.ABANDONED
        elif kind == DreamProposalEventKind.COMPLETED:
            completed_at = _dt_from_json(payload.get("completed_at"), occurred_at) or occurred_at
            status = DreamProposalStatus.COMPLETED
        elif kind == DreamProposalEventKind.WITHDRAWN:
            withdrawn_reason = _text(payload.get("reason"))[:400]
            status = DreamProposalStatus.WITHDRAWN
        elif kind == DreamProposalEventKind.SUPERSEDED:
            # There is no separate ``superseded`` status: a superseded proposal is withdrawn
            # with a reason, and the successor carries ``supersedes_proposal_id``, so the chain
            # is walkable in both directions without an eleventh status.
            withdrawn_reason = "superseded"
            status = DreamProposalStatus.WITHDRAWN
        elif kind == DreamProposalEventKind.EXPIRED:
            status = DreamProposalStatus.EXPIRED
        elif kind == DreamProposalEventKind.EVIDENCE_REVALIDATED:
            # NEVER changes status.  If the surviving set is below the floor the caller appends
            # a ``withdrawn`` event in the SAME write; a fold that changed status on its own
            # would make the event log a lie about what happened.
            citations = citations_from_json(payload.get("citations"))
            evidence_basis = _text(payload.get("evidence_basis"), evidence_basis) or evidence_basis
            evidence_revision = _text(payload.get("evidence_revision"), evidence_revision)
            evidence_cutoff_at = _dt_from_json(payload.get("evidence_cutoff_at"), evidence_cutoff_at)

    nonresponse = nonresponse_state(
        surfaced_count=surfaced_count,
        last_surfaced_at=last_surfaced_at,
        now=now,
        after_surfaces=after_surfaces,
    )

    projection = DreamProposalProjection(
        proposal_id=proposal_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        subject_user_id=subject_user_id,
        project_id=project_id,
        scope_kind=scope_kind,
        session_id=session_id,
        revision=int(revision),
        status=status,
        nonresponse=nonresponse,
        surfaced_count=surfaced_count,
        surfaced_attested=surfaced_attested,
        first_surfaced_at=first_surfaced_at,
        last_surfaced_at=last_surfaced_at,
        title=title,
        connection_text=connection_text,
        benefit_text=benefit_text,
        first_step=first_step,
        citations=citations,
        theme_tokens=theme_tokens,
        evidence_basis=evidence_basis,
        evidence_revision=evidence_revision,
        evidence_cutoff_at=evidence_cutoff_at,
        supersedes_proposal_id=supersedes_proposal_id,
        repropose_depth=repropose_depth,
        snooze_until=snooze_until,
        expires_at=expires_at,
        accepted_at=accepted_at,
        rejected_at=rejected_at,
        rejection_reason=rejection_reason,
        task_id=task_id,
        objective_hash=objective_hash,
        plan_root_goal_id=plan_root_goal_id,
        pursuit_started_at=pursuit_started_at,
        completed_at=completed_at,
        abandoned_at=abandoned_at,
        abandon_reason=abandon_reason,
        withdrawn_reason=withdrawn_reason,
    )
    return DreamFoldResult(
        projection=projection,
        source_revision=_source_revision(ordered),
        unknown_kinds=tuple(unknown_kinds),
        truncated=truncated,
    )


def prepare_dream_write(
    prior_events: Sequence[DreamProposalEvent],
    new_events: Sequence[DreamProposalEvent],
    *,
    proposal_id: str,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    session_id: str,
    expected_revision: int,
    highest_seq: int,
    now: datetime,
    after_surfaces: int,
) -> DreamProposalWrite:
    """Stamp sequence numbers and fold prior+new at ``expected_revision + 1``.  Pure."""
    stamped: list[DreamProposalEvent] = []
    seq = int(highest_seq)
    for event in new_events:
        seq += 1
        stamped.append(replace(event, seq=seq))
    folded = fold_dream_proposal(
        list(prior_events) + stamped,
        proposal_id=proposal_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        subject_user_id=subject_user_id,
        session_id=session_id,
        revision=int(expected_revision) + 1,
        now=now,
        after_surfaces=after_surfaces,
    )
    return DreamProposalWrite(
        expected_revision=int(expected_revision),
        next_revision=int(expected_revision) + 1,
        next_seq_start=int(highest_seq) + 1,
        projection=folded.projection,
        source_revision=folded.source_revision,
        events=tuple(stamped),
    )


# --------------------------------------------------------------------------- wire helper


def dream_proposal_summary_fields(
    projection: DreamProposalProjection, *, source_revision: str, citations_verified: str
) -> dict[str, Any]:
    """Every ``DreamProposal`` wire field, derived once so the two backends cannot diverge.

    Returns a plain dict: this module stays pydantic-free and the caller builds the model.
    ``attribution`` is derived here rather than in a renderer, because a rendering rule that
    lives in two backends is a rendering rule that will disagree in one of them.
    """
    return {
        "id": projection.proposal_id,
        "workspace_id": projection.workspace_id,
        "project_id": projection.project_id,
        "scope_kind": projection.scope_kind,
        "status": str(projection.status),
        "nonresponse": str(projection.nonresponse),
        "surfaced_count": projection.surfaced_count,
        "surfaced_attested": projection.surfaced_attested,
        "title": projection.title,
        "connection": projection.connection_text,
        "benefit": projection.benefit_text,
        "first_step": projection.first_step,
        "citations": citations_to_json(projection.citations),
        "citation_count": len(projection.citations),
        "citations_verified": citations_verified,
        "evidence_basis": projection.evidence_basis,
        "evidence_revision": projection.evidence_revision,
        "attribution": attribution_for(projection.evidence_basis),
        "supersedes_proposal_id": projection.supersedes_proposal_id,
        "snooze_until": _dt_to_json(projection.snooze_until),
        "expires_at": _dt_to_json(projection.expires_at),
        "task_id": projection.task_id,
        "plan_root_goal_id": projection.plan_root_goal_id,
        "pursuit_started_at": _dt_to_json(projection.pursuit_started_at),
        "completed_at": _dt_to_json(projection.completed_at),
        "rejection_reason": projection.rejection_reason,
        "revision": projection.revision,
        "source_revision": source_revision,
    }
