from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tce_worker.jobs import compaction, decision_extraction, embedding, patterns, validation, workflow


class _Settings:
    model_provider = "ollama"
    ollama_url = "http://localhost:11434"
    embed_model = "nomic-embed-text"
    extract_model = "qwen2.5:7b"
    redis_url = "redis://localhost:6379/0"
    pattern_min_frequency = 3
    pattern_min_confidence = 0.58
    worker_retry_max = 1
    retry_intervals = [1]
    confidence_weights = (0.35, 0.30, 0.20, 0.15)
    decision_extraction_enabled = True
    decision_extraction_batch_size = 100
    decision_extraction_lease_seconds = 300
    decision_extraction_max_attempts = 10
    capture_opportunity_ttl_seconds = 3600
    behavior_storage_min_score = 0.55
    security_encryption_secret = ""


class _Result:
    def __init__(self, *, scalar=None, rows=None, rowcount=None):
        self._scalar = scalar
        self._rows = rows or []
        self.rowcount = len(self._rows) if rowcount is None else rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows

    def first(self):
        if not self._rows:
            return None
        return self._rows[0]

    def mappings(self):
        return self


class _FakeSession:
    def __init__(self, responses: list[_Result | Exception]):
        self._responses = list(responses)
        self.committed = False
        self.rolled_back = False
        self.executed: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _query, _params=None):
        self.executed.append((str(_query), dict(_params or {})))
        if self._responses:
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _Result(rows=[])

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def add(self, _obj):
        return None

    def flush(self):
        return None

    def refresh(self, _obj):
        return None


class _FakeGateway:
    def __init__(self, *_args, **_kwargs):
        pass

    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    def extract_structured(self, *, prompt: str, schema_name: str):
        _ = (prompt, schema_name)
        return {"statement": "Fallback pattern"}


class _FakeQueue:
    def __init__(self):
        self.enqueued: list[tuple] = []

    def enqueue(self, *args, **kwargs):
        self.enqueued.append((args, kwargs))
        return None


def test_embedding_job_missing_event(monkeypatch) -> None:
    monkeypatch.setattr(embedding, "get_settings", lambda: _Settings())
    monkeypatch.setattr(embedding, "create_gateway", lambda _settings: _FakeGateway())
    monkeypatch.setattr(embedding, "SessionLocal", lambda: _FakeSession([_Result(scalar=None)]))
    result = embedding.run(str(uuid4()))
    assert result["status"] == "missing"


def test_validation_job_missing_pattern(monkeypatch) -> None:
    monkeypatch.setattr(validation, "SessionLocal", lambda: _FakeSession([_Result(scalar=None)]))
    result = validation.run(str(uuid4()))
    assert result["status"] == "missing"


def test_workflow_job_missing_pattern(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "SessionLocal", lambda: _FakeSession([_Result(scalar=None)]))
    result = workflow.run(str(uuid4()))
    assert result["status"] == "missing"


def test_compaction_job_rejects_invalid_window() -> None:
    result = compaction.run("2026-02-18T12:00:00+00:00", "2026-02-18T11:00:00+00:00")
    assert result["status"] == "invalid_window"


def test_pattern_mining_job_handles_empty_window(monkeypatch) -> None:
    fake_queue = _FakeQueue()
    monkeypatch.setattr(patterns, "get_settings", lambda: _Settings())
    monkeypatch.setattr(patterns, "create_gateway", lambda _settings: _FakeGateway())
    monkeypatch.setattr(patterns.Redis, "from_url", lambda _url: object())
    monkeypatch.setattr(patterns, "Queue", lambda *_args, **_kwargs: fake_queue)
    monkeypatch.setattr(patterns, "SessionLocal", lambda: _FakeSession([_Result(rows=[])]))

    result = patterns.run(domain="coding", window_days=7)
    assert result["status"] == "ok"
    assert result["generated"] == 0
    assert result["updated"] == 0
    assert fake_queue.enqueued == []


def test_pattern_feedback_signal_prefers_positive() -> None:
    db = _FakeSession(
        [
            _Result(rows=[{"positive_feedback": 3, "negative_feedback": 1}]),
            _Result(rows=[{"positive_outcome": 4, "negative_outcome": 0}]),
        ]
    )
    signal = patterns._pattern_feedback_signal(db, event_ids=[uuid4()])
    assert signal > 0.5


def test_pattern_feedback_signal_prefers_negative() -> None:
    db = _FakeSession(
        [
            _Result(rows=[{"positive_feedback": 0, "negative_feedback": 2}]),
            _Result(rows=[{"positive_outcome": 0, "negative_outcome": 3}]),
        ]
    )
    signal = patterns._pattern_feedback_signal(db, event_ids=[uuid4()])
    assert signal < 0.5


