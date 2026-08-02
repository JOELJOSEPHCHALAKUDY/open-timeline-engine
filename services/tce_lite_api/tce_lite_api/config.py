from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TCE_", extra="ignore")

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    lite_db_path: str = "/data/tce-lite.db"
    auth_mode: str = "bearer"
    api_tokens: str = "local-dev-token"
    block_sensitivity: int = 3
    default_operation_mode: str = "timeline_only"
    clone_max_turns_per_interaction: int = 8
    takeover_clone_max_turns_per_interaction: int = 80
    clone_fallback_to_timeline: bool = True
    behavior_evidence_enabled: bool = True
    behavior_fidelity_enabled: bool = True
    behavior_projections_enabled: bool = False
    behavior_projection_pilot_enabled: bool = False
    behavior_pilot_assignment_salt: str = "tce-behavior-pilot-v1"
    behavior_pilot_assignment_ttl_days: int = 30
    behavior_pilot_min_window_days: int = 28
    behavior_pilot_min_completed_per_arm: int = 30
    behavior_pilot_min_completion_coverage: float = 0.80
    behavior_pilot_max_p95_retrieval_latency_ms: float = 120.0
    behavior_pilot_max_top1_degradation: float = 0.05
    behavior_prediction_enabled: bool = True
    behavior_calibration_enabled: bool = False
    behavior_autonomy_gate_enabled: bool = False
    behavior_storage_gate_mode: str = "shadow"
    behavior_storage_min_score: float = 0.55
    behavior_prediction_min_confidence: float = 0.55
    behavior_capability_broker_enabled: bool = True
    behavior_process_mining_enabled: bool = True
    behavior_shadow_evaluation_enabled: bool = True
    behavior_memory_review_enabled: bool = True
    behavior_counterfactual_enabled: bool = True
    behavior_process_min_support: int = 2
    capability_grant_ttl_seconds: int = 120
    behavior_control_retention_days: int = 365
    workspace_access_mode: str = "compat"
    identity_claims_mode: str = "compat"
    identity_claims_json: str = "{}"
    audit_write_mode: str = "durable"
    runtime_profile: str = "local-lite"
    mcp_tool_profile: str = "core"
    execution_enforcement_level: str = "protocol_only"
    execution_interception_attested: bool = False
    execution_interception_provider: str = ""
    behavior_subject_bindings: str = ""
    cold_start_min_events: int = 3
    cold_start_min_patterns: int = 2
    pattern_min_frequency: int = 3
    pattern_min_confidence: float = 0.58
    context_bundle_cache_ttl_seconds: int = 300
    rate_limit_requests_per_minute: int = 900
    takeover_step_rate_limit_requests_per_minute: int = 1200
    takeover_lifecycle_rate_limit_requests_per_minute: int = 1800
    search_enable_recency: bool = True
    search_recency_lambda: float = 0.01
    search_weight_lexical: float = 0.55
    search_weight_vector: float = 0.20
    search_candidate_pool_multiplier: int = 8
    search_candidate_pool_max: int = 400
    search_rrf_enabled: bool = False
    search_rrf_k: int = 60
    search_graph_bonus: float = 0.10
    search_weight_recency: float = 0.15
    search_feedback_enabled: bool = True
    search_feedback_weight: float = 0.08
    search_query_expansion_enabled: bool = False
    search_query_expansion_timeout_ms: int = 25
    search_query_expansion_max_terms: int = 4
    search_query_expansion_skip_candidate_multiplier: int = 4
    search_query_expansion_skip_dup_ratio: float = 0.80
    search_mmr_enabled: bool = False
    search_mmr_lambda: float = 0.65
    search_mmr_candidate_pool: int = 60
    search_enhancement_budget_ms: int = 120
    search_retry_enabled: bool = True
    search_retry_max_attempts: int = 2
    search_retry_backoff_ms: int = 8
    search_retry_budget_ms: int = 40
    qdrant_retry_max_attempts: int = 2
    qdrant_retry_backoff_ms: int = 12
    qdrant_retry_budget_ms: int = 80
    search_embedding_timeout_seconds: float = 5.00
    context_tiers_enabled: bool = False
    intent_retrieval_enabled: bool = False
    retry_feedback_enabled: bool = False
    profile_tuning_enabled: bool = False
    memory_activation_autonomy_weight_multiplier: float = 1.15
    memory_activation_enabled: bool = True
    memory_activation_half_life_days: int = 45
    memory_activation_weight: float = 0.12
    memory_multi_hop_enabled: bool = True
    memory_multi_hop_limit: int = 600
    semantic_consolidation_enabled: bool = True
    semantic_consolidation_lookback_days: int = 21
    reflection_enabled: bool = True
    vector_backend: str = "pgvector"
    qdrant_enabled: bool = True
    qdrant_timeout_ms: int = 60
    qdrant_canary_workspace_ids: str = ""
    context_retrieval_trigger_score: float = 0.68
    context_retrieval_escalate_score: float = 0.52
    context_retrieval_budget_ms: int = 120
    context_backend_timeout_ms: int = 60
    takeover_retrieval_confidence_trigger: float = 0.70
    takeover_retrieval_low_evidence_threshold: int = 2
    takeover_retrieval_min_turn_for_evidence_gate: int = 4
    takeover_retrieval_cooldown_turns: int = 2
    takeover_retrieval_budget_guard_enabled: bool = True
    takeover_working_set_refresh_every_n_turns: int = 24
    takeover_advisor_every_n_turns: int = 2
    takeover_advisor_fail_streak_escalate: int = 2
    takeover_advisor_backoff_turns: int = 2
    takeover_advisor_runtime_cooloff_turns: int = 64
    advisor_local_ollama_timeout_ms: int = 90000
    takeover_advisor_runtime_attempt_cap_ms: int = 60000
    takeover_advisor_local_ollama_runtime_attempt_cap_ms: int = 100000
    obs_semantic_enabled: bool = False
    obs_semantic_autogate: bool = True
    obs_semantic_min_observations: int = 300
    obs_semantic_fallback_min_results: int = 3
    obs_semantic_top_k: int = 5
    obs_semantic_query_timeout_ms: int = 80
    obs_semantic_max_queue_depth: int = 1000
    semantic_classifier_enabled: bool = False
    semantic_classifier_intent_threshold: float = 0.67
    semantic_classifier_situation_threshold: float = 0.61
    semantic_classifier_margin: float = 0.06
    feedback_base_alpha: float = 0.15
    feedback_alpha_min: float = 0.08
    feedback_alpha_max: float = 0.35
    event_lifecycle_enabled: bool = True
    event_retention_days: int = 180
    archive_enabled: bool = True
    archive_path: str = "/data/archives"
    audit_retention_days: int = 180
    interaction_retention_days: int = 180
    lifecycle_max_partitions_per_run: int = 2
    lifecycle_max_delete_rows_per_run: int = 50000
    lifecycle_dry_run: bool = False
    snapshot_rehydrate_enabled: bool = True
    snapshot_max_age_days: int = 14
    snapshot_max_per_session: int = 10
    takeover_rationale_enrichment_enabled: bool = True
    takeover_context_snapshot_enrichment_enabled: bool = True
    takeover_context_snapshot_max_chars: int = 1000
    takeover_context_snapshot_max_evidence_snippets: int = 3
    takeover_context_snapshot_max_alternatives: int = 3
    takeover_execution_details_contract_enabled: bool = True
    takeover_execution_reasoning_summary_max_chars: int = 500
    handoff_milestone_validation_mode: str = "shadow"
    handoff_retention_days: int = 90
    resume_packet_enabled: bool = True
    takeover_citation_snippets_enabled: bool = True
    takeover_citation_snippet_max_chars: int = 100
    takeover_citation_snippet_max_items: int = 20
    graph_health_alert_threshold_zero_entities_minutes: int = 30
    auto_capture_interactions: bool = True
    auto_capture_skip_sensitive: bool = True
    auto_capture_max_chars: int = 2000
    episode_extraction_enabled: bool = True
    episode_extraction_async: bool = True
    episode_extraction_model_mode: str = "llm_first"
    episode_extraction_timeout_seconds: float = 3.0
    search_weight_relevance: float = 0.45
    search_weight_stability: float = 0.25
    search_weight_authority: float = 0.20
    memory_rule_p0_always_include: bool = True
    ingest_source_allowlist: str = "codex,claude,cursor,vscode,browser,git,mcp,tce-api,tce-lite-api,cli"
    security_encrypt_sensitive_required: bool = True
    security_encryption_key_id: str = "local-dev"
    security_encryption_secret: str = ""
    event_ordering_enable_vector_clock: bool = True
    event_ordering_accept_legacy: bool = True
    takeover_safety_policy: str = "high-risk-pause"
    takeover_confirm_keyword: str = "confirm"
    takeover_deny_keyword: str = "abort"
    takeover_autonomy_policy_default: str = "human_consultative"
    takeover_goal_source: str = "open_discovery"
    takeover_goal_min_confidence: float = 0.62
    takeover_goal_selection_min_confidence: float = 0.62
    takeover_goal_discovery_every_n_turns: int = 24
    takeover_permit_ttl_seconds: int = 300
    takeover_continuity_gap_seconds: int = 600
    takeover_needs_human_threshold_cold: float = 0.45
    takeover_needs_human_threshold_hot: float = 0.60
    takeover_needs_human_ramp_start: float = 0.50
    takeover_needs_human_ramp_end: float = 0.80
    takeover_affective_enabled: bool = True
    takeover_affective_profile: str = "balanced"
    takeover_affective_use_unknown_goals: bool = True
    takeover_affective_use_nothing_goals: bool = True
    takeover_affective_every_n_turns: int = 6
    takeover_affective_similarity_timeout_ms: int = 60
    takeover_affective_goal_embedding_enabled: bool = True
    takeover_goal_cache_enabled: bool = True
    takeover_goal_cache_l1_ttl_seconds: int = 8
    takeover_goal_cache_l2_ttl_seconds: int = 300
    takeover_goal_cache_max_items: int = 2000
    takeover_goal_cache_precompute_on_activate: bool = True
    takeover_goal_cache_precompute_async: bool = True
    takeover_nothing_goal_overwhelm_threshold: float = 0.60
    takeover_nothing_goal_release_threshold: float = 0.40
    takeover_autonomy_tick_enabled: bool = True
    takeover_autonomy_tick_interval_seconds: int = 300
    takeover_notice_threshold: float = 0.72
    takeover_notice_dedupe_minutes: int = 60
    takeover_retry_enabled: bool = True
    takeover_retry_max_attempts: int = 3
    takeover_retry_window_limit: int = 8
    takeover_retry_window_minutes: int = 30
    typed_contract_enabled: bool = False
    workflow_template_reuse_min_reliability: float = 0.70
    takeover_execution_claim_ttl_seconds: int = 300
    takeover_enforcement_mode: str = "strict_takeover"
    advisor_primary_provider: str = "openai"
    advisor_primary_model: str = "gpt-4o-mini"
    advisor_fallback_chain: str = "anthropic,gemini"
    advisor_custom_base_url: str = ""
    advisor_custom_model: str = ""
    advisor_custom_api_key_ref: str = "advisor-custom"
    advisor_provider_timeout_ms: int = 2200
    advisor_provider_retry_max: int = 1
    advisor_router_v2_enabled: bool = True
    advisor_total_budget_ms: int = 2200
    advisor_attempt_timeout_ms: int = 900
    advisor_connect_timeout_ms: int = 250
    advisor_read_timeout_ms: int = 900
    advisor_total_budget_hard_cap_ms: int = 8000
    advisor_attempt_timeout_hard_cap_ms: int = 3000
    advisor_read_timeout_hard_cap_ms: int = 3000
    search_embedding_timeout_hard_cap_seconds: float = 5.0
    takeover_advisor_runtime_attempt_cap_hard_cap_ms: int = 5000
    takeover_advisor_local_ollama_runtime_attempt_cap_hard_cap_ms: int = 8000
    advisor_switch_status_cache_ttl_ms: int = 5000
    advisor_route_cache_ttl_ms: int = 30000
    advisor_circuit_open_failures: int = 5
    advisor_circuit_half_open_seconds: int = 30
    advisor_circuit_close_successes: int = 2
    advisor_failover_min_remaining_ms: int = 250
    autonomy_gate_lookback_turns: int = 120
    autonomy_gate_min_turns: int = 20
    autonomy_gate_min_terminal_directives: int = 8
    autonomy_gate_min_execution_success_rate: float = 0.88
    autonomy_gate_max_needs_human_rate: float = 0.20
    autonomy_gate_min_avg_decision_confidence: float = 0.72
    autonomy_gate_min_avg_context_quality: float = 0.48
    autonomy_gate_min_eval_floor: float = 0.53
    autonomy_gate_min_confidence_alignment: float = 0.55
    autonomy_gate_min_outcome_feedback_rate: float = 0.60
    autonomy_gate_min_calibration_samples: int = 20
    autonomy_gate_min_outcome_feedback_samples: int = 20
    autonomy_kpi_min_project_completion_rate: float = 0.80
    autonomy_kpi_max_manual_interventions_per_project: float = 1.0
    autonomy_kpi_max_reopen_rate_after_completion: float = 0.10
    autonomy_kpi_min_verification_pass_rate: float = 0.80
    autonomy_verification_required_checks: str = "build,test,lint"
    advisor_required_categories: str = "global,china,custom"
    advisor_custom_headers_allowlist: str = (
        "x-api-key,x-tenant-id,anthropic-version,x-goog-user-project,x-request-id"
    )
    dashboard_advisor_v2_enabled: bool = False
    dashboard_stack_restart_containerized_enabled: bool = True
    dashboard_stack_restart_helper_image: str = "docker:27-cli"
    dashboard_stack_restart_timeout_seconds: int = 240
    dashboard_stack_restart_log_tail_lines: int = 120
    log_level: str = "INFO"
    redaction_zone_paths: str = Field(default="")
    cors_allow_origins: str = "http://localhost:4200,http://127.0.0.1:4200"
    cors_allow_credentials: bool = False

    @property
    def token_set(self) -> set[str]:
        return {token.strip() for token in self.api_tokens.split(",") if token.strip()}

    @property
    def cors_origins(self) -> list[str]:
        raw = self.cors_allow_origins.strip()
        if not raw:
            return []
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def qdrant_canary_workspace_set(self) -> set[str]:
        return {item.strip() for item in self.qdrant_canary_workspace_ids.split(",") if item.strip()}

    @property
    def ingest_source_allowlist_set(self) -> set[str]:
        return {item.strip().lower() for item in self.ingest_source_allowlist.split(",") if item.strip()}

    @property
    def advisor_fallback_chain_list(self) -> list[str]:
        return [item.strip().lower() for item in self.advisor_fallback_chain.split(",") if item.strip()]

    @property
    def advisor_custom_headers_allowlist_set(self) -> set[str]:
        return {item.strip().lower() for item in self.advisor_custom_headers_allowlist.split(",") if item.strip()}

    @property
    def advisor_required_categories_set(self) -> list[str]:
        return [item.strip().lower() for item in self.advisor_required_categories.split(",") if item.strip()]

    @property
    def effective_advisor_total_budget_ms(self) -> int:
        return min(
            max(500, int(self.advisor_total_budget_ms)),
            max(500, int(self.advisor_total_budget_hard_cap_ms)),
        )

    @property
    def effective_advisor_attempt_timeout_ms(self) -> int:
        return min(
            max(200, int(self.advisor_attempt_timeout_ms)),
            max(200, int(self.advisor_attempt_timeout_hard_cap_ms)),
        )

    @property
    def effective_advisor_read_timeout_ms(self) -> int:
        return min(
            max(200, int(self.advisor_read_timeout_ms)),
            max(200, int(self.advisor_read_timeout_hard_cap_ms)),
        )

    @property
    def effective_search_embedding_timeout_seconds(self) -> float:
        return min(
            max(0.05, float(self.search_embedding_timeout_seconds)),
            max(0.05, float(self.search_embedding_timeout_hard_cap_seconds)),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
