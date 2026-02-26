import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';
import { ObservationItem, ObservationListResponse } from '../../core/models';

@Component({
  selector: 'app-observations',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="page-header">
      <h1>Observations</h1>
      <p>Decision observations that shape the behavioral fingerprint</p>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading observations...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div *ngIf="!loading && !error" class="fade-in">
      <!-- Situation type filter pills -->
      <div class="filter-bar">
        <span class="filter-pill" [class.active]="!activeType" (click)="filterByType(null)">
          All <span class="count">({{ total }})</span>
        </span>
        <span *ngFor="let entry of typeEntries" class="filter-pill"
          [class.active]="activeType === entry[0]"
          (click)="filterByType(entry[0])">
          {{ entry[0] }} <span class="count">({{ entry[1] }})</span>
        </span>
      </div>

      <!-- Sentiment filter -->
      <div class="filter-bar">
        <span class="filter-pill" [class.active]="!activeSentiment" (click)="filterBySentiment(null)">All sentiments</span>
        <span class="filter-pill" [class.active]="activeSentiment === 'positive'" (click)="filterBySentiment('positive')">Positive</span>
        <span class="filter-pill" [class.active]="activeSentiment === 'neutral'" (click)="filterBySentiment('neutral')">Neutral</span>
        <span class="filter-pill" [class.active]="activeSentiment === 'negative'" (click)="filterBySentiment('negative')">Negative</span>
      </div>

      <!-- Empty state -->
      <div class="card" *ngIf="observations.length === 0">
        <p style="color: var(--muted); font-size: 14px; text-align: center; padding: 40px 0;">
          No observations recorded yet. Observations are created during clone advisor interactions.
        </p>
      </div>

      <!-- Observation list -->
      <div *ngFor="let obs of observations" class="card" style="margin-bottom: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: flex-start;">
          <div>
            <div style="font-size: 14px; font-weight: 600;">{{ obs.situation_summary }}</div>
            <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">
              {{ obs.ts | date:'short' }} · {{ obs.situation_type }}
            </div>
          </div>
          <div style="display: flex; gap: 6px;">
            <span class="badge" [ngClass]="{
              'badge-success': obs.outcome_sentiment === 'positive',
              'badge-warn': obs.outcome_sentiment === 'neutral',
              'badge-danger': obs.outcome_sentiment === 'negative',
              'badge-muted': !obs.outcome_sentiment
            }">{{ obs.outcome_sentiment || 'unknown' }}</span>
            <span class="badge badge-accent">{{ (obs.confidence * 100).toFixed(0) }}%</span>
          </div>
        </div>

        <div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line);">
          <div style="margin-bottom: 8px;">
            <span style="font-size: 12px; font-weight: 600; color: var(--muted);">User Response</span>
            <p style="font-size: 13px; margin-top: 2px;">{{ obs.user_response }}</p>
          </div>
          <div *ngIf="obs.response_reasoning" style="margin-bottom: 8px;">
            <span style="font-size: 12px; font-weight: 600; color: var(--muted);">Reasoning</span>
            <p style="font-size: 13px; margin-top: 2px;">{{ obs.response_reasoning }}</p>
          </div>
          <div *ngIf="obs.outcome">
            <span style="font-size: 12px; font-weight: 600; color: var(--muted);">Outcome</span>
            <p style="font-size: 13px; margin-top: 2px;">{{ obs.outcome }}</p>
          </div>
        </div>
      </div>

      <div *ngIf="total > observations.length" style="text-align: center; margin-top: 16px;">
        <button class="btn" (click)="loadMore()">Load more</button>
      </div>
    </div>
  `,
})
export class ObservationsComponent implements OnInit {
  observations: ObservationItem[] = [];
  total = 0;
  typeEntries: [string, number][] = [];
  activeType: string | null = null;
  activeSentiment: string | null = null;
  loading = true;
  error: string | null = null;
  offset = 0;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading = true;
    this.offset = 0;
    this.api.getObservations({
      situation_type: this.activeType || undefined,
      outcome_sentiment: this.activeSentiment || undefined,
      limit: 50,
      offset: 0,
    }).subscribe({
      next: (res) => {
        this.observations = res.observations;
        this.total = res.total;
        this.typeEntries = Object.entries(res.situation_type_counts).sort((a, b) => b[1] - a[1]);
        this.loading = false;
      },
      error: (err) => {
        this.error = err.message || 'Failed to load observations';
        this.loading = false;
      },
    });
  }

  filterByType(type: string | null): void {
    this.activeType = type;
    this.load();
  }

  filterBySentiment(sentiment: string | null): void {
    this.activeSentiment = sentiment;
    this.load();
  }

  loadMore(): void {
    this.offset += 50;
    this.api.getObservations({
      situation_type: this.activeType || undefined,
      outcome_sentiment: this.activeSentiment || undefined,
      limit: 50,
      offset: this.offset,
    }).subscribe({
      next: (res) => {
        this.observations = [...this.observations, ...res.observations];
      },
    });
  }
}
