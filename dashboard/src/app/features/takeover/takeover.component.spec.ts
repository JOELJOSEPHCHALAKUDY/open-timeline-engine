import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import { TakeoverComponent } from './takeover.component';

describe('TakeoverComponent', () => {
  let fixture: ComponentFixture<TakeoverComponent>;
  let component: TakeoverComponent;
  let apiSpy: jasmine.SpyObj<ApiService>;

  const baseGoal = {
    id: 'goal-1',
    session_id: 'default',
    workspace_id: 'personal',
    user_id: 'user',
    title: 'Fix dashboard cache rendering',
    description: 'Validate cache status and goal kind rendering in takeover panel',
    source: 'open_discovery',
    priority_score: 0.72,
    risk_tier: 'medium',
    confidence: 0.66,
    reasoning: 'recent regression',
    evidence_event_ids: [],
    goal_kind: 'unknown',
    affective_scores: {},
    selection_score: 0.79,
    cache_hit: true,
    cache_source: 'l2',
    status: 'candidate',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };

  const baseState = {
    session_id: 'default',
    workspace_id: 'personal',
    user_id: 'user',
    active: false,
    mode: 'takeover',
    persona_mode: 'normal',
    autonomy_policy_profile: 'human_consultative',
    active_goal_id: null,
    goal_queue_size: 1,
    last_discovery_at: new Date().toISOString(),
    continuity_violation_count: 0,
    activated_at: null,
    expires_at: null,
    last_message_at: null,
    takeover_context: null,
    last_classification: null,
    last_safety_decision: 'allow',
    updated_at: new Date().toISOString(),
  };

  beforeEach(async () => {
    apiSpy = jasmine.createSpyObj<ApiService>('ApiService', [
      'getTakeoverState',
      'getTakeoverAutonomyStatus',
      'takeoverListGoals',
      'getTakeoverNotices',
      'getExecutionStatus',
      'getTakeoverGoalCacheStatus',
      'getContextRetrievalStatus',
      'getRetrievalEvalStatus',
      'getWorkflowTemplates',
      'takeoverStep',
      'takeoverReset',
      'takeoverPreload',
      'takeoverDiscoverGoals',
      'takeoverPrecomputeGoals',
      'takeoverAutonomyTick',
      'ackTakeoverNotice',
      'invalidateTakeoverGoalCache',
      'takeoverSelectGoal',
      'requestExecutionPermit',
      'resolveExecutionPermit',
      'claimExecution',
      'recomputeDashboardHumanScore',
    ]);
    apiSpy.getTakeoverState.and.returnValue(of(baseState));
    apiSpy.getTakeoverAutonomyStatus.and.returnValue(
      of({
        session_id: 'default',
        state: { ...baseState, active: true, goal_queue_size: 2 },
        active_goal: { ...baseGoal },
        queue_size: 2,
        pending_permit_count: 1,
        pending_directive_count: 0,
        open_notice_count: 0,
        retry_backlog_count: 0,
        enforcement_mode: 'strict_takeover',
        continuity_ok: true,
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.takeoverListGoals.and.returnValue(of({ session_id: 'default', goals: [{ ...baseGoal }] }));
    apiSpy.getTakeoverNotices.and.returnValue(of({ session_id: 'default', notices: [], generated_at: new Date().toISOString() } as any));
    apiSpy.getExecutionStatus.and.returnValue(of({ session_id: 'default', pending: [], recent: [], generated_at: new Date().toISOString() }));
    apiSpy.getTakeoverGoalCacheStatus.and.returnValue(
      of({
        session_id: 'default',
        objective_hash: 'abc123',
        cache_key: 'goalq:abc123',
        l1: { state: 'hit', source: 'l1' },
        l2: { present: true, fresh: true, goal_count: 1 },
        generated_at: new Date().toISOString(),
      })
    );
    apiSpy.getContextRetrievalStatus.and.returnValue(
      of({
        mode: 'pgvector',
        trigger_threshold: 0.68,
        escalate_threshold: 0.52,
        turn_budget_ms: 120,
        backend_timeout_ms: 60,
        qdrant_timeout_ms: 60,
        qdrant_enabled: false,
        fallback_counters: {},
        generated_at: new Date().toISOString(),
      } as any)
    );
    apiSpy.getRetrievalEvalStatus.and.returnValue(
      of({
        session_id: 'default',
        latest: null,
        history: [],
      })
    );
    apiSpy.getWorkflowTemplates.and.returnValue(
      of({
        session_id: 'default',
        templates: [],
        total: 0,
        generated_at: new Date().toISOString(),
      } as any)
    );
    apiSpy.takeoverStep.and.returnValue(of({ state: { ...baseState, active: true } }));
    apiSpy.takeoverReset.and.returnValue(of(baseState));
    apiSpy.takeoverPreload.and.returnValue(of({}));
    apiSpy.takeoverDiscoverGoals.and.returnValue(of({ session_id: 'default', goals: [{ ...baseGoal }] }));
    apiSpy.takeoverPrecomputeGoals.and.returnValue(of({ session_id: 'default', goals: [{ ...baseGoal }] }));
    apiSpy.takeoverAutonomyTick.and.returnValue(
      of({
        session_id: 'default',
        sessions_scanned: 1,
        goals_refreshed: 0,
        notices_created: 0,
        generated_at: new Date().toISOString(),
      } as any)
    );
    apiSpy.ackTakeoverNotice.and.returnValue(of({} as any));
    apiSpy.invalidateTakeoverGoalCache.and.returnValue(of({ removed: 1 }));
    apiSpy.takeoverSelectGoal.and.returnValue(of({ ...baseGoal }));
    apiSpy.claimExecution.and.returnValue(of({ directive_id: 'd-1' } as any));
    apiSpy.recomputeDashboardHumanScore.and.returnValue(
      of({
        session_id: 'default',
        score: 70,
        band: 'advanced',
        subscores: {
          clone_readiness: 70,
          execution_quality: 70,
          goal_coherence: 70,
          affective_alignment: 70,
        },
        inputs: {},
        computed_at: new Date().toISOString(),
        snapshot_id: 'snap-1',
      })
    );
    apiSpy.requestExecutionPermit.and.returnValue(
      of({
        decision: 'allow',
        reason: 'low risk',
        permit_id: 'permit-1',
        expires_at: null,
        required_confirmation: null,
      })
    );
    apiSpy.resolveExecutionPermit.and.returnValue(
      of({
        decision: 'blocked',
        reason: 'manual deny',
        permit_id: 'permit-1',
        expires_at: null,
        required_confirmation: null,
      })
    );

    await TestBed.configureTestingModule({
      imports: [TakeoverComponent],
      providers: [{ provide: ApiService, useValue: apiSpy }, provideRouter([])],
    }).compileComponents();

    fixture = TestBed.createComponent(TakeoverComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => {
    if (fixture) {
      fixture.destroy();
    }
  });

  it('activates takeover mode through takeoverStep', () => {
    component.activate('takeover');
    expect(apiSpy.takeoverStep).toHaveBeenCalledWith(
      jasmine.objectContaining({
        activation_mode_default: 'takeover',
        session_id: 'default',
      })
    );
  });

  it('sends stand-down command through takeoverStep', () => {
    component.standDown();
    expect(apiSpy.takeoverStep).toHaveBeenCalledWith(
      jasmine.objectContaining({
        message: 'advisor stand down',
      })
    );
  });

  it('renders autonomy, cache, and goal-kind panel data', () => {
    component.showAdvanced = true;
    fixture.detectChanges();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('policy: human_consultative');
    expect(text).toContain('queue: 2');
    expect(text).toContain('L1: hit');
    expect(text).toContain('kind: unknown');
    expect(text).toContain('cache: l2');
  });

  it('precomputes goals and updates notice', () => {
    component.precomputeGoals();
    expect(apiSpy.takeoverPrecomputeGoals).toHaveBeenCalledWith('default', true, true);
    expect(component.notice).toContain('Precomputed 1 goals');
  });
});
