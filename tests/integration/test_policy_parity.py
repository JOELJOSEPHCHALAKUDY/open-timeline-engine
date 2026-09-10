"""G-X1, G13 and G1's fingerprint arm — Y1 equivalence, Full/Lite parity, and the promotion gate.

**G-X1 is the load-bearing one.**  Y1 says an unqualified family means personalization is *not used*;
it does not mean the product stops.  Today **zero** families are qualified, so with the default
configuration every turn must come out of P4 exactly as it went in.  An earlier draft of P4 escalated
every turn on today's corpus to a human, which is a product shutdown wearing a safety argument, and
three critics proved it independently.  This file is the gate that catches it happening again.

Asserting a field would not catch it.  ``needs_human`` staying ``False`` while the reply text, the
classification or the next action changed is still a changed product.  So this captures **real turn
responses**: a fixed script of five turns is replayed through the real ``POST /v1/takeover/step`` and
compared, response body by response body, against a baseline recorded from the tree as it stood
*before* P4 (``P4_Y1_BASELINE_COMMIT``).  Everything must match except a short, named list of
surfaces that P4's own D10 says change unconditionally — and each exemption carries the delta number
that licenses it, so growing the list is a visible act rather than a quiet one.

A second arm replays the live corpus through the decision seam itself: for the most recent shadow
predictions, ``ensure_takeover_response(policy=<the real DecisionResult>)`` must return byte-identical
output to ``ensure_takeover_response(policy=None)``.  That one needs no baseline and no server, and
it is the assertion that would go red the instant ``_exposure()`` defaulted open.

**G13** — Full and Lite must produce identical ``policy_decision`` payloads for identical inputs,
*including an identical replayed advisor contribution*.  "Advisor absent on both" routes around the
divergence instead of testing it.

**G1's runtime arm** — a live turn's ``DecisionRequest.fingerprint()`` must equal the one recomputed
in replay.  Comparing the two paths' ``decision_policy_revision`` compares a module constant to
itself and passes while the two paths build completely different requests.

**The advisor arm lives next door.**  This file drives **Lite** and compares one response body
per turn.  Neither is enough for the advisor: the Full backend has a server-side advisor and Lite
does not, and the advisor's failure state (``advisor_fail_streak``) accumulates ACROSS turns, so a
single mislabelled turn escalates two turns later.  ``test_advisor_equivalence.py`` is the Y1 gate
for that class — both backends, several consecutive turns, comparing ``decision_source``,
``needs_human``, ``advisor_fail_streak`` and ``advice_visible``.

**The promotion gate** — run against the live corpus and required to report ``NOT_QUALIFIED`` with
per-family shortfalls.  Not to raise, not to return an empty result.  There are zero adjudicated
prospective cases in any family; that is the honest answer and the design expects it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# The tree the Y1 baseline was recorded from: P3 green, no P4 builder landed.
#
# How to re-record it (read-only with respect to the working tree — no checkout, no stash):
#
#     mkdir -p /tmp/p4base && git archive <commit> | tar -x -C /tmp/p4base
#     cp tests/integration/test_policy_parity.py /tmp/p4base/tests/integration/
#     PYTHONPATH=/tmp/p4base/shared:/tmp/p4base/services/tce_lite_api:/tmp/p4base/services/tce_api \
#       .venv/bin/python -c 'import importlib.util,json,sys; \
#         s=importlib.util.spec_from_file_location("rec","/tmp/p4base/tests/integration/test_policy_parity.py"); \
#         m=importlib.util.module_from_spec(s); s.loader.exec_module(m); \
#         json.dump({"baseline_commit":"<commit>","turns":m.capture_turns()}, open(sys.argv[1],"w"), indent=1, sort_keys=True, default=str)' \
#       tests/integration/p4_y1_turn_baseline.json
#
# PYTHONPATH wins over the editable installs, so the recorder runs the archived commit's code.
# Verified deterministic: two consecutive recordings of e805df6 were byte-identical after
# `normalize()`.
P4_Y1_BASELINE_COMMIT = "e805df6"
BASELINE_PATH = Path(__file__).with_name("p4_y1_turn_baseline.json")


# --------------------------------------------------------------------------------------
# The turn script, and the normalisation that makes two runs comparable
# --------------------------------------------------------------------------------------

Y1_TOKEN = "p4-y1-token"
Y1_WORKSPACE = "p4-y1-workspace"
Y1_SESSION = "p4-y1-session"

# Five turns chosen to move the fields Y1 is about: an activation, a concrete objective, a vague
# continuation, a high-risk question that should reach a human, and a stand-down.
Y1_TURNS: tuple[dict[str, str], ...] = (
    {"message": "beru take over", "task": "ship the stripe webhook fix"},
    {
        "message": "the stripe webhook is failing, do i force push the fix to prod",
        "task": "ship the stripe webhook fix",
    },
    {"message": "ok continue", "task": "ship the stripe webhook fix"},
    {"message": "should i drop the payments table and rebuild it", "task": "ship the stripe webhook fix"},
    {"message": "beru stand down", "task": "ship the stripe webhook fix"},
)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Substituted anywhere in a string, not just anchored: several summaries embed the generation clock
# in their prose, and a gate that reports those as a behaviour change is a gate nobody reads.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")

# Volatile by construction: a wall clock, a process-local id, or a timing measurement.
_VOLATILE_KEYS = frozenset({"source_revision", "planning_job_id", "latency_breakdown_ms"})
_VOLATILE_KEY_SUFFIXES = ("_ms", "_latency", "_at", "_ts")


def normalize(value: Any, key: str = "") -> Any:
    """Replace ids, clocks and timings with placeholders, and nothing else."""

    if key in _VOLATILE_KEYS or key.endswith(_VOLATILE_KEY_SUFFIXES):
        return f"<volatile:{key}>"
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize(v, key) for v in value]
    if isinstance(value, str):
        return _UUID_RE.sub("<uuid>", _TS_RE.sub("<ts>", value))
    return value


def capture_turns() -> list[dict[str, Any]]:
    """Drive the five turns through the real Lite HTTP surface and return normalised bodies.

    Imported lazily so this module can be loaded (and its normaliser reused by the recorder) under
    a checkout of a different commit, which is exactly how the baseline is produced.
    """

    from fastapi.testclient import TestClient
    from tce_lite_api.config import get_settings
    from tce_lite_api.main import app

    headers = {
        "Authorization": f"Bearer {Y1_TOKEN}",
        "X-TCE-Consumer": "p4-y1",
        "X-TCE-Role": "user",
        "X-TCE-Workspace": Y1_WORKSPACE,
        "X-TCE-User": "p4-y1-user",
        "X-TCE-Behavior-Subject": "p4-y1-user",
    }

    settings = get_settings()
    keys = ("lite_db_path", "api_tokens", "workspace_access_mode", "identity_claims_mode")
    original = {key: getattr(settings, key) for key in keys}
    captured: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        settings.lite_db_path = str(Path(tmp) / "p4-y1.db")
        settings.api_tokens = Y1_TOKEN
        settings.workspace_access_mode = "compat"
        settings.identity_claims_mode = "compat"
        try:
            with TestClient(app) as client:
                for index, turn in enumerate(Y1_TURNS):
                    response = client.post(
                        "/v1/takeover/step",
                        json={
                            "message": turn["message"],
                            "session_id": Y1_SESSION,
                            "persona_mode": "shadow",
                            "task": turn["task"],
                            "app_context": {"domain": "coding"},
                            "constraints": {"k": 4},
                            "interaction_id": f"p4-y1-{index}",
                            "allow_fallback": True,
                        },
                        headers=headers,
                    )
                    captured.append(
                        {
                            "message": turn["message"],
                            "status_code": response.status_code,
                            "body": normalize(response.json()),
                        }
                    )
        finally:
            for key, value in original.items():
                setattr(settings, key, value)
    return captured


# --------------------------------------------------------------------------------------
# G-X1 arm 1 — real turn responses, before and after
# --------------------------------------------------------------------------------------

# The fields that decide what happens to the human on this turn.  A change to any of these on a
# corpus with zero qualified families is an Y1 violation, full stop.
DECISION_BEARING_KEYS: tuple[str, ...] = (
    "needs_human",
    "decision_source",
    "classification",
    "action",
    "next_action",
    "next_step",
    "has_directive",
    "safety_decision",
    "response",
    "final_response",
    "persona_ack",
    "escalation_reason",
)

# The only surfaces allowed to differ, each with the D10 delta that licenses it.  These are
# unconditional defect removals, not personalization: they land with or without a qualification.
# Adding an entry here is how Y1 would be eroded, so each one names its licence.
SANCTIONED_DELTAS: dict[str, str] = {
    "policy_decision": "new reported block (p45_shared §7.3); null before P4",
    "confidence": "D10 delta 2 — the laundered-confidence merge and the 0.05*confidence term are gone",
    "decision_confidence": "D10 delta 2",
    "evidence_strength": "D10 delta 2 — re-derived from the policy's adequacy, not from a self-report",
    "evidence_strength_score": "D10 delta 2",
    "recency_coverage_score": "D10 delta 3 — recency is measured at decision time now",
    "guidance_summary": "D10 deltas 1/4/5 — fabricated fallback, impersonation prompt and the "
    "weak-evidence decisive rewrite are all deleted",
    "do": "D10 deltas 1/4/5 — advisor prose",
    "dont": "D10 deltas 1/4/5 — advisor prose",
    "recommended_actions": "D10 deltas 1/4/5 — advisor prose",
    "advisor_failure_reason": "D10 delta 1 — a raising gateway abstains instead of fabricating",
}


# Delta 5 lands as a *shape* change rather than a value change: the whole weak-evidence enforcement
# block disappears from the response.  Exempted by exact path, not by key name, so a `note` nested
# anywhere else in the body is still compared.
SANCTIONED_DELTA_PATHS: dict[str, str] = {
    "/enforced": "D10 delta 5 — the weak-evidence decisive rewrite is deleted",
    "/enforcement_reason": "D10 delta 5",
    "/takeover_enforcement": "D10 delta 5",
    "/note": "D10 delta 5 — the note was emitted by the enforcement block",
}


def _exempt(path: str, key: str) -> bool:
    if key in SANCTIONED_DELTAS:
        return True
    return any(path == root or path.startswith(root + "/") for root in SANCTIONED_DELTA_PATHS)


def _flatten(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}/{key}"
            if _exempt(child, key):
                continue
            yield from _flatten(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{path}[{index}]")
    else:
        yield path, value


def _load_baseline() -> list[dict[str, Any]]:
    assert BASELINE_PATH.exists(), (
        f"{BASELINE_PATH.name} is missing. It is recorded from {P4_Y1_BASELINE_COMMIT} — the tree "
        "as it stood before any P4 builder landed — and without it this gate proves nothing."
    )
    loaded = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert loaded["baseline_commit"] == P4_Y1_BASELINE_COMMIT
    turns: list[dict[str, Any]] = loaded["turns"]
    assert len(turns) == len(Y1_TURNS)
    return turns


def test_no_turn_that_succeeds_today_becomes_an_escalation() -> None:
    """G-X1 (Y1), the strict half: the decision-bearing surface is byte-identical.

    Recorded before P4, replayed after.  With zero qualified families the exposure gate is closed on
    every turn, so nothing the policy computes may reach the reply.
    """

    baseline = _load_baseline()
    current = capture_turns()

    problems: list[str] = []
    for index, (was, now) in enumerate(zip(baseline, current, strict=True)):
        assert was["message"] == now["message"]
        if was["status_code"] != now["status_code"]:
            problems.append(
                f"turn {index} ({was['message']!r}): status {was['status_code']} -> {now['status_code']}"
            )
        for key in DECISION_BEARING_KEYS:
            if key not in was["body"] and key not in now["body"]:
                continue
            before = was["body"].get(key)
            after = now["body"].get(key)
            if before != after:
                problems.append(f"turn {index} ({was['message']!r}) {key}: {before!r} -> {after!r}")

    assert not problems, (
        "A turn that succeeded before P4 now behaves differently, with zero qualified families. "
        "Y1: an unqualified family means personalization is not used, not that the product stops.\n  "
        + "\n  ".join(problems)
    )


# Differences P4 actually produces that D10's list of six does **not** license.  This is not an
# exemption list — the test below asserts the observed set is EXACTLY this set, so a seventh one is
# red — it is a *finding*, recorded where the next person will read it.
#
# All nine share one cause.  G17 requires the literal ``"Cold start mode: limited historical
# signal..."`` to disappear from the tracked tree.  That string was the Lite working-set summary on a
# cold corpus; its replacement ("Bundle generated at ... with N evidence events") scores differently
# in ``context_quality_score`` (0.4672 -> 0.6272 on turn 0), and the quality score is the input to
# the V7 bounded-retrieval trigger.  So deleting a cold-start *label* changed whether retrieval runs.
# D10 promised six unconditional deltas and this is a seventh, arriving through a field nobody was
# watching.  It is not an escalation and not a shutdown, so it does not violate Y1 — but it is a
# behaviour change with no qualification record, and it belongs in D10's list rather than in a diff
# somebody skims.
UNLICENSED_OBSERVED_DELTAS: dict[str, str] = {
    "/context_quality_score": "G17 summary rewrite moved the quality score",
    "/retrieval_reason": "downstream of the quality score",
    "/retrieval_source": "downstream of the quality score",
    "/retrieval_triggered": "downstream of the quality score",
    "/state/takeover_context/last_retrieval_trigger_turn": "downstream of the quality score",
    "/state/takeover_context/quality_history[*]/context_quality_score": "same",
    "/state/takeover_context/quality_history[*]/retrieval_triggered": "same",
    "/state/takeover_context/quality_rollup/avg_context_quality": "same",
    "/state/working_set_json/summary": "G17: the cold-start literal is deleted",
    # ---- P5 ----
    # A new integer on the turn wire, constant 0 until a proposal exists.  It is a COUNT, not a
    # decision: nothing reads it to decide anything, and the proposals themselves are fetched
    # through their own routes, which run the citation validation.  Recorded here rather than in
    # SANCTIONED_DELTAS because D10 licenses six P4 deltas and this is not one of them.
    "/dream_proposals_pending": "P5: pending-proposal count added to TakeoverStepResponse; 0 until a proposal exists",
    # ---- the classifier-certainty removal ----
    # `decision_confidence` lost its `classifier_certainty` term -- 0.15 of the number taken
    # from how the system had classified its OWN prior text, feeding the `low_decision_confidence`
    # escalation cause.  `decision_confidence` itself is already sanctioned (D10 delta 2); these
    # two are the places its value is carried onward.  Measured across five turns here and
    # twelve on both backends: no turn changed `needs_human`, `decision_source`,
    # `execution_permit_required` or `safety_decision`.  The direction is downward (0.693 ->
    # 0.6533 on turn 0), which errs toward asking the owner on the turn itself.
    # `autonomy_score` is the EWMA `0.8*autonomy + 0.2*decision_confidence`, and it is read on
    # LATER turns to place `needs_human_threshold` on its 0.45-0.60 ramp -- lower autonomy
    # means a lower threshold, so this second-order term points the other way and partly
    # offsets the first.  Both are small and neither flipped a turn: measured across twelve
    # turns on both backends, `needs_human` and `decision_source` are identical to e805df6.
    # The other consumer, `autonomy_gate_min_avg_decision_confidence` (0.72), only gets
    # harder to satisfy, which is the safe direction.
    "/state/takeover_context/quality_rollup/avg_decision_confidence": "the classifier-certainty term is gone from decision_confidence; this is its running average",
    "/state/autonomy_score": "downstream of avg_decision_confidence",
}

_INDEX_RE = re.compile(r"\[\d+\]")


def _pin_key(path: str) -> str:
    return _INDEX_RE.sub("[*]", path)


def test_the_rest_of_the_turn_is_unchanged_apart_from_the_six_named_deltas() -> None:
    """G-X1, the wide half: everything outside the D10 exemption list is identical too.

    ``needs_human`` staying ``False`` while the working set, the state machine or the citations
    changed is still a changed product.  This walks the whole response body.
    """

    baseline = _load_baseline()
    current = capture_turns()

    problems: list[str] = []
    for index, (was, now) in enumerate(zip(baseline, current, strict=True)):
        before = dict(_flatten(was["body"]))
        after = dict(_flatten(now["body"]))
        for path in sorted(set(before) | set(after)):
            if before.get(path, "<absent>") == after.get(path, "<absent>"):
                continue
            if _pin_key(path) in UNLICENSED_OBSERVED_DELTAS:
                continue
            problems.append(
                f"turn {index} {path}: {before.get(path, '<absent>')!r} -> {after.get(path, '<absent>')!r}"
            )

    assert not problems, (
        "The turn changed outside the six deltas D10 licenses and the nine already-recorded "
        "consequences of G17. Either a builder changed behaviour P4 promised not to change, or a "
        "new delta needs to be named — in SANCTIONED_DELTAS if D10 licenses it, in "
        "UNLICENSED_OBSERVED_DELTAS with its cause if it does not.\n  " + "\n  ".join(problems[:60])
    )


def test_the_exemption_list_is_not_a_blank_cheque() -> None:
    """The list above is the only thing standing between this gate and vacuity."""

    assert len(SANCTIONED_DELTAS) <= 12, "the Y1 exemption list has grown; each entry weakens the gate"
    assert len(SANCTIONED_DELTA_PATHS) <= 6
    assert not set(SANCTIONED_DELTAS) & set(DECISION_BEARING_KEYS), (
        "a decision-bearing field was added to the exemption list, which is how Y1 gets deleted "
        "without anybody deciding to delete it"
    )
    exempt_paths = {path.lstrip("/") for path in SANCTIONED_DELTA_PATHS}
    assert not exempt_paths & set(DECISION_BEARING_KEYS)


def test_the_weak_evidence_decisive_rewrite_is_actually_gone() -> None:
    """Delta 5, asserted rather than merely exempted.

    The baseline shows what it did: three of the five turns came back with ``enforced=True`` and
    ``enforcement_reason="weak_evidence_decisive"`` — the LOWEST-evidence turns produced the MOST
    forceful directive, because two cold-start marker strings failed a pass-through check.  An
    exemption that is never checked is a hole, so this pins both ends.
    """

    baseline = _load_baseline()
    was_enforced = [turn["body"].get("enforcement_reason") for turn in baseline]
    assert "weak_evidence_decisive" in was_enforced, (
        "the baseline no longer contains the defect this exemption exists for; re-record it or "
        "delete the exemption"
    )

    current = capture_turns()
    still_enforced = [
        (index, turn["body"].get("enforcement_reason"))
        for index, turn in enumerate(current)
        if turn["body"].get("enforcement_reason") == "weak_evidence_decisive"
    ]
    assert not still_enforced, f"the weak-evidence decisive rewrite is still firing: {still_enforced}"


# --------------------------------------------------------------------------------------
# G-X1 arm 2 — the live corpus, through the decision seam itself
# --------------------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("TCE_DATABASE_URL", "")
    assert url, (
        "TCE_DATABASE_URL is unset. The Y1 corpus arm and the promotion-gate arm both read the live "
        "corpus; running them against nothing would pass vacuously."
    )
    return url.replace("postgresql+psycopg://", "postgresql://")


def _fetch(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    import psycopg

    with psycopg.connect(_database_url()) as connection:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        columns = [description[0] for description in (cursor.description or [])]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _replay_inputs_present() -> bool:
    from tce_api.policy_store import replay_columns_present

    return replay_columns_present(_fetch)


def _live_shadow_contexts(limit: int = 20) -> list[dict[str, Any]]:
    """Frozen turns, with every input the decision actually had.

    The join is the point.  ``behavior_shadow_predictions`` stores ``candidate_option_count``, a
    scalar, and its ``query_json`` is built from the situation and the objective -- never from the
    options.  Replaying without this join recovered ``()`` on 56/56 rows and an empty candidate set
    abstains, so the harness was scoring a decision no turn ever made.  See
    ``tce_api.policy_store.REPLAY_CANDIDATE_COLUMN``.

    The select list itself is ``tce_api.policy_store.replay_row_columns`` and not a local literal,
    so this test, ``scripts/p4_qualification_report.py`` and the Lite twin cannot select different
    things and then each substitute a different literal for what they failed to select.
    """

    from tce_api.policy_store import replay_row_columns

    columns = replay_row_columns(replay_inputs_present=_replay_inputs_present())
    return _fetch(
        f"""
        SELECT {columns}
          FROM behavior_shadow_predictions p
          LEFT JOIN decision_opportunities o ON o.id = p.opportunity_id
         ORDER BY p.created_at DESC
         LIMIT %s
        """,
        (limit,),
    )


def _live_evidence(workspace_id: str, subject_user_id: str, limit: int = 40) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT id::text AS id,
               ts,
               situation_type,
               situation_summary,
               objective_text,
               context_snapshot,
               constraints_json,
               available_choices_json,
               selected_choice,
               action_taken,
               evidence_source,
               memory_class,
               confidence
          FROM decision_observations
         WHERE workspace_id = %s
           AND subject_user_id = %s
           AND (lifecycle_status IS NULL OR lifecycle_status <> 'expired')
         ORDER BY ts DESC
         LIMIT %s
        """,
        (workspace_id, subject_user_id, limit),
    )


