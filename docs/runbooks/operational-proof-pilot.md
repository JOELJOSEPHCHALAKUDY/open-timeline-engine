# Operational Proof Pilot — Runbook

## Read this first: the arithmetic, before the procedure

Three arms × 30 closed episodes ÷ 0.80 close coverage = **113 enrolments per (project, decision
family) cell**. The cell axis is `(project, decision_family)`, so a second project needs its own
113, and a second decision family inside that project needs its own 113 again.

Measured on this workspace, not estimated: owner activity falls on **6 calendar days out of a
196-day span** (3.1%), and on **4 of the last 61 days** (6.6%). The window floor is **28 active
days** — days on which at least one episode was *enrolled*, not calendar days. At the recent rate
that is roughly **427 calendar days**; at the full-span rate, roughly **915**.

So, plainly:

- **The four-to-six week figure is a minimum observation window, not a completion date.** It is
  the floor below which nothing can be concluded. It is not the point at which something can be.
- **This runbook will not produce a verdict in six weeks.** It will produce an honest, specific
  statement of what is still missing — per arm, per decision family, per project — every week,
  from week zero.
- **One owner participates.** Every conclusion this pilot can reach applies to that owner and to
  the projects actually observed, and generalises to nobody else. The report prints that sentence
  on every run, whatever the numbers say.

If those three sentences are unacceptable, the correct response is to not run the pilot, not to
lower the floors. The floors are frozen in `shared/tce_shared/pilot_thresholds.py`
(`p6-thresholds-v1`, digest `0216694023456b1537287f9c540b7f6c`) and are not settings. A threshold
a caller can sweep is not a threshold.

---

## Step zero — preconditions

Each of these is verifiable before anything is enrolled. Do them in order.

1. **Rebuild the API container.** The live build predates P2 and serves 110 of the 131 paths in
   the tree; without this, step 5 returns 404.

   ```
   cd infra && docker compose build tce-api && docker compose up -d tce-api && cd ..
   ```

