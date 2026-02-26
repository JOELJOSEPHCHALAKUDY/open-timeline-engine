import { CommonModule } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MemoryRuleItem } from '../../core/models';
import { ApiService } from '../../core/services/api.service';

@Component({
  selector: 'app-memory-rules',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <div class="page-header">
      <h1>Memory Rules</h1>
      <p>First-class negative memory and boundaries for context composition.</p>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <span class="card-title">Add rule</span>
        <a class="btn" routerLink="/episodes">Open Episodes</a>
      </div>
      <div style="display:grid;grid-template-columns:1fr 120px 120px auto;gap:8px;align-items:center;">
        <input class="input" [(ngModel)]="statement" placeholder="Rule statement" />
        <select class="input" [(ngModel)]="ruleType">
          <option value="avoid">avoid</option>
          <option value="always">always</option>
          <option value="prefer">prefer</option>
          <option value="security">security</option>
          <option value="style">style</option>
        </select>
        <select class="input" [(ngModel)]="priority">
          <option [ngValue]="0">P0</option>
          <option [ngValue]="1">P1</option>
          <option [ngValue]="2">P2</option>
          <option [ngValue]="3">P3</option>
        </select>
        <button class="btn btn-primary" (click)="createRule()" [disabled]="!statement.trim()">Create</button>
      </div>
    </div>

    <div *ngIf="error" class="error-banner">{{ error }}</div>
    <div *ngIf="notice" class="card" style="margin-bottom:12px;">{{ notice }}</div>

    <div class="card">
      <div class="card-header">
        <span class="card-title">Rules</span>
        <button class="btn" (click)="load()">Refresh</button>
      </div>
      <div style="overflow:auto;max-height:560px;">
        <table class="table">
          <thead>
            <tr>
              <th>Statement</th>
              <th>Type</th>
              <th>Priority</th>
              <th>Active</th>
              <th>Updated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let rule of rules">
              <td>{{ rule.statement }}</td>
              <td>{{ rule.rule_type }}</td>
              <td>P{{ rule.priority }}</td>
              <td>{{ rule.active ? 'yes' : 'no' }}</td>
              <td>{{ rule.updated_at | date:'short' }}</td>
              <td>
                <button class="btn btn-sm" (click)="deprecate(rule)" [disabled]="!rule.active">Deprecate</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
})
export class MemoryRulesComponent implements OnInit {
  rules: MemoryRuleItem[] = [];
  statement = '';
  ruleType = 'avoid';
  priority = 1;
  error: string | null = null;
  notice: string | null = null;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.error = null;
    this.api.getMemoryRules(true).subscribe({
      next: (resp) => (this.rules = resp.rules || []),
      error: (err) => (this.error = err?.error?.detail || err?.message || 'Failed to load memory rules'),
    });
  }

  createRule(): void {
    this.notice = null;
    this.error = null;
    this.api
      .upsertMemoryRule({
        scope: {},
        rule_type: this.ruleType,
        statement: this.statement.trim(),
        priority: this.priority,
      })
      .subscribe({
        next: () => {
          this.notice = 'Rule created';
          this.statement = '';
          this.load();
        },
        error: (err) => (this.error = err?.error?.detail || err?.message || 'Failed to create memory rule'),
      });
  }

  deprecate(rule: MemoryRuleItem): void {
    this.notice = null;
    this.error = null;
    this.api.deprecateMemoryRule(rule.id).subscribe({
      next: () => {
        this.notice = `Rule ${rule.id} deprecated`;
        this.load();
      },
      error: (err) => (this.error = err?.error?.detail || err?.message || 'Failed to deprecate rule'),
    });
  }
}

