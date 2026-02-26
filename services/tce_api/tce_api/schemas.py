from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from tce_shared.events import (
    CloneAdviceRequest,
    CloneAdviceResponse,
    EventEnvelope,
    EventSearchRequest,
    EventSearchResponse,
    OperationMode,
    PatternFeedbackRequest,
    RuntimeModeConfig,
    RuntimeModeUpdate,
)


class IngestResponse(BaseModel):
    event_id: UUID


class BatchIngestRequest(BaseModel):
    events: list[EventEnvelope] = Field(min_length=1)


class BatchIngestResponse(BaseModel):
    event_ids: list[UUID]


class PatternItem(BaseModel):
    id: UUID
    domain: str
    pattern_type: str
    statement: str
    confidence: float
    status: str
    evidence_event_ids: list[UUID]


class ContextBundleRequest(BaseModel):
    task: str
    app_context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class EvidenceEvent(BaseModel):
    id: UUID
    title: str
    ts: datetime
    key_payload_fields: dict[str, Any]


class ContextBundleResponse(BaseModel):
    summary: str
    top_patterns: list[PatternItem]
    relevant_workflows: list[dict[str, Any]]
    evidence_events: list[EvidenceEvent]
    do_dont: dict[str, list[str]]
    citations: list[UUID]
    policy: dict[str, Any]
    structured_context: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


class ActivitySummaryResponse(BaseModel):
    period: str
    start_ts: datetime
    end_ts: datetime
    total_events: int
    by_domain: dict[str, int]
    by_task_type: dict[str, int]
    by_event_type: dict[str, int]
    highlights: list[str]
    summary: str
    citations: list[UUID]
    policy: dict[str, Any]
    hourly_buckets: dict[str, int] = {}
    outcome_metrics: dict[str, int] = {}
    top_errors: list[str] = []
    top_entities: list[dict[str, Any]] = []
    contradiction_count: int = 0


class HealthResponse(BaseModel):
    status: str


class RuntimeModeResponse(BaseModel):
    mode: OperationMode
    clone_enabled: bool
    updated_at: datetime
    updated_by: str


class RuntimeModeSetRequest(BaseModel):
    mode: OperationMode


class CloneArbitrationRequest(BaseModel):
    interaction_id: str
    executor_plan: str
    advisor_input: str
    human_override: str | None = None


class CloneArbitrationResponse(BaseModel):
    interaction_id: str
    final_guidance: str
    decision_source: str
    citations: list[UUID]
    conflict_resolved: bool


class TeamMembershipUpsertRequest(BaseModel):
    user_id: str
    role: str = "member"
    active: bool = True


class GraphEntitySearchResponse(BaseModel):
    query: str
    workspace_id: str
    owner_id: str
    entities: list[dict[str, Any]]


class GraphEventResponse(BaseModel):
    event_id: UUID
    workspace_id: str
    owner_id: str
    graph: dict[str, Any]


class IngestObservationsRequest(BaseModel):
    observations: list[dict[str, Any]]
    update_fingerprint: bool = True


class IngestObservationsResponse(BaseModel):
    ingested: int
    fingerprint_updated: bool


# ---------------------------------------------------------------------------
# Dashboard endpoints
# ---------------------------------------------------------------------------


class FingerprintResponse(BaseModel):
    fingerprint: dict[str, Any]
    observation_count: int
    last_updated_at: datetime | None = None
    is_default: bool


class ObservationItem(BaseModel):
    id: UUID
    ts: datetime
    situation_type: str
    situation_summary: str
    user_response: str
    response_reasoning: str | None = None
    outcome: str | None = None
    outcome_sentiment: str | None = None
    confidence: float
    source_event_ids: list[UUID] = []


class ObservationListResponse(BaseModel):
    observations: list[ObservationItem]
    total: int
    situation_type_counts: dict[str, int]


class CloneScoreBreakdown(BaseModel):
    observation_count_score: int
    fingerprint_confidence: int
    pattern_coverage: int
    recent_consistency: int


