import { Injectable } from '@angular/core';
import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Observable, catchError, forkJoin, map, of, switchMap, throwError } from 'rxjs';
import { environment } from '../../../environments/environment';
import {
  Fingerprint,
  FingerprintResponse,
  ObservationListResponse,
  CloneScoreResponse,
  SystemStatusResponse,
  EventSearchRequest,
  EventSearchResponse,
  EventItem,
  ActivitySummaryResponse,
  TakeoverState,
  PatternItem,
  RuntimeModeConfig,
  TeamMembership,
  ResourceUsageSnapshot,
  DashboardClientConfig,
  DashboardAgentRolesResponse,
  GraphEntitySearchResponse,
  GraphEventResponse,
  TakeoverGoal,
  TakeoverGoalsResponse,
  ExecutionPermitRequest,
  ExecutionPermitResponse,
  TakeoverAutonomyStatusResponse,
  TakeoverAutonomyReadinessResponse,
  TakeoverAutonomyProjectKpisResponse,
  TakeoverGoalCacheStatusResponse,
  ContextRetrievalStatusResponse,
  ContextBriefResponse,
  EpisodeItem,
  EpisodeListResponse,
  EventAnnotationRequest,
  EventAnnotationResponse,
  MemoryForgetResponse,
  MemoryRuleDeprecateResponse,
  MemoryRuleItem,
  MemoryRuleListResponse,
  RetrievalEvalRunResponse,
  RetrievalEvalStatusResponse,
  TakeoverAutonomyTickResponse,
  TakeoverNoticesResponse,
  AutonomyNotice,
  DirectiveExecution,
  ExecutionStatusResponse,
  WorkflowTemplatesResponse,
  DashboardGoalsIntelligenceResponse,
  DashboardHumanScoreHistoryResponse,
  DashboardHumanScoreResponse,
  AdvisorProvidersResponse,
  AdvisorModelsResponse,
  AdvisorVerifyResponse,
  AdvisorConfigResponse,
  AdvisorLiveModelsRequest,
  AdvisorRouteVerifyRequest,
  AdvisorLocalOllamaPullRequest,
  AdvisorLocalOllamaPullResponse,
  AdvisorLocalOllamaPullStatusResponse,
  DashboardStackRestartRequest,
  DashboardStackRestartResponse,
  DashboardStackRestartStatusResponse,
} from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = environment.apiBase;

  constructor(private http: HttpClient) {}

  private isMissingEndpoint(err: unknown): boolean {
    return err instanceof HttpErrorResponse && [404, 405, 501].includes(err.status);
  }

  private fallbackRuntimeMode(): RuntimeModeConfig {
    return {
      mode: 'timeline_only',
      clone_enabled: false,
      updated_at: new Date(0).toISOString(),
      updated_by: 'dashboard-fallback',
    };
  }

  private fallbackDashboardClientConfig(): DashboardClientConfig {
    return {
      api_base: '/v1',
      default_workspace_id: 'personal',
      default_user_id: 'local-user',
      default_consumer_id: 'dashboard',
      default_session_id: 'default',
      executor_clients: [],
      runtime_mode: {
        mode: 'timeline_only',
        clone_enabled: false,
      },
      known_identities: {
        executor: { user_id: 'executor', label: 'Executor' },
        secondary_executor: { user_id: 'secondary-executor', label: 'Additional Executor' },
        advisor: { user_id: 'secondary-executor', label: 'Additional Executor' },
      },
      generated_at: new Date(0).toISOString(),
    };
  }

  private fallbackFingerprint(): FingerprintResponse {
    return {
      fingerprint: {
        decision_making: {
          risk_tolerance: 'moderate',
          speed_vs_thoroughness: 0.5,
          delegation_tendency: 0.5,
          conflict_resolution_style: 'balanced',
          decision_reversal_frequency: 0.2,
          information_needs_before_deciding: 'medium',
        },
        communication: {
          verbosity: 'moderate',
          formality: 'neutral',
          emoji_usage: false,
          preferred_response_length: 'medium',
          explanation_depth: 'balanced',
          tone_under_pressure: 'calm',
        },
        priorities: {
          speed_vs_quality: 0.5,
          user_experience_vs_technical: 0.5,
          pragmatic_vs_principled: 0.5,
          top_recurring_concerns: [],
        },
        context_switching: {
          multitask_tolerance: 'moderate',
          interruption_handling: 'resume_with_context',
          context_retention_depth: 'medium',
        },
        learning_style: {
          exploration_vs_exploitation: 0.5,
          feedback_response: 'iterative',
          mistake_handling: 'recover_and_document',
        },
        emotional_patterns: {
          frustration_triggers: [],
          satisfaction_signals: [],
          stress_indicators: [],
        },
      },
      observation_count: 0,
      last_updated_at: null,
      is_default: true,
    };
  }

  private normalizeFingerprint(raw: Partial<FingerprintResponse> | null | undefined): FingerprintResponse {
    const fallback = this.fallbackFingerprint();
    const source = (raw ?? {}) as Partial<FingerprintResponse> & { fingerprint?: Partial<Fingerprint> };
    const sourceFp = (source.fingerprint ?? {}) as Partial<Fingerprint>;

    const normalized: FingerprintResponse = {
      fingerprint: {
        decision_making: {
          ...fallback.fingerprint.decision_making,
          ...(sourceFp.decision_making ?? {}),
        },
        communication: {
          ...fallback.fingerprint.communication,
          ...(sourceFp.communication ?? {}),
        },
        priorities: {
          ...fallback.fingerprint.priorities,
          ...(sourceFp.priorities ?? {}),
          top_recurring_concerns: this.toStringList(
            sourceFp.priorities?.top_recurring_concerns ?? fallback.fingerprint.priorities.top_recurring_concerns
          ),
        },
        context_switching: {
          ...fallback.fingerprint.context_switching,
          ...(sourceFp.context_switching ?? {}),
        },
        learning_style: {
          ...fallback.fingerprint.learning_style,
          ...(sourceFp.learning_style ?? {}),
        },
        emotional_patterns: {
          ...fallback.fingerprint.emotional_patterns,
          ...(sourceFp.emotional_patterns ?? {}),
          frustration_triggers: this.toStringList(
            sourceFp.emotional_patterns?.frustration_triggers ??
              fallback.fingerprint.emotional_patterns.frustration_triggers
          ),
          satisfaction_signals: this.toStringList(
            sourceFp.emotional_patterns?.satisfaction_signals ??
              fallback.fingerprint.emotional_patterns.satisfaction_signals
          ),
          stress_indicators: this.toStringList(
            sourceFp.emotional_patterns?.stress_indicators ??
              fallback.fingerprint.emotional_patterns.stress_indicators
          ),
        },
      },
      observation_count:
        typeof source.observation_count === 'number'
          ? source.observation_count
          : fallback.observation_count,
      last_updated_at: source.last_updated_at ?? fallback.last_updated_at,
      is_default: typeof source.is_default === 'boolean' ? source.is_default : fallback.is_default,
    };

    return normalized;
  }

  private fallbackTakeoverState(sessionId: string): TakeoverState {
    return {
      session_id: sessionId,
      active: false,
      mode: 'takeover',
      persona_mode: 'normal',
      activated_at: null,
      expires_at: null,
      last_message_at: null,
      takeover_context: null,
      last_classification: null,
      last_safety_decision: 'allow',
      updated_at: new Date(0).toISOString(),
    };
  }

  private toStringList(values: unknown): string[] {
    if (!Array.isArray(values)) return [];
    return values.map((value) => String(value));
  }

  private isHydratedEvent(value: unknown): value is EventItem {
    if (!value || typeof value !== 'object') return false;
    const row = value as Record<string, unknown>;
    return (
      typeof row['id'] === 'string' &&
      typeof row['title'] === 'string' &&
      typeof row['actor'] === 'string' &&
      typeof row['source'] === 'string' &&
      typeof row['event_type'] === 'string' &&
      typeof row['task_type'] === 'string' &&
      row['payload'] !== undefined
    );
  }

  private eventFromHit(hit: any): EventItem {
    const id = String(hit?.id ?? '');
    return {
      id,
      ts: hit?.ts || new Date(0).toISOString(),
      actor: 'unknown',
      source: 'unknown',
      domain: hit?.domain || 'unknown',
      task_type: hit?.task_type || 'unknown',
      event_type: hit?.event_type || 'TASK_STEP',
      title: hit?.title || id || 'Untitled event',
      payload: {},
      context: {},
      tags: [],
      sensitivity: typeof hit?.sensitivity === 'number' ? hit.sensitivity : 0,
    };
  }

  private normalizeSearchEnvelope(raw: any): {
    hits: any[];
    total: number;
    citations: string[];
    policy: Record<string, any>;
  } {
    const result = raw?.result && typeof raw.result === 'object' ? raw.result : raw;
    const hits = Array.isArray(result?.hits)
      ? result.hits
      : Array.isArray(raw?.events)
      ? raw.events
      : [];
    const total = Number(result?.total ?? raw?.total ?? hits.length) || hits.length;
    const citations = this.toStringList(result?.citations ?? raw?.citations);
    const policy = raw?.policy && typeof raw.policy === 'object' ? raw.policy : {};
    return { hits, total, citations, policy };
  }

  private applyClientFilters(events: EventItem[], request: EventSearchRequest): EventItem[] {
    return events.filter((event) => {
      if (request.domain && event.domain !== request.domain) return false;
      if (request.task_type && event.task_type !== request.task_type) return false;
      if (request.event_type && event.event_type !== request.event_type) return false;
      if (
        typeof request.sensitivity_max === 'number' &&
        event.sensitivity > request.sensitivity_max
      ) {
        return false;
      }
      return true;
    });
  }

  // Dashboard
  getSystemStatus(): Observable<SystemStatusResponse> {
    return this.http
      .get<SystemStatusResponse>(`${this.base}/dashboard/system-status`)
      .pipe(
        catchError((err) => {
          if (!this.isMissingEndpoint(err)) {
            return throwError(() => err);
          }
          return forkJoin({
            health: this.getHealth(),
            runtime: this.getRuntimeMode(),
            activity: this.getActivitySummary('today'),
            patterns: this.getPatterns({ min_confidence: 0.5 }),
          }).pipe(
            map(({ health, runtime, activity, patterns }) => ({
              api: {
                status: health.status || 'ok',
                uptime_seconds: 0,
              },
              database: {
                connected: health.status === 'ok',
                event_count: activity.total_events || 0,
                observation_count: 0,
                pattern_count: patterns.length,
                embedding_count: 0,
              },
              redis: { connected: false, models: [] },
              ollama: { connected: false, models: [] },
              runtime_mode: {
                mode: runtime.mode,
                clone_enabled: runtime.clone_enabled,
              },
            }))
          );
        })
      );
  }

  getCloneScore(): Observable<CloneScoreResponse> {
    return this.http
      .get<CloneScoreResponse>(`${this.base}/dashboard/clone-score`)
      .pipe(
        catchError((err) => {
          if (!this.isMissingEndpoint(err)) {
            return throwError(() => err);
          }
          return forkJoin({
            activity: this.getActivitySummary('today'),
            patterns: this.getPatterns({ min_confidence: 0.5 }),
          }).pipe(
            map(({ activity, patterns }) => {
              const observationCount = activity.total_events || 0;
              const observationCountScore = Math.min(25, Math.floor(observationCount / 4));
              const highConfidence = patterns.filter((p) => p.confidence >= 0.8).length;
              const fingerprintConfidence =
                patterns.length > 0
                  ? Math.min(25, Math.round((highConfidence / patterns.length) * 25))
                  : 0;
              const patternCoverage = Math.min(25, patterns.length * 3);
              const contradictionPenalty = Math.min(10, activity.contradiction_count || 0);
              const recentConsistency = Math.max(0, 20 - contradictionPenalty);
              return {
                score:
                  observationCountScore +
                  fingerprintConfidence +
                  patternCoverage +
                  recentConsistency,
                breakdown: {
                  observation_count_score: observationCountScore,
                  fingerprint_confidence: fingerprintConfidence,
                  pattern_coverage: patternCoverage,
                  recent_consistency: recentConsistency,
                },
                observation_count: observationCount,
                situation_types_covered: patterns.length,
                total_situation_types: 12,
              };
            })
          );
        })
      );
  }

  getDashboardGoalsIntelligence(sessionId: string = 'default'): Observable<DashboardGoalsIntelligenceResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<DashboardGoalsIntelligenceResponse>(`${this.base}/dashboard/goals/intelligence`, {
      params,
    });
  }

  getDashboardHumanScore(sessionId: string = 'default'): Observable<DashboardHumanScoreResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<DashboardHumanScoreResponse>(`${this.base}/dashboard/human-score`, { params });
  }

  getDashboardHumanScoreHistory(
    sessionId: string = 'default',
    days: number = 30
  ): Observable<DashboardHumanScoreHistoryResponse> {
    const params = new HttpParams()
      .set('session_id', sessionId)
      .set('days', String(Math.max(1, Math.floor(days))));
    return this.http.get<DashboardHumanScoreHistoryResponse>(`${this.base}/dashboard/human-score/history`, {
      params,
    });
  }

  recomputeDashboardHumanScore(sessionId: string = 'default'): Observable<DashboardHumanScoreResponse> {
    return this.http.post<DashboardHumanScoreResponse>(`${this.base}/dashboard/human-score/recompute`, {
      session_id: sessionId,
    });
  }

  getDashboardClientConfig(): Observable<DashboardClientConfig> {
    return this.http
      .get<DashboardClientConfig>(`${this.base}/dashboard/client-config`)
      .pipe(
        catchError((err) => {
          if (!this.isMissingEndpoint(err)) {
            return throwError(() => err);
          }
          return of(this.fallbackDashboardClientConfig());
        })
      );
  }

  getDashboardAgentRoles(): Observable<DashboardAgentRolesResponse> {
    return this.http.get<DashboardAgentRolesResponse>(`${this.base}/dashboard/agent-roles`);
  }

  warmDashboardStartup(period: string = 'today'): Observable<{
    health: { status: string } | null;
    runtime: RuntimeModeConfig | null;
    roles: DashboardAgentRolesResponse | null;
    activity: ActivitySummaryResponse | null;
  }> {
    return forkJoin({
      health: this.getHealth().pipe(catchError(() => of(null))),
      runtime: this.getRuntimeMode().pipe(catchError(() => of(null))),
      roles: this.getDashboardAgentRoles().pipe(catchError(() => of(null))),
      activity: this.getActivitySummary(period).pipe(catchError(() => of(null))),
    });
  }

  // Fingerprint
  getFingerprint(): Observable<FingerprintResponse> {
    return this.http.get<FingerprintResponse>(`${this.base}/fingerprint`).pipe(
      map((raw) => this.normalizeFingerprint(raw)),
      catchError((err) => {
        if (!this.isMissingEndpoint(err)) {
          return throwError(() => err);
        }
        return of(this.normalizeFingerprint(this.fallbackFingerprint()));
      })
    );
  }

  // Observations
  getObservations(params?: {
    situation_type?: string;
    outcome_sentiment?: string;
    limit?: number;
    offset?: number;
  }): Observable<ObservationListResponse> {
    let httpParams = new HttpParams();
    if (params?.situation_type) httpParams = httpParams.set('situation_type', params.situation_type);
    if (params?.outcome_sentiment) httpParams = httpParams.set('outcome_sentiment', params.outcome_sentiment);
    if (params?.limit !== undefined) httpParams = httpParams.set('limit', params.limit.toString());
    if (params?.offset !== undefined) httpParams = httpParams.set('offset', params.offset.toString());
    return this.http
      .get<ObservationListResponse>(`${this.base}/observations`, { params: httpParams })
      .pipe(
        catchError((err) => {
          if (!this.isMissingEndpoint(err)) {
            return throwError(() => err);
          }
          return of({
            observations: [],
            total: 0,
            situation_type_counts: {},
          });
        })
      );
  }

  // Events
  searchEvents(request: EventSearchRequest): Observable<EventSearchResponse> {
    const limit = Math.max(1, request.limit ?? 20);
    const offset = Math.max(0, request.offset ?? 0);
    const k = Math.min(100, limit + offset);
    const normalizedQuery = (request.query ?? '').trim();
    const matchAll = request.match_all === true || normalizedQuery.length === 0 || normalizedQuery === '*';
    const filters: Record<string, unknown> = {};
    if (request.domain) filters['domain'] = request.domain;
    if (request.task_type) filters['task_type'] = request.task_type;
    if (typeof request.sensitivity_max === 'number') {
      filters['max_sensitivity'] = request.sensitivity_max;
    }
    const payload: Record<string, unknown> = {
      query: normalizedQuery || '*',
      match_all: matchAll,
      filters,
      k,
    };
    if (request.time_range?.start) payload['time_start'] = request.time_range.start;
    if (request.time_range?.end) payload['time_end'] = request.time_range.end;

    return this.http.post<any>(`${this.base}/search`, payload).pipe(
      switchMap((raw) => {
        const normalized = this.normalizeSearchEnvelope(raw);
        if (normalized.hits.length === 0) {
          return of({
            events: [],
            total: normalized.total,
            citations: normalized.citations,
            policy: normalized.policy,
          });
        }

        if (normalized.hits.every((hit) => this.isHydratedEvent(hit))) {
          const filtered = this.applyClientFilters(normalized.hits as EventItem[], request);
          const paged = filtered.slice(offset, offset + limit);
          return of({
            events: paged,
            total: Math.max(normalized.total, filtered.length),
            citations: normalized.citations,
            policy: normalized.policy,
          });
        }

        const hitIds = normalized.hits
          .map((hit) => String(hit?.id ?? ''))
          .filter((id) => id.length > 0);
        if (hitIds.length === 0) {
          return of({
            events: [],
            total: 0,
            citations: normalized.citations,
            policy: normalized.policy,
          });
        }

        const hitMap = new Map<string, any>(normalized.hits.map((hit) => [String(hit?.id ?? ''), hit]));
        const eventRequests = hitIds.map((id) =>
          this.getEvent(id).pipe(catchError(() => of(this.eventFromHit(hitMap.get(id)))))
        );
        return forkJoin(eventRequests).pipe(
          map((events) => {
            const filtered = this.applyClientFilters(events, request);
            const paged = filtered.slice(offset, offset + limit);
            return {
              events: paged,
              total: Math.max(normalized.total, filtered.length),
              citations: normalized.citations,
              policy: normalized.policy,
            };
          })
        );
      })
    );
  }

  getEvent(id: string): Observable<EventItem> {
    return this.http.get<EventItem>(`${this.base}/events/${id}`);
  }

  // Activity
  getActivitySummary(period: string = 'today'): Observable<ActivitySummaryResponse> {
    return this.http.get<ActivitySummaryResponse>(`${this.base}/summary/activity`, {
      params: { period },
    });
  }

  // Takeover
  getTakeoverState(sessionId: string): Observable<TakeoverState> {
    return this.http.get<TakeoverState>(`${this.base}/takeover/state`, {
      params: { session_id: sessionId },
    }).pipe(
      catchError((err) => {
        if (!(err instanceof HttpErrorResponse) || err.status !== 404) {
          return throwError(() => err);
        }
        return of(this.fallbackTakeoverState(sessionId));
      })
    );
  }

  takeoverStep(body: Record<string, unknown>): Observable<any> {
    return this.http.post<any>(`${this.base}/takeover/step`, body);
  }

  takeoverReset(sessionId: string, personaMode: string = 'normal'): Observable<TakeoverState> {
    return this.http.post<TakeoverState>(`${this.base}/takeover/reset`, null, {
      params: {
        session_id: sessionId,
        persona_mode: personaMode,
      },
    });
  }

  takeoverPreload(body: Record<string, unknown>): Observable<any> {
    return this.http.post<any>(`${this.base}/takeover/preload`, body);
  }

  takeoverDiscoverGoals(sessionId: string, includeOpenDiscovery: boolean = true): Observable<TakeoverGoalsResponse> {
    return this.http.post<TakeoverGoalsResponse>(`${this.base}/takeover/goals/discover`, {
      session_id: sessionId,
      include_open_discovery: includeOpenDiscovery,
    });
  }

  takeoverPrecomputeGoals(
    sessionId: string,
    includeOpenDiscovery: boolean = true,
    forceRecompute: boolean = false
  ): Observable<TakeoverGoalsResponse> {
    return this.http.post<TakeoverGoalsResponse>(`${this.base}/takeover/goals/precompute`, {
      session_id: sessionId,
      include_open_discovery: includeOpenDiscovery,
      force_recompute: forceRecompute,
    });
  }

  takeoverListGoals(sessionId: string, status?: string): Observable<TakeoverGoalsResponse> {
    let params = new HttpParams().set('session_id', sessionId);
    if (status) params = params.set('status', status);
    return this.http.get<TakeoverGoalsResponse>(`${this.base}/takeover/goals`, { params });
  }

  getTakeoverGoalCacheStatus(sessionId: string): Observable<TakeoverGoalCacheStatusResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<TakeoverGoalCacheStatusResponse>(`${this.base}/takeover/goals/cache/status`, {
      params,
    });
  }

  getContextRetrievalStatus(): Observable<ContextRetrievalStatusResponse> {
    return this.http.get<ContextRetrievalStatusResponse>(`${this.base}/context/retrieval/status`);
  }

  getAdvisorProviders(): Observable<AdvisorProvidersResponse> {
    return this.http.get<AdvisorProvidersResponse>(`${this.base}/setup/advisor/providers`);
  }

  getAdvisorModels(providerId: string): Observable<AdvisorModelsResponse> {
    const params = new HttpParams().set('provider', providerId);
    return this.http.get<AdvisorModelsResponse>(`${this.base}/setup/advisor/models`, { params });
  }

  getAdvisorLiveModels(body: AdvisorLiveModelsRequest): Observable<AdvisorModelsResponse> {
    return this.http.post<AdvisorModelsResponse>(`${this.base}/setup/advisor/models/live`, body);
  }

  verifyAdvisor(body: {
    provider_id: string;
    model?: string | null;
    base_url?: string | null;
    api_version?: string | null;
    api_key?: string | null;
    api_key_ref?: string | null;
    fallback_chain?: string[];
    custom_headers?: Record<string, string>;
    timeout_ms?: number;
    retry_max?: number;
  }): Observable<AdvisorVerifyResponse> {
    return this.http.post<AdvisorVerifyResponse>(`${this.base}/setup/advisor/verify`, body);
  }

  verifyAdvisorRoute(body: AdvisorRouteVerifyRequest): Observable<any> {
    return this.http.post<any>(`${this.base}/setup/advisor/route/verify`, body);
  }

  startOllamaPull(body: AdvisorLocalOllamaPullRequest): Observable<AdvisorLocalOllamaPullResponse> {
    return this.http.post<AdvisorLocalOllamaPullResponse>(`${this.base}/setup/advisor/local/ollama/pull`, body);
  }

  getOllamaPullStatus(jobId: string): Observable<AdvisorLocalOllamaPullStatusResponse> {
    return this.http.get<AdvisorLocalOllamaPullStatusResponse>(
      `${this.base}/setup/advisor/local/ollama/pull/${encodeURIComponent(jobId)}`
    );
  }

  updateAdvisorConfig(body: {
    advisor_primary_provider: string;
    advisor_primary_model?: string | null;
    advisor_fallback_chain?: string[];
    profile_id?: string | null;
    routing_mode?: string | null;
    failure_policy?: string | null;
    routes?: Array<{
      provider_id: string;
      model?: string | null;
      api_key_ref?: string | null;
      base_url?: string | null;
      api_version?: string | null;
      region_hint?: string | null;
      priority?: number;
    }>;
    advisor_custom_base_url?: string | null;
    advisor_custom_model?: string | null;
    advisor_custom_api_key_ref?: string | null;
    advisor_provider_timeout_ms?: number;
    advisor_provider_retry_max?: number;
    custom_headers?: Record<string, string>;
    api_key?: string | null;
    api_version?: string | null;
  }): Observable<AdvisorConfigResponse> {
    return this.http.put<AdvisorConfigResponse>(`${this.base}/setup/advisor/config`, body);
  }

  updateExecutorConfig(body: {
    workspace_id?: string | null;
    executor_user_id?: string | null;
    executor_consumer_id?: string | null;
    secondary_executor_user_id?: string | null;
    secondary_executor_consumer_id?: string | null;
    executor_clients?: string[];
  }): Observable<any> {
    return this.http.put<any>(`${this.base}/dashboard/client-config/executor`, body);
  }

  switchAdvisorProfile(body: { profile_id: string; dry_run?: boolean }): Observable<any> {
    return this.http.post<any>(`${this.base}/setup/advisor/switch`, body);
  }

  getAdvisorRuntimeStatus(): Observable<any> {
    return this.http.get<any>(`${this.base}/setup/advisor/runtime/status`);
  }

  probeAdvisorRuntime(body: { profile_id?: string; max_probes?: number; timeout_ms?: number } = {}): Observable<any> {
    return this.http.post<any>(`${this.base}/setup/advisor/runtime/probe`, body);
  }

  restartStack(body: DashboardStackRestartRequest): Observable<DashboardStackRestartResponse> {
    return this.http.post<DashboardStackRestartResponse>(`${this.base}/dashboard/stack/restart`, body);
  }

  getRestartStatus(restartId: string): Observable<DashboardStackRestartStatusResponse> {
    return this.http.get<DashboardStackRestartStatusResponse>(
      `${this.base}/dashboard/stack/restart/${encodeURIComponent(restartId)}`
    );
  }

  getEpisodes(sessionId: string = 'default', status?: string, limit: number = 100): Observable<EpisodeListResponse> {
    let params = new HttpParams().set('session_id', sessionId).set('limit', String(Math.max(1, Math.floor(limit))));
    if (status) params = params.set('status', status);
    return this.http.get<EpisodeListResponse>(`${this.base}/episodes`, { params });
  }

  getEpisode(episodeId: string): Observable<EpisodeItem> {
    return this.http.get<EpisodeItem>(`${this.base}/episodes/${episodeId}`);
  }

  getWorkflowTemplates(sessionId: string = 'default', limit: number = 20): Observable<WorkflowTemplatesResponse> {
    const params = new HttpParams()
      .set('session_id', sessionId)
      .set('limit', String(Math.max(1, Math.floor(limit))));
    return this.http.get<WorkflowTemplatesResponse>(`${this.base}/workflow/templates`, { params });
  }

  annotateEvent(body: EventAnnotationRequest): Observable<EventAnnotationResponse> {
    return this.http.post<EventAnnotationResponse>(`${this.base}/events/annotate`, body);
  }

  getContextBrief(body: {
    task: string;
    session_id?: string;
    app_context?: Record<string, unknown>;
    constraints?: Record<string, unknown>;
    max_items?: number;
    max_tokens?: number;
  }): Observable<ContextBriefResponse> {
    return this.http.post<ContextBriefResponse>(`${this.base}/context/brief`, body);
  }

  getMemoryRules(includeInactive: boolean = false): Observable<MemoryRuleListResponse> {
    const params = new HttpParams().set('include_inactive', String(includeInactive));
    return this.http.get<MemoryRuleListResponse>(`${this.base}/memory/rules`, { params });
  }

  upsertMemoryRule(body: {
    scope?: Record<string, unknown>;
    rule_type: string;
    statement: string;
    priority?: number;
    source_episode_id?: string | null;
  }): Observable<MemoryRuleItem> {
    return this.http.post<MemoryRuleItem>(`${this.base}/memory/rules`, body);
  }

  deprecateMemoryRule(ruleId: string): Observable<MemoryRuleDeprecateResponse> {
    return this.http.post<MemoryRuleDeprecateResponse>(`${this.base}/memory/rules/${ruleId}/deprecate`, {});
  }

  forgetMemory(body: {
    target_type: string;
    target_ids: string[];
    reason?: string;
    hard_delete?: boolean;
  }): Observable<MemoryForgetResponse> {
    return this.http.post<MemoryForgetResponse>(`${this.base}/memory/forget`, body);
  }

  getRetrievalEvalStatus(sessionId: string = 'default'): Observable<RetrievalEvalStatusResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<RetrievalEvalStatusResponse>(`${this.base}/retrieval/eval/status`, { params });
  }

  runRetrievalEval(body: {
    session_id?: string;
    tasks?: string[];
    with_brief?: boolean;
  }): Observable<RetrievalEvalRunResponse> {
    return this.http.post<RetrievalEvalRunResponse>(`${this.base}/retrieval/eval/run`, body);
  }

  invalidateTakeoverGoalCache(sessionId: string, objectiveHash?: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/takeover/goals/cache/invalidate`, {
      session_id: sessionId,
      objective_hash: objectiveHash ?? null,
    });
  }

  takeoverSelectGoal(goalId: string, sessionId: string): Observable<TakeoverGoal> {
    return this.http.post<TakeoverGoal>(`${this.base}/takeover/goals/${goalId}/select`, {
      session_id: sessionId,
    });
  }

  requestExecutionPermit(body: ExecutionPermitRequest): Observable<ExecutionPermitResponse> {
    return this.http.post<ExecutionPermitResponse>(`${this.base}/takeover/permit`, body);
  }

  resolveExecutionPermit(
    permitId: string,
    approved: boolean,
    confirmedBy?: string
  ): Observable<ExecutionPermitResponse> {
    return this.http.post<ExecutionPermitResponse>(`${this.base}/takeover/permit/resolve`, {
      permit_id: permitId,
      approved,
      confirmed_by: confirmedBy ?? null,
    });
  }

  getTakeoverAutonomyStatus(sessionId: string): Observable<TakeoverAutonomyStatusResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<TakeoverAutonomyStatusResponse>(`${this.base}/takeover/autonomy/status`, { params });
  }

  getTakeoverAutonomyReadiness(sessionId: string): Observable<TakeoverAutonomyReadinessResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<TakeoverAutonomyReadinessResponse>(`${this.base}/takeover/autonomy/readiness`, { params });
  }

  getTakeoverAutonomyProjectKpis(sessionId: string): Observable<TakeoverAutonomyProjectKpisResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<TakeoverAutonomyProjectKpisResponse>(`${this.base}/takeover/autonomy/project-kpis`, { params });
  }

  takeoverAutonomyTick(
    sessionId?: string,
    includeOpenDiscovery: boolean = true,
    maxSessions: number = 20
  ): Observable<TakeoverAutonomyTickResponse> {
    return this.http.post<TakeoverAutonomyTickResponse>(`${this.base}/takeover/autonomy/tick`, {
      session_id: sessionId ?? null,
      include_open_discovery: includeOpenDiscovery,
      max_sessions: maxSessions,
    });
  }

  getTakeoverNotices(sessionId: string): Observable<TakeoverNoticesResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<TakeoverNoticesResponse>(`${this.base}/takeover/notices`, { params });
  }

  ackTakeoverNotice(noticeId: string, sessionId: string, selectGoal: boolean = false): Observable<AutonomyNotice> {
    return this.http.post<AutonomyNotice>(`${this.base}/takeover/notices/${noticeId}/ack`, {
      session_id: sessionId,
      select_goal: selectGoal,
    });
  }

  claimExecution(
    sessionId: string,
    directiveId?: string,
    claimedBy?: string
  ): Observable<DirectiveExecution> {
    return this.http.post<DirectiveExecution>(`${this.base}/takeover/execution/claim`, {
      session_id: sessionId,
      directive_id: directiveId ?? null,
      claimed_by: claimedBy ?? null,
    });
  }

  reportExecution(body: {
    session_id: string;
    directive_id: string;
    state: string;
    result: string;
    failure_reason?: string | null;
    details?: Record<string, unknown>;
    rollback_performed?: boolean;
  }): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/takeover/execution/report`, {
      ...body,
      failure_reason: body.failure_reason ?? null,
      details: body.details ?? {},
      rollback_performed: Boolean(body.rollback_performed),
    });
  }

  getExecutionStatus(sessionId: string): Observable<ExecutionStatusResponse> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.get<ExecutionStatusResponse>(`${this.base}/takeover/execution/status`, { params });
  }

  // Patterns
  getPatterns(params?: { domain?: string; min_confidence?: number }): Observable<PatternItem[]> {
    let httpParams = new HttpParams();
    if (params?.domain) httpParams = httpParams.set('domain', params.domain);
    if (params?.min_confidence !== undefined) {
      httpParams = httpParams.set('min_confidence', params.min_confidence.toString());
    }
    return this.http.get<PatternItem[]>(`${this.base}/patterns`, { params: httpParams });
  }

  submitPatternFeedback(pattern_id: string, approved: boolean, note?: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/patterns/feedback`, {
      pattern_id,
      approved,
      note: note || null,
    });
  }

  searchGraphEntities(query: string, k: number = 20): Observable<GraphEntitySearchResponse> {
    return this.http.get<GraphEntitySearchResponse>(`${this.base}/graph/entities`, {
      params: { query, k: String(k) },
    });
  }

  getGraphEvent(eventId: string): Observable<GraphEventResponse> {
    return this.http.get<GraphEventResponse>(`${this.base}/graph/event/${eventId}`);
  }

  // Runtime mode
  getRuntimeMode(): Observable<RuntimeModeConfig> {
    return this.http.get<RuntimeModeConfig>(`${this.base}/runtime/mode`).pipe(
      catchError((err) => {
        if (!this.isMissingEndpoint(err)) {
          return throwError(() => err);
        }
        return of(this.fallbackRuntimeMode());
      })
    );
  }

  setRuntimeMode(mode: string): Observable<RuntimeModeConfig> {
    return this.http.put<RuntimeModeConfig>(`${this.base}/runtime/mode`, { mode });
  }

  // Team/workspace
  getTeamMemberships(): Observable<TeamMembership[]> {
    return this.http.get<TeamMembership[]>(`${this.base}/team/memberships`).pipe(
      catchError((err) => {
        if (!this.isMissingEndpoint(err)) {
          return throwError(() => err);
        }
        return of([]);
      })
    );
  }

  getTeamMembershipsForWorkspace(workspace: string): Observable<TeamMembership[]> {
    return this.http.get<TeamMembership[]>(`${this.base}/team/memberships`, {
      headers: { 'X-TCE-Workspace': workspace },
    }).pipe(
      catchError((err) => {
        if (!this.isMissingEndpoint(err)) {
          return throwError(() => err);
        }
        return of([]);
      })
    );
  }

  // Raw metrics
  getMetricsText(): Observable<string> {
    return this.http.get(`${this.base}/metrics`, { responseType: 'text' }).pipe(
      catchError((err) => {
        if (!this.isMissingEndpoint(err)) {
          return throwError(() => err);
        }
        return of('');
      })
    );
  }

  getResourceUsage(): Observable<ResourceUsageSnapshot> {
    return this.http.get<ResourceUsageSnapshot>(`${this.base}/dashboard/resource-usage`).pipe(
      catchError((err) => {
        if (!this.isMissingEndpoint(err)) {
          return throwError(() => err);
        }
        return of({
          captured_at: new Date().toISOString(),
          database: {
            engine: 'unknown',
            size_bytes: null,
            size_pretty: 'N/A',
            available: false,
            error: 'resource usage endpoint not available',
          },
          docker: {
            available: false,
            source: 'docker_engine_api',
            error: 'resource usage endpoint not available',
          },
          services: [],
        });
      })
    );
  }

  // Health
  getHealth(): Observable<{ status: string }> {
    return this.http.get<{ status: string }>(`${this.base}/health`);
  }
}
