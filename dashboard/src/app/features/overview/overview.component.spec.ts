import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import { OverviewComponent } from './overview.component';

describe('OverviewComponent', () => {
  let fixture: ComponentFixture<OverviewComponent>;
  let component: OverviewComponent;
  let apiSpy: jasmine.SpyObj<ApiService>;

  beforeEach(async () => {
    apiSpy = jasmine.createSpyObj<ApiService>('ApiService', [
      'getSystemStatus',
      'getCloneScore',
      'getActivitySummary',
      'getDashboardAgentRoles',
      'getDashboardClientConfig',
      'getAdvisorProviders',
      'getTakeoverAutonomyStatus',
      'getTakeoverAutonomyReadiness',
      'getTakeoverAutonomyProjectKpis',
      'getDashboardHumanScore',
      'getDashboardHumanScoreHistory',
    ]);
    apiSpy.getSystemStatus.and.returnValue(
      of({
        api: { status: 'ok', uptime_seconds: 120 },
        database: {
          connected: true,
          event_count: 10,
          observation_count: 4,
          pattern_count: 2,
          embedding_count: 1,
        },
        redis: { connected: true, models: [] },
        ollama: { connected: true, models: [] },
        runtime_mode: { mode: 'clone_advisor', clone_enabled: true },
      })
    );
    apiSpy.getCloneScore.and.returnValue(
      of({
        score: 75,
        breakdown: {
          observation_count_score: 20,
          fingerprint_confidence: 18,
          pattern_coverage: 19,
          recent_consistency: 18,
        },
        observation_count: 12,
        situation_types_covered: 5,
        total_situation_types: 12,
      })
    );
    apiSpy.getActivitySummary.and.returnValue(
      of({
        period: 'today',
        start_ts: new Date().toISOString(),
        end_ts: new Date().toISOString(),
        total_events: 5,
        by_domain: { coding: 5 },
        by_task_type: { debug: 3, build: 2 },
        by_event_type: { TASK_STEP: 5 },
        highlights: ['validated dashboard roles'],
        summary: 'dashboard role endpoint is active',
        citations: [],
        policy: {},
        hourly_buckets: {},
        outcome_metrics: {},
        top_errors: [],
        top_entities: [],
        contradiction_count: 0,
      })
    );
    apiSpy.getDashboardAgentRoles.and.returnValue(
      of({
        workspace_id: 'personal',
        runtime_mode: { mode: 'clone_advisor', clone_enabled: true },
        executor: {
          user_id: 'codex-executor',
          consumer_id: 'codex-executor',
          label: 'Codex',
          status: 'active',
          last_seen_ts: new Date().toISOString(),
          source: 'env',
        },
        advisor: {
          user_id: 'claude-executor',
          consumer_id: 'claude-executor',
          label: 'Claude Executor',
          status: 'registered',
          last_seen_ts: null,
          source: 'env',
        },
        secondary_executor: {
          user_id: 'claude-executor',
          consumer_id: 'claude-executor',
          label: 'Claude Executor',
          status: 'registered',
          last_seen_ts: null,
          source: 'env',
        },
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.getDashboardClientConfig.and.returnValue(
      of({
        api_base: '/v1',
        default_workspace_id: 'personal',
        default_user_id: 'local-user',
        default_consumer_id: 'dashboard',
        default_session_id: 'default',
        executor_clients: ['codex', 'claude'],
        runtime_mode: { mode: 'clone_advisor', clone_enabled: true },
        known_identities: {
          executor: { user_id: 'codex-executor', label: 'Codex' },
          secondary_executor: { user_id: 'claude-executor', label: 'Claude Executor' },
          advisor: { user_id: 'claude-executor', label: 'Claude Executor' },
        },
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.getAdvisorProviders.and.returnValue(
      of({
        providers: [
          { provider_id: 'openai', label: 'OpenAI', provider_category: 'global', protocol: 'native' as const, default_models: ['gpt-4o-mini'], required_in_baseline: true },
          { provider_id: 'local_ollama', label: 'Local Ollama', provider_category: 'custom', protocol: 'native' as const, default_models: ['qwen2.5-coder:7b'], required_in_baseline: true },
          { provider_id: 'deepseek', label: 'DeepSeek', provider_category: 'china', protocol: 'native' as const, default_models: ['deepseek-chat'], required_in_baseline: true },
        ],
        config: {
          advisor_primary_provider: 'openai',
          advisor_primary_model: 'gpt-4o-mini',
          advisor_fallback_chain: ['local_ollama'],
          advisor_custom_base_url: null,
          advisor_custom_model: null,
          advisor_custom_api_key_ref: 'advisor-openai',
          advisor_provider_timeout_ms: 6000,
          advisor_provider_retry_max: 2,
          key_storage_backend: 'runtime',
          key_present: true,
          routes: [
            { provider_id: 'openai', model: 'gpt-4o-mini', api_key_ref: 'advisor-openai', base_url: null, api_version: null, region_hint: null, priority: 0 },
            { provider_id: 'local_ollama', model: 'qwen2.5-coder:7b', api_key_ref: null, base_url: 'http://ollama:11434', api_version: null, region_hint: null, priority: 1 },
          ],
          profile_id: 'default',
          active_profile_id: 'default',
          updated_at: new Date().toISOString(),
        },
        generated_at: new Date().toISOString(),
      } as any)
    );
    apiSpy.getTakeoverAutonomyStatus.and.returnValue(
      of({
        session_id: 'default',
        state: {
          session_id: 'default',
          workspace_id: 'personal',
          user_id: 'codex-executor',
          active: true,
          mode: 'takeover',
          persona_mode: 'shadow',
          autonomy_policy_profile: 'human_consultative',
          active_goal_id: null,
          goal_queue_size: 2,
          last_discovery_at: new Date().toISOString(),
          continuity_violation_count: 0,
          activated_at: new Date().toISOString(),
          expires_at: null,
          last_message_at: new Date().toISOString(),
          takeover_context: null,
          last_classification: 'decisive',
          last_safety_decision: 'allow',
          updated_at: new Date().toISOString(),
        },
        active_goal: null,
        queue_size: 2,
        pending_permit_count: 0,
        pending_directive_count: 0,
        open_notice_count: 0,
        retry_backlog_count: 0,
        enforcement_mode: 'strict_takeover',
        continuity_ok: true,
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.getTakeoverAutonomyReadiness.and.returnValue(
      of({
        session_id: 'default',
        gate_passed: true,
        score: 72,
        band: 'pilot_ready',
        metrics: {
          turn_count: 30,
          terminal_directive_count: 12,
          execution_success_rate: 0.8,
          needs_human_rate: 0.1,
          avg_decision_confidence: 0.75,
          avg_context_quality: 0.82,
          retry_rate: 0.08,
          retrieval_trigger_rate: 0.2,
          eval_floor: 0.7,
        },
        thresholds: {
          min_turns: 20,
          min_terminal_directives: 8,
          min_execution_success_rate: 0.7,
          max_needs_human_rate: 0.25,
          min_avg_decision_confidence: 0.65,
          min_avg_context_quality: 0.68,
          min_eval_floor: 0.6,
        },
        checks: {},
        failing_checks: [],
        lookback_turns: 30,
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.getTakeoverAutonomyProjectKpis.and.returnValue(
      of({
        session_id: 'default',
        passed: true,
        band: 'project_autonomy_ready',
        metrics: {
          project_count: 5,
          completed_project_count: 4,
          terminal_directive_count: 20,
          needs_human_turns: 1,
          project_completion_rate: 0.8,
          manual_interventions_per_project: 0.2,
          reopen_rate_after_completion: 0.05,
          verification_pass_rate: 0.85,
        },
        thresholds: {
          min_project_completion_rate: 0.7,
          max_manual_interventions_per_project: 1.0,
          max_reopen_rate_after_completion: 0.2,
          min_verification_pass_rate: 0.7,
        },
        checks: {},
        failing_checks: [],
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.getDashboardHumanScore.and.returnValue(
      of({
        session_id: 'default',
        score: 70,
        band: 'advanced',
        subscores: {
          clone_readiness: 72,
          execution_quality: 69,
          goal_coherence: 68,
          affective_alignment: 71,
        },
        inputs: {},
        computed_at: new Date().toISOString(),
        snapshot_id: 'snap-1',
      })
    );
    apiSpy.getDashboardHumanScoreHistory.and.returnValue(
      of({
        session_id: 'default',
        days: 30,
        points: [
          { ts: new Date().toISOString(), score: 70, band: 'advanced' },
          { ts: new Date().toISOString(), score: 68, band: 'advanced' },
        ],
      })
    );

    await TestBed.configureTestingModule({
      imports: [OverviewComponent],
      providers: [{ provide: ApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(OverviewComponent);
    component = fixture.componentInstance;
  });

  it('renders role identities from dashboard role endpoint', async () => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(apiSpy.getDashboardAgentRoles).toHaveBeenCalled();
    expect(component.executorInfo?.label).toBe('Codex');
    expect(component.additionalExecutorInfo?.label).toBe('Claude Executor');
  });

  it('shows all configured executor clients even when live role endpoint returns only two identities', async () => {
    apiSpy.getDashboardClientConfig.and.returnValue(
      of({
        api_base: '/v1',
        default_workspace_id: 'personal',
        default_user_id: 'local-user',
        default_consumer_id: 'dashboard',
        default_session_id: 'default',
        runtime_mode: { mode: 'clone_advisor', clone_enabled: true },
        known_identities: {
          executor: { user_id: 'codex-executor', label: 'Codex' },
          secondary_executor: { user_id: 'claude-executor', label: 'Claude Executor' },
          advisor: { user_id: 'claude-executor', label: 'Claude Executor' },
        },
        generated_at: new Date().toISOString(),
        executor_clients: ['codex', 'claude', 'cursor', 'generic'],
      })
    );

    fixture = TestBed.createComponent(OverviewComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const ids = component.executorList.map((item) => item.id);
    expect(ids).toContain('codex-executor');
    expect(ids).toContain('claude-executor');
    expect(ids).toContain('cursor-executor');
    expect(ids).toContain('generic-executor');
  });
});
