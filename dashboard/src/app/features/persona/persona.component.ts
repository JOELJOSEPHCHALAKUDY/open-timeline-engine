import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';
import {
  FingerprintResponse,
  CloneScoreResponse,
  Fingerprint,
} from '../../core/models';

interface RadarDimension {
  label: string;
  value: number;
  raw: string | number | boolean;
}

@Component({
  selector: 'app-persona',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="page-header">
      <h1>Persona</h1>
      <p>Behavioral fingerprint and clone readiness</p>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading persona data...</span>
    </div>

    <div *ngIf="error" class="error-banner">&#9888;&#65039; {{ error }}</div>

    <div *ngIf="!loading && !error" class="fade-in">
      <!-- Top row: Radar + Clone Score -->
      <div class="grid grid-2" style="margin-bottom: 20px;">
        <!-- Radar Chart -->
        <div class="card">
          <div class="card-title" style="margin-bottom: 16px;">Behavioral Fingerprint</div>
          <div *ngIf="fingerprint?.is_default" class="badge badge-muted" style="margin-bottom: 12px;">Default profile — no observations yet</div>
          <svg viewBox="0 0 400 400" width="100%" style="max-width: 400px; display: block; margin: 0 auto;">
            <!-- Grid circles -->
            <circle *ngFor="let r of [40, 80, 120, 160]" [attr.cx]="200" [attr.cy]="200" [attr.r]="r"
              fill="none" stroke="var(--line)" stroke-width="0.5" opacity="0.5"/>
            <!-- Axis lines -->
            <line *ngFor="let dim of dimensions; let i = index"
              [attr.x1]="200" [attr.y1]="200"
              [attr.x2]="200 + 160 * cos(i)" [attr.y2]="200 + 160 * sin(i)"
              stroke="var(--line)" stroke-width="0.5" opacity="0.5"/>
            <!-- Data polygon -->
            <polygon
              [attr.points]="polygonPoints"
              fill="var(--accent)" fill-opacity="0.15"
              stroke="var(--accent)" stroke-width="2"/>
            <!-- Data points -->
            <circle *ngFor="let dim of dimensions; let i = index"
              [attr.cx]="200 + dim.value * 160 * cos(i)"
              [attr.cy]="200 + dim.value * 160 * sin(i)"
              r="4" fill="var(--accent)"/>
            <!-- Labels -->
            <text *ngFor="let dim of dimensions; let i = index"
              [attr.x]="200 + 180 * cos(i)"
              [attr.y]="200 + 180 * sin(i)"
              text-anchor="middle" dominant-baseline="middle"
              font-size="11" fill="var(--muted)">
              {{ dim.label }}
            </text>
          </svg>
        </div>

        <!-- Clone Score + Breakdown -->
        <div class="card">
          <div class="card-title" style="margin-bottom: 16px;">Clone Readiness</div>
          <div *ngIf="cloneScore" style="display: flex; align-items: center; gap: 24px; margin-bottom: 24px;">
            <div class="gauge">
              <svg viewBox="0 0 160 160" width="160" height="160">
                <circle class="gauge-bg" cx="80" cy="80" r="68"></circle>
                <circle class="gauge-fill" cx="80" cy="80" r="68"
                  [attr.stroke-dasharray]="circumference"
                  [attr.stroke-dashoffset]="gaugeOffset">
                </circle>
              </svg>
              <div class="gauge-text">
                <div class="gauge-value">{{ cloneScore.score }}%</div>
                <div class="gauge-label">Ready</div>
              </div>
            </div>
            <div>
              <div style="font-size: 14px; color: var(--muted);">
                {{ cloneScore.observation_count }} observations<br>
                {{ cloneScore.situation_types_covered }}/{{ cloneScore.total_situation_types }} situations covered
              </div>
            </div>
          </div>

          <!-- Breakdown bars -->
          <div *ngIf="cloneScore">
            <div *ngFor="let bar of breakdownBars" style="margin-bottom: 14px;">
              <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px;">
                <span style="color: var(--muted);">{{ bar.label }}</span>
                <span style="font-weight: 600;">{{ bar.score }}/25</span>
              </div>
              <div class="progress-bar">
                <div class="progress-fill" [style.width.%]="bar.score / 25 * 100"></div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Trait Details -->
      <div class="grid grid-3" style="margin-bottom: 20px;">
        <!-- Decision Making -->
        <div class="card" *ngIf="fingerprint">
          <div class="card-title" style="margin-bottom: 12px;">Decision Making</div>
          <div class="trait-row" *ngFor="let t of decisionTraits">
            <span class="trait-label">{{ t.label }}</span>
            <span class="badge badge-accent">{{ t.value }}</span>
          </div>
        </div>

        <!-- Communication -->
        <div class="card" *ngIf="fingerprint">
          <div class="card-title" style="margin-bottom: 12px;">Communication</div>
          <div class="trait-row" *ngFor="let t of commTraits">
            <span class="trait-label">{{ t.label }}</span>
            <span class="badge badge-accent">{{ t.value }}</span>
          </div>
        </div>

        <!-- Priorities & Learning -->
        <div class="card" *ngIf="fingerprint">
          <div class="card-title" style="margin-bottom: 12px;">Priorities & Learning</div>
          <div class="trait-row" *ngFor="let t of priorityTraits">
            <span class="trait-label">{{ t.label }}</span>
            <span class="badge badge-accent">{{ t.value }}</span>
          </div>
        </div>
      </div>

      <!-- Emotional Patterns -->
      <div class="card" *ngIf="fingerprint">
        <div class="card-title" style="margin-bottom: 12px;">Emotional Patterns</div>
        <div class="grid grid-3">
          <div>
            <div style="font-size: 13px; font-weight: 600; color: var(--warn); margin-bottom: 8px;">Frustration Triggers</div>
            <div *ngIf="fingerprint.fingerprint.emotional_patterns.frustration_triggers.length === 0"
              style="font-size: 13px; color: var(--muted);">None detected yet</div>
            <div *ngFor="let t of fingerprint.fingerprint.emotional_patterns.frustration_triggers"
              style="padding: 4px 0; font-size: 13px;">&#8226; {{ t }}</div>
          </div>
          <div>
            <div style="font-size: 13px; font-weight: 600; color: var(--success); margin-bottom: 8px;">Satisfaction Signals</div>
            <div *ngIf="fingerprint.fingerprint.emotional_patterns.satisfaction_signals.length === 0"
              style="font-size: 13px; color: var(--muted);">None detected yet</div>
            <div *ngFor="let s of fingerprint.fingerprint.emotional_patterns.satisfaction_signals"
              style="padding: 4px 0; font-size: 13px;">&#8226; {{ s }}</div>
          </div>
          <div>
            <div style="font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px;">Stress Indicators</div>
            <div *ngIf="fingerprint.fingerprint.emotional_patterns.stress_indicators.length === 0"
              style="font-size: 13px; color: var(--muted);">None detected yet</div>
            <div *ngFor="let s of fingerprint.fingerprint.emotional_patterns.stress_indicators"
              style="padding: 4px 0; font-size: 13px;">&#8226; {{ s }}</div>
          </div>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .trait-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
    }
    .trait-row:last-child { border-bottom: none; }
    .trait-label { font-size: 13px; color: var(--ink); }
  `]
})
export class PersonaComponent implements OnInit {
  fingerprint: FingerprintResponse | null = null;
  cloneScore: CloneScoreResponse | null = null;
  loading = true;
  error: string | null = null;

  dimensions: RadarDimension[] = [];
  polygonPoints = '';
  circumference = 2 * Math.PI * 68;
  gaugeOffset = this.circumference;

  breakdownBars: { label: string; score: number }[] = [];
  decisionTraits: { label: string; value: string }[] = [];
  commTraits: { label: string; value: string }[] = [];
  priorityTraits: { label: string; value: string }[] = [];

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    Promise.all([
      this.api.getFingerprint().toPromise(),
      this.api.getCloneScore().toPromise(),
    ])
      .then(([fp, score]) => {
        this.fingerprint = fp!;
        this.cloneScore = score!;
        this.buildRadar(fp!.fingerprint);
        this.buildTraits(fp!.fingerprint);
        this.breakdownBars = [
          { label: 'Observation Count', score: score!.breakdown.observation_count_score },
          { label: 'Fingerprint Confidence', score: score!.breakdown.fingerprint_confidence },
          { label: 'Pattern Coverage', score: score!.breakdown.pattern_coverage },
          { label: 'Recent Consistency', score: score!.breakdown.recent_consistency },
        ];
        setTimeout(() => {
          this.gaugeOffset = this.circumference - (this.cloneScore!.score / 100) * this.circumference;
        }, 100);
        this.loading = false;
      })
      .catch(err => {
        this.error = err.message || 'Failed to load persona data';
        this.loading = false;
      });
  }

  private mapCategorical(val: string, map: Record<string, number>): number {
    return map[val] ?? 0.5;
  }

  private buildRadar(fp: Fingerprint): void {
    const dm: any = fp?.decision_making || {};
    const comm: any = fp?.communication || {};
    const priorities: any = fp?.priorities || {};
    const learning: any = fp?.learning_style || {};
    const switching: any = fp?.context_switching || {};
    const riskTolerance = typeof dm.risk_tolerance === 'string' ? dm.risk_tolerance : 'moderate';
    const verbosity = typeof comm.verbosity === 'string' ? comm.verbosity : 'moderate';
    const formality = typeof comm.formality === 'string' ? comm.formality : 'neutral';
    const multitaskTolerance = typeof switching.multitask_tolerance === 'string' ? switching.multitask_tolerance : 'moderate';
    const speedVsThoroughness = Number(dm.speed_vs_thoroughness ?? 0.5);
    const delegationTendency = Number(dm.delegation_tendency ?? 0.5);
    const speedVsQuality = Number(priorities.speed_vs_quality ?? 0.5);
    const pragmaticVsPrincipled = Number(priorities.pragmatic_vs_principled ?? 0.5);
    const exploration = Number(learning.exploration_vs_exploitation ?? 0.5);
    this.dimensions = [
      { label: 'Risk', value: this.mapCategorical(riskTolerance, { conservative: 0.25, moderate: 0.5, aggressive: 0.75 }), raw: riskTolerance },
      { label: 'Thorough', value: speedVsThoroughness, raw: speedVsThoroughness },
      { label: 'Delegate', value: delegationTendency, raw: delegationTendency },
      { label: 'Verbose', value: this.mapCategorical(verbosity, { terse: 0.25, moderate: 0.5, verbose: 0.75 }), raw: verbosity },
      { label: 'Formal', value: this.mapCategorical(formality, { casual: 0.25, neutral: 0.5, formal: 0.75 }), raw: formality },
      { label: 'Speed/Qual', value: speedVsQuality, raw: speedVsQuality },
      { label: 'Pragmatic', value: pragmaticVsPrincipled, raw: pragmaticVsPrincipled },
      { label: 'Explore', value: exploration, raw: exploration },
      { label: 'Multitask', value: this.mapCategorical(multitaskTolerance, { low: 0.25, moderate: 0.5, high: 0.75 }), raw: multitaskTolerance },
    ];

    this.polygonPoints = this.dimensions
      .map((d, i) => `${200 + d.value * 160 * this.cos(i)},${200 + d.value * 160 * this.sin(i)}`)
      .join(' ');
  }

  private buildTraits(fp: Fingerprint): void {
    const dm: any = fp?.decision_making || {};
    const comm: any = fp?.communication || {};
    const priorities: any = fp?.priorities || {};
    const learning: any = fp?.learning_style || {};
    this.decisionTraits = [
      { label: 'Risk Tolerance', value: String(dm.risk_tolerance || 'moderate') },
      { label: 'Speed vs Thoroughness', value: String(dm.speed_vs_thoroughness ?? 0.5) },
      { label: 'Delegation Tendency', value: String(dm.delegation_tendency ?? 0.5) },
      { label: 'Conflict Resolution', value: String(dm.conflict_resolution_style || 'collaborative') },
      { label: 'Reversal Frequency', value: String(dm.decision_reversal_frequency ?? 0.0) },
      { label: 'Info Needs', value: String(dm.information_needs_before_deciding || 'moderate') },
    ];
    this.commTraits = [
      { label: 'Verbosity', value: String(comm.verbosity || 'moderate') },
      { label: 'Formality', value: String(comm.formality || 'neutral') },
      { label: 'Emoji Usage', value: comm.emoji_usage ? 'Yes' : 'No' },
      { label: 'Response Length', value: String(comm.preferred_response_length || 'medium') },
      { label: 'Explanation Depth', value: String(comm.explanation_depth || 'medium') },
      { label: 'Tone Under Pressure', value: String(comm.tone_under_pressure || 'neutral') },
    ];
    this.priorityTraits = [
      { label: 'Speed vs Quality', value: String(priorities.speed_vs_quality ?? 0.5) },
      { label: 'UX vs Technical', value: String(priorities.user_experience_vs_technical ?? 0.5) },
      { label: 'Pragmatic vs Principled', value: String(priorities.pragmatic_vs_principled ?? 0.5) },
      { label: 'Exploration', value: String(learning.exploration_vs_exploitation ?? 0.5) },
      { label: 'Feedback Response', value: String(learning.feedback_response || 'neutral') },
      { label: 'Mistake Handling', value: String(learning.mistake_handling || 'iterative') },
    ];
  }

  cos(i: number): number {
    const angle = (2 * Math.PI * i) / this.dimensions.length - Math.PI / 2;
    return Math.cos(angle);
  }

  sin(i: number): number {
    const angle = (2 * Math.PI * i) / this.dimensions.length - Math.PI / 2;
    return Math.sin(angle);
  }
}
