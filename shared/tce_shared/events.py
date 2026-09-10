from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# ``DreamProposalStatus`` and ``NonresponseState`` are declared in
# ``tce_shared.aspirations`` and imported here, never mirrored.  Two StrEnums that must
# agree is a drift waiting to happen, and the dependency only runs one way: this module
# imports pydantic, ``aspirations`` must not, because the worker loads it.
from .aspirations import DreamProposalStatus, NonresponseState

# The seven decision-policy enums are declared in ``tce_shared.decision_policy`` and
# re-exported here, never re-declared.  ``decision_policy`` must stay pydantic-free so the
# worker can import it, and this module imports pydantic below, so the dependency can only
# point one way.  ``QualificationState`` has no reader in this module; it is re-exported for
# the two backends, which is why it carries the redundant alias.
from .decision_policy import (
    AbstainReason,
    AdvisorAgreement,
    ConflictStatus,
    DecisionStatus,
    ExposureState,
    OodStatus,
)
from .decision_policy import QualificationState as QualificationState
from .task_state import NextPermittedAction, TaskStatus
from .version import SCHEMA_VERSION


class TrustedInputOriginKind(StrEnum):
    HUMAN_INPUT = "human_input"
    MANAGER_INSTRUCTION = "manager_instruction"
    EXECUTOR_OUTPUT = "executor_output"
    IMPORTED_TRANSCRIPT = "imported_transcript"
    TOOL_RESULT = "tool_result"


class CaptureDeliveryState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    SPOOLING = "spooling"
    GAP = "gap"
    UNAVAILABLE = "unavailable"


class EventType(StrEnum):
    TASK_START = "TASK_START"
    TASK_STEP = "TASK_STEP"
    TASK_DECISION = "TASK_DECISION"
    TASK_DONE = "TASK_DONE"
    CODE_CHANGE = "CODE_CHANGE"
    COMMAND_RUN = "COMMAND_RUN"
    DOC_EDIT = "DOC_EDIT"
    ERROR = "ERROR"
    FIX = "FIX"
    REVIEW = "REVIEW"
    REFLECTION = "REFLECTION"


class OperationMode(StrEnum):
    TIMELINE_ONLY = "timeline_only"
    CLONE_ADVISOR = "clone_advisor"


class AgentRole(StrEnum):
    USER = "user"
    EXECUTOR = "executor"
    ADVISOR = "advisor"


class EventStep(BaseModel):
    order: int = Field(ge=0)
    description: str
    tool: str | None = None
    output_ref: str | None = None


class EventDecision(BaseModel):
    choice: str
    alternatives: list[str] = Field(default_factory=list)
    rationale: str
    signals_used: list[str] = Field(default_factory=list)


class EventOutcome(BaseModel):
    success: bool
    metrics: dict[str, Any] = Field(default_factory=dict)
    followups: list[str] = Field(default_factory=list)


class EventStyle(BaseModel):
    ui_theme: str | None = None
    code_style: str | None = None
    do_first: list[str] = Field(default_factory=list)
    do_last: list[str] = Field(default_factory=list)


class EventLinks(BaseModel):
    issue: str | None = None
    pr: str | None = None
    docs: list[str] = Field(default_factory=list)


class EventEnvelope(BaseModel):
    schema_version: int = Field(default=SCHEMA_VERSION)
    ts: datetime
    actor: str
    source: str
    domain: str
    task_type: str
    event_type: EventType
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    steps: list[EventStep] = Field(default_factory=list)
    decision: EventDecision | None = None
    outcome: EventOutcome | None = None
    style: EventStyle | None = None
    links: EventLinks | None = None
    tags: list[str] = Field(default_factory=list)
    sensitivity: int = Field(default=1, ge=0, le=3)
    redaction_hints: list[str] = Field(default_factory=list)
    source_id: str | None = None
    source_seq: int | None = Field(default=None, ge=0)
    vector_clock: dict[str, int] = Field(default_factory=dict)
    idempotency_key: str | None = None
    authority_level: str = "incidental"

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be >= 1")
        return value


class EventFilter(BaseModel):
    domain: str | None = None
    task_type: str | None = None
    actor: str | None = None
    source: str | None = None
    min_sensitivity: int | None = Field(default=None, ge=0, le=3)
    max_sensitivity: int | None = Field(default=None, ge=0, le=3)


class EventSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    match_all: bool = False
    filters: EventFilter = Field(default_factory=EventFilter)
    k: int = Field(default=10, ge=1, le=100)
    time_start: datetime | None = None
    time_end: datetime | None = None
    continuity_intent: bool = False
    target_owner: str | None = None
    project_id: str | None = None


class EventSearchHit(BaseModel):
    id: UUID
    ts: datetime
    title: str
    domain: str
    task_type: str
    score: float
    sensitivity: int
    summary_l0: str = ""
    summary_l1: dict[str, Any] = Field(default_factory=dict)


class EventSearchResponse(BaseModel):
    hits: list[EventSearchHit]
    citations: list[UUID]


class PatternFeedbackRequest(BaseModel):
    pattern_id: UUID
    approved: bool
    note: str | None = None


class EpisodeDecision(BaseModel):
    decision: str
    why: str = ""
    alternatives: list[str] = Field(default_factory=list)


class EpisodeLesson(BaseModel):
    do_more: list[str] = Field(default_factory=list)
    do_less: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class EpisodeArtifactRef(BaseModel):
    kind: str
    ref: str
    hash: str | None = None


class Episode(BaseModel):
    id: UUID
    workspace_id: str
    user_id: str
    session_id: str = "default"
    goal: str
    context: str = ""
    decisions: list[EpisodeDecision] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    artifacts: list[EpisodeArtifactRef] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    outcome: str = ""
    lessons: EpisodeLesson = Field(default_factory=EpisodeLesson)
    confidence: float = 0.0
    status: str = "open"
    authority_score: float = 0.0
    stability_score: float = 0.0
    source_event_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EventAnnotationRequest(BaseModel):
    event_id: UUID
    session_id: str = "default"
    goal: str | None = None
    decision: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    authority_level: str | None = None


class EventAnnotationResponse(BaseModel):
    event_id: UUID
    episode_id: UUID | None = None
    authority_level: str = "incidental"
    updated: bool = True


class ContextBriefRequest(BaseModel):
    task: str
    session_id: str = "default"
    app_context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    max_items: int = Field(default=15, ge=5, le=30)
    max_tokens: int = Field(default=1600, ge=400, le=4000)


class ContextBriefResponse(BaseModel):
    summary: str
    standard_approach: list[dict[str, Any]] = Field(default_factory=list)
    current_state: list[dict[str, Any]] = Field(default_factory=list)
    constraints_preferences: list[dict[str, Any]] = Field(default_factory=list)
    open_loops: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[UUID] = Field(default_factory=list)
    generated_at: datetime


class MemoryRuleType(StrEnum):
    AVOID = "avoid"
    ALWAYS = "always"
    PREFER = "prefer"
    SECURITY = "security"
    STYLE = "style"


class MemoryRuleScope(BaseModel):
    workspace_id: str | None = None
    session_id: str | None = None
    domain: str | None = None
    repo: str | None = None


class MemoryRule(BaseModel):
    id: UUID
    workspace_id: str
    user_id: str
    scope: MemoryRuleScope = Field(default_factory=MemoryRuleScope)
    rule_type: MemoryRuleType
    statement: str
    priority: int = Field(default=2, ge=0, le=3)
    active: bool = True
    evergreen: bool = True
    expires_at: datetime | None = None
    source_episode_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class MemoryRuleUpsertRequest(BaseModel):
    scope: MemoryRuleScope = Field(default_factory=MemoryRuleScope)
    rule_type: MemoryRuleType
    statement: str
    priority: int = Field(default=2, ge=0, le=3)
    evergreen: bool = True
    expires_at: datetime | None = None
    source_episode_id: UUID | None = None


class MemoryForgetRequest(BaseModel):
    target_type: str
    target_ids: list[str] = Field(default_factory=list)
    reason: str = "user_requested"
    hard_delete: bool = True


class MemoryForgetResponse(BaseModel):
    deleted_count: int = 0
    tombstone_id: UUID
    target_type: str
    target_ids: list[str] = Field(default_factory=list)


class RetrievalEvalRunRequest(BaseModel):
    session_id: str = "default"
    tasks: list[str] = Field(default_factory=list)
    with_brief: bool = True


