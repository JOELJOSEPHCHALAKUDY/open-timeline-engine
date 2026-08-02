# Clone Advisor Guide

> Public release track: `v0.4.0` (pre-1.0).
> `V4`–`V9` labels in this guide are internal milestones.

Use one or more executor AIs at the same time:

- every MCP client runs as `executor`
- each executor session can request advisor guidance through API-side clone tools

Clone-guidance source is explicit:

- MCP-side: both clients call the same MCP toolset with `TCE_MCP_ROLE=executor`
- API-side: optional advisor provider routing configured via `/v1/setup/advisor/*`

Terminology note:

- `advisor` in this guide refers to the API-side guidance lane, not an MCP role.
- MCP clients should run as `executor` identities.

## When to use

- architecture choices
- debugging strategy
- implementation sequencing
- tradeoff decisions where your working style matters

## Runtime mode

Enable clone mode:

```bash
./scripts/set_mode.sh clone_advisor
```

The executor also needs `TCE_MCP_TOOL_PROFILE=autonomy` (or `research`) so takeover and lifecycle tools are exposed. Wizard installs select this automatically. For an existing installation, update `.env`, regenerate MCP client config, and restart the executor.

Fallback mode:

- if clone mode is disabled and request allows fallback, clone-advice calls can return timeline-only guidance

## Advisor provider packs (Milestone V9)

TCE supports primary + fallback routing for advisor models:

- Global: OpenAI, Anthropic, Gemini, OpenRouter, Groq, Together, xAI
- China: DeepSeek, DashScope (Qwen), Zhipu GLM, Moonshot Kimi, Baidu Qianfan, Tencent Hunyuan
- Custom hosted: any OpenAI-compatible endpoint (`base_url` + `model`)

Setup endpoints:

- `GET /v1/setup/advisor/providers`
- `GET /v1/setup/advisor/models?provider=...`
- `POST /v1/setup/advisor/verify`
- `PUT /v1/setup/advisor/config`

## Identity and headers

Executor headers (example A):

- `X-TCE-Consumer: codex-executor`
- `X-TCE-Role: executor`
- `X-TCE-Workspace: personal` (or your shared workspace id)
- `X-TCE-User: codex-executor` (or another unique executor user id)
- `X-TCE-Behavior-Subject: local-user` (the human profile shared across executors)

Executor headers (example B):

- `X-TCE-Consumer: claude-executor` (or another `<client>-executor`)
- `X-TCE-Role: executor`
- `X-TCE-Workspace: personal` (same workspace for shared-memory mode)
- `X-TCE-User: claude-executor` (distinct from other executors for owner/audit separation)
- `X-TCE-Behavior-Subject: local-user` (same human profile as executor A)

Identity rules:

- keep `X-TCE-Workspace` shared when you want shared memory.
- keep `X-TCE-User` distinct per executor identity.
- keep `X-TCE-Behavior-Subject` the same only when the executors assist the same human.
- behavior evidence and fingerprints are isolated by workspace plus behavior subject.
- cross-user memory lookup is explicit-query scoped and returns policy metadata when applied.
- advisor model routing is API-side (`/v1/setup/advisor/*`), not an MCP `advisor` role.
- keep cross-user prompts explicit (examples: `read codex timeline`, `read codex memory`, `continue codex work on README`).

## Standard cycle

Phase 0 (activate shared context): Read timeline first with an explicit query (for example: `read codex timeline`).

1. Executor calls `tce.get_context_bundle`.
2. Executor drafts plan with citations.
3. Any executor lane calls `tce.get_clone_advice` with the same `interaction_id`.
4. If conflict exists, executor calls `tce.arbitrate_with_clone`.
5. Executor logs `TASK_DECISION` and `TASK_DONE`.

Required milestone event format (after each completed change):

- `title`: what changed
- `payload.files`: touched file paths
- `decision`: why this approach
- `outcome`: `{ "status": "succeeded|failed|blocked", "next_step": "..." }`

## Autonomy Milestone V4 control loop

`human_consultative` is now the default autonomy profile.

1. Discover goals:
   - `POST /v1/takeover/goals/discover`
2. Select goal:
   - `POST /v1/takeover/goals/{goal_id}/select`
