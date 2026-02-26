# TCE Four Features Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add alternative model gateways (OpenAI/Anthropic), LLM-assisted entity extraction, automatic contradiction tracking, and richer activity summaries to TCE.

**Architecture:** Bottom-up — build the gateway abstraction first so that entity extraction and contradiction tracking can use it. Activity summaries come last since they read data produced by the other features.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (raw SQL text), PostgreSQL (pgvector), openai SDK, anthropic SDK, pytest

**Test runner:** `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v` from the project root `<repo-root>`

---

## Task 1: Gateway Factory

**Files:**
- Create: `shared/tce_model_gateway/factory.py`
- Test: `tests/unit/test_gateway_factory.py`

**Step 1: Write the failing test**

Create `tests/unit/test_gateway_factory.py`:

```python
from __future__ import annotations

from tce_model_gateway import OllamaGateway
from tce_model_gateway.factory import create_gateway


class _FakeSettings:
    model_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    extract_model: str = "qwen2.5:7b"
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"
    openai_extract_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_extract_model: str = "claude-haiku-4-5-20251001"


def test_factory_returns_ollama_by_default() -> None:
    settings = _FakeSettings()
    gw = create_gateway(settings)
    assert isinstance(gw, OllamaGateway)


def test_factory_returns_openai_gateway() -> None:
    settings = _FakeSettings()
    settings.model_provider = "openai"
    settings.openai_api_key = "sk-test"
    gw = create_gateway(settings)
    from tce_model_gateway.openai_gateway import OpenAIGateway
    assert isinstance(gw, OpenAIGateway)


def test_factory_returns_anthropic_gateway() -> None:
    settings = _FakeSettings()
    settings.model_provider = "anthropic"
    settings.anthropic_api_key = "sk-ant-test"
    gw = create_gateway(settings)
    from tce_model_gateway.anthropic_gateway import AnthropicGateway
    assert isinstance(gw, AnthropicGateway)


def test_factory_raises_on_unknown_provider() -> None:
    settings = _FakeSettings()
    settings.model_provider = "unknown"
    try:
        create_gateway(settings)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_gateway_factory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tce_model_gateway.factory'`

**Step 3: Write OpenAIGateway**

Create `shared/tce_model_gateway/openai_gateway.py`:

```python
from __future__ import annotations

import json
from typing import Any, cast

from .gateway import ModelGateway


class OpenAIGateway(ModelGateway):
    def __init__(self, api_key: str, embed_model: str, extract_model: str, timeout: int = 30) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, timeout=timeout)
        self.embed_model = embed_model
        self.extract_model = extract_model

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(model=self.embed_model, input=text)
        return list(response.data[0].embedding)

    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.extract_model,
            messages=[{"role": "user", "content": f"Schema:{schema_name}\n{prompt}"}],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"raw": raw}
        except Exception:
            return {"raw": raw}
```

**Step 4: Write AnthropicGateway**

Create `shared/tce_model_gateway/anthropic_gateway.py`:

```python
from __future__ import annotations

import json
from typing import Any, cast

from .gateway import ModelGateway


class AnthropicGateway(ModelGateway):
    """Anthropic-backed gateway. Extraction-only — embed() raises NotImplementedError.

    Anthropic does not offer a first-party embedding API, so callers that need
    embeddings should pair this with a different provider or use Ollama for
    embeddings while using Anthropic for extraction.
    """

    def __init__(self, api_key: str, extract_model: str, timeout: int = 30) -> None:
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key, timeout=timeout)
        self.extract_model = extract_model

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError(
            "AnthropicGateway does not support embeddings. "
            "Use Ollama or OpenAI for embedding, and Anthropic for extraction only."
        )

    def extract_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        response = self.client.messages.create(
            model=self.extract_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": f"Schema:{schema_name}\n{prompt}\n\nRespond ONLY with valid JSON."}],
        )
        raw = response.content[0].text if response.content else "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"raw": raw}
        except Exception:
            return {"raw": raw}
```

