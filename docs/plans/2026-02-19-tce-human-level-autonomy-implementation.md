# Universal Decision Cloning — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform the TCE advisor from a pattern-matching engine into a user clone that makes decisions the way the user would, across all domains.

**Architecture:** Add three new layers: (1) a `decision_observations` table + observation extractor that learns situation→response pairs from timeline events, (2) a `behavioral_fingerprints` table that evolves a multi-domain user profile, (3) an `advisor_reason()` LLM reasoning function that uses a 6-layer clone prompt to generate responses as the user would. The existing `derive_clone_guidance()` becomes a fallback; the LLM path is the primary advisor logic.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (pgvector), Alembic raw SQL migrations, existing ModelGateway (Ollama/OpenAI/Anthropic), pytest, Docker Compose.

**Test runner:** `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/ -v`

**Rebuild after API changes:** `cd <repo-root>/infra && docker compose build tce-api && docker compose up -d tce-api`

---

## Task 1: Alembic Migration — decision_observations + behavioral_fingerprints tables

**Files:**
- Create: `infra/alembic/versions/20260219_0005_decision_cloning.py`

**Step 1: Write the migration**

```python
"""decision cloning tables: decision_observations + behavioral_fingerprints"""

from __future__ import annotations

from alembic import op

revision = "20260219_0005"
down_revision = "20260219_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_observations (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          consumer_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL DEFAULT 'default',
          ts TIMESTAMPTZ NOT NULL DEFAULT now(),
          situation_type TEXT NOT NULL,
          situation_summary TEXT NOT NULL,
          context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          user_response TEXT NOT NULL,
          response_reasoning TEXT NULL,
          outcome TEXT NULL,
          outcome_sentiment TEXT NULL,
          source_event_ids UUID[] NOT NULL DEFAULT '{}',
          confidence REAL NOT NULL DEFAULT 1.0
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_obs_consumer ON decision_observations (consumer_id, workspace_id, situation_type, ts DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS behavioral_fingerprints (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          consumer_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL DEFAULT 'default',
          fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,
          observation_count INTEGER NOT NULL DEFAULT 0,
          last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (consumer_id, workspace_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_fingerprint_consumer ON behavioral_fingerprints (consumer_id, workspace_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS clone_feedback (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          observation_id UUID REFERENCES decision_observations(id) ON DELETE CASCADE,
          session_id TEXT NOT NULL,
          feedback_type TEXT NOT NULL,
          correction_text TEXT NULL,
          ts TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS clone_feedback")
    op.execute("DROP TABLE IF EXISTS behavioral_fingerprints")
    op.execute("DROP TABLE IF EXISTS decision_observations")
```

**Step 2: Run migration**

Run: `cd <repo-root>/infra && docker compose run --rm tce-migrate`
Expected: Migration completes, tables created.

**Step 3: Commit**

```bash
git add infra/alembic/versions/20260219_0005_decision_cloning.py
git commit -m "feat: add decision_observations + behavioral_fingerprints tables"
```

---

## Task 2: SQLAlchemy Models for Decision Cloning

**Files:**
- Modify: `services/tce_api/tce_api/models.py` (append after line 185)
- Test: `tests/unit/test_clone_models.py`

**Step 1: Write the failing test**

Create `tests/unit/test_clone_models.py`:

```python
from __future__ import annotations

from tce_api.models import BehavioralFingerprint, CloneFeedback, DecisionObservation


def test_decision_observation_defaults():
    obs = DecisionObservation.__table__
    assert obs.name == "decision_observations"
    assert "situation_type" in {c.name for c in obs.columns}
    assert "user_response" in {c.name for c in obs.columns}
    assert "context_snapshot" in {c.name for c in obs.columns}


def test_behavioral_fingerprint_defaults():
    fp = BehavioralFingerprint.__table__
    assert fp.name == "behavioral_fingerprints"
    assert "fingerprint" in {c.name for c in fp.columns}
    assert "consumer_id" in {c.name for c in fp.columns}


def test_clone_feedback_defaults():
    fb = CloneFeedback.__table__
    assert fb.name == "clone_feedback"
    assert "feedback_type" in {c.name for c in fb.columns}
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_clone_models.py -v`
Expected: FAIL with ImportError — models don't exist yet.

**Step 3: Write minimal implementation**

Append to `services/tce_api/tce_api/models.py` after line 185 (after `AgentInteraction`):

```python
class DecisionObservation(Base):
    __tablename__ = "decision_observations"
    __table_args__ = (
        Index("idx_decision_obs_consumer", "consumer_id", "workspace_id", "situation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consumer_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, default="default")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    situation_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    situation_summary: Mapped[str] = mapped_column(TEXT, nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    user_response: Mapped[str] = mapped_column(TEXT, nullable=False)
    response_reasoning: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    outcome: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    outcome_sentiment: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    source_event_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, default=1.0)


class BehavioralFingerprint(Base):
    __tablename__ = "behavioral_fingerprints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consumer_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, default="default")
    fingerprint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CloneFeedback(Base):
    __tablename__ = "clone_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_observations.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    feedback_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    correction_text: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_clone_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/models.py tests/unit/test_clone_models.py
git commit -m "feat: add DecisionObservation, BehavioralFingerprint, CloneFeedback models"
```

---

## Task 3: Situation Classifier

**Files:**
- Create: `shared/tce_shared/situation.py`
- Test: `tests/unit/test_situation_classifier.py`

**Step 1: Write the failing test**

Create `tests/unit/test_situation_classifier.py`:

```python
from __future__ import annotations

from tce_shared.situation import SITUATION_TYPES, classify_situation


def test_classify_error():
    result = classify_situation("Build failed with exit code 1. TypeError in module X.")
    assert result == "error_occurred"


def test_classify_choice():
    result = classify_situation("Should we use Redis or Memcached for caching?")
    assert result == "choice_required"


def test_classify_approval():
    result = classify_situation("PR #42 needs your review and approval before merge.")
    assert result == "approval_requested"


def test_classify_blocker():
    result = classify_situation("Blocked: dependency X is not available in our registry.")
    assert result == "blocker_encountered"


def test_classify_routine():
    result = classify_situation("Deployed v2.3.1 to staging successfully.")
    assert result == "routine_task"


def test_classify_unknown_returns_valid_type():
    result = classify_situation("The sky is blue today.")
    assert result in SITUATION_TYPES


def test_all_types_present():
    assert len(SITUATION_TYPES) == 12
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_situation_classifier.py -v`
Expected: FAIL with ModuleNotFoundError.

