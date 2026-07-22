from __future__ import annotations

import hashlib
import errno
import json
import math
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import Counter as CollectionCounter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ValidationError
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from prometheus_client import Counter as PromCounter
import logging

import httpx
from ote_advisor_providers import get_provider, list_provider_metadata, resolve_fallback_chain
from ote_advisor_providers.base import ProviderAttemptResult, ProviderRequest
from ote_advisor_providers.router import (
    active_profile as advisor_active_profile,
    enforce_required_category_coverage as advisor_enforce_required_category_coverage,
    normalize_profile as advisor_normalize_profile,
    normalize_profile_bundle as advisor_normalize_profile_bundle,
    resolve_chain_from_profile as advisor_resolve_chain_from_profile,
    select_route as advisor_select_route,
    runtime_status as advisor_runtime_status,
    update_health_state as advisor_update_health_state,
)
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from tce_model_gateway.factory import get_gateway as get_model_gateway
from tce_shared.dashboard import timeline_dashboard_html
from tce_shared.autonomy_context import (
    SUMMARY_VERSION,
    autonomy_profile_tuning,
    build_retry_feedback,
    summarize_event_record,
    summarize_hit_text,
)
from tce_shared.behavior_fidelity import (
    CALIBRATION_SCENARIOS,
    behavior_storage_gate,
    eligible_behavior_evidence,
    evaluate_behavior_fidelity,
    normalize_behavior_evidence,
    predict_behavior,
)
from tce_shared.behavior_projection import BehaviorProjectionNotFound, build_behavior_projection
from tce_shared.behavior_pilot import (
    assign_behavior_pilot_variant,
    behavior_pilot_outcome_digest,
    behavior_pilot_status,
    prepare_behavior_pilot_context,
    sanitize_behavior_pilot_payload,
)
from tce_shared.behavior_control import normalize_counterfactual, redact_control_text
from tce_shared.events import (
    AgentRole,
    BehaviorCalibrationAnswerRequest,
    BehaviorCalibrationScenariosResponse,
    BehaviorEvaluationListResponse,
    BehaviorEvaluationRequest,
    BehaviorEvaluationResponse,
    BehaviorEvidenceSource,
    BehaviorEvidenceRequest,
    BehaviorEvidenceResponse,
    BehaviorPilotAssignmentRequest,
    BehaviorPilotAssignmentResponse,
    BehaviorPilotOutcomeRequest,
    BehaviorPilotOutcomeResponse,
    BehaviorPilotStatusResponse,
    BehaviorPredictionRequest,
    BehaviorPredictionResponse,
    BehaviorProjectionFormat,
    BehaviorProjectionResponse,
    BehaviorShadowStatusResponse,
    CapabilityConsumeRequest,
    CapabilityConsumeResponse,
    CapabilityGrantRequest,
    CapabilityGrantResponse,
    CounterfactualCreateRequest,
    CounterfactualItem,
    CounterfactualListResponse,
    CounterfactualResolveRequest,
    CompletionCaptureRequest,
    CompletionCaptureResponse,
    ContinuityPilotStatusResponse,
    MemoryReviewItem,
    MemoryReviewListResponse,
    MemoryReviewResolveRequest,
    ProcessMiningRequest,
    ProcessMiningResponse,
    ProcessModelItem,
    AutonomyNotice,
    AutonomyGoalSource,
    AutonomyGoalStatus,
    AutonomyPolicyProfile,
    AutonomyRiskTier,
    GoalKind,
    CloneAdviceRequest,
    CloneAdviceResponse,
    DirectiveExecution,
    DirectiveExecutionState,
    ExecutionPermitDecision,
    ExecutionClaimRequest,
    ExecutionReportRequest,
    ExecutionStatusResponse,
    ExecutionPermitRequest,
    ExecutionPermitResolveRequest,
    ExecutionPermitResponse,
    EventDecision,
    EventEnvelope,
    EventLinks,
    EventOutcome,
    EventSearchRequest,
    EventStyle,
    EventType,
    OperationMode,
    PatternFeedbackRequest,
    RuntimeModeConfig,
    RetryStrategy,
    ResumePacketRequest,
    ResumePacketResponse,
    ResumePacketRetrievalMeta,
    ResumePacketFileItem,
    ResumePacketAnchor,
    ResumePacketChangeSummary,
    ResumeFeedbackRequest,
    ResumeFeedbackResponse,
    SafetyDecision,
    TakeoverAutonomyStatusResponse,
    TakeoverAutonomyTickRequest,
    TakeoverAutonomyTickResponse,
    TakeoverClassification,
    TakeoverDecisionSource,
    TakeoverFeedbackRequest,
    TakeoverFeedbackResponse,
    TakeoverGoalCacheInvalidateRequest,
    TakeoverGoalCacheStatusResponse,
    TakeoverGoal,
    TakeoverGoalsPrecomputeRequest,
    TakeoverGoalsDiscoverRequest,
    TakeoverGoalsResponse,
    TakeoverLatencyBreakdown,
    TakeoverMode,
    TakeoverNextAction,
    TakeoverPolicy,
    TakeoverNoticeAckRequest,
    TakeoverNoticesResponse,
    TakeoverGoalSelectRequest,
    TakeoverPreloadRequest,
    TakeoverPreloadResponse,
    TakeoverState,
    TakeoverStepRequest,
    TakeoverStepResponse,
)
from tce_shared.failure_classifier import classify_failure, retry_strategy_for_attempt
from tce_shared.fingerprint import (
    DEFAULT_FINGERPRINT,
    apply_feedback_to_fingerprint,
    feedback_adjusted_alpha,
    merge_observation_into_fingerprint,
)
from tce_shared.rate_limit import InMemoryRateLimiter
from tce_shared.situation import SITUATION_TYPES, classify_situation
from tce_shared.takeover import (
    build_decisive_response,
    build_next_action,
    classify_text,
    compute_decision_confidence,
    contains_phrase,
    ensure_takeover_response,
    evaluate_safety,
    mode_override,
    next_expiry,
    normalize_text,
    objective_hash,
    persona_defaults,
    recent_failure_count,
    resolve_objective,
    should_trigger_deliberation,
    update_recent_outcomes,
)
from tce_shared.autonomy_goals import (
    adjust_consultative_threshold,
    classify_risk_tier,
    continuity_health,
    evaluate_execution_permit,
    score_goal,
)
from tce_shared.goal_affect import classify_goal_kind, compute_affective_scores, score_goal_affective
from tce_shared.goal_cache import (
    cache_key as goal_cache_key,
    deserialize_payload as goal_cache_deserialize,
    get_l1 as goal_cache_get_l1,
    invalidate_l1 as goal_cache_invalidate_l1,
    l1_status as goal_cache_l1_status,
    put_l1 as goal_cache_put_l1,
)
from tce_shared.goal_similarity import dedupe_candidates_by_similarity
from tce_shared.handoff import (
    handoff_intent,
    latest_checkpoint_anchor,
    merge_anchors,
    normalize_anchor_list,
    normalize_handoff_mode,
    normalize_objective_text,
    normalize_milestone_v1,
    rank_resume_candidates,
)

from .audit import write_audit_log
from .auth import AuthContext, get_auth_context
from .bundle import build_context_bundle, search_response
from .behavior_store import (
    latest_fidelity_gate,
    list_fidelity_runs,
    load_behavior_evidence,
    load_behavior_evidence_by_id,
    save_behavior_evidence,
    save_fidelity_run,
)
from .behavior_pilot_store import (
    BehaviorPilotConflict,
    BehaviorPilotExpired,
    BehaviorPilotNotFound,
    create_or_get_assignment as create_or_get_behavior_pilot_assignment,
    list_pilot_rows as list_behavior_pilot_rows,
    record_outcome as record_behavior_pilot_outcome,
)
from .behavior_control_store import (
    consume_capability_grant,
    create_counterfactual,
    create_memory_review,
    issue_capability_grant,
    list_counterfactuals,
    list_memory_reviews as list_behavior_memory_reviews,
    list_process_models,
    load_process_source_rows,
    mine_and_time,
    resolve_counterfactual,
    resolve_memory_review,
    save_process_models,
    save_shadow_prediction,
    shadow_status,
)
from .cache_clients import get_redis_client
from .clone import (
    advisor_reason,
    arbitrate,
    derive_clone_guidance,
    evaluate_loop_guard,
    normalize_interaction_id,
    write_agent_interaction,
)
from .clone_store import (
    build_session_context_from_state,
    load_fingerprint,
    query_similar_observations,
    save_clone_feedback,
    save_fingerprint,
    save_observation,
)
from .config import get_settings
from .crypto import maybe_decrypt_payload, maybe_encrypt_payload
from .continuity_store import (
    deliver_handoff_safely,
    enqueue_handoff,
    pilot_metrics,
    record_resume_attempt,
)
from .db import get_db, get_session_factory
from .graph import (
    graph_for_event,
    index_event_graph,
    list_team_memberships,
    search_entities,
    stamp_context_scope,
    upsert_team_membership,
    workspace_access_allowed,
)
from .logging import configure_logging
from .mode import get_runtime_mode, set_runtime_mode
from .models import (
    ContextBundleCache,
    Episode,
    EpisodeDecision,
    EpisodeEventLink,
    EpisodeLesson,
    Event,
    EventIdentity,
    ContinuityResumeAttempt,
    HandoffRecord,
    MemoryRule,
    MemoryTombstone,
    Pattern,
    PatternFeedback,
    RuntimeSetting,
    WorkflowTemplate,
)
from .otel import setup_otel
from .policy import PolicyEngine
from .queue import enqueue_job, get_queue_depth, queue_name_for_job
from .redaction import apply_redaction_zones, redact_payload, redact_text
from .routes.system import build_system_router
from .schemas import (
    ActivitySummaryResponse,
    ApiStatusInfo,
    BatchIngestRequest,
    BatchIngestResponse,
    CloneArbitrationRequest,
    CloneArbitrationResponse,
    CloneScoreBreakdown,
    CloneScoreResponse,
    ContextBundleRequest,
    ContextBundleResponse,
    DatabaseStatusInfo,
    FingerprintResponse,
    GraphEntitySearchResponse,
    GraphEventResponse,
    HealthResponse,
    IngestObservationsRequest,
    IngestObservationsResponse,
    IngestResponse,
    ObservationItem,
    ObservationListResponse,
    PatternItem,
    RuntimeModeInfo,
    RuntimeModeSetRequest,
    ServiceStatusInfo,
    SystemStatusResponse,
    TeamMembershipUpsertRequest,
    DashboardClientConfigResponse,
    DashboardAgentRole,
    DashboardAgentRolesResponse,
    DashboardIdentityInfo,
    DashboardGoalIntelligenceItem,
    DashboardGoalsIntelligenceResponse,
    DashboardGoalsIntelligenceSummary,
    DashboardHumanScoreHistoryPoint,
    DashboardHumanScoreHistoryResponse,
    DashboardHumanScoreRecomputeRequest,
    DashboardHumanScoreResponse,
    DashboardHumanScoreSubscores,
    EpisodeItem,
    EpisodeListResponse,
    EventAnnotationRequest,
    EventAnnotationResponse,
    ContextBriefRequest,
    ContextBriefResponse,
    ContextBriefSectionItem,
    MemoryRuleItem,
    MemoryRuleUpsertRequest,
    MemoryRuleListResponse,
    MemoryRuleDeprecateResponse,
    MemoryForgetRequest,
    MemoryForgetResponse,
    RetrievalEvalRunRequest,
    RetrievalEvalRunResponse,
    RetrievalEvalStatusResponse,
    GoalEmotionValue,
    GoalRelationEdge,
    AdvisorProviderItem,
    AdvisorProvidersResponse,
    AdvisorModelsResponse,
    AdvisorLiveModelsRequest,
    AdvisorRouteVerifyRequest,
    AdvisorLocalOllamaPullRequest,
    AdvisorLocalOllamaPullResponse,
    AdvisorLocalOllamaPullStatusResponse,
    AdvisorVerifyRequest,
    AdvisorVerifyAttempt,
    AdvisorVerifyResponse,
    AdvisorConfigUpdateRequest,
    AdvisorConfigResponse,
    AdvisorProfileItem,
    AdvisorRouteItem,
    AdvisorRuntimeProbeRequest,
    AdvisorRuntimeProbeResponse,
    AdvisorRuntimeStatusResponse,
    AdvisorRuntimeStatusRoute,
    AdvisorSwitchRequest,
    AdvisorSwitchResponse,
    DashboardStackRestartRequest,
    DashboardStackRestartResponse,
    DashboardStackRestartStatusResponse,
)
from .search import _owner_matches_hint, _resolve_owner_scope, retrieval_status_snapshot, run_search
from .takeover_store import (
    load_recent_session_memory_snapshot,
    load_takeover_state,
    record_takeover_action,
    reset_takeover_state as store_reset_takeover_state,
    save_session_memory_snapshot,
    save_takeover_state,
)

settings = get_settings()
configure_logging(settings.log_level)
setup_otel("tce-api")
logger = logging.getLogger(__name__)
app = FastAPI(title="Open Timeline Engine API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
policy_engine = PolicyEngine()
rate_limiter = InMemoryRateLimiter(limit=settings.rate_limit_requests_per_minute, window_seconds=60)
takeover_step_rate_limiter = InMemoryRateLimiter(
    limit=settings.takeover_step_rate_limit_requests_per_minute,
    window_seconds=60,
)
takeover_lifecycle_rate_limiter = InMemoryRateLimiter(
    limit=settings.takeover_lifecycle_rate_limit_requests_per_minute,
    window_seconds=60,
)
_APP_START_TIME = time.monotonic()

REQUEST_COUNT = PromCounter("tce_api_requests_total", "Total API requests", ["endpoint", "method"])
REQUEST_LATENCY = Histogram("tce_api_request_latency_seconds", "API request latency", ["endpoint", "method"])
TAKEOVER_FAST_PATH_MS = Histogram("tce_api_takeover_fast_path_seconds", "Takeover fast-path latency seconds")
TAKEOVER_DELIBERATION_MS = Histogram("tce_api_takeover_deliberation_seconds", "Takeover deliberation latency seconds")
TAKEOVER_TOTAL_MS = Histogram("tce_api_takeover_total_seconds", "Takeover total latency seconds")
TAKEOVER_DELIBERATION_COUNT = PromCounter("tce_api_takeover_deliberation_total", "Takeover deliberation activations", ["trigger"])
TAKEOVER_NEEDS_HUMAN_COUNT = PromCounter("tce_api_takeover_needs_human_total", "Takeover turns requiring human input")
TAKEOVER_CONFIDENCE = Histogram("tce_api_takeover_confidence", "Takeover decision confidence")
TAKEOVER_RETRIEVAL_TRIGGER_COUNT = PromCounter(
    "tce_api_takeover_retrieval_trigger_total",
    "Takeover retrieval triggers",
    ["reason"],
)
TAKEOVER_RETRIEVAL_SOURCE_COUNT = PromCounter(
    "tce_api_takeover_retrieval_source_total",
    "Takeover retrieval source counts",
    ["source"],
)
TAKEOVER_RETRIEVAL_BUDGET_EXCEEDED_COUNT = PromCounter(
    "tce_api_takeover_retrieval_budget_exceeded_total",
    "Takeover retrieval budget guard suppressions",
)
TAKEOVER_RETRIEVAL_LATENCY = Histogram(
    "tce_api_takeover_retrieval_latency_seconds",
    "Takeover retrieval latency seconds",
    ["source"],
)
DASHBOARD_HUMAN_SCORE_MS = Histogram(
    "tce_api_dashboard_human_score_compute_seconds",
    "Dashboard human score compute latency seconds",
)
DASHBOARD_GOAL_INTELLIGENCE_MS = Histogram(
    "tce_api_dashboard_goal_intelligence_compute_seconds",
    "Dashboard goal intelligence compute latency seconds",
)
DASHBOARD_HUMAN_SCORE_RECOMPUTE_COUNT = PromCounter(
    "tce_api_dashboard_human_score_recompute_total",
    "Dashboard human score recompute invocations",
)
_OLLAMA_PULL_JOBS: dict[str, dict[str, Any]] = {}
_OLLAMA_PULL_LOCK = threading.Lock()
_STACK_RESTART_JOBS: dict[str, dict[str, Any]] = {}
_STACK_RESTART_LOCK = threading.Lock()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return default
        return parsed
    except (TypeError, ValueError):
        return default


def _normalize_session_id_value(session_id: str | None) -> str:
    return str(session_id or "").strip() or "default"


def _latest_known_session_id(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> str | None:
    row = db.execute(
        text(
            """
            SELECT session_id
            FROM takeover_sessions
            WHERE workspace_id = :workspace_id
              AND user_id = :user_id
            ORDER BY
              CASE WHEN session_id = 'default' THEN 1 ELSE 0 END ASC,
              CASE WHEN active THEN 0 ELSE 1 END ASC,
              COALESCE(last_message_at, updated_at, activated_at) DESC NULLS LAST,
              updated_at DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "user_id": user_id},
    ).scalar()
    candidate = str(row or "").strip()
    if candidate:
        return candidate

    row = db.execute(
        text(
            """
            SELECT session_id
            FROM directive_executions
            WHERE workspace_id = :workspace_id
              AND user_id = :user_id
              AND session_id IS NOT NULL
              AND session_id <> 'default'
            ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "user_id": user_id},
    ).scalar()
    candidate = str(row or "").strip()
    if candidate:
        return candidate
    return None


def _resolve_effective_session_id(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str | None,
) -> str:
    requested = _normalize_session_id_value(session_id)
    if requested.lower() != "default":
        return requested
    inferred = _latest_known_session_id(db, workspace_id=workspace_id, user_id=user_id)
    if inferred:
        return inferred
    return "default"


def _verify_provider_with_retries(
    *,
    adapter: Any,
    request: ProviderRequest,
    retry_max: int,
) -> ProviderAttemptResult:
    result: ProviderAttemptResult | None = None
    attempts = max(1, int(retry_max))
    for _ in range(attempts):
        result = adapter.verify(request)
        if result.ok:
            break
        if result.code not in {"rate_limit", "timeout", "network_unreachable", "provider_error"}:
            break
    if result is not None:
        return result
    return ProviderAttemptResult(
        provider_id=adapter.metadata.provider_id,
        ok=False,
        code="provider_error",
        message="provider verification failed",
        latency_ms=0,
        models=[],
    )


def _bytes_to_human(value: int | None) -> str:
    if value is None:
        return "N/A"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value)} B"


def _advisor_scope_user_id(user_id: str) -> str:
    configured = os.getenv("TCE_ADVISOR_CONFIG_USER_ID", "").strip()
    if configured:
        return configured
    fallback = os.getenv("TCE_MCP_EXECUTOR_USER_ID", "").strip()
    if fallback:
        return fallback
    return (user_id or "").strip() or "local-user"


def _advisor_setup_setting_key(workspace_id: str, user_id: str) -> str:
    return f"advisor_setup:{workspace_id}:{_advisor_scope_user_id(user_id)}"


def _advisor_secret_setting_key(secret_ref: str) -> str:
    return f"advisor_secret:{secret_ref}"


def _advisor_profiles_setting_key(workspace_id: str, user_id: str) -> str:
    return f"advisor_profiles:{workspace_id}:{_advisor_scope_user_id(user_id)}"


def _advisor_health_setting_key(workspace_id: str, user_id: str) -> str:
    return f"advisor_runtime_health:{workspace_id}:{_advisor_scope_user_id(user_id)}"


_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_RAW_ENV_KEYS = {"TCE_ADVISOR_ROUTES_JSON"}
_LEGACY_API_KEY_ENV_ALIAS: dict[str, str] = {
    "TCE_OPENAI_API_KEY": "OPENAI_API_KEY",
    "TCE_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "TCE_GEMINI_API_KEY": "GEMINI_API_KEY",
    "TCE_DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY",
}
_DOCKER_STATS_EXECUTOR = ThreadPoolExecutor(max_workers=8)
_SYSTEM_STATUS_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_ADVISOR_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=6)
_SYSTEM_STATUS_HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(2.0, connect=0.5, read=1.5, write=1.0),
    limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
)


def _advisor_env_file_path() -> Path:
    override = os.getenv("TCE_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / ".env"


def _format_env_value(value: str) -> str:
    rendered = value
    if rendered == "":
        return '""'
    if any(ch.isspace() for ch in rendered) or any(ch in rendered for ch in ['#', '"', "'", "\\"]):
        escaped = rendered.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return rendered


def _parse_routes_env_value(raw: str) -> list[dict[str, Any]]:
    value = (raw or "").strip()
    if not value:
        return []
    try:
        parsed: Any = json.loads(value)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _upsert_env_values(updates: dict[str, str]) -> None:
    if not updates:
        return
    env_path = _advisor_env_file_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    index: dict[str, int] = {}
    for idx, line in enumerate(lines):
        match = _ENV_KEY_RE.match(line)
        if match:
            index[match.group(1)] = idx
    for key, value in updates.items():
        if not key:
            continue
        if key in _RAW_ENV_KEYS:
            rendered = f"{key}={str(value)}"
        else:
            rendered = f"{key}={_format_env_value(str(value))}"
        if key in index:
            lines[index[key]] = rendered
        else:
            lines.append(rendered)
    content = "\n".join(lines).rstrip("\n") + "\n"
    temp_path = env_path.with_suffix(env_path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    try:
        temp_path.replace(env_path)
    except OSError as exc:
        if exc.errno not in {errno.EBUSY, errno.EXDEV, errno.EACCES, errno.EPERM}:
            raise
        with env_path.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _persist_advisor_config_to_env(
    *,
    primary_provider: str,
    primary_model: str | None,
    fallback_chain: list[str],
    custom_base_url: str | None,
    custom_model: str | None,
    custom_api_key_ref: str | None,
    provider_timeout_ms: int,
    provider_retry_max: int,
    api_key: str | None,
    api_key_ref: str | None = None,
    routes: list[dict[str, Any]] | None = None,
    active_profile_id: str | None = None,
) -> None:
    local_ollama_base_url = _default_local_base_url("local_ollama") or ""
    local_lmstudio_base_url = _default_local_base_url("local_lmstudio") or ""
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        provider_id = str(route.get("provider_id") or "").strip().lower()
        base_url = str(route.get("base_url") or "").strip()
        if provider_id == "local_ollama" and base_url:
            local_ollama_base_url = base_url
        if provider_id == "local_lmstudio" and base_url:
            local_lmstudio_base_url = base_url
    updates = {
        "TCE_ADVISOR_PRIMARY_PROVIDER": primary_provider.strip().lower(),
        "TCE_ADVISOR_PRIMARY_MODEL": (primary_model or "").strip(),
        "TCE_ADVISOR_FALLBACK_CHAIN": ",".join(item.strip().lower() for item in fallback_chain if item.strip()),
        "TCE_ADVISOR_CUSTOM_BASE_URL": (custom_base_url or "").strip(),
        "TCE_ADVISOR_CUSTOM_MODEL": (custom_model or "").strip(),
        "TCE_ADVISOR_CUSTOM_API_KEY_REF": (custom_api_key_ref or settings.advisor_custom_api_key_ref).strip(),
        "TCE_ADVISOR_PROVIDER_TIMEOUT_MS": str(max(200, int(provider_timeout_ms))),
        "TCE_ADVISOR_PROVIDER_RETRY_MAX": str(max(1, int(provider_retry_max))),
        "TCE_ADVISOR_ROUTES_JSON": json.dumps(routes or [], separators=(",", ":")),
        "TCE_ADVISOR_ACTIVE_PROFILE_ID": (active_profile_id or "default").strip() or "default",
        "TCE_ADVISOR_LOCAL_OLLAMA_BASE_URL": local_ollama_base_url,
        "TCE_ADVISOR_LOCAL_LMSTUDIO_BASE_URL": local_lmstudio_base_url,
    }
    api_key_value = (api_key or "").strip()
    if api_key_value:
        key_ref_value = (api_key_ref or "").strip()
        key_provider = primary_provider
        if key_ref_value:
            for route in routes or []:
                if not isinstance(route, dict):
                    continue
                route_ref = str(route.get("api_key_ref") or "").strip()
                if route_ref != key_ref_value:
                    continue
                route_provider = str(route.get("provider_id") or "").strip().lower()
                if route_provider:
                    key_provider = route_provider
                    break
        adapter = get_provider(key_provider)
        env_key = adapter.metadata.api_key_env if adapter and adapter.metadata.api_key_env else ""
        if env_key:
            updates[env_key] = api_key_value
            alias = _LEGACY_API_KEY_ENV_ALIAS.get(env_key)
            if alias:
                updates[alias] = api_key_value
    _upsert_env_values(updates)


def _is_containerized_runtime() -> bool:
    return Path("/.dockerenv").exists()


def _default_local_base_url(provider_id: str) -> str | None:
    pid = provider_id.strip().lower()
    if pid == "local_ollama":
        override = os.getenv("TCE_ADVISOR_LOCAL_OLLAMA_BASE_URL", "").strip()
        if override:
            return override
        return "http://ollama:11434" if _is_containerized_runtime() else "http://localhost:11434"
    if pid == "local_lmstudio":
        override = os.getenv("TCE_ADVISOR_LOCAL_LMSTUDIO_BASE_URL", "").strip()
        if override:
            return override
        return "http://host.docker.internal:1234/v1" if _is_containerized_runtime() else "http://localhost:1234/v1"
    return None


def _provider_default_base_url(provider_id: str, provider_default: str | None) -> str | None:
    local_default = _default_local_base_url(provider_id)
    if local_default:
        return local_default
    return provider_default


def _should_rewrite_local_provider_base_url(provider_id: str, base_url: str) -> bool:
    pid = provider_id.strip().lower()
    if pid not in {"local_ollama", "local_lmstudio"}:
        return False
    if not _is_containerized_runtime():
        return False
    value = (base_url or "").strip().lower()
    return (
        value.startswith("http://localhost")
        or value.startswith("https://localhost")
        or value.startswith("http://127.0.0.1")
        or value.startswith("https://127.0.0.1")
    )


def _provider_connection_type(provider_id: str) -> str:
    if provider_id in {"local_ollama", "local_lmstudio"}:
        return "local"
    return "web"


def _provider_platform_hint(provider_id: str) -> str | None:
    if provider_id == "local_ollama":
        return "ollama"
    if provider_id == "local_lmstudio":
        return "llm_studio"
    return None


def _mask_secret(secret: str) -> str:
    value = (secret or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _read_runtime_setting_dict(db: Session, key: str) -> dict[str, Any]:
    setting = db.get(RuntimeSetting, key)
    if setting is None:
        return {}
    value = setting.value
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _upsert_runtime_setting(db: Session, key: str, value: dict[str, Any]) -> None:
    setting = db.get(RuntimeSetting, key)
    now = datetime.now(tz=UTC)
    if setting is None:
        db.add(RuntimeSetting(key=key, value=value, updated_at=now))
        return
    setting.value = value
    setting.updated_at = now


def _load_advisor_api_key(db: Session, secret_ref: str | None) -> str:
    _ = db
    _ = secret_ref
    return ""


def _store_advisor_api_key(db: Session, secret_ref: str, api_key: str) -> tuple[str, bool]:
    _ = db
    _ = secret_ref
    value = (api_key or "").strip()
    if not value:
        return "none", False
    return "env_file", True


def _provider_request_for(
    provider_id: str,
    *,
    model: str | None,
    base_url: str | None,
    api_version: str | None,
    api_key: str | None,
    timeout_ms: int,
    custom_headers: dict[str, str],
) -> ProviderRequest:
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")
    effective_base_url = (
        (base_url or "").strip()
        or _provider_default_base_url(provider_id, provider.metadata.default_base_url)
        or ""
    ).strip() or None
    return ProviderRequest(
        model=(model or "").strip() or None,
        api_key=(api_key or "").strip() or None,
        base_url=effective_base_url,
        api_version=(api_version or provider.metadata.api_version or "").strip() or None,
        timeout_ms=max(200, timeout_ms),
        custom_headers=custom_headers,
    )


def _gateway_provider_for_route(provider_id: str, protocol: str) -> str | None:
    pid = provider_id.strip().lower()
    proto = protocol.strip().lower()
    if pid == "local_ollama":
        return "ollama"
    if pid == "anthropic":
        return "anthropic"
    if proto == "openai_compatible":
        return "openai"
    return None


def _build_gateway_settings_for_route(
    *,
    provider_id: str,
    model_provider: str,
    request: ProviderRequest,
) -> Any:
    openai_base_url = None
    if model_provider == "openai":
        openai_base_url = (request.base_url or "").strip() or None
    return SimpleNamespace(
        model_provider=model_provider,
        ollama_url=(request.base_url or settings.ollama_url),
        embed_model=getattr(settings, "embed_model", "mxbai-embed-large"),
        extract_model=(request.model or getattr(settings, "extract_model", "qwen3:8b")),
        openai_api_key=(request.api_key or getattr(settings, "openai_api_key", "")),
        openai_base_url=openai_base_url,
        openai_embed_model=getattr(settings, "openai_embed_model", "text-embedding-3-small"),
        openai_extract_model=(request.model or getattr(settings, "openai_extract_model", "gpt-4o-mini")),
        anthropic_api_key=(request.api_key or getattr(settings, "anthropic_api_key", "")),
        anthropic_extract_model=(request.model or getattr(settings, "anthropic_extract_model", "claude-3-5-haiku-latest")),
        redis_url=getattr(settings, "redis_url", ""),
        advisor_timeout_seconds=max(1.0, float(request.timeout_ms) / 1000.0),
        advisor_attempt_timeout_ms=max(200, int(request.timeout_ms)),
        advisor_read_timeout_ms=max(200, int(request.timeout_ms)),
        _route_provider_id=provider_id,
    )


def _advisor_runtime_reason_from_routes(
    *,
    db: Session,
    auth: AuthContext,
    config: dict[str, Any],
    health: dict[str, Any],
    routes: list[dict[str, Any]],
    fingerprint: dict[str, Any],
    similar_observations: list[dict[str, Any]],
    session_context: dict[str, Any],
    current_situation: str,
    situation_type: str,
    extra_context: str,
    persist_health: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    selected_provider = ""
    selected_model = ""
    decision_payload: dict[str, Any] | None = None
    started_total = time.perf_counter()
    total_budget_ms = int(getattr(settings, "effective_advisor_total_budget_ms", settings.advisor_total_budget_ms))
    per_attempt_ms = int(getattr(settings, "effective_advisor_attempt_timeout_ms", settings.advisor_attempt_timeout_ms))
    runtime_attempt_cap_ms = min(
        max(500, int(getattr(settings, "takeover_advisor_runtime_attempt_cap_ms", 12000))),
        max(500, int(getattr(settings, "takeover_advisor_runtime_attempt_cap_hard_cap_ms", 5000))),
    )
    local_ollama_timeout_ms = min(
        max(500, int(getattr(settings, "advisor_local_ollama_timeout_ms", 25000))),
        max(500, int(getattr(settings, "effective_advisor_read_timeout_ms", per_attempt_ms))),
    )
    local_ollama_runtime_cap_ms = min(
        max(runtime_attempt_cap_ms, int(getattr(settings, "takeover_advisor_local_ollama_runtime_attempt_cap_ms", 30000))),
        max(
            runtime_attempt_cap_ms,
            int(getattr(settings, "takeover_advisor_local_ollama_runtime_attempt_cap_hard_cap_ms", 8000)),
        ),
    )
    failover_min_remaining_ms = max(200, int(settings.advisor_failover_min_remaining_ms))
    provider_timeout_ms = max(
        200,
        int(config.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
    )
    custom_headers = config.get("custom_headers") if isinstance(config.get("custom_headers"), dict) else {}
    api_version = str(config.get("api_version") or "").strip() or None

    for idx, route in enumerate(routes):
        provider_id = str(route.get("provider_id") or "").strip().lower()
        if not provider_id:
            continue
        adapter = get_provider(provider_id)
        if adapter is None:
            attempts.append(
                {
                    "provider_id": provider_id,
                    "model": route.get("model"),
                    "ok": False,
                    "code": "unknown_provider",
                    "message": f"provider '{provider_id}' is not registered",
                    "latency_ms": 0,
                }
            )
            continue

        elapsed_ms = int((time.perf_counter() - started_total) * 1000)
        remaining_ms = total_budget_ms - elapsed_ms
        if idx > 0 and remaining_ms < failover_min_remaining_ms:
            attempts.append(
                {
                    "provider_id": provider_id,
                    "model": route.get("model"),
                    "ok": False,
                    "code": "budget_exhausted",
                    "message": "remaining advisor budget too low for failover",
                    "latency_ms": 0,
                }
            )
            break

        route_attempt_ms = per_attempt_ms
        route_provider_timeout_ms = provider_timeout_ms
        route_runtime_cap_ms = runtime_attempt_cap_ms
        if provider_id == "local_ollama":
            route_attempt_ms = max(route_attempt_ms, local_ollama_timeout_ms)
            route_provider_timeout_ms = max(route_provider_timeout_ms, local_ollama_timeout_ms)
            route_runtime_cap_ms = local_ollama_runtime_cap_ms

        timeout_ms = max(
            200,
            min(
                route_attempt_ms,
                route_provider_timeout_ms,
                max(200, remaining_ms),
                route_runtime_cap_ms,
            ),
        )
        route_headers: dict[str, str] = {}
        route_custom = route.get("custom_headers")
        if isinstance(route_custom, dict):
            for k, v in route_custom.items():
                if str(k).strip() and str(v).strip():
                    route_headers[str(k).strip()] = str(v).strip()
        merged_headers = {**custom_headers, **route_headers}
        model = str(route.get("model") or "").strip() or None
        base_url = str(route.get("base_url") or "").strip() or None
        route_key_ref = str(route.get("api_key_ref") or "").strip() or None
        route_api_key = _resolve_route_api_key(
            db=db,
            provider_id=provider_id,
            route=route,
            config=config,
            explicit_key_ref=route_key_ref,
            provided_key=None,
        )
        request = _provider_request_for(
            provider_id,
            model=model,
            base_url=base_url,
            api_version=api_version,
            api_key=route_api_key,
            timeout_ms=timeout_ms,
            custom_headers=merged_headers,
        )
        route_for_health = {
            "provider_id": provider_id,
            "model": request.model,
            "priority": int(route.get("priority", idx)),
            "provider_category": adapter.metadata.provider_category,
        }
        attempt_started = time.perf_counter()
        ok = False
        code = "provider_error"
        message = "advisor runtime inference failed"
        result: dict[str, Any] | None = None
        if _provider_requires_key(provider_id) and not (request.api_key or "").strip():
            code = "auth_failure"
            message = f"provider '{provider_id}' missing API key"
        else:
            gateway_provider = _gateway_provider_for_route(provider_id, adapter.metadata.protocol)
            if gateway_provider is None:
                code = "unsupported_provider"
                message = f"provider '{provider_id}' does not support runtime advisor inference yet"
            else:
                try:
                    gateway_settings = _build_gateway_settings_for_route(
                        provider_id=provider_id,
                        model_provider=gateway_provider,
                        request=request,
                    )
                    gateway = get_model_gateway(gateway_settings)
                    result = advisor_reason(
                        gateway=gateway,
                        user_name=settings.clone_user_name,
                        fingerprint=fingerprint,
                        similar_observations=similar_observations,
                        session_context=session_context,
                        current_situation=current_situation,
                        situation_type=situation_type,
                        extra_context=extra_context,
                    )
                    decision_text = str((result or {}).get("decision") or "").strip()
                    is_fallback = bool((result or {}).get("fallback"))
                    if decision_text and not is_fallback:
                        ok = True
                        code = "ok"
                        message = "advisor runtime inference succeeded"
                    elif is_fallback:
                        ok = False
                        code = "llm_unavailable_fallback"
                        message = "advisor runtime unavailable; fallback response returned"
                    else:
                        code = "empty_output"
                        message = "advisor runtime returned empty decision"
                except Exception as exc:
                    code = "provider_error"
                    message = f"{type(exc).__name__}: {exc}"
                    ok = False

        latency_ms = int((time.perf_counter() - attempt_started) * 1000)
        attempts.append(
            {
                "provider_id": provider_id,
                "model": request.model,
                "ok": ok,
                "code": code,
                "message": message,
                "latency_ms": latency_ms,
            }
        )
        health = advisor_update_health_state(
            health,
            route=route_for_health,
            ok=ok,
            latency_ms=latency_ms,
            error_code=None if ok else code,
            open_failures=settings.advisor_circuit_open_failures,
            half_open_seconds=settings.advisor_circuit_half_open_seconds,
            close_successes=settings.advisor_circuit_close_successes,
        )
        if ok and isinstance(result, dict):
            decision_payload = result
            selected_provider = provider_id
            selected_model = str(request.model or "")
            break

    if persist_health:
        _save_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
    elapsed_total_ms = int((time.perf_counter() - started_total) * 1000)
    metadata = {
        "used_llm": bool(decision_payload),
        "selected_provider": selected_provider or None,
        "selected_model": selected_model or None,
        "elapsed_ms": elapsed_total_ms,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }
    return decision_payload, metadata


def _default_advisor_config() -> dict[str, Any]:
    env_routes_raw = os.getenv("TCE_ADVISOR_ROUTES_JSON", "").strip()
    env_routes = _parse_routes_env_value(env_routes_raw)
    env_active_profile_id = os.getenv("TCE_ADVISOR_ACTIVE_PROFILE_ID", "default").strip() or "default"
    fallback_chain = list(settings.advisor_fallback_chain_list)
    local_ollama_present = any(str(item.get("provider_id") or "").strip().lower() == "local_ollama" for item in env_routes)
    local_lmstudio_present = any(str(item.get("provider_id") or "").strip().lower() == "local_lmstudio" for item in env_routes)
    if local_lmstudio_present and "local_lmstudio" not in fallback_chain:
        fallback_chain.append("local_lmstudio")
    if local_ollama_present and "local_ollama" not in fallback_chain:
        fallback_chain.append("local_ollama")
    base = {
        "advisor_primary_provider": settings.advisor_primary_provider,
        "advisor_primary_model": settings.advisor_primary_model,
        "advisor_fallback_chain": fallback_chain,
        "advisor_custom_base_url": settings.advisor_custom_base_url,
        "advisor_custom_model": settings.advisor_custom_model,
        "advisor_custom_api_key_ref": settings.advisor_custom_api_key_ref,
        "advisor_provider_timeout_ms": int(settings.advisor_provider_timeout_ms),
        "advisor_provider_retry_max": int(settings.advisor_provider_retry_max),
        "custom_headers": {},
        "api_version": None,
        "key_storage_backend": None,
        "key_present": False,
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "routing_mode": "adaptive",
        "failure_policy": "risk_aware_fail_safe",
        "profile_id": env_active_profile_id,
        "active_profile_id": env_active_profile_id,
        "routes": env_routes,
        "profiles": [],
    }
    if not base["advisor_custom_base_url"]:
        primary_default = _default_local_base_url(str(base["advisor_primary_provider"]))
        if primary_default:
            base["advisor_custom_base_url"] = primary_default
    bundle = advisor_normalize_profile_bundle(
        {},
        legacy_config=base,
        required_categories=settings.advisor_required_categories_set,
    )
    profile = advisor_active_profile(bundle)
    base["active_profile_id"] = str(bundle.get("active_profile_id") or "default")
    base["profile_id"] = str(profile.get("profile_id") or "default")
    base["routing_mode"] = str(profile.get("routing_mode") or "adaptive")
    base["failure_policy"] = str(profile.get("failure_policy") or "risk_aware_fail_safe")
    base["routes"] = list(profile.get("routes") or [])
    base["profiles"] = list(bundle.get("profiles") or [])
    return base


def _load_advisor_config(db: Session, *, workspace_id: str, user_id: str) -> dict[str, Any]:
    merged = _default_advisor_config()
    key = _advisor_setup_setting_key(workspace_id, user_id)
    current = _read_runtime_setting_dict(db, key)
    runtime_meta_keys = {
        "key_storage_backend",
        "key_present",
        "updated_at",
        "custom_headers",
        "api_version",
    }
    if current:
        merged.update({k: v for k, v in current.items() if k in runtime_meta_keys})
    fallback_value = merged.get("advisor_fallback_chain")
    if isinstance(fallback_value, str):
        merged["advisor_fallback_chain"] = [item.strip().lower() for item in fallback_value.split(",") if item.strip()]
    elif isinstance(fallback_value, list):
        merged["advisor_fallback_chain"] = [str(item).strip().lower() for item in fallback_value if str(item).strip()]
    else:
        merged["advisor_fallback_chain"] = []
    if not isinstance(merged.get("custom_headers"), dict):
        merged["custom_headers"] = {}
    runtime_profiles_raw: dict[str, Any] = {}
    if current:
        runtime_primary = str(current.get("advisor_primary_provider") or "").strip().lower()
        runtime_model = str(current.get("advisor_primary_model") or "").strip()
        runtime_fallback_raw = current.get("advisor_fallback_chain")
        if isinstance(runtime_fallback_raw, list):
            runtime_fallback = [str(item).strip().lower() for item in runtime_fallback_raw if str(item).strip()]
        elif isinstance(runtime_fallback_raw, str):
            runtime_fallback = [item.strip().lower() for item in runtime_fallback_raw.split(",") if item.strip()]
        else:
            runtime_fallback = []
        env_primary = str(merged.get("advisor_primary_provider") or "").strip().lower()
        env_model = str(merged.get("advisor_primary_model") or "").strip()
        env_fallback = [str(item).strip().lower() for item in merged.get("advisor_fallback_chain", []) if str(item).strip()]
        if runtime_primary == env_primary and runtime_model == env_model and runtime_fallback == env_fallback:
            runtime_profiles_raw = _read_runtime_setting_dict(db, _advisor_profiles_setting_key(workspace_id, user_id))
    profile_bundle = advisor_normalize_profile_bundle(
        runtime_profiles_raw,
        legacy_config=merged,
        required_categories=settings.advisor_required_categories_set,
    )
    profile = advisor_active_profile(profile_bundle)
    merged["active_profile_id"] = str(profile_bundle.get("active_profile_id") or "default")
    merged["profile_id"] = str(profile.get("profile_id") or "default")
    merged["routing_mode"] = str(profile.get("routing_mode") or "adaptive")
    merged["failure_policy"] = str(profile.get("failure_policy") or "risk_aware_fail_safe")
    normalized_routes: list[dict[str, Any]] = []
    for idx, item in enumerate(list(profile.get("routes") or [])):
        if not isinstance(item, dict):
            continue
        route = dict(item)
        provider_id = str(route.get("provider_id") or "").strip().lower()
        if not provider_id:
            continue
        adapter = get_provider(provider_id)
        if adapter is None:
            continue
        route_key_ref = str(route.get("api_key_ref") or "").strip()
        if _provider_requires_key(provider_id) and not route_key_ref:
            route["api_key_ref"] = f"advisor-{provider_id}"
        elif route_key_ref:
            route["api_key_ref"] = route_key_ref
        if not str(route.get("model") or "").strip():
            defaults = list(adapter.metadata.default_models or [])
            if defaults:
                route["model"] = str(defaults[0]).strip()
        route_base_url = str(route.get("base_url") or "").strip()
        if (not route_base_url) or _should_rewrite_local_provider_base_url(provider_id, route_base_url):
            route["base_url"] = _provider_default_base_url(provider_id, adapter.metadata.default_base_url)
        if not str(route.get("region_hint") or "").strip() and str(adapter.metadata.region_hint or "").strip():
            route["region_hint"] = str(adapter.metadata.region_hint).strip()
        if route.get("priority") is None:
            route["priority"] = idx
        normalized_routes.append(route)
    merged["routes"] = normalized_routes
    merged["profiles"] = list(profile_bundle.get("profiles") or [])
    env_key_present = False
    seen_providers: set[str] = set()
    for route in merged.get("routes", []):
        if not isinstance(route, dict):
            continue
        provider_id = str(route.get("provider_id") or "").strip().lower()
        if not provider_id or provider_id in seen_providers:
            continue
        seen_providers.add(provider_id)
        if _provider_env_key_present(provider_id):
            env_key_present = True
            break
    if not env_key_present:
        if _provider_env_key_present(str(merged.get("advisor_primary_provider") or "").strip().lower()):
            env_key_present = True
    merged["key_present"] = env_key_present
    if "updated_at" not in merged or not merged["updated_at"]:
        merged["updated_at"] = datetime.now(tz=UTC).isoformat()
    return merged


def _normalized_advisor_runtime_profile(
    config: dict[str, Any],
    profile_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(profile_raw, dict):
        candidate = dict(profile_raw)
    else:
        candidate = {
            "profile_id": str(config.get("profile_id") or config.get("active_profile_id") or "default"),
            "routing_mode": str(config.get("routing_mode") or "adaptive"),
            "failure_policy": str(config.get("failure_policy") or "risk_aware_fail_safe"),
            "routes": list(config.get("routes") or []),
        }
    normalized = advisor_normalize_profile(
        candidate,
        required_categories=settings.advisor_required_categories_set,
    )
    normalized_routes: list[dict[str, Any]] = []
    for idx, item in enumerate(list(normalized.get("routes") or [])):
        if not isinstance(item, dict):
            continue
        route = dict(item)
        provider_id = str(route.get("provider_id") or "").strip().lower()
        if not provider_id:
            continue
        adapter = get_provider(provider_id)
        if adapter is None:
            continue
        route_key_ref = str(route.get("api_key_ref") or "").strip()
        if _provider_requires_key(provider_id) and not route_key_ref:
            route["api_key_ref"] = f"advisor-{provider_id}"
        elif route_key_ref:
            route["api_key_ref"] = route_key_ref
        if not str(route.get("model") or "").strip():
            defaults = list(adapter.metadata.default_models or [])
            if defaults:
                route["model"] = str(defaults[0]).strip()
        route_base_url = str(route.get("base_url") or "").strip()
        if (not route_base_url) or _should_rewrite_local_provider_base_url(provider_id, route_base_url):
            route["base_url"] = _provider_default_base_url(provider_id, adapter.metadata.default_base_url)
        if not str(route.get("region_hint") or "").strip() and str(adapter.metadata.region_hint or "").strip():
            route["region_hint"] = str(adapter.metadata.region_hint).strip()
        if route.get("priority") is None:
            route["priority"] = idx
        normalized_routes.append(route)
    normalized["routes"] = normalized_routes
    return normalized


def _load_advisor_health(db: Session, *, workspace_id: str, user_id: str) -> dict[str, Any]:
    payload = _read_runtime_setting_dict(db, _advisor_health_setting_key(workspace_id, user_id))
    routes = payload.get("routes")
    if isinstance(routes, dict):
        return routes
    return {}


def _save_advisor_health(db: Session, *, workspace_id: str, user_id: str, routes: dict[str, Any]) -> None:
    _upsert_runtime_setting(
        db,
        _advisor_health_setting_key(workspace_id, user_id),
        {"routes": routes, "updated_at": datetime.now(tz=UTC).isoformat()},
    )


def _route_for_provider(config: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
    target = provider_id.strip().lower()
    for route in config.get("routes", []):
        if not isinstance(route, dict):
            continue
        if str(route.get("provider_id") or "").strip().lower() == target:
            return route
    return None


def _provider_requires_key(provider_id: str) -> bool:
    adapter = get_provider(provider_id)
    if adapter is None:
        return False
    return not bool(adapter.metadata.key_optional)


def _provider_api_key_env(provider_id: str) -> str:
    adapter = get_provider(provider_id)
    return adapter.metadata.api_key_env if adapter and adapter.metadata.api_key_env else ""


def _provider_env_key_present(provider_id: str) -> bool:
    env_key = _provider_api_key_env(provider_id)
    if not env_key:
        return False
    return bool(os.getenv(env_key, "").strip())


def _purge_advisor_secret_runtime_settings(db: Session) -> None:
    db.execute(text("DELETE FROM runtime_settings WHERE key LIKE 'advisor_secret:%'"))


def _resolve_route_key_ref(
    *,
    provider_id: str,
    route: dict[str, Any] | None,
    config: dict[str, Any],
    explicit_key_ref: str | None = None,
) -> str:
    explicit = str(explicit_key_ref or "").strip()
    if explicit:
        return explicit
    route_ref = str((route or {}).get("api_key_ref") or "").strip()
    if route_ref:
        return route_ref
    primary_provider = str(config.get("advisor_primary_provider") or "").strip().lower()
    if provider_id == primary_provider:
        return str(config.get("advisor_custom_api_key_ref") or settings.advisor_custom_api_key_ref or "").strip()
    return ""


def _resolve_route_api_key(
    *,
    db: Session,
    provider_id: str,
    route: dict[str, Any] | None,
    config: dict[str, Any],
    explicit_key_ref: str | None = None,
    provided_key: str | None = None,
) -> str:
    direct_key = str(provided_key or "").strip()
    if direct_key:
        return direct_key
    _ = db
    _ = _resolve_route_key_ref(
        provider_id=provider_id,
        route=route,
        config=config,
        explicit_key_ref=explicit_key_ref,
    )
    env_key = _provider_api_key_env(provider_id)
    return os.getenv(env_key or "", "").strip()


def _sanitize_event_outcome(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = dict(raw)
    if "success" not in value:
        token = str(
            value.get("status")
            or value.get("label")
            or value.get("result")
            or value.get("outcome")
            or ""
        ).strip().lower()
        sentiment = str(value.get("sentiment") or "").strip().lower()
        success = False
        if token in {"success", "succeeded", "ok", "accepted", "done", "completed", "pass", "improved"}:
            success = True
        elif token in {"failure", "failed", "error", "blocked", "timeout", "rejected"}:
            success = False
        elif sentiment == "positive":
            success = True
        elif sentiment == "negative":
            success = False
        value["success"] = success
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
        for key in ("status", "label", "result", "outcome", "sentiment"):
            if key in value:
                metrics[key] = value.get(key)
        value["metrics"] = metrics
    followups = value.get("followups")
    if not isinstance(followups, list):
        if isinstance(followups, str) and followups.strip():
            value["followups"] = [followups.strip()]
        else:
            next_steps = value.get("next_steps")
            if isinstance(next_steps, list):
                value["followups"] = [str(item).strip() for item in next_steps if str(item).strip()]
            else:
                value["followups"] = []
    return value


_SITUATION_ALIAS_MAP: dict[str, str] = {
    "restart_safety": "escalation_point",
    "uncertainty": "unknown_territory",
    "fallback_reliability": "error_occurred",
    "provider_selection": "choice_required",
    "decision_required": "choice_required",
    "goal_conflict": "conflict_detected",
    "setup_configuration": "routine_task",
    "opportunity_detected": "prioritization_needed",
    "planning": "prioritization_needed",
    "risk_assessment": "escalation_point",
}


def _canonical_situation_type(raw: str | None) -> str:
    token = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    valid = set(SITUATION_TYPES)
    if token in valid:
        return token
    if token in _SITUATION_ALIAS_MAP:
        mapped = _SITUATION_ALIAS_MAP[token]
        if mapped in valid:
            return mapped
    if token:
        inferred = classify_situation(
            token,
            semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
            semantic_threshold=float(getattr(settings, "semantic_classifier_situation_threshold", 0.61)),
            semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
        )
        if inferred in valid:
            return inferred
    return "routine_task"


def _sanitize_event_steps(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if isinstance(item, dict):
            description = str(item.get("description") or "").strip()
            if not description:
                continue
            out.append(
                {
                    "order": int(item.get("order") if item.get("order") is not None else idx),
                    "description": description,
                    "tool": item.get("tool"),
                    "output_ref": item.get("output_ref"),
                }
            )
            continue
        text_value = str(item).strip()
        if text_value:
            out.append({"order": idx, "description": text_value})
    return out


def _safe_validate(model: type[BaseModel], raw: Any, *, outcome: bool = False) -> BaseModel | None:
    if not isinstance(raw, dict):
        return None
    candidate: Any = _sanitize_event_outcome(raw) if outcome else raw
    try:
        return model.model_validate(candidate)
    except ValidationError:
        return None


def _config_response_from_raw(config_raw: dict[str, Any]) -> AdvisorConfigResponse:
    updated_at_raw = str(config_raw.get("updated_at") or "")
    try:
        updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00")) if updated_at_raw else datetime.now(tz=UTC)
    except ValueError:
        updated_at = datetime.now(tz=UTC)
    routes = [
        AdvisorRouteItem.model_validate(item)
        for item in config_raw.get("routes", [])
        if isinstance(item, dict)
    ]
    profiles = [
        AdvisorProfileItem.model_validate(item)
        for item in config_raw.get("profiles", [])
        if isinstance(item, dict)
    ]
    return AdvisorConfigResponse(
        advisor_primary_provider=str(config_raw.get("advisor_primary_provider") or settings.advisor_primary_provider),
        advisor_primary_model=config_raw.get("advisor_primary_model"),
        advisor_fallback_chain=list(config_raw.get("advisor_fallback_chain") or settings.advisor_fallback_chain_list),
        advisor_custom_base_url=config_raw.get("advisor_custom_base_url"),
        advisor_custom_model=config_raw.get("advisor_custom_model"),
        advisor_custom_api_key_ref=config_raw.get("advisor_custom_api_key_ref"),
        advisor_provider_timeout_ms=int(config_raw.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
        advisor_provider_retry_max=int(config_raw.get("advisor_provider_retry_max") or settings.advisor_provider_retry_max),
        custom_headers=config_raw.get("custom_headers") if isinstance(config_raw.get("custom_headers"), dict) else {},
        key_storage_backend=str(config_raw.get("key_storage_backend") or ""),
        key_present=bool(config_raw.get("key_present", False)),
        profile_id=str(config_raw.get("profile_id") or "default"),
        active_profile_id=str(config_raw.get("active_profile_id") or "default"),
        routing_mode=str(config_raw.get("routing_mode") or "adaptive"),
        failure_policy=str(config_raw.get("failure_policy") or "risk_aware_fail_safe"),
        routes=routes,
        profiles=profiles,
        updated_at=updated_at,
    )


def _start_ollama_pull_job(*, model: str, base_url: str) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    with _OLLAMA_PULL_LOCK:
        _OLLAMA_PULL_JOBS[job_id] = {
            "job_id": job_id,
            "state": "queued",
            "progress": 0.0,
            "error": None,
            "started_at": now,
            "completed_at": None,
        }

    def _run() -> None:
        endpoint = f"{base_url.rstrip('/')}/api/pull"
        with _OLLAMA_PULL_LOCK:
            job = _OLLAMA_PULL_JOBS.get(job_id)
            if job:
                job["state"] = "running"
        try:
            timeout_s = max(5.0, settings.advisor_attempt_timeout_ms / 1000.0)
            with httpx.stream("POST", endpoint, json={"name": model, "stream": True}, timeout=timeout_s) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"ollama returned {response.status_code}")
                for raw_line in response.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except Exception:
                        continue
                    total = float(payload.get("total") or 0.0)
                    completed = float(payload.get("completed") or 0.0)
                    progress = 0.0
                    if total > 0:
                        progress = max(0.0, min(1.0, completed / total))
                    done = bool(payload.get("done")) or str(payload.get("status") or "").lower() in {"success", "completed"}
                    with _OLLAMA_PULL_LOCK:
                        job = _OLLAMA_PULL_JOBS.get(job_id)
                        if not job:
                            continue
                        job["progress"] = progress
                        if done:
                            job["progress"] = 1.0
            with _OLLAMA_PULL_LOCK:
                job = _OLLAMA_PULL_JOBS.get(job_id)
                if job:
                    job["state"] = "succeeded"
                    job["progress"] = 1.0
                    job["completed_at"] = datetime.now(tz=UTC)
        except Exception as exc:
            with _OLLAMA_PULL_LOCK:
                job = _OLLAMA_PULL_JOBS.get(job_id)
                if job:
                    job["state"] = "failed"
                    job["error"] = str(exc)
                    job["completed_at"] = datetime.now(tz=UTC)

    threading.Thread(target=_run, name=f"ollama-pull-{job_id}", daemon=True).start()
    return job_id


def _docker_socket_path() -> str:
    return os.getenv("TCE_DOCKER_SOCKET", "/var/run/docker.sock")


def _docker_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "").strip()
            if message:
                return message
    except Exception:
        pass
    text = (response.text or "").strip()
    if text:
        return text[-1200:]
    return f"docker API returned {response.status_code}"


def _compose_restart_shell_command(*, stack: str, project_root: str) -> str:
    commands: list[str] = []

    def _append(compose_file: str) -> None:
        compose_q = shlex.quote(compose_file)
        commands.append(f"docker compose -f {compose_q} up -d --force-recreate --remove-orphans")

    if stack == "full":
        _append(f"{project_root}/infra/docker-compose.yml")
    elif stack == "lite":
        _append(f"{project_root}/infra/docker-compose.lite.yml")
    else:
        _append(f"{project_root}/infra/docker-compose.yml")
        _append(f"{project_root}/infra/docker-compose.lite.yml")
    backup_dir_q = shlex.quote(f"{project_root}/backups/auto")
    full_compose_q = shlex.quote(f"{project_root}/infra/docker-compose.yml")
    env_file_q = shlex.quote(f"{project_root}/.env")
    snapshot_reason = f"pre_restart_{stack}"
    snapshot_cmd = (
        f"mkdir -p {backup_dir_q}; "
        "stamp=$(date -u +%Y%m%d_%H%M%S); "
        f"out={backup_dir_q}/tce_${{stamp}}_{snapshot_reason}.sql.gz; "
        f"docker compose -f {full_compose_q} up -d postgres >/dev/null; "
        "for i in $(seq 1 40); do "
        f"docker compose -f {full_compose_q} exec -T postgres pg_isready -U postgres -d tce >/dev/null 2>&1 && break; "
        "sleep 1; "
        "done; "
        f"docker compose -f {full_compose_q} exec -T postgres pg_isready -U postgres -d tce >/dev/null; "
        f"docker compose -f {full_compose_q} exec -T postgres pg_dump -U postgres -d tce | gzip -c > \"$out\"; "
        f"cp {env_file_q} {backup_dir_q}/.env_${{stamp}}_{snapshot_reason}.snapshot || true; "
        "echo \"pre-restart snapshot: $out\""
    )
    return "set -euo pipefail; " + snapshot_cmd + "; " + "; ".join(commands)


def _resolve_compose_project_root(client: httpx.Client) -> Path:
    container_id = (os.getenv("HOSTNAME") or "").strip()
    if not container_id:
        raise RuntimeError("cannot resolve current container id from HOSTNAME")
    inspect = client.get(f"/containers/{container_id}/json")
    if inspect.status_code >= 400:
        raise RuntimeError(f"failed to inspect current container: {_docker_error_detail(inspect)}")
    labels = (((inspect.json() or {}).get("Config") or {}).get("Labels") or {})
    compose_workdir = str(labels.get("com.docker.compose.project.working_dir") or "").strip()
    if not compose_workdir:
        raise RuntimeError("cannot resolve docker compose working_dir from container labels")
    compose_path = Path(compose_workdir)
    project_root = compose_path.parent if compose_path.name == "infra" else compose_path
    return project_root


def _start_stack_restart_job_containerized(*, stack: str) -> str:
    job_id = f"tce-stack-restart-{uuid.uuid4().hex[:12]}"
    now = datetime.now(tz=UTC)
    with _STACK_RESTART_LOCK:
        _STACK_RESTART_JOBS[job_id] = {
            "restart_id": job_id,
            "mode": "containerized",
            "state": "queued",
            "started_at": now,
            "completed_at": None,
            "error": None,
            "helper_container": job_id,
        }

    def _run() -> None:
        with _STACK_RESTART_LOCK:
            job = _STACK_RESTART_JOBS.get(job_id)
            if job:
                job["state"] = "running"
        try:
            socket_path = _docker_socket_path()
            if not os.path.exists(socket_path):
                raise RuntimeError(f"docker socket not found at {socket_path}")
            helper_image = (settings.dashboard_stack_restart_helper_image or "docker:27-cli").strip() or "docker:27-cli"
            transport = httpx.HTTPTransport(uds=socket_path)
            with httpx.Client(transport=transport, base_url="http://docker", timeout=10.0) as client:
                project_root = _resolve_compose_project_root(client)
                host_project_root = str(project_root)
                restart_cmd = _compose_restart_shell_command(stack=stack, project_root=host_project_root)
                binds = [f"{host_project_root}:{host_project_root}", f"{socket_path}:/var/run/docker.sock"]
                payload = {
                    "Image": helper_image,
                    "Cmd": ["sh", "-lc", restart_cmd],
                    "WorkingDir": host_project_root,
                    "HostConfig": {"Binds": binds},
                    "Labels": {
                        "com.open_timeline_engine.restart_id": job_id,
                        "com.open_timeline_engine.kind": "dashboard_stack_restart",
                        "com.open_timeline_engine.stack": stack,
                    },
                }

                def _create() -> httpx.Response:
                    return client.post("/containers/create", params={"name": job_id}, json=payload)

                create_resp = _create()
                if create_resp.status_code == 404 and "No such image" in _docker_error_detail(create_resp):
                    pull_resp = client.post("/images/create", params={"fromImage": helper_image})
                    if pull_resp.status_code >= 400:
                        raise RuntimeError(f"failed to pull helper image '{helper_image}': {_docker_error_detail(pull_resp)}")
                    create_resp = _create()
                if create_resp.status_code >= 400:
                    raise RuntimeError(f"failed to create restart helper container: {_docker_error_detail(create_resp)}")
                created_id = str((create_resp.json() or {}).get("Id") or "").strip()
                if not created_id:
                    raise RuntimeError("docker did not return helper container id")
                start_resp = client.post(f"/containers/{created_id}/start")
                if start_resp.status_code >= 400:
                    raise RuntimeError(f"failed to start restart helper container: {_docker_error_detail(start_resp)}")
                with _STACK_RESTART_LOCK:
                    job = _STACK_RESTART_JOBS.get(job_id)
                    if job:
                        job["helper_container"] = created_id
        except Exception as exc:
            with _STACK_RESTART_LOCK:
                job = _STACK_RESTART_JOBS.get(job_id)
                if job:
                    job["state"] = "failed"
                    job["error"] = str(exc)
                    job["completed_at"] = datetime.now(tz=UTC)

    threading.Thread(target=_run, name=f"stack-restart-containerized-{job_id}", daemon=True).start()
    return job_id


def _refresh_stack_restart_job_from_docker(restart_id: str) -> dict[str, Any] | None:
    socket_path = _docker_socket_path()
    if not os.path.exists(socket_path):
        return None
    transport = httpx.HTTPTransport(uds=socket_path)
    with httpx.Client(transport=transport, base_url="http://docker", timeout=5.0) as client:
        inspect = client.get(f"/containers/{restart_id}/json")
        if inspect.status_code == 404:
            return None
        if inspect.status_code >= 400:
            raise RuntimeError(f"failed to inspect restart helper container: {_docker_error_detail(inspect)}")
        payload = inspect.json() or {}
        state_data = payload.get("State") or {}
        raw_status = str(state_data.get("Status") or "unknown").strip().lower()
        started_at = _parse_dt(state_data.get("StartedAt")) or datetime.now(tz=UTC)
        completed_at = _parse_dt(state_data.get("FinishedAt")) if raw_status in {"exited", "dead"} else None
        exit_code = _safe_int(state_data.get("ExitCode"))
        state = "running"
        error: str | None = None
        if raw_status in {"created", "running", "restarting", "paused"}:
            state = "running"
        elif raw_status in {"exited", "dead"}:
            if exit_code == 0:
                state = "completed"
            else:
                state = "failed"
                logs_resp = client.get(
                    f"/containers/{restart_id}/logs",
                    params={
                        "stdout": 1,
                        "stderr": 1,
                        "tail": max(20, int(settings.dashboard_stack_restart_log_tail_lines)),
                    },
                )
                if logs_resp.status_code < 400:
                    logs_text = (logs_resp.text or "").strip()
                    if logs_text:
                        error = logs_text[-2000:]
                if not error:
                    error = f"restart helper exited with code {exit_code}"
        else:
            state = raw_status or "unknown"
        return {
            "restart_id": restart_id,
            "mode": "containerized",
            "state": state,
            "started_at": started_at,
            "completed_at": completed_at,
            "error": error,
        }


def _start_stack_restart_job(*, stack: str) -> str:
    script_candidates = [
        Path(__file__).resolve().parents[3] / "scripts" / "install.sh",
        Path(__file__).resolve().parents[4] / "scripts" / "install.sh",
        Path.cwd() / "scripts" / "install.sh",
    ]
    script = next((candidate for candidate in script_candidates if candidate.exists()), None)
    if script is None and settings.dashboard_stack_restart_containerized_enabled:
        return _start_stack_restart_job_containerized(stack=stack)

    job_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    with _STACK_RESTART_LOCK:
        _STACK_RESTART_JOBS[job_id] = {
            "restart_id": job_id,
            "mode": "script",
            "state": "queued",
            "started_at": now,
            "completed_at": None,
            "error": None,
        }

    def _run() -> None:
        with _STACK_RESTART_LOCK:
            job = _STACK_RESTART_JOBS.get(job_id)
            if job:
                job["state"] = "running"
        try:
            if script is None:
                raise RuntimeError(
                    f"restart endpoint unavailable in current runtime; run './scripts/install.sh restart {stack} --yes' from repo root"
                )
            result = subprocess.run(
                [str(script), "restart", stack, "--yes"],
                cwd=str(script.parent.parent),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "restart failed").strip()
                raise RuntimeError(err[-1200:])
            with _STACK_RESTART_LOCK:
                job = _STACK_RESTART_JOBS.get(job_id)
                if job:
                    job["state"] = "completed"
                    job["completed_at"] = datetime.now(tz=UTC)
        except Exception as exc:
            with _STACK_RESTART_LOCK:
                job = _STACK_RESTART_JOBS.get(job_id)
                if job:
                    job["state"] = "failed"
                    job["error"] = str(exc)
                    job["completed_at"] = datetime.now(tz=UTC)

    threading.Thread(target=_run, name=f"stack-restart-{job_id}", daemon=True).start()
    return job_id


def _clean_consumer(value: str | None) -> str:
    if not value:
        return ""
    parts = value.split(":")
    if parts and parts[0] in {"bearer", "mtls"} and len(parts) >= 2:
        return parts[1].strip() or value
    return value


def _agent_label(value: str, fallback: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return fallback
    base = cleaned.split("-")[0].replace("_", " ").strip()
    return base.capitalize() if base else fallback


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _latest_activity_ts(
    db: Session,
    *,
    workspace_id: str,
    role: str,
    user_id: str,
    consumer_id: str,
) -> datetime | None:
    last_event = None
    try:
        last_event = db.execute(
            text(
                """
                SELECT MAX(ts) AS last_seen
                FROM events
                WHERE (
                    (context->>'_tce_workspace' IS NULL OR context->>'_tce_workspace' = :workspace_id)
                    AND (
                        actor = :user_id
                        OR context->>'user' = :user_id
                        OR context->>'consumer' = :consumer_id
                        OR context->>'role' = :role_name
                    )
                )
                """
            ),
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "consumer_id": consumer_id,
                "role_name": role,
            },
        ).scalar()
    except Exception:
        last_event = None

    last_audit = None
    try:
        if consumer_id:
            last_audit = db.execute(
                text(
                    """
                    SELECT MAX(ts) AS last_seen
                    FROM audit_log
                    WHERE consumer ILIKE :consumer_match
                    """
                ),
                {"consumer_match": f"%{consumer_id}%"},
            ).scalar()
    except Exception:
        last_audit = None

    parsed_event = _parse_dt(last_event)
    parsed_audit = _parse_dt(last_audit)
    if parsed_event and parsed_audit:
        return parsed_event if parsed_event >= parsed_audit else parsed_audit
    return parsed_event or parsed_audit


def _resolve_dashboard_role(
    db: Session,
    *,
    workspace_id: str,
    role: str,
    user_id: str,
    consumer_id: str,
    memberships: list[dict[str, Any]],
    now: datetime,
) -> DashboardAgentRole:
    membership = next(
        (
            row
            for row in memberships
            if str(row.get("user_id", "")) == user_id
            or role in str(row.get("user_id", "")).lower()
        ),
        None,
    )
    membership_ts = _parse_dt(membership.get("created_at")) if isinstance(membership, dict) else None
    activity_ts = _latest_activity_ts(
        db,
        workspace_id=workspace_id,
        role=role,
        user_id=user_id,
        consumer_id=consumer_id,
    )
    last_seen = activity_ts or membership_ts
    active_cutoff = now - timedelta(minutes=30)

    if last_seen and last_seen >= active_cutoff:
        status = "active"
        source = "event_or_audit_activity"
    elif membership:
        status = "registered"
        source = "team_membership"
    elif user_id or consumer_id:
        status = "registered"
        source = "runtime_env"
    else:
        status = "not_seen"
        source = "unknown"

    return DashboardAgentRole(
        user_id=user_id or "unknown",
        consumer_id=consumer_id or "unknown",
        label=_agent_label(user_id or consumer_id, role.capitalize()),
        status=status,
        last_seen_ts=last_seen,
        source=source,
    )


def _runtime_mode_info(db: Session) -> RuntimeModeInfo:
    try:
        mode_cfg = get_runtime_mode(db)
        return RuntimeModeInfo(mode=mode_cfg.mode.value, clone_enabled=mode_cfg.clone_enabled)
    except Exception:
        return RuntimeModeInfo(mode="unknown", clone_enabled=False)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _score_band(score: int) -> str:
    if score <= 39:
        return "emerging"
    if score <= 64:
        return "developing"
    if score <= 84:
        return "advanced"
    return "human_like"


def _classify_long_term_goal(
    *,
    source: str,
    status: str,
    created_at: datetime | None,
    affective_scores: dict[str, Any],
    now: datetime,
) -> tuple[bool, str, int]:
    age_days = 0
    if created_at:
        age_days = max(0, int((now - created_at).total_seconds() // 86400))
    temporal = _as_dict(affective_scores.get("temporal", {}))
    dream = _to_float(temporal.get("dream"), 0.0)
    rehearsal_count = _safe_int(temporal.get("rehearsal_count"))
    terminal = status.lower() in {"done", "dropped", "abandoned"}

    if dream >= 0.30:
        return True, "dream>=0.30", age_days
    if age_days >= 21 and rehearsal_count >= 6:
        return True, "age>=21_and_rehearsal>=6", age_days
    if source == "user_objective" and age_days >= 14 and not terminal:
        return True, "user_objective_age>=14_not_terminal", age_days
    return False, "none", age_days


def _compute_clone_readiness(
    db: Session,
    *,
    workspace_id: str,
) -> tuple[int, dict[str, Any]]:
    total_situation_types = 12
    stats = db.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM decision_observations WHERE workspace_id = :ws) AS obs_count,
              (SELECT COUNT(DISTINCT situation_type) FROM decision_observations WHERE workspace_id = :ws) AS distinct_types
            """
        ),
        {"ws": workspace_id},
    ).mappings().first()
    obs_count = _safe_int(stats["obs_count"]) if stats else 0
    distinct_types = _safe_int(stats["distinct_types"]) if stats else 0
    observation_count_score = min(25, int(obs_count * 25 / 100))

    fp_row = db.execute(
        text(
            """
            SELECT fingerprint
            FROM behavioral_fingerprints
            WHERE workspace_id = :ws
            ORDER BY last_updated_at DESC LIMIT 1
            """
        ),
        {"ws": workspace_id},
    ).fetchone()
    fingerprint_confidence = 0
    if fp_row and fp_row[0]:
        fp_data = fp_row[0] if isinstance(fp_row[0], dict) else {}
        non_default = 0
        total_dims = 0
        for category, defaults in DEFAULT_FINGERPRINT.items():
            if not isinstance(defaults, dict):
                continue
            for key, default_val in defaults.items():
                total_dims += 1
                stored_cat = fp_data.get(category, {})
                if isinstance(stored_cat, dict) and stored_cat.get(key) != default_val:
                    non_default += 1
        fingerprint_confidence = int(non_default / max(total_dims, 1) * 25) if total_dims else 0

    pattern_coverage = min(25, int(distinct_types / total_situation_types * 25))

    recent_rows = db.execute(
        text(
            """
            SELECT outcome_sentiment FROM decision_observations
            WHERE workspace_id = :ws
            ORDER BY ts DESC LIMIT 20
            """
        ),
        {"ws": workspace_id},
    ).fetchall()
    if recent_rows:
        positive_neutral = sum(1 for row in recent_rows if row[0] in ("positive", "neutral", None))
        recent_consistency = int(positive_neutral / len(recent_rows) * 25)
    else:
        recent_consistency = 0

    score = observation_count_score + fingerprint_confidence + pattern_coverage + recent_consistency
    return score, {
        "observation_count_score": observation_count_score,
        "fingerprint_confidence": fingerprint_confidence,
        "pattern_coverage": pattern_coverage,
        "recent_consistency": recent_consistency,
        "observation_count": obs_count,
        "situation_types_covered": distinct_types,
    }


def _build_goal_intelligence(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> DashboardGoalsIntelligenceResponse:
    now = datetime.now(tz=UTC)
    rows = db.execute(
        text(
            """
            SELECT
              id,
              title,
              description,
              source,
              status,
              priority_score,
              confidence,
              selection_score,
              parent_goal_id,
              evidence_event_ids,
              affective_scores,
              created_at
            FROM autonomy_goals
            WHERE workspace_id = :ws
              AND user_id = :uid
              AND session_id = :sid
            ORDER BY selection_score DESC, updated_at DESC
            LIMIT 200
            """
        ),
        {"ws": workspace_id, "uid": user_id, "sid": session_id},
    ).mappings().all()

    goals: list[DashboardGoalIntelligenceItem] = []
    goal_goal_edges = 0
    goal_event_edges = 0
    goal_emotion_edges = 0
    scored_refs: list[tuple[str, float]] = []

    for row in rows:
        goal_id = str(row["id"])
        evidence_ids_raw = row.get("evidence_event_ids") or []
        evidence_ids = [str(value) for value in evidence_ids_raw] if isinstance(evidence_ids_raw, list) else []
        affective_scores = _as_dict(row.get("affective_scores", {}))
        emotional = _as_dict(affective_scores.get("emotional", {}))
        top_emotions_pairs = sorted(
            (
                (name, max(0.0, min(1.0, _to_float(value))))
                for name, value in emotional.items()
                if isinstance(name, str)
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        top_emotions = [GoalEmotionValue(name=name, value=round(value, 4)) for name, value in top_emotions_pairs]

        long_term, long_term_reason, age_days = _classify_long_term_goal(
            source=str(row.get("source") or "open_discovery"),
            status=str(row.get("status") or "candidate"),
            created_at=row.get("created_at"),
            affective_scores=affective_scores,
            now=now,
        )
        relations: list[GoalRelationEdge] = []

        for event_id in evidence_ids:
            relations.append(
                GoalRelationEdge(
                    source_goal_id=goal_id,
                    target_id=event_id,
                    target_type="event",
                    relation_type="evidence",
                    weight=1.0,
                    meta={},
                )
            )
            goal_event_edges += 1

        parent_goal_id = row.get("parent_goal_id")
        if parent_goal_id:
            relations.append(
                GoalRelationEdge(
                    source_goal_id=goal_id,
                    target_id=str(parent_goal_id),
                    target_type="goal",
                    relation_type="parent",
                    weight=1.0,
                    meta={},
                )
            )
            goal_goal_edges += 1

        for emotion in top_emotions:
            if emotion.value <= 0.0:
                continue
            relations.append(
                GoalRelationEdge(
                    source_goal_id=goal_id,
                    target_id=emotion.name,
                    target_type="emotion",
                    relation_type="affective",
                    weight=emotion.value,
                    meta={},
                )
            )
            goal_emotion_edges += 1

        selection_score = round(_to_float(row.get("selection_score")), 4)
        scored_refs.append((goal_id, selection_score))
        goals.append(
            DashboardGoalIntelligenceItem(
                id=goal_id,
                title=str(row.get("title") or ""),
                description=str(row.get("description") or ""),
                source=str(row.get("source") or "open_discovery"),
                status=str(row.get("status") or "candidate"),
                selection_score=selection_score,
                priority_score=round(_to_float(row.get("priority_score")), 4),
                confidence=round(_to_float(row.get("confidence")), 4),
                age_days=age_days,
                long_term=long_term,
                long_term_reason=long_term_reason,
                top_emotions=top_emotions,
                affective_scores=affective_scores,
                relation_count=len(relations),
                relations=relations,
            )
        )

    # Add light goal-to-goal similarity edges from adjacent scores.
    by_goal_id = {goal.id: goal for goal in goals}
    for index in range(len(scored_refs) - 1):
        source_id, source_score = scored_refs[index]
        target_id, target_score = scored_refs[index + 1]
        weight = round(max(0.0, 1.0 - abs(source_score - target_score)), 4)
        if weight < 0.65:
            continue
        source_goal = by_goal_id.get(source_id)
        if not source_goal:
            continue
        source_goal.relations.append(
            GoalRelationEdge(
                source_goal_id=source_id,
                target_id=target_id,
                target_type="goal",
                relation_type="similarity",
                weight=weight,
                meta={"score_distance": round(abs(source_score - target_score), 4)},
            )
        )
        source_goal.relation_count = len(source_goal.relations)
        goal_goal_edges += 1

    long_term_count = sum(1 for goal in goals if goal.long_term)
    return DashboardGoalsIntelligenceResponse(
        session_id=session_id,
        generated_at=now,
        summary=DashboardGoalsIntelligenceSummary(
            total_goals=len(goals),
            long_term_goals=long_term_count,
            goal_event_edges=goal_event_edges,
            goal_emotion_edges=goal_emotion_edges,
            goal_goal_edges=goal_goal_edges,
        ),
        goals=goals,
    )


def _compute_human_score(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> DashboardHumanScoreResponse:
    now = datetime.now(tz=UTC)
    clone_score, clone_inputs = _compute_clone_readiness(db, workspace_id=workspace_id)
    goal_intelligence = _build_goal_intelligence(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
    )

    execution_stats = db.execute(
        text(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE state = 'succeeded') AS succeeded,
              COALESCE(SUM(CASE WHEN attempt > 1 THEN attempt - 1 ELSE 0 END), 0) AS retry_units,
              COUNT(*) FILTER (WHERE action_kind IN ('edit', 'write', 'delete', 'command', 'shell')) AS mutating_total,
              COUNT(*) FILTER (
                WHERE action_kind IN ('edit', 'write', 'delete', 'command', 'shell')
                  AND requires_permit = true
                  AND permit_id IS NULL
              ) AS permit_misses
            FROM directive_executions
            WHERE workspace_id = :ws
              AND user_id = :uid
              AND session_id = :sid
            """
        ),
        {"ws": workspace_id, "uid": user_id, "sid": session_id},
    ).mappings().first() or {}
    total_exec = max(0, _safe_int(execution_stats.get("total")))
    succeeded_exec = max(0, _safe_int(execution_stats.get("succeeded")))
    retry_units = max(0, _safe_int(execution_stats.get("retry_units")))
    mutating_total = max(0, _safe_int(execution_stats.get("mutating_total")))
    permit_misses = max(0, _safe_int(execution_stats.get("permit_misses")))

    takeover_row = db.execute(
        text(
            """
            SELECT continuity_violation_count, goal_queue_size, pending_directive_count, retry_backlog_count
            FROM takeover_sessions
            WHERE workspace_id = :ws
              AND user_id = :uid
              AND session_id = :sid
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"ws": workspace_id, "uid": user_id, "sid": session_id},
    ).mappings().first() or {}
    continuity_violations = max(0, _safe_int(takeover_row.get("continuity_violation_count")))
    queue_size = max(0, _safe_int(takeover_row.get("goal_queue_size")))
    pending_directives = max(0, _safe_int(takeover_row.get("pending_directive_count")))
    retry_backlog = max(0, _safe_int(takeover_row.get("retry_backlog_count")))

    success_rate = (succeeded_exec / total_exec) if total_exec else 0.5
    retry_pressure = min(1.0, (retry_units / max(total_exec, 1)))
    continuity_penalty = min(1.0, continuity_violations / 10.0)
    permit_discipline = 1.0 if mutating_total == 0 else max(0.0, 1.0 - (permit_misses / mutating_total))
    execution_quality = max(
        0.0,
        min(
            100.0,
            100.0
            * (
                0.40 * success_rate
                + 0.25 * (1.0 - retry_pressure)
                + 0.20 * (1.0 - continuity_penalty)
                + 0.15 * permit_discipline
            ),
        ),
    )

    goals = goal_intelligence.goals
    total_goals = len(goals)
    completed_goals = sum(1 for goal in goals if goal.status.lower() in {"done"})
    completion_ratio = (completed_goals / total_goals) if total_goals else 0.0
    total_edges = (
        goal_intelligence.summary.goal_event_edges
        + goal_intelligence.summary.goal_emotion_edges
        + goal_intelligence.summary.goal_goal_edges
    )
    relation_density = min(1.0, (total_edges / max(total_goals, 1)) / 4.0) if total_goals else 0.0
    long_term_goals = [goal for goal in goals if goal.long_term]
    long_term_done = sum(1 for goal in long_term_goals if goal.status.lower() in {"done"})
    long_term_progress = (long_term_done / len(long_term_goals)) if long_term_goals else 0.5
    queue_stability = 1.0
    if queue_size == 0:
        queue_stability = 0.65
    elif queue_size > 8:
        queue_stability = max(0.2, 1.0 - ((queue_size - 8) / 12.0))
    queue_stability = max(0.0, queue_stability - min(0.4, continuity_violations * 0.05) - min(0.2, retry_backlog * 0.03))
    goal_coherence = max(
        0.0,
        min(
            100.0,
            100.0
            * (
                0.35 * long_term_progress
                + 0.25 * relation_density
                + 0.25 * completion_ratio
                + 0.15 * queue_stability
            ),
        ),
    )

    emotion_instability_values: list[float] = []
    overwhelm_values: list[float] = []
    identity_values: list[float] = []
    for goal in goals:
        affective = _as_dict(getattr(goal, "affective_scores", {}))
        emotional = _as_dict(affective.get("emotional", {}))
        meta = _as_dict(affective.get("meta", {}))
        human = _as_dict(affective.get("human", {}))
        pain = max(0.0, min(1.0, _to_float(emotional.get("pain"), 0.0)))
        happy = max(0.0, min(1.0, _to_float(emotional.get("happy"), 0.0)))
        anger = max(0.0, min(1.0, _to_float(emotional.get("anger"), 0.0)))
        anxiety = max(0.0, min(1.0, _to_float(emotional.get("anxiety"), 0.0)))
        emotion_instability_values.append((abs(pain - happy) + anger + anxiety) / 3.0)
        overwhelm_values.append(max(0.0, min(1.0, _to_float(meta.get("overwhelm"), 0.0))))
        identity_values.append(max(0.0, min(1.0, _to_float(human.get("identity"), 0.35))))

    avg_emotion_instability = (
        sum(emotion_instability_values) / len(emotion_instability_values)
        if emotion_instability_values
        else 0.4
    )
    emotion_stability = max(0.0, min(1.0, 1.0 - avg_emotion_instability))
    avg_overwhelm = sum(overwhelm_values) / len(overwhelm_values) if overwhelm_values else 0.35
    overwhelm_control = max(0.0, min(1.0, 1.0 - avg_overwhelm))
    identity_alignment = sum(identity_values) / len(identity_values) if identity_values else 0.35

    observation_sentiment = db.execute(
        text(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE outcome_sentiment IN ('positive', 'neutral') OR outcome_sentiment IS NULL) AS positive_neutral
            FROM (
              SELECT outcome_sentiment
              FROM decision_observations
              WHERE workspace_id = :ws
              ORDER BY ts DESC
              LIMIT 80
            ) recent_obs
            """
        ),
        {"ws": workspace_id},
    ).mappings().first() or {}
    obs_total = max(0, _safe_int(observation_sentiment.get("total")))
    obs_positive_neutral = max(0, _safe_int(observation_sentiment.get("positive_neutral")))
    positive_signal_ratio = (obs_positive_neutral / obs_total) if obs_total else 0.5

    affective_alignment = max(
        0.0,
        min(
            100.0,
            100.0
            * (
                0.30 * emotion_stability
                + 0.25 * overwhelm_control
                + 0.25 * positive_signal_ratio
                + 0.20 * identity_alignment
            ),
        ),
    )

    raw_human_score = max(
        0.0,
        min(
            100.0,
            0.35 * clone_score
            + 0.25 * execution_quality
            + 0.20 * goal_coherence
            + 0.20 * affective_alignment,
        ),
    )
    autonomy_maturity = max(
        0.0,
        min(
            1.0,
            0.45 * success_rate
            + 0.20 * permit_discipline
            + 0.20 * (1.0 - retry_pressure)
            + 0.10 * (1.0 - continuity_penalty)
            + 0.05 * (1.0 if pending_directives == 0 else 0.0),
        ),
    )
    maturity_bonus = int(round(18.0 * autonomy_maturity)) if total_exec >= 8 else 0
    human_score = int(round(max(0.0, min(100.0, raw_human_score + maturity_bonus))))
    band = _score_band(human_score)

    inputs = {
        "clone": clone_inputs,
        "execution": {
            "total_directives": total_exec,
            "succeeded_directives": succeeded_exec,
            "retry_pressure": round(retry_pressure, 4),
            "continuity_penalty": round(continuity_penalty, 4),
            "permit_discipline": round(permit_discipline, 4),
            "pending_directives": pending_directives,
            "autonomy_maturity": round(autonomy_maturity, 4),
            "maturity_bonus": maturity_bonus,
        },
        "goals": {
            "total_goals": total_goals,
            "long_term_goals": goal_intelligence.summary.long_term_goals,
            "completion_ratio": round(completion_ratio, 4),
            "relation_density": round(relation_density, 4),
            "long_term_progress": round(long_term_progress, 4),
            "queue_stability": round(queue_stability, 4),
        },
        "affective": {
            "emotion_stability": round(emotion_stability, 4),
            "overwhelm_control": round(overwhelm_control, 4),
            "positive_signal_ratio": round(positive_signal_ratio, 4),
            "identity_alignment": round(identity_alignment, 4),
        },
    }

    return DashboardHumanScoreResponse(
        session_id=session_id,
        score=human_score,
        band=band,
        subscores=DashboardHumanScoreSubscores(
            clone_readiness=round(clone_score, 2),
            execution_quality=round(execution_quality, 2),
            goal_coherence=round(goal_coherence, 2),
            affective_alignment=round(affective_alignment, 2),
        ),
        inputs=inputs,
        computed_at=now,
        snapshot_id=None,
    )


def _persist_human_score_snapshot(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    response: DashboardHumanScoreResponse,
) -> str:
    snapshot_id = str(uuid.uuid4())
    created_at = response.computed_at
    db.execute(
        text(
            """
            INSERT INTO dashboard_human_score_snapshots
              (id, session_id, workspace_id, user_id, score, band, subscores_json, inputs_json, created_at)
            VALUES
              (:id, :sid, :ws, :uid, :score, :band, CAST(:subscores AS jsonb), CAST(:inputs AS jsonb), :created_at)
            """
        ),
        {
            "id": snapshot_id,
            "sid": response.session_id,
            "ws": workspace_id,
            "uid": user_id,
            "score": response.score,
            "band": response.band,
            "subscores": json.dumps(response.subscores.model_dump()),
            "inputs": json.dumps(response.inputs),
            "created_at": created_at,
        },
    )
    db.commit()
    return snapshot_id


def _latest_human_score_snapshot(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> DashboardHumanScoreResponse | None:
    row = db.execute(
        text(
            """
            SELECT id, score, band, subscores_json, inputs_json, created_at
            FROM dashboard_human_score_snapshots
            WHERE workspace_id = :ws
              AND user_id = :uid
              AND session_id = :sid
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"ws": workspace_id, "uid": user_id, "sid": session_id},
    ).mappings().first()
    if not row:
        return None
    subscores_data = _as_dict(row.get("subscores_json"))
    inputs_data = _as_dict(row.get("inputs_json"))
    created_at = row.get("created_at")
    if not isinstance(created_at, datetime):
        created_at = datetime.now(tz=UTC)
    return DashboardHumanScoreResponse(
        session_id=session_id,
        score=max(0, min(100, _safe_int(row.get("score")))),
        band=str(row.get("band") or _score_band(_safe_int(row.get("score")))),
        subscores=DashboardHumanScoreSubscores(
            clone_readiness=round(_to_float(subscores_data.get("clone_readiness"), 0.0), 2),
            execution_quality=round(_to_float(subscores_data.get("execution_quality"), 0.0), 2),
            goal_coherence=round(_to_float(subscores_data.get("goal_coherence"), 0.0), 2),
            affective_alignment=round(_to_float(subscores_data.get("affective_alignment"), 0.0), 2),
        ),
        inputs=inputs_data,
        computed_at=created_at,
        snapshot_id=str(row.get("id")),
    )


_docker_stats_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_DOCKER_CACHE_TTL = 30.0  # seconds
_DOCKER_STATS_REQUEST_TIMEOUT_SECONDS = 0.9
_DOCKER_STATS_OVERALL_BUDGET_SECONDS = 1.2
_DOCKER_STATS_MAX_TARGETS = 12


def _parse_container_stats(item: dict[str, Any], stats: dict[str, Any], project: str, service_name: str, container_name: str) -> dict[str, Any]:
    cpu_now = _safe_int(((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage"))
    cpu_prev = _safe_int(
        ((stats.get("precpu_stats") or {}).get("cpu_usage") or {}).get("total_usage")
    )
    sys_now = _safe_int((stats.get("cpu_stats") or {}).get("system_cpu_usage"))
    sys_prev = _safe_int((stats.get("precpu_stats") or {}).get("system_cpu_usage"))
    cpu_delta = cpu_now - cpu_prev
    sys_delta = sys_now - sys_prev
    online_cpus = _safe_int((stats.get("cpu_stats") or {}).get("online_cpus"))
    if online_cpus <= 0:
        percpu = (((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("percpu_usage") or [])
        online_cpus = len(percpu) or 1
    cpu_percent = (
        round((cpu_delta / sys_delta) * online_cpus * 100.0, 2)
        if cpu_delta > 0 and sys_delta > 0
        else 0.0
    )
    memory_stats = stats.get("memory_stats") or {}
    memory_usage = _safe_int(memory_stats.get("usage"))
    memory_limit = _safe_int(memory_stats.get("limit"))
    memory_percent = round((memory_usage / memory_limit) * 100.0, 2) if memory_limit > 0 else 0.0
    networks = stats.get("networks") or {}
    net_in = 0
    net_out = 0
    for net_stats in networks.values():
        if not isinstance(net_stats, dict):
            continue
        net_in += _safe_int(net_stats.get("rx_bytes"))
        net_out += _safe_int(net_stats.get("tx_bytes"))
    blk_rows = ((stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or [])
    block_in = 0
    block_out = 0
    for row in blk_rows:
        if not isinstance(row, dict):
            continue
        op = str(row.get("op", "")).lower()
        value = _safe_int(row.get("value"))
        if op == "read":
            block_in += value
        elif op == "write":
            block_out += value
    return {
        "project": project,
        "service": service_name,
        "container_name": container_name,
        "cpu_percent": cpu_percent,
        "memory_usage_bytes": memory_usage,
        "memory_limit_bytes": memory_limit,
        "memory_percent": memory_percent,
        "network_rx_bytes": net_in,
        "network_tx_bytes": net_out,
        "block_read_bytes": block_in,
        "block_write_bytes": block_out,
        "pids": _safe_int((stats.get("pids_stats") or {}).get("current")),
    }


def _docker_service_stats() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import time

    now = time.monotonic()
    if _docker_stats_cache["data"] is not None and (now - _docker_stats_cache["ts"]) < _DOCKER_CACHE_TTL:
        return _docker_stats_cache["data"]

    socket_path = os.getenv("TCE_DOCKER_SOCKET", "/var/run/docker.sock")
    if not os.path.exists(socket_path):
        return [], {
            "available": False,
            "source": "docker_engine_api",
            "error": f"docker socket not found at {socket_path}",
            "socket_path": socket_path,
        }

    project_filters = {
        value
        for value in (
            os.getenv("COMPOSE_PROJECT_NAME", "").strip(),
            "open-timeline-engine",
            "open-timeline-engine-lite",
        )
        if value
    }

    services: list[dict[str, Any]] = []
    target_count = 0
    timed_out_targets = 0
    try:
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(
            transport=transport,
            base_url="http://docker",
            timeout=_DOCKER_STATS_REQUEST_TIMEOUT_SECONDS + 0.35,
        ) as client:
            containers_resp = client.get("/containers/json", params={"all": 0})
            containers_resp.raise_for_status()
            containers = containers_resp.json()

            targets: list[tuple[str, str, str, str]] = []
            for item in containers:
                labels = item.get("Labels", {}) or {}
                project = str(labels.get("com.docker.compose.project", "")).strip()
                container_name = str((item.get("Names") or [""])[0]).lstrip("/")
                service_name = str(labels.get("com.docker.compose.service", "")).strip() or container_name

                if project_filters and project and project not in project_filters:
                    continue
                if not project and project_filters and not any(
                    marker in container_name for marker in project_filters
                ):
                    continue

                container_id = str(item.get("Id", "")).strip()
                if not container_id:
                    continue
                targets.append((container_id, project, service_name, container_name))

            def _target_priority(row: tuple[str, str, str, str]) -> tuple[int, str, str]:
                _cid, proj, svc, _cname = row
                if proj in project_filters:
                    return (0, proj, svc)
                return (1, proj, svc)

            targets.sort(key=_target_priority)
            if len(targets) > _DOCKER_STATS_MAX_TARGETS:
                targets = targets[:_DOCKER_STATS_MAX_TARGETS]
            target_count = len(targets)

            def _fetch_stats(cid: str) -> dict[str, Any]:
                resp = client.get(
                    f"/containers/{cid}/stats",
                    params={"stream": "false"},
                    timeout=_DOCKER_STATS_REQUEST_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                return resp.json()

            futures = {
                    _DOCKER_STATS_EXECUTOR.submit(_fetch_stats, cid): (cid, proj, svc, cname)
                    for cid, proj, svc, cname in targets
            }
            if futures:
                remaining_budget = max(
                    0.05,
                    _DOCKER_STATS_OVERALL_BUDGET_SECONDS - (time.monotonic() - now),
                )
                deadline = time.monotonic() + remaining_budget
                pending = set(futures.keys())
                while pending and time.monotonic() < deadline:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    try:
                        fut = next(as_completed(pending, timeout=timeout))
                    except FuturesTimeoutError:
                        break
                    pending.remove(fut)
                    cid, proj, svc, cname = futures[fut]
                    try:
                        stats = fut.result()
                        services.append(_parse_container_stats({}, stats, proj, svc, cname))
                    except Exception:
                        pass
                timed_out_targets = len(pending)

    except Exception as exc:
        return [], {
            "available": False,
            "source": "docker_engine_api",
            "error": str(exc),
            "socket_path": socket_path,
            "project_filters": sorted(project_filters),
            "target_count": target_count,
            "collected_count": len(services),
        }

    services.sort(key=lambda row: (str(row.get("project", "")), str(row.get("service", ""))))
    result = (services, {
        "available": True,
        "source": "docker_engine_api",
        "error": None,
        "socket_path": socket_path,
        "project_filters": sorted(project_filters),
        "target_count": target_count,
        "collected_count": len(services),
        "timed_out_targets": timed_out_targets,
        "max_targets": _DOCKER_STATS_MAX_TARGETS,
        "request_timeout_ms": int(_DOCKER_STATS_REQUEST_TIMEOUT_SECONDS * 1000),
        "collection_budget_ms": int(_DOCKER_STATS_OVERALL_BUDGET_SECONDS * 1000),
    })
    _docker_stats_cache["data"] = result
    _docker_stats_cache["ts"] = time.monotonic()
    return result


@app.middleware("http")
async def apply_rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/v1") and path not in {"/v1/health", "/v1/metrics"}:
        consumer = request.headers.get("X-TCE-Consumer", "anonymous")
        if path.startswith("/v1/takeover/step"):
            limiter_group = "takeover_step"
            limiter = takeover_step_rate_limiter
            limit_per_minute = settings.takeover_step_rate_limit_requests_per_minute
        elif path.startswith("/v1/takeover/permit/") or path.startswith("/v1/takeover/execution/"):
            limiter_group = "takeover_lifecycle"
            limiter = takeover_lifecycle_rate_limiter
            limit_per_minute = settings.takeover_lifecycle_rate_limit_requests_per_minute
        else:
            limiter_group = "default"
            limiter = rate_limiter
            limit_per_minute = settings.rate_limit_requests_per_minute
        limiter_key = f"{consumer}:{limiter_group}" if consumer != "anonymous" else f"{consumer}:{limiter_group}:{path}"
        decision = limiter.allow(limiter_key)
        if not decision.allowed:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(max(1, decision.reset_in_seconds))},
                content={
                    "error": "rate_limited",
                    "consumer": consumer,
                    "limiter_key": limiter_key,
                    "limiter_group": limiter_group,
                    "limit_per_minute": limit_per_minute,
                    "retry_after_seconds": decision.reset_in_seconds,
                },
            )
    return await call_next(request)


def _event_hash(event: EventEnvelope) -> str:
    digest_input = {
        "schema_version": event.schema_version,
        "ts": event.ts.isoformat(),
        "actor": event.actor,
        "source": event.source,
        "domain": event.domain,
        "task_type": event.task_type,
        "event_type": event.event_type.value,
        "title": event.title,
        "payload": event.payload,
    }
    blob = json.dumps(digest_input, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _authority_level(value: str | None) -> str:
    normalized = str(value or "incidental").strip().lower()
    if normalized in {"policy", "standard", "preferred", "observed", "incidental"}:
        return normalized
    return "incidental"


def _authority_score(level: str) -> float:
    return {
        "policy": 1.0,
        "standard": 0.9,
        "preferred": 0.75,
        "observed": 0.5,
        "incidental": 0.2,
    }.get(level, 0.2)


def _stability_score(db: Session, *, workspace_id: str, user_id: str, domain: str, task_type: str) -> float:
    since = datetime.now(tz=UTC) - timedelta(days=90)
    count = db.execute(
        text(
            """
            SELECT COUNT(1)
            FROM events
            WHERE domain = :domain
              AND task_type = :task_type
              AND (context->>'_tce_workspace') = :workspace_id
              AND (context->>'_tce_owner') = :user_id
              AND ts >= :since
            """
        ),
        {
            "domain": domain,
            "task_type": task_type,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "since": since,
        },
    ).scalar()
    try:
        value = int(count or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log1p(float(value)) / math.log(12.0)))


def _ensure_episode_for_event(
    db: Session,
    *,
    event_id: UUID,
    event: EventEnvelope,
    workspace_id: str,
    user_id: str,
    authority_level: str,
) -> UUID:
    session_id = "default"
    if isinstance(event.context, dict):
        session_id = str(event.context.get("session_id") or "default")
    goal = (event.title or event.task_type or "Untitled goal").strip()[:240]
    context_text = ""
    if isinstance(event.payload, dict):
        context_text = str(event.payload.get("summary") or event.payload.get("context") or "")[:1000]
    existing = db.execute(
        select(Episode)
        .where(
            Episode.workspace_id == workspace_id,
            Episode.user_id == user_id,
            Episode.session_id == session_id,
            Episode.goal == goal,
            Episode.status.in_(["open", "in_progress"]),
        )
        .order_by(Episode.updated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    now = datetime.now(tz=UTC)
    authority_score = _authority_score(authority_level)
    stability_score = _stability_score(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        domain=event.domain,
        task_type=event.task_type,
    )
    if existing is None:
        existing = Episode(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            context=context_text,
            outcome=str((event.outcome.model_dump() if event.outcome else {}).get("result", ""))[:300],
            confidence=max(0.0, min(1.0, 0.5 + (0.3 * authority_score))),
            status="open",
            authority_score=authority_score,
            stability_score=stability_score,
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
        db.flush()
    else:
        existing.context = context_text or existing.context
        existing.authority_score = max(float(existing.authority_score or 0.0), authority_score)
        existing.stability_score = max(float(existing.stability_score or 0.0), stability_score)
        existing.updated_at = now
        db.flush()

    link_exists = db.execute(
        select(EpisodeEventLink.id)
        .where(EpisodeEventLink.episode_id == existing.id, EpisodeEventLink.event_id == event_id)
        .limit(1)
    ).scalar_one_or_none()
    if link_exists is None:
        db.add(EpisodeEventLink(episode_id=existing.id, event_id=event_id, created_at=now))

    if event.decision and isinstance(event.decision.model_dump(), dict):
        decision = event.decision.model_dump()
        db.add(
            EpisodeDecision(
                episode_id=existing.id,
                decision=str(decision.get("choice") or decision.get("rationale") or "decision"),
                why=str(decision.get("rationale") or ""),
                alternatives_json=list(decision.get("alternatives") or []),
                created_at=now,
            )
        )

    db.flush()
    lesson = db.get(EpisodeLesson, existing.id)
    if lesson is None:
        lesson = EpisodeLesson(
            episode_id=existing.id,
            do_more_json=[],
            do_less_json=[],
            avoid_json=[],
            updated_at=now,
        )
        db.add(lesson)
    lesson.updated_at = now
    return existing.id


def _ingest_source_allowed(source: str) -> bool:
    allowlist = settings.ingest_source_allowlist_set
    if not allowlist:
        return True
    normalized = source.strip().lower()
    if normalized in allowlist:
        return True
    return normalized.startswith("api-") or normalized.startswith("tce-")


def _store_event(db: Session, event: EventEnvelope, auth: AuthContext) -> UUID:
    if not _ingest_source_allowed(event.source):
        raise HTTPException(
            status_code=403,
            detail=f"event source '{event.source}' is not allowed for ingest",
        )
    idempotency_key = str(getattr(event, "idempotency_key", "") or "").strip() or None
    source_id = str(getattr(event, "source_id", "") or "").strip() or None
    source_seq_raw = getattr(event, "source_seq", None)
    if source_seq_raw is None:
        source_seq = None
    else:
        try:
            source_seq = int(source_seq_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="source_seq must be an integer") from None
    vector_clock = getattr(event, "vector_clock", {})
    if not isinstance(vector_clock, dict):
        vector_clock = {}
    authority_level = _authority_level(getattr(event, "authority_level", None))

    if idempotency_key:
        existing_id = db.execute(
            text(
                """
                SELECT id
                FROM events
                WHERE idempotency_key = :idempotency_key
                  AND (context->>'_tce_workspace') = :workspace_id
                  AND (context->>'_tce_owner') = :user_id
                ORDER BY ts DESC
                LIMIT 1
                """
            ),
            {
                "idempotency_key": idempotency_key,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
            },
        ).scalar_one_or_none()
        if existing_id is not None:
            return existing_id

    if source_id and source_seq is not None:
        existing_id = db.execute(
            text(
                """
                SELECT id
                FROM events
                WHERE source_id = :source_id
                  AND source_seq = :source_seq
                  AND (context->>'_tce_workspace') = :workspace_id
                  AND (context->>'_tce_owner') = :user_id
                ORDER BY ts DESC
                LIMIT 1
                """
            ),
            {
                "source_id": source_id,
                "source_seq": source_seq,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
            },
        ).scalar_one_or_none()
        if existing_id is not None:
            return existing_id

    payload_input = event.payload if isinstance(event.payload, dict) else {}
    if event.task_type == "editor_checkpoint" and event.event_type == EventType.TASK_STEP:
        payload_input = {
            "active_file": payload_input.get("active_file"),
            "line": payload_input.get("line"),
            "symbol": payload_input.get("symbol"),
            "selection_range": payload_input.get("selection_range"),
            "workspace_path": payload_input.get("workspace_path"),
            "ts": payload_input.get("ts") or event.ts.isoformat(),
        }
    zone_payload, _ = apply_redaction_zones(payload_input, event.context, settings.redaction_zones)
    redacted_payload, _ = redact_payload(zone_payload, hints=event.redaction_hints)
    if isinstance(redacted_payload, dict):
        redacted_payload.setdefault("workspace_id", auth.workspace_id)
        redacted_payload.setdefault("user_id", auth.user_id)
    scoped_context = stamp_context_scope(event.context, auth.workspace_id, auth.user_id)
    stored_payload = maybe_encrypt_payload(
        redacted_payload,
        enabled=settings.enable_payload_encryption,
        min_sensitivity=settings.encrypt_min_sensitivity,
        sensitivity=event.sensitivity,
        required=bool(settings.security_encrypt_sensitive_required),
        key_id=settings.security_encryption_key_id,
        secret=settings.security_encryption_secret,
    )
    summary_l0, summary_l1 = summarize_event_record(
        title=event.title,
        task_type=event.task_type,
        domain=event.domain,
        payload=redacted_payload,
        decision=event.decision.model_dump() if event.decision else None,
        outcome=event.outcome.model_dump() if event.outcome else None,
    )
    row = Event(
        ts=event.ts,
        actor=event.actor,
        source=event.source,
        domain=event.domain,
        task_type=event.task_type,
        event_type=event.event_type.value,
        title=event.title,
        summary_l0=summary_l0,
        summary_l1_json=summary_l1,
        summary_version=SUMMARY_VERSION,
        summary_updated_at=event.ts,
        payload=stored_payload,
        context=scoped_context,
        inputs=event.inputs,
        steps=[step.model_dump() for step in event.steps],
        decision=event.decision.model_dump() if event.decision else None,
        outcome=event.outcome.model_dump() if event.outcome else None,
        style=event.style.model_dump() if event.style else None,
        links=event.links.model_dump() if event.links else None,
        tags=event.tags,
        sensitivity=event.sensitivity,
        redaction_hints=event.redaction_hints,
        source_id=source_id,
        source_seq=source_seq,
        vector_clock=vector_clock,
        idempotency_key=idempotency_key,
        authority_level=authority_level,
        hash=_event_hash(event),
        schema_version=event.schema_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.execute(
        text(
            """
            INSERT INTO event_identity (id, ts)
            VALUES (:id, :ts)
            ON CONFLICT (id) DO UPDATE SET ts = excluded.ts
            """
        ),
        {"id": row.id, "ts": row.ts},
    )
    db.commit()
    try:
        graph_index = index_event_graph(
            db,
            event_id=row.id,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            domain=event.domain,
            task_type=event.task_type,
            title=event.title,
            tags=event.tags,
            context=scoped_context,
            payload=redacted_payload,
        )
    except Exception:
        graph_index = {}
    enqueue_job("tce_worker.jobs.embedding.run", str(row.id))
    if settings.episode_extraction_enabled:
        if settings.episode_extraction_async:
            enqueue_job(
                "tce_worker.jobs.episode_extraction.run",
                str(row.id),
                auth.workspace_id,
                auth.user_id,
            )
        else:
            try:
                _ensure_episode_for_event(
                    db,
                    event_id=row.id,
                    event=event,
                    workspace_id=auth.workspace_id,
                    user_id=auth.user_id,
                    authority_level=authority_level,
                )
                db.commit()
            except Exception:
                db.rollback()
    event_type_value = str(event.event_type.value)
    if settings.semantic_consolidation_enabled and event_type_value in {
        "TASK_DONE",
        "TASK_DECISION",
        "TASK_STEP",
        "FIX",
        "ERROR",
    }:
        enqueue_job(
            "tce_worker.jobs.semantic_consolidation.run",
            auth.workspace_id,
            auth.user_id,
            int(settings.semantic_consolidation_lookback_days),
        )
    if settings.reflection_enabled and event_type_value in {"TASK_DONE", "TASK_DECISION"}:
        enqueue_job(
            "tce_worker.jobs.reflection.run",
            str(row.id),
            auth.workspace_id,
            auth.user_id,
        )
    _ = graph_index
    return row.id


def _truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}<TRUNCATED>"


def _collapse_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _redacted_excerpt(value: Any, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    normalized = _collapse_text(value)
    if not normalized:
        return ""
    redacted, _ = redact_text(normalized)
    return redacted[:max_chars].strip()


def _normalize_string_list(value: Any, *, max_items: int, item_max_chars: int) -> list[str]:
    if not isinstance(value, list) or max_items <= 0:
        return []
    normalized: list[str] = []
    for item in value:
        text = _redacted_excerpt(item, max_chars=item_max_chars)
        if not text:
            continue
        normalized.append(text)
        if len(normalized) >= max_items:
            break
    return normalized


def _citation_excerpt_from_event_row(row: dict[str, Any], *, snippet_chars: int) -> str:
    title = _collapse_text(row.get("title"))
    payload = row.get("payload")
    summary = ""
    if isinstance(payload, dict):
        for key in ("summary", "message", "result", "decision"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                summary = candidate
                break
    source = f"{title} | {summary}" if summary else title
    return _redacted_excerpt(source, max_chars=snippet_chars)


def _build_citation_snippets(
    db: Session,
    *,
    citation_ids: list[UUID],
    max_items: int,
    snippet_chars: int,
    evidence_events: list[Any] | None = None,
) -> list[dict[str, str]]:
    if not citation_ids or max_items <= 0 or snippet_chars <= 0:
        return []

    ordered_ids = citation_ids[:max_items]
    evidence_lookup: dict[str, str] = {}
    if isinstance(evidence_events, list):
        for event in evidence_events:
            if event is None:
                continue
            event_id = str(getattr(event, "id", "") or "").strip()
            title = str(getattr(event, "title", "") or "").strip()
            key_fields = getattr(event, "key_payload_fields", None)
            if not event_id or not title:
                continue
            detail_bits: list[str] = []
            if isinstance(key_fields, dict):
                for key, value in key_fields.items():
                    if isinstance(value, (str, int, float, bool)):
                        detail_bits.append(f"{key}={value}")
                    if len(detail_bits) >= 2:
                        break
            source = f"{title} | {'; '.join(detail_bits)}" if detail_bits else title
            excerpt = _redacted_excerpt(source, max_chars=snippet_chars)
            if excerpt:
                evidence_lookup[event_id] = excerpt

    missing_ids: list[UUID] = []
    snippets: list[dict[str, str]] = []
    for citation_id in ordered_ids:
        event_id = str(citation_id)
        excerpt = evidence_lookup.get(event_id)
        if excerpt:
            snippets.append({"id": event_id, "excerpt": excerpt})
        else:
            missing_ids.append(citation_id)

    if missing_ids:
        rows = db.execute(
            text(
                """
                SELECT id, title, payload
                FROM events
                WHERE id = ANY(CAST(:ids AS UUID[]))
                """
            ),
            {"ids": [str(item) for item in missing_ids]},
        ).mappings().all()
        row_lookup = {str(row.get("id")): row for row in rows}
        for citation_id in missing_ids:
            event_id = str(citation_id)
            row = row_lookup.get(event_id)
            if row is None:
                continue
            excerpt = _citation_excerpt_from_event_row(row, snippet_chars=snippet_chars)
            if excerpt:
                snippets.append({"id": event_id, "excerpt": excerpt})

    snippet_lookup = {item["id"]: item["excerpt"] for item in snippets if item.get("id") and item.get("excerpt")}
    ordered: list[dict[str, str]] = []
    for citation_id in ordered_ids:
        event_id = str(citation_id)
        excerpt = snippet_lookup.get(event_id)
        if excerpt:
            ordered.append({"id": event_id, "excerpt": excerpt})
    return ordered


def _interaction_domain(request_payload: dict[str, Any]) -> str:
    app_context = request_payload.get("app_context")
    if isinstance(app_context, dict):
        domain = app_context.get("domain")
        if isinstance(domain, str) and domain.strip():
            return domain.strip()[:64]

    filters = request_payload.get("filters")
    if isinstance(filters, dict):
        domain = filters.get("domain")
        if isinstance(domain, str) and domain.strip():
            return domain.strip()[:64]

    domain = request_payload.get("domain")
    if isinstance(domain, str) and domain.strip():
        return domain.strip()[:64]

    return "interaction"


def _auto_capture_event_type(action: str) -> EventType:
    if action in {"clone_advice", "clone_advice_fallback"}:
        return EventType.TASK_DECISION
    if action == "clone_advice_blocked":
        return EventType.ERROR
    return EventType.TASK_STEP


def _interaction_summary(
    action: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None = None,
) -> str | None:
    candidates: list[str] = []

    request_priority = ("task", "query", "objective", "message", "prompt", "file_path", "event_id")
    if action == "takeover_step":
        request_priority = ("objective", "task", "query", "prompt", "message", "file_path", "event_id")

    response_map = response_payload if isinstance(response_payload, dict) else {}
    if action == "takeover_step":
        state = response_map.get("state")
        if isinstance(state, dict):
            takeover_context = state.get("takeover_context")
            if isinstance(takeover_context, dict):
                objective = takeover_context.get("objective")
                if isinstance(objective, str) and objective.strip():
                    candidates.append(objective.strip())
        selected_goal = response_map.get("selected_goal")
        if isinstance(selected_goal, dict):
            for key in ("title", "description"):
                value = selected_goal.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())

    for key in request_priority:
        value = request_payload.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    app_context = request_payload.get("app_context")
    if isinstance(app_context, dict):
        detail = app_context.get("domain") or app_context.get("feature") or app_context.get("goal")
        if isinstance(detail, str) and detail.strip():
            candidates.append(detail.strip())

    if not candidates:
        return None

    summary = re.sub(r"\s+", " ", candidates[0]).strip()
    if not summary:
        return None

    return summary[:120]


def _auto_capture_interaction_sync(
    auth_consumer: str,
    auth_workspace_id: str,
    auth_user_id: str,
    auth_role_value: str,
    action: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    citations: list[UUID] | None = None,
) -> None:
    """Background thread: store auto-capture event with its own DB session."""
    from .db import get_session_factory

    raw = json.dumps(
        {"action": action, "request": request_payload, "response": response_payload},
        sort_keys=True,
        default=str,
    )
    clipped = _truncate_text(raw, settings.auto_capture_max_chars)
    redacted_text, applied = redact_text(clipped)
    sensitive_skipped = bool(applied) and settings.auto_capture_skip_sensitive

    payload: dict[str, Any]
    summary = _interaction_summary(action, request_payload, response_payload)
    title = f"Interaction: {action}"
    if summary:
        title = f"{title} - {summary}"
    if sensitive_skipped:
        title = f"Interaction: {action} (sensitive content skipped)"
        payload = {
            "action": action,
            "sensitive_skipped": True,
            "applied_redactions": applied,
            "request_keys": sorted(request_payload.keys()),
            "response_keys": sorted(response_payload.keys()),
            "citations_count": len(citations or []),
        }
    else:
        payload = {
            "action": action,
            "summary": summary,
            "request": request_payload,
            "response": response_payload,
            "redacted_excerpt": redacted_text,
            "citations": [str(item) for item in (citations or [])],
        }

    event = EventEnvelope(
        schema_version=1,
        ts=datetime.now(tz=UTC),
        actor=auth_user_id,
        source="api-auto-capture",
        domain=_interaction_domain(request_payload),
        task_type=f"interaction_{action}"[:80],
        event_type=_auto_capture_event_type(action),
        title=title,
        payload=payload,
        context={
            "workspace": auth_workspace_id,
            "user": auth_user_id,
            "consumer": auth_consumer,
            "role": auth_role_value,
        },
        inputs={},
        steps=[],
        decision=None,
        outcome=None,
        style=None,
        links=None,
        tags=["auto_capture", action],
        sensitivity=2 if sensitive_skipped else 1,
        redaction_hints=[],
    )

    db = None
    try:
        db = get_session_factory()()
        auth_proxy = AuthContext(
            consumer=auth_consumer,
            mode="bearer",
            role=AgentRole(auth_role_value),
            workspace_id=auth_workspace_id,
            user_id=auth_user_id,
        )
        _store_event(db, event, auth_proxy)
    except Exception:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _auto_capture_interaction(
    db: Session,
    auth: AuthContext,
    action: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    citations: list[UUID] | None = None,
) -> None:
    if not settings.auto_capture_interactions:
        return

    import threading

    t = threading.Thread(
        target=_auto_capture_interaction_sync,
        args=(
            auth.consumer,
            auth.workspace_id,
            auth.user_id,
            auth.role.value,
            action,
            request_payload,
            response_payload,
            citations,
        ),
        daemon=True,
    )
    t.start()


def _latest_editor_checkpoint_anchor(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT ts, payload
            FROM events
            WHERE task_type = 'editor_checkpoint'
              AND event_type = :event_type
              AND (context->>'_tce_workspace') = :workspace_id
              AND (context->>'_tce_owner') = :owner_id
              AND (context->>'session_id') = :session_id
            ORDER BY ts DESC
            LIMIT 1
            """
        ),
        {
            "event_type": EventType.TASK_STEP.value,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "session_id": session_id,
        },
    ).mappings().first()
    if row is None:
        return None
    payload = row.get("payload")
    ts_value = row.get("ts")
    if not isinstance(ts_value, datetime):
        ts_value = None
    anchor = latest_checkpoint_anchor(payload=payload if isinstance(payload, dict) else None, event_ts=ts_value)
    if not anchor:
        return None
    anchor.pop("ts", None)
    return anchor


def _normalize_execution_milestone(
    *,
    details_map: dict[str, Any],
    fallback_title: str,
    state_value: str,
    checkpoint_anchor: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = normalize_milestone_v1(
        details=details_map,
        state=state_value,
        fallback_title=fallback_title,
    )
    milestone = dict(normalized.get("normalized") or {})
    anchors, _ = normalize_anchor_list(milestone.get("anchors"), max_items=40)
    milestone["anchors"] = merge_anchors(
        plugin_checkpoint=checkpoint_anchor,
        reported_anchors=anchors,
        max_items=40,
    )
    return {
        "milestone": milestone,
        "valid": bool(normalized.get("valid", False)),
        "errors": [str(item) for item in (normalized.get("errors") or []) if str(item).strip()],
        "redaction_applied": bool(normalized.get("redaction_applied", False)),
    }


def _reject_advisor_writes(auth: AuthContext) -> None:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")


def _behavior_subject_access_allowed(auth: AuthContext) -> bool:
    if auth.behavior_subject_id == auth.user_id:
        return True
    raw = str(getattr(settings, "behavior_subject_bindings", "") or "")
    for entry in raw.split(","):
        owner, separator, subjects = entry.partition("=")
        if not separator or owner.strip() != auth.user_id:
            continue
        allowed = {item.strip() for item in subjects.split("|") if item.strip()}
        return auth.behavior_subject_id in allowed
    return False


def _enforce_workspace_access(auth: AuthContext, db: Session) -> None:
    access_mode = str(getattr(settings, "workspace_access_mode", "compat") or "compat").strip().lower()
    strict = access_mode == "strict"
    if not strict and auth.role in {AgentRole.EXECUTOR, AgentRole.ADVISOR}:
        return
    try:
        allowed = workspace_access_allowed(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            require_membership=strict,
        )
    except Exception as exc:
        if strict:
            raise HTTPException(status_code=503, detail="workspace access could not be verified") from exc
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="workspace access denied")
    if strict and not _behavior_subject_access_allowed(auth):
        raise HTTPException(status_code=403, detail="behavior subject access denied")


def _summary_window(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(tz=UTC)
    normalized = period.strip().lower()
    if normalized == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if normalized in {"week", "this_week"}:
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6), now
    raise HTTPException(status_code=400, detail="period must be 'today' or 'week'")


def _resolved_takeover_policy(policy: TakeoverPolicy) -> TakeoverPolicy:
    safety = policy.safety_policy.strip() if policy.safety_policy else settings.takeover_safety_policy
    confirm = policy.confirm_keyword.strip() if policy.confirm_keyword else settings.takeover_confirm_keyword
    deny = policy.deny_keyword.strip() if policy.deny_keyword else settings.takeover_deny_keyword
    return TakeoverPolicy(
        safety_policy=safety or settings.takeover_safety_policy,
        confirm_keyword=confirm or settings.takeover_confirm_keyword,
        deny_keyword=deny or settings.takeover_deny_keyword,
        timeout_minutes=max(1, int(policy.timeout_minutes)),
        auto_handoff_on_question=bool(policy.auto_handoff_on_question),
    )


def _build_takeover_working_set(
    db: Session,
    auth: AuthContext,
    *,
    task: str,
    app_context: dict[str, Any],
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    bundle, blocked, _ = build_context_bundle(
        db,
        ContextBundleRequest(task=task, app_context=app_context, constraints=constraints),
        consumer_ctx,
        policy_engine,
    )
    top_patterns = [
        {
            "id": str(pattern.id),
            "statement": pattern.statement,
            "confidence": float(pattern.confidence),
            "pattern_type": pattern.pattern_type,
        }
        for pattern in bundle.top_patterns[:8]
    ]
    citation_limit = max(1, int(getattr(settings, "takeover_citation_snippet_max_items", 20)))
    snippet_chars = max(32, int(getattr(settings, "takeover_citation_snippet_max_chars", 100)))
    citation_ids = list(bundle.citations[:citation_limit])
    citation_snippets = (
        _build_citation_snippets(
            db,
            citation_ids=citation_ids,
            max_items=citation_limit,
            snippet_chars=snippet_chars,
            evidence_events=list(bundle.evidence_events),
        )
        if bool(getattr(settings, "takeover_rationale_enrichment_enabled", True))
        and bool(getattr(settings, "takeover_citation_snippets_enabled", True))
        else []
    )
    graph = bundle.structured_context.get("graph", {}) if isinstance(bundle.structured_context, dict) else {}
    working_set = {
        "summary": bundle.summary,
        "do": bundle.do_dont.get("do", [])[:5],
        "dont": bundle.do_dont.get("dont", [])[:5],
        "top_patterns": top_patterns,
        "evidence_count": len(bundle.citations),
        "citations": [str(v) for v in citation_ids],
        "citation_snippets": citation_snippets,
        "graph_entities": len(graph.get("entities", [])) if isinstance(graph, dict) else 0,
        "graph_relationships": len(graph.get("relationships", [])) if isinstance(graph, dict) else 0,
        "policy": bundle.policy,
        "context_tier_used": bundle.context_tier_used,
        "summary_coverage": bundle.summary_coverage,
        "planner_used": bundle.planner_used,
        "subquery_count": bundle.subquery_count,
        "subquery_labels": bundle.subquery_labels,
        "episode_boost_applied": bundle.episode_boost_applied,
        "activation_boost_applied": bundle.activation_boost_applied,
        "refreshed_at": datetime.now(tz=UTC).isoformat(),
    }
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return working_set, elapsed_ms


def _record_takeover_action_async(
    *,
    session_id: str,
    workspace_id: str,
    user_id: str,
    turn: int,
    objective_hash_value: str | None,
    action_kind: str,
    result: str,
    latency_ms: int,
    meta: dict[str, Any],
) -> None:
    import threading

    def _write() -> None:
        db = get_session_factory()()
        try:
            record_takeover_action(
                db,
                session_id=session_id,
                workspace_id=workspace_id,
                user_id=user_id,
                turn=turn,
                objective_hash=objective_hash_value,
                action_kind=action_kind,
                result=result,
                latency_ms=latency_ms,
                meta=meta,
            )
        except Exception:
            logger.warning("takeover action log write failed", exc_info=True)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    threading.Thread(target=_write, daemon=True).start()


app.include_router(
    build_system_router(
        request_counter=REQUEST_COUNT,
        get_auth_context_dep=get_auth_context,
        get_db_dep=get_db,
        get_runtime_mode_fn=get_runtime_mode,
        set_runtime_mode_fn=set_runtime_mode,
        write_audit_log_fn=write_audit_log,
        enforce_workspace_access_fn=_enforce_workspace_access,
    )
)


@app.post("/v1/events", response_model=IngestResponse)
def ingest_event(
    event: EventEnvelope,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> IngestResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    start = time.perf_counter()
    REQUEST_COUNT.labels(endpoint="events", method="POST").inc()
    event_id = _store_event(db, event, auth)
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="events", method="POST").observe(latency_ms / 1000)
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="record_event",
        query={"event_id": str(event_id)},
        result_event_ids=[event_id],
        policy_decisions={"mode": auth.mode, "role": auth.role.value},
        latency_ms=latency_ms,
    )
    return IngestResponse(event_id=event_id)


@app.post("/v1/events/batch", response_model=BatchIngestResponse)
def ingest_batch(
    body: BatchIngestRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BatchIngestResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    start = time.perf_counter()
    REQUEST_COUNT.labels(endpoint="events_batch", method="POST").inc()
    event_ids = [_store_event(db, event, auth) for event in body.events]
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="events_batch", method="POST").observe(latency_ms / 1000)
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="record_event_batch",
        query={"count": len(body.events)},
        result_event_ids=event_ids,
        policy_decisions={"mode": auth.mode, "role": auth.role.value},
        latency_ms=latency_ms,
    )
    return BatchIngestResponse(event_ids=event_ids)


@app.get("/v1/events/{event_id}", response_model=EventEnvelope)
def get_event(
    event_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> EventEnvelope:
    REQUEST_COUNT.labels(endpoint="event_get", method="GET").inc()
    _enforce_workspace_access(auth, db)
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    if isinstance(event.context, dict):
        workspace = event.context.get("_tce_workspace")
        owner = event.context.get("_tce_owner")
        if workspace and workspace != auth.workspace_id:
            raise HTTPException(status_code=403, detail="event not in workspace scope")
        if owner and owner != auth.user_id:
            raise HTTPException(status_code=403, detail="event not in user scope")
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    decision = policy_engine.evaluate(consumer_ctx, event.domain, event.sensitivity, event.ts)
    if not decision.allow:
        raise HTTPException(status_code=403, detail=decision.reason)

    return EventEnvelope(
        schema_version=event.schema_version,
        ts=event.ts,
        actor=event.actor,
        source=event.source,
        domain=event.domain,
        task_type=event.task_type,
        event_type=EventType(event.event_type),
        title=event.title,
        payload=event.payload,
        context=event.context,
        inputs=event.inputs,
        steps=_sanitize_event_steps(event.steps),
        decision=_safe_validate(EventDecision, event.decision),
        outcome=_safe_validate(EventOutcome, event.outcome, outcome=True),
        style=_safe_validate(EventStyle, event.style),
        links=_safe_validate(EventLinks, event.links),
        tags=event.tags,
        sensitivity=event.sensitivity,
        redaction_hints=event.redaction_hints,
        source_id=event.source_id,
        source_seq=event.source_seq,
        vector_clock=event.vector_clock or {},
        idempotency_key=event.idempotency_key,
        authority_level=event.authority_level,
    )


@app.post("/v1/search", response_model=dict)
def search(
    body: EventSearchRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    start = time.perf_counter()
    REQUEST_COUNT.labels(endpoint="search", method="POST").inc()
    _enforce_workspace_access(auth, db)
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    hits, citations, blocked, retrieval_meta = run_search(db, body, consumer_ctx, policy_engine)
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="search", method="POST").observe(latency_ms / 1000)
    policy_summary = {
        **policy_engine.summarize(blocked_count=blocked, applied_redactions=[], role=auth.role),
        "retrieval": retrieval_meta,
    }
    if retrieval_meta.get("cross_user_scope_applied"):
        policy_summary["policy_profile"] = "workspace-shared"
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="search_events",
        query=body.model_dump(mode="json"),
        result_event_ids=citations,
        policy_decisions=policy_summary,
        latency_ms=latency_ms,
    )
    _auto_capture_interaction(
        db=db,
        auth=auth,
        action="search_events",
        request_payload=body.model_dump(mode="json"),
        response_payload={
            "hits_count": len(hits),
            "blocked_count": blocked,
            "citations_count": len(citations),
        },
        citations=citations,
    )
    return {
        "result": search_response(hits, citations).model_dump(mode="json"),
        "blocked": blocked,
        "metadata": {
            "handoff_hits_count": int(retrieval_meta.get("handoff_hits_count", 0) or 0),
            "top_handoff_record_ids": list(retrieval_meta.get("top_handoff_record_ids") or []),
            "cross_user_scope_applied": bool(retrieval_meta.get("cross_user_scope_applied", False)),
            "cross_user_scope_owners": list(retrieval_meta.get("cross_user_scope_owners") or []),
            "resume_packet_available": bool(retrieval_meta.get("resume_packet_available", False)),
            "context_tier_used": str(retrieval_meta.get("context_tier_used") or "l2"),
            "summary_coverage": float(retrieval_meta.get("summary_coverage", 0.0) or 0.0),
            "planner_used": bool(retrieval_meta.get("planner_used", False)),
            "subquery_count": int(retrieval_meta.get("subquery_count", 0) or 0),
            "subquery_labels": list(retrieval_meta.get("subquery_labels") or []),
            "episode_boost_applied": bool(retrieval_meta.get("episode_boost_applied", False)),
            "activation_boost_applied": bool(retrieval_meta.get("activation_boost_applied", False)),
        },
        "policy": policy_summary,
    }


def _resume_file_items_from_record(record: dict[str, Any], *, query_text: str) -> list[ResumePacketFileItem]:
    files_raw = record.get("files_json") if isinstance(record.get("files_json"), list) else []
    anchors_raw = record.get("anchors_json") if isinstance(record.get("anchors_json"), list) else []
    change_raw = record.get("change_summary_json") if isinstance(record.get("change_summary_json"), dict) else {}
    anchors_by_file: dict[str, list[ResumePacketAnchor]] = {}
    for item in anchors_raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file") or "").strip()
        if not path:
            continue
        line_value = item.get("line")
        line = int(line_value) if isinstance(line_value, int) and line_value > 0 else None
        anchors_by_file.setdefault(path, []).append(
            ResumePacketAnchor(file=path, line=line, symbol=str(item.get("symbol") or "").strip() or None)
        )
    file_items: list[ResumePacketFileItem] = []
    lowered_query = str(query_text or "").lower()
    for path in [str(item).strip() for item in files_raw if str(item).strip()][:40]:
        anchors = anchors_by_file.get(path, [])
        summary_raw = change_raw.get(path) if isinstance(change_raw, dict) else {}
        if not isinstance(summary_raw, dict):
            summary_raw = {}
        path_boost = 0.12 if path.lower() in lowered_query else 0.0
        priority = min(1.0, 0.55 + (0.25 if anchors else 0.0) + path_boost)
        file_items.append(
            ResumePacketFileItem(
                path=path,
                priority_score=round(priority, 3),
                anchors=anchors[:10],
                change_summary=ResumePacketChangeSummary(
                    added_lines=max(0, int(summary_raw.get("added") or summary_raw.get("added_lines") or 0)),
                    removed_lines=max(0, int(summary_raw.get("removed") or summary_raw.get("removed_lines") or 0)),
                    intent=str(summary_raw.get("intent") or "")[:160],
                ),
            )
        )
    file_items.sort(key=lambda item: item.priority_score, reverse=True)
    return file_items[:40]


@app.post("/v1/handoff/resume", response_model=ResumePacketResponse)
def handoff_resume_packet(
    body: ResumePacketRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ResumePacketResponse:
    REQUEST_COUNT.labels(endpoint="handoff_resume", method="POST").inc()
    if not bool(getattr(settings, "resume_packet_enabled", True)):
        raise HTTPException(status_code=404, detail="resume packet endpoint disabled")
    _enforce_workspace_access(auth, db)
    started = time.perf_counter()
    requested_at = datetime.now(tz=UTC)
    if body.include_cross_user:
        owner_scope, cross_user_scope_applied, cross_user_scope_owners = _resolve_owner_scope(
            db,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            query_text=body.query,
        )
    else:
        owner_scope = {str(auth.user_id or "").strip().lower()}
        cross_user_scope_applied = False
        cross_user_scope_owners = sorted(owner_scope)
    target_owner = str(body.target_owner or "").strip().lower()
    if target_owner:
        filtered = {owner for owner in owner_scope if _owner_matches_hint(owner, target_owner)}
        if filtered:
            owner_scope = filtered.union({str(auth.user_id or "").strip().lower()})
            cross_user_scope_owners = sorted(owner_scope)[:6]
            cross_user_scope_applied = owner_scope != {str(auth.user_id or "").strip().lower()}
    owner_ids = sorted(owner_scope)
    rows = db.execute(
        text(
            """
            SELECT id, owner_id, session_id, ts, title, decision, next_step, status,
                   files_json, anchors_json, git_json, change_summary_json, objective_text,
                   source, event_id, schema_version
            FROM handoff_records
            WHERE workspace_id = :workspace_id
              AND owner_id = ANY(CAST(:owner_ids AS TEXT[]))
              AND session_id = :session_id
            ORDER BY ts DESC
            LIMIT :row_limit
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "owner_ids": owner_ids,
            "session_id": body.session_id,
            "row_limit": max(20, int(body.k) * 20),
        },
    ).mappings().all()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        row_map = dict(row)
        if not str(row_map.get("objective_text") or "").strip():
            objective_text, _ = normalize_objective_text(
                title=row_map.get("title"),
                decision=row_map.get("decision"),
                next_step=row_map.get("next_step"),
            )
            row_map["objective_text"] = objective_text
        candidates.append(row_map)
    selected, alternates, candidate_count = rank_resume_candidates(
        candidates,
        query_text=body.query,
        k=max(1, int(body.k)),
    )
    if not selected:
        raise HTTPException(status_code=404, detail="no matching handoff records found")
    selected_id = str(selected.get("id") or "").strip()
    try:
        selected_uuid = UUID(selected_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="invalid handoff record id") from exc
    files = _resume_file_items_from_record(selected, query_text=body.query)
    packet_id = uuid.uuid4()
    response = ResumePacketResponse(
        packet_id=packet_id,
        selected_record_id=selected_uuid,
        selection_reason="task_overlap_then_recency",
        cross_user_scope_applied=bool(cross_user_scope_applied),
        cross_user_scope_owners=list(cross_user_scope_owners),
        task_summary=str(selected.get("title") or "")[:160],
        decision=str(selected.get("decision") or "")[:500],
        next_step=str(selected.get("next_step") or "")[:300],
        status=str(selected.get("status") or "failed"),
        contract_valid=str(selected.get("schema_version") or "").strip().lower() == "v1" and bool(files),
        git=dict(selected.get("git_json") or {}),
        files=files,
        continuation_steps=[
            "Open top file anchor",
            "Run scoped verification",
            "Report execution with milestone_schema=v1",
        ],
        retrieval_meta=ResumePacketRetrievalMeta(
            latency_ms=int((time.perf_counter() - started) * 1000),
            candidate_count=int(candidate_count),
            alternates=alternates,
        ),
    )
    selected_ts = selected.get("ts")
    if isinstance(selected_ts, datetime):
        record_resume_attempt(
            db,
            packet_id=packet_id,
            workspace_id=auth.workspace_id,
            requesting_owner_id=auth.user_id,
            target_owner_id=str(selected.get("owner_id") or body.target_owner or auth.user_id),
            selected_record_id=selected_uuid,
            query_text=body.query,
            top_file=files[0].path if files else None,
            requested_at=requested_at,
            returned_at=datetime.now(tz=UTC),
            handoff_ts=selected_ts,
        )
    return response


@app.post("/v1/completions", response_model=CompletionCaptureResponse)
def capture_completion(
    body: CompletionCaptureRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CompletionCaptureResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    normalized = normalize_milestone_v1(
        details=body.model_dump(),
        state=body.state,
        fallback_title=body.title,
    )
    if not normalized["valid"]:
        raise HTTPException(
            status_code=422,
            detail={"message": "invalid completion milestone", "errors": normalized["errors"]},
        )
    milestone = dict(normalized["normalized"])
    milestone["change_summary_json"] = dict(body.change_summary or {})
    milestone["source"] = body.source
    now = datetime.now(tz=UTC)
    outbox = enqueue_handoff(
        db,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        behavior_subject_id=auth.behavior_subject_id,
        session_id=body.session_id,
        directive_id=None,
        completion_key=body.completion_key,
        terminal_state=body.state,
        milestone=milestone,
        source=body.source,
        redaction_applied=bool(normalized["redaction_applied"]),
        now=now,
    )
    db.commit()
    delivered = deliver_handoff_safely(
        db,
        outbox_id=outbox.id,
        retention_days=int(settings.handoff_retention_days),
    )
    if delivered.status == "pending":
        try:
            enqueue_job("tce_worker.jobs.handoff_outbox.run", str(delivered.id), 1)
        except Exception:
            logger.warning("failed to enqueue pending handoff outbox", exc_info=True)
    return CompletionCaptureResponse(
        outbox_id=delivered.id,
        delivery_status=delivered.status,
        event_id=delivered.event_id,
        handoff_record_id=delivered.handoff_record_id,
        contract_valid=True,
        captured_at=delivered.created_at,
    )


@app.post("/v1/continuity/pilot/feedback", response_model=ResumeFeedbackResponse)
def capture_resume_feedback(
    body: ResumeFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ResumeFeedbackResponse:
    _enforce_workspace_access(auth, db)
    row = db.execute(
        select(ContinuityResumeAttempt).where(
            ContinuityResumeAttempt.packet_id == body.packet_id,
            ContinuityResumeAttempt.workspace_id == auth.workspace_id,
            ContinuityResumeAttempt.requesting_owner_id == auth.user_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="resume packet not found")
    opened_file, _ = redact_text(str(body.opened_file or "")[:240])
    correction_reason, _ = redact_text(body.correction_reason[:500])
    now = datetime.now(tz=UTC)
    row.opened_file = opened_file or None
    row.correct_file = body.correct_file
    row.correction_required = body.correction_required
    row.correction_reason = correction_reason
    row.feedback_at = now
    db.commit()
    return ResumeFeedbackResponse(packet_id=body.packet_id, recorded=True, feedback_at=now)


@app.get("/v1/continuity/pilot/status", response_model=ContinuityPilotStatusResponse)
def continuity_pilot_status(
    days: int = 30,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ContinuityPilotStatusResponse:
    _enforce_workspace_access(auth, db)
    window_days = max(1, min(365, int(days)))
    return ContinuityPilotStatusResponse(
        window_days=window_days,
        generated_at=datetime.now(tz=UTC),
        **pilot_metrics(db, workspace_id=auth.workspace_id, days=window_days),
    )


@app.get("/v1/auth/whoami", response_model=dict)
def auth_whoami(auth: AuthContext = Depends(get_auth_context)) -> dict[str, Any]:
    return {
        "consumer": auth.consumer,
        "role": auth.role.value,
        "workspace_id": auth.workspace_id,
        "user_id": auth.user_id,
        "behavior_subject_id": auth.behavior_subject_id,
        "identity_claims_mode": settings.identity_claims_mode,
    }


@app.get("/v1/context/retrieval/status", response_model=dict)
def context_retrieval_status(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="context_retrieval_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    snapshot = retrieval_status_snapshot()
    snapshot["workspace_id"] = auth.workspace_id
    snapshot["user_id"] = auth.user_id
    return snapshot


@app.get("/v1/setup/advisor/providers", response_model=AdvisorProvidersResponse)
def setup_advisor_providers(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorProvidersResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_providers", method="GET").inc()
    _enforce_workspace_access(auth, db)
    config_raw = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    providers = [
        AdvisorProviderItem(
            provider_id=meta.provider_id,
            label=meta.label,
            provider_category=meta.provider_category,
            protocol=meta.protocol,
            connection_type=_provider_connection_type(meta.provider_id),
            platform_hint=_provider_platform_hint(meta.provider_id),
            required_in_baseline=bool(meta.required_in_baseline),
            default_base_url=_provider_default_base_url(meta.provider_id, meta.default_base_url),
            api_version=meta.api_version,
            region_hint=meta.region_hint,
            default_models=list(meta.default_models),
            supports_model_listing=meta.supports_model_listing,
            key_optional=meta.key_optional,
        )
        for meta in list_provider_metadata()
    ]
    return AdvisorProvidersResponse(
        providers=providers,
        config=_config_response_from_raw(config_raw),
        generated_at=datetime.now(tz=UTC),
    )


@app.get("/v1/setup/advisor/models", response_model=AdvisorModelsResponse)
def setup_advisor_models(
    provider: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorModelsResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_models", method="GET").inc()
    _enforce_workspace_access(auth, db)
    provider_id = provider.strip().lower()
    adapter = get_provider(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")

    config_raw = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    route = _route_for_provider(config_raw, provider_id)
    api_key = _resolve_route_api_key(
        db=db,
        provider_id=provider_id,
        route=route,
        config=config_raw,
    )
    custom_headers = config_raw.get("custom_headers") if isinstance(config_raw.get("custom_headers"), dict) else {}
    request = _provider_request_for(
        provider_id,
        model=str((route or {}).get("model") or config_raw.get("advisor_custom_model") or config_raw.get("advisor_primary_model") or "").strip() or None,
        base_url=str((route or {}).get("base_url") or config_raw.get("advisor_custom_base_url") or _provider_default_base_url(provider_id, adapter.metadata.default_base_url) or "").strip() or None,
        api_version=str((route or {}).get("api_version") or config_raw.get("api_version") or adapter.metadata.api_version or "").strip() or None,
        api_key=api_key,
        timeout_ms=int(config_raw.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
        custom_headers={str(k): str(v) for k, v in custom_headers.items()},
    )
    try:
        models = adapter.list_models(request)
        source = "dynamic" if models else "static"
        if not models:
            models = list(adapter.metadata.default_models)
        return AdvisorModelsResponse(
            provider_id=provider_id,
            models=models,
            source=source,
            message=None,
            requires_api_key=not adapter.metadata.key_optional,
            connection_type=_provider_connection_type(provider_id),
        )
    except Exception as exc:
        return AdvisorModelsResponse(
            provider_id=provider_id,
            models=list(adapter.metadata.default_models),
            source="static",
            message=f"dynamic listing unavailable: {exc}",
            requires_api_key=not adapter.metadata.key_optional,
            connection_type=_provider_connection_type(provider_id),
        )


@app.post("/v1/setup/advisor/models/live", response_model=AdvisorModelsResponse)
def setup_advisor_models_live(
    body: AdvisorLiveModelsRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorModelsResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_models_live", method="POST").inc()
    _enforce_workspace_access(auth, db)
    provider_id = body.provider_id.strip().lower()
    adapter = get_provider(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")

    config_raw = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    route = _route_for_provider(config_raw, provider_id)
    api_key = _resolve_route_api_key(
        db=db,
        provider_id=provider_id,
        route=route,
        config=config_raw,
        explicit_key_ref=body.api_key_ref,
        provided_key=body.api_key,
    )
    if not api_key and not adapter.metadata.key_optional:
        return AdvisorModelsResponse(
            provider_id=provider_id,
            models=[],
            source="none",
            message="api key required before live model listing",
            requires_api_key=True,
            connection_type=_provider_connection_type(provider_id),
        )
    custom_headers = config_raw.get("custom_headers") if isinstance(config_raw.get("custom_headers"), dict) else {}
    request = _provider_request_for(
        provider_id,
        model=None,
        base_url=str((body.base_url or "").strip() or (route or {}).get("base_url") or config_raw.get("advisor_custom_base_url") or _provider_default_base_url(provider_id, adapter.metadata.default_base_url) or "").strip() or None,
        api_version=str((route or {}).get("api_version") or config_raw.get("api_version") or adapter.metadata.api_version or "").strip() or None,
        api_key=api_key,
        timeout_ms=int(body.timeout_ms or config_raw.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
        custom_headers={str(k): str(v) for k, v in custom_headers.items()},
    )
    try:
        models = adapter.list_models(request)
        return AdvisorModelsResponse(
            provider_id=provider_id,
            models=models,
            source="dynamic",
            message=None,
            requires_api_key=not adapter.metadata.key_optional,
            connection_type=_provider_connection_type(provider_id),
        )
    except Exception as exc:
        return AdvisorModelsResponse(
            provider_id=provider_id,
            models=list(adapter.metadata.default_models),
            source="static",
            message=f"dynamic listing unavailable: {exc}",
            requires_api_key=not adapter.metadata.key_optional,
            connection_type=_provider_connection_type(provider_id),
        )


@app.post("/v1/setup/advisor/route/verify", response_model=AdvisorVerifyAttempt)
def setup_advisor_route_verify(
    body: AdvisorRouteVerifyRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorVerifyAttempt:
    REQUEST_COUNT.labels(endpoint="setup_advisor_route_verify", method="POST").inc()
    _enforce_workspace_access(auth, db)
    provider_id = body.provider_id.strip().lower()
    adapter = get_provider(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")
    config_raw = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    route = _route_for_provider(config_raw, provider_id)
    api_key = _resolve_route_api_key(
        db=db,
        provider_id=provider_id,
        route=route,
        config=config_raw,
        explicit_key_ref=body.api_key_ref,
        provided_key=body.api_key,
    )
    custom_headers_raw = body.custom_headers or (config_raw.get("custom_headers") if isinstance(config_raw.get("custom_headers"), dict) else {})
    custom_headers: dict[str, str] = {}
    for key, value in custom_headers_raw.items():
        k = str(key).strip().lower()
        if k in settings.advisor_custom_headers_allowlist_set and str(value).strip():
            custom_headers[k] = str(value)
    req = _provider_request_for(
        provider_id,
        model=(body.model or "").strip() or str((route or {}).get("model") or "").strip() or None,
        base_url=(body.base_url or "").strip() or str((route or {}).get("base_url") or config_raw.get("advisor_custom_base_url") or _provider_default_base_url(provider_id, adapter.metadata.default_base_url) or "").strip() or None,
        api_version=(body.api_version or "").strip() or str((route or {}).get("api_version") or config_raw.get("api_version") or adapter.metadata.api_version or "").strip() or None,
        api_key=api_key,
        timeout_ms=int(body.timeout_ms or config_raw.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
        custom_headers=custom_headers,
    )
    result = adapter.verify(req)
    return AdvisorVerifyAttempt(
        provider_id=result.provider_id,
        ok=result.ok,
        code=result.code,
        message=result.message,
        latency_ms=result.latency_ms,
        models=result.models,
    )


@app.post("/v1/setup/advisor/local/ollama/pull", response_model=AdvisorLocalOllamaPullResponse)
def setup_advisor_local_ollama_pull(
    body: AdvisorLocalOllamaPullRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorLocalOllamaPullResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_local_ollama_pull", method="POST").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    base_url = (body.base_url or "").strip() or _default_local_base_url("local_ollama") or "http://localhost:11434"
    job_id = _start_ollama_pull_job(model=model, base_url=base_url)
    return AdvisorLocalOllamaPullResponse(
        job_id=job_id,
        accepted=True,
        message=f"pull started for '{model}'",
    )


@app.get("/v1/setup/advisor/local/ollama/pull/{job_id}", response_model=AdvisorLocalOllamaPullStatusResponse)
def setup_advisor_local_ollama_pull_status(
    job_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorLocalOllamaPullStatusResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_local_ollama_pull_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    with _OLLAMA_PULL_LOCK:
        job = _OLLAMA_PULL_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"unknown pull job_id: {job_id}")
        return AdvisorLocalOllamaPullStatusResponse(
            job_id=job_id,
            state=str(job.get("state") or "unknown"),
            progress=float(job.get("progress") or 0.0),
            error=(str(job.get("error")) if job.get("error") else None),
            completed_at=job.get("completed_at"),
        )


@app.post("/v1/setup/advisor/verify", response_model=AdvisorVerifyResponse)
def setup_advisor_verify(
    body: AdvisorVerifyRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorVerifyResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_verify", method="POST").inc()
    _enforce_workspace_access(auth, db)
    configured = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    primary = body.provider_id.strip().lower()
    profile = advisor_active_profile(
        {
            "active_profile_id": configured.get("active_profile_id"),
            "profiles": configured.get("profiles"),
        }
    )
    chain_routes = advisor_resolve_chain_from_profile(
        profile,
        primary_provider=primary,
        fallback_chain=body.fallback_chain or list(configured.get("advisor_fallback_chain") or []),
    )
    chain = [str(item.get("provider_id") or "") for item in chain_routes]
    if not chain:
        raise HTTPException(status_code=400, detail="no valid advisor provider in chain")

    timeout_ms = int(body.timeout_ms or configured.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms)
    retry_max = max(1, int(body.retry_max or configured.get("advisor_provider_retry_max") or settings.advisor_provider_retry_max))
    custom_headers_raw = body.custom_headers or (
        configured.get("custom_headers") if isinstance(configured.get("custom_headers"), dict) else {}
    )
    custom_headers: dict[str, str] = {}
    for key, value in custom_headers_raw.items():
        k = str(key).strip().lower()
        if k in settings.advisor_custom_headers_allowlist_set and str(value).strip():
            custom_headers[k] = str(value)

    health = _load_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    attempts: list[AdvisorVerifyAttempt] = []
    selected_provider = chain[0]
    selected_model: str | None = (body.model or "").strip() or None
    used_fallback = False
    verify_started = time.perf_counter()
    verify_total_budget_ms = int(getattr(settings, "effective_advisor_total_budget_ms", settings.advisor_total_budget_ms))

    for index, provider_id in enumerate(chain):
        elapsed_ms = int((time.perf_counter() - verify_started) * 1000)
        remaining_ms = verify_total_budget_ms - elapsed_ms
        if index > 0 and remaining_ms < settings.advisor_failover_min_remaining_ms:
            attempts.append(
                AdvisorVerifyAttempt(
                    provider_id=provider_id,
                    ok=False,
                    code="budget_exhausted",
                    message="fallback skipped: insufficient remaining budget",
                    latency_ms=0,
                    models=[],
                )
            )
            break
        adapter = get_provider(provider_id)
        if adapter is None:
            attempts.append(
                AdvisorVerifyAttempt(
                    provider_id=provider_id,
                    ok=False,
                    code="unknown_provider",
                    message=f"provider '{provider_id}' is not registered",
                    latency_ms=0,
                    models=[],
                )
            )
            continue
        route = chain_routes[index] if index < len(chain_routes) else {}
        provider_model = selected_model
        if not provider_model and isinstance(route, dict):
            provider_model = str(route.get("model") or "").strip() or None
        if not provider_model and provider_id == "custom":
            provider_model = str(body.model or configured.get("advisor_custom_model") or "").strip() or None
        if not provider_model and provider_id == primary:
            provider_model = str(configured.get("advisor_primary_model") or "").strip() or None
        provider_base_url = (
            (body.base_url if provider_id in {primary, "custom"} else None)
            or str((route or {}).get("base_url") or "").strip()
            or str(configured.get("advisor_custom_base_url") or "").strip()
            or _provider_default_base_url(provider_id, adapter.metadata.default_base_url)
        )
        provider_api_key = _resolve_route_api_key(
            db=db,
            provider_id=provider_id,
            route=route if isinstance(route, dict) else None,
            config=configured,
            explicit_key_ref=body.api_key_ref if provider_id == primary else None,
            provided_key=body.api_key if provider_id == primary else None,
        )
        request = _provider_request_for(
            provider_id,
            model=provider_model,
            base_url=provider_base_url,
            api_version=(body.api_version or "").strip()
            or str((route or {}).get("api_version") or configured.get("api_version") or adapter.metadata.api_version or ""),
            api_key=provider_api_key,
            timeout_ms=timeout_ms,
            custom_headers=custom_headers,
        )
        result: ProviderAttemptResult | None = None
        for _ in range(retry_max):
            result = adapter.verify(request)
            if result.ok:
                break
            if result.code not in {"rate_limit", "timeout", "network_unreachable", "provider_error"}:
                break
        if result is None:
            continue
        attempts.append(
            AdvisorVerifyAttempt(
                provider_id=result.provider_id,
                ok=result.ok,
                code=result.code,
                message=result.message,
                latency_ms=result.latency_ms,
                models=result.models,
            )
        )
        health = advisor_update_health_state(
            health,
            route={
                "provider_id": provider_id,
                "model": provider_model,
                "priority": index,
                "provider_category": adapter.metadata.provider_category,
            },
            ok=result.ok,
            latency_ms=result.latency_ms,
            error_code=result.code if not result.ok else None,
            open_failures=settings.advisor_circuit_open_failures,
            half_open_seconds=settings.advisor_circuit_half_open_seconds,
            close_successes=settings.advisor_circuit_close_successes,
        )
        if result.ok:
            selected_provider = provider_id
            if not selected_model and result.models:
                selected_model = result.models[0]
            used_fallback = index > 0
            _save_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
            db.commit()
            return AdvisorVerifyResponse(
                ok=True,
                selected_provider=selected_provider,
                selected_model=selected_model,
                used_fallback=used_fallback,
                attempts=attempts,
            )
    _save_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
    db.commit()

    return AdvisorVerifyResponse(
        ok=False,
        selected_provider=selected_provider,
        selected_model=selected_model,
        used_fallback=used_fallback,
        attempts=attempts,
    )


@app.put("/v1/setup/advisor/config", response_model=AdvisorConfigResponse)
def setup_advisor_config(
    body: AdvisorConfigUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorConfigResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_config", method="PUT").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)

    chain = resolve_fallback_chain(body.advisor_primary_provider, body.advisor_fallback_chain)
    if not chain:
        raise HTTPException(status_code=400, detail="advisor_primary_provider must be a known provider")
    primary = chain[0]
    fallback = chain[1:]
    custom_headers: dict[str, str] = {}
    for key, value in body.custom_headers.items():
        k = str(key).strip().lower()
        if k in settings.advisor_custom_headers_allowlist_set and str(value).strip():
            custom_headers[k] = str(value)
    api_key_ref = (body.advisor_custom_api_key_ref or settings.advisor_custom_api_key_ref).strip()

    payload = {
        "advisor_primary_provider": primary,
        "advisor_primary_model": (body.advisor_primary_model or "").strip() or None,
        "advisor_fallback_chain": fallback,
        "advisor_custom_base_url": (body.advisor_custom_base_url or "").strip() or None,
        "advisor_custom_model": (body.advisor_custom_model or "").strip() or None,
        "advisor_custom_api_key_ref": api_key_ref or None,
        "advisor_provider_timeout_ms": max(200, int(body.advisor_provider_timeout_ms)),
        "advisor_provider_retry_max": max(1, int(body.advisor_provider_retry_max)),
        "custom_headers": custom_headers,
        "api_version": (body.api_version or "").strip() or None,
        "key_storage_backend": None,
        "key_present": False,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    routes_in = [
        item.model_dump(mode="json")
        for item in body.routes
    ] if body.routes else [
        {
            "provider_id": primary,
            "model": (body.advisor_primary_model or "").strip() or None,
            "api_key_ref": api_key_ref or None,
            "base_url": (body.advisor_custom_base_url or "").strip() or None,
            "api_version": (body.api_version or "").strip() or None,
            "region_hint": None,
            "priority": 0,
        },
        *[
            {
                "provider_id": provider_id,
                "model": None,
                "api_key_ref": api_key_ref or None,
                "base_url": None,
                "api_version": None,
                "region_hint": None,
                "priority": idx + 1,
            }
            for idx, provider_id in enumerate(fallback)
        ],
    ]
    routes = advisor_enforce_required_category_coverage(
        routes_in,
        required_categories=settings.advisor_required_categories_set,
    )
    route_errors: list[str] = []
    for idx, route in enumerate(routes):
        provider_id = str(route.get("provider_id") or "").strip().lower()
        if not provider_id:
            continue
        adapter = get_provider(provider_id)
        if adapter is None:
            route_errors.append(f"route[{idx}] unknown provider '{provider_id}'")
            continue
        route_key_ref = str(route.get("api_key_ref") or "").strip()
        if not route_key_ref and _provider_requires_key(provider_id):
            if provider_id == primary and api_key_ref:
                route_key_ref = api_key_ref
            else:
                route_key_ref = f"advisor-{provider_id}"
            route["api_key_ref"] = route_key_ref
    if route_errors:
        raise HTTPException(status_code=400, detail="; ".join(route_errors))
    profile_id = str(body.profile_id or payload.get("profile_id") or "default").strip() or "default"
    profile = advisor_normalize_profile(
        {
            "profile_id": profile_id,
            "scope": "workspace",
            "routing_mode": str(body.routing_mode or "adaptive"),
            "failure_policy": str(body.failure_policy or "risk_aware_fail_safe"),
            "routes": routes,
        },
        required_categories=settings.advisor_required_categories_set,
    )
    profile_bundle = advisor_normalize_profile_bundle(
        _read_runtime_setting_dict(db, _advisor_profiles_setting_key(auth.workspace_id, auth.user_id)),
        legacy_config={**payload, "routes": routes},
        required_categories=settings.advisor_required_categories_set,
    )
    merged_profiles: list[dict[str, Any]] = []
    replaced = False
    for item in profile_bundle.get("profiles", []):
        if str(item.get("profile_id") or "") == profile_id:
            merged_profiles.append(profile)
            replaced = True
        else:
            merged_profiles.append(item if isinstance(item, dict) else {})
    if not replaced:
        merged_profiles.append(profile)
    profile_bundle = advisor_normalize_profile_bundle(
        {
            "active_profile_id": profile_id,
            "profiles": merged_profiles,
            "updated_at": payload["updated_at"],
        },
        legacy_config={**payload, "routes": routes},
        required_categories=settings.advisor_required_categories_set,
    )
    active = advisor_active_profile(profile_bundle)
    payload["profile_id"] = str(active.get("profile_id") or profile_id)
    payload["active_profile_id"] = str(profile_bundle.get("active_profile_id") or profile_id)
    payload["routing_mode"] = str(active.get("routing_mode") or "adaptive")
    payload["failure_policy"] = str(active.get("failure_policy") or "risk_aware_fail_safe")
    payload["routes"] = list(active.get("routes") or [])
    payload["profiles"] = list(profile_bundle.get("profiles") or [])
    env_key_present = False
    seen_key_providers: set[str] = set()
    for route in payload["routes"]:
        if not isinstance(route, dict):
            continue
        provider_id = str(route.get("provider_id") or "").strip().lower()
        if not provider_id or provider_id in seen_key_providers:
            continue
        seen_key_providers.add(provider_id)
        if _provider_env_key_present(provider_id):
            env_key_present = True
            break
    if not env_key_present and _provider_env_key_present(primary):
        env_key_present = True
    if body.api_key and str(body.api_key).strip():
        env_key_present = True
    payload["key_present"] = env_key_present
    payload["key_storage_backend"] = "env_file" if env_key_present else None
    try:
        _persist_advisor_config_to_env(
            primary_provider=primary,
            primary_model=payload.get("advisor_primary_model"),
            fallback_chain=list(payload.get("advisor_fallback_chain") or []),
            custom_base_url=payload.get("advisor_custom_base_url"),
            custom_model=payload.get("advisor_custom_model"),
            custom_api_key_ref=payload.get("advisor_custom_api_key_ref"),
            provider_timeout_ms=int(payload.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
            provider_retry_max=int(payload.get("advisor_provider_retry_max") or settings.advisor_provider_retry_max),
            api_key=body.api_key,
            api_key_ref=api_key_ref,
            routes=list(payload.get("routes") or []),
            active_profile_id=payload.get("active_profile_id"),
        )
    except Exception:
        logger.exception("failed to persist advisor config to .env")
        raise HTTPException(status_code=500, detail="failed to persist advisor config to .env")
    _purge_advisor_secret_runtime_settings(db)
    _upsert_runtime_setting(db, _advisor_setup_setting_key(auth.workspace_id, auth.user_id), payload)
    _upsert_runtime_setting(db, _advisor_profiles_setting_key(auth.workspace_id, auth.user_id), profile_bundle)
    db.commit()
    return _config_response_from_raw(payload)


@app.post("/v1/setup/advisor/switch", response_model=AdvisorSwitchResponse)
def setup_advisor_switch(
    body: AdvisorSwitchRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorSwitchResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_switch", method="POST").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)
    config = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    bundle = advisor_normalize_profile_bundle(
        _read_runtime_setting_dict(db, _advisor_profiles_setting_key(auth.workspace_id, auth.user_id)),
        legacy_config=config,
        required_categories=settings.advisor_required_categories_set,
    )
    profile_id = (body.profile_id or "").strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="profile_id is required")
    available = {str(item.get("profile_id") or "") for item in bundle.get("profiles", [])}
    if profile_id not in available:
        raise HTTPException(status_code=404, detail=f"unknown profile_id: {profile_id}")
    if not body.dry_run:
        bundle["active_profile_id"] = profile_id
        bundle["updated_at"] = datetime.now(tz=UTC).isoformat()
        _upsert_runtime_setting(db, _advisor_profiles_setting_key(auth.workspace_id, auth.user_id), bundle)
        active = advisor_active_profile(bundle)
        config["active_profile_id"] = profile_id
        config["profile_id"] = str(active.get("profile_id") or profile_id)
        config["routing_mode"] = str(active.get("routing_mode") or "adaptive")
        config["failure_policy"] = str(active.get("failure_policy") or "risk_aware_fail_safe")
        config["routes"] = list(active.get("routes") or [])
        config["profiles"] = list(bundle.get("profiles") or [])
        config["updated_at"] = bundle["updated_at"]
        try:
            _upsert_env_values(
                {
                    "TCE_ADVISOR_ACTIVE_PROFILE_ID": profile_id,
                    "TCE_ADVISOR_ROUTES_JSON": json.dumps(config["routes"], separators=(",", ":")),
                }
            )
        except Exception:
            logger.exception("failed to persist active advisor profile to .env")
            raise HTTPException(status_code=500, detail="failed to persist active advisor profile to .env")
        _upsert_runtime_setting(db, _advisor_setup_setting_key(auth.workspace_id, auth.user_id), config)
        db.commit()
    return AdvisorSwitchResponse(
        ok=True,
        active_profile_id=profile_id,
        message="dry_run" if body.dry_run else "switched",
        switched_at=datetime.now(tz=UTC),
    )


@app.get("/v1/setup/advisor/runtime/status", response_model=AdvisorRuntimeStatusResponse)
def setup_advisor_runtime_status(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorRuntimeStatusResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_runtime_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    config = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    profile = _normalized_advisor_runtime_profile(config)
    health = _load_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    status = advisor_runtime_status(
        profile,
        health=health,
        total_budget_ms=int(getattr(settings, "effective_advisor_total_budget_ms", settings.advisor_total_budget_ms)),
        attempt_timeout_ms=int(
            getattr(settings, "effective_advisor_attempt_timeout_ms", settings.advisor_attempt_timeout_ms)
        ),
    )
    return AdvisorRuntimeStatusResponse(
        active_profile_id=str(status.get("profile_id") or "default"),
        routing_mode=str(status.get("routing_mode") or "adaptive"),
        failure_policy=str(status.get("failure_policy") or "risk_aware_fail_safe"),
        category_coverage=list(status.get("category_coverage") or []),
        advisor_total_budget_ms=int(
            status.get("advisor_total_budget_ms")
            or getattr(settings, "effective_advisor_total_budget_ms", settings.advisor_total_budget_ms)
        ),
        advisor_attempt_timeout_ms=int(
            status.get("advisor_attempt_timeout_ms")
            or getattr(settings, "effective_advisor_attempt_timeout_ms", settings.advisor_attempt_timeout_ms)
        ),
        routes=[
            AdvisorRuntimeStatusRoute.model_validate(item)
            for item in status.get("routes", [])
            if isinstance(item, dict)
        ],
        generated_at=datetime.now(tz=UTC),
    )


@app.post("/v1/setup/advisor/runtime/probe", response_model=AdvisorRuntimeProbeResponse)
def setup_advisor_runtime_probe(
    body: AdvisorRuntimeProbeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AdvisorRuntimeProbeResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_runtime_probe", method="POST").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)
    config = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    profile = _normalized_advisor_runtime_profile(config)
    if body.profile_id:
        wanted = str(body.profile_id).strip()
        for item in config.get("profiles", []):
            if isinstance(item, dict) and str(item.get("profile_id") or "") == wanted:
                profile = _normalized_advisor_runtime_profile(config, item)
                break
    routes = list(profile.get("routes") or [])[: max(1, int(body.max_probes))]
    attempts: list[AdvisorVerifyAttempt] = []
    health = _load_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
    timeout_ms = int(body.timeout_ms or settings.advisor_attempt_timeout_ms)
    probe_deadline = time.monotonic() + (
        max(1.0, (max(200, timeout_ms) * max(1, len(routes)) / 1000.0) + 0.75)
    )
    custom_headers = config.get("custom_headers") if isinstance(config.get("custom_headers"), dict) else {}

    attempts_by_index: dict[int, AdvisorVerifyAttempt] = {}
    prepared_routes: dict[int, dict[str, Any]] = {}
    for idx, raw_route in enumerate(routes):
        route = raw_route if isinstance(raw_route, dict) else {}
        provider_id = str(route.get("provider_id") or "").strip().lower()
        adapter = get_provider(provider_id)
        if adapter is None:
            attempts_by_index[idx] = AdvisorVerifyAttempt(
                provider_id=provider_id,
                ok=False,
                code="unknown_provider",
                message=f"provider '{provider_id}' is not registered",
                latency_ms=0,
                models=[],
            )
            continue
        provider_api_key = _resolve_route_api_key(
            db=db,
            provider_id=provider_id,
            route=route,
            config=config,
        )
        req = _provider_request_for(
            provider_id,
            model=str(route.get("model") or "").strip() or None,
            base_url=str(route.get("base_url") or adapter.metadata.default_base_url or "").strip() or None,
            api_version=str(route.get("api_version") or adapter.metadata.api_version or "").strip() or None,
            api_key=provider_api_key,
            timeout_ms=max(200, timeout_ms),
            custom_headers=custom_headers,
        )
        prepared_routes[idx] = {
            "provider_id": provider_id,
            "adapter": adapter,
            "request": req,
            "route": route,
        }

    verify_results: dict[int, ProviderAttemptResult] = {}
    future_to_index: dict[Any, int] = {}
    for idx, prepared in prepared_routes.items():
        future = _ADVISOR_PROBE_EXECUTOR.submit(
            _verify_provider_with_retries,
            adapter=prepared["adapter"],
            request=prepared["request"],
            retry_max=1,
        )
        future_to_index[future] = idx

    verify_timeout_s = max(0.3, float(max(200, timeout_ms)) / 1000.0 + 0.35)
    for future, idx in future_to_index.items():
        prepared = prepared_routes[idx]
        provider_id = str(prepared["provider_id"])
        try:
            verify_results[idx] = future.result(timeout=verify_timeout_s)
        except FuturesTimeoutError:
            future.cancel()
            verify_results[idx] = ProviderAttemptResult(
                provider_id=provider_id,
                ok=False,
                code="timeout",
                message="provider verify timed out",
                latency_ms=int(verify_timeout_s * 1000),
                models=[],
            )
        except Exception as exc:
            verify_results[idx] = ProviderAttemptResult(
                provider_id=provider_id,
                ok=False,
                code="provider_error",
                message=f"{type(exc).__name__}: {exc}",
                latency_ms=0,
                models=[],
            )

    for idx, raw_route in enumerate(routes):
        if time.monotonic() > probe_deadline:
            route_budget = raw_route if isinstance(raw_route, dict) else {}
            provider_budget = str(route_budget.get("provider_id") or "unknown").strip().lower() or "unknown"
            attempts.append(
                AdvisorVerifyAttempt(
                    provider_id=provider_budget,
                    ok=False,
                    code="budget_exhausted",
                    message="runtime probe budget exceeded",
                    latency_ms=0,
                    models=[],
                )
            )
            continue
        if idx in attempts_by_index:
            attempts.append(attempts_by_index[idx])
            continue
        prepared = prepared_routes.get(idx)
        if not prepared:
            attempts.append(
                AdvisorVerifyAttempt(
                    provider_id="unknown",
                    ok=False,
                    code="provider_error",
                    message="missing prepared route",
                    latency_ms=0,
                    models=[],
                )
            )
            continue
        route = prepared["route"]
        provider_id = str(prepared["provider_id"])
        adapter = prepared["adapter"]
        req = prepared["request"]
        verify_result = verify_results.get(idx) or ProviderAttemptResult(
            provider_id=provider_id,
            ok=False,
            code="provider_error",
            message="provider verify returned no result",
            latency_ms=0,
            models=[],
        )
        attempt_ok = bool(verify_result.ok)
        attempt_code = str(verify_result.code or "provider_error")
        attempt_message = str(verify_result.message or "")
        attempt_latency_ms = int(verify_result.latency_ms or 0)
        if verify_result.ok and verify_result.message:
            attempt_message = str(verify_result.message)
        attempts.append(
            AdvisorVerifyAttempt(
                provider_id=verify_result.provider_id,
                ok=attempt_ok,
                code=attempt_code,
                message=attempt_message,
                latency_ms=attempt_latency_ms,
                models=verify_result.models,
            )
        )
        health = advisor_update_health_state(
            health,
            route={"provider_id": provider_id, "model": req.model, "priority": idx, "provider_category": adapter.metadata.provider_category},
            ok=attempt_ok,
            latency_ms=attempt_latency_ms,
            error_code=attempt_code if not attempt_ok else None,
            open_failures=settings.advisor_circuit_open_failures,
            half_open_seconds=settings.advisor_circuit_half_open_seconds,
            close_successes=settings.advisor_circuit_close_successes,
        )
    _save_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
    db.commit()
    return AdvisorRuntimeProbeResponse(
        ok=all(item.ok for item in attempts) if attempts else False,
        profile_id=str(profile.get("profile_id") or "default"),
        attempts=attempts,
        generated_at=datetime.now(tz=UTC),
    )


@app.post("/v1/context_bundle", response_model=ContextBundleResponse)
def context_bundle(
    body: ContextBundleRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ContextBundleResponse:
    start = time.perf_counter()
    REQUEST_COUNT.labels(endpoint="context_bundle", method="POST").inc()
    _enforce_workspace_access(auth, db)
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    query_hash = hashlib.sha256(
        json.dumps(
            {
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "body": body.model_dump(mode="json"),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    cached = db.execute(select(ContextBundleCache).where(ContextBundleCache.query_hash == query_hash)).scalar_one_or_none()
    if cached:
        expiry = cached.created_at.timestamp() + cached.ttl_seconds
        if time.time() < expiry:
            cached_bundle = ContextBundleResponse.model_validate(cached.bundle)
            write_audit_log(
                db,
                consumer=auth.consumer,
                action="get_context_bundle_cache_hit",
                query=body.model_dump(mode="json"),
                result_event_ids=cached_bundle.citations,
                policy_decisions=policy_engine.summarize(blocked_count=0, applied_redactions=[], role=auth.role),
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
            _auto_capture_interaction(
                db=db,
                auth=auth,
                action="context_bundle_cache_hit",
                request_payload=body.model_dump(mode="json"),
                response_payload={
                    "citations_count": len(cached_bundle.citations),
                    "cold_start": bool(cached_bundle.policy.get("cold_start", False)),
                },
                citations=cached_bundle.citations,
            )
            return cached_bundle

    bundle, blocked_count, redactions = build_context_bundle(db, body, consumer_ctx, policy_engine)
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="context_bundle", method="POST").observe(latency_ms / 1000)

    cache_bundle = bundle.model_dump(mode="json")
    cache_created_at = datetime.now(tz=UTC)
    cache_ttl = settings.context_bundle_cache_ttl_seconds
    if cached:
        cached.bundle = cache_bundle
        cached.created_at = cache_created_at
        cached.ttl_seconds = cache_ttl
    else:
        db.add(
            ContextBundleCache(
                query_hash=query_hash,
                bundle=cache_bundle,
                created_at=cache_created_at,
                ttl_seconds=cache_ttl,
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        cache_row = db.execute(select(ContextBundleCache).where(ContextBundleCache.query_hash == query_hash)).scalar_one_or_none()
        if cache_row is None:
            raise
        cache_row.bundle = cache_bundle
        cache_row.created_at = cache_created_at
        cache_row.ttl_seconds = cache_ttl
        db.commit()

    write_audit_log(
        db,
        consumer=auth.consumer,
        action="get_context_bundle",
        query=body.model_dump(mode="json"),
        result_event_ids=bundle.citations,
        policy_decisions=policy_engine.summarize(blocked_count=blocked_count, applied_redactions=redactions, role=auth.role),
        latency_ms=latency_ms,
    )
    _auto_capture_interaction(
        db=db,
        auth=auth,
        action="context_bundle",
        request_payload=body.model_dump(mode="json"),
        response_payload={
            "citations_count": len(bundle.citations),
            "blocked_count": blocked_count,
            "cold_start": bool(bundle.policy.get("cold_start", False)),
        },
        citations=bundle.citations,
    )
    return bundle


def _episode_item_from_model(db: Session, episode: Episode) -> EpisodeItem:
    decision_rows = db.execute(
        select(EpisodeDecision).where(EpisodeDecision.episode_id == episode.id).order_by(EpisodeDecision.created_at.asc())
    ).scalars().all()
    lesson = db.get(EpisodeLesson, episode.id)
    event_ids = db.execute(
        select(EpisodeEventLink.event_id).where(EpisodeEventLink.episode_id == episode.id).order_by(EpisodeEventLink.created_at.asc())
    ).scalars().all()
    decisions = [
        {
            "decision": row.decision,
            "why": row.why,
            "alternatives": list(row.alternatives_json or []),
        }
        for row in decision_rows
    ]
    lessons = {
        "do_more": list((lesson.do_more_json if lesson else []) or []),
        "do_less": list((lesson.do_less_json if lesson else []) or []),
        "avoid": list((lesson.avoid_json if lesson else []) or []),
    }
    return EpisodeItem(
        id=episode.id,
        workspace_id=episode.workspace_id,
        user_id=episode.user_id,
        session_id=episode.session_id,
        goal=episode.goal,
        context=episode.context,
        outcome=episode.outcome,
        confidence=float(episode.confidence or 0.0),
        status=episode.status,
        authority_score=float(episode.authority_score or 0.0),
        stability_score=float(episode.stability_score or 0.0),
        decisions=decisions,
        constraints=[],
        lessons=lessons,
        artifacts=[],
        entities=[],
        source_event_ids=list(event_ids),
        created_at=episode.created_at,
        updated_at=episode.updated_at,
    )


def _rule_matches_scope(
    rule_scope: dict[str, Any] | None,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str | None = None,
    domain: str | None = None,
    task_type: str | None = None,
) -> bool:
    scope = rule_scope if isinstance(rule_scope, dict) else {}
    if scope.get("workspace_id") and str(scope.get("workspace_id")) != workspace_id:
        return False
    if scope.get("user_id") and str(scope.get("user_id")) != user_id:
        return False
    if session_id and scope.get("session_id") and str(scope.get("session_id")) != session_id:
        return False
    if domain and scope.get("domain") and str(scope.get("domain")) != domain:
        return False
    if task_type and scope.get("task_type") and str(scope.get("task_type")) != task_type:
        return False
    return True


@app.post("/v1/events/annotate", response_model=EventAnnotationResponse)
def annotate_event(
    body: EventAnnotationRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> EventAnnotationResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="events_annotate", method="POST").inc()
    event = db.get(Event, body.event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    context = event.context if isinstance(event.context, dict) else {}
    if context.get("_tce_workspace") not in {None, auth.workspace_id}:
        raise HTTPException(status_code=403, detail="event not in workspace scope")
    if context.get("_tce_owner") not in {None, auth.user_id}:
        raise HTTPException(status_code=403, detail="event not in user scope")

    authority_level = _authority_level(body.authority_level or event.authority_level)
    event.authority_level = authority_level

    link_episode_id = db.execute(
        select(EpisodeEventLink.episode_id).where(EpisodeEventLink.event_id == body.event_id).limit(1)
    ).scalar_one_or_none()
    episode = db.get(Episode, link_episode_id) if link_episode_id else None
    now = datetime.now(tz=UTC)
    if episode is None:
        episode = Episode(
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=body.session_id,
            goal=(body.goal or event.title or event.task_type or "Untitled goal")[:240],
            context=str((event.payload or {}).get("summary") if isinstance(event.payload, dict) else "")[:1000],
            outcome="",
            confidence=max(0.0, min(1.0, 0.5 + (_authority_score(authority_level) * 0.3))),
            status="open",
            authority_score=_authority_score(authority_level),
            stability_score=_stability_score(
                db,
                workspace_id=auth.workspace_id,
                user_id=auth.user_id,
                domain=event.domain,
                task_type=event.task_type,
            ),
            created_at=now,
            updated_at=now,
        )
        db.add(episode)
        db.flush()
    else:
        if body.goal:
            episode.goal = body.goal[:240]
        episode.authority_score = max(float(episode.authority_score or 0.0), _authority_score(authority_level))
        episode.updated_at = now
        db.flush()

    if body.decision:
        db.add(
            EpisodeDecision(
                episode_id=episode.id,
                decision=body.decision[:500],
                why="user_annotation",
                alternatives_json=list(body.alternatives or []),
                created_at=now,
            )
        )
    if body.constraints:
        merged = [piece.strip() for piece in body.constraints if isinstance(piece, str) and piece.strip()]
        if merged:
            current_ctx = episode.context or ""
            constraints_blob = "\n".join(f"- {item}" for item in merged)
            episode.context = (f"{current_ctx}\nConstraints:\n{constraints_blob}".strip())[:2000]

    lesson = db.get(EpisodeLesson, episode.id)
    if lesson is None:
        lesson = EpisodeLesson(
            episode_id=episode.id,
            do_more_json=[],
            do_less_json=[],
            avoid_json=[],
            updated_at=now,
        )
        db.add(lesson)
    if body.avoid:
        lesson.avoid_json = list({*list(lesson.avoid_json or []), *[value for value in body.avoid if value]})
    lesson.updated_at = now

    has_link = db.execute(
        select(EpisodeEventLink.id)
        .where(EpisodeEventLink.episode_id == episode.id, EpisodeEventLink.event_id == body.event_id)
        .limit(1)
    ).scalar_one_or_none()
    if has_link is None:
        db.add(EpisodeEventLink(episode_id=episode.id, event_id=body.event_id, created_at=now))

    db.commit()
    return EventAnnotationResponse(
        event_id=body.event_id,
        episode_id=episode.id,
        authority_level=authority_level,
        updated=True,
    )


@app.get("/v1/episodes", response_model=EpisodeListResponse)
def list_episodes(
    session_id: str = "default",
    status: str | None = None,
    limit: int = 100,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> EpisodeListResponse:
    REQUEST_COUNT.labels(endpoint="episodes_list", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    stmt = (
        select(Episode)
        .where(
            Episode.workspace_id == auth.workspace_id,
            Episode.user_id == auth.user_id,
            Episode.session_id == effective_session_id,
        )
        .order_by(Episode.updated_at.desc())
        .limit(max(1, min(limit, 300)))
    )
    if status:
        stmt = stmt.where(Episode.status == status)
    rows = db.execute(stmt).scalars().all()
    items = [_episode_item_from_model(db, row) for row in rows]
    return EpisodeListResponse(episodes=items, total=len(items))


@app.get("/v1/episodes/{episode_id}", response_model=EpisodeItem)
def get_episode_details(
    episode_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> EpisodeItem:
    REQUEST_COUNT.labels(endpoint="episodes_get", method="GET").inc()
    _enforce_workspace_access(auth, db)
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if episode.workspace_id != auth.workspace_id or episode.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="episode not in workspace scope")
    return _episode_item_from_model(db, episode)


@app.post("/v1/context/brief", response_model=ContextBriefResponse)
def context_brief(
    body: ContextBriefRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ContextBriefResponse:
    REQUEST_COUNT.labels(endpoint="context_brief", method="POST").inc()
    _enforce_workspace_access(auth, db)
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    bundle_request = ContextBundleRequest(
        task=body.task,
        app_context=body.app_context,
        constraints=body.constraints,
    )
    bundle, _, _ = build_context_bundle(db, bundle_request, consumer_ctx, policy_engine)
    max_items = max(5, min(int(body.max_items), 30))
    domain = body.app_context.get("domain") if isinstance(body.app_context, dict) else None
    task_type = body.app_context.get("task_type") if isinstance(body.app_context, dict) else None
    typed_memory = (
        bundle.structured_context.get("typed_memory", {})
        if isinstance(bundle.structured_context, dict)
        else {}
    )
    typed_facts = [str(item).strip() for item in typed_memory.get("facts", []) if str(item).strip()]
    typed_opinions = [str(item).strip() for item in typed_memory.get("opinions", []) if str(item).strip()]
    typed_skills = [str(item).strip() for item in typed_memory.get("skills", []) if str(item).strip()]

    rules = db.execute(
        select(MemoryRule)
        .where(
            MemoryRule.workspace_id == auth.workspace_id,
            MemoryRule.user_id == auth.user_id,
            MemoryRule.active.is_(True),
        )
        .order_by(MemoryRule.priority.asc(), MemoryRule.updated_at.desc())
        .limit(100)
    ).scalars().all()
    if not rules:
        fallback_rules = db.execute(
            select(MemoryRule)
            .where(
                MemoryRule.workspace_id == auth.workspace_id,
                MemoryRule.active.is_(True),
            )
            .order_by(MemoryRule.priority.asc(), MemoryRule.updated_at.desc())
            .limit(100)
        ).scalars().all()
        fallback_users = {str(rule.user_id or "").strip() for rule in fallback_rules if str(rule.user_id or "").strip()}
        if len(fallback_users) == 1:
            rules = fallback_rules
    scoped_rules = [
        row
        for row in rules
        if _rule_matches_scope(
            row.scope,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=body.session_id,
            domain=domain,
            task_type=task_type,
        )
    ]
    hard_rules = [row for row in scoped_rules if int(row.priority) <= 1]

    standard_approach: list[ContextBriefSectionItem] = []
    for item in bundle.do_dont.get("do", [])[: max_items]:
        standard_approach.append(ContextBriefSectionItem(text=item, citations=[]))
    for item in typed_skills[: max(0, max_items - len(standard_approach))]:
        standard_approach.append(ContextBriefSectionItem(text=item, citations=[]))

    context_tiers_enabled = bool(getattr(settings, "context_tiers_enabled", False))
    current_state = [
        ContextBriefSectionItem(
            text=(
                summarize_hit_text(event.title, event.summary_l0, event.ts)
                if context_tiers_enabled
                else f"{event.title} ({event.ts.isoformat()})"
            ),
            citations=[event.id],
        )
        for event in bundle.evidence_events[:max_items]
    ]
    for item in typed_facts[: max(0, max_items - len(current_state))]:
        current_state.append(ContextBriefSectionItem(text=item, citations=[]))

    constraints_preferences: list[ContextBriefSectionItem] = []
    if settings.memory_rule_p0_always_include:
        for rule in hard_rules[:max_items]:
            constraints_preferences.append(
                ContextBriefSectionItem(text=f"[P{rule.priority}] {rule.statement}", citations=[])
            )
    for item in typed_opinions[: max(0, max_items - len(constraints_preferences))]:
        constraints_preferences.append(ContextBriefSectionItem(text=item, citations=[]))
    for item in bundle.do_dont.get("dont", [])[: max(0, max_items - len(constraints_preferences))]:
        constraints_preferences.append(ContextBriefSectionItem(text=item, citations=[]))

    open_rows = db.execute(
        select(Episode)
        .where(
            Episode.workspace_id == auth.workspace_id,
            Episode.user_id == auth.user_id,
            Episode.session_id == body.session_id,
            Episode.status.in_(["open", "in_progress", "blocked"]),
        )
        .order_by(Episode.updated_at.desc())
        .limit(max_items)
    ).scalars().all()
    open_loops = [
        ContextBriefSectionItem(
            text=f"{row.goal} [{row.status}]",
            citations=[
                value
                for value in db.execute(
                    select(EpisodeEventLink.event_id)
                    .where(EpisodeEventLink.episode_id == row.id)
                    .order_by(EpisodeEventLink.created_at.desc())
                    .limit(2)
                ).scalars().all()
            ],
        )
        for row in open_rows
    ]

    artifacts = [
        ContextBriefSectionItem(text=f"event:{citation}", citations=[citation])
        for citation in bundle.citations[:max_items]
    ]

    merged_citations: list[UUID] = []
    seen: set[UUID] = set()
    for group in (standard_approach, current_state, constraints_preferences, open_loops, artifacts):
        for item in group:
            for citation in item.citations:
                if citation in seen:
                    continue
                seen.add(citation)
                merged_citations.append(citation)
    return ContextBriefResponse(
        summary=bundle.summary,
        standard_approach=standard_approach[:max_items],
        current_state=current_state[:max_items],
        constraints_preferences=constraints_preferences[:max_items],
        open_loops=open_loops[:max_items],
        artifacts=artifacts[:max_items],
        citations=merged_citations,
        context_tier_used=bundle.context_tier_used,
        summary_coverage=bundle.summary_coverage,
        planner_used=bundle.planner_used,
        subquery_count=bundle.subquery_count,
        subquery_labels=bundle.subquery_labels,
        episode_boost_applied=bundle.episode_boost_applied,
        activation_boost_applied=bundle.activation_boost_applied,
        generated_at=datetime.now(tz=UTC),
    )


def _memory_rule_to_item(rule: MemoryRule) -> MemoryRuleItem:
    return MemoryRuleItem(
        id=rule.id,
        workspace_id=rule.workspace_id,
        user_id=rule.user_id,
        scope=rule.scope or {},
        rule_type=rule.rule_type,
        statement=rule.statement,
        priority=int(rule.priority),
        active=bool(rule.active),
        evergreen=bool(getattr(rule, "evergreen", True)),
        expires_at=getattr(rule, "expires_at", None),
        source_episode_id=rule.source_episode_id,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@app.get("/v1/memory/rules", response_model=MemoryRuleListResponse)
def list_memory_rules(
    include_inactive: bool = False,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MemoryRuleListResponse:
    REQUEST_COUNT.labels(endpoint="memory_rules_list", method="GET").inc()
    _enforce_workspace_access(auth, db)
    now = datetime.now(tz=UTC)
    stmt = (
        select(MemoryRule)
        .where(MemoryRule.workspace_id == auth.workspace_id, MemoryRule.user_id == auth.user_id)
        .order_by(MemoryRule.priority.asc(), MemoryRule.updated_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(
            MemoryRule.active.is_(True),
            or_(
                MemoryRule.evergreen.is_(True),
                MemoryRule.expires_at.is_(None),
                MemoryRule.expires_at >= now,
            ),
        )
    rules = db.execute(stmt).scalars().all()
    if not rules:
        fallback_stmt = (
            select(MemoryRule)
            .where(MemoryRule.workspace_id == auth.workspace_id)
            .order_by(MemoryRule.priority.asc(), MemoryRule.updated_at.desc())
        )
        if not include_inactive:
            fallback_stmt = fallback_stmt.where(
                MemoryRule.active.is_(True),
                or_(
                    MemoryRule.evergreen.is_(True),
                    MemoryRule.expires_at.is_(None),
                    MemoryRule.expires_at >= now,
                ),
            )
        fallback_rules = db.execute(fallback_stmt).scalars().all()
        fallback_users = {str(rule.user_id) for rule in fallback_rules if str(rule.user_id).strip()}
        if len(fallback_users) == 1:
            rules = fallback_rules
    items = [_memory_rule_to_item(rule) for rule in rules]
    return MemoryRuleListResponse(rules=items, total=len(items))


@app.post("/v1/memory/rules", response_model=MemoryRuleItem)
def upsert_memory_rule(
    body: MemoryRuleUpsertRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MemoryRuleItem:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="memory_rules_upsert", method="POST").inc()
    now = datetime.now(tz=UTC)
    rule = MemoryRule(
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        scope=body.scope,
        rule_type=str(body.rule_type),
        statement=body.statement.strip(),
        priority=int(body.priority),
        active=True,
        evergreen=bool(body.evergreen),
        expires_at=body.expires_at,
        source_episode_id=body.source_episode_id,
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _memory_rule_to_item(rule)


@app.post("/v1/memory/rules/{rule_id}/deprecate", response_model=MemoryRuleDeprecateResponse)
def deprecate_memory_rule(
    rule_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MemoryRuleDeprecateResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="memory_rules_deprecate", method="POST").inc()
    rule = db.get(MemoryRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="memory rule not found")
    if rule.workspace_id != auth.workspace_id or rule.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="memory rule not in workspace scope")
    rule.active = False
    rule.updated_at = datetime.now(tz=UTC)
    db.commit()
    return MemoryRuleDeprecateResponse(rule_id=rule_id, active=False, updated=True)


@app.post("/v1/memory/forget", response_model=MemoryForgetResponse)
def forget_memory(
    body: MemoryForgetRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MemoryForgetResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="memory_forget", method="POST").inc()
    ids = [str(value).strip() for value in body.target_ids if str(value).strip()]
    deleted_count = 0
    if body.target_type == "event":
        if ids:
            deleted_count = int(
                db.execute(
                    text(
                        """
                        DELETE FROM events
                        WHERE id = ANY(:ids)
                          AND (context->>'_tce_workspace') = :workspace_id
                          AND (context->>'_tce_owner') = :user_id
                        """
                    ),
                    {"ids": ids, "workspace_id": auth.workspace_id, "user_id": auth.user_id},
                ).rowcount
                or 0
            )
    elif body.target_type == "episode":
        if ids:
            deleted_count = int(
                db.execute(
                    text(
                        """
                        DELETE FROM episodes
                        WHERE id = ANY(:ids)
                          AND workspace_id = :workspace_id
                          AND user_id = :user_id
                        """
                    ),
                    {"ids": ids, "workspace_id": auth.workspace_id, "user_id": auth.user_id},
                ).rowcount
                or 0
            )
    elif body.target_type == "rule":
        if ids:
            deleted_count = int(
                db.execute(
                    text(
                        """
                        DELETE FROM memory_rules
                        WHERE id = ANY(:ids)
                          AND workspace_id = :workspace_id
                          AND user_id = :user_id
                        """
                    ),
                    {"ids": ids, "workspace_id": auth.workspace_id, "user_id": auth.user_id},
                ).rowcount
                or 0
            )
    else:
        raise HTTPException(status_code=400, detail="unsupported target_type")

    tombstone = MemoryTombstone(
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        target_type=body.target_type,
        target_ids=ids,
        reason=body.reason,
        requested_by=auth.consumer,
        deleted_at=datetime.now(tz=UTC),
        meta={"hard_delete": bool(body.hard_delete), "deleted_count": deleted_count},
    )
    db.add(tombstone)
    db.commit()
    return MemoryForgetResponse(
        deleted_count=deleted_count,
        tombstone_id=tombstone.id,
        target_type=body.target_type,
        target_ids=ids,
    )


def _retrieval_eval_status_key(workspace_id: str, user_id: str, session_id: str) -> str:
    return f"retrieval_eval_status:{workspace_id}:{user_id}:{session_id}"


def _autonomy_quality_snapshot(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    lookback: int,
) -> dict[str, Any]:
    limit = max(10, int(lookback or 120))
    action_rows = db.execute(
        text(
            """
            SELECT result, meta
            FROM takeover_action_log
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND action_kind = :action_kind
            ORDER BY ts DESC
            LIMIT :limit
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "action_kind": "takeover_step",
            "limit": limit,
        },
    ).mappings().all()
    decision_confidence_values: list[float] = []
    context_quality_values: list[float] = []
    action_log_directive_ids: set[str] = set()
    needs_human_count = 0
    retrieval_trigger_count = 0
    for row in action_rows:
        raw_meta = row.get("meta")
        if isinstance(raw_meta, dict):
            meta = raw_meta
        elif isinstance(raw_meta, str):
            try:
                parsed = json.loads(raw_meta)
            except Exception:
                parsed = {}
            meta = parsed if isinstance(parsed, dict) else {}
        else:
            meta = {}
        if str(row.get("result") or "").strip().lower() == "needs_human" or bool(meta.get("needs_human")):
            needs_human_count += 1
        if bool(meta.get("retrieval_triggered")):
            retrieval_trigger_count += 1
        directive_id = str(meta.get("directive_id") or "").strip()
        if directive_id:
            action_log_directive_ids.add(directive_id)
        decision_confidence_values.append(_safe_float(meta.get("decision_confidence"), default=0.0))
        context_quality_values.append(_safe_float(meta.get("context_quality_score"), default=0.0))

    directive_rows = db.execute(
        text(
            """
            SELECT state, meta
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            ORDER BY updated_at DESC
            LIMIT :limit
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "limit": limit,
        },
    ).mappings().all()
    terminal_states = {
        DirectiveExecutionState.SUCCEEDED.value,
        DirectiveExecutionState.FAILED.value,
        DirectiveExecutionState.BLOCKED.value,
        DirectiveExecutionState.ABANDONED.value,
    }
    execution_states = {
        DirectiveExecutionState.SUCCEEDED.value,
        DirectiveExecutionState.FAILED.value,
        DirectiveExecutionState.BLOCKED.value,
    }
    terminal_count = 0
    execution_count = 0
    success_count = 0
    retry_count = 0
    calibration_abs_errors: list[float] = []
    outcome_feedback_count = 0
    for row in directive_rows:
        state_value = str(row.get("state") or "").strip().lower()
        if state_value in terminal_states:
            terminal_count += 1
        if state_value in execution_states:
            execution_count += 1
            if state_value == DirectiveExecutionState.SUCCEEDED.value:
                success_count += 1
        raw_meta = row.get("meta")
        meta: dict[str, Any]
        if isinstance(raw_meta, dict):
            meta = raw_meta
        elif isinstance(raw_meta, str):
            try:
                parsed = json.loads(raw_meta)
            except Exception:
                parsed = {}
            meta = parsed if isinstance(parsed, dict) else {}
        else:
            meta = {}
        if meta.get("retry_of"):
            retry_count += 1
        if state_value in execution_states:
            if bool(meta.get("outcome_recorded")) or bool(meta.get("observation_id")):
                outcome_feedback_count += 1
            raw_conf = meta.get("decision_confidence")
            conf_value = _safe_float(raw_conf, default=-1.0)
            if conf_value >= 0.0:
                conf_value = max(0.0, min(1.0, conf_value))
                actual = 1.0 if state_value == DirectiveExecutionState.SUCCEEDED.value else 0.0
                calibration_abs_errors.append(abs(conf_value - actual))

    # Keep readiness sample-size stable across takeover resets: directive_executions can
    # be compacted per-session, while takeover_action_log still records emitted directives.
    terminal_count = max(terminal_count, len(action_log_directive_ids))

    calibration_sample_count = len(calibration_abs_errors)
    confidence_alignment = (
        max(0.0, min(1.0, 1.0 - (sum(calibration_abs_errors) / max(1, calibration_sample_count))))
        if calibration_sample_count > 0
        else 0.0
    )

    eval_floor = 0.0
    setting = db.get(RuntimeSetting, _retrieval_eval_status_key(workspace_id, user_id, session_id))
    if setting is not None and isinstance(setting.value, dict):
        latest = setting.value.get("latest")
        if isinstance(latest, dict):
            eval_values = [
                _safe_float(latest.get("style_alignment"), default=0.0),
                _safe_float(latest.get("constraint_compliance"), default=0.0),
                _safe_float(latest.get("decision_traceability"), default=0.0),
                _safe_float(latest.get("followup_reduction"), default=0.0),
            ]
            eval_floor = max(0.0, min(1.0, min(eval_values))) if eval_values else 0.0

    turn_count = len(action_rows)
    return {
        "turn_count": turn_count,
        "terminal_directive_count": terminal_count,
        "execution_success_rate": (
            max(0.0, min(1.0, float(success_count) / max(1, execution_count)))
            if execution_count > 0
            else 0.0
        ),
        "needs_human_rate": (
            max(0.0, min(1.0, float(needs_human_count) / max(1, turn_count)))
            if turn_count > 0
            else 1.0
        ),
        "avg_decision_confidence": (
            max(0.0, min(1.0, sum(decision_confidence_values) / max(1, len(decision_confidence_values))))
            if decision_confidence_values
            else 0.0
        ),
        "avg_context_quality": (
            max(0.0, min(1.0, sum(context_quality_values) / max(1, len(context_quality_values))))
            if context_quality_values
            else 0.0
        ),
        "retry_rate": max(0.0, min(1.0, float(retry_count) / max(1, len(directive_rows)))),
        "retrieval_trigger_rate": (
            max(0.0, min(1.0, float(retrieval_trigger_count) / max(1, turn_count)))
            if turn_count > 0
            else 0.0
        ),
        "eval_floor": eval_floor,
        "confidence_alignment": confidence_alignment,
        "calibration_sample_count": calibration_sample_count,
        "outcome_feedback_rate": (
            max(0.0, min(1.0, float(outcome_feedback_count) / max(1, execution_count)))
            if execution_count > 0
            else 0.0
        ),
        "outcome_feedback_count": outcome_feedback_count,
        "outcome_feedback_sample_count": execution_count,
    }


def _autonomy_readiness_payload(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    lookback = max(20, int(getattr(settings, "autonomy_gate_lookback_turns", 120)))
    metrics = _autonomy_quality_snapshot(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        lookback=lookback,
    )
    thresholds = {
        "min_turns": int(getattr(settings, "autonomy_gate_min_turns", 20)),
        "min_terminal_directives": int(getattr(settings, "autonomy_gate_min_terminal_directives", 8)),
        "min_execution_success_rate": float(getattr(settings, "autonomy_gate_min_execution_success_rate", 0.88)),
        "max_needs_human_rate": float(getattr(settings, "autonomy_gate_max_needs_human_rate", 0.20)),
        "min_avg_decision_confidence": float(getattr(settings, "autonomy_gate_min_avg_decision_confidence", 0.72)),
        "min_avg_context_quality": float(getattr(settings, "autonomy_gate_min_avg_context_quality", 0.70)),
        "min_eval_floor": float(getattr(settings, "autonomy_gate_min_eval_floor", 0.70)),
        "min_confidence_alignment": float(getattr(settings, "autonomy_gate_min_confidence_alignment", 0.55)),
        "min_outcome_feedback_rate": float(getattr(settings, "autonomy_gate_min_outcome_feedback_rate", 0.60)),
        "min_calibration_samples": int(getattr(settings, "autonomy_gate_min_calibration_samples", 20)),
        "min_outcome_feedback_samples": int(getattr(settings, "autonomy_gate_min_outcome_feedback_samples", 20)),
    }
    legacy_outcome_feedback = (
        int(metrics.get("outcome_feedback_sample_count", 0) or 0) > 0
        and int(metrics.get("outcome_feedback_count", 0) or 0) == 0
        and int(metrics.get("calibration_sample_count", 0) or 0) == 0
    )
    confidence_alignment_for_score = float(metrics.get("confidence_alignment", 0.0) or 0.0)
    if int(metrics.get("calibration_sample_count", 0) or 0) < int(thresholds["min_calibration_samples"]):
        confidence_alignment_for_score = float(metrics.get("avg_decision_confidence", 0.0) or 0.0)
    outcome_feedback_for_score = float(metrics.get("outcome_feedback_rate", 0.0) or 0.0)
    if legacy_outcome_feedback or (
        int(metrics.get("outcome_feedback_sample_count", 0) or 0) < int(thresholds["min_outcome_feedback_samples"])
    ):
        outcome_feedback_for_score = float(metrics.get("execution_success_rate", 0.0) or 0.0)
    eval_missing = float(metrics.get("eval_floor", 0.0) or 0.0) <= 0.0
    if eval_missing and int(metrics.get("turn_count", 0) or 0) > 0:
        bootstrap_eval = max(
            0.0,
            min(
                1.0,
                (
                    (0.50 * float(metrics.get("avg_decision_confidence", 0.0) or 0.0))
                    + (0.50 * float(metrics.get("avg_context_quality", 0.0) or 0.0))
                ),
            ),
        )
        metrics["eval_floor"] = round(bootstrap_eval, 4)
    checks = {
        "sample_size": (
            int(metrics["turn_count"]) >= int(thresholds["min_turns"])
            and int(metrics["terminal_directive_count"]) >= int(thresholds["min_terminal_directives"])
        ),
        "execution_success_rate": float(metrics["execution_success_rate"]) >= float(thresholds["min_execution_success_rate"]),
        "needs_human_rate": float(metrics["needs_human_rate"]) <= float(thresholds["max_needs_human_rate"]),
        "avg_decision_confidence": float(metrics["avg_decision_confidence"]) >= float(thresholds["min_avg_decision_confidence"]),
        "avg_context_quality": float(metrics["avg_context_quality"]) >= float(thresholds["min_avg_context_quality"]),
        "eval_floor": float(metrics["eval_floor"]) >= float(thresholds["min_eval_floor"]),
        "confidence_alignment": (
            int(metrics.get("calibration_sample_count", 0) or 0) < int(thresholds["min_calibration_samples"])
            or float(metrics.get("confidence_alignment", 0.0) or 0.0) >= float(thresholds["min_confidence_alignment"])
        ),
        "outcome_feedback_rate": (
            legacy_outcome_feedback
            or int(metrics.get("outcome_feedback_sample_count", 0) or 0) < int(thresholds["min_outcome_feedback_samples"])
            or float(metrics.get("outcome_feedback_rate", 0.0) or 0.0) >= float(thresholds["min_outcome_feedback_rate"])
        ),
    }
    if not checks["sample_size"] and eval_missing:
        checks["eval_floor"] = True
    if not checks["sample_size"]:
        checks["confidence_alignment"] = True
        checks["outcome_feedback_rate"] = True
    gate_passed = all(bool(value) for value in checks.values())
    score = int(
        round(
            max(
                0.0,
                min(
                    100.0,
                    100.0
                    * (
                        (0.25 * float(metrics["execution_success_rate"]))
                        + (0.15 * (1.0 - float(metrics["needs_human_rate"])))
                        + (0.15 * float(metrics["avg_decision_confidence"]))
                        + (0.15 * float(metrics["avg_context_quality"]))
                        + (0.10 * float(metrics["eval_floor"]))
                        + (0.10 * confidence_alignment_for_score)
                        + (0.10 * outcome_feedback_for_score)
                    ),
                ),
            )
        )
    )
    if gate_passed and score >= 85:
        band = "full_product_ready"
    elif score >= 70:
        band = "pilot_ready"
    else:
        band = "not_ready"
    if not checks["sample_size"]:
        status_phase = "warmup"
    elif gate_passed:
        status_phase = "ready"
    else:
        status_phase = "stabilizing"
    failing_checks = [name for name, passed in checks.items() if not passed]
    check_reasons = {
        "sample_size": (
            f"turns={int(metrics['turn_count'])}/{int(thresholds['min_turns'])}, "
            f"directives={int(metrics['terminal_directive_count'])}/{int(thresholds['min_terminal_directives'])}"
        ),
        "execution_success_rate": (
            f"{float(metrics['execution_success_rate']):.2f} >= {float(thresholds['min_execution_success_rate']):.2f}"
        ),
        "needs_human_rate": (
            f"{float(metrics['needs_human_rate']):.2f} <= {float(thresholds['max_needs_human_rate']):.2f}"
        ),
        "avg_decision_confidence": (
            f"{float(metrics['avg_decision_confidence']):.2f} >= {float(thresholds['min_avg_decision_confidence']):.2f}"
        ),
        "avg_context_quality": (
            f"{float(metrics['avg_context_quality']):.2f} >= {float(thresholds['min_avg_context_quality']):.2f}"
        ),
        "eval_floor": (
            (
                "missing retrieval eval window, using bootstrap estimate"
                if eval_missing
                else f"{float(metrics['eval_floor']):.2f} >= {float(thresholds['min_eval_floor']):.2f}"
            )
        ),
        "confidence_alignment": (
            "insufficient calibration samples; collecting live confidence/outcome pairs"
            if int(metrics.get("calibration_sample_count", 0) or 0) < int(thresholds["min_calibration_samples"])
            else (
                f"{float(metrics.get('confidence_alignment', 0.0) or 0.0):.2f} >= "
                f"{float(thresholds['min_confidence_alignment']):.2f}"
            )
        ),
        "outcome_feedback_rate": (
            "legacy session without outcome meta; pass is deferred while new traces are collected"
            if legacy_outcome_feedback
            else (
                "insufficient outcome-feedback samples; collecting execution outcome traces"
                if int(metrics.get("outcome_feedback_sample_count", 0) or 0)
                < int(thresholds["min_outcome_feedback_samples"])
                else (
                    f"{float(metrics.get('outcome_feedback_rate', 0.0) or 0.0):.2f} >= "
                    f"{float(thresholds['min_outcome_feedback_rate']):.2f}"
                )
            )
        ),
    }
    return {
        "session_id": session_id,
        "gate_passed": gate_passed,
        "score": score,
        "band": band,
        "status_phase": status_phase,
        "metrics": metrics,
        "thresholds": thresholds,
        "checks": checks,
        "check_reasons": check_reasons,
        "failing_checks": failing_checks,
        "eval_missing": eval_missing,
        "lookback_turns": lookback,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


def _update_structured_plan_progress_context(
    context: dict[str, Any],
    *,
    execution_state: DirectiveExecutionState,
    updated_at: datetime,
) -> None:
    plan = context.get("structured_plan")
    if not isinstance(plan, dict):
        return
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "pending").strip().lower()
        if status != "pending":
            continue
        if execution_state == DirectiveExecutionState.SUCCEEDED:
            step["status"] = "done"
        elif execution_state == DirectiveExecutionState.ABANDONED:
            step["status"] = "abandoned"
        elif execution_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}:
            step["status"] = "blocked"
        step["updated_at"] = updated_at.isoformat()
        break
    plan["updated_at"] = updated_at.isoformat()
    if all(
        isinstance(step, dict)
        and str(step.get("status") or "").strip().lower() in {"done", "skipped"}
        for step in steps
    ):
        plan["completed_at"] = updated_at.isoformat()
    context["structured_plan"] = plan


def _update_objective_quality_context(
    context: dict[str, Any],
    *,
    execution_state: DirectiveExecutionState,
    retry_scheduled: bool,
    updated_at: datetime,
    details: dict[str, Any] | None = None,
    required_verifications: list[str] | None = None,
) -> None:
    stats = context.get("objective_quality")
    if not isinstance(stats, dict):
        stats = {}
    stats["total_reports"] = int(stats.get("total_reports", 0) or 0) + 1
    state_key = f"{execution_state.value}_count"
    stats[state_key] = int(stats.get(state_key, 0) or 0) + 1
    if retry_scheduled:
        stats["retry_scheduled_count"] = int(stats.get("retry_scheduled_count", 0) or 0) + 1
    terminal_total = sum(
        int(stats.get(f"{state.value}_count", 0) or 0)
        for state in (
            DirectiveExecutionState.SUCCEEDED,
            DirectiveExecutionState.FAILED,
            DirectiveExecutionState.BLOCKED,
        )
    )
    abandoned_total = int(stats.get(f"{DirectiveExecutionState.ABANDONED.value}_count", 0) or 0)
    success_total = int(stats.get(f"{DirectiveExecutionState.SUCCEEDED.value}_count", 0) or 0)
    stats["execution_terminal_count"] = terminal_total
    stats["terminal_directive_count"] = terminal_total + abandoned_total
    stats["abandoned_directive_count"] = abandoned_total
    stats["execution_success_rate"] = round(float(success_total) / max(1, terminal_total), 4) if terminal_total else 0.0
    stats["retry_pressure"] = round(
        float(int(stats.get("retry_scheduled_count", 0) or 0)) / max(1, int(stats.get("total_reports", 1) or 1)),
        4,
    )
    required_checks = [str(item).strip().lower() for item in (required_verifications or []) if str(item).strip()]
    details_map = details if isinstance(details, dict) else {}
    verification_map = details_map.get("verification")
    if isinstance(verification_map, dict):
        normalized_verification: dict[str, bool] = {}
        for key, value in verification_map.items():
            key_name = str(key).strip().lower()
            if not key_name:
                continue
            normalized_verification[key_name] = bool(value)
        if normalized_verification:
            stats["verification_runs"] = int(stats.get("verification_runs", 0) or 0) + 1
            verification_ok = (
                all(bool(normalized_verification.get(item, False)) for item in required_checks)
                if required_checks
                else all(bool(v) for v in normalized_verification.values())
            )
            if verification_ok:
                stats["verification_passed"] = int(stats.get("verification_passed", 0) or 0) + 1
            stats["verification_pass_rate"] = round(
                float(int(stats.get("verification_passed", 0) or 0))
                / max(1, int(stats.get("verification_runs", 0) or 0)),
                4,
            )
            stats["last_verification"] = {
                "checks": normalized_verification,
                "required": required_checks,
                "passed": bool(verification_ok),
                "updated_at": updated_at.isoformat(),
            }
    stats["updated_at"] = updated_at.isoformat()
    context["objective_quality"] = stats


def _required_verification_checks() -> list[str]:
    raw = str(getattr(settings, "autonomy_verification_required_checks", "build,test,lint") or "")
    checks = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return checks or ["build", "test", "lint"]


def _ensure_objective_contract_context(
    context: dict[str, Any],
    *,
    objective: str,
    updated_at: datetime,
) -> None:
    objective_text = str(objective or "").strip()
    if not objective_text:
        return
    current = context.get("objective_contract")
    if isinstance(current, dict):
        same_objective = str(current.get("objective") or "").strip().lower() == objective_text.lower()
        if same_objective:
            return
    contract = {
        "objective": objective_text[:260],
        "completion_state": "in_progress",
        "definition_of_done": [
            {"id": "scope", "label": "Scope accepted", "status": "done"},
            {"id": "implementation", "label": "Implementation completed", "status": "pending"},
            {"id": "verification", "label": "Build/test/lint verification passed", "status": "pending"},
            {"id": "docs", "label": "Docs or handoff notes updated", "status": "pending"},
        ],
        "verification": {
            "required": _required_verification_checks(),
            "status": "pending",
            "passed_checks": [],
            "failed_checks": [],
            "last_run_at": None,
        },
        "created_at": updated_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }
    context["objective_contract"] = contract


def _update_objective_contract_context(
    context: dict[str, Any],
    *,
    execution_state: DirectiveExecutionState,
    details: dict[str, Any] | None,
    updated_at: datetime,
) -> None:
    contract = context.get("objective_contract")
    if not isinstance(contract, dict):
        return
    items = contract.get("definition_of_done")
    if not isinstance(items, list):
        return
    item_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip().lower()
        if item_id:
            item_by_id[item_id] = item
    details_map = details if isinstance(details, dict) else {}

    if execution_state == DirectiveExecutionState.SUCCEEDED and "implementation" in item_by_id:
        item_by_id["implementation"]["status"] = "done"

    docs_updated = bool(details_map.get("docs_updated") or details_map.get("handoff_notes_updated"))
    if docs_updated and "docs" in item_by_id:
        item_by_id["docs"]["status"] = "done"

    verification_block = contract.get("verification")
    if not isinstance(verification_block, dict):
        verification_block = {}
    required_checks = [
        str(item).strip().lower()
        for item in verification_block.get("required", _required_verification_checks())
        if str(item).strip()
    ]
    verification_map = details_map.get("verification")
    if isinstance(verification_map, dict):
        normalized: dict[str, bool] = {}
        for key, value in verification_map.items():
            key_name = str(key).strip().lower()
            if key_name:
                normalized[key_name] = bool(value)
        if normalized:
            passed_checks = [key for key, value in normalized.items() if value]
            failed_checks = [key for key, value in normalized.items() if not value]
            verification_ok = (
                all(bool(normalized.get(item, False)) for item in required_checks)
                if required_checks
                else not failed_checks
            )
            verification_block["status"] = "passed" if verification_ok else "failed"
            verification_block["passed_checks"] = passed_checks
            verification_block["failed_checks"] = failed_checks
            verification_block["last_run_at"] = updated_at.isoformat()
            if "verification" in item_by_id:
                item_by_id["verification"]["status"] = "done" if verification_ok else "blocked"

    contract["verification"] = verification_block
    all_done = all(
        isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "done"
        for item in items
    )
    if all_done:
        contract["completion_state"] = "completed"
        contract["completed_at"] = updated_at.isoformat()
    elif execution_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}:
        contract["completion_state"] = "blocked"
    else:
        contract["completion_state"] = "in_progress"
    contract["updated_at"] = updated_at.isoformat()
    context["objective_contract"] = contract


def _autonomy_project_kpis_payload(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    lookback = max(20, int(getattr(settings, "autonomy_gate_lookback_turns", 120)))
    limit = max(20, lookback)
    action_rows = db.execute(
        text(
            """
            SELECT result
            FROM takeover_action_log
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND action_kind = :action_kind
            ORDER BY ts DESC
            LIMIT :limit
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "action_kind": "takeover_step",
            "limit": limit,
        },
    ).mappings().all()
    needs_human_turns = sum(
        1
        for row in action_rows
        if str(row.get("result") or "").strip().lower() == "needs_human"
    )
    directive_rows = db.execute(
        text(
            """
            SELECT objective_hash, state, meta, updated_at
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            ORDER BY updated_at ASC
            LIMIT :limit
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "limit": limit,
        },
    ).mappings().all()
    terminal_states = {
        DirectiveExecutionState.SUCCEEDED.value,
        DirectiveExecutionState.FAILED.value,
        DirectiveExecutionState.BLOCKED.value,
        DirectiveExecutionState.ABANDONED.value,
    }
    project_state: dict[str, dict[str, Any]] = {}
    verification_runs = 0
    verification_passed = 0
    terminal_directive_count = 0
    for row in directive_rows:
        objective_hash_value = str(row.get("objective_hash") or "").strip()
        state_value = str(row.get("state") or "").strip().lower()
        if state_value in terminal_states:
            terminal_directive_count += 1
        if not objective_hash_value:
            continue
        state_bucket = project_state.setdefault(
            objective_hash_value,
            {"has_success": False, "reopened_after_success": False},
        )
        if state_value == DirectiveExecutionState.SUCCEEDED.value:
            state_bucket["has_success"] = True
        elif state_value in {
            DirectiveExecutionState.FAILED.value,
            DirectiveExecutionState.BLOCKED.value,
            DirectiveExecutionState.ABANDONED.value,
        } and bool(state_bucket.get("has_success")):
            state_bucket["reopened_after_success"] = True
        raw_meta = row.get("meta")
        if isinstance(raw_meta, dict):
            meta = raw_meta
        elif isinstance(raw_meta, str):
            try:
                parsed = json.loads(raw_meta)
            except Exception:
                parsed = {}
            meta = parsed if isinstance(parsed, dict) else {}
        else:
            meta = {}
        verification_summary = meta.get("verification_summary")
        verification_map = verification_summary if isinstance(verification_summary, dict) else meta.get("verification")
        if isinstance(verification_map, dict):
            required_checks = _required_verification_checks()
            verification_runs += 1
            verification_ok = all(bool(verification_map.get(check, False)) for check in required_checks)
            if verification_ok:
                verification_passed += 1

    project_count = len(project_state)
    completed_projects = sum(1 for item in project_state.values() if bool(item.get("has_success")))
    reopened_projects = sum(1 for item in project_state.values() if bool(item.get("reopened_after_success")))
    completion_rate = (
        float(completed_projects) / max(1, project_count)
        if project_count > 0
        else 0.0
    )
    manual_interventions_per_project = (
        float(needs_human_turns) / max(1, project_count)
        if project_count > 0
        else float(needs_human_turns)
    )
    reopen_rate = (
        float(reopened_projects) / max(1, completed_projects)
        if completed_projects > 0
        else 0.0
    )
    verification_pass_rate = (
        float(verification_passed) / max(1, verification_runs)
        if verification_runs > 0
        else 1.0
    )
    thresholds = {
        "min_project_completion_rate": float(getattr(settings, "autonomy_kpi_min_project_completion_rate", 0.80)),
        "max_manual_interventions_per_project": float(
            getattr(settings, "autonomy_kpi_max_manual_interventions_per_project", 1.0)
        ),
        "max_reopen_rate_after_completion": float(
            getattr(settings, "autonomy_kpi_max_reopen_rate_after_completion", 0.10)
        ),
        "min_verification_pass_rate": float(getattr(settings, "autonomy_kpi_min_verification_pass_rate", 0.80)),
    }
    checks = {
        "project_completion_rate": completion_rate >= thresholds["min_project_completion_rate"],
        "manual_interventions_per_project": manual_interventions_per_project <= thresholds["max_manual_interventions_per_project"],
        "reopen_rate_after_completion": reopen_rate <= thresholds["max_reopen_rate_after_completion"],
        "verification_pass_rate": (
            verification_pass_rate >= thresholds["min_verification_pass_rate"]
            if verification_runs > 0
            else True
        ),
    }
    passed = all(bool(value) for value in checks.values())
    band = "project_autonomy_ready" if passed else "project_autonomy_blocked"
    failing_checks = [name for name, ok in checks.items() if not ok]
    return {
        "session_id": session_id,
        "passed": passed,
        "band": band,
        "metrics": {
            "project_count": project_count,
            "completed_project_count": completed_projects,
            "terminal_directive_count": terminal_directive_count,
            "needs_human_turns": needs_human_turns,
            "project_completion_rate": round(max(0.0, min(1.0, completion_rate)), 4),
            "manual_interventions_per_project": round(max(0.0, manual_interventions_per_project), 4),
            "reopen_rate_after_completion": round(max(0.0, min(1.0, reopen_rate)), 4),
            "verification_pass_rate": round(max(0.0, min(1.0, verification_pass_rate)), 4),
            "verification_runs": verification_runs,
        },
        "thresholds": thresholds,
        "checks": checks,
        "failing_checks": failing_checks,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }

@app.post("/v1/retrieval/eval/run", response_model=RetrievalEvalRunResponse)
def run_retrieval_eval(
    body: RetrievalEvalRunRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> RetrievalEvalRunResponse:
    REQUEST_COUNT.labels(endpoint="retrieval_eval_run", method="POST").inc()
    _enforce_workspace_access(auth, db)
    start_ts = datetime.now(tz=UTC)
    task_count = max(1, len(body.tasks))
    rule_count = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM memory_rules
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND active = true
                  AND (
                    evergreen = true
                    OR expires_at IS NULL
                    OR expires_at >= :now
                  )
                """
            ),
            {"workspace_id": auth.workspace_id, "user_id": auth.user_id, "now": datetime.now(tz=UTC)},
        ).scalar()
        or 0
    )
    episode_count = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM episodes
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND session_id = :session_id
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "session_id": body.session_id,
            },
        ).scalar()
        or 0
    )
    quality_snapshot = _autonomy_quality_snapshot(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=body.session_id,
        lookback=max(30, int(getattr(settings, "autonomy_gate_lookback_turns", 120))),
    )
    style_alignment = max(
        0.0,
        min(
            1.0,
            0.30
            + (0.04 * min(task_count, 10))
            + (0.15 * min(rule_count, 20) / 20.0)
            + (0.20 * float(quality_snapshot["avg_decision_confidence"]))
            + (0.10 * float(quality_snapshot["avg_context_quality"])),
        ),
    )
    constraint_compliance = max(
        0.0,
        min(
            1.0,
            0.30
            + (0.15 * min(rule_count, 20) / 20.0)
            + (0.20 * float(quality_snapshot["execution_success_rate"]))
            + (0.15 * (1.0 - float(quality_snapshot["needs_human_rate"])))
            + (0.10 * (1.0 - float(quality_snapshot["retry_rate"])))
            + (0.08 if body.with_brief else 0.0),
        ),
    )
    decision_traceability = max(
        0.0,
        min(
            1.0,
            0.25
            + (0.15 * min(episode_count, 20) / 20.0)
            + (0.20 * float(quality_snapshot["avg_context_quality"]))
            + (0.20 * float(quality_snapshot["eval_floor"]))
            + (0.10 * float(quality_snapshot["execution_success_rate"])),
        ),
    )
    followup_reduction = max(
        0.0,
        min(
            1.0,
            0.20
            + (0.04 * min(task_count, 10))
            + (0.20 * (1.0 - float(quality_snapshot["needs_human_rate"])))
            + (0.20 * float(quality_snapshot["execution_success_rate"]))
            + (0.06 * (1.0 - float(quality_snapshot["retrieval_trigger_rate"])))
            + (0.10 if body.with_brief else 0.0),
        ),
    )
    result = RetrievalEvalRunResponse(
        run_id=uuid.uuid4(),
        session_id=body.session_id,
        style_alignment=round(style_alignment, 4),
        constraint_compliance=round(constraint_compliance, 4),
        decision_traceability=round(decision_traceability, 4),
        followup_reduction=round(followup_reduction, 4),
        started_at=start_ts,
        completed_at=datetime.now(tz=UTC),
    )
    key = _retrieval_eval_status_key(auth.workspace_id, auth.user_id, body.session_id)
    setting = db.get(RuntimeSetting, key)
    current_payload = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    history = current_payload.get("history", []) if isinstance(current_payload, dict) else []
    if not isinstance(history, list):
        history = []
    history.insert(0, result.model_dump(mode="json"))
    history = history[:20]
    payload = {
        "latest": result.model_dump(mode="json"),
        "history": history,
        "quality_snapshot": quality_snapshot,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    if setting is None:
        setting = RuntimeSetting(key=key, value=payload, updated_at=datetime.now(tz=UTC))
        db.add(setting)
    else:
        setting.value = payload
        setting.updated_at = datetime.now(tz=UTC)
    db.commit()
    return result


@app.get("/v1/retrieval/eval/status", response_model=RetrievalEvalStatusResponse)
def retrieval_eval_status(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> RetrievalEvalStatusResponse:
    REQUEST_COUNT.labels(endpoint="retrieval_eval_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    key = _retrieval_eval_status_key(auth.workspace_id, auth.user_id, effective_session_id)
    setting = db.get(RuntimeSetting, key)
    payload = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    latest = payload.get("latest")
    history = payload.get("history", [])
    return RetrievalEvalStatusResponse(
        session_id=effective_session_id,
        latest=RetrievalEvalRunResponse.model_validate(latest) if isinstance(latest, dict) else None,
        history=[
            RetrievalEvalRunResponse.model_validate(item)
            for item in history
            if isinstance(item, dict)
        ],
    )


def _row_ts(row: Any) -> Any:
    """Access ts from ORM object or dict-like mapping."""
    return row["ts"] if isinstance(row, dict) else getattr(row, "ts", None)


def _row_payload(row: Any) -> dict:
    """Access payload from ORM object or dict-like mapping."""
    p = row["payload"] if isinstance(row, dict) else getattr(row, "payload", None)
    return p if isinstance(p, dict) else {}


def _compute_hourly_buckets(rows: list) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for row in rows:
        ts = _row_ts(row)
        if ts is None:
            continue
        hour_key = ts.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        buckets[hour_key] = buckets.get(hour_key, 0) + 1
    return dict(sorted(buckets.items()))


def _compute_outcome_metrics(rows: list) -> dict[str, int]:
    counter: dict[str, int] = {}
    for row in rows:
        outcome = _row_payload(row).get("outcome")
        if isinstance(outcome, str) and outcome.strip():
            key = outcome.strip().lower()
            counter[key] = counter.get(key, 0) + 1
    return counter


def _compute_top_errors(rows: list, max_errors: int = 3) -> list[str]:
    error_counter: dict[str, int] = {}
    for row in rows:
        error = _row_payload(row).get("error")
        if isinstance(error, str) and error.strip():
            key = error.strip()
            error_counter[key] = error_counter.get(key, 0) + 1
    sorted_errors = sorted(error_counter.items(), key=lambda x: x[1], reverse=True)
    return [err for err, _count in sorted_errors[:max_errors]]


@app.get("/v1/summary/activity", response_model=ActivitySummaryResponse)
def activity_summary(
    period: str = "today",
    domain: str | None = None,
    max_events: int = 400,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ActivitySummaryResponse:
    REQUEST_COUNT.labels(endpoint="activity_summary", method="GET").inc()
    _enforce_workspace_access(auth, db)
    start_ts, end_ts = _summary_window(period)
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )

    act_params: dict[str, Any] = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "max_sensitivity": consumer_ctx.max_sensitivity,
        "row_limit": max(10, min(max_events, 2000)),
    }
    domain_clause = ""
    if domain:
        domain_clause = "AND domain = :f_domain"
        act_params["f_domain"] = domain

    rows = db.execute(
        text(f"""
            SELECT id, ts, domain, task_type, event_type, title, payload, context
            FROM events
            WHERE ts >= :start_ts AND ts <= :end_ts
              AND sensitivity <= :max_sensitivity
              {domain_clause}
            ORDER BY ts DESC
            LIMIT :row_limit
        """),
        act_params,
    ).mappings().all()

    domain_counter: CollectionCounter[str] = CollectionCounter()
    task_counter: CollectionCounter[str] = CollectionCounter()
    event_type_counter: CollectionCounter[str] = CollectionCounter()
    highlights: list[str] = []
    citations: list[UUID] = []
    blocked_scope = 0

    for row in rows:
        ctx = row["context"]
        if isinstance(ctx, dict):
            workspace = ctx.get("_tce_workspace")
            owner = ctx.get("_tce_owner")
            if workspace and workspace != auth.workspace_id:
                blocked_scope += 1
                continue
            if owner and owner != auth.user_id:
                blocked_scope += 1
                continue
        domain_counter[row["domain"]] += 1
        task_counter[row["task_type"]] += 1
        event_type_counter[row["event_type"]] += 1
        citations.append(row["id"])
        title = row["title"].strip()
        if title and title not in highlights:
            highlights.append(title)
        if len(highlights) >= 12:
            break

    total = sum(domain_counter.values())
    top_domains = ", ".join([f"{name} ({count})" for name, count in domain_counter.most_common(3)]) or "none"
    top_tasks = ", ".join([f"{name} ({count})" for name, count in task_counter.most_common(3)]) or "none"
    summary = (
        f"Activity summary for {period}: {total} events captured. "
        f"Top domains: {top_domains}. Top task types: {top_tasks}."
    )
    policy_summary = policy_engine.summarize(blocked_count=blocked_scope, applied_redactions=[], role=auth.role)
    policy_summary["workspace_id"] = auth.workspace_id
    policy_summary["owner_id"] = auth.user_id

    # Compute new analytics from scoped rows
    citation_set = set(citations)
    scoped_rows = [dict(r) for r in rows if r["id"] in citation_set]
    hourly_buckets = _compute_hourly_buckets(scoped_rows)
    outcome_metrics = _compute_outcome_metrics(scoped_rows)
    top_errors = _compute_top_errors(scoped_rows)

    # Top entities + contradiction count — combined queries using ANY(:ids)
    top_entities: list[dict[str, Any]] = []
    contradiction_count = 0
    if citations:
        cit_ids = citations[:200]
        try:
            entity_rows = db.execute(
                text("""
                    SELECT en.entity_key, en.entity_type, COUNT(*) as ref_count
                    FROM event_entity_links eel
                    JOIN entity_nodes en ON en.id = eel.entity_id
                    WHERE eel.event_id = ANY(:ids)
                      AND eel.workspace_id = :workspace_id
                    GROUP BY en.entity_key, en.entity_type
                    ORDER BY ref_count DESC
                    LIMIT 10
                """),
                {"ids": cit_ids, "workspace_id": auth.workspace_id},
            ).mappings().all()
            top_entities = [
                {"key": r["entity_key"], "type": r["entity_type"], "count": int(r["ref_count"])}
                for r in entity_rows
            ]
        except Exception as exc:
            logger.warning("entity hotspot query failed: %s", exc)

        try:
            ctr_row = db.execute(
                text("""
                    SELECT COUNT(*) as cnt
                    FROM event_relationships
                    WHERE relationship_type = 'contradicts'
                      AND workspace_id = :workspace_id
                      AND source_event_id = ANY(:ids)
                """),
                {"workspace_id": auth.workspace_id, "ids": cit_ids},
            ).mappings().first()
            contradiction_count = int(ctr_row["cnt"]) if ctr_row else 0
        except Exception as exc:
            logger.warning("contradiction count query failed: %s", exc)

    write_audit_log(
        db,
        consumer=auth.consumer,
        action="get_activity_summary",
        query={"period": period, "domain": domain, "max_events": max_events},
        result_event_ids=citations,
        policy_decisions=policy_summary,
        latency_ms=0,
    )

    return ActivitySummaryResponse(
        period=period,
        start_ts=start_ts,
        end_ts=end_ts,
        total_events=total,
        by_domain=dict(domain_counter),
        by_task_type=dict(task_counter),
        by_event_type=dict(event_type_counter),
        highlights=highlights,
        summary=summary,
        citations=citations,
        policy=policy_summary,
        hourly_buckets=hourly_buckets,
        outcome_metrics=outcome_metrics,
        top_errors=top_errors,
        top_entities=top_entities,
        contradiction_count=contradiction_count,
    )


@app.get("/v1/patterns", response_model=list[PatternItem])
def get_patterns(
    domain: str | None = None,
    min_confidence: float = 0.5,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[PatternItem]:
    REQUEST_COUNT.labels(endpoint="patterns", method="GET").inc()
    _enforce_workspace_access(auth, db)
    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    pat_params: dict[str, Any] = {"min_conf": min_confidence}
    domain_filter = ""
    if domain:
        domain_filter = "AND domain = :f_domain"
        pat_params["f_domain"] = domain

    rows = db.execute(
        text(f"""
            SELECT id, domain, pattern_type, statement, confidence, status, evidence_event_ids
            FROM patterns
            WHERE confidence >= :min_conf {domain_filter}
            ORDER BY updated_at DESC
            LIMIT 50
        """),
        pat_params,
    ).mappings().all()

    # Batch-fetch evidence event contexts to avoid N+1 queries
    all_evidence_ids: list[Any] = []
    for row in rows:
        if row["evidence_event_ids"]:
            all_evidence_ids.extend(row["evidence_event_ids"][:10])
    evidence_contexts: dict[Any, dict] = {}
    if all_evidence_ids:
        ev_rows = db.execute(
            text("SELECT id, context FROM events WHERE id = ANY(:ids)"),
            {"ids": all_evidence_ids},
        ).fetchall()
        for ev in ev_rows:
            evidence_contexts[ev[0]] = ev[1] if isinstance(ev[1], dict) else {}

    results: list[PatternItem] = []
    for row in rows:
        if not policy_engine.evaluate(consumer_ctx, row["domain"], 1).allow:
            continue
        if row["evidence_event_ids"]:
            scoped = False
            for evidence_id in row["evidence_event_ids"][:10]:
                ctx = evidence_contexts.get(evidence_id)
                if ctx is None:
                    continue
                workspace = ctx.get("_tce_workspace")
                owner = ctx.get("_tce_owner")
                if (not workspace or workspace == auth.workspace_id) and (not owner or owner == auth.user_id):
                    scoped = True
                    break
            if not scoped:
                continue
        results.append(
            PatternItem(
                id=row["id"],
                domain=row["domain"],
                pattern_type=row["pattern_type"],
                statement=row["statement"],
                confidence=row["confidence"],
                status=row["status"],
                evidence_event_ids=row["evidence_event_ids"],
            )
        )
    return results


@app.post("/v1/patterns/feedback")
def submit_pattern_feedback(
    body: PatternFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="patterns_feedback", method="POST").inc()
    _enforce_workspace_access(auth, db)
    pattern = db.get(Pattern, body.pattern_id)
    if not pattern:
        raise HTTPException(status_code=404, detail="pattern not found")

    feedback = PatternFeedback(
        pattern_id=body.pattern_id,
        approved=body.approved,
        note=body.note,
        created_at=datetime.now(tz=UTC),
    )
    db.add(feedback)

    if body.approved and pattern.confidence >= 0.8:
        pattern.status = "active"
    elif body.approved:
        pattern.status = "needs_review"
    else:
        pattern.status = "deprecated"

    db.commit()
    return {"status": "recorded", "pattern_status": pattern.status, "consumer": auth.consumer}


def _compute_graph_health_status(db: Session, *, workspace_id: str, owner_id: str) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    window_minutes = max(5, int(getattr(settings, "graph_health_alert_threshold_zero_entities_minutes", 30)))
    window_start = now - timedelta(minutes=window_minutes)
    entity_total = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM entity_nodes
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        ).scalar()
        or 0
    )
    link_total = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM event_entity_links
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        ).scalar()
        or 0
    )
    searchable_entity_count = int(
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT en.id)
                FROM entity_nodes en
                JOIN event_entity_links eel ON eel.entity_id = en.id
                WHERE en.workspace_id = :workspace_id
                  AND en.owner_id = :owner_id
                  AND eel.workspace_id = :workspace_id
                  AND eel.owner_id = :owner_id
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        ).scalar()
        or 0
    )
    entity_created_window = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM entity_nodes
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND created_at >= :window_start
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "window_start": window_start},
        ).scalar()
        or 0
    )
    link_created_window = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM event_entity_links
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND created_at >= :window_start
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "window_start": window_start},
        ).scalar()
        or 0
    )
    setting_key = f"graph_health_status:{workspace_id}:{owner_id}"
    previous = db.execute(
        text("SELECT value FROM runtime_settings WHERE key = :key LIMIT 1"),
        {"key": setting_key},
    ).scalar()
    previous_payload = previous if isinstance(previous, dict) else {}
    if isinstance(previous, str):
        try:
            decoded = json.loads(previous)
            previous_payload = decoded if isinstance(decoded, dict) else {}
        except Exception:
            previous_payload = {}
    zero_since: str | None
    if searchable_entity_count <= 0:
        prior_zero_since = str(previous_payload.get("zero_entities_since") or "").strip()
        zero_since = prior_zero_since or now.isoformat()
    else:
        zero_since = None
    zero_minutes = 0.0
    if zero_since:
        try:
            zero_minutes = max(0.0, (now - datetime.fromisoformat(zero_since)).total_seconds() / 60.0)
        except Exception:
            zero_minutes = 0.0
    alert = bool(searchable_entity_count <= 0 and zero_minutes >= window_minutes)
    status = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "window_minutes": window_minutes,
        "entity_total": entity_total,
        "link_total": link_total,
        "searchable_entity_count": searchable_entity_count,
        "entity_creation_rate_per_min": round(float(entity_created_window) / float(window_minutes), 4),
        "link_creation_rate_per_min": round(float(link_created_window) / float(window_minutes), 4),
        "entity_created_in_window": entity_created_window,
        "link_created_in_window": link_created_window,
        "zero_entities_since": zero_since,
        "zero_entities_minutes": round(zero_minutes, 2),
        "alert": alert,
        "generated_at": now.isoformat(),
    }
    db.execute(
        text(
            """
            INSERT INTO runtime_settings(key, value, updated_at)
            VALUES (:key, CAST(:value AS jsonb), :updated_at)
            ON CONFLICT (key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """
        ),
        {"key": setting_key, "value": json.dumps(status), "updated_at": now},
    )
    db.commit()
    return status


@app.get("/v1/graph/health/status", response_model=dict)
def graph_health_status(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="graph_health_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    try:
        return _compute_graph_health_status(db, workspace_id=auth.workspace_id, owner_id=auth.user_id)
    except Exception:
        return {
            "workspace_id": auth.workspace_id,
            "owner_id": auth.user_id,
            "alert": False,
            "error": "graph_health_unavailable",
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }


@app.get("/v1/graph/entities", response_model=GraphEntitySearchResponse)
def graph_entities(
    query: str,
    k: int = 20,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> GraphEntitySearchResponse:
    REQUEST_COUNT.labels(endpoint="graph_entities", method="GET").inc()
    _enforce_workspace_access(auth, db)
    try:
        entities = search_entities(
            db=db,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            query=query,
            limit=k,
        )
    except Exception:
        entities = []
    return GraphEntitySearchResponse(
        query=query,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        entities=entities,
    )


@app.get("/v1/graph/event/{event_id}", response_model=GraphEventResponse)
def graph_event(
    event_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> GraphEventResponse:
    REQUEST_COUNT.labels(endpoint="graph_event", method="GET").inc()
    _enforce_workspace_access(auth, db)
    try:
        graph = graph_for_event(
            db=db,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            event_id=event_id,
        )
    except Exception:
        graph = {"entities": [], "relationships": [], "facts": []}
    return GraphEventResponse(
        event_id=event_id,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        graph=graph,
    )


@app.get("/v1/team/memberships", response_model=list[dict])
def team_memberships(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[dict]:
    REQUEST_COUNT.labels(endpoint="team_memberships_get", method="GET").inc()
    _enforce_workspace_access(auth, db)
    try:
        return list_team_memberships(db=db, workspace_id=auth.workspace_id)
    except Exception:
        return []


@app.post("/v1/team/memberships", response_model=dict)
def upsert_membership(
    body: TeamMembershipUpsertRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="team_memberships_post", method="POST").inc()
    _enforce_workspace_access(auth, db)
    try:
        return upsert_team_membership(
            db=db,
            workspace_id=auth.workspace_id,
            user_id=body.user_id,
            role=body.role,
            added_by=auth.user_id,
            active=body.active,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"team membership store unavailable: {exc}") from exc


@app.post("/v1/clone/advice", response_model=CloneAdviceResponse)
def clone_advice(
    body: CloneAdviceRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CloneAdviceResponse:
    REQUEST_COUNT.labels(endpoint="clone_advice", method="POST").inc()
    _enforce_workspace_access(auth, db)
    mode = get_runtime_mode(db)
    interaction_id = normalize_interaction_id(body.interaction_id)
    effective_constraints = dict(body.constraints or {})
    if body.takeover_context:
        effective_constraints["takeover_context"] = body.takeover_context
    if body.message_delta:
        effective_constraints["message_delta"] = body.message_delta
    if body.executor_output and "latest_executor_output" not in effective_constraints:
        effective_constraints["latest_executor_output"] = body.executor_output

    if mode.mode != OperationMode.CLONE_ADVISOR:
        if body.allow_fallback and settings.clone_fallback_to_timeline:
            consumer_ctx = policy_engine.resolve_consumer(
                auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
            )
            bundle, blocked_count, redactions = build_context_bundle(
                db,
                ContextBundleRequest(task=body.task, app_context=body.app_context, constraints=effective_constraints),
                consumer_ctx,
                policy_engine,
            )
            summary, recommended_actions, confidence, evidence_strength, conflict_flags = derive_clone_guidance(
                bundle, body.executor_output
            )
            conflict_flags = [*conflict_flags, "clone_mode_disabled_fallback_to_timeline"]
            response = CloneAdviceResponse(
                interaction_id=interaction_id,
                guidance_summary=f"Fallback to timeline_only: {summary}",
                recommended_actions=recommended_actions,
                do=bundle.do_dont.get("do", []),
                dont=bundle.do_dont.get("dont", []),
                confidence=confidence,
                evidence_strength=evidence_strength,
                citations=bundle.citations,
                conflict_flags=conflict_flags,
                loop_guard={
                    "allowed": True,
                    "reason": "mode_fallback_to_timeline",
                    "turns_in_last_hour": 0,
                    "max_turns": settings.clone_max_turns_per_interaction,
                },
                policy={
                    **policy_engine.summarize(blocked_count=blocked_count, applied_redactions=redactions, role=auth.role),
                    "mode": mode.mode.value,
                    "fallback": "timeline_only",
                },
            )
            write_agent_interaction(
                db,
                interaction_id=interaction_id,
                source_consumer=auth.consumer,
                source_role=auth.role.value,
                target_role=AgentRole.EXECUTOR.value,
                action="clone_advice_fallback",
                citations=response.citations,
                payload=response.model_dump(mode="json"),
                allowed=True,
                reason="mode_fallback_to_timeline",
            )
            write_audit_log(
                db,
                consumer=auth.consumer,
                action="clone_advice_fallback",
                query=body.model_dump(mode="json"),
                result_event_ids=response.citations,
                policy_decisions=response.policy,
                latency_ms=0,
            )
            _auto_capture_interaction(
                db=db,
                auth=auth,
                action="clone_advice_fallback",
                request_payload=body.model_dump(mode="json"),
                response_payload={
                    "confidence": response.confidence,
                    "evidence_strength": response.evidence_strength,
                    "conflict_flags": response.conflict_flags,
                    "loop_guard": response.loop_guard,
                },
                citations=response.citations,
            )
            return response

        raise HTTPException(
            status_code=409,
            detail={
                "error": "clone_mode_disabled",
                "message": "clone advisor mode is disabled",
                "current_mode": mode.mode.value,
                "action": "Set mode to clone_advisor or allow fallback to timeline_only",
            },
        )

    loop_guard_max_turns = int(settings.clone_max_turns_per_interaction)
    if isinstance(body.takeover_context, dict) and body.takeover_context:
        loop_guard_max_turns = max(
            loop_guard_max_turns,
            int(getattr(settings, "takeover_clone_max_turns_per_interaction", 80)),
        )
    allowed, reason, turns = evaluate_loop_guard(db, interaction_id, max_turns=loop_guard_max_turns)
    if not allowed:
        write_agent_interaction(
            db,
            interaction_id=interaction_id,
            source_consumer=auth.consumer,
            source_role=auth.role.value,
            target_role=AgentRole.EXECUTOR.value,
            action="clone_advice_blocked",
            citations=[],
            payload={"reason": reason, "turns": turns},
            allowed=False,
            reason=reason,
        )
        blocked_response = CloneAdviceResponse(
            interaction_id=interaction_id,
            guidance_summary="Loop guard blocked additional advisor turns. Require human decision.",
            recommended_actions=["Pause advisor feedback", "Request explicit user arbitration"],
            do=[],
            dont=["Do not continue autonomous advisor loop"],
            confidence=0.0,
            evidence_strength="blocked",
            citations=[],
            conflict_flags=["loop_guard_block"],
            loop_guard={
                "allowed": False,
                "reason": reason,
                "turns_in_last_hour": turns,
                "max_turns": loop_guard_max_turns,
            },
            policy={"mode": mode.mode.value},
        )
        _auto_capture_interaction(
            db=db,
            auth=auth,
            action="clone_advice_blocked",
            request_payload=body.model_dump(mode="json"),
            response_payload={
                "confidence": blocked_response.confidence,
                "evidence_strength": blocked_response.evidence_strength,
                "conflict_flags": blocked_response.conflict_flags,
                "loop_guard": blocked_response.loop_guard,
            },
            citations=blocked_response.citations,
        )
        return blocked_response

    consumer_ctx = policy_engine.resolve_consumer(
        auth.consumer, auth.role, workspace_id=auth.workspace_id, owner_id=auth.user_id
    )
    bundle, blocked_count, redactions = build_context_bundle(
        db,
        ContextBundleRequest(task=body.task, app_context=body.app_context, constraints=effective_constraints),
        consumer_ctx,
        policy_engine,
    )

    # --- Build clone context for whichever advisor execution path is configured ---
    # Default path is client-side advisor reasoning via MCP.
    # Optional cloud/local advisor routing is configured through /v1/setup/advisor/*.
    # We always return prompt + context so external executors and advisors stay deterministic.
    clone_context = None
    advisor_decision: dict[str, Any] | None = None
    if settings.clone_reasoning_enabled:
        try:
            from .clone_prompt import build_clone_prompt

            situation_type = classify_situation(
                body.task,
                semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
                semantic_threshold=float(getattr(settings, "semantic_classifier_situation_threshold", 0.61)),
                semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
            )
            similar_obs = query_similar_observations(
                db,
                consumer_id=auth.consumer,
                workspace_id=auth.workspace_id,
                subject_user_id=auth.behavior_subject_id,
                situation_type=situation_type,
                situation_text=body.task,
            )
            fingerprint = load_fingerprint(db, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
            if fingerprint is None:
                fingerprint = DEFAULT_FINGERPRINT
            session_ctx = build_session_context_from_state(
                body.app_context if isinstance(body.app_context, dict) else {}
            )
            clone_prompt = build_clone_prompt(
                user_name=settings.clone_user_name,
                fingerprint=fingerprint,
                similar_observations=similar_obs,
                session_context=session_ctx,
                current_situation=body.task,
                situation_type=situation_type,
                extra_context=body.executor_output or "",
            )
            clone_context = {
                "clone_prompt": clone_prompt,
                "situation_type": situation_type,
                "fingerprint": fingerprint,
                "similar_observations": similar_obs,
                "session_context": session_ctx,
            }
            if settings.advisor_router_v2_enabled:
                advisor_cfg = _load_advisor_config(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
                advisor_health = _load_advisor_health(db, workspace_id=auth.workspace_id, user_id=auth.user_id)
                profile = advisor_active_profile(
                    {
                        "active_profile_id": advisor_cfg.get("active_profile_id"),
                        "profiles": advisor_cfg.get("profiles"),
                    }
                )
                selected = advisor_select_route(
                    profile,
                    health=advisor_health,
                    total_budget_ms=int(
                        getattr(settings, "effective_advisor_total_budget_ms", settings.advisor_total_budget_ms)
                    ),
                    min_remaining_ms=settings.advisor_failover_min_remaining_ms,
                )
                ordered_routes = [item for item in selected.get("ordered", []) if isinstance(item, dict)]
                advisor_decision, inference_meta = _advisor_runtime_reason_from_routes(
                    db=db,
                    auth=auth,
                    config=advisor_cfg,
                    health=advisor_health,
                    routes=ordered_routes,
                    fingerprint=fingerprint,
                    similar_observations=similar_obs,
                    session_context=session_ctx,
                    current_situation=body.task,
                    situation_type=situation_type,
                    extra_context=body.executor_output or "",
                )
                clone_context["advisor_runtime"] = {
                    "profile_id": profile.get("profile_id"),
                    "routing_mode": profile.get("routing_mode"),
                    "selected_route": selected.get("selected"),
                    "route_scores": selected.get("scores"),
                    "inference": inference_meta,
                }
            if not advisor_decision and body.allow_fallback:
                try:
                    legacy_gateway = get_model_gateway(settings)
                    legacy_decision = advisor_reason(
                        gateway=legacy_gateway,
                        user_name=settings.clone_user_name,
                        fingerprint=fingerprint,
                        similar_observations=similar_obs,
                        session_context=session_ctx,
                        current_situation=body.task,
                        situation_type=situation_type,
                        extra_context=body.executor_output or "",
                    )
                    if (
                        str((legacy_decision or {}).get("decision") or "").strip()
                        and not bool((legacy_decision or {}).get("fallback"))
                    ):
                        advisor_decision = legacy_decision
                    runtime_meta = clone_context.get("advisor_runtime") if isinstance(clone_context.get("advisor_runtime"), dict) else {}
                    runtime_meta["legacy_fallback_used"] = True
                    runtime_meta["legacy_model_provider"] = str(getattr(settings, "model_provider", "ollama"))
                    clone_context["advisor_runtime"] = runtime_meta
                except Exception:
                    logging.getLogger(__name__).warning("Legacy advisor gateway fallback failed", exc_info=True)
            elif not advisor_decision:
                runtime_meta = (
                    clone_context.get("advisor_runtime")
                    if isinstance(clone_context.get("advisor_runtime"), dict)
                    else {}
                )
                runtime_meta["legacy_fallback_skipped"] = True
                clone_context["advisor_runtime"] = runtime_meta
        except Exception:
            logging.getLogger(__name__).warning("Clone context build failed", exc_info=True)

    summary, recommended_actions, confidence, evidence_strength, conflict_flags = derive_clone_guidance(
        bundle, body.executor_output
    )
    if advisor_decision:
        llm_decision = str(advisor_decision.get("decision") or "").strip()
        if llm_decision:
            summary = llm_decision
            if not recommended_actions:
                recommended_actions = [llm_decision]
        llm_actions = advisor_decision.get("recommended_actions")
        if isinstance(llm_actions, list):
            clean_actions = [str(item).strip() for item in llm_actions if str(item).strip()]
            if clean_actions:
                recommended_actions = clean_actions[:5]
        llm_conf_raw = advisor_decision.get("confidence")
        try:
            llm_conf = float(llm_conf_raw)
            if math.isfinite(llm_conf):
                confidence = _clamp_confidence(llm_conf, low=0.0, high=1.0)
        except (TypeError, ValueError):
            pass
        citation_count = len(bundle.citations)
        if citation_count >= 5 and confidence >= 0.65:
            evidence_strength = "strong"
        elif citation_count < 2 or confidence < 0.45:
            evidence_strength = "weak"
        else:
            evidence_strength = "moderate"
        if "advisor_runtime_llm" not in conflict_flags:
            conflict_flags.append("advisor_runtime_llm")

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
            "max_turns": loop_guard_max_turns,
        },
        policy=policy_engine.summarize(blocked_count=blocked_count, applied_redactions=redactions, role=auth.role),
        clone_context=clone_context,
        evidence_observations=(clone_context or {}).get("similar_observations", []),
    )

    write_agent_interaction(
        db,
        interaction_id=interaction_id,
        source_consumer=auth.consumer,
        source_role=auth.role.value,
        target_role=AgentRole.EXECUTOR.value,
        action="clone_advice",
        citations=response.citations,
        payload=response.model_dump(mode="json"),
        allowed=True,
        reason="ok",
    )

    write_audit_log(
        db,
        consumer=auth.consumer,
        action="clone_advice",
        query=body.model_dump(mode="json"),
        result_event_ids=response.citations,
        policy_decisions=response.policy,
        latency_ms=0,
    )
    _auto_capture_interaction(
        db=db,
        auth=auth,
        action="clone_advice",
        request_payload=body.model_dump(mode="json"),
        response_payload={
            "confidence": response.confidence,
            "evidence_strength": response.evidence_strength,
            "conflict_flags": response.conflict_flags,
            "loop_guard": response.loop_guard,
        },
        citations=response.citations,
    )
    return response


def _rebuild_behavior_fingerprint_full(db: Session, *, workspace_id: str, subject_user_id: str) -> None:
    evidence = load_behavior_evidence(
        db,
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        limit=5000,
        eligible_only=True,
    )
    fingerprint = DEFAULT_FINGERPRINT.copy()
    for item in evidence:
        fingerprint = merge_observation_into_fingerprint(
            fingerprint,
            {
                "situation_type": item.get("situation_type"),
                "user_response": item.get("selected_choice") or item.get("user_response"),
                "response_reasoning": item.get("response_reasoning"),
                "outcome": item.get("outcome"),
                "outcome_sentiment": item.get("outcome_sentiment"),
            },
        )
    save_fingerprint(
        db,
        consumer_id=subject_user_id,
        workspace_id=workspace_id,
        fingerprint=fingerprint,
        observation_count=len(evidence),
    )


def _store_behavior_evidence_full(
    *,
    body: BehaviorEvidenceRequest,
    auth: AuthContext,
    db: Session,
) -> BehaviorEvidenceResponse:
    if not bool(getattr(settings, "behavior_evidence_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior evidence capture is disabled")
    raw = body.model_dump(mode="python")
    if body.evidence_source.value in {"explicit", "correction", "calibration"} and not raw.get("confirmed_at"):
        raw["confirmed_at"] = datetime.now(tz=UTC)
    normalized = normalize_behavior_evidence(raw)
    storage_gate = behavior_storage_gate(
        normalized,
        threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)),
    )
    prior_evidence: list[dict[str, Any]] = []
    shadow_prediction: dict[str, Any] | None = None
    shadow_latency_ms = 0
    if bool(getattr(settings, "behavior_shadow_evaluation_enabled", True)):
        prior_evidence = load_behavior_evidence(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            limit=500,
            eligible_only=True,
        )
        shadow_started = time.perf_counter()
        shadow_prediction = predict_behavior(
            prior_evidence,
            {
                "situation_type": normalized["situation_type"],
                "situation_summary": normalized["situation_summary"],
                "objective_text": normalized["objective_text"],
                "constraints": normalized["constraints"],
                "context_snapshot": normalized["context_snapshot"],
            },
            candidate_choices=list(normalized.get("available_choices") or []),
            min_confidence=float(getattr(settings, "behavior_prediction_min_confidence", 0.55)),
        )
        shadow_latency_ms = max(0, int((time.perf_counter() - shadow_started) * 1000))
    mode = str(getattr(settings, "behavior_storage_gate_mode", "shadow") or "shadow").strip().lower()
    if mode not in {"shadow", "warn", "enforce"}:
        mode = "shadow"
    warnings: list[str] = []
    if not storage_gate["learning_eligible"]:
        message = "evidence stored for audit but excluded from behavioral learning"
        if mode == "enforce":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "behavior_storage_gate_rejected",
                    "message": message,
                    "storage_gate": storage_gate,
                },
            )
        if mode == "warn":
            warnings.append(message)
    review_pending = bool(
        getattr(settings, "behavior_memory_review_enabled", True)
        and storage_gate["learning_eligible"]
        and normalized.get("evidence_source") in {"inferred", "backfill"}
    )
    if review_pending:
        storage_gate = {
            **storage_gate,
            "learning_eligible": False,
            "decision": "pending_review",
            "reasons": [*list(storage_gate.get("reasons") or []), "human_review_required"],
        }
        warnings.append("evidence is pending memory review and cannot influence behavior yet")
    supersedes = normalized.get("supersedes_observation_id")
    if supersedes:
        target_exists = db.execute(
            text(
                """
                SELECT 1 FROM decision_observations
                WHERE id = :observation_id
                  AND workspace_id = :workspace_id
                  AND subject_user_id = :subject_user_id
                """
            ),
            {
                "observation_id": supersedes,
                "workspace_id": auth.workspace_id,
                "subject_user_id": auth.behavior_subject_id,
            },
        ).scalar()
        if not target_exists:
            raise HTTPException(status_code=404, detail="superseded observation not found in workspace")
    observation_id = save_behavior_evidence(
        db,
        consumer_id=auth.consumer,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        evidence=normalized,
        storage_gate=storage_gate,
    )
    review_id: UUID | None = None
    if bool(getattr(settings, "behavior_memory_review_enabled", True)):
        review_status = "pending" if review_pending else ("promoted" if storage_gate["learning_eligible"] else "rejected")
        review_id = create_memory_review(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            target_type="evidence",
            target_id=observation_id,
            title=str(normalized.get("situation_summary") or normalized.get("objective_text") or "Behavior evidence"),
            rationale=str(normalized.get("rationale") or ""),
            source=str(normalized.get("evidence_source") or "unknown"),
            score=float(storage_gate.get("score", 0.0) or 0.0),
            status=review_status,
        )
    shadow_prediction_id: UUID | None = None
    if shadow_prediction is not None:
        shadow_prediction_id = save_shadow_prediction(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            observation_id=observation_id,
            actual_choice=str(normalized["selected_choice"]),
            query={
                "situation_type": normalized["situation_type"],
                "situation_summary": normalized["situation_summary"],
                "objective_text": normalized["objective_text"],
            },
            prediction=shadow_prediction,
            evidence_count=len(prior_evidence),
            latency_ms=shadow_latency_ms,
        )
    if storage_gate["learning_eligible"]:
        fingerprint = load_fingerprint(db, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
        if fingerprint is None:
            fingerprint = DEFAULT_FINGERPRINT.copy()
        fingerprint = merge_observation_into_fingerprint(
            fingerprint,
            {
                "situation_type": normalized["situation_type"],
                "user_response": normalized["selected_choice"],
                "response_reasoning": normalized["rationale"],
                "outcome": normalized["outcome"],
                "outcome_sentiment": normalized["outcome_sentiment"],
            },
        )
        observation_count = int(
            db.execute(
                text(
                    """
                    SELECT COUNT(1) FROM decision_observations
                    WHERE workspace_id = :workspace_id
                      AND subject_user_id = :subject_user_id
                      AND learning_eligible = true
                    """
                ),
                {"workspace_id": auth.workspace_id, "subject_user_id": auth.behavior_subject_id},
            ).scalar()
            or 0
        )
        save_fingerprint(
            db,
            consumer_id=auth.behavior_subject_id,
            workspace_id=auth.workspace_id,
            fingerprint=fingerprint,
            observation_count=observation_count,
        )
    return BehaviorEvidenceResponse(
        observation_id=observation_id,
        stored=True,
        learning_eligible=bool(storage_gate["learning_eligible"]),
        storage_score=float(storage_gate["score"]),
        storage_decision=str(storage_gate["decision"]),
        storage_reasons=list(storage_gate.get("reasons") or []),
        warnings=warnings,
        redaction_applied=bool(normalized.get("redaction_applied", False)),
        superseded_observation_id=body.supersedes_observation_id,
        review_id=review_id,
        shadow_prediction_id=shadow_prediction_id,
    )


@app.post("/v1/behavior/evidence", response_model=BehaviorEvidenceResponse)
def record_behavior_evidence(
    body: BehaviorEvidenceRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorEvidenceResponse:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_evidence", method="POST").inc()
    _enforce_workspace_access(auth, db)
    return _store_behavior_evidence_full(body=body, auth=auth, db=db)


def _behavior_projection_full(
    *,
    auth: AuthContext,
    db: Session,
    view: str,
    format_name: BehaviorProjectionFormat,
    topic: str | None = None,
    observation_id: UUID | None = None,
) -> BehaviorProjectionResponse:
    if not bool(getattr(settings, "behavior_projections_enabled", False)):
        raise HTTPException(status_code=404, detail="behavior projections are disabled")
    started = time.perf_counter()
    if observation_id is not None:
        evidence_item = load_behavior_evidence_by_id(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            observation_id=observation_id,
        )
        if evidence_item is None:
            raise HTTPException(status_code=404, detail="behavior evidence was not found")
        evidence = [evidence_item]
    else:
        evidence = load_behavior_evidence(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            limit=5000,
            eligible_only=view != "review",
        )
    try:
        projection = build_behavior_projection(
            evidence,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            view=view,
            format_name=format_name.value,
            topic=topic,
            observation_id=str(observation_id) if observation_id else None,
        )
    except BehaviorProjectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response = BehaviorProjectionResponse.model_validate(projection)
    latency_ms = int((time.perf_counter() - started) * 1000)
    REQUEST_LATENCY.labels(endpoint=f"behavior_projection_{view}", method="GET").observe(latency_ms / 1000)
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_projection_read",
        query={
            "workspace_id": auth.workspace_id,
            "behavior_subject_id": auth.behavior_subject_id,
            "projection_id": str(response.projection_id),
            "uri": response.uri,
            "view": response.view.value,
            "topic": response.topic,
            "observation_id": response.observation_id,
            "source_revision": response.source_revision,
            "source_evidence_ids": [str(item) for item in response.source_evidence_ids],
        },
        result_event_ids=[],
        policy_decisions={
            "role": auth.role.value,
            "trust_level": response.trust_level,
            "read_only": response.read_only,
            "projection_learning_eligible": response.projection_learning_eligible,
        },
        latency_ms=latency_ms,
    )
    return response


@app.get("/v1/behavior/projections/current", response_model=BehaviorProjectionResponse)
def current_behavior_projection(
    format: BehaviorProjectionFormat = BehaviorProjectionFormat.MARKDOWN,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_current", method="GET").inc()
    _enforce_workspace_access(auth, db)
    return _behavior_projection_full(auth=auth, db=db, view="current", format_name=format)


@app.get("/v1/behavior/projections/decisions/{topic}", response_model=BehaviorProjectionResponse)
def behavior_decisions_projection(
    topic: str,
    format: BehaviorProjectionFormat = BehaviorProjectionFormat.MARKDOWN,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_decisions", method="GET").inc()
    _enforce_workspace_access(auth, db)
    if len(topic) > 120 or any(ord(char) < 32 for char in topic):
        raise HTTPException(status_code=422, detail="topic must be 1 to 120 printable characters")
    return _behavior_projection_full(
        auth=auth,
        db=db,
        view="decisions",
        format_name=format,
        topic=topic,
    )


@app.get("/v1/behavior/projections/evidence/{observation_id}", response_model=BehaviorProjectionResponse)
def behavior_evidence_projection(
    observation_id: UUID,
    format: BehaviorProjectionFormat = BehaviorProjectionFormat.JSON,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_evidence", method="GET").inc()
    _enforce_workspace_access(auth, db)
    return _behavior_projection_full(
        auth=auth,
        db=db,
        view="evidence",
        format_name=format,
        observation_id=observation_id,
    )


@app.get("/v1/behavior/projections/review", response_model=BehaviorProjectionResponse)
def behavior_review_projection(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_review", method="GET").inc()
    _enforce_workspace_access(auth, db)
    return _behavior_projection_full(
        auth=auth,
        db=db,
        view="review",
        format_name=BehaviorProjectionFormat.HTML,
    )


@app.get("/v1/behavior/projections/review.html", response_class=HTMLResponse)
def behavior_review_projection_html(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    response = behavior_review_projection(auth=auth, db=db)
    return HTMLResponse(
        content=response.content,
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def _require_behavior_projection_pilot() -> None:
    if not bool(getattr(settings, "behavior_projection_pilot_enabled", False)):
        raise HTTPException(status_code=404, detail="behavior projection pilot is disabled")


@app.post(
    "/v1/behavior/projections/pilot/assign",
    response_model=BehaviorPilotAssignmentResponse,
)
def assign_behavior_projection_pilot(
    body: BehaviorPilotAssignmentRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorPilotAssignmentResponse:
    _require_behavior_projection_pilot()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="behavior_projection_pilot_assign", method="POST").inc()
    raw_request = body.model_dump(mode="json")
    sanitized, redaction_applied = sanitize_behavior_pilot_payload(raw_request)
    if not isinstance(sanitized, dict):
        raise HTTPException(status_code=422, detail="invalid behavior pilot request")
    request_digest = behavior_pilot_outcome_digest(sanitized)
    variant = assign_behavior_pilot_variant(
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        trial_key=str(sanitized["trial_key"]),
        assignment_salt=str(getattr(settings, "behavior_pilot_assignment_salt", "tce-behavior-pilot-v1")),
    )
    started = time.perf_counter()
    evidence = load_behavior_evidence(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=5000,
        eligible_only=True,
    )
    context = prepare_behavior_pilot_context(
        evidence,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        request=sanitized,
        variant=variant,
    )
    retrieval_latency_ms = int((time.perf_counter() - started) * 1000)
    assigned_at = datetime.now(tz=UTC)
    expires_at = assigned_at + timedelta(
        days=max(1, int(getattr(settings, "behavior_pilot_assignment_ttl_days", 30)))
    )
    try:
        assignment, inserted = create_or_get_behavior_pilot_assignment(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            owner_id=auth.user_id,
            request=sanitized,
            request_digest=request_digest,
            variant=variant.value,
            context_payload=dict(context["context_payload"]),
            context_sha256=str(context["context_sha256"]),
            source_revision=str(context["source_revision"]),
            citations=[str(item) for item in context["citations"]],
            injected_tokens=int(context["injected_tokens"]),
            retrieval_latency_ms=retrieval_latency_ms,
            redaction_applied=bool(redaction_applied),
            assigned_at=assigned_at,
            expires_at=expires_at,
        )
    except BehaviorPilotConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response = BehaviorPilotAssignmentResponse(
        assignment_id=assignment["id"],
        variant=assignment["variant"],
        assigned_at=assignment["assigned_at"],
        expires_at=assignment["expires_at"],
        context_payload=assignment["context_payload"],
        citations=assignment["citations"],
        source_revision=assignment["source_revision"],
        context_sha256=assignment["context_sha256"],
        injected_tokens=assignment["injected_tokens"],
        retrieval_latency_ms=assignment["retrieval_latency_ms"],
    )
    REQUEST_LATENCY.labels(endpoint="behavior_projection_pilot_assign", method="POST").observe(
        retrieval_latency_ms / 1000
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_projection_pilot_assignment",
        query={
            "workspace_id": auth.workspace_id,
            "behavior_subject_id": auth.behavior_subject_id,
            "assignment_id": str(response.assignment_id),
            "trial_key": sanitized["trial_key"],
            "variant": response.variant.value,
            "source_revision": response.source_revision,
            "source_evidence_ids": [str(item) for item in response.citations],
        },
        result_event_ids=[],
        policy_decisions={
            "role": auth.role.value,
            "idempotent_replay": not inserted,
            "projection_learning_eligible": False,
            "redaction_applied": bool(redaction_applied),
        },
        latency_ms=retrieval_latency_ms,
    )
    return response


@app.post(
    "/v1/behavior/projections/pilot/outcome",
    response_model=BehaviorPilotOutcomeResponse,
)
def report_behavior_projection_pilot_outcome(
    body: BehaviorPilotOutcomeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorPilotOutcomeResponse:
    _require_behavior_projection_pilot()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="behavior_projection_pilot_outcome", method="POST").inc()
    sanitized, redaction_applied = sanitize_behavior_pilot_payload(body.model_dump(mode="json"))
    if not isinstance(sanitized, dict):
        raise HTTPException(status_code=422, detail="invalid behavior pilot outcome")
    digest = behavior_pilot_outcome_digest(sanitized)
    now = datetime.now(tz=UTC)
    evidence = load_behavior_evidence(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=5000,
        eligible_only=True,
    )
    active_ids = {
        str(item.get("id"))
        for item in eligible_behavior_evidence(evidence, at=now)
        if item.get("id")
    }
    try:
        outcome, inserted = record_behavior_pilot_outcome(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            reporter_id=auth.user_id,
            assignment_id=body.assignment_id,
            outcome=sanitized,
            outcome_digest=digest,
            active_evidence_ids=active_ids,
            redaction_applied=bool(redaction_applied),
            reported_at=now,
        )
    except BehaviorPilotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BehaviorPilotConflict, BehaviorPilotExpired) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response = BehaviorPilotOutcomeResponse(
        assignment_id=outcome["assignment_id"],
        outcome_id=outcome["id"],
        recorded=True,
        stale_evidence_used=bool(outcome["stale_evidence_used"]),
        reported_at=outcome["reported_at"],
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_projection_pilot_outcome",
        query={
            "workspace_id": auth.workspace_id,
            "behavior_subject_id": auth.behavior_subject_id,
            "assignment_id": str(response.assignment_id),
            "outcome_id": str(response.outcome_id),
        },
        result_event_ids=[],
        policy_decisions={
            "role": auth.role.value,
            "idempotent_replay": not inserted,
            "stale_evidence_used": response.stale_evidence_used,
            "redaction_applied": bool(redaction_applied),
            "promoted_to_behavior_evidence": False,
        },
        latency_ms=0,
    )
    return response


@app.get(
    "/v1/behavior/projections/pilot/status",
    response_model=BehaviorPilotStatusResponse,
)
def behavior_projection_pilot_status(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorPilotStatusResponse:
    _require_behavior_projection_pilot()
    _enforce_workspace_access(auth, db)
    REQUEST_COUNT.labels(endpoint="behavior_projection_pilot_status", method="GET").inc()
    rows = list_behavior_pilot_rows(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
    )
    status_payload = behavior_pilot_status(
        rows,
        min_window_days=max(1, int(getattr(settings, "behavior_pilot_min_window_days", 28))),
        min_completed_per_arm=max(
            1, int(getattr(settings, "behavior_pilot_min_completed_per_arm", 30))
        ),
        min_completion_coverage=float(
            getattr(settings, "behavior_pilot_min_completion_coverage", 0.80)
        ),
        max_p95_retrieval_latency_ms=float(
            getattr(settings, "behavior_pilot_max_p95_retrieval_latency_ms", 120.0)
        ),
        max_top1_degradation=float(
            getattr(settings, "behavior_pilot_max_top1_degradation", 0.05)
        ),
    )
    return BehaviorPilotStatusResponse.model_validate(status_payload)


@app.post("/v1/behavior/predict", response_model=BehaviorPredictionResponse)
def predict_behavior_choice(
    body: BehaviorPredictionRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorPredictionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_predict", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_prediction_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior prediction is disabled")
    evidence = load_behavior_evidence(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=2000,
        eligible_only=True,
    )
    prediction = predict_behavior(
        evidence,
        {
            "situation_type": body.situation_type,
            "situation_summary": body.situation_summary,
            "objective_text": body.objective,
            "constraints": body.constraints,
            "context_snapshot": body.context_snapshot,
        },
        candidate_choices=body.candidate_choices,
        min_confidence=max(
            body.min_confidence,
            float(getattr(settings, "behavior_prediction_min_confidence", 0.55)),
        ),
    )
    gate = latest_fidelity_gate(db, workspace_id=auth.workspace_id, subject_user_id=auth.behavior_subject_id)
    if bool(getattr(settings, "behavior_autonomy_gate_enabled", False)) and not bool(gate.get("passed", False)):
        prediction.update(
            {
                "predicted_choice": None,
                "abstained": True,
                "needs_clarification": True,
                "clarification_question": "Behavior fidelity is not validated yet. What choice should be made?",
            }
        )
    return BehaviorPredictionResponse(**prediction, fidelity_gate=gate)


@app.post("/v1/behavior/evaluate", response_model=BehaviorEvaluationResponse)
def run_behavior_evaluation(
    body: BehaviorEvaluationRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorEvaluationResponse:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_evaluate", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_fidelity_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior fidelity evaluation is disabled")
    config = body.model_dump(mode="json")
    evidence = load_behavior_evidence(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=body.max_cases,
        eligible_only=True,
    )
    result = evaluate_behavior_fidelity(evidence, **body.model_dump(mode="python"))
    run_id, created_at = save_fidelity_run(
        db,
        consumer_id=auth.consumer,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        config=config,
        result=result,
    )
    return BehaviorEvaluationResponse(
        run_id=run_id,
        status=str(result["status"]),
        metrics=dict(result["metrics"]),
        gate=dict(result["gate"]),
        case_results=list(result["case_results"]),
        config=config,
        created_at=created_at,
        duration_ms=int(result["duration_ms"]),
    )


@app.get("/v1/behavior/evaluations", response_model=BehaviorEvaluationListResponse)
def behavior_evaluations(
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorEvaluationListResponse:
    REQUEST_COUNT.labels(endpoint="behavior_evaluations", method="GET").inc()
    _enforce_workspace_access(auth, db)
    rows = list_fidelity_runs(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=limit,
    )
    return BehaviorEvaluationListResponse(
        runs=[
            BehaviorEvaluationResponse(
                run_id=row["id"],
                status=str(row["status"]),
                metrics=dict(row.get("metrics_json") or {}),
                gate=dict(row.get("gate_json") or {}),
                case_results=list(row.get("case_results_json") or []),
                config=dict(row.get("config_json") or {}),
                created_at=row["created_at"],
                duration_ms=int(row.get("duration_ms", 0) or 0),
                schema_version=str(row.get("schema_version") or "v1"),
            )
            for row in rows
        ]
    )


@app.get("/v1/behavior/calibration/scenarios", response_model=BehaviorCalibrationScenariosResponse)
def behavior_calibration_scenarios(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorCalibrationScenariosResponse:
    REQUEST_COUNT.labels(endpoint="behavior_calibration_scenarios", method="GET").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_calibration_enabled", False)):
        raise HTTPException(status_code=404, detail="behavior calibration is disabled")
    return BehaviorCalibrationScenariosResponse(scenarios=[dict(item) for item in CALIBRATION_SCENARIOS])


@app.post("/v1/behavior/calibration/answer", response_model=BehaviorEvidenceResponse)
def answer_behavior_calibration(
    body: BehaviorCalibrationAnswerRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorEvidenceResponse:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_calibration_answer", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_calibration_enabled", False)):
        raise HTTPException(status_code=404, detail="behavior calibration is disabled")
    scenario = next((dict(item) for item in CALIBRATION_SCENARIOS if item["id"] == body.scenario_id), None)
    if scenario is None:
        raise HTTPException(status_code=404, detail="calibration scenario not found")
    if body.selected_choice not in scenario["choices"]:
        raise HTTPException(status_code=422, detail="selected_choice must be one of the scenario choices")
    evidence_body = BehaviorEvidenceRequest(
        situation_type=scenario["situation_type"],
        situation_summary=scenario["objective"],
        objective=scenario["objective"],
        available_choices=list(scenario["choices"]),
        selected_choice=body.selected_choice,
        rationale=body.rationale,
        action_taken=body.action_taken,
        memory_class=scenario["memory_class"],
        evidence_source=BehaviorEvidenceSource.CALIBRATION,
        confirmed_at=datetime.now(tz=UTC),
    )
    return _store_behavior_evidence_full(body=evidence_body, auth=auth, db=db)


@app.post("/v1/capabilities/grants", response_model=CapabilityGrantResponse)
def capability_grant_create(
    body: CapabilityGrantRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CapabilityGrantResponse:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="capability_grant_create", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_capability_broker_enabled", True)):
        raise HTTPException(status_code=404, detail="capability broker is disabled")
    payload = body.model_dump(mode="python")
    if body.ttl_seconds == 120:
        payload["ttl_seconds"] = int(getattr(settings, "capability_grant_ttl_seconds", 120))
    response = CapabilityGrantResponse(
        **issue_capability_grant(
            db,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            body=payload,
        )
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="capability_grant_create",
        query={
            "grant_id": str(response.grant_id),
            "capability": response.capability,
            "action": response.action,
            "resource": response.resource,
            "action_digest": response.action_digest,
            "mutating": response.mutating,
        },
        result_event_ids=[],
        policy_decisions={
            "decision": response.decision,
            "status": response.status,
            "risk_tier": response.risk_tier,
            "role": auth.role.value,
        },
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return response


@app.post("/v1/capabilities/consume", response_model=CapabilityConsumeResponse)
def capability_grant_consume(
    body: CapabilityConsumeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CapabilityConsumeResponse:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="capability_grant_consume", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_capability_broker_enabled", True)):
        raise HTTPException(status_code=404, detail="capability broker is disabled")
    response = CapabilityConsumeResponse(
        **consume_capability_grant(
            db,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            body=body.model_dump(mode="python"),
        )
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="capability_grant_consume",
        query={
            "grant_id": str(response.grant_id),
            "action_digest": response.action_digest,
        },
        result_event_ids=[],
        policy_decisions={
            "authorized": response.authorized,
            "status": response.status,
            "reason": response.reason,
            "role": auth.role.value,
        },
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return response


@app.post("/v1/behavior/processes/mine", response_model=ProcessMiningResponse)
def mine_behavior_processes(
    body: ProcessMiningRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProcessMiningResponse:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_processes_mine", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_process_mining_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior process mining is disabled")
    rows = load_process_source_rows(
        db,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        lookback_days=body.lookback_days,
        limit=body.max_sequences,
    )
    min_support = max(body.min_support, int(getattr(settings, "behavior_process_min_support", 2)))
    models, duration_ms = mine_and_time(rows, min_support=min_support, max_steps=body.max_steps)
    stored = save_process_models(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        models=models,
    )
    if bool(getattr(settings, "behavior_memory_review_enabled", True)):
        for model in stored:
            create_memory_review(
                db,
                workspace_id=auth.workspace_id,
                subject_user_id=auth.behavior_subject_id,
                target_type="process_model",
                target_id=UUID(str(model["process_id"])),
                title=str(model["name"]),
                rationale=f"Observed in {model['support']} sessions with reliability {model['reliability']:.2f}",
                source="process_mining",
                score=float(model["reliability"]),
            )
        stored = list_process_models(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            status=None,
            limit=100,
        )
    response = ProcessMiningResponse(
        models=[ProcessModelItem(**item) for item in stored],
        source_event_count=len(rows),
        source_session_count=len({str(row.get("session_id")) for row in rows}),
        duration_ms=duration_ms,
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_processes_mine",
        query={
            "lookback_days": body.lookback_days,
            "min_support": min_support,
            "max_sequences": body.max_sequences,
            "max_steps": body.max_steps,
        },
        result_event_ids=[],
        policy_decisions={
            "model_count": len(response.models),
            "source_event_count": response.source_event_count,
            "source_session_count": response.source_session_count,
            "role": auth.role.value,
        },
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return response


@app.get("/v1/behavior/processes", response_model=ProcessMiningResponse)
def behavior_processes(
    status: str | None = None,
    limit: int = 100,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProcessMiningResponse:
    REQUEST_COUNT.labels(endpoint="behavior_processes", method="GET").inc()
    _enforce_workspace_access(auth, db)
    models = list_process_models(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        status=status,
        limit=limit,
    )
    return ProcessMiningResponse(models=[ProcessModelItem(**item) for item in models])


@app.get("/v1/behavior/shadow/status", response_model=BehaviorShadowStatusResponse)
def behavior_shadow_evaluation_status(
    limit: int = 200,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> BehaviorShadowStatusResponse:
    REQUEST_COUNT.labels(endpoint="behavior_shadow_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_shadow_evaluation_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior shadow evaluation is disabled")
    return BehaviorShadowStatusResponse(
        **shadow_status(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            limit=limit,
        )
    )


@app.get("/v1/behavior/reviews", response_model=MemoryReviewListResponse)
def behavior_memory_reviews(
    status: str | None = "pending",
    limit: int = 100,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MemoryReviewListResponse:
    REQUEST_COUNT.labels(endpoint="behavior_memory_reviews", method="GET").inc()
    _enforce_workspace_access(auth, db)
    rows = list_behavior_memory_reviews(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        status=status,
        limit=limit,
    )
    return MemoryReviewListResponse(reviews=[MemoryReviewItem(**row) for row in rows])


@app.post("/v1/behavior/reviews/{review_id}/resolve", response_model=MemoryReviewItem)
def behavior_memory_review_resolve(
    review_id: UUID,
    body: MemoryReviewResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> MemoryReviewItem:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_memory_review_resolve", method="POST").inc()
    _enforce_workspace_access(auth, db)
    note, _ = redact_control_text(body.note, limit=1000)
    row = resolve_memory_review(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        review_id=review_id,
        reviewer_id=auth.user_id,
        decision=body.decision,
        note=note,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="pending memory review not found")
    if body.decision == "promote" and row.get("target_type") == "evidence":
        _rebuild_behavior_fingerprint_full(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
        )
    response = MemoryReviewItem(**row)
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_memory_review_resolve",
        query={
            "review_id": str(review_id),
            "target_type": response.target_type,
            "target_id": str(response.target_id),
        },
        result_event_ids=[],
        policy_decisions={"decision": body.decision, "status": response.status, "role": auth.role.value},
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return response


@app.post("/v1/behavior/counterfactuals", response_model=CounterfactualItem)
def behavior_counterfactual_create(
    body: CounterfactualCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CounterfactualItem:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_counterfactual_create", method="POST").inc()
    _enforce_workspace_access(auth, db)
    if not bool(getattr(settings, "behavior_counterfactual_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior counterfactual logging is disabled")
    normalized = normalize_counterfactual(body.model_dump(mode="python"))
    payload = {**body.model_dump(mode="python"), **normalized}
    response = CounterfactualItem(
        **create_counterfactual(
            db,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            owner_id=auth.user_id,
            body=payload,
        )
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_counterfactual_create",
        query={
            "counterfactual_id": str(response.counterfactual_id),
            "observation_id": str(response.observation_id) if response.observation_id else None,
            "directive_id": str(response.directive_id) if response.directive_id else None,
        },
        result_event_ids=[],
        policy_decisions={
            "status": response.status,
            "redaction_applied": response.redaction_applied,
            "role": auth.role.value,
        },
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return response


@app.get("/v1/behavior/counterfactuals", response_model=CounterfactualListResponse)
def behavior_counterfactuals(
    status: str | None = None,
    limit: int = 100,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CounterfactualListResponse:
    REQUEST_COUNT.labels(endpoint="behavior_counterfactuals", method="GET").inc()
    _enforce_workspace_access(auth, db)
    rows = list_counterfactuals(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        status=status,
        limit=limit,
    )
    return CounterfactualListResponse(records=[CounterfactualItem(**row) for row in rows])


@app.post("/v1/behavior/counterfactuals/{counterfactual_id}/resolve", response_model=CounterfactualItem)
def behavior_counterfactual_resolve(
    counterfactual_id: UUID,
    body: CounterfactualResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CounterfactualItem:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_counterfactual_resolve", method="POST").inc()
    _enforce_workspace_access(auth, db)
    observed, observed_redacted = redact_control_text(body.observed_outcome, limit=1000)
    lesson, lesson_redacted = redact_control_text(body.lesson, limit=1000)
    row = resolve_counterfactual(
        db,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        counterfactual_id=counterfactual_id,
        body={
            **body.model_dump(mode="python"),
            "observed_outcome": observed,
            "lesson": lesson,
            "redaction_applied": observed_redacted or lesson_redacted,
        },
    )
    if row is None:
        raise HTTPException(status_code=404, detail="open counterfactual not found")
    response = CounterfactualItem(**row)
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="behavior_counterfactual_resolve",
        query={"counterfactual_id": str(counterfactual_id)},
        result_event_ids=[],
        policy_decisions={
            "assessment": response.assessment,
            "status": response.status,
            "redaction_applied": response.redaction_applied,
            "role": auth.role.value,
        },
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return response


@app.post("/v1/clone/ingest-observations", response_model=IngestObservationsResponse)
def ingest_observations(
    body: IngestObservationsRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> IngestObservationsResponse:
    REQUEST_COUNT.labels(endpoint="ingest_observations", method="POST").inc()
    _enforce_workspace_access(auth, db)

    ingested = 0
    observation_ids: list[UUID] = []
    for obs in body.observations:
        obs.setdefault("consumer_id", auth.consumer)
        obs.setdefault("workspace_id", auth.workspace_id)
        observation_ids.append(save_observation(db, obs))
        ingested += 1

    if observation_ids:
        queue_depth = None
        try:
            queue_depth = get_queue_depth(queue_name_for_job("tce_worker.jobs.embed_observations.run"))
        except Exception:
            queue_depth = None
        if queue_depth is not None and queue_depth < settings.obs_semantic_max_queue_depth:
            for observation_id in observation_ids:
                try:
                    enqueue_job("tce_worker.jobs.embed_observations.run", str(observation_id))
                except Exception:
                    logger.warning("failed to enqueue observation embedding job", exc_info=True)
        elif queue_depth is not None:
            logger.warning(
                "skipping observation embedding enqueue due to queue depth",
                extra={
                    "queue_depth": queue_depth,
                    "max_queue_depth": settings.obs_semantic_max_queue_depth,
                    "observation_count": len(observation_ids),
                },
            )

    fingerprint_updated = False
    if body.update_fingerprint and ingested > 0:
        fp = load_fingerprint(db, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
        if fp is None:
            fp = DEFAULT_FINGERPRINT.copy()
        obs_count = ingested
        for obs in body.observations:
            fp = merge_observation_into_fingerprint(fp, obs)
            obs_count += 1
        save_fingerprint(db, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id, fingerprint=fp, observation_count=obs_count)
        fingerprint_updated = True

    return IngestObservationsResponse(ingested=ingested, fingerprint_updated=fingerprint_updated)


class CheckContextRequest(BaseModel):
    file_path: str
    intended_action: str = "edit"


class CheckContextResponse(BaseModel):
    signal: str  # "allow", "warn", "block"
    reason: str
    past_decisions: list[dict[str, Any]]


@app.post("/v1/clone/check-context")
def check_context(
    body: CheckContextRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CheckContextResponse:
    """Check timeline for past decisions relevant to a file before modifying it."""
    REQUEST_COUNT.labels(endpoint="check_context", method="POST").inc()
    _enforce_workspace_access(auth, db)

    # Search all observations whose situation_summary or context_snapshot
    # mention the file path or any of its parent directory components.
    path_parts = [p for p in body.file_path.replace("\\", "/").split("/") if p]
    # Build search terms: full path + directory prefixes
    search_terms = [body.file_path]
    for i in range(len(path_parts)):
        search_terms.append("/".join(path_parts[: i + 1]))

    # Query observations that mention any of the path components
    placeholders = ", ".join(f":term_{i}" for i in range(len(search_terms)))
    params: dict[str, Any] = {"workspace_id": auth.workspace_id}
    for i, term in enumerate(search_terms):
        params[f"term_{i}"] = f"%{term}%"

    like_clauses = " OR ".join(
        f"situation_summary ILIKE :term_{i} OR CAST(context_snapshot AS TEXT) ILIKE :term_{i}"
        for i in range(len(search_terms))
    )

    rows = db.execute(
        text(
            f"""
            SELECT situation_summary, user_response, outcome, confidence
            FROM decision_observations
            WHERE workspace_id = :workspace_id
              AND ({like_clauses})
            ORDER BY confidence DESC, ts DESC
            LIMIT 5
            """
        ),
        params,
    ).fetchall()

    past_decisions = [
        {
            "situation": row[0][:150],
            "decision": row[1][:200],
            "outcome": (row[2] or "")[:100],
        }
        for row in rows
    ]

    # Determine signal
    if not past_decisions:
        signal = "allow"
        reason = "No past decisions found for this file. Proceed with caution."
    else:
        # Check if any decision explicitly blocks modification
        block_keywords = ["never modify", "never edit", "do not modify", "protected", "do not change"]
        is_blocked = any(
            any(kw in d["decision"].lower() for kw in block_keywords)
            for d in past_decisions
        )
        if is_blocked:
            signal = "block"
            reason = "Past decisions indicate this file should NOT be modified. See past_decisions for details."
        else:
            signal = "warn"
            reason = "Past decisions exist for this file. Review them before proceeding."

    return CheckContextResponse(
        signal=signal,
        reason=reason,
        past_decisions=past_decisions,
    )


# ---------------------------------------------------------------------------
# Dashboard endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/fingerprint", response_model=FingerprintResponse)
def get_fingerprint(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> FingerprintResponse:
    """Return the behavioral fingerprint for the current consumer/workspace."""
    REQUEST_COUNT.labels(endpoint="fingerprint", method="GET").inc()
    _enforce_workspace_access(auth, db)

    fp = db.execute(
        text("""
            SELECT fingerprint, observation_count, last_updated_at
            FROM behavioral_fingerprints
            WHERE consumer_id = :consumer AND workspace_id = :ws
            LIMIT 1
        """),
        {"consumer": auth.consumer, "ws": auth.workspace_id},
    ).fetchone()
    if fp is None:
        fp = db.execute(
            text("""
                SELECT fingerprint, observation_count, last_updated_at
                FROM behavioral_fingerprints
                WHERE workspace_id = :ws
                ORDER BY last_updated_at DESC LIMIT 1
            """),
            {"ws": auth.workspace_id},
        ).fetchone()

    if fp is None:
        return FingerprintResponse(
            fingerprint=DEFAULT_FINGERPRINT,
            observation_count=0,
            last_updated_at=None,
            is_default=True,
        )

    return FingerprintResponse(
        fingerprint=fp[0] if isinstance(fp[0], dict) else DEFAULT_FINGERPRINT,
        observation_count=fp[1],
        last_updated_at=fp[2],
        is_default=False,
    )


@app.get("/v1/observations", response_model=ObservationListResponse)
def list_observations(
    situation_type: str | None = None,
    outcome_sentiment: str | None = None,
    limit: int = 50,
    offset: int = 0,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ObservationListResponse:
    """List decision observations with optional filters."""
    REQUEST_COUNT.labels(endpoint="observations", method="GET").inc()
    _enforce_workspace_access(auth, db)

    # Build dynamic WHERE for raw SQL
    obs_where = ["workspace_id = :ws"]
    obs_params: dict[str, Any] = {"ws": auth.workspace_id}
    if situation_type:
        obs_where.append("situation_type = :sit_type")
        obs_params["sit_type"] = situation_type
    if outcome_sentiment:
        obs_where.append("outcome_sentiment = :out_sent")
        obs_params["out_sent"] = outcome_sentiment

    where_clause = " AND ".join(obs_where)

    # Total count (with filters applied)
    total = db.execute(
        text(f"SELECT COUNT(*) FROM decision_observations WHERE {where_clause}"),
        obs_params,
    ).scalar() or 0

    # Paginated rows
    obs_params["row_limit"] = min(limit, 200)
    obs_params["row_offset"] = offset
    rows = db.execute(
        text(f"""
            SELECT id, ts, situation_type, situation_summary, user_response,
                   response_reasoning, outcome, outcome_sentiment, confidence, source_event_ids
            FROM decision_observations
            WHERE {where_clause}
            ORDER BY ts DESC
            OFFSET :row_offset LIMIT :row_limit
        """),
        obs_params,
    ).mappings().all()

    items = [
        ObservationItem(
            id=r["id"],
            ts=r["ts"],
            situation_type=r["situation_type"],
            situation_summary=r["situation_summary"],
            user_response=r["user_response"],
            response_reasoning=r["response_reasoning"],
            outcome=r["outcome"],
            outcome_sentiment=r["outcome_sentiment"],
            confidence=r["confidence"],
            source_event_ids=r["source_event_ids"] or [],
        )
        for r in rows
    ]

    # Situation type distribution (unfiltered for workspace) — raw SQL
    type_counts_rows = db.execute(
        text("""
            SELECT situation_type, COUNT(*) AS cnt
            FROM decision_observations
            WHERE workspace_id = :ws
            GROUP BY situation_type
        """),
        {"ws": auth.workspace_id},
    ).fetchall()
    situation_type_counts = {row[0]: row[1] for row in type_counts_rows}

    return ObservationListResponse(
        observations=items,
        total=total,
        situation_type_counts=situation_type_counts,
    )


@app.get("/v1/dashboard/clone-score", response_model=CloneScoreResponse)
def get_clone_score(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CloneScoreResponse:
    """Calculate clone readiness score (0-100)."""
    REQUEST_COUNT.labels(endpoint="clone_score", method="GET").inc()
    _enforce_workspace_access(auth, db)

    TOTAL_SITUATION_TYPES = 12

    # Single raw SQL: obs count + distinct types + recent sentiments
    cs_stats = db.execute(
        text("""
            SELECT
                (SELECT COUNT(*) FROM decision_observations WHERE workspace_id = :ws) AS obs_count,
                (SELECT COUNT(DISTINCT situation_type) FROM decision_observations WHERE workspace_id = :ws) AS distinct_types
        """),
        {"ws": auth.workspace_id},
    ).mappings().first()
    obs_count = int(cs_stats["obs_count"]) if cs_stats else 0
    distinct_types = int(cs_stats["distinct_types"]) if cs_stats else 0
    observation_count_score = min(25, int(obs_count * 25 / 100))

    # 2. Fingerprint confidence — raw SQL
    fp_row = db.execute(
        text("""
            SELECT fingerprint FROM behavioral_fingerprints
            WHERE workspace_id = :ws
            ORDER BY last_updated_at DESC LIMIT 1
        """),
        {"ws": auth.workspace_id},
    ).fetchone()
    if fp_row and fp_row[0]:
        fp_data = fp_row[0] if isinstance(fp_row[0], dict) else {}
        non_default = 0
        total_dims = 0
        for category, defaults in DEFAULT_FINGERPRINT.items():
            if not isinstance(defaults, dict):
                continue
            for key, default_val in defaults.items():
                total_dims += 1
                stored_cat = fp_data.get(category, {})
                if isinstance(stored_cat, dict) and stored_cat.get(key) != default_val:
                    non_default += 1
        fingerprint_confidence = int(non_default / max(total_dims, 1) * 25) if total_dims else 0
    else:
        fingerprint_confidence = 0

    # 3. Pattern coverage
    pattern_coverage = min(25, int(distinct_types / TOTAL_SITUATION_TYPES * 25))

    # 4. Recent consistency — raw SQL
    recent_rows = db.execute(
        text("""
            SELECT outcome_sentiment FROM decision_observations
            WHERE workspace_id = :ws
            ORDER BY ts DESC LIMIT 20
        """),
        {"ws": auth.workspace_id},
    ).fetchall()
    if recent_rows:
        positive_neutral = sum(
            1 for r in recent_rows if r[0] in ("positive", "neutral", None)
        )
        recent_consistency = int(positive_neutral / len(recent_rows) * 25)
    else:
        recent_consistency = 0

    total_score = observation_count_score + fingerprint_confidence + pattern_coverage + recent_consistency

    return CloneScoreResponse(
        score=total_score,
        breakdown=CloneScoreBreakdown(
            observation_count_score=observation_count_score,
            fingerprint_confidence=fingerprint_confidence,
            pattern_coverage=pattern_coverage,
            recent_consistency=recent_consistency,
        ),
        observation_count=obs_count,
        situation_types_covered=distinct_types,
        total_situation_types=TOTAL_SITUATION_TYPES,
    )


@app.get("/v1/dashboard/system-status", response_model=SystemStatusResponse)
def get_system_status(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> SystemStatusResponse:
    """Aggregate system status: API, DB, Redis, Ollama, runtime mode."""
    REQUEST_COUNT.labels(endpoint="system_status", method="GET").inc()
    _enforce_workspace_access(auth, db)

    # API uptime
    uptime = int(time.monotonic() - _APP_START_TIME)
    api_info = ApiStatusInfo(status="ok", uptime_seconds=uptime)

    def _check_db() -> DatabaseStatusInfo:
        try:
            counts = db.execute(text("""
                SELECT
                    (SELECT COUNT(*) FROM events) AS event_count,
                    (SELECT COUNT(*) FROM decision_observations) AS obs_count,
                    (SELECT COUNT(*) FROM patterns) AS pattern_count,
                    (SELECT COUNT(*) FROM event_embeddings) AS embed_count
            """)).mappings().first()
            return DatabaseStatusInfo(
                connected=True,
                event_count=int(counts["event_count"]),
                observation_count=int(counts["obs_count"]),
                pattern_count=int(counts["pattern_count"]),
                embedding_count=int(counts["embed_count"]),
            )
        except Exception:
            return DatabaseStatusInfo(connected=False)

    def _check_redis() -> ServiceStatusInfo:
        try:
            redis_client = get_redis_client(settings.redis_url)
            if redis_client is None:
                return ServiceStatusInfo(connected=False)
            redis_client.ping()
            return ServiceStatusInfo(connected=True)
        except Exception:
            return ServiceStatusInfo(connected=False)

    def _check_ollama() -> ServiceStatusInfo:
        try:
            resp = _SYSTEM_STATUS_HTTP_CLIENT.get(f"{settings.ollama_url}/api/tags")
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                return ServiceStatusInfo(connected=True, models=models)
            return ServiceStatusInfo(connected=False)
        except Exception:
            return ServiceStatusInfo(connected=False)

    # DB runs on main thread (owns the session), Redis + Ollama in parallel
    redis_future = _SYSTEM_STATUS_EXECUTOR.submit(_check_redis)
    ollama_future = _SYSTEM_STATUS_EXECUTOR.submit(_check_ollama)
    db_info = _check_db()
    try:
        redis_info = redis_future.result(timeout=3)
    except Exception:
        redis_info = ServiceStatusInfo(connected=False)
    try:
        ollama_info = ollama_future.result(timeout=3)
    except Exception:
        ollama_info = ServiceStatusInfo(connected=False)

    # Runtime mode
    try:
        mode_cfg = get_runtime_mode(db)
        mode_info = RuntimeModeInfo(
            mode=mode_cfg.mode.value,
            clone_enabled=mode_cfg.clone_enabled,
        )
    except Exception:
        mode_info = RuntimeModeInfo(mode="unknown", clone_enabled=False)

    return SystemStatusResponse(
        api=api_info,
        database=db_info,
        redis=redis_info,
        ollama=ollama_info,
        runtime_mode=mode_info,
    )


class DashboardExecutorConfigUpdateRequest(BaseModel):
    workspace_id: str | None = None
    executor_user_id: str | None = None
    executor_consumer_id: str | None = None
    secondary_executor_user_id: str | None = None
    secondary_executor_consumer_id: str | None = None
    executor_clients: list[str] | None = None


@app.get("/v1/dashboard/client-config", response_model=DashboardClientConfigResponse)
def dashboard_client_config(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardClientConfigResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_client_config", method="GET").inc()
    _enforce_workspace_access(auth, db)
    mode_info = _runtime_mode_info(db)
    default_workspace_id = os.getenv("TCE_MCP_WORKSPACE_ID", auth.workspace_id or "personal").strip() or "personal"
    default_user_id = os.getenv("TCE_MCP_EXECUTOR_USER_ID", auth.user_id or "local-user").strip() or "local-user"
    default_consumer_id = os.getenv("TCE_MCP_EXECUTOR_CONSUMER_ID", _clean_consumer(auth.consumer) or "dashboard").strip() or "dashboard"
    requested_default_session_id = _normalize_session_id_value(os.getenv("TCE_MCP_SESSION_ID", ""))
    default_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=requested_default_session_id,
    )
    executor_clients = [item.strip().lower() for item in os.getenv("TCE_EXECUTOR_CLIENTS", "codex,claude").split(",") if item.strip()]
    executor_id = os.getenv("TCE_MCP_EXECUTOR_USER_ID", "codex-executor").strip() or "codex-executor"
    secondary_executor_id = os.getenv("TCE_MCP_SECONDARY_USER_ID", "claude-executor").strip() or "claude-executor"
    return DashboardClientConfigResponse(
        api_base="/v1",
        default_workspace_id=default_workspace_id,
        default_user_id=default_user_id,
        default_consumer_id=default_consumer_id,
        default_session_id=default_session_id,
        executor_clients=executor_clients,
        runtime_mode=mode_info,
        known_identities={
            "executor": DashboardIdentityInfo(
                user_id=executor_id,
                label=_agent_label(executor_id, "Executor"),
            ),
            "secondary_executor": DashboardIdentityInfo(
                user_id=secondary_executor_id,
                label=_agent_label(secondary_executor_id, "Additional Executor"),
            ),
            "advisor": DashboardIdentityInfo(
                user_id=secondary_executor_id,
                label=_agent_label(secondary_executor_id, "Additional Executor"),
            ),
        },
        generated_at=datetime.now(tz=UTC),
    )


@app.post("/v1/dashboard/stack/restart", response_model=DashboardStackRestartResponse)
def dashboard_stack_restart(
    body: DashboardStackRestartRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardStackRestartResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_stack_restart", method="POST").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)
    requested = (body.stack or "full").strip().lower() or "full"
    stack = requested if requested in {"full", "lite", "all", "auto"} else "full"
    job_id = _start_stack_restart_job(stack=stack)
    return DashboardStackRestartResponse(
        accepted=True,
        restart_id=job_id,
        message=f"restart accepted for stack='{stack}'",
    )


@app.get("/v1/dashboard/stack/restart/{restart_id}", response_model=DashboardStackRestartStatusResponse)
def dashboard_stack_restart_status(
    restart_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardStackRestartStatusResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_stack_restart_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    job: dict[str, Any] | None = None
    with _STACK_RESTART_LOCK:
        existing = _STACK_RESTART_JOBS.get(restart_id)
        if isinstance(existing, dict):
            job = dict(existing)
    if settings.dashboard_stack_restart_containerized_enabled:
        try:
            refreshed = _refresh_stack_restart_job_from_docker(restart_id)
        except Exception as exc:
            refreshed = {
                "restart_id": restart_id,
                "mode": "containerized",
                "state": "failed",
                "started_at": job.get("started_at") if job else datetime.now(tz=UTC),
                "completed_at": datetime.now(tz=UTC),
                "error": str(exc),
            }
        if refreshed is not None:
            with _STACK_RESTART_LOCK:
                current = _STACK_RESTART_JOBS.get(restart_id, {})
                merged = {**current, **refreshed}
                _STACK_RESTART_JOBS[restart_id] = merged
                job = dict(merged)
    if not job:
        raise HTTPException(status_code=404, detail=f"unknown restart_id: {restart_id}")
    return DashboardStackRestartStatusResponse(
        restart_id=restart_id,
        state=str(job.get("state") or "unknown"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        error=(str(job.get("error")) if job.get("error") else None),
    )


@app.put("/v1/dashboard/client-config/executor", response_model=dict)
def dashboard_update_executor_config(
    body: DashboardExecutorConfigUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="dashboard_update_executor_config", method="PUT").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)

    updates: dict[str, str] = {}

    workspace_id = (body.workspace_id or "").strip()
    if workspace_id:
        updates["TCE_MCP_WORKSPACE_ID"] = workspace_id

    executor_user_id = (body.executor_user_id or "").strip()
    if executor_user_id:
        updates["TCE_MCP_EXECUTOR_USER_ID"] = executor_user_id

    executor_consumer_id = (body.executor_consumer_id or "").strip()
    if executor_consumer_id:
        updates["TCE_MCP_EXECUTOR_CONSUMER_ID"] = executor_consumer_id

    secondary_executor_user_id = body.secondary_executor_user_id
    if secondary_executor_user_id:
        value = secondary_executor_user_id.strip()
        if value:
            updates["TCE_MCP_SECONDARY_USER_ID"] = value

    secondary_executor_consumer_id = body.secondary_executor_consumer_id
    if secondary_executor_consumer_id:
        value = secondary_executor_consumer_id.strip()
        if value:
            updates["TCE_MCP_SECONDARY_CONSUMER_ID"] = value

    if body.executor_clients is not None:
        normalized_clients: list[str] = []
        seen: set[str] = set()
        for item in body.executor_clients:
            value = str(item).strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized_clients.append(value)
        if normalized_clients:
            updates["TCE_EXECUTOR_CLIENTS"] = ",".join(normalized_clients)

    if not updates:
        raise HTTPException(status_code=400, detail="no executor config values provided")

    try:
        _upsert_env_values(updates)
    except Exception:
        logger.exception("failed to persist executor config to .env")
        raise HTTPException(status_code=500, detail="failed to persist executor config to .env")

    return {
        "ok": True,
        "message": "saved_to_env_restart_required",
        "restart_required": True,
        "updated_keys": sorted(updates.keys()),
    }


@app.get("/v1/dashboard/agent-roles", response_model=DashboardAgentRolesResponse)
def dashboard_agent_roles(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardAgentRolesResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_agent_roles", method="GET").inc()
    _enforce_workspace_access(auth, db)

    mode_info = _runtime_mode_info(db)
    workspace_id = auth.workspace_id
    now = datetime.now(tz=UTC)
    try:
        memberships = list_team_memberships(db=db, workspace_id=workspace_id)
    except Exception:
        memberships = []

    executor_user_id = os.getenv("TCE_MCP_EXECUTOR_USER_ID", "codex-executor").strip() or "codex-executor"
    secondary_executor_user_id = os.getenv("TCE_MCP_SECONDARY_USER_ID", "claude-executor").strip() or "claude-executor"
    executor_consumer_id = os.getenv("TCE_MCP_EXECUTOR_CONSUMER_ID", executor_user_id).strip() or executor_user_id
    secondary_executor_consumer_id = (
        os.getenv("TCE_MCP_SECONDARY_CONSUMER_ID", secondary_executor_user_id).strip() or secondary_executor_user_id
    )

    executor = _resolve_dashboard_role(
        db,
        workspace_id=workspace_id,
        role="executor",
        user_id=executor_user_id,
        consumer_id=executor_consumer_id,
        memberships=memberships,
        now=now,
    )
    secondary_executor = _resolve_dashboard_role(
        db,
        workspace_id=workspace_id,
        role="executor",
        user_id=secondary_executor_user_id,
        consumer_id=secondary_executor_consumer_id,
        memberships=memberships,
        now=now,
    )
    secondary_executor.label = _agent_label(
        secondary_executor_user_id or secondary_executor_consumer_id,
        "Additional Executor",
    )

    if not mode_info.clone_enabled and secondary_executor.status == "active":
        secondary_executor.status = "registered"
        secondary_executor.source = "runtime_mode_timeline_only"

    return DashboardAgentRolesResponse(
        workspace_id=workspace_id,
        runtime_mode=mode_info,
        executor=executor,
        advisor=secondary_executor,
        secondary_executor=secondary_executor,
        generated_at=now,
    )


@app.get("/v1/dashboard/resource-usage", response_model=dict)
def get_resource_usage(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    REQUEST_COUNT.labels(endpoint="resource_usage", method="GET").inc()
    _enforce_workspace_access(auth, db)

    db_size_bytes: int | None = None
    db_error: str | None = None
    try:
        db_size_raw = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
        db_size_bytes = _safe_int(db_size_raw)
    except Exception as exc:
        db_error = str(exc)

    services, docker_meta = _docker_service_stats()
    return {
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "database": {
            "engine": "postgresql",
            "size_bytes": db_size_bytes,
            "size_pretty": _bytes_to_human(db_size_bytes),
            "available": db_size_bytes is not None,
            "error": db_error,
        },
        "docker": docker_meta,
        "services": services,
    }


@app.get("/v1/dashboard/goals/intelligence", response_model=DashboardGoalsIntelligenceResponse)
def dashboard_goals_intelligence(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardGoalsIntelligenceResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_goals_intelligence", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    with DASHBOARD_GOAL_INTELLIGENCE_MS.time():
        return _build_goal_intelligence(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=effective_session_id,
        )


@app.get("/v1/dashboard/human-score", response_model=DashboardHumanScoreResponse)
def dashboard_human_score(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardHumanScoreResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_human_score", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )

    cached = _latest_human_score_snapshot(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )
    if cached and (datetime.now(tz=UTC) - cached.computed_at) <= timedelta(hours=1):
        return cached

    with DASHBOARD_HUMAN_SCORE_MS.time():
        computed = _compute_human_score(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=effective_session_id,
        )
        computed.snapshot_id = _persist_human_score_snapshot(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            response=computed,
        )
        return computed


@app.get("/v1/dashboard/human-score/history", response_model=DashboardHumanScoreHistoryResponse)
def dashboard_human_score_history(
    session_id: str = "default",
    days: int = 30,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardHumanScoreHistoryResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_human_score_history", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    normalized_days = max(1, min(365, int(days)))
    cutoff = datetime.now(tz=UTC) - timedelta(days=normalized_days)
    rows = db.execute(
        text(
            """
            SELECT created_at, score, band
            FROM dashboard_human_score_snapshots
            WHERE workspace_id = :ws
              AND user_id = :uid
              AND session_id = :sid
              AND created_at >= :cutoff
            ORDER BY created_at ASC
            LIMIT 500
            """
        ),
        {
            "ws": auth.workspace_id,
            "uid": auth.user_id,
            "sid": effective_session_id,
            "cutoff": cutoff,
        },
    ).mappings().all()
    points = [
        DashboardHumanScoreHistoryPoint(
            ts=row["created_at"] if isinstance(row.get("created_at"), datetime) else datetime.now(tz=UTC),
            score=max(0, min(100, _safe_int(row.get("score")))),
            band=str(row.get("band") or _score_band(_safe_int(row.get("score")))),
        )
        for row in rows
    ]
    return DashboardHumanScoreHistoryResponse(
        session_id=effective_session_id,
        days=normalized_days,
        points=points,
    )


@app.post("/v1/dashboard/human-score/recompute", response_model=DashboardHumanScoreResponse)
def dashboard_human_score_recompute(
    body: DashboardHumanScoreRecomputeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DashboardHumanScoreResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_human_score_recompute", method="POST").inc()
    _enforce_workspace_access(auth, db)
    session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=body.session_id,
    )
    with DASHBOARD_HUMAN_SCORE_MS.time():
        computed = _compute_human_score(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=session_id,
        )
        computed.snapshot_id = _persist_human_score_snapshot(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            response=computed,
        )
    DASHBOARD_HUMAN_SCORE_RECOMPUTE_COUNT.inc()
    return computed


@app.get("/v1/takeover/state", response_model=TakeoverState)
def get_takeover_state(
    session_id: str,
    persona_mode: str = "normal",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverState:
    REQUEST_COUNT.labels(endpoint="takeover_state", method="GET").inc()
    _enforce_workspace_access(auth, db)
    return load_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


@app.post("/v1/takeover/reset", response_model=TakeoverState)
def reset_takeover_state(
    session_id: str,
    persona_mode: str = "normal",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverState:
    REQUEST_COUNT.labels(endpoint="takeover_reset", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = store_reset_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )
    _invalidate_goal_queue_cache(db, auth=auth, state=state)
    db.execute(
        text(
            """
            DELETE FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            """
        ),
        {"session_id": session_id, "workspace_id": auth.workspace_id, "user_id": auth.user_id},
    )
    db.execute(
        text(
            """
            UPDATE autonomy_notices
            SET acknowledged_at = :ack
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND acknowledged_at IS NULL
            """
        ),
        {
            "ack": datetime.now(tz=UTC),
            "session_id": session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
        },
    )
    db.commit()
    return state


@app.post("/v1/takeover/goals/discover", response_model=TakeoverGoalsResponse)
def takeover_discover_goals(
    body: TakeoverGoalsDiscoverRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverGoalsResponse:
    REQUEST_COUNT.labels(endpoint="takeover_goals_discover", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    goals = _discover_takeover_goals(
        db,
        auth=auth,
        state=state,
        include_open_discovery=body.include_open_discovery,
    )
    return TakeoverGoalsResponse(session_id=state.session_id, goals=goals)


@app.post("/v1/takeover/goals/precompute", response_model=TakeoverGoalsResponse)
def takeover_precompute_goals(
    body: TakeoverGoalsPrecomputeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverGoalsResponse:
    REQUEST_COUNT.labels(endpoint="takeover_goals_precompute", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    goals = _discover_takeover_goals(
        db,
        auth=auth,
        state=state,
        include_open_discovery=body.include_open_discovery,
        force_recompute=body.force_recompute,
    )
    return TakeoverGoalsResponse(session_id=state.session_id, goals=goals)


@app.get("/v1/takeover/goals", response_model=TakeoverGoalsResponse)
def takeover_list_goals(
    session_id: str,
    status: AutonomyGoalStatus | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverGoalsResponse:
    REQUEST_COUNT.labels(endpoint="takeover_goals_list", method="GET").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    goals = _list_takeover_goals(db, state=state, status=status)
    return TakeoverGoalsResponse(session_id=state.session_id, goals=goals)


@app.get("/v1/takeover/goals/cache/status", response_model=TakeoverGoalCacheStatusResponse)
def takeover_goal_cache_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverGoalCacheStatusResponse:
    REQUEST_COUNT.labels(endpoint="takeover_goals_cache_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    now = datetime.now(tz=UTC)
    state = load_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    cache_key = _goal_cache_key_for_state(state, auth)
    l1_item, l1_state = goal_cache_get_l1(cache_key)
    l1: dict[str, Any]
    if l1_item:
        created_at = _coerce_datetime_value(l1_item.get("created_at"))
        expires_at = _coerce_datetime_value(l1_item.get("expires_at"))
        l1 = {
            "state": "hit",
            "source": l1_item.get("source", "l1"),
            "fresh": expires_at >= now,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "age_seconds": max(0, int((now - created_at).total_seconds())),
            "ttl_seconds": max(0, int((expires_at - now).total_seconds())),
        }
    else:
        l1 = {
            "state": l1_state or "miss",
            "source": None,
            "fresh": False,
        }

    row = db.execute(
        text(
            """
            SELECT payload, created_at, expires_at, last_accessed_at, cache_version
            FROM autonomy_goal_cache
            WHERE cache_key = :cache_key
            LIMIT 1
            """
        ),
        {"cache_key": cache_key},
    ).mappings().first()
    l2: dict[str, Any]
    if row:
        payload = goal_cache_deserialize(row.get("payload"))
        expires_at = _coerce_datetime_value(row.get("expires_at"))
        created_at = _coerce_datetime_value(row.get("created_at"))
        last_accessed_at = _coerce_datetime_value(row.get("last_accessed_at"))
        l2 = {
            "present": True,
            "fresh": expires_at >= now,
            "goal_count": len((payload or {}).get("goals", [])),
            "cache_version": int(row.get("cache_version", 0) or 0),
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_accessed_at": last_accessed_at.isoformat(),
            "age_seconds": max(0, int((now - created_at).total_seconds())),
            "ttl_seconds": max(0, int((expires_at - now).total_seconds())),
        }
    else:
        l2 = {
            "present": False,
            "fresh": False,
        }

    return TakeoverGoalCacheStatusResponse(
        session_id=state.session_id,
        objective_hash=state.objective_hash,
        cache_key=cache_key,
        l1=l1,
        l2=l2,
        generated_at=now,
    )


@app.post("/v1/takeover/goals/cache/invalidate", response_model=dict)
def takeover_goal_cache_invalidate(
    body: TakeoverGoalCacheInvalidateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="takeover_goals_cache_invalidate", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    removed = _invalidate_goal_queue_cache(db, auth=auth, state=state)
    db.commit()
    return {
        "session_id": state.session_id,
        "objective_hash": body.objective_hash or state.objective_hash,
        "removed": int(removed),
        "invalidated_at": datetime.now(tz=UTC).isoformat(),
    }


@app.post("/v1/takeover/goals/{goal_id}/select", response_model=TakeoverGoal)
def takeover_select_goal(
    goal_id: UUID,
    body: TakeoverGoalSelectRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverGoal:
    REQUEST_COUNT.labels(endpoint="takeover_goal_select", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    db.execute(
        text(
            """
            UPDATE autonomy_goals
            SET status = :candidate, updated_at = :updated_at
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND status IN (:selected, :executing)
            """
        ),
        {
            "candidate": AutonomyGoalStatus.CANDIDATE.value,
            "updated_at": datetime.now(tz=UTC),
            "session_id": state.session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
            "selected": AutonomyGoalStatus.SELECTED.value,
            "executing": AutonomyGoalStatus.EXECUTING.value,
        },
    )
    db.execute(
        text(
            """
            UPDATE autonomy_goals
            SET status = :selected, updated_at = :updated_at
            WHERE id = :id
              AND session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            """
        ),
        {
            "selected": AutonomyGoalStatus.SELECTED.value,
            "updated_at": datetime.now(tz=UTC),
            "id": goal_id,
            "session_id": state.session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
        },
    )
    state.active_goal_id = goal_id
    save_takeover_state(db, state)
    _invalidate_goal_queue_cache(db, auth=auth, state=state)
    db.commit()
    selected = _load_active_goal(db, state)
    if not selected:
        raise HTTPException(status_code=404, detail="goal not found for this session")
    return selected


@app.post("/v1/takeover/permit", response_model=ExecutionPermitResponse)
def request_execution_permit(
    body: ExecutionPermitRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ExecutionPermitResponse:
    REQUEST_COUNT.labels(endpoint="takeover_permit_request", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    risk_tier = classify_risk_tier(
        action_kind=body.action_kind,
        target_paths=body.target_paths,
        command_preview=body.command_preview,
        estimated_change_size=body.estimated_change_size,
    )
    sensitive_hit = any(
        any(token in path.lower() for token in ("services/tce_mcp", "infra/", "secrets", ".env"))
        for path in body.target_paths
    )
    decision, reason = evaluate_execution_permit(
        policy_profile=state.autonomy_policy_profile,
        risk_tier=risk_tier,
        estimated_change_size=body.estimated_change_size,
        role=auth.role.value,
        sensitive_path_hit=sensitive_hit,
    )
    permit_id = uuid.uuid4()
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=max(30, int(settings.takeover_permit_ttl_seconds)))
    db.execute(
        text(
            """
            INSERT INTO execution_permits(
              id, session_id, workspace_id, action_kind, target_paths, command_preview,
              estimated_change_size, decision, reason, confirmed_by, expires_at, created_at, resolved_at
            )
            VALUES(
              :id, :session_id, :workspace_id, :action_kind, CAST(:target_paths AS JSONB), :command_preview,
              :estimated_change_size, :decision, :reason, NULL, :expires_at, :created_at, NULL
            )
            """
        ),
        {
            "id": permit_id,
            "session_id": body.session_id,
            "workspace_id": auth.workspace_id,
            "action_kind": body.action_kind,
            "target_paths": json.dumps(body.target_paths),
            "command_preview": body.command_preview,
            "estimated_change_size": body.estimated_change_size,
            "decision": decision.value,
            "reason": reason,
            "expires_at": expires_at,
            "created_at": datetime.now(tz=UTC),
        },
    )
    db.commit()
    return ExecutionPermitResponse(
        decision=decision,
        reason=reason,
        permit_id=permit_id,
        expires_at=expires_at,
        required_confirmation=settings.takeover_confirm_keyword
        if decision == ExecutionPermitDecision.CONFIRM_REQUIRED
        else None,
    )


@app.post("/v1/takeover/permit/resolve", response_model=ExecutionPermitResponse)
def resolve_execution_permit(
    body: ExecutionPermitResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ExecutionPermitResponse:
    REQUEST_COUNT.labels(endpoint="takeover_permit_resolve", method="POST").inc()
    _enforce_workspace_access(auth, db)
    row = db.execute(
        text(
            """
            SELECT id, session_id, decision, reason, expires_at
            FROM execution_permits
            WHERE id = :id
              AND workspace_id = :workspace_id
            LIMIT 1
            """
        ),
        {"id": body.permit_id, "workspace_id": auth.workspace_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="permit not found")
    session_id = str(row[1])
    expires_at = row[4]
    if expires_at and expires_at < datetime.now(tz=UTC):
        decision = ExecutionPermitDecision.BLOCKED
        reason = "permit expired"
    else:
        decision = ExecutionPermitDecision.ALLOW if body.approved else ExecutionPermitDecision.BLOCKED
        reason = "confirmed by operator" if body.approved else "denied by operator"
    db.execute(
        text(
            """
            UPDATE execution_permits
            SET decision = :decision, reason = :reason, confirmed_by = :confirmed_by, resolved_at = :resolved_at
            WHERE id = :id
            """
        ),
        {
            "decision": decision.value,
            "reason": reason,
            "confirmed_by": body.confirmed_by or auth.user_id,
            "resolved_at": datetime.now(tz=UTC),
            "id": body.permit_id,
        },
    )
    try:
        state = load_takeover_state(
            db=db,
            session_id=session_id,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            persona_mode="normal",
            activation_keywords=None,
            stop_keywords=None,
        )
        _invalidate_goal_queue_cache(db, auth=auth, state=state)
    except Exception:
        logger.warning("failed to invalidate takeover goal cache after permit resolve", exc_info=True)
    db.commit()
    return ExecutionPermitResponse(
        decision=decision,
        reason=reason,
        permit_id=body.permit_id,
        expires_at=expires_at,
        required_confirmation=settings.takeover_confirm_keyword
        if decision == ExecutionPermitDecision.CONFIRM_REQUIRED
        else None,
    )


@app.get("/v1/takeover/autonomy/status", response_model=TakeoverAutonomyStatusResponse)
def takeover_autonomy_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverAutonomyStatusResponse:
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    active_goal = _load_active_goal(db, state)
    pending = db.execute(
        text(
            """
            SELECT COUNT(1)
            FROM execution_permits
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND decision = :decision
              AND (expires_at IS NULL OR expires_at >= :now)
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": auth.workspace_id,
            "decision": ExecutionPermitDecision.CONFIRM_REQUIRED.value,
            "now": datetime.now(tz=UTC),
        },
    ).scalar()
    return TakeoverAutonomyStatusResponse(
        session_id=session_id,
        state=state,
        active_goal=active_goal,
        queue_size=int(state.goal_queue_size or 0),
        pending_permit_count=int(pending or 0),
        pending_directive_count=int(state.pending_directive_count or 0),
        open_notice_count=_open_notice_count_for_session(db, state=state),
        retry_backlog_count=int(state.retry_backlog_count or 0),
        enforcement_mode=str(state.enforcement_mode or settings.takeover_enforcement_mode),
        continuity_ok=int(state.continuity_violation_count or 0) < 3,
        generated_at=datetime.now(tz=UTC),
    )


@app.get("/v1/takeover/autonomy/readiness", response_model=dict)
def takeover_autonomy_readiness(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_readiness", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    return _autonomy_readiness_payload(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )


@app.get("/v1/takeover/autonomy/project-kpis", response_model=dict)
def takeover_autonomy_project_kpis(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_project_kpis", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    return _autonomy_project_kpis_payload(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )


@app.post("/v1/takeover/autonomy/tick", response_model=TakeoverAutonomyTickResponse)
def takeover_autonomy_tick(
    body: TakeoverAutonomyTickRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverAutonomyTickResponse:
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_tick", method="POST").inc()
    _enforce_workspace_access(auth, db)
    now = datetime.now(tz=UTC)
    if body.session_id:
        sessions = [body.session_id]
    else:
        rows = db.execute(
            text(
                """
                SELECT session_id
                FROM takeover_sessions
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                ORDER BY updated_at DESC
                LIMIT :max_sessions
                """
            ),
            {"workspace_id": auth.workspace_id, "user_id": auth.user_id, "max_sessions": int(body.max_sessions)},
        ).mappings().all()
        sessions = [str(row["session_id"]) for row in rows]
    goals_refreshed = 0
    notices_created = 0
    dedupe_cutoff = now - timedelta(minutes=max(1, int(settings.takeover_notice_dedupe_minutes)))
    threshold = float(settings.takeover_notice_threshold)
    for session_id in sessions:
        state = load_takeover_state(
            db=db,
            session_id=session_id,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            persona_mode="normal",
            activation_keywords=None,
            stop_keywords=None,
        )
        goals = _discover_takeover_goals(
            db,
            auth=auth,
            state=state,
            include_open_discovery=body.include_open_discovery,
        )
        goals_refreshed += 1
        top_goal = goals[0] if goals else None
        if (
            top_goal is not None
            and not state.active_goal_id
            and float(top_goal.selection_score or top_goal.priority_score or 0.0) >= threshold
        ):
            existing = db.execute(
                text(
                    """
                    SELECT id
                    FROM autonomy_notices
                    WHERE session_id = :session_id
                      AND workspace_id = :workspace_id
                      AND user_id = :user_id
                      AND goal_id = :goal_id
                      AND acknowledged_at IS NULL
                      AND created_at >= :cutoff
                    LIMIT 1
                    """
                ),
                {
                    "session_id": session_id,
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                    "goal_id": top_goal.id,
                    "cutoff": dedupe_cutoff,
                },
            ).first()
            if existing is None:
                db.execute(
                    text(
                        """
                        INSERT INTO autonomy_notices(
                            id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
                            expires_at, created_at, acknowledged_at
                        )
                        VALUES(
                            :id, :session_id, :workspace_id, :user_id, :goal_id, :title, :reason, :priority,
                            :expires_at, :created_at, NULL
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "session_id": session_id,
                        "workspace_id": auth.workspace_id,
                        "user_id": auth.user_id,
                        "goal_id": top_goal.id,
                        "title": f"Next suggested goal: {top_goal.title[:140]}",
                        "reason": "Proactive discovery found a high-priority actionable goal.",
                        "priority": float(top_goal.selection_score or top_goal.priority_score or 0.0),
                        "expires_at": now + timedelta(minutes=max(10, int(settings.takeover_notice_dedupe_minutes))),
                        "created_at": now,
                    },
                )
                notices_created += 1
        state.last_tick_at = now
        state.enforcement_mode = settings.takeover_enforcement_mode
        _sync_enforcement_counters(db, state)
        save_takeover_state(db, state)
    db.commit()
    return TakeoverAutonomyTickResponse(
        sessions_scanned=len(sessions),
        goals_refreshed=goals_refreshed,
        notices_created=notices_created,
        generated_at=now,
    )


@app.get("/v1/takeover/notices", response_model=TakeoverNoticesResponse)
def takeover_notices(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverNoticesResponse:
    REQUEST_COUNT.labels(endpoint="takeover_notices", method="GET").inc()
    _enforce_workspace_access(auth, db)
    rows = db.execute(
        text(
            """
            SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
                   expires_at, created_at, acknowledged_at
            FROM autonomy_notices
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND acknowledged_at IS NULL
              AND (expires_at IS NULL OR expires_at >= :now)
            ORDER BY priority DESC, created_at DESC
            LIMIT 50
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
            "now": datetime.now(tz=UTC),
        },
    ).mappings().all()
    return TakeoverNoticesResponse(
        session_id=session_id,
        notices=[_notice_from_row(row) for row in rows],
        generated_at=datetime.now(tz=UTC),
    )


@app.post("/v1/takeover/notices/{notice_id}/ack", response_model=AutonomyNotice)
def takeover_notice_ack(
    notice_id: UUID,
    body: TakeoverNoticeAckRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> AutonomyNotice:
    REQUEST_COUNT.labels(endpoint="takeover_notice_ack", method="POST").inc()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    row = db.execute(
        text(
            """
            SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
                   expires_at, created_at, acknowledged_at
            FROM autonomy_notices
            WHERE id = :id
              AND session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            LIMIT 1
            """
        ),
        {"id": notice_id, "session_id": body.session_id, "workspace_id": auth.workspace_id, "user_id": auth.user_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="notice not found")
    db.execute(
        text("UPDATE autonomy_notices SET acknowledged_at = :ack WHERE id = :id"),
        {"ack": datetime.now(tz=UTC), "id": notice_id},
    )
    if body.select_goal and row.get("goal_id"):
        takeover_select_goal(
            goal_id=row["goal_id"],
            body=TakeoverGoalSelectRequest(session_id=body.session_id),
            auth=auth,
            db=db,
        )
    updated = db.execute(
        text(
            """
            SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority,
                   expires_at, created_at, acknowledged_at
            FROM autonomy_notices
            WHERE id = :id
            LIMIT 1
            """
        ),
        {"id": notice_id},
    ).mappings().first()
    db.commit()
    return _notice_from_row(updated)


@app.post("/v1/takeover/execution/claim", response_model=DirectiveExecution)
def takeover_execution_claim(
    body: ExecutionClaimRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> DirectiveExecution:
    REQUEST_COUNT.labels(endpoint="takeover_execution_claim", method="POST").inc()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    if body.directive_id:
        row = db.execute(
            text(
                """
                SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                       attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                       failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
                FROM directive_executions
                WHERE directive_id = :directive_id
                  AND session_id = :session_id
                  AND workspace_id = :workspace_id
                  AND user_id = :user_id
                LIMIT 1
                """
            ),
            {
                "directive_id": body.directive_id,
                "session_id": body.session_id,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
            },
        ).mappings().first()
    else:
        row = db.execute(
            text(
                """
                SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                       attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                       failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
                FROM directive_executions
                WHERE session_id = :session_id
                  AND workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND state = :pending
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "session_id": body.session_id,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "pending": DirectiveExecutionState.PENDING.value,
            },
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="no pending directive found")
    directive = _directive_from_row(row)
    now = datetime.now(tz=UTC)
    claimer = body.claimed_by or auth.user_id
    if directive.state == DirectiveExecutionState.IN_PROGRESS:
        if directive.claimed_by and directive.claimed_by != claimer:
            raise HTTPException(status_code=409, detail="directive already claimed by another actor")
        return directive
    if directive.state in {
        DirectiveExecutionState.SUCCEEDED,
        DirectiveExecutionState.FAILED,
        DirectiveExecutionState.BLOCKED,
        DirectiveExecutionState.ABANDONED,
    }:
        return directive

    permit_expiry = None
    permit_id = directive.permit_id
    if directive.requires_permit:
        if not permit_id:
            permit_row = db.execute(
                text(
                    """
                    SELECT id
                    FROM execution_permits
                    WHERE session_id = :session_id
                      AND workspace_id = :workspace_id
                      AND decision = :allow
                      AND (expires_at IS NULL OR expires_at > :now)
                    ORDER BY COALESCE(resolved_at, created_at) DESC
                    LIMIT 1
                    """
                ),
                {
                    "session_id": body.session_id,
                    "workspace_id": auth.workspace_id,
                    "allow": ExecutionPermitDecision.ALLOW.value,
                    "now": now,
                },
            ).mappings().first()
            if permit_row is None:
                raise HTTPException(status_code=409, detail="directive requires permit but no permit_id is attached")
            permit_id = UUID(str(permit_row["id"]))
            db.execute(
                text(
                    """
                    UPDATE directive_executions
                    SET permit_id = :permit_id, updated_at = :updated_at
                    WHERE directive_id = :directive_id
                    """
                ),
                {
                    "permit_id": permit_id,
                    "updated_at": now,
                    "directive_id": directive.directive_id,
                },
            )
            directive.permit_id = permit_id
        permit = db.execute(
            text(
                """
                SELECT decision, expires_at
                FROM execution_permits
                WHERE id = :id AND session_id = :session_id AND workspace_id = :workspace_id
                LIMIT 1
                """
            ),
            {"id": permit_id, "session_id": body.session_id, "workspace_id": auth.workspace_id},
        ).first()
        if permit is None or str(permit[0]) != ExecutionPermitDecision.ALLOW.value:
            raise HTTPException(status_code=409, detail="permit is not approved")
        permit_expiry = _coerce_datetime_value(permit[1]) if permit[1] else None
        if permit_expiry and permit_expiry <= now:
            raise HTTPException(status_code=409, detail="permit expired")
    claim_expiry = now + timedelta(seconds=max(30, int(settings.takeover_execution_claim_ttl_seconds)))
    if permit_expiry and permit_expiry < claim_expiry:
        claim_expiry = permit_expiry
    db.execute(
        text(
            """
            UPDATE directive_executions
            SET state = :state,
                claimed_by = :claimed_by,
                started_at = COALESCE(started_at, :started_at),
                expires_at = :expires_at,
                updated_at = :updated_at
            WHERE directive_id = :directive_id
            """
        ),
        {
            "state": DirectiveExecutionState.IN_PROGRESS.value,
            "claimed_by": claimer,
            "started_at": now,
            "expires_at": claim_expiry,
            "updated_at": now,
            "directive_id": directive.directive_id,
        },
    )
    _sync_enforcement_counters(db, state)
    save_takeover_state(db, state)
    updated = db.execute(
        text(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE directive_id = :directive_id
            LIMIT 1
            """
        ),
        {"directive_id": directive.directive_id},
    ).mappings().first()
    db.commit()
    return _directive_from_row(updated)


def _merge_execution_report_details(body: ExecutionReportRequest) -> dict[str, Any]:
    details_map = dict(body.details or {}) if isinstance(body.details, dict) else {}
    if body.step_id and "step_id" not in details_map:
        details_map["step_id"] = body.step_id
    if body.step_output is not None and "step_output" not in details_map:
        details_map["step_output"] = body.step_output
    if body.contract_type and "contract_type" not in details_map:
        details_map["contract_type"] = body.contract_type
    return details_map


def _normalize_change_summary_map(raw: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(raw, dict):
        return {}, False
    normalized: dict[str, dict[str, Any]] = {}
    redacted_any = False
    for file_path, item in raw.items():
        path_token, redacted_path = redact_text(str(file_path or "").strip())
        redacted_any = redacted_any or redacted_path
        if not path_token:
            continue
        payload = item if isinstance(item, dict) else {}
        try:
            added = max(0, int(payload.get("added") or payload.get("added_lines") or 0))
        except Exception:
            added = 0
        try:
            removed = max(0, int(payload.get("removed") or payload.get("removed_lines") or 0))
        except Exception:
            removed = 0
        intent_token, redacted_intent = redact_text(str(payload.get("intent") or "").strip()[:160])
        redacted_any = redacted_any or redacted_intent
        normalized[path_token[:240]] = {
            "added": added,
            "removed": removed,
            "intent": intent_token[:160],
        }
    return normalized, redacted_any


def _compute_git_change_summary(
    *,
    files: list[str],
    git_payload: dict[str, Any],
    decision_text: str,
) -> dict[str, dict[str, Any]]:
    if not files:
        return {}
    repo_raw = str(git_payload.get("repo") or "").strip()
    repo = repo_raw if repo_raw and os.path.isdir(repo_raw) else os.getcwd()
    commit = str(git_payload.get("commit") or "").strip()
    args: list[str]
    if commit:
        args = ["git", "-C", repo, "show", "--numstat", "--format=", commit]
    else:
        args = ["git", "-C", repo, "diff", "--numstat", "HEAD~1", "HEAD"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=1.5, check=False)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    stats: dict[str, dict[str, Any]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        added_raw, removed_raw, path = parts
        path = str(path or "").strip()
        if not path:
            continue
        try:
            added = 0 if added_raw == "-" else max(0, int(added_raw))
        except Exception:
            added = 0
        try:
            removed = 0 if removed_raw == "-" else max(0, int(removed_raw))
        except Exception:
            removed = 0
        stats[path] = {"added": added, "removed": removed}
    if not stats:
        return {}
    intent, _ = redact_text(str(decision_text or "").strip()[:160])
    summary: dict[str, dict[str, Any]] = {}
    file_set = {str(item).strip() for item in files if str(item).strip()}
    for file_path in file_set:
        item = stats.get(file_path) or {"added": 0, "removed": 0}
        summary[file_path] = {"added": int(item.get("added", 0)), "removed": int(item.get("removed", 0)), "intent": intent[:160]}
    return summary


def _persist_handoff_record(
    db: Session,
    *,
    auth: AuthContext,
    session_id: str,
    directive_id: UUID,
    event_id: UUID | None,
    milestone: dict[str, Any],
    redaction_applied: bool,
    recorded_at: datetime,
) -> UUID:
    payload = milestone.get("payload") if isinstance(milestone.get("payload"), dict) else {}
    outcome = milestone.get("outcome") if isinstance(milestone.get("outcome"), dict) else {}
    files = list(payload.get("files") or [])[:40]
    decision_text = str(milestone.get("decision") or "")[:500]
    next_step_text = str(outcome.get("next_step") or "")[:300]
    objective_text, redacted_objective = normalize_objective_text(
        title=milestone.get("title"),
        decision=decision_text,
        next_step=next_step_text,
        fallback=str(milestone.get("task") or ""),
    )
    precomputed_change_summary, redacted_change_summary = _normalize_change_summary_map(
        milestone.get("change_summary_json")
    )
    if not precomputed_change_summary:
        precomputed_change_summary = _compute_git_change_summary(
            files=files,
            git_payload=dict(milestone.get("git") or {}),
            decision_text=decision_text,
        )
        precomputed_change_summary, redacted_change_summary = _normalize_change_summary_map(precomputed_change_summary)
    handoff = HandoffRecord(
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        session_id=session_id,
        directive_id=directive_id,
        ts=recorded_at,
        title=str(milestone.get("title") or "")[:160],
        decision=decision_text,
        next_step=next_step_text,
        status=str(outcome.get("status") or "failed")[:32],
        files_json=files,
        anchors_json=list(milestone.get("anchors") or [])[:40],
        git_json=dict(milestone.get("git") or {}),
        change_summary_json=precomputed_change_summary,
        objective_text=objective_text[:300],
        source=str(milestone.get("source") or "native")[:24],
        event_id=event_id,
        schema_version=str(milestone.get("milestone_schema") or "v1"),
        redaction_applied=bool(redaction_applied or redacted_objective or redacted_change_summary),
        expires_at=recorded_at + timedelta(days=max(1, int(getattr(settings, "handoff_retention_days", 90)))),
    )
    db.add(handoff)
    db.commit()
    db.refresh(handoff)
    return handoff.id


def _normalize_execution_transcript_details(details_map: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized: dict[str, Any] = {}
    issues: list[str] = []

    tools_raw = details_map.get("tools_called", details_map.get("tool_calls"))
    if tools_raw is not None:
        if isinstance(tools_raw, list):
            normalized["tools_called"] = _normalize_string_list(tools_raw, max_items=12, item_max_chars=120)
        else:
            issues.append("tools_called must be a list")

    findings_raw = details_map.get("key_findings", details_map.get("findings"))
    if findings_raw is not None:
        if isinstance(findings_raw, list):
            normalized["key_findings"] = _normalize_string_list(findings_raw, max_items=12, item_max_chars=180)
        else:
            issues.append("key_findings must be a list")

    files_raw = details_map.get("files_modified", details_map.get("files"))
    if files_raw is not None:
        if isinstance(files_raw, list):
            normalized["files_modified"] = _normalize_string_list(files_raw, max_items=20, item_max_chars=180)
        else:
            issues.append("files_modified must be a list")

    reasoning_raw = details_map.get("reasoning_summary", details_map.get("summary"))
    if reasoning_raw is not None:
        if isinstance(reasoning_raw, str):
            normalized["reasoning_summary"] = _redacted_excerpt(
                reasoning_raw,
                max_chars=max(120, int(getattr(settings, "takeover_execution_reasoning_summary_max_chars", 500))),
            )
        else:
            issues.append("reasoning_summary must be a string")

    return normalized, issues


def _cap_snapshot_payload(snapshot: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    if max_chars <= 0:
        return snapshot
    working = dict(snapshot)

    def _encoded_len(payload: dict[str, Any]) -> int:
        try:
            return len(json.dumps(payload, sort_keys=True, default=str))
        except Exception:
            return 0

    if _encoded_len(working) <= max_chars:
        return working

    for key in ("evidence_snippets", "alternatives_considered"):
        values = working.get(key)
        while isinstance(values, list) and values and _encoded_len(working) > max_chars:
            values.pop()
        if isinstance(values, list) and not values:
            working.pop(key, None)

    if _encoded_len(working) > max_chars and isinstance(working.get("reasoning_summary"), str):
        working["reasoning_summary"] = _redacted_excerpt(working.get("reasoning_summary", ""), max_chars=160)
    if _encoded_len(working) > max_chars and isinstance(working.get("user_question"), str):
        working["user_question"] = _redacted_excerpt(working.get("user_question", ""), max_chars=160)
    if _encoded_len(working) > max_chars:
        working.pop("reasoning_summary", None)
    if _encoded_len(working) > max_chars:
        working.pop("alternatives_considered", None)
    if _encoded_len(working) > max_chars:
        working.pop("evidence_snippets", None)
    return working


def _build_enriched_execution_context_snapshot(
    *,
    base_snapshot: dict[str, Any],
    takeover_context: dict[str, Any],
    working_set: dict[str, Any],
    details_map: dict[str, Any],
    transcript: dict[str, Any],
) -> dict[str, Any]:
    if not bool(getattr(settings, "takeover_rationale_enrichment_enabled", True)):
        return base_snapshot
    if not bool(getattr(settings, "takeover_context_snapshot_enrichment_enabled", True)):
        return base_snapshot

    max_evidence = max(1, int(getattr(settings, "takeover_context_snapshot_max_evidence_snippets", 3)))
    max_alternatives = max(1, int(getattr(settings, "takeover_context_snapshot_max_alternatives", 3)))
    max_chars = max(240, int(getattr(settings, "takeover_context_snapshot_max_chars", 1000)))

    user_question = (
        details_map.get("user_question")
        or takeover_context.get("last_user_message")
        or takeover_context.get("objective")
        or ""
    )
    alternatives = _normalize_string_list(
        details_map.get("alternatives_considered", details_map.get("alternatives")),
        max_items=max_alternatives,
        item_max_chars=160,
    )
    evidence_snippets: list[str] = []
    for item in working_set.get("citation_snippets", []) if isinstance(working_set.get("citation_snippets"), list) else []:
        if not isinstance(item, dict):
            continue
        excerpt = _redacted_excerpt(
            item.get("excerpt"),
            max_chars=max(32, int(getattr(settings, "takeover_citation_snippet_max_chars", 100))),
        )
        if excerpt:
            evidence_snippets.append(excerpt)
        if len(evidence_snippets) >= max_evidence:
            break
    if not evidence_snippets:
        evidence_snippets = _normalize_string_list(
            transcript.get("key_findings"),
            max_items=max_evidence,
            item_max_chars=max(48, int(getattr(settings, "takeover_citation_snippet_max_chars", 100))),
        )

    enriched = dict(base_snapshot)
    if user_question:
        enriched["user_question"] = _redacted_excerpt(user_question, max_chars=280)
    if alternatives:
        enriched["alternatives_considered"] = alternatives
    if evidence_snippets:
        enriched["evidence_snippets"] = evidence_snippets
    if isinstance(transcript.get("reasoning_summary"), str) and transcript.get("reasoning_summary"):
        enriched["reasoning_summary"] = _redacted_excerpt(
            transcript["reasoning_summary"],
            max_chars=max(120, int(getattr(settings, "takeover_execution_reasoning_summary_max_chars", 500))),
        )
    return _cap_snapshot_payload(enriched, max_chars=max_chars)


def _validate_dependency_plan(dependency_plan: Any) -> tuple[bool, str | None]:
    if not isinstance(dependency_plan, dict):
        return False, "dependency_plan must be an object"
    steps = dependency_plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return False, "dependency_plan.steps must be a non-empty array"
    ids: set[str] = set()
    graph: dict[str, set[str]] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return False, f"dependency_plan.steps[{index}] must be an object"
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            return False, f"dependency_plan.steps[{index}].id is required"
        if step_id in ids:
            return False, f"duplicate dependency step id: {step_id}"
        ids.add(step_id)
        depends = step.get("depends_on", [])
        if depends is None:
            depends = []
        if not isinstance(depends, list):
            return False, f"dependency_plan.steps[{index}].depends_on must be an array"
        graph[step_id] = {str(item).strip() for item in depends if str(item).strip()}
    for step_id, deps in graph.items():
        dangling = sorted(dep for dep in deps if dep not in ids)
        if dangling:
            return False, f"dangling dependency for {step_id}: {', '.join(dangling)}"
    visiting: set[str] = set()
    visited: set[str] = set()

    def _has_cycle(node: str) -> bool:
        if node in visited:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in graph.get(node, set()):
            if _has_cycle(dep):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for node in graph:
        if _has_cycle(node):
            return False, "dependency cycle detected"
    return True, None


def _validate_typed_contract(details_map: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(getattr(settings, "typed_contract_enabled", False)):
        return None
    contract_type = str(details_map.get("contract_type") or "").strip().lower()
    if not contract_type:
        return None
    step_id = str(details_map.get("step_id") or "").strip()
    step_output = details_map.get("step_output")
    errors: list[str] = []
    if not step_id:
        errors.append("step_id is required")
    if not isinstance(step_output, dict):
        errors.append("step_output must be an object")
    if contract_type not in {"research_result", "change_plan", "verification_result"}:
        errors.append(f"unsupported contract_type: {contract_type}")
    elif isinstance(step_output, dict):
        if contract_type == "research_result":
            if not isinstance(step_output.get("findings"), list):
                errors.append("research_result.step_output.findings must be a list")
            if not isinstance(step_output.get("sources"), list):
                errors.append("research_result.step_output.sources must be a list")
        elif contract_type == "change_plan":
            if not isinstance(step_output.get("changes"), list):
                errors.append("change_plan.step_output.changes must be a list")
            if not isinstance(step_output.get("files"), list):
                errors.append("change_plan.step_output.files must be a list")
        elif contract_type == "verification_result":
            checks = step_output.get("checks")
            if not isinstance(checks, dict):
                errors.append("verification_result.step_output.checks must be an object")
            if "passed" not in step_output:
                errors.append("verification_result.step_output.passed is required")
    return {
        "enabled": True,
        "contract_type": contract_type,
        "valid": len(errors) == 0,
        "errors": errors,
    }


@app.post("/v1/takeover/execution/report", response_model=dict)
def takeover_execution_report(
    body: ExecutionReportRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="takeover_execution_report", method="POST").inc()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, db)
    row = db.execute(
        text(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE directive_id = :directive_id
              AND session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            LIMIT 1
            """
        ),
        {
            "directive_id": body.directive_id,
            "session_id": body.session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
        },
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="directive not found")
    current = _directive_from_row(row)
    now = datetime.now(tz=UTC)
    failure_class = None
    retry_strategy = None
    retry_feedback: dict[str, Any] | None = None
    retry_scheduled = False
    retry_directive_id = None
    execution_observation_id: UUID | None = None
    current_meta = dict(current.meta or {}) if isinstance(current.meta, dict) else {}
    merged_meta: dict[str, Any] = dict(current_meta)
    details_map = _merge_execution_report_details(body)
    checkpoint_anchor = _latest_editor_checkpoint_anchor(
        db,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        session_id=body.session_id,
    )
    milestone_result = _normalize_execution_milestone(
        details_map=details_map,
        fallback_title=f"Directive {body.state.value}: {current.action_kind}",
        state_value=body.state.value,
        checkpoint_anchor=checkpoint_anchor,
    )
    milestone = dict(milestone_result["milestone"])
    milestone["change_summary_json"] = details_map.get("change_summary_json") or details_map.get("change_summary") or {}
    milestone["source"] = "native"
    details_map.update(milestone)
    milestone_mode = normalize_handoff_mode(getattr(settings, "handoff_milestone_validation_mode", "shadow"))
    merged_meta["milestone_validation"] = {
        "mode": milestone_mode,
        "valid": bool(milestone_result["valid"]),
        "errors": milestone_result["errors"],
        "redaction_applied": bool(milestone_result["redaction_applied"]),
    }
    if milestone_mode == "enforce" and not milestone_result["valid"]:
        raise HTTPException(status_code=422, detail={"message": "invalid milestone payload", "errors": milestone_result["errors"]})
    if details_map:
        merged_meta.update(details_map)
    execution_transcript: dict[str, Any] = {}
    if bool(getattr(settings, "takeover_rationale_enrichment_enabled", True)) and bool(
        getattr(settings, "takeover_execution_details_contract_enabled", True)
    ):
        execution_transcript, transcript_issues = _normalize_execution_transcript_details(details_map)
        merged_meta["execution_transcript_contract"] = {
            "enabled": True,
            "valid": len(transcript_issues) == 0,
            "issues": transcript_issues,
            "present": bool(execution_transcript),
        }
        if execution_transcript:
            merged_meta["execution_transcript"] = execution_transcript
    validation_errors: list[str] = []
    dependency_plan = details_map.get("dependency_plan") or current_meta.get("dependency_plan")
    if dependency_plan is not None:
        plan_valid, plan_error = _validate_dependency_plan(dependency_plan)
        merged_meta["dependency_preflight"] = {
            "valid": bool(plan_valid),
            "error": plan_error,
        }
        if not plan_valid and plan_error:
            validation_errors.append(plan_error)
    contract_validation = _validate_typed_contract(details_map)
    if contract_validation is not None:
        merged_meta["contract_validation"] = contract_validation
        if not bool(contract_validation.get("valid", True)):
            validation_errors.extend([str(item) for item in (contract_validation.get("errors") or []) if str(item)])
    effective_state = body.state
    effective_failure_reason = body.failure_reason
    if validation_errors:
        effective_state = DirectiveExecutionState.FAILED
        effective_failure_reason = (
            f"validation_failure: {'; '.join(validation_errors)}"[:500]
        )
    milestone_outcome = milestone.get("outcome") if isinstance(milestone.get("outcome"), dict) else {}
    milestone_outcome["status"] = effective_state.value
    milestone["outcome"] = milestone_outcome
    merged_meta["outcome_recorded"] = True
    merged_meta["reported_state"] = effective_state.value
    merged_meta["reported_at"] = now.isoformat()
    if effective_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED, DirectiveExecutionState.ABANDONED}:
        failure_class = classify_failure(
            result=body.result,
            failure_reason=effective_failure_reason,
            details=details_map,
        )
        retry_strategy = retry_strategy_for_attempt(
            attempt=int(current.attempt) + 1,
            failure_class=failure_class,
            rollback_available=bool(body.rollback_performed or bool((details_map or {}).get("rollback_available"))),
        )
        if bool(getattr(settings, "retry_feedback_enabled", False)):
            retry_feedback = build_retry_feedback(
                action_kind=current.action_kind,
                failure_class=failure_class,
                failure_reason=effective_failure_reason,
                retry_strategy=retry_strategy,
            )
            merged_meta["retry_feedback"] = retry_feedback
    db.execute(
        text(
            """
            UPDATE directive_executions
            SET state = :state,
                finished_at = :finished_at,
                failure_class = :failure_class,
                failure_reason = :failure_reason,
                retry_strategy = :retry_strategy,
                meta = CAST(:meta AS jsonb),
                updated_at = :updated_at
            WHERE directive_id = :directive_id
            """
        ),
        {
            "state": effective_state.value,
            "finished_at": now,
            "failure_class": failure_class.value if failure_class else None,
            "failure_reason": effective_failure_reason,
            "retry_strategy": retry_strategy.value if hasattr(retry_strategy, "value") else str(retry_strategy)
            if retry_strategy
            else None,
            "meta": json.dumps(merged_meta),
            "updated_at": now,
            "directive_id": body.directive_id,
        },
    )
    completion_outbox = enqueue_handoff(
        db,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        behavior_subject_id=auth.behavior_subject_id,
        session_id=body.session_id,
        directive_id=body.directive_id,
        completion_key=f"directive:{body.directive_id}:{effective_state.value}",
        terminal_state=effective_state.value,
        milestone=milestone,
        source="native",
        redaction_applied=bool(milestone_result.get("redaction_applied", False)),
        now=now,
    )
    if (
        settings.takeover_retry_enabled
        and effective_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}
        and int(current.attempt) < int(settings.takeover_retry_max_attempts)
        and retry_strategy not in {None, RetryStrategy.ESCALATE}
    ):
        window_start = now - timedelta(minutes=max(1, int(settings.takeover_retry_window_minutes)))
        retry_count = db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM directive_executions
                WHERE session_id = :session_id
                  AND workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND created_at >= :window_start
                  AND state IN (:pending, :in_progress)
                """
            ),
            {
                "session_id": body.session_id,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "window_start": window_start,
                "pending": DirectiveExecutionState.PENDING.value,
                "in_progress": DirectiveExecutionState.IN_PROGRESS.value,
            },
        ).scalar()
        if int(retry_count or 0) < int(settings.takeover_retry_window_limit):
            retry_directive_id = uuid.uuid4()
            db.execute(
                text(
                    """
                    INSERT INTO directive_executions(
                        directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                        attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                        failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
                    )
                    VALUES(
                        :directive_id, :session_id, :workspace_id, :user_id, :goal_id, :objective_hash, :action_kind,
                        :attempt, :state, :requires_permit, :permit_id, NULL, NULL, NULL, NULL,
                        NULL, NULL, :retry_strategy, CAST(:meta AS jsonb), :created_at, :updated_at
                    )
                    """
                ),
                {
                    "directive_id": retry_directive_id,
                    "session_id": current.session_id,
                    "workspace_id": current.workspace_id,
                    "user_id": current.user_id,
                    "goal_id": current.goal_id,
                    "objective_hash": current.objective_hash,
                    "action_kind": current.action_kind,
                    "attempt": int(current.attempt) + 1,
                    "state": DirectiveExecutionState.PENDING.value,
                    "requires_permit": bool(current.requires_permit),
                    "permit_id": None,
                    "retry_strategy": retry_strategy.value if hasattr(retry_strategy, "value") else str(retry_strategy),
                    "meta": json.dumps(
                        {
                            "retry_of": str(current.directive_id),
                            "previous_failure": effective_failure_reason,
                            "retry_feedback": retry_feedback or {},
                        }
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            retry_scheduled = True
    state_for_snapshot = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    takeover_context_for_snapshot = (
        dict(state_for_snapshot.takeover_context or {})
        if isinstance(state_for_snapshot.takeover_context, dict)
        else {}
    )
    working_set_for_snapshot = (
        dict(state_for_snapshot.working_set_json or {})
        if isinstance(state_for_snapshot.working_set_json, dict)
        else {}
    )

    try:
        execution_state = effective_state.value
        if effective_state == DirectiveExecutionState.SUCCEEDED:
            situation_type = "routine_task"
            outcome_sentiment = "positive"
        elif effective_state in {DirectiveExecutionState.FAILED, DirectiveExecutionState.BLOCKED}:
            situation_type = "error_occurred"
            outcome_sentiment = "negative"
        else:
            situation_type = "escalation_point"
            outcome_sentiment = "neutral"
        summary_key = str(current.objective_hash or current.action_kind or "execution")
        citation_ids = (
            [str(item) for item in working_set_for_snapshot.get("citations", []) if isinstance(item, str)][:20]
            if isinstance(working_set_for_snapshot.get("citations"), list)
            else []
        )
        base_context_snapshot = {
            "session_id": body.session_id,
            "directive_id": str(body.directive_id),
            "attempt": int(current.attempt),
            "retry_scheduled": retry_scheduled,
            "failure_class": failure_class.value if failure_class else None,
            "retry_strategy": retry_strategy.value if hasattr(retry_strategy, "value") else str(retry_strategy)
            if retry_strategy
            else None,
            "decision_confidence": _safe_float(merged_meta.get("decision_confidence"), default=0.0),
            "context_quality_score": _safe_float(merged_meta.get("context_quality_score"), default=0.0),
            "citation_ids": citation_ids,
        }
        context_snapshot = _build_enriched_execution_context_snapshot(
            base_snapshot=base_context_snapshot,
            takeover_context=takeover_context_for_snapshot,
            working_set=working_set_for_snapshot,
            details_map=details_map,
            transcript=execution_transcript,
        )
        execution_observation_id = save_observation(
            db,
            {
                "consumer_id": auth.consumer,
                "workspace_id": auth.workspace_id,
                "situation_type": situation_type,
                "situation_summary": f"execution:{current.action_kind}:{summary_key}"[:500],
                "user_response": str(body.result or effective_failure_reason or execution_state)[:1000],
                "response_reasoning": "execution_report",
                "outcome": execution_state,
                "outcome_sentiment": outcome_sentiment,
                "confidence": 0.82 if effective_state == DirectiveExecutionState.SUCCEEDED else 0.66,
                "context_snapshot": context_snapshot,
            },
        )
        merged_meta["observation_id"] = str(execution_observation_id)
        db.execute(
            text(
                """
                UPDATE directive_executions
                SET meta = CAST(:meta AS jsonb),
                    updated_at = :updated_at
                WHERE directive_id = :directive_id
                """
            ),
            {
                "meta": json.dumps(merged_meta),
                "updated_at": now,
                "directive_id": body.directive_id,
            },
        )
    except Exception:
        logger.warning("failed to persist execution report observation", exc_info=True)
    try:
        execution_state = effective_state.value
        _evolve_execution_pattern(
            db,
            workspace_id=auth.workspace_id,
            action_kind=str(current.action_kind or "execute"),
            objective_hash_value=current.objective_hash,
            execution_state=execution_state,
        )
        _upsert_workflow_skill_template(
            db,
            workspace_id=auth.workspace_id,
            action_kind=str(current.action_kind or "execute"),
            objective_hash_value=current.objective_hash,
            execution_state=execution_state,
        )
    except Exception:
        logger.warning("failed to update phase3 skill/evolution state", exc_info=True)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    context = dict(state.takeover_context or {})
    _update_structured_plan_progress_context(
        context,
        execution_state=effective_state,
        updated_at=now,
    )
    _update_objective_quality_context(
        context,
        execution_state=effective_state,
        retry_scheduled=retry_scheduled,
        updated_at=now,
        details=details_map,
        required_verifications=[
            str(item).strip().lower()
            for item in ((current.meta or {}).get("verification_required") or _required_verification_checks())
            if str(item).strip()
        ],
    )
    _update_objective_contract_context(
        context,
        execution_state=effective_state,
        details=details_map,
        updated_at=now,
    )
    feedback_result = (
        "success"
        if effective_state == DirectiveExecutionState.SUCCEEDED
        else "blocked"
        if effective_state == DirectiveExecutionState.BLOCKED
        else "failure"
    )
    updated_outcomes, updated_autonomy = update_recent_outcomes(
        state.recent_outcomes_json,
        feedback_result,
        turn=int(state.takeover_context.get("turn_count", 0) or 0),
        latency_ms=int((details_map or {}).get("latency_ms") or 0) or None,
    )
    state.recent_outcomes_json = updated_outcomes
    state.autonomy_score = updated_autonomy
    if effective_state == DirectiveExecutionState.SUCCEEDED:
        goal_to_complete = current.goal_id or state.active_goal_id
        if goal_to_complete:
            db.execute(
                text(
                    """
                    UPDATE autonomy_goals
                    SET status = :status,
                        updated_at = :updated_at
                    WHERE id = :id
                      AND session_id = :session_id
                      AND workspace_id = :workspace_id
                      AND user_id = :user_id
                    """
                ),
                {
                    "status": AutonomyGoalStatus.DONE.value,
                    "updated_at": now,
                    "id": goal_to_complete,
                    "session_id": state.session_id,
                    "workspace_id": state.workspace_id,
                    "user_id": state.user_id,
                },
            )
            if state.active_goal_id and str(state.active_goal_id) == str(goal_to_complete):
                state.active_goal_id = None
        previous_completed_hash = str(context.get("last_completed_objective_hash") or "")
        current_completed_hash = str(current.objective_hash or "")
        if previous_completed_hash and previous_completed_hash == current_completed_hash:
            same_streak = int(context.get("completed_same_objective_streak", 0) or 0) + 1
        else:
            same_streak = 1
        context["completed_same_objective_streak"] = same_streak
        if same_streak >= 3:
            context["force_user_objective_refresh"] = True
        context["awaiting_next_objective"] = True
        context["awaiting_next_objective_turns"] = 0
        context["last_completed_directive_id"] = str(current.directive_id)
        context["last_completed_objective_hash"] = current_completed_hash
        context["last_completed_at"] = now.isoformat()
        context.pop("_pending_directive_locked", None)
        context.pop("objective", None)
        state.objective_hash = ""
        state.goal_queue_size = max(0, int(state.goal_queue_size or 0) - 1)
        _invalidate_goal_queue_cache(db, auth=auth, state=state)
    state.takeover_context = context
    _sync_enforcement_counters(db, state)
    save_takeover_state(db, state)
    db.commit()
    delivered_outbox = deliver_handoff_safely(
        db,
        outbox_id=completion_outbox.id,
        retention_days=int(settings.handoff_retention_days),
    )
    if delivered_outbox.status == "pending":
        try:
            enqueue_job("tce_worker.jobs.handoff_outbox.run", str(delivered_outbox.id), 1)
        except Exception:
            logger.warning("failed to enqueue pending handoff outbox", exc_info=True)
    return {
        "directive_id": str(body.directive_id),
        "state": effective_state.value,
        "retry_scheduled": retry_scheduled,
        "retry_directive_id": str(retry_directive_id) if retry_directive_id else None,
        "failure_class": failure_class.value if failure_class else None,
        "retry_strategy": retry_strategy.value if hasattr(retry_strategy, "value") else retry_strategy,
        "retry_feedback": retry_feedback or {},
        "contract_validation": merged_meta.get("contract_validation"),
        "dependency_preflight": merged_meta.get("dependency_preflight"),
        "milestone_validation": merged_meta.get("milestone_validation"),
        "handoff_record_id": str(delivered_outbox.handoff_record_id),
        "completion_outbox_id": str(delivered_outbox.id),
        "completion_delivery_status": delivered_outbox.status,
        "objective_quality": context.get("objective_quality", {}),
        "objective_contract": context.get("objective_contract", {}),
        "updated_at": now.isoformat(),
    }


@app.get("/v1/takeover/execution/status", response_model=ExecutionStatusResponse)
def takeover_execution_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ExecutionStatusResponse:
    REQUEST_COUNT.labels(endpoint="takeover_execution_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    pending_rows = db.execute(
        text(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND state IN (:pending, :in_progress)
            ORDER BY created_at DESC
            LIMIT 20
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
            "pending": DirectiveExecutionState.PENDING.value,
            "in_progress": DirectiveExecutionState.IN_PROGRESS.value,
        },
    ).mappings().all()
    recent_rows = db.execute(
        text(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            ORDER BY updated_at DESC
            LIMIT 30
            """
        ),
        {"session_id": session_id, "workspace_id": auth.workspace_id, "user_id": auth.user_id},
    ).mappings().all()
    return ExecutionStatusResponse(
        session_id=session_id,
        pending=[_directive_from_row(row) for row in pending_rows],
        recent=[_directive_from_row(row) for row in recent_rows],
        generated_at=datetime.now(tz=UTC),
    )


@app.get("/v1/workflow/templates")
def list_workflow_templates(
    session_id: str = "default",
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="workflow_templates_list", method="GET").inc()
    _enforce_workspace_access(auth, db)
    effective_session_id = _resolve_effective_session_id(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    cap = max(1, min(100, int(limit or 20)))
    domain = f"{auth.workspace_id}:takeover"
    rows = (
        db.query(WorkflowTemplate)
        .filter(WorkflowTemplate.domain == domain)
        .order_by(WorkflowTemplate.updated_at.desc())
        .limit(cap)
        .all()
    )
    templates: list[dict[str, Any]] = []
    for row in rows:
        graph = row.graph if isinstance(row.graph, dict) else {}
        triggers = row.triggers if isinstance(row.triggers, dict) else {}
        templates.append(
            {
                "id": str(row.id),
                "name": str(row.name),
                "domain": str(row.domain),
                "graph": graph,
                "triggers": triggers,
                "version": int(row.version or 1),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    return {
        "session_id": effective_session_id,
        "templates": templates,
        "total": len(templates),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


class LifecycleRunRequest(BaseModel):
    retention_days: int | None = None
    dry_run: bool | None = None


def _read_runtime_setting_json(db: Session, key: str, default: dict[str, Any]) -> dict[str, Any]:
    row = db.execute(text("SELECT value FROM runtime_settings WHERE key = :key"), {"key": key}).fetchone()
    if not row:
        return default
    value = row[0]
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else default
        except json.JSONDecodeError:
            return default
    return default


def _coerce_goal_source(value: str | None) -> AutonomyGoalSource:
    try:
        return AutonomyGoalSource(str(value or AutonomyGoalSource.OPEN_DISCOVERY.value))
    except Exception:
        return AutonomyGoalSource.OPEN_DISCOVERY


def _coerce_goal_status(value: str | None) -> AutonomyGoalStatus:
    try:
        return AutonomyGoalStatus(str(value or AutonomyGoalStatus.CANDIDATE.value))
    except Exception:
        return AutonomyGoalStatus.CANDIDATE


def _coerce_risk_tier(value: str | None) -> AutonomyRiskTier:
    try:
        return AutonomyRiskTier(str(value or AutonomyRiskTier.MEDIUM.value))
    except Exception:
        return AutonomyRiskTier.MEDIUM


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def _coerce_datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except Exception:
            pass
    return datetime.now(tz=UTC)


def _goal_from_row(row: Any) -> TakeoverGoal:
    evidence_raw = _row_value(row, "evidence_event_ids", []) or []
    evidence_ids: list[UUID] = []
    for value in evidence_raw:
        if isinstance(value, UUID):
            evidence_ids.append(value)
            continue
        try:
            evidence_ids.append(UUID(str(value)))
        except Exception:
            continue
    return TakeoverGoal(
        id=UUID(str(_row_value(row, "id"))),
        session_id=str(_row_value(row, "session_id", "")),
        workspace_id=str(_row_value(row, "workspace_id", "")),
        user_id=str(_row_value(row, "user_id", "")),
        title=str(_row_value(row, "title", "")),
        description=str(_row_value(row, "description", "")),
        source=_coerce_goal_source(_row_value(row, "source")),
        priority_score=float(_row_value(row, "priority_score", 0.0) or 0.0),
        risk_tier=_coerce_risk_tier(_row_value(row, "risk_tier")),
        confidence=float(_row_value(row, "confidence", 0.0) or 0.0),
        reasoning=str(_row_value(row, "reasoning", "") or ""),
        evidence_event_ids=evidence_ids,
        goal_kind=GoalKind(str(_row_value(row, "goal_kind", GoalKind.NORMAL.value))),
        affective_scores=_row_value(row, "affective_scores", {}) or {},
        selection_score=float(_row_value(row, "selection_score", 0.0) or 0.0),
        cache_hit=bool(_row_value(row, "cache_hit", False)),
        cache_source=(
            str(_row_value(row, "cache_source"))
            if _row_value(row, "cache_source", None) is not None
            else None
        ),
        status=_coerce_goal_status(_row_value(row, "status")),
        created_at=_coerce_datetime_value(_row_value(row, "created_at")),
        updated_at=_coerce_datetime_value(_row_value(row, "updated_at")),
    )


def _goal_cache_key_for_state(state: TakeoverState, auth: AuthContext) -> str:
    objective_hash_value = state.objective_hash or objective_hash(str(state.takeover_context.get("objective", "")))
    state_version = f"{state.mode.value}:{state.autonomy_policy_profile.value}"
    return goal_cache_key(
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=state.session_id,
        objective_hash=objective_hash_value or "no-objective",
        state_version=state_version,
    )


def _load_goal_queue_cache(db: Session, *, auth: AuthContext, state: TakeoverState) -> tuple[list[TakeoverGoal], str | None]:
    if not settings.takeover_goal_cache_enabled:
        return [], None
    key = _goal_cache_key_for_state(state, auth)
    l1_item, l1_state = goal_cache_get_l1(key)
    if l1_item:
        payload = l1_item.get("payload", {})
        goals_raw = (payload or {}).get("goals", [])
        goals = [TakeoverGoal.model_validate(item) for item in goals_raw if isinstance(item, dict)]
        return goals, "l1"
    if l1_state == "miss":
        try:
            row = db.execute(
                text(
                    """
                    SELECT payload, expires_at
                    FROM autonomy_goal_cache
                    WHERE cache_key = :cache_key
                      AND expires_at >= :now
                    LIMIT 1
                    """
                ),
                {"cache_key": key, "now": datetime.now(tz=UTC)},
            ).mappings().first()
            if row:
                payload = goal_cache_deserialize(row.get("payload"))
                ttl = max(
                    1,
                    int(((_coerce_datetime_value(row.get("expires_at")) - datetime.now(tz=UTC)).total_seconds())),
                )
                goal_cache_put_l1(key, payload, ttl_seconds=ttl, source="l2")
                goals_raw = payload.get("goals", [])
                goals = [TakeoverGoal.model_validate(item) for item in goals_raw if isinstance(item, dict)]
                return goals, "l2"
        except Exception:
            logger.warning("failed loading takeover goal cache", exc_info=True)
    return [], None


def _save_goal_queue_cache(
    db: Session,
    *,
    auth: AuthContext,
    state: TakeoverState,
    goals: list[TakeoverGoal],
) -> None:
    if not settings.takeover_goal_cache_enabled:
        return
    key = _goal_cache_key_for_state(state, auth)
    created_at = datetime.now(tz=UTC)
    l2_ttl = max(1, int(settings.takeover_goal_cache_l2_ttl_seconds))
    expires_at = created_at + timedelta(seconds=l2_ttl)
    payload = {
        "goals": [goal.model_dump(mode="json") for goal in goals],
        "session_id": state.session_id,
        "workspace_id": auth.workspace_id,
        "user_id": auth.user_id,
        "objective_hash": state.objective_hash,
        "generated_at": created_at.isoformat(),
    }
    goal_cache_put_l1(
        key,
        payload,
        ttl_seconds=max(1, int(settings.takeover_goal_cache_l1_ttl_seconds)),
        source="computed",
    )
    try:
        db.execute(
            text(
                """
                INSERT INTO autonomy_goal_cache(
                    cache_key, session_id, workspace_id, user_id, payload,
                    cache_version, created_at, expires_at, last_accessed_at
                )
                VALUES(
                    :cache_key, :session_id, :workspace_id, :user_id, CAST(:payload AS JSONB),
                    :cache_version, :created_at, :expires_at, :last_accessed_at
                )
                ON CONFLICT (cache_key)
                DO UPDATE SET
                    payload = excluded.payload,
                    cache_version = excluded.cache_version,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    last_accessed_at = excluded.last_accessed_at
                """
            ),
            {
                "cache_key": key,
                "session_id": state.session_id,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "payload": json.dumps(payload),
                "cache_version": 1,
                "created_at": created_at,
                "expires_at": expires_at,
                "last_accessed_at": created_at,
            },
        )
    except Exception:
        logger.warning("failed persisting takeover goal cache", exc_info=True)


def _invalidate_goal_queue_cache(
    db: Session,
    *,
    auth: AuthContext,
    state: TakeoverState,
) -> int:
    removed_l1 = goal_cache_invalidate_l1(None)
    removed_l2 = 0
    try:
        removed_l2 = int(
            db.execute(
                text(
                    """
                    DELETE FROM autonomy_goal_cache
                    WHERE session_id = :session_id
                      AND workspace_id = :workspace_id
                      AND user_id = :user_id
                    """
                ),
                {
                    "session_id": state.session_id,
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                },
            ).rowcount
            or 0
        )
    except Exception:
        logger.warning("failed invalidating takeover goal cache", exc_info=True)
    return removed_l1 + removed_l2


def _discover_takeover_goals(
    db: Session,
    *,
    auth: AuthContext,
    state: TakeoverState,
    include_open_discovery: bool = True,
    force_recompute: bool = False,
) -> list[TakeoverGoal]:
    now = datetime.now(tz=UTC)
    candidates: dict[str, dict[str, Any]] = {}
    if not force_recompute:
        cached_goals, cache_source = _load_goal_queue_cache(db, auth=auth, state=state)
        if cached_goals:
            state.takeover_context["_goal_cache_hit"] = True
            state.takeover_context["_goal_cache_source"] = cache_source or "l2"
            state.goal_queue_size = len(cached_goals)
            state.last_discovery_at = now
            save_takeover_state(db, state)
            db.commit()
            return cached_goals
    state.takeover_context["_goal_cache_hit"] = False
    state.takeover_context["_goal_cache_source"] = "computed"

    fingerprint: dict[str, Any] | None = None
    try:
        fp_row = db.execute(
            text(
                """
                SELECT fingerprint
                FROM behavioral_fingerprints
                WHERE workspace_id = :workspace_id
                ORDER BY (consumer_id = :consumer_id) DESC, last_updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": auth.workspace_id, "consumer_id": auth.consumer},
        ).first()
        fingerprint = fp_row[0] if fp_row and isinstance(fp_row[0], dict) else None
    except Exception:
        fingerprint = None

    objective = str(state.takeover_context.get("objective", "")).strip()
    if objective:
        priority = score_goal(urgency=0.9, recency=0.85, blocker_impact=0.8, success_probability=0.75)
        candidates[f"user:{objective.lower()}"] = {
            "title": objective[:140],
            "description": objective[:240],
            "source": AutonomyGoalSource.USER_OBJECTIVE.value,
            "priority_score": priority,
            "risk_tier": AutonomyRiskTier.MEDIUM.value,
            "confidence": 0.88,
            "reasoning": "Active user objective preserved as top priority.",
            "evidence_event_ids": [],
            "urgency": 0.9,
            "recency": 0.85,
            "blocker_impact": 0.8,
            "success_probability": 0.75,
        }

    if include_open_discovery:
        event_rows = db.execute(
            text(
                """
                SELECT id, ts, event_type, title, domain, task_type
                FROM events
                WHERE (context->>'_tce_workspace' IS NULL OR context->>'_tce_workspace' = :workspace_id)
                  AND sensitivity <= :max_sensitivity
                ORDER BY ts DESC
                LIMIT 120
                """
            ),
            {"workspace_id": auth.workspace_id, "max_sensitivity": settings.block_sensitivity - 1},
        ).mappings().all()
        for row in event_rows:
            event_type = str(row["event_type"] or "").upper()
            title = str(row["title"] or "").strip()
            if not title:
                continue
            ts_value = row["ts"]
            if isinstance(ts_value, datetime):
                ts_dt = ts_value if ts_value.tzinfo else ts_value.replace(tzinfo=UTC)
            else:
                ts_dt = datetime.fromisoformat(str(ts_value))
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=UTC)
            age_days = max(0.0, (now - ts_dt.astimezone(UTC)).total_seconds() / 86400.0)
            recency = max(0.0, min(1.0, 1.0 - (age_days / 14.0)))
            urgency = 0.9 if event_type == "ERROR" else 0.65
            blocker_impact = 0.85 if event_type == "ERROR" else 0.55
            confidence = 0.7 if event_type == "ERROR" else 0.58
            priority = score_goal(
                urgency=urgency,
                recency=recency,
                blocker_impact=blocker_impact,
                success_probability=0.68,
            )
            key = f"event:{title.lower()}"
            if key in candidates and candidates[key]["priority_score"] >= priority:
                continue
            candidates[key] = {
                "title": title[:140],
                "description": f"Investigate and resolve: {title[:180]}",
                "source": AutonomyGoalSource.OPEN_DISCOVERY.value,
                "priority_score": priority,
                "risk_tier": AutonomyRiskTier.MEDIUM.value if event_type != "ERROR" else AutonomyRiskTier.HIGH.value,
                "confidence": confidence,
                "reasoning": f"Derived from recent {event_type or 'event'} in workspace timeline.",
                "evidence_event_ids": [row["id"]],
                "urgency": urgency,
                "recency": recency,
                "blocker_impact": blocker_impact,
                "success_probability": 0.68,
            }

        action_rows = db.execute(
            text(
                """
                SELECT action_kind, result, ts
                FROM takeover_action_log
                WHERE session_id = :session_id
                  AND workspace_id = :workspace_id
                  AND user_id = :user_id
                ORDER BY ts DESC
                LIMIT 40
                """
            ),
            {
                "session_id": state.session_id,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
            },
        ).mappings().all()
        for row in action_rows:
            result = str(row["result"] or "").strip().lower()
            if result not in {"blocked", "failure", "needs_human"}:
                continue
            action_kind = str(row["action_kind"] or "work").replace("_", " ").strip()
            title = f"Unblock {action_kind}"[:140]
            key = f"block:{title.lower()}"
            if key in candidates:
                continue
            candidates[key] = {
                "title": title,
                "description": f"Address repeated blocker in takeover flow: {action_kind}.",
                "source": AutonomyGoalSource.OPEN_DISCOVERY.value,
                "priority_score": score_goal(0.86, 0.78, 0.9, 0.52),
                "risk_tier": AutonomyRiskTier.MEDIUM.value,
                "confidence": 0.74,
                "reasoning": "Created from repeated blocked/failed takeover actions.",
                "evidence_event_ids": [],
                "urgency": 0.86,
                "recency": 0.78,
                "blocker_impact": 0.9,
                "success_probability": 0.52,
            }

    db.execute(
        text(
            """
            UPDATE autonomy_goals
            SET status = :status, updated_at = :updated_at
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND status = :candidate_status
            """
        ),
        {
            "status": AutonomyGoalStatus.DROPPED.value,
            "updated_at": now,
            "session_id": state.session_id,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
            "candidate_status": AutonomyGoalStatus.CANDIDATE.value,
        },
    )

    prepared = dedupe_candidates_by_similarity(list(candidates.values()))
    for item in prepared:
        affective_scores = compute_affective_scores(
            source=str(item.get("source", AutonomyGoalSource.OPEN_DISCOVERY.value)),
            evidence_count=len(item.get("evidence_event_ids", []) or []),
            rehearsal_count=len(item.get("evidence_event_ids", []) or []),
            confidence=float(item.get("confidence", 0.6)),
            urgency=float(item.get("urgency", 0.65)),
            recency=float(item.get("recency", 0.65)),
            blocker_impact=float(item.get("blocker_impact", 0.55)),
            success_probability=float(item.get("success_probability", 0.68)),
            recent_outcomes=state.recent_outcomes_json,
            fingerprint=fingerprint,
            similarity={
                "context_relevance": float(item.get("confidence", 0.6)),
                "recent_event_affinity": float(item.get("recency", 0.6)),
                "entity_overlap": min(1.0, len(item.get("evidence_event_ids", []) or []) / 8.0),
                "pattern_confidence": float(item.get("confidence", 0.6)),
                "nearest_goal_distance": 0.7,
                "evidence_depth": min(1.0, len(item.get("evidence_event_ids", []) or []) / 12.0),
            },
        )
        affective_priority, score_breakdown = score_goal_affective(
            urgency=float(item.get("urgency", 0.65)),
            recency=float(item.get("recency", 0.65)),
            blocker_impact=float(item.get("blocker_impact", 0.55)),
            success_probability=float(item.get("success_probability", 0.68)),
            affective_scores=affective_scores,
        )
        item["selection_score"] = affective_priority
        item["goal_kind"] = classify_goal_kind(affective_scores)
        item["affective_scores"] = affective_scores
        item["score_breakdown"] = score_breakdown

    ranked = sorted(
        prepared,
        key=lambda item: (
            float(item.get("selection_score", item.get("priority_score", 0.0))),
            float(item.get("confidence", 0.0)),
        ),
        reverse=True,
    )[:20]
    inserted: list[TakeoverGoal] = []
    for item in ranked:
        goal_id = uuid.uuid4()
        goal_signature = hashlib.sha256(
            f"{item.get('title', '')}|{item.get('description', '')}".encode("utf-8")
        ).hexdigest()[:24]
        db.execute(
            text(
                """
                INSERT INTO autonomy_goals(
                    id, session_id, workspace_id, user_id, title, description,
                    source, priority_score, risk_tier, confidence, reasoning,
                    evidence_event_ids, goal_kind, affective_scores, selection_score,
                    goal_signature, cache_hit, cache_source, status, created_at, updated_at
                )
                VALUES(
                    :id, :session_id, :workspace_id, :user_id, :title, :description,
                    :source, :priority_score, :risk_tier, :confidence, :reasoning,
                    :evidence_event_ids, :goal_kind, CAST(:affective_scores AS JSONB), :selection_score,
                    :goal_signature, :cache_hit, :cache_source, :status, :created_at, :updated_at
                )
                """
            ),
            {
                "id": goal_id,
                "session_id": state.session_id,
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "title": item["title"],
                "description": item["description"],
                "source": item["source"],
                "priority_score": float(item["priority_score"]),
                "risk_tier": item["risk_tier"],
                "confidence": float(item["confidence"]),
                "reasoning": item["reasoning"],
                "evidence_event_ids": item["evidence_event_ids"],
                "goal_kind": item.get("goal_kind", GoalKind.NORMAL.value),
                "affective_scores": json.dumps(item.get("affective_scores", {})),
                "selection_score": float(item.get("selection_score", item["priority_score"])),
                "goal_signature": goal_signature,
                "cache_hit": False,
                "cache_source": "computed",
                "status": AutonomyGoalStatus.CANDIDATE.value,
                "created_at": now,
                "updated_at": now,
            },
        )
        inserted.append(
            TakeoverGoal(
                id=goal_id,
                session_id=state.session_id,
                workspace_id=auth.workspace_id,
                user_id=auth.user_id,
                title=item["title"],
                description=item["description"],
                source=_coerce_goal_source(item["source"]),
                priority_score=float(item["priority_score"]),
                risk_tier=_coerce_risk_tier(item["risk_tier"]),
                confidence=float(item["confidence"]),
                reasoning=item["reasoning"],
                evidence_event_ids=item["evidence_event_ids"],
                goal_kind=GoalKind(str(item.get("goal_kind", GoalKind.NORMAL.value))),
                affective_scores=item.get("affective_scores", {}),
                selection_score=float(item.get("selection_score", item["priority_score"])),
                cache_hit=False,
                cache_source="computed",
                status=AutonomyGoalStatus.CANDIDATE,
                created_at=now,
                updated_at=now,
            )
        )
    state.goal_queue_size = len(inserted)
    state.last_discovery_at = now
    save_takeover_state(db, state)
    _save_goal_queue_cache(db, auth=auth, state=state, goals=inserted)
    db.commit()
    return inserted


def _list_takeover_goals(
    db: Session,
    *,
    state: TakeoverState,
    status: AutonomyGoalStatus | None = None,
) -> list[TakeoverGoal]:
    params: dict[str, Any] = {
        "session_id": state.session_id,
        "workspace_id": state.workspace_id,
        "user_id": state.user_id,
    }
    status_clause = ""
    if status is not None:
        status_clause = " AND status = :status "
        params["status"] = status.value
    rows = db.execute(
        text(
            f"""
            SELECT id, session_id, workspace_id, user_id, title, description, source,
                   priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                   goal_kind, affective_scores, selection_score, cache_hit, cache_source,
                   status, created_at, updated_at
            FROM autonomy_goals
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              {status_clause}
            ORDER BY selection_score DESC, priority_score DESC, updated_at DESC
            LIMIT 50
            """
        ),
        params,
    ).mappings().all()
    return [_goal_from_row(row) for row in rows]


def _notice_from_row(row: Any) -> AutonomyNotice:
    return AutonomyNotice(
        id=row["id"],
        session_id=str(row["session_id"]),
        workspace_id=str(row["workspace_id"]),
        user_id=str(row["user_id"]),
        goal_id=row.get("goal_id"),
        title=str(row.get("title") or ""),
        reason=str(row.get("reason") or ""),
        priority=float(row.get("priority") or 0.0),
        expires_at=_coerce_datetime_value(row.get("expires_at")) if row.get("expires_at") else None,
        created_at=_coerce_datetime_value(row.get("created_at")),
        acknowledged_at=_coerce_datetime_value(row.get("acknowledged_at")) if row.get("acknowledged_at") else None,
    )


def _directive_from_row(row: Any) -> DirectiveExecution:
    return DirectiveExecution(
        directive_id=row["directive_id"],
        session_id=str(row["session_id"]),
        workspace_id=str(row["workspace_id"]),
        user_id=str(row["user_id"]),
        goal_id=row.get("goal_id"),
        objective_hash=str(row.get("objective_hash")) if row.get("objective_hash") else None,
        action_kind=str(row.get("action_kind") or "execute"),
        attempt=int(row.get("attempt") or 1),
        state=DirectiveExecutionState(str(row.get("state") or DirectiveExecutionState.PENDING.value)),
        requires_permit=bool(row.get("requires_permit")),
        permit_id=row.get("permit_id"),
        claimed_by=str(row.get("claimed_by")) if row.get("claimed_by") else None,
        started_at=_coerce_datetime_value(row.get("started_at")) if row.get("started_at") else None,
        finished_at=_coerce_datetime_value(row.get("finished_at")) if row.get("finished_at") else None,
        expires_at=_coerce_datetime_value(row.get("expires_at")) if row.get("expires_at") else None,
        failure_class=str(row.get("failure_class")) if row.get("failure_class") else None,
        failure_reason=str(row.get("failure_reason")) if row.get("failure_reason") else None,
        retry_strategy=str(row.get("retry_strategy")) if row.get("retry_strategy") else None,
        meta=row.get("meta") if isinstance(row.get("meta"), dict) else {},
        created_at=_coerce_datetime_value(row.get("created_at")),
        updated_at=_coerce_datetime_value(row.get("updated_at")),
    )


def _load_pending_directive(db: Session, *, state: TakeoverState) -> DirectiveExecution | None:
    row = db.execute(
        text(
            """
            SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                   attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                   failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND state IN (:pending, :in_progress)
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {
            "session_id": state.session_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "pending": DirectiveExecutionState.PENDING.value,
            "in_progress": DirectiveExecutionState.IN_PROGRESS.value,
        },
    ).mappings().first()
    if not row:
        return None
    directive = _directive_from_row(row)
    now_stamp = datetime.now(tz=UTC)
    if directive.expires_at and directive.expires_at < now_stamp:
        db.execute(
            text(
                """
                UPDATE directive_executions
                SET state = :state, finished_at = :finished_at, updated_at = :updated_at,
                    failure_reason = COALESCE(failure_reason, :failure_reason)
                WHERE directive_id = :directive_id
                """
            ),
            {
                "state": DirectiveExecutionState.ABANDONED.value,
                "finished_at": now_stamp,
                "updated_at": now_stamp,
                "failure_reason": "directive expired",
                "directive_id": directive.directive_id,
            },
        )
        db.commit()
        return None
    stale_seconds = max(120, int(getattr(settings, "takeover_execution_claim_ttl_seconds", 300)) * 3)
    stale_anchor = directive.started_at or directive.updated_at or directive.created_at
    if (
        directive.state == DirectiveExecutionState.IN_PROGRESS
        and stale_anchor is not None
        and stale_anchor < (now_stamp - timedelta(seconds=stale_seconds))
    ):
        db.execute(
            text(
                """
                UPDATE directive_executions
                SET state = :state, finished_at = :finished_at, updated_at = :updated_at,
                    failure_reason = COALESCE(failure_reason, :failure_reason)
                WHERE directive_id = :directive_id
                """
            ),
            {
                "state": DirectiveExecutionState.ABANDONED.value,
                "finished_at": now_stamp,
                "updated_at": now_stamp,
                "failure_reason": "directive stale timeout",
                "directive_id": directive.directive_id,
            },
        )
        db.commit()
        return None
    return directive


def _sync_enforcement_counters(db: Session, state: TakeoverState) -> None:
    pending = db.execute(
        text(
            """
            SELECT COUNT(1) AS total
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND state IN (:pending, :in_progress)
            """
        ),
        {
            "session_id": state.session_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "pending": DirectiveExecutionState.PENDING.value,
            "in_progress": DirectiveExecutionState.IN_PROGRESS.value,
        },
    ).first()
    backlog = db.execute(
        text(
            """
            SELECT COUNT(1) AS total
            FROM directive_executions
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND state = :pending
            """
        ),
        {
            "session_id": state.session_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "pending": DirectiveExecutionState.PENDING.value,
        },
    ).first()
    state.pending_directive_count = int((pending[0] if pending else 0) or 0)
    state.retry_backlog_count = int((backlog[0] if backlog else 0) or 0)


def _open_notice_count_for_session(db: Session, *, state: TakeoverState) -> int:
    row = db.execute(
        text(
            """
            SELECT COUNT(1) AS total
            FROM autonomy_notices
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND acknowledged_at IS NULL
              AND (expires_at IS NULL OR expires_at >= :now)
            """
        ),
        {
            "session_id": state.session_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "now": datetime.now(tz=UTC),
        },
    ).first()
    return int((row[0] if row else 0) or 0)


def _load_active_goal(db: Session, state: TakeoverState) -> TakeoverGoal | None:
    if not state.active_goal_id:
        return None
    row = db.execute(
        text(
            """
            SELECT id, session_id, workspace_id, user_id, title, description, source,
                   priority_score, risk_tier, confidence, reasoning, evidence_event_ids,
                   goal_kind, affective_scores, selection_score, cache_hit, cache_source,
                   status, created_at, updated_at
            FROM autonomy_goals
            WHERE id = :id
              AND session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
            LIMIT 1
            """
        ),
        {
            "id": state.active_goal_id,
            "session_id": state.session_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
        },
    ).mappings().first()
    return _goal_from_row(row) if row else None


def _find_valid_allow_permit(
    db: Session,
    *,
    session_id: str,
    workspace_id: str,
) -> UUID | None:
    row = db.execute(
        text(
            """
            SELECT id
            FROM execution_permits
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND decision = :decision
              AND (expires_at IS NULL OR expires_at >= :now)
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "decision": ExecutionPermitDecision.ALLOW.value,
            "now": datetime.now(tz=UTC),
        },
    ).first()
    return row[0] if row else None


def _is_mutating_intent(message: str, task: str | None, final_response: str | None) -> bool:
    joined = " ".join([message or "", task or "", final_response or ""]).lower()
    hints = (
        "edit ",
        "change ",
        "fix ",
        "implement ",
        "update ",
        "refactor ",
        "rename ",
        "delete ",
        "write ",
        "create ",
    )
    return any(token in joined for token in hints)


def _clamp_confidence(value: float, low: float = 0.05, high: float = 0.98) -> float:
    return max(low, min(high, float(value)))


def _workflow_domain(workspace_id: str) -> str:
    return f"{workspace_id}:takeover"


def _pattern_status_from_confidence(confidence: float) -> str:
    if confidence >= 0.72:
        return "active"
    if confidence >= 0.45:
        return "needs_review"
    return "suppressed"


def _evolve_execution_pattern(
    db: Session,
    *,
    workspace_id: str,
    action_kind: str,
    objective_hash_value: str | None,
    execution_state: str,
) -> None:
    domain = _workflow_domain(workspace_id)
    statement = f"procedure:{action_kind}:{(objective_hash_value or 'general')[:48]}"
    row = db.execute(
        text(
            """
            SELECT id, confidence, version
            FROM patterns
            WHERE domain = :domain
              AND pattern_type = :pattern_type
              AND statement = :statement
            LIMIT 1
            """
        ),
        {"domain": domain, "pattern_type": "procedure", "statement": statement},
    ).mappings().first()
    is_success = execution_state == DirectiveExecutionState.SUCCEEDED.value
    if execution_state in {DirectiveExecutionState.FAILED.value, DirectiveExecutionState.BLOCKED.value}:
        delta = -0.10
    elif is_success:
        delta = 0.08
    else:
        delta = -0.04
    now = datetime.now(tz=UTC)
    if row:
        current_confidence = float(row.get("confidence") or 0.5)
        next_confidence = _clamp_confidence(current_confidence + delta)
        db.execute(
            text(
                """
                UPDATE patterns
                SET confidence = :confidence,
                    status = :status,
                    updated_at = :updated_at,
                    version = :version
                WHERE id = :id
                """
            ),
            {
                "confidence": next_confidence,
                "status": _pattern_status_from_confidence(next_confidence),
                "updated_at": now,
                "version": int(row.get("version") or 1) + 1,
                "id": row["id"],
            },
        )
        return
    seed_confidence = 0.62 if is_success else 0.38
    db.execute(
        text(
            """
            INSERT INTO patterns(id, domain, pattern_type, statement, evidence_event_ids, confidence, status, updated_at, version)
            VALUES(:id, :domain, :pattern_type, :statement, :evidence_event_ids, :confidence, :status, :updated_at, 1)
            """
        ),
        {
            "id": uuid.uuid4(),
            "domain": domain,
            "pattern_type": "procedure",
            "statement": statement,
            "evidence_event_ids": [],
            "confidence": seed_confidence,
            "status": _pattern_status_from_confidence(seed_confidence),
            "updated_at": now,
        },
    )


def _upsert_workflow_skill_template(
    db: Session,
    *,
    workspace_id: str,
    action_kind: str,
    objective_hash_value: str | None,
    execution_state: str,
) -> None:
    domain = _workflow_domain(workspace_id)
    name = f"{action_kind}::{(objective_hash_value or 'general')[:24]}"
    row = db.execute(
        text(
            """
            SELECT id, graph, triggers, version
            FROM workflow_templates
            WHERE name = :name AND domain = :domain
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"name": name, "domain": domain},
    ).mappings().first()
    is_success = execution_state == DirectiveExecutionState.SUCCEEDED.value
    now = datetime.now(tz=UTC)
    default_step_templates = _default_step_templates(action_kind)
    default_steps = [str(item.get("task") or "").strip() for item in default_step_templates if str(item.get("task") or "").strip()]
    if row:
        graph = dict(row.get("graph") or {})
        triggers = dict(row.get("triggers") or {})
        graph["success_count"] = int(graph.get("success_count") or 0) + (1 if is_success else 0)
        graph["failure_count"] = int(graph.get("failure_count") or 0) + (0 if is_success else 1)
        graph["last_result"] = execution_state
        raw_step_templates = graph.get("step_templates", [])
        normalized_step_templates: list[dict[str, Any]] = []
        if isinstance(raw_step_templates, list):
            for raw_template in raw_step_templates:
                normalized = _normalize_step_template(raw_template)
                if normalized is not None:
                    normalized_step_templates.append(normalized)
        if not normalized_step_templates:
            normalized_step_templates = list(default_step_templates)
        graph["step_templates"] = normalized_step_templates
        graph["steps"] = graph.get("steps") or [str(item.get("task") or "").strip() for item in normalized_step_templates]
        triggers["objective_hash"] = objective_hash_value or triggers.get("objective_hash")
        triggers["action_kind"] = action_kind
        db.execute(
            text(
                """
                UPDATE workflow_templates
                SET graph = CAST(:graph AS jsonb),
                    triggers = CAST(:triggers AS jsonb),
                    version = :version,
                    updated_at = :updated_at
                WHERE id = :id
                """
            ),
            {
                "graph": json.dumps(graph),
                "triggers": json.dumps(triggers),
                "version": int(row.get("version") or 1) + 1,
                "updated_at": now,
                "id": row["id"],
            },
        )
        return
    graph = {
        "steps": default_steps,
        "step_templates": default_step_templates,
        "success_count": 1 if is_success else 0,
        "failure_count": 0 if is_success else 1,
        "last_result": execution_state,
    }
    triggers = {
        "workspace_id": workspace_id,
        "action_kind": action_kind,
        "objective_hash": objective_hash_value,
    }
    db.execute(
        text(
            """
            INSERT INTO workflow_templates(id, name, domain, graph, triggers, version, updated_at)
            VALUES(:id, :name, :domain, CAST(:graph AS jsonb), CAST(:triggers AS jsonb), 1, :updated_at)
            """
        ),
        {
            "id": uuid.uuid4(),
            "name": name,
            "domain": domain,
            "graph": json.dumps(graph),
            "triggers": json.dumps(triggers),
            "updated_at": now,
        },
    )


def _default_step_templates(action_kind: str) -> list[dict[str, Any]]:
    normalized_action = str(action_kind or "takeover_step").strip() or "takeover_step"
    return [
        {
            "id": "research",
            "task": f"Analyze scope for {normalized_action}",
            "contract_type": "research_result",
            "expected_keys": ["findings", "sources"],
        },
        {
            "id": "change",
            "task": "Apply minimal change",
            "contract_type": "change_plan",
            "expected_keys": ["changes", "files"],
        },
        {
            "id": "verify",
            "task": "Validate outcome and capture feedback",
            "contract_type": "verification_result",
            "expected_keys": ["checks", "passed"],
        },
    ]


def _normalize_step_template(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    task = str(raw.get("task") or "").strip()
    if not task:
        return None
    step_template: dict[str, Any] = {
        "id": str(raw.get("id") or "step").strip() or "step",
        "task": task[:220],
    }
    contract_type = str(raw.get("contract_type") or "").strip().lower()
    if contract_type in {"research_result", "change_plan", "verification_result"}:
        step_template["contract_type"] = contract_type
    expected_keys_raw = raw.get("expected_keys")
    if isinstance(expected_keys_raw, list):
        expected_keys = [
            str(item).strip()
            for item in expected_keys_raw
            if isinstance(item, str) and str(item).strip()
        ][:6]
        if expected_keys:
            step_template["expected_keys"] = expected_keys
    return step_template


def _template_reliability(graph: dict[str, Any]) -> float:
    success_count = int(graph.get("success_count") or 0)
    failure_count = int(graph.get("failure_count") or 0)
    total = success_count + failure_count
    if total <= 0:
        return 0.0
    return float(success_count) / float(total)


def _load_learned_workflow_step_templates(
    db: Session,
    *,
    workspace_id: str,
    limit: int = 3,
    min_reliability: float = 0.70,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT graph
            FROM workflow_templates
            WHERE domain = :domain
            ORDER BY updated_at DESC
            LIMIT :limit
            """
        ),
        {"domain": _workflow_domain(workspace_id), "limit": max(1, limit)},
    ).mappings().all()
    learned_templates: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    threshold = max(0.0, min(1.0, float(min_reliability)))
    for row in rows:
        graph = row.get("graph") if isinstance(row, dict) else None
        graph = graph if isinstance(graph, dict) else {}
        reliability = _template_reliability(graph)
        if reliability < threshold:
            continue
        candidates: list[dict[str, Any]] = []
        raw_templates = graph.get("step_templates", [])
        if isinstance(raw_templates, list):
            for raw_template in raw_templates:
                normalized = _normalize_step_template(raw_template)
                if normalized is not None:
                    candidates.append(normalized)
        if not candidates:
            fallback_steps = graph.get("steps", [])
            if isinstance(fallback_steps, list):
                for index, raw_step in enumerate(fallback_steps):
                    step = str(raw_step or "").strip()
                    if not step:
                        continue
                    template: dict[str, Any] = {"id": f"learned_{index + 1}", "task": step[:220]}
                    if index == 0:
                        template["contract_type"] = "research_result"
                        template["expected_keys"] = ["findings", "sources"]
                    elif index == 1:
                        template["contract_type"] = "change_plan"
                        template["expected_keys"] = ["changes", "files"]
                    elif index == 2:
                        template["contract_type"] = "verification_result"
                        template["expected_keys"] = ["checks", "passed"]
                    candidates.append(template)
        for template in candidates:
            task = str(template.get("task") or "").strip()
            key = task.lower()
            if not task or key in seen_tasks:
                continue
            seen_tasks.add(key)
            learned_templates.append(template)
            if len(learned_templates) >= 6:
                return learned_templates
    return learned_templates


def _is_complex_objective_text(objective: str) -> bool:
    text_value = str(objective or "").strip().lower()
    if not text_value:
        return False
    if len(text_value) >= 90:
        return True
    separators = [",", ";", " and ", " then ", " after ", " while ", " -> "]
    signal_count = sum(1 for token in separators if token in text_value)
    if signal_count >= 2:
        return True
    action_verbs = ("fix", "implement", "migrate", "refactor", "configure", "verify", "deploy", "test")
    verb_hits = sum(1 for verb in action_verbs if f"{verb} " in text_value)
    return verb_hits >= 3


def _has_explicit_objective_signal(message: str) -> bool:
    normalized = normalize_text(message)
    if not normalized:
        return False
    if normalized in {"ok", "hmm", "sure", "continue", "go on", "next"}:
        return False
    explicit_markers = ("new objective", "next objective", "objective:", "goal:")
    if any(marker in normalized for marker in explicit_markers):
        return True
    objective_verbs = (
        "fix ",
        "build ",
        "decide ",
        "define ",
        "implement ",
        "add ",
        "create ",
        "update ",
        "refactor ",
        "debug ",
        "investigate ",
        "research ",
        "design ",
        "write ",
        "review ",
        "test ",
    )
    if normalized.startswith(objective_verbs):
        return True
    return len(normalized.split()) >= 10 and any(token in normalized for token in objective_verbs)


def _objective_tokens(value: str) -> set[str]:
    normalized = normalize_text(value)
    if not normalized:
        return set()
    parts = re.split(r"[^a-z0-9]+", normalized)
    return {part for part in parts if len(part) >= 3}


def _objective_overlap_score(left: str, right: str) -> float:
    left_tokens = _objective_tokens(left)
    right_tokens = _objective_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    inter = len(left_tokens.intersection(right_tokens))
    union = len(left_tokens.union(right_tokens))
    if union <= 0:
        return 0.0
    return inter / union


def _build_structured_plan_payload(
    *,
    objective: str,
    suggested_steps: list[str],
    learned_templates: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_steps: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for raw_template in learned_templates:
        template = _normalize_step_template(raw_template)
        if template is None:
            continue
        task = str(template.get("task") or "").strip()
        key = task.lower()
        if not task or key in seen_tasks:
            continue
        seen_tasks.add(key)
        step: dict[str, Any] = {
            "task": task[:220],
            "status": "pending",
        }
        contract_type = str(template.get("contract_type") or "").strip().lower()
        if contract_type in {"research_result", "change_plan", "verification_result"}:
            step["contract_type"] = contract_type
        expected_keys_raw = template.get("expected_keys")
        if isinstance(expected_keys_raw, list):
            expected_keys = [
                str(value).strip()
                for value in expected_keys_raw
                if isinstance(value, str) and str(value).strip()
            ][:6]
            if expected_keys:
                step["expected_keys"] = expected_keys
        ordered_steps.append(step)
        if len(ordered_steps) >= 4:
            break
    for candidate in suggested_steps:
        text_value = str(candidate or "").strip()
        key = text_value.lower()
        if not text_value or key in seen_tasks:
            continue
        seen_tasks.add(key)
        ordered_steps.append({"task": text_value[:220], "status": "pending"})
        if len(ordered_steps) >= 4:
            break
    if not ordered_steps:
        ordered_steps = [
            {
                "task": "Scope the task and constraints",
                "status": "pending",
                "contract_type": "research_result",
                "expected_keys": ["findings", "sources"],
            },
            {
                "task": "Implement minimal, reversible changes",
                "status": "pending",
                "contract_type": "change_plan",
                "expected_keys": ["changes", "files"],
            },
            {
                "task": "Run build/test/lint verification and capture results",
                "status": "pending",
                "contract_type": "verification_result",
                "expected_keys": ["checks", "passed"],
            },
            {
                "task": "Update docs/notes and report execution outcome",
                "status": "pending",
            },
        ]
    return {
        "objective": str(objective or "")[:260],
        "steps": [
            {"order": index + 1, **step}
            for index, step in enumerate(ordered_steps)
        ],
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


@app.get("/v1/admin/lifecycle/status")
def lifecycle_status(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="lifecycle_status", method="GET").inc()
    _enforce_workspace_access(auth, db)
    status = _read_runtime_setting_json(db, "lifecycle_status", default={})
    retention = _read_runtime_setting_json(
        db,
        "event_retention",
        default={
            "retention_days": settings.event_retention_days,
            "handoff_retention_days": settings.handoff_retention_days,
            "behavior_control_retention_days": settings.behavior_control_retention_days,
            "archive_enabled": settings.archive_enabled,
            "archive_path": settings.archive_path,
        },
    )
    queue_depth = None
    try:
        queue_depth = get_queue_depth(queue_name_for_job("tce_worker.jobs.archive_events.run"))
    except Exception:
        queue_depth = None
    return {
        "enabled": bool(settings.event_lifecycle_enabled),
        "queue_depth": queue_depth,
        "retention": retention,
        "last_run": status,
        "generated_at": datetime.now(tz=UTC),
    }


@app.post("/v1/admin/lifecycle/run")
def lifecycle_run(
    body: LifecycleRunRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="lifecycle_run", method="POST").inc()
    _enforce_workspace_access(auth, db)
    _reject_advisor_writes(auth)
    retention_days = int(body.retention_days if body.retention_days is not None else settings.event_retention_days)
    dry_run = bool(settings.lifecycle_dry_run if body.dry_run is None else body.dry_run)
    try:
        job_id = enqueue_job("tce_worker.jobs.archive_events.run", retention_days, dry_run)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"failed to enqueue lifecycle job: {exc}") from exc
    queued_status = {
        "status": "queued",
        "queued_at": datetime.now(tz=UTC).isoformat(),
        "queued_by": auth.consumer,
        "retention_days": retention_days,
        "dry_run": dry_run,
        "job_id": job_id,
    }
    db.execute(
        text(
            """
            INSERT INTO runtime_settings (key, value, updated_at)
            VALUES ('lifecycle_status', CAST(:value AS jsonb), :updated_at)
            ON CONFLICT (key) DO UPDATE
            SET value = excluded.value, updated_at = excluded.updated_at
            """
        ),
        {"value": json.dumps(queued_status), "updated_at": datetime.now(tz=UTC)},
    )
    db.commit()
    return queued_status


@app.post("/v1/takeover/preload", response_model=TakeoverPreloadResponse)
def takeover_preload(
    body: TakeoverPreloadRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverPreloadResponse:
    REQUEST_COUNT.labels(endpoint="takeover_preload", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=body.persona_mode,
        activation_keywords=body.activation_keywords,
        stop_keywords=body.stop_keywords,
    )
    now = datetime.now(tz=UTC)
    resolved_task = resolve_objective(
        message=body.task,
        task=body.task,
        takeover_context=state.takeover_context,
    )
    working_set, _ = _build_takeover_working_set(
        db,
        auth,
        task=resolved_task,
        app_context=body.app_context,
        constraints=body.constraints,
    )
    state.takeover_context["objective"] = resolved_task
    state.objective_hash = objective_hash(resolved_task)
    state.working_set_json = working_set
    state.updated_at = now
    save_takeover_state(db, state)
    return TakeoverPreloadResponse(
        session_id=state.session_id,
        objective_hash=state.objective_hash or "",
        working_set_json=working_set,
        refreshed_at=now,
        decision_source=TakeoverDecisionSource.DELIBERATION,
    )


@app.post("/v1/takeover/feedback", response_model=TakeoverFeedbackResponse)
def takeover_feedback(
    body: TakeoverFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverFeedbackResponse:
    REQUEST_COUNT.labels(endpoint="takeover_feedback", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode="normal",
        activation_keywords=None,
        stop_keywords=None,
    )
    updated_outcomes, updated_autonomy = update_recent_outcomes(
        state.recent_outcomes_json,
        body.result,
        turn=body.turn,
        latency_ms=body.latency_ms,
    )
    feedback_details = dict(body.details or {})
    feedback_type = (body.clone_feedback_type.value if body.clone_feedback_type else "").strip().lower()
    if feedback_type:
        feedback_details.setdefault("clone_feedback_type", feedback_type)
    if body.correction_text:
        feedback_details.setdefault("correction_text", body.correction_text)
    normalized_situation_type = _canonical_situation_type(body.situation_type) if body.situation_type else None
    if body.situation_type:
        feedback_details.setdefault("situation_type", normalized_situation_type)
    if body.observation_ids:
        feedback_details.setdefault("observation_ids", [str(value) for value in body.observation_ids])

    behavior_evidence_id: UUID | None = None
    if normalized_situation_type and body.correction_text:
        supersedes_id = body.observation_ids[0] if body.observation_ids else None
        correction_evidence = normalize_behavior_evidence(
            {
                "situation_type": normalized_situation_type,
                "situation_summary": f"Human correction during {body.action_kind}",
                "objective": str(state.takeover_context.get("objective") or f"feedback:{normalized_situation_type}"),
                "selected_choice": body.correction_text,
                "rationale": str(feedback_details.get("reasoning_summary") or "Human correction after takeover feedback"),
                "action_taken": body.correction_text,
                "outcome": body.result,
                "outcome_sentiment": "negative" if feedback_type == "unhelpful" else "neutral",
                "correction_text": body.correction_text if supersedes_id else "",
                "evidence_source": "correction" if supersedes_id else "explicit",
                "memory_class": "preference",
                "supersedes_observation_id": supersedes_id,
                "confidence": 1.0,
                "confirmed_at": datetime.now(tz=UTC),
            }
        )
        correction_gate = behavior_storage_gate(
            correction_evidence,
            threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)),
        )
        try:
            behavior_evidence_id = save_behavior_evidence(
                db,
                consumer_id=auth.consumer,
                workspace_id=auth.workspace_id,
                subject_user_id=auth.behavior_subject_id,
                evidence=correction_evidence,
                storage_gate=correction_gate,
            )
            feedback_details["behavior_evidence_id"] = str(behavior_evidence_id)
        except Exception:
            logger.warning("failed to persist behavior correction evidence", exc_info=True)

    feedback_observation_ids: list[UUID] = list(body.observation_ids or [])
    if behavior_evidence_id is not None and not feedback_observation_ids:
        feedback_observation_ids.append(behavior_evidence_id)
    if normalized_situation_type and body.correction_text and not feedback_observation_ids:
        synthetic_observation = {
            "consumer_id": auth.consumer,
            "workspace_id": auth.workspace_id,
            "situation_type": normalized_situation_type,
            "situation_summary": f"feedback:{normalized_situation_type}",
            "user_response": body.correction_text,
            "response_reasoning": "takeover_feedback",
            "outcome": body.result,
            "outcome_sentiment": "negative" if feedback_type == "unhelpful" else "neutral",
            "confidence": max(0.1, min(1.0, state.autonomy_score)),
        }
        try:
            feedback_observation_ids.append(save_observation(db, synthetic_observation))
        except Exception:
            logger.warning("failed to save synthetic observation from takeover feedback", exc_info=True)

    if feedback_type in {"helpful", "unhelpful", "neutral"} or body.correction_text:
        fp_row = db.execute(
            text(
                """
                SELECT fingerprint, observation_count
                FROM behavioral_fingerprints
                WHERE consumer_id = :consumer_id AND workspace_id = :workspace_id
                """
            ),
            {"consumer_id": auth.behavior_subject_id, "workspace_id": auth.workspace_id},
        ).fetchone()
        if fp_row:
            fingerprint = fp_row[0] if isinstance(fp_row[0], dict) else DEFAULT_FINGERPRINT.copy()
            observation_count = int(fp_row[1] or 0)
        else:
            fingerprint = DEFAULT_FINGERPRINT.copy()
            observation_count = 0
        adjusted_alpha = feedback_adjusted_alpha(
            settings.feedback_base_alpha,
            feedback_type or None,
            alpha_min=settings.feedback_alpha_min,
            alpha_max=settings.feedback_alpha_max,
        )
        feedback_details.setdefault("feedback_alpha", adjusted_alpha)
        if body.correction_text or feedback_type in {"unhelpful", "negative", "wrong"}:
            fingerprint = apply_feedback_to_fingerprint(
                fingerprint,
                feedback_type=feedback_type or "unhelpful",
                correction_text=body.correction_text,
                base_alpha=settings.feedback_base_alpha,
                alpha_min=settings.feedback_alpha_min,
                alpha_max=settings.feedback_alpha_max,
            )
            save_fingerprint(
                db,
                consumer_id=auth.behavior_subject_id,
                workspace_id=auth.workspace_id,
                fingerprint=fingerprint,
                observation_count=max(observation_count, 1),
            )

    if feedback_observation_ids and feedback_type:
        for observation_id in feedback_observation_ids:
            try:
                save_clone_feedback(
                    db,
                    observation_id=observation_id,
                    session_id=body.session_id,
                    feedback_type=feedback_type,
                    correction_text=body.correction_text,
                )
            except Exception:
                logger.warning("failed to persist clone feedback trace", exc_info=True)

    state.recent_outcomes_json = updated_outcomes
    state.autonomy_score = updated_autonomy
    state.updated_at = datetime.now(tz=UTC)
    save_takeover_state(db, state)
    _invalidate_goal_queue_cache(db, auth=auth, state=state)
    _record_takeover_action_async(
        session_id=state.session_id,
        workspace_id=state.workspace_id,
        user_id=state.user_id,
        turn=body.turn,
        objective_hash_value=body.objective_hash or state.objective_hash,
        action_kind=body.action_kind,
        result=body.result,
        latency_ms=body.latency_ms,
        meta=feedback_details,
    )
    return TakeoverFeedbackResponse(
        session_id=state.session_id,
        autonomy_score=state.autonomy_score,
        recent_outcomes_json=state.recent_outcomes_json,
        updated_at=state.updated_at,
    )


@app.post("/v1/takeover/step", response_model=TakeoverStepResponse)
def takeover_step(
    body: TakeoverStepRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TakeoverStepResponse:
    started_total = time.perf_counter()
    REQUEST_COUNT.labels(endpoint="takeover_step", method="POST").inc()
    _enforce_workspace_access(auth, db)
    state_started = time.perf_counter()
    state = load_takeover_state(
        db=db,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=body.persona_mode,
        activation_keywords=body.activation_keywords,
        stop_keywords=body.stop_keywords,
    )
    state_ms = int((time.perf_counter() - state_started) * 1000)
    snapshot_rehydrated = False
    snapshot_age_hours: int | None = None

    now = datetime.now(tz=UTC)
    if state.expires_at and state.expires_at <= now:
        state.active = False
        state.expires_at = None
    try:
        state.autonomy_policy_profile = AutonomyPolicyProfile(
            str(
                getattr(state, "autonomy_policy_profile", None)
                or settings.takeover_autonomy_policy_default
            )
        )
    except Exception:
        state.autonomy_policy_profile = AutonomyPolicyProfile.HUMAN_CONSULTATIVE
    continuity_violations, continuity_ok = continuity_health(
        was_active=bool(state.active),
        last_message_at=state.last_message_at,
        now=now,
        previous_violations=int(getattr(state, "continuity_violation_count", 0) or 0),
        max_gap_seconds=int(settings.takeover_continuity_gap_seconds),
    )
    state.continuity_violation_count = continuity_violations
    if not continuity_ok and state.mode == TakeoverMode.TAKEOVER:
        state.mode = TakeoverMode.SUGGEST

    normalized_message = normalize_text(body.message)
    stop_hit = contains_phrase(normalized_message, state.stop_keywords)
    if stop_hit:
        state.active = False
        state.last_message_at = now
        state.expires_at = None
        state.updated_at = now
        _invalidate_goal_queue_cache(db, auth=auth, state=state)
        db.execute(
            text(
                """
                DELETE FROM directive_executions
                WHERE session_id = :session_id
                  AND workspace_id = :workspace_id
                  AND user_id = :user_id
                """
            ),
            {"session_id": state.session_id, "workspace_id": state.workspace_id, "user_id": state.user_id},
        )
        db.execute(
            text(
                """
                UPDATE autonomy_notices
                SET acknowledged_at = :ack
                WHERE session_id = :session_id
                  AND workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND acknowledged_at IS NULL
                """
            ),
            {
                "ack": now,
                "session_id": state.session_id,
                "workspace_id": state.workspace_id,
                "user_id": state.user_id,
            },
        )
        state.pending_directive_count = 0
        state.retry_backlog_count = 0
        save_session_memory_snapshot(
            db,
            state=state,
            reason="stand_down",
            max_per_session=max(1, int(getattr(settings, "snapshot_max_per_session", 10))),
        )
        save_takeover_state(db, state)
        session_turns = state.takeover_context.get("session_turns", [])
        if isinstance(session_turns, list) and session_turns:
            try:
                for turn in session_turns:
                    if not isinstance(turn, dict):
                        continue
                    obs = {
                        "consumer_id": auth.consumer,
                        "workspace_id": auth.workspace_id,
                        "situation_type": "routine_task",
                        "situation_summary": str(turn.get("message_preview", ""))[:500],
                        "user_response": str(turn.get("message_preview", "")),
                        "outcome_sentiment": "neutral",
                        "confidence": 0.6,
                    }
                    save_observation(db, obs)
            except Exception:
                logger.warning("Auto-ingestion on session stop failed", exc_info=True)
        response = TakeoverStepResponse(
            state=state,
            action="stopped",
            classification=TakeoverClassification.DECISIVE,
            enforced=False,
            safety_decision=SafetyDecision.ALLOW,
            final_response=None,
            note="takeover stopped",
            decision_confidence=0.0,
            decision_source=TakeoverDecisionSource.FAST_PATH,
            next_action=TakeoverNextAction(kind="stop", target="", rationale="stand-down requested"),
            latency_breakdown_ms=TakeoverLatencyBreakdown(state=state_ms, total=int((time.perf_counter() - started_total) * 1000)),
            needs_human=False,
            continuity_ok=continuity_ok,
        )
        write_audit_log(
            db,
            consumer=auth.consumer,
            action="takeover_step",
            query=body.model_dump(mode="json"),
            result_event_ids=[],
            policy_decisions={
                "mode": state.mode.value,
                "classification": response.classification.value,
                "safety_decision": response.safety_decision.value,
                "enforced": response.enforced,
            },
            latency_ms=response.latency_breakdown_ms.total,
        )
        return response

    activation_hit = contains_phrase(normalized_message, state.activation_keywords)
    override_mode = mode_override(state.persona_mode, normalized_message)
    default_activation, _ = persona_defaults(state.persona_mode)
    custom_activation = bool(body.activation_keywords) and normalize_text(body.activation_keywords) != normalize_text(
        default_activation
    )
    resolved_policy = _resolved_takeover_policy(body.policy)
    if activation_hit:
        state.active = True
        state.activated_at = now
        if override_mode is not None:
            state.mode = override_mode
        elif custom_activation:
            state.mode = body.activation_mode_default if body.activation_mode_default else state.mode
            if state.mode.value != "takeover":
                state.mode = TakeoverMode.TAKEOVER
        else:
            state.mode = override_mode or body.activation_mode_default
        state.expires_at = datetime.fromisoformat(next_expiry(resolved_policy.timeout_minutes))
    elif override_mode is not None:
        state.active = True
        state.mode = override_mode
        state.expires_at = datetime.fromisoformat(next_expiry(resolved_policy.timeout_minutes))
    if (not state.active) and body.activation_mode_default and body.activation_mode_default == TakeoverMode.TAKEOVER:
        state.active = True
        state.mode = TakeoverMode.TAKEOVER
        state.activated_at = now
        state.expires_at = datetime.fromisoformat(next_expiry(resolved_policy.timeout_minutes))

    if not state.active:
        state.last_message_at = now
        state.updated_at = now
        save_takeover_state(db, state)
        response = TakeoverStepResponse(
            state=state,
            action="inactive",
            classification=TakeoverClassification.EMPTY,
            enforced=False,
            safety_decision=SafetyDecision.ALLOW,
            final_response=None,
            decision_confidence=0.0,
            decision_source=TakeoverDecisionSource.FAST_PATH,
            next_action=TakeoverNextAction(kind="inactive", target="", rationale="takeover inactive"),
            latency_breakdown_ms=TakeoverLatencyBreakdown(state=state_ms, total=int((time.perf_counter() - started_total) * 1000)),
            needs_human=False,
            continuity_ok=continuity_ok,
        )
        write_audit_log(
            db,
            consumer=auth.consumer,
            action="takeover_step",
            query=body.model_dump(mode="json"),
            result_event_ids=[],
            policy_decisions={
                "mode": state.mode.value,
                "classification": response.classification.value,
                "safety_decision": response.safety_decision.value,
                "enforced": response.enforced,
            },
            latency_ms=response.latency_breakdown_ms.total,
        )
        return response

    classify_started = time.perf_counter()
    state.last_message_at = now
    state.updated_at = now
    state.expires_at = datetime.fromisoformat(next_expiry(resolved_policy.timeout_minutes))
    if body.takeover_context:
        merged_context = dict(state.takeover_context)
        merged_context.update(body.takeover_context)
        state.takeover_context = merged_context
    state.takeover_context["last_user_message"] = body.message[:280]
    state.takeover_context["turn_count"] = int(state.takeover_context.get("turn_count", 0)) + 1
    session_turns = list(state.takeover_context.get("session_turns", []))
    turn_record = {
        "turn": state.takeover_context["turn_count"],
        "message_preview": body.message[:200],
        "timestamp": now.isoformat(),
    }
    if body.executor_output:
        turn_record["executor_output_preview"] = body.executor_output[:200]
    session_turns.append(turn_record)
    state.takeover_context["session_turns"] = session_turns[-20:]
    turn_count = int(state.takeover_context.get("turn_count", 0))
    awaiting_next_objective = bool(state.takeover_context.get("awaiting_next_objective"))
    has_new_objective_signal = bool(str(body.task or "").strip()) or _has_explicit_objective_signal(normalized_message)
    if awaiting_next_objective and has_new_objective_signal:
        state.takeover_context.pop("awaiting_next_objective", None)
        state.takeover_context.pop("awaiting_next_objective_turns", None)
        awaiting_next_objective = False
        state.takeover_context.pop("force_user_objective_refresh", None)
        state.takeover_context.pop("objective_needs_refresh", None)
    elif awaiting_next_objective:
        state.takeover_context["awaiting_next_objective_turns"] = int(
            state.takeover_context.get("awaiting_next_objective_turns", 0)
        ) + 1
    resolved_task = resolve_objective(
        message=body.message,
        task=body.task,
        takeover_context=state.takeover_context,
    )
    pending_execution = _load_pending_directive(db, state=state)
    if pending_execution is not None:
        pending_objective = str((pending_execution.meta or {}).get("objective") or "").strip()
        if pending_objective:
            resolved_task = pending_objective
            state.takeover_context["objective"] = pending_objective
            state.objective_hash = pending_execution.objective_hash or objective_hash(pending_objective)
            state.takeover_context["_pending_directive_locked"] = True
        retry_feedback_payload = (pending_execution.meta or {}).get("retry_feedback") if isinstance(pending_execution.meta, dict) else None
        if bool(getattr(settings, "retry_feedback_enabled", False)) and isinstance(retry_feedback_payload, dict):
            state.takeover_context["retry_feedback"] = retry_feedback_payload
        else:
            state.takeover_context.pop("retry_feedback", None)
    if pending_execution is not None and awaiting_next_objective:
        state.takeover_context.pop("awaiting_next_objective", None)
        state.takeover_context.pop("awaiting_next_objective_turns", None)
        awaiting_next_objective = False
    pinned_objective = str(state.takeover_context.get("pinned_user_objective") or "").strip()
    pinned_active = bool(pinned_objective)
    pin_should_update = bool(
        str(body.task or "").strip()
        or ("new objective" in normalized_message)
        or ("next objective" in normalized_message)
        or (not pinned_objective)
    )
    if has_new_objective_signal and pin_should_update and not pending_execution:
        pinned_objective = str(resolved_task or "").strip()
        if pinned_objective:
            state.takeover_context["pinned_user_objective"] = pinned_objective
            state.takeover_context["pinned_user_objective_hash"] = objective_hash(pinned_objective)
            state.takeover_context["pinned_user_objective_turn"] = turn_count
            state.takeover_context["pinned_user_objective_until_turn"] = turn_count + 200
            pinned_active = True
    if pinned_active and not pending_execution and not body.task:
        resolved_task = pinned_objective
    objective_hash_value = objective_hash(resolved_task)
    objective_changed = objective_hash_value != (state.objective_hash or "")
    selected_goal: TakeoverGoal | None = _load_active_goal(db, state)
    goal_discovery_every_n_turns = max(6, int(getattr(settings, "takeover_goal_discovery_every_n_turns", 24)))
    periodic_goal_discovery_due = turn_count > 0 and (turn_count % goal_discovery_every_n_turns == 0)
    should_discover = bool(
        state.active
        and (not awaiting_next_objective or has_new_objective_signal)
        and (
            selected_goal is None
            or objective_changed
            or periodic_goal_discovery_due
            or recent_failure_count(state.recent_outcomes_json) >= 2
            or any(token in normalized_message for token in ("explore", "research", "what next"))
        )
    )
    if should_discover:
        discovered_goals = _discover_takeover_goals(
            db,
            auth=auth,
            state=state,
            include_open_discovery=settings.takeover_goal_source == "open_discovery",
        )
        if selected_goal is None and discovered_goals:
            chosen_goal = discovered_goals[0]
            if pinned_active and pinned_objective:
                pinned_match = None
                best_overlap = 0.0
                for candidate in discovered_goals:
                    overlap = _objective_overlap_score(
                        pinned_objective,
                        f"{candidate.title} {candidate.description}",
                    )
                    if overlap > best_overlap:
                        best_overlap = overlap
                        pinned_match = candidate
                if pinned_match is not None and best_overlap >= 0.28:
                    chosen_goal = pinned_match
                else:
                    chosen_goal = None
                    state.takeover_context["force_user_objective_refresh"] = True
            if chosen_goal is not None and chosen_goal.confidence >= float(settings.takeover_goal_selection_min_confidence):
                state.active_goal_id = chosen_goal.id
                selected_goal = chosen_goal
    if selected_goal is not None:
        state.takeover_context["selected_goal_title"] = str(selected_goal.title or "").strip()
        resolved_norm = normalize_text(resolved_task)
        if (
            not resolved_norm
            or resolved_norm in {"current objective", "continue active objective", "follow latest concrete objective"}
        ):
            resolved_task = str(selected_goal.description or selected_goal.title or resolved_task)
    if selected_goal is not None and not body.task and not awaiting_next_objective and not pinned_active:
        resolved_task = selected_goal.description or selected_goal.title
    state.takeover_context["objective"] = resolved_task
    state.objective_hash = objective_hash(resolved_task)
    classifier_input = body.executor_output if (body.executor_output or "").strip() else body.message
    classifier_for_conf = classify_text(
        classifier_input,
        takeover_active=state.mode == TakeoverMode.TAKEOVER,
        semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
        semantic_threshold=float(getattr(settings, "semantic_classifier_intent_threshold", 0.67)),
        semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
    )
    classify_ms = int((time.perf_counter() - classify_started) * 1000)

    retrieval_started = time.perf_counter()
    working_set = state.working_set_json if isinstance(state.working_set_json, dict) else {}
    if (
        bool(getattr(settings, "snapshot_rehydrate_enabled", True))
        and objective_hash_value
        and (objective_changed or not working_set)
    ):
        snapshot_record = load_recent_session_memory_snapshot(
            db,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=state.session_id,
            objective_hash=objective_hash_value,
            max_age_days=max(1, int(getattr(settings, "snapshot_max_age_days", 14))),
        )
        if snapshot_record is not None:
            payload = snapshot_record.get("payload", {})
            snapshot_working_set = payload.get("working_set_json") if isinstance(payload, dict) else None
            if isinstance(snapshot_working_set, dict) and snapshot_working_set:
                working_set = dict(snapshot_working_set)
                working_set["rehydrated_from_snapshot_id"] = snapshot_record.get("snapshot_id")
                working_set["rehydrated_at"] = now.isoformat()
                state.working_set_json = working_set
                snapshot_rehydrated = True
                snapshot_age_hours = int(snapshot_record.get("age_hours") or 0)
                state.takeover_context["snapshot_rehydrated"] = {
                    "snapshot_id": snapshot_record.get("snapshot_id"),
                    "age_hours": snapshot_age_hours,
                    "objective_hash": objective_hash_value,
                    "at": now.isoformat(),
                }
    refresh_every_n_turns = max(6, int(getattr(settings, "takeover_working_set_refresh_every_n_turns", 24)))
    periodic_working_set_refresh_due = turn_count > 0 and (turn_count % refresh_every_n_turns == 0)
    refresh_working_set = (
        objective_changed or (not working_set) or periodic_working_set_refresh_due
    ) and not snapshot_rehydrated
    if refresh_working_set:
        working_set, _ = _build_takeover_working_set(
            db,
            auth,
            task=resolved_task,
            app_context=body.app_context,
            constraints=body.constraints,
        )
        state.working_set_json = working_set
    retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)

    decision_confidence, confidence_components = compute_decision_confidence(
        objective=resolved_task,
        message=body.message,
        classification=classifier_for_conf,
        working_set=working_set,
        recent_outcomes=state.recent_outcomes_json,
    )
    TAKEOVER_CONFIDENCE.observe(decision_confidence)

    recent_failures = recent_failure_count(state.recent_outcomes_json)
    run_deliberation = should_trigger_deliberation(
        decision_confidence=decision_confidence,
        objective_changed=objective_changed,
        turn_count=turn_count,
        recent_failures=recent_failures,
        message=body.message,
    )
    evidence_count = int(working_set.get("evidence_count", 0) or 0)
    evidence_strength_score = max(0.0, min(1.0, evidence_count / 6.0))
    recency_coverage_score = 1.0 if evidence_count > 0 else 0.2
    outcome_stability_score = max(0.0, 1.0 - min(1.0, recent_failures / 3.0))
    context_quality_score = round(
        (0.40 * float(decision_confidence))
        + (0.25 * evidence_strength_score)
        + (0.20 * recency_coverage_score)
        + (0.15 * outcome_stability_score),
        4,
    )
    profile_tuning = (
        autonomy_profile_tuning(state.autonomy_policy_profile)
        if bool(getattr(settings, "profile_tuning_enabled", False))
        else autonomy_profile_tuning("human_consultative")
    )
    trigger_threshold = float(settings.context_retrieval_trigger_score) + float(
        profile_tuning.get("retrieval_trigger_delta", 0.0)
    )
    trigger_threshold = max(0.35, min(0.92, trigger_threshold))
    confidence_trigger = float(getattr(settings, "takeover_retrieval_confidence_trigger", 0.70))
    evidence_threshold = int(getattr(settings, "takeover_retrieval_low_evidence_threshold", 2)) + int(
        profile_tuning.get("evidence_floor_delta", 0)
    )
    evidence_threshold = max(1, evidence_threshold)
    min_turn_for_evidence = int(getattr(settings, "takeover_retrieval_min_turn_for_evidence_gate", 4))
    cooldown_turns = max(0, int(getattr(settings, "takeover_retrieval_cooldown_turns", 2)))
    deep_intent = any(token in normalized_message for token in ("research", "deep", "explore", "investigate"))
    forced_trigger = deep_intent or (recent_failures >= 2)
    quality_gate = context_quality_score < trigger_threshold
    confidence_gate = decision_confidence < confidence_trigger
    evidence_gate = turn_count >= min_turn_for_evidence and evidence_count < evidence_threshold
    last_trigger_turn_raw = state.takeover_context.get("last_retrieval_trigger_turn")
    try:
        last_trigger_turn = int(last_trigger_turn_raw) if last_trigger_turn_raw is not None else -10_000
    except Exception:
        last_trigger_turn = -10_000
    cooldown_gate = True if cooldown_turns <= 0 else ((turn_count - last_trigger_turn) >= cooldown_turns)
    retrieval_reason: str | None = None
    if forced_trigger:
        retrieval_triggered = True
        retrieval_reason = "explicit_deep_intent" if deep_intent else "recent_failures"
    else:
        gated_trigger = quality_gate or (confidence_gate and evidence_gate)
        if gated_trigger and cooldown_gate:
            retrieval_triggered = True
            if quality_gate:
                retrieval_reason = "low_context_quality"
            elif confidence_gate and evidence_gate:
                retrieval_reason = "low_confidence"
            elif evidence_gate:
                retrieval_reason = "low_evidence"
        elif gated_trigger and not cooldown_gate:
            retrieval_triggered = False
            retrieval_reason = "cooldown_suppressed"
        else:
            retrieval_triggered = False
            retrieval_reason = None
    if retrieval_triggered:
        state.takeover_context["last_retrieval_trigger_turn"] = turn_count
    retrieval_source = "none"

    auto_handoff: dict[str, Any] = {}
    handoff_source_text = body.executor_output if (body.executor_output or "").strip() else body.message
    if (
        state.mode.value == "suggest"
        and resolved_policy.auto_handoff_on_question
        and classify_text(
            handoff_source_text,
            semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
            semantic_threshold=float(getattr(settings, "semantic_classifier_intent_threshold", 0.67)),
            semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
        )
        in {TakeoverClassification.QUESTION, TakeoverClassification.HANDOFF}
    ):
        state.mode = TakeoverMode.TAKEOVER
        auto_handoff = {
            "trigger": "executor_output_question_or_handoff",
            "mode": "takeover",
            "note": "auto-promoted from suggest to takeover",
        }

    decision_source = TakeoverDecisionSource.FAST_PATH
    citation_values: list[UUID] = []
    for value in working_set.get("citations", []):
        if not isinstance(value, str):
            continue
        try:
            citation_values.append(UUID(value))
        except Exception:
            continue
    clone_payload: dict[str, Any] = {
        "guidance_summary": str(working_set.get("summary", "")),
        "do": working_set.get("do", []),
        "dont": working_set.get("dont", []),
        "confidence": decision_confidence,
        "evidence_strength": (
            "strong"
            if int(working_set.get("evidence_count", 0) or 0) >= 5
            else "medium"
            if int(working_set.get("evidence_count", 0) or 0) >= 2
            else "weak"
        ),
        "citations": citation_values,
        "citation_snippets": (
            working_set.get("citation_snippets", [])
            if isinstance(working_set.get("citation_snippets"), list)
            else []
        ),
        "confidence_components": confidence_components,
    }
    guidance_text = str(working_set.get("summary", "")).strip()
    deliberation_ms = 0
    citations: list[UUID] = citation_values
    fast_path_guidance = guidance_text
    fast_path_payload = dict(clone_payload)
    fast_path_citations = list(citations)
    advisor_fail_streak = max(0, int(state.takeover_context.get("advisor_fail_streak", 0) or 0))
    advisor_backoff_turns = max(1, int(getattr(settings, "takeover_advisor_backoff_turns", 2)))
    advisor_runtime_cooloff_turns = max(
        advisor_backoff_turns,
        int(getattr(settings, "takeover_advisor_runtime_cooloff_turns", 64)),
    )
    last_advisor_failure_turn_raw = state.takeover_context.get("advisor_last_failure_turn")
    try:
        last_advisor_failure_turn = (
            int(last_advisor_failure_turn_raw) if last_advisor_failure_turn_raw is not None else -10_000
        )
    except Exception:
        last_advisor_failure_turn = -10_000
    runtime_cooloff_until_raw = state.takeover_context.get("advisor_runtime_cooloff_until_turn")
    try:
        runtime_cooloff_until_turn = (
            int(runtime_cooloff_until_raw) if runtime_cooloff_until_raw is not None else -10_000
        )
    except Exception:
        runtime_cooloff_until_turn = -10_000
    advisor_runtime_cooloff_active = bool(turn_count < runtime_cooloff_until_turn)
    advisor_backoff_active = bool(
        advisor_fail_streak > 0 and (turn_count - last_advisor_failure_turn) < advisor_backoff_turns
    )
    advisor_backoff_active = bool(advisor_backoff_active or advisor_runtime_cooloff_active)
    advisor_failure_reason: str | None = None
    fast_path_reason = "fast_path_default"
    advisor_required_in_takeover = bool(state.mode == TakeoverMode.TAKEOVER and settings.clone_reasoning_enabled)
    advisor_cadence_turns = max(
        1,
        int(profile_tuning.get("advisor_cadence_turns", getattr(settings, "takeover_advisor_every_n_turns", 2))),
    )
    advisor_cadence_due = turn_count <= 2 or (turn_count % advisor_cadence_turns == 0)
    stable_context_threshold = max(trigger_threshold + 0.08, 0.82)
    stable_evidence_floor = max(2, evidence_threshold)
    high_confidence_context = bool(
        context_quality_score >= stable_context_threshold
        and evidence_count >= stable_evidence_floor
        and decision_confidence >= max(confidence_trigger - 0.04, 0.66)
    )
    if advisor_required_in_takeover and advisor_cadence_due and high_confidence_context and not retrieval_triggered:
        advisor_cadence_due = False
        if run_deliberation and retrieval_reason != "explicit_deep_intent":
            run_deliberation = False
    should_call_clone_advice = bool(
        (
            advisor_required_in_takeover
            and (
                run_deliberation
                or retrieval_triggered
                or advisor_cadence_due
                or retrieval_reason == "explicit_deep_intent"
            )
        )
        or (
            (not advisor_required_in_takeover)
            and run_deliberation
            and (
                decision_confidence < confidence_trigger
                or settings.advisor_router_v2_enabled
                or retrieval_reason == "explicit_deep_intent"
            )
        )
    )
    if advisor_backoff_active:
        should_call_clone_advice = False
        fast_path_reason = "advisor_runtime_cooloff" if advisor_runtime_cooloff_active else "advisor_backoff"
    remaining_budget_ms = max(0, int(getattr(settings, "context_retrieval_budget_ms", 120)) - retrieval_ms)
    backend_budget_ms = max(1, int(getattr(settings, "context_backend_timeout_ms", 60)))
    budget_guard_enabled = bool(getattr(settings, "takeover_retrieval_budget_guard_enabled", True))
    budget_exceeded = bool(
        budget_guard_enabled and retrieval_triggered and should_call_clone_advice and (remaining_budget_ms < backend_budget_ms)
    )
    retrieval_source_override: str | None = None
    if budget_exceeded:
        should_call_clone_advice = False
        retrieval_source_override = "hybrid_fallback"
        retrieval_reason = "budget_exceeded_skip_deliberation"
        fast_path_reason = "budget_exceeded_skip_deliberation"
        TAKEOVER_RETRIEVAL_BUDGET_EXCEEDED_COUNT.inc()
    elif not should_call_clone_advice:
        if advisor_required_in_takeover and not advisor_cadence_due and not run_deliberation and not retrieval_triggered:
            fast_path_reason = "advisor_cadence_skip"
        elif run_deliberation and decision_confidence >= confidence_trigger:
            fast_path_reason = "confidence_above_trigger"
        elif not run_deliberation:
            fast_path_reason = "run_deliberation_false"
    if should_call_clone_advice:
        advisor_call_succeeded = False
        TAKEOVER_DELIBERATION_COUNT.labels(trigger="confidence_or_refresh").inc()
        deliberation_started = time.perf_counter()
        try:
            advice_response = clone_advice(
                body=CloneAdviceRequest(
                    task=resolved_task,
                    app_context=body.app_context,
                    constraints=body.constraints,
                    takeover_context=state.takeover_context,
                    message_delta=body.message_delta,
                    executor_output=body.executor_output,
                    interaction_id=body.interaction_id,
                    allow_fallback=(body.allow_fallback if not advisor_required_in_takeover else False),
                ),
                auth=auth,
                db=db,
            )
            clone_payload = advice_response.model_dump(mode="json")
            guidance_text = advice_response.guidance_summary
            citations = advice_response.citations
            state.last_deliberation_at = now
            citation_limit = max(1, int(getattr(settings, "takeover_citation_snippet_max_items", 20)))
            citation_snippet_chars = max(32, int(getattr(settings, "takeover_citation_snippet_max_chars", 100)))
            refreshed_citation_ids = list(advice_response.citations[:citation_limit])
            refreshed_citation_snippets = (
                _build_citation_snippets(
                    db,
                    citation_ids=refreshed_citation_ids,
                    max_items=citation_limit,
                    snippet_chars=citation_snippet_chars,
                )
                if bool(getattr(settings, "takeover_rationale_enrichment_enabled", True))
                and bool(getattr(settings, "takeover_citation_snippets_enabled", True))
                else []
            )
            state.working_set_json = {
                **working_set,
                "summary": advice_response.guidance_summary,
                "do": advice_response.do[:5],
                "dont": advice_response.dont[:5],
                "evidence_count": len(advice_response.citations),
                "citations": [str(v) for v in refreshed_citation_ids],
                "citation_snippets": refreshed_citation_snippets,
                "top_patterns": [
                    {
                        "statement": item,
                        "confidence": float(advice_response.confidence),
                        "pattern_type": "workflow",
                    }
                    for item in advice_response.recommended_actions[:5]
                ],
                "refreshed_at": now.isoformat(),
            }
            advisor_runtime_used = False
            if isinstance(clone_payload, dict):
                conflict_flags = clone_payload.get("conflict_flags")
                if isinstance(conflict_flags, list) and "advisor_runtime_llm" in conflict_flags:
                    advisor_runtime_used = True
            if advisor_required_in_takeover and not advisor_runtime_used:
                advisor_call_succeeded = False
                if not advisor_failure_reason:
                    advisor_failure_reason = "advisor_runtime_unavailable"
                decision_source = TakeoverDecisionSource.FAST_PATH
                fast_path_reason = "advisor_runtime_unavailable"
            else:
                decision_source = TakeoverDecisionSource.DELIBERATION
                advisor_call_succeeded = True
                fast_path_reason = ""
        except Exception as exc:
            advisor_failure_reason = str(exc).strip()[:220] or exc.__class__.__name__
            fast_path_reason = "advisor_exception"
            logger.warning("takeover deliberation fallback to fast-path", exc_info=True)
        deliberation_ms = int((time.perf_counter() - deliberation_started) * 1000)
        deliberation_timeout_ms = max(
            600,
            int(getattr(settings, "effective_advisor_attempt_timeout_ms", settings.advisor_attempt_timeout_ms)),
        )
        if deliberation_ms > deliberation_timeout_ms:
            TAKEOVER_DELIBERATION_COUNT.labels(trigger="timeout_fallback").inc()
            guidance_text = fast_path_guidance
            clone_payload = fast_path_payload
            citations = fast_path_citations
            decision_source = TakeoverDecisionSource.FAST_PATH
            advisor_call_succeeded = False
            if not advisor_failure_reason:
                advisor_failure_reason = f"advisor_timeout_{deliberation_ms}ms"
            fast_path_reason = "advisor_timeout"
        if advisor_call_succeeded:
            advisor_fail_streak = 0
            state.takeover_context["advisor_fail_streak"] = 0
            state.takeover_context.pop("advisor_last_error", None)
            state.takeover_context.pop("advisor_last_failure_turn", None)
            state.takeover_context.pop("advisor_runtime_cooloff_until_turn", None)
        else:
            advisor_fail_streak += 1
            state.takeover_context["advisor_fail_streak"] = advisor_fail_streak
            state.takeover_context["advisor_last_failure_turn"] = turn_count
            if advisor_failure_reason:
                state.takeover_context["advisor_last_error"] = advisor_failure_reason
                normalized_failure = advisor_failure_reason.lower()
                if normalized_failure.startswith("advisor_timeout_") or "runtime_unavailable" in normalized_failure:
                    state.takeover_context["advisor_runtime_cooloff_until_turn"] = (
                        turn_count + advisor_runtime_cooloff_turns
                    )
                if not fast_path_reason:
                    fast_path_reason = "advisor_error"
        TAKEOVER_DELIBERATION_MS.observe(deliberation_ms / 1000)
    if decision_source == TakeoverDecisionSource.DELIBERATION:
        fast_path_reason = ""
    clone_payload["fast_path_reason"] = fast_path_reason or None
    clone_payload["advisor_failure_reason"] = advisor_failure_reason
    state.takeover_context["last_fast_path_reason"] = fast_path_reason or None
    policy_retrieval_meta = {}
    if isinstance(working_set.get("policy"), dict):
        policy_retrieval_meta = working_set["policy"].get("retrieval", {}) or {}
    if retrieval_triggered:
        raw_source = str(policy_retrieval_meta.get("source") or "").strip()
        if raw_source in {"pgvector_ann", "lexical_only", "hybrid_fallback", "qdrant"}:
            retrieval_source = raw_source
        elif decision_source == TakeoverDecisionSource.DELIBERATION:
            retrieval_source = "hybrid_fallback"
        else:
            retrieval_source = "pgvector_ann" if evidence_count > 0 else "lexical_only"
    if retrieval_source_override is not None:
        retrieval_source = retrieval_source_override
    feedback_adjustment_applied = bool(policy_retrieval_meta.get("feedback_adjustment_applied", False))
    query_expansion_used_meta = bool(policy_retrieval_meta.get("query_expansion_used", False))
    raw_expansion_terms = policy_retrieval_meta.get("query_expansion_terms", [])
    query_expansion_terms_meta = [
        str(value).strip()
        for value in raw_expansion_terms
        if isinstance(value, str) and str(value).strip()
    ][: max(0, int(getattr(settings, "search_query_expansion_max_terms", 4)))]
    rerank_strategy_meta = str(policy_retrieval_meta.get("rerank_strategy") or "none")
    retrieval_latency_ms = retrieval_ms + deliberation_ms
    retrieval_hit_count = evidence_count
    if retrieval_triggered and retrieval_reason:
        TAKEOVER_RETRIEVAL_TRIGGER_COUNT.labels(reason=retrieval_reason).inc()
    TAKEOVER_RETRIEVAL_SOURCE_COUNT.labels(source=retrieval_source).inc()
    TAKEOVER_RETRIEVAL_LATENCY.labels(source=retrieval_source).observe(
        max(0.0, retrieval_latency_ms / 1000.0)
    )
    objective_for_plan = str(state.takeover_context.get("objective") or resolved_task or "")
    if objective_for_plan and state.mode == TakeoverMode.TAKEOVER:
        _ensure_objective_contract_context(
            state.takeover_context,
            objective=objective_for_plan,
            updated_at=now,
        )
    if _is_complex_objective_text(objective_for_plan):
        learned_step_templates = _load_learned_workflow_step_templates(
            db,
            workspace_id=auth.workspace_id,
            limit=3,
            min_reliability=float(getattr(settings, "workflow_template_reuse_min_reliability", 0.70)),
        )
        suggested_steps = [
            str(item).strip()
            for item in clone_payload.get("do", [])
            if isinstance(item, str) and str(item).strip()
        ]
        structured_plan = _build_structured_plan_payload(
            objective=objective_for_plan,
            suggested_steps=suggested_steps,
            learned_templates=learned_step_templates,
        )
        clone_payload["structured_plan"] = structured_plan
        state.takeover_context["structured_plan"] = structured_plan
    contract_payload = state.takeover_context.get("objective_contract")
    if isinstance(contract_payload, dict):
        clone_payload["objective_contract"] = contract_payload

    final_response, enforced, enforcement_reason, classification = ensure_takeover_response(
        mode=state.mode,
        text=guidance_text,
        task=resolved_task,
        takeover_context=state.takeover_context,
        advice=clone_payload,
        semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
        semantic_threshold=float(getattr(settings, "semantic_classifier_intent_threshold", 0.67)),
        semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
    )
    if not final_response and state.active:
        final_response = build_decisive_response(
            task=resolved_task,
            takeover_context=state.takeover_context,
            advice=clone_payload,
        )
        enforced = True
        enforcement_reason = "empty_fallback_decisive"
    elif not final_response:
        final_response = None

    safety_started = time.perf_counter()
    safety_decision, safety_reason = evaluate_safety(
        policy=resolved_policy,
        message=body.message,
        final_response=final_response,
        takeover_context=state.takeover_context,
    )
    safety_ms = int((time.perf_counter() - safety_started) * 1000)
    if safety_decision != SafetyDecision.ALLOW:
        decision_source = TakeoverDecisionSource.SAFETY_GATE

    takeover_enforcement: dict[str, Any] = {}
    if enforced:
        note = "enforced decisive takeover response"
        if enforcement_reason and "weak_evidence" in enforcement_reason:
            note = "weak evidence — enforced explore-and-implement directive"
        elif enforcement_reason and "empty" in enforcement_reason:
            note = "empty advisor output — enforced decisive fallback"
        takeover_enforcement = {
            "trigger": enforcement_reason,
            "mode": state.mode.value,
            "note": note,
        }
    if safety_decision == SafetyDecision.CONFIRM_REQUIRED:
        state.takeover_context["pending_safety"] = {
            "reason": safety_reason or "high-risk-action",
            "pending_response": final_response,
        }
        final_response = (
            f"Safety pause: high-risk action detected ({safety_reason or 'high-risk'}). "
            f"Type '{resolved_policy.confirm_keyword}' to continue or '{resolved_policy.deny_keyword}' to abort."
        )
    elif safety_decision == SafetyDecision.BLOCKED:
        state.takeover_context.pop("pending_safety", None)
        final_response = "High-risk action aborted by operator decision."
    elif safety_reason == "confirmed_high_risk":
        state.takeover_context.pop("pending_safety", None)

    autonomy_value = max(0.0, min(1.0, float(state.autonomy_score or 0.5)))
    low_threshold = max(0.0, min(1.0, float(settings.takeover_needs_human_threshold_cold)))
    high_threshold = max(0.0, min(1.0, float(settings.takeover_needs_human_threshold_hot)))
    ramp_start = max(0.0, min(1.0, float(settings.takeover_needs_human_ramp_start)))
    ramp_end = max(0.0, min(1.0, float(settings.takeover_needs_human_ramp_end)))
    if ramp_end <= ramp_start:
        needs_human_threshold = high_threshold
    elif autonomy_value <= ramp_start:
        needs_human_threshold = low_threshold
    elif autonomy_value >= ramp_end:
        needs_human_threshold = high_threshold
    else:
        ratio = (autonomy_value - ramp_start) / (ramp_end - ramp_start)
        needs_human_threshold = low_threshold + ((high_threshold - low_threshold) * ratio)
    needs_human_threshold = round(max(0.0, min(1.0, needs_human_threshold)), 4)
    evidence_observations = clone_payload.get("evidence_observations", [])
    semantic_ratio = 0.0
    if isinstance(evidence_observations, list) and evidence_observations:
        semantic_hits = sum(
            1
            for item in evidence_observations
            if isinstance(item, dict) and str(item.get("recall_source", "")).strip().lower() == "semantic"
        )
        semantic_ratio = semantic_hits / max(1, len(evidence_observations))
    if state.autonomy_policy_profile == AutonomyPolicyProfile.HUMAN_CONSULTATIVE:
        needs_human_threshold = adjust_consultative_threshold(
            needs_human_threshold,
            recent_outcomes=state.recent_outcomes_json,
            semantic_ratio=semantic_ratio,
        )
    needs_human_threshold = round(
        max(
            0.0,
            min(1.0, needs_human_threshold + float(profile_tuning.get("needs_human_delta", 0.0))),
        ),
        4,
    )
    suppress_auto_directive = bool(
        (
            awaiting_next_objective
            or bool(state.takeover_context.get("force_user_objective_refresh"))
        )
        and not has_new_objective_signal
        and pending_execution is None
        and safety_decision == SafetyDecision.ALLOW
    )
    if suppress_auto_directive:
        if bool(state.takeover_context.get("force_user_objective_refresh")):
            final_response = "Objective drift detected. Provide one concrete next objective so I can continue correctly."
            state.takeover_context["objective_needs_refresh"] = True
        else:
            final_response = "Previous objective completed. Tell me the next concrete task to continue."
        takeover_enforcement = {}
        enforced = False
        enforcement_reason = None
    execution_permit_required = (
        (not suppress_auto_directive)
        and state.mode == TakeoverMode.TAKEOVER
        and _is_mutating_intent(
            body.message,
            resolved_task,
            final_response,
        )
    )
    execution_permit_id: UUID | None = (
        _find_valid_allow_permit(db, session_id=state.session_id, workspace_id=state.workspace_id)
        if execution_permit_required
        else None
    )
    if execution_permit_required and execution_permit_id is None and safety_decision == SafetyDecision.ALLOW:
        final_response = (
            "Execution permit required for mutating action. "
            "Call tce.request_execution_permit before continuing."
        )
        decision_source = TakeoverDecisionSource.SAFETY_GATE
    dependency_plan_meta = None
    dependency_preflight: dict[str, Any] = {"valid": True, "error": None}
    if isinstance(body.constraints, dict):
        candidate_dependency_plan = body.constraints.get("dependency_plan")
        if isinstance(candidate_dependency_plan, dict):
            dependency_plan_meta = candidate_dependency_plan
            plan_valid, plan_error = _validate_dependency_plan(candidate_dependency_plan)
            dependency_preflight = {"valid": bool(plan_valid), "error": plan_error}
            if not plan_valid and safety_decision == SafetyDecision.ALLOW:
                final_response = (
                    f"Dependency preflight failed: {plan_error}. "
                    "Fix dependency_plan and retry."
                )
                decision_source = TakeoverDecisionSource.SAFETY_GATE
    directive_id: UUID | None = pending_execution.directive_id if pending_execution is not None else None
    directive_state: DirectiveExecutionState | None = pending_execution.state if pending_execution is not None else None
    retry_scheduled = False
    execution_claim_required = False
    if (
        state.mode == TakeoverMode.TAKEOVER
        and final_response
        and safety_decision == SafetyDecision.ALLOW
        and pending_execution is None
        and not suppress_auto_directive
        and bool(dependency_preflight.get("valid", True))
    ):
        directive_id = uuid.uuid4()
        directive_state = DirectiveExecutionState.PENDING
        now_for_directive = datetime.now(tz=UTC)
        claim_expires = now_for_directive + timedelta(seconds=max(30, int(settings.takeover_execution_claim_ttl_seconds)))
        if execution_permit_id is not None:
            permit_expiry_row = db.execute(
                text(
                    """
                    SELECT expires_at
                    FROM execution_permits
                    WHERE id = :permit_id AND session_id = :session_id AND workspace_id = :workspace_id
                    LIMIT 1
                    """
                ),
                {
                    "permit_id": execution_permit_id,
                    "session_id": state.session_id,
                    "workspace_id": state.workspace_id,
                },
            ).first()
            if permit_expiry_row and permit_expiry_row[0]:
                permit_expiry_dt = _coerce_datetime_value(permit_expiry_row[0])
                if permit_expiry_dt < claim_expires:
                    claim_expires = permit_expiry_dt
        db.execute(
            text(
                """
                INSERT INTO directive_executions(
                    directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                    attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                    failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
                )
                VALUES(
                    :directive_id, :session_id, :workspace_id, :user_id, :goal_id, :objective_hash, :action_kind,
                    :attempt, :state, :requires_permit, :permit_id, NULL, NULL, NULL, :expires_at,
                    NULL, NULL, NULL, CAST(:meta AS jsonb), :created_at, :updated_at
                )
                """
            ),
            {
                "directive_id": directive_id,
                "session_id": state.session_id,
                "workspace_id": state.workspace_id,
                "user_id": state.user_id,
                "goal_id": selected_goal.id if selected_goal else None,
                "objective_hash": state.objective_hash,
                "action_kind": "takeover_step",
                "attempt": 1,
                "state": DirectiveExecutionState.PENDING.value,
                "requires_permit": execution_permit_required,
                "permit_id": execution_permit_id,
                "expires_at": claim_expires,
                "meta": json.dumps(
                    {
                        "objective": resolved_task,
                        "final_response": final_response,
                        "verification_required": _required_verification_checks(),
                        "decision_confidence": float(round(decision_confidence, 4)),
                        "context_quality_score": float(round(context_quality_score, 4)),
                        "retrieval_triggered": bool(retrieval_triggered),
                        "dependency_plan": dependency_plan_meta,
                        "dependency_preflight": dependency_preflight,
                        "objective_contract_state": (
                            state.takeover_context.get("objective_contract", {}).get("completion_state")
                            if isinstance(state.takeover_context.get("objective_contract"), dict)
                            else None
                        ),
                    }
                ),
                "created_at": now_for_directive,
                "updated_at": now_for_directive,
            },
        )
        pending_row = db.execute(
            text(
                """
                SELECT directive_id, session_id, workspace_id, user_id, goal_id, objective_hash, action_kind,
                       attempt, state, requires_permit, permit_id, claimed_by, started_at, finished_at, expires_at,
                       failure_class, failure_reason, retry_strategy, meta, created_at, updated_at
                FROM directive_executions
                WHERE directive_id = :directive_id
                LIMIT 1
                """
            ),
            {"directive_id": directive_id},
        ).mappings().first()
        if pending_row is not None:
            pending_execution = _directive_from_row(pending_row)
    if execution_permit_required and execution_permit_id is not None:
        if pending_execution is None:
            execution_claim_required = True
        elif pending_execution.state != DirectiveExecutionState.IN_PROGRESS:
            execution_claim_required = True
            directive_id = pending_execution.directive_id
            directive_state = pending_execution.state
    if execution_claim_required and safety_decision == SafetyDecision.ALLOW:
        final_response = (
            "Execution claim required for this mutating directive. "
            "Call tce.claim_execution and then continue."
        )
        decision_source = TakeoverDecisionSource.SAFETY_GATE
    execution_locked = (
        state.mode == TakeoverMode.TAKEOVER
        and pending_execution is not None
        and directive_state in {DirectiveExecutionState.PENDING, DirectiveExecutionState.IN_PROGRESS}
    )
    if (
        execution_locked
        and safety_decision == SafetyDecision.ALLOW
        and not execution_permit_required
        and not execution_claim_required
    ):
        # Keep takeover deterministic: no natural-response fallback while a
        # directive execution lock is active.
        enforced = True
        if not enforcement_reason:
            enforcement_reason = "pending_execution_lock"
        final_response = (
            "Execution lock active for takeover objective. Continue execution now. "
            "Do not hand off or switch to natural-response mode."
        )
        takeover_enforcement = dict(takeover_enforcement or {})
        takeover_enforcement["pending_execution_lock"] = True
        takeover_enforcement["directive_state"] = (
            directive_state.value if directive_state is not None else None
        )
    actionable_lifecycle_pause = (
        safety_decision == SafetyDecision.ALLOW
        and ((execution_permit_required and execution_permit_id is None) or execution_claim_required)
    )
    advisor_fail_streak_limit = max(1, int(getattr(settings, "takeover_advisor_fail_streak_escalate", 2)))
    advisor_unhealthy = bool(
        state.mode == TakeoverMode.TAKEOVER
        and advisor_required_in_takeover
        and should_call_clone_advice
        and advisor_fail_streak >= advisor_fail_streak_limit
    )
    behavior_fidelity_gate = {"enabled": False, "passed": True, "reason": "gate_disabled"}
    behavior_gate_blocked = False
    if bool(getattr(settings, "behavior_autonomy_gate_enabled", False)):
        behavior_fidelity_gate = {
            "enabled": True,
            **latest_fidelity_gate(
                db,
                workspace_id=auth.workspace_id,
                subject_user_id=auth.behavior_subject_id,
            ),
        }
        behavior_gate_blocked = not bool(behavior_fidelity_gate.get("passed", False))
    needs_human = (
        (decision_confidence < needs_human_threshold)
        or (retrieval_triggered and context_quality_score < float(settings.context_retrieval_escalate_score))
        or advisor_unhealthy
        or behavior_gate_blocked
        or (safety_decision != SafetyDecision.ALLOW)
    ) and not actionable_lifecycle_pause
    if advisor_unhealthy and safety_decision == SafetyDecision.ALLOW and not actionable_lifecycle_pause:
        decision_source = TakeoverDecisionSource.SAFETY_GATE
        if not final_response:
            final_response = (
                "Advisor runtime is unavailable in takeover mode. "
                "Fix advisor route/model connectivity, then continue execution."
            )
    if behavior_gate_blocked and safety_decision == SafetyDecision.ALLOW and not actionable_lifecycle_pause:
        decision_source = TakeoverDecisionSource.SAFETY_GATE
        final_response = (
            "Behavior fidelity is not validated for autonomous continuation. "
            "Confirm the preferred choice or run a behavior fidelity evaluation."
        )
    if needs_human:
        TAKEOVER_NEEDS_HUMAN_COUNT.inc()
    quality_history = state.takeover_context.get("quality_history")
    if not isinstance(quality_history, list):
        quality_history = []
    quality_history.append(
        {
            "turn": int(turn_count),
            "decision_confidence": float(round(decision_confidence, 4)),
            "context_quality_score": float(round(context_quality_score, 4)),
            "retrieval_triggered": bool(retrieval_triggered),
            "needs_human": bool(needs_human),
            "decision_source": decision_source.value,
            "advisor_fail_streak": int(advisor_fail_streak),
            "advisor_unhealthy": bool(advisor_unhealthy),
            "advisor_required_in_takeover": bool(advisor_required_in_takeover),
            "advisor_last_error": state.takeover_context.get("advisor_last_error"),
            "ts": datetime.now(tz=UTC).isoformat(),
        }
    )
    quality_history = quality_history[-120:]
    state.takeover_context["quality_history"] = quality_history
    avg_conf = (
        sum(_safe_float(item.get("decision_confidence"), 0.0) for item in quality_history) / max(1, len(quality_history))
    )
    avg_ctx = (
        sum(_safe_float(item.get("context_quality_score"), 0.0) for item in quality_history) / max(1, len(quality_history))
    )
    needs_human_rate_hist = (
        sum(1 for item in quality_history if bool(item.get("needs_human"))) / max(1, len(quality_history))
    )
    state.takeover_context["quality_rollup"] = {
        "window": len(quality_history),
        "avg_decision_confidence": round(max(0.0, min(1.0, avg_conf)), 4),
        "avg_context_quality": round(max(0.0, min(1.0, avg_ctx)), 4),
        "needs_human_rate": round(max(0.0, min(1.0, needs_human_rate_hist)), 4),
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }

    state.last_classification = classification
    state.last_safety_decision = safety_decision
    state.autonomy_score = round((0.8 * float(state.autonomy_score)) + (0.2 * decision_confidence), 4)
    state.enforcement_mode = settings.takeover_enforcement_mode
    _sync_enforcement_counters(db, state)
    save_takeover_state(db, state)

    directive_for_note = None
    actual_final_response = final_response
    if enforced and final_response and safety_decision == SafetyDecision.ALLOW:
        directive_for_note = final_response
        actual_final_response = None
    if safety_decision in {SafetyDecision.CONFIRM_REQUIRED, SafetyDecision.BLOCKED}:
        directive_for_note = None
        actual_final_response = final_response

    total_ms = int((time.perf_counter() - started_total) * 1000)
    TAKEOVER_TOTAL_MS.observe(total_ms / 1000)
    TAKEOVER_FAST_PATH_MS.observe(max(0, total_ms - deliberation_ms) / 1000)

    next_action = build_next_action(
        objective=resolved_task,
        mode=state.mode,
        safety_decision=safety_decision,
        needs_human=needs_human,
    )
    goal_score_breakdown: dict[str, float] = {}
    if selected_goal is not None:
        goal_score_breakdown = {
            "selection_score": float(selected_goal.selection_score or 0.0),
            "priority_score": float(selected_goal.priority_score or 0.0),
            "confidence": float(selected_goal.confidence or 0.0),
        }
    open_notice_row = db.execute(
        text(
            """
            SELECT id, session_id, workspace_id, user_id, goal_id, title, reason, priority, expires_at, created_at, acknowledged_at
            FROM autonomy_notices
            WHERE session_id = :session_id
              AND workspace_id = :workspace_id
              AND user_id = :user_id
              AND acknowledged_at IS NULL
              AND (expires_at IS NULL OR expires_at >= :now)
            ORDER BY priority DESC, created_at DESC
            LIMIT 1
            """
        ),
        {
            "session_id": state.session_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "now": datetime.now(tz=UTC),
        },
    ).mappings().first()
    autonomy_notice = _notice_from_row(open_notice_row) if open_notice_row is not None else None

    response = TakeoverStepResponse(
        state=state,
        action="advisor_takeover" if state.mode.value == "takeover" else "advisor_suggest",
        classification=classification,
        enforced=enforced,
        enforcement_reason=enforcement_reason,
        final_response=actual_final_response,
        safety_decision=safety_decision,
        takeover_enforcement=takeover_enforcement,
        auto_handoff=auto_handoff,
        clone_advice=clone_payload,
        citations=citations,
        note=directive_for_note,
        decision_confidence=decision_confidence,
        decision_source=decision_source,
        next_action=TakeoverNextAction.model_validate(next_action),
        latency_breakdown_ms=TakeoverLatencyBreakdown(
            state=state_ms,
            classify=classify_ms,
            retrieval=retrieval_ms + deliberation_ms,
            safety=safety_ms,
            total=total_ms,
        ),
        needs_human=needs_human,
        selected_goal=selected_goal,
        execution_permit_required=execution_permit_required,
        execution_permit_id=str(execution_permit_id) if execution_permit_id else None,
        continuity_ok=continuity_ok,
        directive_id=directive_id,
        directive_state=directive_state,
        pending_execution=pending_execution if isinstance(pending_execution, DirectiveExecution) else None,
        retry_scheduled=retry_scheduled,
        autonomy_notice=autonomy_notice,
        goal_cache_hit=bool(state.takeover_context.get("_goal_cache_hit", False)),
        goal_cache_source=str(state.takeover_context.get("_goal_cache_source"))
        if state.takeover_context.get("_goal_cache_source") is not None
        else None,
        selected_goal_score_breakdown=goal_score_breakdown,
        context_quality_score=context_quality_score,
        retrieval_triggered=retrieval_triggered,
        retrieval_source=retrieval_source,
        retrieval_reason=retrieval_reason,
        retrieval_latency_ms=retrieval_latency_ms,
        retrieval_hit_count=retrieval_hit_count,
        feedback_adjustment_applied=feedback_adjustment_applied,
        snapshot_rehydrated=snapshot_rehydrated,
        snapshot_age_hours=snapshot_age_hours,
        query_expansion_used=query_expansion_used_meta,
        query_expansion_terms=query_expansion_terms_meta,
        rerank_strategy=rerank_strategy_meta,
        context_tier_used=str(working_set.get("context_tier_used") or "l2"),
        summary_coverage=float(working_set.get("summary_coverage", 0.0) or 0.0),
        planner_used=bool(working_set.get("planner_used", False)),
        subquery_count=int(working_set.get("subquery_count", 0) or 0),
        subquery_labels=list(working_set.get("subquery_labels") or []),
        episode_boost_applied=bool(working_set.get("episode_boost_applied", False)),
        activation_boost_applied=bool(working_set.get("activation_boost_applied", False)),
        behavior_fidelity_gate=behavior_fidelity_gate,
    )
    write_audit_log(
        db,
        consumer=auth.consumer,
        action="takeover_step",
        query=body.model_dump(mode="json"),
        result_event_ids=response.citations,
        policy_decisions={
            "mode": state.mode.value,
            "classification": response.classification.value,
            "safety_decision": response.safety_decision.value,
            "enforced": response.enforced,
            "decision_source": response.decision_source.value,
            "decision_confidence": response.decision_confidence,
            "needs_human_threshold": needs_human_threshold,
            "needs_human": response.needs_human,
            "context_quality_score": response.context_quality_score,
            "retrieval_triggered": response.retrieval_triggered,
            "retrieval_source": response.retrieval_source,
            "retrieval_reason": response.retrieval_reason,
            "retrieval_latency_ms": response.retrieval_latency_ms,
            "retrieval_hit_count": response.retrieval_hit_count,
            "feedback_adjustment_applied": response.feedback_adjustment_applied,
            "snapshot_rehydrated": response.snapshot_rehydrated,
            "snapshot_age_hours": response.snapshot_age_hours,
            "query_expansion_used": response.query_expansion_used,
            "query_expansion_terms": response.query_expansion_terms,
            "rerank_strategy": response.rerank_strategy,
            "execution_permit_required": response.execution_permit_required,
            "execution_permit_id": response.execution_permit_id,
            "directive_id": str(response.directive_id) if response.directive_id else None,
            "directive_state": response.directive_state.value if response.directive_state else None,
            "retry_scheduled": response.retry_scheduled,
            "continuity_ok": response.continuity_ok,
        },
        latency_ms=total_ms,
    )
    _record_takeover_action_async(
        session_id=state.session_id,
        workspace_id=state.workspace_id,
        user_id=state.user_id,
        turn=turn_count,
        objective_hash_value=state.objective_hash,
        action_kind="takeover_step",
        result="needs_human" if needs_human else "success",
        latency_ms=total_ms,
        meta={
            "decision_source": response.decision_source.value,
            "decision_confidence": response.decision_confidence,
            "needs_human_threshold": needs_human_threshold,
            "classification": response.classification.value,
            "context_quality_score": response.context_quality_score,
            "retrieval_triggered": response.retrieval_triggered,
            "retrieval_source": response.retrieval_source,
            "retrieval_reason": response.retrieval_reason,
            "retrieval_latency_ms": response.retrieval_latency_ms,
            "retrieval_hit_count": response.retrieval_hit_count,
            "feedback_adjustment_applied": response.feedback_adjustment_applied,
            "snapshot_rehydrated": response.snapshot_rehydrated,
            "snapshot_age_hours": response.snapshot_age_hours,
            "query_expansion_used": response.query_expansion_used,
            "query_expansion_terms": response.query_expansion_terms,
            "rerank_strategy": response.rerank_strategy,
            "execution_permit_required": response.execution_permit_required,
            "execution_permit_id": response.execution_permit_id,
            "directive_id": str(response.directive_id) if response.directive_id else None,
            "directive_state": response.directive_state.value if response.directive_state else None,
            "retry_scheduled": response.retry_scheduled,
            "continuity_ok": response.continuity_ok,
        },
    )
    _auto_capture_interaction(
        db=db,
        auth=auth,
        action="takeover_step",
        request_payload=body.model_dump(mode="json"),
        response_payload={
            "action": response.action,
            "classification": response.classification.value,
            "safety_decision": response.safety_decision.value,
            "enforced": response.enforced,
            "enforcement_reason": response.enforcement_reason,
            "decision_source": response.decision_source.value,
            "decision_confidence": response.decision_confidence,
            "needs_human_threshold": needs_human_threshold,
            "needs_human": response.needs_human,
            "context_quality_score": response.context_quality_score,
            "retrieval_triggered": response.retrieval_triggered,
            "retrieval_source": response.retrieval_source,
            "retrieval_reason": response.retrieval_reason,
            "retrieval_latency_ms": response.retrieval_latency_ms,
            "retrieval_hit_count": response.retrieval_hit_count,
            "execution_permit_required": response.execution_permit_required,
            "execution_permit_id": response.execution_permit_id,
            "directive_id": str(response.directive_id) if response.directive_id else None,
            "directive_state": response.directive_state.value if response.directive_state else None,
            "retry_scheduled": response.retry_scheduled,
            "continuity_ok": response.continuity_ok,
        },
        citations=response.citations,
    )
    return response


@app.post("/v1/clone/arbitrate", response_model=CloneArbitrationResponse)
def clone_arbitrate(
    body: CloneArbitrationRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CloneArbitrationResponse:
    REQUEST_COUNT.labels(endpoint="clone_arbitrate", method="POST").inc()
    _enforce_workspace_access(auth, db)
    mode = get_runtime_mode(db)
    if mode.mode != OperationMode.CLONE_ADVISOR:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "clone_mode_disabled",
                "message": "clone advisor mode is disabled for arbitration",
                "current_mode": mode.mode.value,
                "action": "Set mode to clone_advisor before arbitration",
            },
        )

    result = arbitrate(
        executor_plan=body.executor_plan,
        advisor_input=body.advisor_input,
        human_override=body.human_override,
        interaction_id=body.interaction_id,
    )

    write_agent_interaction(
        db,
        interaction_id=body.interaction_id,
        source_consumer=auth.consumer,
        source_role=auth.role.value,
        target_role=AgentRole.EXECUTOR.value,
        action="clone_arbitrate",
        citations=result.citations,
        payload=result.model_dump(mode="json"),
        allowed=True,
        reason=result.decision_source,
    )

    write_audit_log(
        db,
        consumer=auth.consumer,
        action="clone_arbitrate",
        query=body.model_dump(mode="json"),
        result_event_ids=result.citations,
        policy_decisions={"mode": mode.mode.value, "decision_source": result.decision_source},
        latency_ms=0,
    )
    return result


# ---------------------------------------------------------------------------
# Static file mount for Angular dashboard (must be LAST — catch-all for /dashboard)
# ---------------------------------------------------------------------------
import os
from pathlib import Path

_DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent.parent / "dashboard-static"
if not _DASHBOARD_DIR.exists():
    # Fallback: check in-container path
    _DASHBOARD_DIR = Path("/app/dashboard-static")

if _DASHBOARD_DIR.exists():
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.staticfiles import StaticFiles

    class _DashboardSPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            leaf = path.rsplit("/", 1)[-1]
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code == 404 and "." not in leaf:
                    return await super().get_response("index.html", scope)
                raise

    app.mount("/dashboard", _DashboardSPAStaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")
