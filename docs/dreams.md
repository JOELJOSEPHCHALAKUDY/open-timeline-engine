# Aspiration proposals

Operator guide to the P5 proposal layer: what a proposal is, where its words come from, what
each state means, why "he never answered" is tracked separately from "he said no", why nothing
is ever deleted, and why — on the corpus that exists today — this system will refuse to produce
a single proposal, and that refusal is the system working.

Audience: whoever runs the stack and reads its telemetry. Every claim here is about observable
behaviour, not about intent. Where something is deliberately *not* guaranteed, it says so.

---

## 1. What a proposal is

A **proposal** is a short statement, in the owner's own words, of something he keeps returning
to. It has four written parts — a `title`, a `connection` (why the system thinks this is a
theme), a `benefit`, and a `first_step` — and it carries **citations**: verbatim quotes from
messages the owner himself is receipted as having typed, each with the event it came from and
when it was said.

The system **proposes**. It never pursues on its own. Accepting a proposal is what mints a plan
root; nothing about a proposal starts work by itself.

Two properties are worth stating up front because they are what the layer is for:

* **A proposal cannot exist without quotes.** A candidate whose citations do not survive
  validation is not shown with a lower score — it is not emitted at all. There is no weak
  emission path and no confidence knob.
* **Completion is read, never inferred.** A proposal becomes `completed` only when the task
  projection for its bound task reports `done` *and* the objective hash still matches. Creating
  a plan is not finishing anything, and the system no longer pretends otherwise.

---

## 2. Where the words come from — and why almost nothing qualifies

This is the deepest rule in the layer, and it is the reason the honest answer today is "no
proposals".

The timeline event corpus is **executor-writable**. An AI executor holding an ordinary API token
can write events into it. If proposals quoted from that corpus, an executor could write a
sentence, and the system would later quote that sentence back to the owner as *something he
said*. That is forgery, and it is the exact class of failure the trusted-capture layer exists to
prevent.

So the proposal corpus is **not** the event corpus. A message may enter the candidate pool only
when a `trusted_input_receipts` row binds it to the subject, written by the host-capture
credential — a credential the MCP executor process is structurally barred from holding. There is
no `task_type` route into the pool and no "owner" context field route into the pool. The receipt
is the only admission.

Receipts carry one of two origins, and the difference is visible on screen:

| `origin_kind` | Means | Rendered as |
|---|---|---|
| `human_input` | The owner typed this, captured live through the trusted channel. | `attribution = "you said"` |
| `imported_transcript` | Labelled backfill: imported history, receipted after the fact. | `attribution = "from your imported history"` |

A proposal built only from backfill has `evidence_basis = "backfill_only"` and is **never**
rendered as something the owner said today. This is not a wording convention that a future
prompt can drift away from: `attribution` is a field, derived from the evidence basis, and it is
asserted in tests.

### What that means on this stack, measured

Re-measured against the live database on 2026-09-10:

```
trusted_input_receipts                       8   (all origin_kind = 'human_input')
receipts carrying a project_id               8   (none is workspace-scoped)
distinct subjects across those receipts      6
events with task_type 'human_input'         17
events with task_type 'human_input_backfill' 4,649
```

`dream_min_messages` is **10**. The pool cannot reach it. Every scope refuses with
`refusal_reason = "insufficient_messages"`, writes a `dream_generation_runs` row saying exactly
that, and mints nothing.

**That is correct, and it is not a bug to route around.** The 4,649 backfill events are the
executor-writable corpus described above; admitting them would mean quoting an executor's words
back to the owner as his own. Making them quotable is a separate, deliberate piece of work — a
one-time host-capture-credentialled import that writes `trusted_input_receipts` rows with
`origin_kind = 'imported_transcript'` — and it is explicitly **out of scope** for this layer.

An earlier version of this feature did generate on that corpus. It produced confident,
generic, consultant-sounding text, and it was rejected. The refusal you see now is the fix.

**How to read a "no proposals" result.** Do not read it as "quiet week". Read the run row:
`state`, `refusal_reason`, `pool_size`, `pool_drops` and `refusals` are populated on every run,
including refused ones. A generation path that is entirely broken must not look like a system
with nothing to say, so the refusal is always written down.

---

## 3. The seven devices, and which one is a blocklist

Every candidate the model returns passes through seven checks. Failing **any** of them drops the
candidate (or, for the per-citation checks, drops the citation and then re-checks the count).