3. Execute via takeover:
   - `POST /v1/takeover/step`
4. For mutating actions request permit:
   - `POST /v1/takeover/permit`
   - `POST /v1/takeover/permit/resolve`
5. Record outcome:
   - `POST /v1/takeover/feedback`

Status endpoint:
- `GET /v1/takeover/autonomy/status?session_id=...`

## Autonomy Milestone V5 extensions

Milestone V5 keeps the Milestone V4 flow and adds cache-first affective goal selection:

1. Warm goal queue for sub-10ms hot selection:
   - `POST /v1/takeover/goals/precompute`
2. Inspect cache health/freshness:
   - `GET /v1/takeover/goals/cache/status?session_id=...`
3. Force clear cache during ops/testing:
   - `POST /v1/takeover/goals/cache/invalidate`

Goal objects now include additive metadata:

- `goal_kind` (`normal | unknown | nothing`)
- `selection_score`
- `affective_scores`
- `cache_hit`
- `cache_source`

## Autonomy Milestone V6 extensions

Milestone V6 closes the last three autonomy gaps with additive controls:

1. Proactive surfacing (pull visibility):
   - `POST /v1/takeover/autonomy/tick`
   - `GET /v1/takeover/notices`
   - `POST /v1/takeover/notices/{notice_id}/ack`
2. Closed-loop execution reporting:
   - `POST /v1/takeover/execution/report` (includes failure classification and bounded retry scheduling)
   - `GET /v1/takeover/execution/status`
3. Strict mutation enforcement:
   - `POST /v1/takeover/execution/claim`
   - mutating directives require permit + claim while takeover is active

Operational note:
- proactive notices are persisted for pull consumers (dashboard/API/MCP); there is no MCP push transport.

## Milestone V7 context expansion (full + lite parity)

Milestone V7 adds bounded retrieval expansion and retrieval observability without breaking existing takeover contracts.

Additive takeover response fields:

- `context_quality_score`
- `retrieval_triggered`
- `retrieval_source` (`none | pgvector_ann | lexical_only | hybrid_fallback | qdrant`)
- `retrieval_reason`
- `retrieval_latency_ms`
- `retrieval_hit_count`

Status endpoint:

- `GET /v1/context/retrieval/status`

Default trigger model:

1. Trigger retrieval when any condition is true:
   - low context-quality score
   - low decision confidence
   - low evidence count
   - repeated failures
   - explicit deep-intent wording (`research|deep|explore|investigate`)
2. Keep retrieval bounded by timeout/budget.
3. If retrieval remains weak after expansion, escalate `needs_human=true` rather than guessing.

Compatibility:

- Existing goal scoring, affective scoring, permits, and safety gates remain active.
- Milestone V7 is additive and keeps full/lite behavioral alignment on retrieval fields.

## Human-level score interpretation

This dashboard score is an operational readiness indicator, not scientific evidence that TCE has cloned a human. Use the Behavior Fidelity v1 chronological evaluation before allowing learned behavior to influence autonomous continuation.

Dashboard human-level score combines four deterministic subscores:

- `clone_readiness` (existing clone score logic)
- `execution_quality` (success, retries, continuity, permit discipline)
- `goal_coherence` (long-term progress, relation density, completion ratio, queue stability)
- `affective_alignment` (emotion stability, overwhelm control, positive signals, identity alignment)

Band mapping:

- `0..39`: `emerging`
- `40..64`: `developing`
- `65..84`: `advanced`
- `85..100`: `human_like`

The `human_like` label is retained for API/UI compatibility. It means high internal readiness on these four subscores, not human equivalence.

## Behavior Fidelity v1

Behavior Evidence v1 records the information required to evaluate decision continuity:

- situation and objective
- available choices and selected choice
- constraints and context snapshot
- rationale and action taken
- outcome and correction
- memory class, provenance, validity window, contradictions, and supersession

Public surfaces:

- `POST /v1/behavior/evidence`
- `POST /v1/behavior/predict`
- `POST /v1/behavior/evaluate`
- `GET /v1/behavior/evaluations`
- `GET /v1/behavior/calibration/scenarios`
- `POST /v1/behavior/calibration/answer`