class RetrievalEvalRunResponse(BaseModel):
    run_id: UUID
    session_id: str
    style_alignment: float = 0.0
    constraint_compliance: float = 0.0
    decision_traceability: float = 0.0
    followup_reduction: float = 0.0
    started_at: datetime
    completed_at: datetime


class RuntimeModeConfig(BaseModel):
    mode: OperationMode = OperationMode.TIMELINE_ONLY
    clone_enabled: bool = False
    updated_at: datetime
    updated_by: str


class RuntimeModeUpdate(BaseModel):
    mode: OperationMode


class TakeoverMode(StrEnum):
    SUGGEST = "suggest"
    TAKEOVER = "takeover"


class TakeoverClassification(StrEnum):
    DECISIVE = "decisive"
    QUESTION = "question"
    HANDOFF = "handoff"
    SUGGESTION = "suggestion"
    EMPTY = "empty"
    ERROR = "error"


class SafetyDecision(StrEnum):
    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    BLOCKED = "blocked"


class TakeoverDecisionSource(StrEnum):
    FAST_PATH = "fast_path"
    DELIBERATION = "deliberation"
    SAFETY_GATE = "safety_gate"


class CloneFeedbackType(StrEnum):
    HELPFUL = "helpful"
    UNHELPFUL = "unhelpful"
    NEUTRAL = "neutral"


class BehaviorMemoryClass(StrEnum):
    DECISION = "decision"
    FACT = "fact"
    PREFERENCE = "preference"
    SAFETY_CONSTRAINT = "safety_constraint"
    PROCEDURAL_RUNBOOK = "procedural_runbook"
    EPISODE = "episode"
    HYPOTHESIS = "hypothesis"
    REJECTED_HYPOTHESIS = "rejected_hypothesis"


class BehaviorEvidenceSource(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    CORRECTION = "correction"
    CALIBRATION = "calibration"
    BACKFILL = "backfill"


class BehaviorLifecycleStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class BehaviorProjectionFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"
    HTML = "html"


class BehaviorProjectionView(StrEnum):
    CURRENT = "current"
    DECISIONS = "decisions"
    EVIDENCE = "evidence"
    REVIEW = "review"


class BehaviorPilotVariant(StrEnum):
    NO_MEMORY = "no_memory"
    CANONICAL_STRUCTURED = "canonical_structured"
    MARKDOWN_PROJECTION = "markdown_projection"
    PROJECTION_INDEX = "projection_index"


class BehaviorPilotStatus(StrEnum):
    COLLECTING = "collecting"
    READY_FOR_REVIEW = "ready_for_review"
    FAILED_QUALITY = "failed_quality"
    FAILED_SAFETY = "failed_safety"


class AutonomyPolicyProfile(StrEnum):
    HUMAN_CONSULTATIVE = "human_consultative"
    HUMAN_SAFE = "human_safe"
    HUMAN_AGGRESSIVE = "human_aggressive"


class AutonomyGoalStatus(StrEnum):
    CANDIDATE = "candidate"
    SELECTED = "selected"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    DONE = "done"
    DROPPED = "dropped"


class AutonomyGoalSource(StrEnum):
    USER_OBJECTIVE = "user_objective"
    OPEN_DISCOVERY = "open_discovery"


class GoalKind(StrEnum):
    NORMAL = "normal"
    UNKNOWN = "unknown"
    NOTHING = "nothing"


class AutonomyRiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionPermitDecision(StrEnum):
    ALLOW = "allow"
    CONFIRM_REQUIRED = "confirm_required"
    BLOCKED = "blocked"


class DirectiveExecutionState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class FailureClass(StrEnum):
    TOOL_ERROR = "tool_error"
    CONSTRAINT_BLOCK = "constraint_block"
    SAFETY_BLOCK = "safety_block"
    VALIDATION_FAILURE = "validation_failure"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class RetryStrategy(StrEnum):
    NARROW_SCOPE = "narrow_scope"
    READ_ONLY_DIAGNOSE = "read_only_diagnose"
    ALTERNATE_PATH = "alternate_path"
    ROLLBACK_THEN_RETRY = "rollback_then_retry"
    ESCALATE = "escalate"


class TakeoverPolicy(BaseModel):
    safety_policy: str = "high-risk-pause"
    confirm_keyword: str = "confirm"
    deny_keyword: str = "abort"
    timeout_minutes: int = 120
    auto_handoff_on_question: bool = True


class TakeoverNextAction(BaseModel):
    kind: str = "execute"
    target: str = ""
    rationale: str = ""


class TakeoverLatencyBreakdown(BaseModel):
    state: int = 0
    classify: int = 0
    retrieval: int = 0
    safety: int = 0
    total: int = 0


# --------------------------------------------------------------------------- task state (P2)
#
# The pydantic layer may depend on the pure layer; the reverse is forbidden. Mirroring the
# two task-state enums here rather than re-declaring their values by hand is what keeps the
# wire vocabulary and the fold's vocabulary from drifting apart.


class TaskLifecycleStatus(StrEnum):
    AWAITING_OBJECTIVE = "awaiting_objective"
    PLANNING = "planning"
    ACTIVE = "active"
    BLOCKED = "blocked"
    AWAITING_DECISION = "awaiting_decision"
    AWAITING_VERIFICATION = "awaiting_verification"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskNextPermittedAction(StrEnum):
    AWAIT_OWNER_OBJECTIVE = "await_owner_objective"
    AWAIT_PLANNING = "await_planning"
    EXECUTE_STEP = "execute_step"
    AWAIT_DECISION = "await_decision"
    AWAIT_VERIFICATION = "await_verification"
    RESOLVE_EFFECTS = "resolve_effects"
    BLOCKED = "blocked"
    NONE = "none"


def to_lifecycle_status(value: TaskStatus | str) -> TaskLifecycleStatus:
    """The ONE conversion at every wire boundary.

    Unknown values fail closed to AWAITING_VERIFICATION: never DONE, and never a terminal
    that silently stops autonomy. Assigning a TaskStatus straight into a
    TaskLifecycleStatus field is a type error, which is what stops two backends inventing
    two conversions and diverging on the unknown case.
    """
    try:
        return TaskLifecycleStatus(str(value))
    except ValueError:
        return TaskLifecycleStatus.AWAITING_VERIFICATION


def to_next_permitted_action(value: NextPermittedAction | str) -> TaskNextPermittedAction:
    """The ONE conversion at every wire boundary. Unknown fails closed to NONE, never EXECUTE_STEP."""
    try:
        return TaskNextPermittedAction(str(value))
    except ValueError:
        return TaskNextPermittedAction.NONE


class TaskStateVerificationRef(BaseModel):
    verification_id: str
    directive_id: UUID | None = None
    state: str = "unverified"
    method: str = "none"
    recorded_at: datetime | None = None
    contract_revision: int = 0
    plan_id: str | None = None
    evidence_event_ids: list[UUID] = Field(default_factory=list)
    summary: str = ""


class TaskStateUnresolvedEffect(BaseModel):
    effect_id: str
    kind: str = "directive"
    description: str = ""
    opened_at: datetime | None = None
    directive_id: UUID | None = None
    paths: list[str] = Field(default_factory=list)


class TaskStateSummary(BaseModel):
    task_id: str = ""
    revision: int = 0
    contract_revision: int = 0
    status: TaskLifecycleStatus = TaskLifecycleStatus.AWAITING_OBJECTIVE
    next_permitted_action: TaskNextPermittedAction = TaskNextPermittedAction.AWAIT_OWNER_OBJECTIVE
    plan_state: str = "absent"
    plan_producer: str | None = None
    open_step_index: int | None = None
    open_decision_count: int = 0
    unresolved_effect_count: int = 0
    source_revision: str = ""


class TaskStateProjectionResponse(BaseModel):
    projection_id: UUID
    uri: str
    task_id: str = ""
    view: str = "state"
    format: str = "markdown"
    mime_type: str = "text/markdown; charset=utf-8"
    schema_version: str = "v1"
    source_revision: str = ""
    content_sha256: str = ""
    generated_at: datetime
    revision: int = 0
    contract_revision: int = 0
    source_evidence_ids: list[UUID] = Field(default_factory=list)
    trust_level: str = "projection"
    sensitivity: int = Field(default=1, ge=0, le=3)
    read_only: bool = True
    projection_learning_eligible: bool = False
    expires_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)
    truncated: bool = False
    redaction_applied: bool = False
    content: str = ""


class TaskCancelRequest(BaseModel):
    reason: str = Field(default="owner_cancelled", max_length=400)