| | Device | What it refuses |
|---|---|---|
| **M0** | **Receipt-bound corpus.** Admission is by `trusted_input_receipts` only (§2). | An executor writing a fabricated "owner message" and having it quoted back. |
| **M1** | **Verbatim quote containment.** Every quote must be a contiguous span of the cited message, whitespace-normalised, with at least three content tokens. | A model picking message numbers at random. A number can be guessed; a verbatim span cannot. |
| **M2** | **Quote/claim overlap, over the title alone.** The title must share at least `dream_min_quote_overlap_tokens` content tokens with the quotes. `first_step` is deliberately **not** on the left side — the model writes both `first_step` and the choice of quote, so including it would let the model manufacture its own overlap. | Generic text bolted onto real citations. |
| **M2b** | **Per-citation relevance.** Each surviving citation must independently share a content token with the title. | A proposal that cites evidence from unrelated work and presents it as one theme. |
| **M3** | **Banned vocabulary and voice, on the claim *and* on every quote.** Consultant words (`comprehensive`, `leverage`, `stakeholder`, `roadmap`, `synergy`, …) and Title Case prose. | Slide-deck voice — including pasted marketing prose arriving inside a quote. |
| **M4** | **No counts reach the generator.** The generator's input carries no row counts at all; the arithmetic producer that used to derive "themes" from tallies is deleted from the tree. | Row-count maintenance suggestions, *by construction* rather than by taste. |
| **M5** | **Two-stage citation validation.** Once before generation, once immediately before the proposal crosses the wire. Citations are deduplicated by content hash, not by event id. | A proposal whose evidence has since been redacted or re-scoped; the same message counted twice because it was ingested twice. |
| **M6** | **Harness text is not owner text.** Pool messages containing known harness strings (`[Request interrupted by user`, `This session is being continued from a previous conversation`, `<system-reminder>`, …) are dropped at admission. | A transcript-tool artefact quoted back as something the owner said. |

**M6 is a blocklist, and that is a real limitation.** It matches the harness strings that exist;
it cannot catch a paste the harness did not author. The structural half of that problem is M0
(the corpus itself) and the presentational half is M3-on-quotes. What remains — the owner pasting
third-party prose into his own message, and the model quoting it — is **not solved**.

None of this makes the model's *reading* correct. It makes an emitted proposal **traceable**:
every sentence on screen is backed by words the owner is receipted as having typed, and the
reader can see them. Whether the reading is a good one is what accept and reject are for.

---

## 4. The state vocabulary

`status` is one axis. It answers *what happened to this proposal*.

| Status | Means | Written by |
|---|---|---|
| `proposed` | Minted. Not yet shown to anyone. | generation |
| `surfaced` | Put in front of the owner at least once. | display record (§6) |
| `accepted` | The owner said yes. | verified human |
| `rejected` | The owner said no. | verified human |
| `snoozed` | The owner said not now, until `snooze_until`. | verified human |
| `pursued` | Work on the bound task has actually started. | pursuit reconciler |
| `completed` | The bound task reached `done` with a matching objective hash. | pursuit reconciler |
| `abandoned` | The bound task's objective changed out from under it, or pursuit stopped. | pursuit reconciler |
| `withdrawn` | Its citations no longer verify; it was pulled rather than shown weakened. | sweep |
| `expired` | Past `expires_at` without an answer. | sweep |

Two things this list does **not** contain, on purpose:

* There is no status meaning "ignored". See §5.
* There is no status meaning "done because a plan was created". `completed` requires the task
  projection to say `done`; a plan with no step events leaves the proposal at `accepted`.

`expired` is reported separately from `rejected` everywhere. A proposal that timed out is not an
answer.

---

## 5. Nonresponse is a separate axis, and why

`nonresponse` is the second axis, and it is the point of the whole redesign.

| `nonresponse` | Means |
|---|---|
| `never_surfaced` | Nobody has ever put this in front of the owner. |
| `awaiting_response` | It has been shown, fewer than `dream_nonresponse_after_surfaces` times, and not answered. |
| `ignored` | It has been shown at least that many times and still not answered. |

It is derived from **explicitly recorded `surfaced` events and from nothing else**. The clock is
not an input. A proposal that was never shown reads `never_surfaced` a year later — because
nothing about it was ignored; it was never shown.

