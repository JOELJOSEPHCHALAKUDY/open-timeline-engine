# Behavior Fidelity v1

Behavior Fidelity v1 measures whether Open Timeline Engine can reproduce future user decisions from prior evidence. It does not claim to clone a person, consciousness, or general human behavior.

- Maintainer: `JOELJOSEPHCHALAKUDY`
- Research revision: `2026-07-22`
- Implementation baseline: `master` merge commit `0b4ed15bda96`
- Validation status: mechanisms implemented; behavioral and continuity benefit not yet established by the required longitudinal pilot

## Research Method and Status

This document distinguishes three kinds of claim:

- **Implemented** means the behavior was verified against the baseline code and tests.
- **Proposed** means a research-informed design that is not present in Full or Lite.
- **External finding** means a result reported by a cited primary source; it is not treated as a TCE result.

Implementation and efficacy are separate. The handoff outbox, resume packet, evidence contract, storage gate, and prospective evaluator can exist in code while their practical benefit remains unproven. The continuity runbook requires at least four weeks of longitudinal use before claiming improvement.

Specific audit clarifications:

- DeepSeek V4 section 5.2.5, "Trajectory Logging and Preemption-Safe Resumption," explicitly describes DSec's globally ordered command/result log, cached-result fast-forwarding, protection against re-running non-idempotent operations, fine-grained provenance, and deterministic replay. The attribution is supported by the paper, but applying that design to TCE is **proposed**; TCE does not currently implement a complete action-level replay journal. Source: [DeepSeek V4](https://arxiv.org/html/2606.19348).
- `CAPABILITY_REGISTRY` contains nine internal authorization categories. These classify operations for the capability broker; they do not filter or reduce MCP tool discovery. At this baseline the MCP server separately registers 69 tools, and lazy capability-based tool grouping is **not implemented**.
- A versioned current-state ledger and a complete execution journal are **proposed** layers. Existing event hashes, idempotency keys, completion outbox, and handoff records are foundations, not equivalent implementations.
- Numeric readiness scores are not evidence. Cross-executor continuity must remain "implemented, longitudinal validation pending" until the pilot meets capture, correct-file, correction, latency, and outbox-health gates.

## What Is Evaluated

The evaluator uses a chronological expanding-window split. For each held-out decision, it may use only evidence recorded earlier in time.

Metrics:

- top-1 and top-3 choice agreement
- pairwise preference accuracy
- Brier score and expected calibration error
- abstention rate and non-abstained precision
- wrong-case abstention rate
- workflow/action similarity
- outcome-regret proxy
- lift over a situation-type majority baseline
- recent-versus-early accuracy for drift visibility

The production gate currently requires at least 30 held-out cases and 10 distinct normalized contexts. It uses Wilson 95% lower bounds: top-1 accuracy must have a lower bound of at least `0.65`, non-abstained precision must have a lower bound of at least `0.70`, Brier score must be at most `0.25`, and the conservative lower bound for lift over the per-situation majority baseline must be at least `0.05`. A trivial “always choose the common option” dataset cannot unlock autonomy.

## Evidence Contract

Record explicit evidence through `POST /v1/behavior/evidence` or `tce.record_behavior_evidence`:

```json
{
  "situation_type": "prioritization_needed",
  "situation_summary": "Choose between a minimal fix and a broad refactor",
  "objective": "Fix production without unrelated regression risk",
  "context_snapshot": {"component": "api"},
  "constraints": {"risk": "production"},
  "available_choices": ["minimal verified fix", "broad refactor"],
  "selected_choice": "minimal verified fix",
  "rationale": "Keep the change reversible and verify the affected package",
  "action_taken": "Patch the focused module and run scoped tests",
  "outcome": "Tests passed",
  "outcome_sentiment": "positive",
  "memory_class": "preference",
  "evidence_source": "explicit",
  "confidence": 0.95,
  "schema_version": "v1"
}
```

Supported memory classes:

- `decision`
- `fact`
- `preference`
- `safety_constraint`
- `procedural_runbook`
- `episode`
- `hypothesis`
- `rejected_hypothesis`

