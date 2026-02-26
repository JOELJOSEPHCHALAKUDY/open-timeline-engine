import { DashboardAgentRolesResponse } from '../core/models';
import { resolveRoleIdentitiesFromDashboard } from './utils';

describe('resolveRoleIdentitiesFromDashboard', () => {
  it('dedupes repeated executor identities across role lanes', () => {
    const roles: DashboardAgentRolesResponse = {
      workspace_id: 'personal',
      runtime_mode: { mode: 'clone_advisor', clone_enabled: true },
      executor: {
        user_id: 'codex-executor',
        consumer_id: 'codex-executor',
        label: 'Codex',
        status: 'active',
        last_seen_ts: null,
        source: 'event_or_audit_activity',
      },
      secondary_executor: {
        user_id: 'claude-executor',
        consumer_id: 'claude-executor',
        label: 'Claude',
        status: 'active',
        last_seen_ts: null,
        source: 'event_or_audit_activity',
      },
      advisor: {
        user_id: 'claude-executor',
        consumer_id: 'claude-executor',
        label: 'Claude',
        status: 'active',
        last_seen_ts: null,
        source: 'event_or_audit_activity',
      },
      generated_at: '2026-02-26T00:00:00Z',
    };

    const mapped = resolveRoleIdentitiesFromDashboard(roles, {
      executorClients: ['codex', 'claude'],
      knownIdentities: {
        executor: { user_id: 'codex-executor', label: 'Codex' },
        secondary_executor: { user_id: 'claude-executor', label: 'Claude' },
        advisor: { user_id: 'claude-executor', label: 'Claude' },
      },
    });

    expect(mapped.executors.map((item) => item.id)).toEqual(['codex-executor', 'claude-executor']);
    expect(mapped.additionalExecutor.id).toBe('claude-executor');
  });
});
