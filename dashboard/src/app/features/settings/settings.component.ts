import { Component, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import { AuthService, AuthSettings } from '../../core/services/auth.service';
import {
  AdvisorProviderItem,
  AdvisorProvidersResponse,
  DashboardClientConfig,
  DashboardStackRestartStatusResponse,
} from '../../core/models';

type RouteMode = 'local' | 'web';
type LocalPlatform = 'ollama' | 'lm_studio';

const WEB_DEFAULT_MODE_IDS = new Set([
  'gpt-4o-mini',
  'gpt-4.1-mini',
  'gpt-4.1',
  'claude-3-7-sonnet-latest',
  'claude-3-5-sonnet-latest',
  'gemini-2.0-flash',
  'gemini-1.5-pro',
]);

const KNOWN_WEB_BASE_URL_MARKERS = [
  'api.openai.com',
  'api.deepseek.com',
  'api.anthropic.com',
  'openrouter.ai',
  'api.groq.com',
  'api.together.xyz',
  'generativelanguage.googleapis.com',
  'dashscope.aliyuncs.com',
  'open.bigmodel.cn',
  'api.moonshot.cn',
  'qianfan.baidubce.com',
  'hunyuan.tencentcloudapi.com',
  'api.x.ai',
];

interface EditableAdvisorRoute {
  id: string;
  mode: RouteMode;
  platform: LocalPlatform;
  providerId: string;
  model: string;
  pullModel: string;
  baseUrl: string;
  apiKeyRef: string;
  apiKeyInput: string;
  timeoutMs: number;
  liveModels: string[];
  liveSource: string;
  statusMessage: string | null;
  statusOk: boolean | null;
  loadingModels: boolean;
  verifying: boolean;
  pullJobId: string | null;
  pullState: string | null;
  pullProgress: number;
  pullError: string | null;
  pullPollTimer: number | null;
  keyStatus: 'checking' | 'stored' | 'missing' | null;
}

interface RouteProbeSample {
  ts: string;
  route_id: string;
  route_label: string;
  provider_id: string;
  model: string | null;
  ok: boolean;
  latency_ms: number;
  code: string;
  message: string;
  source: 'route_verify' | 'runtime_probe';
  circuit_state: string;
}

interface RouteProbeSummary {
  route_label: string;
  provider_id: string;
  sample_count: number;
  success_count: number;
  success_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  last_circuit_state: string;
  last_checked_at: string;
}

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="page-header">
      <h1>Settings</h1>
      <p>Configure your TCE system, advisor LLM routes, and executor identities.</p>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading settings...</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div *ngIf="!loading" class="fade-in">

      <!-- ====== SECTION 1: SYSTEM ====== -->
      <div class="card" style="margin-bottom: 24px;">
        <div class="card-header">
          <span class="card-title">System</span>
          <span
            class="badge"
            [class.badge-success]="connectionState === 'ok'"
            [class.badge-danger]="connectionState === 'error'"
            [class.badge-muted]="connectionState === 'idle'"
          >
            {{ connectionLabel }}
          </span>
        </div>

        <div class="grid grid-2" style="margin-top: 14px; gap: 14px;">
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Runtime mode</label>
            <div style="display:flex; gap:8px; align-items:center;">
              <select class="input" [(ngModel)]="runtimeModeSelection" style="width:auto;min-width:160px;">
                <option value="timeline_only">timeline_only</option>
                <option value="clone_advisor">clone_advisor</option>
              </select>
              <button class="btn btn-primary" (click)="changeRuntimeMode()" [disabled]="runtimeModeSaving">
                {{ runtimeModeSaving ? 'Applying...' : 'Apply' }}
              </button>
            </div>
            <span *ngIf="runtimeModeMessage" style="font-size:12px;color:var(--muted);margin-top:4px;display:block;">{{ runtimeModeMessage }}</span>
          </div>

          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">API base</label>
            <div>{{ serverConfig?.api_base || '/v1' }}</div>
          </div>

          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Default workspace</label>
            <div>{{ serverConfig?.default_workspace_id || 'personal' }}</div>
          </div>

          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Active clients</label>
            <div>{{ (serverConfig?.executor_clients || []).join(', ') || '-' }}</div>
          </div>
        </div>

        <div style="display:flex; gap:10px; margin-top:14px; align-items:center; flex-wrap:wrap;">
          <button class="btn btn-primary" (click)="testConnection()">Test connection</button>
          <button class="btn" (click)="reloadAll()">Refresh all</button>
          <span style="font-size:13px;color:var(--muted);" *ngIf="connectionMessage">{{ connectionMessage }}</span>
        </div>

        <div style="display:flex; gap:10px; margin-top:14px; align-items:center; flex-wrap:wrap; padding-top:12px; border-top:1px solid rgba(255,255,255,0.06);">
          <label style="font-size:12px;color:var(--muted);">Restart stack:</label>
          <select class="input" [(ngModel)]="restartTarget" style="width:auto;min-width:80px;">
            <option value="full">full</option>
            <option value="lite">lite</option>
          </select>
          <button class="btn" (click)="restartNow()" [disabled]="restartRunning">
            {{ restartRunning ? 'Restarting...' : 'Restart' }}
          </button>
          <span style="font-size:13px;color:var(--muted);" *ngIf="restartMessage">{{ restartMessage }}</span>
        </div>
      </div>

      <!-- ====== SECTION 2: ADVISOR CONFIGURATION ====== -->
      <div class="card" style="margin-bottom: 24px;">
        <div class="card-header">
          <span class="card-title">Advisor configuration</span>
          <button class="btn" (click)="addFallbackRoute()">+ Add route</button>
        </div>

        <div *ngIf="advisorError" class="error-banner" style="margin-top:12px;">{{ advisorError }}</div>

        <div *ngIf="advisorRestartVisible" style="
          margin:12px 0; padding:10px 14px;
          background:rgba(251,191,36,0.12);
          border:1px solid rgba(251,191,36,0.3);
          border-radius:8px;
          display:flex; align-items:center; gap:12px; flex-wrap:wrap;
        ">
          <span style="font-size:13px;color:var(--text);">Config saved. Restart to apply?</span>
          <button class="btn btn-primary" (click)="restartFromAdvisor()">Restart</button>
          <button class="btn" (click)="advisorRestartVisible=false">Later</button>
        </div>

        <!-- Route #1 (primary) -->
        <div style="margin-top:12px; padding:12px 14px; border:1px solid rgba(255,255,255,0.08); border-radius:8px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
            <span style="font-size:14px; font-weight:600; color:var(--text);">
              Route #1 <span style="font-size:11px; color:var(--muted); font-weight:400;">(primary)</span>
            </span>
          </div>
          <ng-container *ngTemplateOutlet="routeFields; context: { $implicit: primaryRoute }"></ng-container>
        </div>

        <!-- Fallback routes -->
        <div *ngFor="let route of fallbackRoutes; let i = index"
          style="margin-top:12px; padding:12px 14px; border:1px solid rgba(255,255,255,0.08); border-radius:8px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
            <span style="font-size:14px; font-weight:600; color:var(--text);">Route #{{ i + 2 }}</span>
            <div style="display:flex; gap:6px;">
              <button class="btn" style="padding:4px 8px;font-size:12px;" (click)="moveFallbackRoute(i, -1)">Up</button>
              <button class="btn" style="padding:4px 8px;font-size:12px;" (click)="moveFallbackRoute(i, 1)" [disabled]="i===fallbackRoutes.length-1">Down</button>
              <button class="btn" style="padding:4px 8px;font-size:12px;" (click)="removeFallbackRoute(i)">Remove</button>
            </div>
          </div>
          <ng-container *ngTemplateOutlet="routeFields; context: { $implicit: route }"></ng-container>
        </div>

        <div style="display:flex; gap:10px; margin-top:14px; align-items:center; flex-wrap:wrap;">
          <button class="btn btn-primary" (click)="saveAdvisorConfig()">Apply</button>
          <button class="btn btn-primary" (click)="saveAndRestartAdvisor()" [disabled]="restartRunning"
            style="background:var(--warning, #f59e0b);border-color:var(--warning, #f59e0b);color:#000;">
            {{ restartRunning ? 'Restarting...' : 'Apply & Restart' }}
          </button>
          <button class="btn" (click)="checkAllRoutes()">Check all routes</button>
          <span style="font-size:13px;color:var(--muted);" *ngIf="advisorRuntimeMessage">{{ advisorRuntimeMessage }}</span>
        </div>

        <div style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06);">
          <div class="card-header" style="margin-bottom:10px;">
            <span class="card-title">Route probe history</span>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              <span class="badge badge-muted">{{ routeProbeHistory.length }} samples</span>
              <button class="btn" (click)="probeAdvisorRuntime()">Runtime probe</button>
              <button class="btn" (click)="clearRouteProbeHistory()" [disabled]="routeProbeHistory.length === 0">
                Clear history
              </button>
            </div>
          </div>

          <div *ngIf="routeProbeHistory.length === 0" style="font-size:13px;color:var(--muted);">
            No probe samples yet. Use Verify, Check all routes, or Runtime probe to capture history.
          </div>

          <div *ngIf="routeProbeSummaries.length > 0" style="overflow:auto;max-height:240px;">
            <table class="table">
              <thead>
                <tr>
                  <th>Route</th>
                  <th>Success rate</th>
                  <th>P50</th>
                  <th>P95</th>
                  <th>Circuit</th>
                  <th>Last checked</th>
                </tr>
              </thead>
              <tbody>
                <tr *ngFor="let row of routeProbeSummaries">
                  <td>{{ row.route_label }}</td>
                  <td>{{ row.success_rate | number:'1.0-0' }}% ({{ row.success_count }}/{{ row.sample_count }})</td>
                  <td>{{ row.p50_latency_ms | number:'1.0-0' }}ms</td>
                  <td>{{ row.p95_latency_ms | number:'1.0-0' }}ms</td>
                  <td>{{ row.last_circuit_state }}</td>
                  <td>{{ row.last_checked_at | date:'short' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div *ngIf="routeProbeRecent.length > 0" style="margin-top:10px;overflow:auto;max-height:220px;">
            <table class="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Route</th>
                  <th>Result</th>
                  <th>Latency</th>
                  <th>Code</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                <tr *ngFor="let sample of routeProbeRecent">
                  <td>{{ sample.ts | date:'shortTime' }}</td>
                  <td>{{ sample.route_label }}</td>
                  <td>
                    <span class="badge" [class.badge-success]="sample.ok" [class.badge-danger]="!sample.ok">
                      {{ sample.ok ? 'ok' : 'failed' }}
                    </span>
                  </td>
                  <td>{{ sample.latency_ms | number:'1.0-0' }}ms</td>
                  <td>{{ sample.code }}</td>
                  <td>{{ sample.message }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Shared route fields template -->
      <ng-template #routeFields let-route>
        <div style="margin-bottom:10px;">
          <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Connection mode</label>
          <div class="segmented">
            <button class="btn" [class.btn-primary]="route.mode==='local'" (click)="setRouteMode(route, 'local')">Local</button>
            <button class="btn" [class.btn-primary]="route.mode==='web'" (click)="setRouteMode(route, 'web')">Web</button>
          </div>
        </div>

        <div class="grid grid-2" style="gap:12px;">
          <div *ngIf="route.mode === 'local'">
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Platform</label>
            <select class="input" [ngModel]="route.platform" (ngModelChange)="setRoutePlatform(route, $event)">
              <option value="ollama">Ollama</option>
              <option value="lm_studio">LM Studio</option>
            </select>
          </div>

          <div *ngIf="route.mode === 'web'">
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Provider</label>
            <select class="input" [ngModel]="route.providerId" (ngModelChange)="setRouteProvider(route, $event)">
              <option *ngFor="let p of webProviders" [value]="p.provider_id">{{ p.label }} ({{ p.provider_category }})</option>
            </select>
          </div>

          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Model</label>
            <select class="input" [(ngModel)]="route.model" *ngIf="route.liveModels.length > 0">
              <option *ngFor="let m of route.liveModels" [value]="m">{{ m }}</option>
            </select>
            <input class="input" [(ngModel)]="route.model" placeholder="Model id" *ngIf="route.liveModels.length === 0" />
          </div>

          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Base URL (override)</label>
            <input class="input" [(ngModel)]="route.baseUrl" placeholder="Provider endpoint" />
          </div>

          <div *ngIf="requiresApiKey(route)">
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">
              API key
              <span *ngIf="route.keyStatus === 'checking'" style="font-size:11px;color:var(--muted);margin-left:6px;">⏳ checking...</span>
              <span *ngIf="route.keyStatus === 'stored'" style="font-size:11px;color:#4ade80;margin-left:6px;">● Key configured (.env)</span>
              <span *ngIf="route.keyStatus === 'missing'" style="font-size:11px;color:#f87171;margin-left:6px;">● No key stored</span>
            </label>
            <input class="input" [(ngModel)]="route.apiKeyInput" placeholder="sk-..." type="password" />
          </div>

          <div *ngIf="requiresApiKey(route)">
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Key storage name</label>
            <input class="input" [(ngModel)]="route.apiKeyRef" placeholder="advisor-openai" />
          </div>

          <div *ngIf="isOllama(route)">
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Pull model to Ollama</label>
            <input class="input" [(ngModel)]="route.pullModel" placeholder="qwen3:8b" />
          </div>
        </div>

        <div style="display:flex; gap:8px; margin-top:10px; align-items:center; flex-wrap:wrap;">
          <button class="btn" style="font-size:13px;" (click)="loadLiveModels(route)" [disabled]="route.loadingModels">
            {{ route.loadingModels ? 'Loading...' : 'Load available models' }}
          </button>
          <button class="btn" style="font-size:13px;" (click)="verifyRoute(route)" [disabled]="route.verifying">
            {{ route.verifying ? 'Verifying...' : 'Verify' }}
          </button>
          <button class="btn" style="font-size:13px;" *ngIf="isOllama(route)" (click)="pullOllamaModel(route)">Pull to Ollama</button>
          <span *ngIf="route.statusMessage" style="font-size:12px;"
            [style.color]="route.statusOk === true ? '#4ade80' : route.statusOk === false ? '#f87171' : 'var(--muted)'">
            {{ route.statusMessage }}
          </span>
        </div>

        <div *ngIf="isOllama(route) && route.pullJobId" style="margin-top:8px;font-size:12px;color:var(--muted);">
          Ollama pull {{ route.pullState }} ({{ route.pullProgress | number:'1.0-0' }}%)
          <span *ngIf="route.pullError" style="color:#f87171;"> - {{ route.pullError }}</span>
        </div>
      </ng-template>

      <!-- ====== SECTION 3: EXECUTOR & IDENTITY ====== -->
      <div class="card" style="margin-bottom: 24px;">
        <div class="card-header">
          <span class="card-title">Executor & Identity</span>
        </div>

        <div *ngIf="executorConfigError" class="error-banner" style="margin-top:12px;">{{ executorConfigError }}</div>

        <div *ngIf="executorRestartVisible" style="
          margin:12px 0; padding:10px 14px;
          background:rgba(251,191,36,0.12);
          border:1px solid rgba(251,191,36,0.3);
          border-radius:8px;
          display:flex; align-items:center; gap:12px; flex-wrap:wrap;
        ">
          <span style="font-size:13px;color:var(--text);">Config saved. Restart to apply?</span>
          <button class="btn btn-primary" (click)="restartFromExecutor()">Restart</button>
          <button class="btn" (click)="executorRestartVisible=false">Later</button>
        </div>

        <div class="grid grid-2" style="margin-top:14px; gap:14px;">
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Workspace</label>
            <input class="input" [(ngModel)]="executorWorkspaceId" placeholder="personal" />
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Executor clients</label>
            <input class="input" [(ngModel)]="executorClientsText" placeholder="codex,claude,cursor" />
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Executor user</label>
            <input class="input" [(ngModel)]="executorUserId" placeholder="codex-executor" />
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Executor consumer</label>
            <input class="input" [(ngModel)]="executorConsumerId" placeholder="codex-executor" />
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Additional executor user (next client)</label>
            <input class="input" [(ngModel)]="additionalExecutorUserId" placeholder="claude-executor" />
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Additional executor consumer (next client)</label>
            <input class="input" [(ngModel)]="additionalExecutorConsumerId" placeholder="claude-executor" />
          </div>
        </div>

        <div style="margin-top:12px; padding:10px 12px; background:#f8fafc; border-radius:8px;">
          <div style="font-size:12px; color:var(--muted); margin-bottom:8px;">Resolved executor identities (all configured clients)</div>
          <div *ngIf="executorIdentityRows.length === 0" style="font-size:13px;color:var(--muted);">
            Add clients in <code>Executor clients</code> to preview per-client identities.
          </div>
          <div *ngFor="let row of executorIdentityRows" style="display:grid; grid-template-columns: 120px 1fr 1fr auto; gap:8px; align-items:center; font-size:13px; padding:4px 0;">
            <span style="font-weight:600;">{{ row.client }}</span>
            <span style="font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;">{{ row.userId }}</span>
            <span style="font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;">{{ row.consumerId }}</span>
            <span class="badge" [class.badge-accent]="row.source === 'custom'" [class.badge-muted]="row.source !== 'custom'">
              {{ row.source }}
            </span>
          </div>
          <div style="font-size:12px;color:var(--muted);margin-top:8px;">
            Clients after the second use derived defaults: <code>&lt;client&gt;-executor</code>.
          </div>
        </div>

        <div style="display:flex; gap:10px; margin-top:14px; align-items:center; flex-wrap:wrap;">
          <button class="btn btn-primary" (click)="saveExecutorConfig()">Apply</button>
          <button class="btn btn-primary" (click)="saveAndRestartExecutor()" [disabled]="restartRunning"
            style="background:var(--warning, #f59e0b);border-color:var(--warning, #f59e0b);color:#000;">
            {{ restartRunning ? 'Restarting...' : 'Apply & Restart' }}
          </button>
          <span style="font-size:13px;color:var(--muted);" *ngIf="executorConfigMessage">{{ executorConfigMessage }}</span>
        </div>

        <!-- Dashboard auth sub-section -->
        <div style="margin-top:18px; padding-top:14px; border-top:1px solid rgba(255,255,255,0.06);">
          <div style="font-size:14px; font-weight:600; color:var(--text); margin-bottom:12px;">Dashboard auth</div>
          <div class="grid grid-2" style="gap:14px;">
            <div>
              <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">API token</label>
              <input class="input" [(ngModel)]="settings.token" type="password" />
            </div>
            <div>
              <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Dashboard consumer</label>
              <input class="input" [(ngModel)]="settings.consumer" />
            </div>
            <div>
              <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Dashboard role</label>
              <select class="input" [(ngModel)]="settings.role">
                <option value="user">user</option>
                <option value="executor">executor</option>
                <option value="advisor">advisor</option>
                <option value="admin">admin</option>
              </select>
            </div>
            <div>
              <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Dashboard workspace</label>
              <input class="input" [(ngModel)]="settings.workspace" placeholder="personal" />
            </div>
            <div>
              <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Dashboard user</label>
              <input class="input" [(ngModel)]="settings.user" />
            </div>
          </div>
          <div style="display:flex; gap:10px; margin-top:12px; align-items:center; flex-wrap:wrap;">
            <button class="btn btn-primary" (click)="save()">Save auth</button>
            <button class="btn" (click)="resetLocal()">Reset to defaults</button>
            <span style="font-size:13px;color:var(--muted);" *ngIf="localOverrideMessage">{{ localOverrideMessage }}</span>
          </div>
        </div>
      </div>

    </div>
  `,
})
export class SettingsComponent implements OnInit, OnDestroy {
  loading = true;
  error: string | null = null;
  settings: AuthSettings;
  serverConfig: DashboardClientConfig | null = null;
  advisorProvidersResponse: AdvisorProvidersResponse | null = null;

  advisorError: string | null = null;
  advisorRuntimeMessage: string | null = null;
  primaryRoute: EditableAdvisorRoute = this.createDefaultRoute('web');
  fallbackRoutes: EditableAdvisorRoute[] = [];
  activeProfileId = 'default';

  executorWorkspaceId = 'personal';
  executorClientsText = '';
  executorUserId = '';
  executorConsumerId = '';
  additionalExecutorUserId = '';
  additionalExecutorConsumerId = '';
  executorConfigError: string | null = null;
  executorConfigMessage: string | null = null;

  restartTarget: 'full' | 'lite' = 'full';
  restartMessage: string | null = null;
  restartRunning = false;
  private restartJobId: string | null = null;
  private restartPollTimer: number | null = null;
  routeProbeHistory: RouteProbeSample[] = [];
  private readonly routeProbeStorageKey = 'tce.dashboard.route_probe_history.v1';
  private readonly routeProbeMaxSamples = 300;

  runtimeModeSelection = 'timeline_only';
  runtimeModeSaving = false;
  runtimeModeMessage: string | null = null;

  advisorRestartVisible = false;
  executorRestartVisible = false;

  localOverrideMessage: string | null = null;
  connectionState: 'idle' | 'ok' | 'error' = 'idle';
  connectionMessage: string | null = null;

  constructor(
    private api: ApiService,
    private auth: AuthService,
  ) {
    this.settings = this.auth.get();
  }

  ngOnInit(): void {
    this.loadRouteProbeHistory();
    this.reloadAll();
  }

  ngOnDestroy(): void {
    this.clearRestartPoller();
    this.clearAllRoutePollers();
  }

  get connectionLabel(): string {
    if (this.connectionState === 'ok') return 'Connected';
    if (this.connectionState === 'error') return 'Connection failed';
    return 'Not tested';
  }

  get restartStateLabel(): string {
    if (this.restartRunning) return 'running';
    if (this.restartJobId) return 'completed';
    return 'idle';
  }

  get allProviders(): AdvisorProviderItem[] {
    return this.advisorProvidersResponse?.providers || [];
  }

  get webProviders(): AdvisorProviderItem[] {
    return this.allProviders.filter((p) => (p.connection_type || 'web') !== 'local');
  }

  get localProviders(): AdvisorProviderItem[] {
    return this.allProviders.filter((p) => (p.connection_type || 'web') === 'local');
  }

  get routeProbeRecent(): RouteProbeSample[] {
    return this.routeProbeHistory.slice(0, 20);
  }

  get routeProbeSummaries(): RouteProbeSummary[] {
    const grouped = new Map<string, RouteProbeSample[]>();
    for (const sample of this.routeProbeHistory) {
      const key = `${sample.route_id}|${sample.route_label}|${sample.provider_id}`;
      const bucket = grouped.get(key) || [];
      bucket.push(sample);
      grouped.set(key, bucket);
    }
    const rows: RouteProbeSummary[] = [];
    for (const [key, samples] of grouped.entries()) {
      const [, routeLabel, providerId] = key.split('|');
      const sortedByTime = [...samples].sort((a, b) => b.ts.localeCompare(a.ts));
      const latencyValues = samples
        .map((item) => Number(item.latency_ms || 0))
        .filter((item) => Number.isFinite(item) && item >= 0);
      const successCount = samples.filter((item) => item.ok).length;
      const sampleCount = samples.length;
      rows.push({
        route_label: routeLabel,
        provider_id: providerId,
        sample_count: sampleCount,
        success_count: successCount,
        success_rate: sampleCount > 0 ? (successCount / sampleCount) * 100 : 0,
        p50_latency_ms: this.percentile(latencyValues, 0.5),
        p95_latency_ms: this.percentile(latencyValues, 0.95),
        last_circuit_state: sortedByTime[0]?.circuit_state || 'unknown',
        last_checked_at: sortedByTime[0]?.ts || new Date(0).toISOString(),
      });
    }
    return rows.sort((a, b) => b.last_checked_at.localeCompare(a.last_checked_at));
  }

  private percentile(values: number[], quantile: number): number {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    const index = Math.min(sorted.length - 1, Math.max(0, Math.floor((sorted.length - 1) * quantile)));
    return sorted[index];
  }

  private loadRouteProbeHistory(): void {
    try {
      const raw = localStorage.getItem(this.routeProbeStorageKey);
      if (!raw) {
        this.routeProbeHistory = [];
        return;
      }
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        this.routeProbeHistory = [];
        return;
      }
      this.routeProbeHistory = parsed
        .filter((item) => !!item && typeof item === 'object')
        .map<RouteProbeSample>((item) => ({
          ts: String((item as any).ts || ''),
          route_id: String((item as any).route_id || ''),
          route_label: String((item as any).route_label || ''),
          provider_id: String((item as any).provider_id || ''),
          model: (item as any).model ? String((item as any).model) : null,
          ok: Boolean((item as any).ok),
          latency_ms: Number((item as any).latency_ms || 0),
          code: String((item as any).code || ''),
          message: String((item as any).message || ''),
          source: ((item as any).source === 'runtime_probe' ? 'runtime_probe' : 'route_verify') as RouteProbeSample['source'],
          circuit_state: String((item as any).circuit_state || 'unknown'),
        }))
        .filter((item) => item.ts.length > 0 && item.route_id.length > 0)
        .slice(0, this.routeProbeMaxSamples);
    } catch {
      this.routeProbeHistory = [];
    }
  }

  private persistRouteProbeHistory(): void {
    try {
      localStorage.setItem(this.routeProbeStorageKey, JSON.stringify(this.routeProbeHistory));
    } catch {
      // ignore storage failures
    }
  }

  private routePosition(route: EditableAdvisorRoute): number {
    if (route.id === this.primaryRoute.id) return 1;
    const idx = this.fallbackRoutes.findIndex((item) => item.id === route.id);
    if (idx >= 0) return idx + 2;
    return 0;
  }

  private routeLabel(route: EditableAdvisorRoute): string {
    const pos = this.routePosition(route);
    const routeName = pos > 0 ? `Route #${pos}` : 'Route';
    const model = route.model ? `:${route.model}` : '';
    return `${routeName} · ${route.providerId}${model}`;
  }

  private findRouteForProbe(providerId: string, model: string | null): EditableAdvisorRoute | null {
    const routes = [this.primaryRoute, ...this.fallbackRoutes];
    const exact = routes.find((route) => route.providerId === providerId && (!!model ? route.model === model : true));
    if (exact) return exact;
    return routes.find((route) => route.providerId === providerId) || null;
  }

  private recordRouteProbe(sample: RouteProbeSample): void {
    this.routeProbeHistory = [sample, ...this.routeProbeHistory].slice(0, this.routeProbeMaxSamples);
    this.persistRouteProbeHistory();
  }

  reloadAll(): void {
    this.loading = true;
    this.error = null;
    Promise.all([this.reloadConfigInternal(), this.reloadAdvisorProvidersInternal()])
      .then(() => {
        this.loading = false;
        this.loadAdvisorRuntimeStatus();
        this.autoCheckKeyStatus();
      })
      .catch((err) => {
        this.error = err?.message || 'Failed to load settings';
        this.loading = false;
      });
  }

  clearRouteProbeHistory(): void {
    this.routeProbeHistory = [];
    try {
      localStorage.removeItem(this.routeProbeStorageKey);
    } catch {
      // ignore storage failures
    }
  }

  private async reloadConfigInternal(): Promise<void> {
    const [config, roles] = await Promise.all([
      firstValueFrom(this.api.getDashboardClientConfig()),
      firstValueFrom(this.api.getDashboardAgentRoles()).catch(() => null),
    ]);
    this.serverConfig = config;
    this.runtimeModeSelection = config.runtime_mode?.mode || 'timeline_only';

    // Always load from server — no sessionStorage overlay
    this.executorWorkspaceId = config.default_workspace_id || this.executorWorkspaceId;
    this.executorUserId = config.known_identities?.executor?.user_id || roles?.executor?.user_id || this.executorUserId;
    this.additionalExecutorUserId =
      config.known_identities?.secondary_executor?.user_id ||
      // Backward-compatible alias from older API/config payloads.
      config.known_identities?.advisor?.user_id ||
      roles?.secondary_executor?.user_id ||
      roles?.advisor?.user_id ||
      this.additionalExecutorUserId;
    this.executorConsumerId =
      config.default_consumer_id ||
      roles?.executor?.consumer_id ||
      this.executorConsumerId ||
      this.executorUserId;
    this.additionalExecutorConsumerId =
      config.known_identities?.secondary_executor?.user_id ||
      // Backward-compatible alias from older API/config payloads.
      config.known_identities?.advisor?.user_id ||
      roles?.secondary_executor?.consumer_id ||
      roles?.advisor?.consumer_id ||
      this.additionalExecutorConsumerId ||
      this.additionalExecutorUserId;
    if (Array.isArray(config.executor_clients) && config.executor_clients.length > 0) {
      this.executorClientsText = config.executor_clients.join(',');
    } else if (!this.executorClientsText) {
      this.executorClientsText = '';
    }
    if (!this.additionalExecutorConsumerId) {
      this.additionalExecutorConsumerId = this.additionalExecutorUserId || this.additionalExecutorConsumerId;
    }
  }

  private async reloadAdvisorProvidersInternal(): Promise<void> {
    this.advisorError = null;
    const resp = await firstValueFrom(this.api.getAdvisorProviders());
    this.advisorProvidersResponse = resp;
    this.activeProfileId = resp.config.active_profile_id || resp.config.profile_id || 'default';

    let routeSource: any = resp?.config?.routes;
    if (!Array.isArray(routeSource) && typeof routeSource === 'string') {
      try {
        const parsed = JSON.parse(routeSource);
        if (Array.isArray(parsed)) {
          routeSource = parsed;
        }
      } catch {
        routeSource = [];
      }
    }
    const routes = (Array.isArray(routeSource) ? routeSource : [])
      .filter((item) => !!item && typeof item === 'object')
      .sort((a, b) => ((a as any).priority || 0) - ((b as any).priority || 0));
    if (routes.length > 0) {
      this.primaryRoute = this.fromRouteConfig(routes[0]);
      this.fallbackRoutes = routes.slice(1).map((route) => this.fromRouteConfig(route));
      return;
    }

    this.primaryRoute = this.createDefaultRoute('web');
    this.primaryRoute.providerId = resp.config.advisor_primary_provider || this.defaultWebProviderId();
    this.primaryRoute.model = resp.config.advisor_primary_model || '';
    this.primaryRoute.baseUrl = resp.config.advisor_custom_base_url || this.defaultBaseUrl(this.primaryRoute.providerId);
    this.primaryRoute.apiKeyRef = resp.config.advisor_custom_api_key_ref || this.defaultApiKeyRef(this.primaryRoute.providerId);
    // Clear liveModels so normalizeRoute loads the correct provider's defaults
    this.primaryRoute.liveModels = [];
    this.primaryRoute.liveSource = '';
    this.normalizeRoute(this.primaryRoute);

    const fallbackChain = Array.isArray(resp.config.advisor_fallback_chain) ? resp.config.advisor_fallback_chain : [];
    this.fallbackRoutes = fallbackChain.map((providerId, idx) => {
      const route = this.createDefaultRoute('web');
      route.id = this.routeId(`fallback-${idx}`);
      route.providerId = providerId;
      route.baseUrl = this.defaultBaseUrl(providerId);
      route.apiKeyRef = this.defaultApiKeyRef(providerId);
      // Clear liveModels so normalizeRoute loads the correct provider's defaults
      route.liveModels = [];
      route.liveSource = '';
      this.normalizeRoute(route);
      return route;
    });
  }

  private providerById(providerId: string): AdvisorProviderItem | null {
    return this.allProviders.find((item) => item.provider_id === providerId) || null;
  }

  private defaultWebProviderId(): string {
    return this.webProviders[0]?.provider_id || 'openai';
  }

  private defaultLocalProviderId(platform: LocalPlatform): string {
    return platform === 'ollama' ? 'local_ollama' : 'local_lmstudio';
  }

  private defaultBaseUrl(providerId: string): string {
    return this.providerById(providerId)?.default_base_url || '';
  }

  private defaultApiKeyRef(providerId: string): string {
    return `advisor-${providerId || 'route'}`;
  }

  private providerDefaultModels(providerId: string): string[] {
    const models = this.providerById(providerId)?.default_models || [];
    return Array.isArray(models) ? models.filter((item) => !!item) : [];
  }

  private apiErrorMessage(err: any, fallback: string): string {
    const status = Number(err?.status || err?.error?.status || 0);
    const detail = err?.error?.detail || err?.message || fallback;
    if (status === 401) {
      return 'Invalid credentials (401). Check token/workspace/user in Local overrides, then retry.';
    }
    return detail;
  }

  private localFallbackBaseUrl(platform: LocalPlatform): string {
    if (platform === 'lm_studio') {
      return 'http://localhost:1234/v1';
    }
    return 'http://ollama:11434';
  }

  private isKnownWebBaseUrl(value: string): boolean {
    const lower = (value || '').toLowerCase();
    return KNOWN_WEB_BASE_URL_MARKERS.some((marker) => lower.includes(marker));
  }

  private routeId(prefix: string): string {
    return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
  }

  private createDefaultRoute(mode: RouteMode): EditableAdvisorRoute {
    const platform: LocalPlatform = 'ollama';
    const providerId = mode === 'local' ? this.defaultLocalProviderId(platform) : this.defaultWebProviderId();
    const defaultModels = mode === 'web' ? this.providerDefaultModels(providerId) : [];
    const defaultModel = defaultModels[0] || '';
    return {
      id: this.routeId('route'),
      mode,
      platform,
      providerId,
      model: defaultModel,
      pullModel: mode === 'local' && platform === 'ollama' ? defaultModel : '',
      baseUrl: this.defaultBaseUrl(providerId) || (mode === 'local' ? this.localFallbackBaseUrl(platform) : ''),
      apiKeyRef: this.defaultApiKeyRef(providerId),
      apiKeyInput: '',
      timeoutMs: 6000,
      liveModels: [...defaultModels],
      liveSource: defaultModels.length > 0 ? 'default' : '',
      statusMessage: null,
      statusOk: null,
      loadingModels: false,
      verifying: false,
      pullJobId: null,
      pullState: null,
      pullProgress: 0,
      pullError: null,
      pullPollTimer: null,
      keyStatus: null,
    };
  }

  private fromRouteConfig(route: {
    provider_id: string;
    model?: string | null;
    base_url?: string | null;
    api_key_ref?: string | null;
  }): EditableAdvisorRoute {
    const rawProviderId = String((route as any)?.provider_id ?? (route as any)?.provider ?? '').trim();
    const meta = this.providerById(rawProviderId);
    const isLocal = (meta?.connection_type || '').toLowerCase() === 'local' || rawProviderId.startsWith('local_');
    const platform: LocalPlatform = rawProviderId === 'local_lmstudio' ? 'lm_studio' : 'ollama';
    const parsed = this.createDefaultRoute(isLocal ? 'local' : 'web');
    parsed.providerId = rawProviderId || (isLocal ? this.defaultLocalProviderId(platform) : this.defaultWebProviderId());
    parsed.platform = platform;
    parsed.model = route.model || '';
    parsed.pullModel = route.model || '';
    parsed.baseUrl = route.base_url || this.defaultBaseUrl(parsed.providerId);
    parsed.apiKeyRef = route.api_key_ref || this.defaultApiKeyRef(parsed.providerId);
    // Clear liveModels inherited from createDefaultRoute — they belong to the
    // default web provider, not necessarily this route's provider. normalizeRoute
    // will re-populate from the correct provider's defaults.
    parsed.liveModels = [];
    parsed.liveSource = '';
    this.normalizeRoute(parsed);
    return parsed;
  }

  setRouteMode(route: EditableAdvisorRoute, mode: RouteMode): void {
    const modeChanged = route.mode !== mode;
    route.mode = mode;
    if (mode === 'local') {
      route.providerId = this.defaultLocalProviderId(route.platform || 'ollama');
      route.baseUrl = this.defaultBaseUrl(route.providerId) || this.localFallbackBaseUrl(route.platform || 'ollama');
      route.apiKeyRef = this.defaultApiKeyRef(route.providerId);
      route.apiKeyInput = '';
    } else {
      route.providerId = this.defaultWebProviderId();
      route.baseUrl = this.defaultBaseUrl(route.providerId);
      route.apiKeyRef = this.defaultApiKeyRef(route.providerId);
    }
    if (modeChanged) {
      route.model = '';
      route.pullModel = '';
      route.liveModels = [];
      route.liveSource = '';
      route.statusOk = null;
    }
    route.statusMessage = null;
    this.normalizeRoute(route);
  }

  setRoutePlatform(route: EditableAdvisorRoute, platform: LocalPlatform): void {
    const platformChanged = route.platform !== platform || route.mode !== 'local';
    route.platform = platform;
    route.mode = 'local';
    route.providerId = this.defaultLocalProviderId(platform);
    route.baseUrl = this.defaultBaseUrl(route.providerId) || this.localFallbackBaseUrl(platform);
    route.apiKeyRef = this.defaultApiKeyRef(route.providerId);
    route.apiKeyInput = '';
    if (platformChanged) {
      route.model = '';
      route.pullModel = '';
      route.liveModels = [];
      route.liveSource = '';
      route.statusOk = null;
    }
    route.statusMessage = null;
    this.normalizeRoute(route);
  }

  setRouteProvider(route: EditableAdvisorRoute, providerId: string): void {
    const providerChanged = route.providerId !== providerId;
    route.providerId = providerId;
    const meta = this.providerById(providerId);
    const connectionType = (meta?.connection_type || 'web').toLowerCase();
    route.mode = connectionType === 'local' ? 'local' : 'web';
    if (route.mode === 'local') {
      route.platform = providerId === 'local_lmstudio' ? 'lm_studio' : 'ollama';
      route.baseUrl = this.defaultBaseUrl(route.providerId) || this.localFallbackBaseUrl(route.platform);
      route.apiKeyInput = '';
    } else {
      route.baseUrl = this.defaultBaseUrl(route.providerId);
    }
    route.apiKeyRef = this.defaultApiKeyRef(route.providerId);
    const defaults = this.providerDefaultModels(providerId);
    const modelIncompatible = !!route.model && defaults.length > 0 && !defaults.includes(route.model);
    if (providerChanged || modelIncompatible) {
      route.model = '';
      route.pullModel = '';
      route.liveModels = [];
      route.liveSource = '';
      route.statusOk = null;
    }
    route.statusMessage = null;
    this.normalizeRoute(route);
  }

  private normalizeRoute(route: EditableAdvisorRoute): void {
    route.providerId = String(route.providerId || '').trim();
    if (!route.providerId) {
      route.providerId = route.mode === 'local'
        ? this.defaultLocalProviderId(route.platform || 'ollama')
        : this.defaultWebProviderId();
    }
    if (route.mode === 'local') {
      route.providerId = this.defaultLocalProviderId(route.platform || 'ollama');
    } else if (String(route.providerId || '').startsWith('local_')) {
      route.providerId = this.defaultWebProviderId();
    }
    if (!this.providerById(route.providerId)) {
      route.providerId = route.mode === 'local'
        ? this.defaultLocalProviderId(route.platform || 'ollama')
        : this.defaultWebProviderId();
    }

    const providerDefaultBaseUrl = this.defaultBaseUrl(route.providerId);
    if (route.mode === 'local' && route.providerId === 'local_ollama') {
      const lower = String(route.baseUrl || '').toLowerCase();
      if (!lower || lower.includes('localhost:11434') || lower.includes('127.0.0.1:11434')) {
        route.baseUrl = this.localFallbackBaseUrl('ollama');
      }
    }
    if (route.mode === 'local' && this.isKnownWebBaseUrl(route.baseUrl || '')) {
      route.baseUrl = providerDefaultBaseUrl || this.localFallbackBaseUrl(route.platform || 'ollama');
    }
    if (!route.baseUrl) {
      route.baseUrl = providerDefaultBaseUrl || (route.mode === 'local' ? this.localFallbackBaseUrl(route.platform || 'ollama') : '');
    }
    if (route.mode === 'local') {
      // Local routes should never inherit cloud key refs.
      route.apiKeyRef = this.defaultApiKeyRef(route.providerId);
    } else if (!route.apiKeyRef) {
      route.apiKeyRef = this.defaultApiKeyRef(route.providerId);
    }
    if (route.mode === 'local') {
      const localDefaults = this.providerDefaultModels(route.providerId);
      if (route.liveModels.length === 0 && localDefaults.length > 0) {
        route.liveModels = [...localDefaults];
        route.liveSource = route.liveSource || 'default';
      }
      if (!route.model && route.liveModels.length > 0) {
        route.model = route.liveModels[0];
      }
      if (route.model && route.liveModels.length > 0 && !route.liveModels.includes(route.model)) {
        route.model = route.liveModels[0];
      }
      const modelKey = (route.model || '').trim().toLowerCase();
      const pullModelKey = (route.pullModel || '').trim().toLowerCase();
      if (WEB_DEFAULT_MODE_IDS.has(modelKey)) {
        route.model = localDefaults[0] || '';
      }
      if (WEB_DEFAULT_MODE_IDS.has(pullModelKey)) {
        route.pullModel = '';
      }
      if (!route.pullModel) {
        route.pullModel = route.model || '';
      }
    }
    if (route.mode === 'web') {
      const defaultModels = this.providerDefaultModels(route.providerId);
      if (route.liveModels.length === 0 && defaultModels.length > 0) {
        route.liveModels = [...defaultModels];
        route.liveSource = route.liveSource || 'default';
      }
      if (route.model && route.liveModels.length > 0 && !route.liveModels.includes(route.model)) {
        route.model = '';
      }
      if (!route.model && route.liveModels.length > 0) {
        route.model = route.liveModels[0];
      }
    }
    if (this.isOllama(route) && !route.pullModel) {
      route.pullModel = route.model || '';
    }
  }

  isOllama(route: EditableAdvisorRoute): boolean {
    return route.mode === 'local' && route.platform === 'ollama';
  }

  requiresApiKey(route: EditableAdvisorRoute): boolean {
    if (route.mode === 'local') return false;
    const meta = this.providerById(route.providerId);
    if (!meta) return true;
    return !meta.key_optional;
  }

  addFallbackRoute(): void {
    const route = this.createDefaultRoute('web');
    this.fallbackRoutes = [...this.fallbackRoutes, route];
  }

  removeFallbackRoute(index: number): void {
    const copy = [...this.fallbackRoutes];
    const target = copy[index];
    if (target) {
      this.clearRoutePoller(target);
    }
    copy.splice(index, 1);
    this.fallbackRoutes = copy;
  }

  moveFallbackRoute(index: number, direction: -1 | 1): void {
    if (direction === -1 && index === 0 && this.fallbackRoutes.length > 0) {
      const promoted = this.fallbackRoutes[0];
      const demotedPrimary = this.primaryRoute;
      const remaining = this.fallbackRoutes.slice(1);
      this.primaryRoute = promoted;
      this.fallbackRoutes = [demotedPrimary, ...remaining];
      return;
    }
    const target = index + direction;
    if (target < 0 || target >= this.fallbackRoutes.length) return;
    const copy = [...this.fallbackRoutes];
    const [item] = copy.splice(index, 1);
    copy.splice(target, 0, item);
    this.fallbackRoutes = copy;
  }

  async loadLiveModels(route: EditableAdvisorRoute): Promise<void> {
    route.statusMessage = null;
    if (this.requiresApiKey(route) && !route.apiKeyInput && !route.apiKeyRef) {
      route.statusMessage = 'Set API key or API key ref before loading web models.';
      route.statusOk = false;
      return;
    }
    route.loadingModels = true;
    try {
      const resp = await firstValueFrom(
        this.api.getAdvisorLiveModels({
          provider_id: route.providerId,
          base_url: route.baseUrl || null,
          api_key: route.apiKeyInput || null,
          api_key_ref: route.apiKeyRef || null,
          timeout_ms: route.timeoutMs,
        })
      );
      route.liveModels = resp.models || [];
      route.liveSource = resp.source || 'live';
      if (!route.model && route.liveModels.length > 0) {
        route.model = route.liveModels[0];
      }
      if (this.isOllama(route) && !route.pullModel && route.liveModels.length > 0) {
        route.pullModel = route.liveModels[0];
      }
      route.statusOk = true;
      route.statusMessage = `Loaded ${route.liveModels.length} models (${resp.source}).`;
    } catch (err: any) {
      route.statusOk = false;
      route.statusMessage = this.apiErrorMessage(err, 'Failed to load models');
    } finally {
      route.loadingModels = false;
    }
  }

  async verifyRoute(route: EditableAdvisorRoute): Promise<void> {
    route.statusMessage = null;
    route.verifying = true;
    try {
      const resp = await firstValueFrom(
        this.api.verifyAdvisorRoute({
          provider_id: route.providerId,
          model: route.model || null,
          base_url: route.baseUrl || null,
          api_key: this.requiresApiKey(route) ? (route.apiKeyInput || null) : null,
          api_key_ref: route.apiKeyRef || null,
          timeout_ms: route.timeoutMs,
        })
      );
      route.statusOk = !!resp?.ok;
      route.statusMessage = resp?.ok
        ? `Verified in ${resp?.latency_ms ?? 0}ms`
        : (resp?.message || 'Route verification failed');
      this.recordRouteProbe({
        ts: new Date().toISOString(),
        route_id: route.id,
        route_label: this.routeLabel(route),
        provider_id: route.providerId,
        model: route.model || null,
        ok: Boolean(resp?.ok),
        latency_ms: Number(resp?.latency_ms || 0),
        code: String(resp?.code || (resp?.ok ? 'ok' : 'verify_failed')),
        message: String(resp?.message || ''),
        source: 'route_verify',
        circuit_state: String(resp?.circuit_state || 'unknown'),
      });
    } catch (err: any) {
      route.statusOk = false;
      route.statusMessage = this.apiErrorMessage(err, 'Route verification failed');
      this.recordRouteProbe({
        ts: new Date().toISOString(),
        route_id: route.id,
        route_label: this.routeLabel(route),
        provider_id: route.providerId,
        model: route.model || null,
        ok: false,
        latency_ms: Number(err?.error?.latency_ms || 0),
        code: String(err?.error?.code || err?.status || 'verify_error'),
        message: String(route.statusMessage || 'Route verification failed'),
        source: 'route_verify',
        circuit_state: String(err?.error?.circuit_state || 'unknown'),
      });
    } finally {
      route.verifying = false;
    }
  }

  async pullOllamaModel(route: EditableAdvisorRoute): Promise<void> {
    if (!this.isOllama(route)) return;
    const modelToPull = (route.pullModel || route.model || '').trim();
    if (!modelToPull) {
      route.statusOk = false;
      route.statusMessage = 'Choose or enter a model before pull.';
      return;
    }
    route.pullError = null;
    try {
      const resp = await firstValueFrom(
        this.api.startOllamaPull({ model: modelToPull, base_url: route.baseUrl || null })
      );
      if (!route.model) {
        route.model = modelToPull;
      }
      route.pullJobId = resp.job_id;
      route.pullState = 'queued';
      route.pullProgress = 0;
      route.statusOk = true;
      route.statusMessage = resp.message || 'Ollama pull started';
      this.pollOllamaPull(route);
    } catch (err: any) {
      route.statusOk = false;
      route.statusMessage = err?.error?.detail || err?.message || 'Failed to start Ollama pull';
    }
  }

  private pollOllamaPull(route: EditableAdvisorRoute): void {
    this.clearRoutePoller(route);
    if (!route.pullJobId) return;
    route.pullPollTimer = window.setInterval(async () => {
      if (!route.pullJobId) {
        this.clearRoutePoller(route);
        return;
      }
      try {
        const status = await firstValueFrom(this.api.getOllamaPullStatus(route.pullJobId));
        route.pullState = status.state;
        route.pullProgress = Number(status.progress || 0);
        route.pullError = status.error || null;
        if (status.state === 'completed' || status.state === 'failed') {
          this.clearRoutePoller(route);
          route.statusOk = status.state === 'completed';
          route.statusMessage = status.state === 'completed' ? 'Ollama pull completed.' : (status.error || 'Ollama pull failed.');
        }
      } catch (err: any) {
        this.clearRoutePoller(route);
        route.pullError = err?.error?.detail || err?.message || 'Failed to read pull status';
        route.statusOk = false;
        route.statusMessage = route.pullError;
      }
    }, 1200);
  }

  private clearRoutePoller(route: EditableAdvisorRoute): void {
    if (route.pullPollTimer) {
      window.clearInterval(route.pullPollTimer);
      route.pullPollTimer = null;
    }
  }

  private clearAllRoutePollers(): void {
    this.clearRoutePoller(this.primaryRoute);
    for (const route of this.fallbackRoutes) {
      this.clearRoutePoller(route);
    }
  }

  private buildRoutePayload(route: EditableAdvisorRoute, priority: number): {
    provider_id: string;
    model: string | null;
    api_key_ref: string | null;
    base_url: string | null;
    api_version: string | null;
    region_hint: string | null;
    priority: number;
  } {
    return {
      provider_id: route.providerId,
      model: route.model || null,
      api_key_ref: route.apiKeyRef || null,
      base_url: route.baseUrl || null,
      api_version: null,
      region_hint: null,
      priority,
    };
  }

  async saveAdvisorConfig(): Promise<void> {
    this.advisorError = null;
    this.advisorRuntimeMessage = null;
    this.normalizeRoute(this.primaryRoute);
    this.fallbackRoutes.forEach((route) => this.normalizeRoute(route));

    // Validate: warn about web routes missing API keys (no input AND no ref)
    const noKeyRoutes: string[] = [];
    const noInputRoutes: string[] = [];
    const checkRoute = (route: EditableAdvisorRoute, label: string) => {
      if (!this.requiresApiKey(route)) return;
      const hasInput = !!(route.apiKeyInput || '').trim();
      const hasRef = !!(route.apiKeyRef || '').trim();
      if (!hasInput && !hasRef) {
        noKeyRoutes.push(label);
      } else if (!hasInput && hasRef) {
        noInputRoutes.push(label);
      }
    };
    checkRoute(this.primaryRoute, `Route #1 (${this.primaryRoute.providerId})`);
    this.fallbackRoutes.forEach((route, idx) => {
      checkRoute(route, `Route #${idx + 2} (${route.providerId})`);
    });
    const allRoutes = [this.primaryRoute, ...this.fallbackRoutes];
    const routes = allRoutes.map((route, index) => this.buildRoutePayload(route, index));
    const primaryRef = (this.primaryRoute.apiKeyRef || '').trim() || this.defaultApiKeyRef(this.primaryRoute.providerId);
    const baseConfigPayload = {
      advisor_primary_provider: this.primaryRoute.providerId,
      advisor_primary_model: this.primaryRoute.model || null,
      advisor_fallback_chain: this.fallbackRoutes.map((route) => route.providerId),
      profile_id: this.activeProfileId,
      routes,
      advisor_custom_base_url: this.primaryRoute.providerId === 'custom' ? (this.primaryRoute.baseUrl || null) : null,
      advisor_custom_model: this.primaryRoute.providerId === 'custom' ? (this.primaryRoute.model || null) : null,
      advisor_custom_api_key_ref: primaryRef,
      advisor_provider_timeout_ms: this.primaryRoute.timeoutMs,
      advisor_provider_retry_max: 2,
    };

    // Main config save (with primary API key)
    try {
      const saved = await firstValueFrom(
        this.api.updateAdvisorConfig({
          ...baseConfigPayload,
          api_key: this.requiresApiKey(this.primaryRoute) ? (this.primaryRoute.apiKeyInput || null) : null,
        })
      );
      for (const route of this.fallbackRoutes) {
        const keyValue = (route.apiKeyInput || '').trim();
        if (!keyValue || !this.requiresApiKey(route)) continue;
        const keyRef = (route.apiKeyRef || '').trim() || this.defaultApiKeyRef(route.providerId);
        if (keyRef === primaryRef && (this.primaryRoute.apiKeyInput || '').trim()) {
          route.keyStatus = 'stored';
          continue;
        }
        try {
          await firstValueFrom(
            this.api.updateAdvisorConfig({
              ...baseConfigPayload,
              advisor_custom_api_key_ref: keyRef,
              api_key: keyValue,
            })
          );
          route.keyStatus = 'stored';
        } catch (err: any) {
          this.advisorError = `Failed to store API key for ${route.providerId}: ${err?.error?.detail || err?.message}`;
          return;
        }
      }
      // Re-apply base config so key-only writes cannot drift route/profile state.
      await firstValueFrom(
        this.api.updateAdvisorConfig({
          ...baseConfigPayload,
          api_key: null,
        })
      );
      const roleUserId = (this.additionalExecutorUserId || '').trim() || 'claude-executor';
      const roleConsumerId = (this.additionalExecutorConsumerId || '').trim() || roleUserId;
      let roleSyncWarning: string | null = null;
      try {
        await firstValueFrom(
          this.api.updateExecutorConfig({
            workspace_id: this.executorWorkspaceId || null,
            executor_user_id: this.executorUserId || null,
            executor_consumer_id: this.executorConsumerId || null,
            secondary_executor_user_id: roleUserId,
            secondary_executor_consumer_id: roleConsumerId,
            executor_clients: this.parseExecutorClients(),
          })
        );
        this.additionalExecutorUserId = roleUserId;
        this.additionalExecutorConsumerId = roleConsumerId;
      } catch (err: any) {
        roleSyncWarning = `identity sync warning: ${err?.error?.detail || err?.message || 'additional executor id not synced'}`;
      }
      // Mark keys as stored after successful save
      if (this.requiresApiKey(this.primaryRoute) && (this.primaryRoute.apiKeyInput || '').trim()) {
        this.primaryRoute.keyStatus = 'stored';
      }
      this.primaryRoute.apiKeyInput = '';
      this.fallbackRoutes.forEach((route) => {
        if (this.requiresApiKey(route) && (route.apiKeyInput || '').trim()) {
          route.keyStatus = 'stored';
        }
        route.apiKeyInput = '';
      });
      this.advisorRuntimeMessage = `Saved advisor config (profile: ${saved.active_profile_id || 'default'}).`;
      if (noKeyRoutes.length > 0 || noInputRoutes.length > 0) {
        const notes: string[] = [];
        if (noKeyRoutes.length > 0) {
          notes.push(`missing key/ref: ${noKeyRoutes.join(', ')}`);
        }
        if (noInputRoutes.length > 0) {
          notes.push(`using stored key ref: ${noInputRoutes.join(', ')}`);
        }
        this.advisorRuntimeMessage += ` Route warning (${notes.join('; ')}).`;
      }
      if (roleSyncWarning) {
        this.advisorRuntimeMessage += ` ${roleSyncWarning}`;
      }
      this.advisorRestartVisible = true;
    } catch (err: any) {
      this.advisorError = err?.error?.detail || err?.message || 'Failed to save advisor config';
    }
  }

  async loadAdvisorRuntimeStatus(): Promise<void> {
    this.advisorError = null;
    this.advisorRuntimeMessage = null;
    try {
      const resp: any = await firstValueFrom(this.api.getAdvisorRuntimeStatus());
      const routeCount = Array.isArray(resp?.routes) ? resp.routes.length : 0;
      this.advisorRuntimeMessage = `Active profile ${resp?.active_profile_id || 'default'} (${routeCount} routes)`;
    } catch (err: any) {
      this.advisorError = this.apiErrorMessage(err, 'Failed to load advisor runtime status');
    }
  }

  async probeAdvisorRuntime(): Promise<void> {
    this.advisorError = null;
    this.advisorRuntimeMessage = null;
    try {
      const resp: any = await firstValueFrom(this.api.probeAdvisorRuntime({ max_probes: 5 }));
      const attempts = Array.isArray(resp?.attempts) ? resp.attempts : [];
      const okCount = attempts.filter((item: any) => !!item?.ok).length;
      for (const attempt of attempts) {
        const providerId = String(attempt?.provider_id || '');
        const model = attempt?.model ? String(attempt.model) : null;
        const matchedRoute = this.findRouteForProbe(providerId, model);
        this.recordRouteProbe({
          ts: new Date().toISOString(),
          route_id: matchedRoute?.id || `${providerId}:${model || '-'}`,
          route_label: matchedRoute ? this.routeLabel(matchedRoute) : `${providerId}${model ? `:${model}` : ''}`,
          provider_id: providerId || 'unknown',
          model,
          ok: Boolean(attempt?.ok),
          latency_ms: Number(attempt?.latency_ms || 0),
          code: String(attempt?.code || (attempt?.ok ? 'ok' : 'probe_failed')),
          message: String(attempt?.message || ''),
          source: 'runtime_probe',
          circuit_state: String(attempt?.circuit_state || 'unknown'),
        });
      }
      this.advisorRuntimeMessage = `Probe ${resp?.ok ? 'ok' : 'partial'} (${okCount}/${attempts.length} routes healthy)`;
    } catch (err: any) {
      this.advisorError = this.apiErrorMessage(err, 'Advisor runtime probe failed');
    }
  }

  private parseExecutorClients(): string[] {
    return (this.executorClientsText || '')
      .split(',')
      .map((item) => item.trim().toLowerCase())
      .filter((item) => item.length > 0);
  }

  get executorIdentityRows(): { client: string; userId: string; consumerId: string; source: 'custom' | 'derived' }[] {
    const clients = this.parseExecutorClients();
    if (clients.length === 0) {
      return [];
    }
    return clients.map((client, idx) => {
      const fallbackIdentity = `${client}-executor`;
      if (idx === 0) {
        const userId = (this.executorUserId || '').trim() || fallbackIdentity;
        const consumerId = (this.executorConsumerId || '').trim() || userId;
        return { client, userId, consumerId, source: userId === fallbackIdentity ? 'derived' : 'custom' };
      }
      if (idx === 1) {
        const userId = (this.additionalExecutorUserId || '').trim() || fallbackIdentity;
        const consumerId = (this.additionalExecutorConsumerId || '').trim() || userId;
        return { client, userId, consumerId, source: userId === fallbackIdentity ? 'derived' : 'custom' };
      }
      return {
        client,
        userId: fallbackIdentity,
        consumerId: fallbackIdentity,
        source: 'derived',
      };
    });
  }

  async saveExecutorConfig(): Promise<void> {
    this.executorConfigError = null;
    this.executorConfigMessage = null;
    try {
      await firstValueFrom(
        this.api.updateExecutorConfig({
          workspace_id: this.executorWorkspaceId || null,
          executor_user_id: this.executorUserId || null,
          executor_consumer_id: this.executorConsumerId || null,
          secondary_executor_user_id: this.additionalExecutorUserId || null,
          secondary_executor_consumer_id: this.additionalExecutorConsumerId || null,
          executor_clients: this.parseExecutorClients(),
        })
      );
      const nextWorkspace = (this.executorWorkspaceId || '').trim();
      if (nextWorkspace) {
        this.auth.update({ workspace: nextWorkspace });
        this.settings = this.auth.get();
      }
      this.executorConfigMessage = nextWorkspace
        ? `Saved executor config to .env. Dashboard workspace switched to '${nextWorkspace}'.`
        : 'Saved executor config to .env.';
      this.executorRestartVisible = true;
    } catch (err: any) {
      this.executorConfigError = err?.error?.detail || err?.message || 'Failed to save executor config';
    }
  }

  restartFromAdvisor(): void {
    this.advisorRestartVisible = false;
    this.restartNow();
  }

  restartFromExecutor(): void {
    this.executorRestartVisible = false;
    this.restartNow();
  }

  async saveAndRestartAdvisor(): Promise<void> {
    await this.saveAdvisorConfig();
    if (!this.advisorError) {
      this.advisorRestartVisible = false;
      this.restartNow();
    }
  }

  async saveAndRestartExecutor(): Promise<void> {
    await this.saveExecutorConfig();
    if (!this.executorConfigError) {
      this.executorRestartVisible = false;
      this.restartNow();
    }
  }

  async checkAllRoutes(): Promise<void> {
    this.advisorRuntimeMessage = 'Checking all routes...';
    const allRoutes = [this.primaryRoute, ...this.fallbackRoutes];
    let okCount = 0;
    for (const route of allRoutes) {
      await this.verifyRoute(route);
      if (route.statusOk) okCount++;
    }
    this.advisorRuntimeMessage = `${okCount}/${allRoutes.length} routes healthy.`;
  }

  /** Silently check key status for all web routes on page load. */
  private autoCheckKeyStatus(): void {
    const allRoutes = [this.primaryRoute, ...this.fallbackRoutes];
    for (const route of allRoutes) {
      if (!this.requiresApiKey(route)) {
        route.keyStatus = null; // local routes don't need keys
        continue;
      }
      if (!(route.apiKeyRef || '').trim()) {
        route.keyStatus = 'missing';
        continue;
      }
      // Has a key ref — verify if key is actually stored by probing the route
      route.keyStatus = 'checking';
      this.verifyRoute(route).then(() => {
        if (route.statusOk === true) {
          route.keyStatus = 'stored';
        } else {
          // Key presence and key validity are different.
          // Keep "stored" for auth failures (invalid/expired key) and only mark
          // "missing" when the backend indicates no key/ref was provided.
          const msg = (route.statusMessage || '').toLowerCase();
          const explicitMissing =
            msg.includes('api key required') ||
            msg.includes('missing key') ||
            msg.includes('missing key/ref') ||
            msg.includes('set api key');
          if (explicitMissing) {
            route.keyStatus = 'missing';
          } else {
            // Network/auth/rate-limit/model errors do not prove key absence.
            route.keyStatus = 'stored';
          }
        }
        // Clear the verify status so it doesn't clutter the UI on load
        route.statusMessage = null;
        route.statusOk = null;
        route.verifying = false;
      });
    }
  }

  async restartNow(): Promise<void> {
    this.restartMessage = null;
    this.restartRunning = true;
    try {
      const resp = await firstValueFrom(this.api.restartStack({ stack: this.restartTarget }));
      this.restartJobId = resp.restart_id;
      this.restartMessage = resp.message || 'Restart accepted.';
      this.pollRestartStatus();
    } catch (err: any) {
      this.restartRunning = false;
      this.restartMessage = err?.error?.detail || err?.message || 'Restart request failed';
    }
  }

  private pollRestartStatus(): void {
    this.clearRestartPoller();
    if (!this.restartJobId) {
      this.restartRunning = false;
      return;
    }
    const pollStartedAt = Date.now();
    const pollTimeoutMs = 240000;
    this.restartPollTimer = window.setInterval(async () => {
      if (!this.restartJobId) {
        this.clearRestartPoller();
        this.restartRunning = false;
        return;
      }
      try {
        const status: DashboardStackRestartStatusResponse = await firstValueFrom(
          this.api.getRestartStatus(this.restartJobId)
        );
        if (status.state === 'completed') {
          this.restartRunning = false;
          this.restartMessage = 'Restart completed.';
          this.clearRestartPoller();
          this.advisorRestartVisible = false;
          this.executorRestartVisible = false;
          this.reloadAll();
          return;
        }
        if (status.state === 'failed') {
          this.restartRunning = false;
          this.restartMessage = status.error || 'Restart failed.';
          this.clearRestartPoller();
          return;
        }
        this.restartRunning = true;
        this.restartMessage = `Restart ${status.state}...`;
      } catch (err: any) {
        const elapsed = Date.now() - pollStartedAt;
        if (elapsed < pollTimeoutMs) {
          this.restartRunning = true;
          this.restartMessage = 'Restart in progress (waiting for API to reconnect)...';
          return;
        }
        this.restartRunning = false;
        this.restartMessage = err?.error?.detail || err?.message || 'Failed to poll restart status';
        this.clearRestartPoller();
      }
    }, 1500);
  }

  private clearRestartPoller(): void {
    if (this.restartPollTimer) {
      window.clearInterval(this.restartPollTimer);
      this.restartPollTimer = null;
    }
  }

  applyServerDefaults(): void {
    if (!this.serverConfig) return;
    this.auth.applyServerDefaults(this.serverConfig);
    this.settings = this.auth.get();
  }

  save(): void {
    this.localOverrideMessage = null;
    this.auth.update({
      token: (this.settings.token || '').trim(),
      consumer: (this.settings.consumer || '').trim(),
      role: (this.settings.role || '').trim(),
      workspace: (this.settings.workspace || '').trim(),
      user: (this.settings.user || '').trim(),
    });
    this.settings = this.auth.get();
    this.localOverrideMessage = 'Local overrides saved.';
  }

  resetLocal(): void {
    this.localOverrideMessage = null;
    this.auth.reset();
    if (this.serverConfig) {
      this.auth.applyServerDefaults(this.serverConfig);
    }
    this.settings = this.auth.get();
    this.localOverrideMessage = 'Reset to defaults.';
  }

  testConnection(): void {
    this.connectionState = 'idle';
    this.connectionMessage = null;
    this.api.getHealth().subscribe({
      next: (health) => {
        this.connectionState = health?.status === 'ok' ? 'ok' : 'error';
        this.connectionMessage = health?.status === 'ok'
          ? 'Health check passed.'
          : `Health check returned: ${health?.status ?? 'unknown'}`;
      },
      error: (err) => {
        this.connectionState = 'error';
        this.connectionMessage = err?.error?.detail || err?.message || 'Health check failed';
      },
    });
  }

  async changeRuntimeMode(): Promise<void> {
    this.runtimeModeSaving = true;
    this.runtimeModeMessage = null;
    try {
      const resp = await firstValueFrom(this.api.setRuntimeMode(this.runtimeModeSelection));
      if (this.serverConfig) {
        this.serverConfig = {
          ...this.serverConfig,
          runtime_mode: { mode: resp.mode, clone_enabled: resp.clone_enabled },
        };
      }
      this.runtimeModeMessage = `Mode set to ${resp.mode}. Clone enabled: ${resp.clone_enabled}.`;
    } catch (err: any) {
      this.runtimeModeMessage = err?.error?.detail || err?.message || 'Failed to set runtime mode';
    } finally {
      this.runtimeModeSaving = false;
    }
  }
}
