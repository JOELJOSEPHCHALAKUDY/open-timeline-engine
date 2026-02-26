# Architecture Overview

## Pipeline

1. Capture plugins emit canonical `EventEnvelope` payloads.
2. API performs auth, policy checks, redaction, and persistence.
3. Worker enriches data with embeddings and mined patterns.
4. MCP tools serve citation-backed context bundles.
5. Audit log tracks all context-serving actions.

## Services

- `tce-api`: ingest/search/bundle/pattern feedback
- `tce-lite-api`: lite runtime API with SQLite-backed storage
- `tce-worker`: embedding, compaction, pattern mining, workflow synthesis, validation
- `tce-mcp`: primary MCP executor service
- `tce-mcp-secondary`: additional MCP executor service
- `postgres`: source of truth
- `redis`: queue broker
- `ollama`: local model backend

## Runtime Modes

- `timeline_only`: event capture and retrieval only
- `clone_advisor`: enables second-AI advisory endpoints with loop guard and arbitration

## Data Governance

- Sensitivity levels 0-3
- Default policy blocks sensitivity 3 from outputs
- Redaction occurs before embedding and response
- Audit log records query and policy decisions
