import * as vscode from "vscode";

import { buildEventHeaders, normalizeApiUrl, shouldIgnoreCommand } from "./helpers";
import { personaActivationPhrase, personaStopPhrase, resolveTakeoverMode } from "./takeover";

type TimelineEvent = {
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

type ActiveTask = {
  id: string;
  title: string;
  domain: string;
  taskType: string;
  startedAt: string;
};

type ExtensionConfig = {
  apiUrl: string;
  apiToken: string;
  defaultSensitivity: number;
  consumerId: string;
  role: string;
  workspaceId: string;
  userId: string;
  defaultDomain: string;
  defaultTaskType: string;
  autoReplayOnStartup: boolean;
  autoCaptureCommands: boolean;
  autoCaptureSaves: boolean;
  ignoredCommands: string[];
  takeoverSessionId: string;
  takeoverPersonaMode: string;
  takeoverActivationModeDefault: "suggest" | "takeover";
};

const QUEUE_KEY = "tce.offlineQueue";
const ACTIVE_TASK_KEY = "tce.activeTask";
const REPLAY_CONCURRENCY = 4;

function nowISO(): string {
  return new Date().toISOString();
}

function taskId(): string {
  return `task-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function cfg(): ExtensionConfig {
  const config = vscode.workspace.getConfiguration("tce");
  return {
    apiUrl: normalizeApiUrl(String(config.get("apiUrl", "http://localhost:8080"))),
    apiToken: String(config.get("apiToken", "local-dev-token")),
    defaultSensitivity: Number(config.get("defaultSensitivity", 1)),
    consumerId: String(config.get("consumerId", "vscode-user")),
    role: String(config.get("role", "user")),
    workspaceId: String(config.get("workspaceId", "personal")),
    userId: String(config.get("userId", "vscode-user")),
    defaultDomain: String(config.get("defaultDomain", "coding")),
    defaultTaskType: String(config.get("defaultTaskType", "editor_task")),
    autoReplayOnStartup: Boolean(config.get("autoReplayOnStartup", true)),
    autoCaptureCommands: Boolean(config.get("autoCaptureCommands", true)),
    autoCaptureSaves: Boolean(config.get("autoCaptureSaves", true)),
    ignoredCommands: Array.from(config.get<string[]>("ignoredCommands", ["tce."])),
    takeoverSessionId: String(config.get("takeoverSessionId", "vscode-default")),
    takeoverPersonaMode: String(config.get("takeoverPersonaMode", "normal")),
    takeoverActivationModeDefault: resolveTakeoverMode(String(config.get("takeoverActivationModeDefault", "takeover")))
  };
}

function currentContext(): Record<string, unknown> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  const editor = vscode.window.activeTextEditor;
  return {
    workspace: folder?.uri.fsPath ?? "",
    file: editor?.document.uri.fsPath ?? "",
    language: editor?.document.languageId ?? "",
    _tce_workspace: cfg().workspaceId,
    _tce_owner: cfg().userId
  };
}

function buildEvent(
  eventType: string,
  title: string,
  payload: Record<string, unknown>,
  activeTask: ActiveTask | null
): TimelineEvent {
  const c = cfg();
  return {
    schema_version: 1,
    ts: nowISO(),
    actor: "user",
    source: "vscode",
    domain: activeTask?.domain ?? c.defaultDomain,
    task_type: activeTask?.taskType ?? c.defaultTaskType,
    event_type: eventType,
    title,
    payload: {
      ...payload,
      active_task_id: activeTask?.id ?? null
    },
    context: currentContext(),
    inputs: {},
    steps: [],
    decision: null,
    outcome: null,
    style: null,
    links: null,
    tags: ["vscode"],
    sensitivity: c.defaultSensitivity,
    redaction_hints: []
  };
}

async function getQueue(context: vscode.ExtensionContext): Promise<TimelineEvent[]> {
  return context.globalState.get<TimelineEvent[]>(QUEUE_KEY, []);
}

async function setQueue(context: vscode.ExtensionContext, queue: TimelineEvent[]): Promise<void> {
  await context.globalState.update(QUEUE_KEY, queue);
}

async function enqueue(context: vscode.ExtensionContext, event: TimelineEvent): Promise<void> {
  const queue = await getQueue(context);
  queue.push(event);
  await setQueue(context, queue);
}

async function sendEvent(
  context: vscode.ExtensionContext,
  event: TimelineEvent,
  queueOnFailure: boolean
): Promise<boolean> {
  const c = cfg();
  try {
    const response = await fetch(`${c.apiUrl}/v1/events`, {
      method: "POST",
      headers: buildEventHeaders(c),
      body: JSON.stringify(event)
    });
    if (!response.ok) {
      throw new Error(`event send failed (${response.status})`);
    }
    return true;
  } catch {
    if (queueOnFailure) {
      await enqueue(context, event);
    }
    return false;
  }
}

async function sendCompletion(activeTask: ActiveTask, outcome: string): Promise<boolean> {
  const c = cfg();
  const editor = vscode.window.activeTextEditor;
  const file = editor?.document.uri.fsPath ?? "workspace";
  const line = editor ? editor.selection.active.line + 1 : undefined;
  try {
    const response = await fetch(`${c.apiUrl}/v1/completions`, {
      method: "POST",
      headers: buildEventHeaders(c),
      body: JSON.stringify({
        session_id: c.takeoverSessionId,
        completion_key: `vscode:${activeTask.id}`,
        source: "vscode-completion",
        state: "succeeded",
        title: activeTask.title.slice(0, 160),
        payload: { files: [file] },
        decision: outcome.slice(0, 500),
        outcome: { status: "succeeded", next_step: "Continue from the active editor anchor." },
        git: {},
        anchors: [{ file, ...(line ? { line } : {}) }],
        milestone_schema: "v1"
      })
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function getActiveTask(context: vscode.ExtensionContext): Promise<ActiveTask | null> {
  return context.globalState.get<ActiveTask | null>(ACTIVE_TASK_KEY, null);
}

async function setActiveTask(context: vscode.ExtensionContext, task: ActiveTask | null): Promise<void> {
  await context.globalState.update(ACTIVE_TASK_KEY, task);
}

async function replayQueue(
  context: vscode.ExtensionContext,
  status: vscode.StatusBarItem,
  silent: boolean
): Promise<void> {
  const queue = await getQueue(context);
  if (!queue.length) {
    if (!silent) {
      vscode.window.showInformationMessage("TCE queue is empty");
    }
    await updateStatus(status, context);
    return;
  }
  const remaining: TimelineEvent[] = [];
  let sent = 0;
  const workers = Math.max(1, REPLAY_CONCURRENCY);
  for (let idx = 0; idx < queue.length; idx += workers) {
    const batch = queue.slice(idx, idx + workers);
    const results = await Promise.all(batch.map((event) => sendEvent(context, event, false)));
    for (let i = 0; i < batch.length; i += 1) {
      if (results[i]) {
        sent += 1;
      } else {
        remaining.push(batch[i]);
      }
    }
  }
  await setQueue(context, remaining);
  await updateStatus(status, context);
  if (!silent) {
    vscode.window.showInformationMessage(`TCE replay: sent=${sent}, remaining=${remaining.length}`);
  }
}

async function updateStatus(status: vscode.StatusBarItem, context: vscode.ExtensionContext): Promise<void> {
  const queue = await getQueue(context);
  const activeTask = await getActiveTask(context);
  const active = activeTask ? `task:${activeTask.title}` : "task:none";
  status.text = `$(pulse) TCE ${active} queue:${queue.length}`;
  status.tooltip = "Open Timeline Engine capture status";
}

async function takeoverStateRequest(config: ExtensionConfig): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({
    session_id: config.takeoverSessionId,
    persona_mode: config.takeoverPersonaMode
  });
  const response = await fetch(`${config.apiUrl}/v1/takeover/state?${params.toString()}`, {
    method: "GET",
    headers: buildEventHeaders(config)
  });
  if (!response.ok) {
    throw new Error(`takeover state failed (${response.status})`);
  }
  return (await response.json()) as Record<string, unknown>;
}

async function takeoverStepRequest(
  config: ExtensionConfig,
  message: string,
  task: string | undefined,
  executorOutput: string | undefined
): Promise<Record<string, unknown>> {
  const activation = personaActivationPhrase(config.takeoverPersonaMode);
  const stop = personaStopPhrase(config.takeoverPersonaMode);
  const body = {
    message,
    session_id: config.takeoverSessionId,
    persona_mode: config.takeoverPersonaMode,
    activation_keywords: activation,
    stop_keywords: stop,
    activation_mode_default: config.takeoverActivationModeDefault,
    task: task || message,
    app_context: {
      domain: config.defaultDomain,
      editor_file: vscode.window.activeTextEditor?.document.uri.fsPath || ""
    },
    constraints: {},
    takeover_context: {
      objective: task || message
    },
    message_delta: {
      new_user_message: message,
      latest_executor_output: executorOutput || ""
    },
    executor_output: executorOutput || null,
    allow_fallback: true
  };
  const response = await fetch(`${config.apiUrl}/v1/takeover/step`, {
    method: "POST",
    headers: buildEventHeaders(config),
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`takeover step failed (${response.status})`);
  }
  return (await response.json()) as Record<string, unknown>;
}

export function activate(context: vscode.ExtensionContext): void {
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.command = "tce.replayQueue";
  status.show();
  void updateStatus(status, context);

  const startTask = vscode.commands.registerCommand("tce.startTask", async () => {
    const title = await vscode.window.showInputBox({ prompt: "Task title" });
    if (!title) {
      return;
    }
    const c = cfg();
    const domain = (await vscode.window.showInputBox({ prompt: "Domain", value: c.defaultDomain })) || c.defaultDomain;
    const taskType =
      (await vscode.window.showInputBox({ prompt: "Task type", value: c.defaultTaskType })) || c.defaultTaskType;
    const task: ActiveTask = {
      id: taskId(),
      title,
      domain,
      taskType,
      startedAt: nowISO()
    };
    await setActiveTask(context, task);
    const event = buildEvent("TASK_START", title, { kind: "task_start", task_id: task.id }, task);
    const sent = await sendEvent(context, event, true);
    await updateStatus(status, context);
    vscode.window.showInformationMessage(sent ? "TCE task started" : "TCE task queued (offline)");
  });

  const logStep = vscode.commands.registerCommand("tce.logStep", async () => {
    const description = await vscode.window.showInputBox({ prompt: "Step description" });
    if (!description) {
      return;
    }
    const activeTask = await getActiveTask(context);
    const event = buildEvent(
      "TASK_STEP",
      `Step: ${description.slice(0, 120)}`,
      { kind: "task_step", description },
      activeTask
    );
    const sent = await sendEvent(context, event, true);
    await updateStatus(status, context);
    vscode.window.showInformationMessage(sent ? "TCE step sent" : "TCE step queued");
  });

  const logDecision = vscode.commands.registerCommand("tce.logDecision", async () => {
    const choice = await vscode.window.showInputBox({ prompt: "Decision choice" });
    const rationale = await vscode.window.showInputBox({ prompt: "Decision rationale" });
    if (!choice || !rationale) {
      return;
    }
    const activeTask = await getActiveTask(context);
    const event = buildEvent(
      "TASK_DECISION",
      `Decision: ${choice.slice(0, 120)}`,
      { kind: "task_decision", choice, rationale },
      activeTask
    );
    event.decision = { choice, alternatives: [], rationale, signals_used: [] };
    const sent = await sendEvent(context, event, true);
    await updateStatus(status, context);
    vscode.window.showInformationMessage(sent ? "TCE decision sent" : "TCE decision queued");
  });

  const completeTask = vscode.commands.registerCommand("tce.completeTask", async () => {
    const activeTask = await getActiveTask(context);
    if (!activeTask) {
      vscode.window.showWarningMessage("Start a TCE task before completing it.");
      return;
    }
    const outcome = await vscode.window.showInputBox({ prompt: "Outcome summary" });
    if (!outcome) {
      return;
    }
    const event = buildEvent(
      "TASK_DONE",
      `Done: ${outcome.slice(0, 120)}`,
      { kind: "task_done", outcome, task_closed: true },
      activeTask
    );
    event.outcome = { success: true, metrics: {}, followups: [] };
    const eventSent = await sendEvent(context, event, true);
    const completionSent = await sendCompletion(activeTask, outcome);
    if (completionSent) {
      await setActiveTask(context, null);
    }
    await updateStatus(status, context);
    vscode.window.showInformationMessage(
      eventSent && completionSent ? "TCE task completed" : "TCE completion not delivered; retry Complete Task"
    );
  });

  const noteReflection = vscode.commands.registerCommand("tce.noteReflection", async () => {
    const note = await vscode.window.showInputBox({ prompt: "Reflection note" });
    if (!note) {
      return;
    }
    const tagsRaw = (await vscode.window.showInputBox({ prompt: "Tags (comma separated)", value: "reflection" })) || "";
    const tags = tagsRaw
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean);
    const activeTask = await getActiveTask(context);
    const event = buildEvent(
      "REFLECTION",
      `Reflection: ${note.slice(0, 120)}`,
      { note, tags },
      activeTask
    );
    event.tags = Array.from(new Set([...event.tags, ...tags]));
    const sent = await sendEvent(context, event, true);
    await updateStatus(status, context);
    vscode.window.showInformationMessage(sent ? "TCE reflection sent" : "TCE reflection queued");
  });

  const takeoverActivate = vscode.commands.registerCommand("tce.takeoverActivate", async () => {
    const c = cfg();
    const phrase = personaActivationPhrase(c.takeoverPersonaMode);
    const result = await takeoverStepRequest(c, phrase, "activate takeover", undefined);
    const state = (result.state ?? {}) as { mode?: string; active?: boolean };
    vscode.window.showInformationMessage(
      `TCE takeover ${state.active ? "active" : "inactive"} (${state.mode ?? "unknown"})`
    );
  });

  const takeoverStep = vscode.commands.registerCommand("tce.takeoverStep", async () => {
    const c = cfg();
    const message = await vscode.window.showInputBox({ prompt: "Takeover step message" });
    if (!message) {
      return;
    }
    const task = await vscode.window.showInputBox({ prompt: "Task (optional)", value: message });
    const executorOutput = await vscode.window.showInputBox({ prompt: "Executor output (optional)" });
    const result = await takeoverStepRequest(c, message, task ?? undefined, executorOutput ?? undefined);
    const mode = String((result.state as { mode?: string } | undefined)?.mode ?? "takeover");
    const finalResponse = String(result.final_response ?? "");
    if (mode === "takeover") {
      vscode.window.showInformationMessage(finalResponse || "Takeover step completed.");
    } else {
      vscode.window.showInformationMessage(finalResponse || "Suggestion step completed.");
    }
  });

  const takeoverStop = vscode.commands.registerCommand("tce.takeoverStop", async () => {
    const c = cfg();
    const phrase = personaStopPhrase(c.takeoverPersonaMode);
    const result = await takeoverStepRequest(c, phrase, "stop takeover", undefined);
    const action = String(result.action ?? "stopped");
    vscode.window.showInformationMessage(`TCE takeover ${action}`);
  });

  const takeoverState = vscode.commands.registerCommand("tce.takeoverState", async () => {
    const c = cfg();
    const state = await takeoverStateRequest(c);
    const active = Boolean(state.active);
    const mode = String(state.mode ?? "takeover");
    vscode.window.showInformationMessage(`TCE takeover state: ${active ? "active" : "inactive"} (${mode})`);
  });

  const replayQueueCommand = vscode.commands.registerCommand("tce.replayQueue", async () => {
    await replayQueue(context, status, false);
  });

  type CommandEvent = { command: string; arguments?: unknown[] };
  type CommandsWithEvent = {
    onDidExecuteCommand?: (
      listener: (commandEvent: CommandEvent) => void | Promise<void>
    ) => vscode.Disposable;
  };
  const commandsWithEvent = vscode.commands as unknown as CommandsWithEvent;
  const commandCapture = commandsWithEvent.onDidExecuteCommand?.(async (commandEvent: CommandEvent) => {
    const c = cfg();
    if (!c.autoCaptureCommands) {
      return;
    }
    if (shouldIgnoreCommand(commandEvent.command, c.ignoredCommands)) {
      return;
    }
    const activeTask = await getActiveTask(context);
    const event = buildEvent(
      "COMMAND_RUN",
      `Command: ${commandEvent.command}`,
      {
        command: commandEvent.command,
        argument_count: Array.isArray(commandEvent.arguments) ? commandEvent.arguments.length : 0
      },
      activeTask
    );
    await sendEvent(context, event, true);
    await updateStatus(status, context);
  });

  const saveCapture = vscode.workspace.onDidSaveTextDocument(async (document) => {
    const c = cfg();
    if (!c.autoCaptureSaves) {
      return;
    }
    const activeTask = await getActiveTask(context);
    const event = buildEvent(
      "DOC_EDIT",
      `Saved: ${document.fileName.split("/").pop() ?? document.fileName}`,
      {
        file: document.fileName,
        language: document.languageId,
        line_count: document.lineCount
      },
      activeTask
    );
    await sendEvent(context, event, true);
    await updateStatus(status, context);
  });

  context.subscriptions.push(
    status,
    startTask,
    logStep,
    logDecision,
    completeTask,
    noteReflection,
    takeoverActivate,
    takeoverStep,
    takeoverStop,
    takeoverState,
    replayQueueCommand,
    ...(commandCapture ? [commandCapture] : []),
    saveCapture,
  );

  if (cfg().autoReplayOnStartup) {
    void replayQueue(context, status, true);
  }
}

export function deactivate(): void {
  return;
}
