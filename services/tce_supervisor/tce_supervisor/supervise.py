"""The dispatch sequence, the reconcile, and the command line.

The order in :func:`dispatch_once` is normative, and three properties of it are the reason this
module exists at all:

1. **Reconcile before anything else.**  A stopped service must not silently restart work.  The
   startup reconcile runs once per process, before the first dispatch, and refuses to proceed on
   error.
2. **No charter, no dispatch.**  The charter is resolved before the sandbox is even rendered, and a
   missing, expired or revoked one is a refusal, not a warning.
3. **The profile digest is re-verified before EVERY exec** — the dispatch, every resume, every
   retry, and the verifier run.  Wiring it at one call site and not the others reopens the exact
   attack it exists to close: an agent that rewrites the profile it runs under gets a second,
   unbounded run that still reports itself sandboxed.

**A kill is never an interrupt.**  On a surface whose ``interrupt`` is ``UNSUPPORTED``,
:func:`interrupt_once` refuses and journals the refusal rather than reaching for a signal.  The
supervisor does kill — for the wall clock, and for a charter revoked mid-run — and that kill
resolves the root effect to ``unknown``, never to ``failed``.  We killed it; we do not know what it
did; the pause fires.

**A residual this module mitigates rather than solves.**  If the supervisor itself dies, the
sandboxed child is orphaned and keeps writing to its clone while the reconcile resolves its effect
to ``unknown`` — the journal then records "we do not know" about a process that is demonstrably
still running.  Tier 1 has no hard kill boundary and Tier 2 cannot host the agent on this host.
Before resolving to ``unknown``, :func:`startup_reconcile` checks whether the task directory's pid
file still names a live process and records ``{"orphan_live": true, "pid": ...}`` so an operator
can see the difference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from tce_shared import budget
from tce_shared.charter import (
    CHARTER_POLICY_REVISION,
    CHARTER_SCHEMA_VERSION,
    DEFAULT_DENIED_READ_PATHS,
    DEFAULT_PROTECTED_WRITE_PREFIXES,
    CharterCaps,
    ResolvedCharter,
)
from tce_shared.effect_journal import DEFAULT_REVERSIBILITY_BY_KIND, EffectIntent, effect_intent_digest, effect_intent_to_json
from tce_shared.runtime_contract import (
    RuntimeAdapter,
    RuntimeEvent,
    RuntimeResult,
    StartRequest,
    UnsupportedVerb,
    require_verb,
)
from tce_shared.verification import DECIDING_FILE_PATHS, build_corpus_manifest

from . import sandbox, verifier
from . import taskdir as taskdir_mod
from .adapters import build_adapter
from .apiclient import ApiClient, ApiRefusal
from .config import SupervisorSettings, get_supervisor_settings

PAUSE_PREFIX: str = "AUTONOMOUS MODE PAUSED"
_CHARTER_RECHECK_SECONDS: float = 5.0

# Set once per process by startup_reconcile. dispatch_once refuses to run before it.
_RECONCILED: bool = False


class SupervisorRefusal(RuntimeError):
    """A refusal with a machine-readable reason. Never a silent degradation."""

    reason: str
    detail: Mapping[str, Any]

    def __init__(self, reason: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{reason}: {json.dumps(dict(detail or {}), default=str)}")
        self.reason = reason
        self.detail = dict(detail or {})


class SupervisorPause(RuntimeError):
    """An authorization pause, never a transport error.

    A 409 whose detail starts with "AUTONOMOUS MODE PAUSED" means the manager has withdrawn
    permission — the capture channel is down, or there is no active charter, or an effect from a
    previous run is unresolved. Retrying it as a transport failure would be exactly wrong.
    """

    message: str
    directive_id: str

    def __init__(self, message: str, directive_id: str) -> None:
        super().__init__(message)
        self.message = message
        self.directive_id = directive_id


def reset_process_state() -> None:
    """Clear the once-per-process reconcile flag. For tests and for a long-lived daemon restart."""
    global _RECONCILED
    _RECONCILED = False


# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------


def _reversibility(value: str) -> Literal["reversible", "irreversible", "unknown"]:
    """Narrow a REVERSIBILITY member to the literal type. Anything unrecognised is "unknown".

    Failing open to "reversible" here would make a crashed effect look safe to retry, which is the
    one direction this value must never guess in.
    """
    if value == "reversible":
        return "reversible"
    if value == "irreversible":
        return "irreversible"
    return "unknown"


def _record(recorder: list[str] | None, name: str) -> None:
    if recorder is not None:
        recorder.append(name)


def _is_pause(refusal: ApiRefusal) -> str:
    for value in (refusal.detail.get("message"), refusal.detail.get("detail"), refusal.detail.get("error")):
        if isinstance(value, str) and value.startswith(PAUSE_PREFIX):
            return value
    blob = json.dumps(refusal.detail, default=str)
    marker = blob.find(PAUSE_PREFIX)
    return blob[marker : marker + 300].rstrip('"') if marker >= 0 else ""


def _pid_file(root: str) -> str:
    return os.path.join(root, "logs", "supervisor.pid")


def _write_pid_file(root: str) -> None:
    path = _pid_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def _orphan_evidence(root: str) -> dict[str, Any]:
    """Is a process still holding this task directory? Records the difference honestly.

    ``{"orphan_live": true}`` means the reconcile is about to record "we do not know" about a
    process that is still running and still mutating its clone. That is a different operational
    situation from a finished run whose outcome was lost, and an operator must be able to tell.
    """
    path = _pid_file(root)
    try:
        with open(path, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return {}
    if pid == os.getpid():
        return {}
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return {}
    return {"orphan_live": True, "pid": pid}


def default_self_test_charter(settings: SupervisorSettings) -> ResolvedCharter:
    """The charter the ``selftest`` and ``probe-denials`` verbs measure against.

    It is not an authority: it is never approved, never stored and grants nothing. It exists so the
    boot-time measurement can run before any charter exists, using the same defaults a real charter
    would carry. ``credential_risk_acknowledged`` is True here only because this object never
    authorises a dispatch.
    """
    now = datetime.now(UTC)
    return ResolvedCharter(
        charter_id="selftest",
        workspace_id=settings.workspace_id,
        owner_id=settings.user_id or "selftest",
        project_id=None,
        charter_version="selftest",
        policy_revision=CHARTER_POLICY_REVISION,
        status="active",
        enforcement_tier="os_sandbox",
        permitted_roots=("src/",),
        protected_write_prefixes=DEFAULT_PROTECTED_WRITE_PREFIXES,
        denied_read_paths=DEFAULT_DENIED_READ_PATHS,
        permitted_capabilities=frozenset({"filesystem.read", "filesystem.write", "process.execute"}),
        confirm_required_capabilities=frozenset(),
        egress_mode="https_only",
        runtime_allowlist=(),
        caps=CharterCaps(
            max_attempts=1,
            max_concurrent_dispatches=1,
            max_wall_seconds=settings.dispatch_wall_seconds,
            budget_minor_units=0,
            budget_currency="USD",
            spend_enforcement="unsupported",
        ),
        approved_by="selftest",
        approved_at=now,
        expires_at=now,
        revoked_at=None,
        narrowing_ids=(),
        charter_digest="",
        schema_version=CHARTER_SCHEMA_VERSION,
        credential_risk_acknowledged=True,
    )


# ------------------------------------------------------------------------------------------
# the startup reconcile
# ------------------------------------------------------------------------------------------


def startup_reconcile(*, settings: SupervisorSettings, api: ApiClient) -> dict[str, int]:
    """Resolve every effect left open by a previous process, by READING rather than guessing.

    Step order matters: for an effect that carries a ``provider_run_id`` we ask the runtime what
    happened to that run. Only when the provider has no record does the effect resolve to
    ``unknown`` with ``resolution_source="reaper"``. ``unknown`` is not a failure verdict; it is the
    absence of one, and it is what makes the downstream pause fire instead of a silent retry.
    """
    global _RECONCILED
    counts = {"examined": 0, "resolved_provider_read": 0, "resolved_unknown": 0, "orphan_live": 0}
    open_effects = api.list_open_effects(settings.session_id)
    adapters: dict[str, RuntimeAdapter] = {}
    for row in open_effects:
        counts["examined"] += 1
        effect_id = str(row.get("effect_id") or "")
        if not effect_id:
            continue
        lease = int(row.get("lease_generation") or 0)
        provider_run_id = str(row.get("provider_turn_id") or row.get("provider_run_id") or "")
        surface = str(row.get("surface") or settings.default_runtime_surface)
        record: RuntimeResult | None = None
        if provider_run_id:
            adapter = adapters.get(surface)
            if adapter is None:
                try:
                    adapter = build_adapter(surface, settings=settings)
                except KeyError:
                    adapter = None
                if adapter is not None:
                    adapters[surface] = adapter
            if adapter is not None:
                record = adapter.read_provider_record(provider_run_id)
        directive_root = os.path.join(os.path.abspath(os.path.expanduser(settings.task_root)), str(row.get("directive_id") or ""))
        orphan = _orphan_evidence(directive_root)
        if orphan:
            counts["orphan_live"] += 1
        if record is not None:
            target = "confirmed" if record.outcome == "succeeded" else "failed"
            evidence: dict[str, Any] = {"provider_run_id": provider_run_id, "outcome": record.outcome, "terminal_reason": record.terminal_reason}
            evidence.update(orphan)
            api.resolve_effect(effect_id, target_state=target, resolution_source="provider_read", expected_lease=lease, evidence=evidence)
            counts["resolved_provider_read"] += 1
            continue
        evidence = {"reason": "provider has no record for this run", "provider_run_id": provider_run_id or None}
        evidence.update(orphan)
        api.resolve_effect(effect_id, target_state="unknown", resolution_source="reaper", expected_lease=lease, evidence=evidence)
        counts["resolved_unknown"] += 1
    _RECONCILED = True
    return counts


# ------------------------------------------------------------------------------------------
# dispatch
# ------------------------------------------------------------------------------------------


def _open_child_effect(api: ApiClient, *, directive_id: str, event: RuntimeEvent, tier: str, lease: int, provider_run_id: str, provider_turn_id: str | None) -> None:
    """Open, run and confirm one observed child effect.

    ``action_tracing="observed"`` is claimed ONLY here, for an effect the adapter individually saw.
    Everything a shell one-liner does inside such a command is invisible and its own row would say
    ``unavailable`` — the boundary in force, named, rather than a tracing claim nobody can honour.
    """
    kind = "command" if event.kind == "command" else "write"
    capability = "process.execute" if kind == "command" else "filesystem.write"
    params = event.raw.get("params")
    params_map: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
    item = params_map.get("item") if isinstance(params_map.get("item"), Mapping) else event.raw.get("item")
    item_map: Mapping[str, Any] = item if isinstance(item, Mapping) else {}
    resource = str(item_map.get("path") or item_map.get("command") or item_map.get("cwd") or event.kind)
    argv_raw = item_map.get("argv")
    argv = tuple(str(part) for part in argv_raw) if isinstance(argv_raw, (list, tuple)) else ()
    intent = EffectIntent(
        kind="command" if kind == "command" else "write",
        capability=capability,
        resource=resource,
        argv=argv,
        # Commands default to "unknown", not "reversible": a shell one-liner may be `git push`.
        reversibility="reversible" if kind == "write" else _reversibility(DEFAULT_REVERSIBILITY_BY_KIND["command"]),
        description=f"{event.kind} observed on seq {event.seq}",
    )
    body = {
        "directive_id": directive_id,
        "intent": effect_intent_to_json(intent),
        "intent_digest": effect_intent_digest(intent),
        "enforcement_tier": tier,
        "action_tracing": "observed",
        "lease_generation": lease,
        "provider_run_id": provider_run_id,
        "provider_turn_id": provider_turn_id,
        "state": "prepared",
    }
    opened = api.open_effect(body)
    effect_id = str(opened.get("effect_id") or "")
    if not effect_id:
        return
    api.resolve_effect(effect_id, target_state="running", resolution_source="runtime_result", expected_lease=lease, evidence={"seq": event.seq})
    api.resolve_effect(effect_id, target_state="confirmed", resolution_source="runtime_result", expected_lease=lease, evidence={"seq": event.seq, "raw_kind": event.kind})


def dispatch_once(*, directive_id: str, surface: str | None, settings: SupervisorSettings, api: ApiClient, recorder: list[str] | None = None) -> dict[str, Any]:
    """Run one directive end to end.

    ``recorder`` is a testing seam and nothing else: it collects the names of the ordered steps so
    a test can assert the order even when a refusal aborts the run. Production callers omit it.
    """
    started_wall = time.monotonic()

    # 1 -- reconcile first, once per process, refusing to proceed on error.
    if not _RECONCILED:
        _record(recorder, "startup_reconcile")
        try:
            startup_reconcile(settings=settings, api=api)
        except ApiRefusal as refusal:
            raise SupervisorRefusal("reconcile_pending", {"status": refusal.status_code, "error": refusal.error}) from refusal

    # 2 -- the charter, before the sandbox is even rendered.
    _record(recorder, "get_active_charter")
    charter = api.get_active_charter()
    now = datetime.now(UTC)
    if charter is None:
        raise SupervisorRefusal("no_active_charter", {"directive_id": directive_id})
    if not charter.is_active(now):
        raise SupervisorRefusal("no_active_charter", {"directive_id": directive_id, "status": charter.status, "revoked": charter.revoked_at is not None})

    # Provision BEFORE the measurement. Deliberate, and a deviation from the numbered order in the
    # design: the rendered profile's protected-write clauses carry absolute paths, so a profile
    # rendered for a throwaway directory has a different sha256 from the one this dispatch will run
    # under -- and both backends refuse `open_dispatch` when the self-test row's digest does not
    # match the request's. Measuring a different file than the one you run proves nothing about the
    # one you run, so the self-test measures the real one.
    task = taskdir_mod.provision(task_root=settings.task_root, directive_id=directive_id, repo_path=settings.repo_path, git_binary=settings.git_binary)
    _write_pid_file(task.root)

    # 3 -- render, measure, and bind the tier to the argv that will actually run.
    rendered = sandbox.render_profile(charter, task_root=settings.task_root, taskdir=task.root, template_path=settings.profile_template_path)
    _record(recorder, "sandbox_self_test")
    self_test = sandbox.run_self_test(charter, settings=settings, taskdir=task)
    try:
        posted = api.post_self_test(self_test)
    except ApiRefusal:
        posted = {}
    self_test_id = str(posted.get("self_test_id") or self_test.get("self_test_id") or "")
    plan = sandbox.wrapper_argv(charter.enforcement_tier, rendered, task, charter, settings=settings, self_test_id=self_test_id)
    if settings.sandbox_self_test_required:
        tier = sandbox.select_tier(charter, self_test, plan)
    else:
        tier = charter.enforcement_tier

    # 4 -- the runtime surface, explicitly selected and version-pinned.
    chosen = surface or settings.default_runtime_surface
    adapter = build_adapter(chosen, settings=settings)
    descriptor = adapter.describe()
    if not descriptor.available:
        raise SupervisorRefusal("runtime_unavailable", {"surface": chosen, "reason": descriptor.unavailable_reason})
    if not charter.allows_runtime(descriptor.runtime_id, descriptor.runtime_version, chosen):
        # An EMPTY runtime_allowlist permits nothing. That is fail-closed and deliberate: a charter
        # that names no runtime has not authorised one.
        raise SupervisorRefusal("runtime_not_permitted", {"surface": chosen, "runtime": descriptor.runtime_id, "version": descriptor.runtime_version, "allowlist": list(charter.runtime_allowlist)})
    for verb in ("start", "observe", "read_result"):
        try:
            require_verb(chosen, verb)
        except UnsupportedVerb as error:
            raise SupervisorRefusal("unsupported_verb", {"surface": chosen, "verb": verb, "message": str(error)}) from error

    # 5 -- freeze the criteria BEFORE any child process exists.
    env = taskdir_mod.child_environment(task, charter)
    taskdir_mod.write_child_env_file(task, env)

    tokens = verifier.verification_tokens(_directive_meta(api, directive_id))
    checks = verifier.build_checks(tokens, settings=settings, tier=str(settings.verification_tier or "container"))
    if not checks:
        raise SupervisorRefusal("criteria_empty", {"tokens": list(tokens)})
    manifest = build_corpus_manifest(task.clone, tuple(charter.protected_write_prefixes) + DECIDING_FILE_PATHS)
    criteria = api.freeze_acceptance_criteria(directive_id=directive_id, checks=checks, corpus_manifest=manifest, charter_id=charter.charter_id)

    # 6 -- the provider idempotency key is chosen HERE, before the spawn. A crash between the row
    # and the spawn is then resolvable by asking the runtime about that id.
    provider_run_id = str(uuid.uuid4())
    dispatch = api.open_dispatch(
        {
            "directive_id": directive_id,
            "surface": chosen,
            "runtime_id": descriptor.runtime_id,
            "runtime_version": descriptor.runtime_version,
            "contract_digest": descriptor.contract_digest,
            "enforcement_tier": tier,
            "sandbox_profile_digest": plan.profile_digest,
            "sandbox_self_test_id": self_test_id,
            "wrapper_argv0": plan.argv[0] if plan.argv else "",
            "provider_run_id": provider_run_id,
            "charter_id": charter.charter_id,
            "task_family": "unspecified",
        },
        idempotency_key=f"dispatch:{directive_id}:{provider_run_id}",
    )
    dispatch_id = str(dispatch.get("dispatch_id") or "")

    # 7 -- read the reservation BACK. The supervisor never computes it: the concurrency property
    # requires the sum and the INSERT to share one transaction, which only the backend can do.
    cap = dispatch.get("cap_applied")
    max_budget_minor: int | None = None
    if isinstance(cap, Mapping) and cap.get("max_budget_usd") is not None:
        max_budget_minor = int(round(float(cap["max_budget_usd"]) * 100))

    # 8 -- claim. A pause is an authorization decision, not a transport failure.
    try:
        claim = api.claim(directive_id, lease_generation=None)
    except ApiRefusal as refusal:
        pause = _is_pause(refusal)
        if refusal.status_code == 409 and pause:
            raise SupervisorPause(pause, directive_id) from refusal
        raise
    lease = int(claim.get("lease_generation") or 0)

    # 9 -- the root effect, opened BEFORE the spawn.
    root_intent = EffectIntent(
        kind="directive",
        capability="process.execute",
        resource=task.clone,
        argv=tuple(plan.argv),
        reversibility=_reversibility(DEFAULT_REVERSIBILITY_BY_KIND["directive"]),
        description=f"dispatch of {directive_id} on {chosen}",
    )
    root = api.open_effect(
        {
            "directive_id": directive_id,
            "intent": effect_intent_to_json(root_intent),
            "intent_digest": effect_intent_digest(root_intent),
            "enforcement_tier": tier,
            # D-3: one agent tool call that runs a script is ONE observation. The root effect
            # covers the whole run, so nothing about it is individually observed.
            "action_tracing": "unavailable",
            "lease_generation": lease,
            "provider_run_id": provider_run_id,
            "provider_turn_id": None,
            "state": "prepared",
            "dispatch_id": dispatch_id,
        }
    )
    root_effect_id = str(root.get("effect_id") or "")

    # 10 -- re-hash the profile IMMEDIATELY before the exec, then start.
    _record(recorder, "verify_profile")
    sandbox.verify_profile(rendered)
    wall_seconds = min(int(charter.caps.max_wall_seconds or settings.dispatch_wall_seconds), int(settings.dispatch_wall_seconds))
    _record(recorder, "start")
    handle = adapter.start(
        StartRequest(
            task_dir=task.root,
            prompt=_prompt_for(directive_id, api),
            provider_run_id=provider_run_id,
            env=env,
            argv_prefix=tuple(plan.argv),
            max_budget_minor_units=max_budget_minor,
            wall_seconds=wall_seconds,
        )
    )
    if root_effect_id:
        api.resolve_effect(root_effect_id, target_state="running", resolution_source="runtime_result", expected_lease=lease, evidence={"handle": handle})

    # 11 -- observe.
    outcome_override: str | None = None
    human_intervention_count = 0
    provider_turn_id: str | None = None
    deadline = time.monotonic() + wall_seconds
    last_charter_check = time.monotonic()
    pause_message = ""
    for event in adapter.observe(handle):
        if event.provider_turn_id and provider_turn_id is None:
            provider_turn_id = event.provider_turn_id
            if dispatch_id:
                try:
                    api.bind_provider_run(dispatch_id, provider_run_id, provider_turn_id)
                except ApiRefusal:
                    pass
        if event.kind in {"command", "file_change"}:
            try:
                _open_child_effect(api, directive_id=directive_id, event=event, tier=tier, lease=lease, provider_run_id=provider_run_id, provider_turn_id=provider_turn_id)
            except ApiRefusal:
                pass
        elif event.kind == "approval":
            human_intervention_count += 1

        if time.monotonic() > deadline:
            adapter.kill(handle, "wall_clock_exceeded")
            outcome_override = "wall_clock_exceeded"
            break
        # U3: a charter revoked mid-run stops the run. The re-check is throttled, so a revocation
        # is noticed within this window rather than instantly -- stated, not implied.
        if time.monotonic() - last_charter_check >= _CHARTER_RECHECK_SECONDS:
            last_charter_check = time.monotonic()
            live = api.get_active_charter()
            if live is None or not live.is_active(datetime.now(UTC)) or live.charter_id != charter.charter_id:
                pause_message = f"{PAUSE_PREFIX}: the authority charter was revoked or replaced while the run was in flight"
                try:
                    # A surface WITH a real interrupt gets one. A surface without it is killed --
                    # and that kill is recorded as `unknown`, three lines below, never as `failed`.
                    adapter.interrupt(handle, "charter_revoked")
                except (RuntimeError, OSError):
                    adapter.kill(handle, "charter_revoked")
                outcome_override = "charter_revoked"
                break

    # 12 -- the result, and the root effect resolved against it.
    result = adapter.read_result(handle)
    if outcome_override is not None:
        # A killed or revoked run is UNKNOWN, never failed.
        root_state, resolution_source = "unknown", "reaper"
    elif result.outcome == "succeeded":
        root_state, resolution_source = "confirmed", "runtime_result"
    elif result.outcome in {"unknown", "interrupted"}:
        root_state, resolution_source = "unknown", "runtime_result"
    else:
        root_state, resolution_source = "failed", "runtime_result"
    if root_effect_id:
        api.resolve_effect(
            root_effect_id,
            target_state=root_state,
            resolution_source=resolution_source,
            expected_lease=lease,
            evidence={"outcome": result.outcome, "terminal_reason": outcome_override or result.terminal_reason, "is_error": result.is_error},
        )

    # 13 -- report through the EXISTING transport, which enqueues the handoff in one transaction.
    report_state = "succeeded" if result.outcome == "succeeded" and outcome_override is None else "failed"
    try:
        api.report(
            directive_id,
            state=report_state,
            lease_generation=lease,
            idempotency_key=f"report:{directive_id}:{provider_run_id}",
            milestone={"terminal_reason": outcome_override or result.terminal_reason, "surface": chosen, "provider_run_id": provider_run_id},
        )
    except ApiRefusal as refusal:
        pause = _is_pause(refusal)
        if refusal.status_code == 409 and pause:
            raise SupervisorPause(pause, directive_id) from refusal
        raise

    # 14 -- verify. Re-hash the profile again first: the verifier is another exec.
    _record(recorder, "verify_profile")
    sandbox.verify_profile(rendered)
    verification: dict[str, Any] = {}
    try:
        report = verifier.run(criteria, task, settings=settings, charter=charter)
        _record(recorder, "verifier_run")
        verification = api.record_verification(verifier.to_evidence(report, directive_id=directive_id))
    except verifier.VerifierUnavailable as error:
        # An unrunnable verification stays `unverified`. It never becomes a pass, and the reason is
        # recorded rather than swallowed.
        verification = {"skipped": True, "reason": error.reason}
    except ApiRefusal as refusal:
        # F1: `/v1/verification/results` now refuses a runner whose identity is not server-bound,
        # so an unprovisioned -- or provisioned-but-unbound -- TCE_SUP_VERIFIER_TOKEN answers
        # `403 unknown_runner` where it used to record an `inconclusive` verdict. That is a
        # CONFIGURATION gap, not a supervision failure, and it must not abort the turn: step 13
        # has already reported the execution and step 15 still owes the spend reconciliation.
        # Treat it exactly like an unrunnable verifier -- `verification_state` stays `unverified`,
        # P2's R8 keeps the task out of DONE, and the reason names the missing credential.
        if refusal.status_code != 403 or refusal.error != "unknown_runner":
            raise
        verification = {"skipped": True, "reason": f"unknown_runner:{refusal.detail.get('reason') or 'identity_unverified'}"}

    # 15 -- reconcile the spend. The supervisor computes the RECONCILIATION, never the reservation.
    reconciliation = budget.reconcile(
        surface=chosen,
        result_tokens={
            "input": result.tokens_input,
            "output": result.tokens_output,
            "cached_input": result.tokens_cached_input,
            "reasoning": result.tokens_reasoning,
            "is_error": int(result.is_error),
        },
        provider_cost_minor_units=result.cost_minor_units,
        price_table=settings.token_price_table,
        model_id=descriptor.model_id,
        reservation=budget.reservation_from_json({"reserved_minor_units": max_budget_minor or 0, "currency": "USD", "spend_enforcement": budget.spend_enforcement_for(chosen)}),
    )
    if dispatch_id:
        api.reconcile_dispatch(
            dispatch_id,
            reconciliation=reconciliation,
            outcome=outcome_override or result.outcome,
            terminal_reason=outcome_override or result.terminal_reason,
            wall_ms=int((time.monotonic() - started_wall) * 1000),
            # Structurally 0 on codex/app-server: approvalPolicy "never" means the callbacks never
            # fire. It is a floor, not a count. The real producer is claude's permission_denials[].
            human_intervention_count=human_intervention_count,
        )

    if pause_message:
        raise SupervisorPause(pause_message, directive_id)

    return {
        "directive_id": directive_id,
        "dispatch_id": dispatch_id,
        "surface": chosen,
        "enforcement_tier": tier,
        "profile_digest": rendered.digest,
        "provider_run_id": provider_run_id,
        "provider_turn_id": provider_turn_id,
        "outcome": outcome_override or result.outcome,
        "terminal_reason": outcome_override or result.terminal_reason,
        "human_intervention_count": human_intervention_count,
        "verification": verification,
        "call_order": list(recorder or []),
    }


def _directive_meta(api: ApiClient, directive_id: str) -> Mapping[str, Any]:
    """Best-effort read of the directive's ``meta``. Only its TOKENS are ever used."""
    try:
        status = api.get_execution_status(api.session_id)
    except ApiRefusal:
        return {}
    for key in ("pending_execution", "directive", "latest_directive"):
        row = status.get(key)
        if isinstance(row, Mapping) and str(row.get("directive_id") or "") == directive_id:
            meta = row.get("meta")
            if isinstance(meta, Mapping):
                return meta
    return {}