Before this layer existed there was a single nullable "acknowledged" timestamp that meant "not
seen", "seen and ignored" and "never surfaced" all at once, which made silence
indistinguishable from refusal. That is the defect this axis removes.

**What `ignored` does.** Exactly one thing: it lengthens the re-surface interval, from
`dream_resurface_min_hours` (24) to `dream_resurface_ignored_hours` (168). So an ignored
proposal comes back a week apart instead of a day apart.

**What `ignored` does not do.** It does not suppress the theme. It does not count as a rejection
in any metric or suppression rule. It does not shorten `expires_at`. Time alone never becomes an
answer, in either direction.

---

## 6. Who is allowed to say what

There are two kinds of statement about a proposal and they have different requirements.

**A verdict** — `accepted`, `rejected`, `snoozed`, `unsnoozed` — requires a **verified human
identity**: an authenticated principal in the user role that is either identity-verified or
holds the host-capture capability. An executor gets **403**.

In practice that means the verdict requires the **host-capture credential**, and therefore a
client that holds it.

> **Not shipped yet.** The intended surface is a `tce dreams` command group in
> `plugins/tce_cli_capture` — the one client that already holds the host-capture credential:
>
> ```
> tce dreams list
> tce dreams accept   --id <proposal_id>
> tce dreams reject   --id <proposal_id> --reason "not this quarter"
> tce dreams snooze   --id <proposal_id> --snooze-days 14
> tce dreams unsnooze --id <proposal_id>
> ```
>
> That group does not exist in this build. Until it lands, `accepted`, `rejected`, `snoozed` and
> `unsnoozed` are reachable only by calling `POST /v1/dreams/{proposal_id}/transition` directly
> with a host-capture token, which means `surfaced_count`, `NonresponseState` and the §8.7
> measurement have no everyday producer. Stated here rather than left to be discovered: the
> vocabulary is implemented and enforced end to end; the operator's front door for it is not.

An ordinary API bearer that merely *asserts* `X-TCE-Role: user` is not identity-verified and is
correctly refused. The MCP tool cannot do it either, and never will (see §7).

**A display record** — `surfaced` — is different, and this is a deliberate asymmetry. `surfaced`
is not a verdict; it is the statement *"this was put in front of the owner"*, and the thing that
puts it in front of him is the executor's renderer. Requiring a human to attest that a machine
displayed something would make the field unreachable and collapse nonresponse back into
rejection. So any authenticated principal in scope may post `surfaced`, the event records
`actor_class` (`human` | `executor` | `system`), and the projection carries `surfaced_attested`
— true only if at least one `surfaced` event came from a verified human.

**The abuse ceiling, stated plainly.** An executor spamming `surfaced` can drive `nonresponse`
to `ignored`, whose only effect is a *longer* re-surface interval. It cannot reject anything,
cannot suppress a theme, cannot expire anything early, and cannot appear as the owner's answer.

Re-surfacing is rate-limited: a second `surfaced` inside the current re-surface interval records
nothing and returns no new revision. A polling client cannot inflate the count.

---

## 7. The MCP surface

**`tce.dreams(session_id, action, proposal_id)`** — two actions, and only two.

* `action="list"` returns the open proposals **and then records `surfaced` for exactly the ones
  it returned**. The ids marked come back in `surfaced_recorded`. Listing is what puts them in
  front of the owner, so listing is what records the display; the `GET` route itself never
  writes, which keeps the write in one auditable place instead of hiding it in a read that
  someone will later cache.
* `action="surfaced"` with `proposal_id` records a single display.

**The tool cannot accept, reject, snooze or unsnooze, and does not offer those actions.** The MCP
process authenticates as an executor and is structurally barred from the host-capture credential,
so a verdict posted from there would be refused every time; a tool that offered an action it
will always be 403'd on is worse than one that does not. Passing a verdict verb returns a
refusal naming the CLI.

**`dream_proposals_pending`** is one integer on the takeover step result: how many proposals are
waiting for an answer. It is information, not a directive. No proposal text crosses that
boundary, and P5 contributes **no constraint rule** — the constraint list on an active turn is
unchanged by this layer.

**A Claude Desktop (or Codex / Cursor) restart is required** before either shows up. The MCP
layer is loaded at process start and the slim result is an explicit whitelist: a stale MCP
process keeps dropping the new key and keeps not exposing the new tool, however current the API
is. If `tce.dreams` is missing after an upgrade, restart the MCP client first.

