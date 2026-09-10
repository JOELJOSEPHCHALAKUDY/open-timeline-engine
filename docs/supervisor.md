# The TCE supervisor — running it, and what it is and is not

The supervisor is the process that actually dispatches an autonomous coding runtime, watches
it, verifies its work and reconciles it after a crash. It is a **separate host process**,
not part of the API.

That separation is the point. `services/tce_api` mounts `/var/run/docker.sock` and the repo
`.env`, and it owns the enforcement database. Running the mutating agent inside that process
would give the thing being governed the governor's own privileges. The supervisor therefore
has its own OS process, its own API token, its own consumer id and a deliberately narrow
credential, and **no backend module imports it** — an import of `tce_api`, `tce_lite_api`,
`tce_worker` or `sqlalchemy` anywhere under `services/tce_supervisor/` is a test failure.

Read [docs/charter.md](charter.md) first. This document assumes you know what an enforcement
tier is and what it does not enforce.

---

## 1. Launch

```
python -m tce_supervisor
```

It reads `TCE_SUP_*` from the environment and from
`~/.config/open-timeline-engine/supervisor.env` (mode `0600`, the same convention as the
host-capture token file).

**No `make` target and no compose service is added, deliberately.** Adding one to `infra/`
would put the supervisor back inside the manager's deployment unit. Run it in the foreground
while you are learning it; a `launchd` plist is the supported way to keep it running (§3).

The supervisor **never passes `--bg`** and never uses `claude agents`, so a `respawn --all`
cannot restart its work behind your back. `test_argv_never_backgrounds` asserts `"--bg"` and
`"--background"` are absent from every adapter's constructed argv.

### Startup order is not negotiable

`startup_reconcile()` runs **before** anything can dispatch, and the recorded call order
begins `["startup_reconcile", "get_active_charter", "sandbox_self_test"]` with `start`
strictly after all three. A stopped service does not silently restart work, and it does not
restart work at all without an active charter — `dispatch_once` raises
`SupervisorRefusal("no_active_charter")` and the adapter's `start` is never called.

The supervisor **adopts no process it did not start.** Reconciliation goes through the
provider's own record (`thread/read`, `thread/items/list`, claude session files plus
`--resume`) and only falls back to inspecting external state where no record exists.

---

## 2. Credentials — you need two, and they must be distinct

### 2.1 The supervisor token

`TCE_SUP_API_TOKEN`, with `TCE_SUP_CONSUMER_ID`. The client refuses to start when it
collides with the manager's or the capture channel's credentials:
`assert_supervisor_credential_is_distinct` raises `CredentialCollision` when
`TCE_SUP_API_TOKEN` equals any entry of `TCE_API_TOKENS` or `TCE_HOST_CAPTURE_TOKENS`, and
then **scrubs those keys from `os.environ`** so a spawned child cannot inherit them.

`is_forbidden_path()` refuses host-only (`/v1/inputs`) and manager-only
(`/v1/dashboard/`, `/v1/system/`, `/v1/runtime/mode`) prefixes before the request is built.

Identity headers on every call: `X-TCE-Consumer: <TCE_SUP_CONSUMER_ID>`,
`X-TCE-Role: executor`, plus workspace / user / behavior-subject. `claimed_executor` on
every directive the supervisor claims is therefore the supervisor's consumer id, which the
existing fencing UPDATE already keys on.

**One inherited hazard, corrected here.** The MCP client installs a urllib3 `Retry` that
retries POST and PUT. The supervisor's client **must not** copy that: it installs
`Retry(total=3, allowed_methods=frozenset({"GET"}))` only, and every mutating call carries an
idempotency key. A retried `start` is a duplicate dispatch, and that retry policy is exactly
where one would come from.

### 2.2 The verifier token — provision it and BIND it, or verification never records

`TCE_SUP_VERIFIER_TOKEN`, with its own consumer id, **and a server-side identity claim for
that token** (or the host-capture credential).

