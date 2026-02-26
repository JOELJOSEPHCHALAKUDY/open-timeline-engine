# Milestones

## V4: Human-Level Autonomy (Consultative default)

Three production controls on top of takeover:
- proactive goal queue discovery (`/v1/takeover/goals/*`)
- closed-loop feedback adaptation (`/v1/takeover/feedback`)
- execution permits for mutating actions (`/v1/takeover/permit*`)

Quick flow:
```bash
# discover goals for session
curl -sS -X POST http://localhost:8080/v1/takeover/goals/discover \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"codex","include_open_discovery":true}'

# request permit before mutating actions
curl -sS -X POST http://localhost:8080/v1/takeover/permit \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"codex","action_kind":"edit","target_paths":["README.md"],"estimated_change_size":10}'
```

## V5: Affective Goal Intelligence + Near-Zero-Latency Goal Cache

Deterministic affect-aware goal selection with cache-first hot-path behavior:

- 26-dimension affective scoring across temporal/emotional/behavioral/meta/human/similarity signals.
- Goal kinds: `normal`, `unknown`, `nothing`.
- Two-tier cache:
  - L1 in-process cache (default TTL: 8s)
  - L2 persisted cache (default TTL: 300s)
- Additive takeover response metadata:
  - `goal_cache_hit`, `goal_cache_source`, `selected_goal_score_breakdown`

New API endpoints:

- `POST /v1/takeover/goals/precompute`
- `GET /v1/takeover/goals/cache/status`
- `POST /v1/takeover/goals/cache/invalidate`

New MCP tools:

- `tce.takeover_precompute_goals`
- `tce.get_takeover_goal_cache_status`
- `tce.invalidate_takeover_goal_cache`

## V6: Proactive Notices + Directive Lifecycle Enforcement

Strict, client-agnostic execution continuity on top of Milestone V5:

- Proactive goal surfacing (pull model):
  - `POST /v1/takeover/autonomy/tick`
  - `GET /v1/takeover/notices`
  - `POST /v1/takeover/notices/{notice_id}/ack`
- Closed-loop directive execution reporting:
  - `POST /v1/takeover/execution/claim`
  - `POST /v1/takeover/execution/report`
  - `GET /v1/takeover/execution/status`
- Strict mutation gate:
  - mutating directives require both permit and claim while takeover is active.
- claim TTL is capped by permit TTL.

## V7: Context Expansion (Staged Hybrid)

Retrieval-quality + latency focused, with full + lite parity.

- Adaptive retrieval metadata on takeover responses:
  - `context_quality_score`
  - `retrieval_triggered`
  - `retrieval_source`
  - `retrieval_reason`
  - `retrieval_latency_ms`
  - `retrieval_hit_count`
- Retrieval status endpoint:
  - `GET /v1/context/retrieval/status`
- Trigger policy (bounded):
  - trigger when context/coverage/confidence is low or explicit deep intent is present
  - bounded budgets and same-request fallback (no hard failure on backend timeout)
- Vector backend:
  - default remains `pgvector` (Postgres source of truth)
  - Qdrant long-term sync is enabled by default (`qdrant_enabled=true`)

## V7.2: Workflow-Memory Hints

- MCP takeover responses can now include `workflow_hints` from learned workflow templates.
- Workflow hints are advisory. Enforcement order remains: safety, hard constraints, permit/claim/report lifecycle, then hints.
- Templates are available from `GET /v1/workflow/templates` in both full and lite runtimes.

## V8: Meaningful Memory (additive)

Episode-level memory, rule-driven boundaries, and deterministic context briefs without removing existing V5/V6/V7 behavior.

New API surfaces:

- `POST /v1/events/annotate`
- `GET /v1/episodes`
- `GET /v1/episodes/{episode_id}`
- `POST /v1/context/brief`
- `GET /v1/memory/rules`
- `POST /v1/memory/rules`
- `POST /v1/memory/rules/{rule_id}/deprecate`
- `POST /v1/memory/forget`
- `GET /v1/retrieval/eval/status`
- `POST /v1/retrieval/eval/run`