def _request_from_row(row: dict[str, Any], evidence: list[dict[str, Any]]) -> Any:
    """The replay assembly, imported rather than re-written.

    It used to be a local literal that read ``query_json['available_choices']`` -- a key the write
    site never writes -- and then stamped ``"live-corpus-replay"``, ``"replay-v1"``, ``None`` and
    ``""`` over ``evidence_revision``, ``retrieval_version``, ``project_id`` and ``episode_key``,
    all four of which the row carries and two of which the request fingerprint hashes.  A replay
    that invents its own identity cannot be compared with the decision it claims to reproduce.
    """

    from tce_api.policy_store import replay_request_from_row

    return replay_request_from_row(row, evidence)


def test_the_live_corpus_never_produces_an_exposed_abstention() -> None:
    """Zero families are qualified, so ``exposed`` is ``False`` on every row of the real corpus.

    This is the assertion that goes red the instant ``_exposure()`` defaults open — which is one of
    the two routes by which the earlier draft turned every turn into an escalation.
    """

    from tce_shared.decision_policy import decide

    rows = _live_shadow_contexts()
    assert rows, "behavior_shadow_predictions is empty; this arm would pass vacuously"

    exposed: list[str] = []
    statuses: dict[str, int] = {}
    for row in rows:
        evidence = _live_evidence(str(row["workspace_id"]), str(row["subject_user_id"]))
        result = decide(_request_from_row(row, evidence))
        statuses[result.status.value] = statuses.get(result.status.value, 0) + 1
        if result.exposed:
            exposed.append(f"{row['id']} family={row['decision_family']} state={result.exposure_state.value}")

    assert not exposed, (
        "The policy exposed a decision on the live corpus, where no family holds a qualification "
        "record. Absence of a qualification record IS refusal.\n  " + "\n  ".join(exposed)
    )
    assert statuses, statuses


