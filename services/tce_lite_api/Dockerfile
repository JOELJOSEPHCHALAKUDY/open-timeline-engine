FROM node:20-slim AS dashboard-build
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY dashboard/ ./
RUN npx ng build --configuration production

FROM python:3.12-slim

WORKDIR /app
COPY shared /app/shared
COPY services/tce_lite_api /app/services/tce_lite_api
RUN pip install --no-cache-dir /app/shared /app/services/tce_lite_api
COPY --from=dashboard-build /dashboard/dist/dashboard/browser /app/dashboard-static

ENV PYTHONPATH=/app/shared:/app/services/tce_lite_api
CMD ["uvicorn", "tce_lite_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
