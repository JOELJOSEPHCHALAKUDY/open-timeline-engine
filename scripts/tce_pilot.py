#!/usr/bin/env python3
"""The owner's pilot commands: enrol work before it starts, adjudicate it after.

Three subcommands and no fourth.  ``enroll`` freezes an arm to a piece of work, ``close`` records
the owner's adjudication of that work, and ``adjudicate`` records a judgement about a dream
proposal's fit.  All three are thin HTTP clients over routes that already exist; the interesting
thing about this file is what it *cannot* say.

WHAT THE ENROL LINE CANNOT SAY.  ``enroll`` takes ``--project --family --session --objective`` —
the work — and there is deliberately no ``--arm``, ``--slot``, ``--block``, ``--stratum``,
``--episode-key``, ``--salt``, ``--force``, ``--retry`` or ``--reroll``.  The allocation key is
P4's ``episode_key``, computed server-side from workspace, subject, project, session, objective
hash and cancel epoch, and ``PilotEnrolmentRequest`` has no field that lets a caller vary it.  So
re-running the same enrolment returns the ORIGINAL arm with ``reused=true`` rather than drawing
again, and an owner who cannot run the arm he was given records the deviation at close instead.
A deviation is data; a re-roll is a broken experiment.

The one exception is ``--elect``, which is the human-baseline ELECTION branch (§1.3, D-4): it
names a workflow the owner chose to run himself, it consumes no randomised slot, the server
refuses it unless ``TCE_PILOT_HUMAN_BASELINE_ENABLED`` is on, and the report labels every elected
episode descriptive-only.  It is a separate flag precisely so that electing is never something
the ordinary enrol line can do by accident.

WHAT THE CLOSE LINE NEEDS.  A verified human, which on this host means the host-capture
credential in ``~/.config/open-timeline-engine/host_capture.token`` — not a bare API token and
not the MCP executor, which is structurally barred from that credential.  This is P5 §0.6(2)'s
cost paid again for the same reason: an adjudication the party under test can write is not an
adjudication.  Without that token ``close`` and ``adjudicate`` refuse locally with exit 2 rather
than collecting a 403 from the server.

WHAT NEITHER CAN DO.  There is no subcommand that re-rolls, reassigns, deletes, resets or
unenrols, and this file issues no HTTP ``DELETE``, ``PUT`` or ``PATCH`` — the allocation ledger
and the adjudication ledger are append-only, and a CLI that could rewrite an allocation is a CLI
that could break the experiment after the fact.  Revising a dream judgement is
``adjudicate --supersedes <id>``, which appends.

Exit codes: 0 on success, 1 on an HTTP or transport failure, 2 on a refusal to run at all (no
``TCE_API_BASE_URL``, or no host-capture credential for a command that adjudicates).  Exit 2 is
the same refusal ``scripts/p4_qualification_report.py`` uses, and it exists so a cron job cannot
report a green pilot that never contacted a server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ARMS = ("existing_runtime", "markdown_handoff", "tce_assisted", "owner_unassisted")
RESCUE_LEVELS = ("none", "steered", "took_over", "abandoned_to_owner")
REVIEW_VERDICTS = ("accepted_as_is", "accepted_with_edits", "rejected")
RELEVANCE_VERDICTS = ("relevant", "not_relevant", "cannot_judge")
DELIVERY_USEFULNESS = ("useful", "not_useful", "too_early")

TOKEN_FILE_NAME = "host_capture.token"

NO_BASE_URL = (
    "NO API BASE URL: set TCE_API_BASE_URL (the runbook's precondition 4) before enrolling or "
    "closing anything. Nothing was sent. This is not a pass."
)
NO_HOST_CREDENTIAL = (
    "NO HOST-CAPTURE CREDENTIAL: an adjudication requires a verified human, which on this host "
    f"means ~/.config/open-timeline-engine/{TOKEN_FILE_NAME} (runbook precondition 5) and NOT a "
    "bare API token and NOT the MCP executor. Nothing was sent."
)


# --------------------------------------------------------------------------------------
# Credentials and transport
# --------------------------------------------------------------------------------------


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "open-timeline-engine"


def api_base_url(env: Mapping[str, str]) -> str:
    """The base URL comes from the environment ONLY.

    Deliberately not read from ``host_capture.env``: a command that silently found a server in a
    dotfile could report a pilot it never actually contacted, which is the failure exit 2 exists
    to make impossible.
    """

    return (env.get("TCE_API_BASE_URL") or "").strip().rstrip("/")


def host_capture_token(env: Mapping[str, str]) -> str:
    """The host-capture credential, from the environment or from the token file."""

    direct = (env.get("TCE_HOST_CAPTURE_TOKEN") or "").strip()
    if direct:
        return direct
    token_file = Path(env.get("TCE_HOST_CAPTURE_TOKEN_FILE") or _config_dir(env) / TOKEN_FILE_NAME)
    try:
        return token_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def _headers(env: Mapping[str, str], token: str) -> dict[str, str]:
    user_id = (env.get("TCE_CAPTURE_USER") or env.get("USER") or "local-user").strip() or "local-user"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-TCE-Consumer": "tce-pilot-cli",
        "X-TCE-Role": "user",
        "X-TCE-Workspace": (env.get("TCE_CAPTURE_WORKSPACE") or "personal").strip() or "personal",
        "X-TCE-User": user_id,
        "X-TCE-Behavior-Subject": user_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post(url: str, body: dict[str, Any], headers: Mapping[str, str], *, timeout: float = 20.0) -> tuple[int, dict[str, Any], str]:
    """POST is the only verb this file knows.  Returns (status, payload, error_text).

    There is no DELETE, no PUT and no PATCH here, and there is no code path that constructs one:
    every ledger P6 writes is append-only, so a second verb would have nothing to address.
    """

    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers=dict(headers))  # noqa: S310 - loopback API, scheme fixed by TCE_API_BASE_URL
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - same
            raw = response.read().decode("utf-8", errors="replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), {}, detail.strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {}, f"{type(exc).__name__}: {exc}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return status, {}, f"the server returned a non-JSON body: {raw[:200]}"
    if not isinstance(parsed, dict):
        return status, {}, f"the server returned a {type(parsed).__name__}, not an object"
    return status, parsed, ""


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return "" if value is None else str(value)


def _flag(payload: Mapping[str, Any], key: str) -> str:
    return "true" if bool(payload.get(key)) else "false"


# --------------------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Two ledgers, three verbs, and nothing that undoes either.

    ``allow_abbrev=False`` throughout: with abbreviation on, argparse would quietly accept a
    prefix of a legitimate flag, and "the enrol line cannot name an arm" would become a fact
    about which letters happen to collide rather than a fact about the surface.
    """

    parser = argparse.ArgumentParser(
        prog="tce_pilot.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    enroll = subcommands.add_parser(
        "enroll",
        help="freeze an arm to a piece of work, BEFORE the work starts",
        description=(
            "Enrol work before you start it. You describe the work; the server derives the episode "
            "key and the arm. Re-running this for the same objective in the same session returns "
            "the SAME arm with reused=true -- there is no re-roll, and if you cannot run the arm "
            "you were given, run what you can and record the deviation at close."
        ),
        allow_abbrev=False,
    )
    enroll.add_argument("--project", default=None, help="project id hint; the server canonicalises it and it becomes this episode's report cell (the resolver canonicalises, it does not entitle)")
    enroll.add_argument("--family", required=True, help="decision family, e.g. needs_human")
    enroll.add_argument("--session", required=True, help="the session id you are about to work in")
    enroll.add_argument("--objective", required=True, help="what you are about to do, in your own words")
    enroll.add_argument("--task", default=None, help="optional P2 task id this episode belongs to")
    enroll.add_argument(
        "--elect",
        choices=list(ARMS),
        default=None,
        metavar="ARM",
        help=(
            "the ELECTION branch: record a workflow you chose to run yourself. Consumes no randomised "
            "slot, is refused unless TCE_PILOT_HUMAN_BASELINE_ENABLED is on, and the report labels the "
            "episode descriptive-only. This is not a way to pick a randomised arm; nothing widens it."
        ),
    )
    enroll.add_argument("--json", action="store_true", help="emit the server's response verbatim instead of the one-line summary")

    close = subcommands.add_parser(
        "close",
        help="record your adjudication of an enrolled episode",
        description=(
            "The adjudication. You supply the arm you actually ran, the rescue level, whether it "
            "finished, your review minutes and your review verdict. The server derives deviated, "
            "completion_basis, adjudication_independent and late_close. There is no expiry: a late "
            "close sets a diagnostic flag and is otherwise an ordinary close."
        ),
        allow_abbrev=False,
    )
    close.add_argument("episode_id", help="the episode id printed by enroll")
    close.add_argument("--executed-arm", required=True, choices=list(ARMS), help="the arm you ACTUALLY ran; if it differs from the assigned arm the server records the deviation")
    close.add_argument("--rescue", required=True, choices=list(RESCUE_LEVELS), help="how far you had to step in; nothing in the runtime can observe this, so it is yours to state")
    close.add_argument("--finished", action="store_true", help="your attestation that the work was completed; the LAST fallback for completion_basis, behind P3 verification and P2 task state")
    close.add_argument("--review-minutes", type=int, default=0, help="minutes you spent reviewing the result")
    close.add_argument("--review-verdict", required=True, choices=list(REVIEW_VERDICTS), help="accepted_as_is, accepted_with_edits or rejected")
    close.add_argument("--deviation-reason", default="", help="why you ran a different arm, if you did")
    close.add_argument("--unfinished-reason", default="", help="why it did not finish, if it did not")
    close.add_argument("--json", action="store_true", help="emit the server's response verbatim instead of the one-line summary")

    adjudicate = subcommands.add_parser(
        "adjudicate",
        help="judge whether a dream proposal was a reasonable thing to propose",
        description=(
            "Separate from acceptance, and deliberately so. Accepting a proposal says 'I want this'; "
            "adjudicating it says 'this was a reasonable thing to propose'. The second is never "
            "derived from the first. Changed your mind? Adjudicate again with --supersedes; the "
            "table is append-only and only the latest judgement per proposal is counted."
        ),
        allow_abbrev=False,
    )
    adjudicate.add_argument("proposal_id", help="the dream proposal id")
    adjudicate.add_argument("--relevance", required=True, choices=list(RELEVANCE_VERDICTS), help="cannot_judge is a real answer and sits in neither numerator nor denominator")
    adjudicate.add_argument(
        "--blind",
        action="store_true",
        help="claim you judged this before any human verdict on it. A CLAIM, not a fact: the server checks it against P5's append-only event log and answers with a reason when it refuses.",
    )
    adjudicate.add_argument("--rationale", default="", help="why, in your own words")
    adjudicate.add_argument(
        "--delivery-useful",
        default=None,
        choices=list(DELIVERY_USEFULNESS),
        help="for a proposal that was pursued and delivered; stored always, counted only after the 30-day lookback",
    )
    adjudicate.add_argument("--supersedes", default=None, help="the adjudication id this one replaces; the earlier row stays")
    adjudicate.add_argument("--json", action="store_true", help="emit the server's response verbatim instead of the one-line summary")

    return parser


