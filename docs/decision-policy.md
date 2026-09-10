# The decision policy — what it decides, what it is allowed to say, and what it does not claim

Open Timeline Engine keeps a record of decisions its owner has made and, on a takeover turn,
can try to reproduce one. **The decision policy** is the single piece of code that turns that
record into an answer: `shared/tce_shared/decision_policy.py::decide()`.

This document is written for an operator who has to decide whether to trust it. It is
deliberately blunt about four different things a number or a status on this surface can mean:

- **measured** — produced by a stated procedure against real data, and reproducible;
- **deterministic** — computed by code with no model in the path, but not itself evidence of
  accuracy;
- **uncalibrated** — a number on a 0–1 scale that is *not* a probability and has never been
  checked against outcomes;
- **not claimed** — named here so it is not mistaken for one of the above.

The last category is not empty. Read [What is not claimed](#7-what-is-not-claimed) before you
act on anything on this page.

---

## 1. The one thing to know first

> **No decision family is qualified today. Personalization is therefore not used on any turn,
> and every takeover turn behaves exactly as it did before this policy existed.**

That is the designed, expected and correct state — not a fault, not a partial rollout, and not
something to switch on. "Qualified" means a specific `(project, decision_family)` pair has
cleared a frozen evidence bar (§4). Zero pairs have cleared it, because the number of
adjudicated prospective cases in every family is **0**, and the bar is 100.

What follows from that, concretely:

| | Today |
|---|---|
| Does the policy run on every turn? | **Yes.** It computes a full result and records it. |
| Does its answer change the turn? | **No.** `exposed=false` on every result. |
| Does it escalate turns to a human? | **No.** An unqualified family is a reason not to personalize, never a reason to stop. |
| Is anything calibrated? | **No.** Nothing in this system is calibrated (§5). |
| What would change that? | Adjudicated prospective cases, then a passing report (§4). |

The two halves of that — *the policy decided something* and *the policy was allowed to say it*
— are separate fields on the wire, `status` and `exposed`, and they are separate on purpose.
Collapsing them into one value is what turned an unqualified family into a human escalation in
an earlier draft of this design, which would have been a product shutdown, not a safety
measure. There is a test that fails if a turn that succeeds today becomes an escalation
(`tests/integration/test_policy_parity.py::test_no_turn_that_succeeds_today_becomes_an_escalation`).

---

## 2. Where the policy sits

```
your turn  ->  evidence loader          rows the owner's history already contains
           ->  decide()                 ONE policy: rank, abstain, report OOD and conflict
           ->  _exposure()              may this family use personalization at all?
           ->  the turn                 exposed=false  -> unchanged, exactly as before
                                        exposed=true   -> the policy's answer is used
```

Two properties are structural rather than conventional:

- **`decide()` performs no retrieval.** The caller passes the rows its own loader returned.
  That is what lets the evaluation harness call the *same function* with a frozen training set
  — the route that is deployed is the route that is evaluated. Before P4 those were disjoint:
  the thing with abstention and citations had no consumer, and every live turn was decided by
  a model prompt that nothing had ever scored.
- **`_exposure()` reads no setting.** Refusal is the default and the *absence* of a
  qualification record **is** the refusal. There is no flag that turns exposure on. The only
  way to expose a family is to record a qualification for it, and the only way to record one is
  to pass the gate.

---

## 3. Reading a `policy_decision` block

Every takeover result carries `policy_decision` — over HTTP on `TakeoverStepResponse`, and over
MCP on the slim tool result. It has eleven fields and no more; the MCP layer projects it onto
exactly those eleven and drops anything else.

| Field | Means |
|---|---|
| `status` | `selected` \| `abstained` \| `rule_applied` — what the policy concluded |
| `selected_option` | the chosen option, or `null` |
| `abstain_reason` | why it declined (§6), or `null` |
| `reason_for_asking` | the sentence to show a human when it declines |
| `ood_status` | whether this turn resembles anything in the record (§6) |
| `conflict_status` | `none` \| `split_vote` \| `explicit_contradiction` |
| `evidence_observation_ids` | up to 12 **real** observation ids that were actually used |
| `decision_policy_revision` | `p4-2026-09` — which policy produced this |
| `exposed` | **whether the answer was allowed to be used at all** |
| `exposure_state` | why not, when not |
| `advisor_agreement` | whether the model advisor agreed, disagreed, abstained, or was absent |

**`exposed` is the field that matters.** `exposed=false` means the block is *reporting only*:
the policy is being scored in the shadow, and the turn ran as if it did not exist. It is not an
instruction to stop.

`exposure_state` names the specific refusal:

| `exposure_state` | Means |
|---|---|
| `no_qualification` | no qualification record for this family — **today's answer for every family** |
| `qualification_expired` | there was one; it aged out (90 days) or the run is past its expiry |
| `binding_mismatch` | a record exists but the model, runtime, retrieval version, prompt digest, threshold digest, tuning digest, family or project has changed since it was measured — so the measurement no longer describes what is running |
| `evidence_drift` | the learning-eligible corpus for this family has grown more than 50% since the measurement |
| `rule_layer` | an explicit owner-written rule matched; that path never consults the qualification record |

`rule_layer` is the one route that acts without a qualification. It fires only on rules the
owner wrote himself, in `memory_rules`. **That table is empty today**, so the path is currently
unreachable — but it is real, and the first row written to it changes a turn with no
qualification record anywhere. It is listed here rather than buried because it is the single
exception to §1.

There is **no score on this block**, deliberately. `policy_score` exists internally, it is
uncalibrated, and an executor reading a number it cannot interpret is how four different fields
in this system came to be named some form of "confidence". It does not cross the MCP boundary,
and the projection in `services/tce_mcp/tce_mcp/tools.py` drops it even if a backend sends it.

---

## 4. Qualification — how a family earns permission, and how it loses it

A `(project, decision_family)` pair earns permission by clearing **every** clause below. The
thresholds are frozen module constants in `shared/tce_shared/policy_thresholds.py`, hashed into
`THRESHOLDS_SHA`, and bound onto every qualification record. They are not settings; there is no
environment variable that moves any of them.

| Clause | Bar | Today |
|---|---|---|
| adjudicated prospective cases | ≥ 100 | **0** |
| non-abstained precision, Wilson 95% lower bound | ≥ 0.90 | not computable |
| coverage (non-abstained / adjudicated) | ≥ 0.30 | not computable |
| distinct episodes | ≥ 20 | 0 |
| duplicate-context ratio | ≤ 0.25 | not computable |
| paired lift over **all three** baselines, McNemar exact | lower bound > 0, p ≤ 0.05 | **not computable** — the baselines have nothing to run against |
| split reproduces from the stored episode assignment | exact | n/a |

The verdict is per family and per project, and a failing verdict prints its **shortfall list**,
not an error and not a zero. `NOT_QUALIFIED` with `adjudicated 0 < 100` is the report working
correctly. Run it with:

```
.venv/bin/python scripts/p4_qualification_report.py
```

### The first run, verbatim

```
P4 PROMOTION GATE — live corpus
thresholds_version=p4-thresholds-v1  thresholds_sha=e1ea2984591366fe5a62e3ee690f080d
thresholds_effective_at=2026-09-10T00:00:00+00:00

needs_human: NOT_QUALIFIED
  adjudicated=0  prospective_rows=46
  excluded: unresolved=45 advice_shown=1 no_candidates=0 pre_registration=0

next_objective: NOT_QUALIFIED
  adjudicated=0  prospective_rows=2
  excluded: unresolved=2 advice_shown=0 no_candidates=0 pre_registration=0

safety_confirmation: NOT_QUALIFIED
  adjudicated=0  prospective_rows=7
  excluded: unresolved=6 advice_shown=1 no_candidates=0 pre_registration=0

RESULT: no family is qualified, so personalization is NOT USED on any decision and every
turn behaves exactly as it did before P4.
```

Every family also prints the same seven shortfalls — `adjudicated 0 < 100`,
`precision_lower_bound 0.0 < 0.9`, `coverage 0.0 < 0.3`, `distinct_episodes 0 < 20`, and all
three baselines `not_computable`. A baseline that cannot be computed is reported as such and is
never treated as beaten.

### What the exclusion counters actually say — and it is not what was predicted

The counters exist so a first run does not read as a regression. Read them, because the measured
answer overturns the one that was assumed before the gate existed:

- **`cases_excluded_unresolved` is the binding constraint, and nothing else is close.** Of the 55
  prospective rows across all three families, **53 were never answered** — `resolution_state` is
  still `pending` or `unanswered`, or `actual_choice` is null. The design anticipated that
  *advice contamination* would be the blocker; measured, it excludes **2 rows**. The thing
  standing between this system and a qualifiable corpus is that the human's answer is not being
  captured and written back, which is a far cheaper problem than suppressing advice on an
  interleaved sample.
- `cases_excluded_advice_shown` — a case where the text the human answered already named one of
  the options they were choosing between. 1 row in `needs_human`, 1 in `safety_confirmation`.
- `cases_excluded_pre_registration` — cases frozen before `THRESHOLDS_EFFECTIVE_AT`
  (2026-09-10 UTC), excluded from every denominator because the thresholds were chosen while that
  traffic already existed. **0 today**, since every stored shadow row is excluded earlier, as
  unresolved.
- `cases_excluded_no_candidates` — a turn that offered fewer than two options cannot be scored as
  a choice. 0 today for the same reason.
- `no_candidate_set_at_freeze_site` — a named **structural** shortfall for `next_objective`, whose
  freeze site passes `alternatives=[]`. Its coverage is pinned at zero by construction. Naming
  that impossibility is the deliverable; the policy does not invent candidates to fill it.

**Losing it.** A qualification expires after 90 days. It is also invalidated the moment any
bound key changes — model id, runtime version, retrieval version, advisor prompt digest,
threshold digest, tuning digest — and demoted if precision over the last 50 cases falls below a
0.85 lower bound. All of those surface as `exposure_state`, on the turn, immediately.

---

## 5. Nothing is calibrated

There is no calibrated score in this system and none ships. The identifier `calibrated_score`
appears nowhere in `shared/` or `services/`, and a test keeps it that way
(`tests/unit/test_no_fabricated_decision.py::test_no_calibrated_score_symbol_exists`).

What was specified at one point was an in-sample isotonic fit with no train/test split, on a
corpus with zero adjudicated cases. That is not a calibration; it is a curve drawn through the
data it is scored on. It was removed rather than shipped.

So:

- **`policy_score` is an uncalibrated vote share.** It is a deterministic function of weighted
  neighbour agreement. It is not a probability. 0.8 does not mean "right 80% of the time" —
  nobody has ever measured how often it is right.
- **`CloneAdviceResponse.confidence` is that same number**, kept under its existing name because
  it is a published wire field, and carrying the description *"uncalibrated vote-share
  heuristic; not a probability"*. Do not read it as a probability, do not threshold on it, and
  do not put it in front of a user as a percentage.
- **A calibration map becomes possible** when a single `(project, decision_family)` reaches 100
  adjudicated prospective cases split into a fit fold disjoint from the gate fold. Today that
  count is 0 for every family.

The report prints a diagnostic section instead — effective sample size per family, the topical
overlap histogram, and the `policy_score` histogram — so the shape of the distribution is
visible even though no claim is attached to it.

---

## 6. Abstention, out-of-distribution, and conflict

The policy declines rather than guesses. Every one of these is a **reported status**; on an
unexposed turn none of them changes what happens.

| `abstain_reason` | Means |
|---|---|
| `no_eligible_evidence` | nothing in the record was usable as of this decision's timestamp |
| `no_candidate_match` | **fewer than two options were offered** — today's answer on almost every turn |
| `inadequate_evidence` | neighbours exist but are too few or too weak: fewer than two above the 0.30 similarity floor, or Kish effective sample below 1.5, or the winning option holds less than a 0.60 share |
| `out_of_distribution` | no neighbour shares enough topic with the question |
| `conflicting_evidence` | the record genuinely disagrees with itself |
| `advisor_forced` | the model advisor failed to parse, or abstained |

Three of those are worth an operator's attention.

**An empty candidate set abstains.** Before P4 the code returned an option nobody had offered:
the mapping helper opened with "if there are no allowed options, return the model's string
unchanged". That is the fabrication route, and it is closed — an empty candidate set now
abstains with `no_candidate_match`, before the mapper is reached. The live record is empty of
candidates about 94% of the time, so this is the common case, and under §1 the turn is
unchanged by it.

**`ood_status` is computed from topical overlap alone** — raw token overlap between the question
and each neighbour, and nothing else. It reads no clock, no source and no self-reported number.
That is a correction, not a preference: the previous ranking score drew 45% of its range from
terms unrelated to the question, so a semantically unrelated but *fresh* row scored above the
floor and `ood_status` was effectively reporting freshness. The floor is 0.20, measured against
real pairs:

```
0.0667  "force push to prod to fix the stripe webhook"  vs  "ssh into the box and restart nginx"
0.1333  ...                                             vs  "i keep meaning to rewrite the scheduler in rust"
0.3077  ...                                             vs  "deploy the payment webhook fix to production now"
0.6667  ...                                             vs  "the stripe webhook is failing, do i force push the fix to prod"
```

0.20 sits in the gap. At 0.10 the unrelated scheduler row is "in distribution"; at 0.30 a
genuine paraphrase is rejected.

**No model's self-report reaches a score.** An advisor's own confidence number used to be
written into an evidence column and read back as 5% of a similarity weight, one hop later. That
term is deleted, and an AST-level test asserts that no scoring function reads a field named
`confidence` (`tests/unit/test_no_fabricated_decision.py::test_no_self_report_reaches_a_score`).
The model can still contribute — it agrees, disagrees, abstains, or is absent, and that shows up
as `advisor_agreement` — but it cannot move a number by asserting one. A model that fails to
parse forces abstention unconditionally; there is no setting that softens that.

---

## 7. What is not claimed

- **Nothing here is calibrated, and no accuracy is claimed.** §5.
- **No family has been shown to beat a baseline.** The paired test has never been computable.
  "Not qualified" is honest ignorance, not a measured failure — the policy has not been shown to
  be worse either.
- **`THRESHOLDS_SHA` is a change detector, not a pre-registration.** It can prove two runs used
  different thresholds. It cannot prove the thresholds preceded the data, because
  `THRESHOLDS_EFFECTIVE_AT` is an editable constant with no external anchor. What it buys is that
  admitting pre-registration traffic takes a visible edit to a digested constant rather than
  happening silently. A real anchor — a signed commit timestamp checked in CI, or a server-clock
  registration row — is out of scope here and is named as such.
- **The deployed and evaluated routes are the same callable, not yet the same inputs.** Three
  divergences survive by construction: the model advisor is present live and absent in replay;
  three separate evidence loaders share one hardcoded retrieval version string; and candidate
  options have four producers. A request fingerprint is captured live and recomputed in replay
  so a divergence is *detected* and printed. It is not removed.
- **The promotion gate may not be reachable as currently wired.** The gate admits only
  prospective cases where the advisor's answer was *not* visible, and today the only producer of
  that flag sets it true on every advisor turn. So the adjudicated counter may never move without
  a product change — either interleaving some turns with advice suppressed, or admitting
  advice-visible turns with a stated contamination adjustment. This is an open decision about the
  owner's own turns, not a bug to patch, and it is recorded here rather than hidden behind a zero.
- **The MCP constraint is a cooperative protocol.** §8.

---

## 8. The MCP surface

`policy_decision` reaches an executor over the `tce.takeover_step` tool result. Two operator
facts:

**The MCP layer is loaded at process start.** Upgrading TCE does not update a running MCP
server: the slim result is an explicit whitelist, and a stale process keeps dropping a key the
API has started sending. **Restart the MCP client (Claude Desktop, Codex, Cursor) after any TCE
upgrade**, or `policy_decision` will simply be absent with no error anywhere.

**One conditional constraint rule ships with it**, `no-execute-on-policy-abstention`. It tells
an executor: do not edit, write, delete or execute; show `reason_for_asking`; wait for the
human. It is appended to the per-turn constraint list — never to the process-global list — and
only when **both** of these hold:

```
policy_decision.exposed is true   AND   policy_decision.status == "abstained"
```

Both conjuncts are load-bearing. `status == "abstained"` alone is the state of essentially every
turn today, so a rule without the `exposed` half would fire on every turn and forbid all work.
Since no family is qualified, **this rule does not appear on any turn today.**

What it does and does not enforce, stated exactly: it is a cooperative protocol, like every
other rule in that array. Nothing in the MCP process, in either backend, or in the operating
system stops an executor that ignores it. The one structural property that does hold is narrow:
`tools.py` is loaded into the MCP server at process start, so an executor editing it on disk
does not change the constraints its own running session receives. That is a reload boundary, not
a sandbox. OS-level enforcement exists only for a process tree the supervisor started under an
enforcement tier — see [docs/charter.md](charter.md).

---

## 9. Operator checklist

| Question | Where to look |
|---|---|
| Is any family qualified? | `scripts/p4_qualification_report.py`, or the `policy_qualifications` table. Today: none. |
| Why was personalization not used on this turn? | `policy_decision.exposure_state` on the turn result. |
| Why did the policy decline to choose? | `policy_decision.abstain_reason`, and `reason_for_asking` for the human-readable form. |
| Which evidence was actually used? | `policy_decision.evidence_observation_ids` — real ids, resolvable in `decision_observations`. |
| An executor is not seeing `policy_decision` at all | Restart the MCP client. §8. |
| A qualified family suddenly stopped being used | `exposure_state`: `qualification_expired`, `binding_mismatch` (something in the stack changed) or `evidence_drift`. |
| Can I turn exposure on to try it? | No, and that is the design. Record a qualification by passing the gate; there is no switch. |

## Related

- [Authority charters](charter.md) — what an autonomous run is permitted to do, and which tiers are actually enforced.
- [Behavior Fidelity v1](behavior-fidelity.md) — the evaluation vocabulary and the longitudinal claims that remain unproven.
- [Clone advisor](clone-advisor.md) — the model-facing surface whose output the policy now bounds.
