import { getSettings, setSettings } from "./common";
import { parseAllowedSites } from "./helpers";

const apiUrlEl = document.getElementById("apiUrl") as HTMLInputElement;
const apiTokenEl = document.getElementById("apiToken") as HTMLInputElement;
const consumerIdEl = document.getElementById("consumerId") as HTMLInputElement;
const roleEl = document.getElementById("role") as HTMLSelectElement;
const workspaceIdEl = document.getElementById("workspaceId") as HTMLInputElement;
const userIdEl = document.getElementById("userId") as HTMLInputElement;
const sensitivityEl = document.getElementById("sensitivity") as HTMLInputElement;
const takeoverSessionIdEl = document.getElementById("takeoverSessionId") as HTMLInputElement;
const takeoverPersonaModeEl = document.getElementById("takeoverPersonaMode") as HTMLSelectElement;
const takeoverActivationModeDefaultEl = document.getElementById("takeoverActivationModeDefault") as HTMLSelectElement;
const allowedSitesEl = document.getElementById("allowedSites") as HTMLTextAreaElement;
const saveBtn = document.getElementById("save") as HTMLButtonElement;
const statusEl = document.getElementById("status") as HTMLDivElement;

async function init(): Promise<void> {
  const settings = await getSettings();
  apiUrlEl.value = settings.apiUrl;
  apiTokenEl.value = settings.apiToken;
  consumerIdEl.value = settings.consumerId;
  roleEl.value = settings.role;
  workspaceIdEl.value = settings.workspaceId;
  userIdEl.value = settings.userId;
  sensitivityEl.value = String(settings.defaultSensitivity);
  takeoverSessionIdEl.value = settings.takeoverSessionId;
  takeoverPersonaModeEl.value = settings.takeoverPersonaMode;
  takeoverActivationModeDefaultEl.value = settings.takeoverActivationModeDefault;
  allowedSitesEl.value = settings.allowedSites.join("\n");
}

saveBtn.addEventListener("click", async () => {
  const current = await getSettings();
  await setSettings({
    ...current,
    apiUrl: apiUrlEl.value.trim(),
    apiToken: apiTokenEl.value.trim(),
    consumerId: consumerIdEl.value.trim() || current.consumerId,
    role: roleEl.value as "user" | "executor" | "advisor",
    workspaceId: workspaceIdEl.value.trim() || current.workspaceId,
    userId: userIdEl.value.trim() || current.userId,
    defaultSensitivity: Number(sensitivityEl.value),
    takeoverSessionId: takeoverSessionIdEl.value.trim() || current.takeoverSessionId,
    takeoverPersonaMode: takeoverPersonaModeEl.value || current.takeoverPersonaMode,
    takeoverActivationModeDefault:
      (takeoverActivationModeDefaultEl.value as "suggest" | "takeover") || current.takeoverActivationModeDefault,
    allowedSites: parseAllowedSites(allowedSitesEl.value)
  });
  statusEl.textContent = "Saved settings";
});

void init();
