# Task state, planning and retrieval deadlines

Operator guide to the P2 task-state layer: what the projection is, how planning runs, what an
executor must do when planning is pending, how cancellation actually behaves, and what the new
retrieval deadline labels mean on a dashboard.

Audience: whoever runs the stack and reads its telemetry. Every claim here is about observable
behaviour, not about intent. Where something is deliberately *not* guaranteed, it says so.

---

## 1. The task-state projection

### What it is

A task is a takeover session. `task_id == session_id` — there is no second identifier to keep in
sync.

Everything the engine believes about a task is derived, deterministically, from an append-only
event log in `task_state_events`. The derived answer is stored in one row of `task_states` and is
called the **projection**. Nothing else is authoritative: `takeover_context` on the wire carries a
mirror of two fields for convenience, and that mirror is never read to make a decision.

The projection answers seven questions:

| Field | Question it answers |
|---|---|
| `objective_text` / `objective_hash` | What is this task trying to do? |
| `contract_revision` | Which *version* of that objective is current? |
| `status` | Where is it (`awaiting_objective`, `planning`, `active`, `blocked`, `awaiting_decision`, `awaiting_verification`, `done`, `cancelled`)? |
| `next_permitted_action` | What is the engine willing to do next? |
| `plan` | The recorded decomposition of the objective into steps. |
| `unresolved_effects` | What has been started and not yet closed out? |
| `latest_verification` | What evidence exists that the work is actually finished? |

### The two revisions, and why there are two

They count different things and are not interchangeable.

* **`revision`** — the compare-and-swap counter on the `task_states` row. It increases by one on
  every successful write, including writes that change nothing an operator would care about. Its
  only job is to make concurrent writes safe: a writer states the revision it read, and the update
  applies only if the row is still at that revision. A losing writer gets HTTP **409**, not a
  silent overwrite.
* **`contract_revision`** — how many times the *objective itself* changed. It increases only when a
  new objective hash arrives. Advancing through the steps of an existing plan does **not** bump it.

Read `revision` when you are debugging a write conflict. Read `contract_revision` when you are
asking "is this plan, permit or verification still about the same job?"

A 409 on `POST`-shaped task-state work is a normal, expected outcome under concurrency, not an
error to alert on. Alert on a *rate* of them, not on their existence.

### Truncation

The fold reads at most `MAX_TASK_STATE_EVENTS_PER_FOLD` (2000) events. When a task has more, the
retained window is the last `2000 - 3` events **plus** the pinned kinds — the latest
`objective_set`, `objective_cleared` and `cancellation_requested` — so a very long task can never
lose its own objective or its cancellation. When this happens the projection reports
`truncated: true`. That is informational; the derived status is still correct for the pinned facts.

An event kind this build does not recognise is **counted, never fatal**. It appears in
`unknown_kinds` and the fold continues. This is deliberate: a rolled-back deploy must not make a
task unreadable.

---

## 2. Planning

### The two producers

| Producer | When | Where it runs |
|---|---|---|
| `deterministic` | The default, in both backends | Inline, on the request thread. Pure CPU. |
| `model` | Full only, and only when `TCE_TAKEOVER_PLAN_LLM_ENABLED=true` | The worker, out of process. |

The deterministic planner emits the same three read-only steps for every objective: read the
current state, report what is true, ask for the missing decision. It is **never** mutating, and
there is no flag or caller-supplied way to make it mutating.

### `planning_pending` — when it can actually be true

`planning_pending` is true only while an asynchronous planning job is outstanding. Asynchronous
planning follows the model planner:

```
effective_planning_async_enabled = planning_async_enabled
                                   if explicitly set,
                                   else takeover_plan_llm_enabled   (default: false)
```

So in the **default configuration `planning_pending` is never true**, in either backend. Lite has
no worker, no Redis and no model gateway, so it can never emit it at all. If you are seeing
`planning_pending` you are running Full with `takeover_plan_llm_enabled=true`, and that is the only
way to see it.

