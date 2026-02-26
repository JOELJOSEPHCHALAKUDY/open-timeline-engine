import { AuthService } from './auth.service';

describe('AuthService', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('uses local defaults when no prior settings exist', () => {
    const service = new AuthService();
    const current = service.get();
    expect(current.token).toBe('local-dev-token');
    expect(current.workspace).toBe('personal');
    expect(current.role).toBe('executor');
  });

  it('hydrates workspace/user/consumer from server defaults', () => {
    const service = new AuthService();
    service.applyServerDefaults({
      api_base: '/v1',
      default_workspace_id: 'workspace-a',
      default_user_id: 'user-a',
      default_consumer_id: 'consumer-a',
      default_session_id: 'default',
      runtime_mode: { mode: 'clone_advisor', clone_enabled: true },
      known_identities: {
        executor: { user_id: 'codex-executor', label: 'Codex' },
        secondary_executor: { user_id: 'claude-executor', label: 'Claude Executor' },
        advisor: { user_id: 'claude-executor', label: 'Claude Executor' },
      },
      generated_at: new Date().toISOString(),
    });
    const current = service.get();
    expect(current.workspace).toBe('workspace-a');
    expect(current.user).toBe('user-a');
    expect(current.consumer).toBe('consumer-a');
  });
});
