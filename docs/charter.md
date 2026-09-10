# Authority charters — what they are, and exactly what they enforce

An **authority charter** is an owner-approved, versioned, expiring record that says what an
autonomous run is allowed to do: which paths it may write, which it may never write, which
capabilities it may use, which runtimes it may be dispatched to, how much wall-clock and
money it may spend, and under which **enforcement tier** it runs.

This document is written for an operator who has to decide whether to approve one. It is
deliberately blunt about the difference between the three things a charter field can mean:

- **enforced by the operating system** — the kernel refuses; the agent cannot do it;
- **enforced by the manager** — TCE refuses; the agent could do it if TCE were not asked;
- **cooperative** — the agent is told not to; nothing stops it;
- **not enforced** — named here so it is not mistaken for one of the above.

The last category is not empty. Read [Not enforced, and not claimed to be](#not-enforced-and-not-claimed-to-be)
before approving a mutating charter.

---

## 1. Where the charter sits

```
authority_charters      owner-approved, versioned, expiring     <- this document
  execution_permits     per directive, 300 s
    capability_grants   per action, 120 s, single-use, digest-bound
```

A charter **narrows** the inputs the two lower layers already consume; it can never widen
them. That is a tested property (`tests/unit/test_charter.py::test_narrowing_is_monotone`
and `::test_charter_never_widens`), not a convention: `permitted_roots` and
`permitted_capabilities` may only shrink, every cap is a `min()`, `protected_write_prefixes`
and `denied_read_paths` may only grow, `egress_mode` may only move toward `deny_all`, and
the enforcement tier may only strengthen.

**An absent charter blocks.** With `TCE_CHARTER_ENFORCEMENT_ENABLED=1`, a mutating action
kind with no active, unexpired, unrevoked charter is refused at the claim, at effect open,
at dispatch open and at capability-grant issue — `409 no_active_charter`. With
`TCE_CHARTER_ENFORCEMENT_ENABLED=0` behaviour is exactly what it was before P3. That flag is
the documented off switch and there is no other one.

### Narrowing comes from one channel only

A narrowing must reference a `trusted_input_receipts` row (`charter_narrowings.source_receipt_id`
is NOT NULL), and so must **creating** a charter (`CharterCreateRequest.source_receipt_id`).
Both cost the same, deliberately: without that, the receipt requirement is bypassed by
issuing a fresh, wider charter instead of narrowing an existing one.

`TakeoverPolicy` and `TakeoverStepRequest.constraints` are **never** read by the charter.
They are client-supplied and bidirectional — setting `policy.safety_policy` to anything but
`"high-risk-pause"` disables the whole high-risk gate — so they cannot be an authority
source.

---

## 2. Approving a charter

A create request carries, at minimum:

| field | meaning |
|---|---|
| `source_receipt_id` | the trusted-input receipt that authorises this charter (required) |
| `enforcement_tier` | `os_sandbox` \| `container` \| `advisory` — see §4 |
| `permitted_roots` | repo-relative prefixes the run may write. Non-empty. |
| `protected_write_prefixes` | repo-relative prefixes it may never write. **Non-empty.** |
| `denied_read_paths` | paths the run may never read. **Only entries under `$HOME` are OS-enforced** — see the enforcement table in §8; anything else is advisory and is named in `limitations`. |
| `permitted_capabilities` | a subset of `CAPABILITY_REGISTRY` |
| `confirm_required_capabilities` | capabilities that additionally need a human confirm |
| `egress_mode` | `deny_all` \| `https_only` |
| `runtime_allowlist` | `(runtime_id, runtime_version, surface)` triples |
| `caps` | attempts, concurrency, wall seconds, budget |
| `credential_risk_acknowledged` | required `true` for a mutating `os_sandbox` charter — see §6 |

Three refusals are worth knowing before you write one:

1. **`protected_write_prefixes` must be non-empty.** This is not tidiness. A rendered
   Seatbelt profile with zero deny clauses silently denies *nothing* — `(deny file-write*)`
   with no subpath is a no-op — and the DDL defaults the column to `'[]'`, so an otherwise
   valid charter reaches that edge. The validator refuses it instead.
2. **`advisory` may carry no mutating capability.** A charter whose tier is `advisory` and
   whose `permitted_capabilities` contains anything `CAPABILITY_REGISTRY` marks `mutating`
   is refused, and a dispatch under it must declare a `task_family` in
   `READ_ONLY_TASK_FAMILIES` or `open_dispatch` returns `422 task_family_not_read_only`.
3. **No field may be named after a domain.** A key whose name contains `"domain"` is
   refused. There is no hostname allowlist and there must never be one — see D-2 in §7.

**Populate `permitted_capabilities` fully, including read capabilities.** The permit ladder
blocks when the capability for an action kind is not in `permitted_capabilities`, and that
includes read and review kinds. A write-only charter refuses reads.

---

## 3. `polarity` — read this before acting on `scope.path_prefixes`

Every constraint rule delivered to an executor over MCP carries a **mandatory `polarity`**
field:

```json
{
  "directive_type": "hard_constraint",
  "rule_id": "charter-roots-only",
  "polarity": "allow_only",
  "scope": { "path_prefixes": ["src/", "docs/"], "actions": ["edit", "write", "delete"] },
  "enforcement": "block_and_escalate",
  "reason": "..."
}
```

- `polarity: "deny"` — do **not** perform the scoped actions on paths under those prefixes.
- `polarity: "allow_only"` — perform the scoped actions **only** on paths under those
  prefixes; the same actions anywhere else are refused.
- A rule that arrives without the key is read as `"deny"`.

Every pre-P3 rule used `scope.path_prefixes` as a deny list, and `CLAUDE.md`'s documented
reading was "if `block_and_escalate`, do not perform the scoped action". `charter-roots-only`
puts an allow list in the same field. **Without the flag, a conforming executor reads the
charter as its exact inverse** — "do not write to `src/`" instead of "write only to `src/`".

### The floor a charter cannot delete

Two rules are always present, with or without a charter, and a backend list that omits them
does not remove them (`tce_mcp.tools.FLOOR_CONSTRAINT_RULE_IDS`,
`tests/mcp/test_charter_constraints.py`):

- `no-edit-protected-dirs` — the enforcement code itself.
- `no-edit-firewall-null-response` — the MCP module that strips directive text out of the
  tool result, i.e. the channel these constraints travel through.

A charter may only **add** to that floor. `must-check-context-before-edit` is deliberately
**not** projected from a charter: `check_context` is an ILIKE-over-observations heuristic
that returns `allow` on an empty result. It is evidence, not authority, and it stays in the
MCP fallback list rather than being dressed as a charter rule.

### What the constraints array does not do

It is a **cooperative protocol**. Nothing in the MCP server, in either backend, or in the
operating system stops an executor in an ordinary chat session from ignoring every rule in
it. The one structural property that holds is narrow and worth stating exactly: the MCP
server loads `tools.py` at process start, so an executor editing that file on disk does not
change the constraints its own running session receives. **That is a reload boundary, not a
sandbox.**

**There is no override.** Earlier revisions of `CLAUDE.md`, `AGENTS.md` and the comment at
the top of `tools.py` told executors that a user could say `override constraint <rule_id>`.
No code in this repository ever parsed that phrase. The claim has been deleted rather than
implemented. To relax a rule, change the charter.

---

## 4. Enforcement tiers

`enforcement_tier` is chosen by the charter, not by the dispatcher, and it is a **NOT NULL**
column on `authority_charters`, on `dispatch_records` and on **every** `effect_journal` row.
There is no default anywhere — omitting it is a `TypeError` — so a row can never claim a
tier it did not run under.

| tier | used for | what it is |
|---|---|---|
| `os_sandbox` | **default for the mutating agent** | a hardened Seatbelt profile, re-measured by sixteen non-vacuous assertions at every supervisor boot |
| `container` | **mandatory for the verifier** | `docker run --network none`, task clone bind-mounted, read-only binds per protected prefix, no docker socket, `--cap-drop ALL`, `--security-opt no-new-privileges` |
| `advisory` | read-only work only | unwrapped execution; every journal row is stamped `action_tracing="unavailable"` |

**Tier 2 cannot host the mutating agent on this host, and P3 does not pretend it can.** The
pinned runtime is a `Mach-O arm64` binary; the container is `linux/aarch64`; the only Linux
build obtainable is a *different version* than the pin, which the runtime-pinning rule
refuses. Making Tier 2 the mutating default would mean no dispatch at all, not a slower one.

**There is no inner second layer.** Nesting `sandbox-exec` is refused in both directions —
even a strictly more restrictive inner profile is refused — so the runtime's own sandbox
flag cannot be combined with the outer profile. Both codex surfaces are dispatched with
`-s danger-full-access` *because the outer boundary is the real one and is measured*.
`--dangerously-bypass-approvals-and-sandbox` is never passed. The profile is one layer.

**It never silently downgrades.** If any required self-test assertion fails or is vacuous,
`select_tier` raises `SandboxUnavailable`, the failing self-test row is POSTed so the failure
is durable evidence rather than a log line, and the dispatch is refused.
`GET /v1/governance/status` then reports `effective_execution_enforcement="protocol_only"`
and `sandbox_self_test_passed=false`.

---

## 5. Which control is enforced by what, per tier

`sandbox_self_tests.assertions_json` carries every row; `build_governance_status().limitations`
carries the ADVISORY and UNSUPPORTED rows verbatim.

| control | Tier 1 `os_sandbox` (hardened) | Tier 2 `container` | Tier 3 `advisory` | measurement |
|---|---|---|---|---|
| writes confined to the task clone | **ENFORCED** (OS) | **ENFORCED** (bind mount) | NOT ENFORCED | a1, a3; live probe 3 |
| protected write prefixes, ALL of them | **ENFORCED** (OS) | **ENFORCED** (read-only binds) | NOT ENFORCED | a9; live probes 4, 5; §5.2 |
| `$HOME`, `~/.ssh`, keychains unreadable | **ENFORCED** (OS) | **ENFORCED** (not mounted) | NOT ENFORCED | a4; live probe 1; `ls $HOME` in container |
| runtime's ambient config unreadable | **ENFORCED** (OS + `CODEX_HOME`) | **ENFORCED** | NOT ENFORCED | a5; live probe 2 |
| Docker control socket unreachable | **ENFORCED** (OS) | **ENFORCED** (not mounted) | NOT ENFORCED | a6; F1; live probe 6 |
| TCE postgres/redis/api/ollama unreachable | **ENFORCED** (OS, by port) | **ENFORCED** (`--network none`) | NOT ENFORCED | a7 (4/4 open unsandboxed); live probe 7 |
| plain http (:80) blocked | **ENFORCED** (OS, by port) | **ENFORCED** | NOT ENFORCED | a8 (80=000) |
| **egress restricted to named hosts** | **UNSUPPORTED** | **ENFORCED** with `--internal`+proxy; UNSUPPORTED with `--network none`+agent | UNSUPPORTED | `host must be * or localhost`; §5.7 |
| **the runtime credential cannot leave** | **NOT ENFORCED (D-10)** | **ENFORCED** with `--internal`+proxy | NOT ENFORCED | F7 (auth.json readable; `api.telegram.org` → 302) |
| profile cannot be widened from inside | **ENFORCED** (OS) | **ENFORCED** (no nesting to widen) | n/a | a10; live probe 10 |
| the live profile cannot be rewritten | **ENFORCED** (rendered outside `TASKDIR`) | n/a | n/a | F10, both transcripts |
| out-of-tree launch (AppleEvent, LaunchServices) | **ENFORCED** (OS) | **ENFORCED** (no such services) | NOT ENFORCED | a15; F6, four transcripts; live probes 8, 9 |
| no push credential in the child | **ENFORCED** (env, a separate control) | **ENFORCED** | **ENFORCED** | a16 |
| runtime and version pinning | **manager** (TCE refuses) | manager | manager | D-5 |
| attempt / concurrency / wall-clock caps | **manager** (+ `kill`, §3.7) | manager + a hard kill boundary | manager | §8.2; `--pids-limit`, `--memory` |
| capability grants per action class | **broker** (cooperative) | broker | broker | protocol only |
| spend ceiling | **manager**, `unsupported` on codex, `estimated` on claude | same | same | C3-G8 |
| per-command action tracing | **ADVISORY** — one shell call is one observation | ADVISORY | ADVISORY | D-3; the live run shows `command_execution` items only for what the adapter saw |
| test integrity outside the process tree | **DETECTED, not prevented** | DETECTED | DETECTED | corpus digest; D-4 |
| tool config swapped to silence a frozen check | **DETECTED, not prevented** | DETECTED | DETECTED | `DECIDING_FILE_PATHS` by basename at any depth; §9.1 |
| in-file `# ruff: noqa` in the work product | **NOT DETECTED** | NOT DETECTED | NOT DETECTED | the deliverable must be writable; §9.1 |
| UID separation | **NOT ENFORCED** — same uid as the manager | NOT ENFORCED (`--user` is the same uid) | NOT ENFORCED | D-1; F10 is a direct consequence |
| the sandbox interface itself | `sandbox-exec`, **DEPRECATED**, re-measured every boot | supported | n/a | man page |

### Not enforced, and not claimed to be

The paragraph below ships verbatim here, in the `authority_charters` module docstring, and
as the body of the `limitations` lines returned by `GET /v1/governance/status`. It is the
house style this repository already uses: name the residual risk rather than gloss it.

> **Enforced by the operating system**, for the process tree the supervisor started, and re-measured by
> sixteen non-vacuous assertions at every supervisor boot: writes are confined to the task clone; every
> protected prefix inside it is unwritable; the home directory, `~/.ssh`, the system keychains and the
> runtime's own ambient configuration are unreadable; the Docker daemon socket is unreachable; TCE's own
> Postgres, Redis, API and Ollama ports are unreachable; plain HTTP is unreachable; the AppleEvent and
> LaunchServices paths out of the process tree are closed; the profile cannot be widened from inside; and
> the live profile file itself is outside every directory the agent can write.
>
> **Enforced by the manager**, meaning TCE refuses rather than the OS: which runtime and version may be
> dispatched; attempt, concurrency and wall-clock caps; and the requirement of an active, unexpired,
> unrevoked charter before any mutating claim.
>
> **Enforced by the capability broker**, meaning a single-use, expiring, digest-bound grant is required:
> the action classes the charter permits. This is a cooperative protocol; nothing outside TCE compels the
> executor to ask.
>
> **Not enforced, and not claimed to be.** *A mutating dispatch on this host holds a copy of the owner's
> ChatGPT OAuth credential inside its task directory and an unrestricted TLS channel to any host on port
> 443. Seatbelt filters by port and by localhost-versus-not; hostname filtering is a parse error, so
> nothing at the operating-system level prevents that credential from being sent anywhere. A charter that
> permits a mutating capability under `enforcement_tier="os_sandbox"` is accepting that risk explicitly,
> which is why such a charter must set `credential_risk_acknowledged`, and `CHARTER_FIELD_ENFORCEMENT`
> reports `credential_containment: "unsupported"` for that tier.* Per-command action tracing is
> unavailable: one shell tool call that runs a script is one observation and every command inside it is
> invisible — those journal rows say `action_tracing: unavailable` and name the boundary that was in force
> instead. Test integrity is *detected* by the frozen corpus digest and *prevented* only for the sandboxed
> process tree; nothing prevents a process outside that tree from weakening a test. The supervisor runs as
> the same operating-system user as the manager, so separation is sandbox-profile-only and not UID-level.
> A verification produced inside the Tier-2 container is `linux/aarch64` and does not transfer to the
> owner's `darwin/arm64` shell. And `sandbox-exec` is a deprecated interface: it works today, it is what
> the runtime itself uses, and it is measured at every boot — but it is not a supported API.

---

## 6. D-10 — the credential the agent holds, and why a mutating charter must say so

This is the one field in the charter whose named enforcer **does not exist**:
`CHARTER_FIELD_ENFORCEMENT["credential_risk_acknowledged"] == "unsupported"`.

> A mutating dispatch under enforcement_tier=os_sandbox holds a copy of the runtime
> credential inside its task directory and has an unrestricted TLS channel on port 443 to
> any host; the credential is exfiltrable and nothing at the operating-system level prevents
> it.

That is not a theoretical concern. Measured on this host: `auth.json` is readable from
inside the task directory; `https://example.com` answers `200` and `https://api.telegram.org`
answers `302` from inside the profile; Seatbelt filters by **port** and by
**localhost-versus-not**, and a hostname in a network rule is a *parse error*
(`sandbox-exec: host must be * or localhost in network address`, exit 65), so there is no
way to express "443 to api.openai.com only". Setting `egress_mode=deny_all` closes the
channel and also breaks the runtime outright.

Therefore: a charter that permits a **mutating** capability under
`enforcement_tier="os_sandbox"` must set `credential_risk_acknowledged: true`, or
`validate_charter_payload` refuses it with
`CharterInvalid("credential_risk_acknowledged", ...)`.

Three deliberate design points:

1. The acknowledgement is a **request-body field validated in the API process**, not an
   environment variable. There is no environment escape hatch. It is signed by the same
   verified human who approves the charter and it is visible in `GET /v1/charters/active`.
2. Whenever an `os_sandbox` charter is active, the limitation line above is **emitted** in
   `limitations`. A gate asserts it (`test_unsupported_fields_are_named_in_limitations`), so
   an `"unsupported"` field cannot be added without an operator-visible line.
3. **What would close it**, exactly, and none of it is in P3: a linux/arm64 build of the
   pinned runtime version-matched to the pin; a manager-held CONNECT proxy on a `--internal`
   docker network (measured working — TLS domains are visible in CONNECT without MITM, so
   the proxy can enforce a domain list and journal every CONNECT host); and
   `TCE_SUP_CONTAINER_NETWORK` flipped from `"none"` to that network. Until all three land,
   the honest configuration is the one shipped.

---

## 7. Stated deviations — what this host cannot enforce

These eleven rows are reproduced verbatim from the P3 design. Each has a measured basis.
An operator should read them as the definitive list of places where the name of a control is
stronger than the control.

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
| **D-11** | **NEW.** A minimal `mach-lookup` allowlist | This host exposes **no sandbox denial telemetry to a non-root session**: after a guaranteed denial, `log show --last 1m --predicate 'process=="kernel"' \| grep -ci sandbox` → `0`, and `/var/log/system.log` is `root:admin 0640`. | The profile was derived by **clause-removal bisection**, which proves every shipped clause is load-bearing but cannot prove none is missing for an unexercised code path. The shipped rule is `(allow mach-lookup)` plus the measured deny-list. Assertion 11 `runtime_starts` is the boot-time guard that catches a missing clause at boot rather than at dispatch. |

---

## 8. Field-by-field: who enforces what

`CHARTER_FIELD_ENFORCEMENT` in `shared/tce_shared/charter.py` is **total** over the fields of
`ResolvedCharter` — no orphans, no extras — and a gate asserts the equality
(`tests/unit/test_charter.py::test_every_field_has_a_named_enforcer`). A field cannot be
added to the charter without naming its enforcer, and naming it `"unsupported"` forces a
limitation line.

| field | enforced by | what that means here |
|---|---|---|
| `enforcement_tier` | `os` | the Seatbelt wrapper, or the container |
| `permitted_roots` | `os` | `(allow file-write* (subpath <root>))` |
| `protected_write_prefixes` | `os` | one `(deny file-write* (subpath <prefix>))` **per prefix** — every prefix, not just the first |
| `denied_read_paths` | `os`, **for entries under `$HOME` only** | the profile's blanket `(deny file-read* (subpath HOMEDIR))`. There are **no** per-entry clauses: `render_profile` renders nothing from this field. Every `DEFAULT_DENIED_READ_PATHS` entry is under `$HOME` and is genuinely denied (assertions 4 and 5); an entry elsewhere is denied only if it happens to fall outside the profile's read allow-list. Measured: a charter naming `/private/etc/hosts` reads it `rc=0` inside the live rendered profile. `GET /v1/governance/status` names any such entry in `limitations`. |
| `egress_mode` | `os` | `deny network*`, plus port 443 and the DNS unix socket under `https_only`. **Port-level only.** |
| `permitted_capabilities` | `broker` | a narrowing of `CAPABILITY_REGISTRY`; cooperative — a single-use, expiring, digest-bound grant is required, but nothing outside TCE compels the executor to ask |
| `confirm_required_capabilities` | `broker` | same, plus a human confirm |
| `runtime_allowlist` | `manager` | asserted against `--version` at dispatch; a mismatch marks the adapter `degraded` and refuses mutating dispatch |
| `caps.max_attempts` | `manager` | `min()`-ed at the retry-mint site |
| `caps.max_concurrent_dispatches` | `manager` | `409 dispatch_concurrency_exceeded` |
| `caps.max_wall_seconds` | `manager` | the observe-loop deadline, then `adapter.kill` |
| `caps.budget_minor_units` | `manager` | **enforced only where `spend_enforcement == "enforced"`, which is nothing runnable today** — see below |
| `credential_risk_acknowledged` | **`unsupported`** | §6. The charter records that the owner accepted the risk; nothing enforces containment. |
| everything else (`charter_id`, `status`, `approved_by`, `expires_at`, `revoked_at`, `narrowing_ids`, `charter_digest`, …) | `manager` | bookkeeping TCE refuses against |

### Spend caps are labelled, and nothing runnable is `enforced`

`spend_enforcement ∈ {enforced, estimated, unsupported}` is persisted per dispatch:

- `codex/app-server`, `codex/exec` → **`unsupported`**. Codex has no spend cap and reports no
  currency anywhere in its protocol.
- `claude/stream`, `claude/oneshot` → **`estimated`**. `--max-budget-usd` rests entirely on
  an abort subtype nobody has observed, and both claude surfaces ship `available=False`. A
  follow-on gate flips them to `enforced` the day a captured transcript contains that subtype.
- **`"enforced"` is asserted absent from every surface** (`tests/unit/test_budget.py`).

**Overshoot is visible in the data model, never hidden.** `dispatch_records.cost_minor_units
> budget_reserved_minor_units` on an `unsupported` surface is the expected, honest outcome of
a runtime that reports spend only after the call. It does not mark the charter violated and
it does not fail the dispatch. Quietly clamping the recorded cost to the reservation would
make the data model lie about the very thing the charter asks it to make visible.

---

## 9. Verification — the two limitations

Verification exists because a self-report is not evidence. **The API is the sole computer of
the verdict and the sole writer of `verification_state`.** The supervisor submits raw
evidence only — commands, exit codes, output hashes, redacted excerpts, the observed corpus
digest, the platform string. The request body has no `verdict`, no `reason`, no
`criteria_digest_match` and no `runner_principal` field; `runner_principal` is derived from
the authenticated identity. An **empty criteria set fails** — that is `decide_verdict`'s
first clause, before anything else. Acceptance criteria live in `acceptance_criteria`, which
has **no UPDATE and no DELETE path in either backend**.

Two limitations ship with it and are not worked around:

1. **A Tier-2 verification does not transfer to your shell.** The verifier runs host-side
   inside the supervisor, under Tier 2 `--network none` (mandatory), as a principal distinct
   from the executing identity. `verification_results.platform` records which platform
   produced it. A verification produced as `linux/aarch64 python3.12.13` is **not** a
   statement about `darwin/arm64 python3.12.12`. Read the `platform` column before treating
   a `passed` as applying to your machine.
2. **Test integrity is detected, not prevented.** `protected_write_prefixes` is genuinely
   OS-enforced for the sandboxed process tree, and a deny inside an allow does hold for every
   prefix. It is **not** enforced against anything outside that tree. The frozen corpus
   digest *detects* a weakened test; it does not prevent one.

### 9.1 What the corpus digest actually binds — and what it cannot

Freezing an argv freezes the *command*, not what the command does. The corpus digest is the
control that covers the gap, so its exact extent is worth stating rather than implying.

**Inside the digest** (`build_corpus_manifest(clone, charter.protected_write_prefixes +
DECIDING_FILE_PATHS)`): every file under a protected write prefix — `tests/`, `.github/`,
`.local/` by default — plus every file **at any depth** whose basename is in
`DECIDING_FILE_PATHS` (`pyproject.toml`, `conftest.py`, `setup.cfg`, `tox.ini`, `pytest.ini`,
`ruff.toml`, `.ruff.toml`, `uv.lock`, `poetry.lock`, `requirements.txt`, `Makefile`).

The *at any depth* clause is load-bearing and was added after the root-only version was
measured to be bypassable. The frozen `lint` check is `python -m ruff check .`; ruff resolves
its configuration per file from the nearest ancestor config, so a two-line
`services/pkg/.ruff.toml` containing `[lint]\nselect = []` took that check from
`exit=1, Found 2 errors` to `exit=0, All checks passed!` — while the manifest, and therefore
`corpus_digest`, stayed byte-identical. The verdict was `passed` on a check that had been
switched off, with the argv untouched. All five variants (`ruff.toml`, `.ruff.toml`,
`services/.ruff.toml`, `services/pkg/.ruff.toml`, a nested `conftest.py`) now enter the manifest
and move the digest; `tests/unit/test_verification_criteria.py` holds each one.

**Outside the digest, by design and not closeable:** the work product itself. `services/`,
`shared/` and every other implementation path must stay writable — changing them *is* the
directive — so no digest can bind them. Two consequences that are **NOT prevented and NOT
detected**, stated rather than glossed:

* **In-file suppression.** A `# ruff: noqa` at the top of a file the agent legitimately wrote
  makes the `lint` check pass over it, and the file is the deliverable, so nothing flags it.
  `ruff check --ignore-noqa` would close this; it is **not** enabled, because this repository
  has four legitimate suppressions and the flag makes the honest check fail (measured:
  `Found 4 errors`). The gap is real and the workaround is not free.
* **A charter that drops `tests/`.** `protected_write_prefixes` drives both the OS deny and the
  manifest, so a charter omitting `tests/` loses the write-prevention *and* the detection in one
  move, and nothing at freeze time checks that the frozen argv's target paths (`tests/unit` in the
  default `test` check) are inside the manifest at all. Keep `tests/` protected.