**Step 3: Write minimal implementation**

Create `shared/tce_shared/situation.py`:

```python
from __future__ import annotations

import re

SITUATION_TYPES = (
    "blocker_encountered",
    "choice_required",
    "approval_requested",
    "error_occurred",
    "prioritization_needed",
    "communication_needed",
    "creative_decision",
    "conflict_detected",
    "unknown_territory",
    "routine_task",
    "escalation_point",
    "feedback_received",
)

_PATTERNS: list[tuple[str, list[str]]] = [
    ("error_occurred", [
        r"(?:error|exception|traceback|failed|failure|crash|bug|broke|broken)",
        r"(?:exit code [1-9]|typeerror|valueerror|keyerror|runtime error)",
    ]),
    ("blocker_encountered", [
        r"(?:blocked|blocking|cannot proceed|stuck|deadlock|unavailable|not available)",
        r"(?:dependency.+(?:missing|unavailable)|waiting on|depends on.+not ready)",
    ]),
    ("approval_requested", [
        r"(?:approve|approval|review|sign.?off|merge request|pull request|pr.+review)",
        r"(?:needs?.+(?:your|review|approval)|lgtm|please review)",
    ]),
    ("choice_required", [
        r"(?:should we|which (?:one|option)|choose between|pick|select|or we could)",
        r"(?:option [a-d]|alternative|trade.?off|versus|vs\.?)",
    ]),
    ("prioritization_needed", [
        r"(?:prioriti[sz]e|urgent|important|deadline|backlog|which first|next sprint)",
        r"(?:competing|conflicting priorities|resource allocation)",
    ]),
    ("feedback_received", [
        r"(?:feedback|review comment|suggestion|nit|improvement|could be better)",
        r"(?:code review|praised|complained|reported)",
    ]),
    ("communication_needed", [
        r"(?:reply|respond|message|email|slack|notify|update stakeholder)",
        r"(?:team meeting|standup|sync|announcement)",
    ]),
    ("conflict_detected", [
        r"(?:conflict|contradicts|incompatible|mismatch|disagree)",
        r"(?:merge conflict|breaking change|regression)",
    ]),
    ("escalation_point", [
        r"(?:escalat|beyond.+(?:scope|authority)|budget|management|policy)",
        r"(?:security incident|compliance|legal)",
    ]),
    ("creative_decision", [
        r"(?:design|ui|ux|naming|api shape|architecture decision|color|layout)",
        r"(?:no single right|subjective|aesthetic|branding)",
    ]),
    ("unknown_territory", [
        r"(?:never.+before|unfamiliar|new technology|first time|unknown|unexplored)",
        r"(?:no experience|learning curve|poc|prototype|experiment)",
    ]),
    ("routine_task", [
        r"(?:deploy|deployed|config|configured|update.+version|bump|release|migration ran)",
        r"(?:successfully|completed|done|finished|merged|shipped)",
    ]),
]


def classify_situation(text: str) -> str:
    lower = text.lower()
    best_type = "routine_task"
    best_score = 0
    for situation_type, patterns in _PATTERNS:
        score = 0
        for pattern in patterns:
            if re.search(pattern, lower):
                score += 1
        if score > best_score:
            best_score = score
            best_type = situation_type
    return best_type
```

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_situation_classifier.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add shared/tce_shared/situation.py tests/unit/test_situation_classifier.py
git commit -m "feat: add situation classifier with 12 decision types"
```

---

## Task 4: Behavioral Fingerprint Builder

**Files:**
- Create: `shared/tce_shared/fingerprint.py`
- Test: `tests/unit/test_fingerprint.py`

**Step 1: Write the failing test**

Create `tests/unit/test_fingerprint.py`:

```python
from __future__ import annotations

from tce_shared.fingerprint import (
    DEFAULT_FINGERPRINT,
    merge_observation_into_fingerprint,
    extract_communication_signals,
)


def test_default_fingerprint_has_all_domains():
    fp = DEFAULT_FINGERPRINT.copy()
    assert "decision_making" in fp
    assert "communication" in fp
    assert "priorities" in fp
    assert "context_switching" in fp
    assert "learning_style" in fp
    assert "emotional_patterns" in fp


def test_merge_updates_observation_count():
    fp = DEFAULT_FINGERPRINT.copy()
    observation = {
        "situation_type": "error_occurred",
        "user_response": "Let me investigate the root cause first",
        "outcome_sentiment": "positive",
    }
    updated = merge_observation_into_fingerprint(fp, observation)
    assert updated is not fp  # returns new dict


def test_extract_communication_signals_short():
    signals = extract_communication_signals("ok fix it")
    assert signals["verbosity"] == "terse"


def test_extract_communication_signals_long():
    signals = extract_communication_signals(
        "I think we should investigate the root cause thoroughly. "
        "Let me explain my reasoning in detail. First, the error pattern "
        "suggests a concurrency issue. Second, we saw similar behavior last week."
    )
    assert signals["verbosity"] in ("moderate", "verbose")
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_fingerprint.py -v`
Expected: FAIL with ModuleNotFoundError.

**Step 3: Write minimal implementation**

Create `shared/tce_shared/fingerprint.py`:

```python
from __future__ import annotations

import copy
from typing import Any

DEFAULT_FINGERPRINT: dict[str, Any] = {
    "decision_making": {
        "risk_tolerance": "moderate",
        "speed_vs_thoroughness": 0.5,
        "delegation_tendency": 0.5,
        "conflict_resolution_style": "compromise",
        "decision_reversal_frequency": 0.2,
        "information_needs_before_deciding": "moderate",
    },
    "communication": {
        "verbosity": "moderate",
        "formality": "neutral",
        "emoji_usage": False,
        "preferred_response_length": "medium",
        "explanation_depth": "moderate",
        "tone_under_pressure": "calm",
    },
    "priorities": {
        "speed_vs_quality": 0.5,
        "user_experience_vs_technical": 0.5,
        "pragmatic_vs_principled": 0.5,
        "top_recurring_concerns": [],
    },
    "context_switching": {
        "multitask_tolerance": "moderate",
        "interruption_handling": "accept",
        "context_retention_depth": "moderate",
    },
    "learning_style": {
        "exploration_vs_exploitation": 0.5,
        "feedback_response": "neutral",
        "mistake_handling": "investigate_root_cause",
    },
    "emotional_patterns": {
        "frustration_triggers": [],
        "satisfaction_signals": [],
        "stress_indicators": [],
    },
}