**Step 5: Write the factory**

Create `shared/tce_model_gateway/factory.py`:

```python
from __future__ import annotations

from typing import Any

from .gateway import ModelGateway, OllamaGateway


def create_gateway(settings: Any) -> ModelGateway:
    provider = getattr(settings, "model_provider", "ollama").strip().lower()

    if provider == "ollama":
        return OllamaGateway(
            base_url=settings.ollama_url,
            embed_model=settings.embed_model,
            extract_model=settings.extract_model,
        )

    if provider == "openai":
        from .openai_gateway import OpenAIGateway

        return OpenAIGateway(
            api_key=settings.openai_api_key,
            embed_model=getattr(settings, "openai_embed_model", "text-embedding-3-small"),
            extract_model=getattr(settings, "openai_extract_model", "gpt-4o-mini"),
        )

    if provider == "anthropic":
        from .anthropic_gateway import AnthropicGateway

        return AnthropicGateway(
            api_key=settings.anthropic_api_key,
            extract_model=getattr(settings, "anthropic_extract_model", "claude-haiku-4-5-20251001"),
        )

    raise ValueError(f"Unknown model provider: {provider!r}. Use 'ollama', 'openai', or 'anthropic'.")
```

**Step 6: Update `__init__.py`**

Modify `shared/tce_model_gateway/__init__.py`:

```python
from .factory import create_gateway
from .gateway import ModelGateway, OllamaGateway

__all__ = ["ModelGateway", "OllamaGateway", "create_gateway"]
```

**Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_gateway_factory.py -v`
Expected: 4 passed

**Step 8: Commit**

```bash
git add shared/tce_model_gateway/ tests/unit/test_gateway_factory.py
git commit -m "feat: add gateway factory with OpenAI and Anthropic backends"
```

---

## Task 2: Add provider config to settings

**Files:**
- Modify: `services/tce_api/tce_api/config.py:9-42`
- Modify: `services/tce_worker/tce_worker/config.py:8-25`
- Modify: `.env.example`

**Step 1: Add provider settings to API config**

Add these fields to the `Settings` class in `services/tce_api/tce_api/config.py` after line 18 (`extract_model`):

```python
    model_provider: str = "ollama"
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"
    openai_extract_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_extract_model: str = "claude-haiku-4-5-20251001"
    llm_extraction: bool = False
```

**Step 2: Add provider settings to worker config**

Add the same fields to the `Settings` class in `services/tce_worker/tce_worker/config.py` after line 16 (`extract_model`):

```python
    model_provider: str = "ollama"
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"
    openai_extract_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_extract_model: str = "claude-haiku-4-5-20251001"
    llm_extraction: bool = False
```

**Step 3: Update `.env.example`**

Append after `TCE_EXTRACT_MODEL=qwen2.5:7b` (line 9):

```
TCE_MODEL_PROVIDER=ollama
TCE_OPENAI_API_KEY=
TCE_OPENAI_EMBED_MODEL=text-embedding-3-small
TCE_OPENAI_EXTRACT_MODEL=gpt-4o-mini
TCE_ANTHROPIC_API_KEY=
TCE_ANTHROPIC_EXTRACT_MODEL=claude-haiku-4-5-20251001
TCE_LLM_EXTRACTION=false
```

**Step 4: Run existing tests to verify nothing breaks**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all existing tests pass

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/config.py services/tce_worker/tce_worker/config.py .env.example
git commit -m "feat: add model provider and LLM extraction settings"
```

---

## Task 3: Replace OllamaGateway instantiation with factory

**Files:**
- Modify: `services/tce_api/tce_api/search.py:10,77`
- Modify: `services/tce_worker/tce_worker/jobs/embedding.py:7,27`
- Modify: `services/tce_worker/tce_worker/jobs/patterns.py:9,23`