# --------------------------------------------------------------------------------------
# The three commands
# --------------------------------------------------------------------------------------


def _run_enroll(args: argparse.Namespace, base_url: str, headers: Mapping[str, str]) -> int:
    body: dict[str, Any] = {
        "session_id": args.session,
        "decision_family": args.family,
        "objective_text": args.objective,
    }
    if args.project:
        body["project_id"] = args.project
    if args.task:
        body["task_id"] = args.task
    if args.elect:
        body["elect_arm"] = args.elect
    status, payload, error = _post(f"{base_url}/v1/pilot/episodes", body, headers)
    if status != 200:
        return _report_failure("enroll", status, error)
    if args.json:
        print(json.dumps(payload, indent=1, sort_keys=True))  # noqa: T201 - this IS the command's output
        return 0
    reused = " reused=true" if bool(payload.get("reused")) else ""
    print(  # noqa: T201 - this IS the command's output
        f"episode {_text(payload, 'episode_id')}  arm={_text(payload, 'arm_id')}"
        f"  block={_text(payload, 'block_ordinal')} slot={_text(payload, 'slot')}"
        f"  allocated_at={_text(payload, 'allocated_at')}"
        f"  kind={_text(payload, 'allocation_kind')}{reused}"
    )
    return 0


def _run_close(args: argparse.Namespace, base_url: str, headers: Mapping[str, str]) -> int:
    body: dict[str, Any] = {
        "executed_arm": args.executed_arm,
        "rescue_level": args.rescue,
        "finished": bool(args.finished),
        "review_verdict": args.review_verdict,
        "review_minutes": int(args.review_minutes),
        "deviation_reason": args.deviation_reason,
        "unfinished_reason": args.unfinished_reason,
    }
    status, payload, error = _post(f"{base_url}/v1/pilot/episodes/{args.episode_id}/close", body, headers)
    if status != 200:
        return _report_failure("close", status, error)
    if args.json:
        print(json.dumps(payload, indent=1, sort_keys=True))  # noqa: T201 - this IS the command's output
        return 0
    print(  # noqa: T201 - this IS the command's output
        f"closed  deviated={_flag(payload, 'deviated')}"
        f"  completion_basis={_text(payload, 'completion_basis')}"
        f"  independent={_flag(payload, 'adjudication_independent')}"
        f"  late_close={_flag(payload, 'late_close')}"
    )
    return 0