Supported evidence sources:

- `explicit`
- `inferred`
- `correction`
- `calibration`
- `backfill`

Explicit, correction, and calibration evidence require a rationale. Correction evidence also requires `correction_text` and `supersedes_observation_id`.

Behavior evidence is scoped by `X-TCE-Workspace` and `X-TCE-Behavior-Subject`. `X-TCE-User` remains the event owner/executor identity. Codex and Claude can therefore keep distinct owner IDs while sharing one human profile by sending the same behavior-subject ID. If the new header is absent, it defaults to `X-TCE-User` for backward compatibility. Legacy observations are conservatively assigned to their original consumer identity during migration; they are not automatically merged across executors.

## Lifecycle and Drift

Evidence supports:

- `valid_from` and `valid_until`
- `confirmed_at`
- `supersedes_observation_id`
- `contradicts_observation_ids`
- `active`, `superseded`, and `rejected` lifecycle states

Superseded, rejected, expired, future-dated, and learning-ineligible records are excluded from prediction, clone advice, and evaluation. Raw records remain available for audit and deletion workflows.

## Storage Gate

Every evidence record receives a deterministic storage score. Source quality, rationale, outcome, alternatives, action, confidence, and memory class contribute to the score.

Modes:

- `shadow`: store the record; low-quality evidence remains audit-only
- `warn`: same behavior, plus response warnings
- `enforce`: reject evidence below the learning threshold with HTTP 422

The gate controls whether a record may influence behavior. It does not silently turn every tool call into a user preference.

## Prediction and Clarification

`POST /v1/behavior/predict` ranks choices using active earlier evidence. It returns citations, confidence, and a predicted action. When confidence is below the configured floor, it returns:

- `predicted_choice=null`
- `abstained=true`
- `needs_clarification=true`
- a focused clarification question

When candidate choices are supplied, the predictor can only return one of those choices. Historical labels that cannot be mapped safely are ignored and may cause abstention. Responses also expose effective neighbor count and an out-of-distribution diagnostic.

If autonomy gating is enabled and no valid fidelity run exists, prediction and takeover pause instead of guessing.

## Calibration

Cold-start calibration is optional and disabled by default. It provides fixed decision scenarios covering production risk, speed versus quality, ambiguity, and failure response. Answers are stored as confirmed calibration evidence.

Calibration is supplementary. It must not replace observed decisions and outcomes.

## Research Finding: File-Oriented Memory

### What Anthropic's design actually shows

Anthropic does not use HTML as a special behavioral-memory format. Its Messages API memory tool exposes client-side file operations under `/memories`, while Managed Agents exposes workspace-scoped UTF-8 text documents through filesystem-like mounts. Official examples use Markdown, plain text, and XML. HTML is technically possible text content, but it is not the mechanism that provides continuity.

The Managed Agents layer adds controls that do not exist in a plain folder:

- stable memory IDs independent of path names
- SHA-256 compare-and-swap preconditions for concurrent updates
- immutable, attributed versions
- workspace-scoped stores
- read-only and read-write mounts
- version redaction and archival lifecycle

