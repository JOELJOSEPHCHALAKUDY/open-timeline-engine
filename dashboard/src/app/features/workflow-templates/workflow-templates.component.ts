import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { WorkflowTemplateItem } from '../../core/models';
import { ApiService } from '../../core/services/api.service';

@Component({
  selector: 'app-workflow-templates',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <!-- Page header -->
    <div class="page-header">
      <h1>Workflow Templates</h1>
      <p>Learned execution patterns from your takeover sessions.</p>
    </div>

    <!-- Explainer banner -->
    <div class="card" style="margin-bottom:16px;background:var(--accent-light,#0b7a7510);border:1px solid var(--accent,#0b7a75)30;">
      <div style="display:flex;gap:12px;align-items:flex-start;">
        <span style="font-size:20px;line-height:1;">&#129504;</span>
        <div style="font-size:13px;color:var(--muted);line-height:1.6;">
          Every time the takeover system completes a task, it records the steps taken and whether it succeeded.
          Over time these become reusable recipes that guide future similar tasks &mdash; the system's muscle memory.
        </div>
      </div>
    </div>

    <!-- Controls -->
    <div class="card" style="margin-bottom:16px;">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" style="flex:1;min-width:140px;" />
        <input class="input" type="number" min="1" max="100" [(ngModel)]="limit" placeholder="Limit" style="width:80px;" />
        <button class="btn btn-primary" (click)="load()" [disabled]="loading">
          {{ loading ? 'Loading...' : 'Refresh' }}
        </button>
        <a class="btn" routerLink="/takeover">Open Takeover</a>
      </div>
    </div>

    <!-- Loading -->
    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading workflow templates...</span>
    </div>

    <!-- Error -->
    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <!-- Stats strip -->
    <div class="grid grid-4" style="margin-bottom:16px;" *ngIf="!loading && templates.length > 0">
      <div class="card" style="text-align:center;">
        <div class="card-title" style="margin-bottom:8px;">TEMPLATES</div>
        <div class="stat-value">{{ templates.length }}</div>
        <div class="stat-label">learned</div>
      </div>
      <div class="card" style="text-align:center;">
        <div class="card-title" style="margin-bottom:8px;">AVG SUCCESS</div>
        <div class="stat-value" [style.color]="rateColor(avgSuccessRate)">{{ avgSuccessRate }}%</div>
        <div style="margin-top:8px;">
          <div class="progress-bar">
            <div class="progress-fill" [style.width.%]="avgSuccessRate" [style.background]="rateColor(avgSuccessRate)"></div>
          </div>
        </div>
      </div>
      <div class="card" style="text-align:center;">
        <div class="card-title" style="margin-bottom:8px;">TOTAL RUNS</div>
        <div class="stat-value">{{ totalExecutions }}</div>
        <div class="stat-label">executions</div>
      </div>
      <div class="card" style="text-align:center;">
        <div class="card-title" style="margin-bottom:8px;">LAST LEARNED</div>
        <div class="stat-value" style="font-size:22px;">{{ lastLearnedTime }}</div>
        <div class="stat-label" *ngIf="lastLearnedVersion">v{{ lastLearnedVersion }}</div>
      </div>
    </div>

    <!-- Empty state -->
    <div class="card" *ngIf="!loading && templates.length === 0 && !error" style="text-align:center;padding:48px 24px;">
      <div style="font-size:40px;margin-bottom:12px;">&#129504;</div>
      <div style="font-size:18px;font-weight:600;color:var(--ink);margin-bottom:8px;">No workflow templates yet</div>
      <div style="color:var(--muted);max-width:480px;margin:0 auto;line-height:1.6;">
        Templates are automatically created as the takeover system completes tasks.
        Activate takeover mode and complete a few tasks to start building your library.
      </div>
      <a class="btn btn-primary" routerLink="/takeover" style="margin-top:16px;display:inline-block;">
        Go to Takeover
      </a>
    </div>

    <!-- Template cards grid -->
    <div class="grid grid-2" *ngIf="!loading && templates.length > 0">
      <div class="card" *ngFor="let t of templates" style="display:flex;flex-direction:column;gap:12px;">
        <!-- Header: readable name + version -->
        <div>
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
            <div style="font-size:16px;font-weight:600;color:var(--ink);">{{ parseReadableName(t.name) }}</div>
            <span class="badge badge-muted">v{{ t.version }}</span>
          </div>
          <div style="font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:2px;word-break:break-all;">
            {{ t.name }}
          </div>
        </div>

        <!-- Step flow -->
        <div *ngIf="workflowSteps(t).length > 0" style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">
          <ng-container *ngFor="let step of workflowSteps(t); let last = last">
            <span style="font-size:12px;color:var(--ink);background:var(--accent-light,#0b7a7515);padding:3px 8px;border-radius:4px;white-space:nowrap;">
              {{ step }}
            </span>
            <span *ngIf="!last" style="color:var(--muted);font-size:11px;">&rarr;</span>
          </ng-container>
        </div>
        <div *ngIf="workflowSteps(t).length === 0" style="font-size:12px;color:var(--muted);font-style:italic;">
          No steps recorded
        </div>

        <!-- Success rate bar -->
        <div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="font-size:12px;color:var(--muted);">Success rate</span>
            <span style="font-size:13px;font-weight:600;" [style.color]="rateColor(successRate(t))">
              {{ successRate(t) }}%
            </span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" [style.width.%]="successRate(t)" [style.background]="rateColor(successRate(t))"></div>
          </div>
        </div>

        <!-- Run stats + time footer -->
        <div style="display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--line);padding-top:10px;font-size:12px;color:var(--muted);">
          <span>
            {{ totalRuns(t) }} runs
            <span *ngIf="totalRuns(t) > 0">
              &middot;
              <span style="color:var(--success);">{{ workflowMetric(t, 'success_count') }} &#10003;</span>
              &middot;
              <span style="color:var(--danger);">{{ workflowMetric(t, 'failure_count') }} &#10007;</span>
            </span>
          </span>
          <span>{{ relativeTime(t.updated_at) }}</span>
        </div>
      </div>
    </div>
  `,
})
export class WorkflowTemplatesComponent implements OnInit {
  sessionId = 'default';
  limit = 100;
  loading = false;
  error: string | null = null;
  templates: WorkflowTemplateItem[] = [];

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    void this.bootstrap();
  }

  private async bootstrap(): Promise<void> {
    await this.initializeSessionId();
    this.load();
  }

  private async initializeSessionId(): Promise<void> {
    try {
      const config = await firstValueFrom(this.api.getDashboardClientConfig());
      const configured = String(config?.default_session_id || '').trim();
      if (configured) {
        this.sessionId = configured;
      }
    } catch {
      // keep fallback "default"
    }
  }

  load(): void {
    this.loading = true;
    this.error = null;
    this.api.getWorkflowTemplates(this.sessionId, Math.max(1, Math.floor(this.limit || 1))).subscribe({
      next: (resp) => {
        this.templates = resp.templates || [];
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load workflow templates';
        this.loading = false;
      },
    });
  }

  // --- Template helpers ---

  parseReadableName(name: string): string {
    if (!name) return 'Unnamed Template';
    // Skill template: "execute::abc123" → "Execute Workflow"
    if (name.includes('::')) {
      const actionKind = name.split('::')[0];
      const label = actionKind.charAt(0).toUpperCase() + actionKind.slice(1);
      return `${label} Workflow`;
    }
    // Pattern template: "domain:behavioral:uuid" → "Behavioral Pattern"
    const parts = name.split(':');
    if (parts.length >= 3) {
      const patternType = parts[parts.length - 2];
      if (patternType && patternType.length > 2) {
        return (patternType.charAt(0).toUpperCase() + patternType.slice(1)).replace(/_/g, ' ') + ' Pattern';
      }
    }
    // Fallback: capitalize first meaningful segment
    const first = parts[0] || name;
    return first.charAt(0).toUpperCase() + first.slice(1);
  }

  workflowSteps(template: WorkflowTemplateItem): string[] {
    const raw = template?.graph?.['steps'];
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => String(item || '').trim()).filter((item) => item.length > 0).slice(0, 6);
  }

  workflowMetric(template: WorkflowTemplateItem, key: string): number {
    const value = template?.graph?.[key];
    const parsed = Number(value ?? 0);
    if (!Number.isFinite(parsed)) return 0;
    return Math.max(0, Math.floor(parsed));
  }

  successRate(template: WorkflowTemplateItem): number {
    const s = this.workflowMetric(template, 'success_count');
    const f = this.workflowMetric(template, 'failure_count');
    const total = s + f;
    return total > 0 ? Math.round((s / total) * 100) : 0;
  }

  totalRuns(template: WorkflowTemplateItem): number {
    return this.workflowMetric(template, 'success_count') + this.workflowMetric(template, 'failure_count');
  }

  rateColor(rate: number): string {
    if (rate >= 70) return 'var(--success)';
    if (rate >= 40) return 'var(--warn)';
    return 'var(--danger)';
  }

  relativeTime(dateStr: string | null): string {
    if (!dateStr) return '-';
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    if (isNaN(then)) return '-';
    const diffMs = now - then;
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    return `${Math.floor(days / 30)}mo ago`;
  }

  // --- Aggregate stats ---

  get avgSuccessRate(): number {
    if (this.templates.length === 0) return 0;
    let totalSuccess = 0;
    let totalAll = 0;
    for (const t of this.templates) {
      const s = this.workflowMetric(t, 'success_count');
      const f = this.workflowMetric(t, 'failure_count');
      totalSuccess += s;
      totalAll += s + f;
    }
    return totalAll > 0 ? Math.round((totalSuccess / totalAll) * 100) : 0;
  }

  get totalExecutions(): number {
    let total = 0;
    for (const t of this.templates) {
      total += this.workflowMetric(t, 'success_count') + this.workflowMetric(t, 'failure_count');
    }
    return total;
  }

  get lastLearnedTime(): string {
    if (this.templates.length === 0) return '-';
    // Templates come sorted by updated_at DESC from the API
    return this.relativeTime(this.templates[0].updated_at);
  }

  get lastLearnedVersion(): number {
    if (this.templates.length === 0) return 0;
    return this.templates[0].version || 0;
  }
}