def _prompt_for(directive_id: str, api: ApiClient) -> str:
    """The objective text the agent is given.

    It comes from the directive's own record, never from a local file, so what the agent is asked to
    do is the thing the manager recorded that it was asked to do.
    """
    try:
        status = api.get_execution_status(api.session_id)
    except ApiRefusal:
        return f"Work the TCE directive {directive_id}."
    for key in ("pending_execution", "directive", "latest_directive"):
        row = status.get(key)
        if isinstance(row, Mapping) and str(row.get("directive_id") or "") == directive_id:
            objective = row.get("objective") or row.get("note") or ""
            if objective:
                return str(objective)
    return f"Work the TCE directive {directive_id}."


# ------------------------------------------------------------------------------------------
# resume and interrupt
# ------------------------------------------------------------------------------------------


def resume_once(*, directive_id: str, prompt: str, settings: SupervisorSettings, api: ApiClient) -> dict[str, Any]:
    """Re-validate the charter and the profile, then refuse — because this build cannot resume.

    A resume re-uses the same task directory, which makes it precisely the second exec that the
    profile-rewrite attack targets, so the two checks that guard it run first and for real: the
    charter is re-resolved and the rendered profile is re-hashed.

    **Then it refuses, and that is the honest answer rather than a stub that returns success.**
    Resuming needs a live adapter handle, and a handle does not survive the process that made it.
    Both runtimes CAN be resumed cross-process — codex by ``thread/resume`` on the stored
    ``thread_id``, claude by ``--resume`` on the stored ``session_id`` — but this build does not
    reconnect to a stored provider id, so the verb would be a claim with no mechanism behind it.
    ``docs/supervisor.md`` is where that gap belongs; a ``{"resumed": false}`` success body is not.
    """
    charter = api.get_active_charter()
    if charter is None or not charter.is_active(datetime.now(UTC)):
        raise SupervisorRefusal("no_active_charter", {"directive_id": directive_id})
    plan_dirs = taskdir_mod.plan(task_root=settings.task_root, directive_id=directive_id)
    rendered = sandbox.render_profile(charter, task_root=settings.task_root, taskdir=plan_dirs.root, template_path=settings.profile_template_path)
    sandbox.verify_profile(rendered)
    raise SupervisorRefusal(
        "resume_requires_live_handle",
        {
            "directive_id": directive_id,
            "profile_digest": rendered.digest,
            "prompt_bytes": len(prompt.encode()),
            "message": "the charter and the sandbox profile re-validated, but this build cannot reconnect to a provider run it did not start",
        },
    )


