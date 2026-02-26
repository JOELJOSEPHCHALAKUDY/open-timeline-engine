# Threat Model

## Assets

- Timeline events
- Pattern library and workflows
- Context bundles served to agents
- API credentials and optional mTLS identities

## Threats

- Secret leakage via payloads or embeddings
- Over-broad context retrieval
- Unauthorized consumer access
- Plugin abuse collecting outside opted scopes

## Controls

- ABAC policy enforcement
- Sensitivity-based filtering
- Regex and hint redaction
- Audit logs for every context-serving action
- Optional app-level payload encryption
