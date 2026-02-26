import { Component, OnInit } from '@angular/core';
import { CommonModule, DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import {
  SystemStatusResponse,
  CloneScoreResponse,
  ActivitySummaryResponse,
  DashboardClientConfig,
  TakeoverAutonomyStatusResponse,
  TakeoverAutonomyReadinessResponse,
  TakeoverAutonomyProjectKpisResponse,
  DashboardHumanScoreResponse,
  DashboardHumanScoreHistoryResponse,
} from '../../core/models';
import { resolveRoleIdentitiesFromDashboard, formatUptime, AgentInfo } from '../../shared/utils';

interface AdvisorRoutePreview {
  providerId: string;
  model: string | null;
  label: string;
}

@Component({
  selector: 'app-overview',
  standalone: true,
  imports: [CommonModule, RouterLink, DatePipe],
  template: `
    <div class="page-header">
      <h1>Overview</h1>
      <p>System health, clone readiness, and recent activity</p>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading dashboard...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div *ngIf="!loading && !error" class="fade-in">
      <!-- System Status Cards -->
      <div class="grid grid-4" style="margin-bottom: 20px;">
        <div class="card" *ngIf="status">
          <div class="card-header">
            <span class="card-title">API</span>
            <span class="status-dot" [class.connected]="status.api.status === 'ok'" [class.disconnected]="status.api.status !== 'ok'"></span>
          </div>
          <div class="stat-value">{{ fmtUptime(status.api.uptime_seconds) }}</div>
          <div class="stat-label">uptime</div>
        </div>

        <div class="card" *ngIf="status">
          <div class="card-header">
            <span class="card-title">Database</span>
            <span class="status-dot" [class.connected]="status.database.connected" [class.disconnected]="!status.database.connected"></span>
          </div>
          <div class="stat-value">{{ status.database.event_count | number }}</div>
          <div class="stat-label">events stored</div>
        </div>

        <div class="card" *ngIf="status">
          <div class="card-header">
            <span class="card-title">Redis</span>
            <span class="status-dot" [class.connected]="status.redis.connected" [class.disconnected]="!status.redis.connected"></span>
          </div>
          <div class="stat-value">{{ status.redis.connected ? 'Online' : 'Offline' }}</div>
          <div class="stat-label">cache layer</div>
        </div>

        <div class="card" *ngIf="status">
          <div class="card-header">
            <span class="card-title">Ollama</span>
            <span class="status-dot" [class.connected]="status.ollama.connected" [class.disconnected]="!status.ollama.connected"></span>
          </div>
          <div class="stat-value">{{ status.ollama.models.length }}</div>
          <div class="stat-label">models loaded</div>
        </div>
      </div>

      <!-- Clone Score + Quick Stats -->
      <div class="grid grid-2" style="margin-bottom: 20px;">
        <div class="card" *ngIf="cloneScore" style="display: flex; align-items: center; gap: 32px;">
          <div class="gauge">
            <svg viewBox="0 0 160 160" width="160" height="160">
              <circle class="gauge-bg" cx="80" cy="80" r="68"></circle>
              <circle class="gauge-fill" cx="80" cy="80" r="68"
                [attr.stroke-dasharray]="circumference"
                [attr.stroke-dashoffset]="gaugeOffset">
              </circle>
            </svg>
            <div class="gauge-text">
              <div class="gauge-value">{{ cloneScore.score }}</div>
              <div class="gauge-label">Clone Score</div>
            </div>
          </div>
          <div>
            <div style="margin-bottom: 16px;">
              <div style="font-size: 13px; color: var(--muted); margin-bottom: 6px;">Observation Count ({{ cloneScore.breakdown.observation_count_score }}/25)</div>
              <div class="progress-bar" style="width: 200px;">
                <div class="progress-fill" [style.width.%]="cloneScore.breakdown.observation_count_score / 25 * 100"></div>
              </div>
            </div>
            <div style="margin-bottom: 16px;">
              <div style="font-size: 13px; color: var(--muted); margin-bottom: 6px;">Fingerprint Confidence ({{ cloneScore.breakdown.fingerprint_confidence }}/25)</div>
              <div class="progress-bar" style="width: 200px;">
                <div class="progress-fill" [style.width.%]="cloneScore.breakdown.fingerprint_confidence / 25 * 100"></div>
              </div>
            </div>
            <div style="margin-bottom: 16px;">
              <div style="font-size: 13px; color: var(--muted); margin-bottom: 6px;">Pattern Coverage ({{ cloneScore.breakdown.pattern_coverage }}/25)</div>
              <div class="progress-bar" style="width: 200px;">
                <div class="progress-fill" [style.width.%]="cloneScore.breakdown.pattern_coverage / 25 * 100"></div>
              </div>
            </div>
            <div>
              <div style="font-size: 13px; color: var(--muted); margin-bottom: 6px;">Recent Consistency ({{ cloneScore.breakdown.recent_consistency }}/25)</div>
              <div class="progress-bar" style="width: 200px;">
                <div class="progress-fill" [style.width.%]="cloneScore.breakdown.recent_consistency / 25 * 100"></div>
              </div>
            </div>
          </div>
        </div>

        <div class="card" *ngIf="status">
          <div class="card-title" style="margin-bottom: 20px;">Quick Stats</div>
          <div class="grid grid-2">
            <div>
              <div class="stat-value">{{ status.database.event_count | number }}</div>
              <div class="stat-label">Events</div>
            </div>
            <div>
              <div class="stat-value">{{ status.database.observation_count | number }}</div>
              <div class="stat-label">Observations</div>
            </div>
            <div>
              <div class="stat-value">{{ status.database.pattern_count | number }}</div>
              <div class="stat-label">Patterns</div>
            </div>
            <div>
              <div class="stat-value">{{ status.database.embedding_count | number }}</div>
              <div class="stat-label">Embeddings</div>
            </div>
          </div>
          <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line);">
            <span class="badge" [class.badge-success]="status.runtime_mode.clone_enabled" [class.badge-muted]="!status.runtime_mode.clone_enabled">
              {{ status.runtime_mode.mode }}
            </span>
          </div>
        </div>
      </div>

      <!-- Active AI Roles -->
      <div class="card" style="margin-bottom: 20px;" *ngIf="status && executorList.length > 0">
        <div class="card-header">
          <span class="card-title">Active AI Roles</span>
          <span class="badge" [class.badge-success]="status.runtime_mode.clone_enabled" [class.badge-muted]="!status.runtime_mode.clone_enabled">
            {{ status.runtime_mode.clone_enabled ? 'clone_advisor active' : 'timeline_only' }}
          </span>
        </div>
        <div class="grid grid-2" style="margin-top: 16px;">
          <div
            *ngFor="let role of executorList; let idx = index"
            style="padding: 12px; background: #f8fafc; border-radius: 8px;"
          >
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
              <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 4px;">
                Executor
              </div>
              <span *ngIf="idx === 0" class="badge badge-accent">primary</span>
            </div>
            <div style="font-size: 20px; font-weight: 700;">{{ role.label }}</div>
            <div style="font-size: 12px; color: var(--muted); margin-top: 2px;">{{ role.id }}</div>
            <div style="margin-top: 8px;">
              <span class="badge" [class.badge-success]="role.status === 'active'" [class.badge-accent]="role.status === 'registered'" [class.badge-muted]="role.status === 'not seen'">
                {{ role.status }}
              </span>
              <span *ngIf="role.lastSeen" style="font-size: 11px; color: var(--muted); margin-left: 8px;">
                last seen {{ role.lastSeen | date:'short' }}
              </span>
            </div>
          </div>
        </div>
        <div
          *ngIf="advisorRoutes.length > 0"
          style="margin-top: 10px; padding: 10px 12px; background: #f8fafc; border-radius: 8px;"
        >
          <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 6px;">
            Advisor Routes
          </div>
          <div
            *ngFor="let route of advisorRoutes; let idx = index"
            style="display:flex; justify-content:space-between; gap:10px; font-size:13px; padding:4px 0;"
          >
            <span style="color: var(--muted);">Route #{{ idx + 1 }}</span>
            <span style="font-weight:600;">{{ route.label }}</span>
          </div>
        </div>
        <div style="font-size: 12px; color: var(--muted); margin-top: 10px;">
          {{ roleSource }}
        </div>
      </div>

      <div class="card" style="margin-bottom: 20px;" *ngIf="autonomyStatus">
        <div class="card-header">
          <span class="card-title">Autonomy Snapshot</span>
          <span class="badge" [class.badge-success]="autonomyStatus.continuity_ok" [class.badge-danger]="!autonomyStatus.continuity_ok">
            {{ autonomyStatus.continuity_ok ? 'continuity ok' : 'continuity degraded' }}
          </span>
        </div>
        <div class="grid grid-3" style="margin-top: 12px;">
          <div>
            <div class="stat-value">{{ autonomyStatus.queue_size }}</div>
            <div class="stat-label">goal queue</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyStatus.pending_permit_count }}</div>
            <div class="stat-label">pending permits</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyStatus.state.autonomy_policy_profile || 'human_consultative' }}</div>
            <div class="stat-label">policy profile</div>
          </div>
        </div>
        <div class="grid grid-3" style="margin-top: 10px;">
          <div>
            <div class="stat-value">{{ autonomyStatus.pending_directive_count || 0 }}</div>
            <div class="stat-label">pending directives</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyStatus.open_notice_count || 0 }}</div>
            <div class="stat-label">open notices</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyStatus.retry_backlog_count || 0 }}</div>
            <div class="stat-label">retry backlog</div>
          </div>
        </div>
        <div style="margin-top: 8px; font-size: 12px; color: var(--muted);">
          enforcement: {{ autonomyStatus.enforcement_mode || autonomyStatus.state.enforcement_mode || 'strict_takeover' }}
        </div>
        <div style="margin-top: 8px; font-size: 13px;" *ngIf="autonomyStatus.active_goal">
          Active goal: <strong>{{ autonomyStatus.active_goal.title }}</strong>
        </div>
      </div>

      <div class="card" style="margin-bottom: 20px;" *ngIf="autonomyReadiness">
        <div class="card-header">
          <span class="card-title">Autonomy Readiness Gate</span>
          <span
            class="badge"
            [class.badge-success]="autonomyReadiness.gate_passed"
            [class.badge-danger]="!autonomyReadiness.gate_passed"
          >
            {{ autonomyReadiness.gate_passed ? 'pass' : 'fail' }}
          </span>
        </div>
        <div class="grid grid-3" style="margin-top: 12px;">
          <div>
            <div class="stat-value">{{ autonomyReadiness.score }}</div>
            <div class="stat-label">readiness score</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyReadiness.band }}</div>
            <div class="stat-label">readiness band</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyReadiness.lookback_turns }}</div>
            <div class="stat-label">lookback turns</div>
          </div>
        </div>
        <div class="grid grid-3" style="margin-top: 10px;">
          <div>
            <div class="stat-value">{{ asPercent(autonomyReadiness.metrics.execution_success_rate) }}</div>
            <div class="stat-label">execution success</div>
          </div>
          <div>
            <div class="stat-value">{{ asPercent(autonomyReadiness.metrics.needs_human_rate) }}</div>
            <div class="stat-label">needs-human rate</div>
          </div>
          <div>
            <div class="stat-value">{{ asPercent(autonomyReadiness.metrics.avg_context_quality) }}</div>
            <div class="stat-label">avg context quality</div>
          </div>
        </div>
        <div *ngIf="autonomyReadiness.checks as checks" style="margin-top: 10px;">
          <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 8px;">
            Gate checks
          </div>
          <div style="display:flex; flex-wrap:wrap; gap:8px;">
            <span class="badge" [class.badge-success]="checks['sample_size']" [class.badge-danger]="!checks['sample_size']">
              sample_size {{ checks['sample_size'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="checks['execution_success_rate']" [class.badge-danger]="!checks['execution_success_rate']">
              execution_success_rate {{ checks['execution_success_rate'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="checks['needs_human_rate']" [class.badge-danger]="!checks['needs_human_rate']">
              needs_human_rate {{ checks['needs_human_rate'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="checks['avg_decision_confidence']" [class.badge-danger]="!checks['avg_decision_confidence']">
              avg_decision_confidence {{ checks['avg_decision_confidence'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="checks['avg_context_quality']" [class.badge-danger]="!checks['avg_context_quality']">
              avg_context_quality {{ checks['avg_context_quality'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="checks['eval_floor']" [class.badge-danger]="!checks['eval_floor']">
              eval_floor {{ checks['eval_floor'] ? 'pass' : 'blocked' }}
            </span>
          </div>
        </div>
        <div style="margin-top: 8px; font-size: 12px; color: var(--muted);" *ngIf="autonomyReadiness.failing_checks.length > 0; else readinessAllGood">
          blocked checks: {{ autonomyReadiness.failing_checks.join(', ') }}
        </div>
        <ng-template #readinessAllGood>
          <div style="margin-top: 8px; font-size: 12px; color: var(--muted);">
            all readiness checks passed
          </div>
        </ng-template>
        <div style="margin-top: 6px; font-size: 12px; color: var(--muted);">
          thresholds: turns >= {{ autonomyReadiness.thresholds.min_turns }}, directives >= {{ autonomyReadiness.thresholds.min_terminal_directives }}
        </div>
      </div>

      <div class="card" style="margin-bottom: 20px;" *ngIf="autonomyProjectKpis">
        <div class="card-header">
          <span class="card-title">Project Autonomy KPIs</span>
          <span
            class="badge"
            [class.badge-success]="autonomyProjectKpis.passed"
            [class.badge-danger]="!autonomyProjectKpis.passed"
          >
            {{ autonomyProjectKpis.passed ? 'pass' : 'blocked' }}
          </span>
        </div>
        <div class="grid grid-3" style="margin-top: 12px;">
          <div>
            <div class="stat-value">{{ asPercent(autonomyProjectKpis.metrics.project_completion_rate) }}</div>
            <div class="stat-label">project completion</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyProjectKpis.metrics.manual_interventions_per_project | number:'1.2-2' }}</div>
            <div class="stat-label">manual interventions/project</div>
          </div>
          <div>
            <div class="stat-value">{{ asPercent(autonomyProjectKpis.metrics.reopen_rate_after_completion) }}</div>
            <div class="stat-label">reopen rate</div>
          </div>
        </div>
        <div class="grid grid-3" style="margin-top: 10px;">
          <div>
            <div class="stat-value">{{ asPercent(autonomyProjectKpis.metrics.verification_pass_rate) }}</div>
            <div class="stat-label">verification pass</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyProjectKpis.metrics.project_count }}</div>
            <div class="stat-label">projects in window</div>
          </div>
          <div>
            <div class="stat-value">{{ autonomyProjectKpis.metrics.terminal_directive_count }}</div>
            <div class="stat-label">terminal directives</div>
          </div>
        </div>
        <div *ngIf="autonomyProjectKpis.checks as projectChecks" style="margin-top: 10px;">
          <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 8px;">
            KPI checks
          </div>
          <div style="display:flex; flex-wrap:wrap; gap:8px;">
            <span class="badge" [class.badge-success]="projectChecks['project_completion_rate']" [class.badge-danger]="!projectChecks['project_completion_rate']">
              project_completion_rate {{ projectChecks['project_completion_rate'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="projectChecks['manual_interventions_per_project']" [class.badge-danger]="!projectChecks['manual_interventions_per_project']">
              manual_interventions_per_project {{ projectChecks['manual_interventions_per_project'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="projectChecks['reopen_rate_after_completion']" [class.badge-danger]="!projectChecks['reopen_rate_after_completion']">
              reopen_rate_after_completion {{ projectChecks['reopen_rate_after_completion'] ? 'pass' : 'blocked' }}
            </span>
            <span class="badge" [class.badge-success]="projectChecks['verification_pass_rate']" [class.badge-danger]="!projectChecks['verification_pass_rate']">
              verification_pass_rate {{ projectChecks['verification_pass_rate'] ? 'pass' : 'blocked' }}
            </span>
          </div>
        </div>
        <div style="margin-top: 8px; font-size: 12px; color: var(--muted);" *ngIf="autonomyProjectKpis.failing_checks.length > 0">
          blocked checks: {{ autonomyProjectKpis.failing_checks.join(', ') }}
        </div>
      </div>

      <div class="card" style="margin-bottom: 20px;" *ngIf="humanScore">
        <div class="card-header">
          <span class="card-title">Human-Level Score</span>
          <span class="badge badge-accent">{{ humanScore.band }}</span>
        </div>
        <div class="grid grid-2" style="margin-top: 12px;">
          <div>
            <div class="stat-value" style="font-size: 40px;">{{ humanScore.score }}</div>
            <div class="stat-label">hybrid autonomy index</div>
            <div style="margin-top: 10px; font-size: 12px; color: var(--muted);">
              snapshot: {{ humanScore.snapshot_id || 'computed' }}
            </div>
          </div>
          <div>
            <div style="margin-bottom: 10px;">
              <div style="font-size: 12px; color: var(--muted); margin-bottom: 4px;">Clone readiness</div>
              <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.clone_readiness"></div></div>
            </div>
            <div style="margin-bottom: 10px;">
              <div style="font-size: 12px; color: var(--muted); margin-bottom: 4px;">Execution quality</div>
              <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.execution_quality"></div></div>
            </div>
            <div style="margin-bottom: 10px;">
              <div style="font-size: 12px; color: var(--muted); margin-bottom: 4px;">Goal coherence</div>
              <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.goal_coherence"></div></div>
            </div>
            <div>
              <div style="font-size: 12px; color: var(--muted); margin-bottom: 4px;">Affective alignment</div>
              <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.affective_alignment"></div></div>
            </div>
          </div>
        </div>
        <div style="margin-top: 12px; font-size: 12px; color: var(--muted);">
          Trend (last 7): {{ recentHumanTrend.length ? recentHumanTrend.join(' → ') : 'n/a' }}
        </div>
      </div>

      <!-- Activity Summary -->
      <div class="card" *ngIf="activity">
        <div class="card-header">
          <span class="card-title">Today's Activity</span>
          <span class="badge badge-accent">{{ activity.total_events }} events</span>
        </div>
        <p style="font-size: 14px; color: var(--ink); margin-bottom: 16px;">{{ activity.summary }}</p>

        <div *ngIf="activity.highlights.length > 0" style="margin-bottom: 16px;">
          <div style="font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px;">Highlights</div>
          <ul style="list-style: none; padding: 0;">
            <li *ngFor="let h of activity.highlights" style="padding: 6px 0; font-size: 14px; color: var(--ink); border-bottom: 1px solid var(--line);">
              {{ h }}
            </li>
          </ul>
        </div>

        <div class="grid grid-3" *ngIf="domainEntries.length > 0">
          <div>
            <div style="font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px;">By Domain</div>
            <div *ngFor="let entry of domainEntries" style="display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px;">
              <span>{{ entry[0] }}</span>
              <span class="badge badge-muted">{{ entry[1] }}</span>
            </div>
          </div>
          <div>
            <div style="font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px;">By Task Type</div>
            <div *ngFor="let entry of taskTypeEntries" style="display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px;">
              <span>{{ entry[0] }}</span>
              <span class="badge badge-muted">{{ entry[1] }}</span>
            </div>
          </div>
          <div>
            <div style="font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px;">By Event Type</div>
            <div *ngFor="let entry of eventTypeEntries" style="display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px;">
              <span>{{ entry[0] }}</span>
              <span class="badge badge-muted">{{ entry[1] }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class OverviewComponent implements OnInit {
  status: SystemStatusResponse | null = null;
  cloneScore: CloneScoreResponse | null = null;
  activity: ActivitySummaryResponse | null = null;
  loading = true;
  error: string | null = null;

  circumference = 2 * Math.PI * 68;
  gaugeOffset = this.circumference;

  domainEntries: [string, number][] = [];
  taskTypeEntries: [string, number][] = [];
  eventTypeEntries: [string, number][] = [];

  executorInfo: AgentInfo | null = null;
  additionalExecutorInfo: AgentInfo | null = null;
  executorList: AgentInfo[] = [];
  advisorRoutes: AdvisorRoutePreview[] = [];
  roleSource = '';
  autonomyStatus: TakeoverAutonomyStatusResponse | null = null;
  autonomyReadiness: TakeoverAutonomyReadinessResponse | null = null;
  autonomyProjectKpis: TakeoverAutonomyProjectKpisResponse | null = null;
  humanScore: DashboardHumanScoreResponse | null = null;
  humanScoreHistory: DashboardHumanScoreHistoryResponse | null = null;

  constructor(private api: ApiService) {}

  get recentHumanTrend(): number[] {
    const points = this.humanScoreHistory?.points || [];
    return points.slice(-7).map((point) => point.score);
  }

  ngOnInit(): void {
    const configPromise = firstValueFrom(this.api.getDashboardClientConfig()).catch(() => null);
    const rolesPromise = firstValueFrom(this.api.getDashboardAgentRoles()).catch(() => null);
    Promise.all([
      firstValueFrom(this.api.getSystemStatus()),
      firstValueFrom(this.api.getCloneScore()),
      firstValueFrom(this.api.getActivitySummary('today')),
      configPromise,
      rolesPromise,
      firstValueFrom(this.api.getAdvisorProviders()).catch(() => null),
    ])
      .then(([
        status,
        score,
        activity,
        config,
        roles,
        advisorProviders,
      ]) => {
        this.status = status ?? null;
        this.cloneScore = score ?? null;
        this.activity = activity ?? null;
        this.advisorRoutes = this.resolveAdvisorRoutes(advisorProviders);

        const mapped = this.resolveRoleCards(config, roles);
        if (mapped) {
          this.executorInfo = mapped.executor;
          this.additionalExecutorInfo = mapped.additionalExecutor;
          this.executorList = mapped.executors;
          this.roleSource = mapped.source;
        }

        if (this.cloneScore) {
          setTimeout(() => {
            this.gaugeOffset = this.circumference - (this.cloneScore!.score / 100) * this.circumference;
          }, 100);
        }

        if (activity) {
          this.domainEntries = Object.entries(activity.by_domain).sort((a, b) => b[1] - a[1]);
          this.taskTypeEntries = Object.entries(activity.by_task_type).sort((a, b) => b[1] - a[1]);
          this.eventTypeEntries = Object.entries(activity.by_event_type).sort((a, b) => b[1] - a[1]);
        }

        const configuredSessionId = String(config?.default_session_id || '').trim() || 'default';
        return Promise.all([
          Promise.resolve(configuredSessionId),
          firstValueFrom(this.api.getTakeoverAutonomyStatus(configuredSessionId)).catch(() => null),
          firstValueFrom(this.api.getTakeoverAutonomyReadiness(configuredSessionId)).catch(() => null),
          firstValueFrom(this.api.getTakeoverAutonomyProjectKpis(configuredSessionId)).catch(() => null),
          firstValueFrom(this.api.getDashboardHumanScore(configuredSessionId)).catch(() => null),
          firstValueFrom(this.api.getDashboardHumanScoreHistory(configuredSessionId, 30)).catch(() => null),
        ]);
      })
      .then(([
        configuredSessionId,
        autonomyStatus,
        autonomyReadiness,
        autonomyProjectKpis,
        humanScore,
        humanScoreHistory,
      ]) => {
        this.autonomyStatus = autonomyStatus;
        this.autonomyReadiness = autonomyReadiness;
        this.autonomyProjectKpis = autonomyProjectKpis;
        this.humanScore = humanScore;
        this.humanScoreHistory = humanScoreHistory;

        const activeSessionId = autonomyStatus?.state?.session_id || autonomyStatus?.session_id || configuredSessionId;
        if (activeSessionId !== configuredSessionId) {
          return Promise.all([
            firstValueFrom(this.api.getTakeoverAutonomyStatus(activeSessionId)).catch(() => null),
            firstValueFrom(this.api.getTakeoverAutonomyReadiness(activeSessionId)).catch(() => null),
            firstValueFrom(this.api.getTakeoverAutonomyProjectKpis(activeSessionId)).catch(() => null),
            firstValueFrom(this.api.getDashboardHumanScore(activeSessionId)).catch(() => null),
            firstValueFrom(this.api.getDashboardHumanScoreHistory(activeSessionId, 30)).catch(() => null),
          ]).then(([activeStatus, activeReadiness, activeProjectKpis, activeHumanScore, activeHumanHistory]) => {
            if (activeStatus) {
              this.autonomyStatus = activeStatus;
            }
            if (activeReadiness) {
              this.autonomyReadiness = activeReadiness;
            }
            if (activeProjectKpis) {
              this.autonomyProjectKpis = activeProjectKpis;
            }
            if (activeHumanScore) {
              this.humanScore = activeHumanScore;
            }
            if (activeHumanHistory) {
              this.humanScoreHistory = activeHumanHistory;
            }
          });
        }
        return null;
      })
      .then(() => {
        this.loading = false;
      })
      .catch(err => {
        this.error = err.message || 'Failed to load dashboard data';
        this.loading = false;
      });
  }

  private resolveAdvisorRoutes(payload: any): AdvisorRoutePreview[] {
    if (!payload || typeof payload !== 'object') {
      return [];
    }
    const providers = Array.isArray(payload.providers) ? payload.providers : [];
    const providerLabelById = new Map<string, string>();
    for (const item of providers) {
      const providerId = String(item?.provider_id || '').trim();
      if (!providerId) continue;
      providerLabelById.set(providerId, String(item?.label || providerId).trim());
    }
    const routes = Array.isArray(payload?.config?.routes) ? payload.config.routes : [];
    if (!routes.length) {
      return [];
    }
    return routes
      .filter((route: any) => route && typeof route === 'object')
      .sort((a: any, b: any) => Number(a?.priority ?? 0) - Number(b?.priority ?? 0))
      .slice(0, 2)
      .map((route: any) => {
        const providerId = String(route?.provider_id || '').trim() || 'unknown';
        const providerLabel = providerLabelById.get(providerId) || providerId;
        const model = String(route?.model || '').trim();
        return {
          providerId,
          model: model || null,
          label: model ? `${providerLabel} · ${model}` : providerLabel,
        };
      });
  }

  private resolveRoleCards(
    config: DashboardClientConfig | null,
    roles: any
  ): { executor: AgentInfo; additionalExecutor: AgentInfo; executors: AgentInfo[]; source: string } | null {
    if (!config && !roles) {
      return null;
    }

    const mapped = roles
      ? resolveRoleIdentitiesFromDashboard(roles, {
          executorClients: config?.executor_clients || [],
          knownIdentities: config?.known_identities || {},
        })
      : {
          executor: { label: 'Executor', id: 'unknown', status: 'not seen', lastSeen: null },
          additionalExecutor: { label: 'Additional Executor', id: 'unknown', status: 'not seen', lastSeen: null },
          executors: [],
          source: 'Role state unavailable.',
        };

    if (config?.known_identities?.executor?.user_id) {
      mapped.executor.id = config.known_identities.executor.user_id;
    }
    if (config?.known_identities?.executor?.label) {
      mapped.executor.label = config.known_identities.executor.label;
    }
    if (config?.known_identities?.secondary_executor?.user_id) {
      mapped.additionalExecutor.id = config.known_identities.secondary_executor.user_id;
    // Backward-compatible alias from older API/config payloads.
    } else if (config?.known_identities?.advisor?.user_id) {
      mapped.additionalExecutor.id = config.known_identities.advisor.user_id;
    }
    if (config?.known_identities?.secondary_executor?.label) {
      mapped.additionalExecutor.label = config.known_identities.secondary_executor.label;
    // Backward-compatible alias from older API/config payloads.
    } else if (config?.known_identities?.advisor?.label) {
      mapped.additionalExecutor.label = config.known_identities.advisor.label;
    }

    if (!mapped.executors.length) {
      const fallbackExecutors: AgentInfo[] = [mapped.executor];
      if (mapped.additionalExecutor.id !== mapped.executor.id) {
        fallbackExecutors.push(mapped.additionalExecutor);
      }
      if (Array.isArray(config?.executor_clients)) {
        for (const raw of config.executor_clients) {
          const client = String(raw || '').trim().toLowerCase();
          if (!client) continue;
          const id = `${client}-executor`;
          if (fallbackExecutors.some((item) => item.id === id)) continue;
          fallbackExecutors.push({
            label: client.charAt(0).toUpperCase() + client.slice(1),
            id,
            status: 'not seen',
            lastSeen: null,
          });
        }
      }
      mapped.executors = fallbackExecutors;
    }

    if (config && roles) {
      mapped.source = 'Configured identities from /v1/dashboard/client-config with live status from /v1/dashboard/agent-roles.';
    } else if (config) {
      mapped.source = 'Configured identities from /v1/dashboard/client-config.';
    }

    return mapped;
  }

  fmtUptime(seconds: number): string {
    return formatUptime(seconds);
  }

  asPercent(value: number): string {
    if (!Number.isFinite(value)) {
      return '0%';
    }
    return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
  }
}
