# Settings Page Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rewrite the settings page template and fix data loading so users can configure advisor LLM providers and executor identities with per-section Apply + Restart flows.

**Architecture:** Single-file rewrite of `settings.component.ts` — replace the 7-card inline template with 3 sections (System / Advisor / Executor+Identity), add per-section restart banners, remove sessionStorage caching that causes stale data, improve all labels. No new files, no backend changes.

**Tech Stack:** Angular 17+ standalone component, FormsModule, inline template, existing ApiService/AuthService.

**File:** `dashboard/src/app/features/settings/settings.component.ts` (1,391 lines — inline template lines 71-431, component logic lines 433-1391)

---

### Task 1: Replace the template — Section 1 (System)

**Files:**
- Modify: `dashboard/src/app/features/settings/settings.component.ts:71-431` (template)

**Step 1: Replace the entire inline template**

Replace lines 71-431 (everything inside `template: \`...\``) with the new 3-section template. This task covers Section 1 (System). The template starts with:

```html
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
        <div>{{ (serverConfig?.executor_clients || []).join(', ') || 'codex, claude' }}</div>
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
```

**Step 2: Verify template compiles**

Run: `cd dashboard && npx ng build 2>&1 | tail -5`
Expected: Build succeeds (warnings OK, no errors)

**Step 3: Commit**

```bash
git add dashboard/src/app/features/settings/settings.component.ts
git commit -m "feat(settings): rewrite Section 1 (System) template"
```

---

### Task 2: Replace the template — Section 2 (Advisor Configuration)

**Files:**
- Modify: `dashboard/src/app/features/settings/settings.component.ts` (template, continued)

**Step 1: Add Section 2 to the template**

