import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import {
  BehaviorMemoryReview,
  BehaviorShadowStatus,
  ContinuityPilotStatus,
} from '../../core/models';
import { ApiService } from '../../core/services/api.service';

@Component({
  selector: 'app-behavior-review',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="page-header control-header">
      <div>
        <p class="eyebrow">Behavior control plane</p>
        <h1>Review & Drift</h1>
        <p>Approve learned memory, watch behavioral drift, and measure whether handoffs actually help.</p>
      </div>
      <button class="btn" (click)="load()" [disabled]="loading">{{ loading ? 'Refreshing...' : 'Refresh' }}</button>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <section class="metric-band" *ngIf="pilot">
      <article><span>Capture coverage</span><strong>{{ percent(pilot.handoff_capture_coverage) }}</strong><small>{{ pilot.captured_completion_count }} / {{ pilot.eligible_completion_count }} completions</small></article>
      <article><span>Correct file</span><strong>{{ percent(pilot.correct_file_rate) }}</strong><small>{{ pilot.feedback_count }} measured resumes</small></article>
      <article><span>Correction rate</span><strong>{{ percent(pilot.correction_rate) }}</strong><small>Lower is better</small></article>
      <article><span>Median resume age</span><strong>{{ duration(pilot.median_time_to_resume_ms) }}</strong><small>p95 {{ duration(pilot.p95_time_to_resume_ms) }}</small></article>
      <article [class.alert]="pilot.outbox_dead_count > 0"><span>Outbox health</span><strong>{{ pilot.outbox_pending_count }} pending</strong><small>{{ pilot.outbox_dead_count }} dead letter</small></article>
    </section>

    <div class="control-grid">
      <section class="card drift-card">
        <div class="card-header"><span class="card-title">Shadow fidelity</span><span class="badge" [class.badge-danger]="metricBool('drift_alert')" [class.badge-success]="!metricBool('drift_alert')">{{ metricBool('drift_alert') ? 'Drift alert' : 'Stable' }}</span></div>
        <div class="drift-score">{{ percent(metricNumber('precision')) }}</div>
        <p>Precision when the clone chooses not to abstain.</p>
        <div class="meter"><i [style.width.%]="metricNumber('precision') * 100"></i></div>
        <dl>
          <div><dt>Coverage</dt><dd>{{ percent(metricNumber('coverage')) }}</dd></div>
          <div><dt>Abstention</dt><dd>{{ percent(metricNumber('abstention_rate')) }}</dd></div>
          <div><dt>Drift delta</dt><dd>{{ signedPercent(metricNumber('drift_delta')) }}</dd></div>
          <div><dt>Samples</dt><dd>{{ metricNumber('sample_count') }}</dd></div>
        </dl>
      </section>

      <section class="card queue-card">
        <div class="card-header"><span class="card-title">Memory review queue</span><span class="badge badge-accent">{{ reviews.length }} pending</span></div>
        <div class="empty" *ngIf="!reviews.length">No memory candidates need review.</div>
        <article class="review-row" *ngFor="let review of reviews">
          <div class="review-copy">
            <div><span class="type">{{ review.target_type }}</span><strong>{{ review.title }}</strong></div>
            <p>{{ review.rationale || 'No rationale supplied.' }}</p>
            <small>{{ review.source }} · score {{ review.score | number:'1.2-2' }}</small>
          </div>
          <div class="review-actions">
            <input class="input" [(ngModel)]="notes[review.review_id]" placeholder="Review note" />
            <button class="btn btn-primary btn-sm" (click)="resolve(review, 'promote')">Promote</button>
            <button class="btn btn-sm" (click)="resolve(review, 'reject')">Reject</button>
          </div>
        </article>
      </section>
    </div>
  `,
  styles: [`
    .control-header{display:flex;align-items:flex-end;justify-content:space-between;gap:20px}.eyebrow{font:600 11px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin:0 0 5px}.metric-band{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--line);background:linear-gradient(135deg,#f0fdfa,#fff 48%,#fff7ed);margin-bottom:22px}.metric-band article{padding:18px;border-right:1px solid var(--line)}.metric-band span,.metric-band small{display:block;color:var(--muted);font-size:12px}.metric-band strong{display:block;font:700 23px var(--mono);margin:5px 0;color:var(--ink)}.metric-band .alert strong{color:var(--danger)}.control-grid{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(420px,1.7fr);gap:20px}.drift-score{font:700 54px var(--mono);letter-spacing:-.06em}.drift-card p{color:var(--muted);font-size:13px}.meter{height:8px;background:#e2e8f0;margin:18px 0;border-radius:8px;overflow:hidden}.meter i{display:block;height:100%;background:linear-gradient(90deg,#0b7a75,#22c55e)}dl{display:grid;grid-template-columns:1fr 1fr;gap:12px}dl div{padding:10px;background:#f8fafc}dt{font-size:11px;text-transform:uppercase;color:var(--muted)}dd{font:600 16px var(--mono);margin:3px 0 0}.review-row{display:grid;grid-template-columns:1fr 250px;gap:18px;padding:16px 0;border-top:1px solid var(--line)}.review-copy strong{display:block;margin:5px 0}.review-copy p{font-size:13px;color:var(--muted)}.type{display:inline-block;font:600 10px var(--mono);text-transform:uppercase;color:var(--accent)}.review-actions{display:grid;grid-template-columns:1fr 1fr;gap:7px;align-content:start}.review-actions .input{grid-column:1/-1}.empty{padding:28px;text-align:center;color:var(--muted)}@media(max-width:1100px){.metric-band{grid-template-columns:repeat(2,1fr)}.control-grid{grid-template-columns:1fr}}@media(max-width:700px){.metric-band{grid-template-columns:1fr}.review-row{grid-template-columns:1fr}.control-header{align-items:flex-start;flex-direction:column}}
  `],
})
export class BehaviorReviewComponent implements OnInit {
  loading = false;
  error = '';
  shadow: BehaviorShadowStatus | null = null;
  pilot: ContinuityPilotStatus | null = null;
  reviews: BehaviorMemoryReview[] = [];
  notes: Record<string, string> = {};

  constructor(private api: ApiService) {}

  ngOnInit(): void { void this.load(); }

  async load(): Promise<void> {
    this.loading = true;
    this.error = '';
    try {
      const [shadow, reviewList, pilot] = await Promise.all([
        firstValueFrom(this.api.getBehaviorShadowStatus()),
        firstValueFrom(this.api.getBehaviorReviews()),
        firstValueFrom(this.api.getContinuityPilotStatus(30)),
      ]);
      this.shadow = shadow;
      this.reviews = reviewList.reviews;
      this.pilot = pilot;
    } catch (error) {
      this.error = error instanceof Error ? error.message : 'Unable to load behavior controls.';
    } finally {
      this.loading = false;
    }
  }

  async resolve(review: BehaviorMemoryReview, decision: 'promote' | 'reject'): Promise<void> {
    await firstValueFrom(this.api.resolveBehaviorReview(review.review_id, decision, this.notes[review.review_id] || ''));
    this.reviews = this.reviews.filter(item => item.review_id !== review.review_id);
  }

  metricNumber(key: string): number { return Number(this.shadow?.metrics?.[key] ?? 0); }
  metricBool(key: string): boolean { return Boolean(this.shadow?.metrics?.[key]); }
  percent(value: number | null): string { return value === null ? 'No data' : `${(value * 100).toFixed(1)}%`; }
  signedPercent(value: number): string { return `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}%`; }
  duration(value: number | null): string {
    if (value === null) return 'No data';
    if (value < 1000) return `${Math.round(value)} ms`;
    if (value < 60000) return `${(value / 1000).toFixed(1)} s`;
    if (value < 3600000) return `${(value / 60000).toFixed(1)} min`;
    return `${(value / 3600000).toFixed(1)} h`;
  }
}