### What `planning_pending` means for an executor

It means: *there is no executable directive for the current objective revision yet, and there may
not be one for a second or two.* The plan that existed a moment ago has been invalidated, or none
has ever existed for this contract revision.

The MCP layer turns this into an explicit instruction rather than leaving it to the model's
judgement. `tce.takeover_step` returns:

* `has_directive: false`
* `planning_pending: true`
* `planning_job_id` — the job to look up if you want to see why it is slow
* `planning_pending_hint_ms` — roughly how long to wait before calling again (default 1500)
* `next_step` — a non-empty, actionable string beginning **`PLANNING IN PROGRESS`**

The correct executor behaviour is: tell the user planning is running, do not start work, do not
fall back to normal chat, and call `tce.takeover_step` again after roughly `planning_pending_hint_ms`.

`next_step` is non-empty on purpose. Under the executor policy (`CLAUDE.md` rule 18) a natural
conversational response is permitted only when `has_directive=false` **and** `next_step` is empty.
Keeping `next_step` populated is what stops an active takeover from silently degrading into chat
while a plan is being produced.

### A live directive always wins

The planning branch sits **below** the directive branch and additionally requires that no execution
lock is held. Concretely, the executor is told to poll only when all of these are true:

* `planning_pending` is true, and
* `execution_permit_required` is false, and
* `execution_claim_required` is false, and
* `pending_execution` is absent, and
* `directive_state` is neither `pending` nor `in_progress`.

The last two are the execution-lock state that `CLAUDE.md` rule 18a defines. Without them, a
directive that had already been claimed and was `in_progress` could have been hidden behind a
"poll again" message. It cannot be. If there is work to do, you are told to do the work.

### The `no-execute-while-planning-pending` constraint

On turns where `planning_pending` is true, the tool result carries one extra machine-readable
constraint alongside the standing ones:

```json
{
  "directive_type": "hard_constraint",
  "rule_id": "no-execute-while-planning-pending",
  "scope": {"actions": ["edit", "write", "delete", "execute"]},
  "enforcement": "block_and_escalate",
  "reason": "planning_pending=true means no approved plan exists for the current objective revision..."
}
```

It ships **only** on those turns. The standing `HARD_CONSTRAINTS` list is copied per turn and the
conditional entry is appended to the copy — the module-level list is never mutated. This matters
more than it looks: that list is handed out by reference on every active turn, so a single append
would tell every session in the process, for the life of the process, never to mutate anything
again. If you ever see `no-execute-while-planning-pending` on a turn where `planning_pending` is
false, that is a real bug and worth reporting.

### Inspecting a job

`GET /v1/planning/jobs/{job_id}` reports `state`, `attempts`, `last_error` and `next_attempt_at`.
States are `pending`, `leased`, `succeeded`, `failed`, `discarded`, `cancelled`.

* `discarded` with `stale_input_revision` — the objective changed while the job was running. The
  result was thrown away on purpose. This is correct behaviour, not a failure.
* `lost_lease` — the job's lease expired and another worker took it, or the task was cancelled
  mid-flight. The completion was fenced out and wrote nothing.
* `failed` with `unparseable_plan` — the model returned something the parser could not use. The
  job fails; it does **not** silently substitute a deterministic plan, because a deterministic plan
  labelled `producer=model` would be a lie in the record.

Retries back off 5 s, 10 s, … capped at `planning_job_backoff_cap_seconds` (900).

---

## 3. Cancellation, stand-down and reactivation

### What cancellation actually does

Cancelling a task (`POST /v1/tasks/{task_id}/cancel`) does five things:

1. Appends `cancellation_requested` to the event log and re-folds the projection to `cancelled`.
2. Flips pending `directive_executions` rows for the session to `cancelled`.
3. Expires outstanding execution permits for the session.
4. Marks outstanding planning jobs `cancel_requested`, so their completion is fenced out.
5. Best-effort cancels the queued RQ job.

