import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import {
  ContextBriefResponse,
  ContextRetrievalStatusResponse,
  RetrievalEvalStatusResponse,
} from '../../core/models';
import { ApiService } from '../../core/services/api.service';

@Component({
  selector: 'app-retrieval-context',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div class="page-header">
      <h1>Retrieval & Context</h1>
      <p>Dedicated page for context brief generation and retrieval evaluation runs.</p>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div style="display:grid;grid-template-columns:1fr 1fr 120px 120px;gap:8px;align-items:center;">
        <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" />
        <input class="input" [(ngModel)]="task" placeholder="Task objective for context brief/eval" />
        <input class="input" type="number" min="1" max="30" [(ngModel)]="maxItems" placeholder="Max items" />
        <input class="input" type="number" min="200" max="4000" [(ngModel)]="maxTokens" placeholder="Max tokens" />
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
        <button class="btn" (click)="loadRetrievalStatus()">Refresh retrieval status</button>
        <button class="btn btn-primary" (click)="generateContextBrief()" [disabled]="loadingBrief">
          {{ loadingBrief ? 'Generating...' : 'Generate context brief' }}
        </button>
        <button class="btn" (click)="loadRetrievalEvalStatus()">Refresh eval status</button>
        <button class="btn btn-primary" (click)="runRetrievalEval()" [disabled]="runningEval">
          {{ runningEval ? 'Running...' : 'Run retrieval eval' }}
        </button>
        <a class="btn" routerLink="/takeover">Open Takeover</a>
      </div>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>
    <div *ngIf="notice" class="card" style="margin-bottom:12px;">{{ notice }}</div>

    <div class="grid grid-2">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Retrieval backend status</span>
          <span class="badge badge-muted">{{ retrievalStatus?.mode || 'unknown' }}</span>
        </div>
        <div *ngIf="!retrievalStatus" style="color:var(--muted);">No retrieval status loaded.</div>
        <div *ngIf="retrievalStatus" style="display:flex;gap:8px;flex-wrap:wrap;">
          <span class="badge badge-muted">trigger < {{ retrievalStatus.trigger_threshold }}</span>
          <span class="badge badge-muted">escalate < {{ retrievalStatus.escalate_threshold }}</span>
          <span class="badge badge-muted">budget {{ retrievalStatus.turn_budget_ms }}ms</span>
          <span class="badge badge-muted">timeout {{ retrievalStatus.backend_timeout_ms }}ms</span>
          <span class="badge badge-muted">qdrant {{ retrievalStatus.qdrant_enabled ? 'on' : 'off' }}</span>
        </div>
        <div *ngIf="retrievalStatus" style="margin-top:12px;">
          <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">Fallback counters</div>
          <table class="table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              <tr *ngFor="let item of (retrievalStatus.fallback_counters || {}) | keyvalue">
                <td>{{ item.key }}</td>
                <td>{{ item.value }}</td>
              </tr>
              <tr *ngIf="fallbackCounterEntries() === 0">
                <td colspan="2" style="color:var(--muted);">No fallback counters reported.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-title">Retrieval eval</span>
          <span class="badge badge-muted">{{ retrievalEvalStatus?.history?.length || 0 }} runs</span>
        </div>
        <div *ngIf="!retrievalEvalStatus?.latest" style="color:var(--muted);">No retrieval eval run yet.</div>
        <div *ngIf="retrievalEvalStatus?.latest as latest">
          <div style="font-weight:600;">{{ latest.run_id }}</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
            <span class="badge badge-muted">style {{ latest.style_alignment | number:'1.2-2' }}</span>
            <span class="badge badge-muted">constraints {{ latest.constraint_compliance | number:'1.2-2' }}</span>
            <span class="badge badge-muted">traceability {{ latest.decision_traceability | number:'1.2-2' }}</span>
            <span class="badge badge-muted">follow-up {{ latest.followup_reduction | number:'1.2-2' }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px;">
      <div class="card-header">
        <span class="card-title">Context brief</span>
        <span class="badge badge-muted">{{ contextBrief ? 'ready' : 'not generated' }}</span>
      </div>
      <div *ngIf="!contextBrief" style="color:var(--muted);">No context brief generated.</div>
      <div *ngIf="contextBrief">
        <div style="font-size:14px;margin-bottom:10px;">
          <strong>Summary:</strong> {{ contextBrief.summary }}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
          <span class="badge badge-muted">standard {{ contextBrief.standard_approach.length }}</span>
          <span class="badge badge-muted">state {{ contextBrief.current_state.length }}</span>
          <span class="badge badge-muted">constraints {{ contextBrief.constraints_preferences.length }}</span>
          <span class="badge badge-muted">open loops {{ contextBrief.open_loops.length }}</span>
          <span class="badge badge-muted">artifacts {{ contextBrief.artifacts.length }}</span>
          <span class="badge badge-muted">citations {{ contextBrief.citations.length }}</span>
        </div>

        <div class="grid grid-2">
          <div>
            <div class="card-title" style="margin-bottom:6px;">Standard approach</div>
            <ul style="padding-left:18px;">
              <li *ngFor="let item of contextBrief.standard_approach">{{ item.text }}</li>
            </ul>
          </div>
          <div>
            <div class="card-title" style="margin-bottom:6px;">Current state</div>
            <ul style="padding-left:18px;">
              <li *ngFor="let item of contextBrief.current_state">{{ item.text }}</li>
            </ul>
          </div>
          <div>
            <div class="card-title" style="margin-bottom:6px;">Constraints & preferences</div>
            <ul style="padding-left:18px;">
              <li *ngFor="let item of contextBrief.constraints_preferences">{{ item.text }}</li>
            </ul>
          </div>
          <div>
            <div class="card-title" style="margin-bottom:6px;">Open loops</div>
            <ul style="padding-left:18px;">
              <li *ngFor="let item of contextBrief.open_loops">{{ item.text }}</li>
            </ul>
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px;" *ngIf="retrievalEvalStatus?.history?.length">
      <div class="card-header">
        <span class="card-title">Eval history</span>
      </div>
      <div style="overflow:auto;max-height:420px;">
        <table class="table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Style</th>
              <th>Constraints</th>
              <th>Traceability</th>
              <th>Follow-up</th>
              <th>Completed</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let item of retrievalEvalStatus?.history">
              <td>{{ item.run_id }}</td>
              <td>{{ item.style_alignment | number:'1.2-2' }}</td>
              <td>{{ item.constraint_compliance | number:'1.2-2' }}</td>
              <td>{{ item.decision_traceability | number:'1.2-2' }}</td>
              <td>{{ item.followup_reduction | number:'1.2-2' }}</td>
              <td>{{ item.completed_at | date:'short' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
})
export class RetrievalContextComponent implements OnInit {
  sessionId = 'default';
  task = '';
  maxItems = 15;
  maxTokens = 1600;
  loadingBrief = false;
  runningEval = false;
  retrievalStatus: ContextRetrievalStatusResponse | null = null;
  contextBrief: ContextBriefResponse | null = null;
  retrievalEvalStatus: RetrievalEvalStatusResponse | null = null;
  error: string | null = null;
  notice: string | null = null;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    void this.bootstrap();
  }

  private async bootstrap(): Promise<void> {
    await this.initializeSessionId();
    this.loadRetrievalStatus();
    this.loadRetrievalEvalStatus();
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

  fallbackCounterEntries(): number {
    if (!this.retrievalStatus?.fallback_counters) return 0;
    return Object.keys(this.retrievalStatus.fallback_counters).length;
  }

  loadRetrievalStatus(): void {
    this.error = null;
    this.api.getContextRetrievalStatus().subscribe({
      next: (status) => (this.retrievalStatus = status),
      error: (err) => (this.error = err?.error?.detail || err?.message || 'Failed to load retrieval status'),
    });
  }

  generateContextBrief(): void {
    const task = this.task.trim();
    if (!task) {
      this.notice = 'Enter a task before generating a context brief.';
      return;
    }
    this.error = null;
    this.notice = null;
    this.loadingBrief = true;
    this.api
      .getContextBrief({
        task,
        session_id: this.sessionId,
        max_items: Math.max(1, Math.floor(this.maxItems || 1)),
        max_tokens: Math.max(200, Math.floor(this.maxTokens || 200)),
      })
      .subscribe({
        next: (resp) => {
          this.contextBrief = resp;
          this.notice = 'Context brief generated.';
          this.loadingBrief = false;
        },
        error: (err) => {
          this.error = err?.error?.detail || err?.message || 'Failed to generate context brief';
          this.loadingBrief = false;
        },
      });
  }

  loadRetrievalEvalStatus(): void {
    this.error = null;
    this.api.getRetrievalEvalStatus(this.sessionId).subscribe({
      next: (resp) => (this.retrievalEvalStatus = resp),
      error: (err) => (this.error = err?.error?.detail || err?.message || 'Failed to load retrieval eval status'),
    });
  }

  runRetrievalEval(): void {
    const task = this.task.trim();
    this.error = null;
    this.notice = null;
    this.runningEval = true;
    this.api
      .runRetrievalEval({
        session_id: this.sessionId,
        tasks: task ? [task] : undefined,
        with_brief: true,
      })
      .subscribe({
        next: (resp) => {
          this.notice = `Retrieval eval completed: ${resp.run_id}`;
          this.runningEval = false;
          this.loadRetrievalEvalStatus();
        },
        error: (err) => {
          this.error = err?.error?.detail || err?.message || 'Failed to run retrieval eval';
          this.runningEval = false;
        },
      });
  }
}