class CloneScoreResponse(BaseModel):
    score: int
    breakdown: CloneScoreBreakdown
    observation_count: int
    situation_types_covered: int
    total_situation_types: int


class ApiStatusInfo(BaseModel):
    status: str
    uptime_seconds: int


class DatabaseStatusInfo(BaseModel):
    connected: bool
    event_count: int = 0
    observation_count: int = 0
    pattern_count: int = 0
    embedding_count: int = 0


class ServiceStatusInfo(BaseModel):
    connected: bool
    models: list[str] = []


class RuntimeModeInfo(BaseModel):
    mode: str
    clone_enabled: bool


class SystemStatusResponse(BaseModel):
    api: ApiStatusInfo
    database: DatabaseStatusInfo
    redis: ServiceStatusInfo
    ollama: ServiceStatusInfo
    runtime_mode: RuntimeModeInfo


class DashboardIdentityInfo(BaseModel):
    user_id: str
    label: str


class DashboardClientConfigResponse(BaseModel):
    api_base: str = "/v1"
    default_workspace_id: str
    default_user_id: str
    default_consumer_id: str
    default_session_id: str
    executor_clients: list[str] = Field(default_factory=list)
    runtime_mode: RuntimeModeInfo
    known_identities: dict[str, DashboardIdentityInfo]
    generated_at: datetime


class DashboardAgentRole(BaseModel):
    user_id: str
    consumer_id: str
    label: str
    status: str
    last_seen_ts: datetime | None = None
    source: str


class DashboardAgentRolesResponse(BaseModel):
    workspace_id: str
    runtime_mode: RuntimeModeInfo
    executor: DashboardAgentRole
    advisor: DashboardAgentRole
    secondary_executor: DashboardAgentRole | None = None
    generated_at: datetime


class GoalEmotionValue(BaseModel):
    name: str
    value: float


class GoalRelationEdge(BaseModel):
    source_goal_id: str
    target_id: str
    target_type: str
    relation_type: str
    weight: float
    meta: dict[str, Any] = Field(default_factory=dict)


class DashboardGoalIntelligenceItem(BaseModel):
    id: str
    title: str
    description: str
    source: str
    status: str
    selection_score: float
    priority_score: float
    confidence: float
    age_days: int
    long_term: bool
    long_term_reason: str
    top_emotions: list[GoalEmotionValue] = Field(default_factory=list)
    affective_scores: dict[str, Any] = Field(default_factory=dict)
    relation_count: int
    relations: list[GoalRelationEdge] = Field(default_factory=list)


class DashboardGoalsIntelligenceSummary(BaseModel):
    total_goals: int
    long_term_goals: int
    goal_event_edges: int
    goal_emotion_edges: int
    goal_goal_edges: int


class DashboardGoalsIntelligenceResponse(BaseModel):
    session_id: str
    generated_at: datetime
    summary: DashboardGoalsIntelligenceSummary
    goals: list[DashboardGoalIntelligenceItem] = Field(default_factory=list)


class DashboardHumanScoreSubscores(BaseModel):
    clone_readiness: float
    execution_quality: float
    goal_coherence: float
    affective_alignment: float


class DashboardHumanScoreResponse(BaseModel):
    session_id: str
    score: int
    band: str
    subscores: DashboardHumanScoreSubscores
    inputs: dict[str, Any] = Field(default_factory=dict)
    computed_at: datetime
    snapshot_id: str | None = None


class DashboardHumanScoreHistoryPoint(BaseModel):
    ts: datetime
    score: int
    band: str


class DashboardHumanScoreHistoryResponse(BaseModel):
    session_id: str
    days: int
    points: list[DashboardHumanScoreHistoryPoint] = Field(default_factory=list)


class DashboardHumanScoreRecomputeRequest(BaseModel):
    session_id: str = "default"


class EpisodeDecisionItem(BaseModel):
    decision: str
    why: str = ""
    alternatives: list[str] = Field(default_factory=list)


