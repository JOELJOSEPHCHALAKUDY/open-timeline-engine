# Open Timeline Engine — Executor Policy

## CRITICAL: Protected Files — DO NOT MODIFY
The following directories contain core TCE infrastructure. NEVER edit, revert, or modify files in these paths, even if you believe there is a bug:
- `shared/tce_shared/`
- `services/tce_api/`
- `services/tce_lite_api/`
- `services/tce_mcp/`
- `scripts/`
- `infra/`

If you find a bug in these paths, report it to the user instead of changing it, unless the user explicitly asks to override this protection.

## Required tool-calling behavior

### Session management
1. Use one stable `session_id` per conversation and reuse it on every call.
2. Always pass `activation_mode_default="takeover"` on every `tce.takeover_step` call.
2a. On the first takeover call in a repository, pass `app_context={"project_root":"<absolute repo root>","project":"<repo name>"}`. Re-send it when the active repository changes; TCE binds it to the session for later calls.

### State resolution (history-driven)
3. Determine takeover state from conversation history ordering, not local flags:
   - If any activation phrase appears and no stand-down appears after it => active.
   - If a stand-down phrase appears after the last activation => inactive.
4. Do not rely on persistent local flags like `tce_takeover_active`.

### When to call takeover tools
5. On activation turn, call `tce.takeover_step`.
6. While active, call `tce.takeover_step` on every user message.
7. On stand-down turn, call `tce.reset_takeover_state(session_id)` and stop takeover calls afterward.
8. While inactive, do not call takeover tools in normal chat flow.

### Activation and stop phrases
- Activation: `beru take over`, `beru takeover`, `beru suggest`, `igris take over`, `kurama take over`, plus custom phrases.
- Stop: `beru stand down`, `shadow stand down`, `advisor stand down`, `kurama stand down`, plus custom stop phrases.

### Handling the result
9. If `persona_ack` exists, output exactly that text first; if `has_directive=true`, immediately continue directive execution in the same turn.
10. If `safety_decision=confirm_required`, show `final_response` and wait for confirmation.
11. If `has_directive=true`, immediately work on `state.takeover_context.objective` using tools. Do not narrate tool payloads.

### V6 directive lifecycle (mandatory)
12. If `execution_permit_required=true` and permit is missing, call `tce.request_execution_permit` immediately.
13. If execution claim is required (or `next_step` says to claim), call `tce.claim_execution` before mutating actions.
14. After execution:
    - Success: call `tce.report_execution(..., state="succeeded")`
    - Failure: call `tce.report_execution(..., state="failed", failure_reason="<reason>")`
    - For completed changes without a takeover directive, call `tce.complete_task` with title, files, decision, outcome/next step, git refs, and anchors before ending the turn.
15. If `retry_scheduled=true`, continue the same objective automatically unless safety requires pause.
16. If `autonomy_notice` is present, call `tce.ack_takeover_notice` unless already acknowledged.

### next_step precedence (critical)
17. If `next_step` contains actionable instructions (for example `AUTONOMOUS MODE PAUSED`, `Call tce.request_execution_permit`, `Call tce.claim_execution`), follow `next_step` first.
18. Natural response is allowed only when:
    - `has_directive=false`
    - `final_response` is null
    - `next_step` is empty or non-actionable
    - no execution lock is present (`pending_execution` absent and `directive_state` not `pending`/`in_progress`)
18a. If takeover is active and execution is locked (`pending_execution` exists or `directive_state` is `pending`/`in_progress`) but `has_directive=false` and `next_step` is empty:
    - call `tce.get_execution_status(session_id)`
    - call `tce.claim_execution(...)` for the latest pending directive
    - continue the same objective; do not emit natural handoff text (for example "If you want, I can do next...")

### Continuation until done or no longer relevant
19. While active, continue autonomously until one is true:
    - goal achieved/completed
    - explicit stand-down phrase
    - existing goal remains low relevance: `selected_goal.selection_score < 0.45` for 3 consecutive evaluation cycles
20. Empty goal queue handling:
    - if `selected_goal` is null or queue is empty, call `tce.takeover_discover_goals`.
    - if goals are discovered, continue with highest-priority goal.
    - if still empty, keep takeover active, ask a concise "what next?" prompt, and do NOT reset session.
21. Vague-response standby criteria while queue remains empty:
    - treat as vague when no actionable verb+target objective can be extracted (for example: "ok", "hmm", "sure", "continue")
    - treat as concrete when a specific task intent is present (for example: "fix dashboard takeover scroll")
    - if vague for 3 consecutive turns: keep session active, switch to suggest/standby, and do NOT call `tce.reset_takeover_state`
22. If `selection_score` is missing for an existing goal, remain active and re-evaluate next cycle.
23. On auto-stop (`done` or `low_relevance`), call `tce.reset_takeover_state(session_id)` and return a concise reason.
24. Suggest mode and takeover mode are both persistent while active.

