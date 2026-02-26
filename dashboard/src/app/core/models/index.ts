export interface Fingerprint {
  decision_making: {
    risk_tolerance: string;
    speed_vs_thoroughness: number;
    delegation_tendency: number;
    conflict_resolution_style: string;
    decision_reversal_frequency: number;
    information_needs_before_deciding: string;
  };
  communication: {
    verbosity: string;
    formality: string;
    emoji_usage: boolean;
    preferred_response_length: string;
    explanation_depth: string;
    tone_under_pressure: string;
  };
  priorities: {
    speed_vs_quality: number;
    user_experience_vs_technical: number;
    pragmatic_vs_principled: number;
    top_recurring_concerns: string[];
  };
  context_switching: {
    multitask_tolerance: string;
    interruption_handling: string;
    context_retention_depth: string;
  };
  learning_style: {
    exploration_vs_exploitation: number;
    feedback_response: string;
    mistake_handling: string;
  };
  emotional_patterns: {
    frustration_triggers: string[];
    satisfaction_signals: string[];
    stress_indicators: string[];
  };
}

export interface FingerprintResponse {
  fingerprint: Fingerprint;
  observation_count: number;
  last_updated_at: string | null;
  is_default: boolean;
}

export interface ObservationItem {
  id: string;
  ts: string;
  situation_type: string;
  situation_summary: string;
  user_response: string;
  response_reasoning: string | null;
  outcome: string | null;
  outcome_sentiment: string | null;
  confidence: number;
  source_event_ids: string[];
}

export interface ObservationListResponse {
  observations: ObservationItem[];
  total: number;
  situation_type_counts: Record<string, number>;
}

export interface CloneScoreBreakdown {
  observation_count_score: number;
  fingerprint_confidence: number;
  pattern_coverage: number;
  recent_consistency: number;
}

export interface CloneScoreResponse {
  score: number;
  breakdown: CloneScoreBreakdown;
  observation_count: number;
  situation_types_covered: number;
  total_situation_types: number;
}

export interface ApiStatusInfo {
  status: string;
  uptime_seconds: number;
}

export interface DatabaseStatusInfo {
  connected: boolean;
  event_count: number;
  observation_count: number;
  pattern_count: number;
  embedding_count: number;
}

export interface ServiceStatusInfo {
  connected: boolean;
  models: string[];
}

export interface RuntimeModeInfo {
  mode: string;
  clone_enabled: boolean;
}

export interface SystemStatusResponse {
  api: ApiStatusInfo;
  database: DatabaseStatusInfo;
  redis: ServiceStatusInfo;
  ollama: ServiceStatusInfo;
  runtime_mode: RuntimeModeInfo;
}

export interface EventSearchRequest {
  query: string;
  match_all?: boolean;
  domain?: string;
  task_type?: string;
  event_type?: string;
  time_range?: { start: string; end: string };
  sensitivity_max?: number;
  limit?: number;
  offset?: number;
}

export interface EventItem {
  id: string;
  ts: string;
  actor: string;
  source: string;
  domain: string;
  task_type: string;
  event_type: string;
  title: string;
  payload: Record<string, any>;
  context: Record<string, any>;
  tags: string[];
  sensitivity: number;
}

export interface EventSearchResponse {
  events: EventItem[];
  total: number;
  citations: string[];
  policy: {
    blocked_count?: number;
    block_sensitivity?: number;
    role?: string;
    [key: string]: unknown;
  };
}

export interface ActivitySummaryResponse {
  period: string;
  start_ts: string;
  end_ts: string;
  total_events: number;
  by_domain: Record<string, number>;
  by_task_type: Record<string, number>;
  by_event_type: Record<string, number>;
  highlights: string[];
  summary: string;
  citations: string[];
  policy: Record<string, any>;
  hourly_buckets: Record<string, number>;
  outcome_metrics: Record<string, number>;
  top_errors: string[];
  top_entities: Record<string, any>[];
  contradiction_count: number;
}

export interface TakeoverContext {
  objective?: string;
  origin_message?: string;
  activated_at?: string;
  [key: string]: unknown;
}

