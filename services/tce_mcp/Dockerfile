FROM python:3.12-slim

WORKDIR /app
COPY shared /app/shared
COPY services/tce_mcp /app/services/tce_mcp
RUN pip install --no-cache-dir /app/shared /app/services/tce_mcp

ENV PYTHONPATH=/app/shared:/app/services/tce_mcp
CMD ["python", "-m", "tce_mcp.server"]
