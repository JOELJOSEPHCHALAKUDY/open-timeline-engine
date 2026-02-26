export function hostFromUrl(url: string): string {
  return new URL(url).host.toLowerCase();
}

export function classifyDomainByUrl(url: string): string {
  const host = hostFromUrl(url);
  if (host.includes("github.com") || host.includes("gitlab.com")) {
    return "coding";
  }
  if (host.includes("stackoverflow.com") || host.includes("developer") || host.includes("docs")) {
    return "research";
  }
  return "research";
}

export function normalizeHost(input: string): string | null {
  const value = input.trim();
  if (!value) return null;
  const candidate = value.includes("://") ? value : `https://${value}`;
  try {
    return hostFromUrl(candidate);
  } catch {
    return null;
  }
}

export function parseAllowedSites(raw: string): string[] {
  const values = raw
    .split(/[\n,]/)
    .map((item) => normalizeHost(item))
    .filter((item): item is string => Boolean(item));
  return Array.from(new Set(values));
}