Note also that the tool profile decides what is exposed. The default profile is `all`; a
narrower profile exposes only the tools named in it.

---

## 8. History is never deleted

`dream_proposals` is a **fold** of an append-only log in `dream_proposal_events`, the same shape
the task-state layer uses. The row is derived; the log is the truth.

The guarantees, and what enforces each:

* **Refresh never deletes.** There is no `DELETE FROM dream_proposal*` anywhere in the tree. The
  previous implementation deleted prior proposals on every refresh, which is why there was
  nothing to learn from.
* **No blind update.** The only `UPDATE dream_proposals` statement in either backend carries a
  compare-and-swap on `revision`. A lost race is a **409** naming the proposal and both
  revisions, not a silent overwrite.
* **No migration drops a table holding human answers.** The P5 migration's `downgrade()` is a
  **documented no-op** for `dream_proposals`, `dream_proposal_events` and
  `dream_generation_runs`. A rolled-back deploy leaves every accept and reject readable. The
  `upgrade()` is `CREATE TABLE IF NOT EXISTS` throughout, so downgrade-then-upgrade is still a
  clean round trip for CI.
* **Retiring the old rows destroys nothing.** The legacy goal rows the previous dream path wrote
  are not deleted; they are simply no longer read.

A proposal is never edited in place. A revision of a proposal is a **new** proposal that
supersedes the old one, and the old one keeps its own history.

This is why the layer exists at all: before it, there was no place to put "no", so there was
nothing to learn from. `dream_proposal_events` is where the answer to *"is any of this useful?"*
will eventually come from.

---

## 9. When a rejected theme may come back

A rejection suppresses its theme. Themes are compared on **tokens** (Jaccard over the content
tokens of title, first step and quotes), not on a hash, because a hash cannot express
"near-duplicate".

Rejection suppression is checked **before** the duplicate-merge path, so a rejected theme cannot
sneak back by being merged into a live proposal it happens to resemble. It is also checked
**across scope kinds**: a theme rejected while working unbound is the same theme when it comes
back inside a project.

To reopen a rejected theme, **all five** conditions must hold. The run row names the first one
that failed, so an over-suppressing system is visible instead of looking like a quiet week.

| Condition | Requirement |
|---|---|
| `repropose_limit_reached` | The theme has been re-proposed fewer than `dream_max_reproposals` times. |
| `new_citations` | At least `dream_material_new_citations` citations the rejected proposal did not have. |
| `evidence_predates_rejection` | Those new citations were said **after** the rejection. Evidence that already existed when the owner said no is not new evidence — he rejected the theme while it existed. |
| `theme_too_similar` | The reworded theme is not simply the same theme again (similarity at or below `dream_material_max_similarity`). |
| `cooldown_not_elapsed` | At least `dream_rejected_cooldown_days` have passed. |

Two settings must stay in step, and a test asserts it: `dream_rejected_lookback_days` (365) must
be at least `dream_rejected_cooldown_days` (30), or a rejection would age out of the scan window
before its cooldown expired and suppression would lift by amnesia.

---

## 10. Settings

Identical names and defaults in the Full API, the Lite API and the worker.  Environment prefix
`TCE_` (so `dream_min_messages` is `TCE_DREAM_MIN_MESSAGES`).

