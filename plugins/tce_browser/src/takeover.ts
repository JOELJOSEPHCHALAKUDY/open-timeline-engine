export function personaActivationPhrase(personaMode: string): string {
  const mode = personaMode.trim().toLowerCase();
  if (mode === "naruto") {
    return "hey kurama take over";
  }
  if (mode === "shadow" || mode === "shadowmode" || mode === "shadow_mode") {
    return "hey beru take over";
  }
  return "hey advisor take over";
}

export function personaStopPhrase(personaMode: string): string {
  const mode = personaMode.trim().toLowerCase();
  if (mode === "naruto") {
    return "kurama stand down";
  }
  if (mode === "shadow" || mode === "shadowmode" || mode === "shadow_mode") {
    return "shadow stand down";
  }
  return "advisor stand down";
}

export function normalizeTakeoverMode(value: string): "suggest" | "takeover" {
  return value.trim().toLowerCase() === "suggest" ? "suggest" : "takeover";
}
