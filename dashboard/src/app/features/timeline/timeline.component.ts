import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { EventItem } from '../../core/models';

@Component({
  selector: 'app-timeline',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="page-header">
      <h1>Timeline</h1>
      <p>Search and explore recorded events</p>
    </div>

    <!-- Search bar -->
    <div class="card" style="margin-bottom: 20px;">
      <div style="display: flex; gap: 12px;">
        <input class="input" [(ngModel)]="query" placeholder="Search events..." (keyup.enter)="search()" style="flex: 1;">
        <button class="btn btn-primary" (click)="search()">Search</button>
      </div>
      <div class="filter-bar" style="margin-top: 12px;">
        <input class="input" [(ngModel)]="filterDomain" placeholder="Domain" style="width: 150px;">
        <input class="input" [(ngModel)]="filterEventType" placeholder="Event type" style="width: 150px;">
        <select class="input" [(ngModel)]="filterSensitivity" style="width: 150px;">
          <option value="">All sensitivity</option>
          <option value="0">0 - Public</option>
          <option value="1">1 - Internal</option>
          <option value="2">2 - Sensitive</option>
          <option value="3">3 - Restricted</option>
        </select>
      </div>
      <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;" *ngIf="lastPolicy">
        <span class="badge badge-muted">blocked: {{ lastPolicy.blocked_count ?? 0 }}</span>
        <span class="badge badge-accent" *ngIf="lastPolicy.block_sensitivity !== undefined">
          max sensitivity: {{ lastPolicy.block_sensitivity }}
        </span>
        <span class="badge badge-muted" *ngIf="lastPolicy.role">role: {{ lastPolicy.role }}</span>
      </div>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Searching...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div class="grid grid-2" *ngIf="!loading">
      <!-- Event list -->
      <div>
        <div class="card" *ngIf="events.length === 0 && searched">
          <p style="color: var(--muted); font-size: 14px;">No events found. Try a different query.</p>
        </div>
        <div *ngFor="let event of events" class="card" style="margin-bottom: 12px; cursor: pointer;"
          [style.border-left]="selectedEvent?.id === event.id ? '3px solid var(--accent)' : '3px solid transparent'"
          (click)="selectEvent(event)">
          <div style="display: flex; justify-content: space-between; align-items: flex-start;">
            <div>
              <div style="font-size: 14px; font-weight: 600;">{{ event.title }}</div>
              <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">
                {{ event.ts | date:'short' }} · {{ event.domain }} · {{ event.event_type }}
              </div>
            </div>
            <span class="badge" [ngClass]="{
              'badge-muted': event.sensitivity === 0,
              'badge-accent': event.sensitivity === 1,
              'badge-warn': event.sensitivity === 2,
              'badge-danger': event.sensitivity === 3
            }">S{{ event.sensitivity }}</span>
          </div>
          <div *ngIf="event.tags.length > 0" style="margin-top: 8px; display: flex; gap: 4px; flex-wrap: wrap;">
            <span *ngFor="let tag of event.tags" class="badge badge-muted" style="font-size: 11px;">{{ tag }}</span>
          </div>
        </div>

        <div *ngIf="total > events.length" style="text-align: center; margin-top: 16px;">
          <button class="btn" (click)="loadMore()">Load more ({{ total - events.length }} remaining)</button>
        </div>
      </div>

      <!-- Event detail -->
      <div>
        <div class="card" *ngIf="!selectedEvent">
          <p style="color: var(--muted); font-size: 14px; text-align: center; padding: 40px 0;">
            Select an event to view details
          </p>
        </div>
        <div class="card" *ngIf="selectedEvent">
          <div class="card-title" style="margin-bottom: 12px;">Event Detail</div>
          <div style="margin-bottom: 16px;">
            <h3 style="font-size: 16px; font-weight: 600;">{{ selectedEvent.title }}</h3>
            <div style="font-size: 13px; color: var(--muted); margin-top: 4px;">
              ID: <code style="font-family: var(--mono); font-size: 12px;">{{ selectedEvent.id }}</code>
            </div>
          </div>
          <div class="grid grid-2" style="margin-bottom: 16px; gap: 12px;">
            <div><span style="font-size: 12px; color: var(--muted);">Timestamp</span><br>{{ selectedEvent.ts | date:'medium' }}</div>
            <div><span style="font-size: 12px; color: var(--muted);">Actor</span><br>{{ selectedEvent.actor }}</div>
            <div><span style="font-size: 12px; color: var(--muted);">Source</span><br>{{ selectedEvent.source }}</div>
            <div><span style="font-size: 12px; color: var(--muted);">Domain</span><br>{{ selectedEvent.domain }}</div>
            <div><span style="font-size: 12px; color: var(--muted);">Task Type</span><br>{{ selectedEvent.task_type }}</div>
            <div><span style="font-size: 12px; color: var(--muted);">Event Type</span><br>{{ selectedEvent.event_type }}</div>
          </div>
          <div style="margin-top: 16px;">
            <div style="font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px;">Payload</div>
            <pre style="background: #f1f5f9; padding: 12px; border-radius: 6px; font-family: var(--mono); font-size: 12px; overflow-x: auto; max-height: 400px;">{{ selectedEvent.payload | json }}</pre>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class TimelineComponent implements OnInit {
  query = '';
  filterDomain = '';
  filterEventType = '';
  filterSensitivity = '';
  events: EventItem[] = [];
  selectedEvent: EventItem | null = null;
  total = 0;
  loading = false;
  error: string | null = null;
  searched = false;
  offset = 0;
  lastPolicy: { blocked_count?: number; block_sensitivity?: number; role?: string } | null = null;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.search();
  }

  search(): void {
    this.loading = true;
    this.error = null;
    this.offset = 0;
    this.events = [];
    this.selectedEvent = null;
    this.searched = true;

    this.api.searchEvents(this.currentRequest(0)).subscribe({
      next: (res) => {
        this.events = res.events;
        this.total = res.total;
        this.lastPolicy = {
          blocked_count: res.policy?.blocked_count as number | undefined,
          block_sensitivity: res.policy?.block_sensitivity as number | undefined,
          role: res.policy?.role as string | undefined,
        };
        this.loading = false;
      },
      error: (err) => {
        this.error = err.message || 'Search failed';
        this.loading = false;
      },
    });
  }

  loadMore(): void {
    this.offset += 20;
    this.api.searchEvents(this.currentRequest(this.offset)).subscribe({
      next: (res) => {
        this.events = [...this.events, ...res.events];
        this.lastPolicy = {
          blocked_count: res.policy?.blocked_count as number | undefined,
          block_sensitivity: res.policy?.block_sensitivity as number | undefined,
          role: res.policy?.role as string | undefined,
        };
      },
    });
  }

  selectEvent(event: EventItem): void {
    this.selectedEvent = event;
  }

  private currentRequest(offset: number): {
    query: string;
    match_all: boolean;
    domain?: string;
    event_type?: string;
    sensitivity_max?: number;
    limit: number;
    offset: number;
  } {
    const normalizedQuery = (this.query || '').trim();
    const matchAll = normalizedQuery.length === 0 || normalizedQuery === '*';
    return {
      query: normalizedQuery || '*',
      match_all: matchAll,
      domain: this.filterDomain || undefined,
      event_type: this.filterEventType || undefined,
      sensitivity_max: this.filterSensitivity ? parseInt(this.filterSensitivity, 10) : undefined,
      limit: 20,
      offset,
    };
  }
}
