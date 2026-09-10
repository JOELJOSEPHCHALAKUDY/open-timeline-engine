"""Proposal generation: what this person keeps returning to, in words he is receipted as having typed.

This job **proposes and nothing else**. It writes no goal row, sets no objective, enqueues no
plan and appends no task-state event. The owner decides what gets pursued, by rendering a
verdict through a credential this process structurally cannot hold.

Three properties are structural rather than advisory, and they are the whole point:

*The corpus cannot be written by an executor.* Candidates come from
``dream_store.select_candidate_messages``, whose only admission is a ``trusted_input_receipts``
row bound to the behaviour subject. The event corpus itself is executor-writable — an executor
holding an ordinary API token can POST a row with ``task_type='human_input_backfill'`` — so
quoting from it would let an executor write a message and have the system quote it back to the
owner as something he said. Measured on this stack: the clause this job used to carry admitted
4,649 rows in workspace ``personal``, every one of them stamped ``_tce_owner='codex-executor'``;
the receipt-bound clause admits 1.

*The producer cannot see a count.* Its inputs are the planning-job row, the resolved scope and
the message pool. No count of failed directives, stalled goals or unembedded events is
computed, passed or reachable, so "you have 41 unembedded events, consider improving embedding
coverage" is not a thing this code can emit. That is the difference between a rule and a
preference: the arithmetic producer is gone, not discouraged.

*A claim with no verified verbatim quote is not emitted at all.* There is no weak-emission
path, no confidence knob and no "surface it with a low score". ``parse_dream_payload`` resolves
every citation number against the frozen pool, ``validate_candidate`` requires the quote to be a
contiguous span of the message it claims to come from, requires each quote to share a content
token with the title, and re-checks the count after every drop. A candidate that fails any of
them is refused by name into ``dream_generation_runs.refusals_json``.

**On today's corpus this job mints nothing.** Every scope in the live database is below
``dream_min_messages`` once the receipt rule is applied, so the route refuses
``insufficient_messages`` before this job is ever enqueued. That is the correct answer and it
is written down rather than dressed up: the 4,649 backfill rows cannot be quoted back to the
owner as his own words until a host-capture-credentialled import gives them receipts.

Off by default (``takeover_dream_llm_enabled``): a model call nobody asked for is a cost nobody
agreed to.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.db import get_session_factory
from tce_api.dream_store import (
    apply_dream_events,
    finish_generation_run,
    load_generation_run_by_job,
    load_live_proposals,
    load_rejected_proposals,
    mint_proposal,
    select_candidate_messages,
    validate_citations_deep,
)
from tce_api.plan_rows import resolved_scope_from_json
from tce_api.planning_store import claim_planning_job, complete_planning_job
from tce_api.task_state_store import load_task_state
from tce_model_gateway.factory import get_gateway
from tce_shared.aspirations import (
    DREAM_PROMPT_ID,
    DREAM_SETTING_DEFAULTS,
    SCOPE_KIND_PROJECT,
    SCOPE_KIND_WORKSPACE,
    DreamCandidate,
    DreamCitation,
    DreamProposalEvent,
    DreamProposalEventKind,
    DreamProposalProjection,
    DreamProposalStatus,
    PoolMessage,
    citations_to_json,
    dedupe_by_content_sha256,
    evidence_basis_for,
    is_duplicate_theme,
    parse_dream_payload,
    suppressed_by_rejection,
    theme_similarity,
    validate_candidate,
)
from tce_shared.decision_capture import evidence_revision
from tce_shared.scope import ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DREAM,
    PLANNING_LOST_LEASE_REASON,
    PLANNING_PRODUCER_MODEL,
    PLANNING_STALE_REASON,
    TASK_STATE_POLICY_REVISION,
    TaskStateProjection,
    plan_input_revision,
    planning_result_is_stale,
    task_scope_digest,
)

from ..config import get_settings
from .planning import _worker_plan_gateway_settings

LOGGER = logging.getLogger(__name__)

_COUNT_KEYS = ("processed", "succeeded", "failed", "discarded", "cancelled", "skipped")

# The model never sees more than this many characters of pool, whatever the per-message cap
# and the message limit multiply out to.
POOL_RENDER_MAX_CHARS = 24_000


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNT_KEYS, 0)


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


DREAM_PROMPT = """Below are real messages a developer sent to their coding assistant, newest first.