Key runtime notes:

- Event ingest now supports idempotency metadata (`source_id`, `source_seq`, `idempotency_key`, `vector_clock`) while preserving compatibility for legacy clients.
- Episode extraction is asynchronous by default via worker job `tce_worker.jobs.episode_extraction.run`.
- Search ranking now favors `relevance + stability + authority + bounded recency` rather than recency-dominant behavior.
- `context/brief` always includes active scoped P0/P1 memory rules when enabled.
- Memory reflection and consolidation loops are active:
  - task completion/decision events update episode lessons (`do_more`, `do_less`, `avoid`)
  - semantic summaries are upserted as typed patterns (`semantic_fact`, `experience`, `skill`, `opinion`)
- Situation types are canonicalized at ingest/feedback boundaries to avoid unreachable seed categories.
- Retrieval applies activation decay (frequency + recency) and optional multi-hop entity expansion.
- Full and lite runtimes expose the same retrieval metadata fields and context-brief section shape.

## V9: Advisor Provider Routing + Runtime Unification

Executors (`codex`, `claude`, `cursor`, etc.) are clients that call MCP tools. The advisor model provider is configured separately in TCE. You can choose:

- local providers: `local_ollama`, `local_lmstudio`
- global providers: `openai`, `anthropic`, `gemini`, `openrouter`, `groq`, `together`, `xai`
- China providers: `deepseek`, `dashscope`, `zhipu`, `moonshot`, `qianfan`, `hunyuan`
- custom hosted: `custom` (`openai_compatible`)

Routing behavior: user-selected primary provider is attempted first, then an ordered fallback chain on failure/rate-limit/timeout.

Setup endpoints:

- `GET /v1/setup/advisor/providers`
- `GET /v1/setup/advisor/models?provider=...`
- `POST /v1/setup/advisor/verify`
- `PUT /v1/setup/advisor/config`
- `POST /v1/setup/advisor/switch`
- `GET /v1/setup/advisor/runtime/status`
- `POST /v1/setup/advisor/runtime/probe`

Dashboard advisor settings (V9.6):

1. Pick primary connection mode: `Local` or `Web`.
2. Local mode: choose `Ollama` or `LM Studio`, set base URL, load models, verify route. For Ollama, pull models directly from dashboard.
3. Web mode: choose provider (global/china/custom), set API key, load live models, verify route.
4. Configure fallback routes in an ordered list (add/remove/reorder, verify each route).
5. Save writes `.env` first, then prompts restart via `POST /v1/dashboard/stack/restart`.

Runtime defaults (V9.4, performance hardened):

- `advisor_total_budget_ms=2200`
- `advisor_attempt_timeout_ms=900`
- `advisor_connect_timeout_ms=250`
- `advisor_read_timeout_ms=900`
- `advisor_failover_min_remaining_ms=250`
- circuit breaker open after `5` consecutive failures
- half-open probe interval `30s`, close after `2` probe successes

Compatibility: existing `advisor_primary_provider` / `advisor_fallback_chain` configuration still works — legacy config is automatically normalized into profile routes.

## Dashboard Intelligence Upgrade

The dashboard now exposes higher-order goal intelligence and a persisted human-level score trend:

- `GET /v1/dashboard/goals/intelligence`
  - goal-event edges
  - goal-emotion edges
  - goal-goal edges
  - deterministic long-term goal classification
- `GET /v1/dashboard/human-score`
  - hybrid score (0–100): clone readiness + execution quality + goal coherence + affective alignment
- `GET /v1/dashboard/human-score/history`
  - historical score points for trend rendering
- `POST /v1/dashboard/human-score/recompute`
  - on-demand snapshot recompute + persistence

UI entry point:

- `http://localhost:8080/dashboard/goal-intelligence`
