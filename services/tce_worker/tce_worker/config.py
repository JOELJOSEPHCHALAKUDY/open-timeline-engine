from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TCE_", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/tce"
    redis_url: str = "redis://localhost:6379/0"
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "mxbai-embed-large"
    embedding_dimensions: int = 1024
    extract_model: str = "qwen2.5:7b"
    model_provider: str = "ollama"
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"
    openai_extract_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_extract_model: str = "claude-haiku-4-5-20251001"
    llm_extraction: bool = False
    pattern_min_frequency: int = 3
    pattern_min_confidence: float = 0.58
    pattern_weight_frequency: float = 0.35
    pattern_weight_recency: float = 0.30
    pattern_weight_consistency: float = 0.20
    pattern_weight_feedback: float = 0.15
    worker_retry_max: int = 3
    worker_retry_intervals: str = "10,30,120"
    obs_semantic_enabled: bool = False
    obs_semantic_autogate: bool = True
    obs_semantic_min_observations: int = 300
    obs_semantic_top_k: int = 5
    obs_embedding_batch_size: int = 50
    episode_extraction_enabled: bool = True
    episode_extraction_async: bool = True
    episode_extraction_model_mode: str = "llm_first"
    episode_extraction_timeout_seconds: float = 3.0
    semantic_consolidation_enabled: bool = True
    semantic_consolidation_lookback_days: int = 21
    reflection_enabled: bool = True
    event_lifecycle_enabled: bool = True
    event_retention_days: int = 180
    handoff_retention_days: int = 90
    handoff_outbox_batch_size: int = 100
    behavior_control_retention_days: int = 365
    archive_enabled: bool = True
    archive_path: str = "/data/archives"
    audit_retention_days: int = 180
    interaction_retention_days: int = 180
    lifecycle_max_partitions_per_run: int = 2
    lifecycle_max_delete_rows_per_run: int = 50000
    lifecycle_dry_run: bool = False
    snapshot_max_per_session: int = 10
    graph_health_alert_threshold_zero_entities_minutes: int = 30
    queue_default_name: str = "tce-default"
    queue_embeddings_name: str = "tce-embeddings"
    queue_patterns_name: str = "tce-patterns"
    queue_lifecycle_name: str = "tce-lifecycle"
    qdrant_enabled: bool = True
    qdrant_url: str = ""
    qdrant_collection: str = "tce_event_embeddings"
    qdrant_timeout_seconds: float = 2.0
    qdrant_sync_enabled: bool = True
    qdrant_sync_batch_size: int = 500
    qdrant_sync_max_batches: int = 10
    worker_listen_queues: str = "tce-default,tce-embeddings,tce-patterns,tce-lifecycle"
    worker_enable_scheduler: bool = True
    decision_extraction_enabled: bool = True
    decision_extraction_batch_size: int = 100
    decision_extraction_lease_seconds: int = 300
    decision_extraction_max_attempts: int = 10
    capture_opportunity_ttl_seconds: int = 3600
    behavior_storage_min_score: float = 0.55
    security_encryption_key_id: str = "local-dev"
    security_encryption_secret: str = ""
    planning_enabled: bool = True
    planning_job_lease_seconds: int = 120
    planning_job_max_attempts: int = 3
    planning_job_batch_size: int = 20
    planning_job_backoff_cap_seconds: int = 900
    # These two knobs now select whether the WORKER may use a model, not whether a request
    # thread may. Off by default: the bounded control path must never wait on one.
    takeover_plan_llm_enabled: bool = False
    takeover_dream_llm_enabled: bool = False
    takeover_plan_llm_timeout_seconds: float = 25.0
    takeover_plan_max_steps: int = 8
    # Without these two, create_gateway falls through to its 30 s default in the worker.
    advisor_attempt_timeout_ms: int = 900
    advisor_read_timeout_ms: int = 900
    # Read by the relocated dream-message reader on its first line.
    block_sensitivity: int = 3
    # ---- P5 ----
    # The generation half of the proposal settings (p5_design.md 0.8).  The worker carries
    # only the names the synthesis job itself reads.  The route owns dream_proposals_enabled,
    # dream_run_stale_minutes, dream_snooze_default_days, dream_list_limit and the two
    # re-surface intervals; duplicating those here would be a second source of truth for a
    # number the worker never consults.  dream_nonresponse_after_surfaces is here because the
    # duplicate-merge path appends an event and every append re-folds the proposal.  Defaults
    # are the canonical ones in tce_shared.aspirations.DREAM_SETTING_DEFAULTS, and
    # test_worker_dream_synthesis.py pins the two together so a drift breaks the build.
    dream_message_limit: int = 60
    dream_min_messages: int = 10
    dream_min_message_chars: int = 25
    dream_max_message_chars: int = 1200
    dream_max_proposals_per_run: int = 3
    dream_min_citations: int = 2
    dream_max_citations: int = 6
    dream_quote_max_chars: int = 200
    dream_min_quote_overlap_tokens: int = 2
    dream_min_citation_relevance_tokens: int = 1
    dream_max_live_proposals: int = 20
    dream_duplicate_similarity: float = 0.60
    dream_material_new_citations: int = 2
    dream_material_max_similarity: float = 0.60
    dream_rejected_cooldown_days: int = 30
    dream_rejected_lookback_days: int = 365
    dream_rejected_scan_limit: int = 200
    dream_max_reproposals: int = 2
    dream_proposal_ttl_days: int = 45
    dream_nonresponse_after_surfaces: int = 3

    @property
    def confidence_weights(self) -> tuple[float, float, float, float]:
        weights = (
            self.pattern_weight_frequency,
            self.pattern_weight_recency,
            self.pattern_weight_consistency,
            self.pattern_weight_feedback,
        )
        total = sum(weights)
        if total <= 0:
            return (0.35, 0.30, 0.20, 0.15)
        return (
            self.pattern_weight_frequency / total,
            self.pattern_weight_recency / total,
            self.pattern_weight_consistency / total,
            self.pattern_weight_feedback / total,
        )

    @property
    def retry_intervals(self) -> list[int]:
        intervals = []
        for chunk in self.worker_retry_intervals.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                intervals.append(max(1, int(chunk)))
            except ValueError:
                continue
        return intervals or [10, 30, 120]

    @property
    def listen_queues(self) -> list[str]:
        queues = []
        for chunk in self.worker_listen_queues.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            queues.append(chunk)
        return queues or [self.queue_default_name]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
