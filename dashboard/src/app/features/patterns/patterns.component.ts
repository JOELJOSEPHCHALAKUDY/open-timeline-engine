import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiService } from '../../core/services/api.service';
import { PatternItem } from '../../core/models';

@Component({
  selector: 'app-patterns',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div class="page-header">
      <h1>Pattern Review Queue</h1>
      <p>Review high-impact behavior patterns first, then approve or reject with context.</p>
    </div>

    <div class="card" style="margin-bottom: 20px;">
      <div class="filter-bar" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px;">
        <input class="input" [(ngModel)]="domain" placeholder="Domain (optional)" />
        <input class="input" [(ngModel)]="minConfidenceText" placeholder="Min confidence (0-1)" />
        <button class="btn btn-primary" (click)="loadPatterns()">Refresh queue</button>
      </div>
      <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px;">
        <button class="btn" [class.btn-primary]="viewMode === 'needs_review'" (click)="viewMode = 'needs_review'">
          Needs review ({{ needsReviewCount }})
        </button>
        <button class="btn" [class.btn-primary]="viewMode === 'active'" (click)="viewMode = 'active'">
          Active ({{ activeCount }})
        </button>
        <button class="btn" [class.btn-primary]="viewMode === 'all'" (click)="viewMode = 'all'">
          All ({{ patterns.length }})
        </button>
      </div>
    </div>

    <div class="grid grid-4" style="margin-bottom: 20px;" *ngIf="!loading">
      <div class="card">
        <div class="stat-value">{{ needsReviewCount }}</div>
        <div class="stat-label">needs review</div>
      </div>
      <div class="card">
        <div class="stat-value">{{ activeCount }}</div>
        <div class="stat-label">active</div>
      </div>
      <div class="card">
        <div class="stat-value">{{ deprecatedCount }}</div>
        <div class="stat-label">deprecated</div>
      </div>
      <div class="card">
        <div class="stat-value">{{ averageConfidence | number:'1.2-2' }}</div>
        <div class="stat-label">avg confidence</div>
      </div>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading pattern queue...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>
    <div *ngIf="notice" class="card" style="margin-bottom: 20px; border-left: 3px solid var(--accent);">{{ notice }}</div>

    <div *ngIf="!loading" class="grid grid-1">
      <div class="card" *ngIf="filteredPatterns.length === 0">
        <div style="font-size: 14px; color: var(--muted);">
          No patterns found for the selected queue and filters.
        </div>
      </div>

      <div class="card" *ngFor="let pattern of filteredPatterns" style="margin-bottom: 12px;">
        <div class="card-header">
          <span class="card-title">{{ pattern.pattern_type }}</span>
          <div style="display: flex; gap: 8px; align-items: center;">
            <span class="badge" [ngClass]="statusBadgeClass(pattern.status)">{{ pattern.status }}</span>
            <span class="badge" [class.badge-success]="pattern.confidence >= 0.8" [class.badge-accent]="pattern.confidence >= 0.5 && pattern.confidence < 0.8" [class.badge-muted]="pattern.confidence < 0.5">
              {{ pattern.confidence | number:'1.2-2' }}
            </span>
          </div>
        </div>

        <div style="font-size: 14px; margin-top: 10px;">{{ pattern.statement }}</div>
        <div style="font-size: 12px; color: var(--muted); margin-top: 6px;">
          Domain: {{ pattern.domain }} · Evidence count: {{ pattern.evidence_event_ids.length }}
        </div>

        <div style="margin-top: 10px; padding: 10px; background: #f8fafc; border-radius: 8px;">
          <div style="font-size: 12px; color: var(--muted); margin-bottom: 6px;">Estimated impact if approved</div>
          <div style="font-size: 13px;">{{ impactPreview(pattern) }}</div>
        </div>

        <div style="margin-top: 10px;">
          <div style="font-size: 12px; color: var(--muted); margin-bottom: 6px;">Evidence event IDs</div>
          <div style="display: flex; gap: 6px; flex-wrap: wrap;">
            <span class="badge badge-muted" *ngFor="let evidence of pattern.evidence_event_ids | slice:0:6">{{ evidence }}</span>
          </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 12px;">
          <input class="input" [(ngModel)]="feedbackNotes[pattern.id]" placeholder="Optional review note" />
          <a class="btn" [routerLink]="['/timeline']">Open timeline</a>
        </div>

        <div style="display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap;">
          <button class="btn btn-primary" [disabled]="submittingId === pattern.id" (click)="submitFeedback(pattern, true)">Approve</button>
          <button class="btn" [disabled]="submittingId === pattern.id" (click)="submitFeedback(pattern, false)">Reject</button>
        </div>
      </div>
    </div>
  `,
})
export class PatternsComponent implements OnInit {
  domain = '';
  minConfidenceText = '0.5';
  patterns: PatternItem[] = [];
  feedbackNotes: Record<string, string> = {};
  viewMode: 'needs_review' | 'active' | 'all' = 'needs_review';
  submittingId: string | null = null;
  loading = false;
  error: string | null = null;
  notice: string | null = null;

  constructor(private api: ApiService) {}

  get needsReviewCount(): number {
    return this.patterns.filter((p) => p.status === 'needs_review').length;
  }

  get activeCount(): number {
    return this.patterns.filter((p) => p.status === 'active').length;
  }

  get deprecatedCount(): number {
    return this.patterns.filter((p) => p.status === 'deprecated').length;
  }

  get averageConfidence(): number {
    if (this.patterns.length === 0) return 0;
    const total = this.patterns.reduce((sum, p) => sum + p.confidence, 0);
    return total / this.patterns.length;
  }

  get filteredPatterns(): PatternItem[] {
    const filtered = this.patterns.filter((pattern) => {
      if (this.viewMode === 'all') return true;
      return pattern.status === this.viewMode;
    });
    return filtered.sort((a, b) => {
      const statusOrder = this.statusRank(a.status) - this.statusRank(b.status);
      if (statusOrder !== 0) return statusOrder;
      return b.confidence - a.confidence;
    });
  }

  ngOnInit(): void {
    this.loadPatterns();
  }

  loadPatterns(): void {
    const minConfidence = Number(this.minConfidenceText);
    this.loading = true;
    this.error = null;
    this.notice = null;
    this.api.getPatterns({
      domain: this.domain.trim() || undefined,
      min_confidence: Number.isFinite(minConfidence) ? minConfidence : 0.5,
    }).subscribe({
      next: (patterns) => {
        this.patterns = Array.isArray(patterns) ? patterns : [];
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load patterns';
        this.loading = false;
      },
    });
  }

  impactPreview(pattern: PatternItem): string {
    const type = (pattern.pattern_type || '').toLowerCase();
    if (type.includes('workflow')) {
      return 'Bundle ranking will prefer this sequence when similar tasks are requested.';
    }
    if (type.includes('preference')) {
      return 'Advisor guidance will bias toward this option in future tradeoff decisions.';
    }
    if (type.includes('rule')) {
      return 'This rule will be surfaced as a stronger do/don’t constraint in context bundles.';
    }
    if (type.includes('anti')) {
      return 'System will warn earlier when execution starts matching this anti-pattern.';
    }
    return 'This pattern will influence future advisor guidance and retrieval ranking.';
  }

  statusBadgeClass(status: string): string {
    if (status === 'active') return 'badge-success';
    if (status === 'needs_review') return 'badge-accent';
    if (status === 'deprecated') return 'badge-muted';
    return 'badge-muted';
  }

  submitFeedback(pattern: PatternItem, approved: boolean): void {
    this.error = null;
    this.notice = null;
    this.submittingId = pattern.id;
    const note = (this.feedbackNotes[pattern.id] || '').trim() || undefined;
    this.api.submitPatternFeedback(pattern.id, approved, note).subscribe({
      next: (res) => {
        const status = typeof res?.['pattern_status'] === 'string'
          ? String(res['pattern_status'])
          : (approved ? 'active' : 'deprecated');
        pattern.status = status;
        this.notice = approved ? 'Pattern approved.' : 'Pattern rejected.';
        this.submittingId = null;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to submit pattern feedback';
        this.submittingId = null;
      },
    });
  }

  private statusRank(status: string): number {
    if (status === 'needs_review') return 0;
    if (status === 'active') return 1;
    if (status === 'deprecated') return 2;
    return 3;
  }
}