`runner_principal` is derived from the authenticated identity, and `decide_verdict` returns
**`inconclusive`** when it equals the claiming consumer — an identity does not grade its own
work.

The consumer id alone is not an identity. Under the shipped `identity_claims_mode=compat` a
bearer holder may assert any `X-TCE-Consumer` it likes, so `POST /v1/verification/results`
requires the principal to come from a **server-bound claim** (`identity_claims_json`) or from
`HOST_CAPTURE_CAPABILITY`; a token that has neither is refused with
`403 {"error": "unknown_runner", "reason": "identity_unverified"}` and **nothing is recorded**.
Otherwise the implementing agent could grade its own directive by sending
`X-TCE-Consumer: system:verifier` on its own token.

So there are three outcomes, all fail-closed, none of them a false `passed`:

| Verifier credential | Result |
|---|---|
| absent | supervisor records `{"skipped": true, "reason": "unknown_runner:identity_unverified"}`; `verification_state` stays `unverified` |
| present but not server-bound | same — the `403` is caught, the run continues, the spend still reconciles |
| present and server-bound | evidence recorded; verdict `passed` / `failed` / `inconclusive` on its merits |

`unverified` keeps the task out of `DONE` via P2's R8 exactly as `inconclusive` did. That is
the intended failure mode, not a bug to route around.

---

## 3. One-time owner setup that is NOT done for you

### 3.1 A dedicated OS user (D-1) — currently absent

**The supervisor runs as the same uid as the manager.** `sudo -n true` requires a password on
this host and no service account exists, so separation today is **sandbox-profile-only, not
UID-level**. `run_self_test` records `uid_separation: false` and `build_governance_status`
emits the limitation line. Do not read "sandboxed" as "runs as another user".

This is not cosmetic. A same-uid agent can `chmod` its own `0400` files, which is why file
permissions are not a control here and why `chmod` is not a Tier-2 control either.

The one-time interactive setup that would flip it, with **no code change**:

```sh
# as the owner, interactively (this will prompt for a password)
sudo sysadminctl -addUser tce-executor -fullName "TCE executor" -shell /usr/bin/false
sudo dseditgroup -o edit -a tce-executor -t user staff

# give it its own task root and its own supervisor.env
sudo install -d -o tce-executor -g staff -m 0700 /Users/Shared/tce-tasks
```

Then run the supervisor as that user (a `launchd` plist under
`/Library/LaunchDaemons/` with `UserName: tce-executor`), and set two environment values:
the task root above, and that user's own `TCE_SUP_API_TOKEN`. `uid_separation` then records
`true` and the limitation line stops being emitted.

### 3.2 Claude as a dispatchable runtime (D-6) — off by default

`claude/stream` and `claude/oneshot` ship with `available=False` and a populated
`unavailable_reason`. A bare `claude` subprocess returns `authentication_failed` /
"Not logged in · Please run /login".

To enable them the owner must both:

1. set `TCE_SUP_CLAUDE_BINARY` to an **absolute, versioned path** (see D-5 below — two
   `claude` versions already exist side by side on this host), and
2. **provision a dedicated credential** for it. Do not point it at your own interactive
   login: a dispatched agent holding your personal credential is the D-10 exposure with your
   account attached.

`codex/app-server` is the default surface, and it is the only one with all six verbs real.

### 3.3 Runtime pinning (D-5) — why a dispatch suddenly refuses

Both runtime binaries live inside auto-updating consumer applications. The supervisor pins by
**absolute versioned path** and asserts `--version` at dispatch; a mismatch marks the adapter
`degraded` and **refuses mutating dispatch** rather than running an unpinned binary. When an
app auto-updates under you, that refusal is the system working. Re-pin deliberately.

---

## 4. Verbs, and the two surfaces that cannot interrupt

`RUNTIME_CAPABILITIES` declares, per surface, a total map over the six verbs. It is not a
uniform interface and is not pretended to be:

