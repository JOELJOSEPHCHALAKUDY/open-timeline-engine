import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import {
  SystemStatusResponse,
  ActivitySummaryResponse,
  TeamMembership,
  ResourceUsageSnapshot,
  ResourceServiceUsage,
} from '../../core/models';
import { resolveRoleIdentitiesFromDashboard, formatUptime, formatBytes, AgentInfo } from '../../shared/utils';

@Component({
  selector: 'app-health',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="page-header">
      <h1>Health & System</h1>
      <p>Service status, runtime role visibility, DB size, and container resource usage</p>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading system status...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div *ngIf="!loading && !error && status" class="fade-in">
      <div class="grid grid-4" style="margin-bottom: 20px;">
        <div class="card">
          <div class="card-header">
            <span class="card-title">API Server</span>
            <span class="status-dot" [class.connected]="status.api.status === 'ok'" [class.disconnected]="status.api.status !== 'ok'"></span>
          </div>
          <div class="stat-value" style="font-size: 24px;">{{ status.api.status === 'ok' ? 'Healthy' : 'Down' }}</div>
          <div class="stat-label">Uptime: {{ fmtUptime(status.api.uptime_seconds) }}</div>
        </div>

        <div class="card">
          <div class="card-header">
            <span class="card-title">Database</span>
            <span class="status-dot" [class.connected]="status.database.connected" [class.disconnected]="!status.database.connected"></span>
          </div>
          <div class="stat-value" style="font-size: 24px;">{{ status.database.connected ? 'Connected' : 'Down' }}</div>
          <div class="stat-label">{{ status.database.event_count | number }} events</div>
        </div>

        <div class="card">
          <div class="card-header">
            <span class="card-title">Redis</span>
            <span class="status-dot" [class.connected]="status.redis.connected" [class.disconnected]="!status.redis.connected"></span>
          </div>
          <div class="stat-value" style="font-size: 24px;">{{ status.redis.connected ? 'Connected' : 'Down' }}</div>
          <div class="stat-label">Cache & queue</div>
        </div>

        <div class="card">
          <div class="card-header">
            <span class="card-title">Ollama</span>
            <span class="status-dot" [class.connected]="status.ollama.connected" [class.disconnected]="!status.ollama.connected"></span>
          </div>
          <div class="stat-value" style="font-size: 24px;">{{ status.ollama.connected ? 'Connected' : 'Down' }}</div>
          <div class="stat-label">{{ status.ollama.models.length > 0 ? status.ollama.models.join(', ') : 'No models' }}</div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-bottom: 20px;">
        <div class="card">
          <div class="card-title" style="margin-bottom: 16px;">Database Metrics</div>
          <div class="grid grid-2">
            <div>
              <div class="stat-value">{{ status.database.event_count | number }}</div>
              <div class="stat-label">Events</div>
            </div>
            <div>
              <div class="stat-value">{{ status.database.observation_count | number }}</div>
              <div class="stat-label">Observations</div>
            </div>
            <div>
              <div class="stat-value">{{ status.database.pattern_count | number }}</div>
              <div class="stat-label">Patterns</div>
            </div>
            <div>
              <div class="stat-value">{{ status.database.embedding_count | number }}</div>
              <div class="stat-label">Embeddings</div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-title" style="margin-bottom: 16px;">Runtime Mode</div>
          <div style="display: flex; align-items: center; gap: 16px; margin-bottom: 16px;">
            <div class="toggle" [class.active]="status.runtime_mode.clone_enabled" (click)="toggleMode()"></div>
            <div>
              <div style="font-size: 16px; font-weight: 600;">{{ status.runtime_mode.mode }}</div>
              <div style="font-size: 13px; color: var(--muted);">
                Clone advisor is {{ status.runtime_mode.clone_enabled ? 'enabled' : 'disabled' }}
              </div>
            </div>
          </div>
          <div style="font-size: 13px; color: var(--muted); padding: 12px; background: #f8fafc; border-radius: 6px;">
            <strong>timeline_only:</strong> Record events, no clone advice<br>
            <strong>clone_advisor:</strong> Full clone advice pipeline active
          </div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-bottom: 20px;">
        <div class="card" *ngIf="executorList.length > 0">
          <div class="card-header">
            <span class="card-title">AI Role Visibility</span>
            <span class="badge badge-accent">{{ status.runtime_mode.mode }}</span>
          </div>

          <div class="grid grid-2" style="margin-top: 12px;">
            <div *ngFor="let role of executorList; let idx = index" style="padding: 10px; background: #f8fafc; border-radius: 8px;">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 4px;">Executor</div>
                <span *ngIf="idx === 0" class="badge badge-accent">primary</span>
              </div>
              <div style="font-size: 18px; font-weight: 700;">{{ role.label }}</div>
              <div style="font-size: 12px; color: var(--muted);">{{ role.id }}</div>
              <span class="badge" style="margin-top: 6px;" [class.badge-success]="role.status === 'active'" [class.badge-accent]="role.status === 'registered'" [class.badge-muted]="role.status === 'not seen'">
                {{ role.status }}
              </span>
            </div>
          </div>

          <div style="margin-top: 12px;" *ngIf="teamMemberships.length > 0">
            <div style="font-size: 12px; color: var(--muted); margin-bottom: 6px;">Workspace memberships</div>
            <div *ngFor="let member of teamMemberships" style="display:flex; justify-content:space-between; font-size:13px; padding: 3px 0;">
              <span>{{ member.user_id }}</span>
              <span class="badge badge-muted">{{ member.role }}</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-title" style="margin-bottom: 12px;">Storage & Memory</div>
          <div class="grid grid-2">
            <div>
              <div class="stat-value" style="font-size: 18px;">{{ dbSizePretty }}</div>
              <div class="stat-label">Database on-disk size</div>
            </div>
            <div>
              <div class="stat-value" style="font-size: 18px;">{{ fmtBytes(apiResidentBytes) }}</div>
              <div class="stat-label">API resident memory (RSS)</div>
            </div>
            <div>
              <div class="stat-value" style="font-size: 18px;">{{ fmtBytes(apiVirtualBytes) }}</div>
              <div class="stat-label">API virtual memory</div>
            </div>
            <div>
              <div class="stat-value" style="font-size: 18px;">{{ fmtBytes(localStorageBytes) }}</div>
              <div class="stat-label">Browser local storage</div>
            </div>
          </div>
          <div style="margin-top: 10px; font-size: 12px; color: var(--muted);" *ngIf="resourceUsage?.database?.error">
            DB metric note: {{ resourceUsage?.database?.error }}
          </div>
        </div>
      </div>

      <div class="card" style="margin-bottom: 20px;">
        <div class="card-header">
          <span class="card-title">Container CPU / Memory Usage</span>
          <span class="badge badge-muted">{{ containerServices.length }} services</span>
        </div>
        <div *ngIf="containerServices.length === 0" style="font-size: 13px; color: var(--muted); padding-top: 8px;">
          {{ dockerStatusMessage }}
        </div>
        <div *ngIf="containerServices.length > 0" style="margin-top: 12px;">
          <div *ngFor="let svc of containerServices" style="padding: 10px 0; border-bottom: 1px solid var(--line);">
            <div style="display:flex; justify-content:space-between; align-items: center;">
              <div style="font-weight: 600;">{{ svc.service }}</div>
              <span class="badge badge-accent">{{ svc.cpu_percent.toFixed(2) }}% CPU</span>
            </div>
            <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">
              {{ svc.container_name }} &#183; {{ svc.project || 'unscoped' }}
            </div>
            <div class="grid grid-3" style="margin-top: 8px;">
              <div style="font-size: 12px;">
                <strong>{{ fmtBytes(svc.memory_usage_bytes) }}</strong><br>
                <span style="color: var(--muted);">memory usage</span>
              </div>
              <div style="font-size: 12px;">
                <strong>{{ svc.memory_percent.toFixed(2) }}%</strong><br>
                <span style="color: var(--muted);">memory %</span>
              </div>
              <div style="font-size: 12px;">
                <strong>{{ svc.pids }}</strong><br>
                <span style="color: var(--muted);">pids</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="card" *ngIf="activity">
        <div class="card-header">
          <span class="card-title">Today's Hourly Activity</span>
          <span class="badge badge-accent">{{ activity.total_events }} events</span>
        </div>
        <div style="display: flex; align-items: flex-end; gap: 4px; height: 120px; padding-top: 16px;" *ngIf="hourlyData.length > 0">
          <div *ngFor="let bar of hourlyData" style="flex: 1; display: flex; flex-direction: column; align-items: center;">
            <div [style.height.px]="bar.height" style="width: 100%; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 2px; transition: height 0.3s;"></div>
            <div style="font-size: 10px; color: var(--muted); margin-top: 4px;">{{ bar.hour }}</div>
          </div>
        </div>
        <div *ngIf="hourlyData.length === 0" style="text-align: center; padding: 24px 0; color: var(--muted); font-size: 14px;">
          No hourly activity data available
        </div>
      </div>
    </div>
  `,
})
export class HealthComponent implements OnInit {
  status: SystemStatusResponse | null = null;
  activity: ActivitySummaryResponse | null = null;
  resourceUsage: ResourceUsageSnapshot | null = null;

  loading = true;
  error: string | null = null;
  hourlyData: { hour: string; count: number; height: number }[] = [];

  teamMemberships: TeamMembership[] = [];
  executorInfo: AgentInfo | null = null;
  additionalExecutorInfo: AgentInfo | null = null;
  executorList: AgentInfo[] = [];

  apiResidentBytes: number | null = null;
  apiVirtualBytes: number | null = null;
  localStorageBytes = 0;
  dbSizePretty = 'N/A';
  containerServices: ResourceServiceUsage[] = [];
  dockerStatusMessage = 'Container metrics unavailable.';

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    const configPromise = firstValueFrom(this.api.getDashboardClientConfig()).catch(() => null);
    const rolesPromise = firstValueFrom(this.api.getDashboardAgentRoles()).catch(() => null);
    Promise.all([
      firstValueFrom(this.api.getSystemStatus()),
      firstValueFrom(this.api.getActivitySummary('today')),
      firstValueFrom(this.api.getTeamMemberships()),
      firstValueFrom(this.api.getMetricsText()),
      configPromise,
      rolesPromise,
      firstValueFrom(this.api.getResourceUsage()),
    ])
      .then(([status, activity, memberships, metricsText, config, roles, resourceUsage]) => {
        this.status = status ?? null;
        this.activity = activity ?? null;
        this.resourceUsage = resourceUsage ?? null;
        this.teamMemberships = memberships ?? [];

        if (activity) {
          this.buildHourlyChart(activity);
        }

        if (roles) {
          const mapped = resolveRoleIdentitiesFromDashboard(roles, {
            executorClients: config?.executor_clients || [],
            knownIdentities: config?.known_identities || {},
          });
          this.executorInfo = mapped.executor;
          this.additionalExecutorInfo = mapped.additionalExecutor;
          this.executorList = mapped.executors.length > 0 ? mapped.executors : [mapped.executor, mapped.additionalExecutor];
        }

        this.resolveStorageAndMemory(metricsText ?? '', resourceUsage ?? null);
        this.loading = false;
      })
      .catch((err) => {
        this.error = err.message || 'Failed to load system status';
        this.loading = false;
      });
  }

  private buildHourlyChart(activity: ActivitySummaryResponse): void {
    if (!activity.hourly_buckets || Object.keys(activity.hourly_buckets).length === 0) return;
    const entries = Object.entries(activity.hourly_buckets).sort();
    const maxCount = Math.max(...entries.map((e) => e[1]), 1);
    this.hourlyData = entries.map(([hour, count]) => ({
      hour: hour.split(':')[0] || hour,
      count,
      height: Math.max(2, (count / maxCount) * 100),
    }));
  }

  private resolveStorageAndMemory(metricsText: string, resourceUsage: ResourceUsageSnapshot | null): void {
    this.apiResidentBytes = this.readMetric(metricsText, 'process_resident_memory_bytes');
    this.apiVirtualBytes = this.readMetric(metricsText, 'process_virtual_memory_bytes');
    this.localStorageBytes = this.calculateLocalStorageBytes();

    if (resourceUsage?.database?.available) {
      this.dbSizePretty = resourceUsage.database.size_pretty || formatBytes(resourceUsage.database.size_bytes);
    } else {
      this.dbSizePretty = 'N/A';
    }

    this.containerServices = resourceUsage?.services ?? [];
    if (resourceUsage?.docker?.available && this.containerServices.length === 0) {
      this.dockerStatusMessage = 'No running TCE containers detected by Docker API.';
    } else if (!resourceUsage?.docker?.available) {
      this.dockerStatusMessage = resourceUsage?.docker?.error || 'Docker API is unavailable from this runtime.';
    } else {
      this.dockerStatusMessage = '';
    }
  }

  private readMetric(metricsText: string, metricName: string): number | null {
    if (!metricsText) return null;
    const line = metricsText
      .split('\n')
      .find((row) => row.startsWith(`${metricName} `) && !row.startsWith('#'));
    if (!line) return null;
    const value = Number(line.split(' ').pop());
    return Number.isFinite(value) ? value : null;
  }

  private calculateLocalStorageBytes(): number {
    try {
      let bytes = 0;
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i) || '';
        const value = localStorage.getItem(key) || '';
        bytes += new Blob([key]).size;
        bytes += new Blob([value]).size;
      }
      return bytes;
    } catch {
      return 0;
    }
  }

  toggleMode(): void {
    if (!this.status) return;
    const newMode = this.status.runtime_mode.clone_enabled ? 'timeline_only' : 'clone_advisor';
    this.api.setRuntimeMode(newMode).subscribe({
      next: () => {
        this.status!.runtime_mode.mode = newMode;
        this.status!.runtime_mode.clone_enabled = newMode === 'clone_advisor';
      },
      error: (err) => {
        this.error = 'Failed to toggle mode: ' + (err.message || 'unknown error');
      },
    });
  }

  fmtUptime(seconds: number): string {
    return formatUptime(seconds);
  }

  fmtBytes(value: number | null): string {
    return formatBytes(value);
  }
}
