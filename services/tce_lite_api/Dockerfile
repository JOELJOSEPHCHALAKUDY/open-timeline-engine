FROM python:3.12-slim

WORKDIR /app
COPY shared /app/shared
COPY services/tce_lite_api /app/services/tce_lite_api
RUN pip install --no-cache-dir /app/services/tce_lite_api

ENV PYTHONPATH=/app/shared:/app/services/tce_lite_api
CMD ["uvicorn", "tce_lite_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