def extract_communication_signals(text: str) -> dict[str, str]:
    word_count = len(text.split())
    if word_count < 10:
        verbosity = "terse"
    elif word_count < 40:
        verbosity = "moderate"
    else:
        verbosity = "verbose"

    has_emoji = any(ord(ch) > 0x1F600 for ch in text)
    formality = "casual" if any(w in text.lower() for w in ("lol", "haha", "yeah", "nah", "ok")) else "neutral"

    return {
        "verbosity": verbosity,
        "emoji_usage": str(has_emoji).lower(),
        "formality": formality,
    }


def _update_running_average(current: float, new_value: float, alpha: float = 0.15) -> float:
    return round(current * (1 - alpha) + new_value * alpha, 3)


def merge_observation_into_fingerprint(
    fingerprint: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    fp = copy.deepcopy(fingerprint)
    situation_type = observation.get("situation_type", "")
    user_response = observation.get("user_response", "")
    outcome_sentiment = observation.get("outcome_sentiment", "")

    # Communication signals
    comm = extract_communication_signals(user_response)
    fp["communication"]["verbosity"] = comm["verbosity"]
    if comm["formality"] != "neutral":
        fp["communication"]["formality"] = comm["formality"]

    # Decision making signals from situation type
    response_lower = user_response.lower()

    if situation_type == "error_occurred":
        if "investigate" in response_lower or "root cause" in response_lower:
            fp["learning_style"]["mistake_handling"] = "investigate_root_cause"
            fp["decision_making"]["speed_vs_thoroughness"] = _update_running_average(
                fp["decision_making"]["speed_vs_thoroughness"], 0.8
            )
        elif "fix" in response_lower and "quick" in response_lower:
            fp["learning_style"]["mistake_handling"] = "fix_silently"
            fp["decision_making"]["speed_vs_thoroughness"] = _update_running_average(
                fp["decision_making"]["speed_vs_thoroughness"], 0.2
            )

    if situation_type == "choice_required":
        if "safe" in response_lower or "conservative" in response_lower:
            fp["decision_making"]["risk_tolerance"] = "conservative"
        elif "try" in response_lower or "experiment" in response_lower:
            fp["decision_making"]["risk_tolerance"] = "aggressive"

    if situation_type == "prioritization_needed":
        if "quality" in response_lower or "thorough" in response_lower:
            fp["priorities"]["speed_vs_quality"] = _update_running_average(
                fp["priorities"]["speed_vs_quality"], 0.8
            )
        elif "fast" in response_lower or "ship" in response_lower:
            fp["priorities"]["speed_vs_quality"] = _update_running_average(
                fp["priorities"]["speed_vs_quality"], 0.2
            )

    # Emotional pattern tracking
    if outcome_sentiment == "negative":
        triggers = fp["emotional_patterns"]["frustration_triggers"]
        if situation_type not in triggers:
            triggers.append(situation_type)
            fp["emotional_patterns"]["frustration_triggers"] = triggers[-5:]
    elif outcome_sentiment == "positive":
        signals = fp["emotional_patterns"]["satisfaction_signals"]
        if situation_type not in signals:
            signals.append(situation_type)
            fp["emotional_patterns"]["satisfaction_signals"] = signals[-5:]

    return fp
```

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_fingerprint.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add shared/tce_shared/fingerprint.py tests/unit/test_fingerprint.py
git commit -m "feat: add behavioral fingerprint builder with observation merging"
```

---

## Task 5: Observation Extractor (Timeline → Decision Observations)

**Files:**
- Create: `services/tce_api/tce_api/observation_extractor.py`
- Test: `tests/unit/test_observation_extractor.py`

**Step 1: Write the failing test**

Create `tests/unit/test_observation_extractor.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from tce_api.observation_extractor import extract_observations_from_events


def _event(event_type: str, title: str, decision: dict | None = None, outcome: dict | None = None) -> dict:
    return {
        "id": str(uuid4()),
        "ts": datetime.now(tz=UTC).isoformat(),
        "event_type": event_type,
        "title": title,
        "payload": {},
        "decision": decision,
        "outcome": outcome,
    }


def test_extract_from_decision_event():
    events = [
        _event(
            "decision",
            "Chose Redis over Memcached for session storage",
            decision={"choice": "Redis", "reasoning": "Better persistence support"},
            outcome={"result": "success", "sentiment": "positive"},
        )
    ]
    observations = extract_observations_from_events(events, consumer_id="user-1")
    assert len(observations) == 1
    assert observations[0]["situation_type"] == "choice_required"
    assert "Redis" in observations[0]["user_response"]


def test_extract_from_error_response_pair():
    events = [
        _event("error", "Build failed: TypeError in auth module"),
        _event("action", "Investigated root cause and fixed auth module type error"),
    ]
    observations = extract_observations_from_events(events, consumer_id="user-1")
    assert len(observations) >= 1
    obs = observations[0]
    assert obs["situation_type"] == "error_occurred"


def test_empty_events_returns_empty():
    assert extract_observations_from_events([], consumer_id="user-1") == []
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_observation_extractor.py -v`
Expected: FAIL with ModuleNotFoundError.

**Step 3: Write minimal implementation**

Create `services/tce_api/tce_api/observation_extractor.py`:

```python
from __future__ import annotations

from typing import Any

from tce_shared.situation import classify_situation


def extract_observations_from_events(
    events: list[dict[str, Any]],
    consumer_id: str,
    workspace_id: str = "default",
) -> list[dict[str, Any]]:
    if not events:
        return []

    observations: list[dict[str, Any]] = []

    for i, event in enumerate(events):
        event_type = event.get("event_type", "")
        title = event.get("title", "")
        decision = event.get("decision")
        outcome = event.get("outcome")

        # Direct decision events
        if decision and isinstance(decision, dict):
            situation_type = classify_situation(title)
            choice = decision.get("choice", title)
            reasoning = decision.get("reasoning", "")
            user_response = f"{choice}. {reasoning}".strip().rstrip(".")
            outcome_text = None
            outcome_sentiment = None
            if outcome and isinstance(outcome, dict):
                outcome_text = outcome.get("result", str(outcome))
                outcome_sentiment = outcome.get("sentiment")
            observations.append({
                "consumer_id": consumer_id,
                "workspace_id": workspace_id,
                "situation_type": situation_type,
                "situation_summary": title,
                "context_snapshot": event.get("payload", {}),
                "user_response": user_response,
                "response_reasoning": reasoning or None,
                "outcome": outcome_text,
                "outcome_sentiment": outcome_sentiment,
                "source_event_ids": [event.get("id")],
                "confidence": 1.0,
            })
            continue

        # Error → action pairs
        if event_type == "error" and i + 1 < len(events):
            next_event = events[i + 1]
            next_type = next_event.get("event_type", "")
            if next_type in ("action", "fix", "decision", "resolution"):
                observations.append({
                    "consumer_id": consumer_id,
                    "workspace_id": workspace_id,
                    "situation_type": "error_occurred",
                    "situation_summary": title,
                    "context_snapshot": event.get("payload", {}),
                    "user_response": next_event.get("title", ""),
                    "response_reasoning": None,
                    "outcome": None,
                    "outcome_sentiment": None,
                    "source_event_ids": [event.get("id"), next_event.get("id")],
                    "confidence": 0.8,
                })

    return observations
```

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_observation_extractor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/observation_extractor.py tests/unit/test_observation_extractor.py
git commit -m "feat: add observation extractor to convert timeline events to decision observations"
```

---

## Task 6: Clone Prompt Builder (6-Layer Architecture)

**Files:**
- Create: `services/tce_api/tce_api/clone_prompt.py`
- Test: `tests/unit/test_clone_prompt.py`

**Step 1: Write the failing test**

Create `tests/unit/test_clone_prompt.py`:

```python
from __future__ import annotations

from tce_shared.fingerprint import DEFAULT_FINGERPRINT
from tce_api.clone_prompt import build_clone_prompt


def test_prompt_contains_all_layers():
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[
            {
                "situation_summary": "Build failed",
                "user_response": "Investigate root cause",
                "outcome": "Fixed in 10 min",
            }
        ],
        session_context={
            "objective": "refactor auth module",
            "turn_count": 3,
            "turns": [],
            "unresolved_threads": [],
        },
        current_situation="Test suite failing on CI",
        situation_type="error_occurred",
    )
    assert "Joel" in prompt
    assert "error_occurred" in prompt
    assert "Test suite failing on CI" in prompt
    assert "Build failed" in prompt
    assert "step by step" in prompt.lower()