**Detected, not prevented** is the honest verb for everything in this section: a moved digest
grades `failed` with reason `corpus_digest_mismatch` at `POST /v1/verification/results`. That
refuses the verdict; it does not stop the edit.

And one operational note: **if the supervisor is absent, `verification_state` stays
`unverified`**, and the task-state fold rule keeps the task out of `DONE` rather than letting
it pass unverified.

A reviewer model, where one is used, is bounded to the evidence and is **evidence, never
truth**. It does not enter the verdict.

---

## 10. Revocation and expiry

Revoking a charter invalidates already-issued permits **in the same transaction**. A revoked
charter found on an observe tick raises a pause, which interrupts (or kills) the live
dispatch and resolves its root effect to `unknown`.

The distinction the routes encode: **a revoked charter must not stop the system from
recording what happened, only from doing more.** Reporting an execution, resolving an effect
and reconciling a dispatch continue to work after revocation. Claiming, dispatching, opening
an effect and issuing a capability grant do not.

An effect left in state `unknown` **pauses** and never re-enters the retry ladder. That is
the point of the state: an effect that may have landed and may be irreversible is not
something to retry. Clearing it is an operator action — see the escape hatches in
[docs/supervisor.md](supervisor.md).

---

## See also

- [docs/supervisor.md](supervisor.md) — running the supervisor, its two credentials, the
  operator escape hatches, and the one-time setup that would add UID separation.
- `shared/tce_shared/charter.py` — `CHARTER_FIELD_ENFORCEMENT`, `charter_constraints`,
  `validate_charter_payload`.
- `GET /v1/governance/status` — what is enforced right now on this installation, measured
  rather than claimed.
