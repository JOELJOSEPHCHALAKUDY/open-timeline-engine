FROM python:3.12-slim

WORKDIR /app
COPY shared /app/shared
COPY services/tce_worker /app/services/tce_worker
RUN pip install --no-cache-dir /app/shared /app/services/tce_worker

ENV PYTHONPATH=/app/shared:/app/services/tce_worker
CMD ["python", "-m", "tce_worker.worker"]
