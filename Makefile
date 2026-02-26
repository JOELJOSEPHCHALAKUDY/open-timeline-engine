.PHONY: up down migrate api worker mcp lite-up lite-down lite-api lite-mcp lite-e2e start openapi git-bootstrap

up:
	docker compose -f infra/docker-compose.yml up --build

down:
	docker compose -f infra/docker-compose.yml down

migrate:
	alembic -c infra/alembic.ini upgrade head

api:
	PYTHONPATH=shared:services/tce_api uvicorn tce_api.main:app --host 0.0.0.0 --port 8080

worker:
	PYTHONPATH=shared:services/tce_worker python -m tce_worker.worker

mcp:
	PYTHONPATH=shared:services/tce_mcp python -m tce_mcp.server

lite-up:
	docker compose -f infra/docker-compose.lite.yml up --build

lite-down:
	docker compose -f infra/docker-compose.lite.yml down -v

lite-api:
	PYTHONPATH=shared:services/tce_lite_api uvicorn tce_lite_api.main:app --host 0.0.0.0 --port 8080

lite-mcp:
	PYTHONPATH=shared:services/tce_mcp python -m tce_mcp.server

lite-e2e:
	./scripts/e2e_lite_smoke.sh

start:
	./scripts/start.sh

openapi:
	PYTHONPATH=shared:services/tce_api:services/tce_lite_api python3 scripts/export_openapi.py

git-bootstrap:
	PYTHONPATH=shared:services/tce_api python3 scripts/import_git_history.py