Predictions abstain below the configured confidence floor. When `TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED=true`, takeover also pauses unless the latest chronological evaluation passes its accuracy, calibration, precision, and sample-count gates. Permit/claim/report and hard safety gates remain mandatory.

Detailed rollout instructions: [Behavior Fidelity v1](behavior-fidelity.md).

## Project-scoped dreams

When ordered planning is enabled, idle autonomy may convert a bounded aspiration into a plan. Directional dreams require a canonical project binding plus repeated, similar relayed user asks. The stored dream retains the source event IDs; global folder/domain activity alone cannot create a directional dream. Operational signals such as unfinished directives and indexing gaps are scoped to the bound session/project. No project binding means no dream generation.

Executors should send `app_context.project_root` and `app_context.project` on the first takeover call in a repository and whenever the repository changes. TCE pins the redaction-safe identity to the takeover session and copies it to events and completion handoffs.

## Long-term goal definition

A goal is flagged `long_term=true` when any rule matches (first-match reason is emitted):

1. `affective_scores.temporal.dream >= 0.30`
2. `age_days >= 21` and `affective_scores.temporal.rehearsal_count >= 6`
3. `source == "user_objective"` and `age_days >= 14` and goal status is not terminal

## Loop guard and arbitration

- Advisor loops are capped per interaction.
- Arbitration resolves executor/advisor disagreement.
- `human_override` wins when provided.

## Safety defaults

- MCP clients should run as `executor`; mutation safety is enforced by permit/claim/report + constraints
- hard constraints block edits to protected core paths: `shared/tce_shared/`, `services/tce_api/`, `services/tce_lite_api/`, `services/tce_mcp/`, `scripts/`, `infra/`
- `check_context` is required before file edits in active takeover/suggest mode
- sensitivity `3` blocked by default in output paths
- every advisory/bundle operation is audit logged

Same-project protected-file lock example: [README.md (MCP machine-readable locks)](../README.md#mcp-machine-readable-locks-important)

## Milestone V8 context brief and rule injection

`POST /v1/context/brief` produces a deterministic, citation-backed brief with fixed sections:

- `standard_approach`
- `current_state`
- `constraints_preferences`
- `open_loops`
- `artifacts`

Rule precedence:

- Active scoped P0/P1 `memory_rules` are injected into `constraints_preferences` when `memory_rule_p0_always_include=true`.
- Retrieved ranking cannot suppress these high-priority rules.

Episode and annotation flow:

- events are captured immediately
- episode extraction runs async by worker (default)
- `POST /v1/events/annotate` can override/enrich goal/decision/avoid/authority fields

Retrieval evaluation:

- `POST /v1/retrieval/eval/run` records a deterministic eval run
- `GET /v1/retrieval/eval/status` returns latest/history for session-scoped tracking

## Milestone V8.1 memory loop hardening

To avoid “memory system with no memories” drift:

1. Reflection loop runs on decisive task events and writes:
   - episode lesson updates (`do_more`, `do_less`, `avoid`)
   - feedback-linked observations
2. Consolidation loop upserts typed patterns:
   - `semantic_fact`, `experience`, `skill`, `opinion`
3. Situation types are normalized to canonical categories on feedback/observation ingest.
4. Retrieval uses activation-weighted ranking and optional multi-hop graph expansion.

## Workflow hint integration

- Clone/takeover guidance can include `workflow_hints` derived from stored workflow templates.
- Hints are non-blocking recommendations and do not override hard constraints or safety controls.

## Shared handoff continuity (full + lite parity)

Use explicit cross-executor prompts:
- `read codex timeline`
- `read claude timeline`
- `continue codex work on <task>`
- `continue claude work on <task>`

Execution milestone contract (`milestone_schema=v1`) on `report_execution`:
- `title`
- `payload.files`
- `decision`
- `outcome.status` + `outcome.next_step`
- optional `git` and `anchors`

Validation mode (full + lite):
- `TCE_HANDOFF_MILESTONE_VALIDATION_MODE=shadow|warn|enforce`

Resume packet endpoint/tool (file-level continuation):
- `POST /v1/handoff/resume`
- MCP: `tce.get_resume_packet`
- deterministic selection: task overlap first, then recency
- response includes prioritized files, anchors, compact change summary, git refs, and continuation steps
