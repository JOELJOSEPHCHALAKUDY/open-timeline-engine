export const environment = {
  production: true,
  apiBase: '/v1',
  agents: {
    executor: { id: 'codex-executor', label: 'Codex' },
    additionalExecutor: { id: 'claude-executor', label: 'Claude' },
  },
};
