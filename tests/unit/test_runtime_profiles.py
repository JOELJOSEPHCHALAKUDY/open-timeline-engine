from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
PROFILE_DIR = ROOT / "config" / "profiles"


def _profile(name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in (PROFILE_DIR / f"{name}.env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid profile line: {raw_line}"
        assert key not in values, f"duplicate profile key: {key}"
        values[key] = value
    return values


def test_profiles_are_named_and_never_contain_secrets() -> None:
    for name in ("local-lite", "local-full", "research", "team-secure"):
        profile = _profile(name)
        assert profile["TCE_RUNTIME_PROFILE"] == name
        assert "TCE_API_TOKEN" not in profile
        assert "TCE_API_TOKENS" not in profile
        assert not any(key.endswith("_API_KEY") for key in profile)
        assert profile["TCE_CORS_ALLOW_ORIGINS"] != "*"
        assert profile["TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED"] == "false"


def test_team_profile_is_fail_closed() -> None:
    profile = _profile("team-secure")

    assert profile["TCE_AUTH_MODE"] == "bearer"
    assert profile["TCE_IDENTITY_CLAIMS_MODE"] == "enforce"
    assert profile["TCE_WORKSPACE_ACCESS_MODE"] == "strict"
    assert profile["TCE_AUDIT_WRITE_MODE"] == "durable"
    assert profile["TCE_CORS_ALLOW_ORIGINS"] == ""
    assert profile["TCE_BEHAVIOR_STORAGE_GATE_MODE"] == "enforce"


def test_research_profile_collects_without_unlocking_autonomy() -> None:
    profile = _profile("research")

    assert profile["TCE_BEHAVIOR_PROJECTION_PILOT_ENABLED"] == "true"
    assert profile["TCE_BEHAVIOR_SHADOW_EVALUATION_ENABLED"] == "true"
    assert profile["TCE_BEHAVIOR_AUTONOMY_GATE_ENABLED"] == "false"
