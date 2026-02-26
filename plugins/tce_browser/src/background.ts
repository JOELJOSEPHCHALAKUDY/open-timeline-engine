import { TimelineEvent, getQueue, getSettings, setQueue } from "./common";
import { classifyDomainByUrl, hostFromUrl } from "./helpers";
import { normalizeTakeoverMode, personaActivationPhrase, personaStopPhrase } from "./takeover";

const REPLAY_CONCURRENCY = 4;

async function sendEvent(event: TimelineEvent, queueOnFailure: boolean): Promise<boolean> {
  const settings = await getSettings();
  const apiUrl = settings.apiUrl.replace(/\/$/, "");
  try {
    const response = await fetch(`${apiUrl}/v1/events`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${settings.apiToken}`,
        "X-TCE-Consumer": settings.consumerId,
        "X-TCE-Role": settings.role,
        "X-TCE-Workspace": settings.workspaceId,
        "X-TCE-User": settings.userId
      },
      body: JSON.stringify(event)
    });
    if (!response.ok) {
      throw new Error(`send failed: ${response.status}`);
    }
    return true;
  } catch {
    if (queueOnFailure) {
      const queue = await getQueue();
      queue.push(event);
      await setQueue(queue);
    }
    return false;
  }
}

async function callTakeoverApi(
  method: "GET" | "POST",
  path: string,
  params?: URLSearchParams,
  body?: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const settings = await getSettings();
  const apiUrl = settings.apiUrl.replace(/\/$/, "");
  const query = params && params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiUrl}${path}${query}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${settings.apiToken}`,
      "X-TCE-Consumer": settings.consumerId,
      "X-TCE-Role": settings.role,
      "X-TCE-Workspace": settings.workspaceId,
      "X-TCE-User": settings.userId
    },
    body: body ? JSON.stringify(body) : undefined
  });
  if (!response.ok) {
    throw new Error(`takeover api failed (${response.status})`);
  }
  return (await response.json()) as Record<string, unknown>;
}

function makeEvent(
  title: string,
  url: string,
  pageTitle: string,
  sensitivity: number,
  tags: string[],
  extra: Record<string, unknown> = {}
): TimelineEvent {
  return {
    schema_version: 1,
    ts: new Date().toISOString(),
    actor: "user",
    source: "browser",
    domain: classifyDomainByUrl(url),
    task_type: "research",
    event_type: "TASK_STEP",
    title,
    payload: { url, page_title: pageTitle, ...extra },
    context: {
      url,
      host: hostFromUrl(url)
    },
    inputs: {},
    steps: [],
    decision: null,
    outcome: null,
    style: null,
    links: { docs: [url] },
    tags: Array.from(new Set(["browser", "research", ...tags])),
    sensitivity,
    redaction_hints: []
  };
}

async function siteAllowed(url: string): Promise<boolean> {
  const { allowedSites } = await getSettings();
  const host = hostFromUrl(url);
  return allowedSites.includes(host);
}

