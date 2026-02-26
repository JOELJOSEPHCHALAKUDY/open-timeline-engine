import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import {
  DashboardGoalIntelligenceItem,
  DashboardGoalsIntelligenceResponse,
  DashboardHumanScoreHistoryResponse,
  DashboardHumanScoreResponse,
  GoalRelationEdge,
} from '../../core/models';

@Component({
  selector: 'app-goal-intelligence',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div class="page-header">
      <h1>Goal Intelligence</h1>
      <p>Track long-term goals, relation edges, affective signals, and human-level score trends.</p>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div style="display:grid;grid-template-columns:1fr auto auto auto;gap:8px;align-items:center;">
        <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" />
        <button class="btn btn-primary" (click)="loadAll()">Refresh</button>
        <button class="btn" (click)="recomputeScore()">Recompute Human Score</button>
        <a class="btn" routerLink="/takeover">Open Takeover</a>
      </div>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading goal intelligence…</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>
    <div *ngIf="notice" class="card" style="margin-bottom:12px;">{{ notice }}</div>

    <div class="grid grid-4" *ngIf="intelligence">
      <div class="card">
        <div class="stat-value">{{ intelligence.summary.total_goals }}</div>
        <div class="stat-label">total goals</div>
      </div>
      <div class="card">
        <div class="stat-value">{{ intelligence.summary.long_term_goals }}</div>
        <div class="stat-label">long-term goals</div>
      </div>
      <div class="card">
        <div class="stat-value">{{ intelligence.summary.goal_event_edges }}</div>
        <div class="stat-label">goal-event edges</div>
      </div>
      <div class="card">
        <div class="stat-value">{{ intelligence.summary.goal_emotion_edges + intelligence.summary.goal_goal_edges }}</div>
        <div class="stat-label">emotion + goal edges</div>
      </div>
    </div>

    <div class="grid grid-2" style="margin-top:16px;" *ngIf="humanScore">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Human-Level Score</span>
          <span class="badge badge-accent">{{ humanScore.band }}</span>
        </div>
        <div class="stat-value" style="font-size:36px;margin-top:8px;">{{ humanScore.score }}</div>
        <div class="stat-label">0-100 hybrid autonomy index</div>
        <div style="margin-top:12px;">
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Clone readiness</div>
          <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.clone_readiness"></div></div>
        </div>
        <div style="margin-top:10px;">
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Execution quality</div>
          <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.execution_quality"></div></div>
        </div>
        <div style="margin-top:10px;">
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Goal coherence</div>
          <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.goal_coherence"></div></div>
        </div>
        <div style="margin-top:10px;">
          <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Affective alignment</div>
          <div class="progress-bar"><div class="progress-fill" [style.width.%]="humanScore.subscores.affective_alignment"></div></div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-title">7 / 30 day trend</span>
          <span class="badge badge-muted">{{ history?.points?.length || 0 }} points</span>
        </div>
        <div *ngIf="!history || history.points.length === 0" style="color:var(--muted);margin-top:8px;">No score history yet.</div>
        <div *ngFor="let point of (history?.points || []).slice(-10)" style="display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:6px 0;font-size:13px;">
          <span>{{ point.ts | date:'short' }}</span>
          <span>
            <span class="badge badge-muted">{{ point.band }}</span>
            <strong style="margin-left:8px;">{{ point.score }}</strong>
          </span>
        </div>
      </div>
    </div>

    <div class="grid grid-2" style="margin-top:16px;" *ngIf="intelligence">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Long-term goals</span>
          <span class="badge badge-accent">{{ longTermGoals.length }}</span>
        </div>
        <div *ngIf="longTermGoals.length === 0" style="color:var(--muted);margin-top:10px;">No long-term goals detected.</div>
        <div *ngFor="let goal of longTermGoals" style="border-top:1px solid var(--line);padding:10px 0;">
          <div style="font-weight:600;">{{ goal.title }}</div>
          <div style="font-size:12px;color:var(--muted);margin-top:2px;">
            reason={{ goal.long_term_reason }} · age={{ goal.age_days }} days · status={{ goal.status }}
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;">
            <span class="badge badge-muted" *ngFor="let emotion of goal.top_emotions">
              {{ emotion.name }}={{ emotion.value | number:'1.2-2' }}
            </span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-title">Decision path graph</span>
          <span class="badge badge-muted">derived from relation edges</span>
        </div>
        <div *ngIf="relationEdges.length === 0" style="color:var(--muted);margin-top:8px;">No relation edges available.</div>
        <div *ngFor="let edge of relationEdges.slice(0, 14)" style="display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:6px 0;font-size:13px;">
          <span>{{ edge.source_goal_id }} → {{ edge.target_id }}</span>
          <span>
            <span class="badge badge-muted">{{ edge.relation_type }}</span>
            <span style="margin-left:8px;">w={{ edge.weight | number:'1.2-2' }}</span>
          </span>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px;" *ngIf="intelligence">
      <div class="card-header">
        <span class="card-title">Goal–Emotion matrix</span>
        <span class="badge badge-muted">top affective links</span>
      </div>
      <div style="overflow:auto;max-height:360px;">
        <table class="table">
          <thead>
            <tr>
              <th>Goal</th>
              <th>Status</th>
              <th>Pain</th>
              <th>Happy</th>
              <th>Anger</th>
              <th>Anxiety</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let goal of intelligence.goals">
              <td>{{ goal.title }}</td>
              <td>{{ goal.status }}</td>
              <td>{{ emotionValue(goal, 'pain') | number:'1.2-2' }}</td>
              <td>{{ emotionValue(goal, 'happy') | number:'1.2-2' }}</td>
              <td>{{ emotionValue(goal, 'anger') | number:'1.2-2' }}</td>
              <td>{{ emotionValue(goal, 'anxiety') | number:'1.2-2' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
})
export class GoalIntelligenceComponent implements OnInit {
  sessionId = 'default';
  loading = false;
  error: string | null = null;
  notice: string | null = null;
  intelligence: DashboardGoalsIntelligenceResponse | null = null;
  humanScore: DashboardHumanScoreResponse | null = null;
  history: DashboardHumanScoreHistoryResponse | null = null;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    void this.bootstrap();
  }

  private async bootstrap(): Promise<void> {
    await this.initializeSessionId();
    this.loadAll();
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

  get longTermGoals(): DashboardGoalIntelligenceItem[] {
    return (this.intelligence?.goals || []).filter((goal) => goal.long_term);
  }

  get relationEdges(): GoalRelationEdge[] {
    const edges: GoalRelationEdge[] = [];
    for (const goal of this.intelligence?.goals || []) {
      for (const relation of goal.relations || []) {
        edges.push(relation);
      }
    }
    return edges;
  }

  emotionValue(goal: DashboardGoalIntelligenceItem, key: string): number {
    const affective = (goal.affective_scores || {}) as Record<string, unknown>;
    const emotional = (affective['emotional'] || {}) as Record<string, unknown>;
    const raw = Number(emotional[key] ?? 0);
    return Number.isFinite(raw) ? raw : 0;
  }

  loadAll(): void {
    this.loading = true;
    this.error = null;
    this.notice = null;
    this.api.getDashboardGoalsIntelligence(this.sessionId).subscribe({
      next: (intelligence) => {
        this.intelligence = intelligence;
        this.api.getDashboardHumanScore(this.sessionId).subscribe({
          next: (score) => {
            this.humanScore = score;
            this.api.getDashboardHumanScoreHistory(this.sessionId, 30).subscribe({
              next: (history) => {
                this.history = history;
                this.loading = false;
              },
              error: (err) => {
                this.error = err?.error?.detail || err?.message || 'Failed to load score history';
                this.loading = false;
              },
            });
          },
          error: (err) => {
            this.error = err?.error?.detail || err?.message || 'Failed to load human score';
            this.loading = false;
          },
        });
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load goal intelligence';
        this.loading = false;
      },
    });
  }

  recomputeScore(): void {
    this.notice = null;
    this.error = null;
    this.api.recomputeDashboardHumanScore(this.sessionId).subscribe({
      next: (score) => {
        this.humanScore = score;
        this.notice = `Human score recomputed: ${score.score} (${score.band}).`;
        this.api.getDashboardHumanScoreHistory(this.sessionId, 30).subscribe({
          next: (history) => (this.history = history),
          error: () => undefined,
        });
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to recompute score';
      },
    });
  }
}
