#!/usr/bin/env python3
"""Run P4's promotion gate against the live corpus and print the per-family verdict.

This is the operator command `docs/decision-policy.md` names.  It reads only; it writes no
qualification record and mutates nothing.

What it prints is a **recorded refusal**, not an error and not a zero.  A family becomes
QUALIFIED only by clearing every frozen clause in `tce_shared.policy_thresholds`; absence of a
qualification record IS refusal, so the report has to say *which* clause fell short, per family,
in words.  On today's corpus there are zero adjudicated prospective cases in any family, so every
family reports NOT_QUALIFIED and the binding shortfall is the adjudicated count.  That is the
expected output.

Nothing here is calibrated.  `policy_score` is an uncalibrated vote-share heuristic and the
report says so on every run (Y6): no isotonic fit ships, because the only fit that was specified
was in-sample, unsplit, and over a corpus with zero adjudicated cases.

Replay is a **reconstruction**, not a re-run.  Each case is rebuilt from the stored row: the
candidate set from the joined ``decision_opportunities.alternatives_json``, the evidence set from
the offered ids on the row (alembic ``20260909_0041``), and every identity field --
``evidence_revision``, ``retrieval_version``, ``project_id``, ``episode_key``, ``decision_at`` --
from the row rather than from a literal invented here.  All of that lives in
``tce_api.policy_store`` so this script and ``tests/integration/test_policy_parity.py`` cannot
drift apart.  Where ``20260909_0041`` is not applied the report says so on stderr and prints
``replay: inputs_available=False`` beside every verdict, because a replay against an evidence set
that was never stored is measuring a different decision.

The four exclusion counters are printed beside the verdict on purpose.  "adjudicated = 0" with no
reason is indistinguishable from a hidden shutdown; "0, of which 45 were never answered" is a
product decision the owner can act on.

Usage:

    TCE_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/tce \
        .venv/bin/python scripts/p4_qualification_report.py [--json]

Exit code is 0 when the report was produced, whatever the verdict.  It is 2 only when the report
could not be produced at all -- no database, or no prospective rows to measure, because a gate
that measures nothing must not read as a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

# scripts/ is marked protected in the project CLAUDE.md.  The owner granted an explicit
# override for THIS FILE and THIS CHANGE (Z3): the report is the harness that produces the
# qualification verdict, and it was replaying every case against an empty candidate set, an
# invented identity and a freshly-retrieved evidence set.  The rule is not being pretended away
# -- it is being overridden, once, on the record, for the file whose defect made the verdict
# meaningless.  Everything the replay needs now lives in `tce_api.policy_store` so that this
# script, `tests/integration/test_policy_parity.py` and the Lite twin cannot drift apart again.
from tce_api.policy_store import (
    replay_columns_present,
    replay_evidence_rows,
    replay_request_from_row,
    replay_row_columns,
)
from tce_shared.policy_evaluation import (
    REQUIRED_BASELINES,
    PolicyCase,
    episode_key,
    evaluate_policy,
)
from tce_shared.policy_thresholds import (
    THRESHOLDS_EFFECTIVE_AT,
    THRESHOLDS_SHA,
    THRESHOLDS_VERSION,
)

_PROSPECTIVE_ROWS = """
    SELECT {columns}
      FROM behavior_shadow_predictions p
      LEFT JOIN decision_opportunities o ON o.id = p.opportunity_id
     WHERE p.decision_family = %s
       AND p.prediction_stage = 'prospective'
     ORDER BY p.created_at DESC
"""

_FAMILIES = """
    SELECT DISTINCT decision_family
      FROM behavior_shadow_predictions
     WHERE prediction_stage = 'prospective'
       AND decision_family IS NOT NULL
     ORDER BY decision_family
