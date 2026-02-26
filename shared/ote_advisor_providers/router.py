from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .registry import get_provider, list_provider_metadata, resolve_fallback_chain

DEFAULT_REQUIRED_CATEGORY_ROUTE: dict[str, str] = {
    "global": "openai",
    "china": "deepseek",
    "custom": "local_ollama",
}


def _now() -> datetime:
    return datetime.now(tz=UTC)


def route_key(provider_id: str, model: str | None) -> str:
    return f"{provider_id.strip().lower()}::{(model or '').strip().lower()}"


def _provider_category(provider_id: str) -> str:
    adapter = get_provider(provider_id)
    if adapter is None:
        return "unknown"
    return str(adapter.metadata.provider_category or "unknown").strip().lower()


def _default_model(provider_id: str) -> str | None:
    adapter = get_provider(provider_id)
    if adapter is None:
        return None
    models = list(adapter.metadata.default_models or [])
    return models[0] if models else None


def _provider_requires_key(provider_id: str) -> bool:
    adapter = get_provider(provider_id)
    if adapter is None:
        return False
    return not bool(adapter.metadata.key_optional)


def _default_api_key_ref(provider_id: str) -> str | None:
    if not _provider_requires_key(provider_id):
        return None
    return f"advisor-{provider_id.strip().lower()}"


def normalize_route(route: dict[str, Any], *, priority: int) -> dict[str, Any] | None:
    provider_id = str(route.get("provider_id") or "").strip().lower()
    if not provider_id:
        return None
    adapter = get_provider(provider_id)
    if adapter is None:
        return None
    default_base_url = str(adapter.metadata.default_base_url or "").strip() or None
    default_region_hint = str(adapter.metadata.region_hint or "").strip() or None
    key_ref = str(route.get("api_key_ref") or "").strip() or _default_api_key_ref(provider_id)
    model = str(route.get("model") or "").strip() or _default_model(provider_id)
    return {
        "provider_id": provider_id,
        "model": model,
        "api_key_ref": key_ref,
        "base_url": str(route.get("base_url") or "").strip() or default_base_url,
        "api_version": str(route.get("api_version") or "").strip() or None,
        "region_hint": str(route.get("region_hint") or "").strip() or default_region_hint,
        "priority": int(route.get("priority") if route.get("priority") is not None else priority),
        "provider_category": _provider_category(provider_id),
    }