class EpisodeLessonItem(BaseModel):
    do_more: list[str] = Field(default_factory=list)
    do_less: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class EpisodeItem(BaseModel):
    id: UUID
    workspace_id: str
    user_id: str
    session_id: str
    goal: str
    context: str = ""
    outcome: str = ""
    confidence: float = 0.0
    status: str = "open"
    authority_score: float = 0.0
    stability_score: float = 0.0
    decisions: list[EpisodeDecisionItem] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    lessons: EpisodeLessonItem = Field(default_factory=EpisodeLessonItem)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    source_event_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EpisodeListResponse(BaseModel):
    episodes: list[EpisodeItem] = Field(default_factory=list)
    total: int = 0


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


class ContextBriefSectionItem(BaseModel):
    text: str
    citations: list[UUID] = Field(default_factory=list)


class ContextBriefResponse(BaseModel):
    summary: str
    standard_approach: list[ContextBriefSectionItem] = Field(default_factory=list)
    current_state: list[ContextBriefSectionItem] = Field(default_factory=list)
    constraints_preferences: list[ContextBriefSectionItem] = Field(default_factory=list)
    open_loops: list[ContextBriefSectionItem] = Field(default_factory=list)
    artifacts: list[ContextBriefSectionItem] = Field(default_factory=list)
    citations: list[UUID] = Field(default_factory=list)
    generated_at: datetime


class MemoryRuleItem(BaseModel):
    id: UUID
    workspace_id: str
    user_id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    rule_type: str
    statement: str
    priority: int = Field(default=2, ge=0, le=3)
    active: bool = True
    source_episode_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class MemoryRuleUpsertRequest(BaseModel):
    scope: dict[str, Any] = Field(default_factory=dict)
    rule_type: str
    statement: str
    priority: int = Field(default=2, ge=0, le=3)
    source_episode_id: UUID | None = None


class MemoryRuleListResponse(BaseModel):
    rules: list[MemoryRuleItem] = Field(default_factory=list)
    total: int = 0


class MemoryRuleDeprecateResponse(BaseModel):
    rule_id: UUID
    active: bool = False
    updated: bool = True


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
    style_alignment: float
    constraint_compliance: float
    decision_traceability: float
    followup_reduction: float
    started_at: datetime
    completed_at: datetime


class RetrievalEvalStatusResponse(BaseModel):
    session_id: str
    latest: RetrievalEvalRunResponse | None = None
    history: list[RetrievalEvalRunResponse] = Field(default_factory=list)


class AdvisorProviderItem(BaseModel):
    provider_id: str
    label: str
    provider_category: str
    protocol: str
    connection_type: str = "web"
    platform_hint: str | None = None
    required_in_baseline: bool = True
    default_base_url: str | None = None
    api_version: str | None = None
    region_hint: str | None = None
    default_models: list[str] = Field(default_factory=list)
    supports_model_listing: bool = True
    key_optional: bool = False


class AdvisorRouteItem(BaseModel):
    provider_id: str
    model: str | None = None
    api_key_ref: str | None = None
    base_url: str | None = None
    api_version: str | None = None
    region_hint: str | None = None
    priority: int = 0
    provider_category: str | None = None


class AdvisorProfileItem(BaseModel):
    profile_id: str = "default"
    scope: str = "workspace"
    routing_mode: str = "adaptive"
    failure_policy: str = "risk_aware_fail_safe"
    routes: list[AdvisorRouteItem] = Field(default_factory=list)


class AdvisorConfigResponse(BaseModel):
    advisor_primary_provider: str
    advisor_primary_model: str | None = None
    advisor_fallback_chain: list[str] = Field(default_factory=list)
    advisor_custom_base_url: str | None = None
    advisor_custom_model: str | None = None
    advisor_custom_api_key_ref: str | None = None
    advisor_provider_timeout_ms: int = 6000
    advisor_provider_retry_max: int = 2
    custom_headers: dict[str, str] = Field(default_factory=dict)
    key_storage_backend: str | None = None
    key_present: bool = False
    profile_id: str = "default"
    active_profile_id: str = "default"
    routing_mode: str = "adaptive"
    failure_policy: str = "risk_aware_fail_safe"
    routes: list[AdvisorRouteItem] = Field(default_factory=list)
    profiles: list[AdvisorProfileItem] = Field(default_factory=list)
    updated_at: datetime