class TaskCancelResponse(BaseModel):
    task_id: str = ""
    revision: int = 0
    cancelled_planning_jobs: int = 0
    cancelled_directives: int = 0
    revoked_constraints: int = 0
    expired_permits: int = 0
    cancelled_rq_jobs: int = 0


class PlanningJobStatusResponse(BaseModel):
    job_id: UUID
    task_id: str = ""
    job_kind: str = ""
    state: str = "pending"
    attempts: int = 0
    max_attempts: int = 3
    producer: str | None = None
    contract_revision: int = 0
    input_revision: str = ""
    queue_state: str = "inline"
    cancel_requested: bool = False
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TakeoverGoal(BaseModel):
    id: UUID
    session_id: str
    workspace_id: str
    user_id: str
    title: str
    description: str
    source: AutonomyGoalSource = AutonomyGoalSource.OPEN_DISCOVERY
    priority_score: float = 0.0
    risk_tier: AutonomyRiskTier = AutonomyRiskTier.MEDIUM
    confidence: float = 0.0
    reasoning: str = ""
    evidence_event_ids: list[UUID] = Field(default_factory=list)
    goal_kind: GoalKind = GoalKind.NORMAL
    affective_scores: dict[str, Any] = Field(default_factory=dict)
    selection_score: float = 0.0
    cache_hit: bool = False
    cache_source: str | None = None
    status: AutonomyGoalStatus = AutonomyGoalStatus.CANDIDATE
    created_at: datetime
    updated_at: datetime
    step_index: int | None = None
    parent_goal_id: UUID | None = None
    depends_on: list[int] = Field(default_factory=list)
    attempts: int = 0
    mutating: bool = False


class AutonomyNotice(BaseModel):
    id: UUID
    session_id: str
    workspace_id: str
    user_id: str
    goal_id: UUID | None = None
    title: str
    reason: str
    priority: float = 0.0
    expires_at: datetime | None = None
    created_at: datetime
    acknowledged_at: datetime | None = None


class DirectiveExecution(BaseModel):
    directive_id: UUID
    session_id: str
    workspace_id: str
    user_id: str
    goal_id: UUID | None = None
    objective_hash: str | None = None
    action_kind: str = "execute"
    attempt: int = 1
    state: DirectiveExecutionState = DirectiveExecutionState.PENDING
    requires_permit: bool = False
    permit_id: UUID | None = None
    claimed_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    failure_class: FailureClass | None = None
    failure_reason: str | None = None
    retry_strategy: RetryStrategy | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    lease_generation: int = 0
    claimed_executor: str | None = None
    lease_expires_at: datetime | None = None
    verification_state: str = "unverified"
    report_idempotency_key: str | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None


class AffectiveScores(BaseModel):
    temporal: dict[str, Any] = Field(default_factory=dict)
    emotional: dict[str, Any] = Field(default_factory=dict)
    behavioral: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    human: dict[str, Any] = Field(default_factory=dict)
    similarity: dict[str, Any] = Field(default_factory=dict)
    computed_at: str | None = None
    version: int = 1


class TakeoverState(BaseModel):
    session_id: str
    workspace_id: str
    user_id: str
    active: bool = False
    mode: TakeoverMode = TakeoverMode.TAKEOVER
    persona_mode: str = "normal"
    activation_keywords: str = ""
    stop_keywords: str = ""
    expires_at: datetime | None = None
    activated_at: datetime | None = None
    last_message_at: datetime | None = None
    takeover_context: dict[str, Any] = Field(default_factory=dict)
    objective_hash: str | None = None
    working_set_json: dict[str, Any] = Field(default_factory=dict)
    last_deliberation_at: datetime | None = None
    recent_outcomes_json: list[dict[str, Any]] = Field(default_factory=list)
    autonomy_score: float = 0.5
    autonomy_policy_profile: AutonomyPolicyProfile = AutonomyPolicyProfile.HUMAN_CONSULTATIVE
    active_goal_id: UUID | None = None
    goal_queue_size: int = 0
    last_discovery_at: datetime | None = None
    continuity_violation_count: int = 0
    enforcement_mode: str = "strict_takeover"
    last_tick_at: datetime | None = None
    pending_directive_count: int = 0
    retry_backlog_count: int = 0
    last_classification: TakeoverClassification | None = None
    last_safety_decision: SafetyDecision = SafetyDecision.ALLOW
    updated_at: datetime


class TakeoverStepRequest(BaseModel):
    message: str
    session_id: str = "default"
    persona_mode: str = "normal"
    activation_keywords: str | None = None
    stop_keywords: str | None = None
    activation_mode_default: TakeoverMode = TakeoverMode.TAKEOVER
    policy: TakeoverPolicy = Field(default_factory=TakeoverPolicy)
    task: str | None = None
    app_context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    takeover_context: dict[str, Any] = Field(default_factory=dict)
    message_delta: dict[str, Any] = Field(default_factory=dict)
    interaction_id: str | None = None
    executor_output: str | None = None
    allow_fallback: bool = True
    real_takeover: bool = False
    real_takeover_mode: TakeoverMode = TakeoverMode.SUGGEST
    advisor_command: str | None = None
    advisor_timeout_seconds: int = 45
    enrich_with_tce: bool = True


class TakeoverStepResponse(BaseModel):
    state: TakeoverState
    action: str
    classification: TakeoverClassification
    enforced: bool = False
    enforcement_reason: str | None = None
    final_response: str | None = None
    safety_decision: SafetyDecision = SafetyDecision.ALLOW
    takeover_enforcement: dict[str, Any] = Field(default_factory=dict)
    auto_handoff: dict[str, Any] = Field(default_factory=dict)
    clone_advice: dict[str, Any] = Field(default_factory=dict)
    citations: list[UUID] = Field(default_factory=list)
    note: str | None = None
    decision_confidence: float = 0.0
    decision_source: TakeoverDecisionSource = TakeoverDecisionSource.FAST_PATH
    next_action: TakeoverNextAction = Field(default_factory=TakeoverNextAction)
    latency_breakdown_ms: TakeoverLatencyBreakdown = Field(default_factory=TakeoverLatencyBreakdown)
    needs_human: bool = False
    selected_goal: TakeoverGoal | None = None
    execution_permit_required: bool = False
    execution_permit_id: str | None = None
    continuity_ok: bool = True
    directive_id: UUID | None = None
    directive_state: DirectiveExecutionState | None = None
    pending_execution: DirectiveExecution | None = None
    retry_scheduled: bool = False
    autonomy_notice: AutonomyNotice | None = None
    goal_cache_hit: bool = False
    goal_cache_source: str | None = None
    selected_goal_score_breakdown: dict[str, float] = Field(default_factory=dict)
    context_quality_score: float = 0.0
    retrieval_triggered: bool = False
    retrieval_source: str = "none"
    retrieval_reason: str | None = None
    project_binding: str = "unbound"
    retrieval_latency_ms: int = 0
    retrieval_hit_count: int = 0
    feedback_adjustment_applied: bool = False
    snapshot_rehydrated: bool = False
    snapshot_age_hours: int | None = None
    query_expansion_used: bool = False
    query_expansion_terms: list[str] = Field(default_factory=list)
    rerank_strategy: str = "none"
    context_tier_used: str = "l2"
    summary_coverage: float = 0.0
    planner_used: bool = False
    subquery_count: int = 0
    subquery_labels: list[str] = Field(default_factory=list)
    episode_boost_applied: bool = False
    activation_boost_applied: bool = False
    behavior_fidelity_gate: dict[str, Any] = Field(default_factory=dict)
    capture_delivery_state: CaptureDeliveryState = CaptureDeliveryState.UNKNOWN
    open_decision_opportunity_id: UUID | None = None
    planning_pending: bool = False
    planning_job_id: UUID | None = None
    planning_pending_hint_ms: int = 0
    task_state: TaskStateSummary = Field(default_factory=TaskStateSummary)
    task_state_revision: int = 0
    charter_active: bool = False
    charter_version: str = ""
    enforcement_tier: str | None = None
    unresolved_effects: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    # ---- P4 ----
    policy_decision: PolicyDecisionBlock | None = None
    # ---- P5 ----
    # A count, not a payload.  The proposals themselves are read through ``tce.dreams``/
    # ``GET /v1/dreams``, which run the citation validation; putting proposal text on the turn
    # response would put unvalidated quotes in front of the owner on every step.
    dream_proposals_pending: int = 0


class TakeoverGoalsDiscoverRequest(BaseModel):
    session_id: str = "default"
    include_open_discovery: bool = True


