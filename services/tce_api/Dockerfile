# --- Stage 1: Build Angular dashboard ---
FROM node:20-slim AS dashboard-build
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY dashboard/ ./
RUN npx ng build --configuration production

# --- Stage 2: Python API ---
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY shared /app/shared
COPY infra /app/infra
COPY services/tce_api /app/services/tce_api
RUN pip install --no-cache-dir /app/shared /app/services/tce_api

# Copy Angular build output
COPY --from=dashboard-build /dashboard/dist/dashboard/browser /app/dashboard-static

ENV PYTHONPATH=/app/shared:/app/services/tce_api
CMD ["sh", "-c", "if [ -n \"$PROMETHEUS_MULTIPROC_DIR\" ]; then mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\" && rm -f \"$PROMETHEUS_MULTIPROC_DIR\"/*.db; fi; exec uvicorn tce_api.main:app --host 0.0.0.0 --port 8080 --workers ${TCE_API_WORKERS:-4}"]