def test_prompt_includes_anti_patterns():
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "test", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Deploy to staging",
        situation_type="routine_task",
    )
    assert "ANTI-PATTERN" in prompt or "Do NOT" in prompt


def test_prompt_empty_observations():
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "test", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Something new",
        situation_type="unknown_territory",
    )
    assert "Joel" in prompt
    assert len(prompt) > 100
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_clone_prompt.py -v`
Expected: FAIL with ModuleNotFoundError.

**Step 3: Write minimal implementation**

Create `services/tce_api/tce_api/clone_prompt.py`:

```python
from __future__ import annotations

from typing import Any


def _format_fingerprint(fingerprint: dict[str, Any]) -> str:
    dm = fingerprint.get("decision_making", {})
    comm = fingerprint.get("communication", {})
    pri = fingerprint.get("priorities", {})
    ls = fingerprint.get("learning_style", {})
    lines = [
        f"- Decision style: risk_tolerance={dm.get('risk_tolerance', 'moderate')}, "
        f"speed_vs_thoroughness={dm.get('speed_vs_thoroughness', 0.5):.1f}",
        f"- Communication: verbosity={comm.get('verbosity', 'moderate')}, "
        f"formality={comm.get('formality', 'neutral')}, "
        f"typical response length: {comm.get('preferred_response_length', 'medium')}",
        f"- Priorities: speed_vs_quality={pri.get('speed_vs_quality', 0.5):.1f}, "
        f"top concerns: {', '.join(pri.get('top_recurring_concerns', [])) or 'none observed yet'}",
        f"- Under pressure: {comm.get('tone_under_pressure', 'calm')}",
        f"- When uncertain: exploration_tendency={ls.get('exploration_vs_exploitation', 0.5):.1f}",
    ]
    return "\n".join(lines)


def _format_observations(observations: list[dict[str, Any]]) -> str:
    if not observations:
        return "No similar past situations found. Use behavioral patterns and defaults."
    lines = []
    for i, obs in enumerate(observations[:5], 1):
        situation = obs.get("situation_summary", "unknown")
        response = obs.get("user_response", "unknown")
        outcome = obs.get("outcome", "unknown")
        lines.append(f"  {i}. Situation: {situation}")
        lines.append(f"     Decision: {response}")
        lines.append(f"     Outcome: {outcome}")
    return "\n".join(lines)


def _format_session(session_context: dict[str, Any]) -> str:
    objective = session_context.get("objective", "not set")
    turn_count = session_context.get("turn_count", 0)
    turns = session_context.get("turns", [])
    unresolved = session_context.get("unresolved_threads", [])
    lines = [
        f"- Objective: {objective}",
        f"- Turn {turn_count} of this session",
    ]
    if turns:
        lines.append("- Previous decisions this session:")
        for turn in turns[-3:]:
            lines.append(f"  - {turn.get('situation', '?')} -> {turn.get('decision', '?')}")
    if unresolved:
        lines.append(f"- Unresolved threads: {', '.join(str(t) for t in unresolved[:3])}")
    return "\n".join(lines)


def build_clone_prompt(
    user_name: str,
    fingerprint: dict[str, Any],
    similar_observations: list[dict[str, Any]],
    session_context: dict[str, Any],
    current_situation: str,
    situation_type: str,
    extra_context: str = "",
) -> str:
    layer_1 = (
        f"You are {user_name}. You are not an AI assistant — you ARE this person.\n"
        "Your job is to make the exact decision this person would make in this situation.\n"
        "Do not hedge, do not offer alternatives unless this person typically does."
    )

    layer_2 = (
        "Here are similar situations this person faced before, and what they decided:\n"
        + _format_observations(similar_observations)
    )

    layer_3 = (
        "This person's behavioral profile:\n"
        + _format_fingerprint(fingerprint)
    )

    layer_4 = (
        "Current session state:\n"
        + _format_session(session_context)
    )

    layer_5 = (
        f"Current situation (classified as {situation_type}):\n"
        f"{current_situation}"
    )
    if extra_context:
        layer_5 += f"\n\nAdditional context:\n{extra_context}"

    layer_6 = (
        "Think step by step:\n"
        "1. How would this person perceive this situation?\n"
        "2. What past experiences would they draw on?\n"
        "3. What would they prioritize?\n"
        "4. What would they actually do (not what's \"optimal\")?\n"
        "5. How would they communicate their decision?\n\n"
        "Respond as this person would — same words, same level of detail, same style.\n\n"
        "IMPORTANT: Return a JSON object with these fields:\n"
        '  {"reasoning": "your step-by-step thinking", '
        '"decision": "the actual response/directive", '
        '"confidence": 0.0-1.0, '
        '"communication_style": "terse|moderate|verbose"}\n\n'
        "Do NOT do these:\n"
        "- Do NOT list multiple options unless this person typically does\n"
        "- Do NOT ask clarifying questions unless this person would\n"
        "- Do NOT hedge with \"it depends\" unless that's their pattern\n"
        "- Do NOT be more cautious than this person would be"
    )

    sections = [
        "=== IDENTITY ===",
        layer_1,
        "",
        "=== HISTORICAL EVIDENCE ===",
        layer_2,
        "",
        "=== BEHAVIORAL PATTERNS ===",
        layer_3,
        "",
        "=== SESSION CONTEXT ===",
        layer_4,
        "",
        "=== CURRENT SITUATION ===",
        layer_5,
        "",
        "=== REASONING INSTRUCTION ===",
        layer_6,
    ]

    return "\n".join(sections)