class TakeoverGoalsPrecomputeRequest(BaseModel):
    session_id: str = "default"
    include_open_discovery: bool = True
    force_recompute: bool = False


class TakeoverGoalsResponse(BaseModel):
    session_id: str
    goals: list[TakeoverGoal] = Field(default_factory=list)


class TakeoverGoalCacheInvalidateRequest(BaseModel):
    session_id: str = "default"
    objective_hash: str | None = None


class TakeoverGoalCacheStatusResponse(BaseModel):
    session_id: str
    objective_hash: str | None = None
    cache_key: str | None = None
    l1: dict[str, Any] = Field(default_factory=dict)
    l2: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime


class TakeoverGoalSelectRequest(BaseModel):
    session_id: str = "default"


class ExecutionPermitRequest(BaseModel):
    session_id: str = "default"
    action_kind: str = "edit"
    target_paths: list[str] = Field(default_factory=list)
    command_preview: str | None = None
    estimated_change_size: int = Field(default=0, ge=0)
    directive_id: UUID | None = None
    attempt: int | None = Field(default=None, ge=1)
    objective_hash: str | None = None


class ExecutionPermitResolveRequest(BaseModel):
    permit_id: UUID
    approved: bool
    confirmed_by: str | None = None


class ExecutionPermitResponse(BaseModel):
    decision: ExecutionPermitDecision
    reason: str
    permit_id: UUID
    expires_at: datetime | None = None
    required_confirmation: str | None = None


class TakeoverAutonomyStatusResponse(BaseModel):
    session_id: str
    state: TakeoverState
    active_goal: TakeoverGoal | None = None
    queue_size: int = 0
    pending_permit_count: int = 0
    pending_directive_count: int = 0
    open_notice_count: int = 0
    retry_backlog_count: int = 0
    enforcement_mode: str = "strict_takeover"
    continuity_ok: bool = True
    generated_at: datetime


class TakeoverAutonomyTickRequest(BaseModel):
    session_id: str | None = None
    include_open_discovery: bool = True
    max_sessions: int = Field(default=25, ge=1, le=500)


class TakeoverAutonomyTickResponse(BaseModel):
    sessions_scanned: int = 0
    goals_refreshed: int = 0
    notices_created: int = 0
    generated_at: datetime


class TakeoverNoticesResponse(BaseModel):
    session_id: str
    notices: list[AutonomyNotice] = Field(default_factory=list)
    generated_at: datetime


class TakeoverNoticeAckRequest(BaseModel):
    session_id: str = "default"
    select_goal: bool = False


class ExecutionClaimRequest(BaseModel):
    session_id: str = "default"
    directive_id: UUID | None = None
    action_kind: str = "execute"
    claimed_by: str | None = None


class ExecutionReportRequest(BaseModel):
    session_id: str = "default"
    directive_id: UUID
    state: DirectiveExecutionState
    result: str = "success"
    failure_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None
    step_output: dict[str, Any] | None = None
    contract_type: str | None = None
    rollback_performed: bool = False
    lease_generation: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, max_length=160)
    cancel_reason: str | None = Field(default=None, max_length=500)


class ResumePacketRequest(BaseModel):
    query: str = Field(min_length=1)
    target_owner: str | None = None
    session_id: str = "default"
    k: int = Field(default=5, ge=1, le=20)
    include_cross_user: bool = True
    source_session_id: str | None = None
    legacy_session_scope: bool = False
    current_git: dict[str, Any] = Field(default_factory=dict)


class ResumePacketAnchor(BaseModel):
    file: str
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None
    stale: bool | None = None


class ResumePacketChangeSummary(BaseModel):
    added_lines: int = Field(default=0, ge=0)
    removed_lines: int = Field(default=0, ge=0)
    intent: str = ""


class ResumePacketFileItem(BaseModel):
    path: str
    priority_score: float = 0.0
    anchors: list[ResumePacketAnchor] = Field(default_factory=list)
    change_summary: ResumePacketChangeSummary = Field(default_factory=ResumePacketChangeSummary)


class ResumePacketRetrievalMeta(BaseModel):
    latency_ms: int = 0
    candidate_count: int = 0
    alternates: list[str] = Field(default_factory=list)


class ResumePacketResponse(BaseModel):
    packet_id: UUID
    selected_record_id: UUID
    selection_reason: str = "task_overlap_then_recency"
    cross_user_scope_applied: bool = False
    cross_user_scope_owners: list[str] = Field(default_factory=list)
    task_summary: str
    decision: str
    next_step: str
    status: str
    contract_valid: bool = True
    git: dict[str, Any] = Field(default_factory=dict)
    files: list[ResumePacketFileItem] = Field(default_factory=list)
    continuation_steps: list[str] = Field(default_factory=list)
    retrieval_meta: ResumePacketRetrievalMeta = Field(default_factory=ResumePacketRetrievalMeta)
    source_session_id: str | None = None
    source_owner_id: str | None = None
    record_ts: datetime | None = None
    anchor_freshness: str = "unknown"
    freshness_reasons: list[str] = Field(default_factory=list)
    project_binding: str = "unbound"
    task_id: str | None = None
    task_state_revision: int = 0
    contract_revision: int = 0
    verification_refs: list[TaskStateVerificationRef] = Field(default_factory=list)
    unresolved_effects: list[TaskStateUnresolvedEffect] = Field(default_factory=list)
    source_event_id: UUID | None = None


class CompletionCaptureRequest(BaseModel):
    session_id: str = Field(default="default", min_length=1, max_length=160)
    completion_key: str = Field(min_length=1, max_length=240)
    source: str = Field(default="executor", min_length=1, max_length=64)
    state: str = Field(default="succeeded", pattern="^(succeeded|failed|blocked)$")
    title: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)
    decision: str = Field(min_length=1, max_length=500)
    outcome: dict[str, Any] = Field(default_factory=dict)
    git: dict[str, Any] = Field(default_factory=dict)
    anchors: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    change_summary: dict[str, Any] = Field(default_factory=dict)
    milestone_schema: str = Field(default="v1", pattern="^v1$")
    app_context: dict[str, Any] = Field(default_factory=dict)


class CompletionCaptureResponse(BaseModel):
    outbox_id: UUID
    delivery_status: str
    event_id: UUID
    handoff_record_id: UUID
    contract_valid: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    captured_at: datetime
    project_binding: str = "unbound"


class ResumeFeedbackRequest(BaseModel):
    packet_id: UUID
    phase: str = Field(default="feedback", pattern="^(file_opened|productive|completed|feedback)$")
    opened_file: str | None = Field(default=None, max_length=240)
    correct_file: bool | None = None
    correct_anchor: bool | None = None
    opened_file_rank: int | None = Field(default=None, ge=1, le=40)
    correction_required: bool | None = None
    correction_reason: str = Field(default="", max_length=500)
    archaeology_tool_calls: int | None = Field(default=None, ge=0, le=10000)
    archaeology_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    outcome_status: str | None = Field(default=None, max_length=40)
    progress_source: str = Field(default="manual", max_length=64)


class ResumeFeedbackResponse(BaseModel):
    packet_id: UUID
    recorded: bool
    phase: str = "feedback"
    feedback_at: datetime


class ContinuityPilotStatusResponse(BaseModel):
    window_days: int
    eligible_completion_count: int
    captured_completion_count: int
    handoff_capture_coverage: float
    resume_attempt_count: int
    feedback_count: int
    productive_resume_count: int = 0
    completed_resume_count: int = 0
    correct_file_rate: float | None = None
    correct_file_at_1_rate: float | None = None
    correct_file_at_3_rate: float | None = None
    correct_anchor_rate: float | None = None
    correction_rate: float | None = None
    # Deprecated compatibility fields. They measure handoff age at request time.
    median_time_to_resume_ms: float | None = None
    p95_time_to_resume_ms: float | None = None
    median_handoff_age_at_resume_ms: float | None = None
    p95_handoff_age_at_resume_ms: float | None = None
    median_time_to_first_file_ms: float | None = None
    p95_time_to_first_file_ms: float | None = None
    median_active_resume_ms: float | None = None
    p95_active_resume_ms: float | None = None
    median_completion_after_resume_ms: float | None = None
    p95_completion_after_resume_ms: float | None = None
    median_retrieval_latency_ms: float | None = None
    median_archaeology_tool_calls: float | None = None
    median_archaeology_tokens: float | None = None
    outbox_pending_count: int = 0
    outbox_dead_count: int = 0
    generated_at: datetime