**Step 1: Update search.py**

In `services/tce_api/tce_api/search.py`:
- Change line 10 import from `from tce_model_gateway import OllamaGateway` to `from tce_model_gateway import create_gateway`
- Change line 77 from `gateway = OllamaGateway(settings.ollama_url, settings.embed_model, settings.extract_model, timeout=8)` to `gateway = create_gateway(settings)`

**Step 2: Update embedding.py**

In `services/tce_worker/tce_worker/jobs/embedding.py`:
- Change line 7 import from `from tce_model_gateway import OllamaGateway` to `from tce_model_gateway import create_gateway`
- Change line 27 from `gateway = OllamaGateway(settings.ollama_url, settings.embed_model, settings.extract_model)` to `gateway = create_gateway(settings)`

**Step 3: Update patterns.py**

In `services/tce_worker/tce_worker/jobs/patterns.py`:
- Change line 9 import from `from tce_model_gateway import OllamaGateway` to `from tce_model_gateway import create_gateway`
- Change line 23 from `gateway = OllamaGateway(settings.ollama_url, settings.embed_model, settings.extract_model)` to `gateway = create_gateway(settings)`

**Step 4: Update test mocks**

In `tests/unit/test_worker_jobs.py`:
- Add `model_provider = "ollama"` to the `_Settings` class (after line 11)
- Change line 84 monkeypatch from `embedding, "OllamaGateway"` to `embedding, "create_gateway"` with a lambda returning `_FakeGateway()`
- Change line 110 monkeypatch from `patterns, "OllamaGateway"` to `patterns, "create_gateway"` with a lambda returning `_FakeGateway()`

**Step 5: Run all tests**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all pass

**Step 6: Commit**

```bash
git add services/tce_api/tce_api/search.py services/tce_worker/tce_worker/jobs/embedding.py services/tce_worker/tce_worker/jobs/patterns.py tests/unit/test_worker_jobs.py
git commit -m "refactor: replace direct OllamaGateway with create_gateway factory"
```

---

## Task 4: LLM Entity Extraction

**Files:**
- Modify: `services/tce_api/tce_api/graph.py:1-72,185-332`
- Test: `tests/unit/test_llm_entity_extraction.py`

**Step 1: Write the failing test**

Create `tests/unit/test_llm_entity_extraction.py`:

```python
from __future__ import annotations

import json
from tce_api.graph import llm_extract_entities


class _FakeGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        return {
            "entities": [
                {"name": "Joel", "type": "person", "confidence": 0.9},
                {"name": "PostgreSQL", "type": "technology", "confidence": 0.85},
                {"name": "Acme Corp", "type": "organization", "confidence": 0.7},
            ]
        }


class _FailingGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        raise RuntimeError("LLM unavailable")


def test_llm_extract_returns_entities() -> None:
    results = llm_extract_entities("Joel deployed PostgreSQL for Acme Corp", _FakeGateway())
    assert len(results) == 3
    assert results[0] == ("person", "joel", 0.9)
    assert results[1] == ("technology", "postgresql", 0.85)
    assert results[2] == ("organization", "acme corp", 0.7)


def test_llm_extract_returns_empty_on_failure() -> None:
    results = llm_extract_entities("some text", _FailingGateway())
    assert results == []


def test_llm_extract_clamps_confidence() -> None:
    class _HighConfGateway:
        def extract_structured(self, prompt, schema_name):
            return {"entities": [{"name": "X", "type": "concept", "confidence": 5.0}]}

    results = llm_extract_entities("text", _HighConfGateway())
    assert results[0][2] == 1.0
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_llm_entity_extraction.py -v`
Expected: FAIL — `ImportError: cannot import name 'llm_extract_entities' from 'tce_api.graph'`

**Step 3: Implement `llm_extract_entities`**

Add to `services/tce_api/tce_api/graph.py` after the imports (after line 11), add a logging import and the prompt constant + function:

```python
import logging

logger = logging.getLogger(__name__)

LLM_ENTITY_PROMPT = (
    "Extract named entities from the following text. "
    "Return a JSON object with key 'entities' containing an array. "
    "Each element: {\"name\": str, \"type\": str, \"confidence\": float 0-1}. "
    "Valid types: person, organization, technology, decision, error_type, concept, path, url, domain. "
    "Text: {text}"
)


def llm_extract_entities(
    text: str,
    gateway: Any,
) -> list[tuple[str, str, float]]:
    """Extract entities via LLM. Returns list of (type, key, confidence) tuples."""
    try:
        result = gateway.extract_structured(
            prompt=LLM_ENTITY_PROMPT.replace("{text}", text[:2000]),
            schema_name="entity_extraction_v1",
        )
    except Exception as exc:
        logger.warning("LLM entity extraction failed, falling back to regex-only: %s", exc)
        return []

    entities_raw = result.get("entities", [])
    if not isinstance(entities_raw, list):
        return []

    output: list[tuple[str, str, float]] = []
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        entity_type = item.get("type")
        confidence = item.get("confidence", 0.65)
        if not isinstance(name, str) or not isinstance(entity_type, str):
            continue
        name = name.strip().lower()
        entity_type = entity_type.strip().lower()
        if not name or not entity_type:
            continue
        confidence = max(0.0, min(1.0, float(confidence)))
        output.append((entity_type, name, confidence))
    return output
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_llm_entity_extraction.py -v`
Expected: 3 passed

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/graph.py tests/unit/test_llm_entity_extraction.py
git commit -m "feat: add llm_extract_entities function for LLM-based entity extraction"
```

---

## Task 5: Wire LLM extraction into index_event_graph

**Files:**
- Modify: `services/tce_api/tce_api/graph.py:185-332`

**Step 1: Add gateway and settings parameters to `index_event_graph`**

Update the `index_event_graph` function signature (line 185) to accept optional gateway and settings:

```python
def index_event_graph(
    db: Session,
    *,
    event_id: UUID,
    workspace_id: str,
    owner_id: str,
    domain: str,
    task_type: str,
    title: str,
    tags: list[str],
    context: dict[str, Any],
    payload: dict[str, Any],
    gateway: Any | None = None,
    llm_extraction: bool = False,
) -> dict[str, Any]:
```

**Step 2: After the regex entity loop (after line 266), add LLM extraction**

Insert after the `entity_count += 1` block:

```python
    # LLM-assisted entity extraction (opt-in)
    llm_entity_count = 0
    if llm_extraction and gateway is not None:
        text_for_llm = f"{title}\n{payload.get('summary', '')}"
        llm_entities = llm_extract_entities(text_for_llm, gateway)
        existing_keys = {(etype, ekey) for etype, ekey in entity_pairs}
        for entity_type, entity_key, confidence in llm_entities:
            if (entity_type, entity_key) in existing_keys:
                continue
            existing_keys.add((entity_type, entity_key))
            row = db.execute(
                text(
                    """
                    INSERT INTO entity_nodes (
                      id, workspace_id, owner_id, entity_type, entity_key, display_name, created_at, updated_at
                    )
                    VALUES (
                      :id, :workspace_id, :owner_id, :entity_type, :entity_key, :display_name, :ts, :ts
                    )
                    ON CONFLICT (workspace_id, owner_id, entity_type, entity_key)
                    DO UPDATE SET updated_at = excluded.updated_at
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "entity_type": entity_type,
                    "entity_key": entity_key,
                    "display_name": entity_key,
                    "ts": datetime.now(tz=UTC),
                },
            ).scalar_one()
            db.execute(
                text(
                    """
                    INSERT INTO event_entity_links (
                      id, workspace_id, owner_id, event_id, entity_id, role, confidence, created_at
                    )
                    VALUES (
                      :id, :workspace_id, :owner_id, :event_id, :entity_id, :role, :confidence, :created_at
                    )
                    ON CONFLICT (workspace_id, event_id, entity_id, role) DO NOTHING
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "event_id": event_id,
                    "entity_id": row,
                    "role": "mentioned",
                    "confidence": confidence,
                    "created_at": datetime.now(tz=UTC),
                },
            )
            llm_entity_count += 1