```

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_clone_prompt.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/clone_prompt.py tests/unit/test_clone_prompt.py
git commit -m "feat: add 6-layer clone prompt builder for user decision cloning"
```

---

## Task 7: LLM Reasoning Function — advisor_reason()

**Files:**
- Modify: `services/tce_api/tce_api/clone.py` (add `advisor_reason()` after `derive_clone_guidance()`)
- Test: `tests/unit/test_advisor_reason.py`

**Step 1: Write the failing test**

Create `tests/unit/test_advisor_reason.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

from tce_shared.fingerprint import DEFAULT_FINGERPRINT
from tce_api.clone import advisor_reason


def _mock_gateway(response: dict) -> MagicMock:
    gw = MagicMock()
    gw.extract_structured.return_value = response
    return gw


def test_advisor_reason_returns_structured_response():
    gw = _mock_gateway({
        "reasoning": "User typically investigates root cause",
        "decision": "Investigate the failing test root cause before fixing",
        "confidence": 0.85,
        "communication_style": "moderate",
    })
    result = advisor_reason(
        gateway=gw,
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "fix tests", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Test suite failing on CI",
        situation_type="error_occurred",
    )
    assert result["decision"] == "Investigate the failing test root cause before fixing"
    assert result["confidence"] == 0.85
    gw.extract_structured.assert_called_once()


def test_advisor_reason_fallback_on_error():
    gw = MagicMock()
    gw.extract_structured.side_effect = RuntimeError("LLM unavailable")
    result = advisor_reason(
        gateway=gw,
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=[],
        session_context={"objective": "fix tests", "turn_count": 1, "turns": [], "unresolved_threads": []},
        current_situation="Test suite failing on CI",
        situation_type="error_occurred",
    )
    assert "decision" in result
    assert result.get("fallback") is True
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_advisor_reason.py -v`
Expected: FAIL with ImportError — `advisor_reason` doesn't exist yet.

**Step 3: Write minimal implementation**

Add to `services/tce_api/tce_api/clone.py` after line 61 (after `derive_clone_guidance()`):

```python
import logging
from tce_model_gateway.gateway import ModelGateway
from .clone_prompt import build_clone_prompt

logger = logging.getLogger(__name__)


def advisor_reason(
    gateway: ModelGateway,
    user_name: str,
    fingerprint: dict,
    similar_observations: list[dict],
    session_context: dict,
    current_situation: str,
    situation_type: str,
    extra_context: str = "",
) -> dict:
    prompt = build_clone_prompt(
        user_name=user_name,
        fingerprint=fingerprint,
        similar_observations=similar_observations,
        session_context=session_context,
        current_situation=current_situation,
        situation_type=situation_type,
        extra_context=extra_context,
    )
    try:
        result = gateway.extract_structured(prompt, "clone_decision")
        if isinstance(result, dict) and "decision" in result:
            return result
        # Handle case where LLM returns {"raw": "..."} wrapper
        raw = result.get("raw", "")
        if isinstance(raw, str) and raw.strip():
            return {
                "reasoning": "",
                "decision": raw.strip(),
                "confidence": 0.5,
                "communication_style": "moderate",
            }
        return _fallback_response(current_situation, situation_type)
    except Exception:
        logger.warning("advisor_reason LLM call failed, using fallback", exc_info=True)
        return _fallback_response(current_situation, situation_type)


def _fallback_response(current_situation: str, situation_type: str) -> dict:
    return {
        "reasoning": "LLM unavailable, using conservative defaults",
        "decision": f"Proceed with safest approach for: {current_situation[:200]}",
        "confidence": 0.3,
        "communication_style": "moderate",
        "fallback": True,
    }
```

Also add the necessary imports at the top of `clone.py`:
- `import logging`
- `from tce_model_gateway.gateway import ModelGateway`
- `from .clone_prompt import build_clone_prompt`

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_advisor_reason.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/clone.py tests/unit/test_advisor_reason.py
git commit -m "feat: add advisor_reason() LLM function with chain-of-thought clone prompt"
```

---

## Task 8: Observation + Fingerprint DB Operations

**Files:**
- Create: `services/tce_api/tce_api/clone_store.py`
- Test: `tests/unit/test_clone_store.py`

**Step 1: Write the failing test**

Create `tests/unit/test_clone_store.py`:

```python
from __future__ import annotations

from tce_api.clone_store import (
    query_similar_observations,
    build_session_context_from_state,
)


def test_build_session_context_empty():
    ctx = build_session_context_from_state({})
    assert "objective" in ctx
    assert "turn_count" in ctx
    assert "turns" in ctx
    assert "unresolved_threads" in ctx


def test_build_session_context_with_data():
    ctx = build_session_context_from_state({
        "objective": "fix auth",
        "turn_count": 5,
        "session_turns": [
            {"situation": "error", "decision": "investigate"},
        ],
        "unresolved_threads": ["update docs"],
    })
    assert ctx["objective"] == "fix auth"
    assert ctx["turn_count"] == 5
    assert len(ctx["turns"]) == 1
```

**Step 2: Run test to verify it fails**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_clone_store.py -v`
Expected: FAIL with ModuleNotFoundError.

**Step 3: Write minimal implementation**