class AdvisorProvidersResponse(BaseModel):
    providers: list[AdvisorProviderItem] = Field(default_factory=list)
    config: AdvisorConfigResponse
    generated_at: datetime


class AdvisorModelsResponse(BaseModel):
    provider_id: str
    models: list[str] = Field(default_factory=list)
    source: str = "static"
    message: str | None = None
    requires_api_key: bool = False
    connection_type: str = "web"


class AdvisorLiveModelsRequest(BaseModel):
    provider_id: str
    base_url: str | None = None
    api_key: str | None = None
    api_key_ref: str | None = None
    timeout_ms: int | None = None


class AdvisorRouteVerifyRequest(BaseModel):
    provider_id: str
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_ref: str | None = None
    api_version: str | None = None
    timeout_ms: int | None = None
    custom_headers: dict[str, str] = Field(default_factory=dict)


class AdvisorLocalOllamaPullRequest(BaseModel):
    model: str
    base_url: str | None = None


class AdvisorLocalOllamaPullResponse(BaseModel):
    job_id: str
    accepted: bool = True
    message: str


class AdvisorLocalOllamaPullStatusResponse(BaseModel):
    job_id: str
    state: str
    progress: float = 0.0
    error: str | None = None
    completed_at: datetime | None = None


class DashboardStackRestartRequest(BaseModel):
    stack: str | None = None


class DashboardStackRestartResponse(BaseModel):
    accepted: bool = True
    restart_id: str
    message: str


class DashboardStackRestartStatusResponse(BaseModel):
    restart_id: str
    state: str
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class AdvisorVerifyRequest(BaseModel):
    provider_id: str
    model: str | None = None
    provider_category: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_version: str | None = None
    region_hint: str | None = None
    api_key: str | None = None
    api_key_ref: str | None = None
    fallback_chain: list[str] = Field(default_factory=list)
    custom_headers: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int | None = None
    retry_max: int | None = None


class AdvisorVerifyAttempt(BaseModel):
    provider_id: str
    ok: bool
    code: str
    message: str
    latency_ms: int
    models: list[str] = Field(default_factory=list)


class AdvisorVerifyResponse(BaseModel):
    ok: bool
    selected_provider: str
    selected_model: str | None = None
    used_fallback: bool = False
    attempts: list[AdvisorVerifyAttempt] = Field(default_factory=list)


class AdvisorConfigUpdateRequest(BaseModel):
    advisor_primary_provider: str
    advisor_primary_model: str | None = None
    advisor_fallback_chain: list[str] = Field(default_factory=list)
    advisor_custom_base_url: str | None = None
    advisor_custom_model: str | None = None
    advisor_custom_api_key_ref: str | None = None
    advisor_provider_timeout_ms: int = 6000
    advisor_provider_retry_max: int = 2
    custom_headers: dict[str, str] = Field(default_factory=dict)
    api_key: str | None = None
    api_version: str | None = None
    profile_id: str | None = None
    routing_mode: str = "adaptive"
    failure_policy: str = "risk_aware_fail_safe"
    routes: list[AdvisorRouteItem] = Field(default_factory=list)


class AdvisorSwitchRequest(BaseModel):
    profile_id: str
    dry_run: bool = False


class AdvisorSwitchResponse(BaseModel):
    ok: bool
    active_profile_id: str
    message: str | None = None
    switched_at: datetime


class AdvisorRuntimeStatusRoute(BaseModel):
    provider_id: str
    model: str | None = None
    provider_category: str | None = None
    priority: int = 0
    score: float = 0.0
    circuit_state: str = "closed"
    consecutive_failures: int = 0
    success_ewma: float = 0.0
    latency_ewma_ms: float = 0.0
    last_error: str | None = None
    last_updated: str | None = None