# --------------------------------------------------------------------------- decision extraction (P1 §6)


def _capture_receipt_row(*, receipt_id: UUID, event_id: UUID, observed_at: datetime) -> dict:
    return {
        "id": receipt_id,
        "workspace_id": "personal",
        "owner_id": "human-1",
        "subject_user_id": "human-1",
        "host_session_id": "claude-session",
        "event_id": event_id,
        "origin_kind": "human_input",
        "capture_principal": "host:host-capture-claude",
        "project_id": None,
        "observed_at": observed_at,
        "content_sha256": "ab" * 32,
        "extraction_attempts": 1,
    }


def _opportunity_row(*, opportunity_id: UUID, shadow_id: UUID, created_at: datetime, alternatives: list[str]) -> dict:
    return {
        "id": opportunity_id,
        "session_id": "codex",
        "turn": 1,
        "objective_hash": "obj-1",
        "task_id": None,
        "project_id": None,
        "decision_family": "safety_confirmation",
        "situation_type": "approval_requested",
        "question_text": "Safety pause: high-risk action detected",
        "alternatives_json": alternatives,
        "pre_answer_snapshot_json": {"objective": "delete the build artifacts"},
        "evidence_revision": "rev-1",
        "shadow_prediction_id": shadow_id,
        "status": "open",
        "expires_at": created_at + timedelta(hours=1),
        "created_at": created_at,
        "frozen_at": created_at,
        "resolved_at": None,
    }


def _sql_index(session: _FakeSession, fragment: str) -> int:
    return next(index for index, (query, _params) in enumerate(session.executed) if fragment in query)


def _run_confirm_scenario(monkeypatch, *, frozen_at: datetime, observed_at: datetime):
    receipt_id, event_id, opportunity_id, shadow_id, candidate_id, observation_id = (uuid4() for _ in range(6))
    session = _FakeSession(
        [
            _Result(rows=[_capture_receipt_row(receipt_id=receipt_id, event_id=event_id, observed_at=observed_at)]),
            _Result(rows=[{"payload": {"input_excerpt": "confirm"}, "context": {}}]),
            _Result(rows=[_opportunity_row(opportunity_id=opportunity_id, shadow_id=shadow_id, created_at=observed_at - timedelta(minutes=5), alternatives=["confirm", "abort"])]),
            _Result(rows=[]),
            _Result(rows=[{"id": candidate_id}]),
            _Result(rows=[{"id": shadow_id, "frozen_at": frozen_at, "predicted_choice": "confirm", "abstained": False}]),
        ]
    )
    saved: list[dict] = []

    def fake_save(_db, **kwargs):
        saved.append({**kwargs, "executed_before": len(session.executed)})
        return observation_id

    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    monkeypatch.setattr(decision_extraction, "save_behavior_evidence", fake_save)
    result = decision_extraction.run(str(receipt_id))
    return result, session, saved, {"receipt_id": receipt_id, "event_id": event_id, "opportunity_id": opportunity_id, "shadow_id": shadow_id, "observation_id": observation_id}


def test_decision_extraction_missing_receipt(monkeypatch) -> None:
    session = _FakeSession([_Result(rows=[]), _Result(rows=[])])
    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    result = decision_extraction.run(str(uuid4()))
    assert result["status"] == "missing"
    assert result["processed"] == 0


def test_decision_extraction_skips_when_already_claimed(monkeypatch) -> None:
    session = _FakeSession([_Result(rows=[]), _Result(rows=[{"id": uuid4()}])])
    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    result = decision_extraction.run(str(uuid4()))
    assert result["status"] == "skipped"
    assert result["reason"] == "already_claimed"
    assert result["skipped"] == 1