| surface | interrupt | notes |
|---|---|---|
| `codex/app-server` | **SUPPORTED** | the default; all six verbs real |
| `claude/stream` | **SUPPORTED** | real interrupt; `available=False` until credentialled |
| `codex/exec` | **UNSUPPORTED** | fire-and-forget |
| `claude/oneshot` | **UNSUPPORTED** | non-interruptible task families only |

`require_verb()` raises `UnsupportedVerb` naming the surface, and **the refusal is journalled
as an effect-journal `evidence` entry rather than silently no-oped.** If you ask a
non-interruptible surface to stop, you get a durable record that it could not, not silence.

`kill` is deliberately not one of the six verbs: it is the wall-clock and revocation
mechanism and lives entirely in the supervisor.

### `python -m tce_supervisor resume` always refuses. It is a stub, not a feature.

The CLI exposes a `resume` verb and **it never resumes anything.** `resume_once` re-resolves
the charter and re-hashes the rendered sandbox profile for real — so a revoked charter or a
drifted profile is caught — and then raises `SupervisorRefusal("resume_requires_live_handle")`.

The reason is mechanical, not philosophical: a runtime handle does not survive the process
that created it, and this build does not reconnect to a stored provider id. Both runtimes
*can* be resumed cross-process — codex by `thread/resume` on the stored `thread_id`, claude by
`--resume` on the stored `session_id` — so this is a **missing mechanism, not an impossible
one.** It refuses loudly rather than returning a success-shaped `{"resumed": false}` body,
because a resume that silently did nothing is how a directive gets abandoned in place.

What to do instead: reconcile the session (`python -m tce_supervisor reconcile`), which
resolves the effects the dead process left open, then dispatch again. Section 5.1 covers the
case where an effect is left in `unknown` and needs a human to close it.

**Design §3.6 is therefore half implemented**, and the half that exists is the validation half.

---

## 5. Operator escape hatches — how to unstick a session

A session can be wedged in two ways, and both have a documented way out. Both routes require
a **verified human** (not a bare `X-TCE-Role: user` header), both are audited, and both
refuse while the underlying condition still holds.

### 5.1 An effect stuck in `unknown`

An effect whose reversibility is `irreversible` or `unknown` and which a reap or crash left
unresolved is resolved to `unknown`, and that **pauses**. It must not enter the retry ladder:
an effect that may have landed and may be irreversible is not something to retry
automatically. The next takeover step returns, and the MCP layer prepends to `next_step`:

```
AUTONOMOUS MODE PAUSED: an effect from a previous run is unresolved and may be irreversible.
Resolve effect <id> before continuing.
```

The pause is scoped to rows in state `unknown` only. `prepared` and `running` rows are the
normal state of a live dispatch and never pause it.

To clear it, once you have established what actually happened:

```
POST /v1/effects/{effect_id}/resolve
{ "target_state": "confirmed" | "reverted" | "failed",
  "resolution_source": "operator",
  "expected_lease": <the directive's current lease_generation>,
  "evidence": { ... what you checked ... } }
```

`expected_lease` is a fence, not a formality: a stale lease returns `409 stale_lease` and the
row stays as it was. If a reap bumped the lease from 1 to 2, a resolve carrying 1 is refused.

The actor for an operator resolve is **derived** from the verified human as
`system:owner` — it is not caller-supplied, which is what makes the system-only rule real
while still leaving you a way in.

### 5.2 A dead handoff blocking every future grant

A `handoff_outbox` row that reaches `status='dead'` (ten failed attempts) is never retried
and never clears the completion obligation, which would otherwise **permanently** block all
future mutating capability grants in that session with no way out.

```
POST /v1/handoffs/outbox/{id}/requeue
```

Same requirements: verified human, audited, refused while the underlying condition holds.

### 5.3 The obligation does not block the caller's own dispatch

Worth knowing so you do not go hunting for a deadlock that is not there: the completion
obligation excludes the calling directive's own effects. The root effect is opened and moved
to `running` for the whole of a dispatch and resolved only at the end — precisely the window
in which the agent requests its grants. Without that exclusion every healthy dispatch would
deadlock.