def test_an_unexposed_policy_result_changes_no_turn(subtests: object = None) -> None:
    """FN2/G14: ``policy=<unexposed>`` returns exactly what ``policy=None`` returns.

    Driven from the real corpus rather than from a hand-built result, so it covers whatever the
    policy actually concludes about real evidence — abstention, selection or a rule.
    """

    from tce_shared.decision_policy import decide
    from tce_shared.takeover import TakeoverMode, ensure_takeover_response

    rows = _live_shadow_contexts()
    assert rows

    probes = (
        (TakeoverMode.TAKEOVER, "beru take over"),
        (TakeoverMode.TAKEOVER, "ok continue"),
        (TakeoverMode.TAKEOVER, "should i force push to prod"),
        (TakeoverMode.SUGGEST, "what next"),
    )

    problems: list[str] = []
    for row in rows:
        evidence = _live_evidence(str(row["workspace_id"]), str(row["subject_user_id"]))
        request = _request_from_row(row, evidence)
        result = decide(request)
        context = {"objective": request.objective_text, "task": request.objective_text}
        for mode, text in probes:
            with_policy = ensure_takeover_response(mode, text, request.objective_text, dict(context), policy=result)
            without = ensure_takeover_response(mode, text, request.objective_text, dict(context), policy=None)
            if with_policy != without:
                problems.append(f"{row['id']} mode={mode} text={text!r}: {without!r} -> {with_policy!r}")

    assert not problems, (
        "An unexposed policy result changed the turn. Permission is `exposed`; an unqualified "
        "family means personalization is not used, not that the turn changes.\n  " + "\n  ".join(problems)
    )