def _run_adjudicate(args: argparse.Namespace, base_url: str, headers: Mapping[str, str]) -> int:
    body: dict[str, Any] = {
        "relevance": args.relevance,
        "rationale": args.rationale,
        "blind_claimed": bool(args.blind),
    }
    if args.delivery_useful:
        body["delivery_useful"] = args.delivery_useful
    if args.supersedes:
        body["supersedes_adjudication_id"] = args.supersedes
    status, payload, error = _post(f"{base_url}/v1/dreams/{args.proposal_id}/adjudicate", body, headers)
    if status != 200:
        return _report_failure("adjudicate", status, error)
    if args.json:
        print(json.dumps(payload, indent=1, sort_keys=True))  # noqa: T201 - this IS the command's output
        return 0
    # ``blind_verified`` is the server's finding and ``blind_reason`` says why, so a refused
    # blindness claim is never a bare ``false``.
    reason = _text(payload, "blind_reason")
    suffix = f" ({reason})" if reason else ""
    print(  # noqa: T201 - this IS the command's output
        f"adjudicated  blind_claimed={_flag(payload, 'blind_claimed')}"
        f"  blind_verified={_flag(payload, 'blind_verified')}{suffix}"
    )
    return 0


def _report_failure(command: str, status: int, error: str) -> int:
    where = "transport" if status == 0 else f"HTTP {status}"
    print(f"{command} failed ({where}): {error}", file=sys.stderr)  # noqa: T201 - a failure belongs on stderr
    return 1


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = os.environ

    base_url = api_base_url(env)
    if not base_url:
        print(NO_BASE_URL, file=sys.stderr)  # noqa: T201 - the refusal IS the output
        return 2

    token = host_capture_token(env)
    adjudicating = args.command in {"close", "adjudicate"}
    if adjudicating and not token:
        print(NO_HOST_CREDENTIAL, file=sys.stderr)  # noqa: T201 - the refusal IS the output
        return 2

    headers = _headers(env, token)
    if args.command == "enroll":
        return _run_enroll(args, base_url, headers)
    if args.command == "close":
        return _run_close(args, base_url, headers)
    return _run_adjudicate(args, base_url, headers)


if __name__ == "__main__":
    raise SystemExit(main())
