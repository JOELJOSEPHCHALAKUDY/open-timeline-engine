from __future__ import annotations

from uuid import uuid4

from tce_worker.jobs import compaction, embedding, patterns, validation, workflow


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


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

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
    def __init__(self, responses: list[_Result]):
        self._responses = list(responses)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _query, _params=None):
        if self._responses:
            return self._responses.pop(0)
        return _Result(rows=[])

    def commit(self):
        self.committed = True

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