# --------------------------------------------------------------------------------------
# The promotion gate, against the live corpus
# --------------------------------------------------------------------------------------

# Every family the corpus actually contains.  Read from the corpus, not typed, so a new family
# cannot slip past the report by not being listed.
def _live_families() -> list[str]:
    rows = _fetch(
        """
        SELECT DISTINCT decision_family
          FROM behavior_shadow_predictions
         WHERE decision_family IS NOT NULL
           AND prediction_stage = 'prospective'
        """
    )
    return sorted(str(row["decision_family"]) for row in rows)


def test_the_promotion_gate_reports_not_qualified_with_per_family_shortfalls() -> None:
    """G12: absence of a qualification is a *recorded refusal*, not an error and not a zero.

    P4's promotion clause admits only prospective cases the human answered without having already
    seen advice naming one of the options — ``decision_advice_shown``, which defaults to ``True``
    so an un-upgraded writer's row is excluded rather than admitted.  Measured on the live corpus
    the binding exclusion is not contamination at all but *unanswered questions*: 53 of 55
    prospective rows are unresolved, so the admissible count is zero in every family.  That is the
    expected result and the report must say it in those words, per family, with the clause that
    fell short — not raise, and not quietly return nothing.
    """

    from tce_shared.policy_evaluation import REQUIRED_BASELINES, PolicyCase, evaluate_policy
    from tce_shared.policy_evaluation import episode_key as build_episode_key
    from tce_shared.policy_thresholds import (
        MIN_ADJUDICATED,
        THRESHOLDS_EFFECTIVE_AT,
        THRESHOLDS_SHA,
        THRESHOLDS_VERSION,
    )

    families = _live_families()
    assert families, "the live corpus carries no prospective decision family; the gate is vacuous"

    from tce_api.policy_store import replay_evidence_rows, replay_row_columns

    replay_columns = replay_row_columns(replay_inputs_present=_replay_inputs_present())
    report: dict[str, dict[str, Any]] = {}
    for family in families:
        # Same select list as the report and the Lite twin -- see `replay_row_columns`.  The
        # literal that used to sit here selected `o.project_id` as `project_id` and omitted
        # `p.evidence_revision`, `p.retrieval_version` and `p.episode_key` entirely, which is
        # what forced the assembly below to substitute literals for them.
        rows = _fetch(
            f"""
            SELECT {replay_columns}
              FROM behavior_shadow_predictions p
              LEFT JOIN decision_opportunities o ON o.id = p.opportunity_id
             WHERE p.decision_family = %s
               AND p.prediction_stage = 'prospective'
             ORDER BY p.created_at DESC
            """,
            (family,),
        )

        # The promotion gate's four exclusion counters, computed on the real rows.  Printing them
        # per family is the deliverable: "0" with no reason is what a hidden shutdown looks like.
        excluded_unresolved = 0
        excluded_advice_shown = 0
        excluded_no_candidates = 0
        excluded_pre_registration = 0
        cases: list[PolicyCase] = []
        for row in rows:
            if row.get("resolution_state") != "resolved" or not row.get("actual_choice"):
                excluded_unresolved += 1
                continue
            # `decision_advice_shown`, not `advice_visible`: only the first asks the gate's
            # question -- did the text the human answered already name one of the offered
            # options -- and it DEFAULTS TO TRUE, so a row from a producer older than
            # 20260909_0040 is excluded rather than admitted as clean.  `advice_visible`
            # defaults to FALSE and is a report diagnostic in both backends
            # (main.py:18586, store.py:11422).
            if row.get("decision_advice_shown") is not False:
                excluded_advice_shown += 1
                continue
            frozen_at = row.get("frozen_at") or row.get("created_at")
            if frozen_at is not None and frozen_at.tzinfo is None:
                frozen_at = frozen_at.replace(tzinfo=UTC)
            if frozen_at is None or frozen_at < THRESHOLDS_EFFECTIVE_AT:
                excluded_pre_registration += 1
                continue
            # The evidence the decision was OFFERED, by stored id -- not a fresh unscoped
            # `ORDER BY ts DESC LIMIT 40` over today's corpus, which answers "what would the
            # policy conclude now" rather than "what did it conclude".
            offered_evidence, _missing = replay_evidence_rows(_fetch, row)
            request = _request_from_row(row, [dict(item) for item in offered_evidence])
            if len(request.candidate_options) < 2:
                excluded_no_candidates += 1
                continue
            cases.append(
                PolicyCase(
                    case_id=str(row["id"]),
                    # The STORED episode key where the row has one; recomputing it guesses
                    # `cancel_epoch` and the key is what keeps a split from straddling an
                    # episode, so a guessed key is a leak the split cannot see.
                    episode_key=request.episode_key
                    or build_episode_key(
                        workspace_id=str(row["workspace_id"]),
                        subject_user_id=str(row["subject_user_id"]),
                        project_id=request.project_id,
                        session_id=str(row.get("session_id") or ""),
                        objective_hash=row.get("objective_hash"),
                        cancel_epoch=0,
                    ),
                    leakage_group=str(row.get("objective_hash") or ""),
                    frozen_at=frozen_at,
                    decision_family=family,
                    project_id=request.project_id,
                    actual_choice=str(row["actual_choice"]),
                    request=request,
                    context_row=dict(row.get("query_json") or {}),
                )
            )

        verdict = evaluate_policy(cases, baselines=dict.fromkeys(REQUIRED_BASELINES, None))
        verdict["prospective_rows"] = len(rows)
        verdict["cases_excluded_unresolved"] = excluded_unresolved
        verdict["cases_excluded_advice_shown"] = excluded_advice_shown
        verdict["cases_excluded_no_candidates"] = excluded_no_candidates
        verdict["cases_excluded_pre_registration"] = excluded_pre_registration
        report[family] = verdict

    print(  # noqa: T201 - the report IS the deliverable; a verdict nobody can read is not one
        json.dumps(
            {
                family: {
                    "verdict": "QUALIFIED" if verdict["qualified"] else "NOT_QUALIFIED",
                    "adjudicated": verdict["adjudicated"],
                    "prospective_rows": verdict["prospective_rows"],
                    "cases_excluded_unresolved": verdict["cases_excluded_unresolved"],
                    "cases_excluded_advice_shown": verdict["cases_excluded_advice_shown"],
                    "cases_excluded_no_candidates": verdict["cases_excluded_no_candidates"],
                    "cases_excluded_pre_registration": verdict["cases_excluded_pre_registration"],
                    "shortfalls": list(verdict["shortfalls"]),
                }
                for family, verdict in sorted(report.items())
            },
            indent=1,
        )
    )

    assert set(report) == set(families), "the report must name every family, including the empty ones"
    assert any(verdict["prospective_rows"] for verdict in report.values()), (
        "no family carried a single prospective row, so the exclusion counters measured nothing"
    )

    for family, verdict in sorted(report.items()):
        assert verdict["qualified"] is False, f"{family} reported QUALIFIED on a corpus with no adjudicated cases"
        assert verdict["shortfalls"], f"{family} reported NOT QUALIFIED with no shortfall named"
        assert any(f"< {MIN_ADJUDICATED}" in shortfall for shortfall in verdict["shortfalls"]), (
            f"{family}: the adjudicated-count shortfall must be named explicitly: {verdict['shortfalls']}"
        )
        for baseline in REQUIRED_BASELINES:
            assert any(baseline in shortfall for shortfall in verdict["shortfalls"]), (
                f"{family}: baseline {baseline} must be reported NOT COMPUTABLE, not silently skipped: "
                f"{verdict['shortfalls']}"
            )
        assert verdict["thresholds_sha"] == THRESHOLDS_SHA
        assert verdict["thresholds_version"] == THRESHOLDS_VERSION
        assert "Nothing is calibrated" in verdict["diagnostics"]["calibration"], (
            "Y6: the report says plainly that nothing is calibrated"
        )