### What it does not do

**Cancellation is "stop dispatching and reject the report", not "abort in flight."**

An executor that is already mid-edit in another process is not reachable. It will finish its edit
and land its change. What happens is that its later `report_execution` is rejected by the claim
fence, and no further work is dispatched. If you need an in-flight editor stopped, stop the
editor — the engine cannot do it for you. This is residual **R-1** below.

### Stand-down, then reactivate

`beru stand down` (`tce.reset_takeover_state`) is the supported end of a session, and re-activating
on the **same** `session_id` afterwards is the supported way to start again. It works because the
terminal-cancel rule is time-ordered: a `cancellation_requested` event only makes the task
`cancelled` while it is *later* than the most recent `objective_set`. A new activation appends a new
objective, which is later, so the task revives with a fresh contract revision, and no constraints,
open decisions or unresolved effects are inherited from the cancelled contract.

One field is deliberately exempt from that revival: `last_cancel_seq`, the **cancel epoch**, is
never cleared. It feeds the planning idempotency key. Without it, re-issuing an identical objective
after a cancel would compute the same key, hit the existing cancelled job row, and wait forever for
a plan that will never be produced. With it, the re-issue mints a fresh job.

---

## 4. The Markdown view

`GET /v1/tasks/{task_id}/state.md` returns a JSON body whose `content` field carries a Markdown
rendering of the projection. It is JSON, not `text/markdown`, so that the Full and Lite OpenAPI
documents stay identical.

Over MCP the same thing is a **resource**, not a tool:

```
tce://workspace/{workspace_id}/task/{task_id}/state.md      mime: text/markdown; charset=utf-8
```

It is a resource on purpose. Resources are read by a human or pulled in as context; they do not
appear in the executor's tool vocabulary and never pass through the takeover firewall.

### Reading the document

Frontmatter first, between `---` fences, one JSON-encoded value per line. The lines an operator
actually uses:

| Key | What to do with it |
|---|---|
| `projection_id` | Stable id for this (workspace, task, contract revision). Quote it in a bug report. |
| `source_revision` | Digest of the exact event set this render folded. Two renders with the same value are the same document. |
| `content_sha256` | SHA-256 of the **body only**, computed before the frontmatter was prepended. Recompute it to prove the body was not edited. |
| `revision` / `contract_revision` | The two counters from §1. |
| `source_evidence_ids` | The citations, as a YAML list. Always UUIDs — non-UUID candidates are dropped by the fold and counted, so this list never has to be sanitised downstream. |
| `truncated` | The plan table was cut at `task_state_markdown_max_steps` (24). |
| `read_only` | Always `true`. |
| `projection_learning_eligible` | Always `false`. |

`read_only: true` and `projection_learning_eligible: false` are not configurable. Authority stays in
the database, and the projection must never be re-ingested as evidence — otherwise the engine ends
up learning from its own summaries.

`redaction_applied` is always `false`, and that is honest rather than lax: the renderer prints only
the projection's own typed fields, never free-text event payloads, so there is nothing to redact.

Then the body, in a fixed section order so two renders diff cleanly:

```
# Task <task_id>
## Objective              text, objective_hash, contract_revision, revision
## Status                 status, next permitted action
## Approved constraints   table: id, kind, scope digest, expires
## Open decisions         question + alternatives
## Plan                   table: #, title, status, depends_on, attempts, mutating
## Unresolved effects
## Latest verification
## Citations
```

Every section prints `_none_` when empty. The shape never varies, which is what makes the document
diffable across time.

The view is gated on `task_state_markdown_enabled` (default true); with it off the endpoint 404s.
Every render writes an audit entry carrying `projection_id`, `source_revision` and `content_sha256`.

---

## 5. What the plan is, and what it is not

Read this section before wiring anything to `mutating`.

**The plan records and reports progress. It does not authorize or constrain execution.**