export interface TakeoverState {
  session_id: string;
  workspace_id?: string;
  user_id?: string;
  active: boolean;
  mode: string;
  persona_mode: string;
  autonomy_policy_profile?: string;
  active_goal_id?: string | null;
  goal_queue_size?: number;
  last_discovery_at?: string | null;
  continuity_violation_count?: number;
  enforcement_mode?: string;
  last_tick_at?: string | null;
  pending_directive_count?: number;
  retry_backlog_count?: number;
  activation_keywords?: string;
  stop_keywords?: string;
  activated_at: string | null;
  expires_at: string | null;
  last_message_at: string | null;
  takeover_context: TakeoverContext | null;
  last_classification: string | null;
  last_safety_decision: string;
  updated_at?: string;
}

export interface TakeoverGoal {
  id: string;
  session_id: string;
  workspace_id: string;
  user_id: string;
  title: string;
  description: string;
  source: string;
  priority_score: number;
  risk_tier: string;
  confidence: number;
  reasoning: string;
  evidence_event_ids: string[];
  goal_kind?: 'normal' | 'unknown' | 'nothing' | string;
  affective_scores?: Record<string, unknown>;
  selection_score?: number;
  cache_hit?: boolean;
  cache_source?: 'l1' | 'l2' | 'computed' | string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface TakeoverGoalsResponse {
  session_id: string;
  goals: TakeoverGoal[];
}

export interface ExecutionPermitRequest {
  session_id: string;
  action_kind: string;
  target_paths: string[];
  command_preview?: string | null;
  estimated_change_size: number;
}

export interface ExecutionPermitResponse {
  decision: 'allow' | 'confirm_required' | 'blocked' | string;
  reason: string;
  permit_id: string;
  expires_at: string | null;
  required_confirmation: string | null;
}

export interface TakeoverAutonomyStatusResponse {
  session_id: string;
  state: TakeoverState;
  active_goal: TakeoverGoal | null;
  queue_size: number;
  pending_permit_count: number;
  pending_directive_count: number;
  open_notice_count: number;
  retry_backlog_count: number;
  enforcement_mode: string;
  continuity_ok: boolean;
  generated_at: string;
}

export interface TakeoverAutonomyReadinessResponse {
  session_id: string;
  gate_passed: boolean;
  score: number;
  band: 'full_product_ready' | 'pilot_ready' | 'not_ready' | string;
  status_phase?: 'warmup' | 'stabilizing' | 'ready' | string;
  metrics: {
    turn_count: number;
    terminal_directive_count: number;
    execution_success_rate: number;
    needs_human_rate: number;
    avg_decision_confidence: number;
    avg_context_quality: number;
    retry_rate: number;
    retrieval_trigger_rate: number;
    eval_floor: number;
  };
  thresholds: {
    min_turns: number;
    min_terminal_directives: number;
    min_execution_success_rate: number;
    max_needs_human_rate: number;
    min_avg_decision_confidence: number;
    min_avg_context_quality: number;
    min_eval_floor: number;
  };
  checks: Record<string, boolean>;
  check_reasons?: Record<string, string>;
  failing_checks: string[];
  eval_missing?: boolean;
  lookback_turns: number;
  generated_at: string;
}

export interface TakeoverAutonomyProjectKpisResponse {
  session_id: string;
  passed: boolean;
  band: 'project_autonomy_ready' | 'project_autonomy_blocked' | string;
  metrics: {
    project_count: number;
    completed_project_count: number;
    terminal_directive_count: number;
    needs_human_turns: number;
    project_completion_rate: number;
    manual_interventions_per_project: number;
    reopen_rate_after_completion: number;
    verification_pass_rate: number;
  };
  thresholds: {
    min_project_completion_rate: number;
    max_manual_interventions_per_project: number;
    max_reopen_rate_after_completion: number;
    min_verification_pass_rate: number;
  };
  checks: Record<string, boolean>;
  failing_checks: string[];
  generated_at: string;
}

export interface AutonomyNotice {
  id: string;
  session_id: string;
  workspace_id: string;
  user_id: string;
  goal_id: string | null;
  title: string;
  reason: string;
  priority: number;
  expires_at: string | null;
  created_at: string;
  acknowledged_at: string | null;
}

export interface DirectiveExecution {
  directive_id: string;
  session_id: string;
  workspace_id: string;
  user_id: string;
  goal_id?: string | null;
  objective_hash?: string | null;
  action_kind: string;
  attempt: number;
  state: string;
  requires_permit: boolean;
  permit_id?: string | null;
  claimed_by?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  expires_at?: string | null;
  failure_class?: string | null;
  failure_reason?: string | null;
  retry_strategy?: string | null;
  meta?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface TakeoverAutonomyTickResponse {
  sessions_scanned: number;
  goals_refreshed: number;
  notices_created: number;
  generated_at: string;
}

export interface TakeoverNoticesResponse {
  session_id: string;
  notices: AutonomyNotice[];
  generated_at: string;
}

export interface ExecutionStatusResponse {
  session_id: string;
  pending: DirectiveExecution[];
  recent: DirectiveExecution[];
  generated_at: string;
}

export interface WorkflowTemplateItem {
  id: string;
  name: string;
  domain: string;
  graph: Record<string, unknown>;
  triggers: Record<string, unknown>;
  version: number;
  updated_at: string | null;
}

export interface WorkflowTemplatesResponse {
  session_id: string;
  templates: WorkflowTemplateItem[];
  total: number;
  generated_at: string;
}

export interface TakeoverGoalCacheTierStatus {
  [key: string]: unknown;
}

export interface TakeoverGoalCacheStatusResponse {
  session_id: string;
  objective_hash: string | null;
  cache_key: string | null;
  l1: TakeoverGoalCacheTierStatus;
  l2: TakeoverGoalCacheTierStatus;
  generated_at: string;
}

export interface ContextRetrievalStatusResponse {
  mode: string;
  qdrant_enabled: boolean;
  qdrant_timeout_ms: number;
  trigger_threshold: number;
  escalate_threshold: number;
  turn_budget_ms: number;
  backend_timeout_ms: number;
  fallback_counters: Record<string, number>;
  generated_at: string;
  workspace_id?: string;
  user_id?: string;
}

export interface AdvisorProviderItem {
  provider_id: string;
  label: string;
  provider_category: 'global' | 'china' | 'custom' | string;
  protocol: 'native' | 'openai_compatible' | string;
  connection_type: 'local' | 'web' | string;
  platform_hint: string | null;
  default_base_url: string | null;
  api_version: string | null;
  region_hint: string | null;
  default_models: string[];
  supports_model_listing: boolean;
  key_optional: boolean;
}

export interface AdvisorConfigResponse {
  advisor_primary_provider: string;
  advisor_primary_model: string | null;
  advisor_fallback_chain: string[];
  advisor_custom_base_url: string | null;
  advisor_custom_model: string | null;
  advisor_custom_api_key_ref: string | null;
  advisor_provider_timeout_ms: number;
  advisor_provider_retry_max: number;
  custom_headers: Record<string, string>;
  key_storage_backend: string | null;
  key_present: boolean;
  profile_id?: string;
  active_profile_id?: string;
  routing_mode?: string;
  failure_policy?: string;
  routes?: AdvisorRouteTuple[];
  updated_at: string;
}

export interface AdvisorProvidersResponse {
  providers: AdvisorProviderItem[];
  config: AdvisorConfigResponse;
  generated_at: string;
}

export interface AdvisorModelsResponse {
  provider_id: string;
  models: string[];
  source: string;
  message: string | null;
  requires_api_key: boolean;
  connection_type: 'local' | 'web' | string;
}

export interface AdvisorRouteTuple {
  provider_id: string;
  model: string | null;
  api_key_ref: string | null;
  base_url: string | null;
  api_version: string | null;
  region_hint: string | null;
  priority: number;
}

export interface AdvisorLiveModelsRequest {
  provider_id: string;
  base_url?: string | null;
  api_key?: string | null;
  api_key_ref?: string | null;
  timeout_ms?: number;
}

export interface AdvisorRouteVerifyRequest {
  provider_id: string;
  model?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  api_key_ref?: string | null;
  timeout_ms?: number;
}

export interface AdvisorLocalOllamaPullRequest {
  model: string;
  base_url?: string | null;
}

export interface AdvisorLocalOllamaPullResponse {
  job_id: string;
  accepted: boolean;
  message: string;
}

export interface AdvisorLocalOllamaPullStatusResponse {
  job_id: string;
  state: 'queued' | 'running' | 'completed' | 'failed' | string;
  progress: number;
  error: string | null;
  completed_at: string | null;
}

export interface DashboardStackRestartRequest {
  stack?: 'full' | 'lite';
}

export interface DashboardStackRestartResponse {
  accepted: boolean;
  restart_id: string;
  message: string;
}

export interface DashboardStackRestartStatusResponse {
  restart_id: string;
  state: 'queued' | 'running' | 'completed' | 'failed' | string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface AdvisorVerifyAttempt {
  provider_id: string;
  ok: boolean;
  code: string;
  message: string;
  latency_ms: number;
  models: string[];
}

export interface AdvisorVerifyResponse {
  ok: boolean;
  selected_provider: string;
  selected_model: string | null;
  used_fallback: boolean;
  attempts: AdvisorVerifyAttempt[];
}

export interface EpisodeDecisionItem {
  decision: string;
  why: string;
  alternatives: string[];
}

export interface EpisodeLessonItem {
  do_more: string[];
  do_less: string[];
  avoid: string[];
}

export interface EpisodeItem {
  id: string;
  workspace_id: string;
  user_id: string;
  session_id: string;
  goal: string;
  context: string;
  outcome: string;
  confidence: number;
  status: string;
  authority_score: number;
  stability_score: number;
  decisions: EpisodeDecisionItem[];
  constraints: string[];
  lessons: EpisodeLessonItem;
  artifacts: Record<string, unknown>[];
  entities: Record<string, unknown>[];
  source_event_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface EpisodeListResponse {
  episodes: EpisodeItem[];
  total: number;
}

export interface EventAnnotationRequest {
  event_id: string;
  session_id?: string;
  goal?: string | null;
  decision?: string | null;
  alternatives?: string[];
  constraints?: string[];
  avoid?: string[];
  authority_level?: string | null;
}

export interface EventAnnotationResponse {
  event_id: string;
  episode_id: string | null;
  authority_level: string;
  updated: boolean;
}

export interface ContextBriefSectionItem {
  text: string;
  citations: string[];
}

export interface ContextBriefResponse {
  summary: string;
  standard_approach: ContextBriefSectionItem[];
  current_state: ContextBriefSectionItem[];
  constraints_preferences: ContextBriefSectionItem[];
  open_loops: ContextBriefSectionItem[];
  artifacts: ContextBriefSectionItem[];
  citations: string[];
  generated_at: string;
}

export interface MemoryRuleItem {
  id: string;
  workspace_id: string;
  user_id: string;
  scope: Record<string, unknown>;
  rule_type: string;
  statement: string;
  priority: number;
  active: boolean;
  source_episode_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryRuleListResponse {
  rules: MemoryRuleItem[];
  total: number;
}

export interface MemoryRuleDeprecateResponse {
  rule_id: string;
  active: boolean;
  updated: boolean;
}

export interface MemoryForgetResponse {
  deleted_count: number;
  tombstone_id: string;
  target_type: string;
  target_ids: string[];
}

export interface RetrievalEvalRunResponse {
  run_id: string;
  session_id: string;
  style_alignment: number;
  constraint_compliance: number;
  decision_traceability: number;
  followup_reduction: number;
  started_at: string;
  completed_at: string;
}

export interface RetrievalEvalStatusResponse {
  session_id: string;
  latest: RetrievalEvalRunResponse | null;
  history: RetrievalEvalRunResponse[];
}

export interface GoalEmotionValue {
  name: string;
  value: number;
}

export interface GoalRelationEdge {
  source_goal_id: string;
  target_id: string;
  target_type: 'goal' | 'event' | 'emotion' | string;
  relation_type: 'parent' | 'similarity' | 'evidence' | 'affective' | string;
  weight: number;
  meta: Record<string, unknown>;
}

export interface DashboardGoalIntelligenceItem {
  id: string;
  title: string;
  description: string;
  source: string;
  status: string;
  selection_score: number;
  priority_score: number;
  confidence: number;
  age_days: number;
  long_term: boolean;
  long_term_reason: string;
  top_emotions: GoalEmotionValue[];
  affective_scores: Record<string, unknown>;
  relation_count: number;
  relations: GoalRelationEdge[];
}

export interface DashboardGoalsIntelligenceSummary {
  total_goals: number;
  long_term_goals: number;
  goal_event_edges: number;
  goal_emotion_edges: number;
  goal_goal_edges: number;
}

export interface DashboardGoalsIntelligenceResponse {
  session_id: string;
  generated_at: string;
  summary: DashboardGoalsIntelligenceSummary;
  goals: DashboardGoalIntelligenceItem[];
}

export interface DashboardHumanScoreSubscores {
  clone_readiness: number;
  execution_quality: number;
  goal_coherence: number;
  affective_alignment: number;
}

export interface DashboardHumanScoreResponse {
  session_id: string;
  score: number;
  band: 'emerging' | 'developing' | 'advanced' | 'human_like' | string;
  subscores: DashboardHumanScoreSubscores;
  inputs: Record<string, unknown>;
  computed_at: string;
  snapshot_id: string | null;
}

export interface DashboardHumanScoreHistoryPoint {
  ts: string;
  score: number;
  band: 'emerging' | 'developing' | 'advanced' | 'human_like' | string;
}

export interface DashboardHumanScoreHistoryResponse {
  session_id: string;
  days: number;
  points: DashboardHumanScoreHistoryPoint[];
}

export interface PatternItem {
  id: string;
  domain: string;
  pattern_type: string;
  statement: string;
  confidence: number;
  status: string;
  evidence_event_ids: string[];
}

export interface RuntimeModeConfig {
  mode: string;
  clone_enabled: boolean;
  updated_at: string;
  updated_by: string;
}

export interface DashboardIdentityInfo {
  user_id: string;
  label: string;
}

export interface DashboardClientConfig {
  api_base: string;
  default_workspace_id: string;
  default_user_id: string;
  default_consumer_id: string;
  default_session_id: string;
  executor_clients?: string[];
  runtime_mode: RuntimeModeInfo;
  known_identities: {
    executor: DashboardIdentityInfo;
    secondary_executor?: DashboardIdentityInfo;
    advisor?: DashboardIdentityInfo;
  };
  generated_at: string;
}

export interface DashboardAgentRoleInfo {
  user_id: string;
  consumer_id: string;
  label: string;
  status: 'active' | 'registered' | 'not_seen' | string;
  last_seen_ts: string | null;
  source: string;
}

export interface DashboardAgentRolesResponse {
  workspace_id: string;
  runtime_mode: RuntimeModeInfo;
  executor: DashboardAgentRoleInfo;
  advisor?: DashboardAgentRoleInfo;
  secondary_executor?: DashboardAgentRoleInfo;
  generated_at: string;
}

export interface GraphEntityItem {
  entity_id?: string;
  entity_key: string;
  display_name: string;
  entity_type?: string;
  score?: number;
  [key: string]: unknown;
}

export interface GraphEntitySearchResponse {
  query: string;
  workspace_id: string;
  owner_id: string;
  entities: GraphEntityItem[];
}

export interface GraphEventResponse {
  event_id: string;
  workspace_id: string;
  owner_id: string;
  graph: {
    entities: Record<string, unknown>[];
    relationships: Record<string, unknown>[];
    facts: Record<string, unknown>[];
    [key: string]: unknown;
  };
}

export interface TeamMembership {
  workspace_id: string;
  user_id: string;
  role: string;
  added_by: string;
  created_at: string;
  active: boolean;
}

export interface ResourceDatabaseUsage {
  engine: string;
  path?: string;
  size_bytes: number | null;
  size_pretty: string;
  available: boolean;
  error: string | null;
}

export interface ResourceDockerUsage {
  available: boolean;
  source: string;
  error: string | null;
  socket_path?: string;
  project_filters?: string[];
}

export interface ResourceServiceUsage {
  project: string;
  service: string;
  container_name: string;
  cpu_percent: number;
  memory_usage_bytes: number;
  memory_limit_bytes: number;
  memory_percent: number;
  network_rx_bytes: number;
  network_tx_bytes: number;
  block_read_bytes: number;
  block_write_bytes: number;
  pids: number;
}

export interface ResourceUsageSnapshot {
  captured_at: string;
  database: ResourceDatabaseUsage;
  docker: ResourceDockerUsage;
  services: ResourceServiceUsage[];
}
