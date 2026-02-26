import { getSettings, setSettings } from "./common";

const statusEl = document.getElementById("status") as HTMLDivElement;
const queueSizeEl = document.getElementById("queueSize") as HTMLDivElement;
const captureToggle = document.getElementById("captureEnabled") as HTMLInputElement;
const captureNow = document.getElementById("captureNow") as HTMLButtonElement;
const replayQueue = document.getElementById("replayQueue") as HTMLButtonElement;
const takeoverMessage = document.getElementById("takeoverMessage") as HTMLInputElement;
const takeoverActivate = document.getElementById("takeoverActivate") as HTMLButtonElement;
const takeoverStep = document.getElementById("takeoverStep") as HTMLButtonElement;
const takeoverStop = document.getElementById("takeoverStop") as HTMLButtonElement;
const takeoverState = document.getElementById("takeoverState") as HTMLButtonElement;
const openOptions = document.getElementById("openOptions") as HTMLButtonElement;

function setStatus(text: string): void {
  statusEl.textContent = text;
}

async function updateQueueSize(): Promise<void> {
  try {
    const result = await chrome.runtime.sendMessage({ type: "queue-size" });
    if (result?.ok) {
      queueSizeEl.textContent = `Queued events: ${result.size}`;
      return;
    }
  } catch {
    // no-op
  }
  queueSizeEl.textContent = "Queued events: unknown";
}

async function currentHost(): Promise<string | null> {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tabs[0]?.url;
  if (!url) return null;
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}

async function init(): Promise<void> {
  const host = await currentHost();
  const settings = await getSettings();
  await updateQueueSize();
  captureToggle.checked = !!host && settings.allowedSites.includes(host);
  setStatus(host ? `Site: ${host}` : "No active tab");
}

captureToggle.addEventListener("change", async () => {
  const host = await currentHost();
  if (!host) return;
  const settings = await getSettings();
  const allowedSites = new Set(settings.allowedSites);
  if (captureToggle.checked) allowedSites.add(host);
  else allowedSites.delete(host);
  await setSettings({ ...settings, allowedSites: Array.from(allowedSites) });
  setStatus(`Capture ${captureToggle.checked ? "enabled" : "disabled"} for ${host}`);
});

captureNow.addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({ type: "capture-current-tab" });
  await updateQueueSize();
  if (result?.ok) setStatus("Captured and sent");
  else setStatus(`Capture skipped: ${result?.reason ?? "error"}`);
});

replayQueue.addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({ type: "replay-queue" });
  await updateQueueSize();
  if (result?.ok) setStatus(`Replay sent=${result.sent} remaining=${result.remaining}`);
  else setStatus("Replay failed");
});

takeoverActivate.addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({
    type: "takeover-step",
    message: takeoverMessage.value.trim() || "hey advisor take over"
  });
  if (result?.ok) {
    setStatus(`Takeover: ${result.result?.action ?? "ok"}`);
    return;
  }
  setStatus(`Takeover activate failed: ${result?.error ?? "error"}`);
});

takeoverStep.addEventListener("click", async () => {
  const msg = takeoverMessage.value.trim();
  if (!msg) {
    setStatus("Enter takeover message first");
    return;
  }
  const result = await chrome.runtime.sendMessage({ type: "takeover-step", message: msg, task: msg });
  if (result?.ok) {
    const response = result.result?.final_response || "";
    setStatus(response ? `Takeover: ${response}` : `Takeover action: ${result.result?.action ?? "ok"}`);
    return;
  }
  setStatus(`Takeover step failed: ${result?.error ?? "error"}`);
});

takeoverStop.addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({ type: "takeover-stop" });
  if (result?.ok) {
    setStatus(`Takeover: ${result.result?.action ?? "stopped"}`);
    return;
  }
  setStatus(`Takeover stop failed: ${result?.error ?? "error"}`);
});

takeoverState.addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({ type: "takeover-state" });
  if (result?.ok) {
    const state = result.state || {};
    setStatus(`Takeover state: ${state.active ? "active" : "inactive"} (${state.mode ?? "unknown"})`);
    return;
  }
  setStatus(`Takeover state failed: ${result?.error ?? "error"}`);
});

openOptions.addEventListener("click", () => {
  void chrome.runtime.openOptionsPage();
});

void init();