* `autonomy_goals.mutating` and `TakeoverGoal.mutating` are **descriptive records, never
  authorization signals.** The field says what kind of step the planner wrote down. It is not
  consulted by the permit path, the claim fence, or `_is_mutating_intent`. Do not build a gate on
  it; the authorization path is unchanged by P2 and is the only thing that decides what may run.
* With the model planner off — the default — every plan in both backends is the three-step read-only
  diagnosis, while the directive `takeover_step` mints is still derived from the *objective* by the
  untouched mint path. So `next_permitted_action: execute_step` can legitimately ship alongside a
  read-only plan, and the executor will work the objective. That is not a contradiction; it is the
  consequence of the plan being a record rather than a governor. Making the plan govern execution
  is P3 work. (Residual **R-9**.)
* **Verification is task-level, not step-level.** A verification must name the current contract
  *and* the current plan to count, which closes the case where a verification from an older
  objective made a new one look done. It does **not** require one verification per step: a plan
  whose steps all completed, carrying one passing verification against that plan, reaches `done`.
  Per-step verification refs are P3. (Residual **R-6**.)

---

## 6. Retrieval deadlines

One `Deadline` is started per turn, seeded from `takeover_turn_budget_ms` (3500). Retrieval gets a
child of it, created at the retrieval call site, budgeted by `context_retrieval_budget_ms` (120).
The child can never outlive the parent.

With `retrieval_deadline_enabled=false` the child **is the parent, unchanged** — no guard is
installed at all. The knob genuinely switches the feature off; it does not quietly leave a 120 ms
cap in place.

### The first query of a turn is unguarded

Deliberately. It runs with no statement timeout and no progress handler, so it cannot be aborted by
the budget. Only *secondary* queries — trigram, ANN, Qdrant, entity multi-hop, activation, feedback,
handoff, episode — are skipped when the remaining budget is below `retrieval_statement_floor_ms`.

This is why `queries_executed` is always at least 1 on a turn that retrieved anything, and why a
tight budget degrades breadth rather than degrading everything.

### `retrieval_meta` keys

Ten keys, identical in Full and Lite (values differ; the key set does not):

```
deadline_policy_revision   deadline_budget_ms      deadline_backend_budget_ms
deadline_remaining_ms      deadline_expired        queries_executed
queries_timed_out          queries_skipped         queries_cached
retrieval_degraded
```

`queries_cached` is legitimately always empty in Full: Full never reads or writes `context_bundles`.
That is residual **R-3**, not a bug in your deployment.

### Labels

| Label | Where | Meaning |
|---|---|---|
| `deadline_partial` | `retrieval_source` | Retrieval completed with at least one query skipped or timed out. The answer is real but narrower than usual. |
| `deadline_expired_skip` | `retrieval_reason` | A secondary query was skipped because the budget was gone before it started. |
| `deadline_expired_abort` | `retrieval_reason` | A guarded query was cut off mid-flight by the statement timeout (Postgres) or progress handler (SQLite). |

`retrieval_degraded: true` and `context_quality_score` multiplied by 0.75 accompany either of the
last two. A turn that completes its primary query and skips its secondaries typically scores around
0.60 — visibly degraded, and above the 0.55 threshold at which an executor is expected to stop
guessing and ask.

### Retired reason code — action required if you have dashboards

**`budget_exceeded_skip_deliberation` no longer exists on the Full backend.** The `budget_exceeded`
block that produced it there is gone, replaced by the turn-deadline advisor gate. Any alert, panel or
saved query keying on that string against Full will silently match nothing from P2 onward.

**Move those alerts to `deadline_expired_skip`.** If you also care about mid-flight aborts, alert on
`deadline_expired_abort` separately; they mean different things (budget gone before the query vs.
budget gone during it).

**Lite still emits it.** P2 does not install the turn-deadline advisor gate in Lite (§5.3's Lite row
does not list it), so Lite's pre-existing `context_retrieval_budget_ms` guard is untouched and still
reports `budget_exceeded_skip_deliberation`. A dashboard that reads both backends should match either
string until Lite's advisor gate lands. Tracked as a known Full/Lite divergence, not a bug.