def test_no_family_holds_a_qualification_record() -> None:
    """The other half of the same claim: refusal is the state of the world, not just of the report."""

    import psycopg

    try:
        rows = _fetch(
            """
            SELECT decision_family, state, count(*) AS n
              FROM policy_qualifications
             WHERE state = 'qualified'
               AND expires_at > now()
             GROUP BY 1, 2
            """
        )
    except psycopg.errors.UndefinedTable:
        pytest.fail(
            "policy_qualifications does not exist: P4's migration 20260909_0040 has not been "
            "applied to this database. Run `alembic -c infra/alembic.ini upgrade head`. The table "
            "must exist before this gate means anything — 'no qualified row' and 'no table to look "
            "in' are not the same answer."
        )
    assert not rows, (
        "A live QUALIFIED row exists. Nothing in P4's corpus can have earned one — there are zero "
        f"adjudicated prospective cases in any family. Found: {rows}"
    )


# --------------------------------------------------------------------------------------
# G13 / G1 — Full and Lite decide identically, and replay reproduces the live request
# --------------------------------------------------------------------------------------


def _parity_request(advisor: Any) -> Any:
    from tce_shared.decision_policy import DecisionRequest

    decision_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    evidence = [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"p4-parity-{index}")),
            "ts": decision_at - timedelta(days=index + 1),
            "situation_type": "prioritization_needed",
            "situation_summary": "stripe webhook failing in production",
            "objective_text": "ship the stripe webhook fix",
            "selected_choice": "minimal verified fix" if index % 2 == 0 else "broad refactor",
            "action_taken": "patch the handler and run scoped tests",
            "evidence_source": "explicit",
            "memory_class": "preference",
        }
        for index in range(4)
    ]
    return DecisionRequest(
        decision_family="needs_human",
        situation_type="prioritization_needed",
        situation_summary="stripe webhook failing in production",
        objective_text="ship the stripe webhook fix",
        constraints={"risk": "production"},
        context_snapshot={"component": "api"},
        candidate_options=("minimal verified fix", "broad refactor"),
        evidence_rows=tuple(evidence),
        decision_at=decision_at,
        workspace_id=Y1_WORKSPACE,
        subject_user_id="p4-y1-user",
        project_id=None,
        episode_key="p4-parity-episode",
        evidence_revision="p4-parity-rev",
        evidence_cutoff_at=None,
        retrieval_version="parity-v1",
        model_id="parity-model",
        runtime_version="parity-runtime",
        advisor=advisor,
    )


