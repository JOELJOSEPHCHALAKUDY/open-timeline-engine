import { Component, OnInit } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ApiService } from './core/services/api.service';
import { AuthService } from './core/services/auth.service';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <div class="app-shell">
      <nav class="sidebar">
        <div class="sidebar-brand">
          <div class="logo">T</div>
          <span>Engine Dashboard</span>
        </div>

        <div class="sidebar-section">Main</div>
        <a class="sidebar-link" routerLink="/" routerLinkActive="active" [routerLinkActiveOptions]="{exact: true}">
          <span class="icon">📊</span> Overview
        </a>
        <a class="sidebar-link" routerLink="/persona" routerLinkActive="active">
          <span class="icon">🧬</span> Persona
        </a>
        <a class="sidebar-link" routerLink="/timeline" routerLinkActive="active">
          <span class="icon">📅</span> Timeline
        </a>
        <a class="sidebar-link" routerLink="/observations" routerLinkActive="active">
          <span class="icon">🔍</span> Observations
        </a>

        <div class="sidebar-section">System</div>
        <a class="sidebar-link" routerLink="/takeover" routerLinkActive="active">
          <span class="icon">🎮</span> Takeover
        </a>
        <a class="sidebar-link" routerLink="/goal-intelligence" routerLinkActive="active">
          <span class="icon">🧠</span> Goal Intelligence
        </a>
        <a class="sidebar-link" routerLink="/human-clone" routerLinkActive="active">
          <span class="icon">🫀</span> Human Clone
        </a>
        <a class="sidebar-link" routerLink="/episodes" routerLinkActive="active">
          <span class="icon">🧾</span> Episodes
        </a>
        <a class="sidebar-link" routerLink="/workflow-templates" routerLinkActive="active">
          <span class="icon">🗂️</span> Workflow Templates
        </a>
        <a class="sidebar-link" routerLink="/memory-rules" routerLinkActive="active">
          <span class="icon">🛡️</span> Memory Rules
        </a>
        <a class="sidebar-link" routerLink="/retrieval-context" routerLinkActive="active">
          <span class="icon">🧪</span> Retrieval & Context
        </a>
        <a class="sidebar-link" routerLink="/graph" routerLinkActive="active">
          <span class="icon">🕸️</span> Graph
        </a>
        <a class="sidebar-link" routerLink="/patterns" routerLinkActive="active">
          <span class="icon">🧩</span> Patterns
        </a>
        <a class="sidebar-link" routerLink="/health" routerLinkActive="active">
          <span class="icon">💚</span> Health
        </a>
        <a class="sidebar-link" routerLink="/settings" routerLinkActive="active">
          <span class="icon">⚙️</span> Settings
        </a>
        <a class="sidebar-link" routerLink="/export-center" routerLinkActive="active">
          <span class="icon">📤</span> Super Export
        </a>
      </nav>

      <main class="main-content">
        <router-outlet />
      </main>
    </div>
  `,
  styles: []
})
export class AppComponent implements OnInit {
  constructor(
    private api: ApiService,
    private auth: AuthService,
  ) {}

  ngOnInit(): void {
    firstValueFrom(this.api.getDashboardClientConfig())
      .then((config) => {
        this.auth.applyServerDefaults(config);
        return firstValueFrom(this.api.warmDashboardStartup('today')).catch(() => undefined);
      })
      .catch(() => undefined);
  }
}