---

## 6. Reading the numbers honestly

### `human_intervention_count` is a floor on codex, and structurally zero on the default surface

It has **one real producer**. Claude's `result.permission_denials[]` length plus hook denials
is real. Codex's `item/*/requestApproval` and `item/tool/requestUserInput` pairs are counted
during `observe` — but the default surface sets `approvalPolicy: "never"`, **under which
those callbacks never fire**. So on `codex/app-server`, the default, the figure is
structurally `0`. Read it as a floor, never as "nobody intervened".

### Cost

`spend_enforcement` is `unsupported` on both codex surfaces and `estimated` on both claude
surfaces; **nothing runnable is `enforced`**. Overshoot
(`cost_minor_units > budget_reserved_minor_units`) is recorded and visible in the row, in
`GET /v1/dispatch/{id}` and in the governance limitation line. It does not mark the charter
violated and it does not fail the dispatch.

`dispatch_store.open_dispatch` is the **sole** caller of the budget reservation; the
supervisor reads the number back from `DispatchResponse.cap_applied["max_budget_usd"]`.

Two accounting traps are encoded rather than left to be discovered: claude's per-step
`output_tokens` is a placeholder (the terminal `result` carries the real count), `usage`
excludes subagents while `total_cost_usd` and `modelUsage` include them, and a crashed result
may carry an all-zero cost — an all-zero usage with `is_error=True` is recorded as
`cost_source="unavailable"`, not as a genuine zero.

### Per-command action tracing is unavailable

Every effect row carries `action_tracing ∈ {observed, unavailable}`. `observed` is used only
for effects the adapter individually saw. **One shell/exec tool call that runs a script is
ONE decision and ONE event**; every command inside it is invisible. Shell one-liners,
Makefile targets and test runners are `unavailable`, and the row names the boundary that was
in force instead of implying a trace exists.

### The sandbox self-test

Sixteen non-vacuous assertions, run for real at every supervisor boot, each with a
non-vacuity control. `sandbox_self_tests.assertions_json` carries every row.
`open_dispatch` refuses server-side (`409 sandbox_self_test_missing` / `_stale` /
`_digest_mismatch`) unless a self-test row exists, passed, ran recently enough, and its
`profile_digest` matches the profile the dispatch declares. **"We have a sandbox" is a
per-boot measurement, never a claim.**

One honest limit on the profile itself: it was derived by clause-removal bisection, which
proves every shipped clause is load-bearing but **cannot prove none is missing** for a code
path nobody exercised. This host exposes no sandbox denial telemetry to a non-root session,
so that gap cannot be closed by reading logs. The boot-time `runtime_starts` assertion is the
guard that catches a missing clause at boot rather than at dispatch.

---

## 7. Stated deviations — what this host cannot enforce

Reproduced verbatim from the P3 design. Each has a measured basis. If you are deciding
whether to trust an autonomous run, this table is the shortest honest summary of where the
name of a control is stronger than the control.