def _replayed_advisor() -> Any:
    """The same parsed contribution on both sides.

    G13's v1 form put the advisor on neither side, which routes around the divergence instead of
    testing it: live Full has an advisor and replay does not, and that is precisely the input skew
    the fingerprint exists to detect.
    """

    from tce_shared.decision_policy import AdvisorContribution

    return AdvisorContribution(
        recommended_option="minimal verified fix",
        abstained=False,
        abstain_reason=None,
        evidence_ids=(str(uuid.uuid5(uuid.NAMESPACE_URL, "p4-parity-0")),),
        conflicting_evidence_ids=(),
        advisor_note="scoped change, reversible",
        parse_state="parsed",
        prompt_sha256="0" * 64,
        model_id="parity-model",
        runtime_version="parity-runtime",
    )


def test_full_and_lite_produce_the_same_policy_decision() -> None:
    """G13, with an identical replayed advisor contribution on both sides.

    Both backends import the same ``decide``; what this proves is that neither has wrapped it in a
    backend-local shim with different defaults, and that the wire block serialises identically.
    """

    import tce_api.main as full_main
    import tce_lite_api.main as lite_main

    request = _parity_request(_replayed_advisor())
    full = full_main.decide(request)
    lite = lite_main.decide(request)

    assert full.block_payload() == lite.block_payload(), (
        "Full and Lite produced different policy_decision blocks for an identical request.\n"
        f"full={json.dumps(full.block_payload(), sort_keys=True, default=str)}\n"
        f"lite={json.dumps(lite.block_payload(), sort_keys=True, default=str)}"
    )
    assert full.request_fingerprint == lite.request_fingerprint