class AdvisorRuntimeStatusResponse(BaseModel):
    active_profile_id: str
    routing_mode: str = "adaptive"
    failure_policy: str = "risk_aware_fail_safe"
    category_coverage: list[str] = Field(default_factory=list)
    advisor_total_budget_ms: int = 2200
    advisor_attempt_timeout_ms: int = 900
    routes: list[AdvisorRuntimeStatusRoute] = Field(default_factory=list)
    generated_at: datetime


class AdvisorRuntimeProbeRequest(BaseModel):
    profile_id: str | None = None
    max_probes: int = 3
    timeout_ms: int | None = None


class AdvisorRuntimeProbeResponse(BaseModel):
    ok: bool
    profile_id: str
    attempts: list[AdvisorVerifyAttempt] = Field(default_factory=list)
    generated_at: datetime


__all__ = [
    "BatchIngestRequest",
    "BatchIngestResponse",
    "CloneAdviceRequest",
    "CloneAdviceResponse",
    "CloneArbitrationRequest",
    "CloneArbitrationResponse",
    "CloneScoreResponse",
    "ContextBundleRequest",
    "ContextBundleResponse",
    "ActivitySummaryResponse",
    "EventEnvelope",
    "EventSearchRequest",
    "EventSearchResponse",
    "FingerprintResponse",
    "GraphEntitySearchResponse",
    "GraphEventResponse",
    "HealthResponse",
    "IngestObservationsRequest",
    "IngestObservationsResponse",
    "IngestResponse",
    "ObservationListResponse",
    "PatternFeedbackRequest",
    "PatternItem",
    "RuntimeModeConfig",
    "RuntimeModeResponse",
    "RuntimeModeSetRequest",
    "SystemStatusResponse",
    "TeamMembershipUpsertRequest",
    "RuntimeModeUpdate",
    "DashboardIdentityInfo",
    "DashboardClientConfigResponse",
    "DashboardAgentRole",
    "DashboardAgentRolesResponse",
    "GoalEmotionValue",
    "GoalRelationEdge",
    "DashboardGoalIntelligenceItem",
    "DashboardGoalsIntelligenceSummary",
    "DashboardGoalsIntelligenceResponse",
    "DashboardHumanScoreSubscores",
    "DashboardHumanScoreResponse",
    "DashboardHumanScoreHistoryPoint",
    "DashboardHumanScoreHistoryResponse",
    "DashboardHumanScoreRecomputeRequest",
    "EpisodeDecisionItem",
    "EpisodeLessonItem",
    "EpisodeItem",
    "EpisodeListResponse",
    "EventAnnotationRequest",
    "EventAnnotationResponse",
    "ContextBriefRequest",
    "ContextBriefSectionItem",
    "ContextBriefResponse",
    "MemoryRuleItem",
    "MemoryRuleUpsertRequest",
    "MemoryRuleListResponse",
    "MemoryRuleDeprecateResponse",
    "MemoryForgetRequest",
    "MemoryForgetResponse",
    "RetrievalEvalRunRequest",
    "RetrievalEvalRunResponse",
    "RetrievalEvalStatusResponse",
    "AdvisorProviderItem",
    "AdvisorProvidersResponse",
    "AdvisorRouteItem",
    "AdvisorProfileItem",
    "AdvisorModelsResponse",
    "AdvisorVerifyRequest",
    "AdvisorVerifyAttempt",
    "AdvisorVerifyResponse",
    "AdvisorConfigUpdateRequest",
    "AdvisorConfigResponse",
    "AdvisorSwitchRequest",
    "AdvisorSwitchResponse",
    "AdvisorRuntimeStatusRoute",
    "AdvisorRuntimeStatusResponse",
    "AdvisorRuntimeProbeRequest",
    "AdvisorRuntimeProbeResponse",
]