Sources: [Anthropic memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool), [Managed Agents memory stores](https://platform.claude.com/docs/en/managed-agents/memory), and [memory update API](https://platform.claude.com/docs/en/api/beta/memory_stores/memories/update).

The filesystem is therefore an agent-facing compatibility interface over governed storage. It is not evidence that an unversioned `memory.html` or `memory.md` file is sufficient for long-term behavioral fidelity.

### Relevance to behavioral fidelity

File-oriented memory can improve fidelity indirectly by making evidence easier to inspect, navigate, correct, and transfer between executors. Recent work provides supporting evidence for maintained topic documents and hierarchical navigation:

- [Infini Memory](https://arxiv.org/abs/2606.10677) groups related evidence into maintainable topic documents rather than isolated fragments.
- [HORMA](https://arxiv.org/abs/2606.11680) links hierarchical summaries to raw trajectories and retrieves minimal sufficient context.
- [Everything is Context](https://arxiv.org/abs/2512.05470) treats filesystem organization as an accountable context interface with explicit construction, loading, and evaluation stages.

These results support a document projection over TCE evidence. They do not show that document storage itself improves prediction of a person's future decisions.

Behavioral fidelity still depends on:

- evidence quality and temporal ordering
- correct subject identity
- representative decision coverage
- alternatives, rationale, action, and observed outcome
- supersession and contradiction handling
- calibration and abstention
- prospective held-out evaluation

### Main fidelity risk: circular reinforcement

An agent-written summary can create a feedback loop:

1. The agent infers a preference from limited evidence.
2. It writes the inference into a persistent file.
3. A later executor reads the file as authoritative context.
4. The executor follows the inferred preference.
5. The resulting action is captured as new supporting evidence.

This produces apparent agreement without independent evidence from the user. Evaluation can then overstate fidelity because the predictor helped create the behavior it later predicts.

To prevent this:

- Generated documents must remain projections, not new learning evidence.
- A projection must cite the canonical evidence IDs used to generate it.
- Reading or following a projection must not count as independent confirmation.
- Agent-written files default to `inferred` or untrusted status and require review before learning eligibility.
- Predictions used for evaluation may only use evidence available before the held-out decision.
- Exposure to a memory item must be logged so agreement caused by that exposure can be separated from independent user behavior.

### HTML-specific finding

HTML is useful as a generated human-review format because it can provide clickable citations, timelines, tables, trust labels, and evidence drill-down. It is a poor canonical behavioral-memory format because markup adds token cost and creates hidden-content, script, event-handler, external-resource, and prompt-injection surfaces.

For browser activity, raw HTML is also too noisy to be the default memory representation. [Mind2Web](https://openreview.net/forum?id=kiYqbO3wqw) reports that real-world raw HTML is often too large for direct model input. [Prune4Web](https://arxiv.org/abs/2511.21398) describes DOM inputs in the 10,000 to 100,000 token range and reports large reductions from programmatic pruning. Browser checkpoints should normally retain a normalized accessibility or semantic snapshot, URL, active element, screenshot hash, action, and outcome. Raw HTML may be retained as short-lived, quarantined evidence behind a citation.

### Memory-poisoning finding

Persistent files can convert one transient injection into cross-session influence. Anthropic explicitly warns that fetched web content, user prompts, or third-party tool results can poison writable memory stores and recommends read-only access where writes are unnecessary.

Recent research demonstrates the same risk under different threat models:

- [eTAMP](https://arxiv.org/abs/2604.02623) poisons later web-agent behavior from a contaminated environmental observation without direct memory access.
- [GhostWriter](https://arxiv.org/abs/2607.06595) targets the write and later activation stages of personal-agent memory.
- [MemPoison](https://arxiv.org/abs/2607.14651) evaluates direct, compositional, and dormant context-triggered corruption.

The reported attack rates are study-specific and should not be treated as universal production rates. The attack class is nevertheless relevant to TCE because poisoned behavioral memory could alter predictions, workflow guidance, and autonomy decisions long after the original input disappears.

Required boundary:

- Untrusted HTML, web content, and tool output cannot directly create a preference, safety rule, or procedural runbook.
- Shared reference projections should be read-only by default.
- Redaction, provenance, subject binding, and authorization apply before projection generation and retrieval.
- Sanitized HTML output contains no scripts, inline event handlers, hidden instructions, active forms, or external resources.
- A memory promotion requires canonical evidence and the existing storage/review gates.

### TCE document-projection design

Status: **production projection and prospective-evaluation surfaces implemented behind default-off feature flags**. Full and Lite use the same deterministic renderer, assignment algorithm, metrics, and response models.

Postgres and SQLite remain the canonical behavioral evidence stores. File-shaped memory should be additive:

```text
canonical behavior evidence
  -> deterministic topic/state projection
  -> Markdown or JSON for executor retrieval
  -> sanitized passive HTML for human review
  -> citation back to immutable evidence
```

The executor-facing interface uses read-only MCP resources with stable URIs and MIME types rather than another mutation tool. Mutations continue through `record_behavior_evidence`, correction, review, and supersession APIs. A document may propose a change, but it cannot overwrite canonical behavioral state or become learning evidence merely because an executor read it.

Implemented MCP resources:

```text
tce://workspace/{workspace_id}/behavior/{subject_id}/current.md
tce://workspace/{workspace_id}/behavior/{subject_id}/current.json
tce://workspace/{workspace_id}/behavior/{subject_id}/review.html
tce://workspace/{workspace_id}/behavior/{subject_id}/decisions/{topic}.md
tce://workspace/{workspace_id}/behavior/{subject_id}/evidence/{observation_id}.json
```

The MCP server rejects a resource URI when its workspace or subject does not exactly match the server's configured identity. The API resolves scope from authenticated workspace and behavior-subject claims; request paths cannot override it.

Implemented HTTP read endpoints:

```text
GET /v1/behavior/projections/current?format=markdown|json
GET /v1/behavior/projections/decisions/{topic}?format=markdown|json
GET /v1/behavior/projections/evidence/{observation_id}?format=markdown|json
GET /v1/behavior/projections/review
GET /v1/behavior/projections/review.html
```

Each response includes a stable projection ID, schema version, body SHA-256, source revision, source evidence IDs, deterministic generated timestamp, trust level, sensitivity, expiry, truncation state, redaction state, MIME type, and content. `content_sha256` covers the canonical projection body and excludes the metadata wrapper, avoiding a self-referential hash. `source_revision` covers the normalized selected evidence and projection selector.

Current and topic projections include only active, learning-eligible evidence after expiry, supersession, rejection, and explicit contradiction filtering. An evidence citation can still resolve a historical or audit-only record, but its lifecycle and trust level remain visible. Projection generation re-applies bounded redaction defensively and never calls an LLM.

The review document deliberately includes active, superseded, rejected, and audit-only evidence so a human can detect drift and bad capture. It escapes all evidence text and ships with a deny-by-default Content Security Policy, no scripts, no event handlers, no forms, no external resources, `nosniff`, and private no-store caching. Its only links are canonical `tce://` evidence citations.

Every successful API or MCP projection read writes a metadata-only `behavior_projection_read` audit record containing the projection identity, selector, source revision, and cited evidence IDs. Rendered content is not copied into the audit log. This exposure record allows later evaluation to distinguish an independent decision from one made after memory exposure. Configure `TCE_AUDIT_WRITE_MODE=durable` when exposure logging must fail closed.

The following are intentionally excluded:

- writable file mounts or agent-authored canonical memory
- using a generated projection as new behavioral evidence
- any claim that projections improve behavioral prediction before the prospective pilot completes

### Evaluation requirement

File/document projections must not be enabled as predictive evidence based only on retrieval quality. The feature-gated prospective pilot implements these four conditions:

1. No behavioral memory.
2. Canonical structured evidence retrieval.
3. Compact Markdown projection.
4. Projection index followed by cited evidence drill-down.

Measure:

- top-1 and top-3 decision agreement
- non-abstained precision and calibration
- correct-action and workflow similarity
- stale or superseded memory use
- irrelevant-personalization rate
- correction and outcome-regret rate
- malicious-memory activation rate
- injected tokens and retrieval latency

A projection is beneficial only if it improves held-out decision or workflow performance without increasing stale-memory use, harmful personalization, security failures, or latency beyond the existing gates. Human review convenience alone is not evidence of improved behavioral fidelity.

Pilot interfaces:

```text
POST /v1/behavior/projections/pilot/assign
POST /v1/behavior/projections/pilot/outcome
GET  /v1/behavior/projections/pilot/status

tce.assign_behavior_projection_pilot
tce.report_behavior_projection_pilot_outcome
tce.get_behavior_projection_pilot_status
```

Assignment is deterministic from the server-configured salt, authenticated workspace, behavior subject, and caller-provided trial key. The unique `(workspace_id, subject_user_id, trial_key)` constraint makes assignment retries idempotent. Each assignment stores a redacted snapshot hash, source revision, citations, injected-token estimate, and retrieval latency so later evidence changes cannot rewrite what the agent saw. The no-memory arm receives no evidence or citations.

Outcome writes are one-per-assignment and idempotent. They record held-out choice agreement, confidence, action/workflow similarity, correction, regret, irrelevant personalization, malicious activation, and cited evidence use. Evidence used outside the assignment snapshot, or no longer active at report time, is marked stale. Outcomes never create or promote a behavior observation.

The status remains `collecting` until at least 28 real elapsed days, 30 completed outcomes per arm, 80% assignment completion, p95 retrieval latency at or below 120 ms, and zero malicious-memory activation. Once those collection gates pass, the pre-registered quality comparison produces `ready_for_review`, `failed_quality`, or `failed_safety`. `ready_for_review` means the dataset is reviewable; it is not an automatic autonomy enablement decision. See [behavior projection pilot runbook](runbooks/behavior-projection-pilot.md).

## Configuration

```dotenv
TCE_BEHAVIOR_EVIDENCE_ENABLED=true
TCE_BEHAVIOR_FIDELITY_ENABLED=true
TCE_BEHAVIOR_PROJECTIONS_ENABLED=false
TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=false
TCE_BEHAVIOR_PILOT_ASSIGNMENT_SALT=tce-behavior-pilot-v1
TCE_BEHAVIOR_PILOT_ASSIGNMENT_TTL_DAYS=30
TCE_BEHAVIOR_PILOT_MIN_WINDOW_DAYS=28
TCE_BEHAVIOR_PILOT_MIN_COMPLETED_PER_ARM=30
TCE_BEHAVIOR_PILOT_MIN_COMPLETION_COVERAGE=0.80
TCE_BEHAVIOR_PILOT_MAX_P95_RETRIEVAL_LATENCY_MS=120
TCE_BEHAVIOR_PILOT_MAX_TOP1_DEGRADATION=0.05
TCE_BEHAVIOR_PREDICTION_ENABLED=true
TCE_BEHAVIOR_CALIBRATION_ENABLED=false
TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED=false
TCE_BEHAVIOR_STORAGE_GATE_MODE=shadow
TCE_BEHAVIOR_STORAGE_MIN_SCORE=0.55
TCE_BEHAVIOR_PREDICTION_MIN_CONFIDENCE=0.55
TCE_BEHAVIOR_SUBJECT_ID=local-user
TCE_MCP_BEHAVIOR_SUBJECT_ID=local-user
TCE_BEHAVIOR_SUBJECT_BINDINGS=codex-executor=local-user,claude-executor=local-user
TCE_BEHAVIOR_CAPABILITY_BROKER_ENABLED=true
TCE_BEHAVIOR_PROCESS_MINING_ENABLED=true
TCE_BEHAVIOR_SHADOW_EVALUATION_ENABLED=true
TCE_BEHAVIOR_MEMORY_REVIEW_ENABLED=true
TCE_BEHAVIOR_COUNTERFACTUAL_ENABLED=true
TCE_BEHAVIOR_PROCESS_MIN_SUPPORT=2
TCE_CAPABILITY_GRANT_TTL_SECONDS=120
TCE_BEHAVIOR_CONTROL_RETENTION_DAYS=365
```

When `TCE_WORKSPACE_ACCESS_MODE=strict`, a behavior subject different from `X-TCE-User` must be authorized by `TCE_BEHAVIOR_SUBJECT_BINDINGS`. Separate multiple subjects for one owner with `|`.

## Rollout

1. Deploy with `TCE_BEHAVIOR_PROJECTIONS_ENABLED=false`, `TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=false`, and `TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED=false`.
2. Enable projections for an authenticated test workspace and verify resource scope, redaction, hashes, and latency.
3. Capture explicit evidence and corrections for normal work.
4. Enable `TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED=true` for the enrolled workspace and run assignments before the agent acts.
5. Report each held-out human outcome without promoting pilot results into evidence.
6. Review confidence intervals, context diversity, calibration, abstention, baseline lift, recent drift, and malicious-memory activation only after the status leaves `collecting`.
7. Enable the autonomy gate only after the fidelity run and prospective pilot both support the intended use case.
8. Set either feature flag to `false` for immediate rollback without a schema rollback.

## Performance Model

- Evidence normalization, redaction, prediction, and evaluation are deterministic.
- The production gate requires at least 30 held-out cases, 10 distinct normalized contexts, Wilson 95% lower bounds for accuracy and precision, and conservative lift over the majority baseline.
- No LLM call is added to ingestion, prediction, or takeover.
- Projection generation runs only on its dedicated read endpoints and does not alter existing search, prediction, ingestion, or takeover paths.
- Projections are capped at 100 active evidence records and bound nested structures before rendering.
- Human review is capped at 500 records and is never injected into normal retrieval automatically.
- Pilot contexts are capped at 12 task-ranked evidence records and are generated only on the assignment endpoint.
- Evaluation is on-demand and outside the normal retrieval path.
- Full and Lite CI gate pilot assignment p95 at 120 ms and include the path in mixed Locust traffic.
- Takeover performs one indexed latest-run lookup only when the autonomy gate is enabled.
- Full and Lite use the same shared scoring and evaluation implementation.

## Behavioral Control Plane

### Prospective shadow evaluation

Each new behavior-evidence write is predicted from earlier eligible evidence before the new answer is allowed into the evidence set. The result is stored separately in `behavior_shadow_predictions`. It never changes the answer or influences autonomy. `GET /v1/behavior/shadow/status` and `tce.get_behavior_shadow_status` expose coverage, precision, abstention, recent-versus-previous precision, and a drift alert.

### Memory review and promotion

Explicit, correction, and calibration evidence that passes the storage gate remains immediately eligible. Inferred and backfilled evidence that would otherwise pass is stored as `pending_review` and remains learning-ineligible. Use `GET /v1/behavior/reviews` or `tce.get_behavior_memory_reviews`, then resolve the review with `promote` or `reject`. Promotion rebuilds the behavior fingerprint from all currently eligible evidence.

Mined process models use the same review queue. A promoted model becomes an active `workflow_template`; a candidate model cannot guide takeover before promotion.

### Sequence and process mining

`POST /v1/behavior/processes/mine` groups chronologically ordered takeover and directive actions by session, compresses repeated adjacent actions, and mines exact multi-step variants with cross-session support. Each model records ordered steps, direct-follow transitions, support, success rate, reliability, source sessions, and evidence IDs. This is deterministic process discovery, not an LLM-generated workflow claim.

### Counterfactual decisions

`POST /v1/behavior/counterfactuals` records the chosen decision, a serious alternative, expected outcome, assumptions, and confidence. A later resolution marks the alternative `supported`, `refuted`, or `inconclusive` against an observed outcome. Counterfactual records are redacted and remain separate from learning evidence unless a human later records an explicit evidence item.

### Capability grants

The capability broker uses a closed capability registry. Unknown capabilities fail closed. Read grants are short-lived; mutating grants also require a claimed, in-progress directive bound to an allowed, unexpired execution permit. A grant token is returned once, stored only as a SHA-256 hash, bound to the canonical action/resource/arguments digest, and atomically consumed once.

The broker is an authorization and replay-protection protocol, not an arbitrary shell runner. Non-bypassable host enforcement requires the executor or sandbox to route every mutation through `request_capability_grant` and `consume_capability_grant`; direct shell/filesystem access outside that route is not intercepted by TCE.

Lifecycle maintenance removes expired historical grants and shadow predictions after `TCE_BEHAVIOR_CONTROL_RETENTION_DAYS` (365 by default). It also removes old resolved reviews, resolved counterfactuals, and rejected process models. Pending reviews, open counterfactuals, active process models, and the underlying behavior-evidence ledger are preserved.
