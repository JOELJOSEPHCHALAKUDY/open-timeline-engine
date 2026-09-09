"""P1 trusted-capture review regressions, pinned against the Lite HTTP boundary.

Companion to ``test_trusted_evidence_lite.py`` (the exit gate). Every case here pins one
blocker/major from the P1 review so the defect cannot come back, and so the Full backend has a
behavioural twin to match. As in the exit-gate module, each case drives the real FastAPI app over
``TestClient`` with the real SQLite store; SQL only *observes* state.

Matrix (defect -> case):
  main.py:4087 (blocker, Full) the host-capture principal is locked out once the executor seeds
      team_memberships -> case 1. Lite already exempts credential-bound principals from the
      membership gate and seeds the behavior subject as a member; this pins both halves.
  main.py:8473 (major) a header-asserted USER is not an authenticated human -> cases 2 and 3.
  main.py:11017 (major) permit resolution must target the safety question, not the newest one -> case 4.
  main.py:15450 (major) human feedback with no opportunity_id must still resolve the subject's
      open question -> case 5.
  main.py:17139 (major) a resolved question must never be handed back as still open -> case 6.
      ``_nothing_left_to_do`` is Full-only; the Lite twin of "the session can move on" is that
      ``open_decision_opportunity_id`` stops naming the answered question.
  capture_store.py:545 (major) the sweep must expire a relayed-but-unlabelled opportunity, not
      only mark its shadow row missing_label -> case 7.
  install.sh:792 (major) the host credential must not live in the repo .env; the API reads it from
      ``host_capture_tokens_file`` as well as the env CSV -> case 8.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_shared.decision_capture import compute_delivery_key
from tce_shared.identity import credential_fingerprint
from tce_shared.redaction import redact_text

_EXEC_TOKEN = "exec-token"
_OPERATOR_TOKEN = "operator-token"
_HOST_TOKEN = "host-token"
_FILE_HOST_TOKEN = "file-host-token"
_HUMAN = "human-1"
_WORKSPACE = "personal"
_HOST_PRINCIPAL = "host:host-capture-claude"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "host_capture_tokens",
    "host_capture_tokens_file",
    "allow_default_token",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "scope_strict_tags",
    "capture_extraction_enabled",
    "capture_opportunity_ttl_seconds",
    "capture_delivery_stale_seconds",
    "capture_max_chars",
    "behavior_storage_gate_mode",
    "behavior_autonomy_gate_enabled",
    "behavior_shadow_evaluation_enabled",
    "takeover_needs_human_threshold_hot",
    "takeover_needs_human_threshold_cold",
    "takeover_autonomy_policy_default",
)

_BOUND_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_HIGH_RISK_TASK = "delete the build artifacts with rm -rf /tmp/build"
_HIGH_RISK_MESSAGE = "beru take over and delete the build artifacts with rm -rf /tmp/build"
_READ_ONLY_TASK = "summarize the failing unit tests and report back"
_SAFETY_PREFIX = "Safety pause: high-risk action detected"


# --------------------------------------------------------------------------- fixtures


def _snapshot_settings() -> dict[str, Any]:
    settings = get_settings()
    return {key: getattr(settings, key, _MISSING) for key in _GUARDED_SETTINGS}


def _restore_settings(snapshot: dict[str, Any]) -> None:
    settings = get_settings()
    for key, value in snapshot.items():
        if value is _MISSING:
            if hasattr(settings, key):
                try:
                    delattr(settings, key)
                except AttributeError:
                    pass
            continue
        setattr(settings, key, value)


@pytest.fixture()
def lite_client(tmp_path: Path) -> Iterator[TestClient]:
    """Compat identity mode, with ONE server-bound claim: the operator token.

    That is the point of the fixture. ``exec-token`` is unbound, so everything it asserts through
    ``X-TCE-*`` headers (including ``X-TCE-Role: user``) stays *unverified*; ``operator-token``
    carries a server-bound human claim and is therefore verified even in compat mode; the host
    credential is verified by construction. The three principals are otherwise identical callers.
    """
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "trusted-capture-regressions.db")
    settings.default_operation_mode = "clone_advisor"
    settings.workspace_access_mode = "compat"
    settings.behavior_storage_gate_mode = "shadow"
    settings.behavior_autonomy_gate_enabled = False
    settings.behavior_shadow_evaluation_enabled = True
    settings.capture_extraction_enabled = True
    settings.capture_opportunity_ttl_seconds = 3600
    settings.capture_delivery_stale_seconds = 21600
    settings.capture_max_chars = 2000
    settings.takeover_autonomy_policy_default = "human_consultative"
    settings.allow_default_token = False
    settings.api_tokens = f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}"
    settings.host_capture_tokens = _HOST_TOKEN
    settings.host_capture_tokens_file = ""
    settings.identity_claims_mode = "compat"
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _OPERATOR_TOKEN): {
                "consumer": "operator-ui",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


# --------------------------------------------------------------------------- headers


def _executor_headers(*, role: str | None = "executor", user: str | None = None, subject: str = _HUMAN) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_EXEC_TOKEN}",
        "X-TCE-Consumer": "codex-executor",
        "X-TCE-User": user or "codex-executor",
        "X-TCE-Behavior-Subject": subject,
        "X-TCE-Workspace": _WORKSPACE,
    }
    if role is not None:
        headers["X-TCE-Role"] = role
    return headers


def _asserted_operator_headers() -> dict[str, str]:
    """A human identity *asserted in headers* on the executor's own bearer: is_human, not verified."""
    return _executor_headers(role="user", user=_HUMAN)