| # | Asked for | What this host can actually do | What P3 ships |
|---|---|---|---|
| **D-1** | A dedicated OS user for the executor | `sudo -n true` requires a password; only `root`/`daemon`/`nobody`/`joeljoseph` exist. | Supervisor runs as the **same uid** as the manager. Separation is Seatbelt-only. `sandbox_self_tests.uid_separation` records `false` and `limitations` carries the line. **This is what makes F10 possible in the first place** (a same-uid agent can `chmod` its own 0400 files) and why chmod is not a Tier-2 control either. `docs/supervisor.md` documents the one-time owner setup that would upgrade it. |
| **D-2** | A domain-level egress allowlist | Seatbelt filters by port and localhost only; hostnames are a parse error. | No domain field (C3). Domain control is `unsupported`. The extension point is a CONNECT proxy on a `--internal` docker network — **measured working** (§5.7), unlike v1's localhost variant. |
| **D-3** | Per-command action tracing | Seatbelt binds a process tree, not commands. One Bash/exec tool call that runs a script is ONE decision and ONE event. | Effect rows carry `action_tracing ∈ {observed, unavailable}`. `observed` only for effects the adapter individually saw. Shell one-liners, Makefile targets and test runners are `unavailable`, and the row names the boundary in force instead. |
| **D-4** | Test-integrity enforcement | A deny inside an allow **does** hold (§5, assertion 9, all prefixes). | `charter.protected_write_prefixes` is genuinely OS-enforced for the sandboxed tree. It is **not** enforced against anything outside it, and the corpus digest **detects** weakening rather than preventing it. Both statements ship in `docs/charter.md` and `limitations`. |
| **D-5** | A pinned, stable runtime | Both binaries live inside auto-updating consumer apps (two `claude` versions already side by side). | Pin by absolute versioned path; assert `--version` at dispatch; a mismatch marks the adapter `degraded` and refuses mutating dispatch. **This is also why Tier 2 cannot host the agent** — the only linux build is npm `@openai/codex 0.153.4` against a host pin of `0.153.1`, which D-5 refuses. |
| **D-6** | Claude as a dispatchable runtime | A bare `claude` subprocess returns `authentication_failed` / "Not logged in · Please run /login". | `claude/stream` and `claude/oneshot` ship with `available=False` until the owner sets `TCE_SUP_CLAUDE_BINARY` **and** provisions a dedicated credential. `codex/app-server` is the default. |
| **D-7** | `sandbox-exec` as a supported API | Its man page says DEPRECATED in capitals. It works on macOS 26.5.2 and Codex itself shells out to it (`strings` → 2 hits plus `seatbelt_base_policy.sbpl`). | Version-pinned and re-measured at every supervisor boot by **sixteen non-vacuous assertions**. A failed self-test **refuses mutating dispatch**. "We have a sandbox" is a per-boot measurement, never a claim. |
| **D-8** | Server-side commit/tree verification | `git` is installed in **no** service image and no repo is bind-mounted; `_compute_git_change_summary` returns `{}` silently in Docker. | The verifier runs **host-side, inside the supervisor**, under **Tier 2 `--network none`** (mandatory), as a principal distinct from the executing identity, and POSTs raw evidence. `verification_results.platform` records which platform produced it. A Tier-2 verification (`linux/aarch64 python3.12.13`) **does not transfer** to the owner's shell (`darwin/arm64 python3.12.12`). If the supervisor is absent, `verification_state` stays `unverified` and P2's R8 keeps the task out of `DONE`. |
| **D-9** | The confused-deputy escape closed | **Critic 2's conclusion was wrong and is corrected by measurement, not accepted.** `osascript` *runs* under the v1 profile but does **not escape**: `do shell script` inherits the profile (write outside TASKDIR → DENIED); the System Events deputy fails `-54`; Finder and Terminal fail `-600` with Finder demonstrably running (pid 767); adding `(allow appleevent-send)` still fails (`AppleEvent timed out -1712`, no file written); `open -a Calculator` fails `_LSOpenURLsWithCompletionHandler … error -54` and launches nothing. | `applescript_escape` moves from `unverified` to a **measured denial with a standing assertion** (`no_out_of_tree_launch`, §5.3 assertion 15). The v2 profile additionally denies `process-exec` on `osascript`/`open`/`launchctl`/`automator`/`sudo`/`su` and denies the four LaunchServices `mach-lookup` global names — **measured independently load-bearing**, not defensive decoration. |
| **D-10** | **NEW.** The mutating agent cannot exfiltrate the owner's credential | It can. Measured: `auth.json` is readable inside `TASKDIR`; `https://example.com` → 200 and `https://api.telegram.org` → 302 from inside the profile; hostname filtering is a parse error; `egress_mode=deny_all` breaks the runtime outright. | **NOT CLOSED, and stated as such in the operator paragraph (§5.5) and as a charter precondition.** `CHARTER_FIELD_ENFORCEMENT["credential_containment"] == "unsupported"` for `os_sandbox`, and a mutating `os_sandbox` charter must carry `credential_risk_acknowledged: true` or `validate_charter_payload` refuses it (§0.5). §5.7 names the exact three-item change that would close it. |
| **D-12** | **NEW, found at the exit gate.** The verifier's checks cannot edit the tree they judge (design §5.2 "Verifier variant": the protected block expanded to the whole clone). | Implementable, and **not implemented**. `verifier.run` renders the ordinary dispatch profile: same charter, same `protected_write_prefixes`, byte-identical file, **same sha256 as the agent's own profile**. Measured at the gate: under it a check rewrote `<clone>/src/*.py`; with the whole-clone deny rendered instead the same write was denied and `compileall -q .` still exited 0. | **NOT CLOSED for `TCE_SUP_VERIFICATION_TIER=os_sandbox`, and the code comment that claimed it now says so.** Tier 2 — the default — is unaffected: `container_argv` binds every protected prefix read-only, which is what design §3 asks of that tier. The corpus digest still **detects** a check that edited the corpus (D-4), so this is a prevention gap, not a detection gap. |
| **D-13** | **NEW, found by adversary 3.** Frozen criteria that cannot be weakened after the freeze without detection. | Freezing an argv freezes the command, not what the command does. `ruff` resolves configuration from the **nearest ancestor** config file, so `services/pkg/.ruff.toml` with `[lint]\nselect = []` took the frozen `lint` check (`python -m ruff check .`) from `exit=1, Found 2 errors` to `exit=0, All checks passed!`. With `DECIDING_FILE_PATHS` matched only at the repository root, neither that file nor a root `ruff.toml` entered the corpus manifest: `corpus_digest` was byte-identical before and after, so the verdict was `passed` on a check that had been switched off. | **CLOSED as detection.** `DECIDING_FILE_PATHS` gains `ruff.toml` and `.ruff.toml`, and `_manifest_wanted` now matches a deciding **basename at any depth** rather than an exact root path — so nested tool config and nested `conftest.py` move the digest and grade `failed` / `corpus_digest_mismatch`. Detection, not prevention (D-4). What stays **open and is stated, not glossed**: a `# ruff: noqa` inside the work product silences the lint check and no digest can see it, because the file being changed is the deliverable; `ruff check --ignore-noqa` would close it and is not enabled because it fails on this repository's four legitimate suppressions. `docs/charter.md` §9.1. |
| **D-11** | **NEW.** A minimal `mach-lookup` allowlist | This host exposes **no sandbox denial telemetry to a non-root session**: after a guaranteed denial, `log show --last 1m --predicate 'process=="kernel"' \| grep -ci sandbox` → `0`, and `/var/log/system.log` is `root:admin 0640`. | The profile was derived by **clause-removal bisection**, which proves every shipped clause is load-bearing but cannot prove none is missing for an unexercised code path. The shipped rule is `(allow mach-lookup)` plus the measured deny-list. Assertion 11 `runtime_starts` is the boot-time guard that catches a missing clause at boot rather than at dispatch. |

The three that most often surprise an operator:

- **D-1** — there is no UID separation. Same user as the manager.
- **D-3** — there is no per-command tracing. One shell call is one observation.
- **D-10** — the agent holds a copy of the owner's credential with unrestricted TLS egress
  on port 443, and nothing at the OS level stops it leaving. See
  [docs/charter.md §6](charter.md).
- **D-12** — on `TCE_SUP_VERIFICATION_TIER=os_sandbox` the verifier runs under the *agent's* profile,
  not a tightened one, so a check can write into the tree it is judging. The default tier
  (`container`) does not have this gap.

---

## See also

- [docs/charter.md](charter.md) — approving a charter, the enforcement tiers, `polarity`,
  and the field-by-field enforcement table.
- `GET /v1/governance/status` — what is enforced on this installation right now, measured.
- `sandbox_self_tests` — the sixteen assertions from the most recent boot.