```

**Step 3: Update the return dict to include LLM entity count**

In the return statement (around line 328), add the field:

```python
    return {
        "entities_indexed": entity_count,
        "llm_entities_indexed": llm_entity_count,
        "sequence_relationship": str(sequence_relation_id) if sequence_relation_id else None,
        "conflict_relationships": [str(value) for value in conflict_ids],
    }
```

**Step 4: Run all tests**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all pass (existing callers don't set gateway/llm_extraction so behavior is unchanged)

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/graph.py
git commit -m "feat: wire LLM entity extraction into index_event_graph"
```

---

## Task 6: LLM-Derived Fact Extraction for Contradiction Tracking

**Files:**
- Modify: `services/tce_api/tce_api/graph.py`
- Test: `tests/unit/test_fact_extraction.py`

**Step 1: Write the failing test**

Create `tests/unit/test_fact_extraction.py`:

```python
from __future__ import annotations

from tce_api.graph import llm_extract_facts


class _FakeGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        return {
            "facts": [
                {"subject": "database", "predicate": "version", "object": "PostgreSQL 16", "confidence": 0.85},
                {"subject": "deploy_target", "predicate": "environment", "object": "production", "confidence": 0.9},
            ]
        }


class _FailingGateway:
    def extract_structured(self, prompt: str, schema_name: str) -> dict:
        raise RuntimeError("LLM down")


def test_llm_extract_facts_returns_tuples() -> None:
    results = llm_extract_facts("We deployed PostgreSQL 16 to production", _FakeGateway())
    assert len(results) == 2
    assert results[0] == ("database__version", "PostgreSQL 16", 0.85)
    assert results[1] == ("deploy_target__environment", "production", 0.9)


def test_llm_extract_facts_returns_empty_on_failure() -> None:
    results = llm_extract_facts("text", _FailingGateway())
    assert results == []
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_fact_extraction.py -v`
Expected: FAIL — `ImportError: cannot import name 'llm_extract_facts'`

**Step 3: Implement `llm_extract_facts`**

Add to `services/tce_api/tce_api/graph.py` after `llm_extract_entities`:

```python
LLM_FACT_PROMPT = (
    "Extract factual assertions from the following text. "
    "Return a JSON object with key 'facts' containing an array. "
    "Each element: {\"subject\": str, \"predicate\": str, \"object\": str, \"confidence\": float 0-1}. "
    "Only include assertions that state something concrete and verifiable. "
    "Text: {text}"
)


def llm_extract_facts(
    text: str,
    gateway: Any,
) -> list[tuple[str, str, float]]:
    """Extract facts via LLM. Returns list of (fact_key, fact_value, confidence) tuples.

    fact_key is '{subject}__{predicate}' to match the existing fact_assertions schema.
    """
    try:
        result = gateway.extract_structured(
            prompt=LLM_FACT_PROMPT.replace("{text}", text[:2000]),
            schema_name="fact_extraction_v1",
        )
    except Exception as exc:
        logger.warning("LLM fact extraction failed: %s", exc)
        return []

    facts_raw = result.get("facts", [])
    if not isinstance(facts_raw, list):
        return []

    output: list[tuple[str, str, float]] = []
    for item in facts_raw:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        predicate = item.get("predicate")
        obj = item.get("object")
        confidence = item.get("confidence", 0.65)
        if not all(isinstance(v, str) for v in (subject, predicate, obj)):
            continue
        subject = subject.strip()
        predicate = predicate.strip()
        obj = obj.strip()
        if not subject or not predicate or not obj:
            continue
        confidence = max(0.0, min(1.0, float(confidence)))
        fact_key = f"{subject}__{predicate}"
        output.append((fact_key, obj, confidence))
    return output
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_fact_extraction.py -v`
Expected: 2 passed

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/graph.py tests/unit/test_fact_extraction.py
git commit -m "feat: add llm_extract_facts for LLM-derived contradiction tracking"
```

---

## Task 7: Wire LLM facts into `_record_fact_assertions`

**Files:**
- Modify: `services/tce_api/tce_api/graph.py:75-182,319-326`

**Step 1: Extend `_record_fact_assertions` to accept LLM-derived facts**

Update the function signature at line 75 to accept an optional `derived_facts` parameter:

```python
def _record_fact_assertions(
    db: Session,
    event_id: UUID,
    workspace_id: str,
    owner_id: str,
    domain: str,
    payload: dict[str, Any],
    derived_facts: list[tuple[str, str, float]] | None = None,
) -> list[UUID]:
```

**Step 2: Merge derived facts into the facts list**

After the existing fact parsing logic (after line 98), add:

```python
    # Merge LLM-derived facts (with confidence)
    fact_confidences: dict[str, float] = {}
    if derived_facts:
        for fact_key, fact_value, confidence in derived_facts:
            if confidence > 0.5:
                facts.append((str(fact_key), str(fact_value)))
                fact_confidences[str(fact_key)] = confidence
```

**Step 3: Use per-fact confidence for contradiction edges**

In the contradiction edge creation (around line 150), replace the hardcoded `0.92` with:

```python
                    "confidence": fact_confidences.get(fact_key, 0.92),
```

**Step 4: Update the call in `index_event_graph`**

In the `index_event_graph` function, update the `_record_fact_assertions` call (around line 319) to pass derived facts:

```python
    # LLM-derived fact extraction (opt-in)
    derived_facts: list[tuple[str, str, float]] | None = None
    if llm_extraction and gateway is not None:
        fact_text = f"{title}\n{payload.get('summary', '')}"
        derived_facts = llm_extract_facts(fact_text, gateway)

    conflict_ids = _record_fact_assertions(
        db=db,
        event_id=event_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        domain=domain,
        payload=payload,
        derived_facts=derived_facts,
    )
```

**Step 5: Run all tests**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all pass

**Step 6: Commit**

```bash
git add services/tce_api/tce_api/graph.py
git commit -m "feat: wire LLM-derived facts into contradiction tracking pipeline"
```

---

## Task 8: Richer Activity Summaries — Schema

**Files:**
- Modify: `services/tce_api/tce_api/schemas.py:68-79`
- Modify: `services/tce_lite_api/tce_lite_api/types.py:71-82` (keep in sync)

**Step 1: Extend `ActivitySummaryResponse` in schemas.py**

Add new optional fields after `citations` (line 78):

```python
class ActivitySummaryResponse(BaseModel):
    period: str
    start_ts: datetime
    end_ts: datetime
    total_events: int
    by_domain: dict[str, int]
    by_task_type: dict[str, int]
    by_event_type: dict[str, int]
    highlights: list[str]
    summary: str
    citations: list[UUID]
    policy: dict[str, Any]
    hourly_buckets: dict[str, int] = {}
    outcome_metrics: dict[str, int] = {}
    top_errors: list[str] = []
    top_entities: list[dict[str, Any]] = []
    contradiction_count: int = 0