Work out what this person is actually trying to get done. Not what they asked for in any
one message -- what they keep returning to.

Then write it the way THEY would write it. Look at how they type in these messages and
match it. They are one person building alone, at a keyboard, with no team.

Hard rules on wording:
- Write it as they'd say it out loud. Short. Plain. Lower case is fine.
- Name the actual thing. "get the witness engine in front of someone" beats
  "validate market positioning". "stop the CI failing" beats "improve pipeline health".
- Banned words: comprehensive, leverage, stakeholder, framework, roadmap, strategy,
  ecosystem, robust, holistic, real-world, best practice, optimize, streamline.
- No Title Case. No consultant voice. If it reads like a slide, rewrite it.
- It has to be something they could have typed themselves.

Hard rules on content:
- Only name something you can point at specific messages for.
- Ignore interruptions, pasted links, one-word replies and tool output.
- Something said once but clearly counts for more than boilerplate repeated ten times.
- If nothing clear comes through, return an empty list. That is a good answer.
- At most 3.

For each one you name, you must also give:
- "connection": what in these messages makes you say it. Point at what they said.
- "benefit": what is better for them once it is done. One line, concrete.
- "first_step": the smallest thing they could do next. Something they could start today.
- "citations": at least 2 of the numbered messages, and for each one a "quote" COPIED
  WORD FOR WORD from that message. Copy it exactly -- do not tidy it, do not shorten a
  word, do not fix the spelling. A quote that is not in the message is thrown away, and
  a proposal left with fewer than 2 quotes is thrown away with it.
- Every quote must be about the thing in the title. A quote that does not share a word
  with the title is thrown away, and do not quote a sentence that reads like marketing.

Messages:
__MESSAGES__

Reply with JSON only:
{"dreams": [{"title": "the goal, in their words",
             "connection": "what in the messages says so",
             "benefit": "what is better once it is done",
             "first_step": "the smallest next thing",
             "citations": [{"n": 1, "quote": "copied word for word"},
                           {"n": 4, "quote": "copied word for word"}]}]}
