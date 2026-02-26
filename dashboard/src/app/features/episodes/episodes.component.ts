import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { EpisodeItem } from '../../core/models';
import { ApiService } from '../../core/services/api.service';

@Component({
  selector: 'app-episodes',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div class="page-header">
      <h1>Episodes</h1>
      <p>Meaning-layer memory extracted from timeline events.</p>
    </div>

    <div class="card" style="margin-bottom: 16px;">
      <div style="display:grid;grid-template-columns:1fr auto auto auto;gap:8px;align-items:center;">
        <input class="input" [(ngModel)]="sessionId" placeholder="Session ID" />
        <select class="input" [(ngModel)]="statusFilter">
          <option value="">all statuses</option>
          <option value="open">open</option>
          <option value="in_progress">in_progress</option>
          <option value="blocked">blocked</option>
          <option value="done">done</option>
        </select>
        <button class="btn btn-primary" (click)="load()">Refresh</button>
        <a class="btn" routerLink="/memory-rules">Open Memory Rules</a>
      </div>
    </div>

    <div *ngIf="loading" class="loading-container">
      <div class="spinner"></div>
      <span>Loading episodes…</span>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>

    <div class="card" *ngIf="!loading">
      <div class="card-header">
        <span class="card-title">Episodes</span>
        <span class="badge badge-muted">{{ episodes.length }}</span>
      </div>
      <div style="overflow:auto;max-height:560px;">
        <table class="table">
          <thead>
            <tr>
              <th>Goal</th>
              <th>Status</th>
              <th>Authority</th>
              <th>Stability</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let episode of episodes">
              <td>
                <div style="font-weight:600;">{{ episode.goal }}</div>
                <div style="color:var(--muted);font-size:12px;">{{ episode.context || 'no context' }}</div>
              </td>
              <td>{{ episode.status }}</td>
              <td>{{ episode.authority_score | number:'1.2-2' }}</td>
              <td>{{ episode.stability_score | number:'1.2-2' }}</td>
              <td>{{ episode.updated_at | date:'short' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
})
export class EpisodesComponent implements OnInit {
  sessionId = 'default';
  statusFilter = '';
  loading = false;
  error: string | null = null;
  episodes: EpisodeItem[] = [];

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    void this.bootstrap();
  }

  private async bootstrap(): Promise<void> {
    await this.initializeSessionId();
    this.load();
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

  load(): void {
    this.loading = true;
    this.error = null;
    this.api.getEpisodes(this.sessionId, this.statusFilter || undefined, 200).subscribe({
      next: (resp) => {
        this.episodes = resp.episodes || [];
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load episodes';
        this.loading = false;
      },
    });
  }
}