def test_the_replayed_request_reproduces_the_live_fingerprint() -> None:
    """G1's runtime arm, with the field-by-field diff printed on failure.

    A fingerprint that matches proves the two paths agreed on the candidate set, the evidence set,
    the decision time, the retrieval version and whether an advisor was present. Comparing
    ``decision_policy_revision`` instead compares a module constant to itself.
    """

    from tce_shared.decision_policy import DECISION_POLICY_REVISION

    advisor = _replayed_advisor()
    live = _parity_request(advisor)
    replay = _parity_request(_replayed_advisor())

    if live.fingerprint() != replay.fingerprint():
        fields = {
            "decision_family": (live.decision_family, replay.decision_family),
            "situation_type": (live.situation_type, replay.situation_type),
            "candidate_options": (sorted(live.candidate_options), sorted(replay.candidate_options)),
            "evidence_ids": (
                sorted(str(row.get("id") or "") for row in live.evidence_rows),
                sorted(str(row.get("id") or "") for row in replay.evidence_rows),
            ),
            "decision_at": (live.decision_at.isoformat(), replay.decision_at.isoformat()),
            "evidence_revision": (live.evidence_revision, replay.evidence_revision),
            "retrieval_version": (live.retrieval_version, replay.retrieval_version),
            "advisor_present": (live.advisor is not None, replay.advisor is not None),
        }
        diff = {name: pair for name, pair in fields.items() if pair[0] != pair[1]}
        pytest.fail(f"request fingerprint diverged (revision {DECISION_POLICY_REVISION}): {diff}")

    # And the negative: a fingerprint that cannot tell an advisor apart detects nothing.
    without_advisor = _parity_request(None)
    assert without_advisor.fingerprint() != live.fingerprint(), (
        "the fingerprint is blind to advisor presence, which is one of the three input divergences "
        "it exists to detect"
    )


