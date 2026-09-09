"""P1 trusted-capture exit gate against the Lite HTTP boundary.

Every case drives the real FastAPI app over ``TestClient`` with the real
SQLite store. SQL is used only to *observe* state -- never as the assertion
path for behaviour the HTTP boundary must enforce.

Exit gate (owner's plan, verbatim intent):
  an ordinary captured human choice flows
    /v1/inputs (host capability) -> receipt -> extraction -> candidate ->
    provenance validation -> eligible evidence -> a shadow prediction row that
    was FROZEN BEFORE the answer
  with no manual /v1/behavior/evidence submission, and an executor's forged
  "explicit" assertion never follows that path.

Matrix:
  1 exit gate end to end (safety question -> executor relay is not a label -> host capture -> promoted evidence)
  2 prospective freeze precedes the answer; an answer that predates its question resolves nothing
  3 "yes" resolves only a unique, still-current question; a generic ack against two questions is discarded
  4 host-capture capability is credential-only: executor bearer -> 403, role header cannot mint it
  5 human-origin events cannot be forged through /v1/events by an executor
  6 forged explicit evidence stays inferred / unconfirmed / pending_review (P0) and never binds to a receipt
  7 credential configuration guards (token in both sets, default token, subject mismatch, no bearer)
  8 enforce mode requires a bound claim carrying the host_capture capability
  9 unattended profile pauses mutation while the capture channel is unavailable
 10 an operator permit resolution resolves the open safety question (verified only; unverified is audit-only)
 11 stand-down abandons open questions; a later answer has nothing to resolve
 12 a header-asserted human never mints learning-eligible evidence
 13 a pending review is promoted only by a server-verified human
 14 an untrusted project_hint is redacted server-side, whatever the client sent
 15 a host clock running ahead cannot forge a prospective answer
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
_API_TOKENS = f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}"
_HOST_TOKEN = "host-token"
_UNBOUND_HOST_TOKEN = "unbound-host-token"
_HUMAN = "human-1"
_OTHER_HUMAN = "human-2"
_WORKSPACE = "personal"
_HOST_PRINCIPAL = "host:host-capture-claude"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "host_capture_tokens",
    "allow_default_token",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "takeover_lease_strict",
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
    # P2 §0.5 — the durable-task-state and retrieval-deadline settings. They are guarded
    # here for the same reason as every name above: this fixture mutates the process-wide
    # Settings singleton, and an unguarded name leaks into every later test in the run.
    # getattr/_MISSING makes listing them safe before Builder C adds them to the Lite config.
    "takeover_turn_budget_ms",
    "task_state_enabled",
    "task_state_markdown_enabled",
    "task_state_markdown_max_steps",
    "planning_async_enabled",
    "planning_job_lease_seconds",
    "planning_job_max_attempts",
    "planning_job_batch_size",
    "planning_job_backoff_cap_seconds",
    "planning_pending_hint_ms",
    "retrieval_deadline_enabled",
    "retrieval_deadline_floor_ms",
    "retrieval_statement_floor_ms",
    "retrieval_advisor_min_ms",
    "sqlite_progress_instructions",
)

_BOUND_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_HIGH_RISK_TASK = "delete the build artifacts with rm -rf /tmp/build"
_MUTATING_TASK = "update the changelog notes"
_READ_ONLY_TASK = "summarize the failing unit tests and report back"
_SAFETY_PREFIX = "Safety pause: high-risk action detected"
_CAPTURE_PAUSE_PREFIX = "AUTONOMOUS MODE PAUSED: capture channel"


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


def _apply_common_settings(db_path: Path) -> None:
    settings = get_settings()
    settings.lite_db_path = str(db_path)
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


@pytest.fixture()
def lite_client(tmp_path: Path) -> Iterator[TestClient]:
    """Compat-mode client: X-TCE-* headers assert identity; the host capability must still be credential-only."""
    snapshot = _snapshot_settings()
    _apply_common_settings(tmp_path / "trusted-evidence.db")
    settings = get_settings()
    settings.api_tokens = _API_TOKENS
    settings.host_capture_tokens = _HOST_TOKEN
    settings.identity_claims_mode = "compat"
    # Compat mode, with one exception: the operator token is bound to a server claim, so the human
    # operator is identity-verified while everything asserted through X-TCE-* headers is not.
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


@pytest.fixture()
def bound_client(tmp_path: Path) -> Iterator[TestClient]:
    """Enforce-mode client: one bound executor, one bound human operator, one bound host-capture claim, one unbound host token."""
    snapshot = _snapshot_settings()
    _apply_common_settings(tmp_path / "trusted-evidence-bound.db")
    settings = get_settings()
    settings.api_tokens = f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}"
    settings.host_capture_tokens = f"{_HOST_TOKEN},{_UNBOUND_HOST_TOKEN}"
    settings.identity_claims_mode = "enforce"
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _OPERATOR_TOKEN): {
                "consumer": "operator-ui",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            },
            credential_fingerprint("bearer", _EXEC_TOKEN): {
                "consumer": "codex-executor",
                "role": "executor",
                "workspace_id": _WORKSPACE,
                "user_id": "codex-executor",
                "behavior_subject_id": _HUMAN,
            },
            credential_fingerprint("bearer", _HOST_TOKEN): {
                "consumer": "host-capture-claude",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
                "capabilities": ["host_capture"],
            },
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


# --------------------------------------------------------------------------- headers


def _executor_headers(
    consumer: str = "codex-executor",
    *,
    role: str | None = "executor",
    user: str | None = None,
    subject: str = _HUMAN,
    token: str = _EXEC_TOKEN,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": consumer,
        "X-TCE-User": user or consumer,
        "X-TCE-Behavior-Subject": subject,
        "X-TCE-Workspace": _WORKSPACE,
    }
    if role is not None:
        headers["X-TCE-Role"] = role
    return headers


def _host_headers(
    *,
    consumer: str = "host-capture-claude",
    user: str = _HUMAN,
    subject: str | None = None,
    token: str = _HOST_TOKEN,
) -> dict[str, str]:
    """Host-capture headers carry NO role header: the capability comes from the credential alone."""
    return {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": consumer,
        "X-TCE-User": user,
        "X-TCE-Behavior-Subject": subject or user,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _operator_headers(*, subject: str = _HUMAN) -> dict[str, str]:
    """A human operator identity on the ordinary api token (role is caller-asserted in compat mode)."""
    return _executor_headers("operator-ui", role="user", user=subject, subject=subject)


def _verified_operator_headers() -> dict[str, str]:
    """A human operator whose identity the server bound to the credential, not to a header assertion."""
    return {
        "Authorization": f"Bearer {_OPERATOR_TOKEN}",
        "X-TCE-Consumer": "operator-ui",
        "X-TCE-Role": "user",
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


def _maybe_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    conn = _db()
    try:
        row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return row


def _all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = _db()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _count(sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(_one(sql, params)[0])


def _ts(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> Any:
    return json.loads(str(value or "null"))


def _opportunity(opportunity_id: str) -> sqlite3.Row:
    return _one("SELECT * FROM decision_opportunities WHERE id = ?", (opportunity_id,))


def _shadow_for_opportunity(opportunity_id: str) -> sqlite3.Row:
    return _one("SELECT * FROM behavior_shadow_predictions WHERE opportunity_id = ?", (opportunity_id,))


def _open_opportunities(subject: str = _HUMAN) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM decision_opportunities WHERE workspace_id = ? AND subject_user_id = ? AND status = 'open' ORDER BY created_at ASC",
        (_WORKSPACE, subject),
    )


def _candidates_for_receipt(receipt_id: str) -> list[sqlite3.Row]:
    return _all("SELECT * FROM decision_candidates WHERE receipt_id = ? ORDER BY created_at ASC", (receipt_id,))


def _observations_for_receipt(receipt_id: str) -> list[sqlite3.Row]:
    return _all("SELECT * FROM decision_observations WHERE capture_receipt_id = ? ORDER BY ts ASC", (receipt_id,))


# --------------------------------------------------------------------------- takeover helpers


def _session() -> str:
    return f"codex-{uuid.uuid4().hex[:8]}"


def _step(
    client: TestClient,
    session_id: str,
    headers: dict[str, str] | None = None,
    *,
    message: str = "beru take over",
    task: str | None = _HIGH_RISK_TASK,
    app_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "persona_mode": "shadow",
        "app_context": dict(_BOUND_APP_CONTEXT if app_context is None else app_context),
        "constraints": {"k": 4},
        "allow_fallback": True,
    }
    if task is not None:
        payload["task"] = task
    step = client.post("/v1/takeover/step", json=payload, headers=headers or _executor_headers())
    assert step.status_code == 200, step.text
    body: dict[str, Any] = step.json()
    return body


def _open_safety_question(client: TestClient, session_id: str, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], str]:
    """Activate takeover on a destructive task: the safety gate poses a confirm/abort question to the human."""
    body = _step(client, session_id, headers, message="beru take over", task=_HIGH_RISK_TASK)
    assert body["safety_decision"] == "confirm_required", body
    assert str(body.get("final_response") or "").startswith(_SAFETY_PREFIX), body.get("final_response")
    opportunity_id = body.get("open_decision_opportunity_id")
    assert opportunity_id, f"the safety question must be frozen as a decision opportunity: {body}"
    return body, str(opportunity_id)


def _request_permit(client: TestClient, session_id: str, headers: dict[str, str], *, target: str = "docs/notes.md") -> dict[str, Any]:
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


def _claim_raw(client: TestClient, session_id: str, directive_id: str, headers: dict[str, str]) -> httpx.Response:
    response: httpx.Response = client.post("/v1/takeover/execution/claim", json={"session_id": session_id, "directive_id": directive_id}, headers=headers)
    return response


def _execution_status(client: TestClient, session_id: str, headers: dict[str, str]) -> dict[str, Any]:
    status = client.get("/v1/takeover/execution/status", params={"session_id": session_id}, headers=headers)
    assert status.status_code == 200, status.text
    body: dict[str, Any] = status.json()
    return body


# --------------------------------------------------------------------------- evidence + capture helpers


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


def _seed_prior_evidence(client: TestClient, count: int = 3) -> list[str]:
    """Eligible human evidence so the frozen prediction is non-abstained (verified: predicts 'confirm' at ~0.83)."""
    ids: list[str] = []
    for index in range(count):
        # The seed must be *verified* human evidence: a header-asserted operator is review-gated (case 12).
        response = client.post("/v1/behavior/evidence", json=_evidence_payload(index), headers=_verified_operator_headers())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["learning_eligible"] is True, body
        ids.append(str(body["observation_id"]))
    return ids


def _capture_body(
    content: str,
    *,
    host_session_id: str | None = None,
    prompt_id: str = "p1",
    observed_at: datetime | None = None,
    origin_kind: str = "human_input",
    cwd: str = "/work/open-timeline-engine",
) -> dict[str, Any]:
    """Exactly what the host hook sends: sha256 over the ORIGINAL text, client-side redaction, deterministic delivery key."""
    host_session_id = host_session_id or str(uuid.uuid4())
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    redacted, applied = redact_text(content)
    return {
        "session_id": host_session_id,
        "delivery_key": compute_delivery_key(host_session_id, prompt_id, content_sha256),
        "content_sha256": content_sha256,
        "content": redacted,
        "origin_kind": origin_kind,
        "observed_at": (observed_at or datetime.now(tz=UTC)).isoformat(),
        "original_char_count": len(content),
        "content_truncated": False,
        "redaction_applied": applied,
        "prompt_id": prompt_id,
        "hook_event_name": "UserPromptSubmit",
        "host_client": "claude",
        "cwd": cwd,
        "project_hint": {"project": "open-timeline-engine", "project_root": cwd},
        "schema_version": "v1",
    }


def _post_input(client: TestClient, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    response: httpx.Response = client.post("/v1/inputs", json=body, headers=headers or _host_headers())
    return response


def _capture(client: TestClient, content: str, **kwargs: Any) -> dict[str, Any]:
    """Host-capture a human message and assert the durable receipt contract."""
    body = _capture_body(content, **kwargs)
    response = _post_input(client, body)
    assert response.status_code == 201, response.text
    receipt: dict[str, Any] = response.json()
    assert receipt["deduplicated"] is False
    assert receipt["delivery_key"] == body["delivery_key"]
    assert receipt["content_sha256"] == body["content_sha256"]
    assert receipt["origin_kind"] == body["origin_kind"]
    assert receipt["capture_principal"] == _HOST_PRINCIPAL
    assert receipt["event_id"], receipt
    assert receipt["extraction_state"] in {"pending", "extracted"}, receipt
    return receipt


def _human_input_event(*, source: str = "tce-host-capture", task_type: str = "human_input", context: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": source,
        "domain": "coding",
        "task_type": task_type,
        "event_type": "TASK_STEP",
        "title": "confirm",
        "payload": {"input_excerpt": "confirm", "live_capture": True},
        "context": {"project": "open-timeline-engine", **(context or {})},
        "inputs": {},
        "steps": [],
        "tags": ["human_input"],
        "sensitivity": 2,
        "redaction_hints": [],
    }


def _assert_frozen_pending(shadow: sqlite3.Row, *, predicted: str | None = "confirm") -> datetime:
    """A prospective prediction: frozen, unanswered, no label, no observation -- and it already exists."""
    assert shadow["prediction_stage"] == "prospective", dict(shadow)
    assert shadow["resolution_state"] == "pending", dict(shadow)
    assert str(shadow["actual_choice"] or "") == "", dict(shadow)
    assert shadow["correct"] is None, dict(shadow)
    assert shadow["observation_id"] is None, dict(shadow)
    assert shadow["resolved_at"] is None, dict(shadow)
    assert shadow["frozen_at"], dict(shadow)
    if predicted is not None:
        assert shadow["predicted_choice"] == predicted, dict(shadow)
        assert int(shadow["abstained"]) == 0, dict(shadow)
    return _ts(shadow["frozen_at"])


# --------------------------------------------------------------------------- case 1: exit gate


def test_exit_gate_human_choice_flows_from_host_capture_to_frozen_shadow_prediction(lite_client: TestClient) -> None:
    _seed_prior_evidence(lite_client)
    session_id = _session()
    host_session_id = str(uuid.uuid4())  # a Claude session id: deliberately NOT the takeover session id

    # --- the decision opportunity is frozen when the question is posed, before any answer exists
    activation, opportunity_id = _open_safety_question(lite_client, session_id)
    assert activation["capture_delivery_state"] == "unavailable", "no receipt has ever been delivered for this subject"

    opportunity = _opportunity(opportunity_id)
    assert opportunity["status"] == "open"
    assert opportunity["decision_family"] == "safety_confirmation"
    assert opportunity["situation_type"] == "approval_requested"
    assert opportunity["session_id"] == session_id
    assert opportunity["workspace_id"] == _WORKSPACE
    assert opportunity["subject_user_id"] == _HUMAN
    assert _json(opportunity["alternatives_json"]) == ["confirm", "abort"]
    assert str(opportunity["question_text"]).startswith(_SAFETY_PREFIX)
    assert opportunity["evidence_revision"], "the evidence revision used for the prediction must be pinned"
    assert opportunity["frozen_at"] and opportunity["expires_at"]
    assert opportunity["shadow_prediction_id"]
    assert _json(opportunity["pre_answer_snapshot_json"]).get("objective"), _json(opportunity["pre_answer_snapshot_json"])

    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["id"] == opportunity["shadow_prediction_id"]
    assert shadow["decision_family"] == "safety_confirmation"
    assert shadow["session_id"] == session_id
    assert int(shadow["evidence_count"]) >= 3
    frozen_at = _assert_frozen_pending(shadow)

    # --- the executor relaying 'confirm' is NOT an authenticated human answer: nothing resolves
    relayed = _step(lite_client, session_id, message="confirm", task=None)
    assert relayed["safety_decision"] == "allow", relayed
    opportunity = _opportunity(opportunity_id)
    assert opportunity["status"] == "open"
    assert opportunity["relayed_answer"] == "confirm"
    assert opportunity["relayed_at"]
    assert opportunity["resolved_at"] is None
    _assert_frozen_pending(_shadow_for_opportunity(opportunity_id))
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,)) == 0
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE opportunity_id = ?", (opportunity_id,)) == 0

    # --- the host captures the human's actual keystrokes, after the freeze
    observed_at = datetime.now(tz=UTC)
    assert observed_at > frozen_at
    receipt = _capture(lite_client, "confirm", host_session_id=host_session_id, prompt_id="p1", observed_at=observed_at)
    receipt_id = str(receipt["receipt_id"])
    event_id = str(receipt["event_id"])
    assert receipt["capture_delivery_state"] in {"healthy", "unknown"}, receipt

    # Idempotent redelivery (spool replay): same delivery key -> same receipt, nothing duplicated.
    replay = _post_input(lite_client, _capture_body("confirm", host_session_id=host_session_id, prompt_id="p1", observed_at=observed_at))
    assert replay.status_code in {200, 201}, replay.text
    assert replay.json()["deduplicated"] is True
    assert str(replay.json()["receipt_id"]) == receipt_id
    assert _count("SELECT COUNT(*) FROM trusted_input_receipts WHERE delivery_key = ?", (receipt["delivery_key"],)) == 1
    assert _count("SELECT COUNT(*) FROM events WHERE task_type = 'human_input'") == 1

    # The receipt is durable and bound to a human-origin event stamped with the host principal.
    receipt_row = _one("SELECT * FROM trusted_input_receipts WHERE id = ?", (receipt_id,))
    assert receipt_row["workspace_id"] == _WORKSPACE
    assert receipt_row["subject_user_id"] == _HUMAN
    assert receipt_row["host_session_id"] == host_session_id
    assert receipt_row["origin_kind"] == "human_input"
    assert receipt_row["capture_principal"] == _HOST_PRINCIPAL
    assert receipt_row["event_id"] == event_id
    assert receipt_row["content_sha256"] == receipt["content_sha256"]
    assert receipt_row["extraction_state"] == "extracted", dict(receipt_row)
    assert receipt_row["extraction_version_done"] == "dc-v1"

    event_row = _one("SELECT * FROM events WHERE id = ?", (event_id,))
    assert event_row["task_type"] == "human_input"
    assert event_row["source"] == "tce-host-capture"
    assert event_row["actor"] == "user"
    event_context = _json(event_row["context"])
    assert event_context["input_origin"] == "human_input"
    assert event_context["capture_principal"] == _HOST_PRINCIPAL
    assert event_context["host_session_id"] == host_session_id
    event_payload = _json(event_row["payload"])
    assert event_payload["input_excerpt"] == "confirm"
    assert event_payload["input_sha256"] == receipt["content_sha256"]
    assert event_payload["live_capture"] is True

    # --- extraction: exactly one promoted ANSWER candidate against the unique open question
    candidates = _candidates_for_receipt(receipt_id)
    assert len(candidates) == 1, [dict(row) for row in candidates]
    candidate = candidates[0]
    assert candidate["candidate_kind"] == "answer"
    assert candidate["selected_option"] == "confirm"
    assert candidate["opportunity_id"] == opportunity_id
    assert candidate["promotion"] == "promote"
    assert candidate["status"] == "promoted"
    assert candidate["origin_kind"] == "human_input"
    assert candidate["extraction_version"] == "dc-v1"
    assert candidate["supporting_span"] == "confirm"
    assert candidate["stated_rationale"] is None, "no rationale marker in the text -> none may be synthesized"
    assert _json(candidate["observed_alternatives_json"]) == ["confirm", "abort"]
    assert candidate["promoted_observation_id"]

    # --- promotion: eligible explicit evidence, confirmed by the server, provenance-bound to the receipt
    observations = _observations_for_receipt(receipt_id)
    assert len(observations) == 1, [dict(row) for row in observations]
    observation = observations[0]
    assert observation["id"] == candidate["promoted_observation_id"]
    assert observation["evidence_source"] == "explicit"
    assert observation["confirmed_at"] is not None
    assert int(observation["learning_eligible"]) == 1
    assert observation["storage_decision"] == "learn"
    assert observation["origin_kind"] == "human_input"
    assert observation["capture_receipt_id"] == receipt_id
    assert observation["opportunity_id"] == opportunity_id
    assert observation["extraction_version"] == "dc-v1"
    assert observation["selected_choice"] == "confirm"
    assert observation["situation_type"] == "approval_requested"
    assert observation["subject_user_id"] == _HUMAN
    assert event_id in [str(value) for value in _json(observation["source_event_ids"])]
    assert str(observation["consumer_id"]).startswith("extraction:"), "evidence came from the extraction job, not a manual submission"
    # No manual evidence submission happened for this question: the only observation bound to it is the extracted one.
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE opportunity_id = ?", (opportunity_id,)) == 1

    resolution = _one("SELECT * FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,))
    assert resolution["resolution_source"] == "host_capture"
    assert resolution["human_source_ref"] == receipt_id
    assert resolution["receipt_id"] == receipt_id
    assert resolution["source_event_id"] == event_id
    assert resolution["candidate_id"] == candidate["id"]
    assert resolution["observation_id"] == observation["id"]
    assert resolution["selected_choice"] == "confirm"
    assert resolution["supersedes_resolution_id"] is None

    # --- the frozen prediction is resolved against the authenticated answer and stays prospective
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["prediction_stage"] == "prospective"
    assert shadow["resolution_state"] == "resolved"
    assert shadow["resolution_source"] == "host_capture"
    assert shadow["human_source_ref"] == receipt_id
    assert shadow["resolution_source_event_id"] == event_id
    assert shadow["actual_choice"] == "confirm"
    assert shadow["predicted_choice"] == "confirm"
    assert int(shadow["correct"]) == 1
    assert shadow["observation_id"] == observation["id"]
    assert _ts(shadow["frozen_at"]) == frozen_at
    assert _ts(shadow["frozen_at"]) < observed_at
    assert _ts(shadow["resolved_at"]) >= observed_at

    opportunity = _opportunity(opportunity_id)
    assert opportunity["status"] == "resolved"
    assert opportunity["resolved_at"]

    # --- visible through the existing surfaces
    shadow_status = lite_client.get("/v1/behavior/shadow/status", headers=_operator_headers())
    assert shadow_status.status_code == 200, shadow_status.text
    metrics = shadow_status.json()["metrics"]
    assert metrics["resolved_count"] >= 1
    assert metrics["prospective_count"] >= 1
    assert metrics["prospective_precision"] == 1.0
    assert metrics["pending_count"] == 0
    recent = {str(item["prediction_id"]): item for item in shadow_status.json()["recent"]}
    assert recent[str(shadow["id"])]["prediction_stage"] == "prospective"
    assert recent[str(shadow["id"])]["resolution_state"] == "resolved"
    assert str(recent[str(shadow["id"])]["opportunity_id"]) == opportunity_id

    fetched = lite_client.get(f"/v1/inputs/{receipt_id}", headers=_executor_headers())
    assert fetched.status_code == 200, fetched.text
    assert str(fetched.json()["receipt_id"]) == receipt_id
    assert fetched.json()["extraction_state"] == "extracted"
    foreign = lite_client.get(f"/v1/inputs/{receipt_id}", headers=_executor_headers(subject=_OTHER_HUMAN))
    assert foreign.status_code == 404, foreign.text

    # The answered question never comes back, and the delivered receipt marks the channel healthy. The
    # stored objective is still the destructive one, so this turn legitimately poses a *new* question;
    # what must never happen is the resolved id being handed back as still open (covered head-on in
    # test_trusted_capture_regressions_lite.py::test_resolved_question_is_not_re_exposed_on_the_next_step).
    follow_up = _step(lite_client, session_id, message="continue", task=None)
    exposed = str(follow_up.get("open_decision_opportunity_id") or "")
    assert exposed != opportunity_id, f"the answered question was handed back (safety={follow_up.get('safety_decision')})"
    if exposed:
        assert _opportunity(exposed)["status"] == "open", dict(_opportunity(exposed))
    assert _opportunity(opportunity_id)["status"] == "resolved"
    assert follow_up["capture_delivery_state"] == "healthy", follow_up


# --------------------------------------------------------------------------- case 2: prospective vs retrospective


def test_prospective_freeze_precedes_answer_and_an_early_answer_resolves_nothing(lite_client: TestClient) -> None:
    _seed_prior_evidence(lite_client)

    # (a) A prediction frozen BEFORE the answer keeps its prospective label after resolution.
    early_session = _session()
    _, early_id = _open_safety_question(lite_client, early_session)
    early_frozen = _assert_frozen_pending(_shadow_for_opportunity(early_id))
    assert _count("SELECT COUNT(*) FROM trusted_input_receipts") == 0, "the prediction exists before any answer was captured"

    _capture(lite_client, "confirm", prompt_id="early", observed_at=early_frozen + timedelta(seconds=5))
    early_shadow = _shadow_for_opportunity(early_id)
    assert early_shadow["resolution_state"] == "resolved"
    assert early_shadow["prediction_stage"] == "prospective"
    assert _ts(early_shadow["frozen_at"]) < _ts(early_shadow["resolved_at"])

    # (b) An answer that PREDATES its question is not that question's answer: it never resolves it.
    # This is the forged-consent shape -- a prompt POSTed by the hook before the safety question was
    # frozen would otherwise be promoted as the human's confirmation of a question they never saw.
    # Two layers enforce it and the first one wins here: the open-opportunity load is bounded by
    # ``frozen_at <= answer moment``, so the question is not even visible to the extractor and the text
    # degrades to a bare acknowledgement. The second layer (promotion_decision returning PENDING_REVIEW /
    # ``answer_precedes_question`` when a question frozen later is still in view) is covered at the unit
    # boundary in tests/unit/test_decision_capture.py.
    late_session = _session()
    _, late_id = _open_safety_question(lite_client, late_session)
    late_frozen = _assert_frozen_pending(_shadow_for_opportunity(late_id))
    late_receipt = _capture(lite_client, "abort", prompt_id="late", observed_at=late_frozen - timedelta(seconds=60))
    late_receipt_id = str(late_receipt["receipt_id"])
    late_candidates = _candidates_for_receipt(late_receipt_id)
    assert [row["promotion"] for row in late_candidates] == ["discard"], [dict(row) for row in late_candidates]
    assert [
        (row["candidate_kind"], row["promotion"], row["status"], row["promotion_reason"], row["opportunity_id"])
        for row in late_candidates
    ] == [("acknowledgement", "discard", "discarded", "acknowledgement", None)]
    # Nothing was promoted, nothing was resolved, nothing was labelled.
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE opportunity_id = ?", (late_id,)) == 0
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE receipt_id = ?", (late_receipt_id,)) == 0
    assert _observations_for_receipt(late_receipt_id) == []
    late_opportunity = _opportunity(late_id)
    assert late_opportunity["status"] == "open", dict(late_opportunity)
    assert late_opportunity["resolved_at"] is None, dict(late_opportunity)
    # The frozen prediction is still waiting for a real answer: prospective, pending, unscored.
    late_shadow = _shadow_for_opportunity(late_id)
    assert late_shadow["prediction_stage"] == "prospective", dict(late_shadow)
    assert late_shadow["resolution_state"] == "pending", dict(late_shadow)
    assert str(late_shadow["actual_choice"] or "") == "", dict(late_shadow)
    assert late_shadow["correct"] is None, dict(late_shadow)
    assert late_shadow["resolved_at"] is None, dict(late_shadow)
    assert late_shadow["observation_id"] is None, dict(late_shadow)

    # (c) The manual evidence path predicts in the same request as the answer: always retrospective, never pending.
    manual = lite_client.post("/v1/behavior/evidence", json=_evidence_payload(90, choice="abort"), headers=_operator_headers())
    assert manual.status_code == 200, manual.text
    manual_shadow = _one("SELECT * FROM behavior_shadow_predictions WHERE id = ?", (str(manual.json()["shadow_prediction_id"]),))
    assert manual_shadow["prediction_stage"] == "retrospective"
    assert manual_shadow["resolution_state"] == "resolved"
    assert manual_shadow["opportunity_id"] is None
    assert manual_shadow["resolved_at"] is not None

    # Exact row counts: 3 seeded + (c) manual = 4 retrospective resolved; (a) + (b) = 2 prospective, of
    # which only (a) is answered. The unanswered (b) row sits in pending, never in the precision numerator.
    metrics = lite_client.get("/v1/behavior/shadow/status", headers=_operator_headers()).json()["metrics"]
    assert metrics["prospective_count"] == 2
    assert metrics["retrospective_count"] == 4
    assert metrics["resolved_count"] == 5
    assert metrics["pending_count"] == 1
    assert metrics["abandoned_count"] == 0
    assert metrics["prospective_precision"] == 1.0


# --------------------------------------------------------------------------- case 3: "yes" needs a unique question


def test_generic_yes_resolves_only_a_unique_open_question(lite_client: TestClient) -> None:
    _seed_prior_evidence(lite_client)

    # Exactly one open confirm/abort question: "yes" maps onto 'confirm' and promotes.
    unique_session = _session()
    _, unique_id = _open_safety_question(lite_client, unique_session)
    receipt = _capture(lite_client, "yes", prompt_id="yes-1")
    candidates = _candidates_for_receipt(str(receipt["receipt_id"]))
    assert [(row["candidate_kind"], row["promotion"], row["status"]) for row in candidates] == [("answer", "promote", "promoted")]
    assert candidates[0]["selected_option"] == "confirm"
    assert _opportunity(unique_id)["status"] == "resolved"
    assert _shadow_for_opportunity(unique_id)["actual_choice"] == "confirm"

    # Two open questions for the same human: a bare acknowledgement is ambiguous -> discarded, nothing resolves.
    first_session, second_session = _session(), _session()
    _, first_id = _open_safety_question(lite_client, first_session)
    _, second_id = _open_safety_question(lite_client, second_session)
    assert {row["id"] for row in _open_opportunities()} == {first_id, second_id}

    ambiguous = _capture(lite_client, "yes", prompt_id="yes-2")
    ambiguous_id = str(ambiguous["receipt_id"])
    assert _one("SELECT extraction_state FROM trusted_input_receipts WHERE id = ?", (ambiguous_id,))["extraction_state"] == "extracted"
    candidates = _candidates_for_receipt(ambiguous_id)
    assert [(row["candidate_kind"], row["promotion"], row["status"]) for row in candidates] == [("acknowledgement", "discard", "discarded")]
    assert candidates[0]["opportunity_id"] is None
    assert _observations_for_receipt(ambiguous_id) == []
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE receipt_id = ?", (ambiguous_id,)) == 0
    assert _opportunity(first_id)["status"] == "open"
    assert _opportunity(second_id)["status"] == "open"
    _assert_frozen_pending(_shadow_for_opportunity(first_id))
    _assert_frozen_pending(_shadow_for_opportunity(second_id))

    # A generic ack is never a preference either: no pending-review observation is minted from it.
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE capture_receipt_id IS NOT NULL") == 1


# --------------------------------------------------------------------------- case 4: capability is credential-only


def test_executor_bearer_cannot_present_the_host_capture_capability(lite_client: TestClient) -> None:
    body = _capture_body("confirm")

    denied = _post_input(lite_client, body, _executor_headers())
    assert denied.status_code == 403, denied.text
    # The role header is caller-asserted; it must not mint the host capability.
    asserted_human = _post_input(lite_client, body, _executor_headers(role="user", user=_HUMAN))
    assert asserted_human.status_code == 403, asserted_human.text
    # Nor does naming yourself like the host, with no role header at all.
    impostor = _post_input(lite_client, body, _executor_headers("host-capture-claude", role=None, user=_HUMAN))
    assert impostor.status_code == 403, impostor.text
    # The operator identity (human role on the api token) is still not a trusted capture channel.
    operator = _post_input(lite_client, body, _operator_headers())
    assert operator.status_code == 403, operator.text

    assert _count("SELECT COUNT(*) FROM trusted_input_receipts") == 0
    assert _count("SELECT COUNT(*) FROM events WHERE task_type = 'human_input'") == 0

    # Positive control: the host credential, with the same subject, is accepted.
    accepted = _post_input(lite_client, body, _host_headers())
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["capture_principal"] == _HOST_PRINCIPAL


# --------------------------------------------------------------------------- case 5: /v1/events forgery


def test_human_origin_events_cannot_be_forged_by_an_executor(lite_client: TestClient) -> None:
    forged_variants = {
        "task_type": _human_input_event(source="cli", context={}),
        "source": _human_input_event(task_type="debug", context={}),
        "input_origin": _human_input_event(source="cli", task_type="debug", context={"input_origin": "human_input"}),
        "capture_principal": _human_input_event(source="cli", task_type="debug", context={"capture_principal": _HOST_PRINCIPAL}),
        "everything": _human_input_event(context={"input_origin": "human_input", "capture_principal": _HOST_PRINCIPAL}),
    }
    for name, event in forged_variants.items():
        forged = lite_client.post("/v1/events", json=event, headers=_executor_headers())
        assert forged.status_code == 403, f"{name}: {forged.status_code} {forged.text}"
        asserted = lite_client.post("/v1/events", json=event, headers=_executor_headers(role="user", user=_HUMAN))
        assert asserted.status_code == 403, f"{name} with X-TCE-Role=user: {asserted.status_code} {asserted.text}"
    assert _count("SELECT COUNT(*) FROM events WHERE task_type = 'human_input' OR source = 'tce-host-capture'") == 0

    # Controls: an ordinary executor event is still accepted, and the host credential may write human-origin events.
    ordinary = _human_input_event(source="cli", task_type="debug", context={})
    assert lite_client.post("/v1/events", json=ordinary, headers=_executor_headers()).status_code in {200, 201}
    allowed = lite_client.post("/v1/events", json=_human_input_event(context={"input_origin": "human_input"}), headers=_host_headers())
    assert allowed.status_code in {200, 201}, allowed.text
    assert _count("SELECT COUNT(*) FROM events WHERE task_type = 'human_input'") == 1


# --------------------------------------------------------------------------- case 6: forged explicit evidence


def test_forged_explicit_evidence_never_enters_the_trusted_path(lite_client: TestClient) -> None:
    _seed_prior_evidence(lite_client)
    session_id = _session()
    _, opportunity_id = _open_safety_question(lite_client, session_id)
    receipt = _capture(lite_client, "confirm", prompt_id="real")
    event_id = str(receipt["event_id"])
    assert _opportunity(opportunity_id)["status"] == "resolved"
    baseline_confirmed = _count("SELECT COUNT(*) FROM decision_observations WHERE confirmed_at IS NOT NULL")

    # The executor asserts explicit, self-confirmed evidence and cites the real human receipt event.
    forged_payload = {**_evidence_payload(7), "confirmed_at": datetime.now(tz=UTC).isoformat(), "source_event_ids": [event_id]}
    forged = lite_client.post("/v1/behavior/evidence", json=forged_payload, headers=_executor_headers())
    assert forged.status_code == 200, forged.text
    forged_body = forged.json()
    assert forged_body["learning_eligible"] is False
    assert forged_body["storage_decision"] == "pending_review"
    forged_row = _one("SELECT * FROM decision_observations WHERE id = ?", (str(forged_body["observation_id"]),))
    assert forged_row["evidence_source"] == "inferred"
    assert forged_row["confirmed_at"] is None
    assert int(forged_row["learning_eligible"]) == 0
    assert forged_row["capture_receipt_id"] is None, "an executor cannot bind its own text to a trusted receipt"
    assert forged_row["opportunity_id"] is None
    assert forged_row["origin_kind"] != "human_input"
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE observation_id = ?", (forged_row["id"],)) == 0
    # Its shadow prediction was made with the answer in hand: labelled retrospective, never pending.
    forged_shadow = _one("SELECT * FROM behavior_shadow_predictions WHERE id = ?", (str(forged_body["shadow_prediction_id"]),))
    assert forged_shadow["prediction_stage"] == "retrospective"
    assert forged_shadow["resolution_state"] == "resolved"
    assert forged_shadow["opportunity_id"] is None

    # Even a human-role manual submission never confirms: confirmation is reserved for the verified receipt path.
    manual = lite_client.post("/v1/behavior/evidence", json={**_evidence_payload(8), "source_event_ids": [event_id]}, headers=_operator_headers())
    assert manual.status_code == 200, manual.text
    manual_row = _one("SELECT * FROM decision_observations WHERE id = ?", (str(manual.json()["observation_id"]),))
    assert manual_row["confirmed_at"] is None
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE confirmed_at IS NOT NULL") == baseline_confirmed

    # A source event owned by another human subject is outside the caller's scope.
    other_receipt = _post_input(lite_client, _capture_body("confirm", prompt_id="other"), _host_headers(user=_OTHER_HUMAN))
    assert other_receipt.status_code == 201, other_receipt.text
    other_event_id = str(other_receipt.json()["event_id"])
    cross_scope = lite_client.post("/v1/behavior/evidence", json={**_evidence_payload(9), "source_event_ids": [other_event_id]}, headers=_operator_headers())
    assert cross_scope.status_code == 403, cross_scope.text
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE source_event_ids LIKE ?", (f"%{other_event_id}%",)) == 0


# --------------------------------------------------------------------------- case 7: credential configuration guards


def test_host_capture_credential_configuration_guards(lite_client: TestClient) -> None:
    settings = get_settings()
    body = _capture_body("confirm")

    missing = lite_client.post("/v1/inputs", json=body)
    assert missing.status_code == 401, missing.text

    # A host may not attest human input on behalf of a different subject.
    mismatched = _post_input(lite_client, body, _host_headers(user=_HUMAN, subject=_OTHER_HUMAN))
    assert mismatched.status_code == 403, mismatched.text

    # One credential configured as both api and host token is a config error: refused on every request.
    settings.api_tokens = f"{_API_TOKENS},{_HOST_TOKEN}"
    try:
        both = _post_input(lite_client, body, _host_headers())
        assert both.status_code == 403, both.text
        assert "both" in str(both.json()["detail"]).lower()
    finally:
        settings.api_tokens = _API_TOKENS

    # The well-known default token can never be a trusted capture credential.
    settings.host_capture_tokens = "local-dev-token"
    try:
        insecure = _post_input(lite_client, body, _host_headers(token="local-dev-token"))
        assert insecure.status_code == 403, insecure.text
        assert "default token" in str(insecure.json()["detail"]).lower()
    finally:
        settings.host_capture_tokens = _HOST_TOKEN

    assert _count("SELECT COUNT(*) FROM trusted_input_receipts") == 0
    ok = _post_input(lite_client, body, _host_headers())
    assert ok.status_code == 201, ok.text


# --------------------------------------------------------------------------- case 8: enforce mode


def test_enforce_mode_requires_a_bound_host_claim_with_the_capability(bound_client: TestClient) -> None:
    body = _capture_body("confirm")

    unbound = _post_input(bound_client, body, _host_headers(token=_UNBOUND_HOST_TOKEN))
    assert unbound.status_code == 403, unbound.text
    bound_executor = _post_input(bound_client, body, _executor_headers())
    assert bound_executor.status_code == 403, bound_executor.text

    bound_host = _post_input(bound_client, body, _host_headers())
    assert bound_host.status_code == 201, bound_host.text
    receipt = bound_host.json()
    assert receipt["capture_principal"] == _HOST_PRINCIPAL
    row = _one("SELECT * FROM trusted_input_receipts WHERE id = ?", (str(receipt["receipt_id"]),))
    assert row["subject_user_id"] == _HUMAN
    assert row["workspace_id"] == _WORKSPACE


# --------------------------------------------------------------------------- case 9: unattended pause


def test_unattended_profile_pauses_mutation_while_capture_channel_is_unavailable(lite_client: TestClient) -> None:
    get_settings().takeover_autonomy_policy_default = "human_aggressive"
    executor = _executor_headers()

    # A mutating directive is minted, but it cannot be claimed while the audit channel has never delivered.
    session_id = _session()
    permit = _request_permit(lite_client, session_id, executor)
    assert permit["decision"] == "allow", permit
    activation = _step(lite_client, session_id, executor, task=_MUTATING_TASK)
    assert activation["capture_delivery_state"] == "unavailable", activation
    directive_id = activation.get("directive_id")
    assert directive_id, activation
    blocked = _claim_raw(lite_client, session_id, str(directive_id), executor)
    assert blocked.status_code == 409, blocked.text
    assert _CAPTURE_PAUSE_PREFIX in json.dumps(blocked.json()["detail"])
    assert _execution_status(lite_client, session_id, executor)["capture_delivery_state"] == "unavailable"

    # A turn with no permit/claim lifecycle pause surfaces the pause to the executor as needs_human.
    paused = _step(lite_client, _session(), executor, task=_READ_ONLY_TASK)
    assert paused["needs_human"] is True, paused
    assert str(paused.get("final_response") or "").startswith(_CAPTURE_PAUSE_PREFIX), paused.get("final_response")
    assert paused["safety_decision"] == "allow"

    # One delivered receipt restores the channel: the claim proceeds and the status reflects it.
    _capture(lite_client, "carry on with the changelog", prompt_id="restore")
    assert _execution_status(lite_client, session_id, executor)["capture_delivery_state"] == "healthy"
    claimed = _claim_raw(lite_client, session_id, str(directive_id), executor)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["state"] == "in_progress"

    # The consultative profile never pauses on the channel: the audit requirement is for unattended mutation only.
    get_settings().takeover_autonomy_policy_default = "human_consultative"
    consultative = _step(lite_client, _session(), _executor_headers(subject=_OTHER_HUMAN), task=_READ_ONLY_TASK)
    assert consultative["capture_delivery_state"] == "unavailable"
    assert not str(consultative.get("final_response") or "").startswith(_CAPTURE_PAUSE_PREFIX)


# --------------------------------------------------------------------------- case 10: operator permit resolution


def test_operator_permit_resolution_resolves_the_open_safety_question(bound_client: TestClient) -> None:
    """The verified path, driven by a server-bound human in enforce identity mode."""
    _seed_prior_evidence(bound_client)
    executor = _executor_headers()
    session_id = _session()
    _, opportunity_id = _open_safety_question(bound_client, session_id, executor)
    frozen_at = _assert_frozen_pending(_shadow_for_opportunity(opportunity_id))

    permit = _request_permit(bound_client, session_id, executor, target="infra/docker-compose.yml")
    assert permit["decision"] == "confirm_required", permit
    permit_id = str(permit["permit_id"])

    # An executor cannot resolve the permit, so it cannot resolve the question through it either.
    assert _resolve_permit(bound_client, permit_id, executor).status_code == 403
    assert _opportunity(opportunity_id)["status"] == "open"
    _assert_frozen_pending(_shadow_for_opportunity(opportunity_id))
    assert _count("SELECT COUNT(*) FROM human_resolutions") == 0

    approved = _resolve_permit(bound_client, permit_id, _verified_operator_headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["decision"] == "allow"

    opportunity = _opportunity(opportunity_id)
    assert opportunity["status"] == "resolved"
    assert opportunity["resolved_at"]
    resolution = _one("SELECT * FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,))
    assert resolution["resolution_source"] == "operator_permit"
    assert resolution["human_source_ref"] == permit_id
    assert resolution["selected_choice"] == "confirm"
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["resolution_state"] == "resolved"
    assert shadow["resolution_source"] == "operator_permit"
    assert shadow["human_source_ref"] == permit_id
    assert shadow["actual_choice"] == "confirm"
    assert int(shadow["correct"]) == 1
    assert shadow["prediction_stage"] == "prospective"
    assert _ts(shadow["frozen_at"]) == frozen_at
    # A permit answer is a resolution, not evidence: no observation is minted for it.
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE opportunity_id = ?", (opportunity_id,)) == 0
    assert resolution["observation_id"] is None


def test_unverified_operator_permit_resolution_is_audited_but_never_labels(lite_client: TestClient) -> None:
    """The same call from a header-asserted human (compat ``X-TCE-Role: user`` on the executor's bearer).

    The permit decision itself is the caller's to make; what it must never do is answer the frozen safety
    question. The answer is kept for audit as ``operator_unverified``, nothing is confirmed, and the
    prediction stays unscored -- otherwise the executor grades its own forecast.
    """
    _seed_prior_evidence(lite_client)
    executor = _executor_headers()
    session_id = _session()
    _, opportunity_id = _open_safety_question(lite_client, session_id, executor)
    frozen_at = _assert_frozen_pending(_shadow_for_opportunity(opportunity_id))

    permit = _request_permit(lite_client, session_id, executor, target="infra/docker-compose.yml")
    assert permit["decision"] == "confirm_required", permit
    permit_id = str(permit["permit_id"])

    # The permit decision itself still stands (the caller holds the USER role) ...
    unverified = _resolve_permit(lite_client, permit_id, _operator_headers())
    assert unverified.status_code == 200, unverified.text
    assert unverified.json()["decision"] == "allow", unverified.text

    resolution = _one("SELECT * FROM human_resolutions WHERE opportunity_id = ?", (opportunity_id,))
    assert resolution["resolution_source"] == "operator_unverified", dict(resolution)
    assert resolution["human_source_ref"] == permit_id
    assert resolution["observation_id"] is None
    if "confirmed_at" in resolution.keys():
        assert resolution["confirmed_at"] is None, dict(resolution)
    assert _count("SELECT COUNT(*) FROM human_resolutions WHERE resolution_source = 'operator_permit'") == 0
    # Nothing was confirmed and no evidence was minted from the unverified answer.
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE opportunity_id = ?", (opportunity_id,)) == 0
    assert _count("SELECT COUNT(*) FROM decision_observations WHERE confirmed_at IS NOT NULL") == 0
    # The question is still open and its prediction is still unscored, waiting for a real human answer.
    opportunity = _opportunity(opportunity_id)
    assert opportunity["status"] == "open", dict(opportunity)
    assert opportunity["resolved_at"] is None, dict(opportunity)
    assert _assert_frozen_pending(_shadow_for_opportunity(opportunity_id)) == frozen_at
    assert _shadow_for_opportunity(opportunity_id)["resolution_source"] is None


# --------------------------------------------------------------------------- case 11: stand-down abandons


def test_stand_down_abandons_open_questions(lite_client: TestClient) -> None:
    _seed_prior_evidence(lite_client)
    session_id = _session()
    _, opportunity_id = _open_safety_question(lite_client, session_id)

    stopped = _step(lite_client, session_id, message="beru stand down", task=None)
    assert stopped["state"]["active"] is False, stopped
    assert stopped["open_decision_opportunity_id"] is None
    assert _opportunity(opportunity_id)["status"] == "abandoned"
    shadow = _shadow_for_opportunity(opportunity_id)
    assert shadow["resolution_state"] == "abandoned"
    assert str(shadow["actual_choice"] or "") == ""
    assert shadow["correct"] is None

    # A later human 'confirm' has no open question to answer: it is an acknowledgement, not evidence.
    receipt = _capture(lite_client, "confirm", prompt_id="after-stand-down")
    candidates = _candidates_for_receipt(str(receipt["receipt_id"]))
    assert [(row["candidate_kind"], row["promotion"]) for row in candidates] == [("acknowledgement", "discard")]
    assert _observations_for_receipt(str(receipt["receipt_id"])) == []
    assert _opportunity(opportunity_id)["status"] == "abandoned"
    assert _shadow_for_opportunity(opportunity_id)["resolution_state"] == "abandoned"

    # Abandoned rows sit in their own denominator, never in precision.
    metrics = lite_client.get("/v1/behavior/shadow/status", headers=_operator_headers()).json()["metrics"]
    assert metrics["abandoned_count"] == 1
    assert metrics["resolved_count"] == 3, "only the three seeded (retrospective) rows are resolved"
    assert metrics["prospective_count"] == 1


# --------------------------------------------------------------------------- case 12: minting evidence is identity-gated


def test_header_asserted_human_cannot_mint_learning_eligible_evidence(lite_client: TestClient) -> None:
    """``X-TCE-Role: user`` on the executor's own bearer is an assertion, not an identity.

    Without this gate the executor mints explicit, learning-eligible evidence about its owner in a single
    call -- the self-reinforcement loop where the system learns its own output and calls it a preference.
    """
    asserted = lite_client.post("/v1/behavior/evidence", json=_evidence_payload(30), headers=_operator_headers())
    assert asserted.status_code == 200, asserted.text
    asserted_body = asserted.json()
    assert asserted_body["stored"] is True, asserted_body
    assert asserted_body["learning_eligible"] is False, asserted_body
    assert asserted_body["storage_decision"] == "pending_review", asserted_body
    assert "identity_unverified" in asserted_body["storage_reasons"], asserted_body
    assert asserted_body["review_id"], asserted_body
    asserted_row = _one("SELECT * FROM decision_observations WHERE id = ?", (str(asserted_body["observation_id"]),))
    assert int(asserted_row["learning_eligible"]) == 0, dict(asserted_row)
    assert asserted_row["storage_decision"] == "pending_review", dict(asserted_row)
    assert asserted_row["confirmed_at"] is None, dict(asserted_row)
    # It is held out of the learning corpus, not merely labelled.
    eligible = _all("SELECT id FROM decision_observations WHERE learning_eligible = 1")
    assert str(asserted_body["observation_id"]) not in {str(row["id"]) for row in eligible}

    # The same payload from the server-bound human is the control: identity is what changed, nothing else.
    verified = lite_client.post("/v1/behavior/evidence", json=_evidence_payload(31), headers=_verified_operator_headers())
    assert verified.status_code == 200, verified.text
    verified_body = verified.json()
    assert verified_body["learning_eligible"] is True, verified_body
    assert verified_body["storage_decision"] == "learn", verified_body
    assert "identity_unverified" not in verified_body["storage_reasons"], verified_body
    verified_row = _one("SELECT * FROM decision_observations WHERE id = ?", (str(verified_body["observation_id"]),))
    assert int(verified_row["learning_eligible"]) == 1, dict(verified_row)
    assert verified_row["evidence_source"] == "explicit", dict(verified_row)
    # A human's manual submission still never self-confirms: confirmation is the receipt path's alone.
    assert verified_row["confirmed_at"] is None, dict(verified_row)


# --------------------------------------------------------------------------- case 13: promotion is identity-gated


def test_pending_review_is_promoted_only_by_a_verified_human(lite_client: TestClient) -> None:
    """Resolving a review promotes a pending row into learning-eligible evidence: the same gate as minting it.

    Otherwise the unverified caller round-trips its own held row past the identity gate in one extra call.
    """
    held = lite_client.post("/v1/behavior/evidence", json=_evidence_payload(40), headers=_operator_headers())
    assert held.status_code == 200, held.text
    held_body = held.json()
    assert held_body["learning_eligible"] is False, held_body
    review_id = str(held_body["review_id"])
    observation_id = str(held_body["observation_id"])

    refused = lite_client.post(
        f"/v1/behavior/reviews/{review_id}/resolve",
        json={"decision": "promote", "note": "looks right to me"},
        headers=_operator_headers(),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["error"] == "behavior_review_promotion_rejected", refused.text
    assert "identity_unverified" in refused.json()["detail"]["reasons"], refused.text
    refused_executor = lite_client.post(
        f"/v1/behavior/reviews/{review_id}/resolve",
        json={"decision": "promote", "note": "self-promotion"},
        headers=_executor_headers(),
    )
    assert refused_executor.status_code == 403, refused_executor.text
    assert "non_human_caller" in refused_executor.json()["detail"]["reasons"], refused_executor.text
    still_held = _one("SELECT * FROM decision_observations WHERE id = ?", (observation_id,))
    assert int(still_held["learning_eligible"]) == 0, dict(still_held)
    assert still_held["storage_decision"] == "pending_review", dict(still_held)
    assert _one("SELECT status FROM behavior_memory_reviews WHERE id = ?", (review_id,))["status"] == "pending"

    promoted = lite_client.post(
        f"/v1/behavior/reviews/{review_id}/resolve",
        json={"decision": "promote", "note": "reviewed by the owner"},
        headers=_verified_operator_headers(),
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "promoted", promoted.text
    assert _one("SELECT status FROM behavior_memory_reviews WHERE id = ?", (review_id,))["status"] == "promoted"


# --------------------------------------------------------------------------- case 14: project_hint is untrusted input


def test_project_hint_credentials_are_redacted_server_side(lite_client: TestClient) -> None:
    """The hook redacts the hint client-side; the server does not trust that, and the client can simply not do it.

    A credential URL hides in exactly the keys ('repo', 'project', 'branch') a content allowlist would spare.
    """
    secret = "ghp_0123456789abcdefghij"
    remote = f"https://joel:{secret}@github.com/owner/open-timeline-engine.git"
    body = _capture_body("confirm", prompt_id="hint")
    body["project_hint"] = {
        "project": "open-timeline-engine",
        "project_root": "/work/open-timeline-engine",
        "git_remote": remote,
        "branch": f"feature/{secret}",
        "nested": {"repo": remote},
    }
    response = _post_input(lite_client, body)
    assert response.status_code == 201, response.text
    receipt_id = str(response.json()["receipt_id"])
    event_id = str(response.json()["event_id"])

    receipt = _one("SELECT * FROM trusted_input_receipts WHERE id = ?", (receipt_id,))
    applied = _json(receipt["redaction_applied_json"])
    assert "url_credentials" in applied, applied
    assert "token" in applied, "an allowlisted key ('branch') must not spare a bare credential"

    event = _one("SELECT * FROM events WHERE id = ?", (event_id,))
    context = _json(event["context"])
    assert context["git_remote"] == "https://<REDACTED:URL_CREDENTIALS>github.com/owner/open-timeline-engine.git", context
    assert context["branch"] == "feature/<REDACTED:TOKEN>", context
    assert context["project"] == "open-timeline-engine", context
    # Nothing anywhere in the durable record still carries the credential.
    assert secret not in json.dumps(dict(event)), dict(event)
    assert secret not in json.dumps({key: receipt[key] for key in receipt.keys()}), dict(receipt)
    assert _count("SELECT COUNT(*) FROM events WHERE context LIKE ?", (f"%{secret}%",)) == 0
    assert _count("SELECT COUNT(*) FROM audit_log WHERE query LIKE ?", (f"%{secret}%",)) == 0


# --------------------------------------------------------------------------- case 15: host clock clamp


def test_host_clock_running_ahead_is_clamped_and_cannot_forge_a_prospective_answer(lite_client: TestClient) -> None:
    """``observed_at`` is host-supplied. Clamped to server time, a clock 10 minutes ahead buys nothing.

    Sequence (the forged-consent shape): the human's message is captured BEFORE the safety question exists,
    stamped 10 minutes in the future; the question is frozen a moment later; extraction then runs. The future
    stamp is what would out-date the freeze -- putting an answer the human never gave to that question into
    the exit gate's prospective numerator -- so it never survives ingestion in the first place.
    """
    settings = get_settings()
    _seed_prior_evidence(lite_client)

    # Capture first, with extraction deferred, so the message provably predates the question.
    settings.capture_extraction_enabled = False
    try:
        sent_at = datetime.now(tz=UTC) + timedelta(minutes=10)
        skewed = _capture(lite_client, "confirm", prompt_id="skew", observed_at=sent_at)
    finally:
        settings.capture_extraction_enabled = True
    receipt_id = str(skewed["receipt_id"])
    # The clamp is visible in the acknowledged receipt, in the durable row, and on the human-origin event.
    assert _ts(skewed["observed_at"]) <= _ts(skewed["ingested_at"]), skewed
    assert _ts(skewed["observed_at"]) < sent_at - timedelta(minutes=5), skewed
    receipt = _one("SELECT * FROM trusted_input_receipts WHERE id = ?", (receipt_id,))
    assert receipt["extraction_state"] == "pending", dict(receipt)
    observed_at, ingested_at = _ts(receipt["observed_at"]), _ts(receipt["ingested_at"])
    assert observed_at <= ingested_at, f"host-supplied observed_at was not clamped: {observed_at} > {ingested_at}"
    assert ingested_at - observed_at < timedelta(seconds=30), dict(receipt)
    event = _one("SELECT * FROM events WHERE id = ?", (str(skewed["event_id"]),))
    assert _ts(event["ts"]) <= ingested_at, dict(event)
    assert _ts(_json(event["context"])["observed_at"]) == observed_at, dict(event)

    session_id = _session()
    _, opportunity_id = _open_safety_question(lite_client, session_id)
    frozen_at = _assert_frozen_pending(_shadow_for_opportunity(opportunity_id))
    assert observed_at < frozen_at, "precondition: the message really was captured before the question existed"

    # The deferred receipt is swept on startup: the moment a future stamp would have paid off. It answers
    # nothing -- the judging moment is min(observed_at, ingested_at), and both now sit before the freeze.
    with TestClient(app):
        pass

    assert _one("SELECT extraction_state FROM trusted_input_receipts WHERE id = ?", (receipt_id,))["extraction_state"] == "extracted"
    candidates = _candidates_for_receipt(receipt_id)
    assert [row["promotion"] for row in candidates] == ["discard"], [dict(row) for row in candidates]
    assert _observations_for_receipt(receipt_id) == []
    assert _count("SELECT COUNT(*) FROM human_resolutions") == 0
    assert _opportunity(opportunity_id)["status"] == "open"
    assert _assert_frozen_pending(_shadow_for_opportunity(opportunity_id)) == frozen_at

    metrics = lite_client.get("/v1/behavior/shadow/status", headers=_operator_headers()).json()["metrics"]
    assert metrics["prospective_count"] == 1
    assert metrics["pending_count"] == 1
    assert metrics["resolved_count"] == 3, "only the three seeded (retrospective) rows are resolved"
    assert metrics["prospective_precision"] is None, "no prospective row was ever answered"
