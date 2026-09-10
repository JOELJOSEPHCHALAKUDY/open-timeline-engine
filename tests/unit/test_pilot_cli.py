"""The owner's two commands — and the four things neither of them can do.

``scripts/tce_pilot.py`` is the only way an episode is enrolled or closed by hand (§7.2, §7.3).
It is a thin HTTP client, so the interesting assertions are about its *surface*: what the owner is
allowed to say.  An allocation that a caller can influence is not an allocation, and an
adjudication an executor can write is not an adjudication.

Four properties, each with a discrimination twin:

1. **``enroll`` takes the work, never the arm.**  ``--project --family --session --objective`` and
   nothing that names an arm, a slot, a block, a stratum or an episode key.  The single exception
   is ``--elect``, which is the *election* branch: it is descriptive-only by construction, it
   consumes no randomised slot, and the report labels it so.  Proof 4 in the design measured what
   the alternative looks like — four label edits reached all four arms.
2. **There is no re-roll.**  Re-running the same enrolment returns the same arm with
   ``reused=true``; there is no ``--force``, no ``--reroll``, no ``--retry``, and no subcommand
   that reassigns or deletes.  "If you cannot run the assigned arm, do not re-enroll" (§7.2) is a
   sentence in a runbook; this is the part that holds when nobody reads it.
3. **``close`` is the adjudication and needs a verified human.**  It carries the host-capture
   credential, not a bare API token and not the MCP executor — P5 §0.6(2)'s cost, paid again here
   for the same reason.
4. **It refuses to run against nothing.**  No base URL is exit 2, in the shape
   ``p4_qualification_report.py`` already uses, so a cron job cannot report a green pilot that
   never contacted a server.

THE CONTRACT this file pins on ``scripts/tce_pilot.py`` (Builder F's CLI half):

    def build_parser() -> argparse.ArgumentParser     # subcommands: enroll, close
    def main(argv: list[str] | None = None) -> int

Every flag asserted below is quoted verbatim from the runbook transcripts in p6_design §7.2/§7.3.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
CLI_PATH = REPO / "scripts" / "tce_pilot.py"

_ABSENT = (
    "scripts/tce_pilot.py does not exist. This gate is LIVE: it fails until Builder F's CLI half "
    "lands. The contract it must satisfy is stated in this module's docstring — build_parser() and "
    "main(argv) -> int, with the enroll/close flags quoted from p6_design §7.2 and §7.3."
)

# §7.2 and §7.3, verbatim.
ENROLL_ARGV = ["enroll", "--project", "proj_x", "--family", "needs_human", "--session", "sess-1", "--objective", "fix the dashboard takeover scroll"]
CLOSE_ARGV = [
    "close",
    "4f0c0000-0000-4000-8000-000000000000",
    "--executed-arm",
    "markdown_handoff",
    "--rescue",
    "none",
    "--finished",
    "--review-minutes",
    "12",
    "--review-verdict",
    "accepted_with_edits",
]

#: Anything here would let the caller reach into the allocation instead of describing the work.
FORBIDDEN_ENROLL_FLAGS = ("--arm", "--slot", "--block", "--stratum", "--episode-key", "--reroll", "--force", "--retry", "--salt")

#: A subcommand that undoes an allocation or an adjudication. There is no such thing in P6.
FORBIDDEN_SUBCOMMANDS = ("reroll", "reassign", "reallocate", "delete", "reset", "unenroll", "withdraw")


# --------------------------------------------------------------------------------------
# Loading the artifact under test
# --------------------------------------------------------------------------------------


def _cli_source() -> str:
    if not CLI_PATH.exists():
        pytest.fail(_ABSENT)
    return CLI_PATH.read_text(encoding="utf-8")


def _cli_module() -> ModuleType:
    _cli_source()
    spec = importlib.util.spec_from_file_location("tce_pilot_under_test", CLI_PATH)
    assert spec is not None and spec.loader is not None, CLI_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("build_parser", "main"):
        if not callable(getattr(module, name, None)):
            pytest.fail(f"scripts/tce_pilot.py defines no callable {name!r}; see this module's docstring for the contract.")
    return module


def _parser() -> argparse.ArgumentParser:
    parser = _cli_module().build_parser()
    assert isinstance(parser, argparse.ArgumentParser), "build_parser() must return an ArgumentParser"
    return parser


def _string_literals(source: str) -> Iterator[str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _accepts(parser: argparse.ArgumentParser, argv: list[str]) -> bool:
    """True when argparse takes the line.  ``parse_args`` exits 2 on an unknown flag."""

    try:
        parser.parse_args(argv)
    except SystemExit:
        return False
    return True


# --------------------------------------------------------------------------------------
# The scanners, as pure predicates.  Each is applied to the real artifact and to a twin.
# --------------------------------------------------------------------------------------


def scan_mutating_calls(source: str) -> list[str]:
    """A CLI that can rewrite an allocation is a CLI that can break the experiment."""

    offences: list[str] = []
    delete_sql = re.compile(r"DELETE\s+FROM\s+(pilot_|dream_relevance_)", re.IGNORECASE)
    arm_update = re.compile(r"UPDATE\s+pilot_episodes[\s\S]*\barm_id\b\s*=", re.IGNORECASE)
    for literal in _string_literals(source):
        if delete_sql.search(literal) or arm_update.search(literal):
            offences.append(" ".join(literal.split())[:120])
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {"delete", "put", "patch"}:
            offences.append(f"line {node.lineno}: HTTP {node.attr.upper()}")
    return offences


def scan_arm_in_request_body(source: str) -> list[str]:
    """``arm_id`` in an enrolment request body is arm shopping with extra steps."""

    offences: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        for forbidden in ("arm_id", "episode_key", "slot", "stratum_id", "block_ordinal"):
            if forbidden in keys:
                offences.append(f"line {node.lineno}: request body carries {forbidden!r}")
    return offences


# --------------------------------------------------------------------------------------
# 1 + 2 — enroll takes the work, and there is no re-roll
# --------------------------------------------------------------------------------------


def test_enroll_takes_the_work_and_never_the_arm() -> None:
    parser = _parser()
    assert _accepts(parser, ENROLL_ARGV), f"the runbook's own enrol line was rejected: {ENROLL_ARGV}"
    for flag in FORBIDDEN_ENROLL_FLAGS:
        assert not _accepts(parser, [*ENROLL_ARGV, flag, "tce_assisted"]), (
            f"enroll accepts {flag}: the caller can reach into the allocation, and the arm is no longer frozen "
            "before it is revealed"
        )


def test_the_election_branch_is_the_only_way_to_name_an_arm() -> None:
    """``--elect`` exists, is separate, and is the descriptive-only branch (§1.3, D-4)."""

    parser = _parser()
    assert _accepts(parser, [*ENROLL_ARGV, "--elect", "owner_unassisted"]), (
        "--elect is how the owner records work he did himself; without it Claim B has no rows at all"
    )
    # ``--elect`` names a WORKFLOW, not one of the randomised arms. Refusing a randomised value
    # here is server-side (``elect_arm`` raises), so the CLI is only required to keep the two
    # branches distinct: an election must not be reachable from the ordinary enrol line.
    assert not _accepts(parser, [*ENROLL_ARGV, "--elect"]), "--elect must take a value; a bare flag would elect by accident"


def test_no_subcommand_undoes_an_allocation_or_an_adjudication() -> None:
    parser = _parser()
    assert _accepts(parser, ENROLL_ARGV) and _accepts(parser, CLOSE_ARGV), "enroll and close are the two commands"
    for name in FORBIDDEN_SUBCOMMANDS:
        assert not _accepts(parser, [name]), f"{name!r} is a subcommand; an allocation and an adjudication are append-only"

    source = _cli_source()
    assert not scan_mutating_calls(source), scan_mutating_calls(source)
    assert not scan_arm_in_request_body(source), scan_arm_in_request_body(source)


def test_the_mutation_scanners_discriminate() -> None:
    """The twins.  Both scanners above are only evidence if they can see a violation."""

    assert scan_mutating_calls('SQL = "DELETE FROM pilot_episodes WHERE id = %s"')
    assert scan_mutating_calls('SQL = "UPDATE pilot_episodes SET arm_id = %s WHERE id = %s"')
    assert scan_mutating_calls("resp = client.delete(url)")
    assert not scan_mutating_calls('resp = client.post(url, json={"project_id": p})')

    assert scan_arm_in_request_body('body = {"arm_id": "tce_assisted", "project_id": p}')
    assert not scan_arm_in_request_body('body = {"project_id": p, "decision_family": f}')


# --------------------------------------------------------------------------------------
# 3 — close is the adjudication, and it needs a verified human
# --------------------------------------------------------------------------------------


def test_close_carries_the_adjudication_the_runbook_promises() -> None:
    parser = _parser()
    assert _accepts(parser, CLOSE_ARGV), f"the runbook's own close line was rejected: {CLOSE_ARGV}"
    # The two human_attested components of the success composite must both be sayable.
    for flag, value in (("--review-verdict", "rejected"), ("--rescue", "took_over")):
        argv = [*CLOSE_ARGV]
        argv[argv.index(flag) + 1] = value
        assert _accepts(parser, argv), f"close cannot record {flag}={value}; the honest answer is unsayable"


def test_close_uses_the_host_capture_credential() -> None:
    """The adjudicator is a verified human, which on this host means the host-capture token."""

    source = _cli_source()
    assert "host_capture" in source, (
        "the close path names no host-capture credential. Closing an episode requires a verified human, "
        "which means the host-capture token file and NOT the MCP executor and NOT a bare API token "
        "(runbook precondition 5)."
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tce_mcp"):
            raise AssertionError(f"line {node.lineno}: the CLI imports the MCP layer; the executor must not be able to close an episode")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("tce_mcp"), f"line {node.lineno}: the CLI imports {alias.name}"


# --------------------------------------------------------------------------------------
# 4 — it refuses to run against nothing
# --------------------------------------------------------------------------------------


def test_the_cli_refuses_without_a_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _cli_module()
    monkeypatch.delenv("TCE_API_BASE_URL", raising=False)
    try:
        result = module.main(ENROLL_ARGV)
    except SystemExit as exit_signal:
        code = exit_signal.code
        result = code if isinstance(code, int) else 1
    except Exception as error:  # noqa: BLE001 - a traceback IS the failure being asserted against
        pytest.fail(f"the CLI raised {type(error).__name__} instead of exiting 2: {error}")
    assert int(result) == 2, "with no TCE_API_BASE_URL the CLI must exit 2, the same refusal p4_qualification_report.py uses"


def test_the_runbook_transcripts_are_not_silently_empty() -> None:
    """Non-vacuity: the two argv lines above are the runbook's, and they carry real flags."""

    assert ENROLL_ARGV[0] == "enroll" and "--objective" in ENROLL_ARGV
    assert CLOSE_ARGV[0] == "close" and "--executed-arm" in CLOSE_ARGV and "--review-verdict" in CLOSE_ARGV
    assert FORBIDDEN_ENROLL_FLAGS and FORBIDDEN_SUBCOMMANDS