```

**Step 2: Update the lite API types.py to match**

Add the same 5 new fields to `services/tce_lite_api/tce_lite_api/types.py:71-82`.

**Step 3: Run all tests**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all pass (new fields have defaults, so existing code doesn't break)

**Step 4: Commit**

```bash
git add services/tce_api/tce_api/schemas.py services/tce_lite_api/tce_lite_api/types.py
git commit -m "feat: extend ActivitySummaryResponse with hourly buckets, outcomes, errors, entities, contradictions"
```

---

## Task 9: Richer Activity Summaries — Endpoint Logic

**Files:**
- Modify: `services/tce_api/tce_api/main.py:645-734`
- Test: `tests/unit/test_activity_summary.py`

**Step 1: Write the failing test**

Create `tests/unit/test_activity_summary.py`:

```python
from __future__ import annotations

from collections import Counter
from tce_api.main import _compute_hourly_buckets, _compute_outcome_metrics, _compute_top_errors


class _FakeRow:
    def __init__(self, ts, payload=None):
        self.ts = ts
        self.payload = payload or {}


def test_hourly_buckets_groups_by_hour() -> None:
    from datetime import datetime, UTC
    rows = [
        _FakeRow(ts=datetime(2026, 2, 19, 10, 15, tzinfo=UTC)),
        _FakeRow(ts=datetime(2026, 2, 19, 10, 45, tzinfo=UTC)),
        _FakeRow(ts=datetime(2026, 2, 19, 11, 5, tzinfo=UTC)),
    ]
    buckets = _compute_hourly_buckets(rows)
    assert buckets["2026-02-19T10:00:00"] == 2
    assert buckets["2026-02-19T11:00:00"] == 1


def test_outcome_metrics_counts_payload_outcome() -> None:
    rows = [
        _FakeRow(ts=None, payload={"outcome": "success"}),
        _FakeRow(ts=None, payload={"outcome": "success"}),
        _FakeRow(ts=None, payload={"outcome": "failure"}),
        _FakeRow(ts=None, payload={}),
    ]
    metrics = _compute_outcome_metrics(rows)
    assert metrics == {"success": 2, "failure": 1}


def test_top_errors_extracts_error_messages() -> None:
    rows = [
        _FakeRow(ts=None, payload={"error": "ConnectionTimeout"}),
        _FakeRow(ts=None, payload={"error": "ConnectionTimeout"}),
        _FakeRow(ts=None, payload={"error": "NullPointerException"}),
        _FakeRow(ts=None, payload={}),
    ]
    errors = _compute_top_errors(rows, max_errors=2)
    assert errors == ["ConnectionTimeout", "NullPointerException"]
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_activity_summary.py -v`
Expected: FAIL — cannot import the helper functions

**Step 3: Implement helper functions**

Add these helper functions to `services/tce_api/tce_api/main.py` before the `activity_summary` endpoint (before line 645):

```python
def _compute_hourly_buckets(rows: list) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for row in rows:
        if row.ts is None:
            continue
        hour_key = row.ts.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        buckets[hour_key] = buckets.get(hour_key, 0) + 1
    return dict(sorted(buckets.items()))


def _compute_outcome_metrics(rows: list) -> dict[str, int]:
    counter: dict[str, int] = {}
    for row in rows:
        outcome = row.payload.get("outcome") if isinstance(row.payload, dict) else None
        if isinstance(outcome, str) and outcome.strip():
            key = outcome.strip().lower()
            counter[key] = counter.get(key, 0) + 1
    return counter


def _compute_top_errors(rows: list, max_errors: int = 3) -> list[str]:
    error_counter: dict[str, int] = {}
    for row in rows:
        error = row.payload.get("error") if isinstance(row.payload, dict) else None
        if isinstance(error, str) and error.strip():
            key = error.strip()
            error_counter[key] = error_counter.get(key, 0) + 1
    sorted_errors = sorted(error_counter.items(), key=lambda x: x[1], reverse=True)
    return [err for err, _count in sorted_errors[:max_errors]]
