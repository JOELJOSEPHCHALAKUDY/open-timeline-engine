from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_lite_api.store import (
    _query_similar_observations_lite,
    load_behavior_evidence_lite,
    run_lifecycle_maintenance,
)


def _headers(
    *,
    consumer: str = "behavior-test-user",
    user: str = "behavior-test-user",
    behavior_subject: str | None = None,
) -> dict[str, str]:
    return {
        "Authorization": "Bearer behavior-test-token",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "user",
        "X-TCE-Workspace": "behavior-test-workspace",
        "X-TCE-User": user,
        "X-TCE-Behavior-Subject": behavior_subject or user,
    }


def _payload(index: int, *, choice: str = "minimal verified fix") -> dict:
    return {
        "situation_type": "prioritization_needed",
        "situation_summary": f"Choose scope for production bug {index}",
        "objective": "Fix production bug without broad regression",
        "context_snapshot": {"component": "api"},
        "constraints": {"risk": "production"},
        "available_choices": ["minimal verified fix", "broad refactor"],
        "selected_choice": choice,
        "rationale": "Keep the change reversible and verify the affected package",
        "action_taken": "patch focused module and run scoped tests",
        "outcome": "tests passed",
        "outcome_sentiment": "positive",
        "memory_class": "preference",
        "evidence_source": "explicit",
        "confidence": 0.95,
    }


@pytest.fixture()
def client(tmp_path) -> Generator[TestClient, None, None]:
    settings = get_settings()
    original = {
        "lite_db_path": settings.lite_db_path,
        "api_tokens": settings.api_tokens,
        "behavior_storage_gate_mode": settings.behavior_storage_gate_mode,
        "behavior_autonomy_gate_enabled": settings.behavior_autonomy_gate_enabled,
        "behavior_calibration_enabled": settings.behavior_calibration_enabled,
        "behavior_control_retention_days": settings.behavior_control_retention_days,
        "event_lifecycle_enabled": settings.event_lifecycle_enabled,
        "lifecycle_dry_run": settings.lifecycle_dry_run,
        "archive_enabled": settings.archive_enabled,
        "workspace_access_mode": settings.workspace_access_mode,
    }
    settings.lite_db_path = str(tmp_path / "behavior-fidelity.db")
    settings.api_tokens = "behavior-test-token"
    settings.behavior_storage_gate_mode = "shadow"
    settings.behavior_autonomy_gate_enabled = False
    settings.behavior_calibration_enabled = True
    settings.workspace_access_mode = "compat"
    with TestClient(app) as test_client:
        yield test_client
    for key, value in original.items():
        setattr(settings, key, value)


