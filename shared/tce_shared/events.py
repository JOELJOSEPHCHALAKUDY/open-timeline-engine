from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .version import SCHEMA_VERSION


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


class EventSearchHit(BaseModel):
    id: UUID
    ts: datetime
    title: str
    domain: str
    task_type: str
    score: float
    sensitivity: int


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
    retrieval_latency_ms: int = 0
    retrieval_hit_count: int = 0
    feedback_adjustment_applied: bool = False
    snapshot_rehydrated: bool = False
    snapshot_age_hours: int | None = None
    query_expansion_used: bool = False
    query_expansion_terms: list[str] = Field(default_factory=list)
    rerank_strategy: str = "none"


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


class ResumePacketRequest(BaseModel):
    query: str = Field(min_length=1)
    target_owner: str | None = None
    session_id: str = "default"
    k: int = Field(default=5, ge=1, le=20)
    include_cross_user: bool = True


class ResumePacketAnchor(BaseModel):
    file: str
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None


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


class ExecutionStatusResponse(BaseModel):
    session_id: str
    pending: list[DirectiveExecution] = Field(default_factory=list)
    recent: list[DirectiveExecution] = Field(default_factory=list)
    generated_at: datetime


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


class TakeoverFeedbackResponse(BaseModel):
    session_id: str
    autonomy_score: float
    recent_outcomes_json: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime


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
    confidence: float
    evidence_strength: str
    citations: list[UUID]
    conflict_flags: list[str]
    loop_guard: dict[str, Any]
    policy: dict[str, Any]
    clone_context: dict[str, Any] | None = None
    evidence_observations: list[dict[str, Any]] = Field(default_factory=list)
