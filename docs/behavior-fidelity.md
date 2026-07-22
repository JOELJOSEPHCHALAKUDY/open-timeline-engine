# Behavior Fidelity v1

Behavior Fidelity v1 measures whether Open Timeline Engine can reproduce future user decisions from prior evidence. It does not claim to clone a person, consciousness, or general human behavior.

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

The production gate currently requires at least 20 held-out cases, top-1 accuracy of at least `0.65`, Brier score at most `0.25`, non-abstained precision of at least `0.75`, and at least `0.05` lift over the per-situation majority baseline. A trivial “always choose the common option” dataset cannot unlock autonomy.

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

## Configuration

```dotenv
TCE_BEHAVIOR_EVIDENCE_ENABLED=true
TCE_BEHAVIOR_FIDELITY_ENABLED=true
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

1. Deploy the schema and keep `TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED=false`.
2. Capture explicit evidence and corrections for normal work.
3. Run `tce.run_behavior_fidelity_eval` periodically.
4. Review confidence intervals, context diversity, calibration, abstention, baseline lift, and recent drift.
5. Enable the autonomy gate only after the latest run passes and the sample count is representative.
6. Disable one flag to roll back gating without a schema rollback.

## Performance Model

- Evidence normalization, redaction, prediction, and evaluation are deterministic.
- The production gate requires at least 30 held-out cases, 10 distinct normalized contexts, Wilson 95% lower bounds for accuracy and precision, and conservative lift over the majority baseline.
- No LLM call is added to ingestion, prediction, or takeover.
- Evaluation is on-demand and outside the normal retrieval path.
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
