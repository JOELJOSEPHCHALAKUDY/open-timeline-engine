import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';

type ExportFormat = 'json' | 'markdown' | 'csv';
type ExportPreset = 'full' | 'audit' | 'autonomy';
type ReadinessPhase = 'all' | 'warmup' | 'stabilizing' | 'ready';

const EXPORT_SECTIONS = [
  { key: 'system_status', label: 'System Status', defaultOn: true, description: 'Health, runtime services, storage status' },
  { key: 'agent_roles', label: 'Agent Roles', defaultOn: true, description: 'Executor identity role bindings' },
  { key: 'clone_score', label: 'Clone Score', defaultOn: true, description: 'Current clone readiness metrics' },
  { key: 'human_score', label: 'Human Score', defaultOn: true, description: 'Human-level score and subscores' },
  { key: 'human_score_history', label: 'Human Score History', defaultOn: true, description: 'Trend points for recent days' },
  { key: 'goal_intelligence', label: 'Goal Intelligence', defaultOn: true, description: 'Long-term + relation graph export payload' },
  { key: 'takeover_state', label: 'Takeover State', defaultOn: true, description: 'Current takeover state snapshot' },
  { key: 'autonomy_status', label: 'Autonomy Status', defaultOn: true, description: 'Queue, directives, readiness context' },
  { key: 'autonomy_readiness', label: 'Autonomy Readiness Gate', defaultOn: true, description: 'Pass/fail gate details' },
  { key: 'autonomy_project_kpis', label: 'Autonomy Project KPIs', defaultOn: true, description: 'Project completion KPI metrics' },
  { key: 'retrieval_status', label: 'Retrieval Status', defaultOn: true, description: 'Runtime retrieval mode and fallback counters' },
  { key: 'retrieval_eval_status', label: 'Retrieval Eval Status', defaultOn: true, description: 'Latest eval and history summary' },
  { key: 'events', label: 'Events', defaultOn: true, description: 'Search-based event export' },
  { key: 'observations', label: 'Observations', defaultOn: true, description: 'Observation memory entries' },
  { key: 'patterns', label: 'Patterns', defaultOn: true, description: 'Extracted patterns' },
  { key: 'episodes', label: 'Episodes', defaultOn: true, description: 'Episode abstraction layer entries' },
  { key: 'workflow_templates', label: 'Workflow Templates', defaultOn: true, description: 'Reusable workflow hints/templates' },
  { key: 'memory_rules', label: 'Memory Rules', defaultOn: true, description: 'Active preference and boundary rules' },
] as const;

type ExportSectionKey = (typeof EXPORT_SECTIONS)[number]['key'];

type SectionDataMap = Record<ExportSectionKey, unknown>;

type SectionStatus = {
  state: 'idle' | 'loading' | 'ok' | 'error';
  count: number;
  error: string | null;
};

type ExportSnapshot = {
  generated_at: string;
  session_id: string;
  preset: ExportPreset;
  query: string;
  events_limit: number;
  days: number;
  summary: {
    selected: number;
    succeeded: number;
    failed: number;
  };
  sections: Partial<Record<ExportSectionKey, unknown>>;
  errors: Partial<Record<ExportSectionKey, string>>;
};