Create `services/tce_api/tce_api/clone_store.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def query_similar_observations(
    db: Session,
    consumer_id: str,
    workspace_id: str,
    situation_type: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT id, situation_type, situation_summary, context_snapshot,
                   user_response, response_reasoning, outcome, outcome_sentiment,
                   source_event_ids, confidence, ts
            FROM decision_observations
            WHERE consumer_id = :consumer_id
              AND workspace_id = :workspace_id
              AND situation_type = :situation_type
            ORDER BY ts DESC
            LIMIT :limit
            """
        ),
        {
            "consumer_id": consumer_id,
            "workspace_id": workspace_id,
            "situation_type": situation_type,
            "limit": limit,
        },
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "situation_type": row[1],
            "situation_summary": row[2],
            "context_snapshot": row[3] or {},
            "user_response": row[4],
            "response_reasoning": row[5],
            "outcome": row[6],
            "outcome_sentiment": row[7],
            "source_event_ids": row[8] or [],
            "confidence": row[9],
            "ts": row[10].isoformat() if row[10] else None,
        }
        for row in rows
    ]


def load_fingerprint(db: Session, consumer_id: str, workspace_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT fingerprint, observation_count, last_updated_at
            FROM behavioral_fingerprints
            WHERE consumer_id = :consumer_id AND workspace_id = :workspace_id
            """
        ),
        {"consumer_id": consumer_id, "workspace_id": workspace_id},
    ).fetchone()
    if not row:
        return None
    return row[0] if isinstance(row[0], dict) else {}


def save_fingerprint(
    db: Session,
    consumer_id: str,
    workspace_id: str,
    fingerprint: dict[str, Any],
    observation_count: int,
) -> None:
    import json

    now = datetime.now(tz=UTC)
    db.execute(
        text(
            """
            INSERT INTO behavioral_fingerprints (consumer_id, workspace_id, fingerprint, observation_count, last_updated_at, created_at)
            VALUES (:consumer_id, :workspace_id, :fingerprint::jsonb, :observation_count, :now, :now)
            ON CONFLICT (consumer_id, workspace_id)
            DO UPDATE SET fingerprint = :fingerprint::jsonb, observation_count = :observation_count, last_updated_at = :now
            """
        ),
        {
            "consumer_id": consumer_id,
            "workspace_id": workspace_id,
            "fingerprint": json.dumps(fingerprint),
            "observation_count": observation_count,
            "now": now,
        },
    )
    db.commit()


def save_observation(db: Session, observation: dict[str, Any]) -> UUID:
    import json

    now = datetime.now(tz=UTC)
    result = db.execute(
        text(
            """
            INSERT INTO decision_observations
                (consumer_id, workspace_id, ts, situation_type, situation_summary,
                 context_snapshot, user_response, response_reasoning, outcome,
                 outcome_sentiment, source_event_ids, confidence)
            VALUES
                (:consumer_id, :workspace_id, :ts, :situation_type, :situation_summary,
                 :context_snapshot::jsonb, :user_response, :response_reasoning, :outcome,
                 :outcome_sentiment, :source_event_ids, :confidence)
            RETURNING id
            """
        ),
        {
            "consumer_id": observation["consumer_id"],
            "workspace_id": observation.get("workspace_id", "default"),
            "ts": now,
            "situation_type": observation["situation_type"],
            "situation_summary": observation["situation_summary"],
            "context_snapshot": json.dumps(observation.get("context_snapshot", {})),
            "user_response": observation["user_response"],
            "response_reasoning": observation.get("response_reasoning"),
            "outcome": observation.get("outcome"),
            "outcome_sentiment": observation.get("outcome_sentiment"),
            "source_event_ids": observation.get("source_event_ids", []),
            "confidence": observation.get("confidence", 1.0),
        },
    )
    db.commit()
    return result.scalar_one()


def build_session_context_from_state(takeover_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "objective": takeover_context.get("objective", "not set"),
        "turn_count": takeover_context.get("turn_count", 0),
        "turns": takeover_context.get("session_turns", []),
        "unresolved_threads": takeover_context.get("unresolved_threads", []),
    }
```

**Step 4: Run test to verify it passes**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/test_clone_store.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/clone_store.py tests/unit/test_clone_store.py
git commit -m "feat: add clone store with observation queries, fingerprint CRUD, session context builder"
```

---

## Task 9: Wire advisor_reason() into clone_advice Endpoint

**Files:**
- Modify: `services/tce_api/tce_api/main.py:1146-1174` (the `clone_advice()` endpoint body)
- Modify: `services/tce_api/tce_api/config.py` (add `clone_reasoning_enabled` setting)

**Step 1: Add config setting**

In `services/tce_api/tce_api/config.py`, add after line 42 (`auto_capture_interactions`):

```python
    clone_reasoning_enabled: bool = True
    clone_user_name: str = "User"