| Setting | Default | What it decides |
|---|---|---|
| `dream_proposals_enabled` | `true` | Whether the routes answer at all (503 when off). |
| `takeover_dream_llm_enabled` | `false` | **Shipped off.** With it off, a refresh writes a real `refused` / `model_disabled` run row rather than returning silence. |
| `dream_message_limit` | `60` | Candidate pool size ceiling. |
| `dream_min_messages` | `10` | Below this, refuse `insufficient_messages`. **This is the floor today's corpus cannot reach.** |
| `dream_min_message_chars` | `25` | Shorter messages are dropped from the pool (`too_short`). |
| `dream_max_message_chars` | `1200` | Per-message truncation for the prompt. |
| `dream_max_proposals_per_run` | `3` | Ceiling on candidates parsed from one model reply. |
| `dream_min_citations` | `2` | Fewer surviving citations than this and the candidate is dropped. |
| `dream_max_citations` | `6` | Ceiling per proposal. |
| `dream_quote_max_chars` | `200` | Maximum length of a single quote. |
| `dream_min_quote_overlap_tokens` | `2` | M2 floor, title against quotes. |
| `dream_min_citation_relevance_tokens` | `1` | M2b floor, per citation. |
| `dream_max_live_proposals` | `20` | Above this, refuse `too_many_open_proposals`. |
| `dream_duplicate_similarity` | `0.60` | Duplicate / rejected-theme similarity threshold. |
| `dream_material_new_citations` | `2` | Material-change rule 1. |
| `dream_material_max_similarity` | `0.60` | Material-change rule 3. |
| `dream_rejected_cooldown_days` | `30` | Material-change rule 4. |
| `dream_rejected_lookback_days` | `365` | How far back the rejection scan reaches. |
| `dream_rejected_scan_limit` | `200` | How many rejections the scan reads. |
| `dream_max_reproposals` | `2` | Material-change rule 5. |
| `dream_nonresponse_after_surfaces` | `3` | Surfaces before `awaiting_response` becomes `ignored`. |
| `dream_resurface_min_hours` | `24` | Re-surface interval while awaiting a response. |
| `dream_resurface_ignored_hours` | `168` | Re-surface interval once ignored. Must exceed the line above. |
| `dream_snooze_default_days` | `7` | Snooze length when the request omits one. |
| `dream_proposal_ttl_days` | `45` | How long an unanswered proposal lives before `expired`. |
| `dream_run_stale_minutes` | `30` | After this, an abandoned generation run is swept to `failed` / `run_abandoned` and its slot is reusable. |
| `dream_list_limit` | `10` | Page size of the list route. |

---

## 11. Reading a refused run

Every refusal is recorded in `dream_generation_runs`, and the vocabularies are closed and
disjoint.

**Run-level** (`refusal_reason` — the whole run produced nothing):
`model_disabled`, `model_unavailable`, `model_call_failed`, `unentitled_project`,
`insufficient_messages`, `too_many_open_proposals`, `run_already_in_flight`, `run_abandoned`,
`stale_input_revision`, `lost_lease`.

**Candidate-level** (`refusals_json` — a per-reason count of candidates the devices dropped):
`unparseable_payload`, `no_citations`, `citation_out_of_range`, `quote_not_found`,
`quote_voice_violation`, `citation_not_relevant`, `insufficient_citations`, `no_quote_overlap`,
`voice_violation`, `empty_required_field`, `duplicate_theme`, `suppressed_rejected_theme`,
`citations_unverifiable`.

**Pool-level** (`pool_drops_json` — a per-reason count of messages the pool filter dropped):
`undecryptable`, `too_short`, `duplicate_message`, `harness_text`, `truncated_paste`.

Two you will see on this stack today: `insufficient_messages` (§2) and, with the shipped
default, `model_disabled`.

The frozen pool for a run is stored as an ordered index → `(event_id, receipt_id, content hash,
origin kind, timestamp)` mapping **with no message text**, so a citation number stays auditable
after the fact without the run carrying a second copy of the owner's words.

`unentitled_project` deserves a note: a project id is client-assertable, so a project-scoped run
is refused unless the subject actually has a receipt for that project. Without that check, a
caller could bind a run to a project they have never spoken into.

---

## 12. What this layer does not guarantee

* **It does not guarantee the proposals are any good.** It guarantees they are traceable. The
  first real measurement of usefulness is the owner's own accept/reject rate, and there has never
  been one, because until now there was no way to record an answer.
* **It does not generate on the current corpus.** Eight receipted human messages against a floor
  of ten. See §2.
* **`ignored` is not a judgement.** It is a re-surface interval.
* **M6 is a blocklist**, with the limits stated in §3.
* **The constraint an executor receives is cooperative.** Nothing in the MCP process, in either
  backend, or in the operating system makes an executor obey a tool description. What does hold
  is that this code is loaded at process start, so an executor editing it on disk does not change
  what its own running session receives. That is a reload boundary, not a sandbox.
* **Lite never generates.** The Lite API has no model gateway; its refresh route returns the
  identical response model with `state = "refused"`, `refusal_reason = "model_unavailable"`, and
  writes a run row saying so. Every other route — storage, transitions, the fold, validation,
  suppression, the pursuit reconciler — is fully functional in Lite.

---

## 13. Related

* `docs/task-state.md` — the task projection a pursued proposal's completion is read from.
* `docs/charter.md` — what the enforcement tiers do and do not enforce.
* `docs/mcp-setup-walkthrough.md` — MCP client configuration, including the restart this layer
  needs before its tool appears.
