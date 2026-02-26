# TCE Four Features Design

**Date:** 2026-02-19
**Status:** Approved
**Implementation order:** Model Gateways → LLM Entity Extraction → Contradiction Tracking → Activity Summaries

---

## 1. Alternative Model Gateways

**Goal:** Support OpenAI and Anthropic alongside existing Ollama backend.

**Architecture:**
- Keep abstract `ModelGateway` interface (`embed()` + `extract_structured()`) unchanged
- Add `OpenAIGateway` using `text-embedding-3-small` (1536-dim) + `gpt-4o-mini` for extraction
- Add `AnthropicGateway` using Voyage embeddings (or skip embed, extraction-only via Claude)
- Add `gateway_factory(settings) -> ModelGateway` to replace inline `OllamaGateway()` instantiation

**Embedding dimension handling:**
- Add `embedding_dimensions` to gateway settings per provider
- Database migration: alter `event_embeddings.embedding` to use configurable dimension via a new column or re-create with the configured size
- Re-embed existing events when switching providers (worker job)

**Config:**
- `TCE_MODEL_PROVIDER=ollama|openai|anthropic` (default: `ollama`)
- Provider-specific: `TCE_OPENAI_API_KEY`, `TCE_ANTHROPIC_API_KEY`
- Model names: `TCE_OPENAI_EMBED_MODEL`, `TCE_OPENAI_EXTRACT_MODEL`, etc.

**New files:**
- `shared/tce_model_gateway/openai_gateway.py`
- `shared/tce_model_gateway/anthropic_gateway.py`
- `shared/tce_model_gateway/factory.py`

**Modified files:**
- `shared/tce_model_gateway/__init__.py` — export factory
- `services/tce_api/tce_api/config.py` — new settings
- `services/tce_worker/tce_worker/config.py` — new settings
- `services/tce_api/tce_api/search.py` — use factory
- `services/tce_worker/tce_worker/jobs/embedding.py` — use factory
- `services/tce_worker/tce_worker/jobs/patterns.py` — use factory
- `infra/.env.example` — document new vars

---

## 2. LLM-Assisted Entity Extraction

**Goal:** Extract richer entity types beyond regex (person, organization, technology, decision, error_type).

**Architecture:**
- Hybrid: keep regex as fast first-pass, optionally run LLM via `ModelGateway.extract_structured()`
- New function `llm_extract_entities(text, gateway) -> list[Entity]` in `graph.py`
- Called after regex pass in `index_event_graph()` when `TCE_LLM_EXTRACTION=true`
- Deduplicates against regex results (same name + type → keep higher confidence)
- Prompt template as constant in `graph.py` — asks for JSON array of `{name, type, confidence}`
- Falls back to regex-only if gateway call fails (log warning, no crash)

**New entity types:** `person`, `organization`, `technology`, `decision`, `error_type`
**Confidence:** LLM-provided (0.0–1.0) instead of hardcoded 0.65

**Config:**
- `TCE_LLM_EXTRACTION=true|false` (default: `false`)

**Modified files:**
- `shared/tce_shared/graph.py` — add `llm_extract_entities()`, call from `index_event_graph()`
- `shared/tce_shared/config.py` — add `llm_extraction` setting

---

## 3. Contradiction Tracking

**Goal:** Automatically derive facts from event content and detect conflicts, not just explicit `payload.facts`.

**Architecture:**
- Two-layer approach:
  1. **Explicit facts** (existing) — `payload.facts` dict, unchanged
  2. **Derived facts** (new) — LLM extracts `{subject, predicate, object, confidence}` tuples from event content/summary

- Contradiction detection:
  - After extracting facts, query `fact_assertions` for existing assertions with same `subject + predicate` but different `object`
  - If both confidences > 0.5, create "contradicts" relationship
  - Set `supersedes_event_id` on newer assertion

- New function `extract_and_check_facts(event, gateway) -> list[Contradiction]` in `graph.py`
- Gated by `TCE_LLM_EXTRACTION` flag (same as entity extraction)
- Falls back to explicit-facts-only if LLM unavailable

**Modified files:**
- `shared/tce_shared/graph.py` — add `extract_and_check_facts()`, integrate into `index_event_graph()`

---

## 4. Activity Summaries

**Goal:** Richer analytics beyond flat event counts.

**New response fields:**
- **Temporal buckets** — events grouped into hourly bins (`date_trunc('hour', created_at)`)
- **Outcome metrics** — counts by `payload.outcome` (success/failure/partial)
- **Error clustering** — top 3 error patterns from events with `payload.error` or `error_type` entities
- **Entity hotspots** — top 10 most-referenced entities in the period
- **Contradiction count** — number of fact contradictions detected in the period

**Implementation:**
- Extend existing SQL queries in the activity summary endpoint (no new tables)
- Add fields to `ActivitySummaryResponse` schema
- Error clustering uses string similarity on error messages (no LLM)

**Modified files:**
- `services/tce_api/tce_api/main.py` — extend activity summary endpoint
- `shared/tce_shared/schemas.py` — extend `ActivitySummaryResponse`

---

## Dependencies

```
Model Gateways (1) ──→ LLM Entity Extraction (2) ──→ Contradiction Tracking (3)
                                                   └──→ Activity Summaries (4)
```

Feature 2 and 3 depend on Feature 1 (need gateway for LLM calls).
Feature 4 depends on 2 and 3 for entity hotspots and contradiction counts.