class GovernanceStatusResponse(BaseModel):
    runtime: str
    runtime_profile: str
    auth_mode: str
    identity_claims_mode: str
    workspace_access_mode: str
    audit_write_mode: str
    mcp_tool_profile: str
    requested_execution_enforcement: str
    effective_execution_enforcement: str
    lifecycle_protocol_enforced: bool
    host_mutation_interception: bool
    interception_provider: str | None = None
    non_bypassable_execution: bool
    server_boundary_checks: dict[str, bool] = Field(default_factory=dict)
    server_boundary_secure: bool
    production_autonomy_ready: bool
    limitations: list[str] = Field(default_factory=list)
    charter_active: bool = False
    charter_id: str | None = None
    charter_version: str | None = None
    charter_expires_at: str | None = None
    enforcement_tier: str | None = None
    sandbox_self_test_passed: bool = False
    sandbox_self_test_at: str | None = None
    sandbox_provider: str = ""
    action_tracing_available: bool = False
    spend_enforcement: str = "unsupported"
    uid_separation: bool = False
    schema_version: str = "v1"


class ExecutionStatusResponse(BaseModel):
    session_id: str
    pending: list[DirectiveExecution] = Field(default_factory=list)
    recent: list[DirectiveExecution] = Field(default_factory=list)
    generated_at: datetime
    capture_delivery_state: CaptureDeliveryState = CaptureDeliveryState.UNKNOWN
    task_state_revision: int = 0
    next_permitted_action: TaskNextPermittedAction = TaskNextPermittedAction.NONE
    verification_state: str = "unverified"
    unresolved_effects: list[dict[str, Any]] = Field(default_factory=list)
    enforcement_tier: str | None = None


class TakeoverPreloadRequest(BaseModel):
    session_id: str = "default"
    persona_mode: str = "normal"
    activation_keywords: str | None = None
    stop_keywords: str | None = None
    task: str
    app_context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class TakeoverPreloadResponse(BaseModel):
    session_id: str
    objective_hash: str
    working_set_json: dict[str, Any] = Field(default_factory=dict)
    refreshed_at: datetime
    decision_source: TakeoverDecisionSource = TakeoverDecisionSource.DELIBERATION
    project_binding: str = "unbound"


class TakeoverFeedbackRequest(BaseModel):
    session_id: str = "default"
    turn: int = 0
    objective_hash: str | None = None
    action_kind: str = "execute"
    result: str = "success"
    latency_ms: int = 0
    details: dict[str, Any] = Field(default_factory=dict)
    clone_feedback_type: CloneFeedbackType | None = None
    correction_text: str | None = None
    observation_ids: list[UUID] | None = None
    situation_type: str | None = None
    opportunity_id: UUID | None = None


class TakeoverFeedbackResponse(BaseModel):
    session_id: str
    autonomy_score: float
    recent_outcomes_json: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime


class BehaviorEvidenceRequest(BaseModel):
    situation_type: str = Field(default="routine_task", min_length=1, max_length=80)
    situation_summary: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=500)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    available_choices: list[str] = Field(default_factory=list, max_length=20)
    selected_choice: str = Field(min_length=1, max_length=500)
    rationale: str = Field(default="", max_length=1000)
    action_taken: str = Field(default="", max_length=1000)
    outcome: str = Field(default="", max_length=1000)
    outcome_sentiment: str | None = Field(default=None, max_length=40)
    correction_text: str = Field(default="", max_length=1000)
    memory_class: BehaviorMemoryClass = BehaviorMemoryClass.DECISION
    evidence_source: BehaviorEvidenceSource = BehaviorEvidenceSource.EXPLICIT
    lifecycle_status: BehaviorLifecycleStatus = BehaviorLifecycleStatus.ACTIVE
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=40)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    # Ignored server-side: confirmation is never caller-asserted (P0 trust boundary).
    confirmed_at: datetime | None = None
    supersedes_observation_id: UUID | None = None
    contradicts_observation_ids: list[UUID] = Field(default_factory=list, max_length=40)
    schema_version: str = "v1"

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> BehaviorEvidenceRequest:
        if self.schema_version != "v1":
            raise ValueError("schema_version must be v1")
        if self.evidence_source in {
            BehaviorEvidenceSource.EXPLICIT,
            BehaviorEvidenceSource.CORRECTION,
            BehaviorEvidenceSource.CALIBRATION,
        } and not self.rationale.strip():
            raise ValueError(f"rationale is required for {self.evidence_source.value} evidence")
        if self.evidence_source == BehaviorEvidenceSource.CORRECTION:
            if not self.correction_text.strip():
                raise ValueError("correction_text is required for correction evidence")
            if self.supersedes_observation_id is None:
                raise ValueError("supersedes_observation_id is required for correction evidence")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class BehaviorEvidenceResponse(BaseModel):
    observation_id: UUID | None = None
    stored: bool
    learning_eligible: bool
    storage_score: float
    storage_decision: str
    storage_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    redaction_applied: bool = False
    superseded_observation_id: UUID | None = None
    review_id: UUID | None = None
    shadow_prediction_id: UUID | None = None
    schema_version: str = "v1"


class BehaviorProjectionResponse(BaseModel):
    projection_id: UUID
    uri: str
    view: BehaviorProjectionView
    format: BehaviorProjectionFormat
    topic: str | None = None
    observation_id: str | None = None
    mime_type: str
    schema_version: str = "v1"
    source_revision: str
    content_sha256: str
    generated_at: datetime
    source_evidence_ids: list[UUID] = Field(default_factory=list)
    trust_level: str
    sensitivity: int = Field(default=1, ge=0, le=3)
    read_only: bool = True
    projection_learning_eligible: bool = False
    expires_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)
    truncated: bool = False
    redaction_applied: bool = False
    content: str


class BehaviorPilotAssignmentRequest(BaseModel):
    trial_key: str = Field(min_length=1, max_length=160)
    situation_type: str = Field(default="routine_task", min_length=1, max_length=80)
    situation_summary: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=500)
    constraints: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidate_choices: list[str] = Field(default_factory=list, max_length=20)


class BehaviorPilotAssignmentResponse(BaseModel):
    assignment_id: UUID
    variant: BehaviorPilotVariant
    assigned_at: datetime
    expires_at: datetime
    context_payload: dict[str, Any] = Field(default_factory=dict)
    citations: list[UUID] = Field(default_factory=list)
    source_revision: str
    context_sha256: str
    injected_tokens: int = Field(default=0, ge=0)
    retrieval_latency_ms: int = Field(default=0, ge=0)
    projection_learning_eligible: bool = False
    schema_version: str = "v1"


class BehaviorPilotOutcomeRequest(BaseModel):
    assignment_id: UUID
    agent_choice: str | None = Field(default=None, max_length=500)
    top3_choices: list[str] = Field(default_factory=list, max_length=3)
    actual_choice: str = Field(min_length=1, max_length=500)
    agent_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    abstained: bool = False
    action_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    workflow_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    correction_required: bool = False
    outcome_regret: bool = False
    irrelevant_personalization: bool = False
    malicious_memory_activated: bool = False
    used_evidence_ids: list[UUID] = Field(default_factory=list, max_length=40)
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_pilot_outcome(self) -> BehaviorPilotOutcomeRequest:
        if not self.abstained and not str(self.agent_choice or "").strip():
            raise ValueError("agent_choice is required unless abstained=true")
        return self


class BehaviorPilotOutcomeResponse(BaseModel):
    assignment_id: UUID
    outcome_id: UUID
    recorded: bool
    stale_evidence_used: bool = False
    reported_at: datetime
    schema_version: str = "v1"


class BehaviorPilotArmMetrics(BaseModel):
    variant: BehaviorPilotVariant
    assignment_count: int = 0
    completed_count: int = 0
    completion_rate: float = 0.0
    top1_agreement: float | None = None
    top3_agreement: float | None = None
    non_abstained_precision: float | None = None
    calibration_brier: float | None = None
    mean_action_similarity: float | None = None
    mean_workflow_similarity: float | None = None
    stale_memory_use_rate: float | None = None
    irrelevant_personalization_rate: float | None = None
    correction_rate: float | None = None
    outcome_regret_rate: float | None = None
    malicious_activation_rate: float | None = None
    median_injected_tokens: float | None = None
    p95_injected_tokens: float | None = None
    median_retrieval_latency_ms: float | None = None
    p95_retrieval_latency_ms: float | None = None


