import { Injectable } from '@angular/core';
import { DashboardClientConfig } from '../models';

export interface AuthSettings {
  token: string;
  consumer: string;
  role: string;
  workspace: string;
  user: string;
}

const STORAGE_KEY = 'tce_auth';
const DEFAULTS: AuthSettings = {
  token: 'local-dev-token',
  consumer: 'dashboard',
  role: 'executor',
  workspace: 'personal',
  user: 'local-user',
};

@Injectable({ providedIn: 'root' })
export class AuthService {
  private settings: AuthSettings;

  constructor() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) {
      this.settings = { ...DEFAULTS };
      return;
    }
    try {
      const parsed = JSON.parse(stored) as Partial<AuthSettings>;
      this.settings = { ...DEFAULTS, ...parsed };
    } catch {
      this.settings = { ...DEFAULTS };
      localStorage.removeItem(STORAGE_KEY);
    }
    if (!['user', 'executor', 'advisor', 'admin'].includes(this.settings.role)) {
      this.settings.role = DEFAULTS.role;
    }
    if (!this.settings.workspace?.trim()) {
      this.settings.workspace = DEFAULTS.workspace;
    }
    if (!this.settings.user?.trim()) {
      this.settings.user = DEFAULTS.user;
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(this.settings));
  }

  get(): AuthSettings {
    return { ...this.settings };
  }

  update(partial: Partial<AuthSettings>): void {
    this.settings = { ...this.settings, ...partial };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(this.settings));
  }

  reset(): void {
    this.settings = { ...DEFAULTS };
    localStorage.removeItem(STORAGE_KEY);
  }

  applyServerDefaults(config: DashboardClientConfig): void {
    const next: AuthSettings = {
      ...this.settings,
      workspace: (config.default_workspace_id || this.settings.workspace || DEFAULTS.workspace).trim(),
      user: (config.default_user_id || this.settings.user || DEFAULTS.user).trim(),
      consumer: (config.default_consumer_id || this.settings.consumer || DEFAULTS.consumer).trim(),
    };
    if (!next.workspace) next.workspace = DEFAULTS.workspace;
    if (!next.user) next.user = DEFAULTS.user;
    if (!next.consumer) next.consumer = DEFAULTS.consumer;
    this.settings = next;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(this.settings));
  }
}
