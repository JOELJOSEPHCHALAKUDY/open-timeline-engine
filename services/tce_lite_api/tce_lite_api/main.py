from __future__ import annotations

import errno
import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from ote_advisor_providers import get_provider, list_provider_metadata, resolve_fallback_chain
from ote_advisor_providers.base import ProviderAttemptResult, ProviderRequest
from ote_advisor_providers.router import (
    active_profile as advisor_active_profile,
)
from ote_advisor_providers.router import (
    enforce_required_category_coverage as advisor_enforce_required_category_coverage,
)
from ote_advisor_providers.router import (
    normalize_profile as advisor_normalize_profile,
)
from ote_advisor_providers.router import (
    normalize_profile_bundle as advisor_normalize_profile_bundle,
)
from ote_advisor_providers.router import (
    resolve_chain_from_profile as advisor_resolve_chain_from_profile,
)
from ote_advisor_providers.router import (
    runtime_status as advisor_runtime_status,
)
from ote_advisor_providers.router import (
    update_health_state as advisor_update_health_state,
)
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from tce_shared.behavior_control import normalize_counterfactual, redact_control_text
from tce_shared.behavior_fidelity import (
    CALIBRATION_SCENARIOS,
    behavior_storage_gate,
    eligible_behavior_evidence,
    evaluate_behavior_fidelity,
    normalize_behavior_evidence,
    predict_behavior,
)
from tce_shared.behavior_pilot import (
    assign_behavior_pilot_variant,
    behavior_pilot_outcome_digest,
    behavior_pilot_status,
    prepare_behavior_pilot_context,
    sanitize_behavior_pilot_payload,
)
from tce_shared.behavior_projection import BehaviorProjectionNotFound, build_behavior_projection
from tce_shared.dashboard import timeline_dashboard_html
from tce_shared.events import (
    AgentRole,
    AutonomyGoalStatus,
    AutonomyNotice,
    BehaviorCalibrationAnswerRequest,
    BehaviorCalibrationScenariosResponse,
    BehaviorEvaluationListResponse,
    BehaviorEvaluationRequest,
    BehaviorEvaluationResponse,
    BehaviorEvidenceRequest,
    BehaviorEvidenceResponse,
    BehaviorEvidenceSource,
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
    CloneAdviceRequest,
    CloneAdviceResponse,
    CompletionCaptureRequest,
    CompletionCaptureResponse,
    ContinuityPilotStatusResponse,
    CounterfactualCreateRequest,
    CounterfactualItem,
    CounterfactualListResponse,
    CounterfactualResolveRequest,
    DirectiveExecution,
    EventEnvelope,
    EventSearchRequest,
    EventType,
    ExecutionClaimRequest,
    ExecutionPermitRequest,
    ExecutionPermitResolveRequest,
    ExecutionPermitResponse,
    ExecutionReportRequest,
    ExecutionStatusResponse,
    GovernanceStatusResponse,
    MemoryReviewItem,
    MemoryReviewListResponse,
    MemoryReviewResolveRequest,
    PatternFeedbackRequest,
    ProcessMiningRequest,
    ProcessMiningResponse,
    ProcessModelItem,
    ResumeFeedbackRequest,
    ResumeFeedbackResponse,
    ResumePacketRequest,
    ResumePacketResponse,
    TakeoverAutonomyStatusResponse,
    TakeoverAutonomyTickRequest,
    TakeoverAutonomyTickResponse,
    TakeoverFeedbackRequest,
    TakeoverFeedbackResponse,
    TakeoverGoal,
    TakeoverGoalCacheInvalidateRequest,
    TakeoverGoalCacheStatusResponse,
    TakeoverGoalsDiscoverRequest,
    TakeoverGoalSelectRequest,
    TakeoverGoalsPrecomputeRequest,
    TakeoverGoalsResponse,
    TakeoverNoticeAckRequest,
    TakeoverNoticesResponse,
    TakeoverPreloadRequest,
    TakeoverPreloadResponse,
    TakeoverState,
    TakeoverStepRequest,
    TakeoverStepResponse,
)
from tce_shared.execution_transitions import completion_payload_fingerprint
from tce_shared.fingerprint import DEFAULT_FINGERPRINT, merge_observation_into_fingerprint
from tce_shared.governance import build_governance_status
from tce_shared.handoff import normalize_milestone_v1
from tce_shared.project_context import canonical_project_context, project_context_from_payload
from tce_shared.rate_limit import InMemoryRateLimiter
from tce_shared.redaction import redact_text
from tce_shared.scope import PROJECT_BOUND

from .auth import AuthContext, get_auth_context
from .behavior_control_store import (
    consume_capability_grant,
    create_counterfactual,
    create_memory_review,
    issue_capability_grant,
    list_counterfactuals,
    list_process_models,
    load_process_source_rows,
    mine_and_time,
    resolve_counterfactual,
    resolve_memory_review,
    save_process_models,
    save_shadow_prediction,
    shadow_status,
)
from .behavior_control_store import (
    list_memory_reviews as list_behavior_memory_reviews,
)
from .behavior_pilot_store import (
    BehaviorPilotConflict,
    BehaviorPilotExpired,
    BehaviorPilotNotFound,
)
from .behavior_pilot_store import (
    create_or_get_assignment_lite as create_or_get_behavior_pilot_assignment,
)
from .behavior_pilot_store import (
    list_pilot_rows_lite as list_behavior_pilot_rows,
)
from .behavior_pilot_store import (
    record_outcome_lite as record_behavior_pilot_outcome,
)
from .config import get_settings
from .continuity_store import (
    CompletionConflictError,
    deliver_handoff_safely,
    drain_pending_handoffs,
    enqueue_handoff,
    pilot_metrics,
    record_resume_progress,
)
from .db import get_db, init_db
from .store import (
    acknowledge_takeover_notice as store_acknowledge_takeover_notice,
)
from .store import (
    activity_summary,
    build_clone_advice,
    build_clone_arbitration,
    context_bundle,
    discover_takeover_goals,
    get_event,
    get_resume_packet,
    invalidate_takeover_goal_cache,
    json_dumps,
    json_loads,
    latest_fidelity_gate_lite,
    list_fidelity_runs_lite,
    list_patterns,
    list_takeover_goals,
    load_behavior_evidence_by_id_lite,
    load_behavior_evidence_lite,
    load_fingerprint_lite,
    request_execution_permit_lite,
    resolve_execution_permit_lite,
    run_lifecycle_maintenance,
    runtime_mode,
    save_behavior_evidence_lite,
    save_fidelity_run_lite,
    save_fingerprint_lite,
    save_observation_lite,
    search_events,
    select_takeover_goal,
    set_runtime_mode,
    store_event,
    submit_pattern_feedback,
    write_audit,
)
from .store import (
    annotate_event as store_annotate_event,
)
from .store import (
    autonomy_project_kpis as store_autonomy_project_kpis,
)
from .store import (
    autonomy_readiness as store_autonomy_readiness,
)
from .store import (
    claim_execution as store_claim_execution,
)
from .store import (
    context_brief as store_context_brief,
)
from .store import (
    context_retrieval_status as store_context_retrieval_status,
)
from .store import (
    deprecate_memory_rule as store_deprecate_memory_rule,
)
from .store import (
    execution_status as store_execution_status,
)
from .store import (
    forget_memory as store_forget_memory,
)
from .store import (
    get_episode as store_get_episode,
)
from .store import (
    graph_health_status_lite as store_graph_health_status,
)
from .store import (
    lifecycle_status as store_lifecycle_status,
)
from .store import (
    list_episodes as store_list_episodes,
)
from .store import (
    list_memory_rules as store_list_memory_rules,
)
from .store import (
    list_takeover_notices as store_list_takeover_notices,
)
from .store import (
    report_execution as store_report_execution,
)
from .store import (
    reset_takeover_state as store_reset_takeover_state,
)
from .store import (
    retrieval_eval_status as store_retrieval_eval_status,
)
from .store import (
    run_retrieval_eval as store_run_retrieval_eval,
)
from .store import (
    takeover_autonomy_status as store_takeover_autonomy_status,
)
from .store import (
    takeover_autonomy_tick as store_takeover_autonomy_tick,
)
from .store import (
    takeover_feedback as store_takeover_feedback,
)
from .store import (
    takeover_goal_cache_status as store_takeover_goal_cache_status,
)
from .store import (
    takeover_preload as store_takeover_preload,
)
from .store import (
    takeover_state as store_takeover_state,
)
from .store import (
    takeover_step as store_takeover_step,
)
from .store import (
    upsert_memory_rule as store_upsert_memory_rule,
)
from .store_graph import (
    graph_for_event,
    list_team_memberships,
    search_entities,
    upsert_team_membership,
    workspace_access_allowed,
)
from .types import (
    ActivitySummaryResponse,
    AdvisorConfigResponse,
    AdvisorConfigUpdateRequest,
    AdvisorLiveModelsRequest,
    AdvisorLocalOllamaPullRequest,
    AdvisorLocalOllamaPullResponse,
    AdvisorLocalOllamaPullStatusResponse,
    AdvisorModelsResponse,
    AdvisorProfileItem,
    AdvisorProviderItem,
    AdvisorProvidersResponse,
    AdvisorRouteItem,
    AdvisorRouteVerifyRequest,
    AdvisorRuntimeProbeRequest,
    AdvisorRuntimeProbeResponse,
    AdvisorRuntimeStatusResponse,
    AdvisorRuntimeStatusRoute,
    AdvisorSwitchRequest,
    AdvisorSwitchResponse,
    AdvisorVerifyAttempt,
    AdvisorVerifyRequest,
    AdvisorVerifyResponse,
    ApiStatusInfo,
    BatchIngestRequest,
    BatchIngestResponse,
    CloneArbitrationRequest,
    CloneArbitrationResponse,
    CloneScoreBreakdown,
    CloneScoreResponse,
    ContextBriefRequest,
    ContextBriefResponse,
    ContextBundleRequest,
    ContextBundleResponse,
    DashboardAgentRole,
    DashboardAgentRolesResponse,
    DashboardClientConfigResponse,
    DashboardGoalIntelligenceItem,
    DashboardGoalsIntelligenceResponse,
    DashboardGoalsIntelligenceSummary,
    DashboardHumanScoreHistoryPoint,
    DashboardHumanScoreHistoryResponse,
    DashboardHumanScoreRecomputeRequest,
    DashboardHumanScoreResponse,
    DashboardHumanScoreSubscores,
    DashboardIdentityInfo,
    DashboardStackRestartRequest,
    DashboardStackRestartResponse,
    DashboardStackRestartStatusResponse,
    DatabaseStatusInfo,
    EpisodeItem,
    EpisodeListResponse,
    EventAnnotationRequest,
    EventAnnotationResponse,
    FingerprintResponse,
    GoalEmotionValue,
    GoalRelationEdge,
    GraphEntitySearchResponse,
    GraphEventResponse,
    HealthResponse,
    IngestResponse,
    MemoryForgetRequest,
    MemoryForgetResponse,
    MemoryRuleDeprecateResponse,
    MemoryRuleItem,
    MemoryRuleListResponse,
    MemoryRuleUpsertRequest,
    ObservationItem,
    ObservationListResponse,
    PatternItem,
    RetrievalEvalRunRequest,
    RetrievalEvalRunResponse,
    RetrievalEvalStatusResponse,
    RuntimeModeConfig,
    RuntimeModeInfo,
    RuntimeModeSetRequest,
    ServiceStatusInfo,
    SystemStatusResponse,
    TeamMembershipUpsertRequest,
)

settings = get_settings()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    connection_scope = get_db()
    conn = next(connection_scope)
    try:
        drain_pending_handoffs(conn, retention_days=int(settings.handoff_retention_days))
    finally:
        connection_scope.close()
    yield


app = FastAPI(
    title="Open Timeline Engine Lite API",
    version="0.4.0",
    lifespan=_lifespan,
)
_APP_START_TIME = time.monotonic()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
rate_limiter = InMemoryRateLimiter(limit=settings.rate_limit_requests_per_minute, window_seconds=60)
takeover_step_rate_limiter = InMemoryRateLimiter(
    limit=settings.takeover_step_rate_limit_requests_per_minute,
    window_seconds=60,
)
takeover_lifecycle_rate_limiter = InMemoryRateLimiter(
    limit=settings.takeover_lifecycle_rate_limit_requests_per_minute,
    window_seconds=60,
)