def _verified_operator_headers() -> dict[str, str]:
    """A human identity the server bound to the credential: is_human AND identity_verified."""
    return {
        "Authorization": f"Bearer {_OPERATOR_TOKEN}",
        "X-TCE-Consumer": "operator-ui",
        "X-TCE-Role": "user",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _host_headers(*, token: str = _HOST_TOKEN) -> dict[str, str]:
    """Host-capture headers carry NO role header: the capability comes from the credential alone."""
    return {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": "host-capture-claude",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


# --------------------------------------------------------------------------- db observation


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().lite_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row:
    conn = _db()
    try:
        row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no row for {sql} {params}"
    return row


def _all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = _db()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _count(sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(_one(sql, params)[0])


def _opportunity(opportunity_id: str) -> sqlite3.Row:
    return _one("SELECT * FROM decision_opportunities WHERE id = ?", (opportunity_id,))


def _shadow_for_opportunity(opportunity_id: str) -> sqlite3.Row:
    return _one("SELECT * FROM behavior_shadow_predictions WHERE opportunity_id = ?", (opportunity_id,))


def _observation(observation_id: str) -> sqlite3.Row:
    return _one("SELECT * FROM decision_observations WHERE id = ?", (observation_id,))


def _assert_still_open_and_pending(opportunity_id: str) -> None:
    """The question is untouched: still open, its frozen prediction still unlabelled."""
    assert _opportunity(opportunity_id)["status"] == "open"
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["resolution_state"] == "pending", dict(shadow)
    assert str(shadow["actual_choice"] or "") == "", dict(shadow)
    assert shadow["correct"] is None, dict(shadow)
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,)) == 0


# --------------------------------------------------------------------------- takeover / capture helpers


def _session() -> str:
    return f"codex-{uuid.uuid4().hex[:8]}"


def _step(
    client: TestClient,
    session_id: str,
    headers: dict[str, str] | None = None,
    *,
    message: str = "beru take over",
    task: str | None = _HIGH_RISK_TASK,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "persona_mode": "shadow",
        "app_context": dict(_BOUND_APP_CONTEXT),
        "constraints": {"k": 4},
        "allow_fallback": True,
    }
    if task is not None:
        payload["task"] = task
    step = client.post("/v1/takeover/step", json=payload, headers=headers or _executor_headers())
    assert step.status_code == 200, step.text
    body: dict[str, Any] = step.json()
    return body


def _open_safety_question(client: TestClient, session_id: str, *, task: str | None = _HIGH_RISK_TASK, message: str = "beru take over") -> str:
    body = _step(client, session_id, message=message, task=task)
    assert body["safety_decision"] == "confirm_required", body
    assert str(body.get("final_response") or "").startswith(_SAFETY_PREFIX), body.get("final_response")
    opportunity_id = str(body.get("open_decision_opportunity_id") or "")
    assert opportunity_id, f"the safety question must be frozen as a decision opportunity: {body}"
    assert _opportunity(opportunity_id)["decision_family"] == "safety_confirmation"
    return opportunity_id


def _request_permit(client: TestClient, session_id: str, headers: dict[str, str], *, target: str) -> dict[str, Any]:
    permit = client.post(
        "/v1/takeover/permit",
        json={"session_id": session_id, "action_kind": "edit", "target_paths": [target], "estimated_change_size": 5},
        headers=headers,
    )
    assert permit.status_code == 200, permit.text
    body: dict[str, Any] = permit.json()
    return body


def _resolve_permit(client: TestClient, permit_id: str, headers: dict[str, str], *, approved: bool = True) -> httpx.Response:
    response: httpx.Response = client.post("/v1/takeover/permit/resolve", json={"permit_id": permit_id, "approved": approved}, headers=headers)
    return response


def _evidence_payload(index: int, *, choice: str = "confirm") -> dict[str, Any]:
    return {
        "situation_type": "approval_requested",
        "situation_summary": f"Safety pause: high-risk action detected (destructive_filesystem) {index}",
        "objective": "delete the build artifacts safely",
        "context_snapshot": {"safety_reason": "destructive_filesystem"},
        "constraints": {"risk": "filesystem"},
        "available_choices": ["confirm", "abort"],
        "selected_choice": choice,
        "rationale": "The target is a disposable build directory and the command is scoped",
        "action_taken": "confirmed the scoped delete",
        "outcome": "build dir removed",
        "outcome_sentiment": "positive",
        "memory_class": "decision",
        "evidence_source": "explicit",
        "confidence": 0.95,
    }


def _seed_prior_evidence(client: TestClient, count: int = 3) -> None:
    """Eligible human evidence so the frozen prediction is non-abstained."""
    for index in range(count):
        response = client.post("/v1/behavior/evidence", json=_evidence_payload(index), headers=_verified_operator_headers())
        assert response.status_code == 200, response.text
        assert response.json()["learning_eligible"] is True, response.json()


def _capture_body(content: str, *, prompt_id: str = "p1", host_session_id: str | None = None) -> dict[str, Any]:
    host_session_id = host_session_id or str(uuid.uuid4())
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    redacted, applied = redact_text(content)
    return {
        "session_id": host_session_id,
        "delivery_key": compute_delivery_key(host_session_id, prompt_id, content_sha256),
        "content_sha256": content_sha256,
        "content": redacted,
        "origin_kind": "human_input",
        "observed_at": datetime.now(tz=UTC).isoformat(),
        "original_char_count": len(content),
        "content_truncated": False,
        "redaction_applied": applied,
        "prompt_id": prompt_id,
        "hook_event_name": "UserPromptSubmit",
        "host_client": "claude",
        "cwd": "/work/open-timeline-engine",
        "project_hint": {"project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"},
        "schema_version": "v1",
    }


def _post_input(client: TestClient, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    response: httpx.Response = client.post("/v1/inputs", json=body, headers=headers or _host_headers())
    return response


def _capture(client: TestClient, content: str, *, prompt_id: str = "p1") -> dict[str, Any]:
    response = _post_input(client, _capture_body(content, prompt_id=prompt_id))
    assert response.status_code == 201, response.text
    receipt: dict[str, Any] = response.json()
    assert receipt["capture_principal"] == _HOST_PRINCIPAL
    assert receipt["event_id"], receipt
    return receipt


def _executor_event(title: str = "ran the unit tests") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "agent",
        "source": "cli",
        "domain": "coding",
        "task_type": "debug",
        "event_type": "TASK_STEP",
        "title": title,
        "payload": {"summary": title},
        "context": {"project": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "tags": [],
        "sensitivity": 2,
        "redaction_hints": [],
    }


# --------------------------------------------------------------------------- case 1: membership must not lock the human out


def test_host_capture_principal_is_not_gated_by_seeded_workspace_memberships(lite_client: TestClient) -> None:
    """Full blocker main.py:4087, pinned on the backend that gets it right.

    The first executor-stored event auto-seeds ``team_memberships`` with the executor as owner. From
    that moment a membership-gated workspace check denies every non-member USER -- which is exactly
    the human the executor is acting for, and the host adapter attesting that human's keystrokes.
    """
    stored = lite_client.post("/v1/events", json=_executor_event(), headers=_executor_headers())
    assert stored.status_code in {200, 201}, stored.text
    members = {str(row["user_id"]) for row in _all("SELECT user_id FROM team_memberships WHERE workspace_id = ?", (_WORKSPACE,))}
    assert "codex-executor" in members, "precondition: storing an event seeds the executor as a member"
    assert _HUMAN in members, "the human the executor acts for must be seeded too, or they are locked out of their own workspace"

    # The host-capture credential (a USER that is not the executor) can still attest human input.
    captured = _post_input(lite_client, _capture_body("confirm", prompt_id="after-seed"))
    assert captured.status_code == 201, captured.text
    assert captured.json()["capture_principal"] == _HOST_PRINCIPAL

    # And the human's own surfaces stay reachable: evidence, feedback and the receipt read.
    evidence = lite_client.post("/v1/behavior/evidence", json=_evidence_payload(1), headers=_verified_operator_headers())
    assert evidence.status_code == 200, evidence.text
    receipt_id = str(captured.json()["receipt_id"])
    fetched = lite_client.get(f"/v1/inputs/{receipt_id}", headers=_verified_operator_headers())
    assert fetched.status_code == 200, fetched.text

    # Strict mode gates the host adapter too; it passes here only because the human it attests for is
    # a member. The compat exemption above is what keeps that human from being locked out in the first place.
    get_settings().workspace_access_mode = "strict"
    try:
        strict_capture = _post_input(lite_client, _capture_body("confirm", prompt_id="strict"))
        assert strict_capture.status_code == 201, strict_capture.text
    finally:
        get_settings().workspace_access_mode = "compat"


# --------------------------------------------------------------------------- case 2: header-asserted human is not a human


def test_identity_verified_gates_receipt_bound_human_evidence(lite_client: TestClient) -> None:
    """main.py:8473 (a). ``X-TCE-Role: user`` on the executor's bearer is an assertion, not an identity.

    Both callers below submit byte-identical evidence citing the same real host-captured event while
    the same question is open. Only the server-bound human may bind it to the receipt.
    """
    _seed_prior_evidence(lite_client)
    # Captured before any question exists, so extraction leaves it unpromoted and the question open.
    receipt = _capture(lite_client, "confirm", prompt_id="pre-question")
    event_id = str(receipt["event_id"])
    opportunity_id = _open_safety_question(lite_client, _session())
    _assert_still_open_and_pending(opportunity_id)

    unverified = lite_client.post(
        "/v1/behavior/evidence",
        json={**_evidence_payload(20), "source_event_ids": [event_id]},
        headers=_asserted_operator_headers(),
    )
    assert unverified.status_code == 200, unverified.text
    unverified_body = unverified.json()
    assert unverified_body["learning_eligible"] is False, unverified_body
    assert unverified_body["storage_decision"] == "pending_review", unverified_body
    assert "identity_unverified" in unverified_body["storage_reasons"], unverified_body
    unverified_row = _observation(str(unverified_body["observation_id"]))
    assert unverified_row["origin_kind"] != "human_input", dict(unverified_row)
    assert unverified_row["capture_receipt_id"] is None, "an unverified caller may not bind evidence to a trusted receipt"
    assert unverified_row["opportunity_id"] is None, "nor to the frozen question"
    assert unverified_row["confirmed_at"] is None
    # The evidence is kept for audit, but the question is untouched by it.
    _assert_still_open_and_pending(opportunity_id)

    verified = lite_client.post(
        "/v1/behavior/evidence",
        json={**_evidence_payload(21), "source_event_ids": [event_id]},
        headers=_verified_operator_headers(),
    )
    assert verified.status_code == 200, verified.text
    verified_body = verified.json()
    assert verified_body["learning_eligible"] is True, verified_body
    assert "identity_unverified" not in verified_body["storage_reasons"], verified_body
    verified_row = _observation(str(verified_body["observation_id"]))
    assert verified_row["origin_kind"] == "human_input", dict(verified_row)
    assert str(verified_row["capture_receipt_id"]) == str(receipt["receipt_id"])
    assert str(verified_row["opportunity_id"]) == opportunity_id
    # Evidence is still not a resolution: only the receipt-backed extraction path confirms.
    assert verified_row["confirmed_at"] is None
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,)) == 0