@Component({
  selector: 'app-export-center',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="page-header">
      <h1>Super Export Center</h1>
      <p>
        Research-grade export workspace for dashboard intelligence, autonomy state, and memory artifacts.
      </p>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div style="display:grid;grid-template-columns:160px 1fr 130px 130px 130px 150px;gap:8px;align-items:center;">
        <select class="input" [(ngModel)]="preset">
          <option value="full">Full Bundle</option>
          <option value="audit">Executive Audit</option>
          <option value="autonomy">Autonomy Incident</option>
        </select>
        <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" />
        <input class="input" [(ngModel)]="query" placeholder="Event query (* for all)" />
        <input class="input" type="number" min="1" max="1000" [(ngModel)]="eventsLimit" placeholder="Events limit" />
        <input class="input" type="number" min="1" max="365" [(ngModel)]="days" placeholder="Days" />
        <select class="input" [(ngModel)]="readinessPhase" title="Autonomy readiness phase filter">
          <option value="all">Readiness: all</option>
          <option value="warmup">Readiness: warmup</option>
          <option value="stabilizing">Readiness: stabilizing</option>
          <option value="ready">Readiness: ready</option>
        </select>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
        <button class="btn" (click)="applyPreset()">Apply preset</button>
        <button class="btn" (click)="refreshSnapshot()" [disabled]="busy">
          {{ busy ? 'Collecting…' : 'Collect snapshot' }}
        </button>
        <select class="input" style="width:150px;" [(ngModel)]="format">
          <option value="json">JSON</option>
          <option value="markdown">Markdown</option>
          <option value="csv">CSV</option>
        </select>
        <button class="btn btn-primary" (click)="exportCurrent()" [disabled]="busy">Export current format</button>
        <button class="btn btn-primary" (click)="exportAll()" [disabled]="busy">Export all formats</button>
      </div>
    </div>

    <div *ngIf="notice" class="card" style="margin-bottom:12px;">{{ notice }}</div>
    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <span class="card-title">Section selector</span>
        <span class="badge badge-muted">Selected {{ selectedCount() }}/{{ sectionDefs.length }}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
        <label
          *ngFor="let section of sectionDefs"
          style="display:flex;align-items:flex-start;gap:8px;border:1px solid var(--border);border-radius:8px;padding:8px;"
        >
          <input type="checkbox" [ngModel]="isEnabled(section.key)" (ngModelChange)="setEnabled(section.key, $event)" />
          <div style="flex:1;">
            <div style="display:flex;justify-content:space-between;gap:8px;">
              <strong>{{ section.label }}</strong>
              <span class="badge"
                [ngClass]="{
                  'badge-success': sectionStatus(section.key).state === 'ok',
                  'badge-danger': sectionStatus(section.key).state === 'error',
                  'badge-muted': sectionStatus(section.key).state === 'idle' || sectionStatus(section.key).state === 'loading'
                }"
              >
                {{ sectionStatusLabel(section.key) }}
              </span>
            </div>
            <div style="font-size:12px;color:var(--muted);margin-top:2px;">
              {{ section.description }}
            </div>
            <div *ngIf="sectionStatus(section.key).error" style="font-size:12px;color:#f87171;margin-top:4px;">
              {{ sectionStatus(section.key).error }}
            </div>
          </div>
        </label>
      </div>
    </div>

    <div class="card" *ngIf="snapshot">
      <div class="card-header">
        <span class="card-title">Snapshot summary</span>
        <span class="badge badge-muted">{{ snapshot.generated_at | date:'short' }}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
        <span class="badge badge-muted">selected {{ snapshot.summary.selected }}</span>
        <span class="badge badge-success">ok {{ snapshot.summary.succeeded }}</span>
        <span class="badge badge-danger">failed {{ snapshot.summary.failed }}</span>
      </div>
      <div style="font-size:12px;color:var(--muted);">
        Export filename prefix: <code>{{ filePrefix() }}</code>
      </div>
    </div>
  `,
})
export class ExportCenterComponent implements OnInit {
  readonly sectionDefs = EXPORT_SECTIONS;
  readonly sectionState: Record<ExportSectionKey, boolean> = {
    system_status: true,
    agent_roles: true,
    clone_score: true,
    human_score: true,
    human_score_history: true,
    goal_intelligence: true,
    takeover_state: true,
    autonomy_status: true,
    autonomy_readiness: true,
    autonomy_project_kpis: true,
    retrieval_status: true,
    retrieval_eval_status: true,
    events: true,
    observations: true,
    patterns: true,
    episodes: true,
    workflow_templates: true,
    memory_rules: true,
  };

  sectionStatuses: Record<ExportSectionKey, SectionStatus> = {
    system_status: { state: 'idle', count: 0, error: null },
    agent_roles: { state: 'idle', count: 0, error: null },
    clone_score: { state: 'idle', count: 0, error: null },
    human_score: { state: 'idle', count: 0, error: null },
    human_score_history: { state: 'idle', count: 0, error: null },
    goal_intelligence: { state: 'idle', count: 0, error: null },
    takeover_state: { state: 'idle', count: 0, error: null },
    autonomy_status: { state: 'idle', count: 0, error: null },
    autonomy_readiness: { state: 'idle', count: 0, error: null },
    autonomy_project_kpis: { state: 'idle', count: 0, error: null },
    retrieval_status: { state: 'idle', count: 0, error: null },
    retrieval_eval_status: { state: 'idle', count: 0, error: null },
    events: { state: 'idle', count: 0, error: null },
    observations: { state: 'idle', count: 0, error: null },
    patterns: { state: 'idle', count: 0, error: null },
    episodes: { state: 'idle', count: 0, error: null },
    workflow_templates: { state: 'idle', count: 0, error: null },
    memory_rules: { state: 'idle', count: 0, error: null },
  };

  preset: ExportPreset = 'full';
  readinessPhase: ReadinessPhase = 'all';
  format: ExportFormat = 'json';
  sessionId = 'default';
  query = '*';
  eventsLimit = 250;
  days = 30;
  busy = false;
  notice: string | null = null;
  error: string | null = null;
  snapshot: ExportSnapshot | null = null;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    void this.initializeSessionId();
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

  setEnabled(key: ExportSectionKey, value: boolean): void {
    this.sectionState[key] = Boolean(value);
  }

  isEnabled(key: ExportSectionKey): boolean {
    return this.sectionState[key];
  }

  selectedCount(): number {
    return this.sectionDefs.filter((item) => this.sectionState[item.key]).length;
  }

  sectionStatus(key: ExportSectionKey): SectionStatus {
    return this.sectionStatuses[key];
  }

  sectionStatusLabel(key: ExportSectionKey): string {
    const current = this.sectionStatuses[key];
    if (current.state === 'ok') return `ok (${current.count})`;
    if (current.state === 'error') return 'error';
    if (current.state === 'loading') return 'loading';
    return 'idle';
  }

  applyPreset(): void {
    const next: Record<ExportSectionKey, boolean> = { ...this.sectionState };
    const allOff = (): void => {
      for (const entry of this.sectionDefs) {
        next[entry.key] = false;
      }
    };
    if (this.preset === 'full') {
      for (const entry of this.sectionDefs) {
        next[entry.key] = true;
      }
      this.query = '*';
      this.eventsLimit = 250;
      this.days = 30;
    } else if (this.preset === 'audit') {
      allOff();
      next.system_status = true;
      next.agent_roles = true;
      next.clone_score = true;
      next.human_score = true;
      next.human_score_history = true;
      next.goal_intelligence = true;
      next.autonomy_status = true;
      next.autonomy_readiness = true;
      next.retrieval_status = true;
      next.patterns = true;
      next.memory_rules = true;
      this.query = '*';
      this.eventsLimit = 120;
      this.days = 30;
    } else {
      allOff();
      next.takeover_state = true;
      next.autonomy_status = true;
      next.autonomy_readiness = true;
      next.autonomy_project_kpis = true;
      next.goal_intelligence = true;
      next.events = true;
      next.observations = true;
      next.episodes = true;
      next.retrieval_status = true;
      next.retrieval_eval_status = true;
      next.workflow_templates = true;
      next.memory_rules = true;
      this.query = '*';
      this.eventsLimit = 300;
      this.days = 14;
    }
    for (const key of Object.keys(next) as ExportSectionKey[]) {
      this.sectionState[key] = next[key];
    }
    this.notice = `Preset applied: ${this.preset}`;
  }

  async refreshSnapshot(): Promise<void> {
    this.busy = true;
    this.error = null;
    this.notice = null;
    this.resetSectionStatuses();

    const enabled = this.sectionDefs.filter((entry) => this.sectionState[entry.key]).map((entry) => entry.key);
    if (enabled.length === 0) {
      this.error = 'Select at least one section before collecting snapshot.';
      this.busy = false;
      return;
    }

    const sectionData: Partial<SectionDataMap> = {};
    const sectionErrors: Partial<Record<ExportSectionKey, string>> = {};

    await Promise.all(
      enabled.map(async (key) => {
        this.sectionStatuses[key] = { state: 'loading', count: 0, error: null };
        try {
          const data = await this.fetchSectionData(key);
          sectionData[key] = data;
          this.sectionStatuses[key] = {
            state: 'ok',
            count: this.estimateCount(data),
            error: null,
          };
        } catch (err: unknown) {
          const msg = this.errorMessage(err);
          sectionErrors[key] = msg;
          this.sectionStatuses[key] = { state: 'error', count: 0, error: msg };
        }
      })
    );

    const failed = Object.keys(sectionErrors).length;
    const selected = enabled.length;
    const succeeded = selected - failed;
    this.snapshot = {
      generated_at: new Date().toISOString(),
      session_id: this.sessionId || 'default',
      preset: this.preset,
      query: this.query || '*',
      events_limit: Math.max(1, Math.floor(this.eventsLimit || 1)),
      days: Math.max(1, Math.floor(this.days || 1)),
      summary: {
        selected,
        succeeded,
        failed,
      },
      sections: sectionData,
      errors: sectionErrors,
    };
    this.notice = `Snapshot collected: ${succeeded}/${selected} sections successful.`;
    this.busy = false;
  }

  async exportCurrent(): Promise<void> {
    if (!this.snapshot) {
      await this.refreshSnapshot();
      if (!this.snapshot) return;
    }
    if (this.format === 'json') {
      this.downloadJson(this.snapshot);
    } else if (this.format === 'markdown') {
      this.downloadMarkdown(this.snapshot);
    } else {
      this.downloadCsv(this.snapshot);
    }
  }

  async exportAll(): Promise<void> {
    if (!this.snapshot) {
      await this.refreshSnapshot();
      if (!this.snapshot) return;
    }
    this.downloadJson(this.snapshot);
    this.downloadMarkdown(this.snapshot);
    this.downloadCsv(this.snapshot);
    this.notice = 'Exported JSON, Markdown, and CSV bundles.';
  }

  filePrefix(): string {
    const stamp = new Date().toISOString().replaceAll(':', '-');
    return `tce-export-${this.preset}-${stamp}`;
  }

  private resetSectionStatuses(): void {
    for (const entry of this.sectionDefs) {
      this.sectionStatuses[entry.key] = { state: 'idle', count: 0, error: null };
    }
  }

  private async fetchSectionData(key: ExportSectionKey): Promise<unknown> {
    const session = (this.sessionId || 'default').trim() || 'default';
    const days = Math.max(1, Math.floor(this.days || 1));
    switch (key) {
      case 'system_status':
        return firstValueFrom(this.api.getSystemStatus());
      case 'agent_roles':
        return firstValueFrom(this.api.getDashboardAgentRoles());
      case 'clone_score':
        return firstValueFrom(this.api.getCloneScore());
      case 'human_score':
        return firstValueFrom(this.api.getDashboardHumanScore(session));
      case 'human_score_history':
        return firstValueFrom(this.api.getDashboardHumanScoreHistory(session, days));
      case 'goal_intelligence':
        return firstValueFrom(this.api.getDashboardGoalsIntelligence(session));
      case 'takeover_state':
        return firstValueFrom(this.api.getTakeoverState(session));
      case 'autonomy_status':
        return firstValueFrom(this.api.getTakeoverAutonomyStatus(session));
      case 'autonomy_readiness':
        {
          const readiness = await firstValueFrom(this.api.getTakeoverAutonomyReadiness(session));
          const phase = String(readiness?.status_phase || '').trim().toLowerCase();
          if (this.readinessPhase !== 'all' && phase !== this.readinessPhase) {
            return {
              ...readiness,
              filtered_out: true,
              selected_phase: this.readinessPhase,
            };
          }
          return readiness;
        }
      case 'autonomy_project_kpis':
        return firstValueFrom(this.api.getTakeoverAutonomyProjectKpis(session));
      case 'retrieval_status':
        return firstValueFrom(this.api.getContextRetrievalStatus());
      case 'retrieval_eval_status':
        return firstValueFrom(this.api.getRetrievalEvalStatus(session));
      case 'events':
        return firstValueFrom(
          this.api.searchEvents({
            query: (this.query || '*').trim() || '*',
            match_all: (this.query || '*').trim() === '*',
            limit: Math.max(1, Math.floor(this.eventsLimit || 1)),
            offset: 0,
          })
        );
      case 'observations':
        return firstValueFrom(this.api.getObservations({ limit: Math.max(1, Math.floor(this.eventsLimit || 1)) }));
      case 'patterns':
        return firstValueFrom(this.api.getPatterns({ min_confidence: 0.5 }));
      case 'episodes':
        return firstValueFrom(this.api.getEpisodes(session, undefined, Math.max(1, Math.floor(this.eventsLimit || 1))));
      case 'workflow_templates':
        return firstValueFrom(this.api.getWorkflowTemplates(session, Math.max(1, Math.floor(this.eventsLimit || 1))));
      case 'memory_rules':
        return firstValueFrom(this.api.getMemoryRules(true));
      default:
        return null;
    }
  }

  private estimateCount(data: unknown): number {
    if (Array.isArray(data)) return data.length;
    if (!data || typeof data !== 'object') return 0;
    const row = data as Record<string, unknown>;
    if (Array.isArray(row['events'])) return row['events'].length;
    if (Array.isArray(row['observations'])) return row['observations'].length;
    if (Array.isArray(row['patterns'])) return row['patterns'].length;
    if (Array.isArray(row['episodes'])) return row['episodes'].length;
    if (Array.isArray(row['templates'])) return row['templates'].length;
    if (Array.isArray(row['rules'])) return row['rules'].length;
    if (Array.isArray(row['history'])) return row['history'].length;
    if (Array.isArray(row['points'])) return row['points'].length;
    if (Array.isArray(row['goals'])) return row['goals'].length;
    if (Array.isArray(row['routes'])) return row['routes'].length;
    if (Array.isArray(row['attempts'])) return row['attempts'].length;
    return 1;
  }

  private errorMessage(err: unknown): string {
    if (!err || typeof err !== 'object') return 'unknown_error';
    const candidate = err as { error?: { detail?: string }; message?: string };
    return candidate.error?.detail || candidate.message || 'request_failed';
  }

  private downloadJson(snapshot: ExportSnapshot): void {
    const body = JSON.stringify(snapshot, null, 2);
    this.downloadBlob(new Blob([body], { type: 'application/json;charset=utf-8' }), `${this.filePrefix()}.json`);
  }

  private downloadMarkdown(snapshot: ExportSnapshot): void {
    const lines: string[] = [];
    lines.push('# TCE Export Bundle');
    lines.push('');
    lines.push(`- Generated: ${snapshot.generated_at}`);
    lines.push(`- Session: ${snapshot.session_id}`);
    lines.push(`- Preset: ${snapshot.preset}`);
    lines.push(`- Query: ${snapshot.query}`);
    lines.push(`- Events limit: ${snapshot.events_limit}`);
    lines.push(`- Days: ${snapshot.days}`);
    lines.push('');
    lines.push('## Summary');
    lines.push('');
    lines.push(`- Selected sections: ${snapshot.summary.selected}`);
    lines.push(`- Successful sections: ${snapshot.summary.succeeded}`);
    lines.push(`- Failed sections: ${snapshot.summary.failed}`);
    lines.push('');
    lines.push('## Section Metrics');
    lines.push('');
    lines.push('| Section | Status | Count |');
    lines.push('|---|---:|---:|');
    for (const section of this.sectionDefs) {
      if (!this.sectionState[section.key]) continue;
      const status = this.sectionStatuses[section.key];
      lines.push(`| ${section.label} | ${status.state} | ${status.count} |`);
    }
    lines.push('');
    lines.push('## Errors');
    lines.push('');
    if (Object.keys(snapshot.errors).length === 0) {
      lines.push('- none');
    } else {
      for (const key of Object.keys(snapshot.errors) as ExportSectionKey[]) {
        lines.push(`- ${key}: ${snapshot.errors[key]}`);
      }
    }
    lines.push('');
    lines.push('## Raw Payload');
    lines.push('');
    lines.push('```json');
    lines.push(JSON.stringify(snapshot.sections, null, 2));
    lines.push('```');
    lines.push('');
    const body = lines.join('\n');
    this.downloadBlob(new Blob([body], { type: 'text/markdown;charset=utf-8' }), `${this.filePrefix()}.md`);
  }

  private downloadCsv(snapshot: ExportSnapshot): void {
    const rows: Array<Record<string, unknown>> = [];
    for (const section of this.sectionDefs) {
      if (!this.sectionState[section.key]) continue;
      const payload = snapshot.sections[section.key];
      const sectionRows = this.extractRows(section.key, payload);
      if (sectionRows.length === 0) {
        rows.push({ section: section.key, value: JSON.stringify(payload ?? null) });
      } else {
        for (const row of sectionRows) {
          rows.push({ section: section.key, ...this.flattenRecord(row) });
        }
      }
    }
    const headers = this.unionHeaders(rows);
    const csv = [headers.join(','), ...rows.map((row) => headers.map((h) => this.escapeCsv(row[h])).join(','))].join('\n');
    this.downloadBlob(new Blob([csv], { type: 'text/csv;charset=utf-8' }), `${this.filePrefix()}.csv`);
  }

  private extractRows(section: ExportSectionKey, payload: unknown): Array<Record<string, unknown>> {
    if (!payload || typeof payload !== 'object') return [];
    const source = payload as Record<string, unknown>;
    if (section === 'events' && Array.isArray(source['events'])) return source['events'] as Array<Record<string, unknown>>;
    if (section === 'observations' && Array.isArray(source['observations'])) return source['observations'] as Array<Record<string, unknown>>;
    if (section === 'episodes' && Array.isArray(source['episodes'])) return source['episodes'] as Array<Record<string, unknown>>;
    if (section === 'workflow_templates' && Array.isArray(source['templates'])) return source['templates'] as Array<Record<string, unknown>>;
    if (section === 'memory_rules' && Array.isArray(source['rules'])) return source['rules'] as Array<Record<string, unknown>>;
    if (section === 'patterns' && Array.isArray(payload)) return payload as Array<Record<string, unknown>>;
    if (section === 'human_score_history' && Array.isArray(source['points'])) return source['points'] as Array<Record<string, unknown>>;
    if (section === 'retrieval_eval_status' && Array.isArray(source['history'])) return source['history'] as Array<Record<string, unknown>>;
    if (Array.isArray(payload)) return payload as Array<Record<string, unknown>>;
    return [];
  }

  private flattenRecord(input: Record<string, unknown>): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(input)) {
      if (value === null || value === undefined) {
        out[key] = '';
      } else if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
        out[key] = value;
      } else {
        out[key] = JSON.stringify(value);
      }
    }
    return out;
  }

  private unionHeaders(rows: Array<Record<string, unknown>>): string[] {
    const set = new Set<string>();
    for (const row of rows) {
      for (const key of Object.keys(row)) {
        set.add(key);
      }
    }
    return Array.from(set);
  }

  private escapeCsv(value: unknown): string {
    const raw = value === null || value === undefined ? '' : String(value);
    if (raw.includes('"') || raw.includes(',') || raw.includes('\n')) {
      return `"${raw.replaceAll('"', '""')}"`;
    }
    return raw;
  }

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}