REQUEST_COUNT = Counter("tce_lite_api_requests_total", "Total API requests", ["endpoint", "method"])
REQUEST_LATENCY = Histogram("tce_lite_api_request_latency_seconds", "API request latency", ["endpoint", "method"])
TAKEOVER_FAST_PATH_MS = Histogram("tce_lite_api_takeover_fast_path_seconds", "Takeover fast-path latency seconds")
TAKEOVER_DELIBERATION_MS = Histogram("tce_lite_api_takeover_deliberation_seconds", "Takeover deliberation latency seconds")
TAKEOVER_TOTAL_MS = Histogram("tce_lite_api_takeover_total_seconds", "Takeover total latency seconds")
TAKEOVER_DELIBERATION_COUNT = Counter("tce_lite_api_takeover_deliberation_total", "Takeover deliberation activations")
TAKEOVER_NEEDS_HUMAN_COUNT = Counter("tce_lite_api_takeover_needs_human_total", "Takeover turns requiring human input")
TAKEOVER_CONFIDENCE = Histogram("tce_lite_api_takeover_confidence", "Takeover decision confidence")
TAKEOVER_RETRIEVAL_TRIGGER_COUNT = Counter(
    "tce_lite_api_takeover_retrieval_trigger_total",
    "Takeover retrieval triggers",
    ["reason"],
)
TAKEOVER_RETRIEVAL_SOURCE_COUNT = Counter(
    "tce_lite_api_takeover_retrieval_source_total",
    "Takeover retrieval source counts",
    ["source"],
)
TAKEOVER_RETRIEVAL_BUDGET_EXCEEDED_COUNT = Counter(
    "tce_lite_api_takeover_retrieval_budget_exceeded_total",
    "Takeover retrieval budget guard suppressions",
)
TAKEOVER_RETRIEVAL_LATENCY = Histogram(
    "tce_lite_api_takeover_retrieval_latency_seconds",
    "Takeover retrieval latency seconds",
    ["source"],
)
_DOCKER_STATS_EXECUTOR = ThreadPoolExecutor(max_workers=6)
_ADVISOR_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=6)
_docker_stats_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_DOCKER_CACHE_TTL = 20.0
_DOCKER_STATS_REQUEST_TIMEOUT_SECONDS = 0.9
_DOCKER_STATS_OVERALL_BUDGET_SECONDS = 1.2
_DOCKER_STATS_MAX_TARGETS = 10
DASHBOARD_HUMAN_SCORE_MS = Histogram(
    "tce_lite_api_dashboard_human_score_compute_seconds",
    "Dashboard human score compute latency seconds",
)
DASHBOARD_GOAL_INTELLIGENCE_MS = Histogram(
    "tce_lite_api_dashboard_goal_intelligence_compute_seconds",
    "Dashboard goal intelligence compute latency seconds",
)
DASHBOARD_HUMAN_SCORE_RECOMPUTE_COUNT = Counter(
    "tce_lite_api_dashboard_human_score_recompute_total",
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


def _normalize_session_id_value(session_id: str | None) -> str:
    return str(session_id or "").strip() or "default"


def _latest_known_session_id(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT session_id
        FROM takeover_sessions
        WHERE workspace_id = ? AND user_id = ?
        ORDER BY
          CASE WHEN session_id = 'default' THEN 1 ELSE 0 END ASC,
          CASE WHEN active = 1 THEN 0 ELSE 1 END ASC,
          COALESCE(last_message_at, updated_at, activated_at) DESC,
          updated_at DESC
        LIMIT 1
        """,
        (workspace_id, user_id),
    ).fetchone()
    candidate = str((row["session_id"] if row else "") or "").strip()
    if candidate:
        return candidate

    row = conn.execute(
        """
        SELECT session_id
        FROM directive_executions
        WHERE workspace_id = ?
          AND user_id = ?
          AND session_id IS NOT NULL
          AND session_id <> 'default'
        ORDER BY COALESCE(updated_at, created_at) DESC
        LIMIT 1
        """,
        (workspace_id, user_id),
    ).fetchone()
    candidate = str((row["session_id"] if row else "") or "").strip()
    if candidate:
        return candidate
    return None


def _resolve_effective_session_id(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str | None,
) -> str:
    requested = _normalize_session_id_value(session_id)
    if requested.lower() != "default":
        return requested
    inferred = _latest_known_session_id(conn, workspace_id=workspace_id, user_id=user_id)
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


def _lite_advisor_scope_user_id(user_id: str) -> str:
    configured = os.getenv("TCE_ADVISOR_CONFIG_USER_ID", "").strip()
    if configured:
        return configured
    fallback = os.getenv("TCE_MCP_EXECUTOR_USER_ID", "").strip()
    if fallback:
        return fallback
    return (user_id or "").strip() or "local-user"


def _lite_advisor_setup_key(workspace_id: str, user_id: str) -> str:
    return f"advisor_setup:{workspace_id}:{_lite_advisor_scope_user_id(user_id)}"


def _lite_advisor_secret_key(secret_ref: str) -> str:
    return f"advisor_secret:{secret_ref}"


def _lite_advisor_profiles_key(workspace_id: str, user_id: str) -> str:
    return f"advisor_profiles:{workspace_id}:{_lite_advisor_scope_user_id(user_id)}"


def _lite_advisor_health_key(workspace_id: str, user_id: str) -> str:
    return f"advisor_runtime_health:{workspace_id}:{_lite_advisor_scope_user_id(user_id)}"


_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_RAW_ENV_KEYS = {"TCE_ADVISOR_ROUTES_JSON"}
_LEGACY_API_KEY_ENV_ALIAS: dict[str, str] = {
    "TCE_OPENAI_API_KEY": "OPENAI_API_KEY",
    "TCE_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "TCE_GEMINI_API_KEY": "GEMINI_API_KEY",
    "TCE_DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY",
}


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
    parsed = json_loads(value, [])
    if isinstance(parsed, str):
        parsed = json_loads(parsed, [])
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
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


def _lite_runtime_setting_json(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return {}
    value = row["value"]
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json_loads(value, {})
        if isinstance(parsed, dict):
            return parsed
    return {}


def _lite_upsert_runtime_setting(conn: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    now = datetime.now(tz=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json_dumps(value), now),
    )


def _lite_load_advisor_key(conn: sqlite3.Connection, secret_ref: str | None) -> str:
    _ = conn
    _ = secret_ref
    return ""


def _lite_store_advisor_key(conn: sqlite3.Connection, secret_ref: str, api_key: str) -> tuple[str, bool]:
    _ = conn
    _ = secret_ref
    value = (api_key or "").strip()
    if not value:
        return "none", False
    return "env_file", True


def _lite_default_advisor_config() -> dict[str, Any]:
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


def _lite_load_advisor_config(conn: sqlite3.Connection, *, workspace_id: str, user_id: str) -> dict[str, Any]:
    merged = _lite_default_advisor_config()
    key = _lite_advisor_setup_key(workspace_id, user_id)
    payload = _lite_runtime_setting_json(conn, key)
    runtime_meta_keys = {
        "key_storage_backend",
        "key_present",
        "updated_at",
        "custom_headers",
        "api_version",
    }
    merged.update({k: v for k, v in payload.items() if k in runtime_meta_keys})
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
    runtime_primary = str(payload.get("advisor_primary_provider") or "").strip().lower()
    runtime_model = str(payload.get("advisor_primary_model") or "").strip()
    runtime_fallback_raw = payload.get("advisor_fallback_chain")
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
        runtime_profiles_raw = _lite_runtime_setting_json(conn, _lite_advisor_profiles_key(workspace_id, user_id))
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
    if not merged.get("updated_at"):
        merged["updated_at"] = datetime.now(tz=UTC).isoformat()
    return merged


def _lite_normalized_advisor_runtime_profile(
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


def _lite_load_advisor_health(conn: sqlite3.Connection, *, workspace_id: str, user_id: str) -> dict[str, Any]:
    payload = _lite_runtime_setting_json(conn, _lite_advisor_health_key(workspace_id, user_id))
    routes = payload.get("routes")
    if isinstance(routes, dict):
        return routes
    return {}


def _lite_save_advisor_health(conn: sqlite3.Connection, *, workspace_id: str, user_id: str, routes: dict[str, Any]) -> None:
    _lite_upsert_runtime_setting(
        conn,
        _lite_advisor_health_key(workspace_id, user_id),
        {"routes": routes, "updated_at": datetime.now(tz=UTC).isoformat()},
    )


def _lite_route_for_provider(config: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
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


def _lite_purge_advisor_secret_runtime_settings(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM runtime_settings WHERE key LIKE 'advisor_secret:%'")


def _lite_resolve_route_key_ref(
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


def _lite_resolve_route_api_key(
    *,
    conn: sqlite3.Connection,
    provider_id: str,
    route: dict[str, Any] | None,
    config: dict[str, Any],
    explicit_key_ref: str | None = None,
    provided_key: str | None = None,
) -> str:
    direct_key = str(provided_key or "").strip()
    if direct_key:
        return direct_key
    _ = conn
    _ = _lite_resolve_route_key_ref(
        provider_id=provider_id,
        route=route,
        config=config,
        explicit_key_ref=explicit_key_ref,
    )
    env_key = _provider_api_key_env(provider_id)
    return os.getenv(env_key or "", "").strip()


def _lite_config_response_from_raw(config_raw: dict[str, Any]) -> AdvisorConfigResponse:
    updated_at = _parse_dt(config_raw.get("updated_at")) or datetime.now(tz=UTC)
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
        custom_headers=_normalized_custom_headers(config_raw.get("custom_headers")),
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


def _normalized_custom_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, raw_value in value.items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(raw_value).strip()
        if normalized_key in settings.advisor_custom_headers_allowlist_set and normalized_value:
            normalized[normalized_key] = normalized_value
    return normalized


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
                    payload = json_loads(raw_line, {})
                    if not isinstance(payload, dict):
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

    threading.Thread(target=_run, name=f"lite-ollama-pull-{job_id}", daemon=True).start()
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
        commands.append(f"docker compose -f {compose_q} down")
        commands.append(f"docker compose -f {compose_q} up -d --build")

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

    threading.Thread(target=_run, name=f"lite-stack-restart-containerized-{job_id}", daemon=True).start()
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

    threading.Thread(target=_run, name=f"lite-stack-restart-{job_id}", daemon=True).start()
    return job_id


def _latest_activity_ts(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    role: str,
    user_id: str,
    consumer_id: str,
) -> datetime | None:
    row = conn.execute(
        """
        SELECT MAX(ts) AS last_seen
        FROM events
        WHERE (json_extract(context, '$._tce_workspace') IS NULL OR json_extract(context, '$._tce_workspace') = ?)
          AND (
            actor = ?
            OR json_extract(context, '$.user') = ?
            OR json_extract(context, '$.consumer') = ?
            OR json_extract(context, '$.role') = ?
          )
        """,
        (workspace_id, user_id, user_id, consumer_id, role),
    ).fetchone()
    return _parse_dt(row["last_seen"] if row else None)


def _resolve_dashboard_role(
    conn: sqlite3.Connection,
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
        conn,
        workspace_id=workspace_id,
        role=role,
        user_id=user_id,
        consumer_id=consumer_id,
    )
    last_seen = activity_ts or membership_ts
    active_cutoff = now - timedelta(minutes=30)

    if last_seen and last_seen >= active_cutoff:
        status = "active"
        source = "event_activity"
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


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json_loads(value, {})
        if isinstance(parsed, dict):
            return parsed
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


def _compute_clone_readiness_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
) -> tuple[int, dict[str, Any]]:
    total_situation_types = 12
    obs_count = _safe_int(
        conn.execute(
            "SELECT COUNT(1) AS c FROM decision_observations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()["c"]
    )
    observation_count_score = min(25, int(obs_count * 25 / 100))

    fp_data = load_fingerprint_lite(conn, consumer_id="dashboard", workspace_id=workspace_id)
    fingerprint_confidence = 0
    if fp_data and fp_data.get("fingerprint"):
        non_default = 0
        total_dims = 0
        for category, defaults in DEFAULT_FINGERPRINT.items():
            if not isinstance(defaults, dict):
                continue
            for key, default_val in defaults.items():
                total_dims += 1
                stored_cat = fp_data["fingerprint"].get(category, {})
                if isinstance(stored_cat, dict) and stored_cat.get(key) != default_val:
                    non_default += 1
        fingerprint_confidence = int(non_default / max(total_dims, 1) * 25) if total_dims else 0

    distinct_types = _safe_int(
        conn.execute(
            "SELECT COUNT(DISTINCT situation_type) AS c FROM decision_observations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()["c"]
    )
    pattern_coverage = min(25, int(distinct_types / total_situation_types * 25))

    recent_rows = conn.execute(
        "SELECT outcome_sentiment FROM decision_observations WHERE workspace_id = ? ORDER BY ts DESC LIMIT 20",
        (workspace_id,),
    ).fetchall()
    if recent_rows:
        positive_neutral = sum(
            1 for row in recent_rows if row["outcome_sentiment"] in ("positive", "neutral", None)
        )
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


def _build_goal_intelligence_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> DashboardGoalsIntelligenceResponse:
    now = datetime.now(tz=UTC)
    rows = conn.execute(
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
        WHERE workspace_id = ?
          AND user_id = ?
          AND session_id = ?
        ORDER BY selection_score DESC, updated_at DESC
        LIMIT 200
        """,
        (workspace_id, user_id, session_id),
    ).fetchall()

    goals: list[DashboardGoalIntelligenceItem] = []
    goal_goal_edges = 0
    goal_event_edges = 0
    goal_emotion_edges = 0
    scored_refs: list[tuple[str, float]] = []

    for row in rows:
        goal_id = str(row["id"])
        evidence_ids = [str(item) for item in json_loads(row["evidence_event_ids"], [])]
        affective_scores = _as_dict(row["affective_scores"])
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
        created_at = _parse_dt(row["created_at"])
        long_term, long_term_reason, age_days = _classify_long_term_goal(
            source=str(row["source"] or "open_discovery"),
            status=str(row["status"] or "candidate"),
            created_at=created_at,
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

        if row["parent_goal_id"]:
            relations.append(
                GoalRelationEdge(
                    source_goal_id=goal_id,
                    target_id=str(row["parent_goal_id"]),
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

        selection_score = round(_to_float(row["selection_score"]), 4)
        scored_refs.append((goal_id, selection_score))
        goals.append(
            DashboardGoalIntelligenceItem(
                id=goal_id,
                title=str(row["title"] or ""),
                description=str(row["description"] or ""),
                source=str(row["source"] or "open_discovery"),
                status=str(row["status"] or "candidate"),
                selection_score=selection_score,
                priority_score=round(_to_float(row["priority_score"]), 4),
                confidence=round(_to_float(row["confidence"]), 4),
                age_days=age_days,
                long_term=long_term,
                long_term_reason=long_term_reason,
                top_emotions=top_emotions,
                affective_scores=affective_scores,
                relation_count=len(relations),
                relations=relations,
            )
        )

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

    return DashboardGoalsIntelligenceResponse(
        session_id=session_id,
        generated_at=now,
        summary=DashboardGoalsIntelligenceSummary(
            total_goals=len(goals),
            long_term_goals=sum(1 for goal in goals if goal.long_term),
            goal_event_edges=goal_event_edges,
            goal_emotion_edges=goal_emotion_edges,
            goal_goal_edges=goal_goal_edges,
        ),
        goals=goals,
    )


def _compute_human_score_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> DashboardHumanScoreResponse:
    now = datetime.now(tz=UTC)
    clone_score, clone_inputs = _compute_clone_readiness_lite(conn, workspace_id=workspace_id)
    goal_intelligence = _build_goal_intelligence_lite(
        conn,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
    )

    execution_row = conn.execute(
        """
        SELECT
          COUNT(1) AS total,
          SUM(CASE WHEN state = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
          SUM(CASE WHEN attempt > 1 THEN attempt - 1 ELSE 0 END) AS retry_units,
          SUM(CASE WHEN action_kind IN ('edit', 'write', 'delete', 'command', 'shell') THEN 1 ELSE 0 END) AS mutating_total,
          SUM(
            CASE
              WHEN action_kind IN ('edit', 'write', 'delete', 'command', 'shell')
               AND requires_permit = 1
               AND permit_id IS NULL
              THEN 1 ELSE 0 END
          ) AS permit_misses
        FROM directive_executions
        WHERE workspace_id = ?
          AND user_id = ?
          AND session_id = ?
        """,
        (workspace_id, user_id, session_id),
    ).fetchone()
    total_exec = _safe_int(execution_row["total"] if execution_row else 0)
    succeeded_exec = _safe_int(execution_row["succeeded"] if execution_row else 0)
    retry_units = _safe_int(execution_row["retry_units"] if execution_row else 0)
    mutating_total = _safe_int(execution_row["mutating_total"] if execution_row else 0)
    permit_misses = _safe_int(execution_row["permit_misses"] if execution_row else 0)

    takeover_row = conn.execute(
        """
        SELECT continuity_violation_count, goal_queue_size, pending_directive_count, retry_backlog_count
        FROM takeover_sessions
        WHERE workspace_id = ?
          AND user_id = ?
          AND session_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (workspace_id, user_id, session_id),
    ).fetchone()
    continuity_violations = _safe_int(takeover_row["continuity_violation_count"] if takeover_row else 0)
    queue_size = _safe_int(takeover_row["goal_queue_size"] if takeover_row else 0)
    pending_directives = _safe_int(takeover_row["pending_directive_count"] if takeover_row else 0)
    retry_backlog = _safe_int(takeover_row["retry_backlog_count"] if takeover_row else 0)

    success_rate = (succeeded_exec / total_exec) if total_exec else 0.5
    retry_pressure = min(1.0, retry_units / max(total_exec, 1))
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
        affective = _as_dict(goal.affective_scores)
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

    sentiment_row = conn.execute(
        """
        SELECT
          COUNT(1) AS total,
          SUM(CASE WHEN outcome_sentiment IN ('positive', 'neutral') OR outcome_sentiment IS NULL THEN 1 ELSE 0 END) AS positive_neutral
        FROM (
          SELECT outcome_sentiment
          FROM decision_observations
          WHERE workspace_id = ?
          ORDER BY ts DESC
          LIMIT 80
        )
        """,
        (workspace_id,),
    ).fetchone()
    obs_total = _safe_int(sentiment_row["total"] if sentiment_row else 0)
    obs_positive_neutral = _safe_int(sentiment_row["positive_neutral"] if sentiment_row else 0)
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
    return DashboardHumanScoreResponse(
        session_id=session_id,
        score=human_score,
        band=_score_band(human_score),
        subscores=DashboardHumanScoreSubscores(
            clone_readiness=round(clone_score, 2),
            execution_quality=round(execution_quality, 2),
            goal_coherence=round(goal_coherence, 2),
            affective_alignment=round(affective_alignment, 2),
        ),
        inputs={
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
        },
        computed_at=now,
        snapshot_id=None,
    )


def _persist_human_score_snapshot_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    response: DashboardHumanScoreResponse,
) -> str:
    snapshot_id = os.urandom(16).hex()
    conn.execute(
        """
        INSERT INTO dashboard_human_score_snapshots (
          id, session_id, workspace_id, user_id, score, band, subscores_json, inputs_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            response.session_id,
            workspace_id,
            user_id,
            response.score,
            response.band,
            json_dumps(response.subscores.model_dump()),
            json_dumps(response.inputs),
            response.computed_at.isoformat(),
        ),
    )
    conn.commit()
    return snapshot_id


def _latest_human_score_snapshot_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
) -> DashboardHumanScoreResponse | None:
    row = conn.execute(
        """
        SELECT id, score, band, subscores_json, inputs_json, created_at
        FROM dashboard_human_score_snapshots
        WHERE workspace_id = ?
          AND user_id = ?
          AND session_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workspace_id, user_id, session_id),
    ).fetchone()
    if not row:
        return None
    created_at = _parse_dt(row["created_at"]) or datetime.now(tz=UTC)
    subscores_data = json_loads(row["subscores_json"], {})
    inputs_data = json_loads(row["inputs_json"], {})
    return DashboardHumanScoreResponse(
        session_id=session_id,
        score=max(0, min(100, _safe_int(row["score"]))),
        band=str(row["band"] or _score_band(_safe_int(row["score"]))),
        subscores=DashboardHumanScoreSubscores(
            clone_readiness=round(_to_float((subscores_data or {}).get("clone_readiness"), 0.0), 2),
            execution_quality=round(_to_float((subscores_data or {}).get("execution_quality"), 0.0), 2),
            goal_coherence=round(_to_float((subscores_data or {}).get("goal_coherence"), 0.0), 2),
            affective_alignment=round(_to_float((subscores_data or {}).get("affective_alignment"), 0.0), 2),
        ),
        inputs=inputs_data if isinstance(inputs_data, dict) else {},
        computed_at=created_at,
        snapshot_id=str(row["id"]),
    )


def _parse_container_stats(stats: dict[str, Any], project: str, service_name: str, container_name: str) -> dict[str, Any]:
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
    now = time.monotonic()
    cached = _docker_stats_cache["data"]
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and isinstance(cached[0], list)
        and isinstance(cached[1], dict)
        and (now - float(_docker_stats_cache["ts"])) < _DOCKER_CACHE_TTL
    ):
        return cached[0], cached[1]

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
                _cid, proj, svc, _name = row
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
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Docker stats endpoint returned a non-object payload")
                return payload

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
                    _cid, proj, svc, cname = futures[fut]
                    try:
                        stats = fut.result()
                        services.append(_parse_container_stats(stats, proj, svc, cname))
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


def _enforce_workspace_access(auth: AuthContext, conn: sqlite3.Connection) -> None:
    access_mode = str(getattr(settings, "workspace_access_mode", "compat") or "compat").strip().lower()
    if access_mode != "strict" and auth.role in {AgentRole.EXECUTOR, AgentRole.ADVISOR}:
        return
    try:
        allowed = workspace_access_allowed(
            conn,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            require_membership=access_mode == "strict",
        )
    except Exception as exc:
        if access_mode == "strict":
            raise HTTPException(status_code=503, detail="workspace access could not be verified") from exc
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="workspace access denied")
    if access_mode == "strict" and not _behavior_subject_access_allowed(auth):
        raise HTTPException(status_code=403, detail="behavior subject access denied")


def _reject_advisor_writes(auth: AuthContext) -> None:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")


def _truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}<TRUNCATED>"


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


def _auto_capture_interaction(
    conn: sqlite3.Connection,
    auth: AuthContext,
    action: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    citations: list[UUID] | None = None,
) -> None:
    if not settings.auto_capture_interactions:
        return

    raw = str(
        {
            "action": action,
            "request": request_payload,
            "response": response_payload,
        }
    )
    clipped = _truncate_text(raw, settings.auto_capture_max_chars)
    redacted, applied = redact_text(clipped)
    sensitive_skipped = bool(applied) and settings.auto_capture_skip_sensitive

    summary = _interaction_summary(action, request_payload, response_payload)
    title = f"Interaction: {action}"
    if summary:
        title = f"{title} - {summary}"
    if sensitive_skipped:
        title = f"Interaction: {action} (sensitive content skipped)"
        payload: dict[str, Any] = {
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
            "redacted_excerpt": redacted,
            "citations": [str(item) for item in (citations or [])],
        }

    project_context = project_context_from_payload(request_payload)
    event = EventEnvelope(
        schema_version=1,
        ts=datetime.now(tz=UTC),
        actor=auth.user_id,
        source="api-auto-capture",
        domain=_interaction_domain(request_payload),
        task_type=f"interaction_{action}"[:80],
        event_type=_auto_capture_event_type(action),
        title=title,
        payload=payload,
        context={
            "workspace": auth.workspace_id,
            "user": auth.user_id,
            "consumer": auth.consumer,
            "role": auth.role.value,
            "input_origin": "executor_relay" if action == "takeover_step" else "system",
            **project_context,
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
    try:
        store_event(conn, event, settings, auth)
    except Exception:
        return


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


@app.get("/", include_in_schema=False)
def dashboard_root() -> Response:
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/ui", include_in_schema=False)
def dashboard() -> Response:
    return HTMLResponse(content=timeline_dashboard_html(api_base=""))


@app.get("/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    REQUEST_COUNT.labels(endpoint="health", method="GET").inc()
    return HealthResponse(status="ok")


@app.get("/v1/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/runtime/mode", response_model=RuntimeModeConfig)
def get_runtime_mode(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> RuntimeModeConfig:
    _ = auth
    REQUEST_COUNT.labels(endpoint="runtime_mode_get", method="GET").inc()
    return runtime_mode(conn, settings)


@app.put("/v1/runtime/mode", response_model=RuntimeModeConfig)
def update_runtime_mode(
    body: RuntimeModeSetRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> RuntimeModeConfig:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="runtime_mode_set", method="PUT").inc()
    start = time.perf_counter()
    mode = set_runtime_mode(conn, body.mode, updated_by=auth.consumer)
    latency_ms = int((time.perf_counter() - start) * 1000)
    write_audit(
        conn,
        consumer=auth.consumer,
        action="set_runtime_mode",
        query={"mode": body.mode.value},
        result_event_ids=[],
        policy_decisions={"role": auth.role.value},
        latency_ms=latency_ms,
    )
    return mode


@app.post("/v1/events", response_model=IngestResponse)
def ingest_event(
    event: EventEnvelope,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> IngestResponse:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="events", method="POST").inc()
    start = time.perf_counter()
    event_id = store_event(conn, event, settings, auth)
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="events", method="POST").observe(latency_ms / 1000.0)
    write_audit(
        conn,
        consumer=auth.consumer,
        action="record_event",
        query={"event_id": str(event_id)},
        result_event_ids=[event_id],
        policy_decisions={"role": auth.role.value},
        latency_ms=latency_ms,
    )
    return IngestResponse(event_id=event_id)


@app.post("/v1/events/batch", response_model=BatchIngestResponse)
def ingest_batch(
    body: BatchIngestRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BatchIngestResponse:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="events_batch", method="POST").inc()
    start = time.perf_counter()
    event_ids = [store_event(conn, event, settings, auth) for event in body.events]
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="events_batch", method="POST").observe(latency_ms / 1000.0)
    write_audit(
        conn,
        consumer=auth.consumer,
        action="record_event_batch",
        query={"count": len(body.events)},
        result_event_ids=event_ids,
        policy_decisions={"role": auth.role.value},
        latency_ms=latency_ms,
    )
    return BatchIngestResponse(event_ids=event_ids)


@app.get("/v1/events/{event_id}", response_model=EventEnvelope)
def read_event(
    event_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
):
    REQUEST_COUNT.labels(endpoint="event_get", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    event = get_event(conn, event_id, settings, workspace_id=auth.workspace_id, owner_id=auth.user_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@app.post("/v1/search", response_model=dict)
def search(
    body: EventSearchRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    REQUEST_COUNT.labels(endpoint="search", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    start = time.perf_counter()
    scope = auth.resolved_scope(
        project_hint=getattr(body, "app_context", None) if isinstance(getattr(body, "app_context", None), dict) else None,
        target_owner=getattr(body, "target_owner", None),
        continuity_intent=bool(getattr(body, "continuity_intent", False)),
    )
    result, blocked, retrieval_meta = search_events(
        conn, body, settings, workspace_id=auth.workspace_id, owner_id=auth.user_id, scope=scope
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="search", method="POST").observe(latency_ms / 1000.0)
    policy_profile = "workspace-shared" if retrieval_meta.get("cross_user_scope_applied") else "user-only"
    policy_payload = {"blocked": blocked, "retrieval": retrieval_meta, "policy_profile": policy_profile}
    write_audit(
        conn,
        consumer=auth.consumer,
        action="search_events",
        query=body.model_dump(mode="json"),
        result_event_ids=result.citations,
        policy_decisions=policy_payload,
        latency_ms=latency_ms,
    )
    _auto_capture_interaction(
        conn=conn,
        auth=auth,
        action="search_events",
        request_payload=body.model_dump(mode="json"),
        response_payload={
            "hits_count": len(result.hits),
            "blocked_count": blocked,
            "citations_count": len(result.citations),
        },
        citations=result.citations,
    )
    return {
        "result": result.model_dump(mode="json"),
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
        "policy": {
            "blocked_sensitivity": settings.block_sensitivity,
            "policy_profile": policy_profile,
            "retrieval": retrieval_meta,
        },
    }


@app.post("/v1/handoff/resume", response_model=ResumePacketResponse)
def handoff_resume(
    body: ResumePacketRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ResumePacketResponse:
    REQUEST_COUNT.labels(endpoint="handoff_resume", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "resume_packet_enabled", True)):
        raise HTTPException(status_code=404, detail="resume packet endpoint disabled")
    return get_resume_packet(conn, auth=auth, body=body, settings=settings)


@app.post("/v1/completions", response_model=CompletionCaptureResponse)
def capture_completion(
    body: CompletionCaptureRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> CompletionCaptureResponse:
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, conn)
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
    state_row = conn.execute(
        """
        SELECT takeover_context FROM takeover_sessions
        WHERE session_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (body.session_id, auth.workspace_id, auth.user_id),
    ).fetchone()
    session_project: dict[str, Any] | None = None
    if state_row is not None:
        try:
            takeover_context = json.loads(str(state_row["takeover_context"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            takeover_context = {}
        session_project_raw = takeover_context.get("project_context") if isinstance(takeover_context, dict) else None
        session_project = dict(session_project_raw) if isinstance(session_project_raw, dict) and session_project_raw else None
    # Project binding for an ordinary completion (no takeover activation required): explicit
    # app_context => bound, session-bound project => inherited, otherwise visibly unbound.
    explicit_app_context = getattr(body, "app_context", None)
    scope = auth.resolved_scope(
        project_hint=explicit_app_context if isinstance(explicit_app_context, dict) and explicit_app_context else None,
        session_project=session_project,
        task_id=body.session_id,
    )
    if scope.is_bound():
        project_context = canonical_project_context(
            explicit_app_context if scope.project_binding == PROJECT_BOUND and isinstance(explicit_app_context, dict) else None,
            session_project,
        )
        if project_context:
            milestone["project_context"] = project_context
    milestone["project_binding"] = scope.project_binding
    now = datetime.now(tz=UTC)
    try:
        outbox = enqueue_handoff(
            conn,
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
            executor_id=auth.consumer,
            payload_hash=completion_payload_fingerprint(milestone),
        )
    except CompletionConflictError as exc:
        conn.rollback()
        write_audit(
            conn,
            consumer=auth.consumer,
            action="completion_conflict",
            query={"completion_key": body.completion_key, "session_id": body.session_id, "outbox_id": str(exc.outbox_id)},
            result_event_ids=[],
            policy_decisions={"reason": "idempotency_conflict"},
            latency_ms=0,
        )
        conn.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "idempotency_conflict",
                "completion_key": body.completion_key,
                "outbox_id": str(exc.outbox_id),
                "message": "same completion_key with a different payload",
            },
        ) from exc
    conn.commit()
    delivered = deliver_handoff_safely(
        conn,
        outbox_id=str(outbox["id"]),
        retention_days=int(settings.handoff_retention_days),
    )
    record_resume_progress(
        conn,
        workspace_id=auth.workspace_id,
        requesting_owner_id=auth.user_id,
        session_id=body.session_id,
        phase="completed",
        outcome_status=body.state,
        progress_source="complete_task",
    )
    return CompletionCaptureResponse(
        outbox_id=UUID(str(delivered["id"])),
        delivery_status=str(delivered["status"]),
        event_id=UUID(str(delivered["event_id"])),
        handoff_record_id=UUID(str(delivered["handoff_record_id"])),
        contract_valid=True,
        captured_at=datetime.fromisoformat(str(delivered["created_at"])),
        project_binding=scope.project_binding,
    )


@app.post("/v1/continuity/pilot/feedback", response_model=ResumeFeedbackResponse)
def capture_resume_feedback(
    body: ResumeFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ResumeFeedbackResponse:
    _enforce_workspace_access(auth, conn)
    progress = record_resume_progress(
        conn,
        workspace_id=auth.workspace_id,
        requesting_owner_id=auth.user_id,
        packet_id=str(body.packet_id),
        phase=body.phase,
        opened_file=body.opened_file,
        correct_file=body.correct_file,
        correct_anchor=body.correct_anchor,
        opened_file_rank=body.opened_file_rank,
        correction_required=body.correction_required,
        correction_reason=body.correction_reason,
        archaeology_tool_calls=body.archaeology_tool_calls,
        archaeology_tokens=body.archaeology_tokens,
        outcome_status=body.outcome_status,
        progress_source=body.progress_source,
    )
    if progress is None:
        raise HTTPException(status_code=404, detail="resume packet not found")
    now = datetime.now(tz=UTC)
    return ResumeFeedbackResponse(
        packet_id=body.packet_id,
        recorded=True,
        phase=progress[1],
        feedback_at=now,
    )


@app.get("/v1/continuity/pilot/status", response_model=ContinuityPilotStatusResponse)
def continuity_pilot_status(
    days: int = 30,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ContinuityPilotStatusResponse:
    _enforce_workspace_access(auth, conn)
    window_days = max(1, min(365, int(days)))
    return ContinuityPilotStatusResponse(
        window_days=window_days,
        generated_at=datetime.now(tz=UTC),
        **pilot_metrics(conn, workspace_id=auth.workspace_id, days=window_days),
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


@app.get("/v1/governance/status", response_model=GovernanceStatusResponse)
def governance_status(
    _auth: AuthContext = Depends(get_auth_context),
) -> GovernanceStatusResponse:
    return GovernanceStatusResponse(
        **build_governance_status(
            runtime="lite",
            runtime_profile=settings.runtime_profile,
            auth_mode=settings.auth_mode,
            identity_claims_mode=settings.identity_claims_mode,
            workspace_access_mode=settings.workspace_access_mode,
            audit_write_mode=settings.audit_write_mode,
            cors_origins=settings.cors_origins,
            api_tokens=settings.token_set,
            capability_broker_enabled=settings.behavior_capability_broker_enabled,
            mcp_tool_profile=settings.mcp_tool_profile,
            requested_execution_enforcement=settings.execution_enforcement_level,
            execution_interception_attested=settings.execution_interception_attested,
            execution_interception_provider=settings.execution_interception_provider,
        )
    )


@app.get("/v1/context/retrieval/status", response_model=dict)
def context_retrieval_status(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="context_retrieval_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    snapshot = store_context_retrieval_status(settings=settings)
    snapshot["workspace_id"] = auth.workspace_id
    snapshot["user_id"] = auth.user_id
    return snapshot


@app.get("/v1/setup/advisor/providers", response_model=AdvisorProvidersResponse)
def setup_advisor_providers(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorProvidersResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_providers", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
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
        config=_lite_config_response_from_raw(config),
        generated_at=datetime.now(tz=UTC),
    )


@app.get("/v1/setup/advisor/models", response_model=AdvisorModelsResponse)
def setup_advisor_models(
    provider: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorModelsResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_models", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    provider_id = provider.strip().lower()
    adapter = get_provider(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    route = _lite_route_for_provider(config, provider_id)
    api_key = _lite_resolve_route_api_key(
        conn=conn,
        provider_id=provider_id,
        route=route,
        config=config,
    )
    custom_headers = _normalized_custom_headers(config.get("custom_headers"))
    req = ProviderRequest(
        model=str((route or {}).get("model") or config.get("advisor_custom_model") or config.get("advisor_primary_model") or "").strip() or None,
        api_key=api_key or None,
        base_url=str((route or {}).get("base_url") or config.get("advisor_custom_base_url") or _provider_default_base_url(provider_id, adapter.metadata.default_base_url) or "").strip() or None,
        api_version=str((route or {}).get("api_version") or config.get("api_version") or adapter.metadata.api_version or "").strip() or None,
        timeout_ms=int(config.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
        custom_headers=custom_headers,
    )
    try:
        models = adapter.list_models(req)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorModelsResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_models_live", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    provider_id = body.provider_id.strip().lower()
    adapter = get_provider(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    route = _lite_route_for_provider(config, provider_id)
    api_key = _lite_resolve_route_api_key(
        conn=conn,
        provider_id=provider_id,
        route=route,
        config=config,
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
    custom_headers = _normalized_custom_headers(config.get("custom_headers"))
    req = ProviderRequest(
        model=None,
        api_key=api_key or None,
        base_url=str(
            (body.base_url or "").strip()
            or (route or {}).get("base_url")
            or config.get("advisor_custom_base_url")
            or _provider_default_base_url(provider_id, adapter.metadata.default_base_url)
            or ""
        ).strip()
        or None,
        api_version=str((route or {}).get("api_version") or config.get("api_version") or adapter.metadata.api_version or "").strip() or None,
        timeout_ms=int(body.timeout_ms or config.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
        custom_headers=custom_headers,
    )
    try:
        models = adapter.list_models(req)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorVerifyAttempt:
    REQUEST_COUNT.labels(endpoint="setup_advisor_route_verify", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    provider_id = body.provider_id.strip().lower()
    adapter = get_provider(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown advisor provider: {provider_id}")
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    route = _lite_route_for_provider(config, provider_id)
    api_key = _lite_resolve_route_api_key(
        conn=conn,
        provider_id=provider_id,
        route=route,
        config=config,
        explicit_key_ref=body.api_key_ref,
        provided_key=body.api_key,
    )
    custom_headers = _normalized_custom_headers(body.custom_headers or config.get("custom_headers"))
    req = ProviderRequest(
        model=(body.model or "").strip() or str((route or {}).get("model") or "").strip() or None,
        api_key=api_key or None,
        base_url=(body.base_url or "").strip()
        or str(
            (route or {}).get("base_url")
            or config.get("advisor_custom_base_url")
            or _provider_default_base_url(provider_id, adapter.metadata.default_base_url)
            or ""
        ).strip()
        or None,
        api_version=(body.api_version or "").strip() or str((route or {}).get("api_version") or config.get("api_version") or adapter.metadata.api_version or "").strip() or None,
        timeout_ms=int(body.timeout_ms or config.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorLocalOllamaPullResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_local_ollama_pull", method="POST").inc()
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorLocalOllamaPullStatusResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_local_ollama_pull_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorVerifyResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_verify", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    primary = body.provider_id.strip().lower()
    profile = advisor_active_profile(
        {
            "active_profile_id": config.get("active_profile_id"),
            "profiles": config.get("profiles"),
        }
    )
    chain_routes = advisor_resolve_chain_from_profile(
        profile,
        primary_provider=primary,
        fallback_chain=body.fallback_chain or list(config.get("advisor_fallback_chain") or []),
    )
    chain = [str(item.get("provider_id") or "") for item in chain_routes]
    if not chain:
        raise HTTPException(status_code=400, detail="no valid advisor provider in chain")
    timeout_ms = int(body.timeout_ms or config.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms)
    retry_max = max(1, int(body.retry_max or config.get("advisor_provider_retry_max") or settings.advisor_provider_retry_max))
    custom_headers = _normalized_custom_headers(body.custom_headers or config.get("custom_headers"))
    attempts: list[AdvisorVerifyAttempt] = []
    selected_provider = chain[0]
    selected_model = (body.model or "").strip() or None
    used_fallback = False
    health = _lite_load_advisor_health(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
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
        req = ProviderRequest(
            model=selected_model
            or str((route or {}).get("model") or "").strip()
            or (str(config.get("advisor_primary_model") or "").strip() if provider_id == primary else None),
            api_key=_lite_resolve_route_api_key(
                conn=conn,
                provider_id=provider_id,
                route=route if isinstance(route, dict) else None,
                config=config,
                explicit_key_ref=body.api_key_ref if provider_id == primary else None,
                provided_key=body.api_key if provider_id == primary else None,
            ),
            base_url=((body.base_url or "").strip() if provider_id in {primary, "custom"} else "")
            or str((route or {}).get("base_url") or config.get("advisor_custom_base_url") or _provider_default_base_url(provider_id, adapter.metadata.default_base_url) or "").strip()
            or None,
            api_version=(body.api_version or "").strip()
            or str((route or {}).get("api_version") or config.get("api_version") or adapter.metadata.api_version or "").strip()
            or None,
            timeout_ms=max(200, timeout_ms),
            custom_headers=custom_headers,
        )
        result: ProviderAttemptResult | None = None
        for _ in range(retry_max):
            result = adapter.verify(req)
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
                "model": req.model,
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
            used_fallback = index > 0
            selected_provider = provider_id
            if not selected_model and result.models:
                selected_model = result.models[0]
            _lite_save_advisor_health(conn, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
            conn.commit()
            return AdvisorVerifyResponse(
                ok=True,
                selected_provider=selected_provider,
                selected_model=selected_model,
                used_fallback=used_fallback,
                attempts=attempts,
            )
    _lite_save_advisor_health(conn, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
    conn.commit()
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorConfigResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_config", method="PUT").inc()
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
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
    key_ref = (body.advisor_custom_api_key_ref or settings.advisor_custom_api_key_ref).strip()
    payload: dict[str, Any] = {
        "advisor_primary_provider": primary,
        "advisor_primary_model": (body.advisor_primary_model or "").strip() or None,
        "advisor_fallback_chain": fallback,
        "advisor_custom_base_url": (body.advisor_custom_base_url or "").strip() or None,
        "advisor_custom_model": (body.advisor_custom_model or "").strip() or None,
        "advisor_custom_api_key_ref": key_ref or None,
        "advisor_provider_timeout_ms": max(200, int(body.advisor_provider_timeout_ms)),
        "advisor_provider_retry_max": max(1, int(body.advisor_provider_retry_max)),
        "custom_headers": custom_headers,
        "api_version": (body.api_version or "").strip() or None,
        "key_storage_backend": None,
        "key_present": False,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    routes_in: list[dict[str, Any]] = [
        item.model_dump(mode="json")
        for item in body.routes
    ] if body.routes else [
        {
            "provider_id": primary,
            "model": (body.advisor_primary_model or "").strip() or None,
            "api_key_ref": key_ref or None,
            "base_url": (body.advisor_custom_base_url or "").strip() or None,
            "api_version": (body.api_version or "").strip() or None,
            "region_hint": None,
            "priority": 0,
        },
        *[
            {
                "provider_id": provider_id,
                "model": None,
                "api_key_ref": key_ref or None,
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
            if provider_id == primary and key_ref:
                route_key_ref = key_ref
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
        _lite_runtime_setting_json(conn, _lite_advisor_profiles_key(auth.workspace_id, auth.user_id)),
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
    active_routes = payload.get("routes")
    for route in active_routes if isinstance(active_routes, list) else []:
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
            primary_model=str(payload.get("advisor_primary_model") or "") or None,
            fallback_chain=[
                str(item)
                for item in payload.get("advisor_fallback_chain", [])
                if isinstance(item, str)
            ],
            custom_base_url=str(payload.get("advisor_custom_base_url") or "") or None,
            custom_model=str(payload.get("advisor_custom_model") or "") or None,
            custom_api_key_ref=str(payload.get("advisor_custom_api_key_ref") or "") or None,
            provider_timeout_ms=int(payload.get("advisor_provider_timeout_ms") or settings.advisor_provider_timeout_ms),
            provider_retry_max=int(payload.get("advisor_provider_retry_max") or settings.advisor_provider_retry_max),
            api_key=body.api_key,
            api_key_ref=key_ref,
            routes=[
                dict(item)
                for item in payload.get("routes", [])
                if isinstance(item, dict)
            ],
            active_profile_id=str(payload.get("active_profile_id") or "") or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="failed to persist advisor config to .env") from exc
    _lite_purge_advisor_secret_runtime_settings(conn)
    _lite_upsert_runtime_setting(conn, _lite_advisor_setup_key(auth.workspace_id, auth.user_id), payload)
    _lite_upsert_runtime_setting(conn, _lite_advisor_profiles_key(auth.workspace_id, auth.user_id), profile_bundle)
    conn.commit()
    return _lite_config_response_from_raw(payload)


@app.post("/v1/setup/advisor/switch", response_model=AdvisorSwitchResponse)
def setup_advisor_switch(
    body: AdvisorSwitchRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorSwitchResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_switch", method="POST").inc()
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    bundle = advisor_normalize_profile_bundle(
        _lite_runtime_setting_json(conn, _lite_advisor_profiles_key(auth.workspace_id, auth.user_id)),
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
        _lite_upsert_runtime_setting(conn, _lite_advisor_profiles_key(auth.workspace_id, auth.user_id), bundle)
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
        except Exception as exc:
            raise HTTPException(status_code=500, detail="failed to persist active advisor profile to .env") from exc
        _lite_upsert_runtime_setting(conn, _lite_advisor_setup_key(auth.workspace_id, auth.user_id), config)
        conn.commit()
    return AdvisorSwitchResponse(
        ok=True,
        active_profile_id=profile_id,
        message="dry_run" if body.dry_run else "switched",
        switched_at=datetime.now(tz=UTC),
    )


@app.get("/v1/setup/advisor/runtime/status", response_model=AdvisorRuntimeStatusResponse)
def setup_advisor_runtime_status(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorRuntimeStatusResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_runtime_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    profile = _lite_normalized_advisor_runtime_profile(config)
    health = _lite_load_advisor_health(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
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
        advisor_total_budget_ms=_safe_int(
            status.get("advisor_total_budget_ms")
            or getattr(settings, "effective_advisor_total_budget_ms", settings.advisor_total_budget_ms)
        ),
        advisor_attempt_timeout_ms=_safe_int(
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
    conn: sqlite3.Connection = Depends(get_db),
) -> AdvisorRuntimeProbeResponse:
    REQUEST_COUNT.labels(endpoint="setup_advisor_runtime_probe", method="POST").inc()
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    config = _lite_load_advisor_config(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    profile = _lite_normalized_advisor_runtime_profile(config)
    if body.profile_id:
        wanted = str(body.profile_id).strip()
        for item in config.get("profiles", []):
            if isinstance(item, dict) and str(item.get("profile_id") or "") == wanted:
                profile = _lite_normalized_advisor_runtime_profile(config, item)
                break
    routes = list(profile.get("routes") or [])[: max(1, int(body.max_probes))]
    attempts: list[AdvisorVerifyAttempt] = []
    health = _lite_load_advisor_health(conn, workspace_id=auth.workspace_id, user_id=auth.user_id)
    timeout_ms = int(body.timeout_ms or settings.advisor_attempt_timeout_ms)
    probe_deadline = time.monotonic() + (
        max(1.0, (max(200, timeout_ms) * max(1, len(routes)) / 1000.0) + 0.75)
    )
    custom_headers = _normalized_custom_headers(config.get("custom_headers"))
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
        provider_api_key = _lite_resolve_route_api_key(
            conn=conn,
            provider_id=provider_id,
            route=route,
            config=config,
        )
        req = ProviderRequest(
            model=str(route.get("model") or "").strip() or None,
            api_key=provider_api_key,
            base_url=str(route.get("base_url") or adapter.metadata.default_base_url or "").strip() or None,
            api_version=str(route.get("api_version") or adapter.metadata.api_version or "").strip() or None,
            timeout_ms=max(200, timeout_ms),
            custom_headers=custom_headers,
        )
        prepared_routes[idx] = {
            "provider_id": provider_id,
            "adapter": adapter,
            "request": req,
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
        provider_id = str(prepared_routes[idx]["provider_id"])
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

    for idx, _raw_route in enumerate(routes):
        if time.monotonic() > probe_deadline:
            route_budget = _raw_route if isinstance(_raw_route, dict) else {}
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
        prepared_route = prepared_routes.get(idx)
        if not prepared_route:
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
        provider_id = str(prepared_route["provider_id"])
        adapter = prepared["adapter"]
        req = prepared["request"]
        result = verify_results.get(idx) or ProviderAttemptResult(
            provider_id=provider_id,
            ok=False,
            code="provider_error",
            message="provider verify returned no result",
            latency_ms=0,
            models=[],
        )
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
            route={"provider_id": provider_id, "model": req.model, "priority": idx, "provider_category": adapter.metadata.provider_category},
            ok=result.ok,
            latency_ms=result.latency_ms,
            error_code=result.code if not result.ok else None,
            open_failures=settings.advisor_circuit_open_failures,
            half_open_seconds=settings.advisor_circuit_half_open_seconds,
            close_successes=settings.advisor_circuit_close_successes,
        )
    _lite_save_advisor_health(conn, workspace_id=auth.workspace_id, user_id=auth.user_id, routes=health)
    conn.commit()
    return AdvisorRuntimeProbeResponse(
        ok=all(item.ok for item in attempts) if attempts else False,
        profile_id=str(profile.get("profile_id") or "default"),
        attempts=attempts,
        generated_at=datetime.now(tz=UTC),
    )


@app.post("/v1/context_bundle", response_model=ContextBundleResponse)
def get_context_bundle(
    body: ContextBundleRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ContextBundleResponse:
    REQUEST_COUNT.labels(endpoint="context_bundle", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    start = time.perf_counter()
    scope = auth.resolved_scope(
        project_hint=body.app_context if isinstance(body.app_context, dict) else None,
        target_owner=getattr(body, "target_owner", None),
        continuity_intent=bool(getattr(body, "continuity_intent", False)),
    )
    bundle, blocked = context_bundle(
        conn, body, settings, workspace_id=auth.workspace_id, owner_id=auth.user_id, scope=scope
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    REQUEST_LATENCY.labels(endpoint="context_bundle", method="POST").observe(latency_ms / 1000.0)
    write_audit(
        conn,
        consumer=auth.consumer,
        action="get_context_bundle",
        query=body.model_dump(mode="json"),
        result_event_ids=bundle.citations,
        policy_decisions={"blocked": blocked, "cold_start": bundle.policy.get("cold_start", False)},
        latency_ms=latency_ms,
    )
    _auto_capture_interaction(
        conn=conn,
        auth=auth,
        action="context_bundle",
        request_payload=body.model_dump(mode="json"),
        response_payload={
            "citations_count": len(bundle.citations),
            "blocked_count": blocked,
            "cold_start": bool(bundle.policy.get("cold_start", False)),
        },
        citations=bundle.citations,
    )
    return bundle


@app.post("/v1/events/annotate", response_model=EventAnnotationResponse)
def annotate_event(
    body: EventAnnotationRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> EventAnnotationResponse:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="events_annotate", method="POST").inc()
    result = store_annotate_event(
        conn,
        event_id=body.event_id,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        goal=body.goal,
        decision=body.decision,
        alternatives=body.alternatives,
        constraints=body.constraints,
        avoid=body.avoid,
        authority_level=body.authority_level,
    )
    return EventAnnotationResponse.model_validate(result)


@app.get("/v1/episodes", response_model=EpisodeListResponse)
def episodes(
    session_id: str = "default",
    status: str | None = None,
    limit: int = 100,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> EpisodeListResponse:
    REQUEST_COUNT.labels(endpoint="episodes_list", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    result = store_list_episodes(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
        status=status,
        limit=limit,
    )
    return EpisodeListResponse.model_validate(result)


@app.get("/v1/episodes/{episode_id}", response_model=EpisodeItem)
def episode_details(
    episode_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> EpisodeItem:
    REQUEST_COUNT.labels(endpoint="episodes_get", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    result = store_get_episode(
        conn,
        episode_id=episode_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return EpisodeItem.model_validate(result)


@app.post("/v1/context/brief", response_model=ContextBriefResponse)
def context_brief(
    body: ContextBriefRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ContextBriefResponse:
    REQUEST_COUNT.labels(endpoint="context_brief", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    scope = auth.resolved_scope(
        project_hint=body.app_context if isinstance(body.app_context, dict) else None,
        task_id=body.session_id,
    )
    result = store_context_brief(
        conn,
        settings=settings,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        task=body.task,
        session_id=body.session_id,
        app_context=body.app_context,
        constraints=body.constraints,
        max_items=body.max_items,
        scope=scope,
    )
    return ContextBriefResponse.model_validate(result)


@app.get("/v1/memory/rules", response_model=MemoryRuleListResponse)
def memory_rules(
    include_inactive: bool = False,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> MemoryRuleListResponse:
    REQUEST_COUNT.labels(endpoint="memory_rules_list", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    result = store_list_memory_rules(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        include_inactive=include_inactive,
    )
    return MemoryRuleListResponse.model_validate(result)


@app.post("/v1/memory/rules", response_model=MemoryRuleItem)
def upsert_memory_rule(
    body: MemoryRuleUpsertRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> MemoryRuleItem:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="memory_rules_upsert", method="POST").inc()
    result = store_upsert_memory_rule(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        scope=body.scope,
        rule_type=body.rule_type,
        statement=body.statement,
        priority=body.priority,
        evergreen=body.evergreen,
        expires_at=body.expires_at,
        source_episode_id=body.source_episode_id,
    )
    return MemoryRuleItem.model_validate(result)


@app.post("/v1/memory/rules/{rule_id}/deprecate", response_model=MemoryRuleDeprecateResponse)
def deprecate_memory_rule(
    rule_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> MemoryRuleDeprecateResponse:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="memory_rules_deprecate", method="POST").inc()
    result = store_deprecate_memory_rule(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        rule_id=rule_id,
    )
    return MemoryRuleDeprecateResponse.model_validate(result)


@app.post("/v1/memory/forget", response_model=MemoryForgetResponse)
def forget_memory(
    body: MemoryForgetRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> MemoryForgetResponse:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="memory_forget", method="POST").inc()
    result = store_forget_memory(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        requested_by=auth.consumer,
        target_type=body.target_type,
        target_ids=body.target_ids,
        reason=body.reason,
        hard_delete=body.hard_delete,
    )
    return MemoryForgetResponse.model_validate(result)


@app.post("/v1/retrieval/eval/run", response_model=RetrievalEvalRunResponse)
def retrieval_eval_run(
    body: RetrievalEvalRunRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> RetrievalEvalRunResponse:
    REQUEST_COUNT.labels(endpoint="retrieval_eval_run", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    result = store_run_retrieval_eval(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=body.session_id,
        tasks=body.tasks,
        with_brief=body.with_brief,
        settings=settings,
    )
    return RetrievalEvalRunResponse.model_validate(result)


@app.get("/v1/retrieval/eval/status", response_model=RetrievalEvalStatusResponse)
def retrieval_eval_status(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> RetrievalEvalStatusResponse:
    REQUEST_COUNT.labels(endpoint="retrieval_eval_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    result = store_retrieval_eval_status(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )
    return RetrievalEvalStatusResponse.model_validate(result)


@app.get("/v1/summary/activity", response_model=ActivitySummaryResponse)
def get_activity_summary(
    period: str = "today",
    domain: str | None = None,
    max_events: int = 400,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ActivitySummaryResponse:
    REQUEST_COUNT.labels(endpoint="activity_summary", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    result = activity_summary(
        conn=conn,
        settings=settings,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        period=period,
        domain=domain,
        max_events=max_events,
        scope=auth.resolved_scope(),
    )
    write_audit(
        conn,
        consumer=auth.consumer,
        action="get_activity_summary",
        query={"period": period, "domain": domain, "max_events": max_events},
        result_event_ids=result["citations"],
        policy_decisions=result["policy"],
        latency_ms=0,
    )
    return ActivitySummaryResponse.model_validate(result)


@app.get("/v1/dashboard/resource-usage", response_model=dict)
def get_resource_usage(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    REQUEST_COUNT.labels(endpoint="resource_usage", method="GET").inc()
    _enforce_workspace_access(auth, conn)

    db_path = Path(settings.lite_db_path)
    db_size_bytes: int | None = None
    db_error: str | None = None
    try:
        db_size_bytes = db_path.stat().st_size if db_path.exists() else None
        if db_size_bytes is None:
            db_error = f"sqlite database file not found at {db_path}"
    except Exception as exc:
        db_error = str(exc)

    services, docker_meta = _docker_service_stats()
    return {
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "database": {
            "engine": "sqlite",
            "path": str(db_path),
            "size_bytes": db_size_bytes,
            "size_pretty": _bytes_to_human(db_size_bytes),
            "available": db_size_bytes is not None,
            "error": db_error,
        },
        "docker": docker_meta,
        "services": services,
    }


@app.get("/v1/patterns", response_model=list[PatternItem])
def get_patterns(
    domain: str | None = None,
    min_confidence: float = 0.5,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[PatternItem]:
    REQUEST_COUNT.labels(endpoint="patterns_get", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    return list_patterns(
        conn,
        domain=domain,
        min_confidence=min_confidence,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        limit=100,
        scope=auth.resolved_scope(),
    )


@app.post("/v1/patterns/feedback", response_model=dict)
def patterns_feedback(
    body: PatternFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="patterns_feedback", method="POST").inc()
    return submit_pattern_feedback(conn, body)


@app.get("/v1/graph/health/status", response_model=dict)
def graph_health_status(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="graph_health_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    return store_graph_health_status(
        conn,
        settings,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        refresh=True,
    )


@app.get("/v1/graph/entities", response_model=GraphEntitySearchResponse)
def graph_entities(
    query: str,
    k: int = 20,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> GraphEntitySearchResponse:
    REQUEST_COUNT.labels(endpoint="graph_entities", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    entities = search_entities(
        conn=conn,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        query=query,
        limit=k,
    )
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
    conn: sqlite3.Connection = Depends(get_db),
) -> GraphEventResponse:
    REQUEST_COUNT.labels(endpoint="graph_event", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    graph = graph_for_event(
        conn=conn,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        event_id=event_id,
    )
    return GraphEventResponse(
        event_id=event_id,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        graph=graph,
    )


@app.get("/v1/team/memberships", response_model=list[dict])
def team_memberships(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    REQUEST_COUNT.labels(endpoint="team_memberships_get", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    return list_team_memberships(conn=conn, workspace_id=auth.workspace_id)


@app.post("/v1/team/memberships", response_model=dict)
def team_membership_upsert(
    body: TeamMembershipUpsertRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="team_memberships_post", method="POST").inc()
    return upsert_team_membership(
        conn=conn,
        workspace_id=auth.workspace_id,
        user_id=body.user_id,
        role=body.role,
        added_by=auth.user_id,
        active=body.active,
    )


@app.post("/v1/clone/advice", response_model=CloneAdviceResponse)
def clone_advice(
    body: CloneAdviceRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
):
    REQUEST_COUNT.labels(endpoint="clone_advice", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    start = time.perf_counter()
    response = build_clone_advice(conn, body, auth, settings)
    latency_ms = int((time.perf_counter() - start) * 1000)
    write_audit(
        conn,
        consumer=auth.consumer,
        action="clone_advice",
        query=body.model_dump(mode="json"),
        result_event_ids=response.citations,
        policy_decisions=response.policy,
        latency_ms=latency_ms,
    )
    _auto_capture_interaction(
        conn=conn,
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


@app.post("/v1/clone/arbitrate", response_model=CloneArbitrationResponse)
def clone_arbitrate(
    body: CloneArbitrationRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> CloneArbitrationResponse:
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="clone_arbitrate", method="POST").inc()
    start = time.perf_counter()
    response = build_clone_arbitration(body)
    latency_ms = int((time.perf_counter() - start) * 1000)
    write_audit(
        conn,
        consumer=auth.consumer,
        action="clone_arbitrate",
        query=body.model_dump(mode="json"),
        result_event_ids=response.citations,
        policy_decisions={"decision_source": response.decision_source},
        latency_ms=latency_ms,
    )
    return response


@app.get("/v1/takeover/state", response_model=TakeoverState)
def get_takeover_state(
    session_id: str,
    persona_mode: str = "normal",
    activation_keywords: str | None = None,
    stop_keywords: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverState:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_state", method="GET").inc()
    return store_takeover_state(
        conn=conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverState:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_reset", method="POST").inc()
    state = store_reset_takeover_state(
        conn=conn,
        session_id=session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )
    invalidate_takeover_goal_cache(conn, auth=auth, session_id=session_id)
    return state


@app.post("/v1/takeover/goals/discover", response_model=TakeoverGoalsResponse)
def takeover_goals_discover(
    body: TakeoverGoalsDiscoverRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverGoalsResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_goals_discover", method="POST").inc()
    goals = discover_takeover_goals(
        conn,
        auth=auth,
        session_id=body.session_id,
        include_open_discovery=body.include_open_discovery,
        settings=settings,
    )
    return TakeoverGoalsResponse(session_id=body.session_id, goals=goals)


@app.post("/v1/takeover/goals/precompute", response_model=TakeoverGoalsResponse)
def takeover_goals_precompute(
    body: TakeoverGoalsPrecomputeRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverGoalsResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_goals_precompute", method="POST").inc()
    goals = discover_takeover_goals(
        conn,
        auth=auth,
        session_id=body.session_id,
        include_open_discovery=body.include_open_discovery,
        settings=settings,
        force_recompute=body.force_recompute,
    )
    return TakeoverGoalsResponse(session_id=body.session_id, goals=goals)


@app.get("/v1/takeover/goals", response_model=TakeoverGoalsResponse)
def takeover_goals_list(
    session_id: str,
    status: AutonomyGoalStatus | None = None,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverGoalsResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_goals_list", method="GET").inc()
    goals = list_takeover_goals(conn, auth=auth, session_id=session_id, status=status)
    return TakeoverGoalsResponse(session_id=session_id, goals=goals)


@app.get("/v1/takeover/goals/cache/status", response_model=TakeoverGoalCacheStatusResponse)
def takeover_goals_cache_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverGoalCacheStatusResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_goals_cache_status", method="GET").inc()
    payload = store_takeover_goal_cache_status(conn, auth=auth, session_id=session_id)
    return TakeoverGoalCacheStatusResponse.model_validate(payload)


@app.post("/v1/takeover/goals/cache/invalidate", response_model=dict)
def takeover_goals_cache_invalidate(
    body: TakeoverGoalCacheInvalidateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_goals_cache_invalidate", method="POST").inc()
    return invalidate_takeover_goal_cache(
        conn,
        auth=auth,
        session_id=body.session_id,
        objective_hash_value=body.objective_hash,
    )


@app.post("/v1/takeover/goals/{goal_id}/select", response_model=TakeoverGoal)
def takeover_goal_select(
    goal_id: UUID,
    body: TakeoverGoalSelectRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverGoal:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_goal_select", method="POST").inc()
    return select_takeover_goal(conn, auth=auth, session_id=body.session_id, goal_id=goal_id)


@app.post("/v1/takeover/permit", response_model=ExecutionPermitResponse)
def takeover_request_permit(
    body: ExecutionPermitRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ExecutionPermitResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_permit_request", method="POST").inc()
    state = store_takeover_state(
        conn=conn,
        session_id=body.session_id,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
    )
    return request_execution_permit_lite(
        conn,
        auth=auth,
        body=body,
        policy_profile=state.autonomy_policy_profile,
        permit_ttl_seconds=settings.takeover_permit_ttl_seconds,
        confirm_keyword=settings.takeover_confirm_keyword,
    )


@app.post("/v1/takeover/permit/resolve", response_model=ExecutionPermitResponse)
def takeover_resolve_permit(
    body: ExecutionPermitResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ExecutionPermitResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_permit_resolve", method="POST").inc()
    return resolve_execution_permit_lite(
        conn,
        auth=auth,
        body=body,
        confirm_keyword=settings.takeover_confirm_keyword,
    )


@app.get("/v1/takeover/autonomy/status", response_model=TakeoverAutonomyStatusResponse)
def takeover_autonomy_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverAutonomyStatusResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_status", method="GET").inc()
    return store_takeover_autonomy_status(conn, auth=auth, session_id=session_id)


@app.get("/v1/takeover/autonomy/readiness", response_model=dict)
def takeover_autonomy_readiness(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_readiness", method="GET").inc()
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    return store_autonomy_readiness(
        conn,
        settings=settings,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )


@app.get("/v1/takeover/autonomy/project-kpis", response_model=dict)
def takeover_autonomy_project_kpis(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_project_kpis", method="GET").inc()
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    return store_autonomy_project_kpis(
        conn,
        settings=settings,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )


@app.post("/v1/takeover/autonomy/tick", response_model=TakeoverAutonomyTickResponse)
def takeover_autonomy_tick(
    body: TakeoverAutonomyTickRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverAutonomyTickResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_autonomy_tick", method="POST").inc()
    return store_takeover_autonomy_tick(conn, auth=auth, body=body, settings=settings)


@app.get("/v1/takeover/notices", response_model=TakeoverNoticesResponse)
def takeover_notices(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverNoticesResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_notices", method="GET").inc()
    return store_list_takeover_notices(conn, auth=auth, session_id=session_id)


@app.post("/v1/takeover/notices/{notice_id}/ack", response_model=AutonomyNotice)
def takeover_notice_ack(
    notice_id: UUID,
    body: TakeoverNoticeAckRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> AutonomyNotice:
    _enforce_workspace_access(auth, conn)
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    REQUEST_COUNT.labels(endpoint="takeover_notice_ack", method="POST").inc()
    return store_acknowledge_takeover_notice(conn, auth=auth, notice_id=notice_id, body=body)


@app.post("/v1/takeover/execution/claim", response_model=DirectiveExecution)
def takeover_execution_claim(
    body: ExecutionClaimRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> DirectiveExecution:
    _enforce_workspace_access(auth, conn)
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    REQUEST_COUNT.labels(endpoint="takeover_execution_claim", method="POST").inc()
    return store_claim_execution(conn, auth=auth, body=body, settings=settings)


@app.post("/v1/takeover/execution/report", response_model=dict)
def takeover_execution_report(
    body: ExecutionReportRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _enforce_workspace_access(auth, conn)
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    REQUEST_COUNT.labels(endpoint="takeover_execution_report", method="POST").inc()
    return store_report_execution(conn, auth=auth, body=body, settings=settings)


@app.get("/v1/takeover/execution/status", response_model=ExecutionStatusResponse)
def takeover_execution_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ExecutionStatusResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_execution_status", method="GET").inc()
    return store_execution_status(conn, auth=auth, session_id=session_id)


@app.get("/v1/workflow/templates")
def workflow_templates_list(
    session_id: str = "default",
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="workflow_templates_list", method="GET").inc()
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    cap = max(1, min(100, int(limit or 20)))
    domain = f"{auth.workspace_id}:takeover"
    rows = conn.execute(
        """
        SELECT id, name, domain, graph, triggers, version, updated_at
        FROM workflow_templates
        WHERE domain = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (domain, cap),
    ).fetchall()
    templates: list[dict[str, Any]] = []
    for row in rows:
        graph = json_loads(row["graph"], {})
        triggers = json_loads(row["triggers"], {})
        templates.append(
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "domain": str(row["domain"]),
                "graph": graph if isinstance(graph, dict) else {},
                "triggers": triggers if isinstance(triggers, dict) else {},
                "version": int(row["version"] or 1),
                "updated_at": row["updated_at"],
            }
        )
    return {
        "session_id": effective_session_id,
        "templates": templates,
        "total": len(templates),
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


@app.post("/v1/takeover/step", response_model=TakeoverStepResponse)
def takeover_step(
    body: TakeoverStepRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverStepResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_step", method="POST").inc()
    started = time.perf_counter()
    response = store_takeover_step(conn=conn, body=body, auth=auth, settings=settings)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    TAKEOVER_TOTAL_MS.observe(elapsed_ms / 1000)
    TAKEOVER_FAST_PATH_MS.observe(max(0, elapsed_ms - int(response.latency_breakdown_ms.retrieval)) / 1000)
    if response.decision_source.value == "deliberation":
        TAKEOVER_DELIBERATION_COUNT.inc()
        TAKEOVER_DELIBERATION_MS.observe(int(response.latency_breakdown_ms.retrieval) / 1000)
    if response.needs_human:
        TAKEOVER_NEEDS_HUMAN_COUNT.inc()
    TAKEOVER_CONFIDENCE.observe(response.decision_confidence)
    if response.retrieval_triggered and response.retrieval_reason:
        TAKEOVER_RETRIEVAL_TRIGGER_COUNT.labels(reason=response.retrieval_reason).inc()
    source_label = response.retrieval_source or "none"
    TAKEOVER_RETRIEVAL_SOURCE_COUNT.labels(source=source_label).inc()
    TAKEOVER_RETRIEVAL_LATENCY.labels(source=source_label).observe(
        max(0.0, float(response.retrieval_latency_ms) / 1000.0)
    )
    if response.retrieval_reason == "budget_exceeded_skip_deliberation":
        TAKEOVER_RETRIEVAL_BUDGET_EXCEEDED_COUNT.inc()
    write_audit(
        conn,
        consumer=auth.consumer,
        action="takeover_step",
        query=body.model_dump(mode="json"),
        result_event_ids=response.citations,
        policy_decisions={
            "mode": response.state.mode.value,
            "classification": response.classification.value,
            "safety_decision": response.safety_decision.value,
            "enforced": response.enforced,
            "decision_source": response.decision_source.value,
            "decision_confidence": response.decision_confidence,
            "needs_human": response.needs_human,
            "context_quality_score": response.context_quality_score,
            "retrieval_triggered": response.retrieval_triggered,
            "retrieval_source": response.retrieval_source,
            "retrieval_reason": response.retrieval_reason,
            "retrieval_latency_ms": response.retrieval_latency_ms,
            "retrieval_hit_count": response.retrieval_hit_count,
        },
        latency_ms=elapsed_ms,
    )
    _auto_capture_interaction(
        conn=conn,
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
            "needs_human": response.needs_human,
            "context_quality_score": response.context_quality_score,
            "retrieval_triggered": response.retrieval_triggered,
            "retrieval_source": response.retrieval_source,
            "retrieval_reason": response.retrieval_reason,
            "retrieval_latency_ms": response.retrieval_latency_ms,
            "retrieval_hit_count": response.retrieval_hit_count,
        },
        citations=response.citations,
    )
    return response


@app.post("/v1/takeover/preload", response_model=TakeoverPreloadResponse)
def takeover_preload(
    body: TakeoverPreloadRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverPreloadResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_preload", method="POST").inc()
    return store_takeover_preload(conn=conn, body=body, auth=auth, settings=settings)


@app.post("/v1/takeover/feedback", response_model=TakeoverFeedbackResponse)
def takeover_feedback(
    body: TakeoverFeedbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> TakeoverFeedbackResponse:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="takeover_feedback", method="POST").inc()
    return store_takeover_feedback(conn=conn, body=body, auth=auth, settings=settings)


class LifecycleRunRequest(BaseModel):
    retention_days: int | None = None
    dry_run: bool | None = None


@app.get("/v1/admin/lifecycle/status")
def lifecycle_status(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="lifecycle_status", method="GET").inc()
    return store_lifecycle_status(conn=conn, settings=settings)


@app.post("/v1/admin/lifecycle/run")
def lifecycle_run(
    body: LifecycleRunRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _enforce_workspace_access(auth, conn)
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    REQUEST_COUNT.labels(endpoint="lifecycle_run", method="POST").inc()
    return run_lifecycle_maintenance(
        conn=conn,
        settings=settings,
        retention_days=body.retention_days,
        dry_run=body.dry_run,
    )


# ---------------------------------------------------------------------------
# Observation ingestion (needed for clone system)
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _PydanticBaseModel  # noqa: E402


class IngestObservationsRequest(_PydanticBaseModel):
    observations: list[dict[str, Any]]
    update_fingerprint: bool = True


class IngestObservationsResponse(_PydanticBaseModel):
    ingested: int
    fingerprint_updated: bool


def _rebuild_behavior_fingerprint_lite(
    conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str
) -> None:
    evidence = load_behavior_evidence_lite(
        conn,
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
    save_fingerprint_lite(
        conn,
        consumer_id=subject_user_id,
        workspace_id=workspace_id,
        fingerprint=fingerprint,
        observation_count=len(evidence),
    )


def _store_behavior_evidence_lite_api(
    *,
    body: BehaviorEvidenceRequest,
    auth: AuthContext,
    conn: sqlite3.Connection,
) -> BehaviorEvidenceResponse:
    if not bool(getattr(settings, "behavior_evidence_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior evidence capture is disabled")
    raw = body.model_dump(mode="python")
    # P0: confirmation is never caller-asserted; a verified source-event path (P1) is the only writer.
    raw["confirmed_at"] = None
    # P0: only a human operator can author human-origin evidence. A non-human caller (executor/advisor)
    # is downgraded to inferred, may not supersede prior observations, and is held out of learning as
    # pending_review below regardless of score.
    is_human = auth.role == AgentRole.USER
    if not is_human:
        raw["evidence_source"] = BehaviorEvidenceSource.INFERRED.value
        raw["supersedes_observation_id"] = None
    normalized = normalize_behavior_evidence(raw)
    storage_gate = behavior_storage_gate(
        normalized,
        threshold=float(getattr(settings, "behavior_storage_min_score", 0.55)),
    )
    prior_evidence: list[dict[str, Any]] = []
    shadow_prediction: dict[str, Any] | None = None
    shadow_latency_ms = 0
    if bool(getattr(settings, "behavior_shadow_evaluation_enabled", True)):
        prior_evidence = load_behavior_evidence_lite(
            conn,
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
        storage_gate["learning_eligible"]
        and (
            not is_human
            or (
                getattr(settings, "behavior_memory_review_enabled", True)
                and normalized.get("evidence_source") in {"inferred", "backfill"}
            )
        )
    )
    if review_pending:
        review_reasons = [*list(storage_gate.get("reasons") or []), "human_review_required"]
        if not is_human:
            review_reasons.append("non_human_caller")
        storage_gate = {
            **storage_gate,
            "learning_eligible": False,
            "decision": "pending_review",
            "reasons": review_reasons,
        }
        warnings.append("evidence is pending memory review and cannot influence behavior yet")
    supersedes = normalized.get("supersedes_observation_id")
    if supersedes:
        target_exists = conn.execute(
            """
            SELECT 1 FROM decision_observations
            WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
            """,
            (str(supersedes), auth.workspace_id, auth.behavior_subject_id),
        ).fetchone()
        if target_exists is None:
            raise HTTPException(status_code=404, detail="superseded observation not found in workspace")
    observation_id = save_behavior_evidence_lite(
        conn,
        consumer_id=auth.consumer,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        evidence=normalized,
        storage_gate=storage_gate,
    )
    review_id: str | None = None
    if bool(getattr(settings, "behavior_memory_review_enabled", True)):
        review_status = "pending" if review_pending else ("promoted" if storage_gate["learning_eligible"] else "rejected")
        review_id = create_memory_review(
            conn,
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
    shadow_prediction_id: str | None = None
    if shadow_prediction is not None:
        shadow_prediction_id = save_shadow_prediction(
            conn,
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
        fingerprint_data = load_fingerprint_lite(
            conn,
            consumer_id=auth.behavior_subject_id,
            workspace_id=auth.workspace_id,
        )
        fingerprint = fingerprint_data["fingerprint"] if fingerprint_data else DEFAULT_FINGERPRINT.copy()
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
            conn.execute(
                """
                SELECT COUNT(1) AS c FROM decision_observations
                WHERE workspace_id = ? AND subject_user_id = ? AND learning_eligible = 1
                """,
                (auth.workspace_id, auth.behavior_subject_id),
            ).fetchone()["c"]
        )
        save_fingerprint_lite(
            conn,
            consumer_id=auth.behavior_subject_id,
            workspace_id=auth.workspace_id,
            fingerprint=fingerprint,
            observation_count=observation_count,
        )
    return BehaviorEvidenceResponse(
        observation_id=UUID(observation_id),
        stored=True,
        learning_eligible=bool(storage_gate["learning_eligible"]),
        storage_score=float(storage_gate["score"]),
        storage_decision=str(storage_gate["decision"]),
        storage_reasons=list(storage_gate.get("reasons") or []),
        warnings=warnings,
        redaction_applied=bool(normalized.get("redaction_applied", False)),
        superseded_observation_id=body.supersedes_observation_id,
        review_id=UUID(review_id) if review_id else None,
        shadow_prediction_id=UUID(shadow_prediction_id) if shadow_prediction_id else None,
    )


@app.post("/v1/behavior/evidence", response_model=BehaviorEvidenceResponse)
def record_behavior_evidence(
    body: BehaviorEvidenceRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorEvidenceResponse:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_evidence", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    return _store_behavior_evidence_lite_api(body=body, auth=auth, conn=conn)


def _behavior_projection_lite(
    *,
    auth: AuthContext,
    conn: sqlite3.Connection,
    view: str,
    format_name: BehaviorProjectionFormat,
    topic: str | None = None,
    observation_id: UUID | None = None,
) -> BehaviorProjectionResponse:
    if not bool(getattr(settings, "behavior_projections_enabled", False)):
        raise HTTPException(status_code=404, detail="behavior projections are disabled")
    started = time.perf_counter()
    if observation_id is not None:
        evidence_item = load_behavior_evidence_by_id_lite(
            conn,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            observation_id=str(observation_id),
        )
        if evidence_item is None:
            raise HTTPException(status_code=404, detail="behavior evidence was not found")
        evidence = [evidence_item]
    else:
        evidence = load_behavior_evidence_lite(
            conn,
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
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_current", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    return _behavior_projection_lite(auth=auth, conn=conn, view="current", format_name=format)


@app.get("/v1/behavior/projections/decisions/{topic}", response_model=BehaviorProjectionResponse)
def behavior_decisions_projection(
    topic: str,
    format: BehaviorProjectionFormat = BehaviorProjectionFormat.MARKDOWN,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_decisions", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    if len(topic) > 120 or any(ord(char) < 32 for char in topic):
        raise HTTPException(status_code=422, detail="topic must be 1 to 120 printable characters")
    return _behavior_projection_lite(
        auth=auth,
        conn=conn,
        view="decisions",
        format_name=format,
        topic=topic,
    )


@app.get("/v1/behavior/projections/evidence/{observation_id}", response_model=BehaviorProjectionResponse)
def behavior_evidence_projection(
    observation_id: UUID,
    format: BehaviorProjectionFormat = BehaviorProjectionFormat.JSON,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_evidence", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    return _behavior_projection_lite(
        auth=auth,
        conn=conn,
        view="evidence",
        format_name=format,
        observation_id=observation_id,
    )


@app.get("/v1/behavior/projections/review", response_model=BehaviorProjectionResponse)
def behavior_review_projection(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorProjectionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_projection_review", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    return _behavior_projection_lite(
        auth=auth,
        conn=conn,
        view="review",
        format_name=BehaviorProjectionFormat.HTML,
    )


@app.get("/v1/behavior/projections/review.html", response_class=HTMLResponse)
def behavior_review_projection_html(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    response = behavior_review_projection(auth=auth, conn=conn)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorPilotAssignmentResponse:
    _require_behavior_projection_pilot()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, conn)
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
    evidence = load_behavior_evidence_lite(
        conn,
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
            conn,
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
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorPilotOutcomeResponse:
    _require_behavior_projection_pilot()
    _reject_advisor_writes(auth)
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="behavior_projection_pilot_outcome", method="POST").inc()
    sanitized, redaction_applied = sanitize_behavior_pilot_payload(body.model_dump(mode="json"))
    if not isinstance(sanitized, dict):
        raise HTTPException(status_code=422, detail="invalid behavior pilot outcome")
    digest = behavior_pilot_outcome_digest(sanitized)
    now = datetime.now(tz=UTC)
    evidence = load_behavior_evidence_lite(
        conn,
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
            conn,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            reporter_id=auth.user_id,
            assignment_id=str(body.assignment_id),
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
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorPilotStatusResponse:
    _require_behavior_projection_pilot()
    _enforce_workspace_access(auth, conn)
    REQUEST_COUNT.labels(endpoint="behavior_projection_pilot_status", method="GET").inc()
    rows = list_behavior_pilot_rows(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorPredictionResponse:
    REQUEST_COUNT.labels(endpoint="behavior_predict", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_prediction_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior prediction is disabled")
    evidence = load_behavior_evidence_lite(
        conn,
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
    gate = latest_fidelity_gate_lite(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
    )
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorEvaluationResponse:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_evaluate", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_fidelity_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior fidelity evaluation is disabled")
    config = body.model_dump(mode="json")
    evidence = load_behavior_evidence_lite(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=body.max_cases,
        eligible_only=True,
    )
    result = evaluate_behavior_fidelity(evidence, **body.model_dump(mode="python"))
    run_id, created_at = save_fidelity_run_lite(
        conn,
        consumer_id=auth.consumer,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        config=config,
        result=result,
    )
    return BehaviorEvaluationResponse(
        run_id=UUID(run_id),
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorEvaluationListResponse:
    REQUEST_COUNT.labels(endpoint="behavior_evaluations", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    rows = list_fidelity_runs_lite(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        limit=limit,
    )
    return BehaviorEvaluationListResponse(
        runs=[
            BehaviorEvaluationResponse(
                run_id=UUID(str(row["id"])),
                status=str(row["status"]),
                metrics=dict(row.get("metrics") or {}),
                gate=dict(row.get("gate") or {}),
                case_results=list(row.get("case_results") or []),
                config=dict(row.get("config") or {}),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                duration_ms=int(row.get("duration_ms", 0) or 0),
                schema_version=str(row.get("schema_version") or "v1"),
            )
            for row in rows
        ]
    )


@app.get("/v1/behavior/calibration/scenarios", response_model=BehaviorCalibrationScenariosResponse)
def behavior_calibration_scenarios(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorCalibrationScenariosResponse:
    REQUEST_COUNT.labels(endpoint="behavior_calibration_scenarios", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_calibration_enabled", False)):
        raise HTTPException(status_code=404, detail="behavior calibration is disabled")
    return BehaviorCalibrationScenariosResponse(scenarios=[dict(item) for item in CALIBRATION_SCENARIOS])


@app.post("/v1/behavior/calibration/answer", response_model=BehaviorEvidenceResponse)
def answer_behavior_calibration(
    body: BehaviorCalibrationAnswerRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorEvidenceResponse:
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_calibration_answer", method="POST").inc()
    _enforce_workspace_access(auth, conn)
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
    return _store_behavior_evidence_lite_api(body=evidence_body, auth=auth, conn=conn)


@app.post("/v1/capabilities/grants", response_model=CapabilityGrantResponse)
def capability_grant_create(
    body: CapabilityGrantRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> CapabilityGrantResponse:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="capability_grant_create", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_capability_broker_enabled", True)):
        raise HTTPException(status_code=404, detail="capability broker is disabled")
    payload = body.model_dump(mode="python")
    if body.ttl_seconds == 120:
        payload["ttl_seconds"] = int(getattr(settings, "capability_grant_ttl_seconds", 120))
    response = CapabilityGrantResponse(
        **issue_capability_grant(
            conn,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            body=payload,
        )
    )
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> CapabilityConsumeResponse:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="capability_grant_consume", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_capability_broker_enabled", True)):
        raise HTTPException(status_code=404, detail="capability broker is disabled")
    response = CapabilityConsumeResponse(
        **consume_capability_grant(
            conn,
            workspace_id=auth.workspace_id,
            owner_id=auth.user_id,
            body=body.model_dump(mode="python"),
        )
    )
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> ProcessMiningResponse:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_processes_mine", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_process_mining_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior process mining is disabled")
    rows = load_process_source_rows(
        conn,
        workspace_id=auth.workspace_id,
        owner_id=auth.user_id,
        lookback_days=body.lookback_days,
        limit=body.max_sequences,
    )
    min_support = max(body.min_support, int(getattr(settings, "behavior_process_min_support", 2)))
    models, duration_ms = mine_and_time(rows, min_support=min_support, max_steps=body.max_steps)
    stored = save_process_models(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        models=models,
    )
    if bool(getattr(settings, "behavior_memory_review_enabled", True)):
        for model in stored:
            create_memory_review(
                conn,
                workspace_id=auth.workspace_id,
                subject_user_id=auth.behavior_subject_id,
                target_type="process_model",
                target_id=str(model["process_id"]),
                title=str(model["name"]),
                rationale=f"Observed in {model['support']} sessions with reliability {model['reliability']:.2f}",
                source="process_mining",
                score=float(model["reliability"]),
            )
        stored = list_process_models(
            conn,
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
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> ProcessMiningResponse:
    REQUEST_COUNT.labels(endpoint="behavior_processes", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    models = list_process_models(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorShadowStatusResponse:
    REQUEST_COUNT.labels(endpoint="behavior_shadow_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_shadow_evaluation_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior shadow evaluation is disabled")
    return BehaviorShadowStatusResponse(
        **shadow_status(
            conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> MemoryReviewListResponse:
    REQUEST_COUNT.labels(endpoint="behavior_memory_reviews", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    rows = list_behavior_memory_reviews(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> MemoryReviewItem:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_memory_review_resolve", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    note, _ = redact_control_text(body.note, limit=1000)
    row = resolve_memory_review(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        review_id=str(review_id),
        reviewer_id=auth.user_id,
        decision=body.decision,
        note=note,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="pending memory review not found")
    if body.decision == "promote" and row.get("target_type") == "evidence":
        _rebuild_behavior_fingerprint_lite(
            conn,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
        )
    response = MemoryReviewItem(**row)
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> CounterfactualItem:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_counterfactual_create", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    if not bool(getattr(settings, "behavior_counterfactual_enabled", True)):
        raise HTTPException(status_code=404, detail="behavior counterfactual logging is disabled")
    normalized = normalize_counterfactual(body.model_dump(mode="python"))
    payload = {**body.model_dump(mode="python"), **normalized}
    response = CounterfactualItem(
        **create_counterfactual(
            conn,
            workspace_id=auth.workspace_id,
            subject_user_id=auth.behavior_subject_id,
            owner_id=auth.user_id,
            body=payload,
        )
    )
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> CounterfactualListResponse:
    REQUEST_COUNT.labels(endpoint="behavior_counterfactuals", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    rows = list_counterfactuals(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> CounterfactualItem:
    start = time.perf_counter()
    _reject_advisor_writes(auth)
    REQUEST_COUNT.labels(endpoint="behavior_counterfactual_resolve", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    observed, observed_redacted = redact_control_text(body.observed_outcome, limit=1000)
    lesson, lesson_redacted = redact_control_text(body.lesson, limit=1000)
    row = resolve_counterfactual(
        conn,
        workspace_id=auth.workspace_id,
        subject_user_id=auth.behavior_subject_id,
        counterfactual_id=str(counterfactual_id),
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
    write_audit(
        conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> IngestObservationsResponse:
    REQUEST_COUNT.labels(endpoint="ingest_observations", method="POST").inc()
    _enforce_workspace_access(auth, conn)

    ingested = 0
    for obs in body.observations:
        obs.setdefault("consumer_id", auth.consumer)
        obs.setdefault("workspace_id", auth.workspace_id)
        save_observation_lite(conn, obs)
        ingested += 1

    fingerprint_updated = False
    if body.update_fingerprint and ingested > 0:
        fp_data = load_fingerprint_lite(conn, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
        fp = fp_data["fingerprint"] if fp_data else DEFAULT_FINGERPRINT.copy()
        obs_count = (fp_data["observation_count"] if fp_data else 0) + ingested
        for obs in body.observations:
            fp = merge_observation_into_fingerprint(fp, obs)
        save_fingerprint_lite(
            conn,
            consumer_id=auth.behavior_subject_id,
            workspace_id=auth.workspace_id,
            fingerprint=fp,
            observation_count=obs_count,
        )
        fingerprint_updated = True

    return IngestObservationsResponse(ingested=ingested, fingerprint_updated=fingerprint_updated)


# ---------------------------------------------------------------------------
# Check context (file safety check against past decisions)
# ---------------------------------------------------------------------------


class CheckContextRequest(_PydanticBaseModel):
    file_path: str
    intended_action: str = "edit"
    session_id: str | None = None


class CheckContextResponse(_PydanticBaseModel):
    signal: str
    reason: str
    past_decisions: list[dict[str, Any]]


@app.post("/v1/clone/check-context")
def check_context(
    body: CheckContextRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> CheckContextResponse:
    """Check timeline for past decisions relevant to a file before modifying it."""
    REQUEST_COUNT.labels(endpoint="check_context", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    record_resume_progress(
        conn,
        workspace_id=auth.workspace_id,
        requesting_owner_id=auth.user_id,
        session_id=body.session_id,
        phase="file_opened",
        opened_file=body.file_path,
        progress_source="check_context",
    )

    path_parts = [p for p in body.file_path.replace("\\", "/").split("/") if p]
    search_terms = [body.file_path]
    for i in range(len(path_parts)):
        search_terms.append("/".join(path_parts[: i + 1]))

    like_clauses = " OR ".join(
        "situation_summary LIKE ? OR context_snapshot LIKE ?"
        for _ in search_terms
    )
    params: list[Any] = [auth.workspace_id]
    for term in search_terms:
        params.append(f"%{term}%")
        params.append(f"%{term}%")

    rows = conn.execute(
        f"""
        SELECT situation_summary, user_response, outcome, confidence
        FROM decision_observations
        WHERE workspace_id = ?
          AND ({like_clauses})
        ORDER BY confidence DESC, ts DESC
        LIMIT 5
        """,
        params,
    ).fetchall()

    past_decisions = [
        {
            "situation": row["situation_summary"][:150],
            "decision": row["user_response"][:200],
            "outcome": (row["outcome"] or "")[:100],
        }
        for row in rows
    ]

    if not past_decisions:
        signal = "allow"
        reason = "No past decisions found for this file. Proceed with caution."
    else:
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

    return CheckContextResponse(signal=signal, reason=reason, past_decisions=past_decisions)


# ---------------------------------------------------------------------------
# Dashboard endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/fingerprint", response_model=FingerprintResponse)
def get_fingerprint(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> FingerprintResponse:
    """Return the behavioral fingerprint for the current consumer/workspace."""
    REQUEST_COUNT.labels(endpoint="fingerprint", method="GET").inc()
    _enforce_workspace_access(auth, conn)

    fp_data = load_fingerprint_lite(conn, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
    if fp_data is None:
        # Fall back to any fingerprint in the workspace
        row = conn.execute(
            "SELECT fingerprint, observation_count, last_updated_at FROM behavioral_fingerprints WHERE workspace_id = ?",
            (auth.workspace_id,),
        ).fetchone()
        if row:
            fp_data = {
                "fingerprint": json_loads(row["fingerprint"], {}),
                "observation_count": row["observation_count"],
                "last_updated_at": row["last_updated_at"],
            }

    if fp_data is None:
        return FingerprintResponse(
            fingerprint=DEFAULT_FINGERPRINT,
            observation_count=0,
            last_updated_at=None,
            is_default=True,
        )

    return FingerprintResponse(
        fingerprint=fp_data["fingerprint"],
        observation_count=fp_data["observation_count"],
        last_updated_at=datetime.fromisoformat(fp_data["last_updated_at"]) if fp_data["last_updated_at"] else None,
        is_default=False,
    )


@app.get("/v1/observations", response_model=ObservationListResponse)
def list_observations(
    situation_type: str | None = None,
    outcome_sentiment: str | None = None,
    limit: int = 50,
    offset: int = 0,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> ObservationListResponse:
    """List decision observations with optional filters."""
    REQUEST_COUNT.labels(endpoint="observations", method="GET").inc()
    _enforce_workspace_access(auth, conn)

    clauses = ["workspace_id = ?"]
    params: list[Any] = [auth.workspace_id]
    if situation_type:
        clauses.append("situation_type = ?")
        params.append(situation_type)
    if outcome_sentiment:
        clauses.append("outcome_sentiment = ?")
        params.append(outcome_sentiment)

    # Total count
    total = conn.execute(
        f"SELECT COUNT(1) AS c FROM decision_observations WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()["c"]

    # Paginated rows
    effective_limit = min(limit, 200)
    rows = conn.execute(
        f"""
        SELECT id, ts, situation_type, situation_summary, user_response,
               response_reasoning, outcome, outcome_sentiment, confidence, source_event_ids
        FROM decision_observations
        WHERE {' AND '.join(clauses)}
        ORDER BY ts DESC
        LIMIT ? OFFSET ?
        """,
        params + [effective_limit, offset],
    ).fetchall()

    items = [
        ObservationItem(
            id=UUID(r["id"]),
            ts=datetime.fromisoformat(r["ts"]),
            situation_type=r["situation_type"],
            situation_summary=r["situation_summary"],
            user_response=r["user_response"],
            response_reasoning=r["response_reasoning"],
            outcome=r["outcome"],
            outcome_sentiment=r["outcome_sentiment"],
            confidence=float(r["confidence"]),
            source_event_ids=[UUID(v) for v in json_loads(r["source_event_ids"], [])],
        )
        for r in rows
    ]

    # Situation type distribution (unfiltered for workspace)
    type_rows = conn.execute(
        "SELECT situation_type, COUNT(1) AS c FROM decision_observations WHERE workspace_id = ? GROUP BY situation_type",
        (auth.workspace_id,),
    ).fetchall()
    situation_type_counts = {row["situation_type"]: row["c"] for row in type_rows}

    return ObservationListResponse(
        observations=items,
        total=total,
        situation_type_counts=situation_type_counts,
    )


@app.get("/v1/dashboard/clone-score", response_model=CloneScoreResponse)
def get_clone_score(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> CloneScoreResponse:
    """Calculate clone readiness score (0-100)."""
    REQUEST_COUNT.labels(endpoint="clone_score", method="GET").inc()
    _enforce_workspace_access(auth, conn)

    TOTAL_SITUATION_TYPES = 12

    # 1. Observation count
    obs_count = conn.execute(
        "SELECT COUNT(1) AS c FROM decision_observations WHERE workspace_id = ?",
        (auth.workspace_id,),
    ).fetchone()["c"]
    observation_count_score = min(25, int(obs_count * 25 / 100))

    # 2. Fingerprint confidence — count non-default dimensions
    fp_data = load_fingerprint_lite(conn, consumer_id=auth.behavior_subject_id, workspace_id=auth.workspace_id)
    if fp_data and fp_data["fingerprint"]:
        non_default = 0
        total_dims = 0
        for category, defaults in DEFAULT_FINGERPRINT.items():
            if not isinstance(defaults, dict):
                continue
            for key, default_val in defaults.items():
                total_dims += 1
                stored_cat = fp_data["fingerprint"].get(category, {})
                if isinstance(stored_cat, dict) and stored_cat.get(key) != default_val:
                    non_default += 1
        fingerprint_confidence = int(non_default / max(total_dims, 1) * 25) if total_dims else 0
    else:
        fingerprint_confidence = 0

    # 3. Pattern coverage — distinct situation types with observations
    distinct_types = conn.execute(
        "SELECT COUNT(DISTINCT situation_type) AS c FROM decision_observations WHERE workspace_id = ?",
        (auth.workspace_id,),
    ).fetchone()["c"]
    pattern_coverage = min(25, int(distinct_types / TOTAL_SITUATION_TYPES * 25))

    # 4. Recent consistency — positive+neutral in last 20 observations
    recent_rows = conn.execute(
        "SELECT outcome_sentiment FROM decision_observations WHERE workspace_id = ? ORDER BY ts DESC LIMIT 20",
        (auth.workspace_id,),
    ).fetchall()
    if recent_rows:
        positive_neutral = sum(
            1 for r in recent_rows if r["outcome_sentiment"] in ("positive", "neutral", None)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> SystemStatusResponse:
    """Aggregate system status: API, DB, runtime mode. Lite has no Redis/Ollama."""
    REQUEST_COUNT.labels(endpoint="system_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)

    # API uptime
    uptime = int(time.monotonic() - _APP_START_TIME)
    api_info = ApiStatusInfo(status="ok", uptime_seconds=uptime)

    # Database counts
    try:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(1) FROM events) AS event_count,
                (SELECT COUNT(1) FROM decision_observations) AS obs_count,
                (SELECT COUNT(1) FROM patterns) AS pattern_count
            """
        ).fetchone()
        db_info = DatabaseStatusInfo(
            connected=True,
            event_count=int(counts["event_count"]) if counts else 0,
            observation_count=int(counts["obs_count"]) if counts else 0,
            pattern_count=int(counts["pattern_count"]) if counts else 0,
            embedding_count=0,  # Lite has no embeddings
        )
    except Exception:
        db_info = DatabaseStatusInfo(connected=False)

    # Redis — not available in lite
    redis_info = ServiceStatusInfo(connected=False)

    # Ollama — not available in lite
    ollama_info = ServiceStatusInfo(connected=False)

    # Runtime mode
    try:
        mode_cfg = runtime_mode(conn, settings)
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
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardClientConfigResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_client_config", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    mode_cfg = runtime_mode(conn, settings)
    mode_info = RuntimeModeInfo(mode=mode_cfg.mode.value, clone_enabled=mode_cfg.clone_enabled)
    default_workspace_id = os.getenv("TCE_MCP_WORKSPACE_ID", auth.workspace_id or "personal").strip() or "personal"
    default_user_id = os.getenv("TCE_MCP_EXECUTOR_USER_ID", auth.user_id or "local-user").strip() or "local-user"
    default_consumer_id = os.getenv("TCE_MCP_EXECUTOR_CONSUMER_ID", auth.consumer or "dashboard").strip() or "dashboard"
    requested_default_session_id = _normalize_session_id_value(os.getenv("TCE_MCP_SESSION_ID", ""))
    default_session_id = _resolve_effective_session_id(
        conn,
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
            "executor": DashboardIdentityInfo(user_id=executor_id, label=_agent_label(executor_id, "Executor")),
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
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardStackRestartResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_stack_restart", method="POST").inc()
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")
    _enforce_workspace_access(auth, conn)
    requested = (body.stack or "lite").strip().lower() or "lite"
    stack = requested if requested in {"full", "lite", "all", "auto"} else "lite"
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
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardStackRestartStatusResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_stack_restart_status", method="GET").inc()
    _enforce_workspace_access(auth, conn)
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
        started_at=_parse_dt(job.get("started_at")) or datetime.now(tz=UTC),
        completed_at=_parse_dt(job.get("completed_at")),
        error=(str(job.get("error")) if job.get("error") else None),
    )


@app.put("/v1/dashboard/client-config/executor", response_model=dict)
def dashboard_update_executor_config(
    body: DashboardExecutorConfigUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    REQUEST_COUNT.labels(endpoint="dashboard_update_executor_config", method="PUT").inc()
    _enforce_workspace_access(auth, conn)
    if auth.role == AgentRole.ADVISOR:
        raise HTTPException(status_code=403, detail="advisor role is read-only")

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
    except Exception as exc:
        raise HTTPException(status_code=500, detail="failed to persist executor config to .env") from exc

    return {
        "ok": True,
        "message": "saved_to_env_restart_required",
        "restart_required": True,
        "updated_keys": sorted(updates.keys()),
    }


@app.get("/v1/dashboard/agent-roles", response_model=DashboardAgentRolesResponse)
def dashboard_agent_roles(
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardAgentRolesResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_agent_roles", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    now = datetime.now(tz=UTC)
    mode_cfg = runtime_mode(conn, settings)
    mode_info = RuntimeModeInfo(mode=mode_cfg.mode.value, clone_enabled=mode_cfg.clone_enabled)

    memberships = list_team_memberships(conn=conn, workspace_id=auth.workspace_id)
    executor_user_id = os.getenv("TCE_MCP_EXECUTOR_USER_ID", "codex-executor").strip() or "codex-executor"
    secondary_executor_user_id = os.getenv("TCE_MCP_SECONDARY_USER_ID", "claude-executor").strip() or "claude-executor"
    executor_consumer_id = os.getenv("TCE_MCP_EXECUTOR_CONSUMER_ID", executor_user_id).strip() or executor_user_id
    secondary_executor_consumer_id = (
        os.getenv("TCE_MCP_SECONDARY_CONSUMER_ID", secondary_executor_user_id).strip() or secondary_executor_user_id
    )

    executor = _resolve_dashboard_role(
        conn,
        workspace_id=auth.workspace_id,
        role="executor",
        user_id=executor_user_id,
        consumer_id=executor_consumer_id,
        memberships=memberships,
        now=now,
    )
    secondary_executor = _resolve_dashboard_role(
        conn,
        workspace_id=auth.workspace_id,
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
        workspace_id=auth.workspace_id,
        runtime_mode=mode_info,
        executor=executor,
        advisor=secondary_executor,
        secondary_executor=secondary_executor,
        generated_at=now,
    )


@app.get("/v1/dashboard/goals/intelligence", response_model=DashboardGoalsIntelligenceResponse)
def dashboard_goals_intelligence(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardGoalsIntelligenceResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_goals_intelligence", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    with DASHBOARD_GOAL_INTELLIGENCE_MS.time():
        return _build_goal_intelligence_lite(
            conn,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=effective_session_id,
        )


@app.get("/v1/dashboard/human-score", response_model=DashboardHumanScoreResponse)
def dashboard_human_score(
    session_id: str = "default",
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardHumanScoreResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_human_score", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    cached = _latest_human_score_snapshot_lite(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=effective_session_id,
    )
    if cached and (datetime.now(tz=UTC) - cached.computed_at) <= timedelta(hours=1):
        return cached

    with DASHBOARD_HUMAN_SCORE_MS.time():
        computed = _compute_human_score_lite(
            conn,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=effective_session_id,
        )
        computed.snapshot_id = _persist_human_score_snapshot_lite(
            conn,
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
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardHumanScoreHistoryResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_human_score_history", method="GET").inc()
    _enforce_workspace_access(auth, conn)
    effective_session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=session_id,
    )
    normalized_days = max(1, min(365, int(days)))
    cutoff = (datetime.now(tz=UTC) - timedelta(days=normalized_days)).isoformat()
    rows = conn.execute(
        """
        SELECT created_at, score, band
        FROM dashboard_human_score_snapshots
        WHERE workspace_id = ?
          AND user_id = ?
          AND session_id = ?
          AND created_at >= ?
        ORDER BY created_at ASC
        LIMIT 500
        """,
        (auth.workspace_id, auth.user_id, effective_session_id, cutoff),
    ).fetchall()
    points = [
        DashboardHumanScoreHistoryPoint(
            ts=_parse_dt(row["created_at"]) or datetime.now(tz=UTC),
            score=max(0, min(100, _safe_int(row["score"]))),
            band=str(row["band"] or _score_band(_safe_int(row["score"]))),
        )
        for row in rows
    ]
    return DashboardHumanScoreHistoryResponse(session_id=effective_session_id, days=normalized_days, points=points)


@app.post("/v1/dashboard/human-score/recompute", response_model=DashboardHumanScoreResponse)
def dashboard_human_score_recompute(
    body: DashboardHumanScoreRecomputeRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: sqlite3.Connection = Depends(get_db),
) -> DashboardHumanScoreResponse:
    REQUEST_COUNT.labels(endpoint="dashboard_human_score_recompute", method="POST").inc()
    _enforce_workspace_access(auth, conn)
    session_id = _resolve_effective_session_id(
        conn,
        workspace_id=auth.workspace_id,
        user_id=auth.user_id,
        session_id=body.session_id,
    )
    with DASHBOARD_HUMAN_SCORE_MS.time():
        computed = _compute_human_score_lite(
            conn,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            session_id=session_id,
        )
        computed.snapshot_id = _persist_human_score_snapshot_lite(
            conn,
            workspace_id=auth.workspace_id,
            user_id=auth.user_id,
            response=computed,
        )
    DASHBOARD_HUMAN_SCORE_RECOMPUTE_COUNT.inc()
    return computed


# ---------------------------------------------------------------------------
# Static file mount for Angular dashboard (MUST be last — catch-all)
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent.parent / "dashboard-static"
if not _DASHBOARD_DIR.exists():
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