"""

DREAM_PROMPT_SHA256 = hashlib.sha256(DREAM_PROMPT.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- settings


def _dream_settings(settings: Any) -> dict[str, Any]:
    """Every setting this job reads, resolved once.

    Spelled out one ``getattr(settings, "<name>", ...)`` per line rather than looped over a
    list of names, for two reasons: a knob nobody can see being read is a knob that quietly
    does nothing, and the fallback is the canonical default beside the vocabulary rather than
    a second copy of the number here.
    """
    return {
        "message_limit": int(getattr(settings, "dream_message_limit", DREAM_SETTING_DEFAULTS["dream_message_limit"])),
        "min_messages": int(getattr(settings, "dream_min_messages", DREAM_SETTING_DEFAULTS["dream_min_messages"])),
        "min_message_chars": int(
            getattr(settings, "dream_min_message_chars", DREAM_SETTING_DEFAULTS["dream_min_message_chars"])
        ),
        "max_message_chars": int(
            getattr(settings, "dream_max_message_chars", DREAM_SETTING_DEFAULTS["dream_max_message_chars"])
        ),
        "max_proposals_per_run": int(
            getattr(settings, "dream_max_proposals_per_run", DREAM_SETTING_DEFAULTS["dream_max_proposals_per_run"])
        ),
        "min_citations": int(getattr(settings, "dream_min_citations", DREAM_SETTING_DEFAULTS["dream_min_citations"])),
        "max_citations": int(getattr(settings, "dream_max_citations", DREAM_SETTING_DEFAULTS["dream_max_citations"])),
        "quote_max_chars": int(
            getattr(settings, "dream_quote_max_chars", DREAM_SETTING_DEFAULTS["dream_quote_max_chars"])
        ),
        "min_quote_overlap_tokens": int(
            getattr(
                settings,
                "dream_min_quote_overlap_tokens",
                DREAM_SETTING_DEFAULTS["dream_min_quote_overlap_tokens"],
            )
        ),
        "min_citation_relevance_tokens": int(
            getattr(
                settings,
                "dream_min_citation_relevance_tokens",
                DREAM_SETTING_DEFAULTS["dream_min_citation_relevance_tokens"],
            )
        ),
        "max_live_proposals": int(
            getattr(settings, "dream_max_live_proposals", DREAM_SETTING_DEFAULTS["dream_max_live_proposals"])
        ),
        "duplicate_similarity": float(
            getattr(settings, "dream_duplicate_similarity", DREAM_SETTING_DEFAULTS["dream_duplicate_similarity"])
        ),
        "material_new_citations": int(
            getattr(settings, "dream_material_new_citations", DREAM_SETTING_DEFAULTS["dream_material_new_citations"])
        ),
        "material_max_similarity": float(
            getattr(settings, "dream_material_max_similarity", DREAM_SETTING_DEFAULTS["dream_material_max_similarity"])
        ),
        "rejected_cooldown_days": int(
            getattr(settings, "dream_rejected_cooldown_days", DREAM_SETTING_DEFAULTS["dream_rejected_cooldown_days"])
        ),
        "rejected_lookback_days": int(
            getattr(settings, "dream_rejected_lookback_days", DREAM_SETTING_DEFAULTS["dream_rejected_lookback_days"])
        ),
        "rejected_scan_limit": int(
            getattr(settings, "dream_rejected_scan_limit", DREAM_SETTING_DEFAULTS["dream_rejected_scan_limit"])
        ),
        "max_reproposals": int(
            getattr(settings, "dream_max_reproposals", DREAM_SETTING_DEFAULTS["dream_max_reproposals"])
        ),
        "proposal_ttl_days": int(
            getattr(settings, "dream_proposal_ttl_days", DREAM_SETTING_DEFAULTS["dream_proposal_ttl_days"])
        ),
        "nonresponse_after_surfaces": int(
            getattr(
                settings,
                "dream_nonresponse_after_surfaces",
                DREAM_SETTING_DEFAULTS["dream_nonresponse_after_surfaces"],
            )
        ),
        "max_sensitivity": int(getattr(settings, "block_sensitivity", 3)) - 1,
    }


def _scope_kind(scope: ResolvedScope) -> str:
    """D9: derived, never asserted.

    The run row carries a ``scope_kind`` the route wrote, but it wrote it from this same
    scope by this same rule. Reading it back would be a second source of truth for one fact.
    """
    if scope.is_bound() and scope.project_id:
        return SCOPE_KIND_PROJECT
    return SCOPE_KIND_WORKSPACE


def _render_pool(pool: Sequence[PoolMessage]) -> str:
    """The only thing the model is shown: numbers and bodies.

    No event id, no receipt id, no hash, no project id, no workspace id and no count. The
    number is 1-based in ``ts DESC`` order, matching the prompt's "newest first", and it is
    the handle every citation is resolved against.
    """
    rendered = "\n\n".join(f"{message.n}. {message.body}" for message in pool)
    return rendered[:POOL_RENDER_MAX_CHARS]


# --------------------------------------------------------------------------- job plumbing


def _reread_cancel_requested(db: Session, job_id: str) -> bool:
    value = db.execute(
        text("SELECT cancel_requested FROM planning_jobs WHERE id = CAST(:job_id AS UUID)"),
        {"job_id": job_id},
    ).scalar_one_or_none()
    return bool(value)


def _current_input_revision(projection: TaskStateProjection, scope: ResolvedScope) -> str:
    return plan_input_revision(
        objective_hash=projection.objective_hash,
        contract_revision=projection.contract_revision,
        policy_revision=TASK_STATE_POLICY_REVISION,
        scope_digest=task_scope_digest(
            workspace_id=scope.workspace_id,
            executor_id=scope.executor_id,
            owner_ids=scope.sql_owner_ids(),
            subject_user_id=scope.subject_user_id,
            project_id=scope.project_id,
            project_binding=scope.project_binding,
        ),
        cancel_epoch=projection.last_cancel_seq,
    )


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value


def _backoff(settings: Any, attempts: int, now: datetime) -> datetime | None:
    delay = min(
        int(getattr(settings, "planning_job_backoff_cap_seconds", 900)),
        5 * (2 ** max(0, attempts - 1)),
    )
    return now + timedelta(seconds=delay)


def _complete(
    db: Session,
    *,
    job_id: str,
    lease_owner: str,
    state: str,
    producer: str | None,
    result: dict[str, Any],
    last_error: str | None,
    next_attempt_at: datetime | None,
    now: datetime,
) -> bool:
    fenced = complete_planning_job(
        db,
        job_id=job_id,
        lease_owner=lease_owner,
        state=state,
        producer=producer,
        result=result,
        last_error=last_error,
        next_attempt_at=next_attempt_at,
        now=now,
    )
    db.commit()
    return fenced


def _finish_run(
    db: Session,
    *,
    run_id: str,
    state: str,
    refusal_reason: str,
    pool: Sequence[PoolMessage],
    pool_drops: Mapping[str, int],
    refusals: Mapping[str, int],
    candidates_returned: int,
    proposals_written: int,
    model_provider: str,
    now: datetime,
) -> None:
    """Every exit writes a real row. A generation path that is broken must never read as 'no dreams today'.

    The failure this guards against is live and was measured: the API swallowed every dream
    exception into a warning while the pursued counter stayed at zero, so a completely broken
    generator and a quiet week were the same observation.
    """
    digest, cutoff = evidence_revision([{"id": message.event_id, "ts": message.observed_at} for message in pool])
    finish_generation_run(
        db,
        run_id=run_id,
        state=state,
        refusal_reason=refusal_reason,
        pool=pool,
        evidence_revision=digest,
        evidence_cutoff_at=cutoff,
        candidates_returned=candidates_returned,
        proposals_written=proposals_written,
        refusals=dict(refusals),
        pool_drops=dict(pool_drops),
        prompt_hash=DREAM_PROMPT_SHA256,
        model_provider=model_provider,
        now=now,
    )


# --------------------------------------------------------------------------- entry point


def run(job_id: str | None = None, batch_size: int | None = None) -> dict[str, Any]:
    """Generate proposals for one job (``job_id``) or a batch. Never raises.

    Identical signature and lifecycle to ``planning.run``; the differences are the job kind,
    the prompt, and that it writes proposal rows rather than plan step rows.
    """
    settings = get_settings()
    counts = _zero_counts()
    if not bool(getattr(settings, "planning_enabled", True)):
        return {"status": "disabled", **counts}
    if not bool(getattr(settings, "takeover_dream_llm_enabled", False)):
        return {"status": "disabled", **counts}

    now = datetime.now(tz=UTC)
    lease_owner = _lease_owner()
    session_factory = get_session_factory()

    try:
        with session_factory() as db:
            rows = claim_planning_job(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                lease_seconds=int(getattr(settings, "planning_job_lease_seconds", 120)),
                now=now,
                batch_size=int(batch_size or 0) or int(getattr(settings, "planning_job_batch_size", 20) or 20),
            )
    except Exception:
        LOGGER.warning("dream synthesis claim failed", exc_info=True)
        return {"status": "ok", "reason": "claim_failed", **counts}

    if not rows:
        return {"status": "skipped", **counts}

    last_status = "ok"
    last_reason = ""
    for row in rows:
        if str(row.get("job_kind") or "") != PLANNING_JOB_KIND_DREAM:
            counts["skipped"] += 1
            continue
        try:
            outcome = _process_job(dict(row), settings=settings, lease_owner=lease_owner)
        except Exception:
            LOGGER.warning("dream synthesis crashed for %s", row.get("id"), exc_info=True)
            outcome = {"status": "failed"}
        last_status = str(outcome.get("status") or "ok")
        last_reason = str(outcome.get("reason") or "")
        counts["processed"] += 1
        if last_status in counts:
            counts[last_status] += 1

    if job_id:
        # The single-job form is what tests and the probe call: they need to know WHY, not
        # just that something was discarded.
        result: dict[str, Any] = {"status": last_status, **counts}
        if last_reason:
            result["reason"] = last_reason
        return result
    return {"status": "ok", **counts}


def _process_job(row: dict[str, Any], *, settings: Any, lease_owner: str) -> dict[str, Any]:
    """W1-W14. The route decided; this calls the model and writes what survives the devices."""
    job_id = str(row["id"])
    now = datetime.now(tz=UTC)
    session_factory = get_session_factory()
    knobs = _dream_settings(settings)
    provider = str(getattr(_worker_plan_gateway_settings(settings), "model_provider", "") or "")

    with session_factory() as db:
        # W2. The route created the run row before enqueuing this job. Without one there is
        # nothing to finish, and minting against no run would leave proposals with no
        # provenance — so this is a release, not a failure.
        run_row = load_generation_run_by_job(db, planning_job_id=job_id)
        if run_row is None or str(run_row.get("state") or "") != "running":
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="discarded",
                producer=None,
                result={"reason": "no_generation_run"},
                last_error="no_generation_run",
                next_attempt_at=None,
                now=now,
            )
            return {"status": "discarded", "reason": "no_generation_run"}
        run_id = str(run_row.get("id") or "")

        loaded = load_task_state(
            db,
            workspace_id=str(row["workspace_id"]),
            owner_id=str(row["owner_id"]),
            task_id=str(row["task_id"]),
        )
        if loaded is None:
            _finish_run(
                db,
                run_id=run_id,
                state="failed",
                refusal_reason="stale_input_revision",
                pool=(),
                pool_drops={},
                refusals={},
                candidates_returned=0,
                proposals_written=0,
                model_provider=provider,
                now=now,
            )
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="discarded",
                producer=None,
                result={"reason": "task_state_missing"},
                last_error="task_state_missing",
                next_attempt_at=None,
                now=now,
            )
            return {"status": "discarded", "reason": "task_state_missing"}

        projection, _highest_seq, _source_revision = loaded
        scope = resolved_scope_from_json(_json_field(row.get("scope_json")) or {})
        scope_kind = _scope_kind(scope)

        # W4. P2's staleness discipline, verbatim.
        stale, reason = planning_result_is_stale(
            job_input_revision=str(row.get("input_revision") or ""),
            job_contract_revision=int(row.get("contract_revision") or 0),
            current_input_revision=_current_input_revision(projection, scope),
            current_contract_revision=projection.contract_revision,
            cancel_requested=bool(row.get("cancel_requested")),
        )
        if stale:
            state = "cancelled" if reason == "cancelled" else "discarded"
            _finish_run(
                db,
                run_id=run_id,
                state="failed",
                refusal_reason="stale_input_revision",
                pool=(),
                pool_drops={},
                refusals={},
                candidates_returned=0,
                proposals_written=0,
                model_provider=provider,
                now=now,
            )
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state=state,
                producer=None,
                result={"reason": reason},
                last_error=None if state == "cancelled" else PLANNING_STALE_REASON,
                next_attempt_at=None,
                now=now,
            )
            return {"status": state, "reason": reason}

        # Second line of defence behind the route's own gate: a job enqueued before the flag
        # was turned off must still finish its run rather than leaving it 'running' forever.
        if not bool(getattr(settings, "takeover_dream_llm_enabled", False)):
            _finish_run(
                db,
                run_id=run_id,
                state="refused",
                refusal_reason="model_disabled",
                pool=(),
                pool_drops={},
                refusals={},
                candidates_returned=0,
                proposals_written=0,
                model_provider=provider,
                now=now,
            )
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="succeeded",
                producer=None,
                result={"proposals": 0, "reason": "model_disabled"},
                last_error=None,
                next_attempt_at=None,
                now=now,
            )
            return {"status": "succeeded", "reason": "model_disabled"}

        # W5. Re-read the pool. The route's copy is not carried: between the route and here a
        # message may have been redacted, re-scoped or had its receipt withdrawn, and the pool
        # the model is shown must be the pool the citations are resolved against.
        pool, pool_drops = select_candidate_messages(
            db,
            scope=scope,
            scope_kind=scope_kind,
            limit=knobs["message_limit"],
            min_chars=knobs["min_message_chars"],
            max_chars=knobs["max_message_chars"],
            max_sensitivity=knobs["max_sensitivity"],
        )
        if len(pool) < knobs["min_messages"]:
            # Not enough of the person's own writing to say anything honest about it. On
            # today's corpus this is the answer for every scope, and it is the right one.
            _finish_run(
                db,
                run_id=run_id,
                state="refused",
                refusal_reason="insufficient_messages",
                pool=pool,
                pool_drops=pool_drops,
                refusals={},
                candidates_returned=0,
                proposals_written=0,
                model_provider=provider,
                now=now,
            )
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="succeeded",
                producer=None,
                result={"proposals": 0, "reason": "insufficient_messages", "pool_size": len(pool)},
                last_error=None,
                next_attempt_at=None,
                now=now,
            )
            return {"status": "succeeded", "reason": "insufficient_messages"}

        # W6/W7. The one model call. No fallback: a fabricated proposal is worse than none.
        try:
            gateway = get_gateway(_worker_plan_gateway_settings(settings))
            # `.replace`, not `.format`: the prompt contains a literal JSON example, so
            # str.format raises KeyError on it.
            payload = gateway.extract_structured(
                DREAM_PROMPT.replace("__MESSAGES__", _render_pool(pool)), DREAM_PROMPT_ID
            )
        except Exception as exc:
            attempts = int(row.get("attempts") or 0)
            max_attempts = int(row.get("max_attempts") or 3)
            _finish_run(
                db,
                run_id=run_id,
                state="failed",
                refusal_reason="model_call_failed",
                pool=pool,
                pool_drops=pool_drops,
                refusals={},
                candidates_returned=0,
                proposals_written=0,
                model_provider=provider,
                now=now,
            )
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="failed",
                producer=None,
                result={"reason": "model_call_failed"},
                last_error=str(exc)[:400] or "model_call_failed",
                next_attempt_at=None if attempts >= max_attempts else _backoff(settings, attempts, now),
                now=now,
            )
            return {"status": "failed", "reason": "model_call_failed"}

        refusals: dict[str, int] = {}
        details: dict[str, str] = {}

        def _refuse(reason: str, detail: str = "") -> None:
            refusals[reason] = refusals.get(reason, 0) + 1
            if detail and reason not in details:
                details[reason] = detail[:200]

        # W8. Structure only: numbers resolved against the FROZEN pool.
        candidates, parse_refusals = parse_dream_payload(
            payload,
            pool=pool,
            max_proposals=knobs["max_proposals_per_run"],
            max_citations=knobs["max_citations"],
            quote_max_chars=knobs["quote_max_chars"],
        )
        for parse_refusal in parse_refusals:
            _refuse(parse_refusal.reason, parse_refusal.detail)
        candidates_returned = len(candidates) + len(parse_refusals)

        # W9. The devices. A candidate that fails any of them is dropped, not weakened.
        survivors: list[DreamCandidate] = []
        for candidate in candidates:
            kept, refusal = validate_candidate(
                candidate,
                pool=pool,
                min_citations=knobs["min_citations"],
                min_quote_overlap_tokens=knobs["min_quote_overlap_tokens"],
                quote_max_chars=knobs["quote_max_chars"],
                min_citation_relevance_tokens=knobs["min_citation_relevance_tokens"],
            )
            if kept is None or refusal is not None:
                if refusal is not None:
                    _refuse(refusal.reason, refusal.detail)
                continue
            survivors.append(kept)

        proposals_written = 0
        if survivors:
            rejected = load_rejected_proposals(
                db,
                scope=scope,
                since=now - timedelta(days=knobs["rejected_lookback_days"]),
                limit=knobs["rejected_scan_limit"],
            )
            live = load_live_proposals(db, scope=scope, limit=knobs["max_live_proposals"])
            proposals_written = _place_survivors(
                db,
                survivors=survivors,
                scope=scope,
                session_id=str(row["session_id"]),
                run_id=run_id,
                rejected=rejected,
                live=live,
                knobs=knobs,
                now=now,
                refuse=_refuse,
            )

        _finish_run(
            db,
            run_id=run_id,
            state="succeeded",
            refusal_reason="",
            pool=pool,
            pool_drops=pool_drops,
            refusals=refusals,
            candidates_returned=candidates_returned,
            proposals_written=proposals_written,
            model_provider=provider,
            now=now,
        )

        fenced = complete_planning_job(
            db,
            job_id=job_id,
            lease_owner=lease_owner,
            state="succeeded",
            producer=PLANNING_PRODUCER_MODEL,
            result={
                "proposals": proposals_written,
                "refusals": refusals,
                "refusal_details": details,
                "pool_size": len(pool),
            },
            last_error=None,
            next_attempt_at=None,
            now=now,
        )
        if not fenced:
            # Someone else holds the lease. Everything above is discarded, including the
            # proposals: a proposal minted under a lost lease is a duplicate of whatever the
            # real leaseholder is about to mint.
            db.rollback()
            _finish_run(
                db,
                run_id=run_id,
                state="failed",
                refusal_reason="lost_lease",
                pool=(),
                pool_drops={},
                refusals={},
                candidates_returned=0,
                proposals_written=0,
                model_provider=provider,
                now=now,
            )
            db.commit()
            return {"status": PLANNING_LOST_LEASE_REASON}
        db.commit()

    return {"status": "succeeded", "reason": "" if proposals_written else "no_candidate_survived"}


def _place_survivors(
    db: Session,
    *,
    survivors: Sequence[DreamCandidate],
    scope: ResolvedScope,
    session_id: str,
    run_id: str,
    rejected: Sequence[DreamProposalProjection],
    live: Sequence[DreamProposalProjection],
    knobs: Mapping[str, Any],
    now: datetime,
    refuse: Callable[..., None],
) -> int:
    """W10-W12, in the one order that is safe.

    Rejected-theme suppression runs BEFORE the duplicate-merge path, and that ordering is the
    whole of it: run the other way round and a candidate carrying a theme the owner has
    already refused merges into a live neighbour and comes back stronger, with no refusal
    recorded anywhere. The merge path is reachable only by a candidate that has cleared
    suppression.
    """
    written = 0
    live_pool = list(live)
    for candidate in survivors:
        blocking, failing = suppressed_by_rejection(
            candidate,
            rejected,
            now=now,
            threshold=knobs["duplicate_similarity"],
            min_new_citations=knobs["material_new_citations"],
            max_similarity=knobs["material_max_similarity"],
            cooldown_days=knobs["rejected_cooldown_days"],
            max_reproposals=knobs["max_reproposals"],
        )
        if blocking is not None:
            refuse("suppressed_rejected_theme", f"{failing}: {blocking.proposal_id}")
            continue

        duplicate = is_duplicate_theme(candidate, live_pool, threshold=knobs["duplicate_similarity"])
        if duplicate is not None:
            if not _merge_into(
                db,
                scope=scope,
                candidate=candidate,
                duplicate=duplicate,
                max_citations=knobs["max_citations"],
                after_surfaces=knobs["nonresponse_after_surfaces"],
                run_id=run_id,
                now=now,
            ):
                refuse("duplicate_theme", duplicate.proposal_id)
            continue

        # W11. Stage 2 at birth: the citations are re-verified against the live rows in the
        # same transaction that mints, so nothing crosses the wire on evidence that has
        # already moved.
        verified, dropped = validate_citations_deep(db, scope=scope, citations=candidate.citations)
        if len(verified) < int(knobs["min_citations"]):
            refuse("citations_unverifiable", ",".join(dropped)[:200])
            continue

        supersedes = _superseded_id(candidate, rejected, threshold=knobs["duplicate_similarity"])
        depth = 0
        if supersedes is not None:
            depth = int(supersedes.repropose_depth) + 1

        citations = dedupe_by_content_sha256(verified)
        digest, cutoff = evidence_revision(
            [{"id": citation.event_id, "ts": citation.observed_at} for citation in citations]
        )
        minted = mint_proposal(
            db,
            scope=scope,
            session_id=session_id,
            run_id=run_id,
            candidate=_with_citations(candidate, citations),
            evidence_revision=digest,
            evidence_cutoff_at=cutoff,
            supersedes_proposal_id=supersedes.proposal_id if supersedes is not None else None,
            repropose_depth=depth,
            ttl_days=int(knobs["proposal_ttl_days"]),
            now=now,
        )
        if minted:
            written += 1
    return written


def _with_citations(candidate: DreamCandidate, citations: Sequence[DreamCitation]) -> DreamCandidate:
    """A narrowed candidate keeps its own basis: the basis is a property of what survived."""
    kept = tuple(citations)
    return replace(candidate, citations=kept, evidence_basis=evidence_basis_for(kept))


def _superseded_id(
    candidate: DreamCandidate,
    rejected: Sequence[DreamProposalProjection],
    *,
    threshold: float,
) -> DreamProposalProjection | None:
    """The rejection this candidate reopens, if suppression lifted for one.

    Reachable only when ``suppressed_by_rejection`` returned ``(None, "")``, so any rejection
    still similar enough to match is one whose five material-change conditions all held.
    """
    best: DreamProposalProjection | None = None
    best_score = float(threshold)
    for proposal in rejected:
        if str(proposal.status) != DreamProposalStatus.REJECTED:
            continue
        score = theme_similarity(candidate.theme_tokens, proposal.theme_tokens)
        if score >= best_score:
            best = proposal
            best_score = score
    return best


def _merge_into(
    db: Session,
    *,
    scope: ResolvedScope,
    candidate: DreamCandidate,
    duplicate: DreamProposalProjection,
    max_citations: int,
    after_surfaces: int,
    run_id: str,
    now: datetime,
) -> bool:
    """A restatement of a live proposal makes that proposal stronger; it never makes a new row.

    Returns False when the candidate brings nothing the live proposal does not already have,
    which is the caller's cue to record ``duplicate_theme``. The live proposal's history —
    its surfaced count, its acceptance, its rejection reason — is untouched: this appends one
    ``evidence_revalidated`` event and changes no status.
    """
    known = {citation.content_sha256 for citation in duplicate.citations if citation.content_sha256}
    fresh = [citation for citation in candidate.citations if citation.content_sha256 not in known]
    if not fresh:
        return False

    merged = dedupe_by_content_sha256(tuple(duplicate.citations) + tuple(fresh))[: max(1, int(max_citations))]
    digest, cutoff = evidence_revision([{"id": citation.event_id, "ts": citation.observed_at} for citation in merged])
    apply_dream_events(
        db,
        scope=scope,
        proposal_id=duplicate.proposal_id,
        new_events=[
            DreamProposalEvent(
                seq=0,
                kind=DreamProposalEventKind.EVIDENCE_REVALIDATED,
                payload={
                    "citations": citations_to_json(merged),
                    "evidence_basis": evidence_basis_for(merged),
                    "evidence_revision": digest,
                    "evidence_cutoff_at": cutoff.isoformat() if cutoff is not None else None,
                    "reason": "duplicate_theme_merge",
                },
                occurred_at=now,
                actor="worker:dream_synthesis",
                actor_class="system",
                run_id=run_id,
            )
        ],
        now=now,
        after_surfaces=after_surfaces,
        retry_once=True,
    )
    return True
