# Settings Page Redesign

## Problem

The current settings page has 7 stacked cards with engineer-facing labels, stale data from sessionStorage caching, and no per-section save/restart flow. Users cannot effectively configure advisor LLM providers or executor identities.

## Design

Collapse 7 cards into **3 sections**, each with its own **Apply + Restart** flow. All fields remain visible with improved labels. Fix data loading to show actual server state.

---

## Section 1: System

Merges old "Connection", "Server defaults", and "Restart" cards.

| Field | Label | Type | Behavior |
|-------|-------|------|----------|
| Runtime mode | **Runtime mode** | Dropdown (`timeline_only` / `clone_advisor`) + Apply | Instant apply, no restart |
| API health | **API connection** | Badge + Test button | Shows Connected/Error/Not tested |
| API base | **API base** | Read-only text | Server-reported |
| Workspace | **Default workspace** | Read-only text | Server-reported |
| Executor clients | **Active clients** | Read-only text | Server-reported |
| Stack restart | **Restart stack** | Dropdown (full/lite) + Restart button | Manual restart trigger |

**Buttons:** `Test connection` | `Refresh all`

---

## Section 2: Advisor Configuration

Numbered list of routes. Route #1 is primary, rest are fallback in priority order.

### Per-route fields

| Field | Label | Shown when |
|-------|-------|------------|
| Source | **Source** | Always — `Local` or `Web` toggle |
| Platform | **Platform** | Local mode — `Ollama` / `LM Studio` |
| Provider | **Provider** | Web mode — dropdown from registered providers |
| Model | **Model** | Always — dropdown if loaded, text input otherwise |
| API key | **API key** | Web providers that require it |
| Endpoint URL | **Endpoint URL** | Always — pre-filled from provider defaults |
| Key storage name | **Key storage name** | Web providers that require it |
| Pull model | **Pull model to Ollama** | Ollama routes only |

### Per-route buttons

`Load models` | `Verify` | `Pull to Ollama` (Ollama only) | `Remove` (fallback routes) | `Up` / `Down` (fallback routes)

### Section-level buttons

`+ Add route` | `Apply` | `Check all routes`

### Apply + Restart flow

Click **Apply** -> saves advisor config to .env -> yellow banner: "Config saved. Restart to apply?" -> `Restart` button inline.

---

## Section 3: Executor & Identity

Merges old "Executor + identity runtime config" and "Local overrides" cards.

### Executor config

| Field | Label |
|-------|-------|
| Workspace | **Workspace** |
| Executor clients | **Executor clients** |
| Executor user | **Executor user** |
| Executor consumer | **Executor consumer** |
| Advisor user | **Advisor user** |
| Advisor consumer | **Advisor consumer** |

**Buttons:** `Apply` -> saves to .env -> yellow "Restart to apply?" banner -> `Restart`

### Dashboard auth

| Field | Label |
|-------|-------|
| API token | **API token** |
| Dashboard consumer | **Dashboard consumer** |
| Dashboard role | **Dashboard role** |
| Dashboard user | **Dashboard user** |

**Buttons:** `Save auth` (local-only, no restart) | `Reset to defaults`

---

## Data Fixes

1. **Remove sessionStorage caching** that overwrites server values — `loadPendingExecutorConfig()` and `applyPendingAdvisorRoutes()` cause stale data to display.
2. **Always load from server** on page init. SessionStorage only used to persist values during an active restart (cleared immediately after restart completes).
3. **Fix normalizeRoute** clearing local route models (already fixed).
4. **Fix fallback API key saving** — pre-store each route's key before main config save (already fixed).

## Label Changes

| Old | New |
|-----|-----|
| Connection mode | Source |
| Base URL (override) | Endpoint URL |
| API key ref | Key storage name |
| Runtime model | Model |
| Load live models | Load models |
| Verify route | Verify |
| Runtime status + Probe routes | Check all routes |
| Executor + identity runtime config | Executor & Identity |
| Local overrides | Dashboard auth |
| Restart required workflow | (removed, integrated per-section) |
| Server defaults | (merged into System) |

## Architecture

Single file: `settings.component.ts` (standalone Angular component with inline template). No new files. No backend changes. No protected file modifications.

## Per-section restart flow

Each section that requires restart gets:
1. An `Apply` button that saves config
2. On successful save, a yellow inline banner: "Config saved. Restart stack to apply?"
3. A `Restart` button in the banner
4. After restart completes, banner disappears and section reloads fresh data from server
