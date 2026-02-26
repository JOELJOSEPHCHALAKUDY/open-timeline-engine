export type HeaderConfig = {
  apiToken: string;
  consumerId: string;
  role: string;
  workspaceId: string;
  userId: string;
};

export function normalizeApiUrl(apiUrl: string): string {
  return apiUrl.replace(/\/+$/, "");
}

export function buildEventHeaders(config: HeaderConfig): Record<string, string> {
  return {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${config.apiToken}`,
    "X-TCE-Consumer": config.consumerId,
    "X-TCE-Role": config.role,
    "X-TCE-Workspace": config.workspaceId,
    "X-TCE-User": config.userId
  };
}

export function shouldIgnoreCommand(command: string, ignoredPrefixes: string[]): boolean {
  return ignoredPrefixes.some((prefix) => prefix.length > 0 && command.startsWith(prefix));
}