Immediately after the System card closing `</div>`, add the Advisor section. This uses a unified numbered list for all routes (primary = #1, fallbacks = #2+):

```html
  <!-- ====== SECTION 2: ADVISOR CONFIGURATION ====== -->
  <div class="card" style="margin-bottom: 24px;">
    <div class="card-header">
      <span class="card-title">Advisor configuration</span>
      <button class="btn" (click)="addFallbackRoute()">+ Add route</button>
    </div>

    <div *ngIf="advisorError" class="error-banner" style="margin-top:12px;">{{ advisorError }}</div>

    <div *ngIf="advisorRestartVisible" style="
      margin: 12px 0; padding: 10px 14px;
      background: rgba(251,191,36,0.12);
      border: 1px solid rgba(251,191,36,0.3);
      border-radius: 8px;
      display:flex; align-items:center; gap:12px; flex-wrap:wrap;
    ">
      <span style="font-size:13px;color:var(--text);">Config saved. Restart to apply?</span>
      <button class="btn btn-primary" (click)="restartFromAdvisor()">Restart</button>
      <button class="btn" (click)="advisorRestartVisible=false">Later</button>
    </div>

    <!-- Route #1 (primary) -->
    <ng-container [ngTemplateOutlet]="routeTemplate"
      [ngTemplateOutletContext]="{ $implicit: primaryRoute, index: 0, isFirst: true, isLast: fallbackRoutes.length === 0 }">
    </ng-container>

    <!-- Fallback routes -->
    <ng-container *ngFor="let route of fallbackRoutes; let i = index">
      <ng-container [ngTemplateOutlet]="routeTemplate"
        [ngTemplateOutletContext]="{ $implicit: route, index: i + 1, isFirst: false, isLast: i === fallbackRoutes.length - 1 }">
      </ng-container>
    </ng-container>

    <div style="display:flex; gap:10px; margin-top:14px; align-items:center; flex-wrap:wrap;">
      <button class="btn btn-primary" (click)="saveAdvisorConfig()">Apply</button>
      <button class="btn" (click)="checkAllRoutes()">Check all routes</button>
      <span style="font-size:13px;color:var(--muted);" *ngIf="advisorRuntimeMessage">{{ advisorRuntimeMessage }}</span>
    </div>
  </div>

  <!-- Route template (shared for primary + fallback) -->
  <ng-template #routeTemplate let-route let-index="index" let-isFirst="isFirst" let-isLast="isLast">
    <div style="margin-top:12px; padding:12px 14px; border:1px solid rgba(255,255,255,0.08); border-radius:8px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <span style="font-size:14px; font-weight:600; color:var(--text);">
          Route #{{ index + 1 }}
          <span *ngIf="index === 0" style="font-size:11px; color:var(--muted); font-weight:400;"> (primary)</span>
        </span>
        <div style="display:flex; gap:6px;" *ngIf="index > 0">
          <button class="btn" style="padding:4px 8px;font-size:12px;" (click)="moveFallbackRoute(index - 1, -1)" [disabled]="index <= 1">Up</button>
          <button class="btn" style="padding:4px 8px;font-size:12px;" (click)="moveFallbackRoute(index - 1, 1)" [disabled]="isLast">Down</button>
          <button class="btn" style="padding:4px 8px;font-size:12px;" (click)="removeFallbackRoute(index - 1)">Remove</button>
        </div>
      </div>

      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Source</label>
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
          <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Endpoint URL</label>
          <input class="input" [(ngModel)]="route.baseUrl" placeholder="Provider endpoint" />
        </div>

        <div *ngIf="requiresApiKey(route)">
          <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">API key</label>
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
          {{ route.loadingModels ? 'Loading...' : 'Load models' }}
        </button>
        <button class="btn" style="font-size:13px;" (click)="verifyRoute(route)" [disabled]="route.verifying">
          {{ route.verifying ? 'Verifying...' : 'Verify' }}
        </button>
        <button class="btn" style="font-size:13px;" *ngIf="isOllama(route)" (click)="pullOllamaModel(route)">Pull to Ollama</button>
        <span *ngIf="route.statusMessage" style="font-size:12px;" [style.color]="route.statusOk === true ? '#4ade80' : route.statusOk === false ? '#f87171' : 'var(--muted)'">
          {{ route.statusMessage }}
        </span>
      </div>

      <div *ngIf="isOllama(route) && route.pullJobId" style="margin-top:8px;font-size:12px;color:var(--muted);">
        Ollama pull {{ route.pullState }} ({{ route.pullProgress | number:'1.0-0' }}%)
        <span *ngIf="route.pullError" style="color:#f87171;"> - {{ route.pullError }}</span>
      </div>
    </div>
  </ng-template>
```

**Step 2: Verify build**

Run: `cd dashboard && npx ng build 2>&1 | tail -5`

**Step 3: Commit**

```bash
git add dashboard/src/app/features/settings/settings.component.ts
git commit -m "feat(settings): rewrite Section 2 (Advisor) template with numbered routes"
```

---

### Task 3: Replace the template — Section 3 (Executor & Identity)

**Files:**
- Modify: `dashboard/src/app/features/settings/settings.component.ts` (template, continued)

**Step 1: Add Section 3 to the template**

After the Advisor card closing `</div>`, add the Executor & Identity section:

```html
  <!-- ====== SECTION 3: EXECUTOR & IDENTITY ====== -->
  <div class="card" style="margin-bottom: 24px;">
    <div class="card-header">
      <span class="card-title">Executor & Identity</span>
    </div>

    <div *ngIf="executorConfigError" class="error-banner" style="margin-top:12px;">{{ executorConfigError }}</div>

    <div *ngIf="executorRestartVisible" style="
      margin: 12px 0; padding: 10px 14px;
      background: rgba(251,191,36,0.12);
      border: 1px solid rgba(251,191,36,0.3);
      border-radius: 8px;
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
        <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Advisor user</label>
        <input class="input" [(ngModel)]="advisorUserId" placeholder="claude-advisor" />
      </div>
      <div>
        <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:6px;">Advisor consumer</label>
        <input class="input" [(ngModel)]="advisorConsumerId" placeholder="claude-advisor" />
      </div>
    </div>

    <div style="display:flex; gap:10px; margin-top:14px; align-items:center; flex-wrap:wrap;">
      <button class="btn btn-primary" (click)="saveExecutorConfig()">Apply</button>
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
```

**Step 2: Verify build**

Run: `cd dashboard && npx ng build 2>&1 | tail -5`

**Step 3: Commit**

```bash
git add dashboard/src/app/features/settings/settings.component.ts
git commit -m "feat(settings): rewrite Section 3 (Executor & Identity) template"
```

---

### Task 4: Add per-section restart properties and methods

**Files:**
- Modify: `dashboard/src/app/features/settings/settings.component.ts` (component class)

**Step 1: Add new properties**

Add after the existing `runtimeModeMessage` property (~line 464):

```typescript
advisorRestartVisible = false;
executorRestartVisible = false;
```

**Step 2: Add section-specific restart methods**

Add these methods to the component (near the existing `restartNow` method):

```typescript
restartFromAdvisor(): void {
  this.advisorRestartVisible = false;
  this.restartNow();
}

restartFromExecutor(): void {
  this.executorRestartVisible = false;
  this.restartNow();
}
```

**Step 3: Add `checkAllRoutes` method**

This replaces the separate "Runtime status" and "Probe routes" buttons:

```typescript
async checkAllRoutes(): Promise<void> {
  this.advisorError = null;
  this.advisorRuntimeMessage = 'Checking routes...';
  try {
    const resp: any = await firstValueFrom(this.api.probeAdvisorRuntime({ max_probes: 10 }));
    const attempts = Array.isArray(resp?.attempts) ? resp.attempts : [];
    const okCount = attempts.filter((a: any) => !!a?.ok).length;
    this.advisorRuntimeMessage = `${okCount}/${attempts.length} routes healthy` + (resp?.ok ? '' : ' (some failed)');
  } catch (err: any) {
    this.advisorError = err?.error?.detail || err?.message || 'Route check failed';
    this.advisorRuntimeMessage = null;
  }
}
```

**Step 4: Update `saveAdvisorConfig` to show per-section restart banner**

In `saveAdvisorConfig()`, change the success handler from `this.promptRestartAfterSave()` to `this.advisorRestartVisible = true;`:

Find:
```typescript
this.advisorRuntimeMessage = `Saved advisor config (profile: ${saved.active_profile_id || 'default'}). Restart required.`;
this.promptRestartAfterSave();
```

Replace with:
```typescript
this.advisorRuntimeMessage = `Saved (profile: ${saved.active_profile_id || 'default'}).`;
this.advisorRestartVisible = true;
```

**Step 5: Update `saveExecutorConfig` to show per-section restart banner**

In `saveExecutorConfig()`, change the success handler:

Find:
```typescript
this.executorConfigMessage = 'Saved executor config to .env. Restart required.';
this.promptRestartAfterSave();
```

Replace with:
```typescript
this.executorConfigMessage = 'Saved.';
this.executorRestartVisible = true;
```

**Step 6: Verify build**

Run: `cd dashboard && npx ng build 2>&1 | tail -5`

**Step 7: Commit**

```bash
git add dashboard/src/app/features/settings/settings.component.ts
git commit -m "feat(settings): add per-section restart banners and checkAllRoutes"
```

---

### Task 5: Fix stale data — remove sessionStorage caching

**Files:**
- Modify: `dashboard/src/app/features/settings/settings.component.ts`

**Step 1: Simplify `reloadConfigInternal`**

Remove the `loadPendingExecutorConfig()` branch. Always load from server:

Replace the full `reloadConfigInternal` method body with:

```typescript
private async reloadConfigInternal(): Promise<void> {
  const config = await firstValueFrom(this.api.getDashboardClientConfig());
  this.serverConfig = config;
  this.runtimeModeSelection = config.runtime_mode?.mode || 'timeline_only';

  this.executorWorkspaceId = config.default_workspace_id || 'personal';
  this.executorUserId = config.known_identities?.executor?.user_id || 'codex-executor';
  this.advisorUserId = config.known_identities?.advisor?.user_id || 'claude-advisor';
  this.executorConsumerId = config.default_consumer_id || 'codex-executor';
  this.advisorConsumerId = this.advisorUserId;
  if (Array.isArray(config.executor_clients) && config.executor_clients.length > 0) {
    this.executorClientsText = config.executor_clients.join(',');
  }
}
```

**Step 2: Simplify `reloadAdvisorProvidersInternal`**

Remove the `applyPendingAdvisorRoutes()` calls. The method should load routes from server config only:

In the method, remove both lines that call `this.applyPendingAdvisorRoutes()`.

**Step 3: Remove dead sessionStorage methods**

Delete these methods entirely:
- `savePendingExecutorConfig()`
- `loadPendingExecutorConfig()`
- `clearPendingExecutorConfig()`
- `savePendingAdvisorRoutes()`
- `loadPendingAdvisorRoutes()`
- `clearPendingAdvisorRoutes()`
- `applyPendingAdvisorRoutes()`

Also remove calls to these in `saveAdvisorConfig()` (`this.savePendingAdvisorRoutes()`), `saveExecutorConfig()` (`this.savePendingExecutorConfig()`), and `pollRestartStatus()` success handler (`this.clearPendingExecutorConfig(); this.clearPendingAdvisorRoutes()`).

**Step 4: Remove the old global restart prompt**

Delete the `restartPromptVisible` property and its related methods:
- `restartPromptVisible = false`
- `promptRestartAfterSave()`
- `dismissRestartPrompt()`
- `acceptRestartPrompt()`

**Step 5: Verify build**

Run: `cd dashboard && npx ng build 2>&1 | tail -5`

**Step 6: Commit**

```bash
git add dashboard/src/app/features/settings/settings.component.ts
git commit -m "fix(settings): remove sessionStorage caching, always load from server"
```

---

### Task 6: Final build + manual verification

**Step 1: Full build**

Run: `cd dashboard && npx ng build 2>&1`
Expected: Build succeeds with only pre-existing warnings from `takeover.component.ts`.

**Step 2: Start dev server**

Run: `cd dashboard && npx ng serve --proxy-config proxy.conf.json`
Expected: Dev server starts on port 4200.

**Step 3: Manual verification checklist**

Open `http://localhost:4200/settings` and verify:

1. Page loads with 3 sections: System, Advisor, Executor & Identity
2. **System section:**
   - Runtime mode dropdown shows current mode from server
   - Apply button changes runtime mode instantly
   - Test connection button works
   - Restart stack button works
3. **Advisor section:**
   - Route #1 shows as primary with correct provider/model from server
   - Can switch between Local/Web source
   - Can load models, verify route
   - Can add fallback routes (#2, #3...)
   - Apply button saves and shows yellow restart banner
   - Restart button in banner works
4. **Executor section:**
   - Fields show correct values from server (not stale sessionStorage)
   - Apply button saves and shows yellow restart banner
   - Dashboard auth section: Save auth works (local, no restart)
   - Reset to defaults works

**Step 4: Final commit**

```bash
git add dashboard/src/app/features/settings/settings.component.ts
git commit -m "feat(settings): complete settings page redesign with 3-section layout"
```