def test_decision_extraction_promotes_unique_confirm(monkeypatch) -> None:
    observed_at = datetime.now(tz=UTC)
    result, session, saved, ids = _run_confirm_scenario(monkeypatch, frozen_at=observed_at - timedelta(minutes=5), observed_at=observed_at)

    assert result["status"] == "ok"
    assert result["processed"] == 1
    assert result["promoted"] == 1
    assert session.committed is True
    candidate_at = _sql_index(session, "INSERT INTO decision_candidates")
    shadow_select_at = _sql_index(session, "SELECT id, frozen_at, predicted_choice, abstained")
    resolution_at = _sql_index(session, "INSERT INTO human_resolutions")
    shadow_update_at = _sql_index(session, "UPDATE behavior_shadow_predictions")
    receipt_done_at = _sql_index(session, "extraction_state = 'extracted'")
    assert candidate_at < shadow_select_at < saved[0]["executed_before"] <= resolution_at < shadow_update_at < receipt_done_at

    evidence = saved[0]["evidence"]
    assert evidence["evidence_source"] == "explicit"
    assert evidence["confirmed_at"] is not None
    assert evidence["origin_kind"] == "human_input"
    assert evidence["capture_receipt_id"] == ids["receipt_id"]
    assert evidence["opportunity_id"] == ids["opportunity_id"]
    assert evidence["extraction_version"] == "dc-v1"
    assert evidence["selected_choice"] == "confirm"
    assert str(ids["event_id"]) in evidence["source_event_ids"]
    assert saved[0]["storage_gate"]["learning_eligible"] is True
    assert saved[0]["consumer_id"] == "extraction:host:host-capture-claude"

    shadow_sql, shadow_params = session.executed[shadow_update_at]
    assert "resolution_state = 'resolved'" in shadow_sql
    assert shadow_params["actual_choice"] == "confirm"
    assert shadow_params["correct"] is True
    assert shadow_params["retro"] is False
    assert shadow_params["observation_id"] == ids["observation_id"]
    done_sql, done_params = session.executed[receipt_done_at]
    assert done_params["version"] == "dc-v1"


def test_decision_extraction_labels_retrospective_when_frozen_after_answer(monkeypatch) -> None:
    observed_at = datetime.now(tz=UTC)
    result, session, _saved, _ids = _run_confirm_scenario(monkeypatch, frozen_at=observed_at + timedelta(seconds=1), observed_at=observed_at)
    assert result["promoted"] == 1
    _sql, params = session.executed[_sql_index(session, "UPDATE behavior_shadow_predictions")]
    assert params["retro"] is True
    assert "prediction_stage = CASE WHEN" in _sql


def test_decision_extraction_marks_failed_with_backoff(monkeypatch) -> None:
    observed_at = datetime.now(tz=UTC)
    receipt_id, event_id = uuid4(), uuid4()
    session = _FakeSession(
        [
            _Result(rows=[_capture_receipt_row(receipt_id=receipt_id, event_id=event_id, observed_at=observed_at)]),
            _Result(rows=[{"payload": {"input_excerpt": "confirm"}, "context": {}}]),
            _Result(rows=[_opportunity_row(opportunity_id=uuid4(), shadow_id=uuid4(), created_at=observed_at - timedelta(minutes=5), alternatives=["confirm", "abort"])]),
            _Result(rows=[]),
            RuntimeError("boom"),
        ]
    )
    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    monkeypatch.setattr(decision_extraction, "save_behavior_evidence", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not save")))
    result = decision_extraction.run(str(receipt_id))
    assert result["status"] == "ok"
    assert result["failed"] == 1
    assert result["promoted"] == 0
    assert session.rolled_back is True
    assert session.committed is True
    failed_sql, failed_params = session.executed[_sql_index(session, "extraction_state = 'failed'")]
    assert "next_extraction_at" in failed_sql
    assert failed_params["next_extraction_at"] > observed_at
    assert "boom" in failed_params["error"]


def test_decision_extraction_discards_generic_ack_with_two_open(monkeypatch) -> None:
    observed_at = datetime.now(tz=UTC)
    receipt_id, event_id, candidate_id = uuid4(), uuid4(), uuid4()
    session = _FakeSession(
        [
            _Result(rows=[_capture_receipt_row(receipt_id=receipt_id, event_id=event_id, observed_at=observed_at)]),
            _Result(rows=[{"payload": {"input_excerpt": "ok"}, "context": {}}]),
            _Result(
                rows=[
                    _opportunity_row(opportunity_id=uuid4(), shadow_id=uuid4(), created_at=observed_at - timedelta(minutes=5), alternatives=["confirm", "abort"]),
                    _opportunity_row(opportunity_id=uuid4(), shadow_id=uuid4(), created_at=observed_at - timedelta(minutes=2), alternatives=[]),
                ]
            ),
            _Result(rows=[]),
            _Result(rows=[{"id": candidate_id}]),
        ]
    )
    saved: list[dict] = []

    def fake_save(_db, **kwargs):
        saved.append(kwargs)
        return uuid4()

    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    monkeypatch.setattr(decision_extraction, "save_behavior_evidence", fake_save)
    result = decision_extraction.run(str(receipt_id))
    assert result["status"] == "ok"
    assert result["discarded"] == 1
    assert result["promoted"] == 0
    assert saved == []
    _sql, params = session.executed[_sql_index(session, "INSERT INTO decision_candidates")]
    assert params["promotion"] == "discard"
    assert params["status"] == "discarded"
    assert params["candidate_kind"] == "acknowledgement"
    assert not any("INSERT INTO human_resolutions" in query for query, _ in session.executed)
    assert any("extraction_state = 'extracted'" in query for query, _ in session.executed)