class BehaviorPilotGate(BaseModel):
    min_window_days: int = 28
    min_completed_per_arm: int = 30
    min_completion_coverage: float = 0.80
    max_p95_retrieval_latency_ms: float = 120.0
    max_top1_degradation: float = 0.05
    elapsed_days: float = 0.0
    window_complete: bool = False
    sample_complete: bool = False
    coverage_complete: bool = False
    latency_passed: bool = False
    quality_passed: bool = False
    safety_passed: bool = True
    evaluation_ready: bool = False
    reasons: list[str] = Field(default_factory=list)


class BehaviorPilotStatusResponse(BaseModel):
    status: BehaviorPilotStatus
    started_at: datetime | None = None
    latest_outcome_at: datetime | None = None
    assignment_count: int = 0
    completed_count: int = 0
    completion_rate: float = 0.0
    arms: list[BehaviorPilotArmMetrics] = Field(default_factory=list)
    gate: BehaviorPilotGate = Field(default_factory=BehaviorPilotGate)
    generated_at: datetime
    schema_version: str = "v1"


class BehaviorPredictionRequest(BaseModel):
    situation_type: str = Field(default="routine_task", min_length=1, max_length=80)
    situation_summary: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=500)
    constraints: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidate_choices: list[str] = Field(default_factory=list, max_length=20)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)


class BehaviorPredictionResponse(BaseModel):
    predicted_choice: str | None = None
    ranked_choices: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    abstained: bool = True
    needs_clarification: bool = True
    clarification_question: str | None = None
    citations: list[UUID] = Field(default_factory=list)
    neighbor_count: int = 0
    effective_neighbor_count: float = 0.0
    out_of_distribution: bool = False
    ood_score: float = 1.0
    predicted_action: str | None = None
    fidelity_gate: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "v1"
    # ---- P4 ----
    policy_decision: PolicyDecisionBlock | None = None


class BehaviorEvaluationRequest(BaseModel):
    holdout_ratio: float = Field(default=0.20, ge=0.05, le=0.50)
    min_train: int = Field(default=5, ge=3, le=1000)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    max_cases: int = Field(default=500, ge=20, le=5000)


class BehaviorEvaluationResponse(BaseModel):
    run_id: UUID
    status: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    gate: dict[str, Any] = Field(default_factory=dict)
    case_results: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    duration_ms: int = 0
    schema_version: str = "v1"


class BehaviorEvaluationListResponse(BaseModel):
    runs: list[BehaviorEvaluationResponse] = Field(default_factory=list)


class BehaviorCalibrationAnswerRequest(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=80)
    selected_choice: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=1000)
    action_taken: str = Field(default="", max_length=1000)


class BehaviorCalibrationScenariosResponse(BaseModel):
    scenarios: list[dict[str, Any]] = Field(default_factory=list)
    schema_version: str = "v1"


class CapabilityGrantRequest(BaseModel):
    session_id: str = Field(default="default", min_length=1, max_length=160)
    directive_id: UUID | None = None
    permit_id: UUID | None = None
    capability: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=80)
    resource: str = Field(min_length=1, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=120, ge=30, le=900)


class CapabilityGrantResponse(BaseModel):
    grant_id: UUID
    token: str | None = None
    status: str
    decision: str
    reason: str
    capability: str
    action: str
    resource: str
    action_digest: str
    risk_tier: str
    mutating: bool
    one_time: bool = True
    expires_at: datetime
    schema_version: str = "v1"


class CapabilityConsumeRequest(BaseModel):
    grant_id: UUID
    token: str = Field(min_length=32, max_length=256)
    capability: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=80)
    resource: str = Field(min_length=1, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CapabilityConsumeResponse(BaseModel):
    grant_id: UUID
    authorized: bool
    status: str
    reason: str
    action_digest: str
    consumed_at: datetime | None = None
    schema_version: str = "v1"


class ProcessMiningRequest(BaseModel):
    lookback_days: int = Field(default=30, ge=1, le=365)
    min_support: int = Field(default=2, ge=2, le=100)
    max_sequences: int = Field(default=500, ge=10, le=5000)
    max_steps: int = Field(default=12, ge=2, le=30)


class ProcessModelItem(BaseModel):
    process_id: UUID
    process_signature: str
    name: str
    steps: list[str] = Field(default_factory=list)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    support: int
    success_rate: float
    reliability: float
    source_sessions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = "candidate"
    review_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: str = "v1"


class ProcessMiningResponse(BaseModel):
    models: list[ProcessModelItem] = Field(default_factory=list)
    source_event_count: int = 0
    source_session_count: int = 0
    duration_ms: int = 0
    schema_version: str = "v1"


class BehaviorShadowPredictionItem(BaseModel):
    prediction_id: UUID
    observation_id: UUID | None = None
    predicted_choice: str | None = None
    actual_choice: str
    confidence: float
    abstained: bool
    correct: bool | None = None
    evidence_count: int
    latency_ms: int
    created_at: datetime
    schema_version: str = "v1"
    prediction_stage: str = "retrospective"
    resolution_state: str = "resolved"
    opportunity_id: UUID | None = None
    decision_family: str | None = None
    frozen_at: datetime | None = None
    resolved_at: datetime | None = None


class BehaviorShadowStatusResponse(BaseModel):
    metrics: dict[str, Any] = Field(default_factory=dict)
    recent: list[BehaviorShadowPredictionItem] = Field(default_factory=list)
    schema_version: str = "v1"


class MemoryReviewItem(BaseModel):
    review_id: UUID
    target_type: str
    target_id: UUID
    title: str
    rationale: str = ""
    status: str
    proposed_action: str
    source: str
    score: float = 0.0
    reviewer_id: str | None = None
    review_note: str = ""
    created_at: datetime
    resolved_at: datetime | None = None
    schema_version: str = "v1"


class MemoryReviewListResponse(BaseModel):
    reviews: list[MemoryReviewItem] = Field(default_factory=list)
    schema_version: str = "v1"


class MemoryReviewResolveRequest(BaseModel):
    decision: str = Field(pattern="^(promote|reject)$")
    note: str = Field(default="", max_length=1000)


class CounterfactualCreateRequest(BaseModel):
    observation_id: UUID | None = None
    session_id: str = Field(default="default", min_length=1, max_length=160)
    directive_id: UUID | None = None
    decision: str = Field(min_length=1, max_length=500)
    alternative: str = Field(min_length=1, max_length=500)
    expected_outcome: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    review_at: datetime | None = None


class CounterfactualResolveRequest(BaseModel):
    assessment: str = Field(pattern="^(supported|refuted|inconclusive)$")
    observed_outcome: str = Field(min_length=1, max_length=1000)
    lesson: str = Field(default="", max_length=1000)
    regret_score: float | None = Field(default=None, ge=0.0, le=1.0)


class CounterfactualItem(BaseModel):
    counterfactual_id: UUID
    observation_id: UUID | None = None
    session_id: str
    directive_id: UUID | None = None
    decision: str
    alternative: str
    expected_outcome: str
    assumptions: list[str] = Field(default_factory=list)
    confidence: float
    status: str
    assessment: str | None = None
    observed_outcome: str = ""
    lesson: str = ""
    regret_score: float | None = None
    redaction_applied: bool = False
    review_at: datetime | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    schema_version: str = "v1"


class CounterfactualListResponse(BaseModel):
    records: list[CounterfactualItem] = Field(default_factory=list)
    schema_version: str = "v1"


class CloneAdviceRequest(BaseModel):
    task: str
    app_context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    takeover_context: dict[str, Any] = Field(default_factory=dict)
    message_delta: dict[str, Any] = Field(default_factory=dict)
    executor_output: str | None = None
    interaction_id: str | None = None
    allow_fallback: bool = True


class CloneAdviceResponse(BaseModel):
    interaction_id: str
    guidance_summary: str
    recommended_actions: list[str]
    do: list[str]
    dont: list[str]
    confidence: float = Field(
        default=0.0,
        description=(
            "Uncalibrated vote-share heuristic derived from the policy result. It is NOT a "
            "probability and nothing in this system is calibrated; do not threshold on it."
        ),
    )
    evidence_strength: str
    citations: list[UUID]
    conflict_flags: list[str]
    loop_guard: dict[str, Any]
    policy: dict[str, Any]
    clone_context: dict[str, Any] | None = None
    evidence_observations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Retrieval provenance for recall_source accounting; never the evidence for a "
            "choice. The evidence for a choice is policy_decision.evidence_observation_ids, "
            "which is bound to the option that was actually selected."
        ),
    )


