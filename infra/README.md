# Infra

- `docker-compose.lite.yml`: separate lightweight stack (SQLite + API + MCP)
- `docker-compose.yml`: local production-like stack
  includes `tce-mcp` (executor) and `tce-mcp-secondary` (additional executor endpoint)
- `alembic/`: DB migrations
- `prometheus.yml`: metrics scrape config
- `grafana/dashboards`: dashboard provisioning
- `systemd/`: host install service units
