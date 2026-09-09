"""Host-capture capability enforcement on the Full API, without Postgres.

The trusted capture channel (``POST /v1/inputs``) must be reachable only with
a host-capture credential. The capability is derived from the bearer token
alone -- never from ``X-TCE-Role`` or any other caller-asserted header -- and
an executor can neither post human input nor forge a human-origin event
through ``/v1/events``.

Storage is stubbed at the function boundary (``_store_event``, the receipt
store, the queue, the audit log); the auth path and the route's own guards
run for real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tce_api import auth as api_auth
from tce_api import config as api_config
from tce_api import main as api_main
from tce_shared.decision_capture import compute_delivery_key
from tce_shared.events import AgentRole
from tce_shared.identity import credential_fingerprint

_EXEC_TOKEN = "exec-token"
_OPERATOR_TOKEN = "operator-token"
_HOST_TOKEN = "host-token"
_FILE_HOST_TOKEN = "file-host-token"
_HUMAN = "human-1"
_WORKSPACE = "personal"
_EVENT_ID = UUID("0b6c7a0e-5e0a-4a7c-9a0e-7f7f6a1f2d3c")


class _FakeSession:
    """Any un-stubbed SQL is a test failure surface: the guards under test must run before storage."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("fake session: unexpected SQL")

    def add(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("fake session: unexpected ORM write")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass
class _Recorder:
    store_event_calls: list[tuple[Any, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[tuple[Any, ...]] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    session: _FakeSession = field(default_factory=_FakeSession)


@dataclass
class _Harness:
    client: TestClient
    recorder: _Recorder
    settings: api_config.Settings


def _settings(**overrides: Any) -> api_config.Settings:
    values: dict[str, Any] = {
        "api_tokens": _EXEC_TOKEN,
        "host_capture_tokens": _HOST_TOKEN,
        "allow_default_token": False,
        "auth_mode": "dual",
        "identity_claims_mode": "compat",
        "identity_claims_json": "{}",
        "workspace_access_mode": "compat",
        "security_encryption_secret": "test-secret",
        "security_encrypt_sensitive_required": True,
    }
    values.update(overrides)
    return api_config.Settings(**values)


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    yield from _build(monkeypatch, _settings())


def _build(monkeypatch: pytest.MonkeyPatch, settings: api_config.Settings, *, stub_store_event: bool = True) -> Iterator[_Harness]:
    recorder = _Recorder()
    monkeypatch.delenv("TCE_SECURITY_ENCRYPTION_SECRET", raising=False)
    monkeypatch.delenv("TCE_EXTRA_TOKENS", raising=False)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(api_main, "settings", settings)
    monkeypatch.setattr(api_auth, "get_settings", lambda: settings)
    # Keyring-provisioned tokens must not leak into the test's credential universe.
    monkeypatch.setattr(api_auth, "resolve_active_tokens", lambda tokens: set(tokens))

    def _store_event(db: Any, event: Any, auth: Any) -> UUID:
        recorder.store_event_calls.append((event, auth))
        return _EVENT_ID

    def _insert_receipt(db: Any, **kwargs: Any) -> None:
        recorder.receipts.append(dict(kwargs))

    def _enqueue(*args: Any, **_kwargs: Any) -> str:
        recorder.jobs.append(tuple(args))
        return "job-1"

    def _audit(db: Any, **kwargs: Any) -> None:
        recorder.audits.append(dict(kwargs))

    if stub_store_event:
        monkeypatch.setattr(api_main, "_store_event", _store_event)
    monkeypatch.setattr(api_main, "_ingest_source_allowed", lambda _source: True)
    monkeypatch.setattr(api_main, "insert_receipt", _insert_receipt)
    monkeypatch.setattr(api_main, "get_receipt_by_delivery_key", lambda *_a, **_k: None)
    monkeypatch.setattr(api_main, "set_receipt_queue_state", lambda *_a, **_k: None)
    monkeypatch.setattr(api_main, "enqueue_job", _enqueue)
    monkeypatch.setattr(api_main, "write_audit_log", _audit)
    monkeypatch.setattr(api_main, "_capture_delivery_state_for", lambda *_a, **_k: "healthy")
    api_main.app.dependency_overrides[api_main.get_db] = lambda: recorder.session
    try:
        yield _Harness(client=TestClient(api_main.app), recorder=recorder, settings=settings)
    finally:
        api_main.app.dependency_overrides.pop(api_main.get_db, None)


def _executor_headers(*, role: str | None = "executor", consumer: str = "codex-executor", token: str = _EXEC_TOKEN) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": consumer,
        "X-TCE-User": consumer if role != "user" else _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }
    if role is not None:
        headers["X-TCE-Role"] = role
    return headers


def _host_headers(*, token: str = _HOST_TOKEN, subject: str = _HUMAN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": "host-capture-claude",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": subject,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _capture_body(content: str = "confirm") -> dict[str, Any]:
    host_session_id = str(uuid.uuid4())
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "session_id": host_session_id,
        "delivery_key": compute_delivery_key(host_session_id, "p1", content_sha256),
        "content_sha256": content_sha256,
        "content": content,
        "origin_kind": "human_input",
        "observed_at": datetime.now(tz=UTC).isoformat(),
        "original_char_count": len(content),
        "prompt_id": "p1",
        "hook_event_name": "UserPromptSubmit",
        "host_client": "claude",
        "cwd": "/work/open-timeline-engine",
        "project_hint": {"project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"},
    }


def _human_event(**context: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": context.pop("source", "tce-host-capture"),
        "domain": "coding",
        "task_type": context.pop("task_type", "human_input"),
        "event_type": "TASK_STEP",
        "title": "confirm",
        "payload": {"input_excerpt": "confirm"},
        "context": context,
        "tags": ["human_input"],
        "sensitivity": 2,
    }


# --------------------------------------------------------------------------- /v1/inputs authentication


def test_missing_bearer_is_401(harness: _Harness) -> None:
    response = harness.client.post("/v1/inputs", json=_capture_body())
    assert response.status_code == 401, response.text
    assert harness.recorder.store_event_calls == []


def test_executor_token_cannot_capture(harness: _Harness) -> None:
    response = harness.client.post("/v1/inputs", json=_capture_body(), headers=_executor_headers())
    assert response.status_code == 403, response.text
    assert "host capture capability required" in response.json()["detail"]
    assert harness.recorder.store_event_calls == []
    assert harness.recorder.receipts == []


def test_role_header_cannot_mint_the_capability(harness: _Harness) -> None:
    asserted = harness.client.post("/v1/inputs", json=_capture_body(), headers=_executor_headers(role="user"))
    assert asserted.status_code == 403, asserted.text
    impostor = harness.client.post("/v1/inputs", json=_capture_body(), headers=_executor_headers(role=None, consumer="host-capture-claude"))
    assert impostor.status_code == 403, impostor.text
    assert harness.recorder.store_event_calls == []


def test_token_configured_in_both_sets_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for h in _build(monkeypatch, _settings(api_tokens=f"{_EXEC_TOKEN},{_HOST_TOKEN}")):
        response = h.client.post("/v1/inputs", json=_capture_body(), headers=_host_headers())
        assert response.status_code == 403, response.text
        assert "both" in response.json()["detail"].lower()
        assert h.recorder.store_event_calls == []


def test_default_host_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for h in _build(monkeypatch, _settings(host_capture_tokens="local-dev-token")):
        response = h.client.post("/v1/inputs", json=_capture_body(), headers=_host_headers(token="local-dev-token"))
        assert response.status_code == 403, response.text
        assert "default token" in response.json()["detail"].lower()
        assert h.recorder.store_event_calls == []


def test_host_capture_requires_the_encryption_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for h in _build(monkeypatch, _settings(security_encryption_secret="")):
        response = h.client.post("/v1/inputs", json=_capture_body(), headers=_host_headers())
        assert response.status_code == 503, response.text
        assert "TCE_SECURITY_ENCRYPTION_SECRET" in response.json()["detail"]
        assert h.recorder.store_event_calls == []
        assert h.recorder.receipts == []


def test_host_may_not_attest_human_input_for_another_subject(harness: _Harness) -> None:
    response = harness.client.post("/v1/inputs", json=_capture_body(), headers=_host_headers(subject="human-2"))
    assert response.status_code == 403, response.text
    assert "human subject" in response.json()["detail"]
    assert harness.recorder.store_event_calls == []


# --------------------------------------------------------------------------- /v1/inputs happy path


def test_host_token_captures_human_input_as_a_host_principal(harness: _Harness) -> None:
    body = _capture_body("api_key=abc123 then confirm")
    response = harness.client.post("/v1/inputs", json=body, headers=_host_headers())
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["deduplicated"] is False
    assert receipt["capture_principal"] == "host:host-capture-claude"
    assert receipt["origin_kind"] == "human_input"
    assert receipt["delivery_key"] == body["delivery_key"]
    assert receipt["content_sha256"] == body["content_sha256"]
    assert receipt["extraction_state"] == "pending"
    assert receipt["queue_state"] == "queued"
    assert receipt["capture_delivery_state"] == "healthy"
    assert str(receipt["event_id"]) == str(_EVENT_ID)

    assert len(harness.recorder.store_event_calls) == 1
    event, auth = harness.recorder.store_event_calls[0]
    assert event.task_type == "human_input"
    assert event.source == "tce-host-capture"
    assert event.actor == "user"
    assert event.idempotency_key == body["delivery_key"]
    assert event.context["capture_principal"].startswith("host:")
    assert event.context["input_origin"] == "human_input"
    assert event.context["host_session_id"] == body["session_id"]
    # The server re-redacts: the raw secret never reaches storage even though the hook is trusted.
    assert "abc123" not in event.payload["input_excerpt"]
    assert "<REDACTED:API_KEY>" in event.payload["input_excerpt"]
    assert event.payload["input_sha256"] == body["content_sha256"]
    assert event.payload["live_capture"] is True
    assert "host_capture" in auth.capabilities
    assert auth.role.value == "user"

    assert len(harness.recorder.receipts) == 1
    stored = harness.recorder.receipts[0]
    assert stored["subject_user_id"] == _HUMAN
    assert stored["owner_id"] == _HUMAN
    assert stored["capture_principal"] == "host:host-capture-claude"
    assert stored["origin_kind"] == "human_input"
    assert stored["event_id"] == _EVENT_ID
    assert str(stored["receipt_id"]) == str(receipt["receipt_id"])
    assert harness.recorder.jobs == [("tce_worker.jobs.decision_extraction.run", str(receipt["receipt_id"]))]
    assert harness.recorder.audits and harness.recorder.audits[-1]["action"] == "capture_input"
    assert "host_capture" in harness.recorder.audits[-1]["policy_decisions"]["capabilities"]
    assert harness.recorder.session.commits >= 1


# --------------------------------------------------------------------------- /v1/events forgery (real _store_event guard)


def test_executor_cannot_forge_human_origin_events(monkeypatch: pytest.MonkeyPatch) -> None:
    for h in _build(monkeypatch, _settings(), stub_store_event=False):
        forged = {
            "task_type": _human_event(source="cli"),
            "source": _human_event(task_type="debug"),
            "input_origin": _human_event(source="cli", task_type="debug", input_origin="human_input"),
            "capture_principal": _human_event(source="cli", task_type="debug", capture_principal="host:host-capture-claude"),
        }
        for name, event in forged.items():
            response = h.client.post("/v1/events", json=event, headers=_executor_headers())
            assert response.status_code == 403, f"{name}: {response.status_code} {response.text}"
            assert "host capture capability" in response.json()["detail"], name
            asserted = h.client.post("/v1/events", json=event, headers=_executor_headers(role="user"))
            assert asserted.status_code == 403, f"{name} with X-TCE-Role=user: {asserted.status_code} {asserted.text}"
        # The guard fires before any storage: the fake session would have raised (500) otherwise.
        assert h.recorder.session.commits == 0
        assert h.recorder.audits == []


# --------------------------------------------------------------------------- identity is credential-bound


def _auth_context(
    settings: api_config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str,
    consumer: str | None = None,
    role: str | None = None,
    user: str | None = None,
    workspace: str | None = None,
    subject: str | None = None,
) -> api_auth.AuthContext:
    """Resolve an AuthContext the way the dependency would, without a route in the way.

    Every header parameter is passed explicitly: an omitted one would arrive as FastAPI's ``Header``
    sentinel, which is truthy, and would silently send the call down the mTLS branch.
    """
    monkeypatch.setattr(api_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(api_auth, "resolve_active_tokens", lambda tokens: set(tokens))
    request = Request({"type": "http", "method": "POST", "path": "/v1/inputs", "headers": [], "query_string": b""})
    return asyncio.run(
        api_auth.get_auth_context(
            request,
            authorization=f"Bearer {token}",
            x_mtls_subject=None,
            x_tce_consumer=consumer,
            x_tce_role=role,
            x_tce_workspace=workspace,
            x_tce_user=user,
            x_tce_behavior_subject=subject,
        )
    )


def _claims(**overrides: Any) -> str:
    claim: dict[str, Any] = {
        "consumer": "operator-ui",
        "role": "user",
        "workspace_id": _WORKSPACE,
        "user_id": _HUMAN,
        "behavior_subject_id": _HUMAN,
    }
    claim.update(overrides)
    return json.dumps({credential_fingerprint("bearer", _OPERATOR_TOKEN): claim})


def test_identity_verified_is_never_asserted_by_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """``X-TCE-Role: user`` on an executor's bearer makes ``is_human`` true and nothing more.

    Human-origin promotion (a human_resolutions row, a resolved shadow label, receipt-bound
    'explicit' evidence) is gated on ``identity_verified``, which only the server can grant.
    """
    settings = _settings(api_tokens=f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}", identity_claims_json=_claims())

    asserted = _auth_context(
        settings,
        monkeypatch,
        token=_EXEC_TOKEN,
        consumer="codex-executor",
        role="user",
        user=_HUMAN,
        workspace=_WORKSPACE,
        subject=_HUMAN,
    )
    assert asserted.role.value == "user", "the header is still honoured for role"
    assert asserted.identity_verified is False, "but a header-asserted role is not a verified identity"
    assert asserted.capabilities == frozenset()

    bound = _auth_context(
        settings,
        monkeypatch,
        token=_OPERATOR_TOKEN,
        consumer="codex-executor",
        role="executor",
        user="codex-executor",
        workspace="other",
        subject="human-2",
    )
    assert bound.identity_verified is True
    assert bound.role.value == "user", "the server-bound claim wins over the asserted headers"
    assert bound.user_id == _HUMAN
    assert bound.workspace_id == _WORKSPACE

    host = _auth_context(
        settings,
        monkeypatch,
        token=_HOST_TOKEN,
        consumer="host-capture-claude",
        user=_HUMAN,
        workspace=_WORKSPACE,
        subject=_HUMAN,
    )
    assert host.identity_verified is True
    assert "host_capture" in host.capabilities

    # A bound claim only carries host_capture to a human: no executor receives host attestation.
    executor_claim = _settings(
        api_tokens=f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}",
        identity_claims_json=_claims(role="executor", user_id="codex-executor", capabilities=["host_capture"]),
    )
    bound_executor = _auth_context(
        executor_claim,
        monkeypatch,
        token=_OPERATOR_TOKEN,
        consumer="codex-executor",
        workspace=_WORKSPACE,
    )
    assert bound_executor.role.value == "executor"
    assert bound_executor.capabilities == frozenset()


# --------------------------------------------------------------------------- the credential lives outside .env


def test_host_capture_tokens_file_unions_with_the_env_csv(tmp_path: Path) -> None:
    """The repo ``.env`` is mounted into the API container, so the host credential is kept in a
    0600 file outside the repo and mounted read-only; the env CSV stays supported for tests and
    existing installs."""
    token_file = tmp_path / "host_capture.token"
    token_file.write_text(f"# managed by scripts/install.sh\n\n{_FILE_HOST_TOKEN}\n", encoding="utf-8")

    both = _settings(host_capture_tokens=_HOST_TOKEN, host_capture_tokens_file=str(token_file))
    assert both.host_capture_token_set == {_HOST_TOKEN, _FILE_HOST_TOKEN}

    file_only = _settings(host_capture_tokens="", host_capture_tokens_file=str(token_file))
    assert file_only.host_capture_token_set == {_FILE_HOST_TOKEN}

    env_only = _settings(host_capture_tokens=_HOST_TOKEN, host_capture_tokens_file="")
    assert env_only.host_capture_token_set == {_HOST_TOKEN}

    # An absent or unreadable file degrades to the env CSV instead of failing every request.
    missing = _settings(host_capture_tokens=_HOST_TOKEN, host_capture_tokens_file=str(tmp_path / "nope.token"))
    assert missing.host_capture_token_set == {_HOST_TOKEN}
    unreadable = _settings(host_capture_tokens=_HOST_TOKEN, host_capture_tokens_file=str(tmp_path))
    assert unreadable.host_capture_token_set == {_HOST_TOKEN}


def test_file_provisioned_host_token_captures_human_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "host_capture.token"
    token_file.write_text(f"{_FILE_HOST_TOKEN}\n", encoding="utf-8")
    settings = _settings(host_capture_tokens="", host_capture_tokens_file=str(token_file))
    for h in _build(monkeypatch, settings):
        response = h.client.post("/v1/inputs", json=_capture_body(), headers=_host_headers(token=_FILE_HOST_TOKEN))
        assert response.status_code == 201, response.text
        assert response.json()["capture_principal"] == "host:host-capture-claude"
        _, auth = h.recorder.store_event_calls[0]
        assert "host_capture" in auth.capabilities
        assert auth.identity_verified is True
        # The file is a host-capture source only: it does not add an api token, so the env-configured
        # executor credential is still the only one that can act as an executor.
        assert settings.token_set == {_EXEC_TOKEN}
        assert _FILE_HOST_TOKEN not in settings.token_set


# --------------------------------------------------------------------------- memberships must not lock the human out


class _RecordingSession:
    """Records SQL text/params; ``begin_nested`` is a no-op savepoint."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def begin_nested(self) -> Iterator[None]:
        yield

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(statement), dict(params or {})))
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _db(session: _RecordingSession) -> Session:
    """The functions under test only commit/rollback and issue one statement; the recorder stands in."""
    return cast(Session, session)


def _ctx(role: AgentRole, *, user_id: str, subject: str = _HUMAN, capabilities: frozenset[str] = frozenset()) -> api_auth.AuthContext:
    return api_auth.AuthContext(
        consumer="test",
        mode="bearer",
        role=role,
        workspace_id=_WORKSPACE,
        user_id=user_id,
        behavior_subject_id=subject,
        capabilities=capabilities,
    )


def test_host_capture_principal_is_not_gated_on_team_memberships(monkeypatch: pytest.MonkeyPatch) -> None:
    """``index_event_graph`` seeds team_memberships with the executor on the first stored event; from
    then on a membership check denies every non-member USER -- including the host adapter attesting
    the human's own keystrokes. In compat mode credential-bound principals are exempt."""
    settings = _settings()
    monkeypatch.setattr(api_main, "settings", settings)
    monkeypatch.setattr(api_main, "workspace_access_allowed", lambda *_a, **_k: False)
    session = _RecordingSession()

    api_main._enforce_workspace_access(_ctx(AgentRole.USER, user_id=_HUMAN, capabilities=frozenset({"host_capture"})), _db(session))
    api_main._enforce_workspace_access(_ctx(AgentRole.EXECUTOR, user_id="codex-executor"), _db(session))
    assert session.executed == [], "the exempt principals must not even reach the membership query"

    with pytest.raises(HTTPException) as denied:
        api_main._enforce_workspace_access(_ctx(AgentRole.USER, user_id=_HUMAN), _db(session))
    assert denied.value.status_code == 403

    # Strict mode gates everyone, the host adapter included.
    strict = _settings(workspace_access_mode="strict")
    monkeypatch.setattr(api_main, "settings", strict)
    with pytest.raises(HTTPException) as strict_denied:
        api_main._enforce_workspace_access(_ctx(AgentRole.USER, user_id=_HUMAN, capabilities=frozenset({"host_capture"})), _db(session))
    assert strict_denied.value.status_code == 403


def test_storing_an_event_keeps_the_behavior_subject_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    monkeypatch.setattr(api_main, "settings", settings)

    session = _RecordingSession()
    api_main._seed_behavior_subject_membership(_db(session), _ctx(AgentRole.EXECUTOR, user_id="codex-executor", subject=_HUMAN))
    assert len(session.executed) == 1, session.executed
    statement, params = session.executed[0]
    assert "team_memberships" in statement
    assert params["user_id"] == _HUMAN
    assert params["added_by"] == "codex-executor"
    assert params["workspace_id"] == _WORKSPACE
    assert session.commits == 1

    # Nothing to seed when the executor is the subject, and strict mode stays explicit.
    same = _RecordingSession()
    api_main._seed_behavior_subject_membership(_db(same), _ctx(AgentRole.EXECUTOR, user_id=_HUMAN, subject=_HUMAN))
    assert same.executed == []
    monkeypatch.setattr(api_main, "settings", _settings(workspace_access_mode="strict"))
    strict = _RecordingSession()
    api_main._seed_behavior_subject_membership(_db(strict), _ctx(AgentRole.EXECUTOR, user_id="codex-executor", subject=_HUMAN))
    assert strict.executed == []