class TrustedInputCapture(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    delivery_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=8000)
    origin_kind: TrustedInputOriginKind = TrustedInputOriginKind.HUMAN_INPUT
    observed_at: datetime
    original_char_count: int = Field(ge=0)
    content_truncated: bool = False
    redaction_applied: list[str] = Field(default_factory=list)
    sequence: int | None = None
    prompt_id: str | None = Field(default=None, max_length=200)
    hook_event_name: str = "UserPromptSubmit"
    host_client: str = Field(default="claude", max_length=32)
    cwd: str | None = Field(default=None, max_length=1024)
    project_hint: dict[str, Any] = Field(default_factory=dict)
    spool_depth: int = Field(default=0, ge=0)
    spool_failures: int = Field(default=0, ge=0)
    gap_since: datetime | None = None
    schema_version: str = "v1"


class TrustedInputReceipt(BaseModel):
    receipt_id: UUID
    event_id: UUID | None = None
    delivery_key: str
    content_sha256: str
    origin_kind: TrustedInputOriginKind
    capture_principal: str
    observed_at: datetime
    ingested_at: datetime
    deduplicated: bool = False
    extraction_state: str = "pending"
    queue_state: str = "inline"
    capture_delivery_state: CaptureDeliveryState = CaptureDeliveryState.UNKNOWN
    schema_version: str = "v1"


# ---------------------------------------------------------------------------------------------
# Charter, dispatch, effect journal, verification and sandbox self-test wire models.
#
# Two shapes here are deliberately missing a field, and the absence is the control:
#
#   * EffectResolveRequest has no `actor`.  The actor is derived server-side from the authenticated
#     identity, so a caller cannot claim to be the reconciler and reopen a terminal effect.
#   * VerificationResultRequest has no `verdict`, no `reason`, no digest-match flags and no
#     `runner_principal`.  Evidence goes in, the API grades it, and a self-report cannot become a
#     verification by asserting one.
# ---------------------------------------------------------------------------------------------


class CharterCapsPayload(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=20)
    max_concurrent_dispatches: int = Field(default=1, ge=1, le=8)
    max_wall_seconds: int = Field(default=1800, ge=60, le=86400)
    budget_minor_units: int = Field(default=0, ge=0)
    budget_currency: str = "USD"
    spend_enforcement: str = "unsupported"


class CharterCreateRequest(BaseModel):
    project_id: str | None = None
    enforcement_tier: str = "container"
    permitted_roots: list[str] = Field(default_factory=list)
    protected_write_prefixes: list[str] = Field(default_factory=lambda: ["tests/", ".github/", ".local/"])
    denied_read_paths: list[str] = Field(
        default_factory=lambda: ["~/.ssh", "~/Library/Keychains", "~/.aws", "~/.claude", "~/.codex"]
    )
    permitted_capabilities: list[str] = Field(default_factory=list)
    confirm_required_capabilities: list[str] = Field(default_factory=list)
    egress_mode: str = "deny_all"
    runtime_allowlist: list[list[str]] = Field(default_factory=list)
    caps: CharterCapsPayload = Field(default_factory=CharterCapsPayload)
    ttl_seconds: int | None = Field(default=None, ge=1)
    task_families: list[str] = Field(default_factory=list)
    charter_version: str = "v1"
    credential_risk_acknowledged: bool = False
    source_receipt_id: UUID
    session_id: str = "default"


class CharterResponse(BaseModel):
    charter: dict[str, Any] | None = None
    charter_id: UUID | None = None
    status: str = "draft"
    charter_digest: str = ""
    charter_version: str = "v1"
    policy_revision: str = ""
    enforcement_tier: str | None = None
    credential_risk_acknowledged: bool = False
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    superseded_by: UUID | None = None
    narrowing_ids: list[str] = Field(default_factory=list)
    generated_at: datetime
    schema_version: str = "v1"


class CharterNarrowingRequest(BaseModel):
    charter_id: UUID
    session_id: str = "default"
    source_receipt_id: UUID
    remove_roots: list[str] = Field(default_factory=list)
    remove_capabilities: list[str] = Field(default_factory=list)
    add_protected_write_prefixes: list[str] = Field(default_factory=list)
    add_denied_read_paths: list[str] = Field(default_factory=list)
    enforcement_tier: str | None = None
    egress_mode: str | None = None
    budget_minor_units: int | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=1)
    max_wall_seconds: int | None = Field(default=None, ge=1)
    max_concurrent_dispatches: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    reason: str = ""


class DispatchOpenRequest(BaseModel):
    session_id: str = "default"
    directive_id: UUID
    task_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    runtime_id: str
    runtime_version: str
    surface: str
    model_id: str = ""
    contract_digest: str = ""
    task_family: str = "unspecified"
    enforcement_tier: str
    sandbox_provider: str
    sandbox_profile_digest: str | None = None
    sandbox_self_test_id: UUID | None = None
    provider_run_id: str
    provider_turn_id: str | None = None
    request_minor_units: int | None = Field(default=None, ge=0)
    idempotency_key: str = ""


class DispatchResponse(BaseModel):
    dispatch_id: UUID
    directive_id: UUID
    attempt: int = 1
    charter_id: UUID
    charter_digest: str = ""
    enforcement_tier: str
    sandbox_provider: str = ""
    sandbox_profile_digest: str | None = None
    sandbox_self_test_id: UUID | None = None
    runtime_id: str = ""
    runtime_version: str = ""
    surface: str = ""
    model_id: str = ""
    contract_digest: str = ""
    capability_matrix: dict[str, str] = Field(default_factory=dict)
    provider_run_id: str | None = None
    provider_turn_id: str | None = None
    task_family: str = "unspecified"
    budget_reserved_minor_units: int = 0
    budget_currency: str = "USD"
    spend_enforcement: str = "unsupported"
    cap_applied: dict[str, float] | None = None
    cost_minor_units: int | None = None
    cost_source: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached_input: int = 0
    tokens_reasoning: int = 0
    human_intervention_count: int = 0
    outcome: str | None = None
    terminal_reason: str | None = None
    wall_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reconciled_at: datetime | None = None
    generated_at: datetime
    schema_version: str = "v1"


class DispatchReconcileRequest(BaseModel):
    outcome: str
    terminal_reason: str = ""
    wall_ms: int = Field(default=0, ge=0)
    human_intervention_count: int = Field(default=0, ge=0)
    reconciliation: dict[str, Any] = Field(default_factory=dict)


class EffectOpenRequest(BaseModel):
    session_id: str = "default"
    directive_id: UUID
    dispatch_id: UUID | None = None
    task_id: str | None = None
    kind: str
    capability: str
    resource: str = ""
    argv: list[str] = Field(default_factory=list)
    reversibility: str
    description: str = ""
    enforcement_tier: str
    action_tracing: str
    lease_generation: int = Field(default=0, ge=0)
    provider_run_id: str | None = None
    provider_turn_id: str | None = None
    runtime_id: str | None = None
    runtime_version: str | None = None
    model_id: str | None = None