```

**Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/unit/test_activity_summary.py -v`
Expected: 3 passed

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/main.py tests/unit/test_activity_summary.py
git commit -m "feat: add hourly_buckets, outcome_metrics, top_errors helper functions"
```

---

## Task 10: Wire summary helpers into the activity endpoint

**Files:**
- Modify: `services/tce_api/tce_api/main.py:645-734`

**Step 1: Add entity hotspot and contradiction queries**

After the existing event loop in the activity_summary endpoint (after line 699), add:

```python
    # Hourly distribution
    hourly_buckets = _compute_hourly_buckets([r for r in rows if r.id in set(citations)])
    outcome_metrics = _compute_outcome_metrics([r for r in rows if r.id in set(citations)])
    top_errors = _compute_top_errors([r for r in rows if r.id in set(citations)])

    # Top entities for the period
    top_entities: list[dict[str, Any]] = []
    if citations:
        try:
            entity_rows = db.execute(
                text(
                    """
                    SELECT en.entity_key, en.entity_type, COUNT(*) as ref_count
                    FROM event_entity_links eel
                    JOIN entity_nodes en ON en.id = eel.entity_id
                    WHERE eel.event_id IN :event_ids
                      AND eel.workspace_id = :workspace_id
                    GROUP BY en.entity_key, en.entity_type
                    ORDER BY ref_count DESC
                    LIMIT 10
                    """
                ).bindparams(bindparam("event_ids", expanding=True)),
                {"event_ids": citations[:200], "workspace_id": auth.workspace_id},
            ).mappings().all()
            top_entities = [
                {"key": r["entity_key"], "type": r["entity_type"], "count": int(r["ref_count"])}
                for r in entity_rows
            ]
        except Exception as exc:
            logger.warning("entity hotspot query failed: %s", exc)

    # Contradiction count
    contradiction_count = 0
    if citations:
        try:
            row = db.execute(
                text(
                    """
                    SELECT COUNT(*) as cnt
                    FROM event_relationships
                    WHERE relationship_type = 'contradicts'
                      AND workspace_id = :workspace_id
                      AND source_event_id IN :event_ids
                    """
                ).bindparams(bindparam("event_ids", expanding=True)),
                {"workspace_id": auth.workspace_id, "event_ids": citations[:200]},
            ).mappings().first()
            contradiction_count = int(row["cnt"]) if row else 0
        except Exception as exc:
            logger.warning("contradiction count query failed: %s", exc)
```

**Step 2: Add `bindparam` import if not present**

Check that `from sqlalchemy import bindparam` is imported at the top of `main.py`. If not, add it.

**Step 3: Pass new fields to the response**

Update the `ActivitySummaryResponse` constructor (around line 722) to include:

```python
    return ActivitySummaryResponse(
        period=period,
        start_ts=start_ts,
        end_ts=end_ts,
        total_events=total,
        by_domain=dict(domain_counter),
        by_task_type=dict(task_counter),
        by_event_type=dict(event_type_counter),
        highlights=highlights,
        summary=summary,
        citations=citations,
        policy=policy_summary,
        hourly_buckets=hourly_buckets,
        outcome_metrics=outcome_metrics,
        top_errors=top_errors,
        top_entities=top_entities,
        contradiction_count=contradiction_count,
    )
```

**Step 4: Run all tests**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all pass

**Step 5: Commit**

```bash
git add services/tce_api/tce_api/main.py
git commit -m "feat: wire hourly buckets, outcomes, errors, entities, contradictions into activity summary"
```

---

## Task 11: Final integration test and rebuild

**Step 1: Run the full test suite**

Run: `PYTHONPATH=shared:services/tce_api:services/tce_worker pytest tests/ -v`
Expected: all pass

**Step 2: Rebuild the Docker containers**

Run from `open-timeline-engine/infra/`:
```bash
docker compose build tce-api tce-worker && docker compose up -d tce-api tce-worker
```

**Step 3: Verify the API starts cleanly**

Run: `docker compose logs tce-api --tail=20`
Expected: no import errors, API started

**Step 4: Commit any remaining changes**

```bash
git add -A
git commit -m "chore: finalize four features implementation"
```
