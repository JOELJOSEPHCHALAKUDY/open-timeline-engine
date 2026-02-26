import { DashboardAgentRolesResponse, DashboardIdentityInfo } from '../core/models';

export interface AgentInfo {
  label: string;
  id: string;
  status: 'active' | 'registered' | 'not seen' | string;
  lastSeen: string | null;
}

export interface RoleIdentities {
  executor: AgentInfo;
  additionalExecutor: AgentInfo;
  executors: AgentInfo[];
  source: string;
}

interface RoleIdentityResolveOptions {
  executorClients?: string[] | null;
  knownIdentities?: Record<string, DashboardIdentityInfo | undefined> | null;
}

interface MutableAgentInfo extends AgentInfo {
  order: number;
}

export function resolveRoleIdentitiesFromDashboard(
  roles: DashboardAgentRolesResponse,
  options: RoleIdentityResolveOptions = {}
): RoleIdentities {
  const knownIdentities = options.knownIdentities || {};
  const executorClients = Array.isArray(options.executorClients) ? options.executorClients : [];

  const roleRows = extractRoleRows(roles);
  const byId = new Map<string, MutableAgentInfo>();
  let rollingOrder = 1000;

  const upsert = (
    id: string,
    label: string,
    status: string,
    lastSeen: string | null,
    preferredOrder?: number
  ): void => {
    const normalizedId = String(id || '').trim();
    if (!normalizedId) return;

    const normalizedLabel = String(label || '').trim() || 'Executor';
    const normalizedStatus = normalizeStatus(status || 'not_seen');
    const order = preferredOrder ?? rollingOrder++;
    const existing = byId.get(normalizedId);
    if (!existing) {
      byId.set(normalizedId, {
        id: normalizedId,
        label: normalizedLabel,
        status: normalizedStatus,
        lastSeen: lastSeen || null,
        order,
      });
      return;
    }

    if (!existing.label || existing.label === 'Executor') {
      existing.label = normalizedLabel;
    }
    if (statusRank(normalizedStatus) > statusRank(existing.status)) {
      existing.status = normalizedStatus;
    }
    if (!existing.lastSeen && lastSeen) {
      existing.lastSeen = lastSeen;
    }
    existing.order = Math.min(existing.order, order);
  };

  const primaryRole = roleRows['executor'];
  // Backward-compatible: some API payloads still emit "advisor" as the second executor lane.
  const additionalRole = roleRows['secondary_executor'] || roleRows['advisor'];
  if (primaryRole) {
    upsert(
      primaryRole.user_id || primaryRole.consumer_id || 'unknown',
      primaryRole.label || 'Executor',
      primaryRole.status || 'not_seen',
      primaryRole.last_seen_ts || null,
      0
    );
  }
  if (additionalRole) {
    upsert(
      additionalRole.user_id || additionalRole.consumer_id || 'unknown',
      additionalRole.label || 'Additional Executor',
      additionalRole.status || 'not_seen',
      additionalRole.last_seen_ts || null,
      1
    );
  }

  for (const [key, value] of Object.entries(roleRows)) {
    upsert(
      value.user_id || value.consumer_id || '',
      value.label || roleLabelFromKey(key),
      value.status || 'not_seen',
      value.last_seen_ts || null
    );
  }

  for (const [key, identity] of Object.entries(knownIdentities)) {
    if (!identity?.user_id) continue;
    upsert(identity.user_id, identity.label || roleLabelFromKey(key), 'not_seen', null, roleOrderFromKey(key));
  }

  executorClients.forEach((rawClient, idx) => {
    const client = String(rawClient || '').trim().toLowerCase();
    if (!client) return;
    const aliases = executorClientIdAliases(client);
    if (aliases.some((id) => byId.has(id))) {
      return;
    }
    // Use the canonical fallback form only when neither known suffix is present.
    upsert(aliases[0], titleCaseClient(client), 'not_seen', null, 200 + idx);
  });

  const executors = Array.from(byId.values())
    .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label))
    .map(({ order, ...item }) => item);

  const defaultExecutor: AgentInfo = {
    label: 'Executor',
    id: 'unknown',
    status: 'not seen',
    lastSeen: null,
  };
  const defaultAdditional: AgentInfo = {
    label: 'Additional Executor',
    id: 'unknown',
    status: 'not seen',
    lastSeen: null,
  };

  const executor = primaryRole
    ? pickFromList(executors, primaryRole.user_id || primaryRole.consumer_id) || defaultExecutor
    : executors[0] || defaultExecutor;
  const additionalExecutor = additionalRole
    ? pickFromList(executors, additionalRole.user_id || additionalRole.consumer_id) || executors[1] || defaultAdditional
    : executors[1] || defaultAdditional;

  const source = roles.runtime_mode.clone_enabled
    ? 'Role state from /v1/dashboard/agent-roles'
    : 'Additional executor lanes may be idle while runtime mode is timeline_only.';
  return { executor, additionalExecutor, executors, source };
}

function normalizeStatus(value: string): string {
  if (!value) return 'not seen';
  if (value === 'not_seen') return 'not seen';
  return value;
}

function extractRoleRows(roles: DashboardAgentRolesResponse): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [key, value] of Object.entries((roles || {}) as unknown as Record<string, unknown>)) {
    if (!value || typeof value !== 'object') continue;
    const role = value as Record<string, unknown>;
    if (typeof role['user_id'] !== 'string' && typeof role['consumer_id'] !== 'string') continue;
    out[key] = role;
  }
  return out;
}

function pickFromList(list: AgentInfo[], id: string | undefined): AgentInfo | null {
  const key = String(id || '').trim();
  if (!key) return null;
  return list.find((item) => item.id === key) || null;
}

function statusRank(status: string): number {
  if (status === 'active') return 3;
  if (status === 'registered') return 2;
  if (status === 'not seen') return 1;
  return 0;
}

function roleOrderFromKey(key: string): number {
  if (key === 'executor') return 0;
  if (key === 'secondary_executor') return 1;
  if (key === 'advisor') return 2;
  return 400;
}

function roleLabelFromKey(key: string): string {
  if (key === 'executor') return 'Executor';
  if (key === 'secondary_executor' || key === 'advisor') return 'Additional Executor';
  return key
    .split('_')
    .filter((part) => part.length > 0)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

function titleCaseClient(client: string): string {
  return client
    .split(/[-_\s]+/)
    .filter((part) => part.length > 0)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

function executorClientIdAliases(client: string): [string, string] {
  const base = String(client || '').trim().toLowerCase();
  return [`${base}-executor`, `${base}-executer`];
}

export function formatUptime(seconds: number): string {
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h + 'h ' + m + 'm';
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'N/A';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