class EffectResolveRequest(BaseModel):
    target_state: str
    resolution_source: str
    expected_lease: int = Field(ge=0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class EffectResponse(BaseModel):
    effect_id: UUID
    directive_id: UUID
    seq: int = 0
    state: str = "prepared"
    intent_digest: str = ""
    kind: str = ""
    capability: str = ""
    resource: str = ""
    reversibility: str = "unknown"
    enforcement_tier: str = ""
    action_tracing: str = ""
    lease_generation: int = 0
    claimed_executor: str | None = None
    provider_run_id: str | None = None
    opened_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution_source: str | None = None
    pause_required: bool = False
    pause_reason: str = ""
    generated_at: datetime
    schema_version: str = "v1"


class AcceptanceCheckPayload(BaseModel):
    check_id: str
    argv: list[str] = Field(min_length=1)
    cwd_rel: str = "."
    expect_exit_code: int = 0
    timeout_seconds: int = Field(default=900, ge=1, le=3600)


class AcceptanceCriteriaRequest(BaseModel):
    directive_id: UUID
    task_id: str | None = None
    charter_id: UUID | None = None
    checks: list[AcceptanceCheckPayload] = Field(min_length=1)
    corpus_manifest: list[list[str]] = Field(default_factory=list)


class AcceptanceCriteriaResponse(BaseModel):
    criteria_id: UUID
    directive_id: UUID
    criteria_digest: str
    corpus_digest: str
    checks: list[AcceptanceCheckPayload] = Field(default_factory=list)
    corpus_manifest: list[list[str]] = Field(default_factory=list)
    frozen_at: datetime
    frozen_by: str
    policy_revision: str = ""
    schema_version: str = "v1"


class CheckResultPayload(BaseModel):
    check_id: str
    argv: list[str] = Field(default_factory=list)
    exit_code: int
    duration_ms: int = Field(default=0, ge=0)
    stdout_sha256: str
    stderr_sha256: str
    excerpt: str = Field(default="", max_length=2000)


class VerificationResultRequest(BaseModel):
    directive_id: UUID
    results: list[CheckResultPayload] = Field(default_factory=list)
    observed_corpus_digest: str
    observed_corpus_manifest: list[list[str]] = Field(default_factory=list)
    platform: str = ""
    commit_sha: str | None = None
    tree_sha: str | None = None
    reviewer_model: dict[str, Any] | None = None


class VerificationResultResponse(BaseModel):
    verification_id: UUID
    directive_id: UUID
    verdict: str
    reason: str = ""
    verification_state: str = "unverified"
    criteria_digest_at_run: str = ""
    corpus_digest_at_run: str = ""
    criteria_digest_match: bool = False
    corpus_digest_match: bool = False
    runner_principal: str = ""
    executing_identity: str = ""
    platform: str = ""
    commit_sha: str | None = None
    tree_sha: str | None = None
    advisory: bool = True
    recorded_at: datetime
    schema_version: str = "v1"


class SandboxAssertionPayload(BaseModel):
    name: str
    expected: str = ""
    observed: str = ""
    passed: bool = False


class SandboxSelfTestRequest(BaseModel):
    host_id: str = ""
    sandbox_provider: str
    provider_version: str = ""
    profile_digest: str = ""
    assertions: list[SandboxAssertionPayload] = Field(default_factory=list)
    passed: bool = False
    uid_separation: bool = False


class SandboxSelfTestResponse(BaseModel):
    self_test_id: UUID
    sandbox_provider: str
    profile_digest: str = ""
    passed: bool = False
    uid_separation: bool = False
    assertions: list[SandboxAssertionPayload] = Field(default_factory=list)
    ran_at: datetime
    schema_version: str = "v1"


# ---- P4 ----
# The seven decision-policy enums are declared in ``tce_shared.decision_policy`` and imported
# at the top of this module, never re-declared here.  ``decision_policy`` has to stay
# pydantic-free so the worker can import it, and this module imports pydantic on line 8, so
# the dependency can only point one way.

class PolicyDecisionBlock(BaseModel):
    """What one turn's decision policy decided, and whether it was allowed to be used.

    ``status`` and ``exposed`` are separate on purpose.  "We abstained" and "we were not
    allowed to speak" are different facts, and collapsing them into one wire value is what
    turned an unqualified family into a human escalation in an earlier draft.

    There is no score on this block.  ``policy_score`` is uncalibrated and an executor reading
    a number it cannot interpret is how four different fields came to be named some form of
    "confidence"; and there is no calibrated sibling to add, because nothing in this system is
    calibrated.
    """

    status: DecisionStatus
    selected_option: str | None = None
    abstain_reason: AbstainReason | None = None
    reason_for_asking: str | None = None
    ood_status: OodStatus = OodStatus.UNKNOWN
    conflict_status: ConflictStatus = ConflictStatus.NONE
    evidence_observation_ids: list[str] = Field(default_factory=list, max_length=12)
    decision_policy_revision: str = ""
    exposed: bool = False
    exposure_state: ExposureState = ExposureState.NO_QUALIFICATION
    advisor_agreement: AdvisorAgreement = AdvisorAgreement.ABSENT


# ``PolicyDecisionBlock`` is declared after the two responses that carry it, because this file
# is append-only across phases and re-ordering it would rewrite another phase's block.  The
# forward reference therefore has to be resolved explicitly, or the field stays an unbuilt
# ForwardRef and the OpenAPI document silently loses the schema.
TakeoverStepResponse.model_rebuild()
BehaviorPredictionResponse.model_rebuild()


# ---- P5 ----
# Wire models for the four dream-proposal routes.  They live here, once, so that Full and Lite
# import one definition and OpenAPI parity is mechanical rather than careful.
#
# There is no status enum declared in this block: ``DreamProposalStatus`` and
# ``NonresponseState`` are imported from ``tce_shared.aspirations`` at the top of the module.


class DreamProposalCitation(BaseModel):
    """One quoted message behind a proposal.

    ``receipt_id`` is required, not optional.  A message reaches a proposal only through a
    ``trusted_input_receipts`` row binding it to the subject, so a citation without one could
    not have been built — and a nullable field here would invite a future writer to build one.
    The quote is a verbatim span of the cited message, checked against the decrypted body
    before the proposal is written and again before it is shown.
    """

    event_id: UUID
    receipt_id: UUID
    origin_kind: str
    observed_at: datetime
    quote: str


class DreamProposal(BaseModel):
    """A proposal the owner can answer, and the evidence he can check it against.

    Two fields carry the honesty of the whole surface.  ``citations_verified`` says whether the
    citations behind this response were checked cheaply (existence, scope, sensitivity, receipt
    binding) or deeply (the body decrypted and the quote re-found in it), so a reader is never
    left guessing how much the system just proved.  ``attribution`` says whether these are words
    the owner is receipted as having typed or lines from imported history — ``"you said"``
    versus ``"from your imported history"`` — because a quote whose provenance is unclear is
    worth less than no quote at all.

    ``nonresponse`` is a separate axis from ``status`` and never becomes a verdict.  A proposal
    nobody ever surfaced reads ``never_surfaced`` however old it is.
    """

    id: UUID
    workspace_id: str
    project_id: str | None = None
    scope_kind: str
    status: DreamProposalStatus
    nonresponse: NonresponseState
    surfaced_count: int = 0
    surfaced_attested: bool = False
    title: str
    connection: str = ""
    benefit: str = ""
    first_step: str = ""
    citations: list[DreamProposalCitation] = Field(default_factory=list)
    citation_count: int = 0
    citations_verified: str = "cheap"
    evidence_basis: str
    evidence_revision: str = ""
    attribution: str = ""
    supersedes_proposal_id: UUID | None = None
    snooze_until: datetime | None = None
    expires_at: datetime | None = None
    task_id: str | None = None
    plan_root_goal_id: UUID | None = None
    pursuit_started_at: datetime | None = None
    completed_at: datetime | None = None
    rejection_reason: str = ""
    revision: int = 0
    created_at: datetime
    updated_at: datetime
    schema_version: str = "v1"


class DreamProposalListResponse(BaseModel):
    proposals: list[DreamProposal] = Field(default_factory=list)
    total: int = 0
    citations_verified: str = "cheap"
    schema_version: str = "v1"


class DreamProposalTransitionRequest(BaseModel):
    """One route, one closed vocabulary.

    The verb is in the body rather than the path so the vocabulary is closed on the wire: a
    reader of the schema can see the five legal actions, and a sixth cannot arrive by someone
    adding a route.  ``surfaced`` is a display record, not a verdict, and is the only action an
    executor may post.
    """

    session_id: str = "default"
    action: str
    reason: str = ""
    snooze_until: datetime | None = None


class DreamRefreshRequest(BaseModel):
    """``app_context`` is a project *hint*, resolved through the authenticated scope.

    It is never an identity.  A project id is client-assertable, so the hint is entitlement-
    checked against the subject's own receipts before a run is allowed to bind to it.  There is
    no ``scope_kind`` field: the scope kind is derived from the resolved scope, and a body that
    could assert it would be a second source of truth for the one thing that decides which
    corpus a run reads.
    """

    session_id: str = "default"
    app_context: dict[str, Any] | None = None


class DreamRefreshResponse(BaseModel):
    """What the refresh did, including — especially including — refusing to do anything.

    A generation path that is entirely broken must not read as "no proposals today".  ``state``
    and ``refusal_reason`` are always populated, and ``refusals``/``pool_drops`` carry the
    per-reason counts, so an over-refusing system is visible in the response rather than looking
    like a quiet week.
    """

    run_id: UUID | None = None
    state: str = "refused"
    refusal_reason: str = ""
    proposals_written: int = 0
    refusals: dict[str, int] = Field(default_factory=dict)
    pool_size: int = 0
    pool_drops: dict[str, int] = Field(default_factory=dict)
    swept: dict[str, int] = Field(default_factory=dict)
    reconciled: dict[str, int] = Field(default_factory=dict)
    schema_version: str = "v1"
