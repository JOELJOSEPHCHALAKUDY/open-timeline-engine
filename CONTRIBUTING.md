# Contributing

## Development

- Python 3.12+
- Node.js 20+
- Docker + Docker Compose

## Standards

- Keep API and MCP schemas versioned and backward-compatible for additive updates.
- Add tests for policy, redaction, and schema validation with every behavioral change.
- Do not emit sensitivity `3` data from API or MCP outputs.

## Pull Requests

- Include migration notes if DB schema changes.
- Include docs updates for public interfaces.
- Include security considerations for capture/plugin changes.
