export type TimelineEvent = {
  schema_version: number;
  ts: string;
  actor: string;
  source: string;
  domain: string;
  task_type: string;
  event_type: string;
  title: string;
  payload: Record<string, unknown>;
  context: Record<string, unknown>;
  inputs: Record<string, unknown>;
  steps: Array<Record<string, unknown>>;
  decision: Record<string, unknown> | null;
  outcome: Record<string, unknown> | null;
  style: Record<string, unknown> | null;
  links: Record<string, unknown> | null;
  tags: string[];
  sensitivity: number;
  redaction_hints: string[];
};

export type Settings = {
  apiUrl: string;
  apiToken: string;
  consumerId: string;
  role: "user" | "executor" | "advisor";
  workspaceId: string;
  userId: string;
  defaultSensitivity: number;
  allowedSites: string[];
  takeoverSessionId: string;
  takeoverPersonaMode: string;
  takeoverActivationModeDefault: "suggest" | "takeover";
};

export const DEFAULT_SETTINGS: Settings = {
  apiUrl: "http://localhost:8080",
  apiToken: "local-dev-token",
  consumerId: "browser-user",
  role: "user",
  workspaceId: "personal",
  userId: "browser-user",
  defaultSensitivity: 1,
  allowedSites: [],
  takeoverSessionId: "browser-default",
  takeoverPersonaMode: "normal",
  takeoverActivationModeDefault: "takeover"
};

export async function getSettings(): Promise<Settings> {
  const res = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(res.settings ?? {}) };
}

export async function setSettings(settings: Settings): Promise<void> {
  await chrome.storage.local.set({ settings });
}

export async function getQueue(): Promise<TimelineEvent[]> {
  const res = await chrome.storage.local.get("queue");
  const maybeQueue = (res as { queue?: unknown }).queue;
  return Array.isArray(maybeQueue) ? (maybeQueue as TimelineEvent[]) : [];
}

export async function setQueue(queue: TimelineEvent[]): Promise<void> {
  await chrome.storage.local.set({ queue });
}