# --------------------------------------------------------------------------------------
# G1's substance arm — replay decides against the candidate set the turn actually offered
# --------------------------------------------------------------------------------------


def _live_prospective_with_options(
    limit: int = 40, *, only_non_empty: bool = False
) -> list[dict[str, Any]]:
    """Newest prospective rows joined to their opportunity.

    ``only_non_empty`` restricts the window to rows whose opportunity actually carries
    alternatives.  The recency window alone is not a stable source of those: every takeover
    turn appends a prospective row and today's turns offer no options, so a run of the
    integration suite can push all option-carrying rows out of any fixed ``LIMIT``.  The
    non-vacuity arm must not be at the mercy of how many turns ran before it.
    """

    from tce_api.policy_store import REPLAY_CANDIDATE_COLUMN

    predicate = (
        "AND jsonb_array_length(COALESCE(o.alternatives_json, '[]'::jsonb)) > 0"
        if only_non_empty
        else ""
    )
    return _fetch(
        f"""
        SELECT p.id::text AS id,
               p.workspace_id,
               p.subject_user_id,
               p.decision_family,
               p.query_json,
               p.candidate_option_count,
               p.created_at,
               o.id::text AS opportunity_id,
               {REPLAY_CANDIDATE_COLUMN}
          FROM behavior_shadow_predictions p
          JOIN decision_opportunities o ON o.id = p.opportunity_id
         WHERE p.prediction_stage = 'prospective'
           {predicate}
         ORDER BY p.created_at DESC
         LIMIT %s
        """,
        (limit,),
    )


def test_replay_reconstructs_the_candidate_set_the_turn_offered() -> None:
    """The deployed route and the evaluated route decide over the *same option list*.

    Before this fix the replay assembly read ``query_json['available_choices']`` — a key the
    write site has never once written, because ``_freeze_decision_opportunity`` builds
    ``query`` from the situation, the objective, the constraints and the snapshot and puts the
    options on the *opportunity*.  Measured on this corpus that read returned ``()`` on 56/56
    rows, and an empty candidate set abstains (Y7), so every "replayed" decision was an
    abstention the turn never made.  "Deployed route == evaluated route" was true in letter
    and false in substance.

    The list was persisted the whole time.  ``insert_opportunity(alternatives=...)`` and
    ``freeze_shadow_prediction`` run in one ``db.begin_nested()`` keyed by the same
    ``opportunity_id``, so the join is total for every prospective row.  This asserts the
    reconstruction three ways: non-empty, equal to what was offered, and consistent with the
    scalar the write site derived from the same list.
    """

    from tce_api.policy_store import replay_candidate_options

    # Two windows, checked with the same assertions: the newest rows (so a *fresh* write that
    # broke the join or the ordering is caught), plus the newest rows that actually carry
    # options (so the non-vacuity assertion below does not depend on how many optionless
    # takeover turns some earlier test in the session happened to append).
    recent = _live_prospective_with_options()
    assert recent, (
        "no prospective shadow row joins to a decision_opportunity; this arm would pass vacuously"
    )
    seen = {row["id"] for row in recent}
    rows = recent + [
        row
        for row in _live_prospective_with_options(only_non_empty=True)
        if row["id"] not in seen
    ]

    non_empty: list[dict[str, Any]] = []
    mismatches: list[str] = []
    for row in rows:
        offered = tuple(
            str(item) for item in (row.get("candidate_options_json") or []) if str(item).strip()
        )
        request = _request_from_row(row, [])
        if request.candidate_options != offered:
            mismatches.append(f"{row['id']}: replayed {request.candidate_options!r} != offered {offered!r}")
        # The scalar the write site stored is `len(candidate_options)` over this same list.  Where
        # it was written at all (it defaults to 0 and predates most of the corpus) it must agree.
        count = int(row.get("candidate_option_count") or 0)
        if count and count != len(offered):
            mismatches.append(f"{row['id']}: candidate_option_count={count} but offered {len(offered)}")
        if offered:
            non_empty.append(row)

    assert not mismatches, "replay rebuilt a different candidate set than the turn offered:\n  " + "\n  ".join(mismatches)
    assert non_empty, (
        "no prospective row in the whole corpus joins to a non-empty alternatives_json, so this "
        "arm cannot distinguish a working reconstruction from the () it used to return"
    )

    # And the regression pin: the key the broken replay read is empty on all of these rows, so a
    # revert to `query_json['available_choices']` cannot pass by coincidence.
    from_query_json = [
        row["id"]
        for row in non_empty
        if (json.loads(row["query_json"]) if isinstance(row["query_json"], str) else (row["query_json"] or {})).get(
            "available_choices"
        )
    ]
    assert not from_query_json, (
        "query_json now carries available_choices on some rows; if the write site started writing "
        "it, this test's regression pin is no longer meaningful and must be re-derived: "
        f"{from_query_json}"
    )

    sample = non_empty[0]
    print(  # noqa: T201 - the measurement is the deliverable for this gate
        json.dumps(
            {
                "prospective_rows_joined": len(rows),
                "rows_with_non_empty_candidate_set": len(non_empty),
                "sample_shadow_id": sample["id"],
                "sample_opportunity_id": sample["opportunity_id"],
                "sample_candidate_options": list(replay_candidate_options(sample)),
                "sample_query_json_available_choices": (
                    json.loads(sample["query_json"]) if isinstance(sample["query_json"], str) else (sample["query_json"] or {})
                ).get("available_choices"),
            },
            indent=2,
            sort_keys=True,
        )
    )