def interrupt_once(*, directive_id: str, reason: str, settings: SupervisorSettings, api: ApiClient) -> dict[str, Any]:
    """Cancel a directive through the EXISTING cancel transition.

    On a surface whose ``interrupt`` is ``UNSUPPORTED`` this refuses rather than reaching for a
    signal, and the refusal is recorded as an effect-journal row so it is evidence rather than a log
    line. The supervisor never kills a process to simulate an interrupt.
    """
    surface = settings.default_runtime_surface
    try:
        require_verb(surface, "interrupt")
    except UnsupportedVerb as error:
        try:
            intent = EffectIntent(kind="external", capability="process.execute", resource=surface, argv=(), reversibility="reversible", description=f"interrupt refused: {error}")
            api.open_effect(
                {
                    "directive_id": directive_id,
                    "intent": effect_intent_to_json(intent),
                    "intent_digest": effect_intent_digest(intent),
                    "enforcement_tier": "advisory",
                    "action_tracing": "unavailable",
                    "lease_generation": 0,
                    "provider_run_id": None,
                    "provider_turn_id": None,
                    "state": "prepared",
                    "evidence": {"unsupported_verb": "interrupt", "surface": surface},
                }
            )
        except ApiRefusal:
            pass
        raise SupervisorRefusal("unsupported_verb", {"surface": surface, "verb": "interrupt", "message": str(error)}) from error
    return api.cancel(directive_id, reason=reason)