def normalize_routes(routes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(routes or []):
        row = normalize_route(raw if isinstance(raw, dict) else {}, priority=idx)
        if row is None:
            continue
        key = route_key(str(row["provider_id"]), row.get("model"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda item: int(item.get("priority", 0)))
    return out


def enforce_required_category_coverage(
    routes: list[dict[str, Any]],
    *,
    required_categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    required = [str(item).strip().lower() for item in (required_categories or ["global", "china", "custom"]) if str(item).strip()]
    if not required:
        return normalize_routes(routes)
    normalized = normalize_routes(routes)
    return normalized


def routes_from_legacy_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    primary = str(config.get("advisor_primary_provider") or "").strip().lower()
    model = str(config.get("advisor_primary_model") or "").strip() or None
    fallback_chain_raw = config.get("advisor_fallback_chain")
    fallback_chain: list[str] = []
    if isinstance(fallback_chain_raw, str):
        fallback_chain = [item.strip().lower() for item in fallback_chain_raw.split(",") if item.strip()]
    elif isinstance(fallback_chain_raw, list):
        fallback_chain = [str(item).strip().lower() for item in fallback_chain_raw if str(item).strip()]
    chain = resolve_fallback_chain(primary, fallback_chain)
    custom_base_url = str(config.get("advisor_custom_base_url") or "").strip() or None
    custom_model = str(config.get("advisor_custom_model") or "").strip() or None
    default_key_ref = str(config.get("advisor_custom_api_key_ref") or "").strip() or None
    routes: list[dict[str, Any]] = []
    for idx, provider_id in enumerate(chain):
        route_key_ref: str | None = None
        if idx == 0 and default_key_ref:
            route_key_ref = default_key_ref
        else:
            route_key_ref = _default_api_key_ref(provider_id)
        routes.append(
            {
                "provider_id": provider_id,
                "model": model if idx == 0 else None,
                "api_key_ref": route_key_ref,
                "base_url": custom_base_url if provider_id == "custom" else None,
                "api_version": str(config.get("api_version") or "").strip() or None,
                "region_hint": None,
                "priority": idx,
            }
        )
    if custom_model:
        for row in routes:
            if str(row.get("provider_id")) == "custom":
                row["model"] = custom_model
    return normalize_routes(routes)


def normalize_profile(profile: dict[str, Any], *, required_categories: list[str] | None = None) -> dict[str, Any]:
    profile_id = str(profile.get("profile_id") or "default").strip() or "default"
    scope = str(profile.get("scope") or "workspace").strip() or "workspace"
    routing_mode = str(profile.get("routing_mode") or "adaptive").strip() or "adaptive"
    failure_policy = str(profile.get("failure_policy") or "risk_aware_fail_safe").strip() or "risk_aware_fail_safe"
    routes = enforce_required_category_coverage(
        normalize_routes(profile.get("routes") if isinstance(profile.get("routes"), list) else []),
        required_categories=required_categories,
    )
    return {
        "profile_id": profile_id,
        "scope": scope,
        "routing_mode": routing_mode,
        "failure_policy": failure_policy,
        "routes": routes,
    }


def normalize_profile_bundle(
    bundle: dict[str, Any],
    *,
    legacy_config: dict[str, Any],
    required_categories: list[str] | None = None,
) -> dict[str, Any]:
    profiles_raw = bundle.get("profiles")
    profiles: list[dict[str, Any]]
    if isinstance(profiles_raw, list) and profiles_raw:
        profiles = [normalize_profile(item if isinstance(item, dict) else {}, required_categories=required_categories) for item in profiles_raw]
    else:
        profiles = [
            normalize_profile(
                {
                    "profile_id": str(bundle.get("active_profile_id") or legacy_config.get("profile_id") or "default"),
                    "scope": "workspace",
                    "routing_mode": str(legacy_config.get("routing_mode") or "adaptive"),
                    "failure_policy": str(legacy_config.get("failure_policy") or "risk_aware_fail_safe"),
                    "routes": routes_from_legacy_config(legacy_config),
                },
                required_categories=required_categories,
            )
        ]
    active = str(bundle.get("active_profile_id") or "").strip() or profiles[0]["profile_id"]
    if active not in {str(item.get("profile_id")) for item in profiles}:
        active = profiles[0]["profile_id"]
    return {
        "active_profile_id": active,
        "profiles": profiles,
        "updated_at": str(bundle.get("updated_at") or legacy_config.get("updated_at") or _now().isoformat()),
    }


def active_profile(bundle: dict[str, Any]) -> dict[str, Any]:
    active_id = str(bundle.get("active_profile_id") or "").strip()
    for profile in bundle.get("profiles", []):
        if str(profile.get("profile_id")) == active_id:
            return profile
    profiles = bundle.get("profiles", [])
    if profiles:
        return profiles[0]
    return normalize_profile({"profile_id": "default", "routes": []})


def find_route(profile: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
    target = str(provider_id or "").strip().lower()
    for route in profile.get("routes", []):
        if str(route.get("provider_id") or "").strip().lower() == target:
            return route
    return None


def resolve_chain_from_profile(
    profile: dict[str, Any],
    *,
    primary_provider: str,
    fallback_chain: list[str] | None,
) -> list[dict[str, Any]]:
    provider_chain = resolve_fallback_chain(primary_provider, fallback_chain)
    resolved: list[dict[str, Any]] = []
    for idx, provider_id in enumerate(provider_chain):
        route = find_route(profile, provider_id)
        if route is None:
            route = normalize_route({"provider_id": provider_id}, priority=idx)
        if route is not None:
            resolved.append(route)
    return normalize_routes(resolved)


def _default_health_entry(now: datetime) -> dict[str, Any]:
    return {
        "success_ewma": 0.80,
        "latency_ewma_ms": 350.0,
        "consecutive_failures": 0,
        "circuit_state": "closed",
        "half_open_successes": 0,
        "last_error": None,
        "last_updated": now.isoformat(),
    }


def update_health_state(
    health: dict[str, Any],
    *,
    route: dict[str, Any],
    ok: bool,
    latency_ms: int,
    error_code: str | None,
    open_failures: int,
    half_open_seconds: int,
    close_successes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    stamp = now or _now()
    key = route_key(str(route.get("provider_id") or ""), str(route.get("model") or ""))
    current = health.get(key) if isinstance(health.get(key), dict) else _default_health_entry(stamp)
    success_prev = float(current.get("success_ewma", 0.80))
    latency_prev = float(current.get("latency_ewma_ms", 350.0))
    alpha = 0.25
    current["success_ewma"] = alpha * (1.0 if ok else 0.0) + (1.0 - alpha) * success_prev
    current["latency_ewma_ms"] = alpha * max(1.0, float(latency_ms)) + (1.0 - alpha) * latency_prev
    current["last_updated"] = stamp.isoformat()
    if ok:
        current["consecutive_failures"] = 0
        if str(current.get("circuit_state") or "closed") == "half_open":
            current["half_open_successes"] = int(current.get("half_open_successes", 0)) + 1
            if int(current.get("half_open_successes", 0)) >= max(1, close_successes):
                current["circuit_state"] = "closed"
                current["half_open_successes"] = 0
        else:
            current["circuit_state"] = "closed"
            current["half_open_successes"] = 0
        current["last_error"] = None
    else:
        current["consecutive_failures"] = int(current.get("consecutive_failures", 0)) + 1
        current["half_open_successes"] = 0
        current["last_error"] = error_code or "provider_error"
        if int(current["consecutive_failures"]) >= max(1, open_failures):
            current["circuit_state"] = "open"
            current["open_until"] = (stamp + timedelta(seconds=max(5, half_open_seconds))).isoformat()
    if str(current.get("circuit_state") or "closed") == "open":
        open_until_raw = str(current.get("open_until") or "")
        if open_until_raw:
            try:
                open_until = datetime.fromisoformat(open_until_raw.replace("Z", "+00:00"))
                if stamp >= open_until:
                    current["circuit_state"] = "half_open"
                    current["half_open_successes"] = 0
            except ValueError:
                current["circuit_state"] = "half_open"
                current["half_open_successes"] = 0
    health[key] = current
    return health


def route_score(route: dict[str, Any], entry: dict[str, Any], *, now: datetime | None = None) -> float:
    stamp = now or _now()
    success = max(0.0, min(1.0, float(entry.get("success_ewma", 0.80))))
    latency_ms = max(1.0, float(entry.get("latency_ewma_ms", 350.0)))
    latency_norm = 1.0 / (1.0 + latency_ms / 1000.0)
    updated_raw = str(entry.get("last_updated") or "")
    freshness = 0.7
    if updated_raw:
        try:
            updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
            age = max(0.0, (stamp - updated).total_seconds())
            freshness = max(0.1, min(1.0, 1.0 - (age / 3600.0)))
        except ValueError:
            freshness = 0.7
    score = 0.45 * success + 0.35 * latency_norm + 0.20 * freshness
    priority = max(0, int(route.get("priority", 0)))
    score += max(0.0, 0.05 - (priority * 0.01))
    if str(entry.get("circuit_state") or "closed") == "open":
        score -= 0.30
    return score


def select_route(
    profile: dict[str, Any],
    *,
    health: dict[str, Any],
    total_budget_ms: int,
    min_remaining_ms: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    stamp = now or _now()
    ranked: list[dict[str, Any]] = []
    for route in profile.get("routes", []):
        key = route_key(str(route.get("provider_id") or ""), str(route.get("model") or ""))
        entry = health.get(key) if isinstance(health.get(key), dict) else _default_health_entry(stamp)
        score = route_score(route, entry, now=stamp)
        ranked.append(
            {
                "route": route,
                "health": entry,
                "score": score,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    selected = ranked[0]["route"] if ranked else None
    return {
        "selected": selected,
        "ordered": [item["route"] for item in ranked],
        "scores": [
            {
                "provider_id": str(item["route"].get("provider_id") or ""),
                "model": item["route"].get("model"),
                "score": round(float(item["score"]), 6),
                "circuit_state": str(item["health"].get("circuit_state") or "closed"),
            }
            for item in ranked
        ],
        "total_budget_ms": int(total_budget_ms),
        "min_remaining_ms": int(min_remaining_ms),
    }


def runtime_status(
    profile: dict[str, Any],
    *,
    health: dict[str, Any],
    total_budget_ms: int,
    attempt_timeout_ms: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    stamp = now or _now()
    rows: list[dict[str, Any]] = []
    for route in profile.get("routes", []):
        key = route_key(str(route.get("provider_id") or ""), str(route.get("model") or ""))
        entry = health.get(key) if isinstance(health.get(key), dict) else _default_health_entry(stamp)
        rows.append(
            {
                "provider_id": str(route.get("provider_id") or ""),
                "model": route.get("model"),
                "priority": int(route.get("priority", 0)),
                "provider_category": str(route.get("provider_category") or _provider_category(str(route.get("provider_id") or ""))),
                "score": round(route_score(route, entry, now=stamp), 6),
                "circuit_state": str(entry.get("circuit_state") or "closed"),
                "consecutive_failures": int(entry.get("consecutive_failures", 0)),
                "success_ewma": round(float(entry.get("success_ewma", 0.80)), 6),
                "latency_ewma_ms": round(float(entry.get("latency_ewma_ms", 350.0)), 3),
                "last_error": entry.get("last_error"),
                "last_updated": entry.get("last_updated"),
            }
        )
    rows.sort(key=lambda item: int(item.get("priority", 0)))
    coverage = sorted({str(item.get("provider_category") or "") for item in rows if str(item.get("provider_category") or "")})
    return {
        "profile_id": str(profile.get("profile_id") or "default"),
        "routing_mode": str(profile.get("routing_mode") or "adaptive"),
        "failure_policy": str(profile.get("failure_policy") or "risk_aware_fail_safe"),
        "category_coverage": coverage,
        "routes": rows,
        "advisor_total_budget_ms": int(total_budget_ms),
        "advisor_attempt_timeout_ms": int(attempt_timeout_ms),
    }


def required_provider_categories() -> list[str]:
    categories = {str(meta.provider_category or "").strip().lower() for meta in list_provider_metadata()}
    return sorted(item for item in categories if item)
