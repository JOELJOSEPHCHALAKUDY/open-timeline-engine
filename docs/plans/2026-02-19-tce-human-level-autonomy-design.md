# TCE Human-Level Autonomy: Universal Decision Cloning

**Date:** 2026-02-19
**Status:** Approved
**Goal:** Achieve human-level autonomy by teaching the advisor to respond as the user would, using timeline-learned behavioral patterns across all decision domains.

---

## 1. Core Concept

The advisor is not a generic AI assistant — it is a **user clone**. During takeover mode, the advisor should respond exactly as the user would: same decision-making patterns, same communication style, same priorities, same emotional responses. The timeline is the training data — every recorded event teaches the advisor how the user thinks, decides, and acts.

This applies universally — not just to coding decisions, but to all situations: approvals, prioritization, communication, error handling, creative choices, and more.

---

## 2. Reasoning Engine

### 2.1 LLM "Think Like User" Step

Replace the current rule-based `derive_clone_guidance()` with an LLM reasoning call via the existing model gateway (`create_gateway(settings)`).

**New function:** `advisor_reason()` in `services/tce_api/tce_api/clone.py`

**Flow:**
1. Gather context bundle (events, patterns, workflows, do/don't rules)
2. Build behavioral fingerprint from timeline
3. Build clone prompt (6-layer architecture — see Section 5)
4. Call LLM with chain-of-thought: "Think step by step about how this user would respond"
5. Parse structured response: `{reasoning, decision, confidence, communication_style}`
6. Fall back to current rule-based logic if LLM call fails

**Model:** Uses the advisor's configured model via `TCE_MODEL_PROVIDER` (set during install, user-changeable for both advisor and executor).

**Key principle:** The LLM prompt says "You ARE this person" not "You are helping this person."

### 2.2 Chain-of-Thought Reasoning

The LLM call includes explicit reasoning instructions:
- "Before responding, think step by step about how this user would approach this situation"
- "Consider their past decisions in similar situations"
- "Match their communication style and level of detail"
- "If uncertain, lean toward their most common pattern"

This implements the "wait, think, and respond" approach for better output quality.

---

## 3. Universal Behavioral Fingerprint

### 3.1 Multi-Domain Profile

Stored as a JSONB column on the user/consumer record. Evolves continuously from timeline observations.

```
behavioral_fingerprint = {
    "decision_making": {
        "risk_tolerance": "conservative|moderate|aggressive",
        "speed_vs_thoroughness": 0.0-1.0,  # 0 = fast, 1 = thorough
        "delegation_tendency": 0.0-1.0,
        "conflict_resolution_style": "avoid|compromise|assert",
        "decision_reversal_frequency": 0.0-1.0,
        "information_needs_before_deciding": "minimal|moderate|extensive"
    },
    "communication": {
        "verbosity": "terse|moderate|verbose",
        "formality": "casual|neutral|formal",
        "emoji_usage": true/false,
        "preferred_response_length": "short|medium|detailed",
        "explanation_depth": "surface|moderate|deep",
        "tone_under_pressure": "calm|urgent|frustrated"
    },
    "priorities": {
        "speed_vs_quality": 0.0-1.0,
        "user_experience_vs_technical": 0.0-1.0,
        "pragmatic_vs_principled": 0.0-1.0,
        "top_recurring_concerns": ["security", "performance", ...]
    },
    "context_switching": {
        "multitask_tolerance": "sequential|moderate|heavy",
        "interruption_handling": "resist|accept|welcome",
        "context_retention_depth": "shallow|moderate|deep"
    },
    "learning_style": {
        "exploration_vs_exploitation": 0.0-1.0,  # 0 = stick to known, 1 = try new
        "feedback_response": "defensive|neutral|receptive",
        "mistake_handling": "fix_silently|acknowledge|investigate_root_cause"
    },
    "emotional_patterns": {
        "frustration_triggers": ["repeated_failures", "slow_progress", ...],
        "satisfaction_signals": ["feature_shipped", "clean_solution", ...],
        "stress_indicators": ["short_messages", "multiple_exclamation_marks", ...]
    }
}
```

### 3.2 Fingerprint Evolution

The fingerprint is not static — it evolves with every timeline event:
- New observations update running averages
- Recent events weighted higher (exponential decay)
- Contradictions logged and resolved via temporal weighting (see Section 8)
- Confidence scores per dimension (low confidence = fall back to defaults)

---

## 4. Situation Classification & Response Memory

### 4.1 Decision Classifier

Classify each incoming situation into one of 12 types:

| Type | Description | Example |
|------|-------------|---------|
| `blocker_encountered` | Something prevents progress | Build failure, dependency conflict |
| `choice_required` | Multiple valid options | Library selection, architecture decision |
| `approval_requested` | Someone asks for sign-off | PR review, deploy approval |
| `error_occurred` | Something broke | Runtime error, test failure |
| `prioritization_needed` | Multiple tasks compete | Feature vs bugfix, urgent vs important |
| `communication_needed` | Must respond to someone | Email, Slack, code review comment |
| `creative_decision` | No single right answer | UI design, naming, API shape |
| `conflict_detected` | Contradictory requirements | Speed vs quality, feature vs deadline |
| `unknown_territory` | No prior experience | New technology, unfamiliar domain |
| `routine_task` | Familiar, low-stakes work | Standard deployment, config change |
| `escalation_point` | Situation exceeds authority | Budget decision, policy change |
| `feedback_received` | Input about past work | Code review, user complaint, praise |

### 4.2 Situation-Response Memory (Decision Observation DB)

New database table: `decision_observations`

```sql
CREATE TABLE decision_observations (
    id UUID PRIMARY KEY,
    consumer_id VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    situation_type VARCHAR NOT NULL,        -- one of 12 types
    situation_summary TEXT NOT NULL,         -- what happened
    context_snapshot JSONB,                 -- relevant state at decision time
    user_response TEXT NOT NULL,            -- what the user actually did
    response_reasoning TEXT,                -- why (if observable)
    outcome TEXT,                           -- what happened next
    outcome_sentiment VARCHAR,              -- positive/negative/neutral
    source_event_ids UUID[],               -- timeline events that formed this observation
    confidence FLOAT DEFAULT 1.0
);
```

**Population:** A background worker scans timeline events and extracts situation-response pairs. For example:
- User receives error event → next event is a fix → record as `error_occurred` → `investigate_root_cause` response
- User receives PR review → next event is approval → record as `approval_requested` → typical response pattern

### 4.3 Causal Session Memory

Track within each takeover session:
```
session_memory = {
    "turns": [
        {
            "turn_number": 1,
            "situation_type": "choice_required",
            "situation": "Which database migration strategy?",
            "decision": "Chose incremental migrations",
            "outcome": "Migration succeeded",
            "confidence": 0.85
        },
        ...
    ],
    "running_context": "Working on database refactor, 3 migrations complete",
    "unresolved_threads": ["Need to update API docs after migration"]
}
```

This gives the advisor memory across turns within a session, so it doesn't repeat questions or lose track of what's been decided.

---

## 5. Six-Layer Clone Prompt Architecture

The LLM prompt for `advisor_reason()` is built from 6 layers:

### Layer 1: Identity
```
You are {user_name}. You are not an AI assistant — you ARE this person.
Your job is to make the exact decision this person would make in this situation.
Do not hedge, do not offer alternatives unless this person typically does.
```

### Layer 2: Historical Evidence
```
Here are {N} similar situations this person faced before, and what they decided:
- Situation: {situation_1} → Decision: {response_1} → Outcome: {outcome_1}
- Situation: {situation_2} → Decision: {response_2} → Outcome: {outcome_2}
...
(Most recent first, weighted by recency and relevance)
```

### Layer 3: Behavioral Patterns
```
This person's behavioral profile:
- Decision style: {risk_tolerance}, {speed_vs_thoroughness}
- Communication: {verbosity}, {formality}, typical response length: {preferred_response_length}
- Priorities: speed_vs_quality={value}, top concerns: {top_recurring_concerns}
- Under pressure: {tone_under_pressure}
- When uncertain: {exploration_vs_exploitation tendency}
```

### Layer 4: Session Context
```
Current session state:
- Objective: {objective}
- Turn {N} of this session
- Previous decisions this session: {causal_session_memory}
- Unresolved threads: {unresolved_threads}
```

### Layer 5: Current Situation
```
Current situation (classified as {situation_type}):
{situation_description}

Available context:
{relevant_events, patterns, workflows from context bundle}
```

### Layer 6: Reasoning Instruction
```
Think step by step:
1. How would this person perceive this situation?
2. What past experiences would they draw on?
3. What would they prioritize?
4. What would they actually do (not what's "optimal")?
5. How would they communicate their decision?

Respond as this person would — same words, same level of detail, same style.

ANTI-PATTERNS (do NOT do these):
- Do not list multiple options unless this person typically does
- Do not ask clarifying questions unless this person would
- Do not hedge with "it depends" unless that's their pattern
- Do not be more cautious than this person would be
```

---

## 6. Continuous Learning Pipeline

### 6.1 Observation Recording

A background process continuously scans new timeline events and:
1. Classifies each event into a situation type
2. Pairs events into situation→response→outcome chains
3. Stores as `decision_observations`
4. Updates the behavioral fingerprint running averages

### 6.2 Fingerprint Drift Detection

Track fingerprint values over time. If a dimension shifts significantly (e.g., `risk_tolerance` moves from conservative to aggressive over 2 weeks), log it as a behavioral shift event. This prevents the clone from using stale patterns.

### 6.3 Clone Accuracy Scoring

After each takeover session, compare the advisor's decisions against what the user actually does when they return:
- If user continues with advisor's work: score +1 (implicit approval)
- If user undoes/changes advisor's work: score -1 (implicit correction)
- If user explicitly corrects: record as correction event with high weight

Running accuracy score per situation type helps identify where the clone is strong vs weak.

---

## 7. Feedback Loop

### 7.1 Explicit Corrections

When the user says "no, I would have done X" or undoes an advisor action:
- Record as high-weight correction event
- Immediately update relevant fingerprint dimensions
- Store the correct response in decision_observations with boosted confidence

### 7.2 Implicit Signals

Track without user action:
- **Approval signals:** User continues session without changes, uses advisor output as-is
- **Rejection signals:** User reverts changes, restarts session, changes objective
- **Style signals:** User's message length, tone, speed of response

### 7.3 Correction Propagation

When a correction is recorded:
1. Find similar past observations in the same situation type
2. Reduce their confidence scores
3. The corrected version gets highest confidence
4. Next time a similar situation arises, the correction takes precedence

---

## 8. Predictive Context & Workflow Chains

### 8.1 Multi-Step Prediction

Instead of responding to one situation at a time, predict the likely next 2-3 steps based on historical workflow chains:

```
If user typically does: fix bug → run tests → update docs → deploy
And we're at: fix bug (done) → run tests (current)
Then: pre-fetch doc context and deployment configs
```

This allows the advisor to include forward-looking context in its directives, enabling the executor to work through multi-step workflows without stopping.

### 8.2 Workflow Chain Storage

Extract common sequences from timeline events:
```
workflow_chains = [
    {
        "trigger": "error_occurred",
        "typical_sequence": ["investigate_logs", "identify_root_cause", "fix", "test", "deploy"],
        "frequency": 15,
        "avg_completion_time": "45min"
    },
    ...
]
```

---

## 9. Contradiction Resolution

### 9.1 Temporal Weighting

When past decisions contradict each other:
- Recent decisions weighted exponentially higher
- Explicit corrections always override implicit patterns
- If still ambiguous, use the response from the most similar situation context

### 9.2 Contradiction Logging

```
contradictions = [
    {
        "dimension": "risk_tolerance",
        "observation_a": {"value": "conservative", "ts": "2026-02-10", "context": "production deploy"},
        "observation_b": {"value": "aggressive", "ts": "2026-02-18", "context": "dev experiment"},
        "resolution": "context_dependent",  # user is conservative in prod, aggressive in dev
        "resolved_rule": "if context contains 'production' → conservative, else → moderate"
    }
]
```

This enables context-dependent behavioral modeling — the user might be cautious about production but experimental in development.

---

## 10. Cold Start Bootstrap

### 10.1 Git History Import

On first setup, scan git history to extract:
- Commit patterns (frequency, message style, size)
- Code review patterns (approve speed, comment depth, common feedback)
- Branch naming conventions
- Work schedule patterns

### 10.2 Onboarding Interview

During setup, ask 5-10 quick questions:
- "When you see a failing test, do you fix it immediately or investigate root cause first?"
- "Do you prefer detailed explanations or just the answer?"
- "How do you handle conflicting priorities?"

Store answers as high-confidence seed observations.

### 10.3 Shadow Mode Learning

Before full takeover activation, run in shadow mode:
- Advisor generates what it *would* respond
- Compares against what user *actually* responds
- Adjusts fingerprint based on delta
- Logs accuracy for user review ("I was 73% accurate this week")

---

## 11. Architecture Summary

```
Timeline Events
      │
      ▼
┌─────────────────┐
│ Observation      │──► decision_observations table
│ Extractor        │──► behavioral_fingerprint updates
│ (background)     │──► workflow_chain extraction
└─────────────────┘
      │
      ▼
┌─────────────────┐    ┌──────────────────┐
│ Situation        │◄───│ Incoming request  │
│ Classifier       │    │ (takeover_step)   │
└─────────────────┘    └──────────────────┘
      │
      ▼
┌─────────────────┐
│ Clone Prompt     │──► 6-layer prompt builder
│ Builder          │──► historical evidence retrieval
│                  │──► session memory injection
└─────────────────┘
      │
      ▼
┌─────────────────┐
│ advisor_reason() │──► LLM call with chain-of-thought
│ (model gateway)  │──► structured response parsing
└─────────────────┘
      │
      ▼
┌─────────────────┐
│ ensure_takeover  │──► enforcement + safety checks
│ _response()      │──► final directive to executor
└─────────────────┘
      │
      ▼
┌─────────────────┐
│ Feedback Loop    │──► correction recording
│ (post-session)   │──► accuracy scoring
│                  │──► fingerprint evolution
└─────────────────┘
```

---

## 12. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Clone depth | Full (decisions + style) | User explicitly requested human-level clone |
| LLM for reasoning | Yes, via model gateway | Rule-based cannot capture nuance |
| Multi-domain fingerprint | Yes, 6 domains | User emphasized "not just coding" |
| Situation classifier | 12 types | Covers all common decision scenarios |
| Session memory | Causal chains | Prevents repetition, maintains context |
| Cold start | Git + interview + shadow | Fastest path to useful clone accuracy |
| Feedback | Implicit + explicit | Both signals improve accuracy |
| Contradictions | Temporal + contextual | People behave differently in different contexts |

---

## 13. Success Criteria

1. **Clone accuracy > 80%** — advisor's decisions match what user would do in > 80% of situations
2. **No handoff regression** — safety-critical situations still pause for confirmation
3. **Style match** — response tone, length, and vocabulary match user's patterns
4. **Cross-domain** — works for coding, communication, prioritization, and creative decisions
5. **Continuous improvement** — accuracy measurably improves week over week
6. **Cold start viable** — useful within first 50 timeline events (not requiring thousands)
