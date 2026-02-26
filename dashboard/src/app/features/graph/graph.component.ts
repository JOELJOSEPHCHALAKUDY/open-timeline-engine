import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { EventItem, GraphEntityItem, GraphEventResponse } from '../../core/models';

@Component({
  selector: 'app-graph',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="page-header">
      <h1>Decision Trace Explorer</h1>
      <p>Start with a question, find related entities, then trace the exact events and contradictions that drove past decisions.</p>
    </div>

    <div class="card" style="margin-bottom: 20px;">
      <div style="display: grid; grid-template-columns: 1fr auto; gap: 10px;">
        <input class="input" [(ngModel)]="query" (keyup.enter)="searchEntities()" placeholder="Example: retry strategy, login flow, postgres migration..." />
        <button class="btn btn-primary" (click)="searchEntities()">Find entities</button>
      </div>
      <div style="display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 12px;">
        <input class="input" [(ngModel)]="eventId" placeholder="Or jump directly to event ID" />
        <button class="btn" (click)="loadGraphFromInput()">Open event graph</button>
      </div>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Building decision trace...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>
    <div *ngIf="notice" class="card" style="margin-bottom: 20px; border-left: 3px solid var(--accent);">{{ notice }}</div>

    <div class="grid grid-2" *ngIf="!loading">
      <div>
        <div class="card" style="margin-bottom: 12px;">
          <div class="card-header">
            <span class="card-title">Entity candidates</span>
            <span class="badge badge-muted">{{ entities.length }}</span>
          </div>
          <div *ngIf="entities.length === 0" style="font-size: 14px; color: var(--muted); margin-top: 10px;">
            Search for a topic to start a practical trace.
          </div>
          <div *ngFor="let entity of entities" style="margin-top: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; cursor: pointer;" (click)="traceEntity(entity)">
            <div style="display: flex; justify-content: space-between; gap: 10px;">
              <div>
                <div style="font-weight: 700;">{{ entity.display_name || entity.entity_key }}</div>
                <div style="font-size: 12px; color: var(--muted);">{{ entity.entity_type || 'entity' }} · {{ entity.entity_key }}</div>
              </div>
              <span class="badge badge-muted" *ngIf="entity.score !== undefined">{{ entity.score }}</span>
            </div>
            <div style="margin-top: 8px;">
              <button class="btn">Trace related decisions</button>
            </div>
          </div>
        </div>
      </div>

      <div>
        <div class="card" style="margin-bottom: 12px;">
          <div class="card-header">
            <span class="card-title">Related timeline events</span>
            <span class="badge badge-accent" *ngIf="activeEntityLabel">{{ activeEntityLabel }}</span>
          </div>
          <div *ngIf="relatedEvents.length === 0" style="font-size: 14px; color: var(--muted); margin-top: 10px;">
            Select an entity to load recent events where it appeared.
          </div>
          <div *ngFor="let event of relatedEvents" style="margin-top: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 8px;">
            <div style="font-size: 14px; font-weight: 600;">{{ event.title }}</div>
            <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">
              {{ event.ts | date:'short' }} · {{ event.domain }} · {{ event.task_type }}
            </div>
            <div style="margin-top: 8px; display: flex; gap: 8px;">
              <button class="btn" (click)="openEventGraph(event)">Open graph</button>
              <span class="badge badge-muted">{{ event.id }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="card" *ngIf="graph && !loading">
      <div class="card-header">
        <span class="card-title">Event decision graph</span>
        <span class="badge badge-accent">{{ graph.event_id }}</span>
      </div>
      <div class="grid grid-4" style="margin-top: 14px;">
        <div>
          <div class="stat-value" style="font-size: 18px;">{{ graph.graph.entities.length }}</div>
          <div class="stat-label">entities</div>
        </div>
        <div>
          <div class="stat-value" style="font-size: 18px;">{{ graph.graph.relationships.length }}</div>
          <div class="stat-label">relationships</div>
        </div>
        <div>
          <div class="stat-value" style="font-size: 18px;">{{ contradictionCount }}</div>
          <div class="stat-label">contradictions</div>
        </div>
        <div>
          <div class="stat-value" style="font-size: 18px;">{{ graph.graph.facts.length }}</div>
          <div class="stat-label">facts</div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-top: 14px;">
        <div>
          <div style="font-size: 12px; color: var(--muted); margin-bottom: 6px;">Relationship timeline</div>
          <div *ngIf="graph.graph.relationships.length === 0" style="font-size: 13px; color: var(--muted);">No relationships found.</div>
          <div *ngFor="let rel of graph.graph.relationships | slice:0:12" style="padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 13px;">
            {{ formatRelation(rel) }}
          </div>
        </div>
        <div>
          <div style="font-size: 12px; color: var(--muted); margin-bottom: 6px;">Fact assertions</div>
          <div *ngIf="graph.graph.facts.length === 0" style="font-size: 13px; color: var(--muted);">No facts found.</div>
          <div *ngFor="let fact of graph.graph.facts | slice:0:12" style="padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 13px;">
            {{ formatFact(fact) }}
          </div>
        </div>
      </div>
    </div>
  `,
})
export class GraphComponent {
  query = '';
  eventId = '';
  loading = false;
  error: string | null = null;
  notice: string | null = null;
  activeEntityLabel = '';
  entities: GraphEntityItem[] = [];
  relatedEvents: EventItem[] = [];
  graph: GraphEventResponse | null = null;

  constructor(private api: ApiService) {}

  get contradictionCount(): number {
    if (!this.graph) return 0;
    return this.graph.graph.relationships.filter((rel: any) => {
      const relationType = String(rel?.relationship_type ?? rel?.type ?? '').toLowerCase();
      return relationType.includes('contradict');
    }).length;
  }

  searchEntities(): void {
    const q = this.query.trim();
    if (!q) {
      this.notice = 'Enter a topic first.';
      return;
    }
    this.loading = true;
    this.error = null;
    this.notice = null;
    this.entities = [];
    this.relatedEvents = [];
    this.graph = null;
    this.api.searchGraphEntities(q, 25).subscribe({
      next: (res) => {
        this.entities = Array.isArray(res?.entities) ? res.entities : [];
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Entity search failed';
        this.loading = false;
      },
    });
  }

  traceEntity(entity: GraphEntityItem): void {
    const topic = (entity.display_name || entity.entity_key || '').trim();
    if (!topic) {
      this.notice = 'Entity has no searchable label.';
      return;
    }
    this.loading = true;
    this.error = null;
    this.notice = null;
    this.activeEntityLabel = topic;
    this.api.searchEvents({
      query: topic,
      match_all: false,
      limit: 12,
      offset: 0,
    }).subscribe({
      next: (res) => {
        this.relatedEvents = Array.isArray(res?.events) ? res.events : [];
        this.loading = false;
        if (this.relatedEvents.length > 0) {
          this.openEventGraph(this.relatedEvents[0]);
          return;
        }
        const fallbackEvent = this.pickEventId(entity);
        if (fallbackEvent) {
          this.eventId = fallbackEvent;
          this.loadGraph(fallbackEvent);
        } else {
          this.notice = 'No related events found yet for this entity.';
        }
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load related events';
        this.loading = false;
      },
    });
  }

  openEventGraph(event: EventItem): void {
    this.eventId = event.id;
    this.loadGraph(event.id);
  }

  loadGraphFromInput(): void {
    const id = this.eventId.trim();
    if (!id) {
      this.notice = 'Enter an event id first.';
      return;
    }
    this.loadGraph(id);
  }

  formatRelation(rel: any): string {
    const relationType = String(rel?.relationship_type ?? rel?.type ?? 'relation');
    const source = String(rel?.source_event_id ?? rel?.source_entity_key ?? rel?.source_id ?? '?');
    const target = String(rel?.target_event_id ?? rel?.target_entity_key ?? rel?.target_id ?? '?');
    return `${relationType}: ${source} -> ${target}`;
  }

  formatFact(fact: any): string {
    const key = String(fact?.fact_key ?? fact?.key ?? 'fact');
    const value = String(fact?.fact_value ?? fact?.value ?? 'unknown');
    const status = String(fact?.status ?? 'active');
    return `${key} = ${value} (${status})`;
  }

  private loadGraph(id: string): void {
    this.loading = true;
    this.error = null;
    this.notice = null;
    this.api.getGraphEvent(id).subscribe({
      next: (res) => {
        this.graph = res;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Event graph lookup failed';
        this.loading = false;
      },
    });
  }

  private pickEventId(entity: GraphEntityItem): string | null {
    const direct = [
      entity['event_id'],
      entity['latest_event_id'],
      entity['last_event_id'],
      entity['sample_event_id'],
    ].find((value) => typeof value === 'string' && value.length > 0);
    if (typeof direct === 'string') return direct;

    const evidence = entity['evidence_event_ids'];
    if (Array.isArray(evidence)) {
      const first = evidence.find((value) => typeof value === 'string' && value.length > 0);
      if (typeof first === 'string') return first;
    }
    return null;
  }
}
