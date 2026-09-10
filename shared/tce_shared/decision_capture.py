"""Pure, deterministic decision-capture primitives shared by Full, Lite and the worker.

No I/O, no pydantic. Everything here is driven by exact text rules so that an
extraction can be replayed and audited: candidates keep the exact supporting span,
alternatives are only ever those present in the text or on the matched
opportunity, and rationale is never synthesized.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from tce_shared.redaction import redact_text
from tce_shared.takeover import normalize_text

HOST_CAPTURE_CAPABILITY = "host_capture"
HUMAN_INPUT_TASK_TYPE = "human_input"
HOST_CAPTURE_SOURCE = "tce-host-capture"
EXTRACTION_VERSION = "dc-v1"
CAPTURE_SCHEMA_VERSION = "v1"
DEFAULT_CAPTURE_MAX_CHARS = 2000
TRUSTED_ORIGINS = frozenset({"human_input"})


class OriginKind(StrEnum):
    HUMAN_INPUT = "human_input"
    MANAGER_INSTRUCTION = "manager_instruction"
    EXECUTOR_OUTPUT = "executor_output"
    IMPORTED_TRANSCRIPT = "imported_transcript"
    TOOL_RESULT = "tool_result"


class CandidateKind(StrEnum):
    ANSWER = "answer"
    PREFERENCE = "preference"
    CORRECTION = "correction"
    REJECTION = "rejection"
    FREE_TEXT = "free_text"
    ACKNOWLEDGEMENT = "acknowledgement"
    INTERRUPT = "interrupt"
    NOISE = "noise"


class Promotion(StrEnum):
    PROMOTE = "promote"
    PENDING_REVIEW = "pending_review"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class CaptureScope:
    workspace_id: str
    owner_id: str
    subject_user_id: str
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class InputReceipt:
    delivery_key: str
    content_sha256: str
    content: str
    original_char_count: int
    content_truncated: bool
    redaction_applied: tuple[str, ...]
    origin_kind: OriginKind
    observed_at: datetime
    scope: CaptureScope
    host_session_id: str
    prompt_id: str | None = None
    sequence: int | None = None
    schema_version: str = CAPTURE_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DecisionOpportunity:
    opportunity_id: str
    decision_family: str
    situation_type: str
    question_text: str
    alternatives: tuple[str, ...]
    status: str
    created_at: datetime
    frozen_at: datetime
    expires_at: datetime | None
    session_id: str
    objective_hash: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    evidence_revision: str | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    kind: CandidateKind
    supporting_span: str
    span_sha256: str
    observed_alternatives: tuple[str, ...]
    selected_option: str | None
    stated_rationale: str | None
    opportunity_id: str | None
    origin_kind: OriginKind
    is_negated: bool = False
    is_correction: bool = False
    contradicted: bool = False
    project_id: str | None = None
    task_id: str | None = None
    extraction_version: str = EXTRACTION_VERSION


@dataclass(frozen=True, slots=True)
class HumanResolution:
    opportunity_id: str
    selected_choice: str
    resolution_source: str
    human_source_ref: str
    resolved_at: datetime
    correction_text: str = ""
    stated_rationale: str | None = None
    supersedes_resolution_id: str | None = None


# --------------------------------------------------------------------------- vocab

ACK_TOKENS: frozenset[str] = frozenset(
    {
        "ok", "okay", "k", "kk", "hmm", "sure", "continue", "go", "on", "next", "yes", "yeah", "yep", "y",
        "no", "nope", "n", "thanks", "thank", "you", "done", "got", "it", "fine", "alright", "right", "cool",
        "proceed", "ahead", "confirm", "abort", "approve", "deny",
    }
)
_ACK_PHRASES: tuple[tuple[str, ...], ...] = (("go", "on"), ("thank", "you"), ("got", "it"), ("go", "ahead"))
_ACK_SINGLE: frozenset[str] = frozenset(
    {
        "ok", "okay", "k", "kk", "hmm", "sure", "continue", "next", "yes", "yeah", "yep", "y", "no", "nope", "n",
        "thanks", "done", "fine", "alright", "right", "cool", "proceed", "confirm", "abort", "approve", "deny",
    }
)
_AFFIRMATIVE_ACKS: frozenset[str] = frozenset({"yes", "yeah", "yep", "y", "ok", "okay", "k", "kk", "sure", "proceed", "go ahead", "confirm", "approve", "fine", "alright", "right", "cool"})
_NEGATIVE_ACKS: frozenset[str] = frozenset({"no", "nope", "n", "abort", "deny", "cancel", "stop"})
_CONFIRM_LIKE: frozenset[str] = frozenset({"confirm", "yes", "approve", "allow", "proceed", "continue"})
_DENY_LIKE: frozenset[str] = frozenset({"abort", "no", "deny", "block", "cancel", "stop"})

NEGATION_TOKENS: frozenset[str] = frozenset(
    {"not", "don't", "dont", "never", "no", "cannot", "can't", "cant", "won't", "wont", "without", "isn't", "isnt", "shouldn't", "shouldnt", "rather than", "instead of"}
)
_NEGATION_BIGRAMS: frozenset[tuple[str, str]] = frozenset({("rather", "than"), ("instead", "of")})
_NEGATION_WINDOW = 3

_INTERRUPT_MARKERS: tuple[str, ...] = ("never mind", "nevermind", "hold on", "hang on", "scratch that", "wait", "stop", "cancel")
_CORRECTION_RE = re.compile(r"\b(?:actually|instead|i meant|correction|change that|scratch that)\b|\bnot\s+(.+?),\s*(.+)", re.IGNORECASE)
_RATIONALE_RE = re.compile(r"(?:\b(?:because|since|so that|to avoid|as it)\b|reason:)\s*(.+)", re.IGNORECASE | re.DOTALL)
_RATIONALE_CUT_RE = re.compile(r"(?:\b(?:because|since|so that|to avoid|as it)\b|reason:)", re.IGNORECASE)

_OBJECTIVE_VERBS: tuple[str, ...] = (
    "fix ",
    "build ",
    "decide ",
    "define ",
    "implement ",
    "add ",
    "create ",
    "update ",
    "refactor ",
    "debug ",
    "investigate ",
    "research ",
    "design ",
    "write ",
    "review ",
    "test ",
)
_OBJECTIVE_MARKERS: tuple[str, ...] = ("new objective", "next objective", "objective:", "goal:")

_AGENT_OUTPUT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(assistant|claude|codex|advisor|beru|igris|kurama)\s*:",
        r"^\[TCE-",
        r"^AUTONOMOUS MODE",
        r"^Safety pause:",
        r"^(has_directive|next_step|final_response|safety_decision)\b",
        r"^Type '",
        r"^\s*[\{\[]",
        r"^Traceback \(most recent call last\)",
        r"^\$ ",
        r"^>>> ",
        r"^(error|warning|info|debug)\s*:",
        r"^\s{4,}\S",
        r"^---+$",
        r"^\|.*\|$",
    )
)
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_LONG_QUOTE_RE = re.compile(r'"([^"\n]{61,})"')
_SENTENCE_SPLIT_RE = re.compile(r"[.?!\n]+")
_TOKEN_RE = re.compile(r"[a-z0-9']+|[,;:]")
_CLAUSE_BREAKS: frozenset[str] = frozenset({",", ";", ":"})

_PREFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bprefer\s+(?P<x>.+?)\s+over\s+(?P<y>.+)$", re.IGNORECASE),
    re.compile(r"\bi'?d rather\s+(?P<x>.+?)(?:\s+than\s+(?P<y>.+))?$", re.IGNORECASE),
    re.compile(r"\buse\s+(?P<x>.+?)\s+instead of\s+(?P<y>.+)$", re.IGNORECASE),
    re.compile(r"\b(?:let'?s go with|go with|stick with)\s+(?P<x>.+)$", re.IGNORECASE),
    re.compile(r"\balways\s+(?P<x>.+)$", re.IGNORECASE),
    re.compile(r"\bnever\s+(?P<x>.+)$", re.IGNORECASE),
)


# --------------------------------------------------------------------------- receipts


def compute_delivery_key(host_session_id: str, prompt_ref: str, content_sha256: str) -> str:
    return hashlib.sha256(f"{host_session_id}|{prompt_ref}|{content_sha256}".encode()).hexdigest()


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _strip_control_chars(value: str) -> str:
    return "".join(ch for ch in value if ch in "\n\r\t" or ord(ch) >= 0x20)


def make_receipt(
    raw_text: str,
    scope: CaptureScope,
    origin: OriginKind,
    observed_at: datetime,
    *,
    host_session_id: str,
    prompt_id: str | None = None,
    sequence: int | None = None,
    max_chars: int = DEFAULT_CAPTURE_MAX_CHARS,
    redaction_hints: Iterable[str] | None = None,
) -> InputReceipt:
    content_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    redacted, applied = redact_text(raw_text, redaction_hints)
    redacted = _strip_control_chars(redacted)
    prompt_ref = prompt_id or str(sequence or 0)
    return InputReceipt(
        delivery_key=compute_delivery_key(host_session_id, prompt_ref, content_sha256),
        content_sha256=content_sha256,
        content=redacted[:max_chars],
        original_char_count=len(raw_text),
        content_truncated=len(redacted) > max_chars,
        redaction_applied=tuple(applied),
        origin_kind=origin,
        observed_at=_ensure_utc(observed_at),
        scope=scope,
        host_session_id=host_session_id,
        prompt_id=prompt_id,
        sequence=sequence,
    )


# --------------------------------------------------------------------------- text pre-processing


def _is_marker_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _AGENT_OUTPUT_MARKERS)


def _echo_pattern(question_text: str) -> re.Pattern[str] | None:
    words = question_text.split()
    if not words:
        return None
    return re.compile(r"\s*".join(re.escape(word) for word in words).replace(r"\ ", r"\s+"), re.IGNORECASE)


def analysable_text(message: str, open_opportunities: Sequence[DecisionOpportunity]) -> tuple[str, list[str]]:
    """Strip quoted / pasted / echoed material and return (kept_text, dropped_reasons)."""
    dropped: list[str] = []
    text = message
    if _FENCE_RE.search(text):
        text = _FENCE_RE.sub("", text)
        dropped.append("fenced_code")

    lines = text.split("\n")
    kept_lines: list[str] = []
    for line in lines:
        if line.lstrip().startswith(">"):
            dropped.append("quoted_line")
            continue
        kept_lines.append(line)

    non_blank = [line for line in kept_lines if line.strip()]
    if len(non_blank) == 1 and _is_marker_line(non_blank[0]):
        kept_lines = []
        dropped.append("pasted_agent_output")
    else:
        marker_flags = [bool(line.strip()) and _is_marker_line(line) for line in kept_lines]
        remove = [False] * len(kept_lines)
        idx = 0
        while idx < len(kept_lines):
            if marker_flags[idx]:
                end = idx
                while end < len(kept_lines) and marker_flags[end]:
                    end += 1
                if end - idx >= 2:
                    for pos in range(idx, end):
                        remove[pos] = True
                    dropped.append("pasted_agent_output")
                idx = end
            else:
                idx += 1
        kept_lines = [line for line, flag in zip(kept_lines, remove, strict=True) if not flag]

    text = "\n".join(kept_lines)
    for opportunity in open_opportunities:
        pattern = _echo_pattern(opportunity.question_text)
        if pattern is not None and pattern.search(text):
            text = pattern.sub("", text)
            dropped.append("echoed_question")

    if _LONG_QUOTE_RE.search(text):
        text = _LONG_QUOTE_RE.sub("", text)
        dropped.append("long_quote")

    kept = "\n".join(line for line in text.split("\n") if line.strip()).strip()
    return kept, dropped


# --------------------------------------------------------------------------- helpers


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold().replace("’", "'"))


def _span_indices(tokens: list[str], phrase: list[str]) -> list[int]:
    if not phrase:
        return []
    span = len(phrase)
    return [idx for idx in range(len(tokens) - span + 1) if tokens[idx : idx + span] == phrase]


def _negated_at(tokens: list[str], idx: int) -> bool:
    """A negation token within the last three tokens, not crossing a clause break (, ; :)."""
    window: list[str] = []
    pos = idx - 1
    while pos >= 0 and len(window) < _NEGATION_WINDOW and tokens[pos] not in _CLAUSE_BREAKS:
        window.insert(0, tokens[pos])
        pos -= 1
    if any(token in NEGATION_TOKENS for token in window):
        return True
    return any((window[pos], window[pos + 1]) in _NEGATION_BIGRAMS for pos in range(len(window) - 1))


def match_alternatives(text: str, alternatives: Sequence[str]) -> list[tuple[str, bool]]:
    """Whole-word/phrase casefold matches, in alternatives order; negated only if every occurrence is negated."""
    tokens = _tokens(text)
    matches: list[tuple[str, bool]] = []
    for alternative in alternatives:
        phrase = _tokens(alternative)
        indices = _span_indices(tokens, phrase)
        if not indices:
            continue
        negated = all(_negated_at(tokens, idx) for idx in indices)
        matches.append((alternative, negated))
    return matches


def _ack_polarity(ack: str) -> bool | None:
    normalized = normalize_text(ack)
    tokens = normalized.split()
    padded = f" {normalized} "
    affirmative = any(token in _AFFIRMATIVE_ACKS for token in tokens) or " go ahead " in padded
    negative = any(token in _NEGATIVE_ACKS for token in tokens)
    if negative and not affirmative:
        return False
    if affirmative and not negative:
        return True
    return None


def map_ack_to_alternative(ack: str, alternatives: Sequence[str]) -> str | None:
    if not alternatives:
        return None
    normalized = normalize_text(ack)
    for alternative in alternatives:
        if normalize_text(alternative) == normalized:
            return alternative
    polarity = _ack_polarity(ack)
    if polarity is None:
        return None
    folded = [normalize_text(alternative) for alternative in alternatives]
    if polarity:
        for alternative, key in zip(alternatives, folded, strict=True):
            if key in _CONFIRM_LIKE:
                return alternative
        if len(alternatives) == 2 and folded[1] in _DENY_LIKE:
            return alternatives[0]
        return None
    for alternative, key in zip(alternatives, folded, strict=True):
        if key in _DENY_LIKE:
            return alternative
    if len(alternatives) == 2 and folded[0] in _CONFIRM_LIKE:
        return alternatives[1]
    return None


def is_concrete_instruction(text: str) -> bool:
    normalized = normalize_text(text)
    if len(normalized.split()) < 4:
        return False
    if any(marker in normalized for marker in _OBJECTIVE_MARKERS):
        return True
    return normalized.startswith(_OBJECTIVE_VERBS)


def is_retrospective(prediction_at: datetime | None, answer_at: datetime | None) -> bool:
    if prediction_at is None or answer_at is None:
        return True
    return _ensure_utc(prediction_at) >= _ensure_utc(answer_at)


def resolve_prediction_fields(predicted_choice: str | None, abstained: bool, actual_choice: str) -> dict[str, bool | None]:
    if abstained or not predicted_choice:
        return {"correct": None}
    return {"correct": predicted_choice.casefold().strip() == actual_choice.casefold().strip()}


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _ensure_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def evidence_revision(evidence_rows: Sequence[Mapping[str, Any]]) -> tuple[str, datetime | None]:
    if not evidence_rows:
        return "empty", None
    ids = sorted(str(row.get("id", "")) for row in evidence_rows)
    stamps = [ts for ts in (_parse_ts(row.get("ts")) for row in evidence_rows) if ts is not None]
    max_ts = max(stamps) if stamps else None
    digest = hashlib.sha256(f"{','.join(ids)}|{max_ts.isoformat() if max_ts else ''}".encode()).hexdigest()
    return digest[:32], max_ts


def opportunity_expired(o: DecisionOpportunity, now: datetime) -> bool:
    if o.expires_at is None:
        return False
    return _ensure_utc(o.expires_at) <= _ensure_utc(now)


# --------------------------------------------------------------------------- extraction internals


def _is_ack_only(normalized: str) -> bool:
    tokens = normalized.split()
    if not tokens or len(tokens) > 3:
        return False
    idx = 0
    while idx < len(tokens):
        if idx + 1 < len(tokens) and (tokens[idx], tokens[idx + 1]) in _ACK_PHRASES:
            idx += 2
            continue
        if tokens[idx] in _ACK_SINGLE:
            idx += 1
            continue
        return False
    return True


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def _sentence_for(text: str, alternatives: Sequence[str]) -> str:
    for sentence in _sentences(text):
        if any(match_alternatives(sentence, [alternative]) for alternative in alternatives):
            return sentence
    return text[:500]


def _rationale(sentence: str) -> str | None:
    found = _RATIONALE_RE.search(sentence)
    if found is None:
        return None
    rationale = found.group(1).strip()
    return rationale[:500] or None


def _strip_rationale(sentence: str) -> str:
    found = _RATIONALE_CUT_RE.search(sentence)
    return sentence[: found.start()].strip() if found else sentence


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _origin_of(message: Mapping[str, Any]) -> OriginKind | None:
    raw = message.get("origin_kind")
    if isinstance(raw, OriginKind):
        return raw
    try:
        return OriginKind(str(raw))
    except ValueError:
        return None


def _candidate(
    kind: CandidateKind,
    span: str,
    *,
    origin: OriginKind,
    message: Mapping[str, Any],
    observed_alternatives: tuple[str, ...] = (),
    selected_option: str | None = None,
    opportunity_id: str | None = None,
    is_negated: bool = False,
    is_correction: bool = False,
    contradicted: bool = False,
    extraction_version: str = EXTRACTION_VERSION,
) -> DecisionCandidate:
    span = span[:500] if len(span) > 500 else span
    return DecisionCandidate(
        kind=kind,
        supporting_span=span,
        span_sha256=_sha(span),
        observed_alternatives=observed_alternatives,
        selected_option=selected_option,
        stated_rationale=_rationale(span),
        opportunity_id=opportunity_id,
        origin_kind=origin,
        is_negated=is_negated,
        is_correction=is_correction,
        contradicted=contradicted,
        project_id=message.get("project_id"),
        task_id=message.get("task_id"),
        extraction_version=extraction_version,
    )


def _starts_with_interrupt(normalized: str) -> bool:
    return any(normalized == marker or normalized.startswith(marker + " ") for marker in _INTERRUPT_MARKERS)


def _clean_term(value: str) -> str:
    return value.strip().strip(",;:").strip()


# --------------------------------------------------------------------------- extraction


def extract_candidates(
    message: Mapping[str, Any],
    context: Mapping[str, Any],
    open_opportunities: Sequence[DecisionOpportunity],
    extraction_version: str = EXTRACTION_VERSION,
) -> list[DecisionCandidate]:
    origin = _origin_of(message)
    # R0: non-human origins are context only.
    if origin is None or origin in {OriginKind.EXECUTOR_OUTPUT, OriginKind.TOOL_RESULT, OriginKind.MANAGER_INSTRUCTION}:
        return []
    content = str(message.get("content") or "")
    # R1
    if not content.strip():
        return []
    open_opps = [o for o in open_opportunities if o.status == "open"]
    kept, dropped = analysable_text(content, open_opps)

    def build(kind: CandidateKind, span: str, **kwargs: Any) -> DecisionCandidate:
        return _candidate(kind, span, origin=origin, message=message, extraction_version=extraction_version, **kwargs)

    # R2
    if not kept:
        return [build(CandidateKind.NOISE, content[:500])] if dropped else []
    normalized = normalize_text(kept)
    tokens = normalized.split()

    # R3: acknowledgement path.
    if _is_ack_only(normalized):
        with_alternatives = [o for o in open_opps if o.alternatives]
        if len(open_opps) == 1 and len(with_alternatives) == 1:
            target = with_alternatives[0]
            selected = map_ack_to_alternative(kept, target.alternatives)
            if selected is not None:
                return [build(CandidateKind.ANSWER, kept, observed_alternatives=target.alternatives, selected_option=selected, opportunity_id=target.opportunity_id)]
        return [build(CandidateKind.ACKNOWLEDGEMENT, kept)]

    # R4: interrupt.
    if _starts_with_interrupt(normalized) and len(tokens) <= 5:
        return [build(CandidateKind.INTERRUPT, kept)]

    # R5: correction against a recently resolved opportunity.
    recently_resolved_raw = context.get("recently_resolved") or ()
    recently_resolved: list[DecisionOpportunity] = [o for o in recently_resolved_raw if isinstance(o, DecisionOpportunity)]
    # A correction keyword never outranks a live question: if the affirmed term answers an OPEN opportunity,
    # it is an answer to that (R6), not a revision of an already-resolved one.
    answers_open_question = any(
        any(not negated for _alternative, negated in match_alternatives(kept, opportunity.alternatives)) for opportunity in open_opps if opportunity.alternatives
    )
    if recently_resolved and not answers_open_question and _CORRECTION_RE.search(kept):
        hits: list[tuple[DecisionOpportunity, str | None]] = []
        for resolved in recently_resolved:
            if not resolved.alternatives:
                # Alternatives-less (open-ended) resolutions only count when the text is itself a concrete instruction (the R7 bar).
                if is_concrete_instruction(kept):
                    hits.append((resolved, None))
                continue
            affirmed_alts = [alternative for alternative, negated in match_alternatives(kept, resolved.alternatives) if not negated]
            if affirmed_alts:
                hits.append((resolved, affirmed_alts[0]))
        if len(hits) == 1:
            resolved, matched = hits[0]
            span = _sentence_for(kept, [matched]) if matched else kept
            return [
                build(
                    CandidateKind.CORRECTION,
                    span,
                    observed_alternatives=resolved.alternatives,
                    selected_option=matched if matched is not None else kept[:500],
                    opportunity_id=resolved.opportunity_id,
                    is_correction=True,
                )
            ]
        if len(hits) >= 2:
            return [build(CandidateKind.CORRECTION, kept, is_correction=True, contradicted=True)]

    # R6: match alternatives per open opportunity.
    results: list[DecisionCandidate] = []
    for opportunity in open_opps:
        if not opportunity.alternatives:
            continue
        matches = match_alternatives(kept, opportunity.alternatives)
        if not matches:
            continue
        affirmed = [alternative for alternative, negated in matches if not negated]
        negated_alts = [alternative for alternative, negated in matches if negated]
        oid = opportunity.opportunity_id
        alts = opportunity.alternatives
        if len(affirmed) == 1:
            results.append(build(CandidateKind.ANSWER, _sentence_for(kept, affirmed), observed_alternatives=alts, selected_option=affirmed[0], opportunity_id=oid))
        elif len(affirmed) >= 2:
            results.append(build(CandidateKind.ANSWER, kept, observed_alternatives=alts, selected_option=None, opportunity_id=oid, contradicted=True))
        elif len(negated_alts) == 1 and len(alts) == 2:
            other = next(alternative for alternative in alts if alternative != negated_alts[0])
            results.append(build(CandidateKind.ANSWER, _sentence_for(kept, negated_alts), observed_alternatives=alts, selected_option=other, opportunity_id=oid, is_negated=True))
        else:
            results.append(build(CandidateKind.REJECTION, _sentence_for(kept, negated_alts), observed_alternatives=alts, selected_option=None, opportunity_id=oid, is_negated=True))
    if results:
        return results

    # R7: concrete instruction answering an open-ended question.
    open_ended = [o for o in open_opps if not o.alternatives]
    if open_ended and is_concrete_instruction(kept):
        return [build(CandidateKind.ANSWER, kept, observed_alternatives=(), selected_option=kept[:500], opportunity_id=o.opportunity_id) for o in open_ended]

    # R8: substantive free text while a question is open.
    if open_opps:
        if len(tokens) > 3:
            return [build(CandidateKind.FREE_TEXT, kept)]
        return []

    # R9: standalone preference.
    for sentence in _sentences(kept):
        core = _strip_rationale(sentence)
        for pattern in _PREFERENCE_PATTERNS:
            found = pattern.search(core)
            if found is None:
                continue
            selected = _clean_term(found.group("x"))
            other = _clean_term(found.group("y")) if "y" in found.groupdict() and found.group("y") else ""
            if not selected:
                continue
            observed = (selected, other) if other else (selected,)
            return [build(CandidateKind.PREFERENCE, sentence, observed_alternatives=observed, selected_option=selected)]
    # R10
    return []


# --------------------------------------------------------------------------- promotion


def _could_match(candidate: DecisionCandidate, opportunity: DecisionOpportunity) -> bool:
    if opportunity.alternatives:
        return candidate.selected_option in opportunity.alternatives
    return opportunity.alternatives == ()


ANSWER_PRECEDES_QUESTION = "answer_precedes_question"


def promotion_decision_detail(
    candidate: DecisionCandidate,
    open_opportunities: Sequence[DecisionOpportunity],
    *,
    now: datetime | None = None,
    expiry_now: datetime | None = None,
) -> tuple[Promotion, str]:
    """Promotion plus the audit reason. `now` is the moment the human answered (receipt.observed_at), not wall-clock.

    Source of truth for both backends: an ANSWER only promotes to a unique, open, unexpired opportunity that was
    frozen at or before the answer moment (a prompt captured before its question existed cannot be its answer).

    The two moments are deliberately different. `now` answers "did this question exist when the human spoke?" and
    is host-supplied. `expiry_now` answers "was the question still live when the answer reached us?" and is server
    time (receipt.ingested_at). Expiry is judged at `max(now, expiry_now)` so a host clock running behind cannot
    rewind a stale answer back inside an expired question's window; the cost is that an answer spooled past the
    TTL is held for review instead of promoted, which is the safe direction.
    """

    if candidate.contradicted:
        return Promotion.DISCARD, "contradictory"
    if candidate.kind in {CandidateKind.ACKNOWLEDGEMENT, CandidateKind.INTERRUPT, CandidateKind.NOISE}:
        return Promotion.DISCARD, candidate.kind.value
    if candidate.origin_kind is OriginKind.IMPORTED_TRANSCRIPT:
        return Promotion.PENDING_REVIEW, "imported_transcript"
    if candidate.kind in {CandidateKind.PREFERENCE, CandidateKind.REJECTION, CandidateKind.FREE_TEXT}:
        return Promotion.PENDING_REVIEW, candidate.kind.value
    moment = _ensure_utc(now) if now is not None else datetime.now(UTC)
    expiry_moment = max(moment, _ensure_utc(expiry_now)) if expiry_now is not None else moment
    if candidate.kind is CandidateKind.ANSWER and candidate.opportunity_id is not None and candidate.selected_option:
        target = next((o for o in open_opportunities if o.opportunity_id == candidate.opportunity_id), None)
        if (
            target is not None
            and target.status == "open"
            and not opportunity_expired(target, expiry_moment)
            and (not target.alternatives or candidate.selected_option in target.alternatives)
            and sum(1 for o in open_opportunities if o.status == "open" and _could_match(candidate, o)) == 1
        ):
            if _ensure_utc(target.frozen_at) > moment:
                return Promotion.PENDING_REVIEW, ANSWER_PRECEDES_QUESTION
            return Promotion.PROMOTE, "unique_open_opportunity"
        return Promotion.PENDING_REVIEW, "ambiguous_or_expired_target"
    if candidate.kind is CandidateKind.CORRECTION and candidate.opportunity_id is not None:
        # Only a correction that names one of the resolved question's own alternatives is an auditable revision;
        # free-text "corrections" of open-ended resolutions are held for review.
        if candidate.selected_option and candidate.selected_option in candidate.observed_alternatives:
            return Promotion.PROMOTE, "correction"
        return Promotion.PENDING_REVIEW, "correction_without_alternative_match"
    if candidate.kind is CandidateKind.CORRECTION:
        return Promotion.PENDING_REVIEW, "correction_without_unique_target"
    return Promotion.PENDING_REVIEW, candidate.kind.value


def promotion_decision(
    candidate: DecisionCandidate,
    open_opportunities: Sequence[DecisionOpportunity],
    *,
    now: datetime | None = None,
    expiry_now: datetime | None = None,
) -> Promotion:
    return promotion_decision_detail(candidate, open_opportunities, now=now, expiry_now=expiry_now)[0]


__all__ = [
    "ACK_TOKENS",
    "ANSWER_PRECEDES_QUESTION",
    "CAPTURE_SCHEMA_VERSION",
    "DEFAULT_CAPTURE_MAX_CHARS",
    "EXTRACTION_VERSION",
    "HOST_CAPTURE_CAPABILITY",
    "HOST_CAPTURE_SOURCE",
    "HUMAN_INPUT_TASK_TYPE",
    "NEGATION_TOKENS",
    "TRUSTED_ORIGINS",
    "CandidateKind",
    "CaptureScope",
    "DecisionCandidate",
    "DecisionOpportunity",
    "HumanResolution",
    "InputReceipt",
    "OriginKind",
    "Promotion",
    "analysable_text",
    "compute_delivery_key",
    "evidence_revision",
    "extract_candidates",
    "is_concrete_instruction",
    "is_retrospective",
    "make_receipt",
    "map_ack_to_alternative",
    "match_alternatives",
    "opportunity_expired",
    "promotion_decision",
    "promotion_decision_detail",
    "resolve_prediction_fields",
]