### What the turn budget does and does not bound

**Bounded:** every retrieval read on the takeover path, the advisor dispatch decision, and the
advisor's socket timeouts.

**Not bounded:** authentication and scope resolution, safety evaluation and classification (all pure
CPU, microseconds), **every write** (the directive insert, the action record, the audit write and the
task-state CAS), and the Redis enqueue on the asynchronous planning path.

Writes are never gated. Gating a write would let the API report a revision that was never persisted,
which is a worse failure than overrunning a budget. A pathologically slow write can therefore still
overrun `takeover_turn_budget_ms`; it is *recorded* (`deadline_expired: true`) rather than prevented.

---

## 7. Full and Lite

Same tables, same job rows, same wire shape. One difference:

**Lite's only planner is deterministic and inline.** Lite has no worker, no Redis, no timer and no
model gateway. `enqueue_planning_job` runs the deterministic planner in-process and writes a
`planning_jobs` row with `state='succeeded'`, `producer='deterministic'` in the same transaction.

Because Full's default is now also the inline deterministic planner, **the divergence is invisible
in the default configuration.** It appears only when a Full operator sets
`takeover_plan_llm_enabled=true`.

One behavioural fix worth knowing if you consume resume packets: `ResumePacketAnchor.stale`
previously differed between backends for a current anchor (`None` in Full, `False` in Lite). Both
now use Full's semantics — **`None` when `freshness == "current"`**. If you have a client that
treated Lite's `False` as meaningful, it needs to treat `None` the same way.

---

## 8. Settings

`TCE_`-prefixed, identical names in both backends' configs.

| Setting | Default | What it does |
|---|---|---|
| `takeover_turn_budget_ms` | 3500 | Seeds the one turn deadline. |
| `task_state_enabled` | true | Master switch for the projection. |
| `task_state_markdown_enabled` | true | Gates the `state.md` endpoint and resource. |
| `task_state_markdown_max_steps` | 24 | Plan table cut-off in the Markdown view. |
| `planning_async_enabled` | unset | Unset derives from `takeover_plan_llm_enabled`. |
| `planning_job_lease_seconds` | 120 | Worker lease length. |
| `planning_job_max_attempts` | 3 | Before a job goes terminally `failed`. |
| `planning_job_batch_size` | 20 | Jobs per worker tick. |
| `planning_job_backoff_cap_seconds` | 900 | Retry backoff ceiling. |
| `planning_pending_hint_ms` | 1500 | Poll hint handed to the executor. |
| `retrieval_deadline_enabled` | true | False installs **no** guard at all. |
| `retrieval_deadline_floor_ms` | 5 | Minimum meaningful slice. |
| `retrieval_statement_floor_ms` | 10 | Below this remaining, skip the secondary query. |
| `retrieval_advisor_min_ms` | 250 | Below this remaining, skip the advisor. |
| `sqlite_progress_instructions` | 1000 | Lite progress-handler granularity. |

Full only, and both flipped to **false** by default in P2:
`takeover_plan_llm_enabled`, `takeover_dream_llm_enabled`. They now select whether the **worker** may
use a model, not whether the request thread may. Lite has neither.

---

## 9. Residuals

Ten known limitations. All ten are accepted for P2 and stated here rather than discovered later.

**R-1 — cancellation does not stop an out-of-process executor.** Covered in §3: pending directives
are flipped to `cancelled` and the later report is rejected by the claim fence, but an executor
already mid-edit lands its change. Stop dispatching and reject the report — not abort in flight.

**R-2 — orphaned thread-pool work in Full.** `future.cancel()` is a no-op on a running future, so
the search and bundle executors keep blocking after a join times out. P2 records the abandonment in
the ledger; pool sizing is a follow-up.

**R-3 — two caches are still not fully scoped.** The worker-side gateway cache is keyed on text
only, and Full never reads or writes `context_bundles`, so `queries_cached` is always empty in Full.
Both are out of P2's scope.