### V7 bounded retrieval behavior (additive)
25. If takeover is active and `context_quality_score < 0.72` (or `retrieval_triggered=true`), allow one bounded retrieval expansion pass for that turn.
26. Keep retrieval budget bounded: total <= 120ms per turn and <= 60ms per backend/source.
27. If retrieval times out or falls back (`retrieval_source=lexical_only` or `retrieval_source=hybrid_fallback`), continue with fallback context and do not block the turn.
28. If post-retrieval quality is still low (`context_quality_score < 0.55`) or `needs_human=true`, pause autonomy and ask the user instead of guessing.
29. If quality is high (`context_quality_score >= 0.72`) with sufficient evidence, do not force deep retrieval.

### V7.2 workflow-memory hints (additive)
30. If `workflow_hints` is present in `tce.takeover_step` result, use the highest-reliability hint first.
31. Reuse hinted steps for the first execution attempt before inventing a new sequence.
32. If workflow hints conflict with `constraints`, permit/claim requirements, or safety decisions, constraints and safety win.
33. Workflow hints are guidance only; always finish with normal directive lifecycle reporting (`tce.report_execution`).

## Mandatory completion capture
1. Every completed mutating change must end with either `tce.report_execution` for a directive or `tce.complete_task` for ordinary work.
2. Use a stable idempotency key such as `git:<commit>` or `<session>:<task>:<result>`.
3. Do not claim completion when capture is queued or dead; report the delivery state and retry it.

## Conditional: check_context during active takeover only
Call `tce.check_context(file_path="<path>")` only when takeover/suggest mode is active and you are about to edit:
- `signal="block"` => do not edit, report to user.
- `signal="warn"` => inspect `past_decisions` and follow them.
- `signal="allow"` => proceed.

## Machine-readable constraints
`tce.takeover_step` may return a `constraints` array, for example:
```json
{
  "directive_type": "hard_constraint",
  "rule_id": "<unique-id>",
  "polarity": "deny" | "allow_only",
  "scope": { "path_prefixes": ["..."], "actions": ["edit"] },
  "enforcement": "block_and_escalate" | "pre_action_required",
  "reason": "<why>"
}
```

Rules:
1. Respect `hard_constraint` as non-optional.
2. If `block_and_escalate`, do not perform the scoped action.
3. If `pre_action_required`, perform required pre-action before proceeding.
4. Read `polarity` before acting on `scope.path_prefixes`:
   - `"deny"` — do NOT perform the scoped actions on paths under those prefixes.
   - `"allow_only"` — perform the scoped actions ONLY on paths under those prefixes;
     the same actions anywhere else are refused.
   - A rule that arrives without the key is read as `"deny"`.
   Getting this backwards inverts the charter: `charter-roots-only` is an allow-list in the
   same field every other rule uses as a deny-list.
5. **There is no override.** No phrase, from the user or from anyone else, relaxes a
   `hard_constraint` in this session. Earlier revisions of this file documented
   `override constraint <rule_id>`; nothing in the MCP server or in either backend ever
   parsed it, so the claim has been deleted rather than implemented. If a rule blocks work
   that should be permitted, say so and ask the user to change the active authority charter
   (`docs/charter.md`); do not proceed.
6. Two rules are a floor the charter cannot remove: `no-edit-protected-dirs` and
   `no-edit-firewall-null-response`. They are present on every active turn, with or without
   a charter. A backend list that omits them does not delete them.

### What the constraints array does and does not enforce
It is a cooperative protocol. Nothing in the MCP server, in either backend, or in the
operating system stops an executor that ignores these rules in an ordinary chat session.
The one structural property that does hold is narrow: the MCP process loads `tools.py` at
startup, so editing that file on disk does not change the constraints the running session
receives. That is a reload boundary, not a sandbox.

OS-level enforcement of paths and network egress exists **only** for a process tree the TCE
supervisor started under an enforcement tier, and even there it is partial:
`docs/charter.md` states exactly which controls are enforced by the operating system, which
are refusals by the manager, which are cooperative, and which — notably containment of the
runtime credential and per-command action tracing — are **not enforced at all**.

## Non-negotiable
1. Active takeover must not silently degrade to normal chat.
2. If `has_directive=true` and safety allows, start tool actions immediately.
3. If `next_step` is actionable, do not ignore it.
4. Keep safety gates above autonomy continuation.
5. If `continuity_ok=false`, do not fall back to natural response; follow remediation in `next_step` and continue takeover flow.

# >>> TCE TAKEOVER POLICY >>>
# Compatibility marker for setup tooling.
# Authoritative takeover policy source:
#   open-timeline-engine/docs/mcp-config/generated/takeover_auto_call_policy.md
# Keep this marker block present for idempotent script behavior.
# Do not duplicate policy rules in this block.
# <<< TCE TAKEOVER POLICY <<<