# ------------------------------------------------------------------------------------------
# the command line
# ------------------------------------------------------------------------------------------


def _emit(payload: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tce_supervisor", description="TCE dispatch supervisor")
    sub = parser.add_subparsers(dest="verb", required=True)

    selftest = sub.add_parser("selftest", help="run the sandbox self-test and print the result")
    selftest.add_argument("--json", action="store_true")

    denials = sub.add_parser("probe-denials", help="attempt every declared prohibition once and record whether it was denied")
    denials.add_argument("--json", action="store_true")

    dispatch = sub.add_parser("dispatch", help="dispatch one directive")
    dispatch.add_argument("--directive", required=True)
    dispatch.add_argument("--surface", default=None)

    resume = sub.add_parser("resume", help="re-validate and resume a directive")
    resume.add_argument("--directive", required=True)
    resume.add_argument("--prompt", default="continue")

    interrupt = sub.add_parser("interrupt", help="interrupt a directive through the existing cancel transition")
    interrupt.add_argument("--directive", required=True)
    interrupt.add_argument("--reason", default="operator interrupt")

    sub.add_parser("reconcile", help="resolve every effect left open by a previous process")

    args = parser.parse_args(list(argv) if argv is not None else None)
    settings = get_supervisor_settings()

    try:
        if args.verb == "selftest":
            _emit(sandbox.run_self_test(default_self_test_charter(settings), settings=settings), as_json=bool(args.json))
            return 0
        if args.verb == "probe-denials":
            _emit(sandbox.probe_denials(default_self_test_charter(settings), settings=settings), as_json=bool(args.json))
            return 0

        api = ApiClient(settings)
        if args.verb == "dispatch":
            _emit(dispatch_once(directive_id=args.directive, surface=args.surface, settings=settings, api=api), as_json=True)
            return 0
        if args.verb == "resume":
            _emit(resume_once(directive_id=args.directive, prompt=args.prompt, settings=settings, api=api), as_json=True)
            return 0
        if args.verb == "interrupt":
            _emit(interrupt_once(directive_id=args.directive, reason=args.reason, settings=settings, api=api), as_json=True)
            return 0
        if args.verb == "reconcile":
            _emit(startup_reconcile(settings=settings, api=api), as_json=True)
            return 0
    except SupervisorPause as pause:
        print(pause.message, file=sys.stderr)
        return 3
    except (SupervisorRefusal, sandbox.SandboxUnavailable, sandbox.ProfileTampered, taskdir_mod.TaskDirUnavailable, verifier.VerifierUnavailable) as refusal:
        print(str(refusal), file=sys.stderr)
        return 1
    except ApiRefusal as refusal:
        print(f"{refusal.status_code} {refusal.error}: {json.dumps(refusal.detail, default=str)}", file=sys.stderr)
        return 1
    parser.error(f"unknown verb {args.verb!r}")
    return 2