# --------------------------------------------------------------------------- case 3: unverified operator cannot label


def test_unverified_operator_permit_resolution_does_not_label_the_frozen_prediction(lite_client: TestClient) -> None:
    """main.py:8473 (c). An executor that calls itself a user must not grade its own prediction.

    The permit decision itself is the executor's to make or not; what it may never do is produce the
    human label the exit gate scores prospective accuracy on.
    """
    _seed_prior_evidence(lite_client)
    session_id = _session()
    executor = _executor_headers()
    opportunity_id = _open_safety_question(lite_client, session_id)
    permit = _request_permit(lite_client, session_id, executor, target="infra/docker-compose.yml")
    assert permit["decision"] == "confirm_required", permit
    permit_id = str(permit["permit_id"])

    asserted = _resolve_permit(lite_client, permit_id, _asserted_operator_headers())
    assert asserted.status_code in {200, 403}, asserted.text
    _assert_still_open_and_pending(opportunity_id)
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE resolution_source = 'operator_permit'") == 0

    # The same decision from the server-bound human is a label.
    verified = _resolve_permit(lite_client, permit_id, _verified_operator_headers())
    assert verified.status_code == 200, verified.text
    assert _opportunity(opportunity_id)["status"] == "resolved"
    resolution = _one("SELECT * FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,))
    assert resolution["resolution_source"] == "operator_permit"
    assert resolution["human_source_ref"] == permit_id
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["resolution_state"] == "resolved"
    assert shadow["actual_choice"] == "confirm"
    assert shadow["prediction_stage"] == "prospective"


# --------------------------------------------------------------------------- case 4: permit resolves the safety question


def test_permit_resolution_targets_the_safety_question_when_a_newer_one_is_open(lite_client: TestClient) -> None:
    """main.py:11017. The permit answers the safety question, whatever was frozen after it.

    Sequence: the safety question is frozen (open), the executor relays 'confirm' (a relay is not a
    label, so it stays open), and the same turn escalates -> a *newer* needs_human question. Resolving
    the permit must answer the safety one and leave the newer question open for its own answer.
    """
    settings = get_settings()
    settings.takeover_needs_human_threshold_hot = 1.0
    settings.takeover_needs_human_threshold_cold = 1.0
    _seed_prior_evidence(lite_client)
    session_id = _session()
    executor = _executor_headers()

    safety_id = _open_safety_question(lite_client, session_id, task=_READ_ONLY_TASK, message=_HIGH_RISK_MESSAGE)
    relayed = _step(lite_client, session_id, executor, message="confirm", task=_READ_ONLY_TASK)
    assert relayed["safety_decision"] == "allow", relayed
    newer_id = str(relayed.get("open_decision_opportunity_id") or "")
    assert newer_id and newer_id != safety_id, relayed
    assert _opportunity(newer_id)["decision_family"] == "needs_human"
    assert _opportunity(safety_id)["status"] == "open", "an executor relay is not an answer"
    assert _opportunity(safety_id)["relayed_answer"] == "confirm"
    newest = _one(
        "SELECT id FROM decision_opportunities WHERE status = 'open' AND session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (session_id,),
    )
    assert str(newest["id"]) == newer_id, "precondition: the needs_human question is the newest open one"

    permit = _request_permit(lite_client, session_id, executor, target="infra/docker-compose.yml")
    assert permit["decision"] == "confirm_required", permit
    approved = _resolve_permit(lite_client, str(permit["permit_id"]), _verified_operator_headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["decision"] == "allow"

    assert _opportunity(safety_id)["status"] == "resolved", "the permit answers the safety question"
    safety_shadow = _shadow_for_opportunity(safety_id)
    assert safety_shadow["resolution_state"] == "resolved"
    assert safety_shadow["resolution_source"] == "operator_permit"
    assert safety_shadow["actual_choice"] == "confirm"
    # The newer question was never asked of this permit: it keeps waiting for its own answer.
    _assert_still_open_and_pending(newer_id)


# --------------------------------------------------------------------------- case 5: feedback with no opportunity id


def test_human_feedback_without_opportunity_id_resolves_the_open_question(lite_client: TestClient) -> None:
    """main.py:15450. Takeover state is keyed by the executor's user id; the human's is a different row.

    A human operator posting feedback therefore never sees ``open_decision`` in their own state.
    Opportunities are subject-scoped, so the open question for that session is still findable.
    """
    _seed_prior_evidence(lite_client)
    session_id = _session()
    opportunity_id = _open_safety_question(lite_client, session_id)

    feedback = lite_client.post(
        "/v1/takeover/feedback",
        json={
            "session_id": session_id,
            "turn": 1,
            "action_kind": "execute",
            "result": "success",
            "situation_type": "approval_requested",
            "correction_text": "confirm",
        },
        headers=_verified_operator_headers(),
    )
    assert feedback.status_code == 200, feedback.text

    assert _opportunity(opportunity_id)["status"] == "resolved"
    resolution = _one("SELECT * FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,))
    assert resolution["resolution_source"] == "operator_feedback"
    assert resolution["selected_choice"] == "confirm"
    assert resolution["observation_id"], "the feedback observation is the human source reference"
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["resolution_state"] == "resolved"
    assert shadow["resolution_source"] == "operator_feedback"
    assert shadow["actual_choice"] == "confirm"
    assert shadow["prediction_stage"] == "prospective"

    # The unverified twin of the same call resolves nothing (main.py:8473 (b)).
    second_session = _session()
    second_id = _open_safety_question(lite_client, second_session)
    asserted = lite_client.post(
        "/v1/takeover/feedback",
        json={
            "session_id": second_session,
            "turn": 1,
            "action_kind": "execute",
            "result": "success",
            "situation_type": "approval_requested",
            "correction_text": "confirm",
        },
        headers=_asserted_operator_headers(),
    )
    assert asserted.status_code == 200, asserted.text
    _assert_still_open_and_pending(second_id)


# --------------------------------------------------------------------------- case 6: an answered question stays answered


def test_resolved_question_is_not_re_exposed_on_the_next_step(lite_client: TestClient) -> None:
    """main.py:17139. Once the human has answered, the turn contract must stop naming that question.

    The answer arrives out of band (the host hook), so the executor's session context still holds the
    id it froze. Handing that id back says "still waiting on the human" forever: the executor keeps
    re-asking, and in Full ``_nothing_left_to_do`` can never become true. The Lite twin of that stall
    is ``open_decision_opportunity_id`` never clearing.
    """
    _seed_prior_evidence(lite_client)
    session_id = _session()
    opportunity_id = _open_safety_question(lite_client, session_id)
    receipt = _capture(lite_client, "confirm", prompt_id="answer")

    assert _opportunity(opportunity_id)["status"] == "resolved", "precondition: the captured answer resolved it"
    assert _shadow_for_opportunity(opportunity_id)["resolution_state"] == "resolved"

    follow_up = _step(lite_client, session_id, message="what should i do next", task=None)
    exposed = str(follow_up.get("open_decision_opportunity_id") or "")
    assert exposed != opportunity_id, f"the answered question was handed back as still open (safety={follow_up.get('safety_decision')})"
    if exposed:
        # A genuinely new question is fine; a non-open one never is.
        assert _opportunity(exposed)["status"] == "open", dict(_opportunity(exposed))

    # The answer is not re-consumed either: one resolution, one promoted observation.
    assert _opportunity(opportunity_id)["status"] == "resolved"
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,)) == 1
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE capture_receipt_id = ?", (str(receipt["receipt_id"]),)) == 1


# --------------------------------------------------------------------------- case 7: sweep closes relayed questions


def test_sweep_expires_relayed_but_unlabelled_opportunities(lite_client: TestClient) -> None:
    """capture_store.py:545. A relayed-but-unlabelled question must leave 'open', not just lose its label.

    Marking only the shadow row missing_label leaves the opportunity matchable forever: a later
    receipt promotes against it and writes a resolution, while ``resolve_shadow_prediction`` silently
    no-ops because the prediction already left 'pending'.
    """
    _seed_prior_evidence(lite_client)
    session_id = _session()
    opportunity_id = _open_safety_question(lite_client, session_id)
    relayed = _step(lite_client, session_id, message="confirm", task=None)
    assert relayed["safety_decision"] == "allow", relayed
    assert _opportunity(opportunity_id)["relayed_at"], "precondition: the executor relayed an answer"
    assert _opportunity(opportunity_id)["status"] == "open"

    get_settings().capture_opportunity_ttl_seconds = 0
    swept = lite_client.post("/v1/admin/lifecycle/run", json={"dry_run": False}, headers=_verified_operator_headers())
    assert swept.status_code == 200, swept.text
    sweep = swept.json()["decision_extraction"]["sweep"]
    assert int(sweep["missing_label"]) >= 1, sweep

    opportunity = _opportunity(opportunity_id)
    assert opportunity["status"] == "expired", "a relayed, unlabelled question must not stay matchable"
    assert opportunity["resolved_at"], dict(opportunity)
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["resolution_state"] == "missing_label"
    assert shadow["resolution_source"] == "executor_relayed"

    # A later human answer has nothing to promote against, and cannot resurrect the swept question.
    late = _capture(lite_client, "confirm", prompt_id="late")
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,)) == 0
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE opportunity_id = ?", (opportunity_id,)) == 0
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE capture_receipt_id = ?", (str(late["receipt_id"]),)) == 0
    assert _opportunity(opportunity_id)["status"] == "expired"
    assert _shadow_for_opportunity(opportunity_id)["resolution_state"] == "missing_label"


