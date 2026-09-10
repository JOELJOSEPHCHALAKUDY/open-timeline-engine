"""G7's live half: run all sixteen assertions for real, against this host's kernel.

This is the only test in the tree that measures whether the sandbox actually holds.  Everything
else asserts the *shape* of the measurement — that the names are frozen, that vacuity counts as
failure, that a failing measurement refuses.  Those are worth having and they are not this.

It is opt-in twice over: ``pytest.mark.e2e`` plus ``pytest.mark.subprocess``, and an in-test skip
unless ``sys.platform == "darwin"`` and ``TCE_SUP_SANDBOX_LIVE=1``.  Belt and braces, because CI
selects suites by path rather than by marker, and a run of these assertions spawns roughly forty
sandboxed processes including two of the real runtime.

To run it::

    TCE_SUP_SANDBOX_LIVE=1 \\
    TCE_SUP_TASK_ROOT=/private/tmp/tce-selftest \\
    TCE_SUP_PYTHON_BINARY="$(readlink -f .venv/bin/python3)" \\
    TCE_SUP_PYTHON_ROOT=... \\
    pytest tests/e2e/test_sandbox_selftest_live.py -q

What it does NOT prove, stated because the distinction is the whole point of the ``vacuous`` flag:
sixteen passes mean the sixteen things measured held on this kernel, on this day, for a process
tree started this way.  The profile was derived by clause-removal bisection because this host
exposes no sandbox denial telemetry to a non-root session, and bisection proves every shipped clause
is load-bearing without proving that no clause is missing for a code path the lab never exercised.
"""

from __future__ import annotations

import os
import sys

import pytest
from tce_supervisor import sandbox
from tce_supervisor.config import get_supervisor_settings
from tce_supervisor.supervise import default_self_test_charter

pytestmark = [pytest.mark.e2e, pytest.mark.subprocess]


def _skip_unless_live() -> None:
    if sys.platform != "darwin":
        pytest.skip("the Seatbelt profile is macOS-only; there is nothing to measure here")
    if not os.getenv("TCE_SUP_SANDBOX_LIVE"):
        pytest.skip("set TCE_SUP_SANDBOX_LIVE=1 to run the live sandbox measurement")
    if not os.path.isfile(sandbox.SANDBOX_EXEC_BINARY):
        pytest.skip(f"{sandbox.SANDBOX_EXEC_BINARY} is not present")


def test_seatbelt_denials_hold_on_this_host() -> None:
    _skip_unless_live()
    settings = get_supervisor_settings()
    result = sandbox.run_self_test(default_self_test_charter(settings), settings=settings)

    assert [row["name"] for row in result["assertions"]] == list(sandbox.SELF_TEST_ASSERTION_NAMES)
    vacuous = [row["name"] for row in result["assertions"] if row["vacuous"]]
    assert vacuous == [], f"these assertions measured nothing: {vacuous}"
    failed = [(row["name"], row["observed"]) for row in result["assertions"] if not row["passed"]]
    assert failed == [], failed
    assert result["passed"] is True

    # Recorded, never asserted as separation: this host has no service account, so the supervisor
    # runs as the same uid as the manager. A True here would mean the host changed, or the value is
    # being claimed rather than measured.
    assert result["uid_separation"] is False
    assert result["mach_lookup_allowlist"] == "underived"
    assert result["profile_digest"]


def test_every_declared_prohibition_is_attempted_and_denied() -> None:
    """A charter whose prohibitions have never been attempted is a document, not a control."""
    _skip_unless_live()
    settings = get_supervisor_settings()
    result = sandbox.probe_denials(default_self_test_charter(settings), settings=settings)

    probes = result["probes"]
    assert probes, "no probes ran; the assertion would be vacuous"
    names = {probe["name"] for probe in probes}
    # Every prohibition the operator-facing documentation names must appear here by name.
    assert "write_outside_task_clone" in names
    assert "read_ssh_private_key" in names
    assert "read_runtime_ambient_config" in names
    assert "connect_docker_daemon_socket" in names
    assert "nest_permissive_profile" in names
    assert "rewrite_rendered_profile" in names
    assert {f"connect_enforcement_port:{port}" for port in sandbox.ENFORCEMENT_PORTS} <= names
    # EVERY protected prefix, not just the first: that was a real defect, not a hypothetical one.
    assert len([name for name in names if name.startswith("write_protected_prefix:")]) == 3

    vacuous = [probe["name"] for probe in probes if probe["vacuous"]]
    assert vacuous == [], f"these probes measured nothing: {vacuous}"
    allowed = [(probe["name"], probe["observed"]) for probe in probes if not probe["denied"]]
    assert allowed == [], allowed


def test_the_agent_cannot_rewrite_the_profile_it_runs_under() -> None:
    """F10, isolated: the specific attack that made rendering-inside-the-taskdir a blocker."""
    _skip_unless_live()
    settings = get_supervisor_settings()
    result = sandbox.probe_denials(default_self_test_charter(settings), settings=settings)
    probe = next(row for row in result["probes"] if row["name"] == "rewrite_rendered_profile")
    assert probe["denied"] is True, probe["observed"]
    assert probe["vacuous"] is False