**R-4 — with the model planner off (the default), every plan is the three-step read-only
diagnosis.** That is weaker step *text* than the old fallback, and it is the direct consequence of
never inventing a mutating plan under uncertainty. It does not disable autonomy — the directive and
its permit gate are unchanged — but a Full operator who wants rich mutating plans must set
`takeover_plan_llm_enabled=true`, which also turns `planning_pending` on. Lite has no such option by
design. See R-9 for the sharper version.

**R-5 — the turn budget bounds waiting, not writes.** §6 states exactly what is covered. A
pathologically slow write can still overrun the budget; it is recorded, not prevented.

**R-6 — verification is task-level, not step-level.** §5. A plan whose steps all completed and
which carries one passing verification against that plan reaches `done`. Per-step verification refs
are P3.

**R-7 — the deadline guard has its own overhead.** Four extra round trips per guarded read, about
1.14 ms each against a loopback Postgres, across roughly ten guarded secondary queries per turn —
about 11 ms of a 120 ms retrieval child. Against a *networked* Postgres the same round trips can
approach half the budget. The first query pays none of it (it is unguarded). The fix not taken: one
`SET LOCAL` per retrieval *segment* rather than per query, restored once at the segment's end.

**R-8 — Lite serialises the CAS section behind the SQLite write lock.** The `BEGIN IMMEDIATE`
bracket covers only the compare-and-swap section, so retrieval and the advisor no longer hold the
lock. Two concurrent Lite turns still serialise on the CAS itself, and `busy_timeout` is never
lowered — a short busy timeout manufactures retryable "database is locked" strings exactly where the
budget is tightest — so a contended CAS section can wait up to 5 s, longer than the 3500 ms turn
budget. Lite is a single-operator runtime; this is accepted, not fixed.

**R-9 — the default plan is descriptive, not governing.** §5, in full. The plan records and reports
progress; it does not authorize or constrain execution. `mutating` is a descriptive record, never an
authorization signal.

**R-10 — the planning enqueue is not deadline-bounded.** `enqueue_job` reaches Redis with a 1 s
connect and 1 s socket timeout under three RQ retries, and receives no deadline slice, so an
unreachable Redis can block for seconds on a path otherwise described as deadline-bounded. The
failure mode is bounded (the turn continues, `queue_state` becomes `queue_unavailable`, the
scheduler tick picks the job up) but the *wait* is not. Unreachable in the default configuration,
since `planning_pending` requires `takeover_plan_llm_enabled=true`. The fix not taken: pass a
deadline slice into a per-call Redis client, which would defeat the shared client's memoisation.

---

## 10. Quick triage

| Symptom | Look at |
|---|---|
| Executor says "planning is running" forever | `GET /v1/planning/jobs/{job_id}`. A `discarded`/`stale_input_revision` loop means the objective keeps changing. A `failed`/`unparseable_plan` means the model output is unusable — turn `takeover_plan_llm_enabled` off to fall back to the deterministic planner. |
| Executor falls into normal chat during an active takeover | Check `next_step` is non-empty in the tool result. If it is empty with `planning_pending: true`, the MCP process is stale — restart it. |
| Cancelled a task, the file still changed | Expected. R-1. Stop the editor process. |
| Re-issued the same objective after a cancel, nothing happens | Should be fixed by the cancel epoch. Confirm a **new** `planning_jobs` row was created rather than the cancelled one being reused. |
| Alerts on `budget_exceeded_skip_deliberation` went silent | Retired **on Full**; Lite still emits it. Match `deadline_expired_skip` as well. §6. |
| Frequent 409s from task-state writes | Two writers on one session. Normal under concurrency; alert on rate, not existence. |
| `queries_cached` always empty in Full | Expected. R-3. |
| Plan says read-only, executor edits files anyway | Expected. R-9 — the plan is a record, not a gate. The permit path is what governs. |