"""

def _database_url() -> str:
    url = os.environ.get("TCE_DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(
            "TCE_DATABASE_URL is unset. This report reads the live corpus; running it against "
            "nothing would print a vacuous NOT_QUALIFIED."
        )
    return url.replace("postgresql+psycopg://", "postgresql://")


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    import psycopg

    with psycopg.connect(_database_url()) as connection:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        columns = [description[0] for description in (cursor.description or [])]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _family_report(family: str, *, replay_inputs_present: bool) -> dict[str, Any]:
    rows = _query(
        _PROSPECTIVE_ROWS.format(columns=replay_row_columns(replay_inputs_present=replay_inputs_present)),
        (family,),
    )

    excluded_unresolved = 0
    excluded_advice_shown = 0
    excluded_no_candidates = 0
    excluded_pre_registration = 0
    evidence_ids_unresolved = 0
    cases: list[PolicyCase] = []

    for row in rows:
        if row.get("resolution_state") != "resolved" or not row.get("actual_choice"):
            excluded_unresolved += 1
            continue
        # `decision_advice_shown`, not `advice_visible`.  The two are different questions and
        # only the first is the gate's: it is true when the text the human answered already
        # named one of the offered options, and it DEFAULTS TO TRUE, so a row written by a
        # producer that predates 20260909_0040 is excluded rather than silently admitted.
        # `advice_visible` merely records that some advisor output was rendered, defaults to
        # FALSE, and both backends label it a report diagnostic (main.py:18586, store.py:11422).
        # Filtering on it admits every legacy row into the denominator as uncontaminated.
        if row.get("decision_advice_shown") is not False:
            excluded_advice_shown += 1
            continue
        frozen_at = _aware(row.get("frozen_at")) or _aware(row.get("created_at"))
        if frozen_at is None or frozen_at < THRESHOLDS_EFFECTIVE_AT:
            excluded_pre_registration += 1
            continue
        # Z3(c): the evidence the decision was OFFERED, loaded by the ids stored on the row,
        # not a fresh unscoped `ORDER BY ts DESC LIMIT 40` over today's corpus.  `missing` names
        # ids that no longer resolve; they are counted, because a case replayed without one of
        # its inputs is not the case that was frozen.
        evidence, missing = replay_evidence_rows(_query, row)
        if missing:
            evidence_ids_unresolved += len(missing)
        # Z3(b): every identity field is read from the row.  This used to stamp
        # "live-corpus-replay" / "replay-v1" / None / "" over four fields the row already
        # carried, two of which the request fingerprint hashes.
        request = replay_request_from_row(row, evidence)
        if len(request.candidate_options) < 2:
            excluded_no_candidates += 1
            continue
        cases.append(
            PolicyCase(
                case_id=str(row["id"]),
                # The STORED episode key, when the row carries one.  Recomputing it here was a
                # third invented identity: `cancel_epoch=0` is a guess, and the episode key is
                # what stops a train/test split straddling one episode, so a guessed key is a
                # leak the split cannot see.  The recomputation survives only as the fallback
                # for rows written before the column existed.
                episode_key=request.episode_key
                or episode_key(
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

    # Every required baseline is passed as None -- NOT COMPUTABLE.  An unavailable baseline is
    # never treated as beaten by default; that is the whole point of REQUIRED_BASELINES.
    verdict: dict[str, Any] = dict(evaluate_policy(cases, baselines=dict.fromkeys(REQUIRED_BASELINES, None)))
    verdict["prospective_rows"] = len(rows)
    verdict["cases_excluded_unresolved"] = excluded_unresolved
    verdict["cases_excluded_advice_shown"] = excluded_advice_shown
    verdict["cases_excluded_no_candidates"] = excluded_no_candidates
    verdict["cases_excluded_pre_registration"] = excluded_pre_registration
    verdict["evidence_ids_unresolved"] = evidence_ids_unresolved
    verdict["replay_inputs_available"] = replay_inputs_present
    return verdict


def _render_text(report: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("P4 PROMOTION GATE — live corpus")
    lines.append(f"thresholds_version={THRESHOLDS_VERSION}  thresholds_sha={THRESHOLDS_SHA}")
    lines.append(f"thresholds_effective_at={THRESHOLDS_EFFECTIVE_AT.isoformat()}")
    lines.append("")
    for family, verdict in sorted(report.items()):
        status = "QUALIFIED" if verdict["qualified"] else "NOT_QUALIFIED"
        lines.append(f"{family}: {status}")
        lines.append(
            f"  adjudicated={verdict['adjudicated']}  prospective_rows={verdict['prospective_rows']}"
        )
        lines.append(
            f"  excluded: unresolved={verdict['cases_excluded_unresolved']} "
            f"advice_shown={verdict['cases_excluded_advice_shown']} "
            f"no_candidates={verdict['cases_excluded_no_candidates']} "
            f"pre_registration={verdict['cases_excluded_pre_registration']}"
        )
        lines.append(
            f"  replay: inputs_available={verdict['replay_inputs_available']} "
            f"evidence_ids_unresolved={verdict['evidence_ids_unresolved']}"
        )
        lines.append("  shortfalls:")
        for shortfall in verdict["shortfalls"]:
            lines.append(f"    - {shortfall}")
        calibration = str(verdict.get("diagnostics", {}).get("calibration", ""))
        if calibration:
            lines.append(f"  calibration: {calibration}")
        lines.append("")
    qualified = [family for family, verdict in report.items() if verdict["qualified"]]
    lines.append(
        "RESULT: no family is qualified, so personalization is NOT USED on any decision and every "
        "turn behaves exactly as it did before P4."
        if not qualified
        else f"RESULT: qualified families: {sorted(qualified)}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the raw verdict dict instead of the text report")
    args = parser.parse_args(argv)

    families = [str(row["decision_family"]) for row in _query(_FAMILIES)]
    if not families:
        print(  # noqa: T201 - this IS the command's output
            "NO PROSPECTIVE ROWS: the corpus carries no frozen prospective decision in any family, "
            "so the gate measured nothing. This is not a pass.",
            file=sys.stderr,
        )
        return 2

    replay_inputs_present = replay_columns_present(_query)
    if not replay_inputs_present:
        print(  # noqa: T201 - a caveat on the verdict belongs beside the verdict
            "REPLAY INPUTS UNAVAILABLE: alembic 20260909_0041 is not applied, so the evidence "
            "set each frozen decision was offered was never stored. Every case below is replayed "
            "with an EMPTY evidence set, which is not what the turn decided against. The verdict "
            "still prints -- it is a refusal either way -- but it is not a reconstruction.",
            file=sys.stderr,
        )
    report = {family: _family_report(family, replay_inputs_present=replay_inputs_present) for family in families}
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True, default=str))  # noqa: T201
    else:
        print(_render_text(report))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
