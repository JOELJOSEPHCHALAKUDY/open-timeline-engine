import { Component, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Subscription, catchError, firstValueFrom, forkJoin, interval, of } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import {
  AutonomyNotice,
  DashboardClientConfig,
  ContextBriefResponse,
  ContextRetrievalStatusResponse,
  DirectiveExecution,
  ExecutionPermitResponse,
  RetrievalEvalStatusResponse,
  TakeoverAutonomyStatusResponse,
  TakeoverGoalCacheStatusResponse,
  TakeoverGoal,
  TakeoverState,
  WorkflowTemplateItem,
} from '../../core/models';

@Component({
  selector: 'app-takeover',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div style="width:100%;max-width:100%;overflow-x:hidden;">

      <!-- ═══════════════════════════════════════════════════════════ -->
      <!-- ZONE 1: Mission Status Hero                                -->
      <!-- ═══════════════════════════════════════════════════════════ -->

      <!-- ACTIVE STATE -->
      <div class="card" style="margin-bottom:16px;border-left:4px solid var(--success);" *ngIf="state?.active">
        <!-- Status bar -->
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <span class="badge badge-success" style="font-size:13px;">&#9679; ACTIVE</span>
            <span class="badge badge-accent">{{ state?.persona_mode || 'normal' }}</span>
            <span class="badge badge-muted">{{ state?.mode || 'takeover' }}</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <span style="font-size:12px;color:var(--muted);">{{ timeSinceRefresh }}</span>
            <span class="badge badge-muted" *ngIf="autoPolling" style="font-size:11px;">auto-refresh on</span>
          </div>
        </div>

        <!-- Objective -->
        <div style="margin-top:16px;">
          <div style="font-size:20px;font-weight:700;color:var(--ink);line-height:1.3;">
            {{ state?.takeover_context?.objective || 'No objective set' }}
          </div>
        </div>

        <!-- Confidence bar -->
        <div style="margin-top:12px;" *ngIf="confidencePercent > 0">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="font-size:12px;color:var(--muted);">Context quality</span>
            <span style="font-size:13px;font-weight:600;" [style.color]="confidenceColor">{{ confidencePercent }}%</span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" [style.width.%]="confidencePercent" [style.background]="confidenceColor"></div>
          </div>
        </div>

        <!-- Status details -->
        <div style="margin-top:14px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
          <div *ngIf="activeGoal" style="font-size:13px;">
            <span style="color:var(--muted);">Goal:</span>
            <strong style="margin-left:4px;">{{ activeGoal.title }}</strong>
          </div>
        </div>
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
          <span class="badge badge-muted" *ngIf="directiveStateLabel !== 'idle'">directive: {{ directiveStateLabel }}</span>
          <span class="badge" [class.badge-success]="autonomy?.continuity_ok" [class.badge-danger]="autonomy && !autonomy.continuity_ok">
            continuity: {{ autonomy?.continuity_ok ? 'ok' : 'degraded' }}
          </span>
          <span class="badge" [class.badge-warn]="(autonomy?.pending_permit_count || 0) > 0" [class.badge-muted]="(autonomy?.pending_permit_count || 0) === 0">
            permits: {{ autonomy?.pending_permit_count || 0 }}
          </span>
          <span class="badge badge-muted" *ngIf="(autonomy?.retry_backlog_count || 0) > 0">
            retries: {{ autonomy?.retry_backlog_count }}
          </span>
          <span class="badge badge-muted" *ngIf="autonomy?.queue_size">
            queue: {{ autonomy?.queue_size }}
          </span>
        </div>

        <!-- Active sessions switcher -->
        <div style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px;" *ngIf="activeSessionOptions.length > 0">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
            <span style="font-size:12px;color:var(--muted);">Active sessions ({{ activeSessionOptions.length }})</span>
            <button class="btn" style="font-size:11px;padding:2px 8px;" (click)="reloadActiveSessions()">Refresh list</button>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button
              class="btn"
              *ngFor="let item of activeSessionOptions"
              [class.btn-primary]="item.sessionId === sessionId"
              style="display:flex;flex-direction:column;align-items:flex-start;min-width:140px;"
              (click)="switchSession(item.sessionId)"
            >
              <span style="font-weight:600;">{{ item.sessionId }}</span>
              <span style="font-size:11px;color:var(--muted);max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                {{ item.objective || 'No objective set' }}
              </span>
            </button>
          </div>
        </div>

        <!-- Actions -->
        <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px;">
          <button class="btn" (click)="standDown()">Stand Down</button>
          <button class="btn" (click)="loadState()">Refresh</button>
          <button class="btn" (click)="togglePolling()">{{ autoPolling ? 'Pause auto-refresh' : 'Resume auto-refresh' }}</button>
        </div>
      </div>

      <!-- INACTIVE STATE -->
      <div class="card" style="margin-bottom:16px;border-left:4px solid var(--line);" *ngIf="!state?.active">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px;">
          <span class="badge badge-muted" style="font-size:13px;">&#9675; INACTIVE</span>
          <span style="font-size:13px;color:var(--muted);">No active takeover session</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;">
          <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" />
          <select class="input" [(ngModel)]="personaMode">
            <option value="normal">normal</option>
            <option value="beru">beru</option>
            <option value="igris">igris</option>
            <option value="shadow">shadow</option>
            <option value="naruto">naruto</option>
          </select>
          <input class="input" [(ngModel)]="objective" placeholder="Enter task objective..." style="grid-column:1/-1;" />
        </div>
        <div style="margin-top:10px;" *ngIf="activeSessionOptions.length > 0">
          <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">Known active sessions</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button
              class="btn"
              *ngFor="let item of activeSessionOptions"
              [class.btn-primary]="item.sessionId === sessionId"
              style="font-size:11px;padding:2px 8px;"
              (click)="switchSession(item.sessionId)"
            >
              {{ item.sessionId }}
            </button>
            <button class="btn" style="font-size:11px;padding:2px 8px;" (click)="reloadActiveSessions()">Refresh list</button>
          </div>
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn btn-primary" (click)="activate('takeover')">Activate Takeover</button>
          <button class="btn" (click)="activate('suggest')">Activate Suggest</button>
          <button class="btn" (click)="preload()">Preload</button>
          <button class="btn" (click)="loadState()">Refresh</button>
        </div>
      </div>

      <!-- Error / Notice banners -->
      <div *ngIf="error" class="error-banner" style="margin-bottom:12px;">{{ error }}</div>
      <div *ngIf="notice" class="card" style="margin-bottom:12px;font-size:13px;color:var(--muted);">{{ notice }}</div>

      <!-- ═══════════════════════════════════════════════════════════ -->
      <!-- ZONE 2: Live Intelligence Cards                            -->
      <!-- ═══════════════════════════════════════════════════════════ -->

      <div class="grid grid-3" style="margin-bottom:16px;">

        <!-- Card 1: Goal Queue -->
        <div class="card">
          <div class="card-header">
            <span class="card-title">Goal Queue</span>
            <span class="badge badge-accent">{{ displayedGoals.length }}</span>
          </div>

          <!-- Active goal (pinned) -->
          <div *ngIf="activeGoal" style="margin-top:10px;padding:10px;background:var(--accent-light,#0b7a7510);border-radius:6px;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;">
              <div>
                <div style="font-size:13px;font-weight:600;color:var(--ink);">&#9733; {{ activeGoal.title }}</div>
                <div style="font-size:11px;color:var(--muted);margin-top:2px;">
                  {{ activeGoal.goal_kind || 'normal' }} &middot; score: {{ (activeGoal.selection_score ?? activeGoal.priority_score) | number:'1.2-2' }}
                </div>
              </div>
            </div>
          </div>

          <!-- Next goals -->
          <div *ngFor="let goal of nextGoals" style="border-top:1px solid var(--line);padding:8px 0;">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
              <div style="flex:1;min-width:0;">
                <div style="font-size:13px;font-weight:500;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{{ goal.title }}</div>
                <div style="font-size:11px;color:var(--muted);">{{ goal.goal_kind || 'normal' }} &middot; {{ (goal.selection_score ?? goal.priority_score) | number:'1.2-2' }}</div>
              </div>
              <button class="btn" style="font-size:11px;padding:2px 8px;" (click)="selectGoal(goal.id)">Select</button>
            </div>
          </div>

          <div *ngIf="displayedGoals.length === 0" style="margin-top:10px;font-size:12px;color:var(--muted);">No goals discovered yet.</div>

          <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn" style="font-size:12px;" (click)="discoverGoals()">Discover Goals</button>
            <button class="btn" style="font-size:12px;" (click)="longTermOnly = !longTermOnly">
              {{ longTermOnly ? 'All' : 'Long-term' }}
            </button>
          </div>
        </div>

        <!-- Card 2: Execution History -->
        <div class="card">
          <div class="card-header">
            <span class="card-title">Executions</span>
            <span class="badge badge-accent">{{ allDirectives.length }}</span>
          </div>

          <div *ngIf="allDirectives.length === 0" style="margin-top:10px;font-size:12px;color:var(--muted);">No directive executions yet.</div>

          <div *ngFor="let ex of recentDirectives" style="border-top:1px solid var(--line);padding:8px 0;">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
              <div style="display:flex;align-items:center;gap:6px;">
                <span *ngIf="ex.state === 'succeeded'" style="color:var(--success);font-weight:600;">&#10003;</span>
                <span *ngIf="ex.state === 'failed'" style="color:var(--danger);font-weight:600;">&#10007;</span>
                <span *ngIf="ex.state !== 'succeeded' && ex.state !== 'failed'" style="color:var(--warn);">&#9672;</span>
                <div>
                  <div style="font-size:13px;font-weight:500;color:var(--ink);">{{ ex.action_kind }}</div>
                  <div style="font-size:11px;color:var(--muted);">{{ ex.state }} &middot; attempt {{ ex.attempt }}</div>
                </div>
              </div>
              <span style="font-size:11px;color:var(--muted);white-space:nowrap;">{{ relativeTime(ex.started_at || ex.finished_at) }}</span>
            </div>
          </div>

          <div style="margin-top:10px;" *ngIf="pendingExecutions.length > 0">
            <button class="btn" style="font-size:12px;" [disabled]="pendingExecutions.length===0" (click)="claimLatestDirective()">Claim Latest</button>
          </div>
        </div>

        <!-- Card 3: Learned Workflows -->
        <div class="card">
          <div class="card-header">
            <span class="card-title">Workflows</span>
            <span class="badge badge-accent">{{ workflowTemplates.length }}</span>
          </div>

          <div *ngIf="workflowTemplates.length === 0" style="margin-top:10px;font-size:12px;color:var(--muted);">No learned workflows yet.</div>

          <div *ngFor="let w of topWorkflows" style="border-top:1px solid var(--line);padding:8px 0;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;">
              <div style="flex:1;min-width:0;">
                <div style="font-size:13px;font-weight:500;color:var(--ink);">{{ parseWorkflowName(w.name) }}</div>
                <div *ngIf="workflowSteps(w).length > 0" style="font-size:11px;color:var(--muted);margin-top:2px;">
                  {{ workflowSteps(w).join(' &rarr; ') }}
                </div>
              </div>
              <span style="font-size:12px;font-weight:600;white-space:nowrap;" [style.color]="workflowRateColor(w)">
                {{ workflowSuccessRate(w) }}%
              </span>
            </div>
          </div>

          <div style="margin-top:10px;">
            <a class="btn" routerLink="/workflow-templates" style="font-size:12px;">View All &rarr;</a>
          </div>
        </div>
      </div>

      <!-- ═══════════════════════════════════════════════════════════ -->
      <!-- ZONE 3: Advanced / Debug Controls (collapsed by default)   -->
      <!-- ═══════════════════════════════════════════════════════════ -->

      <div style="margin-bottom:16px;">
        <button class="btn" (click)="showAdvanced = !showAdvanced" style="width:100%;text-align:left;font-size:13px;color:var(--muted);">
          {{ showAdvanced ? '&#9660;' : '&#9654;' }} Advanced Controls
        </button>
      </div>

      <div *ngIf="showAdvanced" class="fade-in">

        <!-- Session control (all buttons) -->
        <div class="card" style="margin-bottom:16px;">
          <div class="card-header">
            <span class="card-title">Session Controls</span>
            <span class="badge" [class.badge-success]="state?.active" [class.badge-muted]="!state?.active">
              {{ state?.active ? 'active' : 'inactive' }}
            </span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-top:12px;">
            <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" />
            <select class="input" [(ngModel)]="personaMode">
              <option value="normal">normal</option>
              <option value="beru">beru</option>
              <option value="igris">igris</option>
              <option value="shadow">shadow</option>
              <option value="naruto">naruto</option>
            </select>
            <input class="input" [(ngModel)]="objective" placeholder="Objective" />
            <input class="input" [(ngModel)]="stepMessage" placeholder="Step message (optional)" />
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;">
            <button class="btn" (click)="loadState()">Refresh</button>
            <button class="btn btn-primary" (click)="activate('takeover')">Activate takeover</button>
            <button class="btn" (click)="activate('suggest')">Activate suggest</button>
            <button class="btn" (click)="preload()">Preload</button>
            <button class="btn" (click)="standDown()">Stand down</button>
            <button class="btn" (click)="reset()">Reset</button>
            <button class="btn" (click)="discoverGoals()">Discover goals</button>
            <button class="btn" (click)="runStep()">Run step</button>
            <button class="btn" (click)="precomputeGoals()">Precompute goals</button>
            <button class="btn" (click)="runAutonomyTick()">Autonomy tick</button>
            <button class="btn" (click)="loadNotices()">Load notices</button>
            <button class="btn" (click)="loadExecutionStatus()">Execution status</button>
            <button class="btn" (click)="loadCacheStatus()">Cache status</button>
            <button class="btn" (click)="invalidateGoalCache()">Invalidate cache</button>
            <button class="btn" (click)="generateContextBrief()">Generate context brief</button>
            <button class="btn" (click)="loadRetrievalEvalStatus()">Eval status</button>
            <button class="btn" (click)="runRetrievalEval()">Run retrieval eval</button>
            <button class="btn" (click)="recomputeHumanScore()">Recompute Human Score</button>
            <a class="btn" routerLink="/goal-intelligence">Open Goal Intelligence</a>
            <button class="btn" (click)="togglePolling()">{{ autoPolling ? 'Pause auto-refresh' : 'Resume auto-refresh' }}</button>
          </div>
        </div>

        <!-- Autonomy + Permit (2-col) -->
        <div class="grid grid-2" *ngIf="autonomy" style="margin-bottom:16px;">
          <div class="card">
            <div class="card-title">Autonomy Status</div>
            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
              <span class="badge badge-muted">policy: {{ autonomy.state.autonomy_policy_profile || 'human_consultative' }}</span>
              <span class="badge badge-muted">queue: {{ autonomy.queue_size }}</span>
              <span class="badge badge-muted">pending directives: {{ autonomy.pending_directive_count || 0 }}</span>
              <span class="badge badge-muted">open notices: {{ autonomy.open_notice_count || 0 }}</span>
              <span class="badge badge-muted">retry backlog: {{ autonomy.retry_backlog_count || 0 }}</span>
              <span class="badge badge-muted">enforcement: {{ autonomy.enforcement_mode || autonomy.state.enforcement_mode || 'strict_takeover' }}</span>
              <span class="badge" [class.badge-success]="autonomy.continuity_ok" [class.badge-danger]="!autonomy.continuity_ok">
                continuity: {{ autonomy.continuity_ok ? 'ok' : 'degraded' }}
              </span>
              <span class="badge" [class.badge-warn]="autonomy.pending_permit_count > 0" [class.badge-muted]="autonomy.pending_permit_count === 0">
                pending permits: {{ autonomy.pending_permit_count }}
              </span>
            </div>
            <div style="margin-top:10px;font-size:13px;color:var(--muted);" *ngIf="autonomy.active_goal">
              Active goal: <strong>{{ autonomy.active_goal.title }}</strong>
            </div>
          </div>

          <div class="card">
            <div class="card-title">Permit Request</div>
            <div style="display:grid;grid-template-columns:1fr;gap:8px;margin-top:10px;">
              <input class="input" [(ngModel)]="permitActionKind" placeholder="action kind (edit|write|delete)" />
              <input class="input" [(ngModel)]="permitTargetPaths" placeholder="target paths (comma separated)" />
              <input class="input" [(ngModel)]="permitCommandPreview" placeholder="command preview (optional)" />
              <input class="input" type="number" [(ngModel)]="permitChangeSize" placeholder="estimated change size" />
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
              <button class="btn" (click)="requestPermit()">Request permit</button>
              <button class="btn" [disabled]="!lastPermit" (click)="resolvePermit(true)">Approve last</button>
              <button class="btn" [disabled]="!lastPermit" (click)="resolvePermit(false)">Deny last</button>
            </div>
            <div *ngIf="lastPermit" style="margin-top:10px;font-size:13px;">
              <span class="badge badge-muted">decision: {{ lastPermit.decision }}</span>
              <span style="margin-left:8px;">{{ lastPermit.reason }}</span>
            </div>
          </div>
        </div>

        <!-- Notices + Directives (2-col) -->
        <div class="grid grid-2" style="margin-bottom:16px;">
          <div class="card">
            <div class="card-header">
              <span class="card-title">Proactive Notices</span>
              <span class="badge badge-accent">{{ notices.length }}</span>
            </div>
            <div *ngIf="notices.length === 0" style="margin-top:10px;color:var(--muted);">No open notices.</div>
            <div *ngFor="let item of notices" style="border-top:1px solid var(--line);padding:10px 0;">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;">
                <div>
                  <div style="font-weight:600;">{{ item.title }}</div>
                  <div style="font-size:12px;color:var(--muted);">{{ item.reason }}</div>
                </div>
                <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
                  <span class="badge badge-muted">p={{ item.priority | number:'1.2-2' }}</span>
                  <button class="btn" (click)="ackNotice(item.id, false)">Ack</button>
                  <button class="btn" (click)="ackNotice(item.id, true)">Ack + select</button>
                </div>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-header">
              <span class="card-title">Directive Execution</span>
              <span class="badge badge-accent">{{ pendingExecutions.length }}</span>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
              <button class="btn" [disabled]="pendingExecutions.length===0" (click)="claimLatestDirective()">Claim latest</button>
            </div>
            <div *ngIf="pendingExecutions.length === 0" style="margin-top:10px;color:var(--muted);">No pending directives.</div>
            <div *ngFor="let ex of pendingExecutions" style="border-top:1px solid var(--line);padding:10px 0;">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;">
                <div>
                  <div style="font-weight:600;">{{ ex.action_kind }}</div>
                  <div style="font-size:12px;color:var(--muted);">directive {{ ex.directive_id }}</div>
                </div>
                <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
                  <span class="badge badge-muted">{{ ex.state }}</span>
                  <span class="badge badge-muted">attempt {{ ex.attempt }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Cache status -->
        <div class="card" style="margin-bottom:16px;" *ngIf="cacheStatus">
          <div class="card-header">
            <span class="card-title">Goal Cache</span>
            <span class="badge badge-muted">{{ cacheStatus.cache_key || 'no-key' }}</span>
          </div>
          <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
            <span class="badge badge-muted">L1: {{ cacheStatus.l1?.['state'] || 'miss' }}</span>
            <span class="badge badge-muted">L1 source: {{ cacheStatus.l1?.['source'] || '-' }}</span>
            <span class="badge badge-muted">L2 present: {{ cacheStatus.l2?.['present'] ? 'yes' : 'no' }}</span>
            <span class="badge badge-muted">L2 fresh: {{ cacheStatus.l2?.['fresh'] ? 'yes' : 'no' }}</span>
            <span class="badge badge-muted">L2 goals: {{ cacheStatus.l2?.['goal_count'] || 0 }}</span>
          </div>
        </div>

        <!-- Context retrieval -->
        <div class="card" style="margin-bottom:16px;" *ngIf="retrievalStatus || lastStepMeta">
          <div class="card-header">
            <span class="card-title">Context Retrieval</span>
            <span class="badge badge-muted">{{ retrievalStatus?.mode || 'unknown' }}</span>
          </div>
          <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;" *ngIf="retrievalStatus">
            <span class="badge badge-muted">trigger < {{ retrievalStatus.trigger_threshold }}</span>
            <span class="badge badge-muted">escalate < {{ retrievalStatus.escalate_threshold }}</span>
            <span class="badge badge-muted">budget {{ retrievalStatus.turn_budget_ms }}ms</span>
            <span class="badge badge-muted">backend timeout {{ retrievalStatus.backend_timeout_ms }}ms</span>
            <span class="badge badge-muted">qdrant {{ retrievalStatus.qdrant_enabled ? 'on' : 'off' }}</span>
          </div>
          <div style="margin-top:10px;" *ngIf="lastStepMeta">
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">Last takeover step retrieval</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <span class="badge badge-muted">quality {{ lastStepMeta.context_quality_score ?? 0 }}</span>
              <span class="badge badge-muted">triggered {{ lastStepMeta.retrieval_triggered ? 'yes' : 'no' }}</span>
              <span class="badge badge-muted">source {{ lastStepMeta.retrieval_source || 'none' }}</span>
              <span class="badge badge-muted">reason {{ lastStepMeta.retrieval_reason || '-' }}</span>
              <span class="badge badge-muted">latency {{ lastStepMeta.retrieval_latency_ms ?? 0 }}ms</span>
              <span class="badge badge-muted">hits {{ lastStepMeta.retrieval_hit_count ?? 0 }}</span>
            </div>
          </div>
        </div>

        <!-- Context brief + Retrieval eval (2-col) -->
        <div class="grid grid-2" style="margin-bottom:16px;">
          <div class="card">
            <div class="card-header">
              <span class="card-title">Context Brief</span>
              <span class="badge badge-muted">{{ contextBrief ? 'ready' : 'not generated' }}</span>
            </div>
            <div style="display:grid;grid-template-columns:1fr;gap:8px;margin-top:10px;">
              <input class="input" [(ngModel)]="contextBriefTask" placeholder="Task for context brief (defaults to objective)" />
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
              <button class="btn" (click)="generateContextBrief()">Generate</button>
            </div>
            <div *ngIf="contextBrief" style="margin-top:12px;">
              <div style="font-size:13px;margin-bottom:8px;"><strong>Summary:</strong> {{ contextBrief.summary }}</div>
              <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
                <span class="badge badge-muted">standard {{ contextBrief.standard_approach.length }}</span>
                <span class="badge badge-muted">state {{ contextBrief.current_state.length }}</span>
                <span class="badge badge-muted">constraints {{ contextBrief.constraints_preferences.length }}</span>
                <span class="badge badge-muted">open loops {{ contextBrief.open_loops.length }}</span>
                <span class="badge badge-muted">artifacts {{ contextBrief.artifacts.length }}</span>
              </div>
              <div *ngIf="contextBrief.constraints_preferences.length > 0" style="font-size:12px;color:var(--muted);">
                Top constraints: {{ contextBriefTopConstraints() }}
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-header">
              <span class="card-title">Retrieval Eval</span>
              <span class="badge badge-muted">{{ retrievalEvalStatus?.history?.length || 0 }} runs</span>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
              <button class="btn" (click)="loadRetrievalEvalStatus()">Refresh status</button>
              <button class="btn" (click)="runRetrievalEval()">Run eval</button>
            </div>
            <div *ngIf="retrievalEvalStatus?.latest as latest" style="margin-top:12px;">
              <div style="font-size:13px;margin-bottom:6px;"><strong>Latest run:</strong> {{ latest.run_id }}</div>
              <div style="display:flex;gap:8px;flex-wrap:wrap;">
                <span class="badge badge-muted">style {{ latest.style_alignment | number:'1.2-2' }}</span>
                <span class="badge badge-muted">constraints {{ latest.constraint_compliance | number:'1.2-2' }}</span>
                <span class="badge badge-muted">traceability {{ latest.decision_traceability | number:'1.2-2' }}</span>
                <span class="badge badge-muted">follow-up reduction {{ latest.followup_reduction | number:'1.2-2' }}</span>
              </div>
            </div>
            <div *ngIf="!retrievalEvalStatus?.latest" style="margin-top:10px;color:var(--muted);">
              No eval run yet.
            </div>
          </div>
        </div>

        <!-- Full goal list -->
        <div class="card" style="margin-bottom:16px;">
          <div class="card-header">
            <span class="card-title">Full Goal Queue</span>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              <button class="btn" (click)="longTermOnly = !longTermOnly">
                {{ longTermOnly ? 'Show all goals' : 'View long-term goals only' }}
              </button>
              <span class="badge badge-accent">{{ displayedGoals.length }}</span>
            </div>
          </div>
          <div *ngIf="displayedGoals.length === 0" style="margin-top:10px;color:var(--muted);">No discovered goals yet.</div>
          <div *ngFor="let goal of displayedGoals" style="border-top:1px solid var(--line);padding:10px 0;">
            <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;">
              <div>
                <div style="font-weight:600;">{{ goal.title }}</div>
                <div style="font-size:12px;color:var(--muted);">{{ goal.description }}</div>
              </div>
              <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
                <span class="badge badge-muted">{{ goal.status }}</span>
                <span class="badge badge-muted">kind: {{ goal.goal_kind || 'normal' }}</span>
                <span class="badge badge-muted">risk: {{ goal.risk_tier }}</span>
                <span class="badge badge-muted">p={{ goal.priority_score | number:'1.2-2' }}</span>
                <span class="badge badge-muted">s={{ (goal.selection_score ?? goal.priority_score) | number:'1.2-2' }}</span>
                <span class="badge badge-muted">cache: {{ goal.cache_source || 'computed' }}</span>
                <button class="btn" (click)="selectGoal(goal.id)">Select</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Raw takeover context -->
        <div class="card" style="margin-bottom:16px;" *ngIf="state?.takeover_context">
          <div class="card-title">Takeover Context (raw)</div>
          <pre style="background:#f1f5f9;padding:12px;border-radius:8px;overflow:auto;max-height:320px;">{{ state?.takeover_context | json }}</pre>
        </div>

      </div>
    </div>
  `,
})
export class TakeoverComponent implements OnInit, OnDestroy {
  sessionId = 'default';
  personaMode = 'normal';
  objective = '';
  stepMessage = '';
  state: TakeoverState | null = null;
  autonomy: TakeoverAutonomyStatusResponse | null = null;
  cacheStatus: TakeoverGoalCacheStatusResponse | null = null;
  retrievalStatus: ContextRetrievalStatusResponse | null = null;
  contextBrief: ContextBriefResponse | null = null;
  retrievalEvalStatus: RetrievalEvalStatusResponse | null = null;
  goals: TakeoverGoal[] = [];
  notices: AutonomyNotice[] = [];
  pendingExecutions: DirectiveExecution[] = [];
  recentExecutions: DirectiveExecution[] = [];
  lastPermit: ExecutionPermitResponse | null = null;
  workflowTemplates: WorkflowTemplateItem[] = [];
  permitActionKind = 'edit';
  permitTargetPaths = '';
  permitCommandPreview = '';
  permitChangeSize = 10;
  contextBriefTask = '';
  autoPolling = true;
  longTermOnly = false;
  error: string | null = null;
  notice: string | null = null;
  lastStepMeta: {
    context_quality_score?: number;
    retrieval_triggered?: boolean;
    retrieval_source?: string;
    retrieval_reason?: string | null;
    retrieval_latency_ms?: number;
    retrieval_hit_count?: number;
  } | null = null;

  // New properties for zone 1
  showAdvanced = false;
  lastRefreshTime: Date | null = null;
  activeSessionOptions: Array<{
    sessionId: string;
    objective: string;
    updatedAt: string | null;
    mode: string;
    personaMode: string;
  }> = [];

  private pollSub: Subscription | null = null;

  constructor(private api: ApiService) {}

  // --- Zone 1 helper getters ---

  get timeSinceRefresh(): string {
    if (!this.lastRefreshTime) return '';
    const diffMs = Date.now() - this.lastRefreshTime.getTime();
    const secs = Math.floor(diffMs / 1000);
    if (secs < 5) return 'just now';
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.floor(secs / 60);
    return `${mins}m ago`;
  }

  get activeGoal(): TakeoverGoal | null {
    return this.goals.find((g) => g.status === 'active') || this.autonomy?.active_goal || null;
  }

  get confidencePercent(): number {
    return Math.round((this.lastStepMeta?.context_quality_score || 0) * 100);
  }

  get confidenceColor(): string {
    const p = this.confidencePercent;
    if (p >= 70) return 'var(--success)';
    if (p >= 40) return 'var(--warn)';
    return 'var(--danger)';
  }

  get directiveStateLabel(): string {
    if (this.pendingExecutions.length > 0) return this.pendingExecutions[0].state;
    return 'idle';
  }

  // --- Zone 2 helper getters ---

  get nextGoals(): TakeoverGoal[] {
    return this.displayedGoals.filter((g) => g.status !== 'active').slice(0, 4);
  }

  get allDirectives(): DirectiveExecution[] {
    const seen = new Set<string>();
    const all: DirectiveExecution[] = [];
    for (const ex of [...this.pendingExecutions, ...this.recentExecutions]) {
      if (!seen.has(ex.directive_id)) {
        seen.add(ex.directive_id);
        all.push(ex);
      }
    }
    return all;
  }

  get recentDirectives(): DirectiveExecution[] {
    return this.allDirectives.slice(0, 5);
  }

  get topWorkflows(): WorkflowTemplateItem[] {
    return [...this.workflowTemplates]
      .sort((a, b) => this.workflowSuccessRate(b) - this.workflowSuccessRate(a))
      .slice(0, 3);
  }

  parseWorkflowName(name: string): string {
    if (!name) return 'Unnamed';
    if (name.includes('::')) {
      const kind = name.split('::')[0];
      return kind.charAt(0).toUpperCase() + kind.slice(1) + ' Workflow';
    }
    const parts = name.split(':');
    if (parts.length >= 3) {
      const pt = parts[parts.length - 2];
      if (pt && pt.length > 2) return (pt.charAt(0).toUpperCase() + pt.slice(1)).replace(/_/g, ' ') + ' Pattern';
    }
    return parts[0].charAt(0).toUpperCase() + parts[0].slice(1);
  }

  workflowSuccessRate(t: WorkflowTemplateItem): number {
    const s = this.workflowMetric(t, 'success_count');
    const f = this.workflowMetric(t, 'failure_count');
    const total = s + f;
    return total > 0 ? Math.round((s / total) * 100) : 0;
  }

  workflowRateColor(t: WorkflowTemplateItem): string {
    const r = this.workflowSuccessRate(t);
    if (r >= 70) return 'var(--success)';
    if (r >= 40) return 'var(--warn)';
    return 'var(--danger)';
  }

  relativeTime(dateStr: string | null | undefined): string {
    if (!dateStr) return '-';
    const then = new Date(dateStr).getTime();
    if (isNaN(then)) return '-';
    const mins = Math.floor((Date.now() - then) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  // --- Existing getters ---

  get displayedGoals(): TakeoverGoal[] {
    if (!this.longTermOnly) return this.goals;
    return this.goals.filter((goal) => {
      const affective = (goal.affective_scores || {}) as Record<string, unknown>;
      const temporal = (affective['temporal'] || {}) as Record<string, unknown>;
      const dream = Number(temporal['dream'] ?? 0);
      return goal.goal_kind === 'unknown' || dream >= 0.3 || goal.source === 'user_objective';
    });
  }

  // --- Lifecycle ---

  ngOnInit(): void {
    void this.resolveInitialSessionAndLoad();
    this.pollSub = interval(5000).subscribe(() => {
      if (!this.autoPolling || document.hidden) return;
      this.refreshAll(false);
    });
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
    this.pollSub = null;
  }

  private async resolveInitialSessionAndLoad(): Promise<void> {
    await this.resolveLatestActiveSession();
    this.refreshAll();
  }

  private async resolveLatestActiveSession(autoSelect: boolean = true): Promise<void> {
    const fallbackSession = (this.sessionId || 'default').trim() || 'default';
    let config: DashboardClientConfig | null = null;
    try {
      config = await firstValueFrom(this.api.getDashboardClientConfig());
    } catch {
      config = null;
    }

    const candidates = this.buildSessionCandidates(config, fallbackSession);
    const probes = candidates.map((sid) =>
      this.api.getTakeoverState(sid).pipe(catchError(() => of(null as TakeoverState | null)))
    );

    let states: Array<TakeoverState | null> = [];
    try {
      states = await firstValueFrom(forkJoin(probes));
    } catch {
      states = [];
    }

    const zipped = candidates.map((sid, idx) => ({ sessionId: sid, state: states[idx] || null }));
    const active = zipped.filter((entry) => entry.state?.active);
    if (active.length === 0) {
      this.activeSessionOptions = [];
      if (autoSelect) {
        this.sessionId = this.normalizeSessionId(config?.default_session_id, fallbackSession);
      }
      return;
    }

    active.sort((a, b) => {
      const aObjective = String(a.state?.takeover_context?.objective || '').trim().length > 0 ? 1 : 0;
      const bObjective = String(b.state?.takeover_context?.objective || '').trim().length > 0 ? 1 : 0;
      if (aObjective !== bObjective) return bObjective - aObjective;
      const aUpdated = this.toEpochMs(a.state?.updated_at);
      const bUpdated = this.toEpochMs(b.state?.updated_at);
      return bUpdated - aUpdated;
    });

    const selected = active[0];
    this.activeSessionOptions = active.slice(0, 20).map((entry) => ({
      sessionId: entry.sessionId,
      objective: String(entry.state?.takeover_context?.objective || '').trim(),
      updatedAt: entry.state?.updated_at || null,
      mode: String(entry.state?.mode || 'takeover'),
      personaMode: String(entry.state?.persona_mode || 'normal'),
    }));

    if (autoSelect) {
      this.sessionId = selected.sessionId;
      const selectedObjective = String(selected.state?.takeover_context?.objective || '').trim();
      if (!this.objective.trim() && selectedObjective.length > 0) {
        this.objective = selectedObjective;
      }
    }
  }

  private buildSessionCandidates(config: DashboardClientConfig | null, fallbackSession: string): string[] {
    const out = new Set<string>();
    const add = (value: unknown): void => {
      const normalized = String(value ?? '').trim();
      if (!normalized) return;
      out.add(normalized);
    };

    add(fallbackSession);
    add('default');
    add(config?.default_session_id);

    const clients = Array.isArray(config?.executor_clients) ? config!.executor_clients : [];
    for (const raw of clients) {
      const client = String(raw || '').trim().toLowerCase();
      if (!client) continue;
      add(client);
      add(`${client}-executor`);
      add(`${client}-executer`);
    }

    const known = config?.known_identities || {};
    for (const identity of Object.values(known)) {
      add((identity as any)?.user_id);
      add((identity as any)?.consumer_id);
    }

    add('codex');
    add('claude');
    add('cursor');
    add('lovable');
    add('executor');
    add('secondary');

    return Array.from(out).slice(0, 40);
  }

  private normalizeSessionId(preferred: string | null | undefined, fallback: string): string {
    const normalized = String(preferred || '').trim();
    if (normalized) return normalized;
    const safeFallback = String(fallback || '').trim();
    return safeFallback || 'default';
  }

  private toEpochMs(value: string | null | undefined): number {
    if (!value) return 0;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  private refreshAll(showNotice: boolean = false): void {
    this.loadState();
    this.loadAutonomy();
    this.loadGoals();
    this.loadNotices();
    this.loadExecutionStatus();
    this.loadCacheStatus();
    this.loadRetrievalStatus();
    this.loadRetrievalEvalStatus();
    this.loadWorkflowTemplates();
    this.lastRefreshTime = new Date();
    if (showNotice) this.notice = 'State refreshed.';
  }

  togglePolling(): void {
    this.autoPolling = !this.autoPolling;
    this.notice = this.autoPolling ? 'Auto-refresh resumed.' : 'Auto-refresh paused.';
  }

  // --- Data loading methods (all unchanged) ---

  loadState(): void {
    this.error = null;
    this.api.getTakeoverState(this.sessionId).subscribe({
      next: (state) => {
        this.state = state;
        this.upsertActiveSessionFromState(state);
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to load takeover state'),
    });
  }

  loadAutonomy(): void {
    this.api.getTakeoverAutonomyStatus(this.sessionId).subscribe({
      next: (res) => (this.autonomy = res),
      error: () => (this.autonomy = null),
    });
  }

  loadGoals(): void {
    this.api.takeoverListGoals(this.sessionId).subscribe({
      next: (res) => (this.goals = res.goals || []),
      error: () => (this.goals = []),
    });
  }

  loadNotices(): void {
    this.api.getTakeoverNotices(this.sessionId).subscribe({
      next: (res) => (this.notices = res.notices || []),
      error: () => (this.notices = []),
    });
  }

  loadExecutionStatus(): void {
    this.api.getExecutionStatus(this.sessionId).subscribe({
      next: (res) => {
        this.pendingExecutions = res.pending || [];
        this.recentExecutions = res.recent || [];
      },
      error: () => {
        this.pendingExecutions = [];
        this.recentExecutions = [];
      },
    });
  }

  loadCacheStatus(): void {
    this.api.getTakeoverGoalCacheStatus(this.sessionId).subscribe({
      next: (status) => (this.cacheStatus = status),
      error: () => (this.cacheStatus = null),
    });
  }

  loadRetrievalStatus(): void {
    this.api.getContextRetrievalStatus().subscribe({
      next: (status) => (this.retrievalStatus = status),
      error: () => (this.retrievalStatus = null),
    });
  }

  generateContextBrief(): void {
    const task = (this.contextBriefTask || this.objective || '').trim();
    if (!task) {
      this.notice = 'Enter objective or context brief task first.';
      return;
    }
    this.api
      .getContextBrief({
        task,
        session_id: this.sessionId,
        max_items: 15,
        max_tokens: 1600,
      })
      .subscribe({
        next: (res) => {
          this.contextBrief = res;
          this.notice = 'Context brief generated.';
        },
        error: (err) => (this.error = err.error?.detail || err.message || 'Failed to generate context brief'),
      });
  }

  loadRetrievalEvalStatus(): void {
    this.api.getRetrievalEvalStatus(this.sessionId).subscribe({
      next: (res) => (this.retrievalEvalStatus = res),
      error: () => (this.retrievalEvalStatus = null),
    });
  }

  runRetrievalEval(): void {
    const task = (this.contextBriefTask || this.objective || '').trim();
    const tasks = task ? [task] : undefined;
    this.api
      .runRetrievalEval({
        session_id: this.sessionId,
        tasks,
        with_brief: true,
      })
      .subscribe({
        next: (res) => {
          this.notice = `Retrieval eval completed: ${res.run_id}`;
          this.loadRetrievalEvalStatus();
        },
        error: (err) => (this.error = err.error?.detail || err.message || 'Failed to run retrieval eval'),
      });
  }

  loadWorkflowTemplates(): void {
    this.api.getWorkflowTemplates(this.sessionId, 12).subscribe({
      next: (res) => (this.workflowTemplates = res.templates || []),
      error: () => (this.workflowTemplates = []),
    });
  }

  workflowSteps(template: WorkflowTemplateItem): string[] {
    const raw = template?.graph?.['steps'];
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => String(item || '').trim()).filter((item) => item.length > 0).slice(0, 6);
  }

  workflowMetric(template: WorkflowTemplateItem, key: string): number {
    const value = template?.graph?.[key];
    const asNumber = Number(value ?? 0);
    if (!Number.isFinite(asNumber)) return 0;
    return Math.max(0, Math.floor(asNumber));
  }

  contextBriefTopConstraints(): string {
    if (!this.contextBrief || !Array.isArray(this.contextBrief.constraints_preferences)) return '';
    return this.contextBrief.constraints_preferences
      .slice(0, 3)
      .map((item) => String(item?.text || '').trim())
      .filter((item) => item.length > 0)
      .join(' | ');
  }

  private captureStepMeta(res: any): void {
    this.lastStepMeta = {
      context_quality_score: Number(res?.context_quality_score ?? 0),
      retrieval_triggered: Boolean(res?.retrieval_triggered),
      retrieval_source: String(res?.retrieval_source ?? 'none'),
      retrieval_reason: res?.retrieval_reason ?? null,
      retrieval_latency_ms: Number(res?.retrieval_latency_ms ?? 0),
      retrieval_hit_count: Number(res?.retrieval_hit_count ?? 0),
    };
  }

  // --- Action methods (all unchanged) ---

  activate(mode: 'takeover' | 'suggest'): void {
    this.api
      .takeoverStep({
        session_id: this.sessionId,
        persona_mode: this.personaMode,
        message: `dashboard activate ${mode}`,
        activation_mode_default: mode,
        task: this.objective || undefined,
      })
      .subscribe({
        next: (res) => {
          this.state = res?.state ?? this.state;
          this.captureStepMeta(res);
          this.notice = `${mode} mode activation requested.`;
          this.loadAutonomy();
        },
        error: (err) => (this.error = err.error?.detail || err.message || `Failed to activate ${mode}`),
      });
  }

  standDown(): void {
    this.api
      .takeoverStep({
        session_id: this.sessionId,
        persona_mode: this.personaMode,
        message: 'advisor stand down',
        activation_mode_default: 'takeover',
      })
      .subscribe({
        next: (res) => {
          this.state = res?.state ?? this.state;
          this.captureStepMeta(res);
          this.notice = 'Stand down command sent.';
          this.loadAutonomy();
        },
        error: (err) => (this.error = err.error?.detail || err.message || 'Failed to stand down'),
      });
  }

  reset(): void {
    this.api.takeoverReset(this.sessionId, this.personaMode).subscribe({
      next: (state) => {
        this.state = state;
        this.notice = 'Takeover session reset.';
        this.loadAutonomy();
        this.loadGoals();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to reset takeover state'),
    });
  }

  preload(): void {
    const task = this.objective.trim();
    if (!task) {
      this.notice = 'Enter an objective first.';
      return;
    }
    this.api
      .takeoverPreload({
        session_id: this.sessionId,
        persona_mode: this.personaMode,
        task,
        app_context: {},
        constraints: { k: 8 },
      })
      .subscribe({
        next: () => {
          this.notice = 'Objective preloaded.';
          this.refreshAll();
        },
        error: (err) => (this.error = err.error?.detail || err.message || 'Failed to preload objective'),
      });
  }

  discoverGoals(): void {
    this.api.takeoverDiscoverGoals(this.sessionId, true).subscribe({
      next: (res) => {
        this.goals = res.goals || [];
        this.notice = `Discovered ${this.goals.length} goals.`;
        this.loadAutonomy();
        this.loadCacheStatus();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to discover goals'),
    });
  }

  runStep(): void {
    const message = this.stepMessage.trim() || 'dashboard status step';
    this.api
      .takeoverStep({
        session_id: this.sessionId,
        persona_mode: this.personaMode,
        message,
        activation_mode_default: this.state?.mode || 'takeover',
        task: this.objective || undefined,
      })
      .subscribe({
        next: (res) => {
          this.state = res?.state ?? this.state;
          this.captureStepMeta(res);
          this.notice = 'Takeover step executed.';
          this.loadAutonomy();
        },
        error: (err) => (this.error = err.error?.detail || err.message || 'Failed to run takeover step'),
      });
  }

  precomputeGoals(): void {
    this.api.takeoverPrecomputeGoals(this.sessionId, true, true).subscribe({
      next: (res) => {
        this.goals = res.goals || [];
        this.notice = `Precomputed ${this.goals.length} goals and warmed cache.`;
        this.loadAutonomy();
        this.loadCacheStatus();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to precompute goals'),
    });
  }

  runAutonomyTick(): void {
    this.api.takeoverAutonomyTick(this.sessionId, true, 20).subscribe({
      next: (res) => {
        this.notice = `Autonomy tick complete: sessions=${res.sessions_scanned}, notices=${res.notices_created}.`;
        this.refreshAll();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to run autonomy tick'),
    });
  }

  ackNotice(noticeId: string, selectGoal: boolean): void {
    this.api.ackTakeoverNotice(noticeId, this.sessionId, selectGoal).subscribe({
      next: () => {
        this.notice = selectGoal ? 'Notice acknowledged and goal selected.' : 'Notice acknowledged.';
        this.refreshAll();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to acknowledge notice'),
    });
  }

  invalidateGoalCache(): void {
    this.api.invalidateTakeoverGoalCache(this.sessionId).subscribe({
      next: () => {
        this.notice = 'Goal cache invalidated.';
        this.loadCacheStatus();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to invalidate goal cache'),
    });
  }

  selectGoal(goalId: string): void {
    this.api.takeoverSelectGoal(goalId, this.sessionId).subscribe({
      next: (goal) => {
        this.notice = `Selected goal: ${goal.title}`;
        this.refreshAll();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to select goal'),
    });
  }

  requestPermit(): void {
    const paths = this.permitTargetPaths
      .split(',')
      .map((item) => item.trim())
      .filter((item) => item.length > 0);
    this.api
      .requestExecutionPermit({
        session_id: this.sessionId,
        action_kind: this.permitActionKind || 'edit',
        target_paths: paths,
        command_preview: this.permitCommandPreview || null,
        estimated_change_size: Number(this.permitChangeSize || 0),
      })
      .subscribe({
        next: (permit) => {
          this.lastPermit = permit;
          this.notice = `Permit decision: ${permit.decision}`;
          this.loadAutonomy();
        },
        error: (err) => (this.error = err.error?.detail || err.message || 'Failed to request permit'),
      });
  }

  resolvePermit(approved: boolean): void {
    if (!this.lastPermit) return;
    this.api.resolveExecutionPermit(this.lastPermit.permit_id, approved).subscribe({
      next: (permit) => {
        this.lastPermit = permit;
        this.notice = approved ? 'Permit approved.' : 'Permit denied.';
        this.loadAutonomy();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to resolve permit'),
    });
  }

  claimLatestDirective(): void {
    const latest = this.pendingExecutions[0];
    if (!latest) return;
    this.api.claimExecution(this.sessionId, latest.directive_id).subscribe({
      next: (directive) => {
        this.notice = `Claimed directive ${directive.directive_id}`;
        this.refreshAll();
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to claim directive'),
    });
  }

  recomputeHumanScore(): void {
    this.api.recomputeDashboardHumanScore(this.sessionId).subscribe({
      next: (score) => {
        this.notice = `Human score recomputed: ${score.score} (${score.band}).`;
      },
      error: (err) => (this.error = err.error?.detail || err.message || 'Failed to recompute human score'),
    });
  }

  switchSession(nextSessionId: string): void {
    const sid = String(nextSessionId || '').trim();
    if (!sid || sid === this.sessionId) return;
    this.sessionId = sid;
    const matched = this.activeSessionOptions.find((item) => item.sessionId === sid);
    if (matched?.objective && !this.objective.trim()) {
      this.objective = matched.objective;
    }
    this.notice = `Switched to session '${sid}'.`;
    this.refreshAll();
  }

  reloadActiveSessions(): void {
    void this.resolveLatestActiveSession(false).then(() => {
      this.notice = this.activeSessionOptions.length
        ? `Loaded ${this.activeSessionOptions.length} active session(s).`
        : 'No active sessions found.';
    });
  }

  private upsertActiveSessionFromState(state: TakeoverState | null): void {
    if (!state || !state.session_id) return;
    const sid = String(state.session_id || '').trim();
    if (!sid) return;

    const existingIdx = this.activeSessionOptions.findIndex((item) => item.sessionId === sid);
    if (!state.active) {
      if (existingIdx >= 0) {
        this.activeSessionOptions.splice(existingIdx, 1);
      }
      return;
    }

    const next = {
      sessionId: sid,
      objective: String(state.takeover_context?.objective || '').trim(),
      updatedAt: state.updated_at || null,
      mode: String(state.mode || 'takeover'),
      personaMode: String(state.persona_mode || 'normal'),
    };
    if (existingIdx >= 0) {
      this.activeSessionOptions[existingIdx] = next;
    } else {
      this.activeSessionOptions.push(next);
    }

    this.activeSessionOptions.sort((a, b) => this.toEpochMs(b.updatedAt) - this.toEpochMs(a.updatedAt));
    if (this.activeSessionOptions.length > 20) {
      this.activeSessionOptions = this.activeSessionOptions.slice(0, 20);
    }
  }
}
