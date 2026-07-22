from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from tce_api import audit
from tce_api import main as api_main
from tce_api.auth import AuthContext
from tce_shared.events import AgentRole


def _db_stub() -> Session:
    return cast(Session, object())


def test_strict_workspace_access_checks_executor_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main.settings, "workspace_access_mode", "strict")
    received: dict[str, object] = {}

    def deny(_db, **kwargs):
        received.update(kwargs)
        return False

    monkeypatch.setattr(api_main, "workspace_access_allowed", deny)
    auth = AuthContext(
        consumer="bearer:codex-executor",
        mode="bearer",
        role=AgentRole.EXECUTOR,
        workspace_id="production",
        user_id="codex-executor",
        behavior_subject_id="human-a",
    )

    with pytest.raises(HTTPException) as raised:
        api_main._enforce_workspace_access(auth, _db_stub())

    assert raised.value.status_code == 403
    assert received["require_membership"] is True


def test_strict_workspace_access_fails_closed_on_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main.settings, "workspace_access_mode", "strict")

    def fail(_db, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(api_main, "workspace_access_allowed", fail)
    auth = AuthContext(
        consumer="bearer:codex-executor",
        mode="bearer",
        role=AgentRole.EXECUTOR,
        workspace_id="production",
        user_id="codex-executor",
    )

    with pytest.raises(HTTPException) as raised:
        api_main._enforce_workspace_access(auth, _db_stub())

    assert raised.value.status_code == 503


def test_strict_workspace_access_requires_behavior_subject_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main.settings, "workspace_access_mode", "strict")
    monkeypatch.setattr(api_main.settings, "behavior_subject_bindings", "")
    monkeypatch.setattr(api_main, "workspace_access_allowed", lambda _db, **_kwargs: True)
    auth = AuthContext(
        consumer="bearer:codex-executor",
        mode="bearer",
        role=AgentRole.EXECUTOR,
        workspace_id="production",
        user_id="codex-executor",
        behavior_subject_id="human-a",
    )

    with pytest.raises(HTTPException) as raised:
        api_main._enforce_workspace_access(auth, _db_stub())
    assert raised.value.status_code == 403
    assert raised.value.detail == "behavior subject access denied"

    monkeypatch.setattr(
        api_main.settings,
        "behavior_subject_bindings",
        "codex-executor=human-a|human-b",
    )
    api_main._enforce_workspace_access(auth, _db_stub())


def test_durable_audit_write_is_synchronous_and_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit, "get_settings", lambda: SimpleNamespace(audit_write_mode="durable"))

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "_write_audit_sync", fail)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        audit.write_audit_log(
            _db_stub(),
            consumer="codex-executor",
            action="search",
            query={},
            result_event_ids=[],
            policy_decisions={},
            latency_ms=1,
        )