# --------------------------------------------------------------------------- case 8: the credential lives outside .env


def test_host_capture_tokens_file_is_honoured_alongside_the_env_csv(lite_client: TestClient, tmp_path: Path) -> None:
    """install.sh:792. The repo ``.env`` is mounted into the API container and readable by any executor
    with a shell, so the host credential is kept in a 0600 file outside the repo and mounted read-only.
    The API therefore reads host tokens from the union of the env CSV and that file.
    """
    settings = get_settings()
    token_file = tmp_path / "host_capture.token"
    token_file.write_text(f"# managed by scripts/install.sh\n\n{_FILE_HOST_TOKEN}\n", encoding="utf-8")
    settings.host_capture_tokens_file = str(token_file)

    from_file = _post_input(lite_client, _capture_body("confirm", prompt_id="file"), _host_headers(token=_FILE_HOST_TOKEN))
    assert from_file.status_code == 201, from_file.text
    assert from_file.json()["capture_principal"] == _HOST_PRINCIPAL
    # The env CSV keeps working: the file is a union source, not a replacement.
    from_env = _post_input(lite_client, _capture_body("confirm", prompt_id="env"), _host_headers())
    assert from_env.status_code == 201, from_env.text
    # Comments, blank lines and unrelated tokens are not credentials.
    unknown = _post_input(lite_client, _capture_body("confirm", prompt_id="unknown"), _host_headers(token="not-a-host-token"))
    assert unknown.status_code in {401, 403}, unknown.text
    comment = _post_input(lite_client, _capture_body("confirm", prompt_id="comment"), _host_headers(token="# managed by scripts/install.sh"))
    assert comment.status_code in {401, 403}, comment.text

    # An unreadable or absent file degrades to the env CSV rather than failing the request.
    settings.host_capture_tokens_file = str(tmp_path / "does-not-exist.token")
    still_env = _post_input(lite_client, _capture_body("confirm", prompt_id="missing-file"), _host_headers())
    assert still_env.status_code == 201, still_env.text
    gone = _post_input(lite_client, _capture_body("confirm", prompt_id="gone"), _host_headers(token=_FILE_HOST_TOKEN))
    assert gone.status_code in {401, 403}, gone.text

    # Only host-capture credentials get the capability, however they were configured.
    settings.host_capture_tokens_file = str(token_file)
    executor = _post_input(lite_client, _capture_body("confirm", prompt_id="exec"), _executor_headers())
    assert executor.status_code == 403, executor.text
    assert _count("SELECT COUNT(*) FROM trusted_input_receipts WHERE capture_principal NOT LIKE 'host:%'") == 0