2. **Migrate.** The live database is at `20260909_0042` (P5's dream proposals); P6's
   `20260909_0043` is on disk, chains directly off it, and is unapplied. Until it is applied the
   report exits `2` naming the three missing tables, and no `/v1/pilot` route can write a row.

   ```
   .venv/bin/alembic -c infra/alembic.ini upgrade head
   docker exec open-timeline-engine-postgres-1 psql -U postgres -d tce -tAc \
     "select version_num from alembic_version;"     # expect 20260909_0043
   ```

3. **Enable enrolment.** `TCE_PILOT_ENROLLMENT_ENABLED=true` in `.env`, then restart the
   container. Leave `TCE_PILOT_HUMAN_BASELINE_ENABLED` **off** unless you are deliberately
   running an elected human-workflow episode (see "Claim B", below).

4. **Point the tools at the database.**

   ```
   export TCE_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/tce
   ```

   Every P6 command and every P6 corpus test refuses to run without it, by design. A test that
   skips when the database is absent passes without measuring anything, which is the failure mode
   this whole phase exists to prevent.

5. **Have the host-capture credential.** `~/.config/open-timeline-engine/host_capture.token`.
   Closing an episode requires a verified human — the same predicate a dream verdict requires —
   which means this credential, which means `scripts/tce_pilot.py` and **not** the MCP executor
   and not a bare API token. The executor may say what it did; only you may say what happened.

6. **Run the report before anything exists.**

   ```
   .venv/bin/python scripts/p6_pilot_report.py ; echo "EXIT=$?"
   ```

   Expect exit `0` and `NO ENROLLED EPISODES`. If it exits `2`, the tables are absent — something
   in steps 1–4 did not take. Stop and fix it; do not enrol into a database the report cannot
   read.

---

## Enrolling an episode — before you start the work, not after

```
$ .venv/bin/python scripts/tce_pilot.py enroll \
    --project proj_00cb00a314ca7c6302c10e32 \
    --family needs_human \
    --session <the session id you are about to work in> \
    --objective "fix the dashboard takeover scroll"
episode 4f0c…  arm=markdown_handoff  block=3 slot=1  allocated_at=2026-09-11T09:02:11Z
```

The arm comes back **after** it is committed. That ordering is the experiment: the allocation is
frozen before you know what it is.

Re-running the same command for the same objective in the same session returns **the same arm**
with `reused=true`. There is no way to re-roll. The allocation key is the work itself — P4's
`episode_key`, computed server-side from workspace, subject, project, session, objective hash and
cancel epoch — and there is no field on the enrolment request that lets a caller vary it. Changing
the objective text changes the work and therefore is a different episode, which is correct; it is
not a way to draw again for the same task.

The decision families are P1/P4's five plus one honest bucket: `safety_confirmation`,
`needs_human`, `operator_action`, `next_objective`, `policy_abstention`, `unclassified`. P6 adds
none.

### Then do the work under the arm you were given

| Arm | What you actually do |
|---|---|
| `existing_runtime` | Your usual agent. TCE MCP tools **detached**. No handoff document. |
| `markdown_handoff` | TCE detached. **You** write the handoff document by hand, from the template in the appendix. |
| `tce_assisted` | Normal TCE. |
| `owner_unassisted` | No agent. You do the work. Only reachable with `--elect`, and only with `TCE_PILOT_HUMAN_BASELINE_ENABLED=true`. |

**If you cannot run the assigned arm, do not re-enrol.** Run what you can and record the deviation
when you close. A deviation is data — the report has a deviation-rate clause and a floor for it. A
re-roll is a broken experiment with no trace of having been broken.

### Optional: the executor's own account

An executor may post `agent_asserted` observations against an episode it is running. They are
stored in their own table, labelled `producer_class=agent_asserted` on every row, and **no gate
clause reads them**. They exist because the executor's account of a run is genuinely useful when
you are reading a surprising cell — and they are quarantined because the party under test does not
grade itself.

---

## Closing an episode

```
$ .venv/bin/python scripts/tce_pilot.py close 4f0c… \
    --executed-arm markdown_handoff \
    --rescue none \
    --finished \
    --review-minutes 12 \
    --review-verdict accepted_with_edits
closed  deviated=false  completion_basis=task_state_done  independent=true  late_close=false
```

What you supply, and what the server works out for itself:

- **You supply** the arm you actually ran, the rescue level (`none`, `steered`, `took_over`,
  `abandoned_to_owner`), whether it finished, your review minutes and your review verdict
  (`accepted_as_is`, `accepted_with_edits`, `rejected`). Nothing in the runtime can observe "I
  gave up and finished it myself", so rescue and deviation are yours to state.
- **The server derives** `deviated` (executed arm vs assigned arm — it is never accepted from the
  request), `completion_basis` (P3 verification first, then P2 task state, then your attestation),
  `adjudication_independent` (whether the adjudicator is the principal that enrolled the episode)
  and `late_close`.

Your review minutes are self-reported and that is fine: the rule is not *no self-report*, it is
*the party under test does not grade itself*. Your account of your own review time is adjudication.
An executor's account of its own correctness is not, and there is nowhere to put one.

**Close whenever you get to it.** There is no expiry. `pilot_close_grace_days` sets a `late_close`
flag and nothing else. An episode you never close is counted as `excluded: unclosed=N` and depresses
close coverage — honest. An episode a 409 refused would be silently lost — not.

If `adjudication_independent=false` comes back, the close was written by the same principal that
enrolled the episode. The row is kept, and the report excludes it from every numerator under a
named exclusion counter. Independence is a property of the row, not a promise in a runbook.

---

## Adjudicating a dream proposal

Separate from acceptance, and deliberately so. Accepting a proposal says *I want this*. Adjudicating
it says *this was a reasonable thing to propose*. The second is not derivable from the first, and
the report never derives it.

```
$ .venv/bin/python scripts/tce_pilot.py adjudicate <proposal-id> \
    --relevance not_relevant \
    --blind \
    --rationale "about a project I closed in June"
adjudicated  blind_claimed=true  blind_verified=false (adjudicator_gave_prior_verdict)
```

Three things to know:

- **`--blind` is a claim, not a fact.** The server checks it against P5's append-only
  `dream_proposal_events` log: your judgement must strictly precede every human verdict on that
  proposal (`accepted`, `rejected`, `snoozed`, `unsnoozed`), and you must not be the person who
  gave one. If you already rejected it, you are not blind to your own rejection, and the response
  says so with a reason. The report counts `adjudications` and `blind_verified` separately and
  never treats the first as the second.
- **`cannot_judge` is a real answer.** It is in neither the numerator nor the denominator of the
  false-positive rate and is printed as its own count. Use it rather than guessing.
- **Changed your mind?** Adjudicate again with `--supersedes <adjudication-id>`. The table is
  append-only; the earlier row stays and only the latest one per proposal is counted, so revising
  a judgement moves the rate once rather than twice.

For a proposal that was actually pursued and delivered, you can also answer whether the delivery
turned out useful:

```
$ .venv/bin/python scripts/tce_pilot.py adjudicate <proposal-id> \
    --relevance relevant --delivery-useful useful
```

That answer is **stored always and counted only after 30 days from completion**. Answered sooner,
it is kept verbatim (it is your word and the report does not edit it) and reported under
`too_early_not_counted`. The word *later* in "later useful delivery" is a clock, and a same-day
"yes, useful" is enthusiasm rather than evidence.

---

## Weekly

```
$ .venv/bin/python scripts/p6_pilot_report.py | tee ~/pilot-week-$(date +%V).txt
```

Read three lines and skip the rest:

1. `window: active_days=N/28` — how much of the floor you have actually spent.
2. the per-cell `closed_per_arm` — how far the worst arm in the worst cell is from 30.
3. the shortfall list on the worst cell — the specific clause that is short and by how much.

The report names the **worst** cell, never an average. It refuses to pool across projects or across
decision families and prints `POOLING REFUSED` saying so. A weak segment is visible in this report
or it is not measured at all; it is never averaged away.

`GET /v1/pilot/report` returns the same computation over the same shared functions, so the API and
the script cannot drift into two answers.

---

## Stopping

Stopping is not a state and needs no command. **Stop enrolling.** The ledger is append-only and the
report keeps reading it.

`TCE_PILOT_ENROLLMENT_ENABLED=false` closes the enrolment route (404) and deliberately leaves the
close route open, so episodes already in flight can still be adjudicated. A rollback that discarded
pending adjudications would destroy exactly the human answers this schema exists to protect —
which is also why `downgrade()` on `20260909_0043` drops nothing.

If you stop and later resume: nothing needs restarting, and nothing needs re-enrolling. The window
clock counts active days, not elapsed ones, so a gap costs you nothing but the days you did not
work.

---

## What the report will and will not tell you

**It will tell you:**

- how many episodes are enrolled and closed, per arm, per decision family, per project;
- which specific clause is short, and by how much — `SHORTFALL(measured, floor)`, always with both
  numbers;
- how many episodes deviated from their assigned arm, and why;
- how many were rescued, and at which level;
- how much review time you spent;
- how many closes were independently adjudicated, and how many were not;
- for dreams: proposals, surfaced, attested-surfaced, the verdict census and the nonresponse census
  kept strictly apart, adjudications and blind-verified adjudications counted separately, and
  false-positive relevance computed from adjudications alone.

**It will not tell you:**

- **That TCE is better.** Not at any n below the floors, and not by rounding up to "trending
  positive". Below the floor the answer is `NOT ENOUGH EVIDENCE`, per cell.
- **Anything about human-level performance.** `owner_unassisted` is elected, never randomized — an
  allocator cannot randomize a person into doing the work himself — so Claim B's randomization
  clause fails by construction and the report prints, verbatim: *"No adequate human baseline was
  collected, so this result is described as REDUCED SUPERVISION and not as human-level
  capability."* This is not permanently closed; a genuinely randomized human-baseline block would
  satisfy the clause. Until then there is **no state in which this report says Claim B is
  supported** — the verdict type has nowhere to put one.
- **A cost total.** On this host both runnable surfaces report `unsupported` by protocol
  (`codex/exec` and `codex/app-server` carry no currency anywhere), so the cost clause resolves
  `NOT_COMPUTABLE(all_surfaces_unsupported)` and `cost_known=0/N` is printed per cell. A zero that
  means *we do not know* is never printed as a zero that means *free*.
- **Anything about anyone else.** One participant. The single-participant sentence is printed on
  every run and is not conditional on the verdict.
- **Whether anything may be promoted.** The report is read-only. It writes no qualification record,
  and no promotion, exposure, personalization or permit path reads it. That fence is asserted by a
  test, not by this paragraph.

A "no bad event was observed" at n=0 resolves `NOT_COMPUTABLE`, never `PASS`. The previous pilot
surface reported `safety_passed=True` over zero completed trials; that is now three-valued and
reads `not_computable` on an empty corpus.

---

## Appendix A — the `markdown_handoff` template

Write this **yourself**, by hand, before starting work in the `markdown_handoff` arm. TCE stays
detached. Its section structure mirrors `render_task_state_markdown`, which is the point of the
arm: it isolates *the structure of a good handoff* from *the machinery that produces one*.

```markdown
# <objective, one line>

## Where things stand
<what is already done, and what you know to be true>

## Constraints
<what must not change; anything that would break if touched>

## Next step
<the single next action, concretely>

## Open questions
<what you do not know, and who or what would answer it>
```

## Appendix B — troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Every route 404s | Container predates the route | Step zero, item 1 |
| `p6_pilot_report.py` exits 2 | Tables absent | Step zero, item 2 |
| `POST /v1/pilot/episodes` 404 | `pilot_enrollment_enabled` is false | Step zero, item 3 |
| `close` returns 403 | Called with an executor credential | Use `scripts/tce_pilot.py` with the host-capture token |
| `--elect owner_unassisted` refused | `pilot_human_baseline_enabled` is false | Enable it deliberately, and read the Claim B paragraph first |
| `blind_verified=false` on a claim you believe | A human verdict on that proposal precedes the adjudication, or you gave it | Read the reason in the response; it names which |
| `DREAMS: NOT_COMPUTABLE(dream_tables_absent)` | Database below `20260909_0042` | Step zero, item 2 |
| Report says `closed=0` and everything downstream is `NOT_COMPUTABLE` | Episodes are enrolled but not closed | Close them; nothing expires |