```

**Step 2: Modify clone_advice endpoint**

In `services/tce_api/tce_api/main.py`, after the `build_context_bundle()` call at line 1151 and before the `derive_clone_guidance()` call at line 1153, insert the LLM reasoning path.

Replace lines 1153-1174 with:

```python
    # --- LLM clone reasoning path ---
    clone_decision = None
    if settings.clone_reasoning_enabled:
        try:
            from tce_model_gateway.factory import create_gateway
            from .clone_store import (
                build_session_context_from_state,
                load_fingerprint,
                query_similar_observations,
            )
            from .clone import advisor_reason
            from tce_shared.fingerprint import DEFAULT_FINGERPRINT
            from tce_shared.situation import classify_situation

            situation_type = classify_situation(body.task)
            gateway = create_gateway(settings)
            fingerprint = load_fingerprint(db, auth.consumer, auth.workspace_id) or DEFAULT_FINGERPRINT
            similar_obs = query_similar_observations(
                db, auth.consumer, auth.workspace_id, situation_type, limit=5
            )
            session_ctx = build_session_context_from_state(body.takeover_context)

            clone_decision = advisor_reason(
                gateway=gateway,
                user_name=settings.clone_user_name,
                fingerprint=fingerprint,
                similar_observations=similar_obs,
                session_context=session_ctx,
                current_situation=body.task,
                situation_type=situation_type,
                extra_context=bundle.summary,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Clone reasoning failed, falling back to rule-based", exc_info=True)
            clone_decision = None

    # --- Rule-based fallback ---
    summary, recommended_actions, confidence, evidence_strength, conflict_flags = derive_clone_guidance(
        bundle, body.executor_output
    )

    # If LLM reasoning succeeded, use it to enhance the response
    if clone_decision and not clone_decision.get("fallback"):
        summary = clone_decision.get("decision", summary)
        confidence = max(confidence, clone_decision.get("confidence", 0.0))
        if confidence >= 0.5:
            evidence_strength = "strong" if confidence >= 0.7 else "moderate"

    response = CloneAdviceResponse(
        interaction_id=interaction_id,
        guidance_summary=summary,
        recommended_actions=recommended_actions,
        do=bundle.do_dont.get("do", []),
        dont=bundle.do_dont.get("dont", []),
        confidence=confidence,
        evidence_strength=evidence_strength,
        citations=bundle.citations,
        conflict_flags=conflict_flags,
        loop_guard={
            "allowed": True,
            "reason": "ok",
            "turns_in_last_hour": turns,
            "max_turns": settings.clone_max_turns_per_interaction,
        },
        policy=policy_engine.summarize(blocked_count=blocked_count, applied_redactions=redactions, role=auth.role),
    )
```

**Step 3: Run all tests to verify nothing broke**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/ -v`
Expected: All existing tests pass (30 pass, 2 skip, 1 known pre-existing failure).

**Step 4: Commit**

```bash
git add services/tce_api/tce_api/main.py services/tce_api/tce_api/config.py
git commit -m "feat: wire advisor_reason() LLM into clone_advice endpoint with rule-based fallback"
```

---

## Task 10: Session Turn Recording in Takeover State

**Files:**
- Modify: `services/tce_api/tce_api/main.py:1379-1407` (the takeover_step context update section)

**Step 1: Add session turn recording**

In the takeover_step endpoint, after the `resolve_objective()` call, add session turn recording to the takeover_context. Find the section that updates `state.takeover_context` and add:

```python
    # Record this turn in session memory
    if state.active and final_response:
        session_turns = state.takeover_context.get("session_turns", [])
        session_turns.append({
            "turn_number": int(state.takeover_context.get("turn_count", 0)),
            "situation": body.task or body.message[:140],
            "decision": (final_response or "")[:200],
            "classification": classification.value if classification else "unknown",
        })
        # Keep last 20 turns
        state.takeover_context["session_turns"] = session_turns[-20:]
```

This goes after the enforcement and safety evaluation but before `save_takeover_state()`.

**Step 2: Run all tests**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/ -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add services/tce_api/tce_api/main.py
git commit -m "feat: record session turns in takeover_context for causal session memory"
```

---

## Task 11: Observation Ingestion Endpoint

**Files:**
- Modify: `services/tce_api/tce_api/main.py` (add new endpoint after clone_advice)

**Step 1: Add the endpoint**

After the `clone_advice()` endpoint in `main.py`, add:

```python
class IngestObservationsRequest(BaseModel):
    consumer_id: str
    workspace_id: str = "default"
    event_ids: list[UUID] = Field(default_factory=list)
    max_events: int = 50


@app.post("/v1/clone/ingest-observations")
def ingest_observations(
    body: IngestObservationsRequest,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    from .observation_extractor import extract_observations_from_events
    from .clone_store import save_observation, load_fingerprint, save_fingerprint
    from tce_shared.fingerprint import DEFAULT_FINGERPRINT, merge_observation_into_fingerprint

    # Fetch recent events for this consumer
    if body.event_ids:
        rows = db.execute(
            text(
                "SELECT id, ts, event_type, title, payload, decision, outcome "
                "FROM events WHERE id = ANY(:ids) ORDER BY ts"
            ),
            {"ids": body.event_ids},
        ).fetchall()
    else:
        rows = db.execute(
            text(
                "SELECT id, ts, event_type, title, payload, decision, outcome "
                "FROM events WHERE context->>'_tce_owner' = :consumer_id "
                "ORDER BY ts DESC LIMIT :limit"
            ),
            {"consumer_id": body.consumer_id, "limit": body.max_events},
        ).fetchall()

    events = [
        {
            "id": str(row[0]),
            "ts": row[1].isoformat() if row[1] else None,
            "event_type": row[2],
            "title": row[3],
            "payload": row[4] or {},
            "decision": row[5],
            "outcome": row[6],
        }
        for row in rows
    ]

    observations = extract_observations_from_events(events, consumer_id=body.consumer_id, workspace_id=body.workspace_id)

    saved_ids = []
    for obs in observations:
        obs_id = save_observation(db, obs)
        saved_ids.append(str(obs_id))

    # Update fingerprint
    fingerprint = load_fingerprint(db, body.consumer_id, body.workspace_id) or DEFAULT_FINGERPRINT.copy()
    for obs in observations:
        fingerprint = merge_observation_into_fingerprint(fingerprint, obs)
    save_fingerprint(db, body.consumer_id, body.workspace_id, fingerprint, len(saved_ids))

    return {
        "observations_created": len(saved_ids),
        "observation_ids": saved_ids,
        "fingerprint_updated": True,
    }
```

Add these imports at the top of `main.py` if not already present:
- `from pydantic import BaseModel, Field`
- `from uuid import UUID`
- `from sqlalchemy import text`

**Step 2: Run all tests**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/ -v`
Expected: All pass.

**Step 3: Rebuild and test manually**

Run: `cd <repo-root>/infra && docker compose build tce-api && docker compose up -d tce-api`

**Step 4: Commit**

```bash
git add services/tce_api/tce_api/main.py
git commit -m "feat: add /v1/clone/ingest-observations endpoint for learning from timeline"
```

---

## Task 12: Expose Observation Ingestion as MCP Tool

**Files:**
- Modify: `services/tce_mcp/tce_mcp/tools.py` (add `ingest_observations` tool)

**Step 1: Add MCP tool**

In `services/tce_mcp/tce_mcp/tools.py`, add after the existing tools:

```python
@server.tool()
async def ingest_observations(
    consumer_id: str | None = None,
    workspace_id: str = "default",
    max_events: int = 50,
) -> dict:
    """Scan recent timeline events and extract decision observations to teach the advisor how you respond."""
    resolved_consumer = consumer_id or _consumer_id()
    payload = {
        "consumer_id": resolved_consumer,
        "workspace_id": workspace_id,
        "max_events": max_events,
    }
    result = await _post("/v1/clone/ingest-observations", payload)
    return with_schema(result)
```

**Step 2: Run MCP tool shape tests**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker:services/tce_mcp .venv-check/bin/python -m pytest tests/mcp/ -v`
Expected: Pass.

**Step 3: Commit**

```bash
git add services/tce_mcp/tce_mcp/tools.py
git commit -m "feat: expose ingest_observations as MCP tool for clone learning"
```

---

## Task 13: Auto-Ingestion on Takeover Session End

**Files:**
- Modify: `services/tce_api/tce_api/main.py` (in the stop-keyword handling section of takeover_step)

**Step 1: Add auto-ingestion trigger**

In the takeover_step endpoint, when a stop keyword is detected and the session is deactivated, trigger observation ingestion for the session's events. Find the section that handles stop keywords (around line 1281-1312) and add after deactivation:

```python
    # Auto-ingest observations from this session's events
    try:
        from .observation_extractor import extract_observations_from_events
        from .clone_store import save_observation, load_fingerprint, save_fingerprint
        from tce_shared.fingerprint import DEFAULT_FINGERPRINT, merge_observation_into_fingerprint

        session_turns = state.takeover_context.get("session_turns", [])
        if session_turns:
            pseudo_events = [
                {
                    "id": None,
                    "ts": None,
                    "event_type": "decision",
                    "title": turn.get("situation", ""),
                    "payload": {},
                    "decision": {"choice": turn.get("decision", ""), "reasoning": ""},
                    "outcome": None,
                }
                for turn in session_turns
                if turn.get("situation") and turn.get("decision")
            ]
            observations = extract_observations_from_events(
                pseudo_events, consumer_id=auth.user_id, workspace_id=auth.workspace_id
            )
            fingerprint = load_fingerprint(db, auth.user_id, auth.workspace_id) or DEFAULT_FINGERPRINT.copy()
            for obs in observations:
                save_observation(db, obs)
                fingerprint = merge_observation_into_fingerprint(fingerprint, obs)
            if observations:
                save_fingerprint(db, auth.user_id, auth.workspace_id, fingerprint, len(observations))
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Auto-ingestion on session end failed", exc_info=True)
```

**Step 2: Run all tests**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/ -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add services/tce_api/tce_api/main.py
git commit -m "feat: auto-ingest observations when takeover session ends"
```

---

## Task 14: Final Integration Test + Docker Rebuild

**Files:**
- Test: `tests/unit/test_clone_integration.py`

**Step 1: Write integration test**

Create `tests/unit/test_clone_integration.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tce_shared.fingerprint import DEFAULT_FINGERPRINT
from tce_shared.situation import classify_situation
from tce_api.clone import advisor_reason
from tce_api.clone_prompt import build_clone_prompt
from tce_api.clone_store import build_session_context_from_state
from tce_api.observation_extractor import extract_observations_from_events


def test_end_to_end_clone_reasoning_flow():
    """Simulate the full flow: events → observations → fingerprint → prompt → LLM → decision."""

    # 1. Extract observations from events
    events = [
        {
            "id": "fake-id-1",
            "ts": "2026-02-19T10:00:00Z",
            "event_type": "decision",
            "title": "Chose PostgreSQL over MongoDB for user data",
            "payload": {"domain": "infrastructure"},
            "decision": {"choice": "PostgreSQL", "reasoning": "Better for relational data"},
            "outcome": {"result": "success", "sentiment": "positive"},
        }
    ]
    observations = extract_observations_from_events(events, consumer_id="user-1")
    assert len(observations) == 1

    # 2. Classify a new situation
    situation = "Should we use SQLite or PostgreSQL for the new microservice?"
    situation_type = classify_situation(situation)
    assert situation_type == "choice_required"

    # 3. Build session context
    ctx = build_session_context_from_state({"objective": "build microservice", "turn_count": 2})
    assert ctx["objective"] == "build microservice"

    # 4. Build prompt
    prompt = build_clone_prompt(
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=observations,
        session_context=ctx,
        current_situation=situation,
        situation_type=situation_type,
    )
    assert "Joel" in prompt
    assert "PostgreSQL" in prompt

    # 5. Call advisor_reason with mock gateway
    mock_gw = MagicMock()
    mock_gw.extract_structured.return_value = {
        "reasoning": "User chose PostgreSQL before for similar reasons",
        "decision": "Use PostgreSQL — consistent with past infrastructure choices",
        "confidence": 0.9,
        "communication_style": "moderate",
    }
    result = advisor_reason(
        gateway=mock_gw,
        user_name="Joel",
        fingerprint=DEFAULT_FINGERPRINT,
        similar_observations=observations,
        session_context=ctx,
        current_situation=situation,
        situation_type=situation_type,
    )
    assert "PostgreSQL" in result["decision"]
    assert result["confidence"] == 0.9
```

**Step 2: Run full test suite**

Run: `cd <repo-root> && PYTHONPATH=shared:services/tce_api:services/tce_worker .venv-check/bin/python -m pytest tests/unit/ -v`
Expected: All new tests pass alongside existing tests.

**Step 3: Rebuild Docker**

Run: `cd <repo-root>/infra && docker compose build tce-api && docker compose up -d tce-api`

**Step 4: Run migration**

Run: `cd <repo-root>/infra && docker compose run --rm tce-migrate`

**Step 5: Commit**

```bash
git add tests/unit/test_clone_integration.py
git commit -m "feat: add end-to-end clone reasoning integration test"
```

---

## Summary of Files Created/Modified

### New files:
1. `infra/alembic/versions/20260219_0005_decision_cloning.py` — Migration
2. `shared/tce_shared/situation.py` — 12-type situation classifier
3. `shared/tce_shared/fingerprint.py` — Behavioral fingerprint builder
4. `services/tce_api/tce_api/observation_extractor.py` — Timeline → observations
5. `services/tce_api/tce_api/clone_prompt.py` — 6-layer clone prompt builder
6. `services/tce_api/tce_api/clone_store.py` — DB operations for observations/fingerprints
7. `tests/unit/test_clone_models.py`
8. `tests/unit/test_situation_classifier.py`
9. `tests/unit/test_fingerprint.py`
10. `tests/unit/test_observation_extractor.py`
11. `tests/unit/test_clone_prompt.py`
12. `tests/unit/test_advisor_reason.py`
13. `tests/unit/test_clone_store.py`
14. `tests/unit/test_clone_integration.py`

### Modified files:
1. `services/tce_api/tce_api/models.py` — Add 3 new models
2. `services/tce_api/tce_api/clone.py` — Add `advisor_reason()` + `_fallback_response()`
3. `services/tce_api/tce_api/config.py` — Add `clone_reasoning_enabled`, `clone_user_name`
4. `services/tce_api/tce_api/main.py` — Wire LLM into clone_advice, add ingestion endpoint, session turns, auto-ingest
5. `services/tce_mcp/tce_mcp/tools.py` — Add `ingest_observations` MCP tool