async function replayQueueInternal(): Promise<{ ok: true; sent: number; remaining: number }> {
  const queue = await getQueue();
  const remaining: TimelineEvent[] = [];
  let sent = 0;
  const workers = Math.max(1, REPLAY_CONCURRENCY);
  for (let idx = 0; idx < queue.length; idx += workers) {
    const batch = queue.slice(idx, idx + workers);
    const results = await Promise.all(batch.map((event) => sendEvent(event, false)));
    for (let i = 0; i < batch.length; i += 1) {
      if (results[i]) {
        sent += 1;
      } else {
        remaining.push(batch[i]);
      }
    }
  }
  await setQueue(remaining);
  return { ok: true, sent, remaining: remaining.length };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "tce-capture-link",
    title: "Capture link to TCE",
    contexts: ["link"]
  });
  chrome.contextMenus.create({
    id: "tce-capture-page",
    title: "Capture page to TCE",
    contexts: ["page"]
  });
  chrome.contextMenus.create({
    id: "tce-capture-selection",
    title: "Capture selection note to TCE",
    contexts: ["selection"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info: chrome.contextMenus.OnClickData, tab?: chrome.tabs.Tab) => {
  const settings = await getSettings();

  if (info.menuItemId === "tce-capture-link" && info.linkUrl) {
    const allowed = await siteAllowed(info.linkUrl);
    if (!allowed) {
      return;
    }
    await sendEvent(
      makeEvent("Captured research link", info.linkUrl, tab?.title ?? "Link capture", settings.defaultSensitivity, ["link"]),
      true
    );
    return;
  }

  if (info.menuItemId === "tce-capture-page" && tab?.url) {
    const allowed = await siteAllowed(tab.url);
    if (!allowed) {
      return;
    }
    await sendEvent(
      makeEvent("Captured current page", tab.url, tab.title ?? "Page capture", settings.defaultSensitivity, ["page"]),
      true
    );
    return;
  }

  if (info.menuItemId === "tce-capture-selection" && tab?.url && info.selectionText) {
    const allowed = await siteAllowed(tab.url);
    if (!allowed) {
      return;
    }
    await sendEvent(
      makeEvent("Captured selection note", tab.url, tab.title ?? "Selection capture", settings.defaultSensitivity, ["selection"], {
        selection: info.selectionText
      }),
      true
    );
  }
});

type RuntimeMessage = {
  type?:
    | "capture-current-tab"
    | "replay-queue"
    | "queue-size"
    | "takeover-state"
    | "takeover-step"
    | "takeover-stop";
  message?: string;
  task?: string;
  executorOutput?: string;
};

chrome.runtime.onMessage.addListener(
  (
    message: RuntimeMessage | undefined,
    _sender: chrome.runtime.MessageSender,
    sendResponse: (response?: unknown) => void
  ) => {
  if (message?.type === "capture-current-tab") {
    chrome.tabs.query({ active: true, currentWindow: true }, async (tabs: chrome.tabs.Tab[]) => {
      const tab = tabs[0];
      if (!tab?.url) {
        sendResponse({ ok: false, reason: "no-tab" });
        return;
      }
      const allowed = await siteAllowed(tab.url);
      if (!allowed) {
        sendResponse({ ok: false, reason: "site-not-allowed" });
        return;
      }
      const settings = await getSettings();
      const ok = await sendEvent(
        makeEvent(tab.title ?? "Captured tab", tab.url, tab.title ?? "Captured tab", settings.defaultSensitivity, ["manual"]),
        true
      );
      sendResponse({ ok });
    });
    return true;
  }

  if (message?.type === "replay-queue") {
    void replayQueueInternal().then(sendResponse);
    return true;
  }

  if (message?.type === "takeover-state") {
    void (async () => {
      try {
        const settings = await getSettings();
        const params = new URLSearchParams({
          session_id: settings.takeoverSessionId,
          persona_mode: settings.takeoverPersonaMode
        });
        const state = await callTakeoverApi("GET", "/v1/takeover/state", params);
        sendResponse({ ok: true, state });
      } catch (error) {
        sendResponse({ ok: false, error: String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "takeover-stop") {
    void (async () => {
      try {
        const settings = await getSettings();
        const stopMessage = personaStopPhrase(settings.takeoverPersonaMode);
        const activation = personaActivationPhrase(settings.takeoverPersonaMode);
        const result = await callTakeoverApi("POST", "/v1/takeover/step", undefined, {
          message: stopMessage,
          session_id: settings.takeoverSessionId,
          persona_mode: settings.takeoverPersonaMode,
          activation_keywords: activation,
          stop_keywords: stopMessage,
          activation_mode_default: settings.takeoverActivationModeDefault,
          task: "browser takeover stop",
          app_context: { domain: "research", source: "browser" },
          constraints: {},
          takeover_context: {},
          message_delta: { new_user_message: stopMessage, latest_executor_output: "" },
          allow_fallback: true
        });
        sendResponse({ ok: true, result });
      } catch (error) {
        sendResponse({ ok: false, error: String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "takeover-step") {
    void (async () => {
      try {
        const settings = await getSettings();
        const rawMessage = (message.message || "").trim();
        const messageText = rawMessage || personaActivationPhrase(settings.takeoverPersonaMode);
        const activation = personaActivationPhrase(settings.takeoverPersonaMode);
        const stop = personaStopPhrase(settings.takeoverPersonaMode);
        const result = await callTakeoverApi("POST", "/v1/takeover/step", undefined, {
          message: messageText,
          session_id: settings.takeoverSessionId,
          persona_mode: settings.takeoverPersonaMode,
          activation_keywords: activation,
          stop_keywords: stop,
          activation_mode_default: normalizeTakeoverMode(settings.takeoverActivationModeDefault),
          task: message.task || messageText,
          app_context: { domain: "research", source: "browser" },
          constraints: {},
          takeover_context: { objective: message.task || messageText },
          message_delta: {
            new_user_message: messageText,
            latest_executor_output: message.executorOutput || ""
          },
          executor_output: message.executorOutput || null,
          allow_fallback: true
        });
        sendResponse({ ok: true, result });
      } catch (error) {
        sendResponse({ ok: false, error: String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "queue-size") {
    void getQueue().then((queue) => sendResponse({ ok: true, size: queue.length }));
    return true;
  }

    return false;
  }
);
