"""Privileged endpoints must refuse to run on a default/insecure API token.

The docker-socket stack restart and the .env-writing config endpoints are
effective host access; combined with the well-known default token they were
a drive-by takeover path. TCE_ALLOW_DEFAULT_TOKEN=1 restores the old
behavior for explicitly-dev setups.
"""

import pytest
from fastapi.testclient import TestClient
from tce_api import config as api_config

HEADERS = {
    "Authorization": "Bearer local-dev-token",
    "X-TCE-Consumer": "dashboard",
    "X-TCE-Role": "executor",
    "X-TCE-Workspace": "personal",
    "X-TCE-User": "u1",
}


@pytest.fixture()
def default_token_env(monkeypatch: pytest.MonkeyPatch):
    # Isolate by overriding get_settings in the main module's namespace rather
    # than clearing the shared lru_cache — clearing it breaks the settings
    # object-identity that other integration tests rely on.
    from tce_api import main as api_main

    default_settings = api_config.Settings(
        api_tokens="local-dev-token", allow_default_token=False
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: default_settings)
    yield


def test_cors_default_is_not_wildcard() -> None:
    # Assert on the class-level default so the check is env-independent.
    default_origins = str(api_config.Settings.model_fields["cors_allow_origins"].default)
    assert default_origins != "*"
    assert "http://localhost:4200" in default_origins


def test_reject_helper_blocks_default_token() -> None:
    from fastapi import HTTPException
    from tce_api.main import _reject_privileged_default_token

    # Explicit constructor kwargs take precedence over any .env values.
    settings = api_config.Settings(api_tokens="local-dev-token", allow_default_token=False)
    with pytest.raises(HTTPException) as exc:
        _reject_privileged_default_token(settings)
    assert exc.value.status_code == 403

    ok = api_config.Settings(api_tokens="a-real-token", allow_default_token=False)
    _reject_privileged_default_token(ok)

    allowed = api_config.Settings(api_tokens="local-dev-token", allow_default_token=True)
    _reject_privileged_default_token(allowed)


def test_stack_restart_rejects_default_token(
    default_token_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tce_api import main as api_main

    # Safety net: the restart job must never launch from this test.
    def _boom(**_kwargs: object) -> str:
        raise AssertionError("stack restart job must not start under default token")

    monkeypatch.setattr(api_main, "_start_stack_restart_job", _boom)

    client = TestClient(api_main.app)
    resp = client.post(
        "/v1/dashboard/stack/restart", headers=HEADERS, json={"stack": "full"}
    )
    assert resp.status_code == 403
    assert "default API token" in resp.json()["detail"]


def test_executor_config_write_rejects_default_token(
    default_token_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tce_api import main as api_main

    # Safety net: the real .env must never be written from this test.
    def _no_write(_updates: dict[str, str]) -> None:
        raise AssertionError(".env write must not happen under default token")

    monkeypatch.setattr(api_main, "_upsert_env_values", _no_write)

    client = TestClient(api_main.app)
    resp = client.put(
        "/v1/dashboard/client-config/executor",
        headers=HEADERS,
        json={},
    )
    assert resp.status_code == 403
    assert "default API token" in resp.json()["detail"]