def test_evidence_is_redacted_and_correction_supersedes_prior_record(client: TestClient) -> None:
    first_payload = _payload(1)
    first_payload["rationale"] = "Use token=plain-secret-value-12345 only in memory"
    first = client.post("/v1/behavior/evidence", json=first_payload, headers=_headers())
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["learning_eligible"] is True
    assert first_body["redaction_applied"] is True

    correction_payload = _payload(2, choice="broad refactor")
    correction_payload.update(
        {
            "evidence_source": "correction",
            "correction_text": "The minimal patch did not address the shared invariant",
            "supersedes_observation_id": first_body["observation_id"],
        }
    )
    correction = client.post("/v1/behavior/evidence", json=correction_payload, headers=_headers())
    assert correction.status_code == 200

    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        old_row = conn.execute(
            "SELECT superseded_by, lifecycle_status, response_reasoning FROM decision_observations WHERE id = ?",
            (first_body["observation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert old_row["superseded_by"] == correction.json()["observation_id"]
    assert old_row["lifecycle_status"] == "superseded"
    assert "plain-secret-value-12345" not in old_row["response_reasoning"]


def test_prediction_evaluation_and_calibration_flow(client: TestClient) -> None:
    for index in range(30):
        response = client.post("/v1/behavior/evidence", json=_payload(index), headers=_headers())
        assert response.status_code == 200, response.text

    prediction = client.post(
        "/v1/behavior/predict",
        json={
            "situation_type": "prioritization_needed",
            "situation_summary": "Choose scope for another production bug",
            "objective": "Fix production bug without broad regression",
            "candidate_choices": ["minimal verified fix", "broad refactor"],
        },
        headers=_headers(),
    )
    assert prediction.status_code == 200
    assert prediction.json()["predicted_choice"] == "minimal verified fix"
    assert prediction.json()["needs_clarification"] is False

    evaluation = client.post(
        "/v1/behavior/evaluate",
        json={"holdout_ratio": 0.5, "min_train": 5, "max_cases": 100},
        headers=_headers(),
    )
    assert evaluation.status_code == 200
    evaluation_body = evaluation.json()
    assert evaluation_body["metrics"]["evaluation_count"] == 15
    assert evaluation_body["metrics"]["top1_accuracy"] == 1.0

    history = client.get("/v1/behavior/evaluations", headers=_headers())
    assert history.status_code == 200
    assert history.json()["runs"][0]["run_id"] == evaluation_body["run_id"]

    scenarios = client.get("/v1/behavior/calibration/scenarios", headers=_headers())
    assert scenarios.status_code == 200
    scenario = scenarios.json()["scenarios"][0]
    answer = client.post(
        "/v1/behavior/calibration/answer",
        json={
            "scenario_id": scenario["id"],
            "selected_choice": scenario["choices"][0],
            "rationale": "I prefer verified and reversible production changes",
        },
        headers=_headers(),
    )
    assert answer.status_code == 200
    assert answer.json()["learning_eligible"] is True


def test_behavior_is_shared_across_executors_but_isolated_between_users(client: TestClient) -> None:
    codex_headers = _headers(
        consumer="codex-executor", user="codex-owner", behavior_subject="human-a"
    )
    claude_headers = _headers(
        consumer="claude-executor", user="claude-owner", behavior_subject="human-a"
    )
    other_user_headers = _headers(
        consumer="claude-executor", user="claude-owner", behavior_subject="human-b"
    )
    first_observation_id = ""
    for index in range(6):
        response = client.post("/v1/behavior/evidence", json=_payload(index), headers=codex_headers)
        assert response.status_code == 200
        first_observation_id = first_observation_id or response.json()["observation_id"]

    prediction_request = {
        "situation_type": "prioritization_needed",
        "situation_summary": "Choose scope for another production bug",
        "objective": "Fix production bug without broad regression",
        "candidate_choices": ["minimal verified fix", "broad refactor"],
    }
    shared = client.post("/v1/behavior/predict", json=prediction_request, headers=claude_headers)
    assert shared.status_code == 200
    assert shared.json()["predicted_choice"] == "minimal verified fix"

    isolated = client.post("/v1/behavior/predict", json=prediction_request, headers=other_user_headers)
    assert isolated.status_code == 200
    assert isolated.json()["predicted_choice"] is None
    assert isolated.json()["abstained"] is True

    correction = _payload(99, choice="broad refactor")
    correction.update(
        {
            "evidence_source": "correction",
            "correction_text": "Use a broad refactor for this invariant",
            "supersedes_observation_id": first_observation_id,
        }
    )
    forbidden_supersession = client.post(
        "/v1/behavior/evidence",
        json=correction,
        headers=other_user_headers,
    )
    assert forbidden_supersession.status_code == 404


def test_clone_recall_excludes_audit_only_evidence(client: TestClient) -> None:
    payload = _payload(1)
    payload.update({"evidence_source": "backfill", "confidence": 0.0, "rationale": ""})
    stored = client.post("/v1/behavior/evidence", json=payload, headers=_headers())
    assert stored.status_code == 200
    assert stored.json()["learning_eligible"] is False

    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        recalled = _query_similar_observations_lite(
            conn,
            workspace_id="behavior-test-workspace",
            subject_user_id="behavior-test-user",
            situation_type="prioritization_needed",
            situation_text="production bug scope",
            settings=settings,
        )
    finally:
        conn.close()
    assert recalled == []


def test_capability_grants_are_exact_one_use_and_fail_closed(client: TestClient) -> None:
    grant = client.post(
        "/v1/capabilities/grants",
        json={
            "capability": "filesystem.read",
            "action": "open",
            "resource": "src/app.py",
            "arguments": {"line": 10},
        },
        headers=_headers(),
    )
    assert grant.status_code == 200
    grant_body = grant.json()
    assert grant_body["decision"] == "allow"
    assert grant_body["token"]

    mismatched = client.post(
        "/v1/capabilities/consume",
        json={
            "grant_id": grant_body["grant_id"],
            "token": grant_body["token"],
            "capability": "filesystem.read",
            "action": "open",
            "resource": "src/app.py",
            "arguments": {"line": 11},
        },
        headers=_headers(),
    )
    assert mismatched.status_code == 200
    assert mismatched.json()["authorized"] is False

    exact_payload = {
        "grant_id": grant_body["grant_id"],
        "token": grant_body["token"],
        "capability": "filesystem.read",
        "action": "open",
        "resource": "src/app.py",
        "arguments": {"line": 10},
    }
    consumed = client.post("/v1/capabilities/consume", json=exact_payload, headers=_headers())
    replayed = client.post("/v1/capabilities/consume", json=exact_payload, headers=_headers())
    assert consumed.json()["authorized"] is True
    assert replayed.json()["authorized"] is False

    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    try:
        token_hash = conn.execute(
            "SELECT token_hash FROM capability_grants WHERE id = ?",
            (grant_body["grant_id"],),
        ).fetchone()[0]
        audit_rows = conn.execute(
            """
            SELECT action, query, policy_decisions FROM audit_log
            WHERE action IN ('capability_grant_create', 'capability_grant_consume')
            ORDER BY ts ASC
            """
        ).fetchall()
    finally:
        conn.close()
    assert token_hash != grant_body["token"]
    assert grant_body["token"] not in token_hash
    serialized_audit = json.dumps(audit_rows)
    assert grant_body["token"] not in serialized_audit
    assert '"arguments"' not in serialized_audit
    assert {row[0] for row in audit_rows} == {
        "capability_grant_create",
        "capability_grant_consume",
    }

    unknown = client.post(
        "/v1/capabilities/grants",
        json={"capability": "shell.root", "action": "run", "resource": "/"},
        headers=_headers(),
    )
    assert unknown.json()["decision"] == "blocked"
    assert unknown.json()["token"] is None

    blocked_write = client.post(
        "/v1/capabilities/grants",
        json={"capability": "filesystem.write", "action": "patch", "resource": "src/app.py"},
        headers=_headers(),
    )
    assert blocked_write.json()["decision"] == "blocked"

    permit_id = str(uuid.uuid4())
    directive_id = str(uuid.uuid4())
    conn = sqlite3.connect(settings.lite_db_path)
    try:
        conn.execute(
            """
            INSERT INTO execution_permits(
                id, session_id, workspace_id, action_kind, target_paths, command_preview,
                estimated_change_size, decision, reason, confirmed_by, expires_at, created_at, resolved_at
            ) VALUES(?, 'broker-session', 'behavior-test-workspace', 'edit', '["src/app.py"]',
                     NULL, 1, 'allow', 'test', 'behavior-test-user',
                     '2099-01-01T00:00:00+00:00', '2026-07-20T00:00:00+00:00', NULL)
            """,
            (permit_id,),
        )
        conn.execute(
            """
            INSERT INTO directive_executions(
                directive_id, session_id, workspace_id, user_id, goal_id, objective_hash,
                action_kind, attempt, state, requires_permit, permit_id, claimed_by,
                started_at, finished_at, expires_at, failure_class, failure_reason,
                retry_strategy, meta, created_at, updated_at
            ) VALUES(?, 'broker-session', 'behavior-test-workspace', 'behavior-test-user', NULL, NULL,
                     'edit', 1, 'in_progress', 1, ?, 'behavior-test-user',
                     '2026-07-20T00:00:00+00:00', NULL, '2099-01-01T00:00:00+00:00', NULL, NULL,
                     NULL, '{}', '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00')
            """,
            (directive_id, permit_id),
        )
        conn.commit()
    finally:
        conn.close()
    allowed_write = client.post(
        "/v1/capabilities/grants",
        json={
            "session_id": "broker-session",
            "directive_id": directive_id,
            "permit_id": permit_id,
            "capability": "filesystem.write",
            "action": "patch",
            "resource": "src/app.py",
            "arguments": {"patch_hash": "abc123"},
        },
        headers=_headers(),
    )
    assert allowed_write.status_code == 200
    assert allowed_write.json()["decision"] == "allow"


def test_inferred_memory_requires_promotion_and_shadow_eval_is_prospective(client: TestClient) -> None:
    payload = _payload(501)
    payload.update({"evidence_source": "inferred", "outcome": "verified success", "action_taken": "patch then test"})
    stored = client.post("/v1/behavior/evidence", json=payload, headers=_headers())
    assert stored.status_code == 200, stored.text
    body = stored.json()
    assert body["learning_eligible"] is False
    assert body["storage_decision"] == "pending_review"
    assert body["review_id"]
    assert body["shadow_prediction_id"]

    reviews = client.get("/v1/behavior/reviews", headers=_headers())
    assert reviews.status_code == 200
    assert reviews.json()["reviews"][0]["target_id"] == body["observation_id"]

    promoted = client.post(
        f"/v1/behavior/reviews/{body['review_id']}/resolve",
        json={"decision": "promote", "note": "Observed and verified by the user"},
        headers=_headers(),
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"

    shadow = client.get("/v1/behavior/shadow/status", headers=_headers())
    assert shadow.status_code == 200
    assert shadow.json()["metrics"]["sample_count"] >= 1
    assert shadow.json()["recent"][0]["observation_id"] == body["observation_id"]


def test_process_models_are_review_gated(client: TestClient) -> None:
    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    try:
        for session_id in ("process-a", "process-b"):
            for turn, action in enumerate(("diagnose", "patch", "verify"), start=1):
                conn.execute(
                    """
                    INSERT INTO takeover_action_log(
                        id, session_id, workspace_id, user_id, turn, objective_hash,
                        action_kind, result, latency_ms, meta, ts
                    ) VALUES(?, ?, ?, ?, ?, NULL, ?, 'success', 1, '{}', ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        session_id,
                        "behavior-test-workspace",
                        "behavior-test-user",
                        turn,
                        action,
                        f"2026-07-20T10:0{turn}:00+00:00",
                    ),
                )
        conn.commit()
    finally:
        conn.close()

    mined = client.post(
        "/v1/behavior/processes/mine",
        json={"lookback_days": 30, "min_support": 2, "max_sequences": 100, "max_steps": 10},
        headers=_headers(),
    )
    assert mined.status_code == 200, mined.text
    models = mined.json()["models"]
    assert models
    assert models[0]["steps"] == ["diagnose", "patch", "verify"]
    assert models[0]["status"] == "candidate"
    assert models[0]["review_id"]

    promoted = client.post(
        f"/v1/behavior/reviews/{models[0]['review_id']}/resolve",
        json={"decision": "promote", "note": "Validated workflow"},
        headers=_headers(),
    )
    assert promoted.status_code == 200
    active = client.get("/v1/behavior/processes?status=active", headers=_headers())
    assert active.status_code == 200
    assert active.json()["models"][0]["process_id"] == models[0]["process_id"]
    conn = sqlite3.connect(settings.lite_db_path)
    try:
        workflow = conn.execute(
            "SELECT graph, triggers FROM workflow_templates WHERE domain = 'behavior' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert workflow is not None
    assert json.loads(workflow[0])["steps"] == ["diagnose", "patch", "verify"]


def test_counterfactuals_are_redacted_and_resolvable(client: TestClient) -> None:
    created = client.post(
        "/v1/behavior/counterfactuals",
        json={
            "session_id": "counterfactual-test",
            "decision": "Apply the minimal patch",
            "alternative": "Use token=plain-secret-value-12345 during a broad rewrite",
            "expected_outcome": "Potentially remove the shared invariant",
            "assumptions": ["The API remains compatible"],
            "confidence": 0.4,
        },
        headers=_headers(),
    )
    assert created.status_code == 200, created.text
    record = created.json()
    assert record["redaction_applied"] is True
    assert "plain-secret-value-12345" not in record["alternative"]

    resolved = client.post(
        f"/v1/behavior/counterfactuals/{record['counterfactual_id']}/resolve",
        json={
            "assessment": "refuted",
            "observed_outcome": "The minimal patch passed all checks",
            "lesson": "Keep the smaller verified change",
            "regret_score": 0.1,
        },
        headers=_headers(),
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    listed = client.get("/v1/behavior/counterfactuals?status=resolved", headers=_headers())
    assert listed.status_code == 200
    assert listed.json()["records"][0]["assessment"] == "refuted"


def test_behavior_control_retention_preserves_unresolved_and_active_records(client: TestClient) -> None:
    settings = get_settings()
    settings.event_lifecycle_enabled = True
    settings.lifecycle_dry_run = False
    settings.archive_enabled = False
    settings.behavior_control_retention_days = 1
    old = "2020-01-01T00:00:00+00:00"
    future = "2099-01-01T00:00:00+00:00"
    ids = {name: str(uuid.uuid4()) for name in (
        "grant",
        "shadow",
        "resolved_review",
        "pending_review",
        "resolved_counterfactual",
        "open_counterfactual",
        "rejected_process",
        "active_process",
    )}
    conn = sqlite3.connect(settings.lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            INSERT INTO capability_grants(
                id, workspace_id, owner_id, session_id, capability, action, resource,
                action_digest, token_hash, status, decision, reason, risk_tier,
                mutating, redaction_applied, created_at, expires_at
            ) VALUES(?, 'behavior-test-workspace', 'behavior-test-user', 'retention',
                     'filesystem.read', 'open', 'src/app.py', 'digest', 'hash',
                     'authorized', 'allow', 'registered capability', 'low', 0, 0, ?, ?)
            """,
            (ids["grant"], old, future),
        )
        conn.execute(
            """
            INSERT INTO behavior_shadow_predictions(
                id, workspace_id, subject_user_id, actual_choice, confidence,
                abstained, evidence_count, latency_ms, created_at
            ) VALUES(?, 'behavior-test-workspace', 'behavior-test-user', 'choice', 0.0, 1, 0, 1, ?)
            """,
            (ids["shadow"], old),
        )
        for key, status, resolved_at in (
            ("resolved_review", "promoted", old),
            ("pending_review", "pending", None),
        ):
            conn.execute(
                """
                INSERT INTO behavior_memory_reviews(
                    id, workspace_id, subject_user_id, target_type, target_id, title,
                    status, source, created_at, resolved_at
                ) VALUES(?, 'behavior-test-workspace', 'behavior-test-user', 'evidence', ?,
                         'retention review', ?, 'test', ?, ?)
                """,
                (ids[key], str(uuid.uuid4()), status, old, resolved_at),
            )
        for key, status, resolved_at in (
            ("resolved_counterfactual", "resolved", old),
            ("open_counterfactual", "open", None),
        ):
            conn.execute(
                """
                INSERT INTO behavior_counterfactuals(
                    id, workspace_id, subject_user_id, owner_id, session_id, decision,
                    alternative, expected_outcome, status, created_at, resolved_at
                ) VALUES(?, 'behavior-test-workspace', 'behavior-test-user', 'behavior-test-user',
                         'retention', 'decision', 'alternative', 'outcome', ?, ?, ?)
                """,
                (ids[key], status, old, resolved_at),
            )
        for key, status in (("rejected_process", "rejected"), ("active_process", "active")):
            conn.execute(
                """
                INSERT INTO behavior_process_models(
                    id, workspace_id, subject_user_id, process_signature, name, status,
                    created_at, updated_at
                ) VALUES(?, 'behavior-test-workspace', 'behavior-test-user', ?,
                         'retention process', ?, ?, ?)
                """,
                (ids[key], key, status, old, old),
            )
        conn.commit()

        result = run_lifecycle_maintenance(conn, settings, retention_days=3650, dry_run=False)
        remaining = {
            table: {str(row[0]) for row in conn.execute(f"SELECT id FROM {table}").fetchall()}
            for table in (
                "capability_grants",
                "behavior_shadow_predictions",
                "behavior_memory_reviews",
                "behavior_counterfactuals",
                "behavior_process_models",
            )
        }
    finally:
        conn.close()

    assert result["behavior_control_rows_deleted"] == 5
    assert ids["grant"] not in remaining["capability_grants"]
    assert ids["shadow"] not in remaining["behavior_shadow_predictions"]
    assert remaining["behavior_memory_reviews"] == {ids["pending_review"]}
    assert remaining["behavior_counterfactuals"] == {ids["open_counterfactual"]}
    assert remaining["behavior_process_models"] == {ids["active_process"]}


def test_strict_workspace_access_requires_membership_for_executor(client: TestClient) -> None:
    settings = get_settings()
    settings.workspace_access_mode = "strict"
    headers = {**_headers(), "X-TCE-Role": "executor"}

    response = client.post(
        "/v1/behavior/predict",
        json={
            "situation_summary": "Choose a safe implementation",
            "objective": "Verify strict membership",
        },
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "workspace access denied"


def test_capped_evidence_load_uses_most_recent_records(client: TestClient) -> None:
    for index in range(3):
        response = client.post("/v1/behavior/evidence", json=_payload(index), headers=_headers())
        assert response.status_code == 200

    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = load_behavior_evidence_lite(
            conn,
            workspace_id="behavior-test-workspace",
            subject_user_id="behavior-test-user",
            limit=2,
            eligible_only=True,
        )
    finally:
        conn.close()
    assert [row["situation_summary"] for row in rows] == [
        "Choose scope for production bug 1",
        "Choose scope for production bug 2",
    ]


def test_full_and_lite_register_same_behavior_routes() -> None:
    from tce_api.main import app as full_app

    expected = {
        ("/v1/behavior/evidence", "POST"),
        ("/v1/behavior/predict", "POST"),
        ("/v1/behavior/evaluate", "POST"),
        ("/v1/behavior/evaluations", "GET"),
        ("/v1/behavior/calibration/scenarios", "GET"),
        ("/v1/behavior/calibration/answer", "POST"),
    }

    def routes(api) -> set[tuple[str, str]]:
        return {
            (route.path, method)
            for route in api.routes
            for method in (route.methods or set())
            if route.path.startswith("/v1/behavior/")
        }

    assert expected <= routes(app)
    assert expected <= routes(full_app)
